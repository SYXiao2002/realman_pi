"""Shared configuration and argv construction for the host Orbbec launchers."""

import math
import os
import re
from pathlib import Path

import yaml


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate namespace/parameter keys instead of silently losing one."""


def _unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"Duplicate camera configuration key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _deep_merge(base, override):
    merged = dict(base)
    for key, value in override.items():
        merged[key] = (
            _deep_merge(merged[key], value)
            if isinstance(value, dict) and isinstance(merged.get(key), dict)
            else value
        )
    return merged


def base_config_path():
    if os.environ.get("REALMAN_CONFIG_ROOT"):
        return Path(os.environ["REALMAN_CONFIG_ROOT"]) / "ros/cameras_ros2.yaml"
    source_config = Path(__file__).resolve().parents[3] / "config/ros/cameras_ros2.yaml"
    if source_config.is_file():
        return source_config
    from ament_index_python.packages import get_package_share_directory

    return Path(get_package_share_directory("sensor_bringup")) / "config/cameras_ros2.yaml"


def _read_yaml(path):
    with path.open(encoding="utf-8") as stream:
        result = yaml.load(stream, Loader=UniqueKeyLoader)
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise ValueError(f"Camera configuration must be a mapping: {path}")
    return result


def load_config(base_path=None):
    base_path = Path(base_path) if base_path is not None else base_config_path()
    config = _read_yaml(base_path)
    explicit = os.environ.get("RM65_CAMERAS_LOCAL_CONFIG")
    local_path = Path(explicit) if explicit else base_path.resolve().with_suffix(".local.yaml")
    if explicit or local_path.is_file():
        config = _deep_merge(config, _read_yaml(local_path))
    validate_config(config)
    return config


def _mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _parameters(value, label):
    for key, parameter in _mapping(value, label).items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", key):
            raise ValueError(f"Invalid launch parameter in {label}: {key}")
        if not isinstance(parameter, (str, bool, int, float)) or (
            isinstance(parameter, float) and not math.isfinite(parameter)
        ):
            raise ValueError(f"{label}.{key} must be a finite scalar")
        if key == "enable_depth" and not isinstance(parameter, bool):
            raise ValueError(f"{label}.enable_depth must be true or false")


def validate_config(config):
    _mapping(config, "camera configuration")
    if "wrist_cameras" in config or "global_camera" in config:
        raise ValueError("Old wrist_cameras/global_camera schema: migrate to cameras.<namespace>.serial/launch_file")
    unknown = set(config) - {"cameras", "defaults", "launch"}
    if unknown:
        raise ValueError(f"Unknown camera configuration keys: {sorted(unknown)}")
    _parameters(config.get("defaults", {}), "defaults")
    launch = _mapping(config.get("launch", {}), "launch")
    if set(launch) - {"stagger_seconds"}:
        raise ValueError("launch only supports stagger_seconds")
    stagger = launch.get("stagger_seconds", 2.0)
    if isinstance(stagger, bool) or not isinstance(stagger, (int, float)) or not math.isfinite(stagger) or stagger < 0:
        raise ValueError("launch.stagger_seconds must be a finite nonnegative number")
    cameras = _mapping(config.get("cameras"), "cameras")
    serials = set()
    for namespace, camera in cameras.items():
        if not isinstance(namespace, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", namespace):
            raise ValueError(f"Invalid camera namespace: {namespace}")
        _mapping(camera, namespace)
        if set(camera) - {"serial", "launch_file", "enabled", "parameters"}:
            raise ValueError(f"Unknown fields in cameras.{namespace}")
        if not isinstance(camera.get("enabled", True), bool):
            raise ValueError(f"{namespace}.enabled must be true or false")
        serial = camera.get("serial")
        if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", serial):
            raise ValueError(f"{namespace}.serial must be a nonempty serial string")
        launch_file = camera.get("launch_file")
        if not isinstance(launch_file, str) or not re.fullmatch(r"[A-Za-z0-9_]+\.launch\.py", launch_file):
            raise ValueError(f"{namespace}.launch_file must be an official launch filename")
        _parameters(camera.get("parameters", {}), f"cameras.{namespace}.parameters")
        if camera.get("enabled", True):
            if serial in serials:
                raise ValueError(f"Duplicate enabled camera serial: {serial}")
            serials.add(serial)
    if not serials:
        raise ValueError("At least one camera must be enabled")


def enabled_cameras(config):
    return {name: camera for name, camera in config["cameras"].items() if camera.get("enabled", True)}


def camera_commands(config, mode="color"):
    validate_config(config)
    if mode not in {"color", "depth"}:
        raise ValueError("Camera mode must be color or depth")
    cameras = enabled_cameras(config)
    commands = []
    for namespace, camera in cameras.items():
        parameters = {**config.get("defaults", {}), **camera.get("parameters", {})}
        # Keep RGB in color mode; depth is opt-in per camera. The explicit depth
        # mode remains depth-only for all enabled cameras.
        parameters.update(
            camera_name=namespace, serial_number=camera["serial"], device_num=len(cameras),
            enable_color=mode == "color", enable_depth=mode == "depth" or parameters.get("enable_depth", False),
            enable_left_ir=False, enable_right_ir=False, enable_accel=False, enable_gyro=False,
            enable_point_cloud=False, enable_colored_point_cloud=False,
            enable_frame_sync=False, trigger_out_enabled=False, software_trigger_enabled=False,
            sync_mode="standalone",
        )
        argv = ["ros2", "launch", "orbbec_camera", camera["launch_file"]]
        argv.extend(f"{key}:={str(value).lower() if isinstance(value, bool) else value}" for key, value in parameters.items())
        commands.append((namespace, argv))
    return commands


def topic_map(config, mode="color"):
    if mode not in {"color", "depth"}:
        raise ValueError("Camera mode must be color or depth")
    return {name: f"/{name}/{mode}/image_raw" for name in enabled_cameras(config)}


def required_topic_map(config, mode="color"):
    """Wait for each requested stream, including per-camera depth alongside RGB."""
    topics = topic_map(config, mode)
    if mode == "color":
        for name, camera in enabled_cameras(config).items():
            parameters = {**config.get("defaults", {}), **camera.get("parameters", {})}
            if parameters.get("enable_depth", False):
                topics[f"{name}/depth"] = f"/{name}/depth/image_raw"
    return topics
