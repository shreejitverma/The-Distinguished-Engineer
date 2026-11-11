#!/usr/bin/env bash
# sys.monitoring tracer lab: tool ids, bytecode instrumentation, DISABLE, local and
# global events, exception and call events, legacy sys.settrace and sys.setprofile,
# specialization, memory, overhead, and four rebuilt tools.
#
# Needs a final release of CPython 3.15 or newer: python3 when it qualifies, else the
# first python3.15 on PATH that does. The expected output was recorded on 3.15.2, and
# the harness keys expected files by platform only, so an interpreter whose behavior
# differs (3.14, a 3.15 beta, or a later release that changes something) fails verify
# with a diff instead of passing silently.
# SYSMON_LAB_PYTHON=<interpreter> forces an interpreter, CPython 3.14 at the oldest,
# for comparing releases by hand; `bash run.sh 4 9` runs only sections 4 and 9.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# usable <interpreter> <minor> <final|any>: true for CPython 3.<minor> or newer.
usable() {
    command -v "$1" >/dev/null 2>&1 &&
        "$1" -c 'import sys
v = sys.version_info
ok = sys.implementation.name == "cpython" and v >= (3, int(sys.argv[1]))
sys.exit(0 if ok and (sys.argv[2] == "any" or v.releaselevel == "final") else 1)' "$2" "$3" 2>/dev/null
}

PY=""
if [ -n "${SYSMON_LAB_PYTHON:-}" ]; then
    if ! usable "${SYSMON_LAB_PYTHON}" 14 any; then
        echo "sys_monitoring_tracer: SYSMON_LAB_PYTHON=${SYSMON_LAB_PYTHON} is not CPython 3.14 or newer" >&2
        exit 2
    fi
    PY="${SYSMON_LAB_PYTHON}"
elif usable python3 15 final; then
    PY=python3
else
    while IFS= read -r candidate; do
        if usable "${candidate}" 15 final; then
            PY="${candidate}"
            break
        fi
    done < <(type -aP python3.15 2>/dev/null || true)
fi
if [ -z "${PY}" ]; then
    echo "sys_monitoring_tracer: missing prerequisite, a final release of CPython 3.15 (python3 is $(python3 -V 2>&1 || echo absent), and no python3.15 on PATH qualifies)" >&2
    exit 2
fi

# -E ignores PYTHON* variables, -s skips user site-packages, -B writes no .pyc
# files into the lab directory; the script directory stays on sys.path so that
# lab.py can import minitrace.py and target.py. lab.py starts one interpreter
# per section with the same flags.
exec "${PY}" -E -s -B lab.py "$@"
