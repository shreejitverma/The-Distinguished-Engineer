#!/usr/bin/env bash
# requires: docker, network
# Drill: hunt non-determinism with diffoscope.
# Builds a small C++ and Rust project twice, as two CI agents would (different checkout
# paths, start times two seconds apart, different hash seeds), diffs the release tarballs
# with diffoscope, and removes one class of leak per stage until the two are bit-for-bit equal.
# The image build downloads Ubuntu packages (network); the drill itself runs with --network none.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAG="$(python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest()[:12])' \
    "${SCRIPT_DIR}/Dockerfile")"
IMAGE="toolchain-drill-diffoscope:${TAG}"

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    docker build -q -t "${IMAGE}" -f "${SCRIPT_DIR}/Dockerfile" "${SCRIPT_DIR}" >/dev/null
fi

docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges \
    --mount "type=bind,source=${SCRIPT_DIR},target=/mnt/lab,readonly" \
    "${IMAGE}" bash /mnt/lab/drill.sh
