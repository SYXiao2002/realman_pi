"""Bounded, JSON-safe review snapshots. No ROS clients or motion commands."""

import base64
import math

import cv2
import numpy as np

from .geometry import transform
from .gripper_collision import display_masks


# Keep the review payload close to the offline Plotly preview while staying
# bounded for the WebSocket/UI path. Inference receives the same workspace-cropped depth.
DISPLAY_CLOUD_MAX_POINTS = 35000


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


def _workspace_bounds(profile):
    try:
        bounds = np.asarray(profile.get("workspace_m"), dtype=float)
    except (TypeError, ValueError):
        return None
    if bounds.shape != (2, 3) or not np.isfinite(bounds).all() or np.any(bounds[0] >= bounds[1]):
        return None
    return bounds


def snapshot(observation, profile, request, *, response=None, plan=None, rejections=None,
             source="web_evaluation", phase="captured", detail="", collision_config=None):
    """Keep RGB, registered depth, cloud and candidates in one atomic event.

    Points are in rectified color optical coordinates (+X right,+Y down,+Z
    forward). The depth was cropped to the profile workspace before inference;
    only this display cloud is capped to DISPLAY_CLOUD_MAX_POINTS points.
    """
    color, depth, K = (np.asarray(observation[key]) for key in ("color_bgr", "depth_m", "K"))
    valid = np.isfinite(depth) & (depth > 0)
    values = depth[valid]
    low, high = (float(values.min()), float(values.max())) if len(values) else (0., 0.)
    display = np.zeros(depth.shape, np.uint8)
    if len(values):
        display[valid] = 1 + np.clip((values-low)/max(high-low, 1e-6)*254, 0, 254).astype(np.uint8)
    # Match offline preview's scene cloud: the original worker's valid z range.
    # This only affects display, never the frozen RGB-D sent to inference.
    z_range = (response or {}).get("diagnostics", {}).get("z_range_m")
    cloud_valid = valid
    if z_range is not None:
        bounds = np.asarray(z_range, dtype=float)
        if bounds.shape != (2,) or not np.isfinite(bounds).all() or bounds[0] >= bounds[1]:
            raise ValueError("invalid display z range")
        cloud_valid = valid & (depth > bounds[0]) & (depth < bounds[1])
    yy, xx = np.nonzero(cloud_valid)
    zz = depth[yy, xx]
    pixels = np.column_stack((xx, yy, np.ones(len(xx))))
    rays = pixels @ np.linalg.inv(K).T
    points = rays * (zz / rays[:, 2])[:, None]
    base_camera_value = observation.get("T_base_camera")
    base_camera = transform(base_camera_value) if base_camera_value is not None else None
    workspace = _workspace_bounds(profile)
    workspace_crop_status = "applied"
    if workspace is None:
        workspace_crop_status = "invalid_workspace"
        points, yy, xx = points[:0], yy[:0], xx[:0]
    elif base_camera is None:
        workspace_crop_status = "missing_base_camera"
        points, yy, xx = points[:0], yy[:0], xx[:0]
    else:
        base_points = points @ base_camera[:3, :3].T + base_camera[:3, 3]
        workspace_tolerance = np.finfo(depth.dtype).eps * max(1.0, np.max(np.abs(workspace)))
        inside = np.all((base_points >= workspace[0] - workspace_tolerance)
                        & (base_points <= workspace[1] + workspace_tolerance), axis=1)
        points, yy, xx = points[inside], yy[inside], xx[inside]
    workspace_point_count = len(points)
    if len(points) > DISPLAY_CLOUD_MAX_POINTS:
        sample = np.linspace(0, len(points) - 1, DISPLAY_CLOUD_MAX_POINTS, dtype=np.int64)
        points, yy, xx = points[sample], yy[sample], xx[sample]
    zz = depth[yy, xx]
    diagnostics = dict((response or {}).get("diagnostics", {}))
    segmentation = diagnostics.pop("segmap_png", None)
    segmentation_labels = None
    if segmentation is not None:
        labels = cv2.imdecode(np.frombuffer(segmentation, np.uint8), cv2.IMREAD_UNCHANGED)
        if labels is not None and labels.shape == depth.shape and np.issubdtype(labels.dtype, np.integer):
            segmentation_labels = labels
    rejected = {item["candidate_id"]: item["reason"] for item in (rejections or [])}
    selected_id = (plan or {}).get("candidate_id")
    candidates = []
    for candidate in (response or {}).get("candidates", []):
        item = {**candidate}
        for suffix in ("", "_before_alignment", "_after_alignment"):
            camera_pose = candidate.get("T_camera_grasp" + suffix)
            item["T_base_grasp" + suffix] = (base_camera @ transform(camera_pose)
                if base_camera is not None and camera_pose is not None else None)
        item["planning_status"] = ("selected" if item.get("candidate_id") == selected_id
                                   else "rejected" if item.get("candidate_id") in rejected
                                   else "not_checked")
        item["planning_reason"] = rejected.get(item.get("candidate_id"))
        collision = item.get("collision")
        if collision is not None:
            display_labels = segmentation_labels[yy, xx] if segmentation_labels is not None else None
            item["collision"] = {**collision,
                                  "display": display_masks(item, collision, points, display_labels, base_camera,
                                                           collision_config)}
        candidates.append(item)
    instances = None
    if segmentation is not None and segmentation_labels is not None:
        labels = segmentation_labels
        colored = cv2.applyColorMap(((labels.astype(np.uint32) * 47) % 256).astype(np.uint8), cv2.COLORMAP_TURBO)
        instances = color.copy()
        instances[labels > 0] = cv2.addWeighted(color, .5, colored, .5, 0)[labels > 0]
    return json_value({
        "type": "grasp_evaluation", "schema_version": 1, "read_only": True,
        "observation_id": request["observation_id"], "capture_timestamp_ns": str(request["capture_timestamp_ns"]),
        "review_request_id": request["task_id"], "profile_id": request["config_id"], "profile_name": profile["name"], "arm": profile["arm"],
        "camera_frame": request["camera_frame"], "base_frame": profile["base_frame"], "tool_name": profile["tool_name"],
        "target_label": request["target"]["class_name"], "backend": request["backend"],
        "cgn_context": request.get("cgn_context", "mask"),
        "model_id": (response or {}).get("model_id"), "gripper": profile["gripper_geometry"],
        "calibration_id": profile["calibration_data"].get("calibration_id"),
        "readiness_problems": profile["readiness_problems"], "execution_ready": False,
        "phase": phase, "detail": detail, "K": K, "T_base_camera": base_camera,
        "base_camera_transform": observation.get("base_camera_transform"),
        "seed_joint_degrees": observation["seed"], "width": color.shape[1], "height": color.shape[0],
        "images": ({} if source == "behavior_tree" else
                   {"rgb": image_url(color), "depth": image_url(display, ".png"),
                    "overlay": image_url(observation.get("overlay_bgr", color)),
                    **({"instances": image_url(instances)} if instances is not None else {})}),
        "depth_stats": {"valid_pixels": int(valid.sum()), "total_pixels": int(depth.size), "min_m": low, "max_m": high,
                        "valid_pixels_before_workspace_crop": observation.get("depth_valid_pixels_before_crop"),
                        "valid_pixels_after_workspace_crop": observation.get("depth_valid_pixels_after_crop",
                                                                             int(valid.sum()))},
        "cloud": {"xyz_m": points, "rgb": color[yy, xx, ::-1], "z_range_m": z_range,
                  "workspace_crop_status": workspace_crop_status,
                  "workspace_point_count": workspace_point_count,
                  "displayed_point_count": len(points)},
        "candidates": candidates, "plan": plan, "planning_rejections": rejections or [],
        "workspace_m": profile["workspace_m"],
        "collision_workspace_m": profile.get("collision_workspace_m"), "source": source,
        "server_diagnostics": diagnostics,
    })
