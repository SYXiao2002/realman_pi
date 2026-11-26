"""Closed-form spherical-wrist orientation branches."""

from __future__ import annotations

import math

import numpy as np

from .config_loader import RobotConfig
from .fk import _rx, _rz


def rotation_03(joints_123: tuple[float, float, float], config: RobotConfig) -> np.ndarray:
    """Rotation from base to modified-DH frame 3."""
    rotation = np.eye(3)
    for index, angle in enumerate(joints_123):
        _, alpha, _, offset = config.mdh[index]
        rotation = rotation @ _rx(alpha) @ _rz(angle + offset)
    return rotation


def solve_orientation(
    rotation_03_matrix: np.ndarray, rotation_06: np.ndarray, config: RobotConfig
) -> list[tuple[float, float, float]]:
    """Return J4/J5/J6 branches, including the J5=0 wrist singularity.

    Removing fixed MDH twists turns the wrist into a Y-Z-Y Euler decomposition:
    ``R36 * Rx(-pi/2) = Ry(-q4) Rz(q5) Ry(-q6)``.
    """
    relative = rotation_03_matrix.T @ rotation_06
    matrix = relative @ _rx(-math.pi / 2)
    cos_q5 = float(np.clip(matrix[1, 1], -1.0, 1.0))
    q5 = math.acos(cos_q5)
    if abs(math.sin(q5)) <= config.orientation_tolerance:
        # At either wrist singularity only one combination of q4 and q6 is
        # observable. Pick the legal family member with q4 closest to zero.
        offset4, offset5, offset6 = (config.mdh[index][3] for index in (3, 4, 5))
        low4, high4 = config.joint_limits[3]
        low6, high6 = config.joint_limits[5]
        if cos_q5 >= 0.0:
            coupled = math.atan2(matrix[0, 2], matrix[0, 0])
            feasible_low = max(low4, -coupled - offset4 - offset6 - high6)
            feasible_high = min(high4, -coupled - offset4 - offset6 - low6)
            q5_effective = 0.0
            q6_from_q4 = lambda q4: -coupled - q4 - offset4 - offset6
        else:
            coupled = math.atan2(matrix[0, 2], -matrix[0, 0])
            feasible_low = max(low4, low6 - coupled - offset4 + offset6)
            feasible_high = min(high4, high6 - coupled - offset4 + offset6)
            q5_effective = math.pi
            q6_from_q4 = lambda q4: coupled + q4 + offset4 - offset6
        if feasible_low > feasible_high + config.orientation_tolerance:
            return []
        q4 = min(feasible_high, max(feasible_low, 0.0))
        return [(q4, q5_effective - offset5, q6_from_q4(q4))]

    first_yaw = math.atan2(matrix[2, 1], -matrix[0, 1])
    last_yaw = math.atan2(matrix[1, 2], matrix[1, 0])
    candidates = [
        (-first_yaw, q5, -last_yaw),
        (-(first_yaw + math.pi), -q5, -(last_yaw + math.pi)),
    ]
    return _deduplicate([_remove_offsets(candidate, config) for candidate in candidates])


def _remove_offsets(
    effective_angles: tuple[float, float, float], config: RobotConfig
) -> tuple[float, float, float]:
    return tuple(
        angle - config.mdh[index][3]
        for index, angle in zip((3, 4, 5), effective_angles)
    )  # type: ignore[return-value]


def _deduplicate(candidates: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    unique: list[tuple[float, float, float]] = []
    for candidate in candidates:
        if not any(
            all(abs(math.remainder(a - b, 2.0 * math.pi)) <= 1.0e-9 for a, b in zip(candidate, prior))
            for prior in unique
        ):
            unique.append(candidate)
    return unique
