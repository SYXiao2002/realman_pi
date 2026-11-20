from concurrent.futures import Future
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("rclpy")
from std_srvs.srv import SetBool, Trigger

from realman_web_control.protocol import ProtocolError
from realman_web_control.web_control_node import WebControlNode


class Events:
    def __init__(self, *, read_only=False):
        self.read_only = read_only
        self.events = []

    def send_event(self, event, client_id=None):
        self.events.append((event, client_id))


class Client:
    def __init__(self):
        self.future = Future()
        self.requests = []

    def service_is_ready(self):
        return True

    def call_async(self, request):
        self.requests.append(request)
        return self.future


def _bridge(*, read_only=False):
    client = Client()
    node = SimpleNamespace(
        _tool_power_clients={"set": client, "status": client},
        _server=Events(read_only=read_only),
    )
    node._tool_power_result = lambda *args: WebControlNode._tool_power_result(node, *args)
    return node, client


def test_tool_power_bridge_maps_bool_to_setbool_and_returns_verified_state():
    node, client = _bridge()
    WebControlNode._tool_power_command(node, "browser-1", {
        "type": "tool_power_set", "request_id": "set-1", "enabled": True,
    })
    assert isinstance(client.requests[0], SetBool.Request)
    assert client.requests[0].data is True
    assert node._server.events[-1][0]["state"] == "requested"

    client.future.set_result(SimpleNamespace(
        success=True,
        message=json.dumps({"available": True, "voltage_type": 3, "voltage_v": 24, "enabled": True}),
    ))
    event, recipient = node._server.events[-1]
    assert recipient == "browser-1"
    assert event["type"] == "tool_power_state"
    assert event["request_id"] == "set-1"
    assert event["voltage_v"] == 24


def test_tool_power_status_uses_read_only_trigger_and_read_only_mode_rejects_set():
    node, client = _bridge(read_only=True)
    WebControlNode._tool_power_command(node, "browser-1", {
        "type": "tool_power_status", "request_id": "read-1",
    })
    assert isinstance(client.requests[0], Trigger.Request)

    with pytest.raises(ProtocolError, match="read-only"):
        WebControlNode._tool_power_command(node, "browser-1", {
            "type": "tool_power_set", "request_id": "set-2", "enabled": False,
        })
    assert len(client.requests) == 1
