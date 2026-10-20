"""Validation shared by the server and robot bridge (metres, BGR, optical frame)."""

import math

import numpy as np

PROTOCOL_VERSION = 1
IDENTITY_FIELDS = (
    "task_id", "request_id", "observation_id", "generation",
    "capture_timestamp_ns", "camera_frame", "config_id",
)


class ProtocolError(ValueError):
    """An observation/result cannot be used under the grasp contract."""


def _require(condition, message):
    if not condition:
        raise ProtocolError(message)


def _string(value, label):
    _require(isinstance(value, str) and 0 < len(value) <= 256, f"invalid {label}")


def _float(value, label, lower=0.0, upper=10.0):
    _require(type(value) in (int, float) and math.isfinite(value), f"invalid {label}")
    _require(lower <= value <= upper, f"{label} out of range")
    return float(value)


def identity(request):
    return {key: request[key] for key in IDENTITY_FIELDS}


def _validate_identity(message):
    _require(isinstance(message, dict), "message must be a map")
    _require(type(message.get("protocol_version")) is int and message["protocol_version"] == PROTOCOL_VERSION,
             "unsupported protocol version")
    for key in ("task_id", "request_id", "observation_id", "camera_frame", "config_id"):
        _string(message.get(key), key)
    _require(type(message.get("generation")) is int and message["generation"] >= 0, "invalid generation")
    _require(type(message.get("capture_timestamp_ns")) is int and message["capture_timestamp_ns"] > 0,
             "invalid capture_timestamp_ns")


def validate_request(request):
    """Validate without resizing, changing units, supplying calibration or default axes."""
    _validate_identity(request)
    _require(request.get("type") == "infer", "expected infer message")
    _require(request.get("backend") in ("geometry", "mock", "contact_graspnet"), "unknown backend")
    color, depth, K = (request.get(key) for key in ("color_bgr", "depth_m", "K"))
    _require(isinstance(color, np.ndarray) and color.dtype == np.uint8 and color.ndim == 3,
             "color_bgr must be uint8[H,W,3]")
    _require(color.shape[2] == 3 and 8 <= min(color.shape[:2]) and max(color.shape[:2]) <= 2048,
             "unsupported color dimensions")
    _require(isinstance(depth, np.ndarray) and depth.dtype == np.float32 and depth.shape == color.shape[:2],
             "depth_m must be registered float32[H,W] matching color")
    _require(np.isfinite(depth).all() and np.all((depth >= 0) & (depth <= 10)),
             "depth_m must be finite metres in [0,10]; invalid pixels are zero")
    _require(isinstance(K, np.ndarray) and K.dtype.kind == "f" and K.shape == (3, 3) and np.isfinite(K).all(),
             "K must be a finite floating point 3x3 array")
    _require(K[0, 0] > 0 and K[1, 1] > 0 and np.allclose(K[2], [0, 0, 1])
             and abs(K[0, 1]) < 1e-8 and abs(K[1, 0]) < 1e-8, "invalid pinhole intrinsics")
    h, w = depth.shape
    _require(0 <= K[0, 2] < w and 0 <= K[1, 2] < h, "intrinsic principal point is outside image")
    up = np.asarray(request.get("up_camera"), dtype=np.float64)
    _require(up.shape == (3,) and np.isfinite(up).all() and abs(np.linalg.norm(up) - 1) < 1e-3,
             "up_camera must be an explicit unit vector")
    target = request.get("target")
    _require(isinstance(target, dict), "target must be a map")
    _string(target.get("class_name"), "target.class_name")
    gripper = request.get("gripper")
    _require(isinstance(gripper, dict), "gripper must be a map")
    _string(gripper.get("id"), "gripper.id")
    low = _float(gripper.get("min_opening_m"), "min_opening_m", upper=1)
    high = _float(gripper.get("max_opening_m"), "max_opening_m", upper=1)
    _require(high > low, "gripper opening interval is empty")
    _float(gripper.get("finger_depth_m"), "finger_depth_m", 0.001, 1)
    _float(gripper.get("finger_thickness_m"), "finger_thickness_m", 0.001, 1)
    _float(gripper.get("clearance_m"), "clearance_m", upper=0.1)
    count = request.get("max_candidates", 8)
    _require(type(count) is int and 1 <= count <= 64, "max_candidates must be in [1,64]")
    return request


def validate_candidate(candidate, request=None):
    _require(isinstance(candidate, dict), "candidate must be a map")
    for key in ("candidate_id", "object_id", "class_name", "backend"):
        _string(candidate.get(key), key)
    transform = np.asarray(candidate.get("T_camera_grasp"), dtype=np.float64)
    _require(transform.shape == (4, 4) and np.isfinite(transform).all(), "invalid grasp transform")
    R = transform[:3, :3]
    _require(np.allclose(R.T @ R, np.eye(3), atol=1e-5) and abs(np.linalg.det(R) - 1) < 1e-5,
             "grasp rotation must be right handed and orthonormal")
    _require(np.allclose(transform[3], [0, 0, 0, 1], atol=1e-8), "invalid homogeneous transform")
    _require(0 < transform[2, 3] <= 10, "grasp center must be in front of camera")
    width = _float(candidate.get("width_m"), "width_m", upper=1)
    _float(candidate.get("score"), "score", upper=1)
    if request is not None:
        g = request["gripper"]
        _require(candidate["backend"] == request["backend"], "candidate backend mismatch")
        _require(candidate["class_name"] == request["target"]["class_name"], "candidate target mismatch")
        _require(g["min_opening_m"] <= width and width + 2 * g["clearance_m"] <= g["max_opening_m"],
                 "candidate exceeds gripper opening")
    return candidate


def validate_response(response, request):
    _validate_identity(response)
    _require(response.get("type") in ("result", "error"), "invalid response type")
    _require(identity(response) == identity(request), "response identity mismatch or stale generation")
    if response["type"] == "error":
        _string(response.get("code"), "error code")
        return response
    _string(response.get("model_id"), "model_id")
    _require(response.get("gripper_id") == request["gripper"]["id"], "gripper identity mismatch")
    candidates = response.get("candidates")
    _require(isinstance(candidates, list) and len(candidates) <= request.get("max_candidates", 8),
             "invalid candidate count")
    seen = set()
    for candidate in candidates:
        validate_candidate(candidate, request)
        _require(candidate["candidate_id"] not in seen, "duplicate candidate ID")
        seen.add(candidate["candidate_id"])
    return response
