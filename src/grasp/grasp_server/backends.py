"""Lazy model loading and independent candidate backends.

The reference repository supplies the detection/segmentation pipeline design;
its robot orchestration and camera-resize fallback are not imported here.
"""

import hashlib
import json
from pathlib import Path

import numpy as np

from .geometry import geometric_candidates, pointcloud


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model(model, models_root):
    relative = Path(model["file"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("model file must be relative to the mounted models root")
    path = Path(models_root).resolve() / relative
    expected = model.get("sha256", "")
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError(f"missing or invalid SHA-256 for {relative}")
    if not path.is_file():
        raise FileNotFoundError(f"missing model: {relative}")
    if sha256_file(path) != expected:
        raise ValueError(f"SHA-256 mismatch for {relative}")
    return path


class MockBackend:
    """Synthetic foreground extraction for protocol tests, never robot execution."""

    name = "mock"

    def infer(self, request):
        depth = request["depth_m"]
        valid = depth > 0
        if not valid.any():
            return [], {"reason": "no_valid_depth", "diagnostic_only": True}
        nearest = float(np.min(depth[valid]))
        mask = valid & (depth <= nearest + 0.005)
        candidates, diagnostics = geometric_candidates(
            request, mask, object_id="mock-object", class_name=request["target"]["class_name"],
            policy={"min_object_points": 10}, scene_mask=np.zeros_like(mask),
        )
        diagnostics["diagnostic_only"] = True
        return candidates, diagnostics


class DetectorSegmentor:
    def __init__(self, manifest, models_root, *, device="cuda", confidence=0.5):
        # Validate bytes before any model is deserialized; never auto-download
        # a COCO replacement for the site's custom detector.
        yolo_path = verify_model(manifest["yolo"], models_root)
        sam_path = verify_model(manifest["mobile_sam"], models_root)
        import torch
        from mobile_sam import SamPredictor, sam_model_registry
        from ultralytics import YOLO

        if device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("CUDA requested but unavailable")
        self.device = device
        self.confidence = confidence
        self.detector = YOLO(str(yolo_path))
        expected_classes = manifest["yolo"].get("class_names")
        actual_classes = [str(self.detector.names[i]) for i in range(len(self.detector.names))]
        if expected_classes and actual_classes != expected_classes:
            raise ValueError(f"detector class identity mismatch: {actual_classes}")
        sam = sam_model_registry["vit_t"](checkpoint=str(sam_path))
        sam.to(device=device)
        sam.eval()
        self.segmentor = SamPredictor(sam)

    def detect_segment(self, image, targets):
        result = self.detector(image, conf=self.confidence, device=self.device, verbose=False)[0]
        detections = []
        height, width = image.shape[:2]
        for box in result.boxes:
            name = str(self.detector.names[int(box.cls[0])])
            if name not in targets:
                continue
            x1, y1, x2, y2 = box.xyxy[0].detach().cpu().numpy()
            bounds = np.array([max(0, x1), max(0, y1), min(width - 1, x2), min(height - 1, y2)])
            if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                continue
            detections.append({"class_name": name, "bbox": bounds, "confidence": float(box.conf[0])})
        if not detections:
            return []
        self.segmentor.set_image(np.ascontiguousarray(image[:, :, ::-1]))
        masks = []
        # Keep instances separate: do not overwrite masks from earlier objects
        # or mix a scene-level CGN candidate into the selected target.
        for index, detection in enumerate(detections):
            box = detection["bbox"]
            center = np.array([[(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]])
            prediction, _, _ = self.segmentor.predict(
                point_coords=center, point_labels=np.array([1]), box=box, multimask_output=False,
            )
            mask = prediction[0].astype(bool)
            if mask.shape != (height, width):
                raise ValueError("segmentation dimensions changed; refusing to resize")
            masks.append({**detection, "object_id": str(index + 1), "mask": mask})
        return masks


class GeometryBackend:
    name = "geometry"

    def __init__(self, perception, policies):
        self.perception = perception
        self.policies = policies

    def infer(self, request):
        target = request["target"]["class_name"]
        aliases = self.policies.get("class_aliases", {}).get(target, [target])
        objects = self.perception.detect_segment(request["color_bgr"], set(aliases))
        candidates, details = [], []
        for obj in objects:
            policy = {**self.policies["default"], **self.policies.get("classes", {}).get(target, {})}
            values, diagnostic = geometric_candidates(
                request, obj["mask"], object_id=obj["object_id"], class_name=target, policy=policy,
            )
            candidates.extend(values)
            details.append({"object_id": obj["object_id"], "detection_confidence": obj["confidence"], **diagnostic})
        candidates.sort(key=lambda candidate: candidate["score"], reverse=True)
        return candidates[:request.get("max_candidates", 8)], {
            "reason": "no_target" if not objects else ("ok" if candidates else "no_candidates"),
            "objects": details,
        }


def adapt_cgn_candidate(native, *, hand_depth_m):
    """Map CGN's native wrist origin to the public closing center.

    Reference build_6d_grasp: t = contact + width/2*X - hand_depth*Z.
    Its X/Z axes already match the public convention. This is an algorithm
    frame conversion; the robot's measured TCP transform must be applied later.
    """
    T = np.asarray(native, dtype=np.float64).copy()
    if T.shape != (4, 4) or not np.isfinite(T).all():
        raise ValueError("invalid CGN native transform")
    T[:3, 3] += float(hand_depth_m) * T[:3, 2]
    return T


class ContactGraspNetBackend:
    """Optional installed CGN runtime; no neighboring checkout is consulted."""

    name = "contact_graspnet"

    def __init__(self, perception, manifest, models_root, *, aliases=None):
        from contact_graspnet_pytorch import config_utils
        from contact_graspnet_pytorch.checkpoints import CheckpointIO
        from contact_graspnet_pytorch.contact_grasp_estimator import GraspEstimator

        model = manifest["contact_graspnet"]
        path = verify_model(model, models_root)
        cfg = Path(models_root) / model["config_file"]
        if sha256_file(cfg) != model["config_sha256"]:
            raise ValueError("CGN configuration SHA-256 mismatch")
        self.estimator = GraspEstimator(config_utils.load_config(str(cfg.parent)))
        CheckpointIO(checkpoint_dir=str(path.parent), model=self.estimator.model).load(path.name)
        self.estimator.model.eval()
        self.perception = perception
        self.hand_depth_m = float(model["native_hand_depth_m"])
        self.aliases = aliases or {}

    def infer(self, request):
        import torch

        target = request["target"]["class_name"]
        objects = self.perception.detect_segment(request["color_bgr"], set(self.aliases.get(target, [target])))
        if not objects:
            return [], {"reason": "no_target"}
        cloud = pointcloud(request["depth_m"], request["K"])
        valid = request["depth_m"] > 0
        segments = {int(obj["object_id"]): cloud[valid & obj["mask"]] for obj in objects}
        segments = {key: value for key, value in segments.items() if len(value) >= 80}
        if not segments:
            return [], {"reason": "insufficient_object_depth"}
        with torch.inference_mode():
            grasps, scores, _, openings = self.estimator.predict_scene_grasps(
                cloud[valid], pc_segments=segments, local_regions=True,
                filter_grasps=True, forward_passes=1, use_cam_boxes=False,
            )
        candidates = []
        g = request["gripper"]
        for object_id, values in grasps.items():
            # Never admit unassigned scene candidates (including -1).
            if int(object_id) not in segments:
                continue
            for index, native in enumerate(values):
                width = float(np.asarray(openings[object_id]).reshape(-1)[index])
                if not g["min_opening_m"] <= width <= g["max_opening_m"] - 2 * g["clearance_m"]:
                    continue
                candidates.append({
                    "candidate_id": f"{object_id}:{index}", "object_id": str(object_id),
                    "class_name": target, "T_camera_grasp": adapt_cgn_candidate(native, hand_depth_m=self.hand_depth_m),
                    "width_m": width, "score": float(np.asarray(scores[object_id]).reshape(-1)[index]),
                    "backend": self.name, "metrics": {"native_hand_depth_m": self.hand_depth_m},
                })
        candidates.sort(key=lambda candidate: candidate["score"], reverse=True)
        return candidates[:request.get("max_candidates", 8)], {"reason": "ok" if candidates else "no_candidates"}


def model_identity(manifest, policies, runtime=None):
    encoded = json.dumps({"models": manifest, "policies": policies, "runtime": runtime,
                          "implementation": "metric-top-surface-v1"}, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()
