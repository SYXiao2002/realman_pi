"""Geometric evidence for registration and frozen-plan coordinate conventions."""

from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from grasp_bridge.geometry import (adjust_geometry_response, from_pose, pose_drift,
                                   restricted_waypoints, to_pose)
from grasp_bridge.planner import NoReachableCandidate, attach_seed_joints, calculate_plan, choose_plan
from grasp_bridge.rgbd import (CameraModel, crop_depth_to_workspace, image_array,
                               register_depth, validate_timing)


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


def test_workspace_depth_crop_uses_camera_to_base_rotation_and_zeroes_outside():
    depth = np.ones((2, 2), dtype=np.float32)
    depth[1, 1] = 0
    K = np.eye(3)
    T_base_camera = np.eye(4)
    T_base_camera[:3, :3] = [[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]]
    T_base_camera[:3, 3] = [.5, .5, 0.]
    before = depth.copy()
    cropped = crop_depth_to_workspace(
        depth, K, T_base_camera,
        [[.49, 1.49, .99], [.51, 1.51, 1.01]])
    np.testing.assert_array_equal(cropped, [[0., 1.], [0., 0.]])
    np.testing.assert_array_equal(depth, before)


def test_workspace_depth_crop_keeps_float32_boundary_points_with_skew_intrinsics():
    depth = np.full((3, 3), 0.4, dtype=np.float32)
    K = np.array([[20., 2., 1.], [0., 20., 1.], [0., 0., 1.]])
    workspace = [[-0.055, -0.02, 0.39999999],
                 [0.055, 0.02, 0.40000001]]

    cropped = crop_depth_to_workspace(depth, K, np.eye(4), workspace)

    assert np.count_nonzero(cropped) == 9


def test_workspace_depth_crop_rejects_invalid_box_or_missing_transform():
    with pytest.raises(ValueError, match="workspace"):
        crop_depth_to_workspace(np.ones((2, 2)), np.eye(3), np.eye(4), [[0, 0, 0], [0, 1, 1]])
    with pytest.raises(ValueError, match="4x4"):
        crop_depth_to_workspace(np.ones((2, 2)), np.eye(3), None, [[0, 0, 0], [1, 1, 1]])


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


def test_bridge_geometry_offset_moves_all_contact_stages_and_preserves_native_pose():
    Tbc = np.eye(4)
    Tbc[:3, :3] = [[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]]
    native = from_pose([.1, .2, .5], [1, 0, 0, 0])
    before = native.copy(); before[2, 3] += .09
    after = before.copy(); after[:3, :3] = np.diag([1., -1., -1.])
    response = {"candidates": [{
        "candidate_id": "c1", "backend": "geometry", "T_camera_native_grasp": native,
        "T_camera_grasp": after, "T_camera_grasp_before_alignment": before,
        "T_camera_grasp_after_alignment": after, "metrics": {"native_origin_offset_m": .09},
    }], "diagnostics": {"archive_id": "frozen"}}

    adjusted = adjust_geometry_response(response, {"backend": "geometry", "T_base_camera": Tbc}, offset_m=.2)
    candidate = adjusted["candidates"][0]
    down_camera = Tbc[:3, :3].T @ [0., 0., -.2]
    for field, original in (("T_camera_grasp", after),
                            ("T_camera_grasp_before_alignment", before),
                            ("T_camera_grasp_after_alignment", after)):
        np.testing.assert_allclose(candidate[field][:3, 3], original[:3, 3] + down_camera)
        np.testing.assert_array_equal(candidate[field][:3, :3], original[:3, :3])
    np.testing.assert_array_equal(candidate["T_camera_native_grasp"], native)
    assert candidate["metrics"]["geometry_center_offset_m"] == .2
    assert adjusted["diagnostics"]["geometry_center_offset_source"] == "grasp_bridge"


def test_bridge_geometry_offset_is_consumed_by_planner_and_not_cgn():
    candidate = {"candidate_id": "c1", "backend": "geometry", "width_m": .03,
                 "T_camera_grasp": from_pose([0, 0, .5], [1, 0, 0, 0])}
    response = adjust_geometry_response({"candidates": [candidate]},
                                        {"backend": "geometry", "T_base_camera": np.eye(4)},
                                        offset_m=.2)
    profile = {"T_grasp_tcp": np.eye(4), "approach_m": .01, "lift_m": .01,
               "gripper_geometry": {"min_opening_m": 0., "max_opening_m": 1., "clearance_m": 0.},
               "workspace_m": [[-1, -1, 0], [1, 1, 1]]}
    plan = calculate_plan(response["candidates"], profile, np.eye(4))
    assert plan["waypoints"]["grasp"]["position_m"] == pytest.approx([0., 0., .3])
    cgn = {"candidate_id": "cgn", "backend": "contact_graspnet",
           "T_camera_grasp": from_pose([0, 0, .5], [1, 0, 0, 0])}
    cgn_adjusted = adjust_geometry_response({"candidates": [cgn]},
                                             {"backend": "contact_graspnet", "T_base_camera": np.eye(4)},
                                             offset_m=.2)
    np.testing.assert_array_equal(cgn_adjusted["candidates"][0]["T_camera_grasp"], cgn["T_camera_grasp"])


def test_bridge_geometry_offset_falls_back_to_candidate_approach_axis_without_calibration():
    candidate = {"candidate_id": "c1", "backend": "geometry",
                 "T_camera_grasp": from_pose([0, 0, .5], [1, 0, 0, 0])}
    adjusted = adjust_geometry_response({"candidates": [candidate]}, {"backend": "geometry"}, offset_m=.01)
    np.testing.assert_allclose(adjusted["candidates"][0]["T_camera_grasp"][:3, 3], [0., 0., .51])


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


def test_geometric_plan_is_independent_of_ik_and_keeps_pose_waypoints():
    profile = {"T_grasp_tcp": np.eye(4), "approach_m": 0.05, "lift_m": 0.05,
               "gripper_geometry": {"min_opening_m": 0., "max_opening_m": .08, "clearance_m": .002},
               "workspace_m": [[-1, -1, 0], [1, 1, 1]]}
    candidate = {"candidate_id": "c1", "width_m": .03,
                 "T_camera_grasp": from_pose([0, 0, .5], [1, 0, 0, 0])}
    plan = calculate_plan([candidate], profile, np.eye(4))
    assert plan["candidate_id"] == "c1"
    assert all(item["joint_degrees"] is None for item in plan["waypoints"].values())
    with_seed = attach_seed_joints(plan, [1, 2, 3, 4, 5, 6])
    assert all(item["joint_degrees"] == [1, 2, 3, 4, 5, 6]
               for item in with_seed["waypoints"].values())


def test_all_rejected_candidates_remain_structured_on_exception():
    profile = {"T_grasp_tcp": np.eye(4), "approach_m": .05, "lift_m": .05,
               "gripper_geometry": {"min_opening_m": 0., "max_opening_m": .08, "clearance_m": .002},
               "workspace_m": [[-1, -1, 0], [1, 1, 1]]}
    candidate = {"candidate_id": "c1", "width_m": .03,
                 "T_camera_grasp": from_pose([0, 0, .5], [1, 0, 0, 0])}
    with pytest.raises(NoReachableCandidate) as failure:
        choose_plan([candidate], profile, np.eye(4), [0]*6, lambda *_: (_ for _ in ()).throw(ValueError("IK failed")),
                    sample_step_m=.01, max_joint_step=15)
    assert failure.value.rejections == [{"candidate_id": "c1", "reason": "IK failed"}]


def test_collision_gate_blocks_even_without_ik_precheck():
    profile = {"T_grasp_tcp": np.eye(4), "approach_m": .05, "lift_m": .05,
               "gripper_geometry": {"min_opening_m": 0., "max_opening_m": .08, "clearance_m": .002},
               "workspace_m": [[-1, -1, 0], [1, 1, 1]]}
    candidate = {"candidate_id": "c1", "width_m": .03,
                 "T_camera_grasp": from_pose([0, 0, .5], [1, 0, 0, 0]),
                 "collision": {"status": "unknown"}}
    with pytest.raises(NoReachableCandidate) as failure:
        calculate_plan([candidate], profile, np.eye(4), collision_config={"blocking_statuses": ["unknown"]})
    assert "collision status is unknown" in failure.value.rejections[0]["reason"]
