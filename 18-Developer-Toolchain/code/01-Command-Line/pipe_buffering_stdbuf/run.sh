#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT
cd "${WORK_DIR}"

export LC_ALL=C

# Compile C test binaries
"${CC:-gcc}" -O2 "${SCRIPT_DIR}/probe_buffers.c" -o probe_buffers
"${CC:-gcc}" -O2 "${SCRIPT_DIR}/writer.c" -o writer
"${CC:-gcc}" -O2 "${SCRIPT_DIR}/peer_server.c" -o peer_server
"${CC:-gcc}" -O2 "${SCRIPT_DIR}/peer_client.c" -o peer_client
"${CC:-gcc}" -static -O2 "${SCRIPT_DIR}/peer_client.c" -o peer_client_static

# Execute deterministic test driver
python3 "${SCRIPT_DIR}/driver.py" "${WORK_DIR}"
