"""Launch independent left/right wrist eye-in-hand calibration services."""

import os
from datetime import datetime
from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, SetEnvironmentVariable
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    if "REALMAN_CONFIG_ROOT" in os.environ:
        default_config = str(
            Path(os.environ["REALMAN_CONFIG_ROOT"])
            / "ros"
            / "camera_calibration.yaml"
        )
    else:
        default_config = str(Path.cwd() / "config" / "ros" / "camera_calibration.yaml")
    configured_log_dir = os.environ.get("ROS_LOG_DIR")
    if configured_log_dir:
        run_log_dir = Path(configured_log_dir)
    else:
        log_root = Path(os.environ.get("REALMAN_LOG_ROOT", str(Path.cwd() / "logs")))
        run_log_dir = log_root / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_log_dir.mkdir(parents=True, exist_ok=True)
    return LaunchDescription(
        [
            SetEnvironmentVariable("ROS_LOG_DIR", str(run_log_dir)),
            SetEnvironmentVariable("RCUTILS_COLORIZED_OUTPUT", "1"),
            DeclareLaunchArgument("config_file", default_value=default_config),
            DeclareLaunchArgument(
                "layout_config_file",
                default_value=str(Path(default_config).with_name("three_robots.yaml")),
                description="Authoritative layout YAML; explicitly selected left/right sessions update the right-arm base pose.",
            ),
            Node(
                package="realman_camera_calibration",
                executable="camera_calibration_node",
                name="camera_calibration",
                output="screen",
                parameters=[
                    {
                        "config_file": LaunchConfiguration("config_file"),
                        "layout_config_file": LaunchConfiguration("layout_config_file"),
                    }
                ],
            ),
        ]
    )
