from pathlib import Path
import unittest

import yaml

from gripper_ros2.gripper_config import load_gripper_config


ROOT = Path(__file__).resolve().parents[4]
MANAGER_NODE = ROOT / "src/gripper/gripper_ros2/gripper_ros2/gripper_manager_node.py"


class GripperDeploymentTest(unittest.TestCase):
    def test_manager_exposes_nonblocking_percentage_command_topic(self):
        source = MANAGER_NODE.read_text(encoding="utf-8")
        self.assertIn("std_msgs.msg.Float32", source)
        self.assertIn('f"{prefix}/percentage/command"', source)
        self.assertIn("percentage_to_position(", source)
        start = source.index("    def _percentage_command(")
        end = source.index("    def _calibrate(", start)
        command_callback = source[start:end]
        self.assertIn("self.manager.request_move(name, target)", command_callback)
        self.assertNotIn("wait_until_pos_or_torque", command_callback)

    def test_runtime_overrides_file_is_not_tracked(self):
        ignored = (ROOT / "config/ros/.gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("gripper_overrides.yaml", ignored)
        self.assertIn(".gripper_overrides.yaml.*.tmp", ignored)

    def test_limit_interfaces_are_declared_in_the_msgs_package(self):
        msgs = ROOT / "src/gripper/gripper_ros2_msgs"
        cmake = (msgs / "CMakeLists.txt").read_text(encoding="utf-8")
        for path in ("msg/GripperLimits.msg", "srv/SetGripperLimits.srv", "srv/MoveGripperRaw.srv"):
            self.assertTrue((msgs / path).is_file(), path)
            self.assertIn(f'"{path}"', cmake)

    def test_manager_exposes_limit_services_and_latched_limits_topic(self):
        source = MANAGER_NODE.read_text(encoding="utf-8")
        self.assertIn('f"{prefix}/set_limits"', source)
        self.assertIn('f"{prefix}/move_raw"', source)
        self.assertIn('f"{prefix}/limits"', source)
        self.assertIn("QoSDurabilityPolicy.TRANSIENT_LOCAL", source)
        start = source.index("    def _move_raw(")
        end = source.index("    def _publish_feedback(", start)
        body = source[start:end]
        # An out-of-range jog must be rejected before the gripper is auto-enabled.
        self.assertLess(body.index("check_raw_position("), body.index("self._ready("))

    def test_root_config_requires_only_the_shared_42d_port(self):
        config = load_gripper_config(ROOT / "config/ros/gripper.yaml")
        ports = {bus["port"] for bus in config["buses"]}
        self.assertEqual(
            ports,
            {"/dev/tty_gripper_42d"},
        )
        shared = next(bus for bus in config["buses"] if bus["protocol"] == "binary_42d")
        self.assertEqual(shared["baudrate"], 115200)
        self.assertEqual(shared["timeout"], 0.3)
        self.assertEqual(shared["poll_hz"], 5.0)
        self.assertEqual(shared["inter_transaction_s"], 0.1)
        self.assertEqual(shared["binary_threshold"], 0.5)
        self.assertEqual(shared["binary_hysteresis"], 0.05)
        self.assertEqual(
            {gripper["name"]: gripper["slave_id"] for gripper in shared["grippers"]},
            {"gripper_left": 1, "gripper_right": 2},
        )
        for gripper in shared["grippers"]:
            self.assertFalse({"speed_pct", "force_pct", "accel", "decel"} & gripper.keys())
        self.assertEqual(len(config["buses"]), 1)

    def test_remote_bringup_maps_aliases_into_container(self):
        compose = yaml.safe_load((ROOT / "config/docker/compose.yaml").read_text())
        devices = compose["services"]["realman_bringup_remote"]["devices"]
        self.assertIn(
            "${REALMAN_GRIPPER_42D_DEVICE:-/dev/tty_gripper_42d}:/dev/tty_gripper_42d",
            devices,
        )
        self.assertEqual(len(devices), 1)


if __name__ == "__main__":
    unittest.main()
