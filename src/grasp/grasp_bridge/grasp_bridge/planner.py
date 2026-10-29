"""Candidate selection with seeded IK on a preverified restricted corridor."""

import math

import numpy as np

from .geometry import from_pose, restricted_waypoints, to_pose


class NoReachableCandidate(ValueError):
    def __init__(self, rejections):
        self.rejections = rejections
        super().__init__("no reachable candidate: " + str(rejections))


def _candidate_geometry(candidate, profile, T_base_camera, collision_config=None):
    if collision_config is not None:
        collision = candidate.get("collision") or {}
        status = collision.get("status", "unknown")
        if status in collision_config.get("blocking_statuses", ("collision", "warning", "unknown")):
            reason = collision.get("reason")
            detail = f": {reason}" if isinstance(reason, str) and reason else ""
            raise ValueError(f"collision status is {status}{detail}")
    g = profile["gripper_geometry"]
    if not (g["min_opening_m"] <= candidate["width_m"]
            and candidate["width_m"] + 2 * g["clearance_m"] <= g["max_opening_m"]):
        raise ValueError("candidate exceeds measured gripper opening")
    return restricted_waypoints(
        T_base_camera, candidate["T_camera_grasp"], profile["T_grasp_tcp"],
        approach_m=profile["approach_m"], lift_m=profile["lift_m"], workspace=profile["workspace_m"],
    )


def _plan_from_waypoints(candidate_id, waypoints, joints=None, rejections=None):
    joints = joints or {}
    return {"candidate_id": candidate_id, "waypoints": {
        name: {
            "position_m": to_pose(T)[0],
            "quaternion_wxyz": to_pose(T)[1],
            "joint_degrees": joints.get(name),
        }
        for name, T in waypoints.items()
    }, "rejections": rejections or []}


def calculate_plan(candidates, profile, T_base_camera, *, canceled=lambda: False, collision_config=None):
    """Choose a geometrically valid grasp without contacting an IK service.

    This is deliberately separate from IK validation so callers that already
    delegate Cartesian feasibility to the robot controller can reuse the grasp
    geometry and waypoint output.
    """
    reasons = []
    for candidate in candidates:
        if canceled():
            raise RuntimeError("planning canceled")
        try:
            waypoints = _candidate_geometry(candidate, profile, T_base_camera, collision_config)
            return _plan_from_waypoints(candidate["candidate_id"], waypoints, rejections=reasons)
        except ValueError as exc:
            reasons.append({"candidate_id": candidate["candidate_id"], "reason": str(exc)})
    raise NoReachableCandidate(reasons)


def attach_seed_joints(plan, seed_degrees):
    """Attach a six-value placeholder for consumers of the pose-only plan.

    The values are deliberately the observation seed; callers must treat them
    as unvalidated when IK checking is disabled. Motion execution uses poses.
    """
    seed = np.asarray(seed_degrees, dtype=float)
    if seed.shape != (6,) or not np.isfinite(seed).all():
        raise ValueError("invalid joint seed")
    return {
        **plan,
        "waypoints": {
            name: {**waypoint, "joint_degrees": seed.tolist()}
            for name, waypoint in plan["waypoints"].items()
        },
    }


def validate_plan_ik(plan, seed_degrees, solve_ik, *, sample_step_m, max_joint_step,
                     canceled=lambda: False):
    """Validate one already-selected Cartesian plan with seeded IK samples."""
    seed = np.asarray(seed_degrees, dtype=float)
    if seed.shape != (6,) or not np.isfinite(seed).all():
        raise ValueError("invalid joint seed")
    joints = {}
    last = None
    for name, waypoint in plan["waypoints"].items():
        # Keep the orientation from the returned pose while avoiding a second
        # public transform format in the planner contract.
        end = from_pose(waypoint["position_m"], waypoint["quaternion_wxyz"])
        if last is None:
            samples = [end]
        else:
            distance = np.linalg.norm(end[:3, 3] - last[:3, 3])
            samples = []
            for ratio in np.linspace(0, 1, max(2, math.ceil(distance / sample_step_m) + 1))[1:]:
                point = end.copy()
                point[:3, 3] = last[:3, 3] + ratio * (end[:3, 3] - last[:3, 3])
                samples.append(point)
        for point in samples:
            if canceled():
                raise RuntimeError("planning canceled")
            solution = np.asarray(solve_ik(point, seed.tolist()), dtype=float)
            if solution.shape != (6,) or not np.isfinite(solution).all():
                raise ValueError(f"{name}: invalid IK solution")
            if np.max(np.abs(solution - seed)) > max_joint_step:
                raise ValueError(f"{name}: IK branch jump exceeds limit")
            seed = solution
        joints[name] = seed.tolist()
        last = end
    return _plan_from_waypoints(plan["candidate_id"],
                                {name: from_pose(item["position_m"], item["quaternion_wxyz"])
                                 for name, item in plan["waypoints"].items()},
                                joints=joints, rejections=plan.get("rejections"))


def choose_plan(candidates, profile, T_base_camera, seed_degrees, solve_ik, *, sample_step_m, max_joint_step,
                canceled=lambda: False, collision_config=None):
    if not 0 < sample_step_m <= 0.05 or not 0 < max_joint_step <= 45:
        raise ValueError("invalid corridor sampling limits")
    reasons = []
    for candidate in candidates:
        if canceled():
            raise RuntimeError("planning canceled")
        try:
            geometric = _plan_from_waypoints(candidate["candidate_id"],
                                              _candidate_geometry(candidate, profile, T_base_camera, collision_config),
                                              rejections=reasons)
            return validate_plan_ik(geometric, seed_degrees, solve_ik,
                                    sample_step_m=sample_step_m,
                                    max_joint_step=max_joint_step, canceled=canceled)
        except ValueError as exc:
            reasons.append({"candidate_id": candidate["candidate_id"], "reason": str(exc)})
    raise NoReachableCandidate(reasons)
