"""Timestamped RGB-D validation and calibrated depth-to-color reprojection."""

from dataclasses import dataclass

import cv2
import numpy as np

from .geometry import transform


@dataclass(frozen=True)
class CameraModel:
    width: int
    height: int
    frame: str
    K: np.ndarray
    D: np.ndarray
    distortion_model: str = "plumb_bob"

    def validate(self):
        K = np.asarray(self.K, dtype=float)
        if (K.shape != (3, 3) or not np.isfinite(K).all() or K[0, 0] <= 0 or K[1, 1] <= 0
                or not np.allclose(K[2], [0, 0, 1]) or not self.frame or min(self.width, self.height) < 8):
            raise ValueError("invalid CameraInfo")
        if self.distortion_model not in ("plumb_bob", "rational_polynomial"):
            raise ValueError("unsupported distortion model; require calibrated pinhole image")
        if not np.isfinite(self.D).all() or len(self.D) not in (0, 4, 5, 8, 12, 14):
            raise ValueError("invalid distortion coefficients")
        return self


def image_array(message, *, depth=False):
    """Respect ROS Image step, encoding and byte order; never resize depth."""
    encodings = {"bgr8": (np.uint8, 3), "rgb8": (np.uint8, 3),
                 "16UC1": (np.uint16, 1), "32FC1": (np.float32, 1)}
    if message.encoding not in encodings:
        raise ValueError(f"unsupported image encoding {message.encoding}")
    dtype, channels = encodings[message.encoding]
    if depth != (channels == 1):
        raise ValueError("unexpected color/depth encoding")
    dt = np.dtype(dtype).newbyteorder(">" if message.is_bigendian else "<")
    row_bytes = message.width * channels * dt.itemsize
    if message.step < row_bytes or len(message.data) != message.step * message.height:
        raise ValueError("invalid image row stride or data length")
    rows = np.frombuffer(message.data, dtype=np.uint8).reshape(message.height, message.step)
    result = np.frombuffer(rows[:, :row_bytes].copy().tobytes(), dtype=dt).reshape(
        (message.height, message.width, channels) if channels == 3 else (message.height, message.width)
    )
    if depth:
        result = result.astype(np.float32)
        if message.encoding == "16UC1":
            result *= 0.001  # ROS depth convention: uint16 millimetres, float32 metres.
        result[~np.isfinite(result) | (result < 0) | (result > 10)] = 0
    elif message.encoding == "rgb8":
        result = result[:, :, ::-1].copy()
    return result


def register_depth(depth_m, depth_camera, color_camera, T_color_depth):
    """Calibrated D2C with occlusion z-buffer, then color rectification.

    Returns pinhole/undistorted color-aligned depth and validity. Missing pixels
    remain zero. No interpolation or hole fill invents a surface.
    """
    depth_camera.validate()
    color_camera.validate()
    T = transform(T_color_depth)
    if depth_m.shape != (depth_camera.height, depth_camera.width):
        raise ValueError("depth/CameraInfo resolution mismatch")
    yy, xx = np.nonzero(np.isfinite(depth_m) & (depth_m > 0) & (depth_m <= 10))
    output = np.full((color_camera.height, color_camera.width), np.inf, dtype=np.float32)
    if len(xx) == 0:
        return np.zeros_like(output)
    pixels = np.column_stack((xx, yy)).astype(np.float64).reshape(-1, 1, 2)
    rays = cv2.undistortPoints(pixels, depth_camera.K, depth_camera.D).reshape(-1, 2)
    xyz = np.column_stack((rays, np.ones(len(rays)))) * depth_m[yy, xx, None]
    xyz = xyz @ T[:3, :3].T + T[:3, 3]
    xyz = xyz[xyz[:, 2] > 0]
    uv = xyz @ color_camera.K.T
    uv = np.rint(uv[:, :2] / uv[:, 2, None]).astype(np.int64)
    inside = (uv[:, 0] >= 0) & (uv[:, 0] < color_camera.width) & (uv[:, 1] >= 0) & (uv[:, 1] < color_camera.height)
    uv, xyz = uv[inside], xyz[inside]
    np.minimum.at(output, (uv[:, 1], uv[:, 0]), xyz[:, 2].astype(np.float32))
    output[~np.isfinite(output)] = 0
    return output


def rectify_color(color_bgr, model):
    model.validate()
    if color_bgr.shape != (model.height, model.width, 3):
        raise ValueError("color/CameraInfo resolution mismatch")
    return cv2.undistort(color_bgr, model.K, model.D, None, model.K)


def validate_timing(stamps_ns, now_ns, *, skew_sec, age_sec):
    if any(type(s) is not int or s <= 0 for s in stamps_ns):
        raise ValueError("missing capture timestamps")
    if max(stamps_ns)-min(stamps_ns) > skew_sec * 1e9:
        raise ValueError("RGB-D/CameraInfo timestamps are not paired")
    if now_ns-min(stamps_ns) > age_sec*1e9 or max(stamps_ns) > now_ns + 1e7:
        raise ValueError("stale or future observation")
