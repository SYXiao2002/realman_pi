"""Server-side preparation-pose score and workspace eligibility."""

import numpy as np

from .geometry import angle_to_vertical_down_degrees, rigid
from .path import screen_workspace


def assess_prepare_pose(T_base_camera, T_camera_prepare_contact, T_camera_prepare_tcp,
                        motion_config, suction):
    """Return a directional vertical score and the prepare pose's workspace status."""
    result = {"vertical_down_angle_deg": None, "workspace_valid": None, "workspace_reason": None}
    if T_base_camera is None:
        result["workspace_reason"] = "缺少采集时的 BASE-相机变换"
        return result

    try:
        base_camera = rigid(T_base_camera)
        prepare_contact = base_camera @ rigid(T_camera_prepare_contact)
        result["vertical_down_angle_deg"] = angle_to_vertical_down_degrees(prepare_contact)
    except (TypeError, ValueError):
        result["workspace_reason"] = "准备位坐标变换无效，无法评分"
        return result

    if T_camera_prepare_tcp is None:
        result["workspace_reason"] = "缺少准备位 TCP 变换"
        return result
    if motion_config.get("workspace_m") is None:
        result["workspace_reason"] = "未配置 workspace"
        return result

    try:
        bounds = np.asarray(motion_config["workspace_m"], dtype=np.float64)
        margin = float(motion_config["collision_margin_m"])
        body_radius = float(suction["body_radius_m"])
        body_length = float(suction["body_length_m"])
        if (bounds.shape != (2, 3) or not np.isfinite(bounds).all() or not np.all(bounds[0] < bounds[1])
                or not np.isfinite([margin, body_radius, body_length]).all()
                or margin < 0 or body_radius <= 0 or body_length <= 0):
            raise ValueError("invalid workspace or suction envelope")
        prepare_tcp = base_camera @ rigid(T_camera_prepare_tcp)
        screen_workspace([prepare_tcp], motion_config, suction)
    except ValueError as exc:
        if "exceeds configured workspace" in str(exc):
            result["workspace_valid"] = False
            result["workspace_reason"] = "准备位 TCP 或吸盘本体超出 workspace"
        else:
            result["workspace_reason"] = "workspace 检查配置无效"
    except (KeyError, TypeError, IndexError, np.linalg.LinAlgError):
        result["workspace_reason"] = "workspace 检查配置不完整或无效"
    else:
        result["workspace_valid"] = True
    return result
