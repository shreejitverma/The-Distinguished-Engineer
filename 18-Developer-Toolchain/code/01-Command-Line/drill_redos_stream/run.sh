#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

export LC_ALL=C

# Step 0: Compilation
CC="${CC:-gcc}"
"${CC}" -O2 -Wall -Wextra log_producer.c -o log_producer

echo "=== PHASE 1: STDIO PIPE BUFFERING AND STDBUF OVERRIDE ==="
echo "--- Direct pipe buffer check ---"
./log_producer --mode=check | cat

echo "--- stdbuf -oL pipe buffer check ---"
stdbuf -oL ./log_producer --mode=check | cat

# The probe reads a FIFO and opens it itself, so a writer's redirection blocks until the probe is ready.
# A FIFO is the same kernel pipe as `a | b` and reports the same st_blksize (4096), so glibc picks the same buffer size.
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
FIFO="${WORK}/delivery.fifo"
mkfifo "${FIFO}"

echo "--- Real-time delivery probe without stdbuf ---"
python3 probe_delivery.py "${FIFO}" &
PROBE_PID=$!
./log_producer --mode=stream >"${FIFO}"
wait "${PROBE_PID}"

echo "--- Real-time delivery probe with stdbuf -oL ---"
python3 probe_delivery.py "${FIFO}" &
PROBE_PID=$!
stdbuf -oL ./log_producer --mode=stream >"${FIFO}"
wait "${PROBE_PID}"

echo "--- Python sys.stdout probe without -u ---"
python3 probe_delivery.py "${FIFO}" &
PROBE_PID=$!
python3 emit_py.py >"${FIFO}"
wait "${PROBE_PID}"

echo "--- Python sys.stdout probe under stdbuf -oL (no -u) ---"
python3 probe_delivery.py "${FIFO}" &
PROBE_PID=$!
stdbuf -oL python3 emit_py.py >"${FIFO}"
wait "${PROBE_PID}"

echo "--- Python sys.stdout probe with -u ---"
python3 probe_delivery.py "${FIFO}" &
PROBE_PID=$!
python3 -u emit_py.py >"${FIFO}"
wait "${PROBE_PID}"

echo ""
echo "=== PHASE 2: CATASTROPHIC BACKTRACKING COMPLEXITY ==="
python3 measure_backtrack.py

echo ""
echo "--- GNU grep -P (PCRE2) safety limit evaluation ---"
NORMAL_LOG="[2026-11-07T08:00:01Z] GET path=/api/v1/users/profile status=200 latency=12ms"
PATHOLOGICAL_LOG="[2026-11-07T08:00:06Z] GET path=/api/v1/auth_service_v2_internal_token_validation_dump! status=403 latency=99ms"

VULN_REGEX='^\[([^]]+)\]\s+GET\s+path=/api/v1/(([a-zA-Z0-9_-]+)/?)+\s+status=([0-9]{3})\s+latency=([0-9]+)ms$'
LINEAR_REGEX='^\[([^]]+)\]\s+GET\s+path=/api/v1/([a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*)/?\s+status=([0-9]{3})\s+latency=([0-9]+)ms$'

set +e
echo "${NORMAL_LOG}" | grep -P "${VULN_REGEX}" >/dev/null 2>&1
echo "GREP_PCRE_NORMAL_EXIT: $?"

# Pathological input against vulnerable nested quantifier: triggers match limit error
echo "${PATHOLOGICAL_LOG}" | grep -P "${VULN_REGEX}" 2>&1
echo "GREP_PCRE_VULN_EXIT: $?"

# Pathological input against linear deterministic regex: rejects cleanly in linear time
echo "${PATHOLOGICAL_LOG}" | grep -P "${LINEAR_REGEX}" >/dev/null 2>&1
echo "GREP_PCRE_LINEAR_EXIT: $?"
set -e

echo ""
echo "=== PHASE 3: PRODUCTION PIPELINE STALL REPRODUCTION ==="
set +e
./log_producer --mode=burst | timeout 1s python3 filter_stream.py --mode=vulnerable
FILTER_EXIT=$?
echo "VULNERABLE_PIPELINE_EXIT: ${FILTER_EXIT}"
set -e

echo "--- Wedged filter /proc probe ---"
python3 stall_probe.py vulnerable

echo ""
echo "=== PHASE 4: REMEDIATED PIPELINE EXECUTION ==="
# The timeout turns a regression back to the vulnerable regex into a failure instead of a hang.
stdbuf -oL ./log_producer --mode=burst | timeout 10s python3 -u filter_stream.py --mode=linear

echo "--- End-to-end delivery through the unremediated pipeline ---"
./log_producer --mode=stream | timeout 10s python3 filter_stream.py --mode=linear >"${FIFO}" &
PIPELINE_PID=$!
python3 probe_delivery.py "${FIFO}"
wait "${PIPELINE_PID}"

echo "--- End-to-end delivery through the remediated pipeline ---"
stdbuf -oL ./log_producer --mode=stream | timeout 10s python3 -u filter_stream.py --mode=linear >"${FIFO}" &
PIPELINE_PID=$!
python3 probe_delivery.py "${FIFO}"
wait "${PIPELINE_PID}"

echo "DRILL_STATUS: PASS"
