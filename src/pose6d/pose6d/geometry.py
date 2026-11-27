"""Rigid transforms use column vectors and T_A_B maps B into A."""

import math

import numpy as np


def rigid(value):
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        raise ValueError("expected a finite 4x4 transform")
    rotation = matrix[:3, :3]
    if (not np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-6)
            or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-5)
            or not np.isclose(np.linalg.det(rotation), 1, atol=1e-5)):
        raise ValueError("transform must be a proper rigid transform")
    return matrix


def matrix_to_xyzrpy(value, tolerance=1e-6):
    """Return [x, y, z, roll, pitch, yaw] using Rz(yaw) Ry(pitch) Rx(roll)."""
    matrix = rigid(value)
    rotation = matrix[:3, :3]
    pitch = float(np.arcsin(np.clip(-rotation[2, 0], -1.0, 1.0)))
    if abs(np.cos(pitch)) > tolerance:
        roll = float(np.arctan2(rotation[2, 1], rotation[2, 2]))
        yaw = float(np.arctan2(rotation[1, 0], rotation[0, 0]))
    else:
        roll = 0.0
        yaw = float(np.arctan2(-rotation[0, 1], rotation[1, 1]))
    return (float(matrix[0, 3]), float(matrix[1, 3]), float(matrix[2, 3]), roll, pitch, yaw)


def configured_transform(value):
    """Read a rigid transform matrix or an operator-facing xyz/RPY mapping."""
    if not isinstance(value, dict):
        return rigid(value)
    if set(value) != {"xyz_m", "rpy_rad"}:
        raise ValueError("transform mapping must contain only xyz_m and rpy_rad")
    xyz = np.asarray(value["xyz_m"], dtype=np.float64)
    rpy = np.asarray(value["rpy_rad"], dtype=np.float64)
    if xyz.shape != (3,) or rpy.shape != (3,) or not np.isfinite(xyz).all() or not np.isfinite(rpy).all():
        raise ValueError("xyz_m and rpy_rad must each contain three finite values")
    roll, pitch, yaw = rpy
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    rotation = np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ])
    result = np.eye(4)
    result[:3, :3] = rotation
    result[:3, 3] = xyz
    return rigid(result)


def suction_pose(T_base_object, region):
    """Return the base-frame contact pose and outward unit normal."""
    transform = rigid(T_base_object)
    center = np.asarray(region["center_obj"], dtype=np.float64)
    normal = np.asarray(region["normal_obj"], dtype=np.float64)
    tangent = np.asarray(region["tangent_ref_obj"], dtype=np.float64)
    if any(v.shape != (3,) or not np.isfinite(v).all() for v in (center, normal, tangent)):
        raise ValueError("contact vectors must be finite 3-vectors")
    if not np.isclose(np.linalg.norm(normal), 1.0, atol=1e-4):
        raise ValueError("surface normal must be a unit vector")
    normal = transform[:3, :3] @ normal
    tangent = transform[:3, :3] @ tangent
    z = -normal
    x = tangent - np.dot(tangent, z) * z
    length = np.linalg.norm(x)
    if length <= 1e-3:
        raise ValueError("tangent is parallel to the surface normal")
    x /= length
    result = np.eye(4)
    result[:3, :3] = np.column_stack((x, np.cross(z, x), z))
    result[:3, 3] = transform[:3, :3] @ center + transform[:3, 3]
    return result, normal


def tcp_target(contact, normal, distance_m, T_contact_tcp):
    contact = rigid(contact).copy()
    normal = np.asarray(normal, dtype=np.float64)
    if (normal.shape != (3,) or not np.isfinite(normal).all()
            or not np.isclose(np.linalg.norm(normal), 1, atol=1e-4)
            or not np.isfinite(distance_m)):
        raise ValueError("invalid distance or normal")
    if not np.allclose(contact[:3, 2], -normal, atol=1e-5):
        raise ValueError("contact +Z must point opposite the outward normal")
    contact[:3, 3] += float(distance_m) * normal
    return contact @ rigid(T_contact_tcp)


def angle_to_vertical_down_degrees(T_base_pose):
    """Measure the pose's local +Z approach axis against BASE vertical down."""
    pose = rigid(T_base_pose)
    approach = pose[:3, 2]
    cosine = float(np.clip(np.dot(approach, [0.0, 0.0, -1.0]), -1.0, 1.0))
    return math.degrees(math.acos(cosine))

