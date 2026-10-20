from pathlib import Path
import tempfile
import unittest

import yaml

from gripper_ros2.gripper_config import (
    interface_names,
    load_gripper_config,
    percentage_to_position,
)


ROOT = Path(__file__).resolve().parents[4]


class GripperConfigTest(unittest.TestCase):
    def _load_bus(self, **overrides):
        bus = {
            "port": "/dev/test_gripper",
            "protocol": "binary_42d",
            "grippers": [{
                "name": "gripper_left", "slave_id": 1,
                "min_position": 0, "max_position": 100,
                "open_position": 0, "close_position": 100,
            }],
        }
        bus.update(overrides)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gripper.yaml"
            path.write_text(yaml.safe_dump({"buses": [bus]}), encoding="utf-8")
            return load_gripper_config(path)

    def test_root_config_defines_only_installed_left_and_right_grippers(self):
        config = load_gripper_config(ROOT / "config/ros/gripper.yaml")
        names = {
            item["name"]
            for bus in config["buses"]
            for item in bus["grippers"]
        }
        self.assertEqual(names, {"gripper_left", "gripper_right"})

    def test_interfaces_preserve_existing_service_paths(self):
        names = interface_names("gripper_left")
        self.assertEqual(names["percentage"], "/gripper_left/percentage")
        self.assertEqual(names["percentage_command"], "/gripper_left/percentage/command")
        self.assertEqual(names["position"], "/gripper_left/position")

    def test_interfaces_include_limit_endpoints(self):
        names = interface_names("gripper_right")
        self.assertEqual(names["set_limits"], "/gripper_right/set_limits")
        self.assertEqual(names["move_raw"], "/gripper_right/move_raw")
        self.assertEqual(names["limits"], "/gripper_right/limits")

    def test_percentage_maps_closed_zero_and_open_one(self):
        self.assertEqual(percentage_to_position(0.0, open_position=400, close_position=949), 949)
        self.assertEqual(percentage_to_position(1.0, open_position=400, close_position=949), 400)
        self.assertEqual(percentage_to_position(0.5, open_position=400, close_position=1000), 700)

    def test_percentage_rejects_out_of_range_values(self):
        with self.assertRaises(ValueError):
            percentage_to_position(1.1, open_position=0, close_position=9000)

    def test_protocol_must_be_supported(self):
        with self.assertRaises(ValueError):
            self._load_bus(protocol="unknown")

    def test_physical_port_cannot_be_split_into_independent_buses(self):
        document = self._load_bus()
        duplicate = dict(document["buses"][0])
        duplicate["grippers"] = [dict(duplicate["grippers"][0], name="gripper_right", slave_id=2)]
        document["buses"].append(duplicate)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gripper.yaml"
            path.write_text(yaml.safe_dump(document), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_gripper_config(path)

    def test_binary_threshold_must_be_finite_and_inside_unit_interval(self):
        for value in (float("nan"), 0.0, 1.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self._load_bus(binary_threshold=value)

    def test_binary_hysteresis_must_fit_on_both_sides_of_threshold(self):
        for value in (-0.01, 0.51, float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self._load_bus(binary_hysteresis=value)
        with self.assertRaises(ValueError):
            self._load_bus(binary_threshold=0.2, binary_hysteresis=0.21)

    def test_binary_transaction_spacing_respects_hardware_minimum(self):
        for value in (0.09, float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self._load_bus(inter_transaction_s=value)

    def test_binary_coordinates_require_distinct_ordered_endpoints(self):
        for open_position, close_position in ((50, 50), (100, 0)):
            with self.subTest(endpoints=(open_position, close_position)), self.assertRaises(ValueError):
                self._load_bus(grippers=[{
                    "name": "gripper_left", "slave_id": 1,
                    "min_position": 0, "max_position": 100,
                    "open_position": open_position, "close_position": close_position,
                }])


if __name__ == "__main__":
    unittest.main()
