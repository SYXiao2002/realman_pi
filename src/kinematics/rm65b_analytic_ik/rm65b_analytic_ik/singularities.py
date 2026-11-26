"""Shared singularity and numerical-stability helpers."""

from __future__ import annotations

import numpy as np


def rotation_error(target: np.ndarray, actual: np.ndarray) -> float:
    """Geodesic orientation error in radians, stable at zero and pi."""
    relative = target.T @ actual
    cosine = float(np.clip((np.trace(relative) - 1.0) / 2.0, -1.0, 1.0))
    return float(np.arccos(cosine))
