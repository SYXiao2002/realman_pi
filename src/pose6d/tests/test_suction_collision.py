"""Depth-only cylinder interval, noisy/occluded depth and IK gating."""

from pathlib import Path

import numpy as np
import pytest

from pose6d.geometry import suction_pose
from pose6d.suction_collision import check_suction_path


IK = {"checked": True, "reachable": True}
SUCTION = {"cup_diameter_m": .01}


def scene(normal=(0, 0, -1)):
    contact, _ = suction_pose(np.eye(4), {
        "center_obj": [0, 0, .5], "normal_obj": normal, "tangent_ref_obj": [0, 1, 0]})
    K = np.array([[800., 0, 100], [0, 800., 100], [0, 0, 1.]])
    vv, uu = np.indices((200, 200))
    rays = np.stack(((uu - 100) / 800, (vv - 100) / 800, np.ones_like(uu)), axis=-1)
    normal = np.asarray(normal)
    depth = ((contact[:3, 3] @ normal) / (rays @ normal)).astype(np.float32)
    return {"depth_m": depth, "K": K}, contact, rays


def check(obs, contact, **kwargs):
    return check_suction_path(obs, contact, IK, kwargs.pop("suction", SUCTION), kwargs or None)


@pytest.mark.parametrize("normal", [(0, 0, -1), (.6, 0, -.8), (0, .8, -.6)])
def test_cylinder_is_30mm_long_between_10_and_40mm_from_contact(normal):
    obs, contact, _ = scene(normal)
    result = check(obs, contact)
    assert result["status"] == "clear"
    assert result["diameter_m"] == .01 and result["length_m"] == pytest.approx(.03)
    assert result["start_distance_m"] == .01 and result["end_distance_m"] == .04
    assert result["observed_fraction"] > .95
    assert result["roi_pixels"] < obs["depth_m"].size / 20
    assert result["elapsed_ms"] >= 0


@pytest.mark.parametrize("normal", [(0, 0, -1), (.6, 0, -.8)])
def test_object_inside_cylinder_collides_regardless_of_mask(normal):
    obs, contact, rays = scene(normal)
    normal = np.asarray(normal)
    point = contact[:3, 3] + normal * .02
    uv = obs["K"] @ point
    x, y = np.rint(uv[:2] / uv[2]).astype(int)
    patch = np.s_[y-1:y+2, x-1:x+2]
    obs["depth_m"][patch] = (normal @ contact[:3, 3] + .02) / (rays[patch] @ normal)
    counts = []
    for mask in (None, np.zeros(obs["depth_m"].shape), np.ones(obs["depth_m"].shape), np.ones((1, 1))):
        obs["mask"] = mask
        result = check(obs, contact)
        assert result["status"] == "collision" and result["reason"] == "吸取路径上有物体"
        assert result["largest_cluster_pixels"] >= 3
        counts.append(result["collision_pixels"])
    assert len(set(counts)) == 1


@pytest.mark.parametrize("offset_m", [.001, .0025, .0035, .0043, .007, .0099])
def test_first_10mm_is_outside_corridor_with_or_without_target_mask(offset_m):
    obs, contact, _ = scene()
    obs["depth_m"][:] = .5 - offset_m
    for mask in (None, np.zeros_like(obs["depth_m"]), np.ones_like(obs["depth_m"])):
        obs["mask"] = mask
        result = check(obs, contact)
        assert result["status"] == "clear" and result["collision_pixels"] == 0
        assert result["observed_fraction"] > .95


@pytest.mark.parametrize("distance_mm,collides", [(9.9, False), (10, True), (10.1, True),
                                                 (25, True), (39.9, True), (40, True), (40.1, False)])
def test_both_interval_end_caps_are_included(distance_mm, collides):
    obs, contact, _ = scene()
    obs["depth_m"][99:102, 99:102] = .5 - distance_mm / 1000
    assert (check(obs, contact)["status"] == "collision") is collides


@pytest.mark.parametrize("name,expected", [("contact_front", "clear"), ("contact_top", "clear"),
                                          ("neighbor_obstacle", "collision")])
def test_real_depth_crops_do_not_depend_on_segmentation(name, expected):
    with np.load(Path(__file__).parent / "data/suction_contact_depth_offsets.npz", allow_pickle=False) as fixture:
        obs = {"depth_m": fixture[name + "_depth_m"], "K": fixture[name + "_K"]}
        contact = fixture[name + "_contact"]
        counts = []
        for mask in (None, fixture[name + "_mask"], np.ones_like(obs["depth_m"]), np.zeros_like(obs["depth_m"])):
            obs["mask"] = mask
            result = check(obs, contact)
            assert result["status"] == expected
            counts.append(result["collision_pixels"])
        assert len(set(counts)) == 1


def test_isolated_depth_noise_and_points_outside_cylinder_do_not_collide():
    obs, contact, _ = scene()
    obs["depth_m"][100, 100] = .48
    obs["depth_m"][105, 105] = .48
    obs["depth_m"][100:103, 120:123] = .48
    result = check(obs, contact)
    assert result["status"] == "clear" and result["largest_cluster_pixels"] == 1


def test_diameter_is_read_from_current_tool_configuration():
    obs, contact, _ = scene()
    obs["depth_m"][99:102, 113:116] = .48
    assert check(obs, contact)["status"] == "clear"
    assert check(obs, contact, suction={"cup_diameter_m": .02})["status"] == "collision"


@pytest.mark.parametrize("depth", [0, np.nan, .44])
def test_missing_or_foreground_occlusion_never_claims_clear(depth):
    obs, contact, _ = scene()
    obs["depth_m"][:] = depth
    result = check(obs, contact)
    assert result["status"] == "unknown" and result["reason"] == "DEPTH_MISSING_OR_OCCLUDED"


def test_offscreen_behind_camera_invalid_inputs_and_cost_limit_are_unknown():
    obs, contact, _ = scene()
    assert check(obs, contact, maximum_roi_pixels=1)["reason"] == "ROI_TOO_LARGE"
    contact[0, 3] = 1
    assert check(obs, contact)["status"] == "unknown"
    contact[0, 3], contact[2, 3] = 0, -.1
    assert check(obs, contact)["reason"] == "CORRIDOR_BEHIND_CAMERA"
    obs, contact, _ = scene()
    obs["K"] = None
    assert check(obs, contact)["reason"] == "INVALID_COLLISION_INPUT"


@pytest.mark.parametrize("config", [{"start_distance_m": -.001}, {"end_distance_m": .01},
                                    {"start_distance_m": .05}, {"end_distance_m": np.nan},
                                    {"depth_tolerance_m": -.001}])
def test_invalid_interval_is_unknown(config):
    obs, contact, _ = scene()
    assert check(obs, contact, **config)["reason"] == "INVALID_COLLISION_INPUT"


@pytest.mark.parametrize("ik", [{"checked": True, "reachable": False}, {"checked": False, "reachable": None}])
def test_no_confirmed_ik_skips_all_depth_work(ik):
    result = check_suction_path(None, None, ik, None)
    assert result == {"status": "skipped", "reason": "NO_CONFIRMED_ANALYTIC_IK", "elapsed_ms": 0.0}
