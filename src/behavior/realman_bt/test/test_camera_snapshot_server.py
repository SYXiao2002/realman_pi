"""Host-side tests for camera_snapshot_server's capture orchestration.

The server imports rclpy and the generated SaveCameraImages service, which only
exist inside the driver container. To exercise the file-writing logic on the host
we load the module once with lightweight stand-ins for those imports, then drive
_handle_save/_capture with a fake _gather_frames and a monkeypatched image_to_jpeg.
"""

import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).parents[4]
SERVER_PATH = ROOT / "src/behavior/realman_bt/scripts/camera_snapshot_server.py"


class _Recorder:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, message) -> None:
        self.infos.append(str(message))

    def warning(self, message) -> None:
        self.warnings.append(str(message))


def _install_ros_stubs() -> None:
    sensor_msg = ModuleType("sensor_msgs.msg")
    sensor_msg.Image = type("Image", (), {})
    sensor_pkg = ModuleType("sensor_msgs")
    sensor_pkg.msg = sensor_msg

    save_camera_images = type("SaveCameraImages", (), {})
    realman_srv = ModuleType("realman_msgs.srv")
    realman_srv.SaveCameraImages = save_camera_images
    realman_pkg = ModuleType("realman_msgs")
    realman_pkg.srv = realman_srv

    def image_to_jpeg(image, *, quality=90, width=None, height=None):  # noqa: ARG001
        return b"stub-jpeg"

    recording_workers = ModuleType("realman_recording.camera_workers")
    recording_workers.image_to_jpeg = image_to_jpeg
    recording_pkg = ModuleType("realman_recording")
    recording_pkg.camera_workers = recording_workers

    class _QosProfile(dict):
        pass

    class _RclPy(ModuleType):
        qos_profile_sensor_data = _QosProfile()
        ReentrantCallbackGroup = type("ReentrantCallbackGroup", (), {})
        MultiThreadedExecutor = type("MultiThreadedExecutor", (), {})
        Node = type("Node", (), {})

    stubs = {
        "sensor_msgs": sensor_pkg,
        "sensor_msgs.msg": sensor_msg,
        "realman_msgs": realman_pkg,
        "realman_msgs.srv": realman_srv,
        "realman_recording": recording_pkg,
        "realman_recording.camera_workers": recording_workers,
        "rclpy": _RclPy("rclpy"),
        "rclpy.callback_groups": ModuleType("rclpy.callback_groups"),
        "rclpy.executors": ModuleType("rclpy.executors"),
        "rclpy.node": ModuleType("rclpy.node"),
        "rclpy.qos": ModuleType("rclpy.qos"),
    }
    stubs["rclpy.callback_groups"].ReentrantCallbackGroup = _RclPy.ReentrantCallbackGroup
    stubs["rclpy.executors"].MultiThreadedExecutor = _RclPy.MultiThreadedExecutor
    stubs["rclpy.node"].Node = _RclPy.Node
    stubs["rclpy.qos"].qos_profile_sensor_data = _RclPy.qos_profile_sensor_data
    for name, module in stubs.items():
        sys.modules.setdefault(name, module)


def _load_server():
    _install_ros_stubs()
    spec = importlib.util.spec_from_file_location("_camera_snapshot_server", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SERVER = _load_server()


def _make_server(
    jpeg_quality=90,
    default_directory="",
    default_timeout=5.0,
    gather=None,
    lock=None,
):
    server = object.__new__(_SERVER.CameraSnapshotServer)
    server.get_logger = lambda: _Recorder()
    server._callback_group = object()
    server._jpeg_quality = jpeg_quality
    server._default_directory = default_directory
    server._default_timeout = default_timeout
    server._operation_lock = lock if lock is not None else threading.Lock()
    server._gather_frames = gather
    server.destroy_subscription = lambda sub: None
    return server


def _request(topics, directory, timeout_sec=2.0, session_id=""):
    return SimpleNamespace(
        image_topics=topics,
        directory=directory,
        timeout_sec=timeout_sec,
        session_id=session_id,
    )


def _response():
    return SimpleNamespace(success=None, message="", saved_paths=[])


def test_a_successful_capture_writes_one_jpeg_per_topic(tmp_path, monkeypatch):
    left = object()
    right = object()
    topics = ["/camera_left/color/image_raw", "/camera_right/color/image_raw"]
    captured = {}

    def fake_gather(requested, timeout):
        captured["topics"] = list(requested)
        captured["timeout"] = timeout
        return {"__subscriptions__": [], topics[0]: left, topics[1]: right}

    server = _make_server(gather=fake_gather)
    monkeypatch.setattr(_SERVER, "image_to_jpeg", lambda image, *, quality: b"JPEGDATA")

    response = server._handle_save(_request(topics, str(tmp_path)), _response())

    assert response.success is True
    assert len(response.saved_paths) == 2
    assert captured["topics"] == topics
    names = sorted(Path(path).name for path in response.saved_paths)
    assert names[0].startswith("camera_left_")
    assert names[1].startswith("camera_right_")
    for path in response.saved_paths:
        assert Path(path).read_bytes() == b"JPEGDATA"
        assert Path(path).parent == tmp_path


def test_a_session_id_names_every_frame_with_the_same_tag(tmp_path, monkeypatch):
    # One grasp cycle binds session_id="{grasp_session}"; every frame of that cycle
    # (after_plan/after_close/after_lift) must carry the same tag so they group.
    topics = ["/camera_left/color/image_raw", "/camera_right/color/image_raw"]

    def fake_gather(requested, timeout):
        return {"__subscriptions__": [], topics[0]: object(), topics[1]: object()}

    server = _make_server(gather=fake_gather)
    monkeypatch.setattr(_SERVER, "image_to_jpeg", lambda image, *, quality: b"J")

    response = server._handle_save(
        _request(topics, str(tmp_path), session_id="20261029_143748"), _response()
    )

    assert response.success is True
    names = sorted(Path(path).name for path in response.saved_paths)
    assert names == ["camera_left_20261029_143748.jpg", "camera_right_20261029_143748.jpg"]


def test_an_empty_session_id_falls_back_to_a_capture_timestamp(tmp_path, monkeypatch):
    # No caller tag (for example snapshot.xml) keeps the original timestamp naming.
    topic = "/camera_left/color/image_raw"

    def fake_gather(requested, timeout):
        return {"__subscriptions__": [], topic: object()}

    server = _make_server(gather=fake_gather)
    monkeypatch.setattr(_SERVER, "image_to_jpeg", lambda image, *, quality: b"J")

    response = server._handle_save(_request([topic], str(tmp_path)), _response())

    assert response.success is True
    name = Path(response.saved_paths[0]).name
    assert name.startswith("camera_left_")
    # A capture timestamp is %Y%m%d_%H%M%S_%f, i.e. three underscore-joined parts.
    stamp = name[len("camera_left_"):-len(".jpg")]
    assert len(stamp.split("_")) == 3


def test_session_tag_sanitizes_unsafe_characters():
    server = _make_server()
    assert server._session_tag("20261029_143748") == "20261029_143748"
    # Separators and spaces must never reach the filesystem; they become underscores.
    assert server._session_tag("a/b c:d") == "a_b_c_d"
    # Nothing usable collapses to empty so the caller falls back to a timestamp.
    assert server._session_tag("///") == ""
    assert server._session_tag("") == ""


def test_a_missing_frame_fails_and_writes_nothing(tmp_path):
    topics = ["/camera_left/color/image_raw", "/camera_right/color/image_raw"]

    def fake_gather(requested, timeout):  # right camera never produced a frame
        return {"__subscriptions__": [], topics[0]: object(), topics[1]: None}

    server = _make_server(gather=fake_gather)
    response = server._handle_save(_request(topics, str(tmp_path)), _response())

    assert response.success is False
    assert "no frame within" in response.message
    assert "/camera_right/color/image_raw" in response.message
    assert response.saved_paths == []
    assert list(tmp_path.iterdir()) == []


def test_an_encode_failure_is_reported_without_writing(tmp_path, monkeypatch):
    topic = "/camera_left/color/image_raw"

    def fake_gather(requested, timeout):
        return {"__subscriptions__": [], topic: object()}

    def boom(image, *, quality):
        raise ValueError("unsupported image encoding for JPEG archive: 'yuv422'")

    server = _make_server(gather=fake_gather)
    monkeypatch.setattr(_SERVER, "image_to_jpeg", boom)

    response = server._handle_save(_request([topic], str(tmp_path)), _response())

    assert response.success is False
    assert "JPEG encode failed" in response.message
    assert list(tmp_path.iterdir()) == []


def test_duplicate_first_segment_labels_stay_unique(tmp_path, monkeypatch):
    topics = ["/cam/color/image_raw", "/cam/depth/image_raw"]

    def fake_gather(requested, timeout):
        return {"__subscriptions__": [], topics[0]: object(), topics[1]: object()}

    server = _make_server(gather=fake_gather)
    monkeypatch.setattr(_SERVER, "image_to_jpeg", lambda image, *, quality: b"J")

    response = server._handle_save(_request(topics, str(tmp_path)), _response())

    assert response.success is True
    names = sorted(Path(path).name for path in response.saved_paths)
    assert len(set(names)) == 2
    assert all(name.startswith("cam") for name in names)


def test_empty_topics_are_refused(tmp_path):
    server = _make_server(gather=lambda requested, timeout: {"__subscriptions__": []})
    response = server._handle_save(_request([], str(tmp_path)), _response())
    assert response.success is False
    assert "no image topics" in response.message


def test_relative_directory_is_refused(tmp_path):
    server = _make_server(gather=lambda requested, timeout: {"__subscriptions__": []})
    response = server._handle_save(
        _request(["/camera_left/color/image_raw"], "relative/snap"), _response()
    )
    assert response.success is False
    assert "directory must be absolute" in response.message


def test_missing_directory_without_a_default_is_refused(tmp_path):
    server = _make_server(default_directory="", gather=lambda requested, timeout: {})
    response = server._handle_save(
        _request(["/camera_left/color/image_raw"], ""), _response()
    )
    assert response.success is False
    assert "no directory" in response.message


def test_default_directory_is_used_when_the_request_omits_it(tmp_path, monkeypatch):
    default_dir = tmp_path / "default"
    topic = "/camera_left/color/image_raw"

    def fake_gather(requested, timeout):
        return {"__subscriptions__": [], topic: object()}

    server = _make_server(default_directory=str(default_dir), gather=fake_gather)
    monkeypatch.setattr(_SERVER, "image_to_jpeg", lambda image, *, quality: b"J")

    response = server._handle_save(_request([topic], ""), _response())

    assert response.success is True
    assert Path(response.saved_paths[0]).parent == default_dir


def test_a_zero_timeout_falls_back_to_the_server_default():
    seen = {}

    def fake_gather(requested, timeout):
        seen["timeout"] = timeout
        return {"__subscriptions__": [], "/camera_left/color/image_raw": None}

    server = _make_server(default_timeout=3.5, gather=fake_gather)
    response = server._handle_save(
        _request(["/camera_left/color/image_raw"], "/tmp/snap", timeout_sec=0.0),
        _response(),
    )
    assert response.success is False
    assert seen["timeout"] == 3.5


def test_a_busy_server_refuses_a_second_concurrent_request(tmp_path):
    lock = threading.Lock()
    lock.acquire()  # another snapshot is mid-flight
    server = _make_server(gather=lambda requested, timeout: {"__subscriptions__": []}, lock=lock)

    response = server._handle_save(
        _request(["/camera_left/color/image_raw"], str(tmp_path)), _response()
    )

    assert response.success is False
    assert "already in progress" in response.message
    lock.release()


def test_gather_frames_reports_missing_topics_as_none():
    # The real _gather_frames subscribes and polls; without a ROS graph it must
    # simply time out and hand back None per topic plus the (empty) subscription list.
    server = _make_server()
    server.create_subscription = lambda *args, **kwargs: SimpleNamespace()
    result = _SERVER.CameraSnapshotServer._gather_frames(
        server, ["/camera_left/color/image_raw"], 0.05
    )
    assert result["/camera_left/color/image_raw"] is None
    assert len(result["__subscriptions__"]) == 1


def test_label_for_sanitizes_and_deduplicates():
    server = _make_server()
    used: set[str] = set()
    first = server._label_for("/camera_left/color/image_raw", used, 0)
    second = server._label_for("/camera_left/depth/image_raw", used, 1)
    assert first == "camera_left"
    assert second != first
    assert second.startswith("camera_left")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
