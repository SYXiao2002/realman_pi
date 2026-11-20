"""ROS vacuum events retain freshness across Web connection and restarts."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("rclpy")
import rclpy
from rclpy.node import Node
from std_msgs.msg import String

from realman_web_control.web_control_node import WebControlNode


@pytest.fixture
def node():
    rclpy.init()
    value = WebControlNode.__new__(WebControlNode)
    Node.__init__(value, "vacuum_event_test")
    value._control_lock = threading.RLock()
    value._vacuum_states = {}
    events = []
    value._server = SimpleNamespace(send_event=lambda event, client_id=None: events.append((event, client_id)))
    value.events = events
    yield value
    Node.destroy_node(value)
    rclpy.shutdown()


def state(node, **values):
    return {"type": "vacuum_state", "arm": "r", "enabled": True, "available": True,
            "holding": True, "raw_input": 0, "io": 3, "stale_after_sec": 2.0,
            "stamp_ns": node.get_clock().now().nanoseconds, **values}


def test_live_state_broadcast_and_new_client_replay_preserve_deadline(node):
    node._vacuum_state_message("r", String(data=json.dumps(state(node))))
    event, client = node.events[-1]
    assert event["holding"] is True and client is None
    node._vacuum_states["r"] = (event, time.monotonic() - 1)
    node._send_cached_vacuum_state("new-client")
    replay, client = node.events[-1]
    assert client == "new-client" and 0 < replay["stale_after_sec"] <= 1
    node._vacuum_states["r"] = (event, time.monotonic() - 3)
    node.events.clear()
    node._send_cached_vacuum_state("late-client")
    assert not node.events


def test_retained_old_ros_message_cannot_restore_green_on_web_restart(node):
    node._vacuum_state_message("r", String(data=json.dumps(state(node, stamp_ns=1))))
    event, _ = node.events[-1]
    assert event["available"] is False and event["holding"] is None
    assert event["message"] == "状态超时"


def test_disabled_configuration_replays_even_without_live_feedback(node):
    event = state(node, enabled=False, available=False, holding=None, raw_input=None)
    node._vacuum_states["l"] = (event, time.monotonic() - 100)
    node._send_cached_vacuum_state("new-client")
    assert node.events[-1][0]["enabled"] is False


@pytest.mark.parametrize("payload", [
    "{", "[]", '{"type":"other"}',
    '{"type":"vacuum_state","arm":"l"}',
    '{"type":"vacuum_state","arm":"r","stale_after_sec":"invalid"}',
])
def test_invalid_or_wrong_arm_events_are_ignored(node, payload):
    node._vacuum_state_message("r", String(data=payload))
    assert not node.events and not node._vacuum_states
