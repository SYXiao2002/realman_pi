"""Verify hardware selection without executing any driver process."""

import importlib.util
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[2]
CONFIG_ROOT = REPOSITORY_ROOT / "config" / "ros"


@pytest.fixture
def launch_module():
    pytest.importorskip("ament_index_python")
    pytest.importorskip("launch_ros")
    spec = importlib.util.spec_from_file_location(
        "multi_driver_launch", PACKAGE_ROOT / "launch" / "three_realman_drivers.launch.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_production_omits_missing_middle_arm(launch_module):
    assert launch_module._configured_namespaces(CONFIG_ROOT / "realman_driver.yaml") == (
        "l",
        "r",
    )


def test_offline_mock_keeps_three_arm_support(launch_module):
    assert launch_module._configured_namespaces(CONFIG_ROOT / "realman_driver_mock.yaml") == (
        "l",
        "m",
        "r",
    )


def test_launch_builds_only_present_driver_sections(launch_module, monkeypatch, tmp_path):
    from launch import LaunchContext

    config = tmp_path / "drivers.yaml"
    config.write_text(
        "/r/realman_driver:\n  ros__parameters: {mock_mode: true}\n"
        "/l/realman_driver:\n  ros__parameters: {mock_mode: true}\n",
        encoding="utf-8",
    )
    context = LaunchContext()
    context.launch_configurations.update(
        {
            "config_file": str(config),
            "coordinates_config_file": str(CONFIG_ROOT / "realman_coordinates.yaml"),
            "motion_config_file": str(CONFIG_ROOT / "realman_motion.yaml"),
        }
    )
    # Capture launch descriptions; never execute hardware drivers in this test.
    monkeypatch.setattr(launch_module, "Node", lambda **kwargs: kwargs)

    nodes = launch_module._launch_nodes(context)

    assert [node["namespace"] for node in nodes] == ["l", "r"]
    assert all(node["parameters"][0] == str(config) for node in nodes)
    assert all(node["executable"] == "realman_driver_node" for node in nodes)


@pytest.mark.parametrize(
    "document",
    ["", "[]", "{}", "/x/realman_driver:\n  ros__parameters: {}\n"],
)
def test_rejects_configuration_without_supported_drivers(launch_module, tmp_path, document):
    config = tmp_path / "drivers.yaml"
    config.write_text(document, encoding="utf-8")

    with pytest.raises(ValueError, match="Driver configuration"):
        launch_module._configured_namespaces(config)


@pytest.mark.parametrize("section", ["null", "[]", "{}", "{ros__parameters: []}"])
def test_rejects_malformed_driver_section(launch_module, tmp_path, section):
    config = tmp_path / "drivers.yaml"
    config.write_text(f"/l/realman_driver: {section}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="/l/realman_driver.*ros__parameters mapping"):
        launch_module._configured_namespaces(config)
