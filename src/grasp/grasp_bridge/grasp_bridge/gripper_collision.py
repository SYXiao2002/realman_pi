"""ROS-free collision evaluation for the visual two-finger grasp model."""

from collections import deque
import math
import re

import cv2
import numpy as np

from .geometry import transform


STAGES = ("pregrasp", "approach_sweep", "grasp", "lift")
DEFAULT_COLLISION_CONFIG = {
    "enabled": True,
    "warning_clearance_m": 0.003,
    "collision_penetration_m": 0.002,
    "sweep_step_m": 0.005,
    "cluster_voxel_m": 0.003,
    "minimum_local_points": 20,
    "minimum_collision_points": 3,
    "blocking_statuses": ["collision", "warning", "unknown"],
}
_STATUSES = ("safe", "warning", "collision", "unknown")
_ID_RE = re.compile(r"(?:0|[1-9][0-9]*)$")


def validate_collision_config(config=None):
    """Validate and normalize evaluation thresholds from grasp_bridge.yaml."""
    value = dict(DEFAULT_COLLISION_CONFIG)
    value.update(config or {})
    if type(value.get("enabled")) is not bool:
        raise ValueError("collision.enabled must be a boolean")
    for key in ("warning_clearance_m", "collision_penetration_m", "sweep_step_m", "cluster_voxel_m"):
        number = value.get(key)
        if type(number) not in (int, float) or not math.isfinite(number) or number <= 0:
            raise ValueError(f"collision.{key} must be finite and positive")
    if value["collision_penetration_m"] > value["warning_clearance_m"]:
        raise ValueError("collision penetration threshold must not exceed warning clearance")
    for key in ("minimum_local_points", "minimum_collision_points"):
        number = value.get(key)
        if type(number) is not int or number < 1:
            raise ValueError(f"collision.{key} must be a positive integer")
    statuses = value.get("blocking_statuses")
    if (not isinstance(statuses, list) or not statuses
            or any(status not in _STATUSES for status in statuses)):
        raise ValueError("collision.blocking_statuses must contain valid statuses")
    value["blocking_statuses"] = list(dict.fromkeys(statuses))
    return value


def _finite_positive(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return float(value)


def finger_boxes(candidate, gripper):
    """Create the sole canonical ``visual_finger_boxes/v1`` geometry."""
    if not isinstance(candidate, dict) or not isinstance(gripper, dict):
        raise ValueError("candidate and gripper must be mappings")
    width = _finite_positive(candidate.get("width_m"), "candidate.width_m")
    thickness = _finite_positive(gripper.get("finger_thickness_m"), "finger_thickness_m")
    depth = _finite_positive(gripper.get("finger_depth_m"), "finger_depth_m")
    finger_width = gripper.get("finger_width_m")
    if type(finger_width) not in (int, float) or not math.isfinite(finger_width) or finger_width <= 0:
        # This is intentionally the same square-section fallback as the old Web renderer.
        finger_width = thickness
    clearance = gripper.get("clearance_m", 0.0)
    if type(clearance) not in (int, float) or not math.isfinite(clearance) or clearance < 0:
        raise ValueError("clearance_m must be finite and non-negative")
    minimum = gripper.get("min_opening_m", 0.0)
    maximum = gripper.get("max_opening_m")
    if type(minimum) not in (int, float) or not math.isfinite(minimum) or minimum < 0:
        raise ValueError("min_opening_m must be finite and non-negative")
    if type(maximum) not in (int, float) or not math.isfinite(maximum) or maximum <= 0 or maximum < minimum:
        raise ValueError("max_opening_m must be finite and greater than min_opening_m")
    opening = min(max(width + 2.0 * float(clearance), float(minimum)), float(maximum))
    centers = [-(opening / 2.0 + thickness / 2.0), opening / 2.0 + thickness / 2.0]
    return {
        "model": "visual_finger_boxes/v1",
        "frame": "grasp_contact",
        "opening_m": float(opening),
        "boxes": [
            {"name": "left_finger", "center_m": [float(centers[0]), 0.0, -depth / 2.0],
             "size_m": [thickness, float(finger_width), depth]},
            {"name": "right_finger", "center_m": [float(centers[1]), 0.0, -depth / 2.0],
             "size_m": [thickness, float(finger_width), depth]},
        ],
    }


def _box_sdf(points, box):
    center = np.asarray(box["center_m"], dtype=float)
    half = np.asarray(box["size_m"], dtype=float) / 2.0
    q = np.abs(points - center) - half
    outside = np.linalg.norm(np.maximum(q, 0.0), axis=1)
    inside = np.minimum(np.max(q, axis=1), 0.0)
    return outside + inside


def signed_distance(points, geometry):
    """Return the SDF to the closest canonical finger box for each local point."""
    points = np.asarray(points, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("points must be an Nx3 array")
    if not geometry or len(points) == 0:
        return np.empty(len(points), dtype=float)
    return np.min(np.column_stack([_box_sdf(points, box) for box in geometry["boxes"]]), axis=1)


def _expanded_box_mask(points, pose, geometry, margin):
    """Return points in one pose's axis-aligned boxes expanded by ``margin``."""
    return _local_box_mask(_transform_points(points, pose), geometry, margin)


def _local_box_mask(local, geometry, margin):
    mask = np.zeros(len(local), dtype=bool)
    for box in geometry["boxes"]:
        center = np.asarray(box["center_m"], dtype=float)
        half = np.asarray(box["size_m"], dtype=float) / 2.0 + margin
        mask |= np.all(np.abs(local - center) <= half, axis=1)
    return mask


def _stage_poses(grasp, profile, config):
    grasp = transform(grasp)
    approach = _finite_positive(profile.get("approach_m"), "approach_m")
    lift = _finite_positive(profile.get("lift_m"), "lift_m")
    step = _finite_positive(config["sweep_step_m"], "sweep_step_m")
    pre = grasp.copy()
    pre[:3, 3] -= grasp[:3, 2] * approach
    raised = grasp.copy()
    raised[:3, 3] += np.array([0.0, 0.0, lift])
    count = max(2, int(math.ceil(approach / step)) + 1)
    sweep = []
    for ratio in np.linspace(0.0, 1.0, count):
        pose = pre.copy()
        pose[:3, 3] = pre[:3, 3] + ratio * (grasp[:3, 3] - pre[:3, 3])
        sweep.append(pose)
    return {"pregrasp": [pre], "approach_sweep": sweep, "grasp": [grasp], "lift": [raised]}


def _transform_points(points, pose):
    return (points - pose[:3, 3]) @ pose[:3, :3]


def _box_corners(box, margin=0.0):
    center = np.asarray(box["center_m"], dtype=float)
    half = np.asarray(box["size_m"], dtype=float) / 2.0 + margin
    signs = np.array([
        [-1, -1, -1], [-1, -1, 1], [-1, 1, -1], [-1, 1, 1],
        [1, -1, -1], [1, -1, 1], [1, 1, -1], [1, 1, 1],
    ], dtype=float)
    return center + signs * half


def _broadphase_mask(points, poses, geometry, margin):
    """Keep all local evidence and a point attaining the stage's exact minimum.

    A nearby sample gives an upper bound on the minimum SDF. Inflating by that
    bound also retains the nearest surface when every point is outside the
    warning envelope, preserving clearance metrics as well as status/counts.
    """
    if len(points) == 0:
        return np.zeros(0, dtype=bool)
    corners = np.concatenate([_box_corners(box) for box in geometry["boxes"]], axis=0)
    world = np.concatenate([corners @ pose[:3, :3].T + pose[:3, 3] for pose in poses], axis=0)
    lower, upper = np.min(world, axis=0), np.max(world, axis=0)
    delta = np.maximum(np.maximum(lower - points, points - upper), 0.0)
    sample = points[np.argmin(np.einsum("ij,ij->i", delta, delta))][None, :]
    bound = min(float(signed_distance(_transform_points(sample, pose), geometry)[0]) for pose in poses)
    # Expand in the box's local axes before rotating. Adding only `margin` to
    # world bounds would lose corners of rotated local-evidence boxes.
    corners = np.concatenate([_box_corners(box, max(margin, bound)) for box in geometry["boxes"]], axis=0)
    world = np.concatenate([corners @ pose[:3, :3].T + pose[:3, 3] for pose in poses], axis=0)
    tolerance = 1e-12 * max(1.0, float(np.max(np.abs(world))))
    lower = np.min(world, axis=0) - tolerance
    upper = np.max(world, axis=0) + tolerance
    return np.all((points >= lower) & (points <= upper), axis=1)


def _confirmed_clusters(points, indices, voxel, minimum):
    if len(indices) == 0:
        return 0
    cells = np.floor(np.asarray(points)[indices] / voxel).astype(np.int64)
    buckets = {}
    for index, cell in zip(indices, cells):
        buckets.setdefault(tuple(cell), []).append(int(index))
    seen = set(); confirmed = 0
    for cell in buckets:
        if cell in seen:
            continue
        queue = deque([cell]); seen.add(cell); count = 0
        while queue:
            current = queue.popleft(); count += len(buckets[current])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        neighbor = (current[0] + dx, current[1] + dy, current[2] + dz)
                        if neighbor in buckets and neighbor not in seen:
                            seen.add(neighbor); queue.append(neighbor)
        confirmed += count >= minimum
    return confirmed


def _metric(status, minimum, maximum, collision, near, local, clusters):
    return {"status": status, "min_clearance_m": minimum, "max_penetration_m": maximum,
            "collision_points": int(collision), "near_points": int(near),
            "local_environment_points": int(local), "collision_clusters": int(clusters)}


def _unknown_stage():
    return _metric("unknown", None, None, 0, 0, 0, 0)


def evaluate_candidate_collision(candidate, base_points, environment_mask, profile, config):
    """Evaluate one adjusted candidate against base-frame environment points."""
    config = validate_collision_config(config)
    try:
        geometry = finger_boxes(candidate, profile["gripper_geometry"])
        grasp = transform(candidate["T_base_grasp"])
    except (KeyError, TypeError, ValueError) as exc:
        return {"status": "unknown", "geometry": None, "worst_stage": None,
                "min_clearance_m": None, "max_penetration_m": None,
                "collision_points": 0, "near_points": 0, "local_environment_points": 0,
                "collision_clusters": 0, "stages": {stage: _unknown_stage() for stage in STAGES},
                "thresholds": config, "blocking": "unknown" in config["blocking_statuses"],
                "reason": str(exc)}
    result = {"status": "unknown", "geometry": geometry, "worst_stage": None,
              "min_clearance_m": None, "max_penetration_m": None,
              "collision_points": 0, "near_points": 0, "local_environment_points": 0,
              "collision_clusters": 0, "stages": {}, "thresholds": config,
              "approach_m": float(profile["approach_m"]), "lift_m": float(profile["lift_m"]),
              "blocking": "unknown" in config["blocking_statuses"], "reason": None}
    if not config["enabled"]:
        result["stages"] = {stage: _unknown_stage() for stage in STAGES}
        result["reason"] = "collision evaluation is disabled"
        return result
    points = np.asarray(base_points, dtype=float)
    env = np.asarray(environment_mask, dtype=bool)
    if points.ndim != 2 or points.shape[1] != 3 or env.shape != (len(points),) or not env.any():
        result["stages"] = {stage: _unknown_stage() for stage in STAGES}
        result["reason"] = "environment point cloud is unavailable"
        return result
    try:
        poses = _stage_poses(grasp, profile, config)
    except (KeyError, TypeError, ValueError) as exc:
        result["stages"] = {stage: _unknown_stage() for stage in STAGES}
        result["reason"] = str(exc)
        return result
    environment = points if env.all() else points[env]
    # The finger boxes occupy a tiny fraction of the workspace. Their swept
    # world-frame AABB is a conservative broad phase: every point that can be
    # inside the local evidence envelope and each stage's minimum survives;
    # distant points no longer pay for repeated SDF evaluations.
    broadphase = np.zeros(len(environment), dtype=bool)
    for stage_poses in poses.values():
        broadphase |= _broadphase_mask(environment, stage_poses, geometry, config["warning_clearance_m"])
    environment = environment[broadphase]
    collision_union = set(); near_union = set(); local_union = set()
    all_signed = []
    for stage in STAGES:
        stage_signed = []
        stage_local = np.zeros(len(environment), dtype=bool)
        for pose in poses[stage]:
            local_points = _transform_points(environment, pose)
            stage_signed.append(signed_distance(local_points, geometry))
            stage_local |= _local_box_mask(local_points, geometry, config["warning_clearance_m"])
        distances = np.min(np.column_stack(stage_signed), axis=1)
        all_signed.append(distances)
        local = stage_local
        collision = distances <= -config["collision_penetration_m"]
        near = (distances < config["warning_clearance_m"]) & ~collision
        local_indices = np.flatnonzero(local)
        collision_indices = np.flatnonzero(collision)
        near_indices = np.flatnonzero(near)
        local_union.update(local_indices.tolist()); collision_union.update(collision_indices.tolist()); near_union.update(near_indices.tolist())
        minimum = float(np.min(distances)) if len(distances) else None
        maximum = float(max(0.0, -minimum)) if minimum is not None else None
        clusters = _confirmed_clusters(environment, collision_indices, config["cluster_voxel_m"], config["minimum_collision_points"])
        if clusters:
            status = "collision"
        elif len(local_indices) < config["minimum_local_points"]:
            status = "unknown"
        elif minimum is not None and minimum < config["warning_clearance_m"]:
            status = "warning"
        else:
            status = "safe"
        result["stages"][stage] = _metric(status, minimum, maximum, len(collision_indices), len(near_indices),
                                           len(local_indices), clusters)
    all_distances = np.min(np.column_stack(all_signed), axis=0)
    collision_indices = np.array(sorted(collision_union), dtype=int)
    # A point that is near during one stage and colliding during another is
    # reported as collision everywhere, matching the display mask priority.
    near_indices = np.array(sorted(near_union - collision_union), dtype=int)
    local_indices = np.array(sorted(local_union), dtype=int)
    minimum = float(np.min(all_distances)) if len(all_distances) else None
    maximum = float(max(0.0, -minimum)) if minimum is not None else None
    clusters = _confirmed_clusters(environment, collision_indices, config["cluster_voxel_m"], config["minimum_collision_points"])
    if clusters:
        status = "collision"
    elif len(local_indices) < config["minimum_local_points"]:
        status = "unknown"
    elif minimum is not None and minimum < config["warning_clearance_m"]:
        status = "warning"
    else:
        status = "safe"
    result.update(status=status, min_clearance_m=minimum, max_penetration_m=maximum,
                  collision_points=len(collision_indices), near_points=len(near_indices),
                  local_environment_points=len(local_indices), collision_clusters=clusters)
    result["blocking"] = status in config["blocking_statuses"]
    rank = {"safe": 0, "warning": 1, "unknown": 2, "collision": 3}
    result["worst_stage"] = max(STAGES, key=lambda stage: (rank[result["stages"][stage]["status"]],
                                                            -(result["stages"][stage]["min_clearance_m"] or -float("inf"))))
    return result


def _object_id(value):
    value = str(value) if isinstance(value, (str, int, np.integer)) else ""
    if not _ID_RE.fullmatch(value):
        raise ValueError("candidate object_id must be a canonical non-negative integer")
    return int(value)


def _decode_labels(response, shape):
    encoded = (response or {}).get("diagnostics", {}).get("segmap_png")
    if encoded is None:
        raise ValueError("instance segmentation is missing")
    labels = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_UNCHANGED)
    if labels is None or labels.shape != shape or not np.issubdtype(labels.dtype, np.integer):
        raise ValueError("instance segmentation dimensions or type are invalid")
    return labels


def _point_cloud(observation, profile):
    """Environment points for collision, from the wider collision region.

    The TCP corridor (``workspace_m``) is deliberately tight, but the gripper
    fingers reach beyond the TCP point, so obstacles just outside it (e.g. bin
    walls) must still feed collision detection. Callers therefore pass a wider
    ``collision_depth_m``/``collision_workspace_m``; both fall back to the tight
    workspace crop when no separate collision region is configured.
    """
    depth = np.asarray(observation.get("collision_depth_m",
                                       observation.get("depth_m")), dtype=float)
    K = np.asarray(observation.get("K"), dtype=float)
    base = observation.get("T_base_camera")
    if depth.ndim != 2 or K.shape != (3, 3) or not np.isfinite(K).all() or base is None:
        raise ValueError("depth, intrinsics, and T_base_camera are required")
    base = transform(base)
    valid = np.isfinite(depth) & (depth > 0)
    yy, xx = np.nonzero(valid)
    pixels = np.column_stack((xx, yy, np.ones(len(xx))))
    rays = pixels @ np.linalg.inv(K).T
    camera = rays * (depth[yy, xx] / rays[:, 2])[:, None]
    base_points = camera @ base[:3, :3].T + base[:3, 3]
    region = profile.get("collision_workspace_m")
    bounds = np.asarray(region if region is not None else profile.get("workspace_m"), dtype=float)
    if bounds.shape != (2, 3) or not np.isfinite(bounds).all() or np.any(bounds[0] >= bounds[1]):
        raise ValueError("collision workspace is invalid")
    inside = np.all((base_points >= bounds[0]) & (base_points <= bounds[1]), axis=1)
    return camera[inside], base_points[inside], yy[inside], xx[inside]


def annotate_candidates(response, observation, profile, config):
    """Attach collision results to all candidates, preserving inference order."""
    config = validate_collision_config(config)
    adjusted = dict(response or {})
    candidates = [dict(item) for item in adjusted.get("candidates", [])]
    try:
        labels = _decode_labels(response, np.asarray(observation["depth_m"]).shape)
        camera, base, yy, xx = _point_cloud(observation, profile)
        labels = labels[yy, xx]
    except (KeyError, TypeError, ValueError, np.linalg.LinAlgError):
        labels = None; camera = base = np.empty((0, 3), dtype=float)
    base_camera = observation.get("T_base_camera")
    environment_cache = {}
    for index, candidate in enumerate(candidates):
        try:
            object_id = _object_id(candidate.get("object_id"))
            if labels is None:
                raise ValueError("instance segmentation or capture transform unavailable")
            # Candidates from one segmented object share the same environment
            # cloud. Cache that extraction once instead of copying the entire
            # collision cloud for every candidate from the object.
            if object_id not in environment_cache:
                target_mask = labels == object_id
                if not np.any(target_mask):
                    raise ValueError("target instance has no valid depth points")
                environment_cache[object_id] = base[~target_mask]
            environment = environment_cache[object_id]
            item = dict(candidate)
            item["T_base_grasp"] = transform(base_camera) @ transform(candidate["T_camera_grasp"])
            item["collision"] = evaluate_candidate_collision(
                item, environment, np.ones(len(environment), dtype=bool), profile, config)
        except (TypeError, ValueError, KeyError, np.linalg.LinAlgError) as exc:
            geometry = None
            try:
                geometry = finger_boxes(candidate, profile["gripper_geometry"])
            except (TypeError, ValueError, KeyError):
                pass
            item = dict(candidate)
            item["collision"] = {"status": "unknown", "geometry": geometry, "worst_stage": None,
                                  "min_clearance_m": None, "max_penetration_m": None,
                                  "collision_points": 0, "near_points": 0, "local_environment_points": 0,
                                  "collision_clusters": 0, "stages": {stage: _unknown_stage() for stage in STAGES},
                                  "thresholds": config,
                                  "approach_m": profile.get("approach_m"), "lift_m": profile.get("lift_m"),
                                  "blocking": "unknown" in config["blocking_statuses"], "reason": str(exc)}
        candidates[index] = item
    adjusted["candidates"] = candidates
    return adjusted


def display_masks(candidate, collision, points_camera, labels, T_base_camera, config=None):
    """Return near/collision masks for the already bounded Web display cloud."""
    count = len(points_camera)
    empty = {"near_mask": [0] * count, "collision_mask": [0] * count}
    if not collision or collision.get("geometry") is None or T_base_camera is None or labels is None:
        return empty
    try:
        object_id = _object_id(candidate.get("object_id"))
        labels = np.asarray(labels)
        base = transform(T_base_camera)
        config = validate_collision_config(collision.get("thresholds", config))
        if not config["enabled"] or not np.any(labels == object_id):
            return empty
        points = np.asarray(points_camera, dtype=float) @ base[:3, :3].T + base[:3, 3]
        environment = labels != object_id
        grasp = transform(base) @ transform(candidate["T_camera_grasp"])
        poses = _stage_poses(grasp, {"approach_m": collision.get("approach_m"),
                                     "lift_m": collision.get("lift_m")}, config)
        selected = np.flatnonzero(environment & _broadphase_mask(
            points, [pose for stage in STAGES for pose in poses[stage]],
            collision["geometry"], config["warning_clearance_m"]))
        local_points = points[selected]
        # Prefer the exact stage distances used by the evaluator when available.
        near = np.zeros(len(selected), bool); hit = np.zeros(len(selected), bool)
        for stage in STAGES:
            distances = np.min(np.column_stack([
                signed_distance(_transform_points(local_points, pose), collision["geometry"])
                for pose in poses[stage]]), axis=1)
            hit |= distances <= -config["collision_penetration_m"]
            near |= (distances < config["warning_clearance_m"]) & ~hit
        near &= ~hit
        near_mask = np.zeros(count, dtype=int); collision_mask = np.zeros(count, dtype=int)
        near_mask[selected] = near; collision_mask[selected] = hit
        return {"near_mask": near_mask.tolist(), "collision_mask": collision_mask.tolist()}
    except (TypeError, ValueError, KeyError, np.linalg.LinAlgError):
        return empty
