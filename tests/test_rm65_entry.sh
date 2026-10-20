#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENTRY="$ROOT/rm65"

output="$(RM65_DRY_RUN=1 "$ENTRY" up 2>&1)"
grep -Fq "camera_ros2 color" <<<"$output"
grep -Fq "realman_bringup_remote" <<<"$output"
grep -Fq "realman_web_control" <<<"$output"
if grep -Fq "realman_remote_rviz" <<<"$output"; then
  echo "default up unexpectedly starts RViz" >&2
  exit 1
fi

output="$(RM65_DRY_RUN=1 "$ENTRY" up desktop 2>&1)"
grep -Fq "realman_remote_rviz" <<<"$output"
grep -Fq "realman_web_control" <<<"$output"

output="$(RM65_DRY_RUN=1 "$ENTRY" up model 2>&1)"
grep -Fq "rm65_three_rviz" <<<"$output"

output="$(RM65_DRY_RUN=1 "$ENTRY" down 2>&1)"
grep -Fq "realman_bringup_remote" <<<"$output"
grep -Fq "realman_web_control" <<<"$output"

output="$(RM65_DRY_RUN=1 "$ENTRY" build 2>&1)"
grep -Fq "docker buildx bake" <<<"$output"
grep -Fq -- "--allow network.host --load realman_bringup_remote realman_web_control" <<<"$output"
if grep -Eq " (up|stop|restart) " <<<"$output"; then
  echo "build unexpectedly starts services" >&2
  exit 1
fi

output="$(cd /tmp && RM65_DRY_RUN=1 "$ENTRY" build rm65_rviz 2>&1)"
grep -Fq -- "--allow network.host --load rm65_rviz" <<<"$output"
if grep -Fq "realman_bringup_remote" <<<"$output"; then
  echo "selected build unexpectedly includes production services" >&2
  exit 1
fi

output="$(cd /tmp && RM65_DRY_RUN=1 zsh -c 'source "$1/functions.zsh"; rm65_docker_build rm65_rviz' _ "$ROOT" 2>&1)"
grep -Fq -- "--allow network.host --load rm65_rviz" <<<"$output"

output="$(RM65_DRY_RUN=1 "$ENTRY" status 2>&1)"
grep -Fq "status" <<<"$output"

output="$("$ENTRY" help 2>&1)"
grep -Fq "build" <<<"$output"
grep -Fq "up [desktop|model|policy]" <<<"$output"

if "$ENTRY" does-not-exist >/dev/null 2>&1; then
  echo "invalid command unexpectedly succeeded" >&2
  exit 1
fi

# Production bringup must publish camera health consumed by Web control.
grep -Fq "start_camera_calibration:=true" "$ROOT/config/docker/compose.yaml"

# The production graph must refuse to start beside the legacy local-driver
# RViz service, which would create duplicate realman_driver nodes.
grep -Fq 'com.docker.compose.service=realman_driver_rviz' "$ROOT/rm65"
grep -Fq 'refusing to start while realman_driver_rviz is running' "$ROOT/rm65"

# A running legacy driver/RViz container must block startup before camera or
# production services are touched. The exported shell function stands in for
# `docker ps` without connecting to a Docker daemon.
conflict_output="$({
  docker() { printf 'legacy-driver-rviz-id\n'; }
  export -f docker
  bash -c 'source "$0"; check_driver_rviz_conflict' "$ROOT/rm65"
} 2>&1 || true)"
grep -Fq 'refusing to start while realman_driver_rviz is running' <<<"$conflict_output"

# A Docker failure must report failure while preserving cameras that already
# publish frames, including desktop and policy startup profiles.
for profile in production desktop policy; do
  failure_output="$(bash -c '
    source "$1" help >/dev/null
    check_driver_rviz_conflict() { :; }
    camera_start() { echo TEST_CAMERA_STARTED; }
    camera_stop() { echo TEST_CAMERA_STOPPED; }
    compose() { return 1; }
    if up "$2"; then
      echo "Docker failure incorrectly reported success" >&2
      exit 1
    fi
  ' _ "$ENTRY" "$profile" 2>&1)"
  grep -Fq TEST_CAMERA_STARTED <<<"$failure_output"
  grep -Fq 'RGB cameras remain running' <<<"$failure_output"
  if grep -Fq TEST_CAMERA_STOPPED <<<"$failure_output"; then
    echo "Docker failure stopped working cameras for $profile" >&2
    exit 1
  fi
done

# Exercise the real camera_start/camera_stop functions against an isolated
# supervisor. The fake start_sensors script waits for a gate before reporting
# ready, so a second caller must join the first launch while it is starting.
(
  set -euo pipefail
  test_root="$(mktemp -d "${TMPDIR:-/tmp}/rm65-camera-supervisor.XXXXXX")"
  cleanup() {
    local pid=""
    for pid in "${first_caller:-}" "${second_caller:-}"; do
      [[ -n "$pid" ]] || continue
      kill -TERM -- "-$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    done
    if [[ -s "$test_root/start-count" ]]; then
      while read -r pid; do
        kill "$pid" 2>/dev/null || true
        for _ in {1..40}; do
          kill -0 "$pid" 2>/dev/null || break
          sleep 0.05
        done
        kill -KILL "$pid" 2>/dev/null || true
      done <"$test_root/start-count"
    fi
    if [[ -n "${unrelated_pid:-}" ]]; then
      kill "$unrelated_pid" 2>/dev/null || true
      wait "$unrelated_pid" 2>/dev/null || true
    fi
    rm -rf "$test_root"
  }
  trap cleanup EXIT

  mkdir -p "$test_root/logs" "$test_root/src/sensor_bringup/scripts"
  cp "$ENTRY" "$test_root/rm65"
  printf '%s\n' 'rm65_camera_ros2_stop() { :; }' >"$test_root/functions.zsh"
  cat >"$test_root/start_sensors.sh" <<'FAKE_START'
#!/usr/bin/env bash
exec python3 "$(dirname "$0")/src/sensor_bringup/scripts/host_cameras.py" start --mode color
FAKE_START
  cat >"$test_root/src/sensor_bringup/scripts/host_cameras.py" <<'FAKE_SUPERVISOR'
import os
from pathlib import Path
import signal
import sys
import time

pid = str(os.getpid())
with open(os.environ["TEST_START_COUNT"], "a") as starts:
    starts.write(pid + "\n")
Path(os.environ["TEST_STARTED_PID"]).write_text(pid + "\n")
Path(os.environ["TEST_SUPERVISOR_SID"]).write_text(str(os.getsid(0)) + "\n")
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while not Path(os.environ["TEST_RELEASE_FILE"]).exists():
    time.sleep(0.05)
Path(os.environ["REALMAN_CAMERA_READY_FILE"]).write_text(pid + "\n")
while True:
    time.sleep(1)
FAKE_SUPERVISOR
  chmod +x "$test_root/start_sensors.sh"

  export REALMAN_LOG_ROOT="$test_root/logs"
  export CAMERA_START_TIMEOUT=12
  export TEST_START_COUNT="$test_root/start-count"
  export TEST_STARTED_PID="$test_root/started.pid"
  export TEST_SUPERVISOR_SID="$test_root/supervisor.sid"
  export TEST_RELEASE_FILE="$test_root/release-startup"
  state_file="$test_root/logs/.rm65-camera.pid"
  ready_file="$test_root/logs/.rm65-camera.ready"

  setsid bash -c '
    source "$1" help >/dev/null
    caller_sid="$(ps -o sid= -p "$$" | tr -d " ")"
    printf "%s\n" "$caller_sid" >"$2/caller.sid"
    camera_start
  ' _ "$test_root/rm65" "$test_root" >"$test_root/first.out" 2>&1 &
  first_caller=$!

  for _ in {1..100}; do
    [[ -s "$state_file" && -s "$test_root/started.pid" ]] && break
    sleep 0.05
  done
  if [[ ! -s "$state_file" || ! -s "$test_root/started.pid" ]]; then
    echo "camera_start did not launch the fake supervisor" >&2
    exit 1
  fi
  first_caller_sid="$(<"$test_root/caller.sid")"
  supervisor_pid="$(<"$state_file")"

  # Closing the first caller's terminal sends SIGHUP to its process group.
  kill -HUP -- "-$first_caller_sid"
  wait "$first_caller" 2>/dev/null || true
  if ! kill -0 "$supervisor_pid" 2>/dev/null; then
    echo "camera supervisor died with its calling session" >&2
    exit 1
  fi

  setsid bash -c '
    source "$1" help >/dev/null
    camera_start
  ' _ "$test_root/rm65" >"$test_root/second.out" 2>&1 &
  second_caller=$!
  for _ in {1..100}; do
    grep -Fq 'waiting for camera startup' "$test_root/second.out" 2>/dev/null && break
    sleep 0.05
  done
  if ! grep -Fq 'waiting for camera startup' "$test_root/second.out"; then
    echo "second camera_start did not wait on the existing supervisor" >&2
    exit 1
  fi
  if [[ "$(wc -l <"$TEST_START_COUNT")" -ne 1 ]]; then
    echo "camera_start created more than one supervisor while startup was pending" >&2
    exit 1
  fi

  : >"$TEST_RELEASE_FILE"
  wait "$second_caller"
  if ! grep -Fq "four Orbbec RGB streams ready (pid $supervisor_pid)" "$test_root/second.out"; then
    echo "second camera_start did not observe the existing supervisor becoming ready" >&2
    cat "$test_root/second.out" >&2
    exit 1
  fi
  if [[ "$(<"$TEST_STARTED_PID")" != "$supervisor_pid" || "$(<"$ready_file")" != "$supervisor_pid" ]]; then
    echo "supervisor PID, recorded PID, and ready PID did not match" >&2
    exit 1
  fi
  supervisor_sid="$(<"$TEST_SUPERVISOR_SID")"
  if [[ "$supervisor_sid" == "$first_caller_sid" ]]; then
    echo "camera supervisor remained in the calling session" >&2
    exit 1
  fi

  bash -c 'source "$1" help >/dev/null; camera_stop' _ "$test_root/rm65" >"$test_root/stop.out" 2>&1
  if [[ -e "$state_file" || -e "$ready_file" ]] || kill -0 "$supervisor_pid" 2>/dev/null; then
    echo "camera_stop did not stop and clear the temporary supervisor" >&2
    exit 1
  fi

  # A leftover state file can refer to a PID that the OS has reused. Stopping
  # cameras must clear that state without signalling the unrelated process.
  sleep 30 &
  unrelated_pid=$!
  printf '%s\n' "$unrelated_pid" >"$state_file"
  printf '%s\n' "$unrelated_pid" >"$ready_file"
  bash -c 'source "$1" help >/dev/null; camera_stop' _ "$test_root/rm65" >"$test_root/stale-stop.out" 2>&1
  if ! kill -0 "$unrelated_pid" 2>/dev/null; then
    echo "camera_stop killed an unrelated process referenced by stale state" >&2
    exit 1
  fi
  if [[ -e "$state_file" || -e "$ready_file" ]]; then
    echo "camera_stop did not clear stale supervisor state" >&2
    exit 1
  fi
)

echo "rm65 entry tests passed"
