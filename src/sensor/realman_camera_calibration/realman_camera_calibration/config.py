"""Configuration loading kept independent from ROS for easy unit testing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _validate_cameras(config: dict[str, Any]) -> dict[str, Any]:
    """Validate the camera inputs and return them (exactly one per arm)."""

    cameras = config.get("cameras")
    if not isinstance(cameras, dict) or not cameras:
        raise ValueError("camera calibration config must define at least one camera")
    camera_arm_ids: list[str] = []
    for camera_id, camera in cameras.items():
        if not isinstance(camera, dict):
            raise ValueError(f"camera '{camera_id}' must be a mapping")
        for key in ("image_topic", "camera_info_topic"):
            if not isinstance(camera.get(key), str) or not camera[key].strip():
                raise ValueError(f"camera '{camera_id}' requires a non-empty {key}")
        arm_id = camera.get("arm_id")
        if arm_id not in ("l", "r"):
            raise ValueError(f"camera '{camera_id}' arm_id must be l or r")
        for key in ("serial", "optical_frame", "mount_id"):
            if not isinstance(camera.get(key), str) or not camera[key].strip():
                raise ValueError(f"camera '{camera_id}' requires a non-empty {key}")
        camera_arm_ids.append(arm_id)
    if len(set(camera_arm_ids)) != len(camera_arm_ids):
        raise ValueError("cameras must map exactly one input to each configured arm")

    return cameras


def _validate_board(config: dict[str, Any]) -> None:
    """Validate the ChArUco board geometry."""

    board = config.get("board", {})
    if not isinstance(board, dict):
        raise ValueError("board must be a mapping")
    if board.get("type") != "charuco":
        raise ValueError("board.type must be charuco")
    if not isinstance(board.get("dictionary"), str):
        raise ValueError("board.dictionary must be a string")
    for key in ("squares_x", "squares_y"):
        if int(board.get(key, 0)) < 2:
            raise ValueError(f"board.{key} must be at least 2")
    for key in ("square_length_m", "marker_length_m"):
        if float(board.get(key, 0.0)) <= 0.0:
            raise ValueError(f"board.{key} must be positive")
    if float(board["marker_length_m"]) >= float(board["square_length_m"]):
        raise ValueError("board.marker_length_m must be smaller than square_length_m")


def _validate_robots(config: dict[str, Any], cameras: dict[str, Any]) -> None:
    """Validate per-arm frames and that each arm names a configured camera."""

    robots = config.get("robots")
    expected = {camera["arm_id"] for camera in cameras.values()}
    if not isinstance(robots, dict) or set(robots) != expected:
        raise ValueError("robots must define exactly the configured camera arms")
    for arm, robot in robots.items():
        if not isinstance(robot, dict):
            raise ValueError(f"robots.{arm} must be a mapping")
        for key in ("camera_id", "base_frame", "end_effector_frame"):
            if not isinstance(robot.get(key), str) or not robot[key].strip():
                raise ValueError(f"robots.{arm} requires a non-empty {key}")
        if robot["camera_id"] not in cameras:
            raise ValueError(f"robots.{arm}.camera_id must name a configured camera")
        if cameras[robot["camera_id"]]["arm_id"] != arm:
            raise ValueError(f"robots.{arm}.camera_id belongs to another arm")


def _validate_sampling(config: dict[str, Any]) -> None:
    """Validate capture timing and sample-count limits."""

    sampling = config.get("sampling", {})
    if not isinstance(sampling, dict):
        raise ValueError("sampling must be a mapping")
    if "required_arms" in sampling:
        raise ValueError("sampling.required_arms is obsolete; use arm_order and default_arm")
    order = sampling.get("arm_order")
    arms = set(config["robots"])
    if not isinstance(order, list) or len(order) != len(arms) or set(order) != arms:
        raise ValueError("sampling.arm_order must list each configured arm exactly once")
    if sampling.get("default_arm") not in arms:
        raise ValueError("sampling.default_arm must name a configured arm")
    for key in (
        "maximum_message_age_sec",
        "maximum_inter_camera_skew_sec",
        "maximum_timestamp_delay_sec",
        "tf_timeout_sec",
    ):
        if float(sampling.get(key, 0.0)) <= 0.0:
            raise ValueError(f"sampling.{key} must be positive")
    if int(sampling.get("minimum_samples_per_arm", 0)) < 3:
        raise ValueError("sampling.minimum_samples_per_arm must be at least 3")


def _validate_solver(config: dict[str, Any]) -> None:
    """Validate the hand-eye solver method and residual limits."""

    solver = config.get("solver", {})
    if not isinstance(solver, dict):
        raise ValueError("solver must be a mapping")
    if not isinstance(solver.get("hand_eye_method"), str) or not solver["hand_eye_method"]:
        raise ValueError("solver.hand_eye_method must be a non-empty string")
    for key in ("maximum_hand_eye_residual", "maximum_board_translation_spread_m", "maximum_board_rotation_spread_rad"):
        if float(solver.get(key, 0.0)) <= 0.0:
            raise ValueError(f"solver.{key} must be positive")


def load_config(path: str | Path) -> dict[str, Any]:
    """Load and validate the small, project-owned calibration YAML contract."""

    config_path = Path(path)
    with config_path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    if not isinstance(config, dict):
        raise ValueError("camera calibration config must contain a YAML mapping")

    cameras = _validate_cameras(config)
    _validate_board(config)
    _validate_robots(config, cameras)
    _validate_sampling(config)
    _validate_solver(config)
    return config
