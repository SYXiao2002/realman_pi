"""Reserve driver admission around the actual gripper command write window."""

from contextlib import contextmanager
import threading


class TaskAdmission:
    def __init__(self, node, settings):
        from rclpy.callback_groups import ReentrantCallbackGroup
        from realman_msgs.srv import TaskControl

        self._type = TaskControl
        self._timeout = float(settings["timeout_sec"])
        self._group = ReentrantCallbackGroup()
        self._clients = {
            name: node.create_client(TaskControl, f"/{arm}/task_control", callback_group=self._group)
            for name, arm in settings["arms"].items()
        }

    def _call(self, client, **fields):
        if not client.service_is_ready():
            raise PermissionError("driver task admission service is unavailable")
        try:
            future = client.call_async(self._type.Request(**fields))
        except Exception as error:
            raise PermissionError(f"task admission transport failed: {error}") from error
        done = threading.Event()
        future.add_done_callback(lambda _: done.set())
        if not done.wait(self._timeout):
            # A reserve may have been accepted after the reply was lost. Do
            # not retry or guess a release ID: retain driver exclusion.
            raise PermissionError("task admission timed out; inspect driver ownership")
        try:
            response = future.result()
        except Exception as error:
            raise PermissionError(f"task admission response failed: {error}") from error
        if response is None or not response.success:
            raise PermissionError(response.message if response else "missing task admission response")
        return response

    @contextmanager
    def command(self, name, task_token=""):
        client = self._clients.get(name)
        if client is None:
            raise PermissionError(f"no task admission mapping for {name}")
        reserved = self._call(client, operation=self._type.Request.RESERVE_EXTERNAL, task_token=task_token)
        try:
            yield
        finally:
            self._call(client, operation=self._type.Request.RELEASE_EXTERNAL, command_id=reserved.command_id)
