"""Real loopback WS checks: bounded admission and late-result cancellation."""

import asyncio
import subprocess
import sys
import threading

import pytest
import numpy as np
import websockets

from test_grasp_protocol import observation

from grasp_protocol import packb, unpackb
from grasp_protocol.client import WsGraspClient
from grasp_server.backends import MockBackend
from grasp_server.server import GraspService


def test_mock_ws_roundtrip_without_ros_or_models():
    async def scenario():
        service = GraspService({"mock": MockBackend()}, model_id="synthetic-test-model")
        async with websockets.serve(service.handle, "127.0.0.1", 0, compression=None) as listener:
            port = listener.sockets[0].getsockname()[1]
            result = await WsGraspClient(f"ws://127.0.0.1:{port}").infer(observation())
            assert result["type"] == "result" and result["candidates"]
            assert result["diagnostics"]["diagnostic_only"] is True
        await service.drain()
    asyncio.run(scenario())


def test_archive_preserves_inputs_and_records_backend_failure(tmp_path):
    class FailingBackend:
        def infer(self, request):
            raise ValueError("original backend failure")

    service = GraspService({"mock": FailingBackend()}, model_id="test", archive_root=tmp_path)
    request = observation()
    request.update(evaluation_only=True, up_camera=None, gripper={"id": "unmeasured"})
    with pytest.raises(ValueError, match="original backend failure"):
        service._infer_and_archive(request)
    archive, = tmp_path.iterdir()
    saved = unpackb((archive / "request.msgpack").read_bytes())
    np.testing.assert_array_equal(saved["color_bgr"], request["color_bgr"])
    with np.load(archive / "frame.npz", allow_pickle=False) as frozen:
        np.testing.assert_array_equal(frozen["depth_m"], request["depth_m"])
        assert "up_camera" not in frozen.files
    assert unpackb((archive / "error.msgpack").read_bytes())["message"] == "original backend failure"


def test_cancelled_gpu_work_cannot_publish_or_release_busy_slot():
    class BlockingBackend:
        def __init__(self):
            self.entered, self.release = threading.Event(), threading.Event()

        def infer(self, request):
            self.entered.set()
            if not self.release.wait(3):
                raise RuntimeError("test synchronization timed out")
            return [], {"diagnostic_only": True}

    async def scenario():
        backend = BlockingBackend()
        service = GraspService({"mock": backend}, model_id="test")
        async with websockets.serve(service.handle, "127.0.0.1", 0, compression=None) as listener:
            uri = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
            async with websockets.connect(uri) as ws:
                await ws.recv()
                await ws.send(packb(observation()))
                assert await asyncio.to_thread(backend.entered.wait, 1)
                await ws.send(packb({"type": "cancel", "protocol_version": 1, "request_id": "request-1"}))
                await ws.send(packb(observation(request_id="request-2")))
                assert unpackb(await ws.recv())["code"] == "busy"
                backend.release.set()
                await service.drain()
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), 0.05)
                await ws.send(packb(observation(request_id="request-3")))
                result = unpackb(await asyncio.wait_for(ws.recv(), 1))
                assert result["request_id"] == "request-3" and result["type"] == "result"
        await service.drain()
    asyncio.run(scenario())


def test_inference_timeout_never_replays_and_client_cancel_closes_socket():
    class SlowBackend:
        def infer(self, request):
            threading.Event().wait(0.1)
            return [], {}

    async def scenario():
        service = GraspService({"mock": SlowBackend()}, model_id="test", inference_timeout_s=0.02)
        async with websockets.serve(service.handle, "127.0.0.1", 0, compression=None) as listener:
            uri = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
            result = await WsGraspClient(uri).infer(observation())
            assert result["type"] == "error" and result["code"] == "inference_timeout"
            await service.drain()
            client = WsGraspClient(uri, inference_timeout_s=0.005)
            with pytest.raises(asyncio.TimeoutError):
                await client.infer(observation(request_id="request-2"))
            await service.drain()
            assert not client._active_ids
    asyncio.run(scenario())


def test_sigterm_shuts_down_standalone_service_cleanly():
    script = """
import asyncio
import logging
from grasp_server.backends import MockBackend
from grasp_server.server import GraspService, serve
logging.basicConfig(level=logging.INFO)
asyncio.run(serve(GraspService({'mock': MockBackend()}, model_id='test'), '127.0.0.1', 0))
"""
    process = subprocess.Popen([sys.executable, "-u", "-c", script], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    try:
        # Port zero prevents clashes; the service writes this line only after
        # installing its signal handler and opening the listener.
        while True:
            line = process.stdout.readline()
            assert line, "service exited before becoming ready"
            if '"event": "listening"' in line:
                break
        process.terminate()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


def test_archive_and_replay_keep_captured_base_transform(tmp_path):
    from grasp_server.replay import request_from_npz
    request = observation()
    request.update(evaluation_only=True, T_base_camera=np.diag([1., -1., -1., 1.]))
    service = GraspService({"mock": MockBackend()}, model_id="test", archive_root=tmp_path)
    service._infer_and_archive(request)
    archive, = tmp_path.iterdir()
    replay = request_from_npz(archive / "frame.npz", request["gripper"])
    np.testing.assert_array_equal(replay["T_base_camera"], request["T_base_camera"])
    np.testing.assert_array_equal(replay["up_camera"], request["up_camera"])
