"""Explicit frame math and restricted planning; no hardware or ROS imports."""

import math

import numpy as np


def transform(value):
    value = np.asarray(value, dtype=np.float64)
    if value.shape != (4, 4) or not np.isfinite(value).all():
        raise ValueError("expected finite 4x4 transform")
    R = value[:3, :3]
    if not np.allclose(value[3], [0, 0, 0, 1], atol=1e-8) or not np.allclose(
        R.T @ R, np.eye(3), atol=1e-6
    ) or not np.isclose(np.linalg.det(R), 1.0, atol=1e-6):
        raise ValueError("expected a right-handed rigid transform")
    return value.copy()


def from_pose(position, quaternion_wxyz):
    q = np.asarray(quaternion_wxyz, dtype=float)
    p = np.asarray(position, dtype=float)
    if q.shape != (4,) or p.shape != (3,) or not np.isfinite(q).all() or not np.isfinite(p).all():
        raise ValueError("invalid pose")
    if abs(np.linalg.norm(q) - 1) > 1e-3:
        raise ValueError("quaternion is not unit length")
    w, x, y, z = q / np.linalg.norm(q)
    T = np.eye(4)
    T[:3, :3] = [
        [1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
        [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
        [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)],
    ]
    T[:3, 3] = p
    return T


def to_pose(T):
    T = transform(T)
    R = T[:3, :3]
    # Eigenvector avoids the singular trace branch at rotations near pi.
    K = np.array([
        [R[0,0]-R[1,1]-R[2,2], R[1,0]+R[0,1], R[2,0]+R[0,2], R[2,1]-R[1,2]],
        [R[1,0]+R[0,1], R[1,1]-R[0,0]-R[2,2], R[2,1]+R[1,2], R[0,2]-R[2,0]],
        [R[2,0]+R[0,2], R[2,1]+R[1,2], R[2,2]-R[0,0]-R[1,1], R[1,0]-R[0,1]],
        [R[2,1]-R[1,2], R[0,2]-R[2,0], R[1,0]-R[0,1], np.trace(R)],
    ]) / 3
    _, vectors = np.linalg.eigh(K)
    xyzw = vectors[:, -1]
    q = xyzw[[3, 0, 1, 2]]
    if q[0] < 0:
        q = -q
    return T[:3, 3].tolist(), q.tolist()


def pose_drift(a, b):
    a, b = transform(a), transform(b)
    angle = math.acos(float(np.clip((np.trace(a[:3,:3].T @ b[:3,:3])-1)/2, -1, 1)))
    return float(np.linalg.norm(a[:3,3]-b[:3,3])), angle


def restricted_waypoints(T_base_camera, T_camera_grasp, T_grasp_tcp, *, approach_m, lift_m, workspace):
    """TCP endpoints and straight corridor in a measured axis-aligned convex box.

    This is a restricted corridor check, not a whole-arm collision planner.
    The grasp's +Z points INTO the object; pregrasp retracts along -Z.
    """
    bg = transform(T_base_camera) @ transform(T_camera_grasp)
    grasp = bg @ transform(T_grasp_tcp)
    pre = grasp.copy()
    pre[:3, 3] -= bg[:3, 2] * approach_m
    lift = grasp.copy()
    lift[2, 3] += lift_m
    bounds = np.asarray(workspace, dtype=float)
    if bounds.shape != (2, 3) or not np.isfinite(bounds).all() or np.any(bounds[0] >= bounds[1]):
        raise ValueError("workspace must contain measured min/max xyz")
    for name, point in (("pregrasp", pre), ("grasp", grasp), ("lift", lift)):
        if np.any(point[:3, 3] < bounds[0]) or np.any(point[:3, 3] > bounds[1]):
            raise ValueError(f"{name} leaves the verified TCP corridor")
    return {"pregrasp": pre, "grasp": grasp, "lift": lift}
