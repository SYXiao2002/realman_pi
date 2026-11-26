"""Joint-limit and periodic-representative helpers."""

from __future__ import annotations

import math

from .config_loader import RobotConfig


def legal_representatives(angle: float, limits: tuple[float, float], tolerance: float) -> list[float]:
    """Return every angle ``angle + 2*pi*k`` inside a joint's actual limits."""
    low, high = limits
    turn = 2.0 * math.pi
    first = math.ceil((low - angle - tolerance) / turn)
    last = math.floor((high - angle + tolerance) / turn)
    return [
        min(high, max(low, angle + turn * k))
        for k in range(first, last + 1)
        if low - tolerance <= angle + turn * k <= high + tolerance
    ]


def within_joint_limits(joints: tuple[float, ...], config: RobotConfig) -> bool:
    return all(
        low - config.orientation_tolerance <= angle <= high + config.orientation_tolerance
        for angle, (low, high) in zip(joints, config.joint_limits)
    )
