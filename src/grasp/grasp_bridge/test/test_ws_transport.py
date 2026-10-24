"""The robot's synchronous client exercises the real cancellable WS service."""

import asyncio
from contextlib import contextmanager
from pathlib import Path
import sys
import threading
import time

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
pytest.importorskip("websockets.sync.client")
from grasp_bridge.ws_client import PlanningCanceled, infer
from grasp_server.server import GraspService


class Backend:
    def infer(self, request):
        time.sleep(0.15)
        T = np.eye(4)
        T[2, 3] = 0.4
        return [{"candidate_id": "c1", "object_id": "obj1", "class_name": "eu", "backend": "geometry",
                 "T_camera_grasp": T, "score": 0.8, "width_m": 0.03}], {}


@contextmanager
def server():
    import websockets
    ready, stop = threading.Event(), threading.Event()
    address = []

    async def run():
        service = GraspService({"geometry": Backend()}, model_id="test-model")
        async with websockets.serve(service.handle, "127.0.0.1", 0) as ws:
            address.append(f"ws://127.0.0.1:{ws.sockets[0].getsockname()[1]}")
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.01)
    worker = threading.Thread(target=lambda: asyncio.run(run()), daemon=True)
    worker.start()
    assert ready.wait(3), "test server did not bind"
    try:
        yield address[0]
    finally:
        stop.set()
        worker.join(timeout=3)
        assert not worker.is_alive()


def request():
    return {"type": "infer", "protocol_version": 1, "task_id": "task", "request_id": "req",
            "observation_id": "obs", "generation": 1, "capture_timestamp_ns": time.time_ns(),
            "camera_frame": "test_color_optical", "config_id": "frozen-config", "backend": "geometry",
            "target": {"class_name": "eu"}, "color_bgr": np.zeros((8, 8, 3), np.uint8),
            "depth_m": np.ones((8, 8), np.float32), "K": np.array([[10., 0., 4.], [0., 10., 4.], [0., 0., 1.]]),
            "up_camera": [0., 0., -1.], "gripper": {"id": "test-gripper", "min_opening_m": 0.,
             "max_opening_m": 0.1, "finger_depth_m": 0.04, "finger_thickness_m": 0.01, "clearance_m": 0.005}}


def call(uri, **kwargs):
    return infer(uri, request(), expected_model_id="test-model", connect_timeout_sec=1,
                 inference_timeout_sec=kwargs.pop("timeout", 2), **kwargs)


def test_bridge_round_trip_and_model_pin():
    with server() as uri:
        result = call(uri)
        assert result["candidates"][0]["class_name"] == "eu"
        with pytest.raises(ValueError, match="identity mismatch"):
            infer(uri, request(), expected_model_id="other-model", connect_timeout_sec=1, inference_timeout_sec=1)


def test_unpinned_unmeasured_review_works_but_live_planning_requires_pin():
    review = request()
    review.update(evaluation_only=True, up_camera=None, gripper={"id": "unmeasured"})
    with server() as uri:
        result = infer(uri, review, expected_model_id=None, connect_timeout_sec=1, inference_timeout_sec=2)
        assert result["model_id"] == "test-model" and len(result["candidates"]) == 1
        with pytest.raises(ValueError, match="pin expected_model_id"):
            infer(uri, request(), expected_model_id=None, connect_timeout_sec=1, inference_timeout_sec=2)


def test_bridge_cancellation_and_timeout_invalidate_late_result():
    with server() as uri:
        started = time.monotonic()
        with pytest.raises(PlanningCanceled, match="invalidated"):
            call(uri, canceled=lambda: time.monotonic()-started > 0.04)
    with server() as uri:
        with pytest.raises(TimeoutError, match="not be replayed"):
            call(uri, timeout=0.03)
