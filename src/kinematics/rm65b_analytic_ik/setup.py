from setuptools import setup

# ament requires this entry point here; model configuration lives at workspace root.
setup(
    name="rm65b_analytic_ik", version="0.1.0", packages=["rm65b_analytic_ik"],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/rm65b_analytic_ik"]),
        ("share/rm65b_analytic_ik", ["package.xml", "UPSTREAM.md"]),
        ("share/rm65b_analytic_ik/config", ["resource/rm65b_analytic_ik.yaml"]),
    ],
    install_requires=["setuptools", "numpy>=1.23", "PyYAML>=6.0"],
    tests_require=["pytest"], zip_safe=True,
    maintainer="RealMan maintainers", maintainer_email="maintainer@example.com",
    description="RM65-B analytic IK ROS service", license="Proprietary",
    entry_points={"console_scripts": ["analytic_ik_node = rm65b_analytic_ik.ros_node:main"]},
)
