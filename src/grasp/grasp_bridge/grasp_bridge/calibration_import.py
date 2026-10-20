"""Stage one solved hand-eye artifact; physical approval remains explicit."""

import argparse
import json
from pathlib import Path

import yaml

from .geometry import transform
from .profiles import digest, load_profile


def stage(config_root, profile_name, result_file):
    root = Path(config_root)
    profile = load_profile(root, profile_name)
    source = Path(result_file).resolve()
    solved = json.loads(source.read_text(encoding="utf-8"))
    identity = solved.get("identity", {})
    expected = {"arm_id": profile["arm"], "serial": profile["camera_serial"], "mount_id": profile["mount_id"],
                "camera_id": profile["camera"].removeprefix("camera_"),
                "flange_frame": profile["flange_frame"], "optical_frame": profile["color_frame"],
                "base_frame": profile["base_frame"]}
    if solved.get("schema_version") != 1 or solved.get("arm_id") != profile["arm"]:
        raise ValueError("not a selected-arm single-session result")
    if any(identity.get(key) != value for key, value in expected.items()):
        raise ValueError("calibration belongs to another camera/arm/installation")
    calibration_id = solved.get("calibration_id")
    if calibration_id != digest({k: v for k, v in solved.items() if k != "calibration_id"}):
        raise ValueError("calibration content hash mismatch")
    if solved.get("intrinsics_sha256") != digest(solved["intrinsics"]):
        raise ValueError("intrinsics hash mismatch")
    matrix = transform(solved["hand_eye"][profile["arm"]]["tool_to_camera"])
    path = root / "ros/camera_extrinsics.local.yaml"
    override = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.is_file() else {}
    if not isinstance(override, dict) or not isinstance(override.get("calibrations", {}), dict):
        raise ValueError("local calibration override must contain mappings")
    override.setdefault("calibrations", {})[profile["calibration"]] = {
        "verified": False, "calibration_id": calibration_id, "camera_serial": profile["camera_serial"],
        "mount_id": profile["mount_id"], "flange_frame": profile["flange_frame"], "color_frame": profile["color_frame"],
        "intrinsics_sha256": solved["intrinsics_sha256"], "result_file": str(source),
        "T_flange_color": matrix.tolist(), "maximum_validation_error_m": None, "measured_validation_error_m": None,
    }
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text("# Staged results remain unverified until independent physical accuracy acceptance.\n" +
                         yaml.safe_dump(override, sort_keys=False), encoding="utf-8")
    temporary.replace(path)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-root", required=True)
    parser.add_argument("--profile", default="right_wrist")
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    print(stage(args.config_root, args.profile, args.result))


if __name__ == "__main__":
    main()
