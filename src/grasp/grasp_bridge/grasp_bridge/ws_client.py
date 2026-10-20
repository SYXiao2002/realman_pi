"""One connection per frozen observation; disconnect/cancel never replays work."""

import time

from grasp_protocol import MAX_MESSAGE_BYTES, PROTOCOL_VERSION, packb, unpackb, validate_request, validate_response


class PlanningCanceled(RuntimeError):
    pass


def infer(uri, request, *, expected_model_id, connect_timeout_sec, inference_timeout_sec, canceled=lambda: False):
    from websockets.sync.client import connect

    validate_request(request)
    if not expected_model_id:
        raise ValueError("pin expected_model_id before live planning")
    if canceled():
        raise PlanningCanceled("planning canceled")
    with connect(uri, open_timeout=connect_timeout_sec, close_timeout=1.0,
                 max_size=MAX_MESSAGE_BYTES, max_queue=1, compression=None) as ws:
        hello = unpackb(ws.recv(timeout=connect_timeout_sec))
        if (not isinstance(hello, dict) or hello.get("type") != "hello"
                or hello.get("protocol_version") != PROTOCOL_VERSION or hello.get("ready") is not True
                or request["backend"] not in hello.get("backends", [])
                or hello.get("model_id") != expected_model_id):
            raise ValueError("algorithm capability/model identity mismatch")
        ws.send(packb(request))
        deadline = time.monotonic() + inference_timeout_sec
        while True:
            if canceled():
                ws.send(packb({"type": "cancel", "protocol_version": PROTOCOL_VERSION,
                               "request_id": request["request_id"]}))
                raise PlanningCanceled("planning canceled; late result invalidated")
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError("inference timeout; observation will not be replayed")
            try:
                response = unpackb(ws.recv(timeout=min(0.1, remaining)))
                break
            except TimeoutError:
                continue
        if canceled():
            raise PlanningCanceled("planning canceled; late result invalidated")
        validate_response(response, request)
        if response["type"] == "error":
            raise ValueError(f"algorithm {response['code']}: {response.get('message', '')}")
        if response["model_id"] != expected_model_id:
            raise ValueError("result model identity changed")
        return response
