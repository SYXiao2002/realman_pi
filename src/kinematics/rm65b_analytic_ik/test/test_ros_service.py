"""Exercise generated service messages, all branches, and invalid input handling."""

import numpy as np
import pytest

pytest.importorskip("rclpy")
pytest.importorskip("realman_msgs")

from realman_msgs.srv import SolveAnalyticIk
from rm65b_analytic_ik import IKSolver, forward_kinematics
from rm65b_analytic_ik.conversions import xyzrpy_to_matrix
from rm65b_analytic_ik.ros_node import solve_request


def test_all_service_branches_match_tcp_with_rotated_offset_tool():
    solver = IKSolver()
    tool = [.03, -.02, .14, .2, -.1, .8]
    target = forward_kinematics([.2, -.3, .8, .5, .7, -1.2], solver.config, tool)
    request = SolveAnalyticIk.Request(target_base_tcp=target.ravel().tolist(),
                                     flange_tcp=xyzrpy_to_matrix(tool).ravel().tolist())
    response = solve_request(solver, request, SolveAnalyticIk.Response())
    assert response.success and response.reachable and response.valid_solution_count > 1
    assert not response.reason
    branches = np.asarray(response.solutions_rad).reshape(-1, 6)
    assert len(branches) == response.valid_solution_count
    for branch in branches:
        np.testing.assert_allclose(forward_kinematics(branch, solver.config, tool), target, atol=1e-6)


def test_service_distinguishes_no_solution_from_invalid_input():
    solver = IKSolver()
    target = np.eye(4)
    target[0, 3] = 10
    request = SolveAnalyticIk.Request(target_base_tcp=target.ravel().tolist(),
                                     flange_tcp=np.eye(4).ravel().tolist())
    response = solve_request(solver, request, SolveAnalyticIk.Response())
    assert response.success and not response.reachable
    assert response.reason == "OUT_OF_REACH"
    assert response.valid_solution_count == 0 and not response.solutions_rad
    target[0, 0] = 2
    request.target_base_tcp = target.ravel().tolist()
    response = solve_request(solver, request, SolveAnalyticIk.Response())
    assert not response.success and response.reason == "INVALID_POSE"
