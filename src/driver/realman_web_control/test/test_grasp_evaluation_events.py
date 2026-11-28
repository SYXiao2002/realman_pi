"""Cached grasp snapshots accept only bounded, monotonic read-only events."""

import json
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("rclpy")
import rclpy
from rclpy.node import Node
from std_msgs.msg import String

from realman_web_control.web_control_node import WebControlNode


class Events:
    def __init__(self):
        self.events = []

    def send_event(self, value, client_id=None):
        self.events.append((value, client_id))


@pytest.fixture
def web_node():
    rclpy.init()
    node = WebControlNode.__new__(WebControlNode)
    Node.__init__(node, "grasp_evaluation_event_test")
    node._server = Events()
    node._grasp_evaluation = None
    yield node
    Node.destroy_node(node)
    rclpy.shutdown()


def snapshot(*, observation="obs-1", timestamp="100", phase="captured"):
    return {"type": "grasp_evaluation", "read_only": True, "observation_id": observation,
            "capture_timestamp_ns": timestamp, "profile_id": "profile-r", "profile_name": "right_wrist",
            "arm": "r", "phase": phase}


def send(node, value):
    node._grasp_evaluation_message(String(data=json.dumps(value)))


def test_snapshot_cache_advances_only_within_identity(web_node):
    captured = snapshot()
    send(web_node, captured)
    send(web_node, snapshot(phase="inferred"))
    assert web_node._grasp_evaluation["phase"] == "inferred"
    send(web_node, snapshot(phase="planned"))
    assert web_node._grasp_evaluation["phase"] == "planned"
    send(web_node, snapshot(observation="obs-old", phase="rejected"))
    send(web_node, snapshot(phase="captured"))
    send(web_node, snapshot(phase="inferred"))
    assert web_node._grasp_evaluation["observation_id"] == "obs-1"
    assert web_node._grasp_evaluation["phase"] == "planned"


def test_snapshot_cache_accepts_dense_review_payload(web_node):
    payload = {**snapshot(), "cloud_payload": "x" * (3 * 1024 * 1024)}
    send(web_node, payload)
    assert web_node._grasp_evaluation["observation_id"] == "obs-1"


def test_snapshot_cache_rejects_invalid_and_oversized_events(web_node):
    send(web_node, {**snapshot(), "capture_timestamp_ns": "0"})
    send(web_node, {**snapshot(), "phase": "planning"})
    web_node._grasp_evaluation_message(String(data="{"))
    web_node._grasp_evaluation_message(String(data=" " * (8 * 1024 * 1024 + 1)))
    assert web_node._grasp_evaluation is None
    assert web_node._server.events == []


def test_pose6d_live_frames_are_broadcast_but_results_stay_private(web_node):
    web_node._pose6d_event(String(data=json.dumps({"type": "pose6d_frames", "available": False})))
    web_node._pose6d_event(String(data=json.dumps({"type": "pose6d_result", "client_id": "owner", "result_id": "r"})))
    web_node._pose6d_event(String(data=json.dumps({"type": "pose6d_batch_result", "client_id": "owner", "instance_count": 2})))
    assert web_node._server.events == [({"type": "pose6d_frames", "available": False}, None),
                                       ({"type": "pose6d_result", "result_id": "r"}, "owner"),
                                       ({"type": "pose6d_batch_result", "instance_count": 2}, "owner")]


def test_pose6d_socket_snapshot_recovers_web_restart_and_late_bridge_discovery(web_node):
    sent = []
    clients = ["new-browser"]
    subscribers = [0]
    web_node._control_lock = threading.RLock()
    web_node._pose6d_commands = SimpleNamespace(publish=lambda msg: sent.append(json.loads(msg.data)),
        get_subscription_count=lambda: subscribers[0])
    web_node._server.connected_client_ids = lambda: list(clients)
    web_node._publish_pose6d_clients()
    assert sent == []
    subscribers[0] = 1
    web_node._publish_pose6d_clients()
    assert sent[-1] == {"type": "clients", "client_ids": ["new-browser"]}
    # This does not depend on a successfully enqueued disconnect notification.
    clients.clear()
    web_node._publish_pose6d_clients()
    assert sent[-1] == {"type": "clients", "client_ids": []}
    subscribers[0] = 2
    web_node._publish_pose6d_clients()
    assert len(sent) == 2


def test_pose6d_capture_sends_current_membership_before_authenticated_request(web_node):
    sent = []
    web_node._control_lock = threading.RLock()
    web_node._pose6d_commands = SimpleNamespace(publish=lambda msg: sent.append(json.loads(msg.data)),
        get_subscription_count=lambda: 1)
    web_node._server.connected_client_ids = lambda: ["new-browser"]
    web_node._dispatch("new-browser", {"type": "pose6d_capture", "request_id": "capture", "client_id": "old"})
    assert sent == [{"type": "clients", "client_ids": ["new-browser"]},
                    {"type": "capture", "request_id": "capture", "client_id": "new-browser"}]
