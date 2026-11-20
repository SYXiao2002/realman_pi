"""Exercise input polarity, port selection and loss of sensor evidence."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from realman_robot_driver.vacuum_sensor import VacuumSensorConfig


CONFIG = Path(__file__).resolve().parents[4] / "config/ros/vacuum_sensors.yaml"


def test_deployment_enables_right_npn_sensor_and_reserves_left():
    right = VacuumSensorConfig.from_yaml(str(CONFIG), "r")
    # The port is installation configuration; polarity and enabled arms are
    # deployment requirements. Port selection is exercised separately below.
    assert right.enabled and right.io == 1 and right.active_level == 0
    assert right.requires_tool_power
    assert not VacuumSensorConfig.from_yaml(str(CONFIG), "l").enabled
    assert not VacuumSensorConfig.from_yaml(str(CONFIG), "m").enabled


@pytest.mark.parametrize("io,level,inputs,holding", [
    (3, 0, [0, 0, 1, 0], False),  # Unloaded right NPN sensor.
    (3, 0, [1, 1, 0, 1], True),
    (1, 1, [1, 0, 0, 0], True),  # Future active-high left sensor.
    (4, 1, [1, 1, 1, 0], False),
])
def test_selected_port_and_polarity(io, level, inputs, holding):
    adapter = SimpleNamespace(connected=True, controller_io_inputs=Mock(return_value=(0, inputs)))
    state = VacuumSensorConfig(True, io, level).sample(adapter, "r", 123)
    assert state["available"] is True
    assert state["holding"] is holding
    assert state["raw_input"] == inputs[io - 1]
    assert state["stamp_ns"] == 123


@pytest.mark.parametrize("connected,enabled", [(False, True), (True, False)])
def test_disconnected_and_disabled_do_not_read_io(connected, enabled):
    adapter = SimpleNamespace(connected=connected, controller_io_inputs=Mock())
    state = VacuumSensorConfig(enabled).sample(adapter, "l", 0)
    assert state["available"] is False and state["holding"] is None
    adapter.controller_io_inputs.assert_not_called()


@pytest.mark.parametrize("result", [(-2, None), (0, [1, 1, -1, 1]), (0, []), (0, None), (0, [0, 0, "0", 0])])
def test_failed_or_non_input_port_never_reports_object_state(result):
    adapter = SimpleNamespace(connected=True, controller_io_inputs=Mock(return_value=result))
    state = VacuumSensorConfig(True).sample(adapter, "r", 0)
    assert state["available"] is False and state["holding"] is None
    assert state["raw_input"] is None


def test_vendor_exception_is_unavailable():
    adapter = SimpleNamespace(connected=True, controller_io_inputs=Mock(side_effect=RuntimeError("timeout")))
    state = VacuumSensorConfig(True).sample(adapter, "r", 0)
    assert state["available"] is False and "timeout" in state["message"]


@pytest.mark.parametrize(("inputs", "holding"), [
    ([1, 0, 0, 0], False),
    ([0, 0, 0, 0], True),
])
def test_tool_power_gated_sensor_uses_npn_state_only_at_24v(inputs, holding):
    adapter = SimpleNamespace(
        connected=True,
        controller_io_inputs=Mock(return_value=(0, inputs)),
        tool_voltage=Mock(return_value=(0, 3)),
    )

    state = VacuumSensorConfig(True, 1, 0, requires_tool_power=True).sample(adapter, "r", 123)

    assert state["available"] is True
    assert state["holding"] is holding
    assert state["raw_input"] == inputs[0]
    assert state["tool_power_voltage_v"] == 24


@pytest.mark.parametrize("voltage", [(0, 0), (0, 1), (0, 2)])
def test_unpowered_sensor_does_not_report_npn_low_as_held(voltage):
    adapter = SimpleNamespace(
        connected=True,
        controller_io_inputs=Mock(return_value=(0, [0, 1, 1, 1])),
        tool_voltage=Mock(return_value=voltage),
    )

    state = VacuumSensorConfig(True, 1, 0, requires_tool_power=True).sample(adapter, "r", 123)

    assert state["available"] is False
    assert state["holding"] is None
    assert state["raw_input"] == 0
    assert state["tool_power_available"] is True
    assert "24 V 未开启" in state["message"]


def test_sensor_power_read_failure_never_claims_holding():
    adapter = SimpleNamespace(
        connected=True,
        controller_io_inputs=Mock(return_value=(0, [0, 1, 1, 1])),
        tool_voltage=Mock(return_value=(-2, None)),
    )

    state = VacuumSensorConfig(True, 1, 0, requires_tool_power=True).sample(adapter, "r", 123)

    assert state["available"] is False
    assert state["holding"] is None
    assert state["tool_power_available"] is False
    assert state["tool_power_api2_status"] == -2


@pytest.mark.parametrize("values", [
    {"enabled": "false"}, {"io": 0}, {"io": 5}, {"io": True}, {"requires_tool_power": "yes"},
    {"active_level": 2}, {"active_level": False}, {"enabeld": True},
])
def test_invalid_sensor_config_rejected(tmp_path, values):
    path = tmp_path / "sensors.yaml"
    path.write_text(yaml.safe_dump({"arms": {"r": values}}))
    with pytest.raises(ValueError):
        VacuumSensorConfig.from_yaml(str(path), "r")


@pytest.mark.parametrize("rate", [0, -1, 21, float("nan"), float("inf"), True])
def test_invalid_sample_rate_rejected(tmp_path, rate):
    path = tmp_path / "sensors.yaml"
    path.write_text(yaml.safe_dump({"arms": {}, "publish_rate_hz": rate}))
    with pytest.raises(ValueError):
        VacuumSensorConfig.from_yaml(str(path), "r")
