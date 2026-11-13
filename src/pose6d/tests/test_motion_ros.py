"""ROS action boundary tests use fake futures and never connect to robot services."""

from types import SimpleNamespace
import time

import pytest

pytest.importorskip("rclpy")
pytest.importorskip("realman_msgs")

from action_msgs.msg import GoalStatus
from realman_msgs.action import ExecuteMotion
from std_srvs.srv import Trigger

from pose6d_bridge.motion_ros import MotionRosPort


class Future:
    def __init__(self, value=None, *, finished=True):
        self.value, self.finished, self.callbacks = value, finished, []

    def done(self):
        return self.finished

    def result(self):
        if not self.finished:
            raise RuntimeError("future is pending")
        if isinstance(self.value, Exception):
            raise self.value
        return self.value

    def add_done_callback(self, callback):
        self.callbacks.append(callback)
        if self.finished:
            callback(self)

    def finish(self, value):
        self.value, self.finished = value, True
        for callback in list(self.callbacks):
            callback(self)


class Client:
    def __init__(self, response=None):
        self.response = response
        self.requests = []

    def wait_for_service(self, timeout_sec):
        return True

    def call_async(self, request):
        self.requests.append(request)
        return Future(self.response)


class Handle:
    accepted = True

    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.cancel_count = 0

    def get_result_async(self):
        return Future(self.wrapped)

    def cancel_goal_async(self):
        self.cancel_count += 1
        return Future(SimpleNamespace())


class Action:
    def __init__(self, handle=None, *, pending=False):
        self.handle = handle
        self.pending = pending
        self.goal = None
        self.future = None

    def wait_for_server(self, timeout_sec):
        return True

    def send_goal_async(self, goal):
        self.goal = goal
        if self.pending:
            self.future = Future(finished=False)
            return self.future
        return Future(self.handle)


class Node:
    def __init__(self, stop_client):
        self.stop_client = stop_client
        self.client_names = []

    def create_client(self, _type, name, callback_group=None):
        self.client_names.append(name)
        if name.endswith("/stop"):
            return self.stop_client
        return Client()

    def create_subscription(self, *_args, **_kwargs):
        return object()


def port(monkeypatch, action, timeout=.03, selectable_tool_names=()):
    stop = Client(Trigger.Response(success=True, message="stopped"))
    monkeypatch.setattr("pose6d_bridge.motion_ros.ActionClient", lambda *_args, **_kwargs: action)
    config = {"service_timeout_sec": timeout, "timeout_sec": .1, "velocity_percent": 7}
    return MotionRosPort(Node(stop), "r", config, {"cup_diameter_m": .01},
                         selectable_tool_names=selectable_tool_names), stop


def test_motion_ros_port_selects_only_idle_configured_tool(monkeypatch):
    adapter, _stop = port(monkeypatch, Action(), selectable_tool_names={"tcpgrip", "tcpsuck"})
    adapter.coordinates = {"current_tool": "tcpgrip", "expected_tool": "tcpgrip", "tool_matched": True,
                           "matched": True, "motion_allowed": True}
    adapter.coordinates_at = time.monotonic()
    adapter._task_status = lambda: SimpleNamespace(leased=False, command_busy=False)
    adapter._service = lambda client, request: SimpleNamespace(
        success=True, api2_status=0, active_name=request.name, message="verified")

    def publish_selected(name):
        adapter.coordinates = {"current_tool": name, "expected_tool": name, "tool_matched": True,
                               "matched": True, "motion_allowed": True}
        adapter.coordinates_at = time.monotonic()

    original_service = adapter._service
    def select(client, request):
        response = original_service(client, request)
        publish_selected(request.name)
        return response
    adapter._service = select
    assert adapter.select_tool("tcpsuck") == {"current_tool": "tcpsuck", "motion_allowed": True}

    adapter._task_status = lambda: SimpleNamespace(leased=True, command_busy=False)
    with pytest.raises(ValueError, match="arm is busy"):
        adapter.select_tool("tcpgrip")


def goal_result(status=GoalStatus.STATUS_SUCCEEDED, *, success=True, terminal=ExecuteMotion.Result.SUCCEEDED):
    result = ExecuteMotion.Result()
    result.success, result.terminal_state, result.api2_status, result.message = success, terminal, 0, "done"
    return SimpleNamespace(status=status, result=result)


def test_motion_path_checks_workspace_without_ik_service(monkeypatch):
    import numpy as np

    adapter, _stop = port(monkeypatch, Action())
    adapter.config.update({"cartesian_sample_step_m": .005, "angular_sample_step_rad": .05,
                           "workspace_m": [[-1, -1, -1], [1, 1, 1]],
                           "T_contact_tcp": np.eye(4).tolist(), "collision_margin_m": .001})
    adapter.suction.update({"body_radius_m": .005, "body_length_m": .04})
    assert "/r/solve_ik" not in adapter.node.client_names
    start, target = np.eye(4), np.eye(4)
    target[0, 3] = .01
    adapter.check_path(start, target, {}, lambda: None)
    target[0, 3] = 1.01
    with pytest.raises(ValueError, match="workspace"):
        adapter.check_path(start, target, {}, lambda: None)


def test_motion_ros_port_sends_only_base_movel_with_token_and_requires_terminal_success(monkeypatch):
    action = Action(Handle(goal_result()))
    adapter, _stop = port(monkeypatch, action)
    adapter.config["timeout_sec"] = 30
    target = [[1, 0, 0, .1], [0, 1, 0, .2], [0, 0, 1, .3], [0, 0, 0, 1]]
    import numpy as np
    adapter.execute(np.asarray(target), "lease-token", __import__("threading").Event(), lambda _f: None)
    sent = action.goal
    assert sent.command == ExecuteMotion.Goal.MOVEL
    assert sent.reference_type == ExecuteMotion.Goal.BASE and sent.reference_name == "base"
    assert sent.velocity_percent == 7 and sent.blend_radius_percent == 0 and not sent.connect
    assert sent.timeout_sec == 30.0
    assert sent.task_token == "lease-token" and list(sent.pose_position_m) == pytest.approx([.1, .2, .3])
    assert adapter.action_uncertain() is False


def test_motion_ros_port_accepts_integer_lease_ttl(monkeypatch):
    adapter, _stop = port(monkeypatch, Action())
    adapter.task = Client(SimpleNamespace(success=True, task_token="token", leased=True,
                                          expired=False, message=""))
    assert adapter.acquire("client", 6) == "token"
    adapter.renew("token", 6)
    assert [request.ttl_sec for request in adapter.task.requests] == [6.0, 6.0]


def test_motion_ros_port_quarantines_pending_goal_and_stops_without_retry(monkeypatch):
    action = Action(pending=True)
    adapter, stop = port(monkeypatch, action, timeout=.01)
    import numpy as np
    import threading
    target = np.eye(4)
    with pytest.raises(TimeoutError, match="unknown"):
        adapter.execute(target, "lease-token", threading.Event(), lambda _f: None)
    assert adapter.action_uncertain() is True
    assert len(stop.requests) == 1 and len(action.goal.pose_position_m) == 3
    rejected = Handle(goal_result())
    rejected.accepted = False
    action.future.finish(rejected)
    assert adapter.action_uncertain() is False


def test_motion_ros_port_rejects_action_without_success_status(monkeypatch):
    action = Action(Handle(goal_result(GoalStatus.STATUS_ABORTED, success=False,
                                       terminal=ExecuteMotion.Result.ABORTED)))
    adapter, _stop = port(monkeypatch, action)
    import numpy as np
    import threading
    with pytest.raises(ValueError, match="confirmed success"):
        adapter.execute(np.eye(4), "lease-token", threading.Event(), lambda _f: None)
    assert adapter.action_uncertain() is False
