"""Geometry and state for a single externally executed suction jog session.

The caller owns robot action execution, state freshness, control ownership,
collision checks and synchronization. This module never sends motion.
"""

import math

import numpy as np

from .geometry import rigid, tcp_target


class JogSession:
    def __init__(self, contact, outward_normal, T_contact_tcp, *, min_m, max_m,
                 max_step_m, position_tolerance_m, angular_tolerance_rad,
                 prepare_distance_m=.04, contact_offset_m=0.0):
        self.contact = rigid(contact).copy()
        self.normal = np.asarray(outward_normal, dtype=float).copy()
        self.tool = rigid(T_contact_tcp).copy()
        values = (min_m, max_m, max_step_m, position_tolerance_m, angular_tolerance_rad,
                  prepare_distance_m, contact_offset_m)
        if any(not math.isfinite(v) for v in values):
            raise ValueError("jog limits must be finite")
        if (not min_m < 0 or contact_offset_m < 0
                or not min_m <= -contact_offset_m <= max_m
                or not min_m <= 0 < prepare_distance_m <= max_m
                or min(max_step_m, position_tolerance_m, angular_tolerance_rad) <= 0):
            raise ValueError("invalid jog limits or tolerances")
        tcp_target(self.contact, self.normal, 0, self.tool)
        self.min_m, self.max_m = min_m, max_m
        self.max_step_m = max_step_m
        self.prepare_distance_m = prepare_distance_m
        self.contact_distance_m = -contact_offset_m
        self.position_tolerance_m = position_tolerance_m
        self.angular_tolerance_rad = angular_tolerance_rad
        self.state = "review"
        self.confirmed_m = None
        self.pending_m = None

    def _check_pose(self, actual_tcp, expected_m):
        expected = tcp_target(self.contact, self.normal, expected_m, self.tool)
        self._check_target_pose(actual_tcp, expected, "position and orientation")

    def _check_target_pose(self, actual_tcp, expected, description):
        actual = rigid(actual_tcp)
        position_error = np.linalg.norm(actual[:3, 3] - expected[:3, 3])
        relative = expected[:3, :3].T @ actual[:3, :3]
        angle_error = math.acos(float(np.clip((np.trace(relative) - 1) / 2, -1, 1)))
        if position_error > self.position_tolerance_m or angle_error > self.angular_tolerance_rad:
            raise ValueError(f"actual TCP does not match the expected {description}")

    def prepare(self):
        if self.state != "review":
            raise ValueError("session cannot prepare in its current state")
        self.pending_m = self.prepare_distance_m
        self.state = "preparing"
        return tcp_target(self.contact, self.normal, self.pending_m, self.tool)

    def begin_auto_suction(self, actual_tcp, second_prepare_m):
        if self.state != "ready" or not math.isclose(
                self.confirmed_m, self.prepare_distance_m, rel_tol=0, abs_tol=1e-9):
            raise ValueError("automatic suction requires the confirmed preparation position")
        if not 0 <= second_prepare_m < self.prepare_distance_m:
            raise ValueError("second preparation distance must be between contact and preparation")
        self._check_pose(actual_tcp, self.prepare_distance_m)
        self.pending_m = second_prepare_m
        self.state = "auto_suction"
        return tcp_target(self.contact, self.normal, second_prepare_m, self.tool)

    def finish_auto_distance(self, actual_tcp):
        if self.state != "auto_suction" or self.pending_m is None:
            raise ValueError("automatic suction has no pending distance target")
        self._check_pose(actual_tcp, self.pending_m)
        self.confirmed_m, self.pending_m = self.pending_m, None

    def begin_auto_distance(self, distance_m):
        if self.state != "auto_suction" or self.pending_m is not None:
            raise ValueError("automatic suction is not ready for its next distance target")
        if not self.min_m <= distance_m <= self.max_m:
            raise ValueError("automatic suction target is outside configured travel")
        self.pending_m = distance_m
        return tcp_target(self.contact, self.normal, distance_m, self.tool)

    def begin_lift(self, actual_tcp, lift_height_m):
        if self.state != "auto_suction" or self.pending_m is not None or not math.isclose(
                self.confirmed_m, self.contact_distance_m, rel_tol=0, abs_tol=1e-9):
            raise ValueError("vertical lift requires confirmed arrival at the suction position")
        self._check_pose(actual_tcp, self.contact_distance_m)
        if not math.isfinite(lift_height_m) or lift_height_m <= 0:
            raise ValueError("lift height must be positive")
        self.lift_target = rigid(actual_tcp).copy()
        self.lift_target[2, 3] += lift_height_m
        self.state = "lifting"
        return self.lift_target.copy()

    def finish_lift(self, actual_tcp):
        if self.state != "lifting" or not hasattr(self, "lift_target"):
            raise ValueError("vertical lift has no pending target")
        self._check_target_pose(actual_tcp, self.lift_target, "vertical lift pose")
        self.confirmed_m, self.pending_m, self.state = None, None, "lifted"
        del self.lift_target

    def begin_return_home(self, home_pose):
        if self.state != "lifted":
            raise ValueError("return to initial posture requires a confirmed vertical lift")
        self.home_target = rigid(home_pose).copy()
        self.state = "returning_home"

    def finish_return_home(self, actual_tcp):
        if self.state != "returning_home":
            raise ValueError("initial posture has no pending target")
        self._check_target_pose(actual_tcp, self.home_target, "initial posture")
        self.state = "at_home"

    def begin_place(self, actual_tcp, target_pose):
        if self.state != "at_home":
            raise ValueError("placement requires confirmed arrival at the initial posture")
        self._check_target_pose(actual_tcp, self.home_target, "initial posture")
        self.place_target = rigid(target_pose).copy()
        self.state = "placing"
        return self.place_target.copy()

    def finish_place(self, actual_tcp):
        if self.state != "placing" or not hasattr(self, "place_target"):
            raise ValueError("placement has no pending target")
        self._check_target_pose(actual_tcp, self.place_target, "place pose")
        self.state = "placed"

    def begin_return_after_place(self, actual_tcp):
        if self.state != "placed":
            raise ValueError("final return requires confirmed arrival at the place pose")
        self._check_target_pose(actual_tcp, self.place_target, "place pose")
        self.state = "returning_after_place"

    def finish_return_after_place(self, actual_tcp):
        if self.state != "returning_after_place":
            raise ValueError("final return has no pending target")
        self._check_target_pose(actual_tcp, self.home_target, "final initial posture")
        self.state = "completed"
        del self.place_target

    def jog(self, step_m, actual_tcp):
        if self.state != "ready":
            raise ValueError("session is not ready")
        if not math.isfinite(step_m) or step_m == 0 or abs(step_m) > self.max_step_m:
            raise ValueError("invalid jog step")
        try:
            self._check_pose(actual_tcp, self.confirmed_m)
        except ValueError:
            self.state = "blocked"
            raise
        target = self.confirmed_m + step_m
        if not self.min_m <= target <= self.max_m:
            raise ValueError("jog target outside configured limits")
        self.pending_m = target
        self.state = "jogging"
        return tcp_target(self.contact, self.normal, target, self.tool)

    def finish(self, success, actual_tcp=None):
        if self.state not in ("preparing", "jogging"):
            raise ValueError("no motion is pending")
        if not success or actual_tcp is None:
            self.state = "blocked"
            self.pending_m = None
            return
        try:
            self._check_pose(actual_tcp, self.pending_m)
        except ValueError:
            self.state = "blocked"
            self.pending_m = None
            raise
        self.confirmed_m, self.pending_m, self.state = self.pending_m, None, "ready"

    def invalidate(self):
        self.state = "blocked"
        self.pending_m = None
