"""Metric top-surface grasp candidates in a support-plane basis.

Adapted from the reference worker's top-slice/PCA/multi-axis approach. All
physical operations use 3D directions, rather than camera XY pixel widths.
The legacy 90-degree yaw and 90-mm wrist offsets are intentionally absent:
this module emits the defined closing center and closing axis directly.
"""

import math

import numpy as np


def plane_basis(up):
    up = np.asarray(up, dtype=np.float64)
    up = up / np.linalg.norm(up)
    seed = np.eye(3)[int(np.argmin(np.abs(up)))]
    x = seed - np.dot(seed, up) * up
    x /= np.linalg.norm(x)
    return x, np.cross(up, x), up


def pointcloud(depth, K):
    y, x = np.indices(depth.shape)
    return np.stack(((x - K[0, 2]) * depth / K[0, 0],
                     (y - K[1, 2]) * depth / K[1, 1], depth), axis=-1)


def geometric_candidates(request, mask, *, object_id, class_name, policy, scene_mask=None):
    """Return contact-center candidates; empty is a valid geometric outcome.

    This is a bounded top-down heuristic, not an arm or full-scene collision
    planner. The bridge must check whole-arm paths and measured tool geometry.
    """
    up = np.asarray(request["up_camera"], dtype=np.float64)
    max_tilt = float(policy.get("max_view_tilt_deg", 25))
    tilt = math.degrees(math.acos(float(np.clip(np.dot(up, [0, 0, -1]), -1, 1))))
    if tilt > max_tilt:
        return [], {"reason": "unsupported_view", "view_tilt_deg": tilt}
    depth, K = request["depth_m"], request["K"]
    valid = (depth > float(policy.get("min_depth_m", 0.08))) & (depth < float(policy.get("max_depth_m", 2.0)))
    cloud = pointcloud(depth, K)
    points = cloud[valid & mask]
    min_points = int(policy.get("min_object_points", 80))
    if len(points) < min_points:
        return [], {"reason": "insufficient_object_depth", "object_points": len(points)}
    ex, ey, up = plane_basis(up)
    basis = np.column_stack((ex, ey))
    heights = points @ up
    threshold = float(np.percentile(heights, 100 - float(policy.get("top_slice_percentile", 20))))
    top = points[heights >= threshold - float(policy.get("top_band_m", 0.005))]
    if len(top) < min_points:
        return [], {"reason": "insufficient_top_surface", "top_points": len(top)}
    center = np.median(top, axis=0)
    uv = (top - center) @ basis
    _, axes = np.linalg.eigh(np.cov(uv, rowvar=False))
    principal = basis @ axes[:, 0]
    gripper = request["gripper"]
    clearance = gripper["clearance_m"]
    lower = max(gripper["min_opening_m"], float(policy.get("min_grasp_width_m", 0)))
    upper = min(gripper["max_opening_m"] - 2 * clearance, float(policy.get("max_grasp_width_m", 1)))
    neighbors = cloud[valid & ~mask & (scene_mask if scene_mask is not None else True)]
    candidates = []
    for angle in policy.get("yaw_candidates_deg", [0, 30, 60, 90, 120, 150]):
        rad = math.radians(angle)
        close = math.cos(rad) * principal + math.sin(rad) * np.cross(up, principal)
        approach = -up
        lateral = np.cross(approach, close)
        local = (top - center) @ np.column_stack((close, lateral, up))
        # The full visible span is used for opening feasibility, avoiding an
        # attractive but unsafe width from a small central percentile slice.
        lo, hi = float(local[:, 0].min()), float(local[:, 0].max())
        width = hi - lo
        if not lower <= width <= upper:
            continue
        # Anchor to a measured surface point close to the projected center.
        middle = (lo + hi) * 0.5
        nominal = center + close * middle
        nearest = np.argmin(np.linalg.norm((top - nominal) @ basis, axis=1))
        contact = top[nearest].copy()
        # Keep X centered on the object span; only a bounded top surface depth
        # is used. Finger-depth and TCP offsets remain separate tool geometry.
        contact += close * (middle - float((contact - center) @ close))
        span = float(local[:, 1].max() - local[:, 1].min())
        finger_half = float(policy.get("contact_half_span_m", 0.010))
        thickness = gripper["finger_thickness_m"]
        finger_depth = gripper["finger_depth_m"]
        if len(neighbors):
            n = (neighbors - contact) @ np.column_stack((close, lateral, up))
            near_fingers = (
                (np.abs(n[:, 0]) >= width / 2 + clearance)
                & (np.abs(n[:, 0]) <= width / 2 + clearance + thickness)
                & (np.abs(n[:, 1]) <= finger_half + clearance)
                & (n[:, 2] >= -finger_depth) & (n[:, 2] <= finger_depth)
            )
            # This local visible-surface check is conservative and incomplete;
            # no claim of hidden geometry or complete collision detection.
            if np.any(near_fingers):
                continue
        T = np.eye(4)
        T[:3, :3] = np.column_stack((close, lateral, approach))
        T[:3, 3] = contact
        score = float(np.clip(1 - width / max(upper, 1e-6), 0, 1))
        if span >= 2 * finger_half:
            score = 0.7 * score + 0.3
        candidates.append({
            "candidate_id": f"{object_id}:{angle}", "object_id": str(object_id),
            "class_name": class_name, "T_camera_grasp": T, "width_m": width,
            "score": score, "backend": request["backend"],
            "metrics": {"surface_points": len(top), "view_tilt_deg": tilt,
                        "visible_span_m": span, "frame_convention": "contact_x_close_z_approach"},
        })
    candidates.sort(key=lambda candidate: candidate["score"], reverse=True)
    return candidates[:request.get("max_candidates", 8)], {
        "reason": "ok" if candidates else "no_geometrically_feasible_candidate",
        "object_points": len(points), "top_points": len(top), "view_tilt_deg": tilt,
    }
