"""Image-space pose screening; rendered depth is camera Z in metres."""

import numpy as np


def screen(mask, observed_depth, rendered_depth, *, min_valid_pixels=100,
           inlier_tolerance_m=0.01, min_iou=None, max_median_m=None):
    mask = np.asarray(mask, dtype=bool)
    obs = np.asarray(observed_depth)
    rendered = np.asarray(rendered_depth)
    if mask.ndim != 2 or obs.shape != mask.shape or rendered.shape != mask.shape:
        raise ValueError("mask and depths must have identical 2D shapes")
    if (type(min_valid_pixels) is not int or min_valid_pixels < 1
            or not np.isfinite(inlier_tolerance_m) or inlier_tolerance_m <= 0):
        raise ValueError("invalid screening settings")
    for value in (min_iou, max_median_m):
        if value is not None and (not np.isfinite(value) or value < 0):
            raise ValueError("invalid screening threshold")
    if min_iou is not None and min_iou > 1:
        raise ValueError("IoU threshold must be <= 1")
    silhouette = np.isfinite(rendered) & (rendered > 0)
    overlap = mask & silhouette
    valid = overlap & np.isfinite(obs) & (obs > 0)
    m, r, o, v = (int(np.count_nonzero(a)) for a in (mask, silhouette, overlap, valid))
    union = m + r - o
    errors = np.abs(rendered[valid] - obs[valid])
    metrics = {
        "mask_pixels": m, "render_pixels": r, "overlap_pixels": o,
        "valid_depth_pixels": v, "valid_depth_fraction": v / m if m else None,
        "silhouette_iou": o / union if union else None,
        "mask_coverage": o / m if m else None, "render_coverage": o / r if r else None,
        "depth_median_mm": float(np.median(errors) * 1000) if v else None,
        "depth_p90_mm": float(np.percentile(errors, 90) * 1000) if v else None,
        "depth_inlier_ratio": float(np.mean(errors <= inlier_tolerance_m)) if v else None,
        "inlier_tolerance_mm": inlier_tolerance_m * 1000,
    }
    if not m or not r:
        status, warning = "invalid", "empty mask or rendered silhouette"
    elif v < min_valid_pixels:
        status, warning = "insufficient_evidence", "too few valid overlapping depth pixels"
    elif min_iou is None or max_median_m is None:
        status, warning = "insufficient_evidence", "screening thresholds are not calibrated"
    elif metrics["silhouette_iou"] < min_iou or metrics["depth_median_mm"] > max_median_m * 1000:
        status, warning = "suspect", "silhouette or depth differs from the observation"
    else:
        status, warning = "pass_screening", None
    return {"status": status, "warning": warning, "metrics": metrics}

