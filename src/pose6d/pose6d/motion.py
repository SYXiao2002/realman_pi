"""Server-owned suction motion. Robot operations are supplied by a ROS port.

Only successful Action completion AND a fresh, settled TCP readback commit x.
The task lease spans preparation and all jogs. Unknown outcomes retain the lease
until the driver confirms it can be released; no command is automatically retried.
"""

import math
import threading
import time
import uuid

import numpy as np

from .geometry import configured_transform, rigid, suction_pose
from .jog import JogSession


def positive(config, key):
    value = config.get(key)
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"configure a positive {key}")
    return float(value)


def validate_config(config, suction):
    if config.get("enabled") is not True:
        raise ValueError("suction motion is disabled in pose6d_bridge.yaml")
    if not isinstance(config.get("tool_name"), str) or not config["tool_name"]:
        raise ValueError("configure the controller suction tool_name")
    configured_transform(config.get("T_contact_tcp"))
    lo, hi = config.get("min_distance_m"), config.get("max_distance_m")
    contact_offset = config.get("contact_offset_m", 0.0)
    prepare_distance = config.get("prepare_distance_m")
    if (type(lo) not in (int, float) or type(hi) not in (int, float)
            or not math.isfinite(lo) or not math.isfinite(hi) or not lo < 0
            or type(prepare_distance) not in (int, float) or not math.isfinite(prepare_distance)
            or not 0 < prepare_distance <= hi):
        raise ValueError("configure signed min_distance_m < 0 and max_distance_m >= prepare_distance_m")
    if (type(contact_offset) not in (int, float) or not math.isfinite(contact_offset)
            or not 0 <= contact_offset <= -lo):
        raise ValueError("configure contact_offset_m between 0 and -min_distance_m")
    for key in ("max_step_m", "timeout_sec", "position_tolerance_m", "angular_tolerance_rad",
                "cartesian_sample_step_m", "angular_sample_step_rad",
                "collision_margin_m",
                "lease_ttl_sec", "service_timeout_sec"):
        positive(config, key)
    if "read_service_timeout_sec" in config:
        positive(config, "read_service_timeout_sec")
    for key in ("velocity_percent", "prepare_velocity_percent", "approach_velocity_percent",
                "contact_velocity_percent", "lift_velocity_percent",
                "home_velocity_percent", "place_velocity_percent", "return_velocity_percent"):
        if type(config.get(key)) is not int or not 1 <= config[key] <= 100:
            raise ValueError(f"configure {key} in 1..100")
    second_prepare = config.get("second_prepare_distance_m")
    if (type(second_prepare) not in (int, float) or not math.isfinite(second_prepare)
            or not 0 <= second_prepare < prepare_distance):
        raise ValueError("configure second_prepare_distance_m between zero and prepare_distance_m")
    positive(config, "lift_height_m")
    joint_tolerance = config.get("joint_tolerance_deg", .25)
    if (type(joint_tolerance) not in (int, float) or not math.isfinite(joint_tolerance)
            or joint_tolerance <= 0):
        raise ValueError("configure a positive joint_tolerance_deg")
    vacuum_timeout = config.get("vacuum_confirm_timeout_sec", 1.5)
    if (type(vacuum_timeout) not in (int, float) or not math.isfinite(vacuum_timeout)
            or vacuum_timeout <= 0):
        raise ValueError("configure a positive vacuum_confirm_timeout_sec")
    bounds = np.asarray(config.get("workspace_m"), dtype=float)
    if bounds.shape != (2, 3) or not np.isfinite(bounds).all() or not np.all(bounds[0] < bounds[1]):
        raise ValueError("configure measured workspace_m bounds")
    place = np.asarray(config.get("place_joint_degrees"), dtype=float)
    if place.shape != (6,) or not np.isfinite(place).all():
        raise ValueError("configure six finite place_joint_degrees")
    if config.get("home_joint_degrees") is not None:
        home = np.asarray(config["home_joint_degrees"], dtype=float)
        if home.shape != (6,) or not np.isfinite(home).all():
            raise ValueError("configure home_joint_degrees as null or six finite joint angles")
    for key in ("cup_diameter_m", "body_radius_m", "body_length_m"):
        positive(suction, key)
    if suction["body_radius_m"] < suction["cup_diameter_m"] / 2:
        raise ValueError("body cylinder must enclose the suction cup")
    if config.get("allow_partial_scene") is not True:
        raise ValueError("no path collision detection requires explicit scene review configuration")


class MotionController:
    def __init__(self, port, config, suction, *, clock=time.monotonic):
        self.port, self.config, self.suction, self.clock = port, config, suction, clock
        self.lock = threading.RLock()
        self.inflight = threading.Lock()
        self.cancel = threading.Event()
        self.review = None
        self.session = None
        self.renew_at = 0
        self.reason = "select and review a pose"
        self.interrupted_session = None

    def load(self, owner, observation, grasps, result_id):
        with self.lock:
            if self.session is not None:
                raise ValueError("close the existing motion session before replacing a result")
            context = None
            if self.config.get("enabled") is True:
                try:
                    validate_config(self.config, self.suction)
                    context = self.port.context()
                except Exception:
                    # Inference and visualization remain available when motion
                    # parameters or live controller state are incomplete.
                    context = None
            self.review = (owner, observation, grasps, result_id, context)
            self.reason = "review pose and warnings before preparation"
        return self.readiness()

    def invalidate_review(self, owner):
        with self.lock:
            if self.session is not None:
                raise ValueError("close the active motion session before replacing a review")
            if self.review is not None and self.review[0] != owner:
                raise ValueError("pose review belongs to a different client")
            self.review = None
            self.reason = "select and review a pose"

    def select_tool(self, owner, tool_name, allowed_tool_names):
        if not self.inflight.acquire(False):
            raise ValueError("motion busy; tool selection refused")
        try:
            with self.lock:
                if self.session is not None:
                    raise ValueError("close the active motion session before changing tools")
                if not isinstance(owner, str) or not owner:
                    raise ValueError("client identity is required")
                if tool_name not in allowed_tool_names:
                    raise ValueError("tool is not available in this arm's coordinate profile")
                if self.review is not None and self.review[0] != owner:
                    raise ValueError("pose review belongs to a different client")
                self.review = None
                self.reason = f"selecting controller tool {tool_name}"
            result = self.port.select_tool(tool_name)
            with self.lock:
                self.reason = f"controller tool {tool_name} verified; capture a new frame"
            return {"type": "pose6d_tool_selected", "tool_name": tool_name, **result,
                    "message": self.reason}
        finally:
            self.inflight.release()

    def readiness(self):
        try:
            validate_config(self.config, self.suction)
            if self.review is None:
                raise ValueError("a current pose result is required")
            _, obs, _, _, frozen_context = self.review
            if obs.get("T_base_camera") is None:
                raise ValueError("capture-time hand-eye transform is unavailable")
            if self.port.context() != frozen_context:
                raise ValueError("robot coordinate/tool context changed; capture and infer again")
            return True, self.reason
        except Exception as exc:
            return False, str(exc)

    def _owned(self, owner, session_id):
        if (self.session is None or self.session["owner"] != owner
                or self.session["id"] != session_id):
            raise ValueError("motion session identity mismatch")
        return self.session

    def status(self):
        with self.lock:
            session = self.session
            jog = session["jog"] if session else None
            return {"type": "pose6d_motion", "session_id": session["id"] if session else "",
                    "state": jog.state if jog else "review", "busy": self.inflight.locked(),
                    "confirmed_mm": jog.confirmed_m * 1000 if jog and jog.confirmed_m is not None else None,
                    "pending_mm": jog.pending_m * 1000 if jog and jog.pending_m is not None else None,
                    "auto_suction_ready": bool(jog and jog.state == "ready" and math.isclose(
                        jog.confirmed_m, self.config.get("prepare_distance_m", math.nan),
                        rel_tol=0, abs_tol=1e-9)),
                    "lease_retained": bool(session and session.get("token")) or bool(
                        getattr(self.port, "lease_uncertain", lambda: False)()),
                    "tool_power": session.get("tool_power") if session else None,
                    "message": self.reason}

    def prepare(self, command, feedback):
        if not self.inflight.acquire(False):
            raise ValueError("motion busy; commands are not queued")
        created_id = None
        try:
            with self.lock:
                if self.session is not None:
                    raise ValueError("close the existing motion session first")
                ready, reason = self.readiness()
                if not ready:
                    raise ValueError(reason)
                owner, obs, grasps, result_id, frozen_context = self.review
                if (command["client_id"] != owner or command["observation_id"] != obs["observation_id"]
                        or command["result_id"] != result_id or command.get("review_confirmed") is not True):
                    raise ValueError("current observation, result and explicit scene/pose review are required")
                screening = obs["result"].get("screening", {})
                if screening.get("status") != "pass_screening" and command.get("warning_confirmed") is not True:
                    raise ValueError("pose screening warning requires explicit review confirmation")
                grasp = next((g for g in grasps if g["grasp_id"] == command["grasp_id"]), None)
                if grasp is None or grasp.get("status") == "deprecated":
                    raise ValueError("unknown suction grasp")
                contact, normal = suction_pose(obs["T_base_camera"] @ rigid(obs["result"]["T_camera_object"]),
                                               grasp["contact_region"])
                c = self.config
                jog = JogSession(contact, normal, configured_transform(c["T_contact_tcp"]), min_m=c["min_distance_m"],
                                 max_m=c["max_distance_m"], max_step_m=c["max_step_m"],
                                 position_tolerance_m=c["position_tolerance_m"], angular_tolerance_rad=c["angular_tolerance_rad"],
                                 prepare_distance_m=c["prepare_distance_m"],
                                 contact_offset_m=c.get("contact_offset_m", 0.0))
                self.cancel.clear()
                self.session = {"id": uuid.uuid4().hex, "owner": owner, "jog": jog, "token": "",
                                "observation": obs, "grasp_id": grasp["grasp_id"],
                                "context": frozen_context}
                created_id = self.session["id"]
                self.reason = "acquiring arm task lease"
            feedback(self.status())
            token = self.port.acquire(owner, self.config["lease_ttl_sec"])
            with self.lock:
                self.session["token"] = token
                self.renew_at = self.clock()
            actual = self.port.current()
            self._execute(actual, jog.prepare(), feedback, command="movej_p",
                          velocity_percent=self.config["prepare_velocity_percent"])
        except Exception:
            # A rejected/stale command must never invalidate another session.
            if created_id is not None:
                self._fail(created_id)
            raise
        finally:
            self.inflight.release()
            if created_id is not None and self._is_blocked(created_id):
                self._release_if_drained(created_id)
        return self.status()

    def jog(self, command, feedback):
        if not self.inflight.acquire(False):
            raise ValueError("motion busy; commands are not queued")
        session_id = None
        uncertain = False
        try:
            with self.lock:
                session = self._owned(command["client_id"], command["session_id"])
                session_id = session["id"]
                step_mm = command.get("step_mm")
                if type(step_mm) not in (int, float) or not math.isfinite(step_mm) or step_mm == 0:
                    raise ValueError("jog step must be a finite non-zero number")
                step_m = step_mm / 1000
                jog = session["jog"]
                if abs(step_m) > jog.max_step_m:
                    raise ValueError("jog step exceeds the configured single-step limit")
                target_m = jog.confirmed_m + step_m if jog.confirmed_m is not None else math.nan
                if jog.state != "ready" or not jog.min_m <= target_m <= jog.max_m:
                    raise ValueError("jog target outside the configured signed travel range")
            # From this point onward, failures may indicate stale state or an
            # uncertain physical outcome, so the session must be quarantined.
            uncertain = True
            self._guard()
            actual = self.port.current()
            target = session["jog"].jog(step_m, actual)
            self._execute(actual, target, feedback, velocity_percent=self.config["velocity_percent"])
        except Exception:
            if uncertain and session_id is not None:
                self._fail(session_id)
            raise
        finally:
            self.inflight.release()
            if session_id is not None and self._is_blocked(session_id):
                self._release_if_drained(session_id)
        return self.status()

    def auto_suction(self, command, feedback):
        if not self.inflight.acquire(False):
            raise ValueError("motion busy; commands are not queued")
        session_id = None
        uncertain = False
        try:
            with self.lock:
                session = self._owned(command["client_id"], command["session_id"])
                session_id = session["id"]
                jog = session["jog"]
                if jog.state != "ready" or not math.isclose(
                        jog.confirmed_m, self.config["prepare_distance_m"], rel_tol=0, abs_tol=1e-9):
                    raise ValueError("automatic suction can start only at the preparation position")

            self._guard()
            actual = self.port.current()
            target = jog.begin_auto_suction(actual, self.config["second_prepare_distance_m"])
            uncertain = True
            self.reason = "正在开启吸盘，确认工具端 24 V"
            feedback(self.status())
            self._auto_tool_power(session_id, True, feedback)
            self.reason = "MOVEL approaching second preparation position"
            feedback(self.status())
            actual = self._execute(actual, target, feedback, velocity_percent=self.config["approach_velocity_percent"],
                                   finish=False)
            jog.finish_auto_distance(actual)

            target = jog.begin_auto_distance(jog.contact_distance_m)
            self.reason = "MOVEL approaching suction position slowly"
            feedback(self.status())
            actual = self._execute(actual, target, feedback, velocity_percent=self.config["contact_velocity_percent"],
                                   finish=False)
            jog.finish_auto_distance(actual)

            target = jog.begin_lift(actual, self.config["lift_height_m"])
            self.reason = "MOVEL lifting vertically in BASE +Z"
            feedback(self.status())
            actual = self._execute(actual, target, feedback, velocity_percent=self.config["lift_velocity_percent"],
                                   finish=False)
            jog.finish_lift(actual)
            self.reason = "提升已确认，等待真空开关新采样确认吸附"
            feedback(self.status())
            self._guard()
            holding = self.port.wait_vacuum_holding(self.cancel)
            self._guard()
            if holding is None:
                raise ValueError("提升完成，但真空开关状态不可用或已超时；保持吸盘开启")
            if holding:
                configured_home = self.config.get("home_joint_degrees")
                home_joints = (self.port.initial_joints() if configured_home is None else
                               np.asarray(configured_home, dtype=float).copy())
                home_pose = self.port.forward_kinematics(home_joints)
                jog.begin_return_home(home_pose)
                self.reason = "真空开关确认已吸附，正在 MOVEJ 回到初始姿态"
                feedback(self.status())
                # Transit and placement are outside the grasp workspace limits.
                actual = self._execute(actual, home_joints, feedback, command="movej",
                                       velocity_percent=self.config["home_velocity_percent"],
                                       check_path=False, finish=False)
                jog.finish_return_home(actual)
                self.reason = "初始姿态已确认到位，正在校验固定放置关节目标"
                feedback(self.status())
                self._guard()
                place_joints = np.asarray(self.config["place_joint_degrees"], dtype=float)
                jog.begin_place(actual, self.port.forward_kinematics(place_joints))
                self.reason = "初始姿态已确认到位，正在 MOVEJ 前往放置位"
                feedback(self.status())
                actual = self._execute(actual, place_joints, feedback, command="movej",
                                       velocity_percent=self.config["place_velocity_percent"],
                                       check_path=False, finish=False)
                jog.finish_place(actual)
                self.reason = "已到达放置位，正在关闭吸盘，确认工具端 0 V"
            else:
                self.reason = "真空开关确认未吸附，正在关闭吸盘，确认工具端 0 V"
            feedback(self.status())
            self._auto_tool_power(session_id, False, feedback)
            if holding:
                actual = self.port.current()
                jog.begin_return_after_place(actual)
                self.reason = "吸盘已关闭（0 V），正在 MOVEJ 返回初始姿态"
                feedback(self.status())
                actual = self._execute(actual, home_joints, feedback, command="movej",
                                       velocity_percent=self.config["return_velocity_percent"],
                                       check_path=False, finish=False)
                jog.finish_return_after_place(actual)
            self.reason = ("自动运动完成；物体已放置、吸盘已关闭（0 V），已回到初始姿态" if holding else
                           "自动运动完成；未吸到物体，提升位吸盘已关闭（0 V）")
            feedback(self.status())
        except Exception:
            if uncertain and session_id is not None:
                self._fail(session_id)
            raise
        finally:
            self.inflight.release()
            if session_id is not None and self._is_blocked(session_id):
                self._release_if_drained(session_id)
        return self.status()

    def _auto_tool_power(self, session_id, enabled, feedback):
        self._guard()
        with self.lock:
            session = self.session
            if session is None or session["id"] != session_id:
                raise ValueError("motion session identity mismatch")
            token = session["token"]
            session["tool_power"] = {"available": False, "message": "工具电源切换中，等待回读"}
        try:
            power = self.port.set_tool_power(enabled, token)
        except Exception as exc:
            with self.lock:
                session["tool_power"] = {"available": False, "success": False,
                                         "message": f"工具电源切换未确认：{exc}"}
            feedback(self.status())
            raise
        with self.lock:
            session["tool_power"] = {**power, "success": True}
        feedback(self.status())

    def _guard(self):
        if self.cancel.is_set():
            raise ValueError("session stopped; infer again")
        session = self.session
        context = self.port.context()
        if session and context != session.get("context"):
            raise ValueError("robot coordinate/tool context changed during motion; session blocked")
        if session and session.get("token"):
            self.port.validate(session["token"])

    def _execute(self, actual, target, feedback, command="movel", velocity_percent=None,
                 finish=True, check_path=True):
        self._guard()
        session = self.session
        if check_path:
            self.reason = "checking workspace"
            feedback(self.status())
            self.port.check_path(actual, target, session, self._guard)
        self._guard()
        self.reason = f"{command.upper()} in progress"
        feedback(self.status())
        # Port must require ROS SUCCEEDED plus driver success and terminal_state.
        self.port.execute(target, session["token"], self.cancel, feedback, command=command,
                          velocity_percent=velocity_percent)
        self._guard()
        confirmed = self.port.current()
        if finish:
            session["jog"].finish(True, confirmed)
            self.reason = "TCP readback confirmed; + moves outward, − moves inward"
        return confirmed

    def _is_blocked(self, session_id):
        with self.lock:
            return bool(self.session and self.session["id"] == session_id
                        and self.session["jog"].state == "blocked")

    def _fail(self, session_id):
        with self.lock:
            if self.session is None or self.session["id"] != session_id:
                return
            self.session["jog"].invalidate()
            self.review = None
            self.reason = "motion session blocked; stop/close and infer again"
        self.cancel.set()

    def _release_if_drained(self, session_id):
        with self.lock:
            if self.session is None or self.session["id"] != session_id:
                return False
            # A pending client call means action completion is not yet known.
            if self.inflight.locked():
                return False
            token = self.session.get("token") or ""
        if not token and getattr(self.port, "lease_uncertain", lambda: False)():
            return False
        if token:
            try:
                self.port.release(token)
            except Exception:
                # Unknown driver state: retain the token and prohibit takeover.
                return False
            with self.lock:
                if self.session and self.session["id"] == session_id:
                    self.session["token"] = ""
        return True

    def detach(self, owner):
        """Let an in-flight command finish, then release its orphaned session."""
        with self.lock:
            if self.session is not None and self.session["owner"] == owner:
                self.session["detached"] = True
                self.reason = "browser disconnected; waiting for current motion to finish"
            if self.review is not None and self.review[0] == owner:
                self.review = None

    def _close_detached(self):
        with self.lock:
            session = self.session
            if session is None or not session.get("detached") or self.inflight.locked():
                return False
            session_id = session["id"]
        if self._release_if_drained(session_id):
            with self.lock:
                if self.session is not None and self.session["id"] == session_id:
                    self.session = None
                    self.interrupted_session = None
                    self.reason = "disconnected session closed; infer again"
            return True
        return False

    def stop(self, owner, session_id=None):
        with self.lock:
            if self.session is None:
                if self.review and self.review[0] == owner:
                    self.review = None
                return self.status()
            if self.session["owner"] != owner or (session_id and session_id != self.session["id"]):
                raise ValueError("motion session belongs to another client")
            self.cancel.set()
            self.session["jog"].invalidate()
            self.review = None
            self.reason = "stopped; waiting for driver to confirm release"
            session_key = self.session["id"]
            token = bool(self.session.get("token"))
            uncertain_action = bool(getattr(self.port, "action_uncertain", lambda: False)())
            should_interrupt = (token or uncertain_action) and self.interrupted_session != session_key
            if should_interrupt:
                self.interrupted_session = session_key
        if should_interrupt:
            try:
                self.port.interrupt()
            except Exception:
                with self.lock:
                    if self.session and self.session["id"] == session_key:
                        self.interrupted_session = None
                raise
        if not self.inflight.locked() and self._release_if_drained(session_key):
            with self.lock:
                if self.session and self.session["id"] == session_key:
                    self.session = None
                    self.interrupted_session = None
                    self.reason = "session closed; infer again before preparing"
        return self.status()

    def tick(self):
        """Run on a worker thread, independently of the ROS executor."""
        if self._close_detached():
            return
        with self.lock:
            session = self.session
            if session is None:
                return
        try:
            self._guard()
            if session["token"] and self.clock() - self.renew_at >= self.config["lease_ttl_sec"] / 3:
                self.port.renew(session["token"], self.config["lease_ttl_sec"])
                self.renew_at = self.clock()
        except Exception as exc:
            self.reason = str(exc)
            self.stop(session["owner"], session["id"])

