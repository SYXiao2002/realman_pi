"""Isolated ROS Action evidence; no robot or gripper server is involved."""

import copy
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
pytest.importorskip("rclpy")
from action_msgs.msg import GoalStatus
import rclpy
from rclpy.action import ActionClient
from rclpy.context import Context
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from realman_msgs.action import PlanGrasp
from realman_msgs.srv import SolveIk, TaskControl
from std_msgs.msg import Header

import grasp_bridge.node as bridge_module
from grasp_bridge.node import GraspBridge
from grasp_bridge.profiles import load_profile
from grasp_bridge.geometry import from_pose

PROJECT = Path(__file__).resolve().parents[4]


def wait(future):
    until = time.monotonic()+5
    while not future.done() and time.monotonic() < until:
        time.sleep(0.005)
    assert future.done(), "isolated ROS response timed out"
    return future.result()


@pytest.fixture
def graph(monkeypatch):
    monkeypatch.setenv("ROS_DOMAIN_ID", "217")
    context = Context()
    rclpy.init(context=context)
    executor = MultiThreadedExecutor(num_threads=4, context=context)
    nodes = []
    thread = None

    def start(*, dry_run):
        nonlocal thread
        node = GraspBridge(context=context, parameter_overrides=[
            Parameter("config_root", value=str(PROJECT / "config")), Parameter("dry_run", value=dry_run)])
        client_node = Node("grasp_test_client", context=context)
        client = ActionClient(client_node, PlanGrasp, "/grasp_bridge/plan_grasp")
        node.lease_valid = True
        node.lease_checks = 0

        def status(request, response):
            assert request.operation == TaskControl.Request.STATUS
            assert request.task_token == "test-lease"
            node.lease_checks += 1
            response.success = response.leased = node.lease_valid
            response.expired = not node.lease_valid
            return response
        client_node.create_service(TaskControl, "/r/task_control", status)
        def solve_ik(request, response):
            # Mirrors the driver's strict BASE/name pair contract.
            response.success = request.reference_type == SolveIk.Request.BASE and request.reference_name == "base"
            response.api2_status = 0 if response.success else -1
            response.joint_degrees = list(request.seed_joint_degrees)
            response.message = "base reference must be named base"
            return response
        client_node.create_service(SolveIk, "/r/solve_ik", solve_ik)
        nodes.extend([node, client_node])
        for item in nodes:
            executor.add_node(item)
        thread = threading.Thread(target=executor.spin, daemon=True)
        thread.start()
        assert client.wait_for_server(timeout_sec=3)
        return node, client
    yield start
    executor.shutdown(timeout_sec=5)
    if thread:
        thread.join(timeout=5)
    for item in nodes:
        item.destroy_node()
    rclpy.shutdown(context=context)


def goal():
    value = PlanGrasp.Goal()
    value.profile_name, value.task_id, value.target_label = "right_wrist", "test-task", "eu"
    value.lease_token = "test-lease"
    return value


def test_default_dry_run_has_no_hardware_resources_and_reports_refusal(graph):
    node, client = graph(dry_run=True)
    assert list(node.clients) == []
    assert list(node.subscriptions)
    assert hasattr(node, "tf") and not hasattr(node, "ik")
    handle = wait(client.send_goal_async(goal()))
    assert handle.accepted
    wrapped = wait(handle.get_result_async())
    assert wrapped.status == GoalStatus.STATUS_ABORTED
    assert not wrapped.result.success
    assert "configuration dry-run" in wrapped.result.message


def test_dry_run_evaluation_infers_and_publishes_without_ik_or_waypoints(graph, monkeypatch):
    profile = ready_profile()
    profile["readiness_problems"] = ["synthetic test profile"]
    monkeypatch.setattr(bridge_module, "load_profile", lambda *a, **k: copy.deepcopy(profile))
    node, client = graph(dry_run=True)
    published = []
    monkeypatch.setattr(node.evaluations, "publish", lambda message: published.append(json.loads(message.data)))
    T = np.eye(4)
    T[2, 3] = 0.4
    monkeypatch.setattr(node, "capture", lambda *a, **k: {
        "color_bgr": np.zeros((8, 8, 3), np.uint8), "depth_m": np.ones((8, 8), np.float32) * 0.4,
        "K": np.array([[10., 0., 4.], [0., 10., 4.], [0., 0., 1.]]), "up_camera": None,
        "T_base_camera": None, "seed": [0.] * 6, "capture_timestamp_ns": node.get_clock().now().nanoseconds,
        "source_header": Header(frame_id=profile["color_frame"])})
    monkeypatch.setattr(bridge_module, "infer", lambda *a, **k: {
        "model_id": "fixture-model", "diagnostics": {},
        "candidates": [{"candidate_id": "c1", "object_id": "1", "class_name": "eu", "backend": "geometry",
                        "T_camera_grasp": T, "width_m": 0.03, "score": 0.8}],
    })
    monkeypatch.setattr(node, "solve", lambda *a: pytest.fail("evaluation called IK"))
    goal_request = goal()
    goal_request.profile_name = "right_wrist"
    goal_request.backend = "geometry"
    goal_request.target_label = "eu"
    goal_request.evaluation_only = True
    handle = wait(client.send_goal_async(goal_request))
    wrapped = wait(handle.get_result_async())
    assert wrapped.status == GoalStatus.STATUS_SUCCEEDED
    assert wrapped.result.success and not wrapped.result.plan_id
    assert list(node.clients) == []
    assert [event["phase"] for event in published] == ["captured", "inferred"]
    assert published[-1]["candidates"][0]["T_base_grasp"] is None
    assert published[-1]["read_only"] and not published[-1]["execution_ready"]


def ready_profile():
    profile = copy.deepcopy(load_profile(PROJECT / "config", "right_wrist"))
    profile.update(T_grasp_tcp=np.eye(4).tolist(), approach_m=0.03, lift_m=0.05,
                   workspace_m=[[-1, -1, 0], [1, 1, 1]], readiness_problems=[])
    profile["calibration_data"]["T_flange_color"] = np.eye(4).tolist()
    return profile


def test_capture_tf_uses_selected_flange_and_image_time_without_tcp_offset():
    profile = ready_profile()
    calibration = profile["calibration_data"]
    calibration.update({key: profile[key] for key in ("camera_serial", "mount_id", "flange_frame", "color_frame")})
    calibration.update(calibration_id="test-calibration", intrinsics_sha256="test-intrinsics")
    flange_color = from_pose([.03, -.07, .04], [np.cos(.2), np.sin(.2), 0., 0.])
    base_flange = from_pose([.2, .3, .4], [np.cos(.3), 0., 0., np.sin(.3)])
    calibration["T_flange_color"] = flange_color.tolist()
    calls = []
    def lookup(*args):
        calls.append(args)
        return base_flange.copy()
    bridge = SimpleNamespace(profile=profile, lookup=lookup)
    result, info = GraspBridge.camera_at_capture(bridge, 123456789)
    assert calls == [("r/base_link", "r/link_6", 123456789)]
    assert info["status"] == "available" and info["capture_timestamp_ns"] == "123456789"
    point = np.array([.02, -.01, .5, 1.])
    np.testing.assert_allclose(result @ point, base_flange @ (flange_color @ point))
    np.testing.assert_allclose(info["T_base_flange"], base_flange)
    np.testing.assert_allclose(info["T_flange_color"], flange_color)


@pytest.mark.parametrize("problem", ["missing_calibration", "invalid_calibration", "calibration_identity_mismatch", "tf_unavailable"])
def test_capture_tf_diagnoses_failures_without_latest_or_identity_fallback(problem):
    profile = ready_profile()
    calibration = profile["calibration_data"]
    calibration.update({key: profile[key] for key in ("camera_serial", "mount_id", "flange_frame", "color_frame")})
    calibration.update(calibration_id="test-calibration", intrinsics_sha256="test-intrinsics")
    if problem == "missing_calibration":
        calibration["T_flange_color"] = None
    elif problem == "invalid_calibration":
        calibration["T_flange_color"][0][0] = 2.
    elif problem == "calibration_identity_mismatch":
        calibration["flange_frame"] = "l/link_6"
    def lookup(parent, child, timestamp):
        assert problem == "tf_unavailable" and timestamp == 123
        raise LookupError("extrapolation")
    result, info = GraspBridge.camera_at_capture(SimpleNamespace(profile=profile, lookup=lookup), 123)
    assert result is None and info["status"] == problem and info["detail"]


@pytest.mark.parametrize("revoke_during_inference, stale_feedback", [(False, False), (True, False), (False, True)])
def test_frozen_plan_requires_live_task_lease_through_inference(graph, monkeypatch, revoke_during_inference, stale_feedback):
    profile = ready_profile()
    monkeypatch.setattr(bridge_module, "load_profile", lambda *a, **k: copy.deepcopy(profile))
    node, client = graph(dry_run=False)
    # Only read-only clients exist, never ExecuteMotion or gripper.
    assert [c.srv_name for c in node.clients] == ["/r/solve_ik", "/r/task_control"]
    T = np.eye(4)
    T[2, 3] = 0.4
    monkeypatch.setattr(node, "capture", lambda *a, **k: {
        "color_bgr": np.zeros((8, 8, 3), np.uint8), "depth_m": np.ones((8, 8), np.float32),
        "K": np.array([[10., 0., 4.], [0., 10., 4.], [0., 0., 1.]]), "up_camera": [0., 0., 1.],
        "T_base_camera": np.eye(4), "seed": [0.]*6, "capture_timestamp_ns": node.get_clock().now().nanoseconds,
        "source_header": Header(frame_id=profile["color_frame"])})
    # Measured test geometry is synthetic and never persisted as a production value.
    profile["gripper_geometry"].update(min_opening_m=0., max_opening_m=0.1, finger_depth_m=0.04,
                                        finger_thickness_m=0.01, clearance_m=0.005)
    def inference(*args, **kwargs):
        np.testing.assert_array_equal(args[1]["T_base_camera"], np.eye(4))
        np.testing.assert_array_equal(args[1]["up_camera"], [0., 0., 1.])
        if revoke_during_inference:
            node.lease_valid = False
        return {"candidates": [{"candidate_id": "c1", "T_camera_grasp": T, "width_m": .03}],
                "diagnostics": {"segmap_png": b"binary-label-image", "vertical_axis_camera": np.array([0., 0., -1.]),
                                "segmap_sha256": "fixture-label-hash", "archive_id": "fixture-archive"}}
    monkeypatch.setattr(bridge_module, "infer", inference)
    monkeypatch.setattr(node, "solve", lambda T, seed, canceled: seed)
    monkeypatch.setattr(node, "lookup", lambda *a: np.eye(4))
    monkeypatch.setattr(node, "stopped", lambda *a: not stale_feedback)
    node.profile = copy.deepcopy(profile)
    handle = wait(client.send_goal_async(goal()))
    result = wait(handle.get_result_async())
    if revoke_during_inference:
        assert result.status == GoalStatus.STATUS_ABORTED
        assert "lease revoked or expired" in result.result.message
        assert not result.result.success and not result.result.plan_id
        assert node.lease_checks == 3
        return
    if stale_feedback:
        assert result.status == GoalStatus.STATUS_ABORTED
        assert "joint feedback is stale" in result.result.message
        assert not result.result.success and not result.result.plan_id
        return
    assert result.status == GoalStatus.STATUS_SUCCEEDED, result.result.message
    assert result.result.profile_id == profile["profile_id"]
    assert result.result.expires_timestamp_ns > result.result.capture_timestamp_ns > 0
    assert result.result.base_frame == "r/base_link"
    diagnostics = json.loads(result.result.diagnostics_json)["server"]
    assert diagnostics["archive_id"] == "fixture-archive"
    assert diagnostics["vertical_axis_camera"] == [0., 0., -1.]
    assert "segmap_png" not in diagnostics
    np.testing.assert_allclose(result.result.grasp_position_m, [0, 0, 0.4])
    assert node.lease_checks == 4


def test_revoked_task_cannot_begin_capture(graph, monkeypatch):
    profile = ready_profile()
    monkeypatch.setattr(bridge_module, "load_profile", lambda *a, **k: copy.deepcopy(profile))
    node, client = graph(dry_run=False)
    node.lease_valid = False
    monkeypatch.setattr(node, "capture", lambda *a: pytest.fail("revoked task captured a new observation"))
    handle = wait(client.send_goal_async(goal()))
    wrapped = wait(handle.get_result_async())
    assert wrapped.status == GoalStatus.STATUS_ABORTED
    assert "lease revoked or expired" in wrapped.result.message
    assert node.lease_checks == 1


def test_real_ik_client_uses_driver_base_reference_contract(graph):
    node, _ = graph(dry_run=False)
    assert list(node.solve(np.eye(4), [1., 2., 3., 4., 5., 6.], lambda: False)) == [1., 2., 3., 4., 5., 6.]


def test_busy_rejection_cancel_and_subsequent_task(graph, monkeypatch):
    profile = ready_profile()
    monkeypatch.setattr(bridge_module, "load_profile", lambda *a, **k: copy.deepcopy(profile))
    node, client = graph(dry_run=False)
    entered = threading.Event()

    def capture(canceled, not_before, *, evaluation_only=False):
        entered.set()
        while not canceled():
            time.sleep(0.005)
        raise bridge_module.PlanningCanceled("capture canceled")
    monkeypatch.setattr(node, "capture", capture)
    first = wait(client.send_goal_async(goal()))
    assert first.accepted and entered.wait(2)
    assert not wait(client.send_goal_async(goal())).accepted
    assert wait(first.cancel_goal_async()).goals_canceling
    result = wait(first.get_result_async())
    assert result.status == GoalStatus.STATUS_CANCELED and not result.result.success
    second = wait(client.send_goal_async(goal()))
    assert second.accepted
    wait(second.cancel_goal_async())
    assert wait(second.get_result_async()).status == GoalStatus.STATUS_CANCELED


@pytest.mark.parametrize("offset_sec", [-1., 1.])
def test_latest_flange_tf_requires_recent_feedback(graph, monkeypatch, offset_sec):
    from geometry_msgs.msg import TransformStamped
    node, _ = graph(dry_run=False)
    value = TransformStamped()
    timestamp = node.get_clock().now().nanoseconds + int(offset_sec * 1e9)
    value.header.stamp.sec, value.header.stamp.nanosec = divmod(timestamp, 1_000_000_000)
    value.transform.rotation.w = 1.
    monkeypatch.setattr(node.tf, "lookup_transform", lambda *a, **k: value)
    with pytest.raises(ValueError, match="latest flange TF is stale or future"):
        node.lookup("r/base_link", "r/link_6", 0)
