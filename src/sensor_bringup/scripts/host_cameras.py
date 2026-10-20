#!/usr/bin/env python3
"""Start and supervise the configured host-side Orbbec RGB cameras."""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from camera_config import camera_commands, load_config, topic_map
from rclpy.logging import get_logger


LOG = get_logger("host_cameras")
_STOP_SIGNALS = ((signal.SIGINT, 10), (signal.SIGTERM, 2), (signal.SIGKILL, 1))


def _process(pid):
    """Read Linux process identity without relying on truncated ps output."""
    try:
        root = Path("/proc") / str(pid)
        fields = root.joinpath("stat").read_text().rsplit(")", 1)[1].split()
        argv = [os.fsdecode(arg) for arg in root.joinpath("cmdline").read_bytes().split(b"\0") if arg]
        return fields[0], int(fields[2]), int(fields[3]), argv
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def _processes():
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            process = _process(int(entry.name))
            if process is not None:
                yield int(entry.name), process


def _group_running(pgid):
    return any(state != "Z" and group == pgid for _, (state, group, _, _) in _processes())


def _wait_group(pgid, timeout, child=None):
    deadline = time.monotonic() + timeout
    while True:
        if child is not None:
            child.poll()  # Reap our launch process while its descendants finish.
        if not _group_running(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _stop_group(pid, child=None):
    """Stop one independent launch session, including surviving components."""
    process = _process(pid)
    if process is not None and process[1:3] != (pid, pid):
        LOG.error(f"refusing to signal camera process {pid}: it has no independent session")
        return False
    for sig, grace in _STOP_SIGNALS:
        try:
            if sig == signal.SIGINT:
                # Launch forwards SIGINT to its children. Sending it to the
                # group as well interrupts USB cleanup twice.
                os.kill(pid, sig)
            else:
                os.killpg(pid, sig)
        except ProcessLookupError:
            pass
        if _wait_group(pid, grace, child):
            return True
    LOG.error(f"camera process group {pid} did not stop")
    return False


def _matches_launch(argv, command):
    offset = 1 if argv and Path(argv[0]).name.startswith("python") else 0
    if len(argv) < offset + 4 or Path(argv[offset]).name != "ros2":
        return False
    if argv[offset + 1:offset + 4] != command[1:4]:
        return False
    identity = {arg.split(":=", 1)[0]: arg.split(":=", 1)[1]
                for arg in command[4:] if arg.startswith(("camera_name:=", "serial_number:="))}
    actual = {arg.split(":=", 1)[0]: arg.split(":=", 1)[1]
              for arg in argv[offset + 4:] if ":=" in arg}
    return all(actual.get(key) == value for key, value in identity.items())


def stop_configured_cameras(config):
    """Recover camera launches orphaned by an interrupted supervisor."""
    success = True
    for namespace, command in reversed(camera_commands(config)):
        for pid, (_, _, _, argv) in list(_processes()):
            if _matches_launch(argv, command):
                LOG.info(f"stopping remaining {namespace} launch (pid {pid})")
                success = _stop_group(pid) and success
    return 0 if success else 1


def _health(config, timeout, mode):
    from check_cameras import check_topics

    return check_topics(topic_map(config, mode), timeout)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", nargs="?", choices=("start", "stop"), default="start")
    parser.add_argument("--mode", choices=("color", "depth"), default="color")
    parser.add_argument("--rviz", action="store_true")
    parser.add_argument("--ready-file", default=os.environ.get("REALMAN_CAMERA_READY_FILE", ""))
    parser.add_argument("--stagger", type=float, default=None)
    parser.add_argument("--startup-timeout", type=float, default=float(os.environ.get("REALMAN_CAMERA_CHECK_TIMEOUT", "60")))
    args = parser.parse_args(argv)

    config = load_config()
    if args.action == "stop":
        return stop_configured_cameras(config)
    commands = camera_commands(config, mode=args.mode)
    stagger = args.stagger
    if stagger is None:
        stagger = float(config.get("launch", {}).get("stagger_seconds", 2.0))
    ready = Path(args.ready_file) if args.ready_file else None
    if ready:
        ready.unlink(missing_ok=True)
    children = []
    def stop():
        # A repeated down must not interrupt cleanup halfway through.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        if ready:
            ready.unlink(missing_ok=True)
        # Let launch deliver the first SIGINT; signaling its group too would
        # interrupt component containers twice before they finish cleanup.
        # Finish each launch before stopping the next USB device.
        success = True
        for child in reversed(children):
            success = _stop_group(child.pid, child) and success
        if not success:
            raise RuntimeError("one or more camera process groups are still running")

    def interrupted(*_signal):
        raise SystemExit(0)

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        for index, (namespace, command) in enumerate(commands):
            if index and stagger:
                time.sleep(stagger)
            LOG.info(f"starting {namespace}: {' '.join(command)}")
            child_env = os.environ.copy()
            if child_env.get("ROS_LOG_DIR"):
                child_env["ROS_LOG_DIR"] = str(Path(child_env["ROS_LOG_DIR"]) / namespace)
                Path(child_env["ROS_LOG_DIR"]).mkdir(parents=True, exist_ok=True)
            children.append(subprocess.Popen(command, env=child_env, start_new_session=True))
        if args.rviz:
            config_root = Path(os.environ.get("REALMAN_CONFIG_ROOT", Path(__file__).resolve().parents[3] / "config"))
            children.append(subprocess.Popen(["ros2", "run", "rviz2", "rviz2", "-d",
                                             str(config_root / "rviz/cameras.rviz")], start_new_session=True))
        if not _health(config, args.startup_timeout, args.mode):
            LOG.error("one or more configured camera topics did not publish")
            return 1
        if ready:
            ready.parent.mkdir(parents=True, exist_ok=True)
            ready.write_text(f"{os.getpid()}\n", encoding="ascii")
        LOG.info(f"all {len(commands)} configured cameras are publishing {args.mode} frames")
        while True:
            if any(child.poll() is not None for child in children):
                LOG.error("an Orbbec launch process exited")
                return 1
            time.sleep(1)
    finally:
        stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
