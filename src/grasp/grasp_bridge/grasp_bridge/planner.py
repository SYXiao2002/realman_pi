"""Candidate selection with seeded IK on a preverified restricted corridor."""

import math

import numpy as np

from .geometry import restricted_waypoints, to_pose


def choose_plan(candidates, profile, T_base_camera, seed_degrees, solve_ik, *, sample_step_m, max_joint_step,
                canceled=lambda: False):
    if not 0 < sample_step_m <= 0.05 or not 0 < max_joint_step <= 45:
        raise ValueError("invalid corridor sampling limits")
    reasons = []
    for candidate in candidates:
        if canceled():
            raise RuntimeError("planning canceled")
        try:
            waypoints = restricted_waypoints(
                T_base_camera, candidate["T_camera_grasp"], profile["T_grasp_tcp"],
                approach_m=profile["approach_m"], lift_m=profile["lift_m"], workspace=profile["workspace_m"],
            )
            seed = np.asarray(seed_degrees, dtype=float)
            if seed.shape != (6,) or not np.isfinite(seed).all():
                raise ValueError("invalid joint seed")
            joints = {}
            last = None
            for name, end in waypoints.items():
                if last is None:
                    samples = [end]
                else:
                    distance = np.linalg.norm(end[:3, 3]-last[:3, 3])
                    samples = []
                    for ratio in np.linspace(0, 1, max(2, math.ceil(distance/sample_step_m)+1))[1:]:
                        point = end.copy()
                        point[:3, 3] = last[:3, 3] + ratio*(end[:3, 3]-last[:3, 3])
                        samples.append(point)
                for point in samples:
                    if canceled():
                        raise RuntimeError("planning canceled")
                    solution = np.asarray(solve_ik(point, seed.tolist()), dtype=float)
                    if solution.shape != (6,) or not np.isfinite(solution).all():
                        raise ValueError(f"{name}: invalid IK solution")
                    if np.max(np.abs(solution-seed)) > max_joint_step:
                        raise ValueError(f"{name}: IK branch jump exceeds limit")
                    seed = solution
                joints[name] = seed.tolist()
                last = end
            return {"candidate_id": candidate["candidate_id"], "waypoints": {
                name: {"position_m": to_pose(T)[0], "quaternion_wxyz": to_pose(T)[1], "joint_degrees": joints[name]}
                for name, T in waypoints.items()
            }, "rejections": reasons}
        except ValueError as exc:
            reasons.append({"candidate_id": candidate["candidate_id"], "reason": str(exc)})
    raise ValueError("no reachable candidate: " + str(reasons))
