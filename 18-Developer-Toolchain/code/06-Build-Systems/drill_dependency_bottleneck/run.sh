#!/usr/bin/env bash
# Drill: break a critical-path dependency bottleneck in a CMake + Ninja build.
# Builds one 24-TU project in eight configurations, each from clean at -j8, and reads .ninja_log and
# `ninja -t query` to show where the schedule serializes and what each change buys.
# Needs cmake (3.28+), ninja (1.11+ for -t missingdeps; 1.12+ for its priority rule), python3,
# and a C++17 compiler. Harness requirements: none; it runs as is on macOS and on Linux.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C
export DRILL_CODEGEN_SECONDS=3
export DRILL_TU_SECONDS=1
JOBS=8

for tool in cmake ninja python3 "${CXX:-c++}"; do
    command -v "${tool}" >/dev/null || { echo "missing tool: ${tool}" >&2; exit 1; }
done
# On macOS /usr/bin/c++ is an xcrun shim that adds a few hundred ms to every compile; call the
# real driver so the nominal sleeps, not shim overhead, dominate each job.
CXX_BIN="$(command -v "${CXX:-c++}")"
if [[ -z "${CXX:-}" ]] && command -v xcrun >/dev/null 2>&1; then
    CXX_BIN="$(xcrun -f clang++)"
fi

configure() { # name layout gen_include include_mode schema_deps [extra cmake args...]
    local name="$1"
    python3 "${SCRIPT_DIR}/gen_tree.py" "${SCRIPT_DIR}/project" "${WORK}/${name}/src" "$4"
    cmake -S "${WORK}/${name}/src" -B "${WORK}/${name}/build" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_CXX_COMPILER="${CXX_BIN}" \
        -DCMAKE_CXX_COMPILER_LAUNCHER="${WORK}/${name}/src/tools/heavy_tu.sh" \
        -DLAYOUT="$2" -DGEN_INCLUDE="$3" -DSCHEMA_DEPS="$5" "${@:6}" >"${WORK}/${name}/configure.log" 2>&1 \
        || { cat "${WORK}/${name}/configure.log" >&2; exit 1; }
}

# How many compile edges a rebuild runs after one field is added to the schema.
incremental() { # name
    echo "event_time" >>"${WORK}/$1/src/core/schema.def"
    local n
    n="$(ninja -C "${WORK}/$1/build" -j "${JOBS}" | grep -c 'Building CXX object' || true)"
    echo "  after adding a schema field: ${n} TUs recompiled"
}

missingdeps() { # name
    local out n
    out="$(ninja -C "${WORK}/$1/build" -t missingdeps 2>&1 || true)"
    n="$(grep -c '^Missing dep' <<<"${out}" || true)"
    if [[ "${n}" -gt 0 ]]; then
        echo "  ninja -t missingdeps: ${n} object files read schema.h with no path to its generator"
    else
        echo "  ninja -t missingdeps: no missing dependencies"
    fi
}

# Compilers word a missing header differently; print the fact, not the message.
failure_reason() { # log
    if grep -q "core/schema.h" "$1" && grep -Eqi "file not found|No such file" "$1"; then
        echo "missing header core/schema.h"
    else
        echo "unexpected failure"
        cat "$1" >&2
    fi
}

clean_build() { # name label
    echo "== $2 =="
    if ninja -C "${WORK}/$1/build" -j "${JOBS}" >"${WORK}/$1/build1.log" 2>&1; then
        echo "  clean build: ok"
        python3 "${SCRIPT_DIR}/analyze.py" "${WORK}/$1/build" "${JOBS}" "${WORK}/$1/wall_ms"
        return 0
    fi
    echo "  clean build: FAILED ($(failure_reason "${WORK}/$1/build1.log"))"
    return 1
}

retry_build() { # name
    if ninja -C "${WORK}/$1/build" -j "${JOBS}" >"${WORK}/$1/build2.log" 2>&1; then
        echo "  second ninja run in the same tree: ok"
    else
        echo "  second ninja run in the same tree: FAILED ($(failure_reason "${WORK}/$1/build2.log"))"
    fi
}

echo "ninja $(ninja --version), cmake $(cmake --version | head -1 | awk '{print $3}'), -j${JOBS}," \
    "codegen ${DRILL_CODEGEN_SECONDS} s, each TU ${DRILL_TU_SECONDS} s plus real compile time"
echo

configure A monolith global umbrella none
clean_build A "A: monolith core, umbrella header (baseline)"
incremental A
echo

configure B monolith global precise none
clean_build B "B: monolith core, precise includes (header hygiene only)"
incremental B
echo

configure C split global umbrella none
if clean_build C "C: split targets, umbrella header kept, global gen include dir"; then
    echo "  (unexpected: the race did not fire)"
else
    retry_build C
    missingdeps C
fi
echo

configure D split target umbrella none
if clean_build D "D: split targets, umbrella header kept, gen include dir scoped to core_schema"; then
    echo "  (unexpected: the umbrella compiled without the include dir)"
else
    retry_build D
fi
echo

configure E split target precise util
clean_build E "E: split targets, precise includes, core_schema links core_util"
echo

configure H monolith target precise none -DFILE_SETS=on
clean_build H "H: monolith core kept, precise includes, PUBLIC FILE_SET + CMP0154 NEW (CMake 3.28+)"
incremental H
missingdeps H
echo

configure G split target precise util -DCMAKE_ADD_CUSTOM_COMMAND_DEPENDS_EXPLICIT_ONLY=ON
clean_build G "G: as E, plus CMAKE_ADD_CUSTOM_COMMAND_DEPENDS_EXPLICIT_ONLY=ON (CMake 3.27+)"
echo

configure F split target precise none
clean_build F "F: split targets, precise includes, generator target is a leaf (the fix)"
incremental F
missingdeps F
echo

python3 - "${WORK}" <<'PY'
import sys
from pathlib import Path
work = Path(sys.argv[1])
a, e, f, g, h = (int((work / n / "wall_ms").read_text()) for n in "AEFGH")
print("== Measured comparison ==")
# Only orderings with a 2 s slot-model gap are printed. A ratio threshold flapped on a shared host
# (0.60 on one run, above 0.8 on another), and so did "G faster than E", whose model gap is 1 s.
# The slot-model makespans above are the deterministic size of each win.
print(f"  G faster than A: {'yes' if g < a else 'NO'}")
print(f"  F faster than A: {'yes' if f < a else 'NO'}")
print(f"  H faster than A: {'yes' if h < a else 'NO'}")
print(f"[raw ms] A={a} E={e} F={f} G={g} H={h} F/A={f / a:.2f}", file=sys.stderr)
PY
