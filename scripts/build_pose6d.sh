#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose=(docker compose --project-directory "$ROOT" -p realman_pi \
  -f "$ROOT/config/docker/compose.pose6d.yaml")

config_json="$("${compose[@]}" --profile build-base config --format json)"
mapfile -t build_settings < <(python3 -c '
import json, sys
service = json.load(sys.stdin)["services"]["pose6d_runtime_base"]["build"]
print(service["args"]["POSE6D_BASE_IMAGE"])
print(service["args"]["POSE6D_USE_PREINSTALLED_PYTORCH3D"])
print(service["additional_contexts"]["nvdiffrast_source"])
print(service["context"])
' <<<"$config_json")
base_image="${build_settings[0]}"
use_preinstalled_pytorch3d="${build_settings[1]}"
source_spec="${POSE6D_NVDIFFRAST_SOURCE:-${build_settings[2]}}"
reference_root="${build_settings[3]}"

# Resolve the default pinned Git source to a persistent local build context.
# This avoids a fresh TLS Git clone on every base check and keeps the source
# digest stable between invocations.
if [[ "$source_spec" =~ ^https://github\.com/NVlabs/nvdiffrast\.git#([[:xdigit:]]{40})$ ]]; then
  revision="${BASH_REMATCH[1]}"
  cache_dir="$ROOT/.cache/pose6d/nvdiffrast-$revision"
  if [[ ! -f "$cache_dir/setup.py" || ! -d "$cache_dir/nvdiffrast" ]]; then
    mkdir -p "$(dirname "$cache_dir")"
    temporary_dir="$(mktemp -d "$ROOT/.cache/pose6d/.nvdiffrast-$revision.XXXXXX")"
    trap 'rm -rf "$temporary_dir"' EXIT
    curl -fL --retry 3 --retry-delay 1 --max-time 180 \
      "https://codeload.github.com/NVlabs/nvdiffrast/tar.gz/$revision" \
      -o "$temporary_dir/source.tar.gz"
    mkdir "$temporary_dir/source"
    tar -xzf "$temporary_dir/source.tar.gz" --strip-components=1 -C "$temporary_dir/source"
    mv "$temporary_dir/source" "$cache_dir"
    rm -rf "$temporary_dir"
    trap - EXIT
  fi
  source_dir="$cache_dir"
elif [[ -d "$source_spec" ]]; then
  source_dir="$(cd "$source_spec" && pwd)"
else
  echo "POSE6D_NVDIFFRAST_SOURCE must be a local source directory or a pinned NVlabs commit URL" >&2
  exit 2
fi
export POSE6D_NVDIFFRAST_SOURCE="$source_dir"

dockerfile="$reference_root/docker/pose6d-server.Dockerfile"
dependency_dockerfile="$(awk '/^FROM \$\{POSE6D_RUNTIME_IMAGE\}/ { exit } { print }' "$dockerfile")"
source_fingerprint="$(cd "$source_dir" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d ' ' -f 1)"
runtime_fingerprint="$( {
  printf '%s\n' "$dependency_dockerfile"
  cat "$reference_root/docker/pose6d-requirements.txt"
  printf '%s\n' "$base_image" "$use_preinstalled_pytorch3d" "$source_fingerprint"
} | sha256sum | cut -d ' ' -f 1)"
export POSE6D_RUNTIME_FINGERPRINT="$runtime_fingerprint"

image_label="$(docker image inspect "${POSE6D_RUNTIME_IMAGE:-realman-pose6d-runtime:local}" \
  --format '{{ index .Config.Labels "io.realman.pose6d.runtime-fingerprint" }}' 2>/dev/null || true)"

# The base image holds CUDA Python wheels and the compiled nvdiffrast extension.
# Build it first and tag it so later code-only builds are independent of BuildKit
# cache retention and do not fetch or compile those dependencies again.
if [[ "$image_label" != "$runtime_fingerprint" ]]; then
  "${compose[@]}" --profile build-base build --provenance=false pose6d_runtime_base
else
  echo "Pose6D dependency image is current; reusing ${POSE6D_RUNTIME_IMAGE:-realman-pose6d-runtime:local}"
fi
"${compose[@]}" build --provenance=false pose6d_server
