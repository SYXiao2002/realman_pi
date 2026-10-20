#!/usr/bin/env bash
# Direct host binary-driver entry; ./rm65 up runs this in the background.
set -e
WS_MAIN="$(cd "$(dirname "$0")" && pwd)"
action=start
if [[ "${1:-}" == stop ]]; then
  [[ $# == 1 ]] || { echo 'usage: start_sensors.sh stop' >&2; exit 2; }
  action=stop
  shift
fi

# Share Compose's ROS domain without executing arbitrary .env shell content.
if [[ -r "$WS_MAIN/.env" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]%$'\r'}"
    [[ -n "${!key:-}" || -z "$value" ]] || export "$key=$value"
  done < "$WS_MAIN/.env"
fi

unset AMENT_CURRENT_PREFIX AMENT_SHELL
source /opt/ros/humble/setup.bash
if [[ -n "${REALMAN_ORBBEC_ROS2_SETUP:-}" ]]; then
  source "$REALMAN_ORBBEC_ROS2_SETUP"
fi
if [[ "$action" == start ]] && ! ros2 pkg prefix orbbec_camera >/dev/null 2>&1; then
  echo 'start_sensors: install the host orbbec_camera binary ROS package first' >&2
  exit 1
fi

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
export ROS_LOCALHOST_ONLY="${ROS_LOCALHOST_ONLY:-0}"
export FASTDDS_BUILTIN_TRANSPORTS="${FASTDDS_BUILTIN_TRANSPORTS:-UDPv4}"
export RCUTILS_COLORIZED_OUTPUT=1
export REALMAN_CONFIG_ROOT="$WS_MAIN/config"
export ROS_LOG_DIR="${REALMAN_LOG_ROOT:-$WS_MAIN/logs}/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$ROS_LOG_DIR"

# Keep the shell helper's historical color/depth/rviz convenience arguments.
mode=color
extra=()
for arg in "$@"; do
  case "$arg" in
    color|depth) mode="$arg" ;;
    rviz|--rviz|use_rviz:=true) extra+=(--rviz) ;;
    enable_color:=true|enable_depth:=false) ;;
    *) echo "usage: start_sensors.sh [color|depth] [rviz]" >&2; exit 2 ;;
  esac
done
exec python3 "$WS_MAIN/src/sensor_bringup/scripts/host_cameras.py" "$action" --mode "$mode" "${extra[@]}"
