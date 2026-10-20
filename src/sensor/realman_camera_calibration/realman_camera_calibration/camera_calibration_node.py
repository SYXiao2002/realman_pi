"""ROS services for independent single-arm ChArUco eye-in-hand calibration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from typing import Any

import cv2
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
import numpy as np
import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.duration import Duration
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.time import Time
from rclpy.qos import qos_profile_sensor_data
from realman_msgs.srv import CaptureCalibrationSample, SolveCalibration, UpdateCalibrationLayout
from sensor_msgs.msg import CameraInfo, Image
from std_msgs.msg import String
import tf2_ros

from .calibration_math import (
    decode_image,
    detect_charuco,
    make_charuco_board,
    matrix_json,
    annotate_charuco,
    mean_transform_and_spread,
    solve_hand_eye,
    transform_matrix,
)
from .config import load_config
from .input_health import classify_camera_input
from .layout_update import update_layout_file
from .session import content_hash, restore_session, session_identity, write_manifest


@dataclass
class LatestFrame:
    image_message: Image | None = None
    image: np.ndarray | None = None
    image_received_monotonic: float = 0.0
    camera_info: CameraInfo | None = None
    camera_info_received_monotonic: float = 0.0


def _quaternion_rotation(w: float, x: float, y: float, z: float) -> np.ndarray:
    norm = float(np.sqrt(w * w + x * x + y * y + z * z))
    if norm < 1.0e-12:
        raise ValueError("TF quaternion has zero magnitude")
    w, x, y, z = (value / norm for value in (w, x, y, z))
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def _transform_message_matrix(transform: Any) -> np.ndarray:
    rotation = transform.transform.rotation
    translation = transform.transform.translation
    return transform_matrix(
        _quaternion_rotation(rotation.w, rotation.x, rotation.y, rotation.z),
        [translation.x, translation.y, translation.z],
    )


class CameraCalibrationNode(Node):
    """A deep service interface over the ChArUco/TF calibration implementation."""

    def __init__(self) -> None:
        super().__init__("camera_calibration")
        self._callback_group = ReentrantCallbackGroup()
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self.declare_parameter("config_file", self._default_config_file())
        config_file = str(self.get_parameter("config_file").value)
        self.declare_parameter("layout_config_file", str(Path(config_file).resolve().with_name("three_robots.yaml")))
        self._layout_config_file = Path(str(self.get_parameter("layout_config_file").value)).expanduser().resolve()
        self._config = load_config(config_file)
        self._board = make_charuco_board(self._config["board"])
        self._frames = {
            camera_id: LatestFrame() for camera_id in self._config["cameras"]
        }
        self._samples: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._manifests: dict[str, dict[str, Any]] = {}
        self._session_id = ""
        self._camera_subscriptions: list[Any] = []
        self._tf_buffer = tf2_ros.Buffer()
        self._tf_listener = tf2_ros.TransformListener(self._tf_buffer, self)

        for camera_id, camera in self._config["cameras"].items():
            self._camera_subscriptions.append(
                self.create_subscription(
                    Image,
                    camera["image_topic"],
                    lambda message, key=camera_id: self._on_image(key, message),
                    qos_profile_sensor_data,
                    callback_group=self._callback_group,
                )
            )
            self._camera_subscriptions.append(
                self.create_subscription(
                    CameraInfo,
                    camera["camera_info_topic"],
                    lambda message, key=camera_id: self._on_camera_info(key, message),
                    qos_profile_sensor_data,
                    callback_group=self._callback_group,
                )
            )

        services = self._config.get("services", {})
        self._capture_service = self.create_service(
            CaptureCalibrationSample,
            str(services.get("capture", "/camera_calibration/capture_sample")),
            self._capture,
            callback_group=self._callback_group,
        )
        self._solve_service = self.create_service(
            SolveCalibration,
            str(services.get("solve", "/camera_calibration/solve")),
            self._solve,
            callback_group=self._callback_group,
        )
        self._layout_service = self.create_service(
            UpdateCalibrationLayout,
            str(services.get("update_layout", "/camera_calibration/update_layout")),
            self._update_layout,
            callback_group=self._callback_group,
        )
        self._diagnostics = self.create_publisher(
            DiagnosticArray,
            str(self._config.get("diagnostics_topic", "/camera_calibration/diagnostics")),
            10,
        )
        self._camera_health = self.create_publisher(
            String,
            str(self._config.get("camera_health_topic", "/camera_calibration/camera_health")),
            10,
        )
        self._diagnostic_timer = self.create_timer(
            1.0, self._publish_diagnostics, callback_group=self._callback_group
        )
        self.get_logger().info(
            f"Single-arm ChArUco calibration ready for {list(self._required_arms())}; "
            "each session captures and solves one wrist independently"
        )

    def _default_config_file(self) -> str:
        override = os.environ.get("REALMAN_CAMERA_CALIBRATION_CONFIG_FILE", "")
        if override:
            return override
        root = os.environ.get("REALMAN_CONFIG_ROOT", str(Path.cwd() / "config"))
        return str(Path(root) / "ros" / "camera_calibration.yaml")

    def _required_arms(self) -> tuple[str, ...]:
        return tuple(self._config["sampling"]["arm_order"])

    def _on_image(self, camera_id: str, message: Image) -> None:
        try:
            image = decode_image(message)
        except ValueError as error:
            self.get_logger().warn(f"Ignoring {camera_id} image: {error}")
            return
        with self._lock:
            frame = self._frames[camera_id]
            frame.image_message = message
            frame.image = image
            frame.image_received_monotonic = time.monotonic()

    def _on_camera_info(self, camera_id: str, message: CameraInfo) -> None:
        with self._lock:
            frame = self._frames[camera_id]
            frame.camera_info = message
            frame.camera_info_received_monotonic = time.monotonic()

    def _capture(self, request, response):
        if not self._operation_lock.acquire(blocking=False):
            response.message = "another calibration operation is in progress"
            return response
        try:
            return self._capture_locked(request, response)
        except Exception as error:
            response.message = f"capture rejected: {error}"
            self.get_logger().warn(response.message)
            return response
        finally:
            self._operation_lock.release()

    def _capture_locked(self, request, response):
        arm = request.arm_id or self._config["sampling"]["default_arm"]
        identity = session_identity(self._config, arm)
        response.arm_id = arm
        if request.start_new_session:
            if request.session_id:
                raise ValueError("start_new_session requires an empty session_id")
            self._start_session(arm)
        elif request.session_id:
            self._load_session(request.session_id, arm)
        elif not self._session_id:
            self._start_session(arm)
        elif self._manifests[self._session_id]["identity"] != identity:
            raise ValueError("selected arm differs from the active session; start or load its own session")
        session_id = self._session_id
        manifest = self._manifests[session_id]
        response.session_id = session_id
        samples = self._samples[session_id][arm]
        response.sample_count = len(samples)
        camera_id = identity["camera_id"]
        batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        attempt_dir = self._session_directory(session_id) / "attempts" / batch_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        sampling = self._config["sampling"]
        best_error = "waiting for Image and CameraInfo"
        observation = None
        image = None
        message = None
        info = None
        for _ in range(8):
            now = time.monotonic()
            with self._lock:
                frame = self._frames[camera_id]
                health = self._camera_health_state(
                    camera_id, frame, now, self.get_clock().now().nanoseconds
                )
                if health["status"] == "healthy":
                    image = frame.image.copy()
                    message = frame.image_message
                    info = frame.camera_info
                else:
                    image = message = info = None
                    best_error = str(health["message"])
            if image is None:
                time.sleep(0.06)
                continue
            try:
                if self._message_stamp_ns(message) <= 0 or self._message_stamp_ns(info) <= 0:
                    raise ValueError("Image and CameraInfo require non-zero ROS timestamps")
                if message.header.frame_id != identity["optical_frame"] or info.header.frame_id != identity["optical_frame"]:
                    raise ValueError("Image/CameraInfo optical frame differs from configured camera")
                if (info.width, info.height) != (image.shape[1], image.shape[0]):
                    raise ValueError("CameraInfo resolution differs from Image")
                observation = detect_charuco(
                    image, np.asarray(info.k, dtype=np.float64).reshape(3, 3),
                    np.asarray(info.d, dtype=np.float64), self._board,
                    int(self._config["board"]["minimum_corners"]),
                )
                break
            except Exception as error:
                best_error = str(error)
                time.sleep(0.06)
        if image is not None:
            latest = attempt_dir / f"{camera_id}.png"
            if cv2.imwrite(str(latest), image):
                response.latest_image_path = str(latest)
        if observation is None:
            response.detection_status = "missing_input" if image is None else "not_detected"
            response.detection_message = best_error
            raise ValueError(best_error)
        intrinsics = {
            "width": int(info.width), "height": int(info.height),
            "k": list(info.k), "d": list(info.d),
            "distortion_model": str(info.distortion_model),
        }
        intrinsic_hash = content_hash(intrinsics)
        if manifest.get("intrinsics_sha256") not in (None, intrinsic_hash):
            raise ValueError("camera intrinsics changed within this session; start a new session")
        robot = self._config["robots"][arm]
        try:
            transform = self._tf_buffer.lookup_transform(
                robot["base_frame"], robot["end_effector_frame"],
                Time.from_msg(message.header.stamp),
                timeout=Duration(seconds=float(sampling["tf_timeout_sec"])),
            )
        except Exception as error:
            response.detection_status = "tf_unavailable"
            response.detection_message = str(error)
            raise ValueError(f"image-time flange TF unavailable: {error}") from error
        response.detection_status = "detected"
        response.detection_message = f"{arm}: detected {len(observation.ids)} ChArUco corners"
        output_dir = self._session_directory(session_id)
        batch_root = output_dir / "batches"
        batch_root.mkdir(parents=True, exist_ok=True)
        final_batch_dir = batch_root / batch_id
        staging = batch_root / f".{batch_id}.tmp"
        staging.mkdir()
        sample_id = f"{arm}-{len(samples):04d}-{batch_id}"
        image_path = final_batch_dir / f"{sample_id}.png"
        preview_path = final_batch_dir / f"{sample_id}-preview.png"
        sample = {
            "sample_id": sample_id, "arm_id": arm, "identity": identity,
            "image_path": str(image_path), "preview_image_path": str(preview_path),
            "base_to_tool": _transform_message_matrix(transform).tolist(),
            "target_to_camera": observation.target_to_camera.tolist(),
            "reprojection_error_px": observation.reprojection_error_px,
            "charuco_ids": observation.ids.reshape(-1).tolist(),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "stamp_ns": self._message_stamp_ns(message),
            "intrinsics_sha256": intrinsic_hash,
        }
        try:
            if not cv2.imwrite(str(staging / image_path.name), image):
                raise OSError("could not save sample image")
            preview = annotate_charuco(
                image, observation, float(self._config["board"]["square_length_m"]) * 2.0
            )
            if not cv2.imwrite(str(staging / preview_path.name), preview):
                raise OSError("could not save sample preview")
            (staging / f"{sample_id}.json").write_text(
                json.dumps(sample, indent=2, allow_nan=False), encoding="utf-8"
            )
            if manifest.get("intrinsics_sha256") is None:
                manifest = {**manifest, "intrinsics": intrinsics, "intrinsics_sha256": intrinsic_hash}
                write_manifest(output_dir, manifest)
                self._manifests[session_id] = manifest
            staging.replace(final_batch_dir)
        except Exception:
            for path in staging.glob("*"):
                path.unlink(missing_ok=True)
            staging.rmdir()
            raise
        samples.append(sample)
        response.success = True
        response.batch_id = batch_id
        response.sample_count = len(samples)
        response.sample_id = sample_id
        response.image_path = str(image_path)
        response.preview_image_path = str(preview_path)
        response.message = f"accepted one {arm} wrist sample; other arms are not required"
        self.get_logger().info(response.message)
        return response

    def _solve(self, request, response):
        if not self._operation_lock.acquire(blocking=False):
            response.message = "another calibration operation is in progress"
            return response
        try:
            return self._solve_locked(request, response)
        except Exception as error:
            response.message = f"calibration solve rejected: {error}"
            self.get_logger().error(response.message)
            return response
        finally:
            self._operation_lock.release()

    def _solve_locked(self, request, response):
        arm = request.arm_id or self._config["sampling"]["default_arm"]
        session_identity(self._config, arm)
        session_id = request.session_id or self._session_id
        response.session_id = session_id
        response.arm_id = arm
        if not session_id:
            raise ValueError("no calibration session selected")
        # Always reload: immutable disk provenance, not the active arm, owns solve.
        self._load_session(session_id, arm)
        samples = self._samples[session_id][arm]
        manifest = self._manifests[session_id]
        response.sample_count = len(samples)
        minimum = int(self._config["sampling"]["minimum_samples_per_arm"])
        if len(samples) < minimum:
            raise ValueError(f"selected {arm} session needs at least {minimum} pose-diverse samples")
        solver = self._config["solver"]
        solved = solve_hand_eye(samples, str(solver["hand_eye_method"]))
        residual_limit = float(solver["maximum_hand_eye_residual"])
        if solved["mean_residual_m"] > residual_limit:
            raise ValueError(f"hand-eye residual exceeds {residual_limit:.4f}m")
        tool_to_camera = solved["tool_to_camera"]
        board_poses = [
            np.asarray(sample["base_to_tool"]) @ tool_to_camera @ np.asarray(sample["target_to_camera"])
            for sample in samples
        ]
        board_pose, translation_spread, rotation_spread = mean_transform_and_spread(board_poses)
        if translation_spread > float(solver["maximum_board_translation_spread_m"]) or rotation_spread > float(solver["maximum_board_rotation_spread_rad"]):
            raise ValueError(f"board-pose spread too large: {translation_spread:.4f}m/{rotation_spread:.4f}rad")
        result = {
            "schema_version": 1, "session_id": session_id, "arm_id": arm,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "identity": manifest["identity"], "intrinsics": manifest["intrinsics"],
            "intrinsics_sha256": manifest["intrinsics_sha256"],
            "board": self._config["board"], "method": solver["hand_eye_method"],
            "hand_eye": {arm: {
                "sample_count": len(samples), "tool_to_camera": matrix_json(tool_to_camera),
                "mean_hand_eye_residual_m": solved["mean_residual_m"],
                "base_to_board": matrix_json(board_pose),
                "translation_spread_m": translation_spread,
                "rotation_spread_rad": rotation_spread,
            }},
            "verified_for_grasp": False,
        }
        result["calibration_id"] = content_hash(result)
        result_path = self._session_directory(session_id) / "calibration_result.json"
        temporary = result_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
            temporary.replace(result_path)
        finally:
            temporary.unlink(missing_ok=True)
        response.success = True
        response.result_file = str(result_path)
        response.result_json = json.dumps(result, separators=(",", ":"), allow_nan=False)
        response.mean_reprojection_error_px = float(np.mean([sample["reprojection_error_px"] for sample in samples]))
        response.layout_updated = False
        response.layout_backup_file = ""
        response.message = f"{arm} hand-eye solved; select left/right sessions to update base layout"
        self.get_logger().info(response.message)
        return response

    def _start_session(self, arm: str) -> None:
        self._session_id = datetime.now(timezone.utc).strftime("session-%Y%m%dT%H%M%S.%fZ")
        manifest = {
            "schema_version": 1, "session_id": self._session_id,
            "identity": session_identity(self._config, arm),
        }
        write_manifest(self._session_directory(self._session_id), manifest)
        self._manifests[self._session_id] = manifest
        self._samples[self._session_id] = {arm: []}

    def _update_layout(self, request, response):
        response.left_session_id = request.left_session_id
        response.right_session_id = request.right_session_id
        if not self._operation_lock.acquire(blocking=False):
            response.message = "another calibration operation is in progress"
            return response
        try:
            poses = {}
            for arm, session_id in (("l", request.left_session_id), ("r", request.right_session_id)):
                result_path = self._session_directory(session_id) / "calibration_result.json"
                result = json.loads(result_path.read_text(encoding="utf-8"))
                if result.get("session_id") != session_id or result.get("arm_id") != arm:
                    raise ValueError(f"selected {arm} session does not contain a solved {arm} result")
                manifest, samples = restore_session(self._session_directory(session_id), self._config, arm)
                if result.get("identity") != manifest.get("identity"):
                    raise ValueError(f"selected {arm} result identity differs from its session")
                hashed = {key: value for key, value in result.items() if key != "calibration_id"}
                if result.get("calibration_id") != content_hash(hashed):
                    raise ValueError(f"selected {arm} result hash is invalid; solve it again")
                if result.get("intrinsics_sha256") != manifest.get("intrinsics_sha256"):
                    raise ValueError(f"selected {arm} result intrinsics changed since solving")
                if result.get("board") != self._config["board"]:
                    raise ValueError(f"selected {arm} board configuration differs from current config")
                hand_eye = result.get("hand_eye", {}).get(arm)
                if not isinstance(hand_eye, dict) or hand_eye.get("sample_count") != len(samples):
                    raise ValueError(f"selected {arm} result samples changed since solving")
                if len(samples) < int(self._config["sampling"]["minimum_samples_per_arm"]):
                    raise ValueError(f"selected {arm} session has too few samples")
                solver = self._config["solver"]
                for field, limit in (
                    ("mean_hand_eye_residual_m", "maximum_hand_eye_residual"),
                    ("translation_spread_m", "maximum_board_translation_spread_m"),
                    ("rotation_spread_rad", "maximum_board_rotation_spread_rad"),
                ):
                    value = float(hand_eye[field])
                    if not np.isfinite(value) or value > float(solver[limit]):
                        raise ValueError(f"selected {arm} result fails current {limit}")
                pose = np.asarray(result["hand_eye"][arm]["base_to_board"], dtype=np.float64)
                if pose.shape != (4, 4) or not np.isfinite(pose).all():
                    raise ValueError(f"invalid {arm} base_to_board matrix")
                poses[arm] = pose
            relative = poses["l"] @ np.linalg.inv(poses["r"])
            backup = update_layout_file(self._layout_config_file, {"r": relative})
            response.layout_updated = True
            response.layout_backup_file = str(backup)
            response.success = True
            response.message = "right-arm layout updated; left/middle preserved; restart bringup to load base TF"
            self.get_logger().info(
                f"{response.message}; l={request.left_session_id}, r={request.right_session_id}"
            )
        except Exception as error:
            response.message = f"layout update rejected: {error}"
            self.get_logger().error(response.message)
        finally:
            self._operation_lock.release()
        return response

    def _load_session(self, session_id: str, arm: str) -> None:
        manifest, samples = restore_session(self._session_directory(session_id), self._config, arm)
        self._session_id = session_id
        self._manifests[session_id] = manifest
        self._samples[session_id] = {arm: samples}

    def _session_directory(self, session_id: str) -> Path:
        if not re.fullmatch(r"session-[0-9TZ.\-]+", session_id):
            raise ValueError("invalid calibration session id")
        root = Path(os.environ.get("REALMAN_LOG_ROOT", str(Path.cwd() / "logs")))
        subdirectory = str(self._config.get("storage", {}).get("output_subdirectory", "camera_calibration"))
        return root / subdirectory / session_id

    def _publish_diagnostics(self) -> None:
        array = DiagnosticArray()
        array.header.stamp = self.get_clock().now().to_msg()
        now_monotonic = time.monotonic()
        now_ns = self.get_clock().now().nanoseconds
        health_inputs: list[dict[str, Any]] = []
        with self._lock:
            for camera_id, frame in self._frames.items():
                health = self._camera_health_state(camera_id, frame, now_monotonic, now_ns)
                health_inputs.append(health)
                status = DiagnosticStatus()
                status.name = f"camera_calibration/{camera_id}"
                status.hardware_id = camera_id
                status.level = DiagnosticStatus.OK if health["status"] == "healthy" else DiagnosticStatus.WARN
                status.message = str(health["message"])
                status.values = [
                    KeyValue(key=key, value=self._health_value(value))
                    for key, value in health.items()
                    if key not in {"camera_id", "arm_id", "status", "message"}
                ] + [
                    KeyValue(key="session_id", value=self._session_id),
                ]
                array.status.append(status)
        self._diagnostics.publish(array)
        self._camera_health.publish(
            String(data=json.dumps({"type": "camera_health", "inputs": health_inputs}))
        )

    def _camera_health_state(
        self,
        camera_id: str,
        frame: LatestFrame,
        now_monotonic: float,
        now_ns: int,
    ) -> dict[str, Any]:
        camera = self._config["cameras"][camera_id]
        image_age = now_monotonic - frame.image_received_monotonic if frame.image_received_monotonic else None
        info_age = now_monotonic - frame.camera_info_received_monotonic if frame.camera_info_received_monotonic else None
        image_stamp_ns = self._message_stamp_ns(frame.image_message)
        info_stamp_ns = self._message_stamp_ns(frame.camera_info)
        return classify_camera_input(
            camera_id=camera_id,
            arm_id=str(camera["arm_id"]),
            image_age_sec=image_age,
            camera_info_age_sec=info_age,
            image_timestamp_delay_sec=self._timestamp_delay_sec(now_ns, image_stamp_ns),
            camera_info_timestamp_delay_sec=self._timestamp_delay_sec(now_ns, info_stamp_ns),
            image_camera_info_skew_sec=(abs(image_stamp_ns - info_stamp_ns) / 1_000_000_000.0)
            if image_stamp_ns and info_stamp_ns
            else None,
            maximum_message_age_sec=float(self._config["sampling"]["maximum_message_age_sec"]),
            maximum_timestamp_delay_sec=float(self._config["sampling"]["maximum_timestamp_delay_sec"]),
            maximum_inter_camera_skew_sec=float(self._config["sampling"]["maximum_inter_camera_skew_sec"]),
            image_width=int(frame.image.shape[1]) if frame.image is not None else 0,
            image_height=int(frame.image.shape[0]) if frame.image is not None else 0,
        )

    @staticmethod
    def _message_stamp_ns(message: Any) -> int:
        if message is None:
            return 0
        stamp = message.header.stamp
        return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)

    @staticmethod
    def _timestamp_delay_sec(now_ns: int, stamp_ns: int) -> float | None:
        if stamp_ns <= 0:
            return None
        return (now_ns - stamp_ns) / 1_000_000_000.0

    @staticmethod
    def _health_value(value: Any) -> str:
        return f"{value:.3f}" if isinstance(value, float) else str(value)


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = CameraCalibrationNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()
