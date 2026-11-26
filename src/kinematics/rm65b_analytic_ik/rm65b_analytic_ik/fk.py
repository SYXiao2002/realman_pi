"""Forward kinematics for the configured RM65-B modified-DH chain."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from .config_loader import RobotConfig, load_config
from .conversions import matrix_to_xyzrpy, xyzrpy_to_matrix


def _rx(angle: float) -> np.ndarray:
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


def _rz(angle: float) -> np.ndarray:
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _modified_dh(row: tuple[float, float, float, float], joint: float) -> np.ndarray:
    a, alpha, d, offset = row
    rotation_x = _rx(alpha)
    transform = np.eye(4)
    transform[:3, :3] = rotation_x @ _rz(joint + offset)
    transform[:3, 3] = rotation_x @ np.array((a, 0.0, d))
    return transform


def forward_kinematics(
    joint_angles_rad: Sequence[float],
    config: RobotConfig | None = None,
    tool_xyzrpy: Sequence[float] | None = None,
) -> np.ndarray:
    """Return the flange (or optional TCP) transform for six joint angles in rad."""
    model = config if config is not None else load_config()
    try:
        joints = np.asarray(joint_angles_rad, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("joint_angles_rad must contain six finite radians") from exc
    if joints.shape != (6,) or not np.all(np.isfinite(joints)):
        raise ValueError("joint_angles_rad must contain six finite radians")
    transform = np.eye(4)
    for row, angle in zip(model.mdh, joints):
        transform = transform @ _modified_dh(row, float(angle))
    if tool_xyzrpy is not None:
        transform = transform @ xyzrpy_to_matrix(tool_xyzrpy)
    return transform


def forward_kinematics_xyzrpy(
    joint_angles_rad: Sequence[float],
    config: RobotConfig | None = None,
    tool_xyzrpy: Sequence[float] | None = None,
) -> tuple[float, ...]:
    """Return the flange (or optional TCP) pose as xyzrpy in metres/radians."""
    model = config if config is not None else load_config()
    return matrix_to_xyzrpy(
        forward_kinematics(joint_angles_rad, model, tool_xyzrpy),
        tolerance=model.orientation_tolerance,
    )
