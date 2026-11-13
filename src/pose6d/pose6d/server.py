"""Single-GPU, bounded WebSocket registration service."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging

from grasp_protocol.wire import MAX_MESSAGE_BYTES, packb, unpackb

from .protocol import validate_request
from .runtime import FoundationRuntime

LOG = logging.getLogger("pose6d")


class PoseService:
    def __init__(self, runtime, executor, timeout):
        self.runtime = runtime
        self.executor = executor
        self.timeout = timeout
        self.busy = False

    async def handle(self, websocket, path=None):
        from websockets.exceptions import ConnectionClosed

        await websocket.send(packb({"type": "hello", "version": 1, "ready": True,
                                    "object_id": self.runtime.object_id,
                                    "model_sha256": self.runtime.model_sha256}))
        loop = asyncio.get_running_loop()
        receive = asyncio.create_task(websocket.recv())
        pending, current, deadline = None, None, None
        try:
            while True:
                watch = {receive}
                if pending is not None:
                    watch.add(pending)
                timeout = max(0, deadline - loop.time()) if deadline is not None else None
                done, _ = await asyncio.wait(watch, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    await websocket.send(packb(self.error(current, "timeout", "inference timed out; GPU remains busy until completion")))
                    pending, current, deadline = None, None, None
                    continue
                if receive in done:
                    raw = receive.result()
                    receive = asyncio.create_task(websocket.recv())
                    request = None
                    try:
                        request = unpackb(raw)
                        if isinstance(request, dict) and request.get("type") == "cancel":
                            if pending is None or request.get("request_id") != current["request_id"]:
                                raise ValueError("no matching request to cancel")
                            await websocket.send(packb(self.error(current, "canceled", "result discarded; GPU remains busy until completion")))
                            pending, current, deadline = None, None, None
                            continue
                        validate_request(request, self.runtime.object_id, self.runtime.model_sha256)
                        if pending is not None or self.busy:
                            raise ValueError("inference busy")
                        self.busy = True
                        pending = loop.run_in_executor(self.executor, self.runtime.infer, request)
                        pending.add_done_callback(self._worker_done)
                        current, deadline = request, loop.time() + self.timeout
                    except (ValueError, TypeError, KeyError) as exc:
                        await websocket.send(packb(self.error(request, "invalid_request", str(exc))))
                if pending is not None and pending in done:
                    try:
                        response = pending.result()
                    except Exception as exc:
                        LOG.exception("registration failed")
                        response = self.error(current, "inference_failed", str(exc))
                    pending, current, deadline = None, None, None
                    await websocket.send(packb(response))
        except ConnectionClosed:
            pass
        finally:
            receive.cancel()

    def _worker_done(self, worker):
        self.busy = False
        if not worker.cancelled():
            worker.exception()

    @staticmethod
    def error(request, code, message):
        request = request if isinstance(request, dict) else {}
        return {"type": "error", "version": 1, "request_id": request.get("request_id"),
                "observation_id": request.get("observation_id"), "code": code, "message": message}


async def main_async(args):
    from websockets.asyncio.server import serve

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="pose6d-gpu") as executor:
        loop = asyncio.get_running_loop()
        runtime = await loop.run_in_executor(executor, lambda: FoundationRuntime(
            args.mesh, iterations=args.iterations,
            screening={"min_valid_pixels": args.min_valid_pixels,
                       "min_iou": args.min_iou, "max_median_m": args.max_median_m}))
        service = PoseService(runtime, executor, args.timeout)
        async with serve(service.handle, args.host, args.port, max_size=MAX_MESSAGE_BYTES):
            LOG.info("pose6d ready on %s:%s, mesh=%s", args.host, args.port, runtime.model_sha256)
            await asyncio.Future()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mesh", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--min-valid-pixels", type=int, default=100)
    parser.add_argument("--min-iou", type=float, default=None)
    parser.add_argument("--max-median-m", type=float, default=None)
    args = parser.parse_args()
    if args.timeout <= 0 or args.iterations < 1 or args.min_valid_pixels < 1:
        parser.error("timeout, iterations and min-valid-pixels must be positive")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
