"""Test configured camera-topic selection without opening ROS or USB devices."""

import importlib.util
from pathlib import Path
import sys

import pytest

pytest.importorskip("rclpy")
pytest.importorskip("sensor_msgs")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_cameras.py"


@pytest.fixture(scope="module")
def mod():
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec = importlib.util.spec_from_file_location("check_cameras", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.remove(str(SCRIPT.parent))


def _config():
    return {"cameras": {
        "camera_left": {"serial": "LEFT", "launch_file": "gemini_301_series.launch.py"},
        "camera_right": {"serial": "RIGHT", "launch_file": "gemini_301_series.launch.py"},
        "camera_global": {"serial": "BACK", "launch_file": "gemini_330_series.launch.py"},
        "camera_global2": {"serial": "FRONT", "launch_file": "gemini_330_series.launch.py"},
    }}


def test_enabled_topics_drops_disabled_camera(mod):
    config = _config()
    config["cameras"]["camera_left"]["enabled"] = False
    assert set(mod._enabled_topics(config)) == {"camera_right", "camera_global", "camera_global2"}


def test_enabled_topics_defaults_every_camera_on(mod):
    assert mod._enabled_topics(_config()) == {
        name: f"/{name}/color/image_raw"
        for name in ("camera_left", "camera_right", "camera_global", "camera_global2")
    }


def test_enabled_topics_respects_front_global_camera_off(mod):
    config = _config()
    config["cameras"]["camera_global2"]["enabled"] = False
    assert set(mod._enabled_topics(config)) == {"camera_left", "camera_right", "camera_global"}


def test_enabled_topics_includes_selected_depth_without_losing_rgb(mod):
    config = _config()
    config["cameras"]["camera_global"]["parameters"] = {"enable_depth": True}
    config["cameras"]["camera_right"]["parameters"] = {"enable_depth": True}
    assert mod._enabled_topics(config) == {
        **{name: f"/{name}/color/image_raw" for name in config["cameras"]},
        "camera_global/depth": "/camera_global/depth/image_raw",
        "camera_right/depth": "/camera_right/depth/image_raw",
    }


def test_enabled_topics_respects_depth_defaults_overrides_and_disabled_devices(mod):
    config = _config()
    config["defaults"] = {"enable_depth": True}
    config["cameras"]["camera_left"]["parameters"] = {"enable_depth": False}
    config["cameras"]["camera_global2"]["enabled"] = False
    assert set(mod._enabled_topics(config)) == {
        "camera_left", "camera_right", "camera_global",
        "camera_right/depth", "camera_global/depth",
    }


def test_explicit_depth_mode_keeps_all_enabled_devices_depth_only(mod):
    config = _config()
    config["cameras"]["camera_right"]["parameters"] = {"enable_depth": False}
    config["cameras"]["camera_global2"]["enabled"] = False
    assert mod._enabled_topics(config, mode="depth") == {
        name: f"/{name}/depth/image_raw"
        for name in ("camera_left", "camera_right", "camera_global")
    }
