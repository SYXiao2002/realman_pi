"""Evaluation snapshots preserve optical geometry without inventing calibration."""

from pathlib import Path
import sys

import numpy as np
import cv2
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grasp_bridge.evaluation import snapshot


def observation(base_camera):
    return {"color_bgr": np.full((8, 8, 3), (1, 2, 3), np.uint8),
            "depth_m": np.full((8, 8), 0.5, np.float32),
            "K": np.array([[10., 0., 4.], [0., 10., 4.], [0., 0., 1.]]),
            "T_base_camera": base_camera, "seed": [0.] * 6,
            "capture_timestamp_ns": 10, "source_header": None}


def profile():
    return {"name": "right_wrist", "arm": "r", "base_frame": "r/base_link", "tool_name": "tcp",
            "gripper_geometry": {"verified": False}, "calibration_data": {"calibration_id": None},
            "readiness_problems": ["verified calibration identity", "measured gripper geometry"]}


def request():
    return {"observation_id": "obs-1", "capture_timestamp_ns": 10, "task_id": "review-1",
            "config_id": "profile-hash", "camera_frame": "camera_color_optical_frame",
            "target": {"class_name": "eu"}, "backend": "geometry"}


def test_snapshot_keeps_registered_rgbd_and_full_base_pose():
    base_camera = np.eye(4); base_camera[:3, 3] = [1., 2., 3.]
    candidate = {"candidate_id": "c1", "T_camera_grasp": np.eye(4).tolist(), "score": .8}
    result = snapshot(observation(base_camera), profile(), request(), response={"candidates": [candidate]}, phase="inferred")
    assert result["read_only"] and not result["execution_ready"]
    assert len(result["cloud"]["xyz_m"]) == 64
    assert result["cloud"]["xyz_m"][0] == [-.2, -.2, .5]
    assert result["candidates"][0]["T_base_grasp"][0][3] == 1.
    assert result["images"]["rgb"].startswith("data:image/jpeg;base64,")


def test_snapshot_does_not_fabricate_unavailable_base_transform():
    candidate = {"candidate_id": "c1", "T_camera_grasp": np.eye(4).tolist()}
    obs = observation(None)
    obs["base_camera_transform"] = {"status": "missing_calibration", "detail": "import saved calibration"}
    result = snapshot(obs, profile(), request(), response={"candidates": [candidate]})
    assert result["T_base_camera"] is None
    assert result["candidates"][0]["T_base_grasp"] is None
    assert result["base_camera_transform"] == obs["base_camera_transform"]


def test_lossless_instance_labels_become_review_image_without_binary_in_json():
    labels = np.zeros((8, 8), np.uint16)
    labels[2:6, 2:6] = 1
    ok, encoded = cv2.imencode(".png", labels)
    assert ok
    response = {"candidates": [], "diagnostics": {"segmap_png": encoded.tobytes(), "archive_id": "frozen"}}
    result = snapshot(observation(None), profile(), request(), response=response)
    assert result["images"]["instances"].startswith("data:image/jpeg;base64,")
    assert "segmap_png" not in result["server_diagnostics"]
    assert response["diagnostics"]["segmap_png"] == encoded.tobytes()
    assert '"archive_id": "frozen"' in json.dumps(result)


def test_snapshot_keeps_both_alignment_stages_in_same_capture():
    Tbc = np.eye(4); Tbc[:3, 3] = [1., 2., 3.]
    before = np.eye(4); before[2, 3] = .5
    after = before.copy(); after[:3, :3] = np.diag([1., -1., -1.])
    candidate = dict(candidate_id="c1", T_camera_grasp=after,
                     T_camera_grasp_before_alignment=before, T_camera_grasp_after_alignment=after)
    result = snapshot(observation(Tbc), profile(), request(), response={"candidates": [candidate]})
    out, = result["candidates"]
    np.testing.assert_allclose(out["T_base_grasp_before_alignment"], Tbc @ before)
    np.testing.assert_allclose(out["T_base_grasp_after_alignment"], Tbc @ after)
    np.testing.assert_allclose(out["T_base_grasp"], Tbc @ after)
    assert result["observation_id"] == "obs-1"


def test_display_cloud_matches_original_worker_z_range_without_changing_depth():
    obs = observation(None)
    obs["depth_m"][0, :4] = [.1, .2, 1.5, 2.]
    before = obs["depth_m"].copy()
    result = snapshot(obs, profile(), request(), response={"diagnostics": {"z_range_m": [.2, 1.5]}})
    assert len(result["cloud"]["xyz_m"]) == 60
    assert result["cloud"]["z_range_m"] == [.2, 1.5]
    assert result["depth_stats"]["valid_pixels"] == 64
    np.testing.assert_array_equal(before, obs["depth_m"])
