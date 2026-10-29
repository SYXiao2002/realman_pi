"""Synthetic, ROS-free evidence for the visual finger collision contract."""

from pathlib import Path
import sys

import numpy as np
import pytest
import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grasp_bridge.gripper_collision import (annotate_candidates, evaluate_candidate_collision, finger_boxes,
                                             validate_collision_config)


GEOMETRY = {"min_opening_m": .02, "max_opening_m": .08, "finger_thickness_m": .01,
            "finger_width_m": .02, "finger_depth_m": .04, "clearance_m": .003}
PROFILE = {"gripper_geometry": GEOMETRY, "approach_m": .02, "lift_m": .03}
CONFIG = {"enabled": True, "warning_clearance_m": .003, "collision_penetration_m": .002,
          "sweep_step_m": .005, "cluster_voxel_m": .003, "minimum_local_points": 2,
          "minimum_collision_points": 2, "blocking_statuses": ["collision", "warning", "unknown"]}


def candidate():
    return {"width_m": .03, "T_base_grasp": np.eye(4)}


def test_finger_boxes_clamp_opening_and_fallback_width():
    geometry = finger_boxes({"width_m": .2}, {**GEOMETRY, "finger_width_m": None})
    assert geometry["opening_m"] == pytest.approx(.08)
    assert geometry["boxes"][0]["center_m"] == pytest.approx([-.045, 0, -.02])
    assert geometry["boxes"][0]["size_m"] == pytest.approx([.01, .01, .04])
    assert [box["name"] for box in geometry["boxes"]] == ["left_finger", "right_finger"]


def test_collision_uses_signed_sdf_and_confirms_voxel_cluster():
    # Two neighboring points are inside the right finger; an isolated point is
    # near the left finger but cannot confirm a collision cluster by itself.
    points = np.array([[.025, 0, -.02], [.026, .001, -.02], [-.041, 0, -.02], [.4, .4, .4]])
    result = evaluate_candidate_collision(candidate(), points, np.ones(len(points), bool), PROFILE, CONFIG)
    assert result["status"] == "collision"
    assert result["collision_points"] >= 2
    assert result["near_points"] == 0
    assert result["max_penetration_m"] > 0
    assert result["worst_stage"] in {"pregrasp", "approach_sweep", "grasp", "lift"}
    assert result["geometry"] == finger_boxes(candidate(), GEOMETRY)


def test_clearance_with_enough_expanded_envelope_points_is_safe():
    # These points sit in the expanded pregrasp envelope's corners, outside
    # the warning SDF shell. They provide local evidence without a collision.
    points = np.array([
        [-.0155, .0125, -.0625],
        [-.0305, -.0125, -.0625],
    ])
    result = evaluate_candidate_collision(candidate(), points, np.ones(2, bool), PROFILE, CONFIG)
    assert result["status"] == "safe"
    assert result["local_environment_points"] == 2
    assert result["min_clearance_m"] >= CONFIG["warning_clearance_m"]


def test_environment_excludes_target_points_and_unknown_has_null_metrics():
    points = np.array([[.025, 0, -.02], [.026, .001, -.02], [.4, .4, .4]])
    result = evaluate_candidate_collision(candidate(), points, np.array([False, False, True]), PROFILE, CONFIG)
    assert result["status"] == "unknown"
    assert result["min_clearance_m"] is not None
    unknown = evaluate_candidate_collision(candidate(), points, np.zeros(3, bool), PROFILE, CONFIG)
    assert unknown["status"] == "unknown"
    assert unknown["min_clearance_m"] is None and unknown["max_penetration_m"] is None


def test_disabled_collision_is_unknown_and_config_validation_rejects_bad_thresholds():
    disabled = evaluate_candidate_collision(candidate(), np.array([[.4, .4, .4]]), np.ones(1, bool), PROFILE,
                                             {**CONFIG, "enabled": False})
    assert disabled["status"] == "unknown"
    with pytest.raises(ValueError):
        validate_collision_config({**CONFIG, "collision_penetration_m": .01})


def test_annotate_candidates_preserves_collision_object_for_each_candidate():
    labels = np.ones((4, 4), np.uint16)
    ok, encoded = cv2.imencode(".png", labels)
    assert ok
    observation = {"depth_m": np.full((4, 4), .5, np.float32), "K": np.eye(3), "T_base_camera": np.eye(4)}
    response = {"candidates": [{"candidate_id": "1:0", "object_id": "1", "width_m": .03,
                                 "T_camera_grasp": np.eye(4)}],
                "diagnostics": {"segmap_png": encoded.tobytes()}}
    result = annotate_candidates(response, observation,
                                 {**PROFILE, "workspace_m": [[-1, -1, 0], [1, 1, 1]]}, CONFIG)
    assert result["candidates"][0]["collision"]["geometry"]["model"] == "visual_finger_boxes/v1"
