"""ROS 2 adapter for the server-owned, leased suction motion state machine."""

import json
import threading
import time

import numpy as np
from action_msgs.msg import GoalStatus
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from realman_msgs.action import ExecuteMotion
from realman_msgs.srv import GetCurrentPose, SelectFrame, TaskControl
from sensor_msgs.msg import JointState
from std_msgs.msg import String
from std_srvs.srv import Trigger
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy

from grasp_bridge.geometry import from_pose, to_pose
from pose6d.geometry import rigid
from pose6d.feedback import settled_joints
from pose6d.path import samples, screen_workspace


class MotionRosPort:
    """ROS calls used by MotionController. All waits run on its worker thread."""

    def __init__(self, node, arm, config, suction, *, state_timeout_sec=.6,
                 settle_sec=.3, max_joint_velocity_rad_sec=.01, selectable_tool_names=()):
        self.node, self.arm = node, arm
        self.config, self.suction = config, suction
        self.selectable_tool_names = set(selectable_tool_names) or {config.get("tool_name")}
        self.state_timeout_sec = float(state_timeout_sec)
        self.settle_sec = float(settle_sec)
        self.max_joint_velocity_rad_sec = float(max_joint_velocity_rad_sec)
        self.group = ReentrantCallbackGroup()
        self.pose = node.create_client(GetCurrentPose, f"/{arm}/get_current_pose", callback_group=self.group)
        self.task = node.create_client(TaskControl, f"/{arm}/task_control", callback_group=self.group)
        self.select_tool_client = node.create_client(SelectFrame, f"/{arm}/coordinates/select_tool", callback_group=self.group)
        self.stop_client = node.create_client(Trigger, f"/{arm}/stop", callback_group=self.group)
        self.action = ActionClient(node, ExecuteMotion, f"/{arm}/execute_motion", callback_group=self.group)
        self.coordinates = None
        self.coordinates_at = 0.0
        self.coordinate_lock = threading.Lock()
        self.joint_samples = []
        self.joint_lock = threading.Lock()
        self.active_goal = None
        self.goal_lock = threading.Lock()
        self.unknown_lease = False
        self.unknown_action = False
        self.lease_lock = threading.Lock()
        self.subscriptions = [
            node.create_subscription(
                String, f"/{arm}/coordinates/state", self._coordinates,
                QoSProfile(history=QoSHistoryPolicy.KEEP_LAST, depth=1,
                           reliability=QoSReliabilityPolicy.RELIABLE,
                           durability=QoSDurabilityPolicy.TRANSIENT_LOCAL),
                callback_group=self.group),
            node.create_subscription(JointState, f"/{arm}/joint_states", self._joints, 10,
                                     callback_group=self.group),
        ]

    def _coordinates(self, message):
        try:
            value = json.loads(message.data)
            if value.get("type") != "coordinate_state" or value.get("arm") != self.arm:
                return
            with self.coordinate_lock:
                self.coordinates, self.coordinates_at = value, time.monotonic()
        except (TypeError, ValueError):
            return

    def lease_uncertain(self):
        with self.lease_lock, self.goal_lock:
            return self.unknown_lease or self.unknown_action

    def action_uncertain(self):
        with self.goal_lock:
            return self.unknown_action

    def _joints(self, message):
        names = [f"joint_{index}" for index in range(1, 7)]
        positions = dict(zip(message.name, message.position))
        if len(message.name) != 6 or set(positions) != set(names) or len(positions) != 6:
            return
        stamp = int(message.header.stamp.sec) * 1_000_000_000 + int(message.header.stamp.nanosec)
        now_ns = self.node.get_clock().now().nanoseconds
        if stamp <= 0 or stamp > now_ns + 10_000_000 or now_ns - stamp > self.state_timeout_sec * 1e9:
            return
        radians = np.asarray([positions[name] for name in names], dtype=float)
        if not np.isfinite(radians).all():
            return
        velocity = None
        if len(message.velocity) == 6:
            raw_velocity = dict(zip(message.name, message.velocity))
            velocity = [raw_velocity[name] for name in names]
        if velocity is not None and not np.isfinite(velocity).all():
            return
        with self.joint_lock:
            self.joint_samples.append((time.monotonic(), radians,
                                       np.asarray(velocity, dtype=float) if velocity else None, stamp))
            cutoff = time.monotonic() - max(2.0, self.settle_sec * 4)
            self.joint_samples = [sample for sample in self.joint_samples if sample[0] >= cutoff][-4096:]

    def _wait_future(self, future, timeout, *, on_late=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if future.done():
                return future.result()
            time.sleep(.005)
        if on_late is not None:
            future.add_done_callback(on_late)
        raise TimeoutError("ROS service or action call timed out; outcome is unknown")

    def _service(self, client, request, *, on_late=None):
        timeout = float(self.config["service_timeout_sec"])
        if not client.wait_for_service(timeout_sec=timeout):
            raise TimeoutError(f"service {getattr(client, 'srv_name', 'requested ROS service')} is unavailable")
        future = client.call_async(request)
        if on_late is not None:
            timed_out = {"value": False}
            lock = threading.Lock()

            def completed(done):
                with lock:
                    late = timed_out["value"]
                if late:
                    on_late(done)

            try:
                return self._wait_future(future, timeout)
            except TimeoutError:
                if future.done():
                    return future.result()
                with lock:
                    timed_out["value"] = True
                future.add_done_callback(completed)
                raise
        return self._wait_future(future, timeout)

    def context(self):
        with self.lease_lock:
            if self.unknown_lease:
                request = TaskControl.Request()
                request.operation = TaskControl.Request.STATUS
                response = self._service(self.task, request)
                if not response.success or response.leased or response.command_busy:
                    raise ValueError("an earlier task lease outcome is unresolved; arm remains locked")
                self.unknown_lease = False
        with self.coordinate_lock:
            coordinates, received = self.coordinates, self.coordinates_at
        if coordinates is None or time.monotonic() - received > 2.5:
            raise ValueError("active robot coordinate/tool state is missing or stale")
        expected = self.config.get("tool_name")
        if (coordinates.get("motion_allowed") is not True or coordinates.get("matched") is not True
                or coordinates.get("tool_matched") is not True or coordinates.get("current_tool") != expected):
            raise ValueError("controller tool is not verified as the configured suction tool")
        return {key: coordinates.get(key) for key in
                ("arm", "current_tool", "current_work", "motion_allowed", "matched", "tool_matched", "tool")}

    def _task_status(self, token=""):
        request = TaskControl.Request()
        request.operation, request.task_token = TaskControl.Request.STATUS, token
        response = self._service(self.task, request)
        if not response.success:
            raise ValueError("driver task lease status is not valid: " + response.message)
        return response

    def select_tool(self, name):
        if name not in self.selectable_tool_names:
            raise ValueError("tool is not available in this arm's coordinate profile")
        status = self._task_status()
        if status.leased or status.command_busy:
            raise ValueError("arm is busy; controller tool selection refused")
        expected = name
        with self.coordinate_lock:
            state = self.coordinates
            received = self.coordinates_at
        if state is None or time.monotonic() - received > 2.5:
            raise ValueError("active robot coordinate/tool state is missing or stale")
        if (state.get("current_tool") == expected and state.get("expected_tool") == expected
                and state.get("tool_matched") is True and state.get("matched") is True
                and state.get("motion_allowed") is True):
            return {"current_tool": expected, "motion_allowed": state.get("motion_allowed") is True}

        request = SelectFrame.Request()
        request.name = name
        response = self._service(self.select_tool_client, request)
        if (not response.success or response.api2_status != 0 or response.active_name != expected):
            raise ValueError("controller tool selection was not verified: " + response.message)

        deadline = time.monotonic() + float(self.config["service_timeout_sec"])
        while time.monotonic() < deadline:
            with self.coordinate_lock:
                state = self.coordinates
                received = self.coordinates_at
            if (state is not None and time.monotonic() - received <= 2.5
                    and state.get("current_tool") == expected
                    and state.get("expected_tool") == expected
                    and state.get("tool_matched") is True
                    and state.get("matched") is True
                    and state.get("motion_allowed") is True):
                return {"current_tool": expected, "motion_allowed": True}
            time.sleep(.01)
        raise TimeoutError("tool selection completed but matching coordinate state was not observed")

    def acquire(self, owner, ttl):
        with self.lease_lock:
            if self.unknown_lease:
                raise ValueError("previous task lease outcome is unresolved")
            request = TaskControl.Request()
            request.operation, request.owner_id, request.ttl_sec = TaskControl.Request.ACQUIRE, owner, float(ttl)

            def release_late(done):
                try:
                    response = done.result()
                    if not response.success or not response.task_token:
                        return
                    release = TaskControl.Request()
                    release.operation, release.task_token = TaskControl.Request.RELEASE, response.task_token
                    pending = self.task.call_async(release)

                    def confirmed(released):
                        try:
                            result = released.result()
                            with self.lease_lock:
                                if result.success and not result.command_busy:
                                    self.unknown_lease = False
                        except Exception:
                            pass
                    pending.add_done_callback(confirmed)
                except Exception:
                    pass

            self.unknown_lease = True
            try:
                response = self._service(self.task, request, on_late=release_late)
            except Exception:
                # A timeout may leave a lease behind; only STATUS can clear it.
                raise
            if not response.success or not response.task_token or not response.leased:
                self.unknown_lease = False
                raise ValueError("driver refused task lease: " + response.message)
            self.unknown_lease = False
            return response.task_token

    def renew(self, token, ttl):
        request = TaskControl.Request()
        request.operation, request.task_token, request.ttl_sec = TaskControl.Request.RENEW, token, float(ttl)
        response = self._service(self.task, request)
        if not response.success or not response.leased or response.expired:
            raise ValueError("driver refused task lease renewal: " + response.message)

    def validate(self, token):
        response = self._task_status(token)
        if not response.leased or response.expired:
            raise ValueError("driver task lease was revoked or expired")

    def release(self, token):
        with self.goal_lock:
            if self.unknown_action:
                raise ValueError("motion action terminal state is unknown; retaining task lease")
        # STATUS without a token reports busy/expired state without rejecting an
        # expired token; RELEASE itself is the driver's authoritative token check.
        response = self._task_status("")
        if response.command_busy:
            raise ValueError("driver command has not drained; retaining task lease")
        if not response.leased:
            return
        request = TaskControl.Request()
        request.operation, request.task_token = TaskControl.Request.RELEASE, token
        released = self._service(self.task, request)
        if not released.success or released.command_busy:
            raise ValueError("driver did not confirm lease release: " + released.message)

    def _settled_joints(self):
        deadline = time.monotonic() + float(self.config["timeout_sec"])
        problem = "no valid joint feedback"
        while time.monotonic() < deadline:
            with self.joint_lock:
                samples_now = list(self.joint_samples)
            try:
                return settled_joints(samples_now, now=time.monotonic(),
                                      now_ns=self.node.get_clock().now().nanoseconds,
                                      freshness_sec=self.state_timeout_sec, settle_sec=self.settle_sec,
                                      max_speed=self.max_joint_velocity_rad_sec)
            except ValueError as exc:
                problem = str(exc)
            time.sleep(.02)
        raise TimeoutError("fresh, stationary six-joint feedback is unavailable: " + problem)

    def current(self):
        self.context()
        self._settled_joints()
        request = GetCurrentPose.Request()
        request.reference_type, request.reference_name = GetCurrentPose.Request.BASE, "base"
        response = self._service(self.pose, request)
        if not response.success or response.api2_status != 0:
            raise ValueError("BASE TCP readback failed: " + response.message)
        joints = np.asarray(response.current_joint_degrees, dtype=float)
        expected_degrees = np.rad2deg(self._settled_joints())
        if (joints.shape != (6,) or not np.isfinite(joints).all()
                or np.max(np.abs(joints - expected_degrees)) > 1.0):
            raise ValueError("TCP readback does not match fresh stationary joint feedback")
        return rigid(from_pose(response.pose_position_m, response.pose_quaternion_wxyz))

    def check_path(self, actual, target, session, guard):
        poses = list(samples(actual, target, self.config))
        screen_workspace(poses, self.config, self.suction, guard)

    def execute(self, target, token, cancel, feedback, *, command="movel"):
        commands = {"movel": ExecuteMotion.Goal.MOVEL, "movej_p": ExecuteMotion.Goal.MOVEJ_P}
        if command not in commands:
            raise ValueError("unsupported motion command")
        label = command.upper()
        if not self.action.wait_for_server(timeout_sec=self.config["service_timeout_sec"]):
            raise TimeoutError("execute_motion action is unavailable")
        goal = ExecuteMotion.Goal()
        goal.command = commands[command]
        goal.reference_type, goal.reference_name = ExecuteMotion.Goal.BASE, "base"
        goal.pose_position_m, goal.pose_quaternion_wxyz = to_pose(target)
        goal.velocity_percent = self.config["velocity_percent"]
        goal.blend_radius_percent, goal.connect = 0, False
        goal.timeout_sec, goal.task_token = float(self.config["timeout_sec"]), token
        with self.goal_lock:
            self.unknown_action = True
        future = self.action.send_goal_async(goal)
        try:
            handle = self._wait_future(future, self.config["service_timeout_sec"])
        except Exception:
            if not future.done():
                future.add_done_callback(self._cancel_late_goal)
            try:
                self.interrupt()
            except Exception:
                pass
            raise
        if not handle.accepted:
            with self.goal_lock:
                self.unknown_action = False
            raise ValueError(f"driver rejected the {label} action")
        with self.goal_lock:
            self.active_goal = handle
        try:
            result_future = handle.get_result_async()
        except Exception:
            self.interrupt()
            raise
        deadline = time.monotonic() + self.config["timeout_sec"] + self.config["service_timeout_sec"]
        cancel_sent = False
        try:
            while time.monotonic() < deadline:
                if cancel.is_set() and not cancel_sent:
                    cancel_sent = True
                    handle.cancel_goal_async()
                if result_future.done():
                    try:
                        wrapped = result_future.result()
                    except Exception:
                        self.interrupt()
                        raise
                    result = wrapped.result
                    if (wrapped.status != GoalStatus.STATUS_SUCCEEDED or not result.success
                            or result.terminal_state != ExecuteMotion.Result.SUCCEEDED or result.api2_status != 0):
                        with self.goal_lock:
                            self.unknown_action = False
                        raise ValueError(f"{label} did not reach a confirmed success terminal state: " + result.message)
                    with self.goal_lock:
                        self.unknown_action = False
                    return
                time.sleep(.01)
            if not cancel_sent:
                handle.cancel_goal_async()
            result_future.add_done_callback(self._observe_late_result)
            self.interrupt()
            raise TimeoutError(f"{label} terminal outcome is unknown; retaining task lease")
        finally:
            with self.goal_lock:
                self.active_goal = None

    def _cancel_late_goal(self, future):
        try:
            handle = future.result()
            if not handle.accepted:
                with self.goal_lock:
                    self.unknown_action = False
                return
            with self.goal_lock:
                self.active_goal = handle
            handle.cancel_goal_async()
            self.interrupt()
            result = handle.get_result_async()
            result.add_done_callback(self._observe_late_result)
        except Exception:
            # Keep the task lease quarantined until operator recovery.
            pass

    def _observe_late_result(self, future):
        try:
            future.result()
        except Exception:
            return
        with self.goal_lock:
            self.unknown_action = False
            self.active_goal = None

    def interrupt(self):
        with self.goal_lock:
            goal = self.active_goal
        if goal is not None:
            try:
                goal.cancel_goal_async()
            except Exception:
                pass
        if not self.stop_client.wait_for_service(timeout_sec=self.config["service_timeout_sec"]):
            raise TimeoutError("driver stop service is unavailable; motion outcome remains unknown")
        future = self.stop_client.call_async(Trigger.Request())
        response = self._wait_future(future, self.config["service_timeout_sec"])
        if not response.success:
            raise ValueError("driver stop was not confirmed: " + response.message)

    def check_stop_drained(self, token):
        response = self._task_status(token)
        return not response.command_busy

