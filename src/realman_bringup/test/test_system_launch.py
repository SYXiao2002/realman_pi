from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from launch import LaunchDescription, LaunchService
from launch.actions import SetLaunchConfiguration


def test_disabled_system_launch_expands_without_starting_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("REALMAN_LOG_ROOT", str(tmp_path / "logs"))
    launch_path = Path(__file__).parents[1] / "launch" / "system.launch.py"
    spec = spec_from_file_location("realman_system_launch", launch_path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)

    launch_description = module.generate_launch_description()
    disabled_config = [
        SetLaunchConfiguration(name, "false")
        for name in (
            "start_robots",
            "start_driver",
            "start_joy_driver",
            "start_controller",
            "use_gui",
            "use_rviz",
            "start_web_control",
            "start_bt_launcher",
            "start_camera_calibration",
            "start_grasp_bridge",
        )
    ]
    service = LaunchService()
    service.include_launch_description(
        LaunchDescription([*disabled_config, *launch_description.entities])
    )

    assert service.run() == 0


def test_installed_grasp_configuration_loads_without_environment_override(monkeypatch):
    from ament_index_python.packages import get_package_share_directory
    from grasp_bridge.profiles import load_profile

    monkeypatch.delenv("REALMAN_CONFIG_ROOT", raising=False)
    config_root = Path(get_package_share_directory("realman_bringup")) / "config"
    profile = load_profile(config_root, "right_wrist")
    assert profile["arm"] == "r"
    assert profile["gripper_geometry"]["verified"] is False
