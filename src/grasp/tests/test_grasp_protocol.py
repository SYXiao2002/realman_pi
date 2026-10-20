"""Contract tests: hostile payloads, frame validation and stale identities."""

import copy
import sys
from pathlib import Path

import msgpack
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grasp_protocol import ProtocolError, packb, unpackb, validate_request, validate_response
from grasp_protocol.schema import identity


def observation(backend="mock", request_id="request-1"):
    depth = np.zeros((64, 80), dtype=np.float32)
    depth[20:44, 25:55] = 0.5
    return {
        "protocol_version": 1, "type": "infer", "task_id": "task-1", "request_id": request_id,
        "observation_id": "observation-1", "generation": 1, "capture_timestamp_ns": 123456789,
        "camera_frame": "camera_color_optical_frame", "config_id": "config-sha256",
        "backend": backend, "color_bgr": np.zeros((64, 80, 3), dtype=np.uint8),
        "depth_m": depth, "K": np.array([[400., 0, 40], [0, 400., 32], [0, 0, 1]]),
        "up_camera": [0., 0., -1.], "target": {"class_name": "adapter"},
        "gripper": {"id": "synthetic-fixture-only", "min_opening_m": 0.005,
                    "max_opening_m": 0.08, "finger_depth_m": 0.03,
                    "finger_thickness_m": 0.006, "clearance_m": 0.002},
        "max_candidates": 8,
    }


def test_binary_roundtrip_preserves_dtype_and_immutable_observation():
    request = observation()
    decoded = unpackb(packb(request))
    validate_request(decoded)
    for key in ("color_bgr", "depth_m", "K"):
        assert decoded[key].dtype == request[key].dtype
        assert np.array_equal(decoded[key], request[key])
        assert not decoded[key].flags.writeable


@pytest.mark.parametrize("dtype", ["O", "V8", "complex128"])
def test_never_decodes_object_or_structured_arrays(dtype):
    envelope = {"__ndarray__": True, "shape": [1], "dtype": dtype, "data": b"12345678"}
    with pytest.raises(ValueError):
        unpackb(msgpack.packb(envelope, use_bin_type=True))


def test_rejects_large_or_malformed_array_envelopes():
    for shape in ([1000000000], [-1], [True], [1, 1, 1, 1]):
        with pytest.raises(ValueError):
            unpackb(msgpack.packb({"__ndarray__": True, "shape": shape, "dtype": "<f8", "data": b"12345678"},
                                 use_bin_type=True))


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(depth_m=r["depth_m"].astype(np.uint16)),
    lambda r: r.update(depth_m=r["depth_m"][:-1]),
    lambda r: r["depth_m"].__setitem__((0, 0), np.nan),
    lambda r: r.update(up_camera=[0, 0, 0]),
    lambda r: r.update(camera_frame=""),
    lambda r: r["K"].__setitem__((0, 2), 800),
    lambda r: r["gripper"].update(max_opening_m=0),
])
def test_invalid_observation_never_becomes_inference(mutate):
    request = observation()
    mutate(request)
    with pytest.raises(ProtocolError):
        validate_request(request)


def test_stale_generation_and_wrong_model_response_are_rejected():
    request = observation()
    response = {"protocol_version": 1, "type": "result", **identity(request), "model_id": "model",
                "gripper_id": request["gripper"]["id"], "candidates": []}
    validate_response(response, request)
    old = copy.deepcopy(response)
    old["generation"] -= 1
    with pytest.raises(ProtocolError, match="identity"):
        validate_response(old, request)
    response["gripper_id"] = "different-tool"
    with pytest.raises(ProtocolError, match="gripper"):
        validate_response(response, request)
