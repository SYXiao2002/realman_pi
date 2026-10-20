"""Cached grasp snapshots accept only bounded, monotonic read-only events."""

import json

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
    send(web_node, snapshot(observation="obs-old", phase="rejected"))
    send(web_node, snapshot(phase="captured"))
    send(web_node, snapshot(phase="inferred"))
    assert web_node._grasp_evaluation["observation_id"] == "obs-1"
    assert web_node._grasp_evaluation["phase"] == "inferred"


def test_snapshot_cache_rejects_invalid_and_oversized_events(web_node):
    send(web_node, {**snapshot(), "capture_timestamp_ns": "0"})
    send(web_node, {**snapshot(), "phase": "planning"})
    web_node._grasp_evaluation_message(String(data="{"))
    web_node._grasp_evaluation_message(String(data=" " * (2 * 1024 * 1024 + 1)))
    assert web_node._grasp_evaluation is None
    assert web_node._server.events == []
