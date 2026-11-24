"""ROS action boundary tests use fake futures and never connect to robot services."""

from types import SimpleNamespace
import time

import pytest

pytest.importorskip("rclpy")
pytest.importorskip("realman_msgs")

from action_msgs.msg import GoalStatus
from realman_msgs.action import ExecuteMotion
from realman_msgs.srv import SetToolPower
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
        self.now_ns = 10_000_000_000
        self.logs = []

    def create_client(self, _type, name, callback_group=None):
        self.client_names.append(name)
        if name.endswith("/stop"):
            return self.stop_client
        return Client()

    def create_subscription(self, *_args, **_kwargs):
        return object()

    def get_clock(self):
        return SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=self.now_ns))

    def get_logger(self):
        return SimpleNamespace(info=self.logs.append)


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


@pytest.mark.parametrize("enabled", [True, False])
def test_motion_ros_tool_power_uses_task_token_and_verified_readback(monkeypatch, enabled):
    import json

    adapter, _stop = port(monkeypatch, Action())
    state = {"available": True, "voltage_v": 24 if enabled else 0}
    adapter.tool_power = Client(SetToolPower.Response(success=True, message=json.dumps(state)))
    assert adapter.set_tool_power(enabled, "lease-token") == state
    request = adapter.tool_power.requests[0]
    assert request.enabled == enabled and request.task_token == "lease-token"
    assert "/r/tool_power_control" in adapter.node.client_names


@pytest.mark.parametrize(("success", "payload"), [
    (False, '{"available":true,"voltage_v":24,"message":"lease denied"}'),
    (True, '{"available":true,"voltage_v":0}'),
    (True, '{"available":false,"voltage_v":24}'),
    (True, 'not-json'),
])
def test_motion_ros_tool_power_rejects_unverified_output(monkeypatch, success, payload):
    adapter, _stop = port(monkeypatch, Action())
    adapter.tool_power = Client(SetToolPower.Response(success=success, message=payload))
    with pytest.raises(ValueError):
        adapter.set_tool_power(True, "lease-token")


def test_motion_ros_vacuum_holding_requires_fresh_available_switch_state(monkeypatch):
    import json
    from std_msgs.msg import String

    adapter, _stop = port(monkeypatch, Action())
    event = {"type": "vacuum_state", "arm": "r", "enabled": True, "available": True,
             "holding": False, "stamp_ns": adapter.node.now_ns, "stale_after_sec": 2.0}
    adapter._vacuum_state_event(String(data=json.dumps(event)))
    assert adapter.vacuum_holding() is False

    event["holding"] = True
    adapter._vacuum_state_event(String(data=json.dumps(event)))
    assert adapter.vacuum_holding() is True

    adapter.vacuum_state_at -= 3
    assert adapter.vacuum_holding() is None

    event["available"] = False
    adapter._vacuum_state_event(String(data=json.dumps(event)))
    assert adapter.vacuum_holding() is None

    event["available"] = True
    event["stamp_ns"] -= 2_000_000_000
    adapter._vacuum_state_event(String(data=json.dumps(event)))
    assert adapter.vacuum_holding() is None


@pytest.mark.parametrize(("updates", "expected", "elapsed"), [
    ([(.2, False), (.4, True), (.6, True)], True, .6),
    ([(.2, False), (.4, False), (.8, True), (1.0, True)], True, 1.0),
    ([(.2, False), (.4, False), (.6, False)], False, 1.5),
    ([(.2, True), (.4, False), (.6, False)], False, 1.5),
    ([(.2, True)], None, 1.5),
    ([(.2, False)], None, 1.5),
    ([], None, 1.5),
    ([(.2, False), (.4, False), (.6, None)], None, 1.5),
])
def test_post_lift_vacuum_confirmation_waits_for_distinct_new_samples(
        monkeypatch, updates, expected, elapsed):
    import json
    from std_msgs.msg import String

    adapter, _stop = port(monkeypatch, Action())
    adapter.config["vacuum_confirm_timeout_sec"] = 1.5
    started_ns = adapter.node.now_ns
    timer = {"now": 10.0}
    schedule = list(updates)
    monkeypatch.setattr("pose6d_bridge.motion_ros.time.monotonic", lambda: timer["now"])
    # A cached holding result before lift is not evidence of the state after lift.
    cached = {"type": "vacuum_state", "arm": "r", "enabled": True, "available": True,
              "holding": True, "raw_input": 0, "active_level": 0, "io": 1,
              "stamp_ns": started_ns - 1, "stale_after_sec": 2.0}
    adapter._vacuum_state_event(String(data=json.dumps(cached)))

    class Cancel:
        def is_set(self): return False
        def wait(self, delay):
            timer["now"] += delay
            passed = round(timer["now"] - 10, 6)
            adapter.node.now_ns = started_ns + round(passed * 1e9)
            while schedule and schedule[0][0] <= passed:
                stamp_sec, holding = schedule.pop(0)
                event = {**cached, "stamp_ns": started_ns + round(stamp_sec * 1e9),
                         "holding": holding, "available": holding is not None,
                         "raw_input": 0 if holding else 1}
                adapter._vacuum_state_event(String(data=json.dumps(event)))

    result = adapter.wait_vacuum_holding(Cancel())
    assert result is expected
    assert timer["now"] - 10 == pytest.approx(elapsed, abs=.03)
    assert f"result={expected}" in adapter.node.logs[-1]


def test_post_lift_vacuum_confirmation_can_be_stopped(monkeypatch):
    import threading

    adapter, _stop = port(monkeypatch, Action())
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ValueError, match="session stopped"):
        adapter.wait_vacuum_holding(cancel)


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


@pytest.mark.parametrize(("command", "expected"), [
    ("movel", ExecuteMotion.Goal.MOVEL),
    ("movej_p", ExecuteMotion.Goal.MOVEJ_P),
])
def test_motion_ros_port_sends_selected_base_command_with_token_and_requires_terminal_success(
        monkeypatch, command, expected):
    action = Action(Handle(goal_result()))
    adapter, _stop = port(monkeypatch, action)
    adapter.config["timeout_sec"] = 30
    target = [[1, 0, 0, .1], [0, 1, 0, .2], [0, 0, 1, .3], [0, 0, 0, 1]]
    import numpy as np
    adapter.execute(np.asarray(target), "lease-token", __import__("threading").Event(), lambda _f: None,
                    command=command, velocity_percent=13)
    sent = action.goal
    assert sent.command == expected
    assert sent.reference_type == ExecuteMotion.Goal.BASE and sent.reference_name == "base"
    assert sent.velocity_percent == 13 and sent.blend_radius_percent == 0 and not sent.connect
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


@pytest.mark.parametrize("readback_error", [0, .3])
def test_movej_uses_six_joint_targets_and_requires_settled_joint_arrival(monkeypatch, readback_error):
    import numpy as np
    import threading

    action = Action(Handle(goal_result()))
    adapter, _stop = port(monkeypatch, action)
    joints = np.array([95.045, 11.965, 106.228, -1.437, 54.787, 10.14])
    adapter._settled_joints = lambda: np.deg2rad(joints + readback_error)
    if readback_error:
        with pytest.raises(ValueError, match="joint readback"):
            adapter.execute(joints, "lease-token", threading.Event(), lambda _f: None, command="movej")
    else:
        adapter.execute(joints, "lease-token", threading.Event(), lambda _f: None, command="movej")
    assert action.goal.command == ExecuteMotion.Goal.MOVEJ
    assert list(action.goal.joint_degrees) == pytest.approx(joints)
    assert action.goal.task_token == "lease-token"
    assert adapter.action_uncertain() is False


@pytest.mark.parametrize("joints", [[1] * 5, [float("nan")] * 6])
def test_movej_rejects_invalid_joint_targets_before_submitting(monkeypatch, joints):
    import threading

    action = Action(Handle(goal_result()))
    adapter, _stop = port(monkeypatch, action)
    with pytest.raises(ValueError, match="six finite"):
        adapter.execute(joints, "token", threading.Event(), lambda _f: None, command="movej")
    assert action.goal is None


@pytest.mark.parametrize("payload, success, valid", [
    ('{"api2_status":0,"joint_degrees":[1,2,3,4,5,6]}', True, True),
    ('{"api2_status":-2,"joint_degrees":[]}', False, False),
    ('{"api2_status":0,"joint_degrees":[1,2]}', True, False),
    ('invalid', True, False),
])
def test_initial_posture_query_validates_controller_reply(monkeypatch, payload, success, valid):
    adapter, _stop = port(monkeypatch, Action())
    adapter.init_pose = Client(Trigger.Response(success=success, message=payload))
    if valid:
        assert list(adapter.initial_joints()) == [1, 2, 3, 4, 5, 6]
    else:
        with pytest.raises(ValueError):
            adapter.initial_joints()


@pytest.mark.parametrize("success", [True, False])
def test_fixed_joint_target_fk_uses_exact_configured_degrees(monkeypatch, success):
    import numpy as np
    from realman_msgs.srv import ForwardKinematics
    from grasp_bridge.geometry import to_pose

    adapter, _stop = port(monkeypatch, Action())
    expected = np.diag([1., -1., -1., 1.])
    expected[:3, 3] = [.064173, -.274147, .040462]
    position, quaternion = to_pose(expected)
    joints = [95.045, 11.965, 106.228, -1.437, 54.787, 10.14]
    adapter.fk = Client(ForwardKinematics.Response(success=success, api2_status=0 if success else -2,
        pose_position_m=position, pose_quaternion_wxyz=quaternion, message="FK unavailable"))
    if success:
        np.testing.assert_allclose(adapter.forward_kinematics(joints), expected)
    else:
        with pytest.raises(ValueError, match="forward kinematics failed"):
            adapter.forward_kinematics(joints)
    sent = adapter.fk.requests[0]
    assert list(sent.joint_degrees) == joints
    assert sent.reference_type == sent.BASE and sent.reference_name == "base"


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
        adapter.execute(np.eye(4), "lease-token", threading.Event(), lambda _f: None, command="movej_p")
    assert adapter.action_uncertain() is False
