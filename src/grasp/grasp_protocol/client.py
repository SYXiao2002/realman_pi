"""One-shot WS client: cancellation/disconnect closes the response channel.

Each inference uses a fresh connection; reconnect never replays a request. The
robot bridge additionally owns observation age, task generation and calibration.
"""

import asyncio
import uuid

from .schema import PROTOCOL_VERSION, ProtocolError, validate_request, validate_response
from .wire import MAX_MESSAGE_BYTES, packb, unpackb


class WsGraspClient:
    def __init__(self, uri, *, connect_timeout_s=5.0, inference_timeout_s=30.0):
        if not isinstance(uri, str) or not uri.startswith(("ws://", "wss://")):
            raise ValueError("grasp endpoint must be ws:// or wss://")
        if connect_timeout_s <= 0 or inference_timeout_s <= 0:
            raise ValueError("timeouts must be positive")
        self.uri = uri
        self.connect_timeout_s = connect_timeout_s
        self.inference_timeout_s = inference_timeout_s
        self._active_ids = set()

    async def infer(self, request):
        import websockets

        validate_request(request)
        request_id = request["request_id"]
        if request_id in self._active_ids:
            raise ProtocolError("request_id already in flight")
        self._active_ids.add(request_id)
        try:
            async with websockets.connect(
                self.uri, open_timeout=self.connect_timeout_s, close_timeout=1,
                max_size=MAX_MESSAGE_BYTES, max_queue=1, compression=None,
            ) as websocket:
                hello = unpackb(await asyncio.wait_for(websocket.recv(), self.connect_timeout_s))
                if (hello.get("type") != "hello" or hello.get("protocol_version") != PROTOCOL_VERSION
                        or hello.get("ready") is not True):
                    raise ProtocolError(f"grasp server is not ready: {hello.get('unavailable', {})}")
                if request["backend"] not in hello.get("backends", []):
                    raise ProtocolError("requested grasp backend is unavailable")
                await websocket.send(packb(request))
                try:
                    response = unpackb(await asyncio.wait_for(websocket.recv(), self.inference_timeout_s))
                    validate_response(response, request)
                    if response["type"] == "result" and response["model_id"] != hello.get("model_id"):
                        raise ProtocolError("model identity changed during request")
                    return response
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    # Best effort cancellation; closing the connection is the final
                    # barrier even if a GPU kernel cannot be interrupted.
                    try:
                        await asyncio.wait_for(websocket.send(packb({
                            "type": "cancel", "protocol_version": PROTOCOL_VERSION,
                            "request_id": request_id, "cancel_id": uuid.uuid4().hex,
                        })), 0.25)
                    except Exception:
                        pass
                    raise
        finally:
            self._active_ids.discard(request_id)
