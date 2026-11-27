"""Read-only RM65 IK checks through RealMan's native SDK algorithm API."""

import ctypes
import threading

import numpy as np

from pose6d.geometry import matrix_to_xyzrpy, rigid


def unknown(reason):
    return {"checked": False, "reachable": None, "valid_solution_count": None, "reason": reason}


def _load_sdk_api():
    from Robotic_Arm import rm_ctypes_wrap

    return rm_ctypes_wrap


class NativeIkSdkPort:
    """Use the SDK's offline full-solution API; this port never connects or moves the arm."""

    def __init__(self, sdk_api=None, logger=None):
        self._lock = threading.Lock()
        self._sdk_api = sdk_api
        try:
            if self._sdk_api is None:
                self._sdk_api = _load_sdk_api()
            self._initialize_sdk()
        except Exception as exc:
            self._sdk_api = None
            if logger is not None:
                logger.warning(f"native RM65 IK unavailable: {exc}")

    def _initialize_sdk(self):
        api = self._sdk_api
        api.rm_algo_init_sys_data(
            api.rm_robot_arm_model_e.RM_MODEL_RM_65_E,
            api.rm_force_type_e.RM_MODEL_RM_B_E,
        )
        api.rm_algo_set_angle(0.0, 0.0, 0.0)

        # The request pose is the flange pose in robot BASE. Set both SDK frames
        # explicitly to identity so no controller-side work/tool state is needed.
        self._workframe = api.rm_frame_t(
            frame_name="pose6d", pose=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            payload=0.0, x=0.0, y=0.0, z=0.0,
        )
        self._toolframe = api.rm_frame_t(
            frame_name="pose6d", pose=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            payload=0.0, x=0.0, y=0.0, z=0.0,
        )
        api.rm_algo_set_workframe(ctypes.byref(self._workframe))
        api.rm_algo_set_toolframe(ctypes.byref(self._toolframe))

    def check(self, T_base_camera, T_camera_tcp, T_flange_tcp, q_ref_degrees=None):
        if T_base_camera is None:
            return unknown("MISSING_BASE_CAMERA")
        if T_camera_tcp is None:
            return unknown("MISSING_TCP_TARGET")
        try:
            target_base_tcp = rigid(T_base_camera) @ rigid(T_camera_tcp)
            target_base_flange = target_base_tcp @ np.linalg.inv(rigid(T_flange_tcp))
            pose = matrix_to_xyzrpy(target_base_flange)
        except (TypeError, ValueError, np.linalg.LinAlgError):
            return unknown("INVALID_POSE")

        if q_ref_degrees is None:
            q_ref = [0.0] * 6
        else:
            try:
                q_ref = np.asarray(q_ref_degrees, dtype=np.float64)
            except (TypeError, ValueError):
                return unknown("INVALID_REFERENCE_JOINTS")
            if q_ref.shape != (6,) or not np.isfinite(q_ref).all():
                return unknown("INVALID_REFERENCE_JOINTS")
            q_ref = q_ref.tolist()

        if self._sdk_api is None:
            return unknown("SDK_UNAVAILABLE")
        try:
            params = self._sdk_api.rm_inverse_kinematics_params_t(
                [*q_ref, 0.0], pose, 1
            )
            with self._lock:
                result = self._sdk_api.rm_algo_inverse_kinematics_all(None, params)
        except Exception:
            return unknown("SDK_ERROR")

        try:
            status = int(result.result)
        except (AttributeError, TypeError, ValueError):
            return unknown("SDK_INVALID_RESULT")
        if status == 0:
            try:
                count = int(result.num)
            except (AttributeError, TypeError, ValueError):
                return unknown("SDK_INVALID_RESULT")
            if not 0 <= count <= 8:
                return unknown("SDK_INVALID_RESULT")
            return {
                "checked": True,
                "reachable": count > 0,
                "valid_solution_count": count,
                "reason": None if count else "SDK_NO_SOLUTION",
            }
        if status == 1:
            return {"checked": True, "reachable": False,
                    "valid_solution_count": 0, "reason": "SDK_NO_SOLUTION"}
        return unknown({-1: "SDK_INVALID_REFERENCE_JOINTS",
                        -2: "SDK_INVALID_ORIENTATION"}.get(status, f"SDK_ERROR_{status}"))


def tcp_targets(T_base_camera, suction_tcp, prepare_tcp, base_frame):
    """Return operator-facing target coordinates in mm/degrees."""
    base = rigid(T_base_camera) if T_base_camera is not None else np.eye(4)

    def display(value):
        if value is None:
            return None
        pose = matrix_to_xyzrpy(base @ rigid(value))
        return {"xyz_mm": (np.asarray(pose[:3]) * 1000).tolist(),
                "rpy_deg": np.rad2deg(pose[3:]).tolist()}

    return {"frame": base_frame if T_base_camera is not None else "相机",
            "suction": display(suction_tcp), "prepare": display(prepare_tcp)}
