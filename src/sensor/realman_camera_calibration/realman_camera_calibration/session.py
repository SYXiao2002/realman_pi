"""Immutable single-arm session identities, independent of ROS."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def content_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def session_identity(config: dict[str, Any], arm: str) -> dict[str, Any]:
    if arm not in config["robots"] or arm not in ("l", "r"):
        raise ValueError("calibration arm_id must name a configured l or r arm")
    robot = config["robots"][arm]
    camera_id = robot["camera_id"]
    camera = config["cameras"][camera_id]
    return {
        "arm_id": arm,
        "camera_id": camera_id,
        "serial": camera["serial"],
        "mount_id": camera["mount_id"],
        "base_frame": robot["base_frame"],
        "flange_frame": robot["end_effector_frame"],
        "optical_frame": camera["optical_frame"],
        "board_sha256": content_hash(config["board"]),
    }


def write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / ".session.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(directory / "session.json")


def restore_session(
    directory: Path, config: dict[str, Any], arm: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Reject legacy joint sessions and changed hardware/board/intrinsic identity."""
    path = directory / "session.json"
    if not path.is_file():
        raise ValueError("legacy session has no single-arm identity; start a new session")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("session manifest must be an object")
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported calibration session schema")
    if manifest.get("identity") != session_identity(config, arm):
        raise ValueError("session arm, camera installation, frames or board identity changed")
    intrinsics = manifest.get("intrinsics")
    intrinsics_hash = manifest.get("intrinsics_sha256")
    if intrinsics is not None or intrinsics_hash is not None:
        if not isinstance(intrinsics, dict) or intrinsics_hash != content_hash(intrinsics):
            raise ValueError("session camera intrinsics hash is invalid")
    samples = []
    for metadata in sorted(directory.glob("batches/*/*.json")):
        if any(part.startswith(".") for part in metadata.relative_to(directory).parts):
            continue
        sample = json.loads(metadata.read_text(encoding="utf-8"))
        if not isinstance(sample, dict):
            raise ValueError("sample metadata must be an object")
        if intrinsics_hash is None:
            raise ValueError("accepted samples require frozen camera intrinsics")
        if sample.get("arm_id") != arm or sample.get("identity") != manifest["identity"]:
            raise ValueError("session contains a sample from a different arm or installation")
        if sample.get("intrinsics_sha256") != manifest.get("intrinsics_sha256"):
            raise ValueError("session contains incompatible camera intrinsics")
        for key in ("base_to_tool", "target_to_camera"):
            matrix = sample.get(key)
            if not isinstance(matrix, list) or len(matrix) != 4 or any(
                not isinstance(row, list) or len(row) != 4 for row in matrix
            ):
                raise ValueError(f"sample has invalid {key}")
        samples.append(sample)
    return manifest, samples
