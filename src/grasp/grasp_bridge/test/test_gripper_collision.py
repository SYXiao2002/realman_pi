"""Synthetic, ROS-free evidence for the visual finger collision contract."""

from pathlib import Path
import sys

import numpy as np
import pytest
import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grasp_bridge.gripper_collision import (annotate_candidates, evaluate_candidate_collision, finger_boxes,
                                             validate_collision_config, _point_cloud)
import grasp_bridge.gripper_collision as collision_module


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


def test_point_cloud_uses_wider_collision_region_when_present():
    # Pixel (0, 0) with K = I reprojects to the base point [0, 0, 0.5]. The tight
    # workspace crop dropped it (bin wall), but the wider collision crop kept it.
    tight = np.zeros((2, 2), np.float32)
    wide = np.array([[.5, 0.], [0., 0.]], np.float32)
    observation = {"depth_m": tight, "collision_depth_m": wide,
                   "K": np.eye(3), "T_base_camera": np.eye(4)}
    profile = {"workspace_m": [[.1, .1, .1], [.2, .2, .2]],
               "collision_workspace_m": [[-1., -1., -1.], [1., 1., 1.]]}
    _, base, yy, xx = _point_cloud(observation, profile)
    assert len(base) == 1 and list(yy) == [0] and list(xx) == [0]
    np.testing.assert_allclose(base[0], [0., 0., .5])


def test_point_cloud_falls_back_to_workspace_without_collision_region():
    observation = {"depth_m": np.array([[.5, 0.], [0., 0.]], np.float32),
                   "K": np.eye(3), "T_base_camera": np.eye(4)}
    _, base, _, _ = _point_cloud(observation, {"workspace_m": [[-1., -1., -1.], [1., 1., 1.]]})
    assert len(base) == 1
    _, base, _, _ = _point_cloud(observation, {"workspace_m": [[.1, .1, .1], [.2, .2, .2]]})
    assert len(base) == 0


@pytest.mark.parametrize("seed", range(8))
def test_broadphase_preserves_full_cloud_metrics_for_rotated_sweeps(monkeypatch, seed):
    rng = np.random.default_rng(seed)
    rotation, _ = np.linalg.qr(rng.normal(size=(3, 3)))
    pose = np.eye(4)
    pose[:3, :3] = rotation
    pose[:3, 3] = rng.uniform(-.5, .5, 3)
    item = {**candidate(), "T_base_grasp": pose}
    points = rng.uniform(-.12, .12, (1800, 3)) @ rotation.T + pose[:3, 3]
    # Include local-evidence corners, in-box clusters, and distant observations.
    local = np.array([[.0305, .0125, -.0625], [-.0305, -.0125, -.0625],
                      [.025, 0., -.02], [.026, .001, -.02]])
    points = np.concatenate((points, local @ rotation.T + pose[:3, 3], rng.uniform(1., 2., (80, 3))))
    mask = rng.random(len(points)) > .1
    actual = evaluate_candidate_collision(item, points, mask, PROFILE, CONFIG)
    monkeypatch.setattr(collision_module, "_broadphase_mask",
                        lambda points, *args: np.ones(len(points), dtype=bool))
    expected = evaluate_candidate_collision(item, points, mask, PROFILE, CONFIG)
    assert actual == expected


def test_broadphase_preserves_rotated_local_evidence_corners():
    pose = np.eye(4)
    angle = np.pi / 4
    pose[:3, :3] = [[np.cos(angle), -np.sin(angle), 0.],
                   [np.sin(angle), np.cos(angle), 0.], [0., 0., 1.]]
    geometry = finger_boxes(candidate(), GEOMETRY)
    local = np.array([[.0305, .0125, -.0425], [.025, 0., -.02]])
    points = local @ pose[:3, :3].T
    mask = collision_module._broadphase_mask(points, [pose], geometry, CONFIG["warning_clearance_m"])
    assert mask.all()


def test_broadphase_preserves_each_stage_clearance_when_all_points_are_far(monkeypatch):
    points = np.array([[.4, .3, .2], [-.2, -.5, -.8], [.5, .5, -.2]])
    mask = np.ones(len(points), dtype=bool)
    actual = evaluate_candidate_collision(candidate(), points, mask, PROFILE, CONFIG)
    monkeypatch.setattr(collision_module, "_broadphase_mask",
                        lambda points, *args: np.ones(len(points), dtype=bool))
    expected = evaluate_candidate_collision(candidate(), points, mask, PROFILE, CONFIG)
    assert actual == expected
    assert all(stage["min_clearance_m"] > CONFIG["warning_clearance_m"]
               for stage in actual["stages"].values())


def test_display_broadphase_preserves_masks_and_target_exclusion(monkeypatch):
    rng = np.random.default_rng(42)
    points = np.concatenate((rng.uniform(-.1, .1, (3000, 3)),
                             [[.025, 0., -.02], [.031, 0., -.02]]))
    labels = rng.integers(0, 3, len(points))
    item = {**candidate(), "object_id": "1", "T_camera_grasp": np.eye(4)}
    collision = evaluate_candidate_collision(item, points, labels != 1, PROFILE, CONFIG)
    actual = collision_module.display_masks(item, collision, points, labels, np.eye(4))
    monkeypatch.setattr(collision_module, "_broadphase_mask",
                        lambda points, *args: np.ones(len(points), dtype=bool))
    expected = collision_module.display_masks(item, collision, points, labels, np.eye(4))
    assert actual == expected
    assert np.any(actual["collision_mask"])
    assert not np.any(np.asarray(actual["collision_mask"])[labels == 1])
