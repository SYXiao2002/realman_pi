"""Bounded, cancellable WS service with one GPU inference in flight."""

import argparse
import asyncio
import json
import logging
import signal
from pathlib import Path

import yaml

from grasp_protocol import PROTOCOL_VERSION, identity, packb, unpackb, validate_request, validate_response
from grasp_protocol.wire import MAX_MESSAGE_BYTES

from .backends import ContactGraspNetBackend, DetectorSegmentor, GeometryBackend, MockBackend, model_identity

LOGGER = logging.getLogger("grasp_server")


class GraspService:
    def __init__(self, backends, *, model_id, unavailable=None, inference_timeout_s=30.0):
        self.backends = backends
        self.model_id = model_id
        self.unavailable = unavailable or {}
        self.inference_timeout_s = inference_timeout_s
        self._gpu_lock = asyncio.Lock()
        self._workers = set()

    def hello(self):
        return {
            "type": "hello", "protocol_version": PROTOCOL_VERSION,
            "ready": bool(self.backends), "backends": sorted(self.backends),
            "model_id": self.model_id, "unavailable": self.unavailable,
            "capabilities": {"robot_control": False, "queue_depth": 0,
                             "frame": "contact_x_close_z_approach", "depth_unit": "m",
                             "max_message_bytes": MAX_MESSAGE_BYTES, "mock_is_diagnostic_only": True},
        }

    def _error(self, request, code, message):
        return {"type": "error", "protocol_version": PROTOCOL_VERSION,
                **identity(request), "code": code, "message": message}

    async def _compute(self, request):
        # A cancelled connection must not release the GPU slot while a worker
        # thread/kernel is still running. Cancellation invalidates publication;
        # compute continues holding this lock until the underlying call ends.
        try:
            candidates, diagnostics = await asyncio.to_thread(self.backends[request["backend"]].infer, request)
            response = {
                "type": "result", "protocol_version": PROTOCOL_VERSION, **identity(request),
                "model_id": self.model_id, "gripper_id": request["gripper"]["id"],
                "candidates": candidates, "diagnostics": diagnostics,
            }
            validate_response(response, request)
            LOGGER.info(json.dumps({"event": "inference_done", "request_id": request["request_id"],
                                    "backend": request["backend"], "candidates": len(candidates)}))
            return response
        except Exception as exc:
            LOGGER.exception("inference failed request_id=%s", request["request_id"])
            return self._error(request, "inference_failed", str(exc))
        finally:
            self._gpu_lock.release()

    def _forget_worker(self, worker):
        self._workers.discard(worker)
        # Consume detached errors when a connection is already gone.
        if not worker.cancelled():
            worker.exception()

    async def handle(self, websocket, path=None):
        from websockets.exceptions import ConnectionClosed

        await websocket.send(packb(self.hello()))
        pending = None
        current = None
        receive = asyncio.create_task(websocket.recv())
        deadline = None
        try:
            while True:
                watch = {receive}
                if pending is not None:
                    watch.add(pending)
                timeout = max(0, deadline - asyncio.get_running_loop().time()) if deadline is not None else None
                done, _ = await asyncio.wait(watch, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    await websocket.send(packb(self._error(current, "inference_timeout", "result is no longer eligible")))
                    # Abandon publication, retain the actual worker/GPU lock.
                    pending, current, deadline = None, None, None
                    continue
                # Process incoming cancel/disconnect before a simultaneously
                # completed result, so cancellation cannot lose this race.
                if receive in done:
                    payload = receive.result()
                    receive = asyncio.create_task(websocket.recv())
                    try:
                        message = unpackb(payload)
                    except (ValueError, TypeError, OverflowError) as exc:
                        await websocket.close(code=1003, reason=str(exc)[:100])
                        return
                    if not isinstance(message, dict):
                        await websocket.close(code=1003, reason="expected map")
                        return
                    if message.get("type") == "cancel":
                        if (message.get("protocol_version") == PROTOCOL_VERSION and current is not None
                                and message.get("request_id") == current["request_id"]):
                            pending, current, deadline = None, None, None
                        continue
                    try:
                        validate_request(message)
                    except (ValueError, TypeError, OverflowError) as exc:
                        await websocket.close(code=1003, reason=str(exc)[:100])
                        return
                    if pending is not None or self._gpu_lock.locked():
                        await websocket.send(packb(self._error(message, "busy", "another inference is still in flight")))
                        continue
                    if message["backend"] not in self.backends:
                        await websocket.send(packb(self._error(message, "backend_unavailable", "backend is not ready")))
                        continue
                    await self._gpu_lock.acquire()
                    current = message
                    pending = asyncio.create_task(self._compute(message))
                    self._workers.add(pending)
                    pending.add_done_callback(self._forget_worker)
                    deadline = asyncio.get_running_loop().time() + self.inference_timeout_s
                if pending is not None and pending in done:
                    await websocket.send(packb(pending.result()))
                    pending, current, deadline = None, None, None
        except ConnectionClosed:
            pass
        finally:
            receive.cancel()
            await asyncio.gather(receive, return_exceptions=True)
            # Never cancel an executor thread or publish its late result.

    async def drain(self):
        if self._workers:
            await asyncio.gather(*tuple(self._workers), return_exceptions=True)


def load_service(config_path, *, backend_override=None, models_root_override=None):
    with Path(config_path).open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if models_root_override is not None:
        config["models_root"] = str(models_root_override)
    root = Path(config_path).resolve().parent
    with (root / config["models_manifest"]).open(encoding="utf-8") as stream:
        manifest = yaml.safe_load(stream)
    with (root / config["object_policies"]).open(encoding="utf-8") as stream:
        policies = yaml.safe_load(stream)
    names = [backend_override] if backend_override else config["enabled_backends"]
    if not names or set(names) - {"geometry", "mock", "contact_graspnet"}:
        raise ValueError("enabled_backends contains no supported backend")
    inference_timeout_s = float(config["inference_timeout_s"])
    if not 0 < inference_timeout_s <= 300:
        raise ValueError("inference_timeout_s must be in (0,300]")
    backends, unavailable = {}, {}
    if "mock" in names:
        backends["mock"] = MockBackend()
    perception = None
    if set(names) & {"geometry", "contact_graspnet"}:
        try:
            perception = DetectorSegmentor(
                manifest, config["models_root"], device=config["device"],
                confidence=float(config["detection_confidence"]),
            )
        except Exception as exc:
            LOGGER.error("perception unavailable: %s", exc)
            for name in names:
                if name != "mock":
                    unavailable[name] = str(exc)
    if perception is not None:
        if "geometry" in names:
            backends["geometry"] = GeometryBackend(perception, policies)
        if "contact_graspnet" in names:
            try:
                backends["contact_graspnet"] = ContactGraspNetBackend(
                    perception, manifest, config["models_root"], aliases=policies.get("class_aliases", {}))
            except Exception as exc:
                LOGGER.error("Contact-GraspNet unavailable: %s", exc)
                unavailable["contact_graspnet"] = str(exc)
    runtime = {"device": config["device"], "detection_confidence": config["detection_confidence"], "backends": names}
    return GraspService(backends, model_id=model_identity(manifest, policies, runtime), unavailable=unavailable,
                        inference_timeout_s=inference_timeout_s), config


async def serve(service, host, port):
    import websockets

    # Python PID 1 ignores SIGTERM unless a handler is installed. Docker stop
    # must close client channels and finish any actual GPU worker gracefully.
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stopping.set)
    try:
        async with websockets.serve(
            service.handle, host, port, max_size=MAX_MESSAGE_BYTES, max_queue=1,
            compression=None, ping_interval=20, close_timeout=1,
        ):
            LOGGER.info(json.dumps({"event": "listening", "host": host, "port": port, **service.hello()}))
            await stopping.wait()
    finally:
        await service.drain()
        loop.remove_signal_handler(signal.SIGTERM)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/grasp/server.yaml")
    parser.add_argument("--backend", choices=["geometry", "mock", "contact_graspnet"])
    parser.add_argument("--models-root", type=Path, help="explicit host replay mount; Docker defaults to /models")
    parser.add_argument("--check", action="store_true", help="report model readiness and exit; no WS listener")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    service, config = load_service(arguments.config, backend_override=arguments.backend,
                                   models_root_override=arguments.models_root)
    if arguments.check:
        print(json.dumps(service.hello(), sort_keys=True))
        return 0 if service.backends else 1
    try:
        asyncio.run(serve(service, config["host"], int(config["port"])))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
