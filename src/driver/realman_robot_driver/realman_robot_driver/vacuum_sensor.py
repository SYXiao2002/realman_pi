"""Configuration and read-only controller input sampling for a suction sensor."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class VacuumSensorConfig:
    enabled: bool = False
    io: int = 3
    active_level: int = 0
    publish_rate_hz: float = 5.0

    @classmethod
    def from_yaml(cls, path: str, arm: str) -> "VacuumSensorConfig":
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("arms"), dict):
            raise ValueError("vacuum sensor configuration must contain an arms mapping")
        if arm not in {"l", "m", "r"} or set(document["arms"]) - {"l", "m", "r"}:
            raise ValueError("vacuum sensor arms must be l, m or r")
        rate = document.get("publish_rate_hz", 5.0)
        if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 < rate <= 20:
            raise ValueError("vacuum publish_rate_hz must be positive and at most 20")
        values = document["arms"].get(arm, {})
        if not isinstance(values, dict) or set(values) - {"enabled", "io", "active_level"}:
            raise ValueError(f"invalid vacuum sensor configuration for {arm}")
        enabled = values.get("enabled", False)
        io = values.get("io", 3)
        active_level = values.get("active_level", 0)
        if type(enabled) is not bool:
            raise ValueError("vacuum enabled must be a boolean")
        if type(io) is not int or not 1 <= io <= 4:
            raise ValueError("vacuum io must be a controller input number from 1 to 4")
        if type(active_level) is not int or active_level not in (0, 1):
            raise ValueError("vacuum active_level must be 0 or 1")
        return cls(enabled, io, active_level, float(rate))

    def sample(self, adapter: Any, arm: str, stamp_ns: int) -> dict[str, Any]:
        event = {
            "type": "vacuum_state", "arm": arm, "enabled": self.enabled,
            "io": self.io, "active_level": self.active_level,
            "available": False, "holding": None, "raw_input": None,
            "api2_status": None, "stamp_ns": stamp_ns,
            "stale_after_sec": max(2.0, 3.0 / self.publish_rate_hz),
            "message": "未启用",
        }
        if not self.enabled:
            return event
        if not adapter.connected:
            event["message"] = "机械臂未连接"
            return event
        try:
            status, inputs = adapter.controller_io_inputs()
            event["api2_status"] = status
            if status != 0:
                event["message"] = f"IO 读取失败（API2 {status}）"
                return event
            raw = inputs[self.io - 1]
            # The SDK returns -1 when the port is not configured as an input.
            if type(raw) is not int or raw not in (0, 1):
                event["message"] = "IO 非输入模式或读数无效"
                return event
        except Exception as error:
            event["message"] = f"IO 读取失败：{error}"
            return event
        holding = raw == self.active_level
        event.update(available=True, holding=holding, raw_input=raw,
                     message="已吸附" if holding else "未吸附")
        return event
