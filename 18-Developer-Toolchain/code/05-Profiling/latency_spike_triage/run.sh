#!/usr/bin/env bash
# requires: linux
#
# Tail-latency triage lab: build a paced request loop, inject three faults one at a time, and
# diagnose each from outside the process with /proc accounting and eu-stack. It needs no
# privileges, so it is the path the playbook takes when eBPF and perf are not permitted.
# bpf/ and trace_privileged.sh hold the eBPF and perf path for a host where they are.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C

for tool in "${CC:-cc}" python3 eu-stack nm; do
    command -v "${tool}" >/dev/null || { echo "missing required tool: ${tool}" >&2; exit 1; }
done

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# Frame pointers and real call frames keep every stage of the request path visible to a stack
# walker, including the frame-pointer walker that eBPF tools use.
"${CC:-cc}" -O2 -g -fno-omit-frame-pointer -fno-optimize-sibling-calls -Wall -Wextra -pthread \
    -o "${WORK}/spike" "${SCRIPT_DIR}/spike.c"

python3 "${SCRIPT_DIR}/triage.py" "${WORK}/spike" "${WORK}"
