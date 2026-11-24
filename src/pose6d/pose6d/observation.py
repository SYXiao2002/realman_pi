"""Bounded immutable single-observation store for asynchronous inference."""

import threading
import time
import uuid

import numpy as np

from .geometry import rigid


class ObservationStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._observation = None
        self._generation = 0

    def freeze(self, *, rgb, depth_m, K, timestamp_ns, camera_frame, T_base_camera=None,
               captured_at=None, object_id=None):
        rgb = np.asarray(rgb)
        depth = np.asarray(depth_m)
        K = np.asarray(K, dtype=np.float64)
        if (rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8
                or depth.shape != rgb.shape[:2] or depth.dtype != np.float32
                or not np.isfinite(depth).all() or (depth < 0).any()):
            raise ValueError("invalid aligned RGB-D observation")
        if (K.shape != (3, 3) or not np.isfinite(K).all() or K[0, 0] <= 0 or K[1, 1] <= 0
                or not np.allclose(K[2], [0, 0, 1])):
            raise ValueError("invalid camera intrinsics")
        if type(timestamp_ns) is not int or timestamp_ns <= 0 or not isinstance(camera_frame, str) or not camera_frame:
            raise ValueError("capture timestamp and camera frame are required")
        transform = None if T_base_camera is None else rigid(T_base_camera).copy()
        captured_at = time.monotonic() if captured_at is None else float(captured_at)
        if not np.isfinite(captured_at):
            raise ValueError("capture monotonic time must be finite")
        arrays = [np.array(a, copy=True) for a in (rgb, depth, K)]
        if transform is not None:
            arrays.append(transform)
        for array in arrays:
            array.setflags(write=False)
        with self._lock:
            self._generation += 1
            self._observation = {"observation_id": uuid.uuid4().hex, "rgb": arrays[0], "depth_m": arrays[1],
                                 "K": arrays[2], "timestamp_ns": timestamp_ns, "camera_frame": camera_frame,
                                 "T_base_camera": transform, "captured_at": captured_at,
                                 "object_id": object_id,
                                 "mask": None, "result": None, "instances": {}, "vision": None,
                                 "selected_instance_id": None}
            return self._observation["observation_id"]

    def clear(self):
        with self._lock:
            self._generation += 1
            self._observation = None

    def set_instances(self, observation_id, instances, vision, *, max_count=16, max_bytes=16 * 1024 * 1024):
        if not isinstance(instances, dict) or len(instances) > max_count:
            raise ValueError("invalid segmentation instance count")
        with self._lock:
            obs = self._current(observation_id)
            stored = {}
            total_bytes = 0
            for instance_id, item in instances.items():
                if not isinstance(instance_id, str) or not instance_id or not isinstance(item, dict):
                    raise ValueError("invalid segmentation instance")
                if item.get("status") != "ok":
                    stored[instance_id] = {**item, "mask": None, "mask_pixels": 0, "valid_depth_pixels": 0}
                    continue
                mask = np.asarray(item.get("mask"))
                if (mask.shape != obs["rgb"].shape[:2] or mask.dtype != np.uint8
                        or not np.isin(mask, (0, 1)).all()):
                    raise ValueError("instance mask must be matching binary uint8")
                total_bytes += mask.nbytes
                if total_bytes > max_bytes:
                    raise ValueError("segmentation masks exceed the observation cache limit")
                pixels = int(np.count_nonzero(mask))
                valid_depth = int(np.count_nonzero((mask != 0) & (obs["depth_m"] >= .001)))
                stored_mask = np.array(mask, copy=True)
                stored_mask.setflags(write=False)
                stored[instance_id] = {**item, "mask": stored_mask, "mask_pixels": pixels,
                                       "valid_depth_pixels": valid_depth}
            self._generation += 1
            obs["instances"] = stored
            obs["vision"] = dict(vision)
            obs["mask"] = None
            obs["selected_instance_id"] = None
            obs["result"] = None

    def select_instance(self, observation_id, instance_id):
        with self._lock:
            obs = self._current(observation_id)
            item = obs["instances"].get(instance_id)
            if item is None or item.get("status") != "ok":
                raise ValueError("segmentation instance is unavailable")
            mask = item["mask"]
            if int(np.count_nonzero((mask != 0) & (obs["depth_m"] >= .001))) < 4:
                raise ValueError("selected instance contains insufficient valid depth")
            self._generation += 1
            obs["mask"] = mask
            obs["selected_instance_id"] = instance_id
            obs["result"] = None

    def instance(self, observation_id, instance_id):
        with self._lock:
            obs = self._current(observation_id)
            try:
                return dict(obs["instances"][instance_id])
            except KeyError as exc:
                raise ValueError("segmentation instance is unavailable") from exc

    def set_mask(self, observation_id, mask):
        mask = np.asarray(mask)
        with self._lock:
            obs = self._current(observation_id)
            if mask.shape != obs["depth_m"].shape or mask.dtype != np.uint8:
                raise ValueError("mask must be a uint8 image matching RGB")
            mask = (mask != 0).astype(np.uint8)
            if np.count_nonzero(mask & (obs["depth_m"] >= .001)) < 4:
                raise ValueError("mask contains insufficient valid depth")
            mask.setflags(write=False)
            self._generation += 1
            obs["mask"], obs["result"] = mask, None
            obs["selected_instance_id"] = None

    def request(self, observation_id, *, object_id, model_sha256):
        with self._lock:
            obs = self._current(observation_id)
            if obs["mask"] is None:
                raise ValueError("mask is missing")
            self._generation += 1
            obs["result"] = None
            request = {"type": "register", "version": 1, "request_id": uuid.uuid4().hex,
                       "observation_id": observation_id, "object_id": object_id,
                       "model_sha256": model_sha256, "rgb": obs["rgb"], "depth_m": obs["depth_m"],
                       "mask": obs["mask"], "K": obs["K"],
                       "capture_timestamp_ns": str(obs["timestamp_ns"]), "capture_clock": "ros",
                       "camera_frame": obs["camera_frame"], "instance_id": obs.get("selected_instance_id"),
                       "mask_source": "sam2" if obs.get("selected_instance_id") else "manual"}
            return self._generation, request

    def accept_result(self, generation, request, result):
        with self._lock:
            obs = self._current(request["observation_id"])
            if generation != self._generation:
                raise ValueError("observation or mask changed while inference was running")
            for key in ("request_id", "observation_id", "object_id", "model_sha256"):
                if result.get(key) != request[key]:
                    raise ValueError(f"result {key} mismatch")
            if result.get("type") != "result":
                raise ValueError("result is not a successful registration")
            rigid(result.get("T_camera_object"))
            obs["result"] = dict(result)

    def get(self, observation_id):
        with self._lock:
            return dict(self._current(observation_id))

    def _current(self, observation_id):
        if self._observation is None or self._observation["observation_id"] != observation_id:
            raise ValueError("observation is unavailable or has been replaced")
        return self._observation
