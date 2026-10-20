"""Bounded, JSON-safe review snapshots. No ROS clients or motion commands."""

import base64
import math

import cv2
import numpy as np

from .geometry import transform


def json_value(value):
    if isinstance(value, np.ndarray):
        return json_value(value.tolist())
    if isinstance(value, np.generic):
        return json_value(value.item())
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite evaluation value")
    return value


def image_url(array, extension=".jpg"):
    ok, encoded = cv2.imencode(extension, array)
    if not ok:
        raise ValueError("cannot encode review image")
    mime = "png" if extension == ".png" else "jpeg"
    return f"data:image/{mime};base64," + base64.b64encode(encoded).decode("ascii")


def snapshot(observation, profile, request, *, response=None, plan=None, phase="captured", detail=""):
    """Keep RGB, registered depth, cloud and candidates in one atomic event.

    Points are in rectified color optical coordinates (+X right,+Y down,+Z
    forward). The full depth used by inference is never downsampled; only this
    display cloud is capped to 4096 points.
    """
    color, depth, K = (np.asarray(observation[key]) for key in ("color_bgr", "depth_m", "K"))
    valid = np.isfinite(depth) & (depth > 0)
    values = depth[valid]
    low, high = (float(values.min()), float(values.max())) if len(values) else (0., 0.)
    display = np.zeros(depth.shape, np.uint8)
    if len(values):
        display[valid] = 1 + np.clip((values-low)/max(high-low, 1e-6)*254, 0, 254).astype(np.uint8)
    yy, xx = np.nonzero(valid)
    stride = max(1, math.ceil(len(xx)/4096))
    yy, xx = yy[::stride], xx[::stride]
    zz = depth[yy, xx]
    points = np.column_stack(((xx-K[0, 2])*zz/K[0, 0], (yy-K[1, 2])*zz/K[1, 1], zz))
    base_camera_value = observation.get("T_base_camera")
    base_camera = transform(base_camera_value) if base_camera_value is not None else None
    candidates = []
    for candidate in (response or {}).get("candidates", []):
        candidates.append({**candidate, "T_base_grasp": (
            base_camera @ transform(candidate["T_camera_grasp"]) if base_camera is not None else None)})
    return json_value({
        "type": "grasp_evaluation", "schema_version": 1, "read_only": True,
        "observation_id": request["observation_id"], "capture_timestamp_ns": str(request["capture_timestamp_ns"]),
        "review_request_id": request["task_id"], "profile_id": request["config_id"], "profile_name": profile["name"], "arm": profile["arm"],
        "camera_frame": request["camera_frame"], "base_frame": profile["base_frame"], "tool_name": profile["tool_name"],
        "target_label": request["target"]["class_name"], "backend": request["backend"],
        "model_id": (response or {}).get("model_id"), "gripper": profile["gripper_geometry"],
        "calibration_id": profile["calibration_data"].get("calibration_id"),
        "readiness_problems": profile["readiness_problems"], "execution_ready": False,
        "phase": phase, "detail": detail, "K": K, "T_base_camera": base_camera,
        "seed_joint_degrees": observation["seed"], "width": color.shape[1], "height": color.shape[0],
        "images": {"rgb": image_url(color), "depth": image_url(display, ".png"),
                   "overlay": image_url(observation.get("overlay_bgr", color))},
        "depth_stats": {"valid_pixels": int(valid.sum()), "total_pixels": int(depth.size), "min_m": low, "max_m": high},
        "cloud": {"xyz_m": points, "rgb": color[yy, xx, ::-1]},
        "candidates": candidates, "plan": plan, "server_diagnostics": (response or {}).get("diagnostics", {}),
    })
