"""Transport adapters for unchanged chaung_wei_2_jiao_fu algorithms."""

import hashlib
import importlib.util
import json
from pathlib import Path
import random
import tempfile
import uuid

import numpy as np


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model(model, models_root):
    relative = Path(model['file'])
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('model file must be relative to the mounted models root')
    path = Path(models_root).resolve() / relative
    expected = model.get('sha256', '')
    if len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
        raise ValueError(f'missing or invalid SHA-256 for {relative}')
    if not path.is_file():
        raise FileNotFoundError(f'missing model: {relative}')
    if sha256_file(path) != expected:
        raise ValueError(f'SHA-256 mismatch for {relative}')
    return path


def source_identity(backend, expected):
    """Verify source bytes, including the source checkout's local changes."""
    backend = Path(backend).resolve()
    for relative, digest in expected.items():
        verify_model({'file': relative, 'sha256': digest}, backend)
    return dict(expected)


class ReferenceRuntime:
    """Load original code in a temporary layout owning only its log directory.

    Source/config/core remain read-only. The original module creates its logger
    at import, so importing directly from an immutable source tree would fail.
    """

    def __init__(self, backend, manifest, models_root, settings):
        import torch

        yolo = verify_model(manifest['yolo'], models_root)
        sam = verify_model(manifest['mobile_sam'], models_root)
        backend = Path(backend).resolve()
        self.provenance = source_identity(backend, manifest['reference_files'])
        selection_path = backend / 'contact_graspnet_ros2/contact_graspnet_ros2/grasp_selection.py'
        selection_spec = importlib.util.spec_from_file_location('realman_reference_selection_' + uuid.uuid4().hex, selection_path)
        self.selection = importlib.util.module_from_spec(selection_spec)
        selection_spec.loader.exec_module(self.selection)
        if settings['device'] != ('cuda' if torch.cuda.is_available() else 'cpu'):
            raise ValueError("requested device differs from the original worker's automatic device")
        self.settings, self.manifest = settings, manifest
        self.models_root = Path(models_root).resolve()
        self._layout = tempfile.TemporaryDirectory(prefix='realman-grasp-reference-')
        runtime_backend = Path(self._layout.name) / 'backend'
        worker_path = Path('contact_graspnet_ros2/contact_graspnet_ros2/inference_worker.py')
        (runtime_backend / worker_path).parent.mkdir(parents=True)
        (runtime_backend / worker_path).symlink_to(backend / worker_path)
        for name in ('config', 'contact_graspnet_core'):
            (runtime_backend / name).symlink_to(backend / name, target_is_directory=True)
        name = 'realman_reference_worker_' + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(name, runtime_backend / worker_path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.seed()
        self.worker = self.module.InferenceWorker(
            yolo_model=str(yolo), sam_model=str(sam), use_graspnet=False,
            mode='sim', z_range=tuple(settings['z_range_m']),
        )
        self.worker.detector.conf_threshold = float(settings['detection_confidence'])
        classes = [str(self.worker.detector.model.names[i]) for i in range(len(self.worker.detector.model.names))]
        if classes != manifest['yolo']['class_names']:
            raise ValueError(f'detector class identity mismatch: {classes}')

    def seed(self):
        import torch
        random.seed(self.settings['seed'])
        np.random.seed(self.settings['seed'])
        torch.manual_seed(self.settings['seed'])

    def enable_cgn(self):
        model = self.manifest['contact_graspnet']
        checkpoint = verify_model(model, self.models_root)
        config = verify_model({'file': model['config_file'], 'sha256': model['config_sha256']}, self.models_root)
        if checkpoint != config.parent / 'checkpoints/model.pt':
            raise ValueError('reference CGN requires config.yaml and checkpoints/model.pt in one directory')
        self.worker.grasp_estimator = self.module.GraspNetEstimator(str(config.parent))

    def prepare(self, request):
        """Native-resolution path from offline evaluate.py, without process() ROI/resize."""
        bgr, depth, K = (request[k] for k in ('color_bgr', 'depth_m', 'K'))
        pc_image, valid = self.worker.depth_to_pointcloud(depth, K)
        if np.count_nonzero(valid) < 100:
            raise ValueError('Too few valid depth points')
        detections = self.worker.detector.detect(bgr, target_classes=[request['target']['class_name']], input_color='bgr')
        segmap = self.worker.segmentor.segment(bgr, detections)
        segments = {i: pc_image[valid & (segmap == i)] for i in range(1, len(detections) + 1)}
        instance_points = {str(i): len(points) for i, points in segments.items()}
        skipped = [dict(object_id=str(i), valid_points=len(points),
                        minimum_points=self.worker.min_segment_points, reason='insufficient_valid_depth')
                   for i, points in segments.items() if len(points) < self.worker.min_segment_points]
        # Preserve original IDs for archives/review. Only the adapter's geometry
        # call uses compact IDs, since the worker enumerates its detections.
        segments = {i: points for i, points in segments.items() if len(points) >= self.worker.min_segment_points}
        return dict(bgr=bgr, depth=depth, K=K, pc_image=pc_image, valid=valid,
                    detections=detections, segmap=segmap, segments=segments,
                    instance_points=instance_points, skipped_instances=skipped)


def adapt_native_candidate(native, *, offset_m):
    """Origin conversion only; retain the native pose separately."""
    T = np.asarray(native, dtype=np.float64).copy()
    if T.shape != (4, 4) or not np.isfinite(T).all():
        raise ValueError('invalid native grasp transform')
    T[:3, 3] += float(offset_m) * T[:3, 2]
    return T


def adapt_cgn_candidate(native, *, hand_depth_m):
    return adapt_native_candidate(native, offset_m=hand_depth_m)


class ReferenceBackend:
    """Original generators without extra width/collision filters or rescoring."""

    def __init__(self, runtime, name):
        self.runtime, self.name = runtime, name

    def generate(self, request, scene):
        worker = self.runtime.worker
        segments = {i: p.copy() for i, p in scene['segments'].items()}
        if self.name == 'geometry':
            worker._last_slice_debug_image_paths = []
            worker._last_slice_debug_diagnostics = []
            detections, segmap = scene['detections'], scene['segmap']
            original_ids = list(segments)
            if len(segments) != len(detections):
                # Omitting a segment alone is insufficient: the original worker
                # would try its bbox fallback for that detection. Select only
                # eligible detections and map returned IDs back afterwards.
                detections = [detections[i-1] for i in original_ids]
                segments = {i: segments[original] for i, original in enumerate(original_ids, 1)}
                segmap = np.zeros_like(scene['segmap'])
                order = original_ids + [i for i in range(1, len(scene['detections'])+1) if i not in original_ids]
                for compact, original in enumerate(order, 1):
                    segmap[scene['segmap'] == original] = compact
                # Skipped masks remain positive labels, preserving the worker's
                # other-object mask. No depth, valid mask or geometry is altered.
            raw = worker._generate_heuristic_grasps(
                scene['pc_image'], scene['valid'], detections, segments,
                scene['depth'], scene['K'], segmap, max_grasps=request.get('max_candidates', 15),
                vertical_axis_camera=request.get('up_camera'), bgr=scene['bgr'],
            )
            return [{**value, 'obj_id': original_ids[int(value['obj_id'])-1]} for value in raw]
        self.runtime.seed()
        context = request.get('cgn_context') or self.runtime.settings['cgn_context']
        predictions, scores, _, widths = worker.grasp_estimator.predict(
            scene['pc_image'][scene['valid']].copy(), pc_segments=segments,
            local_regions=context != 'full', filter_grasps=True,
            use_cam_boxes=context == 'box', return_openings=True,
        )
        raw = []
        for obj_id, matrices in predictions.items():
            if obj_id not in segments:  # Same target-only rule as offline evaluate.py.
                continue
            for matrix, score, width in zip(matrices, np.atleast_1d(scores[obj_id]),
                                            np.atleast_1d(widths[obj_id]), strict=True):
                raw.append(dict(grasp=matrix, score=float(score), obj_id=obj_id,
                                grasp_width_est_m=float(width)))
        return sorted(raw, key=lambda c: c['score'], reverse=True)[:request.get('max_candidates', 15)]

    def infer(self, request):
        import cv2
        scene = self.runtime.prepare(request)
        sources = [{"file": name, "sha256": digest} for name, digest in self.runtime.provenance.items()]
        has_segments = bool(scene['segments'])
        if has_segments:
            for _ in range(self.runtime.settings['warmup_runs']):
                self.generate(request, scene)
        raw = self.generate(request, scene) if has_segments else []
        offset = (self.runtime.worker.heuristic_wrist_offset if self.name == 'geometry'
                  else self.runtime.manifest['contact_graspnet']['native_hand_depth_m'])
        candidates = []
        alignment_status = ('backend_not_applicable' if self.name != 'geometry' else
                            'applied' if request.get('T_base_camera') is not None else 'missing_base_camera')
        if alignment_status == 'missing_base_camera' and not request.get('evaluation_only', False):
            raise ValueError('geometry vertical alignment requires captured T_base_camera')
        for index, value in enumerate(raw):
            obj_id = int(value['obj_id'])
            native = np.asarray(value['grasp'], dtype=np.float64).copy()
            aligned = (np.asarray(self.runtime.selection.align_grasp_to_base_vertical(
                native, request['T_base_camera'], wrist_offset_m=offset), dtype=np.float64)
                if alignment_status == 'applied' else None)
            before = adapt_native_candidate(native, offset_m=offset)
            after = adapt_native_candidate(aligned, offset_m=offset) if aligned is not None else None
            candidates.append(dict(
                candidate_id=f'{obj_id}:{index}', object_id=str(obj_id),
                class_name=request['target']['class_name'], backend=self.name,
                T_camera_native_grasp=native, T_camera_native_grasp_aligned=aligned,
                T_camera_grasp_before_alignment=before, T_camera_grasp_after_alignment=after,
                T_camera_grasp=after if after is not None else before,
                vertical_alignment=dict(applied=aligned is not None, status=alignment_status,
                                        wrist_offset_m=float(offset), approach_axis_base=[0., 0., -1.]),
                width_m=float(value['grasp_width_est_m']), score=float(value['score']),
                metrics=dict(native_origin_offset_m=offset, tool_geometry_checked=False),
            ))
        ok, encoded = cv2.imencode('.png', scene['segmap'].astype(np.uint16))
        if not ok:
            raise ValueError('cannot encode instance segmentation')
        return candidates, dict(
            reason=('ok' if candidates else 'no_target' if not scene['detections'] else
                    'no_valid_instances' if not has_segments else 'no_candidates'),
            implementation='reference-inference-worker',
            vertical_alignment_status=alignment_status,
            source_files=sources, seed=self.runtime.settings['seed'],
            z_range_m=list(self.runtime.worker.z_range),
            cgn_context=request.get('cgn_context') or self.runtime.settings['cgn_context'],
            vertical_axis_camera=request.get('up_camera'), detections=scene['detections'],
            instance_points=scene.get('instance_points', {str(i): len(p) for i, p in scene['segments'].items()}),
            skipped_instances=scene.get('skipped_instances', []),
            segmap_sha256=hashlib.sha256(scene['segmap'].tobytes()).hexdigest(),
            segmap_png=encoded.tobytes(), tool_geometry_checked=False,
        )


class MockBackend:
    """Fixed protocol-test response; never used for real observations/planning."""
    name = 'mock'

    def infer(self, request):
        T = np.eye(4)
        T[2, 3] = 0.5
        return [dict(candidate_id='fixture', object_id='fixture', class_name=request['target']['class_name'],
                     T_camera_grasp=T, width_m=0.01, score=1., backend='mock')], dict(diagnostic_only=True)


def model_identity(manifest, sources, runtime=None):
    encoded = json.dumps(dict(models=manifest, sources=sources, runtime=runtime,
                              implementation='reference-inference-worker-base-vertical-partial-depth-v3'), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()
