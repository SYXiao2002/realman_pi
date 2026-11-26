"""Full-resolution depth evidence in the 10–40 mm normal-aligned suction corridor.

Only the projected cylinder ROI is backprojected. No path sampling, scene-wide
point cloud, spatial index, robot state or GPU is needed.
"""

import itertools
import time

import cv2
import numpy as np

from .geometry import rigid


DEFAULTS = {
    "start_distance_m": .01,
    "end_distance_m": .04,
    "depth_tolerance_m": .002,
    "minimum_cluster_pixels": 3,
    "minimum_observed_fraction": .8,
    "maximum_roi_pixels": 100000,
}
COLLISION_MESSAGE = "吸取路径上有物体"


def _settings(config, suction):
    settings = {**DEFAULTS, **(config or {})}
    start = float(settings["start_distance_m"])
    end = float(settings["end_distance_m"])
    depth_tolerance = float(settings["depth_tolerance_m"])
    diameter = float(suction["cup_diameter_m"])
    coverage = float(settings["minimum_observed_fraction"])
    if (not np.isfinite([start, end, depth_tolerance, diameter, coverage]).all()
            or not 0 <= start < end or not 0 <= depth_tolerance < end - start
            or diameter <= 0 or not 0 < coverage <= 1):
        raise ValueError("invalid suction collision dimensions or coverage")
    for key in ("minimum_cluster_pixels", "maximum_roi_pixels"):
        if type(settings[key]) is not int or settings[key] < 1:
            raise ValueError(f"invalid suction collision {key}")
    return settings, start, end, depth_tolerance, diameter


def _ray_intervals(rays, center, normal, radius, length):
    """Camera z intervals where each pinhole ray crosses the finite cylinder."""
    dn = rays @ normal
    cn = float(center @ normal)
    a = np.einsum("ij,ij->i", rays, rays) - dn * dn
    b = -2 * (rays @ center - dn * cn)
    c = float(center @ center - cn * cn - radius * radius)
    discriminant = b * b - 4 * a * c
    regular = a > 1e-12
    radial_ok = np.where(regular, discriminant >= 0, c <= 0)
    root = np.sqrt(np.maximum(0, discriminant))
    low = np.full(len(rays), -np.inf)
    high = np.full(len(rays), np.inf)
    np.divide(-b - root, 2 * a, out=low, where=regular)
    np.divide(-b + root, 2 * a, out=high, where=regular)
    crossing = np.abs(dn) > 1e-12
    cap_a = np.full(len(rays), -np.inf)
    cap_b = np.full(len(rays), np.inf)
    np.divide(cn, dn, out=cap_a, where=crossing)
    np.divide(cn + length, dn, out=cap_b, where=crossing)
    axial_ok = crossing | ((-cn >= 0) & (-cn <= length))
    near = np.maximum(np.maximum(low, np.minimum(cap_a, cap_b)), 0)
    far = np.minimum(high, np.maximum(cap_a, cap_b))
    intersects = radial_ok & axial_ok & (far >= near) & (far > 0)
    return intersects, far


def _evaluate(obs, contact, suction, config):
    settings, start_distance, end_distance, depth_tolerance, diameter = _settings(config, suction)
    length = end_distance - start_distance
    radius = diameter / 2
    pose = rigid(contact)
    normal = -pose[:3, 2]
    center = pose[:3, 3] + start_distance * normal
    depth = np.asarray(obs["depth_m"])
    K = np.asarray(obs["K"], dtype=float)
    if (depth.ndim != 2 or K.shape != (3, 3)
            or not np.isfinite(K).all() or K[0, 0] <= 0 or K[1, 1] <= 0
            or not np.allclose(K[2], [0, 0, 1])):
        raise ValueError("invalid depth or intrinsics")
    result = {"status": "unknown", "reason": None, "length_m": length, "diameter_m": diameter,
              "start_distance_m": start_distance, "end_distance_m": end_distance,
              "collision_pixels": 0, "largest_cluster_pixels": 0, "roi_pixels": 0,
              "observed_fraction": 0.0}
    end = center + length * normal
    radial_extent = radius * np.sqrt(np.maximum(0, 1 - normal * normal))
    lower = np.minimum(center, end) - radial_extent
    upper = np.maximum(center, end) + radial_extent
    corners = np.asarray(list(itertools.product(*zip(lower, upper))))
    if np.min(corners[:, 2]) <= 1e-6:
        return {**result, "reason": "CORRIDOR_BEHIND_CAMERA"}
    projected = corners @ K.T
    uv = projected[:, :2] / projected[:, 2, None]
    bounds = np.array([np.floor(uv.min(axis=0)) - 1, np.ceil(uv.max(axis=0)) + 2], dtype=int)
    height, width = depth.shape
    clipped = bool(np.any(bounds[0] < 0) or bounds[1, 0] > width or bounds[1, 1] > height)
    bounds[:, 0] = np.clip(bounds[:, 0], 0, width)
    bounds[:, 1] = np.clip(bounds[:, 1], 0, height)
    (x0, y0), (x1, y1) = bounds
    roi_pixels = int((y1 - y0) * (x1 - x0))
    result["roi_pixels"] = roi_pixels
    if roi_pixels == 0:
        return {**result, "reason": "CORRIDOR_OUTSIDE_IMAGE"}
    if roi_pixels > settings["maximum_roi_pixels"]:
        return {**result, "reason": "ROI_TOO_LARGE"}
    yy, xx = np.indices((y1 - y0, x1 - x0))
    pixels = np.column_stack(((xx + x0).ravel(), (yy + y0).ravel(), np.ones(roi_pixels)))
    rays = pixels @ np.linalg.inv(K).T
    rays /= rays[:, 2, None]
    values = depth[y0:y1, x0:x1].ravel()
    valid = np.isfinite(values) & (values > 0)
    points = rays * np.where(valid, values, 0)[:, None] - center
    axial = points @ normal
    radial_squared = np.einsum("ij,ij->i", points, points) - axial * axial
    # Include both end caps despite float32 depth rounding. Every scene point
    # inside this interval is evidence, including the selected object itself.
    # Mask content and contact-region radius never enter the calculation.
    hits = (valid & (axial >= -1e-7) & (axial <= length + 1e-7)
            & (radial_squared <= radius * radius + 1e-10))
    result["collision_pixels"] = int(np.count_nonzero(hits))
    if result["collision_pixels"]:
        _, _, components, _ = cv2.connectedComponentsWithStats(hits.reshape(yy.shape).astype(np.uint8), connectivity=8)
        largest = int(np.max(components[1:, cv2.CC_STAT_AREA]))
        result["largest_cluster_pixels"] = largest
        if largest >= settings["minimum_cluster_pixels"]:
            return {**result, "status": "collision", "reason": COLLISION_MESSAGE}
    silhouette, far = _ray_intervals(rays, center, normal, radius, length)
    count = int(np.count_nonzero(silhouette))
    # A valid measurement in front of the corridor occludes it; valid depth
    # alone cannot establish free space. Read depth behind its far boundary.
    observed = silhouette & valid & (values >= far - depth_tolerance)
    fraction = float(np.count_nonzero(observed) / count) if count else 0.0
    result["observed_fraction"] = fraction
    if clipped:
        return {**result, "reason": "CORRIDOR_OUTSIDE_IMAGE"}
    if count < settings["minimum_cluster_pixels"]:
        return {**result, "reason": "INSUFFICIENT_RESOLUTION"}
    if fraction < settings["minimum_observed_fraction"]:
        return {**result, "reason": "DEPTH_MISSING_OR_OCCLUDED"}
    return {**result, "status": "clear", "reason": None}


def check_suction_path(obs, contact, analytic_ik, suction, config=None):
    """Check only candidates with confirmed analytic IK; always return JSON data."""
    if analytic_ik.get("checked") is not True or analytic_ik.get("reachable") is not True:
        return {"status": "skipped", "reason": "NO_CONFIRMED_ANALYTIC_IK", "elapsed_ms": 0.0}
    started = time.perf_counter_ns()
    try:
        result = _evaluate(obs, contact, suction, config)
    except (KeyError, TypeError, ValueError, IndexError, np.linalg.LinAlgError):
        result = {"status": "unknown", "reason": "INVALID_COLLISION_INPUT"}
    result["elapsed_ms"] = (time.perf_counter_ns() - started) / 1e6
    return result
