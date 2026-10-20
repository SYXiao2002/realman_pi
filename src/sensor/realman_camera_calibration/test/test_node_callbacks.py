"""Service callback checks without starting DDS, cameras or robot drivers."""

import importlib
import json
import sys
import threading
import time
from pathlib import Path
from types import MethodType, ModuleType, SimpleNamespace

import cv2
import numpy as np
import pytest

from realman_camera_calibration.calibration_math import make_charuco_board, transform_matrix
from realman_camera_calibration.config import load_config
from realman_camera_calibration.session import content_hash, write_manifest


@pytest.fixture
def node_module(monkeypatch):
    pytest.importorskip("rclpy")
    # Generated services may not be built on a development host. The callbacks
    # are exercised with plain requests/responses; no ROS type support is used.
    try:
        importlib.import_module("realman_msgs.srv")
    except ImportError:
        package = ModuleType("realman_msgs")
        services = ModuleType("realman_msgs.srv")
        services.CaptureCalibrationSample = SimpleNamespace(Request=object, Response=object)
        services.SolveCalibration = SimpleNamespace(Request=object, Response=object)
        services.UpdateCalibrationLayout = SimpleNamespace(Request=object, Response=object)
        monkeypatch.setitem(sys.modules, "realman_msgs", package)
        monkeypatch.setitem(sys.modules, "realman_msgs.srv", services)
    return importlib.import_module("realman_camera_calibration.camera_calibration_node")


@pytest.fixture
def callback_node(tmp_path, monkeypatch, node_module):
    from sensor_msgs.msg import CameraInfo, Image

    monkeypatch.setenv("REALMAN_LOG_ROOT", str(tmp_path))
    root = Path(__file__).parents[4]
    config = load_config(root / "config/ros/camera_calibration.yaml")
    board = make_charuco_board(config["board"])
    board_image = board.draw((960, 720), marginSize=20) if hasattr(board, "draw") else board.generateImage((960, 720), marginSize=20)
    image = cv2.cvtColor(board_image, cv2.COLOR_GRAY2BGR)
    image_message = Image()
    image_message.header.stamp.sec = 100
    image_message.header.frame_id = config["cameras"]["right"]["optical_frame"]
    info = CameraInfo()
    info.header = image_message.header
    info.width, info.height = 960, 720
    info.k = [960.0, 0.0, 480.0, 0.0, 960.0, 360.0, 0.0, 0.0, 1.0]
    info.d = [0.0] * 5
    info.distortion_model = "plumb_bob"
    frame = node_module.LatestFrame(
        image_message=image_message, image=image,
        image_received_monotonic=time.monotonic(), camera_info=info,
        camera_info_received_monotonic=time.monotonic(),
    )
    logger = SimpleNamespace(info=lambda *args: None, warn=lambda *args: None, error=lambda *args: None)
    calls = []

    def lookup(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(transform=SimpleNamespace(
            rotation=SimpleNamespace(w=1.0, x=0.0, y=0.0, z=0.0),
            translation=SimpleNamespace(x=0.0, y=0.0, z=0.0),
        ))

    node = SimpleNamespace(
        _config=config, _board=board, _session_id="", _samples={}, _manifests={},
        _frames={"right": frame, "left": node_module.LatestFrame()},
        _lock=threading.RLock(), _operation_lock=threading.Lock(),
        _tf_buffer=SimpleNamespace(lookup_transform=lookup),
        get_logger=lambda: logger,
        get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=100_000_000_000)),
    )
    for name in ("_capture_locked", "_solve_locked", "_start_session", "_load_session", "_session_directory", "_camera_health_state"):
        setattr(node, name, MethodType(getattr(node_module.CameraCalibrationNode, name), node))
    node._message_stamp_ns = node_module.CameraCalibrationNode._message_stamp_ns
    node._timestamp_delay_sec = node_module.CameraCalibrationNode._timestamp_delay_sec
    return node, calls


def test_right_capture_uses_image_time_and_does_not_require_left(callback_node, node_module):
    node, calls = callback_node
    request = SimpleNamespace(arm_id="r", session_id="", start_new_session=True)
    response = SimpleNamespace(success=False)
    node_module.CameraCalibrationNode._capture(node, request, response)
    assert response.success, response.message
    assert response.arm_id == "r" and response.sample_count == 1
    assert Path(response.image_path).is_file()
    assert Path(response.preview_image_path).is_file()
    assert calls[0][0][0:2] == ("r/base_link", "r/link_6")
    assert calls[0][0][2].nanoseconds == 100_000_000_000


def test_switching_arm_in_active_session_is_rejected(callback_node, node_module):
    node, _ = callback_node
    node._start_session("r")
    response = SimpleNamespace(success=False)
    node_module.CameraCalibrationNode._capture(
        node, SimpleNamespace(arm_id="l", session_id="", start_new_session=False), response
    )
    assert not response.success
    assert "selected arm differs" in response.message


def test_concurrent_solve_or_capture_does_not_mutate_session(callback_node, node_module):
    node, _ = callback_node
    node._operation_lock.acquire()
    response = SimpleNamespace(success=False)
    node_module.CameraCalibrationNode._capture(
        node, SimpleNamespace(arm_id="r", session_id="", start_new_session=True), response
    )
    assert not response.success
    assert "in progress" in response.message
    assert not node._session_id


@pytest.mark.parametrize("arm", ["r", "l"])
def test_one_arm_solves_independently_and_never_updates_base_layout(callback_node, node_module, arm):
    node, _ = callback_node
    node._start_session(arm)
    session_id = node._session_id
    manifest = node._manifests[session_id]
    intrinsics = {"k": [1.0] * 9}
    manifest.update(intrinsics=intrinsics, intrinsics_sha256=content_hash(intrinsics))
    directory = node._session_directory(session_id)
    write_manifest(directory, manifest)
    sample_dir = directory / "batches/synthetic"
    sample_dir.mkdir(parents=True)
    rng = np.random.default_rng(41)

    def rotation():
        axis = rng.normal(size=3)
        return cv2.Rodrigues(axis / np.linalg.norm(axis) * 0.6)[0]

    expected = transform_matrix(rotation(), [0.05, 0.01, 0.12])
    board_in_base = transform_matrix(rotation(), [0.4, 0.1, 0.8])
    for index in range(node._config["sampling"]["minimum_samples_per_arm"]):
        base_to_tool = transform_matrix(rotation(), rng.uniform(-0.3, 0.3, 3))
        target_to_camera = np.linalg.inv(expected) @ np.linalg.inv(base_to_tool) @ board_in_base
        sample = {
            "arm_id": arm, "identity": manifest["identity"],
            "sample_id": f"{arm}-{index:04d}-fixture", "intrinsics_sha256": manifest["intrinsics_sha256"],
            "base_to_tool": base_to_tool.tolist(), "target_to_camera": target_to_camera.tolist(),
            "reprojection_error_px": 0.1,
        }
        (sample_dir / f"{index:04d}.json").write_text(json.dumps(sample))
    response = SimpleNamespace(success=False)
    node_module.CameraCalibrationNode._solve(
        node, SimpleNamespace(arm_id=arm, session_id=session_id), response
    )
    assert response.success, response.message
    result = json.loads(Path(response.result_file).read_text())
    assert set(result["hand_eye"]) == {arm}
    assert np.allclose(result["hand_eye"][arm]["tool_to_camera"], expected)
    assert result["verified_for_grasp"] is False
    assert "relative_base_poses" not in result
