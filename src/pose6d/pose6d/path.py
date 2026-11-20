"""Sample a Cartesian path and check its tool geometry against the workspace."""

import math

import numpy as np

from grasp_bridge.geometry import from_pose, to_pose

from .geometry import configured_transform, rigid


def samples(start, target, config):
    a, qa = to_pose(rigid(start))
    b, qb = to_pose(rigid(target))
    qa, qb = np.asarray(qa), np.asarray(qb)
    dot = float(qa @ qb)
    if dot < 0:
        qb, dot = -qb, -dot
    half = math.acos(float(np.clip(dot, -1, 1)))
    count = max(1, math.ceil(np.linalg.norm(np.subtract(b, a)) / config["cartesian_sample_step_m"]),
                math.ceil(2 * half / config["angular_sample_step_rad"]))
    if count > 2000:
        raise ValueError("path requires too many samples")
    for fraction in np.linspace(0, 1, count + 1):
        q = ((1 - fraction) * qa + fraction * qb if half < 1e-6 else
             (math.sin((1 - fraction) * half) * qa + math.sin(fraction * half) * qb) / math.sin(half))
        q /= np.linalg.norm(q)
        yield from_pose((1 - fraction) * np.asarray(a) + fraction * np.asarray(b), q)


def screen_workspace(poses, config, suction, guard=lambda: None):
    poses = list(poses)
    if not poses:
        raise ValueError("suction path is empty")
    bounds = np.asarray(config["workspace_m"])
    radius = suction["body_radius_m"]
    length = suction["body_length_m"]
    margin = config["collision_margin_m"]
    inverse_tool = np.linalg.inv(configured_transform(config["T_contact_tcp"]))
    for tcp in poses:
        guard()
        contact = tcp @ inverse_tool
        axis = contact[:3, 2]
        center = contact[:3, 3] - .5 * length * axis
        extent = .5 * length * np.abs(axis) + radius * np.sqrt(np.maximum(0, 1 - axis ** 2)) + margin
        if (np.any(center - extent < bounds[0]) or np.any(center + extent > bounds[1])
                or np.any(tcp[:3, 3] < bounds[0]) or np.any(tcp[:3, 3] > bounds[1])):
            raise ValueError("TCP or suction swept body exceeds configured workspace")
