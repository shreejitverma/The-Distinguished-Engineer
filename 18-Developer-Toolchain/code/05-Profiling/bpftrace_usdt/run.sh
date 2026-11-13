#!/usr/bin/env bash
# requires: linux
#
# USDT from the ELF note to the armed probe, without privileges.
#
# Needs: a C compiler, readelf (binutils), python3, and gdb with Python support.
# It does not need root, CAP_BPF, or bpftrace: gdb plays the tracer through ptrace and does
# what a kernel uprobe does for a USDT probe (patch the nop, raise the semaphore, fetch the
# arguments the note describes). On a host where bpftrace is installed and the caller is root,
# the same probes are also traced with trace.bt and compared; that result goes to stderr so
# stdout stays identical with and without privileges.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C

cd "${WORK}"
"${CC:-cc}" -O2 -g -Wall -Wextra -Werror -o order_server "${SCRIPT_DIR}/order_server.c"

echo "== 1. Probes decoded from .note.stapsdt =="
python3 "${SCRIPT_DIR}/stapsdt.py" ./order_server

echo
echo "== 2. No tracer attached =="
./order_server

USDT_SITES="$(python3 "${SCRIPT_DIR}/stapsdt.py" --sites ./order_server)"
export USDT_SITES

trace_with_gdb() { # $1 = comma-separated probe names
	export USDT_TRACE="$1" USDT_REPORT="${WORK}/report.txt" USDT_TARGET_OUT="${WORK}/target.txt"
	rm -f "${USDT_REPORT}" "${USDT_TARGET_OUT}"
	if ! gdb -q -nx -batch -x "${SCRIPT_DIR}/probe_tracer.py" ./order_server >"${WORK}/gdb.log" 2>&1 ||
		! grep -q '^  tracer finished$' "${USDT_REPORT}" 2>/dev/null; then
		echo "gdb tracer failed for probes: $1" >&2
		cat "${WORK}/gdb.log" >&2
		exit 1
	fi
	grep -v '^  tracer finished$' "${USDT_REPORT}"
	echo "  traced program output:"
	sed 's/^/    /' "${USDT_TARGET_OUT}"
}

echo
echo "== 3. Tracer attached to orders:order_start (no semaphore) =="
trace_with_gdb order_start

echo
echo "== 4. Tracer attached to orders:order_done (semaphore-gated) =="
trace_with_gdb order_done

# Optional cross-check with the real thing. Silent on stdout by design.
if command -v bpftrace >/dev/null 2>&1 && [ "$(id -u)" = "0" ]; then
	if bpftrace "${SCRIPT_DIR}/trace.bt" -c ./order_server >"${WORK}/bpftrace.txt" 2>"${WORK}/bpftrace.err"; then
		if diff <(grep -E '^(start|done) ' "${WORK}/bpftrace.txt") "${SCRIPT_DIR}/trace.expected" >&2; then
			echo "bpftrace cross-check: PASS (trace.bt saw the same 10 events)" >&2
		else
			echo "bpftrace cross-check: FAIL (events differ from trace.expected)" >&2
			exit 1
		fi
	else
		echo "bpftrace cross-check: SKIPPED (bpftrace could not attach here)" >&2
		cat "${WORK}/bpftrace.err" >&2
	fi
else
	echo "bpftrace cross-check: SKIPPED (needs bpftrace and root)" >&2
fi
