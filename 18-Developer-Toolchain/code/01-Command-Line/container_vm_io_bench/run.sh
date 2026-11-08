#!/usr/bin/env bash
# requires: docker, macos
# Golden output (expected/darwin-arm64.txt) was recorded with OrbStack as the Docker engine.
# Another engine may report a different bind-mount filesystem type; the lab then fails instead of passing silently.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "${SCRIPT_DIR}/../../_harness" && pwd)"

# Verify docker availability
if ! command -v docker >/dev/null 2>&1; then
    echo "docker executable not found on PATH" >&2
    exit 1
fi

IMAGE_TAG=$(python3 -c "import hashlib, pathlib; print(hashlib.sha256(pathlib.Path('${HARNESS_DIR}/linux-labs.Dockerfile').read_bytes()).hexdigest()[:12])")
IMAGE="toolchain-labs:${IMAGE_TAG}"

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    docker build -q -f "${HARNESS_DIR}/linux-labs.Dockerfile" -t "${IMAGE}" "${HARNESS_DIR}" >/dev/null
fi

TMPDIR_HOST="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_HOST}"' EXIT

docker run --rm --network none \
    -v "${TMPDIR_HOST}:/mnt/host" \
    -v "${SCRIPT_DIR}:/mnt/lab:ro" \
    "${IMAGE}" python3 /mnt/lab/bench.py /mnt/host

# Verify cross-boundary file appearance and ownership on host macOS
if [ ! -f "${TMPDIR_HOST}/sync_token.txt" ]; then
    echo "Cross-boundary sync token: MISSING" >&2
    exit 1
fi

# Host-side view of the case-collision test: the same directory, read by macOS rather than by the guest.
CASE_ENTRIES=$(ls "${TMPDIR_HOST}/bench_case_test" | grep -ci '^probe\.txt$' || true)
echo "Host-side entries matching probe.txt: ${CASE_ENTRIES}"

TOKEN=$(cat "${TMPDIR_HOST}/sync_token.txt")
echo "Cross-boundary sync token: ${TOKEN}"

if [ "$(uname -s)" = "Darwin" ]; then
    FILE_UID=$(stat -f "%u" "${TMPDIR_HOST}/sync_token.txt")
else
    FILE_UID=$(stat -c "%u" "${TMPDIR_HOST}/sync_token.txt")
fi

HOST_UID=$(id -u)
if [ "${FILE_UID}" = "${HOST_UID}" ]; then
    echo "Host UID translation: VERIFIED (mapped to host process UID)"
else
    echo "Host UID translation: UNEXPECTED (${FILE_UID} != ${HOST_UID})"
    exit 1
fi
