"""Freeze robot, calibration, tool and gripper identities for one task."""

import hashlib
import json
from pathlib import Path

import numpy as np
import yaml

from .geometry import transform


def default_config_root():
    import os
    source = Path(__file__).resolve().parents[4] / "config"
    if os.environ.get("REALMAN_CONFIG_ROOT"):
        return Path(os.environ["REALMAN_CONFIG_ROOT"])
    if source.is_dir():
        return source
    from ament_index_python.packages import get_package_share_directory
    return Path(get_package_share_directory("grasp_bridge")) / "config"


def read_yaml(path):
    path = Path(path)
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected mapping: {path}")
    local = path.with_suffix(".local.yaml")
    if local.is_file():
        override = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
        if not isinstance(override, dict):
            raise ValueError(f"expected mapping: {local}")
        value = merge(value, override)
    return value


def merge(base, override):
    result = dict(base)
    for key, value in override.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def camera_identity(model):
    # Matches calibration session.py's raw CameraInfo fingerprint. Frame and
    # installation identity are checked separately against the arm profile.
    return digest({"width": model.width, "height": model.height,
                   "k": np.asarray(model.K).reshape(-1).tolist(), "d": np.asarray(model.D).tolist(),
                   "distortion_model": model.distortion_model})


def load_profile(config_root, name=None, *, require_ready=False):
    root = Path(config_root)
    table = read_yaml(root / "ros/grasp_profiles.yaml")
    name = name or table["default_profile"]
    profile = dict(table["profiles"][name])
    if profile.get("arm") not in ("r", "l"):
        raise ValueError("grasp profiles currently support left/right wrist arms")
    for key in ("base_frame", "flange_frame", "tool_name", "color_frame", "depth_frame", "camera_serial",
                "mount_id", "gripper", "color_topic", "depth_topic", "color_info_topic", "depth_info_topic", "joint_topic"):
        if not isinstance(profile.get(key), str) or not profile[key]:
            raise ValueError(f"profile {name} missing {key}")
    calibration = read_yaml(root / "ros/camera_extrinsics.yaml")["calibrations"][profile["calibration"]]
    camera = read_yaml(root / "ros/cameras_ros2.yaml")["cameras"][profile["camera"]]
    if camera["serial"] != profile["camera_serial"] or not camera.get("enabled", True):
        raise ValueError("camera profile serial/enabled state disagrees with camera bringup")
    coordinates = read_yaml(root / "ros/realman_coordinates.yaml")["robots"][profile["arm"]]
    if profile["tool_name"] != coordinates["default_tool"] or profile["tool_name"] not in coordinates["tools"]:
        raise ValueError("grasp tool must match the driver's active configured default tool")
    gripper_config = read_yaml(root / "ros/gripper.yaml")
    if gripper_config.get("task_control", {}).get("arms", {}).get(profile["gripper"]) != profile["arm"]:
        raise ValueError("grasp gripper does not share the selected arm's task admission")
    specs = read_yaml(root / "grasp/gripper_specs.yaml")
    specs = specs.get("grippers", specs.get("specs", specs))
    gripper = specs[profile["gripper_spec"]]
    frozen = {"name": name, **profile, "calibration_data": calibration,
              "tool": coordinates["tools"][profile["tool_name"]], "gripper_geometry": gripper}
    frozen["profile_id"] = digest(frozen)
    problems = readiness_problems(frozen)
    frozen["readiness_problems"] = problems
    if require_ready and problems:
        raise ValueError("profile is not ready: " + "; ".join(problems))
    return frozen


def readiness_problems(profile):
    problems = []
    for key in ("tool_geometry_verified", "corridor_verified", "execution_verified"):
        if profile.get(key) is not True:
            problems.append(key)
    for key in ("observation_joint_degrees", "return_joint_degrees"):
        val = profile.get(key)
        if not isinstance(val, list) or len(val) != 6 or not np.isfinite(val).all():
            problems.append(key)
    for key in ("approach_m", "lift_m"):
        val = profile.get(key)
        if type(val) not in (int, float) or not 0 < val < 0.5:
            problems.append(key)
    velocity = profile.get("velocity_percent")
    if type(velocity) is not int or not 1 <= velocity <= 100:
        problems.append("velocity_percent")
    bounds = np.asarray(profile.get("workspace_m"), dtype=float)
    if bounds.shape != (2, 3) or not np.isfinite(bounds).all() or np.any(bounds[0] >= bounds[1]):
        problems.append("workspace_m")
    try:
        transform(profile.get("T_grasp_tcp"))
    except (ValueError, TypeError):
        problems.append("T_grasp_tcp")
    cal = profile["calibration_data"]
    for key in ("camera_serial", "mount_id", "flange_frame", "color_frame"):
        if cal.get(key) != profile[key]:
            problems.append("calibration " + key)
    if cal.get("verified") is not True or not cal.get("calibration_id") or not cal.get("intrinsics_sha256"):
        problems.append("verified calibration identity")
    try:
        transform(cal.get("T_flange_color"))
    except (ValueError, TypeError):
        problems.append("T_flange_color")
    for key in ("maximum_validation_error_m", "measured_validation_error_m"):
        if type(cal.get(key)) not in (float, int) or not np.isfinite(cal[key]) or cal[key] <= 0:
            problems.append(key)
    if not any(k in problems for k in ("maximum_validation_error_m", "measured_validation_error_m")):
        if cal["measured_validation_error_m"] > cal["maximum_validation_error_m"]:
            problems.append("calibration accuracy")
    g = profile["gripper_geometry"]
    if g.get("verified") is not True:
        problems.append("measured gripper geometry")
    limits = {"min_opening_m": (0, 1), "max_opening_m": (0, 1),
              "finger_depth_m": (0.001, 1), "finger_thickness_m": (0.001, 1), "clearance_m": (0, 0.1)}
    for key, (lower, upper) in limits.items():
        value = g.get(key)
        if type(value) not in (float, int) or not np.isfinite(value) or not lower <= value <= upper:
            problems.append("gripper " + key)
    if not any(p.startswith("gripper ") for p in problems):
        if g["max_opening_m"] <= g["min_opening_m"] + 2*g["clearance_m"]:
            problems.append("gripper opening interval")
    if not isinstance(g.get("id"), str) or not g["id"]:
        problems.append("gripper identity")
    return problems
