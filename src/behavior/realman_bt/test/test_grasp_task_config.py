"""Profile selection must follow the selected wrist and fail closed on real inputs."""

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src/behavior/realman_bt/launch"))
sys.path.insert(0, str(ROOT / "src/grasp/grasp_bridge"))
from grasp_task_config import load_grasp_task_parameters  # noqa: E402


@pytest.fixture
def config_root(tmp_path):
    root = tmp_path / "config"
    for file in ("ros/grasp_task.yaml", "ros/grasp_profiles.yaml", "ros/grasp_bridge.yaml",
                 "ros/camera_extrinsics.yaml", "ros/cameras_ros2.yaml", "ros/realman_coordinates.yaml",
                 "ros/gripper.yaml",
                 "grasp/gripper_specs.yaml"):
        destination = root / file
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "config" / file, destination)
    return root


def test_default_profile_uses_shared_config_without_manual_approval_gate(config_root):
    # Isolate launcher wiring from the changing on-site observation/return poses.
    (config_root / "ros/grasp_profiles.local.yaml").write_text(
        "profiles:\n  right_wrist:\n"
        "    observation_joint_degrees: [1, 2, 3, 4, 5, 6]\n"
        "    return_joint_degrees: [-1, -2, -3, -4, -5, -6]\n")
    params = load_grasp_task_parameters(config_root)
    assert params["grasp_cycles"] == -1
    assert params["arm_id"] == "r"
    assert params["grasp_gripper"] == "gripper_right"
    assert params["grasp_observation_joint_degrees"] == "1,2,3,4,5,6"
    assert params["grasp_return_joint_degrees"] == "-1,-2,-3,-4,-5,-6"
    assert params["grasp_confirmation_timeout_sec"] == 120.0
    assert params["grasp_target_label"] == "eu"
    assert "grasp_execution_ready" not in params


def test_selected_left_wrist_drives_arm_gripper_and_profile_identity(config_root):
    (config_root / "ros/grasp_bridge.local.yaml").write_text("profile: left_wrist\ndry_run: false\ntarget_label: box\n")
    right = load_grasp_task_parameters(config_root, profile_name="right_wrist")
    left = load_grasp_task_parameters(config_root)
    assert left["arm_id"] == "l"
    assert left["grasp_gripper"] == "gripper_left"
    assert left["grasp_base_frame"] == "l/base_link"
    assert left["grasp_profile_id"] != right["grasp_profile_id"]
    assert left["grasp_target_label"] == "box"
    assert "grasp_execution_ready" not in left


@pytest.mark.parametrize("value", ["0", "-1", ".nan", "true", "null"])
def test_invalid_lease_ttl_is_rejected_before_executor_creation(config_root, value):
    (config_root / "ros/grasp_task.local.yaml").write_text("lease_ttl_sec: " + value + "\n")
    with pytest.raises(ValueError, match="lease_ttl_sec"):
        load_grasp_task_parameters(config_root)


def test_mock_backend_is_rejected_for_physical_execution(config_root):
    (config_root / "ros/grasp_bridge.local.yaml").write_text("backend: mock\n")
    with pytest.raises(ValueError, match="mock backend"):
        load_grasp_task_parameters(config_root)


def test_backend_name_matches_wire_protocol(config_root):
    (config_root / "ros/grasp_bridge.local.yaml").write_text("backend: contact_graspnet\n")
    assert load_grasp_task_parameters(config_root)["grasp_backend"] == "contact_graspnet"
    (config_root / "ros/grasp_bridge.local.yaml").write_text("backend: cgn\n")
    with pytest.raises(ValueError, match="backend"):
        load_grasp_task_parameters(config_root)


@pytest.mark.parametrize("value", ["0", "-1", ".nan", "true", "null"])
def test_invalid_confirmation_deadline_is_rejected(config_root, value):
    (config_root / "ros/grasp_task.local.yaml").write_text("confirmation_timeout_sec: " + value + "\n")
    with pytest.raises(ValueError, match="confirmation_timeout_sec"):
        load_grasp_task_parameters(config_root)


@pytest.mark.parametrize("value", ["0", "-1", ".nan", "true", "null"])
def test_invalid_motion_result_grace_is_rejected(config_root, value):
    (config_root / "ros/grasp_task.local.yaml").write_text("motion_result_grace_sec: " + value + "\n")
    with pytest.raises(ValueError, match="motion_result_grace_sec"):
        load_grasp_task_parameters(config_root)
