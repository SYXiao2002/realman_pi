"""Read-only ROS service for all limit-compliant RM65-B analytic IK branches."""

import numpy as np
import rclpy
from rclpy.node import Node
from realman_msgs.srv import SolveAnalyticIk

from .api import IKSolver
from .conversions import matrix_to_xyzrpy


def solve_request(solver, request, response):
    try:
        target = matrix_to_xyzrpy(np.asarray(request.target_base_tcp).reshape(4, 4))
        tool = matrix_to_xyzrpy(np.asarray(request.flange_tcp).reshape(4, 4))
    except (TypeError, ValueError):
        response.success = False
        response.reason = "INVALID_POSE"
        return response
    result = solver.solve_all(target, tool_xyzrpy=tool)
    response.success = result["reason"] not in ("INVALID_POSE", "NUMERICAL_FAILURE")
    response.reachable = result["reachable"]
    response.valid_solution_count = result["valid_solution_count"]
    response.reason = result["reason"] or ""
    response.solutions_rad = [float(angle) for branch in result["solutions"] for angle in branch]
    return response


class AnalyticIkNode(Node):
    def __init__(self, **kwargs):
        super().__init__("rm65b_analytic_ik", **kwargs)
        config_path = self.declare_parameter("config_path", "").value
        self.solver = IKSolver(config_path or None)
        self.service = self.create_service(SolveAnalyticIk, "~/solve_all", self.solve_all)
        self.get_logger().info(f"analytic IK ready: {self.solver.config.name}")

    def solve_all(self, request, response):
        try:
            return solve_request(self.solver, request, response)
        except Exception as exc:
            self.get_logger().error(f"analytic IK failed: {exc}")
            response.success = False
            response.reason = "INTERNAL_ERROR"
            return response


def main():
    rclpy.init()
    node = AnalyticIkNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
