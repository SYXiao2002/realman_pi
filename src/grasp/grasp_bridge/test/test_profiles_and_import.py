"""A solved artifact never silently enables execution or another arm."""

import json
from pathlib import Path
import shutil
import sys

import numpy as np
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grasp_bridge.calibration_import import stage
from grasp_bridge.profiles import digest, load_profile, read_yaml

PROJECT = Path(__file__).resolve().parents[4]


@pytest.fixture
def config(tmp_path):
    shutil.copytree(PROJECT / "config/ros", tmp_path / "ros")
    shutil.copytree(PROJECT / "config/grasp", tmp_path / "grasp")
    return tmp_path


def artifact(profile):
    intrinsics = {"width": 8, "height": 8, "k": [10., 0., 4., 0., 10., 4., 0., 0., 1.],
                  "d": [0.] * 5, "distortion_model": "plumb_bob"}
    identity = {"arm_id": profile["arm"], "camera_id": profile["camera"].removeprefix("camera_"),
                "serial": profile["camera_serial"], "mount_id": profile["mount_id"],
                "base_frame": profile["base_frame"], "flange_frame": profile["flange_frame"],
                "optical_frame": profile["color_frame"], "board_sha256": "test-board"}
    value = {"schema_version": 1, "session_id": "session-test", "arm_id": profile["arm"],
             "identity": identity, "intrinsics": intrinsics, "intrinsics_sha256": digest(intrinsics),
             "hand_eye": {profile["arm"]: {"tool_to_camera": np.eye(4).tolist()}}, "verified_for_grasp": False}
    value["calibration_id"] = digest(value)
    return value


def test_unmeasured_profile_refuses_execution_and_freezes_local_changes(config):
    first = load_profile(config, "right_wrist")
    assert "verified calibration identity" in first["readiness_problems"]
    assert "gripper max_opening_m" in first["readiness_problems"]
    with pytest.raises(ValueError, match="not ready"):
        load_profile(config, "right_wrist", require_ready=True)
    (config / "ros/grasp_profiles.local.yaml").write_text(
        "profiles:\n  right_wrist:\n    velocity_percent: 3\n")
    assert first["profile_id"] != load_profile(config, "right_wrist")["profile_id"]
    assert load_profile(config, "left_wrist")["arm"] == "l"


def test_cross_arm_gripper_mapping_is_rejected(config):
    (config / "ros/grasp_profiles.local.yaml").write_text(
        "profiles:\n  right_wrist:\n    gripper: gripper_left\n")
    with pytest.raises(ValueError, match="task admission"):
        load_profile(config, "right_wrist")


def test_stage_is_unverified_preserves_other_arm_and_checks_provenance(config):
    path = config / "result.json"
    left = artifact(load_profile(config, "left_wrist"))
    path.write_text(json.dumps(left))
    stage(config, "left_wrist", path)
    right = artifact(load_profile(config, "right_wrist"))
    path.write_text(json.dumps(right))
    staged = stage(config, "right_wrist", path)
    data = yaml.safe_load(staged.read_text())["calibrations"]
    assert set(data) == {"left_wrist", "right_wrist"}
    assert data["right_wrist"]["verified"] is False
    assert data["right_wrist"]["maximum_validation_error_m"] is None
    with pytest.raises(ValueError, match="selected-arm"):
        stage(config, "left_wrist", path)
    right["hand_eye"]["r"]["tool_to_camera"][0][3] += 0.1
    path.write_text(json.dumps(right))
    with pytest.raises(ValueError, match="content hash"):
        stage(config, "right_wrist", path)
    assert yaml.safe_load(staged.read_text()) == {"calibrations": data}


def test_comment_only_local_override_is_supported(config):
    path = config / "ros/camera_extrinsics.local.yaml"
    path.write_text("# Operator measurements go here.\n")
    result = config / "result.json"
    result.write_text(json.dumps(artifact(load_profile(config, "right_wrist"))))
    stage(config, "right_wrist", result)
    assert read_yaml(config / "ros/camera_extrinsics.yaml")["calibrations"]["right_wrist"]["verified"] is False
