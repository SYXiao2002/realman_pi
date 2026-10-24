"""Postprocessing uses the reference function and retains native generation."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from grasp_server.backends import ReferenceBackend, ReferenceRuntime, adapt_native_candidate
from grasp_protocol import validate_response
from grasp_protocol.schema import identity
from test_grasp_protocol import observation


@pytest.fixture
def selection():
    backend = Path(os.environ.get("GRASP_REFERENCE_BACKEND", Path(__file__).resolve().parents[3].parent / "chaung_wei_2_jiao_fu/backend"))
    path = backend / "contact_graspnet_ros2/contact_graspnet_ros2/grasp_selection.py"
    if not path.is_file():
        pytest.skip("original reference source not installed")
    spec = importlib.util.spec_from_file_location("test_original_grasp_selection", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runtime(selection):
    raw = np.eye(4); raw[:3, 3] = [.02, -.03, .41]
    scene = dict(detections=[dict(class_name="adapter")], segmap=np.ones((8, 8), np.uint16), segments={1: np.zeros((64, 3))})
    return SimpleNamespace(selection=selection, prepare=lambda request: scene, provenance={},
        settings=dict(warmup_runs=0, seed=42, cgn_context="mask"), worker=SimpleNamespace(heuristic_wrist_offset=.09, z_range=(.2, 1.5)),
        manifest={"contact_graspnet": {"native_hand_depth_m": .1034}}, raw=raw)


@pytest.mark.parametrize("angle", [0., .7, 1.5707963267948966])
def test_geometry_reuses_original_and_preserves_center_and_metadata(runtime, angle):
    Tbc = np.eye(4); c, s = np.cos(angle), np.sin(angle)
    Tbc[:3, :3] = [[c, 0, s], [0, 1, 0], [-s, 0, c]]
    Tbc[:3, 3] = [1., 2., 3.]
    request = observation("geometry"); request.update(evaluation_only=True, up_camera=None, T_base_camera=Tbc)
    backend = ReferenceBackend(runtime, "geometry")
    original = runtime.raw.copy()
    backend.generate = lambda *_: [dict(grasp=runtime.raw, obj_id=1, score=1.7, grasp_width_est_m=.1)]
    candidates, diagnostics = backend.infer(request)
    candidate, = candidates
    expected = runtime.selection.align_grasp_to_base_vertical(original, Tbc, wrist_offset_m=.09)
    np.testing.assert_array_equal(candidate["T_camera_native_grasp"], original)
    np.testing.assert_array_equal(runtime.raw, original)
    np.testing.assert_array_equal(candidate["T_camera_native_grasp_aligned"], expected)
    np.testing.assert_allclose(Tbc[:3, :3] @ candidate["T_camera_grasp"][:3, 2], [0, 0, -1], atol=1e-12)
    np.testing.assert_allclose(candidate["T_camera_grasp"][:3, 3], candidate["T_camera_grasp_before_alignment"][:3, 3], atol=1e-12)
    np.testing.assert_allclose(candidate["T_camera_grasp"], adapt_native_candidate(expected, offset_m=.09), atol=0)
    assert candidate["score"] == 1.7 and candidate["width_m"] == .1
    assert diagnostics["vertical_alignment_status"] == "applied"
    validate_response(dict(protocol_version=1, type="result", **identity(request), model_id="original", gripper_id=request["gripper"]["id"], candidates=candidates), request)


@pytest.mark.parametrize("backend_name,calibrated,expected", [("geometry", False, "missing_base_camera"), ("contact_graspnet", True, "backend_not_applicable")])
def test_unavailable_alignment_never_fabricates_after_pose(runtime, backend_name, calibrated, expected):
    request = observation(backend_name); request.update(evaluation_only=True, up_camera=None, T_base_camera=np.eye(4) if calibrated else None)
    backend = ReferenceBackend(runtime, backend_name)
    backend.generate = lambda *_: [dict(grasp=runtime.raw, obj_id=1, score=.7, grasp_width_est_m=.04)]
    candidate = backend.infer(request)[0][0]
    assert candidate["vertical_alignment"] == dict(applied=False, status=expected, wrist_offset_m=.09 if backend_name == "geometry" else .1034, approach_axis_base=[0., 0., -1.])
    assert candidate["T_camera_native_grasp_aligned"] is None
    assert candidate["T_camera_grasp_after_alignment"] is None
    np.testing.assert_array_equal(candidate["T_camera_grasp"], candidate["T_camera_grasp_before_alignment"])


def test_uncalibrated_geometry_cannot_become_live_plan(runtime):
    backend = ReferenceBackend(runtime, "geometry")
    backend.generate = lambda *_: []
    with pytest.raises(ValueError, match="requires captured T_base_camera"):
        backend.infer(observation("geometry"))


@pytest.mark.parametrize("backend_name", ["geometry", "contact_graspnet"])
@pytest.mark.parametrize("case", ["mixed", "all_insufficient", "no_target"])
def test_insufficient_instance_does_not_discard_valid_instances(runtime, backend_name, case):
    labels = np.zeros((12, 12), np.uint16)
    labels.flat[:40] = 1
    labels.flat[40:43] = 2
    labels.flat[43:75] = 3  # Exactly the minimum is valid.
    if case == "all_insufficient":
        labels[:] = 0
        labels.flat[:3] = [1, 2, 3]
    detections = [dict(class_name="eu")] * 3 if case != "no_target" else []
    if not detections:
        labels[:] = 0
    preparer = ReferenceRuntime.__new__(ReferenceRuntime)
    preparer.worker = SimpleNamespace(
        min_segment_points=32,
        depth_to_pointcloud=lambda *args: (np.zeros((12, 12, 3)), np.ones((12, 12), bool)),
        detector=SimpleNamespace(detect=lambda *a, **k: detections),
        segmentor=SimpleNamespace(segment=lambda *a: labels.copy()))
    runtime.prepare = preparer.prepare
    request = observation(backend_name)
    request.update(evaluation_only=True, T_base_camera=np.eye(4))
    backend = ReferenceBackend(runtime, backend_name)
    calls = []
    def generate(request, scene):
        calls.append(set(scene["segments"]))
        assert set(scene["segments"]) == {1, 3}
        assert scene["detections"] == detections
        np.testing.assert_array_equal(scene["segmap"], labels)
        return [dict(grasp=runtime.raw, obj_id=i, score=.8, grasp_width_est_m=.04) for i in (1, 3)]
    backend.generate = generate
    candidates, diagnostics = backend.infer(request)
    if case == "mixed":
        assert calls == [{1, 3}]
        assert [c["object_id"] for c in candidates] == ["1", "3"]
        assert diagnostics["skipped_instances"] == [dict(object_id="2", valid_points=3, minimum_points=32,
                                                         reason="insufficient_valid_depth")]
        assert diagnostics["instance_points"] == {"1": 40, "2": 3, "3": 32}
    else:
        assert not candidates and not calls
        assert diagnostics["reason"] == ("no_target" if case == "no_target" else "no_valid_instances")
        assert len(diagnostics["skipped_instances"]) == (0 if case == "no_target" else 3)


def test_geometry_adapter_excludes_bad_detection_before_original_bbox_fallback(runtime):
    detections = [dict(class_name="eu", marker=i) for i in (1, 2, 3)]
    labels = np.array([[0, 1], [2, 3]], dtype=np.uint16)
    scene = dict(detections=detections, segmap=labels, segments={1: np.ones((40, 3)), 3: np.ones((32, 3))*3},
                 pc_image=np.zeros((2, 2, 3)), valid=np.ones((2, 2), bool), depth=np.ones((2, 2)), K=np.eye(3), bgr=None)
    def original(pc_image, valid, active, segments, depth, K, segmap, **kwargs):
        assert [d["marker"] for d in active] == [1, 3]
        assert set(segments) == {1, 2}
        np.testing.assert_array_equal(segments[2], scene["segments"][3])
        np.testing.assert_array_equal(segmap, [[0, 1], [3, 2]])
        # All other-object masks retain their pixels, including the bad instance.
        np.testing.assert_array_equal(segmap > 0, labels > 0)
        assert pc_image is scene["pc_image"] and valid is scene["valid"] and depth is scene["depth"]
        return [dict(obj_id=2, grasp=runtime.raw, score=.8, grasp_width_est_m=.04)]
    runtime.worker._generate_heuristic_grasps = original
    raw = ReferenceBackend(runtime, "geometry").generate(observation("geometry"), scene)
    assert raw[0]["obj_id"] == 3
    assert scene["detections"] == detections and set(scene["segments"]) == {1, 3}
    np.testing.assert_array_equal(scene["segmap"], labels)
