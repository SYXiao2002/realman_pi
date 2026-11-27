"""Start pose review and its read-only analytic IK service together."""

from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        Node(package="pose6d_bridge", executable="pose6d_bridge_node", output="screen"),
    ])
