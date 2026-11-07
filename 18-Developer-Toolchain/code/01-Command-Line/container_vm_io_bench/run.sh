#!/usr/bin/env bash
# requires: docker
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
fi
