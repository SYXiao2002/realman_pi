"""ROS RGB-D capture and bounded browser-facing pose review events."""

import base64
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import threading
import time

import cv2
import numpy as np
import rclpy
from rclpy.duration import Duration
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import CameraInfo, Image, JointState
from std_msgs.msg import String
from tf2_ros import Buffer, TransformListener

from grasp_bridge.geometry import from_pose, transform
from grasp_bridge.node import GraspBridge, model, stamp_ns
from grasp_bridge.profiles import camera_identity, default_config_root, load_profile, read_yaml
from grasp_bridge.rgbd import image_array, rectify_color, register_depth, validate_timing
from grasp_protocol.wire import MAX_MESSAGE_BYTES, packb, unpackb
from pose6d.geometry import configured_transform, suction_pose, tcp_target
from pose6d.motion import MotionController
from pose6d.observation import ObservationStore
from pose6d.scoring import assess_prepare_pose
from .motion_ros import MotionRosPort
from .analytic_ik_ros import AnalyticIkRosPort, tcp_targets

WEB_EVENT_LIMIT = 8 * 1024 * 1024
VISION_LOG_MAX_BYTES = 10 * 1024 * 1024
VISION_LOG_BACKUPS = 5


def image_url(image):
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("PNG encoding failed")
    return "data:image/png;base64," + base64.b64encode(encoded).decode("ascii")


class PoseBridge(Node):
    def __init__(self):
        super().__init__("pose6d_bridge")
        self.root = Path(default_config_root())
        self.capture_settings = read_yaml(self.root / "ros/grasp_bridge.yaml")
        self.settings = {**self.capture_settings, **read_yaml(self.root / "ros/pose6d_bridge.yaml")}
        self.profile = load_profile(self.root, self.settings["profile"], require_default_tool=False)
        self.inputs = {k: deque(maxlen=30) for k in ("color", "depth", "color_info", "depth_info", "joints")}
        self.lock = threading.Lock()
        self.busy = threading.Lock()
        self.store = ObservationStore()
        self.vision_log = Path(os.environ.get("REALMAN_LOG_ROOT", "/opt/rm65_ws/logs")) / "pose6d_vision" / "timings.jsonl"
        self.vision_log_lock = threading.Lock()
        self.observation_owner = None
        self.owner_lock = threading.Lock()
        self.disconnected_clients = set()
        self.connected_clients = None  # Unknown until Web control's first socket snapshot.
        self.active_review_client = None
        self.motion_config = self.settings["motion"]
        configured_models = self.settings.get("models")
        if configured_models is None:
            configured_models = {self.settings["object_id"]: {
                "mesh_dir": self.settings["mesh_dir"], "mesh_file": "TexturedMeshSimplified.obj",
                "annotation_file": "eu_plug_grasps.json", "label": self.settings["object_id"]}}
        if not isinstance(configured_models, dict) or not configured_models:
            raise ValueError("pose6d models must be a nonempty mapping")
        self.models = {}
        for object_id, spec in configured_models.items():
            if (not isinstance(object_id, str) or not object_id or not isinstance(spec, dict)
                    or not isinstance(spec.get("mesh_dir"), str) or not isinstance(spec.get("mesh_file"), str)):
                raise ValueError("each pose6d model needs a mesh_dir and mesh_file")
            mesh_dir = Path(spec["mesh_dir"])
            mesh_path = mesh_dir / spec["mesh_file"]
            if not mesh_path.is_file():
                raise FileNotFoundError(f"mesh for {object_id} does not exist: {mesh_path}")
            annotation = None
            if spec.get("annotation_file"):
                annotation = json.loads((mesh_dir / spec["annotation_file"]).read_text(encoding="utf-8"))
                if (annotation.get("object_id") != spec.get("annotation_object_id", object_id)
                        or annotation.get("normal_convention") != "outward_surface"):
                    raise ValueError(f"suction annotation identity or normal convention mismatch for {object_id}")
            self.models[object_id] = {
                **spec, "mesh_dir": mesh_dir, "mesh_path": mesh_path, "annotation": annotation,
                "model_sha256": hashlib.sha256(mesh_path.read_bytes()).hexdigest(),
            }
        self.default_object_id = self.settings.get("default_object_id", self.settings.get("object_id"))
        if self.default_object_id not in self.models:
            raise ValueError("default_object_id must exist in configured pose6d models")
        self.settings["object_id"] = self.default_object_id
        self.mesh_hash = self.models[self.default_object_id]["model_sha256"]
        self.mesh_dir = self.models[self.default_object_id]["mesh_dir"]
        self.annotation = self.models[self.default_object_id]["annotation"]
        coordinate_arm = read_yaml(self.root / "ros/realman_coordinates.yaml")["robots"][self.profile["arm"]]
        self.selectable_tool_names = {self.profile["tool_name"], self.motion_config["tool_name"]}
        if not self.selectable_tool_names.issubset(coordinate_arm["tools"]):
            raise ValueError("pose6d tool choices must be registered in realman_coordinates.yaml")
        tool_pose = coordinate_arm["tools"][self.motion_config["tool_name"]]["pose"]
        self.ik_flange_tcp = from_pose(tool_pose["xyz_m"], tool_pose["quaternion_wxyz"])
        self.analytic_ik_port = AnalyticIkRosPort(self, self.settings["analytic_ik"])
        self.tf = Buffer(cache_time=Duration(seconds=60))
        self.listener = TransformListener(self.tf, self)
        self.events = self.create_publisher(String, "~/events", 10)
        self.command = self.create_subscription(String, "~/command", self.handle_command, 10)
        self.motion_port = MotionRosPort(
            self, self.profile["arm"], self.motion_config, self.settings["suction"],
            state_timeout_sec=self.capture_settings["maximum_joint_state_age_sec"],
            settle_sec=self.capture_settings["settle_sec"],
            max_joint_velocity_rad_sec=self.capture_settings["maximum_joint_velocity_rad_sec"],
            selectable_tool_names=self.selectable_tool_names,
        )
        self.motion = MotionController(self.motion_port, self.motion_config, self.settings["suction"])
        self.motion_stop = threading.Event()
        self.subs = []
        for key, cls, field in (("color", Image, "color_topic"), ("depth", Image, "depth_topic"),
                                ("color_info", CameraInfo, "color_info_topic"),
                                ("depth_info", CameraInfo, "depth_info_topic"), ("joints", JointState, "joint_topic")):
            self.subs.append(self.create_subscription(cls, self.profile[field],
                             lambda message, name=key: self.receive(name, message), qos_profile_sensor_data))
        self.motion_watchdog = threading.Thread(target=self.motion_watchdog_loop, daemon=True)
        self.motion_watchdog.start()
        self.frame_timer = self.create_timer(.2, self.publish_frames)
        self.get_logger().info("pose6d bridge ready")

    def publish_frames(self):
        """Read-only display telemetry; never calls the motion/settle pipeline."""
        configured_models = getattr(self, "models", {})
        default_object_id = getattr(self, "default_object_id", None)
        event = {"type": "pose6d_frames", "arm": self.profile["arm"],
                 "base_frame": self.profile["base_frame"],
                 "workspace_m": self.motion_config.get("workspace_m"), "available": False,
                 "pose6d_models": [{"object_id": object_id, "label": model.get("label", object_id),
                                    "mesh_file": model.get("mesh_file"),
                                    "default": object_id == default_object_id}
                                   for object_id, model in configured_models.items()]}
        try:
            with self.motion_port.coordinate_lock:
                coordinates = self.motion_port.coordinates
                received = self.motion_port.coordinates_at
            if (coordinates is None or time.monotonic() - received > 2.5
                    or coordinates.get("tool_matched") is not True):
                raise ValueError("current controller tool state is missing, stale or unmatched")
            tf = self.tf.lookup_transform(self.profile["base_frame"], self.profile["flange_frame"], Time())
            stamp = int(tf.header.stamp.sec) * 1_000_000_000 + int(tf.header.stamp.nanosec)
            age = (self.get_clock().now().nanoseconds - stamp) * 1e-9
            if stamp <= 0 or age > self.capture_settings["maximum_joint_state_age_sec"] or age < -.01:
                raise ValueError("current flange transform is stale")
            p, q = tf.transform.translation, tf.transform.rotation
            base_flange = from_pose([p.x, p.y, p.z], [q.w, q.x, q.y, q.z])
            tool = coordinates["tool"]
            event.update(available=True, tool_name=coordinates["current_tool"], timestamp_ns=str(stamp),
                         T_base_tcp=(base_flange @ from_pose(tool["xyz_m"], tool["quaternion_wxyz"])).tolist())
            calibration = self.profile["calibration_data"]
            if (calibration.get("calibration_id") and calibration.get("intrinsics_sha256")
                    and all(calibration.get(key) == self.profile[key] for key in
                            ("camera_serial", "mount_id", "flange_frame", "color_frame"))):
                event["T_base_camera_live"] = (base_flange @ transform(calibration["T_flange_color"])).tolist()
        except Exception as exc:
            event.update(available=False, message=str(exc))
        self.emit("", "", event)

    def receive(self, name, message):
        with self.lock:
            self.inputs[name].append(message)

    def emit(self, client_id, request_id, payload):
        message = {"client_id": client_id, "request_id": request_id, **payload}
        encoded = json.dumps(message, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > WEB_EVENT_LIMIT:
            encoded = json.dumps({"client_id": client_id, "request_id": request_id, "type": "pose6d_error",
                                  "message": "result exceeds the Web event size limit"}, separators=(",", ":"))
        self.events.publish(String(data=encoded))

    def handle_command(self, message):
        try:
            command = json.loads(message.data)
            if not isinstance(command, dict) or command.get("type") not in (
                    "capture", "segment", "preview", "infer", "select_tool", "prepare", "jog",
                    "auto_suction", "stop", "close", "heartbeat", "disconnect", "clients"):
                raise ValueError("unknown command")
            if command["type"] == "clients":
                self.reconcile_clients(command.get("client_ids"))
                return
            if command["type"] == "heartbeat":
                # Accept heartbeats from pages opened before this bridge update.
                return
            if command["type"] in ("prepare", "jog", "auto_suction", "stop", "close", "heartbeat", "disconnect"):
                if command["type"] == "disconnect":
                    self.mark_disconnected(command.get("client_id", ""))
                threading.Thread(target=self.run_motion_command, args=(command,), daemon=True).start()
                return
            if command["type"] == "select_tool":
                if not self.busy.acquire(blocking=False):
                    self.emit(command.get("client_id", ""), command.get("request_id", ""),
                              {"type": "pose6d_error", "message": "bridge busy"})
                    return
                with self.owner_lock:
                    self.active_review_client = command.get("client_id", "")
                threading.Thread(target=self.run_tool_selection, args=(command,), daemon=True).start()
                return
            if not self.busy.acquire(blocking=False):
                self.emit(command.get("client_id", ""), command.get("request_id", ""),
                          {"type": "pose6d_error", "message": "bridge busy"})
                return
            with self.owner_lock:
                self.active_review_client = command.get("client_id", "")
            threading.Thread(target=self.run_command, args=(command,), daemon=True).start()
        except Exception as exc:
            self.get_logger().warning(f"invalid pose6d command: {exc}")

    def run_command(self, command):
        client, request_id = command.get("client_id", ""), command.get("request_id", "")
        with self.owner_lock:
            already_disconnected = client in self.disconnected_clients or (
                self.connected_clients is not None and client not in self.connected_clients)
            if not already_disconnected:
                self.active_review_client = client
        try:
            if already_disconnected:
                raise ValueError("browser client disconnected")
            if not isinstance(client, str) or not client:
                raise ValueError("client identity is required")
            if command["type"] == "capture":
                with self.owner_lock:
                    old_owner = self.observation_owner
                if old_owner not in (None, client):
                    raise ValueError("the current pose review belongs to another client")
                self.motion.invalidate_review(client)
                with self.owner_lock:
                    self.observation_owner = None
                if self.motion.status().get("lease_retained"):
                    raise ValueError("close the active suction motion session before capturing a new frame")
                result = self.capture(command.get("object_id"))
                with self.owner_lock:
                    if client not in self.disconnected_clients:
                        self.observation_owner = client
            else:
                with self.owner_lock:
                    current_owner = self.observation_owner
                if client != current_owner:
                    raise ValueError("observation belongs to a different client")
                if self.motion.status().get("lease_retained") and command["type"] != "preview":
                    raise ValueError("close the active suction motion session before changing the mask")
                if command["type"] != "preview":
                    self.motion.invalidate_review(client)
                if command["type"] == "segment":
                    result = self.segment(command, client)
                elif command["type"] == "preview":
                    result = self.instance_preview(command)
                else:
                    result = self.infer(command, client)
            self.emit(client, request_id, result)
        except Exception as exc:
            self.get_logger().warning(f"pose6d {command['type']} failed: {exc}")
            self.emit(client, request_id, {"type": "pose6d_error", "message": str(exc)})
        finally:
            with self.owner_lock:
                disconnected = client in self.disconnected_clients
                self.disconnected_clients.discard(client)
                if self.active_review_client == client:
                    self.active_review_client = None
                if disconnected and self.observation_owner == client:
                    self.observation_owner = None
            if disconnected:
                self.motion.detach(client)
                self.store.clear()
            self.busy.release()

    def run_tool_selection(self, command):
        client, request_id = command.get("client_id", ""), command.get("request_id", "")
        with self.owner_lock:
            disconnected = client in self.disconnected_clients or (
                self.connected_clients is not None and client not in self.connected_clients)
            current_owner = self.observation_owner
            if not disconnected:
                self.active_review_client = client
        try:
            if disconnected:
                raise ValueError("browser client disconnected")
            if current_owner not in (None, client):
                raise ValueError("the current pose review belongs to another client")
            result = self.motion.select_tool(client, command.get("tool_name"), self.selectable_tool_names)
            with self.owner_lock:
                if self.observation_owner == client:
                    self.observation_owner = None
            self.emit(client, request_id, result)
        except Exception as exc:
            self.get_logger().warning(f"pose6d select_tool failed: {exc}")
            self.emit(client, request_id, {"type": "pose6d_error", "message": str(exc)})
        finally:
            with self.owner_lock:
                self.disconnected_clients.discard(client)
                if self.active_review_client == client:
                    self.active_review_client = None
            self.busy.release()

    def motion_watchdog_loop(self):
        while rclpy.ok() and not self.motion_stop.is_set():
            try:
                self.motion.tick()
            except Exception as exc:
                self.get_logger().error(f"pose6d motion watchdog: {exc}")
            self.motion_stop.wait(.1)

    def run_motion_command(self, command):
        client, request_id = command.get("client_id", ""), command.get("request_id", "")
        kind = command.get("type")
        try:
            if not isinstance(client, str) or not client:
                raise ValueError("client identity is required")
            feedback = lambda event: self.emit(client, request_id, event)
            if kind == "prepare":
                result = self.motion.prepare(command, feedback)
            elif kind == "jog":
                result = self.motion.jog(command, feedback)
            elif kind == "auto_suction":
                result = self.motion.auto_suction(command, feedback)
            elif kind in ("stop", "close"):
                result = self.motion.stop(client, command.get("session_id"))
                if kind == "close":
                    self.mark_disconnected(client)
            elif kind == "heartbeat":
                return
            elif kind == "disconnect":
                self.mark_disconnected(client)
                return
            else:
                raise ValueError("unknown motion command")
            self.emit(client, request_id, result)
        except Exception as exc:
            self.get_logger().warning(f"pose6d motion {kind} failed: {exc}")
            self.emit(client, request_id, {"type": "pose6d_error", "message": str(exc),
                                           "motion": self.motion.status()})

    def mark_disconnected(self, client):
        clear_observation = False
        with self.owner_lock:
            if self.active_review_client == client:
                self.disconnected_clients.add(client)
            if self.observation_owner == client:
                self.observation_owner = None
                clear_observation = True
        if clear_observation:
            self.store.clear()
        self.motion.detach(client)

    def reconcile_clients(self, client_ids):
        """Release orphan reviews by socket membership, never observation age."""
        if (not isinstance(client_ids, list) or len(client_ids) > 128
                or any(not isinstance(client, str) or not client for client in client_ids)):
            raise ValueError("invalid connected client snapshot")
        with self.owner_lock:
            self.connected_clients = set(client_ids)
            missing = {self.observation_owner, self.active_review_client} - self.connected_clients - {None}
            clear_observation = self.observation_owner in missing
            if self.active_review_client in missing:
                self.disconnected_clients.add(self.active_review_client)
            if self.observation_owner in missing:
                self.observation_owner = None
        if clear_observation:
            self.store.clear()
        for client in missing:
            # Detach the review without interrupting an in-flight command.
            threading.Thread(target=self.run_motion_command,
                             args=({"type": "disconnect", "client_id": client},), daemon=True).start()

    def lookup(self, parent, child, timestamp):
        tf = self.tf.lookup_transform(parent, child, Time(nanoseconds=timestamp), timeout=Duration(seconds=.25))
        p, q = tf.transform.translation, tf.transform.rotation
        return from_pose([p.x, p.y, p.z], [q.w, q.x, q.y, q.z])

    def capture(self, object_id=None):
        settings = getattr(self, "settings", {})
        object_id = object_id or getattr(self, "default_object_id", settings.get("object_id"))
        models = getattr(self, "models", {})
        model_spec = models.get(object_id)
        if models and model_spec is None:
            raise ValueError("unknown 6D object selection")
        deadline = time.monotonic() + self.capture_settings["capture_timeout_sec"]
        not_before = self.get_clock().now().nanoseconds
        problem = "waiting for RGB-D and stationary arm"
        while time.monotonic() < deadline:
            try:
                with self.lock:
                    streams = {k: list(v) for k, v in self.inputs.items()}
                now = self.get_clock().now().nanoseconds
                # Reuse the existing settle checks without invoking its planning pipeline.
                missing = [name for name, samples in streams.items() if not samples]
                if missing:
                    raise ValueError(f"waiting for sensor input: missing {', '.join(missing)}")
                if not GraspBridge.stopped(self, streams["joints"], now):
                    raise ValueError("arm is moving or joint feedback is stale")
                color = streams["color"][-1]
                stamp = stamp_ns(color)
                if stamp < not_before:
                    raise ValueError("waiting for a new image")
                depth, ci, di = [min(streams[k], key=lambda item: abs(stamp_ns(item) - stamp))
                                 for k in ("depth", "color_info", "depth_info")]
                validate_timing([stamp_ns(item) for item in (color, depth, ci, di)], now,
                                skew_sec=self.capture_settings["maximum_pair_skew_sec"],
                                age_sec=self.capture_settings["maximum_capture_age_sec"])
                cm, dm = model(ci), model(di)
                if (cm.frame != self.profile["color_frame"] or color.header.frame_id != cm.frame
                        or dm.frame != self.profile["depth_frame"] or depth.header.frame_id != dm.frame):
                    raise ValueError("camera frame mismatch")
                calibration = self.profile["calibration_data"]
                if calibration.get("intrinsics_sha256") and camera_identity(cm) != calibration["intrinsics_sha256"]:
                    raise ValueError("camera intrinsics differ from hand-eye calibration")
                cd = self.lookup(cm.frame, dm.frame, stamp_ns(depth))
                bgr = rectify_color(image_array(color), cm)
                aligned = register_depth(image_array(depth, depth=True), dm, cm, cd).astype(np.float32)
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                base_camera, detail = self.base_camera(stamp)
                oid = self.store.freeze(rgb=rgb, depth_m=aligned, K=cm.K, timestamp_ns=stamp,
                                        camera_frame=cm.frame, T_base_camera=base_camera, object_id=object_id)
                depth_display = cv2.applyColorMap(cv2.convertScaleAbs(aligned, alpha=255 / 1.0), cv2.COLORMAP_TURBO)
                depth_display[aligned == 0] = 0
                return {"type": "pose6d_capture", "observation_id": oid, "timestamp_ns": str(stamp),
                        "camera_frame": cm.frame, "profile": self.settings["profile"],
                        "rgb": image_url(bgr), "depth": image_url(depth_display),
                        "width": rgb.shape[1], "height": rgb.shape[0], "calibration": detail,
                        "T_base_camera": base_camera.tolist() if base_camera is not None else None,
                        "base_frame": self.profile["base_frame"],
                        "workspace_m": self.motion_config.get("workspace_m"),
                        "object_id": object_id,
                        "model_sha256": model_spec["model_sha256"] if model_spec else getattr(self, "mesh_hash", ""),
                        "object_label": model_spec.get("label", object_id) if model_spec else object_id}
            except Exception as exc:
                problem = str(exc)
            time.sleep(.02)
        raise TimeoutError("capture failed: " + problem)

    def base_camera(self, stamp):
        cal = self.profile["calibration_data"]
        try:
            if any(cal.get(key) != self.profile[key] for key in ("camera_serial", "mount_id", "flange_frame", "color_frame")):
                raise ValueError("hand-eye camera identity mismatch")
            if not cal.get("calibration_id") or not cal.get("intrinsics_sha256"):
                raise ValueError("hand-eye calibration is incomplete")
            return self.lookup(self.profile["base_frame"], self.profile["flange_frame"], stamp) @ transform(cal["T_flange_color"]), "available"
        except Exception as exc:
            return None, str(exc)

    def _vision_call(self, uri, service, expected_hash, request, timeout_sec):
        from websockets.sync.client import connect

        started = time.perf_counter()
        with connect(uri, open_timeout=self.settings["vision_connect_timeout_sec"],
                     max_size=MAX_MESSAGE_BYTES, compression=None) as ws:
            hello = unpackb(ws.recv(timeout=self.settings["vision_connect_timeout_sec"]))
            if (hello.get("type") != "hello" or hello.get("version") != 1 or not hello.get("ready")
                    or hello.get("service") != service or hello.get("model_sha256") != expected_hash):
                raise ValueError(f"{service} backend identity or readiness mismatch")
            ws.send(packb(request))
            response = unpackb(ws.recv(timeout=timeout_sec))
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if not isinstance(response, dict):
            raise ValueError(f"{service} returned an invalid response")
        if (response.get("request_id") != request["request_id"]
                or response.get("observation_id") != request["observation_id"]):
            raise ValueError(f"{service} response identity mismatch")
        if response.get("type") != "result":
            raise ValueError(response.get("message", f"{service} inference failed"))
        if response.get("model_sha256") != expected_hash:
            raise ValueError(f"{service} response model identity mismatch")
        return response, elapsed_ms

    def _record_vision_timing(self, record):
        self.vision_log.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        os.chmod(self.vision_log.parent, 0o755)
        payload = json.dumps({"timestamp_utc": datetime.now(timezone.utc).isoformat(), **record},
                             separators=(",", ":"), allow_nan=False) + "\n"
        with self.vision_log_lock:
            encoded = payload.encode("utf-8")
            if self.vision_log.exists() and self.vision_log.stat().st_size + len(encoded) > VISION_LOG_MAX_BYTES:
                for index in range(VISION_LOG_BACKUPS - 1, 0, -1):
                    source = self.vision_log.with_name(f"{self.vision_log.name}.{index}")
                    if source.exists():
                        destination = self.vision_log.with_name(f"{self.vision_log.name}.{index + 1}")
                        source.replace(destination)
                        os.chmod(destination, 0o644)
                self.vision_log.replace(self.vision_log.with_name(f"{self.vision_log.name}.1"))
                os.chmod(self.vision_log.with_name(f"{self.vision_log.name}.1"), 0o644)
            descriptor = os.open(self.vision_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.fchmod(descriptor, 0o644)
                remaining = memoryview(encoded)
                while remaining:
                    remaining = remaining[os.write(descriptor, remaining):]
            finally:
                os.close(descriptor)

    def _persist_vision_timing(self, record):
        try:
            self._record_vision_timing(record)
        except (OSError, TypeError, ValueError) as exc:
            self.get_logger().warning(f"could not append pose6d vision timing log: {exc}")

    def segment(self, command, client):
        oid = command.get("observation_id")
        obs = self.store.get(oid)
        height, width = obs["rgb"].shape[:2]
        maximum_pixels = int(self.settings["vision_max_image_pixels"])
        if width * height > maximum_pixels:
            raise ValueError(f"image has {width * height} pixels; configured maximum is {maximum_pixels}")
        maximum_detections = int(self.settings["vision_max_detections"])
        if not 1 <= maximum_detections <= 16:
            raise ValueError("vision_max_detections must be from 1 through 16")
        maximum_mask_bytes = int(self.settings["vision_max_mask_bytes"])
        if not 1 <= maximum_mask_bytes <= 16 * 1024 * 1024:
            raise ValueError("vision_max_mask_bytes must be from 1 through 16777216")
        record = {
            "observation_id": oid,
            "request_id": command.get("request_id"),
            "image_size": [width, height],
            "mask_source": "sam2",
            "yolo_model_sha256": self.settings["yolo_model_sha256"],
            "sam2_model_sha256": self.settings["sam2_model_sha256"],
            "detection_count": 0,
            "successful_instances": 0,
            "failed_instances": 0,
            "status": "error",
        }
        started = time.perf_counter()
        try:
            self.emit(client, command.get("request_id", ""), {
                "type": "pose6d_vision_progress", "observation_id": oid,
                "stage": "yolo", "status": "running", "message": "YOLO 正在检测",
            })
            yolo_request = {
                "type": "detect", "version": 1, "request_id": hashlib.sha256(os.urandom(32)).hexdigest(),
                "observation_id": oid, "rgb": obs["rgb"],
                "confidence_threshold": float(self.settings["vision_confidence_threshold"]),
                "max_detections": maximum_detections,
            }
            detections, yolo_round_trip_ms = self._vision_call(
                self.settings["yolo_ws_uri"], "pose6d_yolo", self.settings["yolo_model_sha256"],
                yolo_request, self.settings["yolo_timeout_sec"])
            if detections.get("image_size") != [width, height]:
                raise ValueError("YOLO image dimensions differ from frozen observation")
            rows = detections.get("detections")
            if not isinstance(rows, list) or len(rows) > maximum_detections:
                raise ValueError("YOLO returned an invalid detection count")
            for row in rows:
                bbox = row.get("bbox") if isinstance(row, dict) else None
                if (not isinstance(bbox, list) or len(bbox) != 4 or any(type(v) is not int for v in bbox)
                        or not (0 <= bbox[0] < bbox[2] <= width and 0 <= bbox[1] < bbox[3] <= height)
                        or not isinstance(row.get("detection_id"), str)
                        or not isinstance(row.get("class_name"), str)
                        or isinstance(row.get("confidence"), bool)
                        or not isinstance(row.get("confidence"), (int, float))
                        or not 0 <= row["confidence"] <= 1):
                    raise ValueError("YOLO returned an invalid detection")
            record.update(yolo_inference_ms=detections.get("yolo_inference_ms"),
                          yolo_request_ms=detections.get("service_ms"),
                          yolo_round_trip_ms=yolo_round_trip_ms, detection_count=len(rows))
            self.store.get(oid)
            self.emit(client, command.get("request_id", ""), {
                "type": "pose6d_vision_progress", "observation_id": oid,
                "stage": "sam2", "status": "running", "detection_count": len(rows),
                "yolo_inference_ms": record["yolo_inference_ms"],
                "message": f"YOLO 已完成，{len(rows)} 个目标；SAM2 正在分割",
            })

            sam_request = {
                "type": "segment", "version": 1, "request_id": hashlib.sha256(os.urandom(32)).hexdigest(),
                "observation_id": oid, "rgb": obs["rgb"], "detections": rows,
            }
            masks, sam_round_trip_ms = self._vision_call(
                self.settings["sam2_ws_uri"], "pose6d_sam2", self.settings["sam2_model_sha256"],
                sam_request, self.settings["sam2_timeout_sec"])
            self.store.get(oid)
            if masks.get("image_size") != [width, height]:
                raise ValueError("SAM2 image dimensions differ from frozen observation")
            mask_results = masks.get("results")
            if not isinstance(mask_results, list) or len(mask_results) != len(rows):
                raise ValueError("SAM2 result count differs from YOLO detections")
            result_by_detection = {item.get("detection_id"): item for item in mask_results if isinstance(item, dict)}
            if len(result_by_detection) != len(rows):
                raise ValueError("SAM2 returned duplicate or invalid detection IDs")

            instances, metadata = {}, []
            total_mask_bytes = 0
            for row in rows:
                item = result_by_detection.get(row["detection_id"])
                if item is None:
                    raise ValueError("SAM2 omitted a detection result")
                instance_id = f"{oid}:{row['detection_id']}"
                stored = {**row, "status": item.get("status", "error"), "sam_score": item.get("sam_score"),
                          "sam_predict_ms": item.get("sam_predict_ms"), "message": item.get("message", "")}
                if stored["status"] == "ok":
                    mask = item.get("mask")
                    if not isinstance(mask, np.ndarray) or mask.dtype != np.uint8 or mask.shape != (height, width):
                        raise ValueError("SAM2 mask dimensions or dtype are invalid")
                    if not np.isin(mask, (0, 1)).all():
                        raise ValueError("SAM2 mask must contain only zero and one")
                    total_mask_bytes += mask.nbytes
                    if total_mask_bytes > maximum_mask_bytes:
                        raise ValueError("SAM2 masks exceed the configured bridge limit")
                    stored["mask"] = mask
                else:
                    stored["status"] = "error"
                    stored["mask"] = None
                instances[instance_id] = stored
            vision = {
                "yolo_inference_ms": record["yolo_inference_ms"],
                "yolo_request_ms": record["yolo_request_ms"],
                "yolo_round_trip_ms": yolo_round_trip_ms,
                "sam_image_ms": masks.get("sam_image_ms"),
                "sam_predict_ms_total": masks.get("sam_predict_ms_total"),
                "sam_inference_ms_total": masks.get("sam_inference_ms_total"),
                "sam_request_ms": masks.get("sam_request_ms"),
                "sam_round_trip_ms": sam_round_trip_ms,
                "yolo_model_sha256": self.settings["yolo_model_sha256"],
                "sam2_model_sha256": self.settings["sam2_model_sha256"],
            }
            self.store.set_instances(oid, instances, vision, max_count=maximum_detections,
                                     max_bytes=maximum_mask_bytes)
            for instance_id, item in instances.items():
                saved = self.store.instance(oid, instance_id)
                metadata.append({key: item.get(key) for key in
                                 ("detection_id", "bbox", "class_id", "class_name", "confidence", "status",
                                  "sam_score", "sam_predict_ms", "message")} |
                                {"instance_id": instance_id, "mask_pixels": saved["mask_pixels"],
                                 "valid_depth_pixels": saved["valid_depth_pixels"]})
            record.update(vision)
            record["successful_instances"] = sum(item["status"] == "ok" for item in metadata)
            record["failed_instances"] = len(metadata) - record["successful_instances"]
            record["status"] = "ok" if not record["failed_instances"] else "partial"
            record["total_ms"] = (time.perf_counter() - started) * 1000.0
            record["instances"] = metadata
            self._persist_vision_timing(record)
            annotated = cv2.cvtColor(obs["rgb"].copy(), cv2.COLOR_RGB2BGR)
            for item in metadata:
                x1, y1, x2, y2 = item["bbox"]
                cv2.rectangle(annotated, (x1, y1), (x2, y2), (35, 145, 170), 2)
            # Draw IDs last so other detection boxes cannot obscure the labels.
            # These observation-local IDs also prefix the browser's instance buttons.
            for item in metadata:
                text = f"#{item['detection_id']}"
                (tw, th), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, .75, 2)
                x = max(0, min(item["bbox"][0], width - tw - 10))
                y = max(0, min(item["bbox"][1], height - th - baseline - 10))
                cv2.rectangle(annotated, (x, y), (x + tw + 10, y + th + baseline + 10), (35, 75, 85), -1)
                cv2.putText(annotated, text, (x + 5, y + th + 5), cv2.FONT_HERSHEY_SIMPLEX,
                            .75, (255, 255, 255), 2, cv2.LINE_AA)
            return {"type": "pose6d_instances", "observation_id": oid, "detections": metadata,
                    "detection_overlay": image_url(annotated), "timings": vision,
                    "status": record["status"], "message": "SAM instances ready" if metadata else "No YOLO detections"}
        except Exception as exc:
            record["total_ms"] = (time.perf_counter() - started) * 1000.0
            record["error"] = str(exc)[:512]
            self._persist_vision_timing(record)
            raise

    def instance_preview(self, command):
        oid, instance_id = command.get("observation_id"), command.get("instance_id")
        obs = self.store.get(oid)
        item = self.store.instance(oid, instance_id)
        if item.get("status") != "ok" or item.get("mask") is None:
            raise ValueError("segmentation instance is unavailable")
        color = ((int(hashlib.sha256(instance_id.encode()).hexdigest()[:2], 16),
                  int(hashlib.sha256(instance_id.encode()).hexdigest()[2:4], 16),
                  int(hashlib.sha256(instance_id.encode()).hexdigest()[4:6], 16)))
        image = cv2.cvtColor(obs["rgb"].copy(), cv2.COLOR_RGB2BGR)
        overlay = image.copy()
        overlay[item["mask"] != 0] = color
        image = cv2.addWeighted(image, .58, overlay, .42, 0)
        x1, y1, x2, y2 = item["bbox"]
        cv2.rectangle(image, (x1, y1), (x2, y2), (0, 220, 255), 2)
        return {"type": "pose6d_instance_preview", "observation_id": oid, "instance_id": instance_id,
                "preview": image_url(image), "mask_pixels": item["mask_pixels"],
                "valid_depth_pixels": item["valid_depth_pixels"]}

    def infer(self, command, client=None):
        from websockets.sync.client import connect

        oid = command["observation_id"]
        obs = self.store.get(oid)
        object_id = command.get("object_id") or obs.get("object_id") or getattr(self, "default_object_id", "")
        models = getattr(self, "models", None)
        model = models.get(object_id) if isinstance(models, dict) else {
            "model_sha256": getattr(self, "mesh_hash", ""),
            "annotation": getattr(self, "annotation", None),
            "mesh_path": getattr(self, "mesh_dir", Path(".")) / "TexturedMeshSimplified.obj",
        }
        if models is not None and model is None:
            raise ValueError("unknown 6D OBJ model selection")
        if "instance_id" in command:
            self.store.select_instance(oid, command["instance_id"])
        else:
            raw = base64.b64decode(command["mask_png"], validate=True)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("mask is too large")
            if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
                raise ValueError("mask must be PNG")
            width, height = struct.unpack(">II", raw[16:24])
            if (height, width) != obs["depth_m"].shape:
                raise ValueError("mask dimensions differ from the observation")
            mask = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
            if mask is None or mask.ndim != 2 or mask.dtype != np.uint8:
                raise ValueError("mask must be a single-channel 8-bit PNG")
            self.store.set_mask(oid, mask)
        obs = self.store.get(oid)
        generation, request = self.store.request(oid, object_id=object_id, model_sha256=model["model_sha256"])
        with connect(self.settings["ws_uri"], open_timeout=self.settings["connect_timeout_sec"],
                     max_size=MAX_MESSAGE_BYTES, compression=None) as ws:
            hello = unpackb(ws.recv(timeout=self.settings["connect_timeout_sec"]))
            registered_models = hello.get("models")
            registered_hash = (registered_models.get(object_id, {}).get("model_sha256")
                               if isinstance(registered_models, dict) else
                               hello.get("model_sha256") if hello.get("object_id") == object_id else None)
            if (hello.get("type") != "hello" or not hello.get("ready")
                    or registered_hash != model["model_sha256"]):
                raise ValueError("backend model identity mismatch")
            ws.send(packb(request))
            result = unpackb(ws.recv(timeout=self.settings["inference_timeout_sec"]))
        if result.get("type") != "result":
            raise ValueError(result.get("message", "backend inference failed"))
        self.store.accept_result(generation, request, result)
        obs = self.store.get(oid)
        T_camera_object = np.asarray(result["T_camera_object"])
        points = self.cloud(obs)
        grasps = []
        annotation = model["annotation"]
        prepare_distance = self.motion_config.get("prepare_distance_m", .04)
        if (type(prepare_distance) not in (int, float) or not math.isfinite(prepare_distance)
                or prepare_distance <= 0):
            prepare_distance = .04
        second_prepare_distance = self.motion_config.get("second_prepare_distance_m", .005)
        if (type(second_prepare_distance) not in (int, float) or not math.isfinite(second_prepare_distance)
                or second_prepare_distance < 0):
            second_prepare_distance = .005
        contact_offset = self.motion_config.get("contact_offset_m", 0.0)
        if (type(contact_offset) not in (int, float) or not math.isfinite(contact_offset)
                or contact_offset < 0):
            contact_offset = 0.0
        lift_height = self.motion_config.get("lift_height_m", .05)
        if type(lift_height) not in (int, float) or not math.isfinite(lift_height) or lift_height <= 0:
            lift_height = .05
        place_joints = self.motion_config.get("place_joint_degrees", [])
        for grasp in (annotation or {}).get("grasps", []):
            if grasp.get("status") == "deprecated":
                continue
            contact, normal = suction_pose(T_camera_object, grasp["contact_region"])
            prepared = tcp_target(contact, normal, prepare_distance, np.eye(4))
            try:
                contact_tcp = configured_transform(self.motion_config["T_contact_tcp"])
                suction_tcp = tcp_target(contact, normal, -contact_offset, contact_tcp)
                prepared_tcp = tcp_target(contact, normal, prepare_distance, contact_tcp)
            except (KeyError, ValueError, TypeError):
                suction_tcp = None
                prepared_tcp = None
            assessment = assess_prepare_pose(obs["T_base_camera"], prepared, prepared_tcp,
                                             self.motion_config, self.settings["suction"])
            analytic_ik = self.analytic_ik_port.check(obs["T_base_camera"], suction_tcp, self.ik_flange_tcp)
            grasps.append({"grasp_id": grasp["grasp_id"], "status": grasp["status"],
                           "radius_m": grasp["contact_region"]["radius_m"],
                           "contact_region": grasp["contact_region"],
                           "T_camera_contact": contact.tolist(), "normal_camera": normal.tolist(),
                           "tangent_camera": (T_camera_object[:3, :3] @ np.asarray(grasp["contact_region"]["tangent_ref_obj"])).tolist(),
                           "tool_name": self.motion_config.get("tool_name", "tcpsuck"),
                           "analytic_ik": analytic_ik,
                           "tcp_targets": tcp_targets(obs["T_base_camera"], suction_tcp, prepared_tcp,
                                                      self.profile["base_frame"]),
                           "T_camera_suction_tcp": suction_tcp.tolist() if suction_tcp is not None else None,
                           "T_camera_prepare_contact": prepared.tolist(),
                           "T_camera_prepare_tcp": prepared_tcp.tolist() if prepared_tcp is not None else None,
                           **assessment})
        screen = result["screening"]
        overlay = screen.pop("overlay_png", None)
        result_id = result["request_id"]
        obs["result"] = result
        if annotation is not None and model.get("suction_enabled", True):
            ready, motion_reason = self.motion.load(client or self.observation_owner or "", obs, grasps, result_id)
        else:
            ready = False
            motion_reason = "该物体尚无吸盘接触标注，已禁用运动"
        return {"type": "pose6d_result", "observation_id": oid, "result_id": result_id,
                "instance_id": obs.get("selected_instance_id"),
                "mask_source": "sam2" if obs.get("selected_instance_id") else "manual",
                "vision_timings": obs.get("vision"),
                "history_id": result.get("history_id"), "history_path": result.get("history_path"),
                "archive_status": result.get("archive_status"), "archive_error": result.get("archive_error"),
                "T_camera_object": result["T_camera_object"],
                "T_base_camera": obs["T_base_camera"].tolist() if obs["T_base_camera"] is not None else None,
                "base_frame": self.profile["base_frame"],
                "workspace_m": self.motion_config.get("workspace_m"),
                "score": result["score"], "screening": screen, "grasps": grasps,
                "object_id": object_id, "object_label": model.get("label", object_id),
                "suction_available": annotation is not None and model.get("suction_enabled", True),
                "motion_waypoints": {"prepare_distance_mm": prepare_distance * 1000,
                                     "second_prepare_distance_mm": second_prepare_distance * 1000,
                                     "contact_offset_mm": contact_offset * 1000,
                                     "lift_height_mm": lift_height * 1000,
                                     "home_joint_degrees": self.motion_config.get("home_joint_degrees"),
                                     "place_joint_degrees": place_joints},
                "overlay": "data:image/png;base64," + base64.b64encode(overlay).decode("ascii") if overlay else None,
                "cloud": points, "mesh_obj": model["mesh_path"].read_text(encoding="utf-8"),
                "motion_ready": ready, "motion_reason": motion_reason,
                "suction": self.settings["suction"]}

    def cloud(self, obs):
        stride = int(self.settings["cloud_stride"])
        if stride < 1:
            raise ValueError("cloud_stride must be positive")
        depth = obs["depth_m"][::stride, ::stride]
        vv, uu = np.indices(depth.shape)
        z = depth.ravel()
        valid = np.isfinite(z) & (z > 0)
        u, v, z = uu.ravel()[valid] * stride, vv.ravel()[valid] * stride, z[valid]
        rays = np.linalg.inv(obs["K"]) @ np.vstack((u, v, np.ones_like(u)))
        xyz = (rays * (z / rays[2])).T
        colors = obs["rgb"][::stride, ::stride].reshape(-1, 3)[valid]
        if len(xyz) > 35000:
            indices = np.linspace(0, len(xyz) - 1, 35000, dtype=np.int64)
            xyz, colors = xyz[indices], colors[indices]
        return {"xyz_m": np.round(xyz, 5).tolist(), "rgb": colors.tolist()}


def main():
    rclpy.init()
    node = PoseBridge()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.motion_stop.set()
        node.motion_watchdog.join(timeout=1.0)
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
