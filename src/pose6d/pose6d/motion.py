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
    if (type(lo) not in (int, float) or type(hi) not in (int, float)
            or not math.isfinite(lo) or not math.isfinite(hi) or not lo < 0 or hi < .04):
        raise ValueError("configure signed min_distance_m < 0 and max_distance_m >= 0.04")
    for key in ("max_step_m", "timeout_sec", "position_tolerance_m", "angular_tolerance_rad",
                "cartesian_sample_step_m", "angular_sample_step_rad",
                "collision_margin_m",
                "lease_ttl_sec", "service_timeout_sec"):
        positive(config, key)
    if type(config.get("velocity_percent")) is not int or not 1 <= config["velocity_percent"] <= 100:
        raise ValueError("configure velocity_percent in 1..100")
    bounds = np.asarray(config.get("workspace_m"), dtype=float)
    if bounds.shape != (2, 3) or not np.isfinite(bounds).all() or not np.all(bounds[0] < bounds[1]):
        raise ValueError("configure measured workspace_m bounds")
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
                    "lease_retained": bool(session and session.get("token")) or bool(
                        getattr(self.port, "lease_uncertain", lambda: False)()), "message": self.reason}

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
                                 position_tolerance_m=c["position_tolerance_m"], angular_tolerance_rad=c["angular_tolerance_rad"])
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
            self._execute(actual, jog.prepare(), feedback)
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
            self._execute(actual, target, feedback)
        except Exception:
            if uncertain and session_id is not None:
                self._fail(session_id)
            raise
        finally:
            self.inflight.release()
            if session_id is not None and self._is_blocked(session_id):
                self._release_if_drained(session_id)
        return self.status()

    def _guard(self):
        if self.cancel.is_set():
            raise ValueError("session stopped; infer again")
        session = self.session
        context = self.port.context()
        if session and context != session.get("context"):
            raise ValueError("robot coordinate/tool context changed during motion; session blocked")
        if session and session.get("token"):
            self.port.validate(session["token"])

    def _execute(self, actual, target, feedback):
        self._guard()
        session = self.session
        self.reason = "checking workspace"
        feedback(self.status())
        self.port.check_path(actual, target, session, self._guard)
        self._guard()
        self.reason = "MOVEL in progress"
        feedback(self.status())
        # Port must require ROS SUCCEEDED plus driver success and terminal_state.
        self.port.execute(target, session["token"], self.cancel, feedback)
        self._guard()
        session["jog"].finish(True, self.port.current())
        self.reason = "TCP readback confirmed; + moves outward, − moves inward"

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

