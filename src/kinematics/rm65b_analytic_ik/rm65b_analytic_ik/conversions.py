"""Pose conversions used by the public xyzrpy API."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def _pose_vector(values: Sequence[float], label: str) -> np.ndarray:
    try:
        pose = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain six finite numbers") from exc
    if pose.shape != (6,) or not np.all(np.isfinite(pose)):
        raise ValueError(f"{label} must contain six finite numbers [x, y, z, roll, pitch, yaw]")
    return pose


def xyzrpy_to_matrix(xyzrpy: Sequence[float]) -> np.ndarray:
    """Convert xyz plus fixed-axis roll/pitch/yaw to a homogeneous matrix.

    The rotation convention is ``Rz(yaw) @ Ry(pitch) @ Rx(roll)``.
    """
    x, y, z, roll, pitch, yaw = _pose_vector(xyzrpy, "xyzrpy")
    cr, sr = np.cos(roll), np.sin(roll)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cy, sy = np.cos(yaw), np.sin(yaw)
    rotation = np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ],
        dtype=float,
    )
    transform = np.eye(4)
    transform[:3, :3] = rotation
    transform[:3, 3] = (x, y, z)
    return transform


def matrix_to_xyzrpy(transform: np.ndarray, tolerance: float = 1.0e-6) -> tuple[float, ...]:
    """Convert a valid homogeneous transform to xyz and fixed-axis RPY."""
    matrix = np.asarray(transform, dtype=float)
    if matrix.shape != (4, 4) or not np.all(np.isfinite(matrix)):
        raise ValueError("transform must be a finite 4x4 matrix")
    rotation = matrix[:3, :3]
    if (
        np.linalg.norm(matrix[3] - (0.0, 0.0, 0.0, 1.0), ord=np.inf) > tolerance
        or np.linalg.norm(rotation.T @ rotation - np.eye(3), ord=np.inf) > tolerance * 10.0
        or abs(np.linalg.det(rotation) - 1.0) > tolerance * 10.0
    ):
        raise ValueError("transform rotation must be a proper orthonormal rotation")
    pitch = float(np.arcsin(np.clip(-rotation[2, 0], -1.0, 1.0)))
    if abs(np.cos(pitch)) > tolerance:
        roll = float(np.arctan2(rotation[2, 1], rotation[2, 2]))
        yaw = float(np.arctan2(rotation[1, 0], rotation[0, 0]))
    else:
        roll = 0.0
        yaw = float(np.arctan2(-rotation[0, 1], rotation[1, 1]))
    return (
        float(matrix[0, 3]), float(matrix[1, 3]), float(matrix[2, 3]),
        roll, pitch, yaw,
    )


def inverse_transform(transform: np.ndarray) -> np.ndarray:
    """Invert a homogeneous rigid transform."""
    rotation = transform[:3, :3]
    inverse = np.eye(4)
    inverse[:3, :3] = rotation.T
    inverse[:3, 3] = -rotation.T @ transform[:3, 3]
    return inverse
