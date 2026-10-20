"""Evaluation snapshots preserve optical geometry without inventing calibration."""

from pathlib import Path
import sys

import numpy as np

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
    result = snapshot(observation(None), profile(), request(), response={"candidates": [candidate]})
    assert result["T_base_camera"] is None
    assert result["candidates"][0]["T_base_grasp"] is None
