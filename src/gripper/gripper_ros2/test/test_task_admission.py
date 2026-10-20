from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from gripper_ros2.task_admission import TaskAdmission


class Request(SimpleNamespace):
    RESERVE_EXTERNAL = 3
    RELEASE_EXTERNAL = 4


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def service_is_ready(self):
        return True

    def call_async(self, request):
        self.requests.append(request)
        future = Future()
        future.set_result(next(self.responses))
        return future


def gate(responses):
    instance = TaskAdmission.__new__(TaskAdmission)
    instance._type = SimpleNamespace(Request=Request)
    instance._timeout = 0.01
    client = Client(responses)
    instance._clients = {"right": client}
    return instance, client


def test_remote_command_reserves_and_releases_same_driver_admission():
    instance, client = gate([
        SimpleNamespace(success=True, command_id="command-1"),
        SimpleNamespace(success=True),
    ])
    with pytest.raises(RuntimeError):
        with instance.command("right", "task-token"):
            assert len(client.requests) == 1
            assert client.requests[0].task_token == "task-token"
            raise RuntimeError("hardware write failed")
    assert client.requests[1].operation == Request.RELEASE_EXTERNAL
    assert client.requests[1].command_id == "command-1"


def test_denied_command_never_enters_hardware_window():
    instance, client = gate([SimpleNamespace(success=False, message="leased")])
    with pytest.raises(PermissionError, match="leased"):
        with instance.command("right"):
            pytest.fail("hardware command was admitted")
    assert len(client.requests) == 1


def test_unmapped_gripper_fails_closed():
    instance, client = gate([])
    with pytest.raises(PermissionError, match="mapping"):
        with instance.command("unknown"):
            pytest.fail("unmapped hardware was admitted")
    assert not client.requests


def test_transport_exception_is_a_denial_before_hardware():
    instance, client = gate([])
    def broken(_request):
        raise RuntimeError("disconnected")
    client.call_async = broken
    with pytest.raises(PermissionError, match="transport"):
        with instance.command("right"):
            pytest.fail("transport failure admitted hardware")


def test_response_exception_is_a_denial_before_hardware():
    instance, client = gate([])
    def broken(_request):
        future = Future()
        future.set_exception(RuntimeError("lost reply"))
        return future
    client.call_async = broken
    with pytest.raises(PermissionError, match="response"):
        with instance.command("right"):
            pytest.fail("response failure admitted hardware")
