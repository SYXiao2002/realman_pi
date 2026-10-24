"""Geometric evidence for registration and frozen-plan coordinate conventions."""

from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from grasp_bridge.geometry import from_pose, pose_drift, restricted_waypoints, to_pose
from grasp_bridge.planner import choose_plan
from grasp_bridge.rgbd import CameraModel, image_array, register_depth, validate_timing


def camera(frame="optical"):
    return CameraModel(8, 8, frame, np.array([[10., 0, 4], [0, 10., 4], [0, 0, 1]]), np.zeros(5))


def test_registration_uses_extrinsic_baseline_and_nearest_surface():
    depth = np.zeros((8, 8), dtype=np.float32)
    depth[4, 3] = 1.0
    depth[4, 4] = 2.0
    T = np.eye(4)
    T[0, 3] = 0.1
    registered = register_depth(depth, camera("depth"), camera("color"), T)
    # 10 pixel/metre baseline shifts z=1 by one pixel; z=2 lands at 4.5 -> 4.
    assert registered[4, 4] == 1.0
    assert registered[4, 3] == 0
    assert np.count_nonzero(registered) == 1


def test_image_decode_respects_padding_and_depth_units():
    raw = np.array([[1000, 2000, 65535], [0, 500, 65535]], dtype=">u2").tobytes()
    message = SimpleNamespace(encoding="16UC1", width=2, height=2, is_bigendian=True, step=6, data=raw)
    depth = image_array(message, depth=True)
    np.testing.assert_allclose(depth, [[1, 2], [0, 0.5]])
    assert depth.dtype == np.float32


def test_stale_and_unpaired_observation_are_rejected():
    validate_timing([1_000_000_000, 1_020_000_000], 1_030_000_000, skew_sec=0.05, age_sec=0.1)
    with pytest.raises(ValueError, match="paired"):
        validate_timing([1_000_000_000, 1_100_000_000], 1_110_000_000, skew_sec=0.05, age_sec=0.2)
    with pytest.raises(ValueError, match="stale"):
        validate_timing([1_000_000_000], 1_500_000_000, skew_sec=0.05, age_sec=0.1)


def test_capture_chain_and_grasp_tool_offset_compose_once():
    bc = from_pose([0.2, 0.1, 0.8], [0, 1, 0, 0])
    cg = from_pose([0.1, 0.0, 0.4], [1, 0, 0, 0])
    gt = from_pose([0, 0, -0.02], [1, 0, 0, 0])
    points = restricted_waypoints(bc, cg, gt, approach_m=0.05, lift_m=0.1,
                                 workspace=[[-1, -1, 0], [1, 1, 1]])
    np.testing.assert_allclose(points["grasp"][:3, 3], [0.3, 0.1, 0.42])
    np.testing.assert_allclose(points["pregrasp"][:3, 3], [0.3, 0.1, 0.47])
    np.testing.assert_allclose(points["lift"][:3, 3], [0.3, 0.1, 0.52])
    for T in points.values():
        p, q = to_pose(T)
        np.testing.assert_allclose(from_pose(p, q), T, atol=1e-10)
    assert pose_drift(points["grasp"], points["lift"])[0] == pytest.approx(0.1)


def test_invalid_rotation_and_workspace_escape_rejected():
    bad = np.eye(4)
    bad[0, 0] = -1
    with pytest.raises(ValueError, match="right-handed"):
        to_pose(bad)
    with pytest.raises(ValueError, match="corridor"):
        restricted_waypoints(np.eye(4), from_pose([0, 0, 0.9], [1, 0, 0, 0]), np.eye(4),
                             approach_m=0.1, lift_m=0.2, workspace=[[-1, -1, 0], [1, 1, 1]])


def test_ik_rejects_first_candidate_then_samples_second_corridor():
    profile = {"T_grasp_tcp": np.eye(4), "approach_m": 0.05, "lift_m": 0.05,
               "gripper_geometry": {"min_opening_m": 0., "max_opening_m": .08, "clearance_m": .002},
               "workspace_m": [[-1, -1, 0], [1, 1, 1]]}
    candidates = [{"candidate_id": label, "width_m": .03, "T_camera_grasp": from_pose([x, 0, 0.5], [1, 0, 0, 0])}
                  for label, x in (("too-wide", .2), ("bad", 0.5), ("good", 0.1))]
    candidates[0]["width_m"] = .1
    visited = []

    def ik(T, seed):
        visited.append(T[:3, 3].copy())
        if T[0, 3] > 0.4:
            raise ValueError("unreachable")
        return [v + 0.1 for v in seed]

    plan = choose_plan(candidates, profile, np.eye(4), [0]*6, ik, sample_step_m=0.01, max_joint_step=5)
    assert plan["candidate_id"] == "good"
    assert plan["rejections"][0]["reason"] == "candidate exceeds measured gripper opening"
    assert plan["rejections"][1]["reason"] == "unreachable"
    assert len(candidates) == 3 and candidates[0]["width_m"] == .1
    assert all(point[0] != .2 for point in visited)
    assert len(visited) >= 12  # endpoints alone cannot pass this evidence.
