"""Load and validate the RM65-B kinematic parameters."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import site
import sys
from typing import Any

import numpy as np
import yaml


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[4] / "config" / "ros" / "rm65b_analytic_ik.yaml"
_JOINTS = tuple(f"J{i}" for i in range(1, 7))


@dataclass(frozen=True)
class RobotConfig:
    """Validated model values in SI units and radians."""

    name: str
    mdh: tuple[tuple[float, float, float, float], ...]
    joint_limits: tuple[tuple[float, float], ...]
    position_tolerance: float
    orientation_tolerance: float
    trigonometric_bound: float

    @property
    def d1(self) -> float:
        return self.mdh[0][2]

    @property
    def a3(self) -> float:
        return self.mdh[2][0]

    @property
    def d4(self) -> float:
        return self.mdh[3][2]

    @property
    def d6(self) -> float:
        return self.mdh[5][2]


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a YAML mapping")
    return value


def load_config(path: str | Path | None = None) -> RobotConfig:
    """Load a YAML model file, converting its published units to SI."""
    if path is not None:
        config_path = Path(path)
    elif os.environ.get("REALMAN_CONFIG_ROOT"):
        config_path = Path(os.environ["REALMAN_CONFIG_ROOT"]) / "ros" / "rm65b_analytic_ik.yaml"
    else:
        candidates = (
            Path(__file__).resolve().parent / "config" / "rm65b_params.yaml",
            DEFAULT_CONFIG_PATH,
            Path(sys.prefix) / "share" / "rm65b_analytic_ik" / "config" / "rm65b_analytic_ik.yaml",
            Path(site.getuserbase()) / "share" / "rm65b_analytic_ik" / "config" / "rm65b_analytic_ik.yaml",
        )
        config_path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
        if not config_path.is_file():
            from ament_index_python.packages import get_package_share_directory
            config_path = Path(get_package_share_directory("rm65b_analytic_ik")) / "config/rm65b_analytic_ik.yaml"
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read RM65-B config {config_path}: {exc}") from exc
    root = _require_mapping(raw, "config")
    robot = _require_mapping(root.get("robot"), "robot")
    mdh_raw = _require_mapping(root.get("mdh_parameters"), "mdh_parameters")
    limits_raw = _require_mapping(root.get("joint_limits"), "joint_limits")
    tolerances = _require_mapping(root.get("tolerances"), "tolerances")

    units = _require_mapping(root.get("units"), "units")
    if units.get("length") != "m" or units.get("angle") != "rad":
        raise ValueError("units must declare length: m and angle: rad")

    mdh: list[tuple[float, float, float, float]] = []
    limits: list[tuple[float, float]] = []
    try:
        for joint in _JOINTS:
            row = np.asarray(mdh_raw[joint], dtype=float)
            limit = np.asarray(limits_raw[joint], dtype=float)
            if row.shape != (4,) or limit.shape != (2,):
                raise ValueError(f"{joint} must have four MDH values and two limits")
            if not np.all(np.isfinite(row)) or not np.all(np.isfinite(limit)):
                raise ValueError(f"{joint} values must be finite")
            mdh.append((float(row[0]) / 1000.0, np.deg2rad(row[1]).item(),
                        float(row[2]) / 1000.0, np.deg2rad(row[3]).item()))
            low, high = np.deg2rad(limit)
            if low > high:
                raise ValueError(f"{joint} lower limit exceeds its upper limit")
            limits.append((float(low), float(high)))
        tol_pos = float(tolerances["position"])
        tol_rot = float(tolerances["orientation"])
        trig = float(tolerances["trigonometric_bound"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid RM65-B configuration: {exc}") from exc
    if not all(np.isfinite(v) and v > 0.0 for v in (tol_pos, tol_rot, trig)):
        raise ValueError("all tolerances must be finite and positive")

    result = RobotConfig(
        name=str(robot.get("name", "RealMan RM65-B")),
        mdh=tuple(mdh),
        joint_limits=tuple(limits),
        position_tolerance=tol_pos,
        orientation_tolerance=tol_rot,
        trigonometric_bound=trig,
    )
    _validate_analytic_topology(result)
    return result


def _validate_analytic_topology(config: RobotConfig) -> None:
    """Reject structural edits that invalidate this robot-specific derivation."""
    expected_alpha = (0.0, np.pi / 2, 0.0, np.pi / 2, -np.pi / 2, np.pi / 2)
    for index, (row, alpha) in enumerate(zip(config.mdh, expected_alpha)):
        a, actual_alpha, d, _ = row
        if abs(actual_alpha - alpha) > 1.0e-8:
            raise ValueError(f"J{index + 1} alpha changes the supported RM65-B wrist geometry")
        if index != 2 and abs(a) > 1.0e-10:
            raise ValueError(f"J{index + 1} a must be zero for the analytic RM65-B model")
        if index not in (0, 3, 5) and abs(d) > 1.0e-10:
            raise ValueError(f"J{index + 1} d must be zero for the analytic RM65-B model")
    if min(config.a3, config.d4, config.d6) <= 0.0:
        raise ValueError("RM65-B link lengths a3, d4, and d6 must be positive")
