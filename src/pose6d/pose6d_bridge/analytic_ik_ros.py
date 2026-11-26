"""Query the analytic IK node from the inference worker, without moving the arm."""

import threading

from rclpy.callback_groups import ReentrantCallbackGroup
from realman_msgs.srv import SolveAnalyticIk
from rm65b_analytic_ik.conversions import matrix_to_xyzrpy

import numpy as np

from pose6d.geometry import rigid


def unknown(reason):
    return {"checked": False, "reachable": None, "valid_solution_count": None, "reason": reason}


def tcp_targets(T_base_camera, suction_tcp, prepare_tcp, base_frame):
    """Send display coordinates in mm/degrees; browser only formats the values."""
    base = rigid(T_base_camera) if T_base_camera is not None else np.eye(4)
    def display(value):
        if value is None:
            return None
        pose = matrix_to_xyzrpy(base @ rigid(value))
        return {"xyz_mm": (np.asarray(pose[:3]) * 1000).tolist(),
                "rpy_deg": np.rad2deg(pose[3:]).tolist()}
    return {"frame": base_frame if T_base_camera is not None else "相机",
            "suction": display(suction_tcp), "prepare": display(prepare_tcp)}


class AnalyticIkRosPort:
    def __init__(self, node, settings):
        self.timeout = float(settings["timeout_sec"])
        if not 0 < self.timeout <= 30:
            raise ValueError("analytic_ik.timeout_sec must be in (0, 30]")
        self.client = node.create_client(SolveAnalyticIk, settings["service"],
                                         callback_group=ReentrantCallbackGroup())

    def check(self, T_base_camera, T_camera_tcp, T_flange_tcp):
        if T_base_camera is None:
            return unknown("MISSING_BASE_CAMERA")
        if T_camera_tcp is None:
            return unknown("MISSING_TCP_TARGET")
        try:
            target = rigid(T_base_camera) @ rigid(T_camera_tcp)
            request = SolveAnalyticIk.Request(
                target_base_tcp=target.ravel().tolist(), flange_tcp=rigid(T_flange_tcp).ravel().tolist())
        except (TypeError, ValueError):
            return unknown("INVALID_POSE")
        if not self.client.wait_for_service(timeout_sec=min(.2, self.timeout)):
            return unknown("SERVICE_UNAVAILABLE")
        future = None
        try:
            done = threading.Event()
            future = self.client.call_async(request)
            future.add_done_callback(lambda _future: done.set())
            if not done.wait(self.timeout):
                future.cancel()
                return unknown("SERVICE_TIMEOUT")
            response = future.result()
            if response is None or not response.success:
                return unknown(response.reason if response else "SERVICE_ERROR")
            return {"checked": True, "reachable": response.reachable,
                    "valid_solution_count": response.valid_solution_count, "reason": response.reason or None}
        except Exception:
            if future is not None:
                future.cancel()
            return unknown("SERVICE_ERROR")
