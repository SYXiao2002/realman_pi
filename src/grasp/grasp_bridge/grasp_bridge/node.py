"""ROS Action server: capture -> infer -> restricted IK plan, without execution."""

from collections import deque
import json
import math
from pathlib import Path
import threading
import time
import uuid

import numpy as np
import rclpy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from geometry_msgs.msg import Pose, PoseArray
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.duration import Duration
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data, QoSProfile, DurabilityPolicy
from rclpy.time import Time
from realman_msgs.action import PlanGrasp
from realman_msgs.srv import SolveIk, TaskControl
from sensor_msgs.msg import CameraInfo, Image, JointState
from std_msgs.msg import String
from tf2_ros import Buffer, TransformListener

from grasp_protocol import PROTOCOL_VERSION, validate_request

from .geometry import from_pose, pose_drift, transform, to_pose
from .evaluation import snapshot
from .planner import choose_plan
from .profiles import camera_identity, default_config_root, load_profile, read_yaml
from .rgbd import CameraModel, image_array, rectify_color, register_depth, validate_timing
from .ws_client import PlanningCanceled, infer


def stamp_ns(message):
    return int(message.header.stamp.sec)*1_000_000_000 + int(message.header.stamp.nanosec)


def model(message):
    return CameraModel(message.width, message.height, message.header.frame_id,
                       np.array(message.k, dtype=float).reshape(3, 3), np.array(message.d, dtype=float),
                       message.distortion_model).validate()


class GraspBridge(Node):
    def __init__(self, **node_options):
        super().__init__("grasp_bridge", **node_options)
        self.declare_parameter("config_root", str(default_config_root()))
        self.root = Path(self.get_parameter("config_root").value)
        self.settings = read_yaml(self.root / "ros/grasp_bridge.yaml")
        self.declare_parameter("profile", self.settings["profile"])
        self.declare_parameter("dry_run", self.settings["dry_run"])
        self.profile_name = self.get_parameter("profile").value
        self.profile = load_profile(self.root, self.profile_name)
        self.dry_run = self.get_parameter("dry_run").value
        if type(self.dry_run) is not bool:
            raise ValueError("dry_run must be a boolean")
        for key in ("capture_timeout_sec", "inference_timeout_sec", "connect_timeout_sec", "ik_timeout_sec",
                    "maximum_pair_skew_sec", "maximum_capture_age_sec", "settle_sec",
                    "maximum_joint_velocity_rad_sec", "maximum_joint_state_age_sec", "maximum_view_tilt_rad",
                    "maximum_translation_drift_m", "maximum_rotation_drift_rad", "plan_validity_sec",
                    "maximum_ik_step_degrees", "cartesian_sample_step_m", "lease_status_timeout_sec"):
            value = self.settings.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be finite positive")
        self.group = ReentrantCallbackGroup()
        self.lock = threading.Lock()
        self.busy = False
        self.generation = 0
        self.inputs = {k: deque(maxlen=30) for k in ("color", "depth", "color_info", "depth_info", "joints")}
        self.profile_names = list(read_yaml(self.root / "ros/grasp_profiles.yaml")["profiles"])
        self.profile_inputs = {name: (self.inputs if name == self.profile_name else
                               {key: deque(maxlen=30) for key in self.inputs}) for name in self.profile_names}
        self.diagnostics = self.create_publisher(DiagnosticArray, "~/diagnostics", 10)
        self.plans = self.create_publisher(String, "~/plan", 10)
        review_qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.evaluations = self.create_publisher(String, "~/evaluation", review_qos)
        self.review_status = self.create_publisher(String, "~/review_status", review_qos)
        self.review_status.publish(String(data=json.dumps({
            "type": "grasp_bridge_status", "dry_run": self.dry_run, "read_only": True,
            "default_profile": self.profile_name, "profiles": [
                {"name": name, "arm": load_profile(self.root, name)["arm"]} for name in self.profile_names]})))
        self.candidates = self.create_publisher(PoseArray, "~/candidates", 10)
        self.overlay = self.create_publisher(Image, "~/overlay", 2)
        self.server = ActionServer(self, PlanGrasp, "~/plan_grasp", execute_callback=self.execute,
                                   goal_callback=self.goal, cancel_callback=lambda _: CancelResponse.ACCEPT,
                                   callback_group=self.group)
        # Dry-run allows read-only capture and inference. IK and lease clients
        # remain absent, so no motion planning/execution path is available.
        self.tf = Buffer(cache_time=Duration(seconds=60))
        self.tf_listener = TransformListener(self.tf, self)
        if not self.dry_run:
            self.ik = self.create_client(SolveIk, f"/{self.profile['arm']}/solve_ik", callback_group=self.group)
            self.task_control = self.create_client(
                TaskControl, f"/{self.profile['arm']}/task_control", callback_group=self.group)
        self.subscriptions_owned = []
        for name in self.profile_names:
            profile = load_profile(self.root, name)
            for key, message_type, topic in (
                ("color", Image, "color_topic"), ("depth", Image, "depth_topic"),
                ("color_info", CameraInfo, "color_info_topic"), ("depth_info", CameraInfo, "depth_info_topic"),
                ("joints", JointState, "joint_topic"),
            ):
                self.subscriptions_owned.append(self.create_subscription(
                    message_type, profile[topic], lambda msg, k=key, n=name: self.receive(k, msg, n),
                    qos_profile_sensor_data, callback_group=self.group))
        self.report("configuration", "; ".join(self.profile["readiness_problems"]) or "ready", warning=True)
        self.get_logger().info(f"Planning-only grasp bridge profile={self.profile_name} dry_run={self.dry_run}")

    def report(self, phase, detail, *, warning=False):
        message = DiagnosticArray()
        message.header.stamp = self.get_clock().now().to_msg()
        status = DiagnosticStatus()
        status.name = "grasp_bridge/" + self.profile_name
        status.level = DiagnosticStatus.WARN if warning else DiagnosticStatus.OK
        status.message = phase
        status.values = [KeyValue(key="detail", value=detail)]
        message.status = [status]
        self.diagnostics.publish(message)

    def receive(self, key, message, profile_name=None):
        with self.lock:
            self.profile_inputs[profile_name or self.profile_name][key].append(message)

    def goal(self, request):
        with self.lock:
            allowed = self.profile_names if request.evaluation_only else [self.profile_name]
            if self.busy or (request.profile_name and request.profile_name not in allowed) or not request.task_id:
                return GoalResponse.REJECT
            self.busy = True
            self.generation += 1
        return GoalResponse.ACCEPT

    def feedback(self, handle, phase, detail, count=0):
        message = PlanGrasp.Feedback()
        message.phase, message.detail, message.candidate_count = phase, detail, count
        handle.publish_feedback(message)
        self.report(phase, detail)

    def lookup(self, parent, child, timestamp_ns):
        value = self.tf.lookup_transform(parent, child, Time(nanoseconds=timestamp_ns), timeout=Duration(seconds=0.25))
        if timestamp_ns == 0:
            now_ns = self.get_clock().now().nanoseconds
            stamp = stamp_ns(value)
            if (stamp <= 0 or stamp > now_ns + 10_000_000
                    or now_ns - stamp > self.settings["maximum_joint_state_age_sec"] * 1e9):
                raise ValueError("latest flange TF is stale or future; pose drift cannot be verified")
        p, q = value.transform.translation, value.transform.rotation
        return from_pose([p.x, p.y, p.z], [q.w, q.x, q.y, q.z])

    def stopped(self, messages, now_ns):
        if len(messages) < 2:
            return False
        # A source clock ahead of ROS time must never establish stationarity.
        if any(stamp_ns(m) <= 0 or stamp_ns(m) > now_ns + 10_000_000 for m in messages):
            return False
        recent = [m for m in messages if now_ns-stamp_ns(m) <= (self.settings["settle_sec"]+0.3)*1e9]
        if len(recent) < 2 or now_ns-stamp_ns(recent[-1]) > self.settings["maximum_joint_state_age_sec"]*1e9:
            return False
        if stamp_ns(recent[-1])-stamp_ns(recent[0]) < self.settings["settle_sec"]*1e9:
            return False
        if any(list(m.name) != list(recent[-1].name) or len(m.position) != 6
               or len(m.name) != 6 or len(set(m.name)) != 6 for m in recent):
            return False
        times = np.array([stamp_ns(m) for m in recent], dtype=float)*1e-9
        positions = np.array([m.position for m in recent], dtype=float)
        dt = np.diff(times)
        if not np.isfinite(positions).all() or np.any(dt <= 0) or np.max(dt) > self.settings["maximum_joint_state_age_sec"]:
            return False
        return bool(np.max(np.abs(np.diff(positions, axis=0)/dt[:, None])) <=
                    self.settings["maximum_joint_velocity_rad_sec"])

    def capture(self, canceled, not_before_ns, *, evaluation_only=False):
        deadline = time.monotonic()+self.settings["capture_timeout_sec"]
        problem = "waiting for paired RGB-D and stopped arm"
        while time.monotonic() < deadline:
            if canceled():
                raise PlanningCanceled("capture canceled")
            with self.lock:
                streams = {k: list(v) for k, v in self.inputs.items()}
            now = self.get_clock().now().nanoseconds
            try:
                if not all(streams.values()) or not self.stopped(streams["joints"], now):
                    raise ValueError("arm not stopped or fresh joint feedback missing")
                color = streams["color"][-1]
                capture_ns = stamp_ns(color)
                if capture_ns < not_before_ns:
                    raise ValueError("waiting for a new observation")
                depth, ci, di = [min(streams[k], key=lambda m: abs(stamp_ns(m)-capture_ns))
                                 for k in ("depth", "color_info", "depth_info")]
                validate_timing([stamp_ns(m) for m in (color, depth, ci, di)], now,
                                skew_sec=self.settings["maximum_pair_skew_sec"], age_sec=self.settings["maximum_capture_age_sec"])
                cm, dm = model(ci), model(di)
                if (cm.frame != self.profile["color_frame"] or color.header.frame_id != cm.frame
                        or dm.frame != self.profile["depth_frame"] or depth.header.frame_id != dm.frame):
                    raise ValueError("image/CameraInfo optical frame identity mismatch")
                approved_intrinsics = self.profile["calibration_data"].get("intrinsics_sha256")
                if approved_intrinsics and camera_identity(cm) != approved_intrinsics:
                    raise ValueError("color intrinsics differ from approved hand-eye calibration")
                try:
                    bc = self.lookup(self.profile["base_frame"], self.profile["flange_frame"], capture_ns) @ transform(
                        self.profile["calibration_data"]["T_flange_color"])
                except (KeyError, TypeError, ValueError, LookupError):
                    if not evaluation_only:
                        raise
                    # Keep the RGB-D observation reviewable when hand-eye data is
                    # not approved.  No base-frame candidate pose is fabricated.
                    bc = None
                cd = self.lookup(cm.frame, dm.frame, stamp_ns(depth))
                up = bc[:3, :3].T @ np.array([0., 0., 1.]) if bc is not None else None
                if up is not None and -up[2] < math.cos(self.settings["maximum_view_tilt_rad"]):
                    raise ValueError("observation is outside verified near-topdown view range")
                color_bgr = rectify_color(image_array(color), cm)
                aligned = register_depth(image_array(depth, depth=True), dm, cm, cd)
                if np.count_nonzero(aligned) < 64:
                    raise ValueError("insufficient registered depth")
                return {"color_bgr": color_bgr, "depth_m": aligned, "K": cm.K,
                        "up_camera": up, "capture_timestamp_ns": capture_ns,
                        "T_base_camera": bc, "seed": np.rad2deg(streams["joints"][-1].position).tolist(),
                        "source_header": color.header}
            except (ValueError, LookupError) as exc:
                problem = str(exc)
            except Exception as exc:
                # TF may briefly lag the image. Retry only inside capture timeout.
                problem = str(exc)
            time.sleep(0.02)
        raise TimeoutError("capture failed: " + problem)

    def solve(self, T, seed, canceled):
        if not self.ik.wait_for_service(timeout_sec=self.settings["ik_timeout_sec"]):
            raise ValueError("IK service unavailable")
        req = SolveIk.Request()
        req.reference_type = SolveIk.Request.BASE
        req.reference_name = "base"
        req.seed_joint_degrees = seed
        req.pose_position_m, req.pose_quaternion_wxyz = to_pose(T)
        future = self.ik.call_async(req)
        deadline = time.monotonic()+self.settings["ik_timeout_sec"]
        while not future.done():
            if canceled() or time.monotonic() > deadline:
                self.ik.remove_pending_request(future)
                raise ValueError("IK canceled or timed out")
            time.sleep(0.01)
        result = future.result()
        if not result.success or result.api2_status != 0:
            raise ValueError("IK rejected pose: " + result.message)
        return result.joint_degrees

    def validate_lease(self, token, canceled):
        # Standalone planning may omit ownership; a BT-owned request must never
        # turn a revoked/expired task into a fresh executable plan. STATUS is
        # read-only and does not renew or reserve an arm command.
        if canceled():
            raise PlanningCanceled("planning canceled")
        if not token:
            return
        timeout = self.settings["lease_status_timeout_sec"]
        if not self.task_control.wait_for_service(timeout_sec=timeout):
            raise ValueError("task lease status service unavailable")
        request = TaskControl.Request()
        request.operation, request.task_token = TaskControl.Request.STATUS, token
        future = self.task_control.call_async(request)
        deadline = time.monotonic() + timeout
        while not future.done():
            if canceled() or time.monotonic() >= deadline:
                self.task_control.remove_pending_request(future)
                raise ValueError("task lease validation canceled or timed out")
            time.sleep(0.01)
        response = future.result()
        if not response.success or not response.leased or response.expired:
            raise ValueError("task lease revoked or expired; plan invalidated")

    def visualize(self, response, observation):
        import cv2
        poses = PoseArray()
        poses.header = observation["source_header"]
        overlay = observation["color_bgr"].copy()
        for candidate in response["candidates"]:
            T = transform(candidate["T_camera_grasp"])
            pos, q = to_pose(T)
            pose = Pose()
            pose.position.x, pose.position.y, pose.position.z = pos
            pose.orientation.w, pose.orientation.x, pose.orientation.y, pose.orientation.z = q
            poses.poses.append(pose)
            uv = observation["K"] @ T[:3, 3]
            u, v = (int(round(x)) for x in uv[:2]/uv[2])
            cv2.circle(overlay, (u, v), 6, (0, 255, 0), 2)
        self.candidates.publish(poses)
        message = Image()
        message.header = observation["source_header"]
        message.height, message.width = overlay.shape[:2]
        message.encoding, message.step = "bgr8", overlay.shape[1]*3
        message.data = overlay.tobytes()
        self.overlay.publish(message)
        observation["overlay_bgr"] = overlay

    def execute(self, handle):
        result = PlanGrasp.Result()
        observation = request = response = None
        generation = self.generation
        canceled = lambda: handle.is_cancel_requested or self.generation != generation
        try:
            if self.dry_run and not handle.request.evaluation_only:
                raise ValueError("configuration dry-run; no hardware clients or observation: " +
                                 "; ".join(self.profile["readiness_problems"]))
            selected_profile = (handle.request.profile_name or self.profile_name) if handle.request.evaluation_only else self.profile_name
            self.profile = load_profile(self.root, selected_profile,
                                        require_ready=not handle.request.evaluation_only)
            self.inputs = self.profile_inputs[selected_profile]
            backend = handle.request.backend or self.settings["backend"]
            if backend == "mock":
                raise ValueError("mock candidates cannot produce live robot plans")
            if not handle.request.target_label:
                raise ValueError("target_label must be explicit")
            if not handle.request.evaluation_only:
                self.validate_lease(handle.request.lease_token, canceled)
            self.feedback(handle, "capture", "waiting for new paired observation")
            observation = self.capture(canceled, self.get_clock().now().nanoseconds,
                                       evaluation_only=handle.request.evaluation_only)
            request = {"type": "infer", "protocol_version": PROTOCOL_VERSION,
                       "task_id": handle.request.task_id, "request_id": uuid.uuid4().hex,
                       "observation_id": uuid.uuid4().hex, "generation": generation,
                       "capture_timestamp_ns": observation["capture_timestamp_ns"],
                       "camera_frame": self.profile["color_frame"], "config_id": self.profile["profile_id"],
                       "target": {"class_name": handle.request.target_label}, "backend": backend,
                       "gripper": self.profile["gripper_geometry"], "max_candidates": self.settings["max_candidates"],
                       **{k: observation[k] for k in ("color_bgr", "depth_m", "K", "up_camera")}}
            if handle.request.evaluation_only:
                self.publish_evaluation(observation, request, phase="captured")
            validate_request(request)
            if not handle.request.evaluation_only:
                self.validate_lease(handle.request.lease_token, canceled)
            self.feedback(handle, "infer", "immutable capture sent to algorithm service")
            response = infer(self.settings["ws_uri"], request, expected_model_id=self.settings["expected_model_id"],
                             connect_timeout_sec=self.settings["connect_timeout_sec"],
                             inference_timeout_sec=self.settings["inference_timeout_sec"], canceled=canceled)
            if not handle.request.evaluation_only:
                self.validate_lease(handle.request.lease_token, canceled)
            self.visualize(response, observation)
            if handle.request.evaluation_only:
                if canceled():
                    raise PlanningCanceled("evaluation canceled")
                self.publish_evaluation(observation, request, response=response, phase="inferred",
                                        detail="evaluation only; no IK or waypoints")
                result.success = True
                result.observation_id = request["observation_id"]
                result.profile_id = self.profile["profile_id"]
                result.capture_timestamp_ns = observation["capture_timestamp_ns"]
                result.base_frame = self.profile["base_frame"]
                result.tool_name = self.profile["tool_name"]
                result.message = "evaluation complete; no IK, waypoints, robot or gripper commands"
                result.diagnostics_json = json.dumps({"observation_id": request["observation_id"],
                                                      "candidate_count": len(response["candidates"]),
                                                      "read_only": True})
                handle.succeed()
                self.report("evaluation", f"{request['observation_id']}: {len(response['candidates'])} candidates")
                return result
            self.feedback(handle, "filter", "checking restricted corridor and IK", len(response["candidates"]))
            plan = choose_plan(response["candidates"], self.profile, observation["T_base_camera"], observation["seed"],
                               lambda T, seed: self.solve(T, seed, canceled),
                               sample_step_m=self.settings["cartesian_sample_step_m"],
                               max_joint_step=self.settings["maximum_ik_step_degrees"], canceled=canceled)
            with self.lock:
                current_joints = list(self.inputs["joints"])
            if not self.stopped(current_joints, self.get_clock().now().nanoseconds):
                raise ValueError("arm is moving or joint feedback is stale after planning")
            current = self.lookup(self.profile["base_frame"], self.profile["flange_frame"], 0) @ transform(
                self.profile["calibration_data"]["T_flange_color"])
            translation, rotation = pose_drift(observation["T_base_camera"], current)
            expires = observation["capture_timestamp_ns"] + int(self.settings["plan_validity_sec"]*1e9)
            if (translation > self.settings["maximum_translation_drift_m"]
                    or rotation > self.settings["maximum_rotation_drift_rad"]
                    or self.get_clock().now().nanoseconds >= expires):
                raise ValueError("captured scene/pose has expired; observe again")
            if load_profile(self.root, self.profile_name)["profile_id"] != self.profile["profile_id"] or canceled():
                raise PlanningCanceled("profile changed or task canceled; plan invalidated")
            self.validate_lease(handle.request.lease_token, canceled)
            result.success = True
            result.plan_id, result.observation_id = uuid.uuid4().hex, request["observation_id"]
            result.profile_id = self.profile["profile_id"]
            result.capture_timestamp_ns, result.expires_timestamp_ns = observation["capture_timestamp_ns"], expires
            result.base_frame, result.tool_name = self.profile["base_frame"], self.profile["tool_name"]
            for name, waypoint in plan["waypoints"].items():
                for field in ("position_m", "quaternion_wxyz", "joint_degrees"):
                    setattr(result, name + "_" + field, waypoint[field])
            result.message = "planned; restricted corridor only, physical grasp unverified"
            result.diagnostics_json = json.dumps({"plan_id": result.plan_id, "observation_id": result.observation_id,
                                                 "profile_id": result.profile_id, **plan, "server": response.get("diagnostics", {})})
            self.plans.publish(String(data=result.diagnostics_json))
            handle.succeed()
            self.report("planned", result.plan_id)
        except Exception as exc:
            if handle.request.evaluation_only and observation is not None and request is not None:
                self.publish_evaluation(observation, request, response=response, phase="rejected", detail=str(exc))
            result.success, result.message = False, str(exc)
            if handle.is_cancel_requested:
                handle.canceled()
            else:
                handle.abort()
            self.get_logger().warning(f"Grasp planning rejected: {exc}")
            self.report("rejected", str(exc), warning=True)
        finally:
            with self.lock:
                self.busy = False
        return result

    def publish_evaluation(self, observation, request, **values):
        try:
            evaluation = snapshot(observation, self.profile, request, **values)
            self.evaluations.publish(String(data=json.dumps(evaluation, separators=(",", ":"))))
        except (TypeError, ValueError, KeyError) as exc:
            self.get_logger().warning(f"Unable to publish grasp evaluation snapshot: {exc}")


def main(args=None):
    rclpy.init(args=args)
    node = GraspBridge()
    executor = MultiThreadedExecutor(num_threads=6)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()
