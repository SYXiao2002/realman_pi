"""Verify the binary adapter's public ROS contract without serial hardware."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import std_msgs.msg
    from std_srvs.srv import SetBool, Trigger
    from gripper_ros2 import gripper_manager_node as gmn
    from gripper_ros2_msgs.msg import GripperLimits
    from gripper_ros2_msgs.srv import GripperPercentage, MoveGripperRaw, SetGripperLimits
    HAVE_ROS = True
except ImportError:
    HAVE_ROS = False


CONFIG = """buses:
  - port: /dev/tty_gripper_42d
    protocol: binary_42d
    grippers:
      - {name: gripper_left, slave_id: 1, open_position: 20, close_position: 900, min_position: 0, max_position: 1000}
      - {name: gripper_right, slave_id: 2, open_position: 50, close_position: 8500, min_position: 0, max_position: 9000}
"""


class StubLogger:
    def __init__(self):
        self.warnings = []

    def info(self, message):
        pass

    def warning(self, message):
        self.warnings.append(message)

    def error(self, message):
        raise AssertionError(message)


class StubPublisher:
    def __init__(self, message_type):
        self.message_type = message_type
        self.messages = []

    def publish(self, message):
        if not isinstance(message, self.message_type):
            raise AssertionError("published an incompatible ROS message type")
        self.messages.append(message)


class StubNode:
    def __init__(self):
        self.logger = StubLogger()
        self.services = {}
        self.subscriptions = {}
        self.publishers = {}

    def get_logger(self):
        return self.logger

    def create_service(self, service_type, name, callback):
        self.services[name] = (service_type, callback)
        return object()

    def create_subscription(self, message_type, name, callback, qos):
        self.subscriptions[name] = (message_type, callback)
        return object()

    def create_publisher(self, message_type, name, qos):
        publisher = StubPublisher(message_type)
        self.publishers[name] = publisher
        return publisher

    def create_timer(self, period, callback):
        return object()


class StubBus:
    def __init__(self):
        self.port = "/dev/tty_gripper_42d"
        self.connected = True
        self.pending = {}


class StubBinaryDevice:
    is_binary = True

    def __init__(self, bus, config):
        self.bus = bus
        for field, value in config.items():
            setattr(self, field, value)
        self.connected = True
        self._enabled = False
        self.calls = []
        self.wait_result = "position"
        self.feedback = {
            "position": self.open_position,
            "speed": 0, "current": 0, "torque_reached": False, "alarm": 0,
        }

    def enable(self, enabled=True):
        self.calls.append(("enable", enabled))
        self._enabled = bool(enabled)
        if not enabled:
            self.bus.pending.pop(self.slave_id, None)

    def disable(self):
        self.enable(False)

    def move_to(self, target):
        self.calls.append(("move_to", target))

    def wait_until_reached(self, timeout):
        self.calls.append(("wait_until_reached", timeout))
        return self.wait_result

    def read_feedback(self):
        self.calls.append(("read_feedback",))
        return self.feedback

    def reset(self):
        raise AssertionError("binary reset must not reach the device")


class StubManager:
    def __init__(self, config):
        self.bus = StubBus()
        self._devices = {
            entry["name"]: StubBinaryDevice(self.bus, entry)
            for entry in config["buses"][0]["grippers"]
        }
        self.device_names = list(self._devices)
        self.requests = []

    def get(self, name):
        return self._devices[name]

    def connect_all(self):
        return {self.bus.port: True}

    def start_all(self):
        pass

    def stop_all(self):
        pass

    def disconnect_all(self):
        pass

    def request_move(self, name, target):
        self.requests.append((name, target))
        self.bus.pending[self.get(name).slave_id] = target


@unittest.skipUnless(HAVE_ROS, "needs a ROS 2 environment with gripper_ros2_msgs built")
class BinaryManagerNodeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config_file = Path(temporary.name) / "gripper.yaml"
        config_file.write_text(CONFIG, encoding="utf-8")
        self.node = StubNode()
        with patch.object(gmn.GripperManager, "from_config", side_effect=StubManager):
            self.adapter = gmn.GripperManagerNode(self.node, str(config_file))
        self.addCleanup(self.adapter.destroy)
        self.manager = self.adapter.manager

    def call(self, name, suffix, **fields):
        service_type, callback = self.node.services[f"/{name}/{suffix}"]
        return callback(service_type.Request(**fields), service_type.Response())

    def command(self, name, value):
        message_type, callback = self.node.subscriptions[f"/{name}/percentage/command"]
        callback(message_type(data=value))

    def test_left_and_right_keep_all_service_and_topic_types(self):
        service_types = {
            "open": Trigger, "close": Trigger, "reset": Trigger,
            "enable": SetBool, "grasp_check": Trigger,
            "percentage": GripperPercentage, "calibrate": Trigger,
            "set_limits": SetGripperLimits, "move_raw": MoveGripperRaw,
        }
        topic_types = {
            "position": std_msgs.msg.Float64, "speed": std_msgs.msg.Int32,
            "current": std_msgs.msg.Int32, "torque_reached": std_msgs.msg.Bool,
            "alarm": std_msgs.msg.Int32, "connected": std_msgs.msg.Bool,
            "limits": GripperLimits,
        }
        names = ("gripper_left", "gripper_right")
        self.assertEqual(set(self.node.services), {
            f"/{name}/{suffix}" for name in names for suffix in service_types
        })
        self.assertEqual(set(self.node.publishers), {
            f"/{name}/{suffix}" for name in names for suffix in topic_types
        })
        self.assertEqual(set(self.node.subscriptions), {
            f"/{name}/percentage/command" for name in names
        })
        for name in names:
            for suffix, expected_type in service_types.items():
                self.assertIs(self.node.services[f"/{name}/{suffix}"][0], expected_type)
            for suffix, expected_type in topic_types.items():
                self.assertIs(self.node.publishers[f"/{name}/{suffix}"].message_type, expected_type)
            self.assertIs(
                self.node.subscriptions[f"/{name}/percentage/command"][0],
                std_msgs.msg.Float32,
            )

    def test_synchronous_commands_wait_for_the_device_endpoint(self):
        for name in self.manager.device_names:
            device = self.manager.get(name)
            for suffix, fields, target in (
                ("open", {}, device.open_position),
                ("close", {}, device.close_position),
                ("percentage", {"percentage": 0.5},
                 (device.open_position + device.close_position) // 2),
                ("move_raw", {"position": 300}, 300),
            ):
                with self.subTest(name=name, command=suffix):
                    device.calls.clear()
                    device._enabled = False
                    response = self.call(name, suffix, **fields)
                    self.assertTrue(response.success, response.message)
                    self.assertEqual(device.calls, [
                        ("enable", True), ("move_to", target),
                        ("wait_until_reached", 20.0), ("read_feedback",),
                    ])
        self.assertEqual(self.manager.requests, [])

    def test_synchronous_commands_report_endpoint_timeout_as_failure(self):
        device = self.manager.get("gripper_left")
        device.wait_result = "timeout"
        for suffix, fields in (
            ("open", {}), ("close", {}),
            ("percentage", {"percentage": 0.5}),
            ("move_raw", {"position": 300}),
        ):
            with self.subTest(command=suffix):
                device.calls.clear()
                response = self.call(device.name, suffix, **fields)
                self.assertFalse(response.success, response.message)
                self.assertIn("timeout", response.message)
                self.assertIn(("wait_until_reached", 20.0), device.calls)

    def test_binary_reset_is_unsupported_without_preparing_the_device(self):
        for connected in (True, False):
            with self.subTest(connected=connected):
                device = self.manager.get("gripper_left")
                device.connected = connected
                response = self.call(device.name, "reset")
                self.assertFalse(response.success)
                self.assertIn("not supported", response.message)
                self.assertFalse(device._enabled)
                self.assertEqual(device.calls, [])

    def test_offline_disable_clears_pending_commands_and_describes_software_gate(self):
        device = self.manager.get("gripper_left")
        device._enabled = True
        self.manager.request_move(device.name, device.close_position)
        device.connected = False
        response = self.call(device.name, "enable", data=False)
        self.assertTrue(response.success, response.message)
        self.assertIn("software", response.message.lower())
        self.assertIn("no hardware stop/disable", response.message.lower())
        self.assertFalse(device._enabled)
        self.assertNotIn(device.slave_id, self.manager.bus.pending)
        self.assertEqual(device.calls, [("enable", False)])

    def test_binary_grasp_check_does_not_claim_a_grasp_or_prepare_the_device(self):
        device = self.manager.get("gripper_left")
        response = self.call(device.name, "grasp_check")
        self.assertFalse(response.success)
        self.assertIn("not supported", response.message)
        self.assertFalse(device._enabled)
        self.assertEqual(device.calls, [])

    def test_continuous_commands_queue_without_enable_or_serial_work(self):
        for name in self.manager.device_names:
            device = self.manager.get(name)
            self.command(name, 0.5)
            self.assertEqual(device.calls, [])
            self.assertFalse(device._enabled)
            self.assertEqual(
                self.manager.requests[-1],
                (name, (device.open_position + device.close_position) // 2),
            )
        self.assertEqual(len(self.manager.requests), 2)

    def test_invalid_or_offline_continuous_commands_do_not_queue(self):
        device = self.manager.get("gripper_left")
        for value in (-0.1, 1.1, float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                self.command(device.name, value)
        device.connected = False
        self.command(device.name, 0.5)
        self.assertEqual(self.manager.requests, [])
        self.assertEqual(self.manager.bus.pending, {})
        self.assertEqual(device.calls, [])
        self.assertEqual(len(self.node.logger.warnings), 6)

    def test_connected_is_published_per_slave_on_the_shared_bus(self):
        left = self.manager.get("gripper_left")
        right = self.manager.get("gripper_right")
        left.connected, right.connected = False, True
        self.assertTrue(left.bus.connected)
        self.assertIs(left.bus, right.bus)
        self.adapter._publish_feedback()
        self.assertFalse(self.node.publishers["/gripper_left/connected"].messages[-1].data)
        self.assertTrue(self.node.publishers["/gripper_right/connected"].messages[-1].data)
        left.connected, right.connected = True, False
        self.adapter._publish_feedback()
        self.assertTrue(self.node.publishers["/gripper_left/connected"].messages[-1].data)
        self.assertFalse(self.node.publishers["/gripper_right/connected"].messages[-1].data)


if __name__ == "__main__":
    unittest.main()
