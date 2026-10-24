"""Opt-in CUDA/weight/archived-frame parity through the actual WS service.

Set GRASP_REFERENCE_BACKEND and GRASP_PARITY_RUN to the original backend and
offline run directory. GRASP_PARITY_URI instead uses an existing image service.
No camera, ROS graph, robot or gripper is accessed.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import numpy as np
import pytest

from grasp_protocol.client import WsGraspClient
from grasp_protocol import MAX_MESSAGE_BYTES
from grasp_server.server import load_service


@pytest.fixture(scope="module")
def reference_service(tmp_path_factory):
    source = os.environ.get("GRASP_REFERENCE_BACKEND")
    run = os.environ.get("GRASP_PARITY_RUN")
    uri = os.environ.get("GRASP_PARITY_URI")
    if uri and run:
        return None, Path(run), uri
    if not source or not run:
        pytest.skip("set GRASP_REFERENCE_BACKEND and GRASP_PARITY_RUN for archived CUDA parity")
    service, _ = load_service(Path(__file__).resolve().parents[3] / "config/grasp/server.yaml",
                              models_root_override=Path(source) / "models", reference_backend_override=source,
                              archive_root_override=tmp_path_factory.mktemp("reference-archives"))
    assert service.hello()["backends"] == ["contact_graspnet", "geometry"], service.hello()
    return service, Path(run), None


@pytest.mark.parametrize("serial", ["CV2856D0002E", "CV2856D0008H"])
@pytest.mark.parametrize("backend,context,calibrated", [("geometry", "mask", False), ("contact_graspnet", "mask", False), ("contact_graspnet", "box", False), ("geometry", "mask", True)])
def test_archived_frame_full_pipeline_matches_original(reference_service, serial, backend, context, calibrated):
    import cv2
    import websockets

    service, run, remote_uri = reference_service
    directory = next(run.glob(f"*_{serial}")) / context
    archived = json.loads((directory / "result.json").read_text())
    with np.load(directory / "shared_input.npz", allow_pickle=False) as data:
        uid = uuid.uuid4().hex
        request = dict(type="infer", protocol_version=1, task_id=uid, request_id=uid,
                       observation_id=uid, generation=0, capture_timestamp_ns=time.time_ns(),
                       camera_frame="parity_color_optical", config_id="parity-only", backend=backend,
                       cgn_context=context, target={"class_name": "eu"}, evaluation_only=True,
                       color_bgr=data["bgr"].copy(), depth_m=data["depth_m"].copy(), K=data["K"].copy(),
                       up_camera=None, gripper={"id": "unmeasured-parity-only"}, max_candidates=15)
        expected_segmap = data["segmap"].copy()
    if calibrated:
        # Synthetic rigid transform verifies postprocessing, not robot calibration.
        request["T_base_camera"] = np.array([[0., 0., 1., .1], [0., 1., 0., -.2], [-1., 0., 0., .3], [0., 0., 0., 1.]])

    async def scenario():
        if remote_uri:
            return await WsGraspClient(remote_uri, inference_timeout_s=30).infer(request)
        async with websockets.serve(service.handle, "127.0.0.1", 0, compression=None, max_size=MAX_MESSAGE_BYTES) as listener:
            uri = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
            response = await WsGraspClient(uri, inference_timeout_s=30).infer(request)
        await service.drain()
        return response

    response = asyncio.run(scenario())
    assert response["type"] == "result", response
    diagnostics = response["diagnostics"]
    assert diagnostics["archive_id"]
    if service is not None:
        from grasp_protocol import unpackb
        archive = service.archive_root / diagnostics["archive_id"]
        assert unpackb((archive / "result.msgpack").read_bytes())["request_id"] == request["request_id"]
        with np.load(archive / "frame.npz", allow_pickle=False) as frozen:
            for key in ("color_bgr", "depth_m", "K"):
                np.testing.assert_array_equal(frozen[key], request[key])
            assert "up_camera" not in frozen.files
            if calibrated:
                np.testing.assert_array_equal(frozen["T_base_camera"], request["T_base_camera"])
    assert diagnostics["detections"] == archived["detections"]
    assert diagnostics["segmap_sha256"] == hashlib.sha256(expected_segmap.tobytes()).hexdigest()
    labels = cv2.imdecode(np.frombuffer(diagnostics["segmap_png"], np.uint8), cv2.IMREAD_UNCHANGED)
    np.testing.assert_array_equal(labels, expected_segmap)
    original = archived["methods"]["geometric" if backend == "geometry" else backend]
    assert len(response["candidates"]) == len(original)
    for candidate, expected in zip(response["candidates"], original, strict=True):
        assert int(candidate["object_id"]) == expected["object_id"]
        np.testing.assert_allclose(candidate["T_camera_native_grasp"], expected["matrix"], atol=1e-6, rtol=0)
        if calibrated:
            import importlib.util
            original_path = Path(os.environ["GRASP_REFERENCE_BACKEND"]) / "contact_graspnet_ros2/contact_graspnet_ros2/grasp_selection.py"
            spec = importlib.util.spec_from_file_location("parity_original_selection", original_path)
            selection = importlib.util.module_from_spec(spec); spec.loader.exec_module(selection)
            aligned = selection.align_grasp_to_base_vertical(expected["matrix"], request["T_base_camera"], wrist_offset_m=.09)
            np.testing.assert_allclose(candidate["T_camera_native_grasp_aligned"], aligned, atol=1e-6, rtol=0)
            np.testing.assert_allclose(candidate["T_camera_grasp_before_alignment"][:3, 3], candidate["T_camera_grasp_after_alignment"][:3, 3], atol=1e-8)
            np.testing.assert_allclose(request["T_base_camera"][:3, :3] @ candidate["T_camera_grasp"][:3, 2], [0, 0, -1], atol=1e-8)
        assert candidate["score"] == pytest.approx(expected["score"], abs=1e-6)
        assert candidate["width_m"] == pytest.approx(expected["width_m"], abs=1e-6)
    print(f"{serial} {backend}/{context} aligned={calibrated}: {len(original)} poses; segmentation, raw matrices, widths and scores match")
