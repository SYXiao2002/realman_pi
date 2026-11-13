"""ROS RGB-D capture and bounded browser-facing pose review events."""

import base64
from collections import deque
import hashlib
import json
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
from .motion_ros import MotionRosPort

WEB_EVENT_LIMIT = 8 * 1024 * 1024


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
        self.observation_owner = None
        self.owner_lock = threading.Lock()
        self.disconnected_clients = set()
        self.connected_clients = None  # Unknown until Web control's first socket snapshot.
        self.active_review_client = None
        self.motion_config = self.settings["motion"]
        coordinate_arm = read_yaml(self.root / "ros/realman_coordinates.yaml")["robots"][self.profile["arm"]]
        self.selectable_tool_names = {self.profile["tool_name"], self.motion_config["tool_name"]}
        if not self.selectable_tool_names.issubset(coordinate_arm["tools"]):
            raise ValueError("pose6d tool choices must be registered in realman_coordinates.yaml")
        self.mesh_dir = Path(self.settings["mesh_dir"])
        self.mesh_hash = hashlib.sha256((self.mesh_dir / "TexturedMeshSimplified.obj").read_bytes()).hexdigest()
        self.annotation = json.loads((self.mesh_dir / "eu_plug_grasps.json").read_text(encoding="utf-8"))
        if (self.annotation.get("object_id") != self.settings["object_id"]
                or self.annotation.get("normal_convention") != "outward_surface"):
            raise ValueError("suction annotation identity or normal convention mismatch")
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
        event = {"type": "pose6d_frames", "arm": self.profile["arm"],
                 "base_frame": self.profile["base_frame"],
                 "workspace_m": self.motion_config.get("workspace_m"), "available": False}
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
                    "capture", "infer", "select_tool", "prepare", "jog", "stop", "close", "heartbeat", "disconnect", "clients"):
                raise ValueError("unknown command")
            if command["type"] == "clients":
                self.reconcile_clients(command.get("client_ids"))
                return
            if command["type"] == "heartbeat":
                # Accept heartbeats from pages opened before this bridge update.
                return
            if command["type"] in ("prepare", "jog", "stop", "close", "heartbeat", "disconnect"):
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
                result = self.capture()
                with self.owner_lock:
                    if client not in self.disconnected_clients:
                        self.observation_owner = client
            else:
                with self.owner_lock:
                    current_owner = self.observation_owner
                if client != current_owner:
                    raise ValueError("observation belongs to a different client")
                if self.motion.status().get("lease_retained"):
                    raise ValueError("close the active suction motion session before changing the mask")
                self.motion.invalidate_review(client)
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
        with self.owner_lock:
            if self.active_review_client == client:
                self.disconnected_clients.add(client)
            if self.observation_owner == client:
                self.observation_owner = None
        self.motion.detach(client)

    def reconcile_clients(self, client_ids):
        """Release orphan reviews by socket membership, never observation age."""
        if (not isinstance(client_ids, list) or len(client_ids) > 128
                or any(not isinstance(client, str) or not client for client in client_ids)):
            raise ValueError("invalid connected client snapshot")
        with self.owner_lock:
            self.connected_clients = set(client_ids)
            missing = {self.observation_owner, self.active_review_client} - self.connected_clients - {None}
            if self.active_review_client in missing:
                self.disconnected_clients.add(self.active_review_client)
            if self.observation_owner in missing:
                self.observation_owner = None
        for client in missing:
            # Detach the review without interrupting an in-flight command.
            threading.Thread(target=self.run_motion_command,
                             args=({"type": "disconnect", "client_id": client},), daemon=True).start()

    def lookup(self, parent, child, timestamp):
        tf = self.tf.lookup_transform(parent, child, Time(nanoseconds=timestamp), timeout=Duration(seconds=.25))
        p, q = tf.transform.translation, tf.transform.rotation
        return from_pose([p.x, p.y, p.z], [q.w, q.x, q.y, q.z])

    def capture(self):
        deadline = time.monotonic() + self.capture_settings["capture_timeout_sec"]
        not_before = self.get_clock().now().nanoseconds
        problem = "waiting for RGB-D and stationary arm"
        while time.monotonic() < deadline:
            try:
                with self.lock:
                    streams = {k: list(v) for k, v in self.inputs.items()}
                now = self.get_clock().now().nanoseconds
                # Reuse the existing settle checks without invoking its planning pipeline.
                if not all(streams.values()) or not GraspBridge.stopped(self, streams["joints"], now):
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
                                        camera_frame=cm.frame, T_base_camera=base_camera)
                depth_display = cv2.applyColorMap(cv2.convertScaleAbs(aligned, alpha=255 / 1.0), cv2.COLORMAP_TURBO)
                depth_display[aligned == 0] = 0
                return {"type": "pose6d_capture", "observation_id": oid, "timestamp_ns": str(stamp),
                        "camera_frame": cm.frame, "profile": self.settings["profile"],
                        "rgb": image_url(bgr), "depth": image_url(depth_display),
                        "width": rgb.shape[1], "height": rgb.shape[0], "calibration": detail,
                        "T_base_camera": base_camera.tolist() if base_camera is not None else None,
                        "base_frame": self.profile["base_frame"],
                        "workspace_m": self.motion_config.get("workspace_m"),
                        "object_id": self.settings["object_id"], "model_sha256": self.mesh_hash}
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

    def infer(self, command, client=None):
        from websockets.sync.client import connect

        oid = command["observation_id"]
        raw = base64.b64decode(command["mask_png"], validate=True)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("mask is too large")
        obs = self.store.get(oid)
        if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("mask must be PNG")
        width, height = struct.unpack(">II", raw[16:24])
        if (height, width) != obs["depth_m"].shape:
            raise ValueError("mask dimensions differ from the observation")
        mask = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if mask is None or mask.ndim != 2 or mask.dtype != np.uint8:
            raise ValueError("mask must be a single-channel 8-bit PNG")
        self.store.set_mask(oid, mask)
        generation, request = self.store.request(oid, object_id=self.settings["object_id"], model_sha256=self.mesh_hash)
        with connect(self.settings["ws_uri"], open_timeout=self.settings["connect_timeout_sec"],
                     max_size=MAX_MESSAGE_BYTES, compression=None) as ws:
            hello = unpackb(ws.recv(timeout=self.settings["connect_timeout_sec"]))
            if (hello.get("type") != "hello" or not hello.get("ready")
                    or hello.get("model_sha256") != self.mesh_hash or hello.get("object_id") != self.settings["object_id"]):
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
        for grasp in self.annotation["grasps"]:
            if grasp.get("status") == "deprecated":
                continue
            contact, normal = suction_pose(T_camera_object, grasp["contact_region"])
            prepared = tcp_target(contact, normal, .04, np.eye(4))
            try:
                prepared_tcp = tcp_target(contact, normal, .04, configured_transform(self.motion_config["T_contact_tcp"]))
            except (KeyError, ValueError, TypeError):
                prepared_tcp = None
            grasps.append({"grasp_id": grasp["grasp_id"], "status": grasp["status"],
                           "radius_m": grasp["contact_region"]["radius_m"],
                           "contact_region": grasp["contact_region"],
                           "T_camera_contact": contact.tolist(), "normal_camera": normal.tolist(),
                           "tangent_camera": (T_camera_object[:3, :3] @ np.asarray(grasp["contact_region"]["tangent_ref_obj"])).tolist(),
                           "T_camera_prepare_contact": prepared.tolist(),
                           "T_camera_prepare_tcp": prepared_tcp.tolist() if prepared_tcp is not None else None})
        screen = result["screening"]
        overlay = screen.pop("overlay_png", None)
        result_id = result["request_id"]
        obs["result"] = result
        ready, motion_reason = self.motion.load(client or self.observation_owner or "", obs, grasps, result_id)
        return {"type": "pose6d_result", "observation_id": oid, "result_id": result_id,
                "T_camera_object": result["T_camera_object"],
                "T_base_camera": obs["T_base_camera"].tolist() if obs["T_base_camera"] is not None else None,
                "base_frame": self.profile["base_frame"],
                "workspace_m": self.motion_config.get("workspace_m"),
                "score": result["score"], "screening": screen, "grasps": grasps,
                "overlay": "data:image/png;base64," + base64.b64encode(overlay).decode("ascii") if overlay else None,
                "cloud": points, "mesh_obj": (self.mesh_dir / "TexturedMeshSimplified.obj").read_text(encoding="utf-8"),
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
