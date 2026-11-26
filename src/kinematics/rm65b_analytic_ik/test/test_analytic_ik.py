import math
import random
from dataclasses import replace

import numpy as np
import pytest

from rm65b_analytic_ik import (
    IKSolver,
    forward_kinematics,
    forward_kinematics_xyzrpy,
    load_config,
)
from rm65b_analytic_ik.conversions import matrix_to_xyzrpy, xyzrpy_to_matrix
from rm65b_analytic_ik.config_loader import DEFAULT_CONFIG_PATH
from rm65b_analytic_ik.singularities import rotation_error


@pytest.fixture(scope="module")
def solver():
    return IKSolver()


def _assert_pose_matches(actual, target, config):
    assert np.linalg.norm(actual[:3, 3] - target[:3, 3]) <= config.position_tolerance * 10
    assert rotation_error(target[:3, :3], actual[:3, :3]) <= config.orientation_tolerance * 10


def test_config_units_and_zero_pose_match_rm65b_model(solver):
    config = solver.config
    assert config.name == "RealMan RM65-B"
    assert config.d1 == pytest.approx(0.2405)
    assert config.a3 == pytest.approx(0.256)
    assert config.d4 == pytest.approx(0.210)
    assert config.d6 == pytest.approx(0.144)

    flange = forward_kinematics([0.0] * 6, config)
    assert flange[:3, 3] == pytest.approx((0.0, 0.0, 0.8505), abs=1e-12)
    assert flange[:3, :3] == pytest.approx(np.diag((-1.0, -1.0, 1.0)), abs=1e-12)


@pytest.mark.parametrize(
    "xyzrpy",
    [
        (0.1, -0.2, 0.3, 0.4, -0.7, 1.2),
        (-1.0, 0.5, 2.0, -2.2, math.pi / 2, 0.9),
        (0.0, 0.0, 0.0, 2.4, -math.pi / 2, -0.2),
    ],
)
def test_xyzrpy_matrix_round_trip(xyzrpy):
    matrix = xyzrpy_to_matrix(xyzrpy)
    recovered = matrix_to_xyzrpy(matrix)
    assert xyzrpy_to_matrix(recovered) == pytest.approx(matrix, abs=1e-9)


def test_random_fk_ik_fk_round_trips(solver):
    rng = random.Random(501)
    config = solver.config
    for _ in range(100):
        joints = [
            rng.uniform(low * 0.7, high * 0.7)
            for low, high in config.joint_limits
        ]
        flange = forward_kinematics(joints, config)
        target = matrix_to_xyzrpy(flange)
        result = solver.solve_all(target)
        assert result["reachable"] is True
        assert result["valid_solution_count"] > 0
        target_matrix = xyzrpy_to_matrix(target)
        for solution in result["solutions"]:
            _assert_pose_matches(forward_kinematics(solution, config), target_matrix, config)


def test_tool_transform_is_removed_before_ik(solver):
    joints = [0.3, -0.25, 0.65, -0.4, 0.55, 1.2]
    tool = [0.018, -0.012, 0.105, 0.1, -0.2, 0.3]
    tcp = forward_kinematics(joints, solver.config, tool)
    result = solver.solve_all(matrix_to_xyzrpy(tcp), tool)
    assert result["reachable"] is True
    target = xyzrpy_to_matrix(matrix_to_xyzrpy(tcp))
    for solution in result["solutions"]:
        _assert_pose_matches(forward_kinematics(solution, solver.config, tool), target, solver.config)


def test_joint_offsets_are_applied_from_the_loaded_model(solver):
    rows = [list(row) for row in solver.config.mdh]
    for index, offset in ((0, 0.12), (3, -0.18), (4, 0.1), (5, -0.25)):
        rows[index][3] = offset
    shifted_config = replace(solver.config, mdh=tuple(tuple(row) for row in rows))
    joints = [0.2, -0.25, 0.65, 0.4, 0.55, -0.8]
    target = forward_kinematics(joints, shifted_config)
    result = IKSolver(config=shifted_config).solve_all(matrix_to_xyzrpy(target))
    assert result["reachable"] is True
    for solution in result["solutions"]:
        _assert_pose_matches(forward_kinematics(solution, shifted_config), target, shifted_config)


def test_offset_can_move_the_wrist_singularity_to_effective_pi(solver):
    rows = [list(row) for row in solver.config.mdh]
    rows[4][3] = math.pi
    shifted_config = replace(solver.config, mdh=tuple(tuple(row) for row in rows))
    joints = [0.2, -0.25, 0.65, 0.4, 0.0, -0.8]
    target = forward_kinematics(joints, shifted_config)
    result = IKSolver(config=shifted_config).solve_all(matrix_to_xyzrpy(target))
    assert result["reachable"] is True
    assert any(abs(solution[4]) <= 1e-6 for solution in result["solutions"])
    for solution in result["solutions"]:
        _assert_pose_matches(forward_kinematics(solution, shifted_config), target, shifted_config)


def test_check_reachable_returns_after_first_valid_branch(solver):
    target = forward_kinematics_xyzrpy([0.2, -0.3, 0.8, 0.5, 0.7, -1.2], solver.config)
    assert solver.check_reachable(target) == {
        "reachable": True,
        "valid_solution_count": 1,
        "reason": None,
    }


@pytest.mark.parametrize(
    ("joints", "singular_joint"),
    [
        ([0.1, -0.2, 0.0, 0.3, 0.6, -0.7], 2),  # elbow branch merge
        ([0.1, -0.2, 0.6, 0.3, 0.0, -0.7], 4),  # wrist q5 = 0
        ([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], 0),  # shoulder-axis family
    ],
)
def test_singular_configurations_remain_solvable(solver, joints, singular_joint):
    target = forward_kinematics_xyzrpy(joints, solver.config)
    result = solver.solve_all(target)
    assert result["reachable"] is True
    assert result["valid_solution_count"] > 0
    target_matrix = xyzrpy_to_matrix(target)
    for solution in result["solutions"]:
        _assert_pose_matches(forward_kinematics(solution, solver.config), target_matrix, solver.config)
    if singular_joint == 0:
        # The shoulder-axis family is represented canonically rather than by a sampled continuum.
        assert result["valid_solution_count"] < 10


def test_reason_codes_for_unreachable_invalid_and_joint_limited_targets(solver):
    assert solver.check_reachable([2.0, 0.0, 0.5, 0.0, 0.0, 0.0])["reason"] == "OUT_OF_REACH"
    assert solver.check_reachable([0.0, 1.0, math.nan, 0.0, 0.0, 0.0])["reason"] == "INVALID_POSE"

    outside_limits = [0.2, math.radians(140), 0.8, 0.5, 1.0, 0.7]
    target = forward_kinematics_xyzrpy(outside_limits, solver.config)
    assert solver.check_reachable(target)["reason"] == "JOINT_LIMIT"


def test_j6_keeps_legal_multiturn_representatives(solver):
    target = forward_kinematics_xyzrpy([0.3, -0.3, 0.7, 0.4, 0.8, 1.0], solver.config)
    result = solver.solve_all(target)
    assert result["reachable"] is True
    assert any(abs(solution[5]) > math.pi for solution in result["solutions"])
    assert all(solver.config.joint_limits[5][0] <= solution[5] <= solver.config.joint_limits[5][1]
               for solution in result["solutions"])


def test_exact_joint_limit_boundaries_are_accepted(solver):
    joints = [math.radians(value) for value in (178, 130, 135, 178, 128, 360)]
    target = forward_kinematics(joints, solver.config)
    result = solver.solve_all(matrix_to_xyzrpy(target))
    assert result["reachable"] is True
    assert any(
        np.allclose(solution[:5], joints[:5], atol=1e-6)
        and abs(abs(solution[5]) - 2.0 * math.pi) <= 1e-6
        for solution in result["solutions"]
    )


def test_invalid_forward_kinematics_input_raises():
    with pytest.raises(ValueError):
        forward_kinematics([0.0] * 5)


def test_unsupported_mdh_structure_is_rejected(tmp_path):
    config_path = tmp_path / "params.yaml"
    source_path = DEFAULT_CONFIG_PATH
    contents = source_path.read_text(encoding="utf-8").replace(
        "J4: [0.0,  90.0", "J4: [0.0,  80.0"
    )
    config_path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="alpha changes"):
        load_config(config_path)
