"""Exercise orphan recovery with fake launchers; no ROS graph or USB access."""

import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "src/sensor_bringup/scripts"


def test_orphan_cleanup_matches_serial_and_reaps_components_after_launch_exits(tmp_path, monkeypatch):
    pytest.importorskip("rclpy")
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("host_cameras_lifecycle", SCRIPTS / "host_cameras.py")
    cameras = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cameras)
    monkeypatch.setattr(cameras, "_STOP_SIGNALS", (
        (signal.SIGINT, 0.4), (signal.SIGTERM, 0.3), (signal.SIGKILL, 1),
    ))

    # The fake launch exits immediately on SIGINT, leaving a component that
    # ignores SIGINT/TERM. Cleanup must still find and kill that process group.
    component = tmp_path / "component_container.py"
    component.write_text("""
import os, signal, time
from pathlib import Path
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(os.environ['FAKE_CAMERA_READY'] + '.component').touch()
while True:
    time.sleep(1)
""")
    launcher = tmp_path / "ros2"
    launcher.write_text("""
import os, signal, subprocess, sys, time
from pathlib import Path
ready = Path(os.environ['FAKE_CAMERA_READY'])
def interrupted(*_):
    ready.with_suffix('.signal').write_text('SIGINT')
    sys.exit(0)
signal.signal(signal.SIGINT, interrupted)
child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('component_container.py'))])
while not Path(str(ready) + '.component').exists():
    time.sleep(0.01)
ready.write_text(str(child.pid))
while True:
    time.sleep(1)
""")
    parent = """
import os, subprocess, sys
env = dict(os.environ, FAKE_CAMERA_READY=sys.argv[3])
child = subprocess.Popen([sys.executable, sys.argv[1], 'launch', 'orbbec_camera',
    'rm65_cleanup_test.launch.py', 'camera_name:=camera_cleanup_test',
    'serial_number:=' + sys.argv[2]], env=env, start_new_session=True,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(child.pid)
"""
    pids = []
    try:
        for serial, name in (("RM65_TEST_MATCH", "match"), ("RM65_TEST_OTHER", "other")):
            ready = tmp_path / name
            result = subprocess.run(
                [sys.executable, "-c", parent, str(launcher), serial, str(ready)],
                check=True, capture_output=True, text=True,
            )
            pids.append(int(result.stdout.strip()))
            deadline = time.monotonic() + 5
            while not ready.is_file() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.is_file(), "fake camera failed to initialize"

        config = {"cameras": {"camera_cleanup_test": {
            "serial": "RM65_TEST_MATCH", "launch_file": "rm65_cleanup_test.launch.py",
        }}}
        assert cameras.stop_configured_cameras(config) == 0
        assert not cameras._group_running(pids[0])
        assert (tmp_path / "match.signal").read_text() == "SIGINT"
        assert cameras._group_running(pids[1]), "cleanup stopped a different camera serial"
        assert not (tmp_path / "other.signal").exists()
    finally:
        for pid in pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
