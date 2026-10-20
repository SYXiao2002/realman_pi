"""Production camera contracts without starting ROS camera drivers or USB streams."""

import importlib.util
from pathlib import Path
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "src/sensor_bringup/scripts"
BASE_CONFIG = ROOT / "config/ros/cameras_ros2.yaml"
HARDWARE = {
    "camera_global": ("CP28563000CK", "gemini_330_series.launch.py"),
    "camera_global2": ("CP28563000CV", "gemini_330_series.launch.py"),
    "camera_left": ("CV2856D0008H", "gemini_301_series.launch.py"),
    "camera_right": ("CV2856D0002E", "gemini_301_series.launch.py"),
}


def _import_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def camera_config():
    sys.path.insert(0, str(SCRIPTS))
    try:
        yield _import_script("camera_config")
    finally:
        sys.path.remove(str(SCRIPTS))


@pytest.fixture
def base_path(tmp_path, monkeypatch):
    # Site-local settings must not change the checked-in production contract.
    monkeypatch.delenv("RM65_CAMERAS_LOCAL_CONFIG", raising=False)
    path = tmp_path / "cameras_ros2.yaml"
    path.write_text(BASE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return path


@pytest.fixture
def config(camera_config, base_path):
    return camera_config.load_config(base_path)


def _write_yaml(path, config):
    path.write_text(yaml.safe_dump(config), encoding="utf-8")


def _arguments(argv):
    return dict(word.split(":=", 1) for word in argv[4:] if ":=" in word)


def test_committed_hardware_map_uses_binary_package_launches(config, camera_config):
    commands = dict(camera_config.camera_commands(config))
    assert set(commands) == set(HARDWARE)
    for namespace, (serial, launch_file) in HARDWARE.items():
        argv = commands[namespace]
        assert argv[:4] == ["ros2", "launch", "orbbec_camera", launch_file]
        args = _arguments(argv)
        assert args["camera_name"] == namespace
        assert args["serial_number"] == serial
        assert args["device_num"] == "4"


def test_production_mode_preserves_all_rgb_and_enables_requested_depth(config, camera_config):
    depth_cameras = {"camera_global", "camera_right"}
    for namespace, argv in camera_config.camera_commands(config):
        args = _arguments(argv)
        assert args["enable_color"] == "true"
        assert args["enable_depth"] == str(namespace in depth_cameras).lower()
        for flag in (
            "enable_left_ir", "enable_right_ir", "enable_accel",
            "enable_gyro", "enable_point_cloud", "enable_colored_point_cloud",
            "enable_frame_sync", "trigger_out_enabled", "software_trigger_enabled",
        ):
            assert args[flag] == "false", flag
        assert args["sync_mode"] == "standalone"


def test_topics_and_depth_mode_follow_each_namespace(config, camera_config):
    assert camera_config.topic_map(config) == {
        namespace: f"/{namespace}/color/image_raw" for namespace in HARDWARE
    }
    assert camera_config.topic_map(config, mode="depth") == {
        namespace: f"/{namespace}/depth/image_raw" for namespace in HARDWARE
    }
    for _, argv in camera_config.camera_commands(config, mode="depth"):
        args = _arguments(argv)
        assert args["enable_color"] == "false"
        assert args["enable_depth"] == "true"


def test_readiness_requires_rgb_and_each_requested_depth_stream(config, camera_config):
    rgb_topics = {namespace: f"/{namespace}/color/image_raw" for namespace in HARDWARE}
    assert camera_config.required_topic_map(config) == {
        **rgb_topics,
        "camera_global/depth": "/camera_global/depth/image_raw",
        "camera_right/depth": "/camera_right/depth/image_raw",
    }
    assert camera_config.required_topic_map(config, mode="depth") == {
        namespace: f"/{namespace}/depth/image_raw" for namespace in HARDWARE
    }


def test_disabling_camera_updates_launch_count_and_health_topics(config, camera_config):
    config["cameras"]["camera_global"]["enabled"] = False
    commands = dict(camera_config.camera_commands(config))
    assert set(commands) == set(HARDWARE) - {"camera_global"}
    assert set(camera_config.topic_map(config)) == set(commands)
    assert set(camera_config.required_topic_map(config)) == set(commands) | {"camera_right/depth"}
    assert all(_arguments(argv)["device_num"] == "3" for argv in commands.values())


def test_per_camera_stream_override_preserves_shared_defaults(config, camera_config):
    config["defaults"]["color_width"] = 640
    config["defaults"]["color_fps"] = 30
    config["cameras"]["camera_left"]["parameters"] = {"color_fps": 15}
    commands = dict(camera_config.camera_commands(config))
    assert _arguments(commands["camera_left"])["color_fps"] == "15"
    assert _arguments(commands["camera_right"])["color_fps"] == "30"
    assert all(_arguments(argv)["color_width"] == "640" for argv in commands.values())


def test_missing_depth_selection_keeps_rgb_only(config, camera_config):
    config["defaults"].pop("enable_depth", None)
    for camera in config["cameras"].values():
        camera.get("parameters", {}).pop("enable_depth", None)
    for _, argv in camera_config.camera_commands(config):
        assert _arguments(argv)["enable_color"] == "true"
        assert _arguments(argv)["enable_depth"] == "false"
    assert camera_config.required_topic_map(config) == camera_config.topic_map(config)


def test_per_camera_depth_overrides_shared_selection_and_profile(config, camera_config):
    config["defaults"].update(enable_depth=True, depth_width=640, depth_height=480, depth_fps=30)
    for camera in config["cameras"].values():
        camera.get("parameters", {}).pop("enable_depth", None)
    config["cameras"]["camera_left"].setdefault("parameters", {})["enable_depth"] = False
    config["cameras"]["camera_right"].setdefault("parameters", {})["depth_fps"] = 15
    commands = dict(camera_config.camera_commands(config))
    assert _arguments(commands["camera_left"])["enable_depth"] == "false"
    assert _arguments(commands["camera_right"])["depth_fps"] == "15"
    for namespace, argv in commands.items():
        args = _arguments(argv)
        assert args["enable_color"] == "true"
        assert args["enable_depth"] == str(namespace != "camera_left").lower()
        assert args["depth_width"] == "640"
        assert args["depth_height"] == "480"
        if namespace != "camera_right":
            assert args["depth_fps"] == "30"
    assert set(camera_config.required_topic_map(config)) == set(HARDWARE) | {
        f"{namespace}/depth" for namespace in HARDWARE if namespace != "camera_left"
    }
    # The explicit depth diagnostic mode still opens every device as depth-only.
    for _, argv in camera_config.camera_commands(config, mode="depth"):
        assert _arguments(argv)["enable_color"] == "false"
        assert _arguments(argv)["enable_depth"] == "true"


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
@pytest.mark.parametrize("scope", ["defaults", "camera"])
def test_depth_selection_requires_yaml_boolean(config, camera_config, base_path, scope, value):
    parameters = (
        config["defaults"] if scope == "defaults"
        else config["cameras"]["camera_left"].setdefault("parameters", {})
    )
    parameters["enable_depth"] = value
    _write_yaml(base_path, config)
    with pytest.raises(ValueError):
        camera_config.load_config(base_path)


@pytest.mark.parametrize("key,value,expected", [
    ("camera_name", "hijacked", "camera_left"),
    ("serial_number", "WRONG", "CV2856D0008H"),
    ("device_num", 99, "4"),
    ("enable_color", False, "true"),
    ("enable_left_ir", True, "false"),
    ("enable_point_cloud", True, "false"),
    ("software_trigger_enabled", True, "false"),
])
def test_parameter_overrides_cannot_rebind_or_change_requested_stream(
    config, camera_config, key, value, expected
):
    config["cameras"]["camera_left"]["parameters"] = {key: value}
    try:
        commands = dict(camera_config.camera_commands(config))
    except ValueError:
        # Rejecting protected settings is also valid; silently applying them is not.
        return
    assert _arguments(commands["camera_left"])[key] == expected


def test_local_file_deep_merges_without_dropping_other_cameras(camera_config, base_path):
    _write_yaml(base_path.with_suffix(".local.yaml"), {
        "cameras": {"camera_left": {"parameters": {"color_fps": 15}}},
    })
    config = camera_config.load_config(base_path)
    commands = dict(camera_config.camera_commands(config))
    assert set(commands) == set(HARDWARE)
    assert _arguments(commands["camera_left"])["serial_number"] == "CV2856D0008H"
    assert _arguments(commands["camera_left"])["color_fps"] == "15"


def test_explicit_override_takes_precedence(camera_config, base_path, tmp_path, monkeypatch):
    _write_yaml(base_path.with_suffix(".local.yaml"), {"defaults": {"color_fps": 15}})
    override = tmp_path / "site.yaml"
    _write_yaml(override, {"defaults": {"color_fps": 10}})
    monkeypatch.setenv("RM65_CAMERAS_LOCAL_CONFIG", str(override))
    commands = dict(camera_config.camera_commands(camera_config.load_config(base_path)))
    assert all(_arguments(argv)["color_fps"] == "10" for argv in commands.values())


def test_missing_explicit_override_fails_instead_of_using_another_site_config(
    camera_config, base_path, tmp_path, monkeypatch
):
    monkeypatch.setenv("RM65_CAMERAS_LOCAL_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises((ValueError, FileNotFoundError)):
        camera_config.load_config(base_path)


def test_duplicate_serial_is_rejected(config, camera_config, base_path):
    config["cameras"]["camera_right"]["serial"] = config["cameras"]["camera_left"]["serial"]
    _write_yaml(base_path, config)
    with pytest.raises(ValueError):
        camera_config.load_config(base_path)


def test_duplicate_yaml_namespace_is_rejected(camera_config, base_path):
    base_path.write_text(
        "cameras:\n"
        "  camera_left: {serial: A, launch_file: gemini_301_series.launch.py}\n"
        "  camera_left: {serial: B, launch_file: gemini_301_series.launch.py}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        camera_config.load_config(base_path)


def test_invalid_namespace_is_rejected(config, camera_config, base_path):
    config["cameras"]["not/a/namespace"] = config["cameras"].pop("camera_left")
    _write_yaml(base_path, config)
    with pytest.raises(ValueError):
        camera_config.load_config(base_path)


def test_removed_camera_schema_fails_with_configuration_error(camera_config, base_path):
    _write_yaml(base_path, {"wrist_cameras": {"devices": {}}, "global_camera": {}})
    with pytest.raises(ValueError):
        camera_config.load_config(base_path)


def test_all_disabled_is_a_configuration_error(config, camera_config, base_path):
    for camera in config["cameras"].values():
        camera["enabled"] = False
    _write_yaml(base_path, config)
    with pytest.raises(ValueError):
        camera_config.load_config(base_path)


def test_host_entry_no_longer_requires_removed_vendor_workspaces():
    source = (ROOT / "functions.zsh").read_text(encoding="utf-8")
    helpers = source.split("_rm65_source_camera_ros2() {", 1)[1].split(
        "rm65_camera_ros2_logs() {", 1
    )[0]
    assert "src/sensor/OrbbecSDK_ROS2" not in helpers
    assert "src/sensor/realsense" not in helpers
    assert "zip_download_grasp_ros_flow" not in helpers
    assert "ros2 pkg prefix realsense2_camera" not in helpers
    assert "ros2 pkg prefix sensor_bringup" not in helpers
    assert "ros2 launch sensor_bringup cameras_ros2.launch.py" not in helpers


def test_health_subscriptions_accept_sensor_data_qos(camera_config, monkeypatch):
    pytest.importorskip("rclpy")
    from rclpy.node import Node
    from rclpy.qos import ReliabilityPolicy

    health = _import_script("check_cameras")
    calls = []
    monkeypatch.setattr(Node, "__init__", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(
        Node, "create_subscription",
        lambda self, msg_type, topic, callback, qos, **kwargs: calls.append((topic, qos)),
    )
    health.HealthCheck({"camera_left": "/camera_left/color/image_raw"})
    assert len(calls) == 1
    assert calls[0][0] == "/camera_left/color/image_raw"
    assert calls[0][1].reliability == ReliabilityPolicy.BEST_EFFORT


def test_supervisor_health_waits_for_requested_depth_too(config, camera_config, monkeypatch):
    pytest.importorskip("rclpy")
    health = _import_script("check_cameras")
    host = _import_script("host_cameras")
    calls = []
    monkeypatch.setattr(health, "check_topics", lambda topics, timeout: calls.append((topics, timeout)) or False)
    assert host._health(config, 42, "color") is False
    assert calls == [(camera_config.required_topic_map(config), 42)]
    assert {"camera_global/depth", "camera_right/depth"} <= set(calls[0][0])


def test_health_check_never_reports_empty_camera_set_as_success(camera_config, monkeypatch):
    pytest.importorskip("rclpy")
    health = _import_script("check_cameras")
    monkeypatch.setattr(health, "_load_config", lambda: {"cameras": {}})
    monkeypatch.setattr(health, "_enabled_topics", lambda *args, **kwargs: {})
    monkeypatch.setattr(sys, "argv", ["check_cameras.py", "0"])
    # An empty config must fail before any ROS node or DDS context is created.
    monkeypatch.setattr(health.rclpy, "init", lambda *args, **kwargs: pytest.fail("ROS initialized for empty camera set"))
    try:
        result = health.main()
    except SystemExit as error:
        result = error.code
    except ValueError:
        return
    assert result not in (None, 0)


def test_frame_sync_is_configurable_per_device_and_defaults_false(config, camera_config):
    config["defaults"].pop("enable_frame_sync", None)
    config["cameras"]["camera_right"]["parameters"]["enable_frame_sync"] = True
    commands = dict(camera_config.camera_commands(config))
    for name, argv in commands.items():
        args = _arguments(argv)
        assert args["enable_frame_sync"] == str(name == "camera_right").lower()
        assert args["sync_mode"] == "standalone"
    config["defaults"]["enable_frame_sync"] = True
    config["cameras"]["camera_left"]["parameters"]["enable_frame_sync"] = False
    commands = dict(camera_config.camera_commands(config))
    assert _arguments(commands["camera_left"])["enable_frame_sync"] == "false"
    assert _arguments(commands["camera_right"])["enable_frame_sync"] == "true"


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
@pytest.mark.parametrize("scope", ["defaults", "camera"])
def test_frame_sync_requires_yaml_boolean(config, camera_config, base_path, scope, value):
    params = config["defaults"] if scope == "defaults" else config["cameras"]["camera_right"]["parameters"]
    params["enable_frame_sync"] = value
    _write_yaml(base_path, config)
    with pytest.raises(ValueError):
        camera_config.load_config(base_path)
