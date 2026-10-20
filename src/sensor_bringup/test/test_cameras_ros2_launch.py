"""Exercise the compatibility launch without starting ROS nodes or USB streams."""

import importlib.util
from pathlib import Path
import sys

import pytest

pytest.importorskip("launch")
pytest.importorskip("ament_index_python")

from launch import LaunchContext
from launch.actions import DeclareLaunchArgument, ExecuteProcess, OpaqueFunction
from launch.utilities import perform_substitutions


LAUNCH = Path(__file__).resolve().parents[1] / "launch" / "cameras_ros2.launch.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("cameras_ros2_launch", LAUNCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _context(color="true", depth="false", rviz="false"):
    context = LaunchContext()
    context.launch_configurations.update(
        enable_color=color, enable_depth=depth, use_rviz=rviz
    )
    return context


def _command(action, context):
    return [perform_substitutions(context, word) for word in action.cmd]


@pytest.mark.parametrize("mode", ["color", "depth"])
def test_launch_delegates_stream_to_host_supervisor(mod, monkeypatch, mode):
    script = Path("/test/host_cameras.py")
    monkeypatch.setattr(mod, "_host_cameras_script", lambda: script)
    context = _context(color=str(mode == "color").lower(), depth=str(mode == "depth").lower())
    actions = mod._start_host_cameras(context)
    assert len(actions) == 1
    assert isinstance(actions[0], ExecuteProcess)
    assert _command(actions[0], context) == [
        sys.executable, str(script), "start", "--mode", mode,
    ]


def test_rviz_remains_opt_in(mod, monkeypatch):
    monkeypatch.setattr(mod, "_host_cameras_script", lambda: Path("/test/host_cameras.py"))
    context = _context(rviz="true")
    assert _command(mod._start_host_cameras(context)[0], context)[-1] == "--rviz"


@pytest.mark.parametrize("color,depth", [("true", "true"), ("false", "false")])
def test_invalid_combined_streams_fail_before_starting_processes(mod, monkeypatch, color, depth):
    monkeypatch.setattr(
        mod, "_host_cameras_script",
        lambda: pytest.fail("resolved camera script before rejecting invalid stream mode"),
    )
    with pytest.raises(ValueError, match="exactly one stream"):
        mod._start_host_cameras(_context(color=color, depth=depth))


@pytest.mark.parametrize("argument", ["enable_color", "enable_depth", "use_rviz"])
def test_invalid_boolean_is_reported(mod, argument):
    context = _context()
    context.launch_configurations[argument] = "typo"
    with pytest.raises(ValueError, match=argument):
        mod._start_host_cameras(context)


def test_source_script_wins_over_installed_copy(mod, monkeypatch, tmp_path):
    launch = tmp_path / "source" / "launch" / "cameras_ros2.launch.py"
    script = launch.parent.parent / "scripts" / "host_cameras.py"
    script.parent.mkdir(parents=True)
    script.touch()
    monkeypatch.setattr(mod, "__file__", str(launch))
    monkeypatch.setattr(
        mod, "get_package_prefix",
        lambda _: pytest.fail("looked for installed script when source script exists"),
    )
    assert mod._host_cameras_script() == script


def test_copied_install_falls_back_to_package_prefix(mod, monkeypatch, tmp_path):
    script = tmp_path / "installed" / "lib" / "sensor_bringup" / "host_cameras.py"
    script.parent.mkdir(parents=True)
    script.touch()
    monkeypatch.setattr(mod, "__file__", str(tmp_path / "share/launch/cameras_ros2.launch.py"))
    monkeypatch.setattr(mod, "get_package_prefix", lambda _: str(tmp_path / "installed"))
    assert mod._host_cameras_script() == script


def test_missing_supervisor_is_reported(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "__file__", str(tmp_path / "share/launch/cameras_ros2.launch.py"))
    monkeypatch.setattr(mod, "get_package_prefix", lambda _: str(tmp_path / "installed"))
    with pytest.raises(FileNotFoundError, match="host_cameras.py not found"):
        mod._host_cameras_script()


def test_launch_defaults_are_rgb_without_rviz(mod):
    entities = mod.generate_launch_description().entities
    arguments = {
        entity.name: entity for entity in entities
        if isinstance(entity, DeclareLaunchArgument)
    }
    context = LaunchContext()
    assert {
        name: perform_substitutions(context, argument.default_value)
        for name, argument in arguments.items()
    } == {"enable_color": "true", "enable_depth": "false", "use_rviz": "false"}
    assert sum(isinstance(entity, OpaqueFunction) for entity in entities) == 1
