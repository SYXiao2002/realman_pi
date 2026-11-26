from glob import glob

from setuptools import setup

setup(
    name="pose6d_bridge", version="0.1.0", packages=["pose6d", "pose6d_bridge"],
    data_files=[("share/ament_index/resource_index/packages", ["resource/pose6d_bridge"]),
                ("share/pose6d_bridge", ["package.xml"]),
                ("share/pose6d_bridge/launch", glob("launch/*.launch.py"))],
    install_requires=["setuptools", "numpy", "websockets>=14", "msgpack"], tests_require=["pytest"], zip_safe=True,
    maintainer="RealMan maintainers", maintainer_email="maintainer@example.com",
    description="Frozen RGB-D observations and FoundationPose bridge", license="BSD-3-Clause",
    entry_points={"console_scripts": ["pose6d_bridge_node = pose6d_bridge.node:main"]},
)
