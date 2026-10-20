from glob import glob
from pathlib import Path

from setuptools import setup

# colcon requires relative data sources; root config remains authoritative.
config_root = Path("../../../config")
ros_config = ["grasp_profiles.yaml", "grasp_bridge.yaml", "camera_extrinsics.yaml",
              "realman_coordinates.yaml", "cameras_ros2.yaml", "gripper.yaml"]
setup(
    name="grasp_bridge", version="0.1.0", packages=["grasp_bridge", "grasp_protocol"],
    package_dir={"grasp_protocol": "../grasp_protocol"},
    data_files=[("share/ament_index/resource_index/packages", ["resource/grasp_bridge"]),
                ("share/grasp_bridge", ["package.xml"]),
                ("share/grasp_bridge/config/ros", [str(config_root / "ros" / name) for name in ros_config]),
                ("share/grasp_bridge/config/grasp", [str(config_root / "grasp/gripper_specs.yaml")]),
                ("share/grasp_bridge/launch", glob("launch/*.launch.py"))],
    install_requires=["setuptools", "msgpack>=1.0.5", "websockets>=11.0"], tests_require=["pytest"], zip_safe=True,
    maintainer="RealMan maintainers", maintainer_email="maintainer@example.com",
    description="Planning-only RGB-D WebSocket grasp bridge", license="BSD-3-Clause",
    entry_points={"console_scripts": ["grasp_bridge_node = grasp_bridge.node:main"]},
)
