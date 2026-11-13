#!/usr/bin/env bash
set -euo pipefail
# requires: network

# Memray 1.20.0 and line_profiler 5.1.0 on CPython 3.14.6: what memray's allocator hooks see
# (pymalloc arenas versus objects, a dlsym pointer that bypasses them), how its temporary,
# high-water-mark, and leak reports are computed, the capture file header, and line_profiler's
# bytecode padding, sys.monitoring tool, hit counts, and time attribution.
#
# Beyond the harness vocabulary above, the lab needs uv (https://docs.astral.sh/uv/). uv creates a
# throwaway virtualenv on its managed CPython 3.14.6, downloading that build if it is missing, and
# installs requirements.txt with --require-hashes from PyPI. The interpreter is pinned to the patch
# release because list growth, object sizes, and free lists decide the counts printed here.
# Nothing is written outside the temporary directory except uv's own cache and that interpreter.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# An explicit template, because a bare `mktemp -d` on macOS ignores TMPDIR.
TMP_ROOT="${TMPDIR:-/tmp}"
TMP_DIR="$(mktemp -d "${TMP_ROOT%/}/memray_lab.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

command -v uv >/dev/null 2>&1 || {
    echo "memray_tracking: missing prerequisite 'uv' (https://docs.astral.sh/uv/)" >&2
    exit 2
}

if ! { uv venv --no-config -q --managed-python --python 3.14.6 "$TMP_DIR/venv" &&
    uv pip install --no-config -q --python "$TMP_DIR/venv/bin/python" --require-hashes \
        -r "$SCRIPT_DIR/requirements.txt"; } >"$TMP_DIR/install.log" 2>&1; then
    cat "$TMP_DIR/install.log" >&2
    exit 1
fi

PY="$TMP_DIR/venv/bin/python"
CAP="$TMP_DIR/captures"
mkdir "$CAP"
cp "$SCRIPT_DIR/workload.py" "$TMP_DIR/workload.py"

# A fixed environment: no inherited PYTHONMALLOC, PYTHONDEVMODE, or tracemalloc settings.
run_env() {
    env -i PATH="/usr/bin:/bin" HOME="$TMP_DIR" LC_ALL=C PYTHONHASHSEED=0 "$@"
}

memray_run() {
    local out="$1"
    shift
    run_env ${EXTRA_ENV[@]+"${EXTRA_ENV[@]}"} "$PY" -m memray run -q -f -o "$CAP/$out.bin" "$@"
}

EXTRA_ENV=()
memray_run small_default "$TMP_DIR/workload.py" small
memray_run small_tpa --trace-python-allocators "$TMP_DIR/workload.py" small
memray_run churn "$TMP_DIR/workload.py" churn
memray_run peak "$TMP_DIR/workload.py" peak
memray_run leak "$TMP_DIR/workload.py" leak
memray_run leak_raw --no-compress "$TMP_DIR/workload.py" leak
memray_run leak_agg --aggregate "$TMP_DIR/workload.py" leak
memray_run dlsym "$TMP_DIR/workload.py" dlsym
EXTRA_ENV=(PYTHONMALLOC=malloc)
memray_run small_malloc "$TMP_DIR/workload.py" small

echo "memray $(run_env "$PY" -c 'import memray; print(memray.__version__)')," \
    "line_profiler $(run_env "$PY" -c 'import line_profiler; print(line_profiler.__version__)')," \
    "CPython $(run_env "$PY" -c 'import platform; print(platform.python_version())')"
run_env "$PY" -I "$SCRIPT_DIR/analyze.py" "$CAP"
run_env "$PY" -I "$SCRIPT_DIR/lineprof_demo.py"
