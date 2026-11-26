"""Analytic inverse kinematics and branch enumeration."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from .config_loader import RobotConfig
from .constraints import legal_representatives
from .conversions import inverse_transform, xyzrpy_to_matrix
from .fk import forward_kinematics
from .orientation_solver import rotation_03, solve_orientation
from .position_solver import solve_position
from .singularities import rotation_error
from .types import FailureReason


def solve_analytic(
    target_xyzrpy: Sequence[float],
    tool_xyzrpy: Sequence[float],
    config: RobotConfig,
    *,
    first_solution_only: bool = False,
    enumerate_shoulder_singularity: bool = False,
) -> tuple[list[list[float]], str | None]:
    """Compute legal joint vectors in radians and return an optional reason code."""
    try:
        target_tcp = xyzrpy_to_matrix(target_xyzrpy)
        flange_tcp = xyzrpy_to_matrix(tool_xyzrpy)
    except (TypeError, ValueError):
        return [], FailureReason.INVALID_POSE.value

    target_flange = target_tcp @ inverse_transform(flange_tcp)
    # In Craig MDH, the final d6 is along the flange z axis. Removing it gives
    # the centre shared by joints 4, 5, and 6.
    wrist_center = target_flange[:3, 3] - config.d6 * target_flange[:3, 2]
    shoulder_singular = float(np.hypot(wrist_center[0], wrist_center[1])) <= config.position_tolerance
    p123, geometrically_reachable = solve_position(
        wrist_center,
        config,
        enumerate_shoulder_singularity=enumerate_shoulder_singularity,
    )
    if not geometrically_reachable:
        return [], FailureReason.OUT_OF_REACH.value

    valid: list[list[float]] = []
    saw_orientation_branch = False
    saw_limit_compliant_candidate = False
    for first_three in p123:
        raw_wrist = solve_orientation(rotation_03(first_three, config), target_flange[:3, :3], config)
        for last_three in raw_wrist:
            saw_orientation_branch = True
            raw_joints = first_three + last_three
            representatives = [
                legal_representatives(angle, limits, config.orientation_tolerance)
                for angle, limits in zip(raw_joints, config.joint_limits)
            ]
            if any(not values for values in representatives):
                continue
            saw_limit_compliant_candidate = True
            for j1 in representatives[0]:
                for j2 in representatives[1]:
                    for j3 in representatives[2]:
                        for j4 in representatives[3]:
                            for j5 in representatives[4]:
                                for j6 in representatives[5]:
                                    candidate = [j1, j2, j3, j4, j5, j6]
                                    actual = forward_kinematics(candidate, config)
                                    position_error = float(np.linalg.norm(actual[:3, 3] - target_flange[:3, 3]))
                                    orientation_err = rotation_error(target_flange[:3, :3], actual[:3, :3])
                                    if (
                                        position_error > config.position_tolerance * 10.0
                                        or orientation_err > config.orientation_tolerance * 10.0
                                    ):
                                        continue
                                    if not any(_same_solution(candidate, prior, config) for prior in valid):
                                        valid.append(candidate)
                                        if first_solution_only:
                                            return valid, None
    if valid:
        if shoulder_singular and not first_solution_only:
            valid = _canonicalize_shoulder_family(valid, config)
        return valid, None
    if saw_limit_compliant_candidate:
        return [], FailureReason.NUMERICAL_FAILURE.value
    if saw_orientation_branch:
        return [], FailureReason.JOINT_LIMIT.value
    if p123:
        # The orientation solver can return no branch at a singularity when the
        # remaining coupled wrist angles cannot fit their limits.
        return [], FailureReason.JOINT_LIMIT.value
    return [], FailureReason.NUMERICAL_FAILURE.value


def _same_solution(first: list[float], second: list[float], config: RobotConfig) -> bool:
    # J6's multi-turn values are distinct legal actuator configurations and are
    # retained; all other branches compare modulo one revolution.
    for index, (a, b) in enumerate(zip(first, second)):
        difference = a - b if index == 5 else float(np.remainder(a - b + np.pi, 2.0 * np.pi) - np.pi)
        if abs(difference) > config.orientation_tolerance:
            return False
    return True


def _canonicalize_shoulder_family(
    solutions: list[list[float]], config: RobotConfig
) -> list[list[float]]:
    """Keep one representative per elbow/wrist branch at the continuous shoulder singularity."""
    representatives: list[list[float]] = []
    for candidate in solutions:
        same_family = next(
            (
                prior
                for prior in representatives
                if all(
                    abs(float(np.remainder(candidate[index] - prior[index] + np.pi, 2.0 * np.pi) - np.pi))
                    <= config.orientation_tolerance
                    for index in (1, 2, 4)
                )
            ),
            None,
        )
        if same_family is None:
            representatives.append(candidate)
        elif _singular_representative_score(candidate) < _singular_representative_score(same_family):
            representatives[representatives.index(same_family)] = candidate
    return representatives


def _singular_representative_score(joints: list[float]) -> tuple[float, tuple[float, ...]]:
    # Choose the member closest to zero without changing its exact end pose.
    return (abs(joints[0]) + abs(joints[3]) + abs(joints[5]), tuple(joints))
