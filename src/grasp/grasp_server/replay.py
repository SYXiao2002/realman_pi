"""Send a frozen NPZ RGB-D scene (or synthetic mock fixture) over WebSocket.

NPZ must contain color_bgr uint8[H,W,3], registered depth_m float32[H,W],
K float[3,3]. The reference archive's bgr key is accepted; up_camera is optional
for this read-only evaluation. Loading uses allow_pickle=False.
This command has no robot motion/gripper client and never acquires control.
"""

import argparse
import asyncio
import base64
import json
import time
import uuid
from pathlib import Path

import numpy as np
import yaml

from grasp_protocol import packb, validate_request
from grasp_protocol.client import WsGraspClient


def fixture_request():
    depth = np.zeros((64, 80), dtype=np.float32)
    depth[20:44, 25:55] = 0.5
    return {
        "color_bgr": np.zeros((64, 80, 3), dtype=np.uint8), "depth_m": depth,
        "K": np.array([[400., 0, 40], [0, 400., 32], [0, 0, 1]]), "up_camera": [0., 0., -1.],
        "gripper": {"id": "synthetic_fixture_only", "min_opening_m": 0.005, "max_opening_m": 0.08,
                    "finger_depth_m": 0.03, "finger_thickness_m": 0.006, "clearance_m": 0.002},
    }


def request_from_npz(path, gripper):
    with np.load(path, allow_pickle=False) as scene:
        data = {key: scene[key].copy() for key in ("depth_m", "K")}
        data["color_bgr"] = scene["color_bgr" if "color_bgr" in scene else "bgr"].copy()
        data["up_camera"] = scene["up_camera"].copy() if "up_camera" in scene else None
        data["T_base_camera"] = scene["T_base_camera"].copy() if "T_base_camera" in scene else None
    data["gripper"] = gripper
    return data


def _json_default(value):
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(type(value).__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture", action="store_true", help="synthetic mock observation; no neural model")
    source.add_argument("--input", type=Path, help="registered RGB-D NPZ scene")
    parser.add_argument("--uri", default="ws://127.0.0.1:8091")
    parser.add_argument("--target", default="adapter")
    parser.add_argument("--camera-frame", default="replay_color_optical_frame")
    parser.add_argument("--backend", choices=["geometry", "contact_graspnet"], default="geometry")
    parser.add_argument("--cgn-context", choices=["mask", "box", "full"], default="mask")
    parser.add_argument("--top-k", type=int, default=15)
    parser.add_argument("--grippers", type=Path, default=Path("config/grasp/gripper_specs.yaml"))
    parser.add_argument("--gripper", default="binary_42d_right")
    parser.add_argument("--output", type=Path, help="optional MessagePack result artifact (outside source)")
    args = parser.parse_args()
    if args.fixture:
        data, backend = fixture_request(), "mock"
    else:
        specs = yaml.safe_load(args.grippers.read_text(encoding="utf-8"))["grippers"]
        spec = dict(specs[args.gripper])
        data, backend = request_from_npz(args.input, spec), args.backend
    run_id = uuid.uuid4().hex
    request = {
        "protocol_version": 1, "type": "infer", "task_id": "replay-" + run_id,
        "request_id": run_id, "observation_id": "observation-" + run_id,
        "generation": 0, "capture_timestamp_ns": time.time_ns(), "camera_frame": args.camera_frame,
        "config_id": "offline-replay", "backend": backend,
        "target": {"class_name": args.target}, "max_candidates": args.top_k,
        "evaluation_only": True, "cgn_context": args.cgn_context, **data,
    }
    validate_request(request)
    response = asyncio.run(WsGraspClient(args.uri).infer(request))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(packb(response))
    print(json.dumps(response, default=_json_default, sort_keys=True))
    return 1 if response["type"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
