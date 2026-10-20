"""Compatibility entry for the host's binary Orbbec camera launcher.

The hardware map and stream settings live in ``config/ros/cameras_ros2.yaml``.
The host script owns process supervision, logging and readiness checks, matching
``./rm65 up`` without including a second copy of the vendor launch graph here.
"""

import sys
from pathlib import Path

from ament_index_python.packages import get_package_prefix
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, OpaqueFunction
from launch.substitutions import LaunchConfiguration


def _host_cameras_script():
    """Prefer the checkout with symlink installs; support copied installs too."""
    source = Path(__file__).resolve().parent.parent / "scripts" / "host_cameras.py"
    if source.is_file():
        return source
    installed = (
        Path(get_package_prefix("sensor_bringup"))
        / "lib" / "sensor_bringup" / "host_cameras.py"
    )
    if installed.is_file():
        return installed
    raise FileNotFoundError(f"host_cameras.py not found at {source} or {installed}")


def _boolean_argument(context, name):
    value = LaunchConfiguration(name).perform(context).strip().lower()
    if value not in ("true", "false"):
        raise ValueError(f"{name} must be true or false, got {value!r}")
    return value == "true"


def _start_host_cameras(context):
    color = _boolean_argument(context, "enable_color")
    depth = _boolean_argument(context, "enable_depth")
    rviz = _boolean_argument(context, "use_rviz")
    if color == depth:
        raise ValueError("Enable exactly one stream: enable_color or enable_depth")
    command = [
        sys.executable, str(_host_cameras_script()), "start", "--mode",
        "color" if color else "depth",
    ]
    if rviz:
        command.append("--rviz")
    return [ExecuteProcess(cmd=command, name="host_cameras", output="screen")]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("enable_color", default_value="true"),
        DeclareLaunchArgument("enable_depth", default_value="false"),
        DeclareLaunchArgument("use_rviz", default_value="false"),
        OpaqueFunction(function=_start_host_cameras),
    ])
