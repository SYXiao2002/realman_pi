"""Closed-form shoulder/elbow solutions for the RM65-B wrist centre."""

from __future__ import annotations

import math

import numpy as np

from .config_loader import RobotConfig


def solve_position(
    wrist_center: np.ndarray,
    config: RobotConfig,
    *,
    enumerate_shoulder_singularity: bool = False,
) -> tuple[list[tuple[float, float, float]], bool]:
    """Return geometric (J1, J2, J3) branches and whether position is reachable.

    The RM65-B arm geometry reduces to
    ``r^2 = a3^2 + d4^2 + 2*a3*d4*sin(theta3)`` with the configured MDH offsets.
    """
    xw, yw, zw = (float(value) for value in wrist_center)
    rho = math.hypot(xw, yw)
    shoulder_singular = rho <= config.position_tolerance
    if shoulder_singular and enumerate_shoulder_singularity:
        low, high = config.joint_limits[0]
        # At the shoulder axis J1 is free. Each sample still gets an exact wrist solve.
        count = max(1, int(math.ceil((high - low) / math.radians(1.0))))
        q1_values = np.linspace(low, high, count + 1).tolist()
        if low <= 0.0 <= high:
            q1_values.insert(0, 0.0)
        effective_theta1_values = [q1 + config.mdh[0][3] for q1 in q1_values]
    elif shoulder_singular:
        q1_values = [-config.mdh[0][3]]
        effective_theta1_values = [0.0]
    else:
        heading = math.atan2(yw, xw)
        effective_theta1_values = [heading, heading + math.pi]
        q1_values = [theta1 - config.mdh[0][3] for theta1 in effective_theta1_values]

    z = zw - config.d1
    branches: list[tuple[float, float, float]] = []
    reachable = False
    a3, d4 = config.a3, config.d4
    for q1, theta1 in zip(q1_values, effective_theta1_values):
        radial_x = math.cos(theta1) * xw + math.sin(theta1) * yw
        radius_squared = radial_x * radial_x + z * z
        sin_theta3 = (radius_squared - a3 * a3 - d4 * d4) / (2.0 * a3 * d4)
        bound = config.trigonometric_bound
        if sin_theta3 < -1.0 - bound or sin_theta3 > 1.0 + bound:
            continue
        reachable = True
        sin_theta3 = min(1.0, max(-1.0, sin_theta3))
        principal = math.asin(sin_theta3)
        theta3_values = (principal, math.pi - principal)
        phi = math.atan2(z, radial_x)
        for theta3 in theta3_values:
            ux = a3 + d4 * math.sin(theta3)
            uy = -d4 * math.cos(theta3)
            theta2 = phi - math.atan2(uy, ux)
            q2 = theta2 - config.mdh[1][3]
            q3 = theta3 - config.mdh[2][3]
            candidate = (q1, q2, q3)
            if not any(_same_angles(candidate, existing) for existing in branches):
                branches.append(candidate)
    return branches, reachable


def _same_angles(first: tuple[float, ...], second: tuple[float, ...], tolerance: float = 1.0e-9) -> bool:
    return all(abs(math.remainder(a - b, 2.0 * math.pi)) <= tolerance for a, b in zip(first, second))
