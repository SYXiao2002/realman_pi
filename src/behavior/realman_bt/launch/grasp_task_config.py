"""Load the selected grasp profile once before any executor client is created."""

import math
from pathlib import Path


def load_grasp_task_parameters(config_root, *, profile_name=None, target_label=None):
    # The ROS image builds grasp_bridge. No algorithm/ML dependency is imported.
    from grasp_bridge.profiles import load_profile, read_yaml

    root = Path(config_root)
    settings = read_yaml(root / "ros/grasp_task.yaml")
    bridge = read_yaml(root / "ros/grasp_bridge.yaml")
    name = profile_name or bridge["profile"]
    profile = load_profile(root, name)
    label = target_label or bridge.get("target_label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("target_label must be configured before starting grasp.xml")
    backend = bridge.get("backend")
    if backend not in ("geometry", "contact_graspnet", "mock"):
        raise ValueError("grasp backend must be geometry/contact_graspnet/mock")
    if backend == "mock":
        raise ValueError("mock backend cannot execute a physical grasp")
    cycles = settings.get("cycles", 1)
    if type(cycles) is not int or cycles == 0 or cycles < -1:
        raise ValueError("cycles must be -1 or a positive integer")
    velocity = profile.get("velocity_percent")
    if type(velocity) is not int or not 1 <= velocity <= 100:
        raise ValueError("profile velocity_percent must be integer [1,100]")
    observation = profile.get("observation_joint_degrees")
    return_joints = profile.get("return_joint_degrees")
    # Missing physical values stay missing. No placeholder robot pose is invented.
    joints = "" if observation is None else ",".join(str(x) for x in observation)
    result = {
        "grasp_cycles": cycles,
        "arm_id": profile["arm"],
        "grasp_profile_name": name,
        "grasp_profile_id": profile["profile_id"],
        "grasp_base_frame": profile["base_frame"],
        "grasp_tool_name": profile["tool_name"],
        "grasp_gripper": profile["gripper"],
        "grasp_target_label": label,
        "grasp_backend": backend,
        "grasp_observation_joint_degrees": joints,
        "grasp_return_joint_degrees": "" if return_joints is None else ",".join(str(x) for x in return_joints),
        "grasp_velocity_percent": velocity,
    }
    for key in ("lease_ttl_sec", "acquire_timeout_sec", "motion_timeout_sec",
                "plan_timeout_sec", "gripper_timeout_sec", "hold_sec", "confirmation_timeout_sec",
                "motion_result_grace_sec"):
        value = settings.get(key, 5.0 if key == "motion_result_grace_sec" else None)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be positive finite seconds")
        result["grasp_" + key] = float(value)
    return result
