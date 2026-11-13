import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import numpy as np
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from grasp_protocol.wire import packb, unpackb
from pose6d.server import PoseService


def test_timeout_keeps_gpu_busy_until_worker_finishes():
    started = threading.Event()
    release = threading.Event()

    class Runtime:
        object_id = "eu_plug"
        model_sha256 = "mesh"

        def infer(self, request):
            started.set()
            release.wait(2)
            return {"type": "result", "request_id": request["request_id"]}

    request = {"type": "register", "version": 1, "request_id": "r", "observation_id": "o",
               "object_id": "eu_plug", "model_sha256": "mesh", "rgb": np.zeros((2, 2, 3), np.uint8),
               "depth_m": np.ones((2, 2), np.float32), "mask": np.ones((2, 2), np.uint8), "K": np.eye(3)}

    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            service = PoseService(Runtime(), executor, .05)
            async with serve(service.handle, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                async with connect(f"ws://127.0.0.1:{port}") as ws:
                    assert unpackb(await ws.recv())["type"] == "hello"
                    await ws.send(packb(request))
                    assert unpackb(await ws.recv())["code"] == "timeout"
                    assert started.is_set()
                    await ws.send(packb(request))
                    assert "busy" in unpackb(await ws.recv())["message"]
                    release.set()
                    for _ in range(100):
                        if not service.busy:
                            break
                        await asyncio.sleep(.01)
                    assert not service.busy

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_cancel_discards_result_and_holds_gpu_slot():
    release = threading.Event()

    class Runtime:
        object_id, model_sha256 = "eu_plug", "mesh"

        def infer(self, request):
            release.wait(2)
            return {"type": "result", "request_id": request["request_id"]}

    request = {"type": "register", "version": 1, "request_id": "r", "observation_id": "o",
               "object_id": "eu_plug", "model_sha256": "mesh", "rgb": np.zeros((2, 2, 3), np.uint8),
               "depth_m": np.ones((2, 2), np.float32), "mask": np.ones((2, 2), np.uint8), "K": np.eye(3)}

    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            service = PoseService(Runtime(), executor, 1)
            async with serve(service.handle, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                async with connect(f"ws://127.0.0.1:{port}") as ws:
                    await ws.recv()
                    await ws.send(packb(request))
                    await ws.send(packb({"type": "cancel", "request_id": "r"}))
                    assert unpackb(await ws.recv())["code"] == "canceled"
                    assert service.busy
                    release.set()
                    for _ in range(100):
                        if not service.busy:
                            break
                        await asyncio.sleep(.01)
                    assert not service.busy
                    await ws.send(packb(request))
                    assert unpackb(await ws.recv())["type"] == "result"

    try:
        asyncio.run(run())
    finally:
        release.set()
