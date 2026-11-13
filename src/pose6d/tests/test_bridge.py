"""ROS bridge boundaries that do not require a camera or robot."""

import base64
import json
import threading
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

pytest.importorskip("rclpy")
pytest.importorskip("realman_msgs")

from pose6d.observation import ObservationStore
from pose6d.motion import MotionController
from pose6d_bridge.node import PoseBridge, WEB_EVENT_LIMIT
from sensor_msgs.msg import CameraInfo, Image, JointState
from geometry_msgs.msg import TransformStamped


def stamped(message, nanoseconds, frame=""):
    message.header.stamp.sec, message.header.stamp.nanosec = divmod(nanoseconds, 1_000_000_000)
    message.header.frame_id = frame
    return message


def test_capture_freezes_aligned_rgbd_without_hand_eye():
    stamp = 9_995_000_000
    color = stamped(Image(height=8, width=8, encoding="bgr8", step=24,
                          data=bytes([10, 20, 30] * 64)), stamp, "color")
    depth = stamped(Image(height=8, width=8, encoding="16UC1", step=16,
                          data=np.full((8, 8), 1000, np.uint16).tobytes()), stamp, "depth")
    k = [50., 0., 3.5, 0., 50., 3.5, 0., 0., 1.]
    ci = stamped(CameraInfo(height=8, width=8, k=k, distortion_model="plumb_bob"), stamp, "color")
    di = stamped(CameraInfo(height=8, width=8, k=k, distortion_model="plumb_bob"), stamp, "depth")
    joints = [stamped(JointState(name=[f"j{i}" for i in range(6)], position=[0.] * 6), t)
              for t in (9_500_000_000, 9_990_000_000)]
    readings = iter([9_990_000_000, 10_000_000_000])
    capture = {"capture_timeout_sec": 1, "maximum_pair_skew_sec": .02, "maximum_capture_age_sec": .1,
               "settle_sec": .3, "maximum_joint_state_age_sec": .5, "maximum_joint_velocity_rad_sec": .01}
    store = ObservationStore()
    bridge = SimpleNamespace(capture_settings=capture, settings={**capture, "profile": "right", "object_id": "eu_plug"},
        profile={"color_frame": "color", "depth_frame": "depth", "calibration_data": {}, "base_frame": "r/base_link"},
        motion_config={"workspace_m": [[-.2, -.1, 0], [.3, .4, .5]]},
        lock=threading.Lock(), inputs={"color": [color], "depth": [depth], "color_info": [ci],
                                       "depth_info": [di], "joints": joints}, store=store, mesh_hash="hash",
        get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=next(readings))),
        lookup=lambda *_: np.eye(4), base_camera=lambda _: (None, "missing calibration"))
    event = PoseBridge.capture(bridge)
    frozen = store.get(event["observation_id"])
    assert event["calibration"] == "missing calibration"
    assert event["base_frame"] == "r/base_link"
    assert event["workspace_m"] == bridge.motion_config["workspace_m"]
    assert frozen["T_base_camera"] is None
    assert np.all(frozen["rgb"] == [30, 20, 10])
    assert np.allclose(frozen["depth_m"], 1)


def observation():
    store = ObservationStore()
    oid = store.freeze(rgb=np.zeros((8, 8, 3), dtype=np.uint8),
                       depth_m=np.ones((8, 8), dtype=np.float32), K=np.eye(3),
                       timestamp_ns=1, camera_frame="camera")
    return store, oid


@pytest.mark.parametrize("shape", [(7, 8), (8, 8, 3)])
def test_infer_rejects_mask_size_or_channels_before_network(shape):
    store, oid = observation()
    ok, png = cv2.imencode(".png", np.ones(shape, dtype=np.uint8))
    assert ok
    bridge = SimpleNamespace(store=store)
    with pytest.raises(ValueError, match="dimensions|single-channel"):
        PoseBridge.infer(bridge, {"observation_id": oid, "mask_png": base64.b64encode(png).decode()})


def test_oversized_web_event_returns_correlated_error():
    sent = []
    bridge = SimpleNamespace(events=SimpleNamespace(publish=sent.append))
    PoseBridge.emit(bridge, "client", "request", {"type": "pose6d_result", "data": "x" * WEB_EVENT_LIMIT})
    event = json.loads(sent[0].data)
    assert event["type"] == "pose6d_error"
    assert event["client_id"] == "client" and event["request_id"] == "request"


def test_other_client_cannot_infer_another_clients_observation():
    events = []
    busy = threading.Lock()
    busy.acquire()
    bridge = SimpleNamespace(observation_owner="owner", owner_lock=threading.Lock(), disconnected_clients=set(),
        connected_clients=None,
        active_review_client=None, busy=busy,
        emit=lambda client, request, event: events.append((client, request, event)),
        get_logger=lambda: SimpleNamespace(warning=lambda _: None),
        infer=lambda _: pytest.fail("cross-client inference must not run"))
    PoseBridge.run_command(bridge, {"type": "infer", "client_id": "other", "request_id": "req"})
    assert events[0][0:2] == ("other", "req")
    assert events[0][2]["type"] == "pose6d_error"
    assert not busy.locked()


def review_bridge(owner="old"):
    motion = MotionController(SimpleNamespace(lease_uncertain=lambda: False), {"enabled": False}, {})
    motion.load(owner, {}, [], "result")
    bridge = SimpleNamespace(observation_owner=owner, owner_lock=threading.Lock(), disconnected_clients=set(),
        connected_clients=None, active_review_client=None, busy=threading.Lock(), motion=motion, events=[],
        stopped=threading.Event(), get_logger=lambda: SimpleNamespace(warning=lambda _: None),
        capture=lambda: {"type": "pose6d_capture", "observation_id": "new"})
    bridge.emit = lambda client, request, event: bridge.events.append((client, request, event))
    bridge.mark_disconnected = lambda client: PoseBridge.mark_disconnected(bridge, client)

    def disconnect(command):
        try:
            PoseBridge.run_motion_command(bridge, command)
        finally:
            bridge.stopped.set()

    bridge.run_motion_command = disconnect
    return bridge


def capture_as(bridge, client):
    assert bridge.busy.acquire(False)
    PoseBridge.run_command(bridge, {"type": "capture", "client_id": client, "request_id": "capture"})
    return bridge.events[-1][2]


def test_web_restart_without_disconnect_releases_orphan_review_for_new_capture():
    bridge = review_bridge()
    PoseBridge.reconcile_clients(bridge, ["new-browser"])
    assert bridge.stopped.wait(2)
    assert bridge.observation_owner is None
    assert bridge.motion.review is None
    assert capture_as(bridge, "new-browser")["type"] == "pose6d_capture"
    assert bridge.observation_owner == "new-browser"


def test_disconnect_detaches_review_without_requesting_stop():
    bridge = review_bridge()
    bridge.motion.stop = lambda *_args: pytest.fail("disconnect must not stop motion")
    PoseBridge.run_motion_command(bridge, {"type": "disconnect", "client_id": "old"})
    assert bridge.motion.review is None
    assert bridge.observation_owner is None


def test_socket_snapshot_preserves_live_owner_and_rejects_other_browser():
    bridge = review_bridge()
    PoseBridge.reconcile_clients(bridge, ["old", "new-browser"])
    assert bridge.observation_owner == "old"
    assert bridge.motion.review[0] == "old"
    assert not bridge.stopped.is_set()
    assert "another client" in capture_as(bridge, "new-browser")["message"]


@pytest.mark.parametrize("kind", ["capture", "infer"])
def test_disconnect_during_request_cannot_reinstall_orphan_ownership_or_review(kind):
    bridge = review_bridge()
    entered, resume = threading.Event(), threading.Event()

    def work(*_):
        entered.set()
        assert resume.wait(3)
        if kind == "infer":
            # An inference already running when Web restarts can finish later.
            bridge.motion.load("old", {}, [], "late-result")
        return {"type": f"pose6d_{kind}"}

    bridge.capture = bridge.infer = work
    bridge.busy.acquire()
    worker = threading.Thread(target=PoseBridge.run_command,
        args=(bridge, {"type": kind, "client_id": "old", "request_id": "work"}), daemon=True)
    worker.start()
    try:
        assert entered.wait(2)
        PoseBridge.reconcile_clients(bridge, [])
        assert bridge.stopped.wait(2)
        assert "old" in bridge.disconnected_clients
    finally:
        resume.set()
        worker.join(3)
    assert not worker.is_alive()
    assert bridge.observation_owner is None
    assert bridge.motion.review is None
    assert not bridge.busy.locked()


def test_queued_capture_from_disconnected_socket_is_rejected():
    bridge = review_bridge()
    bridge.active_review_client = "old"
    PoseBridge.reconcile_clients(bridge, ["new-browser"])
    assert bridge.stopped.wait(2)
    assert "disconnected" in capture_as(bridge, "old")["message"]
    assert bridge.observation_owner is None


def test_orphan_cleanup_keeps_unreleased_motion_session_locked():
    bridge = review_bridge()
    # An uncertain driver release must retain the orphaned session lock.
    def unavailable(*_):
        raise ValueError("close the active motion session before replacing a review")
    bridge.motion = SimpleNamespace(detach=lambda _client: None, invalidate_review=unavailable,
        status=lambda: {"session_id": "old-session", "lease_retained": True})
    PoseBridge.reconcile_clients(bridge, ["new-browser"])
    assert bridge.stopped.wait(2)
    assert bridge.motion.status()["lease_retained"] is True
    assert capture_as(bridge, "new-browser")["type"] == "pose6d_error"
    assert bridge.observation_owner is None


def test_explicit_close_releases_observation_ownership():
    bridge = review_bridge()
    PoseBridge.run_motion_command(bridge, {"type": "close", "client_id": "old"})
    assert bridge.observation_owner is None
    assert bridge.motion.review is None


@pytest.mark.parametrize("clients", [None, "old", [None], [""], [1], ["old"] * 129])
def test_invalid_membership_cannot_release_review(clients):
    bridge = review_bridge()
    with pytest.raises(ValueError, match="snapshot"):
        PoseBridge.reconcile_clients(bridge, clients)
    assert bridge.observation_owner == "old"


def test_live_tool_frame_uses_active_tool_and_rotated_flange():
    tf = TransformStamped()
    tf.header.stamp.sec = 10
    tf.transform.translation.x = .1
    tf.transform.rotation.y = np.sqrt(.5)
    tf.transform.rotation.w = np.sqrt(.5)
    from time import monotonic
    events = []
    bridge = SimpleNamespace(
        profile={"arm": "r", "base_frame": "r/base_link", "flange_frame": "r/link_6", "calibration_data": {}},
        motion_config={"workspace_m": [[-.2, -.1, 0], [.3, .4, .5]]},
        motion_port=SimpleNamespace(coordinate_lock=threading.Lock(), coordinates_at=monotonic(),
            coordinates={"current_tool": "tcpsuck", "tool_matched": True,
                         "tool": {"xyz_m": [0, 0, .37], "quaternion_wxyz": [1, 0, 0, 0]}}),
        tf=SimpleNamespace(lookup_transform=lambda *_: tf),
        get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=10_010_000_000)),
        capture_settings={"maximum_joint_state_age_sec": .25}, emit=lambda _c, _r, payload: events.append(payload))
    PoseBridge.publish_frames(bridge)
    assert events[-1]["available"] is True
    assert events[-1]["base_frame"] == "r/base_link"
    assert events[-1]["workspace_m"] == bridge.motion_config["workspace_m"]
    np.testing.assert_allclose(np.asarray(events[-1]["T_base_tcp"])[:3, 3], [.47, 0, 0], atol=1e-9)
    bridge.motion_port.coordinates_at = 0
    PoseBridge.publish_frames(bridge)
    assert events[-1]["available"] is False
    assert events[-1]["workspace_m"] == bridge.motion_config["workspace_m"]
    assert "T_base_tcp" not in events[-1]
