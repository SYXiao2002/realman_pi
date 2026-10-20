"""Explicit planning-only entry; config dry-run is the default."""

import os
from datetime import datetime
from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, SetEnvironmentVariable
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from grasp_bridge.profiles import default_config_root


def generate_launch_description():
    root = str(default_config_root())
    logs = Path(os.environ.get("REALMAN_LOG_ROOT", Path.cwd() / "logs")) / datetime.now().strftime("%Y%m%d_%H%M%S")
    logs.mkdir(parents=True, exist_ok=True)
    return LaunchDescription([
        SetEnvironmentVariable("RCUTILS_COLORIZED_OUTPUT", "1"),
        SetEnvironmentVariable("ROS_LOG_DIR", str(logs)),
        DeclareLaunchArgument("config_root", default_value=root),
        DeclareLaunchArgument("profile", default_value="right_wrist"),
        DeclareLaunchArgument("dry_run", default_value="true"),
        Node(package="grasp_bridge", executable="grasp_bridge_node", name="grasp_bridge", output="screen",
             parameters=[{"config_root": LaunchConfiguration("config_root"),
                          "profile": LaunchConfiguration("profile"),
                          "dry_run": LaunchConfiguration("dry_run")}]),
    ])
