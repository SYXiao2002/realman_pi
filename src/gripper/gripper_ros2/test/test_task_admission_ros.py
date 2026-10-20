"""Exercise real ROS service admission using a driver with its mock SDK."""

import threading
import time

import pytest


def test_driver_and_gripper_share_task_admission():
    rclpy = pytest.importorskip("rclpy")
    from rclpy.executors import MultiThreadedExecutor
    from rclpy.node import Node
    from rclpy.parameter import Parameter
    from realman_robot_driver.realman_driver_node import RealManDriverNode
    from gripper_ros2.task_admission import TaskAdmission

    rclpy.init()
    driver = RealManDriverNode(
        namespace="/r",
        parameter_overrides=[Parameter("robot_ip", value="127.0.0.1"), Parameter("mock_mode", value=True)],
    )
    client_node = Node("task_admission_test")
    gate = TaskAdmission(client_node, {"timeout_sec": 1.0, "arms": {"right": "r"}})
    executor = MultiThreadedExecutor(num_threads=3)
    executor.add_node(driver)
    executor.add_node(client_node)
    spin = threading.Thread(target=executor.spin, daemon=True)
    spin.start()
    try:
        client = gate._clients["right"]
        deadline = time.monotonic() + 5.0
        while not client.service_is_ready() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.service_is_ready()
        with gate.command("right"):
            assert driver.arm_ownership.is_busy("r")
        lease = gate._call(client, operation=0, owner_id="mock-grasp", ttl_sec=10.0)
        assert lease.task_token
        with pytest.raises(PermissionError):
            with gate.command("right"):
                pytest.fail("legacy command bypassed a task lease")
        with gate.command("right", lease.task_token):
            assert driver.arm_ownership.is_busy("r")
            with pytest.raises(ValueError, match="drained"):
                driver.arm_ownership.release_task("r", lease.task_token)
        gate._call(client, operation=1, task_token=lease.task_token, ttl_sec=10.0)
        gate._call(client, operation=5, task_token=lease.task_token)
        with pytest.raises(PermissionError):
            gate._call(client, operation=5, task_token="wrong-token")
        # A stop during idle inference invalidates the task before its next stage.
        response = driver._stop(None, __import__("std_srvs.srv", fromlist=["Trigger"]).Trigger.Response())
        assert response.success
        with pytest.raises(PermissionError):
            gate._call(client, operation=1, task_token=lease.task_token, ttl_sec=10.0)
        with pytest.raises(PermissionError):
            gate._call(client, operation=5, task_token=lease.task_token)
        gate._call(client, operation=2, task_token=lease.task_token)
        assert driver.arm_ownership.task_state("r") == (None, False)
    finally:
        executor.shutdown()
        spin.join(timeout=2.0)
        client_node.destroy_node()
        driver.destroy_node()
        rclpy.shutdown()
