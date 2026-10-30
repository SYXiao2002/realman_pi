#!/usr/bin/env python3
"""On-demand camera snapshot service for behavior trees.

The behavior-tree executor is a lean C++ process without OpenCV/cv_bridge, so a
``SaveCameraFrames`` leaf delegates the actual capture here. On each request this
node subscribes to the requested image topics, waits for one fresh frame per topic,
JPEG-encodes it through the recorder's tested ``image_to_jpeg`` helper, and writes
``<camera>_<timestamp>.jpg`` files into the requested directory. Subscriptions are
created per request and destroyed again, so the server never maintains its own copy
of the camera configuration and cannot drift from the topics a tree asks for.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from realman_msgs.srv import SaveCameraImages
from realman_recording.camera_workers import image_to_jpeg
from sensor_msgs.msg import Image

# Poll interval while waiting for one fresh frame per requested topic.
_CAPTURE_POLL_SEC = 0.02


class CameraSnapshotServer(Node):
    """Saves the current frame of one or more cameras into a folder."""

    def __init__(self) -> None:
        super().__init__("camera_snapshot_server")
        self._callback_group = ReentrantCallbackGroup()
        # Serializes snapshot requests; a second concurrent request is refused
        # rather than interleaving subscriptions and file writes.
        self._operation_lock = threading.Lock()

        self.declare_parameter("save_service", "/camera_snapshot/save")
        self.declare_parameter("capture_timeout_sec", 5.0)
        self.declare_parameter("jpeg_quality", 90)
        self.declare_parameter("default_directory", "")

        self._default_timeout = float(self.get_parameter("capture_timeout_sec").value)
        self._jpeg_quality = int(self.get_parameter("jpeg_quality").value)
        self._default_directory = str(self.get_parameter("default_directory").value)

        service_name = str(self.get_parameter("save_service").value)
        self._service = self.create_service(
            SaveCameraImages,
            service_name,
            self._handle_save,
            callback_group=self._callback_group,
        )
        self.get_logger().info(
            f"camera snapshot service ready on {service_name} "
            f"(default timeout {self._default_timeout:.1f}s, jpeg quality {self._jpeg_quality})"
        )

    def _handle_save(
        self, request: SaveCameraImages.Request, response: SaveCameraImages.Response
    ) -> SaveCameraImages.Response:
        topics = [str(topic) for topic in request.image_topics if str(topic).strip()]
        directory = str(request.directory).strip() or self._default_directory
        timeout = float(request.timeout_sec) if request.timeout_sec > 0.0 else self._default_timeout
        session_id = str(request.session_id).strip()

        if not topics:
            return self._fail(response, "no image topics requested")
        if not directory:
            return self._fail(response, "no directory given and default_directory is unset")
        if not os.path.isabs(directory):
            return self._fail(response, f"directory must be absolute: {directory}")
        if timeout <= 0.0:
            return self._fail(response, f"capture timeout must be positive, got {timeout}")

        if not self._operation_lock.acquire(blocking=False):
            return self._fail(response, "another snapshot is already in progress")
        try:
            return self._capture(topics, directory, timeout, session_id, response)
        finally:
            self._operation_lock.release()

    def _capture(
        self,
        topics: list[str],
        directory: str,
        timeout: float,
        session_id: str,
        response: SaveCameraImages.Response,
    ) -> SaveCameraImages.Response:
        self.get_logger().info(
            f"capturing {len(topics)} camera frame(s) into {directory}: {', '.join(topics)}"
        )
        # Phase 1: subscribe, wait for one frame per topic, encode in memory. Encoding
        # before any write keeps a partial capture from leaving files on disk.
        frames = self._gather_frames(topics, timeout)
        missing = [topic for topic in topics if frames.get(topic) is None]
        subscriptions = frames.pop("__subscriptions__", [])
        for subscription in subscriptions:
            self.destroy_subscription(subscription)
        if missing:
            return self._fail(
                response,
                f"no frame within {timeout:.1f}s for: {', '.join(missing)}",
            )

        encoded: list[tuple[str, bytes]] = []
        try:
            for topic in topics:
                encoded.append(
                    (topic, image_to_jpeg(frames[topic], quality=self._jpeg_quality))
                )
        except Exception as error:  # noqa: BLE001 - report any encode failure to the tree
            return self._fail(response, f"JPEG encode failed: {error}")

        # Phase 2: write every encoded frame to disk.
        dest_dir = Path(directory)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return self._fail(response, f"cannot create directory {directory}: {error}")

        # A caller session tag (one grasp cycle) names the file so this cycle's
        # after_plan/after_close/after_lift frames share it; with no tag fall back
        # to this capture's own timestamp.
        tag = self._session_tag(session_id) or datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        used_labels: set[str] = set()
        saved_paths: list[str] = []
        try:
            for index, (topic, jpeg) in enumerate(encoded):
                label = self._label_for(topic, used_labels, index)
                path = dest_dir / f"{label}_{tag}.jpg"
                path.write_bytes(jpeg)
                saved_paths.append(str(path))
        except OSError as error:
            return self._fail(response, f"cannot write snapshot into {directory}: {error}")

        response.success = True
        response.saved_paths = saved_paths
        response.message = f"saved {len(saved_paths)} frame(s) into {directory}"
        self.get_logger().info(response.message)
        return response

    def _gather_frames(self, topics: list[str], timeout: float) -> dict:
        """Subscribes to each topic and returns the first frame seen per topic.

        The returned dict maps topic -> Image (or None when nothing arrived) and
        carries the live subscriptions under ``__subscriptions__`` so the caller can
        destroy them. Frames arrive on other MultiThreadedExecutor threads while this
        handler sleeps, which is why the callback group is reentrant.
        """
        latest: dict[str, Image | None] = {topic: None for topic in topics}
        subscriptions = []
        for topic in topics:
            subscriptions.append(
                self.create_subscription(
                    Image,
                    topic,
                    lambda message, captured=topic: latest.__setitem__(captured, message),
                    qos_profile_sensor_data,
                    callback_group=self._callback_group,
                )
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(frame is not None for frame in latest.values()):
                break
            time.sleep(_CAPTURE_POLL_SEC)
        result: dict = dict(latest)
        result["__subscriptions__"] = subscriptions
        return result

    def _label_for(self, topic: str, used: set[str], index: int) -> str:
        """Builds a filesystem-safe filename label from a topic's first segment."""
        segment = topic.strip("/").split("/", 1)[0] or "camera"
        label = "".join(char if char.isalnum() else "_" for char in segment) or "camera"
        candidate = label
        suffix = 2
        while candidate in used:
            candidate = f"{label}_{index}_{suffix}"
            suffix += 1
        used.add(candidate)
        return candidate

    def _session_tag(self, session_id: str) -> str:
        """Sanitizes a caller session tag into a filesystem-safe filename part.

        Returns "" when nothing usable is supplied so the caller falls back to a
        capture timestamp.
        """
        cleaned = "".join(
            char if (char.isalnum() or char in "_-") else "_" for char in session_id
        )
        return cleaned if any(char.isalnum() for char in cleaned) else ""

    def _fail(
        self, response: SaveCameraImages.Response, message: str
    ) -> SaveCameraImages.Response:
        response.success = False
        response.message = message
        response.saved_paths = []
        self.get_logger().warning(f"camera snapshot refused: {message}")
        return response


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = CameraSnapshotServer()
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


if __name__ == "__main__":
    main()
