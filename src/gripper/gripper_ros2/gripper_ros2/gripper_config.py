"""Configuration validation and stable ROS interface naming for grippers."""

from __future__ import annotations

import math
from pathlib import Path
import re

import yaml


NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
PROTOCOLS = ("changingtek", "binary_42d")
SERVICE_SUFFIXES = (
    "open", "close", "reset", "enable", "grasp_check", "percentage", "calibrate",
    "set_limits", "move_raw",
)
TOPIC_SUFFIXES = ("position", "speed", "current", "torque_reached", "alarm", "connected", "limits")
COMMAND_SUFFIXES = ("percentage/command",)


def interface_names(name: str) -> dict[str, str]:
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError(f"invalid gripper name: {name!r}")
    interfaces = {suffix: f"/{name}/{suffix}" for suffix in SERVICE_SUFFIXES + TOPIC_SUFFIXES}
    interfaces["percentage_command"] = f"/{name}/{COMMAND_SUFFIXES[-1]}"
    return interfaces


def percentage_to_position(percentage: float, *, open_position: int,
                           close_position: int) -> int:
    value = float(percentage)
    if not 0.0 <= value <= 1.0:
        raise ValueError("percentage must be in 0.0..1.0")
    return int(round(close_position + value * (open_position - close_position)))


def load_gripper_config(path: str | Path) -> dict:
    path = Path(path)
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    admission = document.get("task_control")
    if admission is not None:
        if not isinstance(admission, dict):
            raise ValueError("task_control must be a mapping")
        timeout = admission.get("timeout_sec")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("task_control.timeout_sec must be positive and finite")
        arms = admission.get("arms")
        if not isinstance(arms, dict) or not arms or any(arm not in {"l", "m", "r"} for arm in arms.values()):
            raise ValueError("task_control.arms must map gripper names to l/m/r")
    buses = document.get("buses")
    if not isinstance(buses, list) or not buses:
        raise ValueError("gripper config must contain a non-empty buses list")
    names, ports = set(), set()
    for bus in buses:
        if not isinstance(bus, dict) or not isinstance(bus.get("port"), str) or not bus["port"]:
            raise ValueError("each gripper bus requires a non-empty port")
        if bus["port"] in ports:
            raise ValueError(f"duplicate gripper bus port: {bus['port']}")
        ports.add(bus["port"])
        protocol = bus.get("protocol", "changingtek")
        if protocol not in PROTOCOLS:
            raise ValueError(f"unsupported gripper protocol: {protocol!r}")
        if protocol == "binary_42d":
            threshold = bus.get("binary_threshold", 0.5)
            hysteresis = bus.get("binary_hysteresis", 0.05)
            interval = bus.get("inter_transaction_s", 0.1)
            for field, value in (("binary_threshold", threshold),
                                 ("binary_hysteresis", hysteresis),
                                 ("inter_transaction_s", interval)):
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value)):
                    raise ValueError(f"{field} must be a finite number")
            if not 0 < threshold < 1:
                raise ValueError("binary_threshold must be in (0, 1)")
            if not 0 <= hysteresis < min(threshold, 1 - threshold):
                raise ValueError("binary_hysteresis must fit inside the threshold range")
            if interval < 0.1:
                raise ValueError("42D inter_transaction_s must be at least 0.1 seconds")
        grippers = bus.get("grippers")
        if not isinstance(grippers, list) or not grippers:
            raise ValueError(f"gripper bus {bus['port']} requires at least one gripper")
        slave_ids = set()
        for gripper in grippers:
            if not isinstance(gripper, dict):
                raise ValueError("each gripper must be a mapping")
            name = gripper.get("name")
            if not isinstance(name, str):
                raise ValueError("each gripper requires a name")
            interface_names(name)
            if name in names:
                raise ValueError(f"duplicate gripper name: {name}")
            names.add(name)
            slave_id = gripper.get("slave_id")
            if isinstance(slave_id, bool) or not isinstance(slave_id, int) or not 1 <= slave_id <= 247:
                raise ValueError(f"{name}.slave_id must be in 1..247")
            if slave_id in slave_ids:
                raise ValueError(f"duplicate slave_id {slave_id} on {bus['port']}")
            slave_ids.add(slave_id)
            for field in ("min_position", "max_position", "open_position", "close_position"):
                if isinstance(gripper.get(field), bool) or not isinstance(gripper.get(field), int):
                    raise ValueError(f"{name}.{field} must be an integer")
            minimum, maximum = gripper["min_position"], gripper["max_position"]
            if minimum > maximum:
                raise ValueError(f"{name}.min_position must not exceed max_position")
            if not minimum <= gripper["open_position"] <= maximum:
                raise ValueError(f"{name}.open_position is outside configured limits")
            if not minimum <= gripper["close_position"] <= maximum:
                raise ValueError(f"{name}.close_position is outside configured limits")
            if protocol == "binary_42d" and gripper["open_position"] >= gripper["close_position"]:
                raise ValueError(f"{name}.open_position must be smaller than close_position")
    if admission is not None and set(admission["arms"]) != names:
        raise ValueError("task_control.arms must cover every configured gripper exactly")
    return document
