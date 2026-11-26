"""Read-only ROS client integration and unavailable/timeout display semantics."""

import threading
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("rclpy")
pytest.importorskip("realman_msgs")

import rclpy
from rclpy.context import Context
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.task import Future
from realman_msgs.srv import SolveAnalyticIk
from rm65b_analytic_ik import forward_kinematics
from rm65b_analytic_ik.conversions import xyzrpy_to_matrix
from rm65b_analytic_ik.ros_node import AnalyticIkNode
from pose6d_bridge.analytic_ik_ros import AnalyticIkRosPort, tcp_targets


def test_ros_client_checks_frozen_base_tcp_with_tool_without_robot():
    context = Context()
    rclpy.init(context=context, domain_id=219)
    server = AnalyticIkNode(context=context)
    client = Node("analytic_ik_pose_test", context=context)
    port = AnalyticIkRosPort(client, {"service": "/rm65b_analytic_ik/solve_all", "timeout_sec": 2})
    executor = MultiThreadedExecutor(context=context, num_threads=2)
    executor.add_node(server)
    executor.add_node(client)
    thread = threading.Thread(target=executor.spin)
    thread.start()
    try:
        assert port.client.wait_for_service(timeout_sec=3)
        tool = [.03, -.02, .14, .2, -.1, .8]
        target = forward_kinematics([.2, -.3, .8, .5, .7, -1.2], server.solver.config, tool)
        base_camera = xyzrpy_to_matrix([.4, -.2, .3, .2, .4, -.5])
        camera_tcp = np.linalg.inv(base_camera) @ target
        result = port.check(base_camera, camera_tcp, xyzrpy_to_matrix(tool))
        assert result["checked"] and result["reachable"]
        assert result["valid_solution_count"] > 1
        unreachable = np.eye(4)
        unreachable[0, 3] = 10
        result = port.check(np.eye(4), unreachable, xyzrpy_to_matrix(tool))
        assert result == {"checked": True, "reachable": False, "valid_solution_count": 0,
                          "reason": "OUT_OF_REACH"}
    finally:
        executor.shutdown(timeout_sec=3)
        thread.join(timeout=3)
        client.destroy_node()
        server.destroy_node()
        context.shutdown()


@pytest.mark.parametrize("failure", ["unavailable", "timeout", "error", "numerical", "missing_base", "missing_tcp"])
def test_incomplete_checks_are_unknown_not_unreachable(failure):
    future = Future()
    if failure == "error":
        future.set_exception(RuntimeError("transport lost"))
    elif failure == "numerical":
        future.set_result(SolveAnalyticIk.Response(success=False, reason="NUMERICAL_FAILURE"))
    client = SimpleNamespace(wait_for_service=lambda **_: failure != "unavailable", call_async=lambda _: future)
    port = AnalyticIkRosPort.__new__(AnalyticIkRosPort)
    port.client, port.timeout = client, .01
    result = port.check(None if failure == "missing_base" else np.eye(4),
                        None if failure == "missing_tcp" else np.eye(4), np.eye(4))
    assert result["checked"] is False and result["reachable"] is None
    assert result["reason"] == {"unavailable": "SERVICE_UNAVAILABLE", "timeout": "SERVICE_TIMEOUT",
                                "error": "SERVICE_ERROR", "numerical": "NUMERICAL_FAILURE",
                                "missing_base": "MISSING_BASE_CAMERA", "missing_tcp": "MISSING_TCP_TARGET"}[failure]
    if failure == "timeout":
        assert future.cancelled()


def test_tcp_display_coordinates_are_computed_on_server():
    base = xyzrpy_to_matrix([.1, .2, .3, 0, 0, np.pi / 2])
    tcp = xyzrpy_to_matrix([.01, .02, .2, 0, 0, 0])
    result = tcp_targets(base, tcp, None, "r/base_link")
    assert result["frame"] == "r/base_link" and result["prepare"] is None
    np.testing.assert_allclose(result["suction"]["xyz_mm"], [80, 210, 500])
    np.testing.assert_allclose(result["suction"]["rpy_deg"], [0, 0, 90])
    result = tcp_targets(None, tcp, tcp, "r/base_link")
    assert result["frame"] == "相机"
    np.testing.assert_allclose(result["suction"]["xyz_mm"], [10, 20, 200])
