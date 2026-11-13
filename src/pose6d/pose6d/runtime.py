"""FoundationPose wrapper; model output uses the original mesh coordinates."""

import hashlib
from pathlib import Path
import tempfile

import numpy as np

from .geometry import rigid
from .protocol import validate_request
from .screening import screen


class FoundationRuntime:
    def __init__(self, mesh_path, *, object_id="eu_plug", iterations=5, screening=None):
        import nvdiffrast.torch as dr
        import trimesh
        from estimater import FoundationPose, PoseRefinePredictor, ScorePredictor
        from Utils import make_mesh_tensors

        path = Path(mesh_path)
        self.object_id = object_id
        self.model_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        self.iterations = iterations
        self.screening = screening or {}
        self.mesh = trimesh.load(str(path), force="mesh", process=False)
        if len(self.mesh.vertices) == 0 or len(self.mesh.faces) == 0:
            raise ValueError("mesh is empty")
        self.context = dr.RasterizeCudaContext()
        self._debug_dir = tempfile.TemporaryDirectory(prefix="pose6d-")
        self.estimator = FoundationPose(model_pts=self.mesh.vertices, model_normals=self.mesh.vertex_normals,
                                        mesh=self.mesh, scorer=ScorePredictor(), refiner=PoseRefinePredictor(),
                                        glctx=self.context, debug=0, debug_dir=self._debug_dir.name)
        # register returns the raw OBJ transform; estimator.mesh_tensors is centered.
        self.raw_mesh_tensors = make_mesh_tensors(self.mesh)

    def infer(self, request):
        import torch
        from Utils import nvdiffrast_render

        validate_request(request, self.object_id, self.model_sha256)
        # The upstream crop projection multiplies K by double-precision mesh points.
        K = np.asarray(request["K"], dtype=np.float64)
        # MessagePack arrays are read-only byte views; upstream Torch expects writable storage.
        rgb = np.array(request["rgb"], copy=True, order="C")
        depth = np.array(request["depth_m"], copy=True, order="C")
        mask = np.ascontiguousarray(request["mask"], dtype=bool)
        pose = self.estimator.register(K=K, rgb=rgb, depth=depth, ob_mask=mask, iteration=self.iterations)
        # register() returns a guessed translation when filtered depth is sparse.
        if self.estimator.best_score is None:
            raise ValueError("registration did not produce a scored pose")
        pose = rigid(pose)
        score = float(self.estimator.best_score.detach().cpu().item())
        if not np.isfinite(score):
            raise ValueError("registration score is not finite")
        try:
            import cv2
            _, rendered, _ = nvdiffrast_render(
                K=K, H=depth.shape[0], W=depth.shape[1],
                ob_in_cams=torch.as_tensor(pose[None], dtype=torch.float32, device="cuda"),
                glctx=self.context, mesh_tensors=self.raw_mesh_tensors)
            rendered = rendered[0].detach().cpu().numpy()
            result = screen(mask, depth, rendered, **self.screening)
            silhouette = np.isfinite(rendered) & (rendered > 0)
            overlay = rgb.copy()
            colors = np.zeros_like(rgb)
            colors[mask & ~silhouette] = (255, 100, 0)
            colors[silhouette & ~mask] = (0, 140, 255)
            colors[silhouette & mask] = (0, 220, 80)
            active = mask | silhouette
            overlay[active] = ((overlay[active].astype(np.uint16) + colors[active]) // 2).astype(np.uint8)
            ok, encoded = cv2.imencode(".png", cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR))
            if not ok:
                raise ValueError("could not encode screening overlay")
            result["overlay_png"] = encoded.tobytes()
        except Exception as exc:
            result = {"status": "invalid", "warning": f"screening failed: {exc}", "metrics": {}}
        return {"type": "result", "version": 1, "request_id": request["request_id"],
                "observation_id": request["observation_id"], "object_id": self.object_id,
                "model_sha256": self.model_sha256, "T_camera_object": pose.tolist(),
                "score": score, "screening": result}
