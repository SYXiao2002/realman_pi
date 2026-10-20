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


def test_unmeasured_default_profile_stays_unready_without_invented_robot_pose(config_root):
    params = load_grasp_task_parameters(config_root)
    assert params["arm_id"] == "r"
    assert params["grasp_gripper"] == "gripper_right"
    assert params["grasp_observation_joint_degrees"] == ""
    assert params["grasp_return_joint_degrees"] == ""
    assert params["grasp_confirmation_timeout_sec"] == 120.0
    assert params["grasp_execution_ready"] is False
    assert "target_label" in params["grasp_readiness_detail"]
    assert "calibration" in params["grasp_readiness_detail"]


def test_selected_left_wrist_drives_arm_gripper_and_profile_identity(config_root):
    (config_root / "ros/grasp_task.local.yaml").write_text("profile: left_wrist\ntarget_label: box\n")
    (config_root / "ros/grasp_bridge.local.yaml").write_text("profile: left_wrist\ndry_run: false\n")
    right = load_grasp_task_parameters(config_root, profile_name="right_wrist")
    left = load_grasp_task_parameters(config_root)
    assert left["arm_id"] == "l"
    assert left["grasp_gripper"] == "gripper_left"
    assert left["grasp_base_frame"] == "l/base_link"
    assert left["grasp_profile_id"] != right["grasp_profile_id"]
    assert left["grasp_target_label"] == "box"
    assert left["grasp_execution_ready"] is False  # Physical acceptance remains required.


@pytest.mark.parametrize("value", ["0", "-1", ".nan", "true", "null"])
def test_invalid_lease_ttl_is_rejected_before_executor_creation(config_root, value):
    (config_root / "ros/grasp_task.local.yaml").write_text("lease_ttl_sec: " + value + "\n")
    with pytest.raises(ValueError, match="lease_ttl_sec"):
        load_grasp_task_parameters(config_root)


def test_mock_backend_is_never_ready_for_physical_execution(config_root):
    (config_root / "ros/grasp_task.local.yaml").write_text("backend: mock\n")
    params = load_grasp_task_parameters(config_root)
    assert not params["grasp_execution_ready"]
    assert "mock backend" in params["grasp_readiness_detail"]


def test_backend_name_matches_wire_protocol(config_root):
    (config_root / "ros/grasp_task.local.yaml").write_text("backend: contact_graspnet\n")
    assert load_grasp_task_parameters(config_root)["grasp_backend"] == "contact_graspnet"
    (config_root / "ros/grasp_task.local.yaml").write_text("backend: cgn\n")
    with pytest.raises(ValueError, match="backend"):
        load_grasp_task_parameters(config_root)


@pytest.mark.parametrize("value", ["0", "-1", ".nan", "true", "null"])
def test_invalid_confirmation_deadline_is_rejected(config_root, value):
    (config_root / "ros/grasp_task.local.yaml").write_text("confirmation_timeout_sec: " + value + "\n")
    with pytest.raises(ValueError, match="confirmation_timeout_sec"):
        load_grasp_task_parameters(config_root)
