"""Exercise binary mapping, bus scheduling and observed completion without hardware."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from gripper_ros2 import binary_42d as binary
from gripper_ros2.gripper_driver import GripperManager


class FakeRTU:
    instances = []

    def __init__(self, port, **kwargs):
        self.instrument = SimpleNamespace(address=1)
        self.calls = []
        self.status = {1: 1, 2: 6}
        self.mode = {1: 0, 2: 0}
        self.fail = set()
        self.opened = False
        self.instances.append(self)

    def connect(self):
        self.opened = True
        return True

    def disconnect(self):
        self.opened = False

    def read_register(self, register):
        slave = self.instrument.address
        self.calls.append((slave, "read", register))
        if (slave, "read", register) in self.fail:
            raise OSError("no response")
        return self.mode[slave] if register == binary.REG_CONTROL_MODE else self.status[slave]

    def write_register(self, register, value):
        slave = self.instrument.address
        self.calls.append((slave, "write", register, value))
        if (slave, "write", register) in self.fail:
            raise OSError("uncertain write")
        if register == binary.REG_CONTROL_MODE:
            self.mode[slave] = value

    def motions(self):
        return [call for call in self.calls if call[1] == "write"
                and call[2] in (binary.REG_OPEN, binary.REG_CLOSE)]


class BinaryDriverTest(unittest.TestCase):
    def setUp(self):
        FakeRTU.instances = []
        fake = patch.object(binary, "Binary42DRTU", FakeRTU)
        fake.start()
        self.addCleanup(fake.stop)
        self.manager = GripperManager()
        self.bus = self.manager.add_bus("/fake", protocol="binary_42d")
        self.left = self.manager.add_gripper(
            "/fake", 1, name="left", open_position=0, close_position=1000,
            min_position=0, max_position=1000,
        )
        self.right = self.manager.add_gripper(
            "/fake", 2, name="right", open_position=50, close_position=8500,
            min_position=0, max_position=8500,
        )
        self.assertEqual(FakeRTU.instances, [])
        self.manager.connect_all()
        self.sdk = self.bus._sdk
        self.addCleanup(self.manager.disconnect_all)

    def prepare(self):
        self.left.enable()
        self.right.enable()
        self.sdk.calls.clear()

    def test_task_gate_rejects_queued_command_before_mode_or_motion_writes(self):
        @contextmanager
        def denied(_name):
            raise PermissionError("grasp owns arm")
            yield

        self.bus.command_guard = denied
        self.manager.request_move("right", 50)
        self.assertFalse(self.bus._process_pending())
        self.assertEqual(self.sdk.calls, [])
        self.assertNotIn(2, self.bus._pending)
        del self.bus.command_guard
        self.assertFalse(self.bus._process_pending())
        self.assertEqual(self.sdk.calls, [])

    def test_task_gate_spans_preparation_and_hardware_write(self):
        phases = []

        @contextmanager
        def admitted(name):
            phases.append(("enter", name, len(self.sdk.calls)))
            yield
            phases.append(("exit", name, len(self.sdk.calls)))

        self.bus.command_guard = admitted
        self.manager.request_move("right", 50)
        self.assertTrue(self.bus._process_pending())
        self.assertEqual(phases[0], ("enter", "right", 0))
        self.assertEqual(phases[-1][0:2], ("exit", "right"))
        self.assertGreater(phases[-1][2], 0)

    def test_connect_and_feedback_are_read_only_and_share_one_instrument(self):
        self.assertEqual(self.sdk.calls, [])
        self.assertFalse(self.left.connected)
        self.left.read_feedback()
        self.right.read_feedback()
        self.assertEqual(self.sdk.calls, [(1, "read", 0x40), (2, "read", 0x40)])
        self.assertTrue(self.left.connected)
        self.assertTrue(self.right.connected)
        self.assertEqual(len(FakeRTU.instances), 1)
        self.assertIs(self.left.bus, self.right.bus)

    def test_enable_verifies_serial_mode_and_motion_requires_preparation(self):
        with self.assertRaises(RuntimeError):
            self.left.move_to(0)
        self.left.enable()
        self.assertEqual(self.sdk.calls, [
            (1, "read", 0x35), (1, "write", 0x35, 1), (1, "read", 0x35),
        ])
        self.assertEqual(self.sdk.motions(), [])
        self.assertFalse(self.left.connected)  # mode reads are not status feedback

    def test_continuous_targets_use_hysteresis_and_deduplicate_endpoints(self):
        self.prepare()
        # Raw coordinates run in the opposite direction to opening percentage.
        for step, opening in enumerate((0.5, 0.49, 0.45, 0.44, 0.5, 0.54, 0.55, 1, 1)):
            self.bus.request_move(1, round(1000 * (1 - opening)), now=float(step))
            self.bus._process_pending(float(step))
        self.assertEqual([call[2] for call in self.sdk.motions()], [0x36, 0x37, 0x36])
        self.assertEqual(self.bus._pending, {})

    def test_coalescing_preserves_hysteresis_from_every_input(self):
        self.prepare()
        self.bus.request_move(1, 600, now=100.0)  # closed
        self.bus.request_move(1, 400, now=100.1)  # cross into open
        self.bus.request_move(1, 500, now=100.2)  # hold open
        self.assertEqual(self.sdk.calls, [])
        self.bus._process_pending(101.0)
        self.assertEqual(self.sdk.motions(), [(1, "write", 0x36, 1)])
        self.left.move_to(450)  # synchronous path shares desired target and deduplication
        self.assertEqual(len(self.sdk.motions()), 1)

    def test_streaming_prepares_in_worker_and_routes_both_slaves(self):
        self.bus.request_move(1, 0, now=100.0)
        self.bus.request_move(2, 8500, now=100.0)
        self.assertEqual(self.sdk.calls, [])
        self.bus._process_pending(100.0)
        self.assertEqual(self.sdk.motions(), [(1, "write", 0x36, 1), (2, "write", 0x37, 1)])

    def test_target_updated_during_preparation_sends_only_the_latest_endpoint(self):
        preparing, resume = threading.Event(), threading.Event()
        original_read = self.sdk.read_register

        def blocked_read(register):
            if register == binary.REG_CONTROL_MODE and not preparing.is_set():
                preparing.set()
                if not resume.wait(2.0):
                    raise RuntimeError("test did not release serial mode preparation")
            return original_read(register)

        self.bus.request_move(1, self.left.open_position, now=100.0)
        with patch.object(self.sdk, "read_register", side_effect=blocked_read):
            with ThreadPoolExecutor(2) as pool:
                worker = pool.submit(self.bus._process_pending, 100.0)
                try:
                    self.assertTrue(preparing.wait(0.5), "worker did not prepare serial mode")
                    updated = pool.submit(
                        self.bus.request_move, 1, self.left.close_position, 100.1,
                    )
                    # An input callback must remain able to update the queue
                    # while the serial mode transactions are in progress.
                    updated.result(timeout=0.5)
                    self.assertEqual(self.sdk.motions(), [])
                finally:
                    resume.set()
                worker.result(timeout=2.0)
        self.bus._process_pending(101.0)
        self.assertEqual(self.sdk.motions(), [(1, "write", 0x37, 1)])
        self.assertEqual(self.bus._pending, {})
        self.assertFalse(self.left._sent_target)

    def test_feedback_is_observed_endpoint_and_ack_does_not_mean_completion(self):
        self.prepare()
        self.left.move_to(0)
        self.assertFalse(self.left.feedback["position_reached"])
        self.assertEqual(self.left.wait_until_reached(0.01), "timeout")  # still closed
        self.sdk.status[1] = 6
        self.assertEqual(self.left.wait_until_reached(0.1), "position")
        feedback = self.left.feedback
        self.assertEqual(feedback["position"], 0)
        self.assertTrue(feedback["position_reached"])
        self.assertEqual((feedback["speed"], feedback["current"], feedback["alarm"]), (0, 0, 0))
        self.left.move_to(1000)
        self.assertEqual(self.left.wait_until_reached(0.01), "timeout")  # still open
        self.sdk.status[1] = 2
        self.assertEqual(self.left.wait_until_reached(0.1), "position")
        self.assertFalse(self.left.feedback["torque_reached"])
        self.assertEqual(self.left.feedback["position"], 1000)

    def test_unknown_status_retains_observed_position_and_cannot_complete_motion(self):
        self.sdk.status[1] = 99
        feedback = self.left.read_feedback()
        self.assertEqual(feedback["position"], 500)
        self.assertFalse(feedback["position_known"])
        self.sdk.status[1] = 6
        self.left.read_feedback()
        self.sdk.status[1] = 99
        feedback = self.left.read_feedback()
        self.assertEqual(feedback["position"], 0)
        self.assertFalse(feedback["ready"])
        self.assertFalse(feedback["position_reached"])
        self.assertEqual(feedback["alarm"], 0)

    def test_missing_slave_does_not_starve_peer_and_invalidates_its_preparation(self):
        self.prepare()
        self.sdk.fail.add((1, "read", 0x40))
        self.bus._poll_devices()
        self.assertFalse(self.left.connected)
        self.assertFalse(self.left._prepared)
        self.assertTrue(self.right.connected)
        self.assertTrue(self.right._prepared)
        self.sdk.fail.clear()
        self.left.read_feedback()
        self.assertTrue(self.left.connected)
        self.assertFalse(self.left._prepared)
        self.assertEqual(self.sdk.motions(), [])

    def test_failed_write_is_not_replayed_and_peer_still_receives_its_target(self):
        self.prepare()
        self.sdk.fail.add((1, "write", 0x36))
        self.bus.request_move(1, 0, now=100)
        self.bus.request_move(2, 8500, now=100)
        self.bus._process_pending(100)
        self.bus._process_pending(101)
        self.assertEqual(self.sdk.motions(), [(1, "write", 0x36, 1), (2, "write", 0x37, 1)])
        self.assertEqual(self.bus._pending, {})
        self.assertIsNone(self.left._sent_target)

    def test_synchronous_failed_write_invalidates_only_its_device_without_replay(self):
        self.prepare()
        self.bus._poll_devices()
        self.assertTrue(self.left.connected)
        self.assertTrue(self.right.connected)
        self.sdk.calls.clear()
        self.bus.request_move(1, self.left.open_position, now=100.0)
        self.sdk.fail.add((1, "write", 0x36))
        with self.assertRaisesRegex(OSError, "uncertain write"):
            self.left.move_to(self.left.open_position)
        self.assertFalse(self.left.connected)
        self.assertFalse(self.left._enabled)
        self.assertFalse(self.left._prepared)
        self.assertIn("uncertain write", self.left.snapshot()["error"])
        self.assertIsNone(self.left._sent_target)
        self.assertIsNone(self.left._motion_target)
        self.assertNotIn(1, self.bus._pending)
        self.assertTrue(self.right.connected)
        self.assertTrue(self.right._enabled)
        self.assertTrue(self.right._prepared)

        self.sdk.fail.clear()
        self.bus._process_pending(101.0)
        self.bus._poll_devices()
        self.bus._process_pending(102.0)
        self.assertEqual(self.sdk.motions(), [(1, "write", 0x36, 1)])
        self.assertTrue(self.left.connected)  # read-only feedback can recover
        self.assertFalse(self.left._enabled)
        self.assertFalse(self.left._prepared)

    def test_reconnect_clears_queued_targets_preparation_and_feedback(self):
        self.prepare()
        self.bus.request_move(1, 0, now=100)
        self.bus._try_reconnect()
        self.bus._process_pending(101)
        self.assertIsNot(self.sdk, self.bus._sdk)
        self.assertEqual(self.bus._sdk.calls, [])
        self.assertFalse(self.left._enabled)
        self.assertFalse(self.left.connected)
        self.assertEqual(self.bus._pending, {})

    def test_disable_clears_only_its_pending_target_without_hardware_writes(self):
        self.prepare()
        self.bus.request_move(1, 0, now=100)
        self.bus.request_move(2, 8500, now=100)
        self.left.disable()
        self.assertEqual(self.sdk.calls, [])
        self.assertNotIn(1, self.bus._pending)
        self.assertIn(2, self.bus._pending)
        self.assertFalse(self.left._enabled)
        with self.assertRaises(NotImplementedError):
            self.left.reset()

    def test_wait_releases_bus_between_reads_for_the_other_slave(self):
        self.prepare()
        self.left.move_to(0)
        observed = threading.Event()
        original_read = self.left.read_feedback

        def read():
            result = original_read()
            observed.set()
            return result

        self.left.read_feedback = read
        with ThreadPoolExecutor(2) as pool:
            waiting = pool.submit(self.left.wait_until_reached, 1.0)
            self.assertTrue(observed.wait(0.5))
            other = pool.submit(self.right.move_to, 8500)
            other.result(timeout=0.5)
            self.sdk.status[1] = 6
            self.assertEqual(waiting.result(timeout=1.0), "position")
        self.assertIn((2, "write", 0x37, 1), self.sdk.motions())

    def test_config_selects_protocol_and_keeps_current_endpoints(self):
        from pathlib import Path
        from gripper_ros2.gripper_config import load_gripper_config
        root = Path(__file__).resolve().parents[4]
        manager = GripperManager.from_config(load_gripper_config(root / "config/ros/gripper.yaml"))
        self.assertIsInstance(manager.get("gripper_left"), binary.Binary42DDevice)
        self.assertIs(manager.get("gripper_left").bus, manager.get("gripper_right").bus)
        self.assertEqual(set(manager.device_names), {"gripper_left", "gripper_right"})
        self.assertEqual(manager.get("gripper_right").slave_id, 2)

    def test_config_still_supports_mixed_binary_and_changingtek_buses(self):
        manager = GripperManager.from_config({"buses": [
            {"port": "/fake_binary", "protocol": "binary_42d", "grippers": [
                {"name": "gripper_left", "slave_id": 1,
                 "open_position": 20, "close_position": 900},
                {"name": "gripper_right", "slave_id": 2,
                 "open_position": 50, "close_position": 8500},
            ]},
            {"port": "/fake_changingtek", "protocol": "changingtek", "grippers": [
                {"name": "gripper_mid", "slave_id": 1,
                 "open_position": 0, "close_position": 9000},
            ]},
        ]})
        self.assertIsInstance(manager.get("gripper_left"), binary.Binary42DDevice)
        self.assertIs(manager.get("gripper_left").bus, manager.get("gripper_right").bus)
        self.assertFalse(getattr(manager.get("gripper_mid"), "is_binary", False))
        self.assertIsNot(manager.get("gripper_mid").bus, manager.get("gripper_left").bus)


if __name__ == "__main__":
    unittest.main()
