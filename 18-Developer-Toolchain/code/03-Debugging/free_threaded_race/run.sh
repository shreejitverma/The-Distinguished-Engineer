#!/usr/bin/env bash
# Free-threaded CPython races, a GIL-reenabling import, ThreadSanitizer on a C
# extension under three thread schedules, asyncio debug mode, and fork() with a held lock.
# requires: macos
#
# macos: ThreadSanitizer is injected with DYLD_INSERT_LIBRARIES and the Xcode
# clang TSan runtime, and extensions link with -undefined dynamic_lookup.
# TSan runs with abort_on_error=0: its Darwin default is 1, which turns every
# report into abort() and exit 134 instead of the documented exitcode 66.
# Also needs a free-threaded CPython 3.14 (python3.14t): set FT_PYTHON, put
# python3.14t on PATH, or install one with `uv python install 3.14t`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# Start from a known environment: each of these changes what the lab measures.
unset PYTHON_GIL PYTHONASYNCIODEBUG PYTHONDEVMODE PYTHONWARNINGS TSAN_OPTIONS PYTHONPATH
export PYTHONDONTWRITEBYTECODE=1
export LC_ALL=C

is_ft314() {
    [ -n "$1" ] && [ -x "$1" ] && "$1" -c '
import sys, sysconfig
ok = sysconfig.get_config_var("Py_GIL_DISABLED") == 1 and sys.version_info[:2] == (3, 14)
sys.exit(0 if ok else 1)' 2>/dev/null
}

FT=""
for cand in "${FT_PYTHON:-}" "$(command -v python3.14t 2>/dev/null || true)" \
    "$(uv python find --no-python-downloads 3.14t 2>/dev/null || true)"; do
    if is_ft314 "${cand}"; then
        FT="${cand}"
        break
    fi
done
if [ -z "${FT}" ]; then
    echo "no free-threaded CPython 3.14 found; run: uv python install 3.14t" >&2
    exit 1
fi

CC_BIN="${CC:-cc}"
TSAN_RT="$("${CC_BIN}" -print-file-name=libclang_rt.tsan_osx_dynamic.dylib)"
if [ ! -f "${TSAN_RT}" ]; then
    echo "TSan runtime not found (got '${TSAN_RT}'); install the Xcode command line tools" >&2
    exit 1
fi

INC="$("${FT}" -c 'import sysconfig; print(sysconfig.get_paths()["include"])')"
SUFFIX="$("${FT}" -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')"
mkdir -p "${WORK}/plain" "${WORK}/tsan"
for mod in ready legacy; do
    "${CC_BIN}" -shared -fPIC -O1 -I"${INC}" -undefined dynamic_lookup \
        "${SCRIPT_DIR}/ext/${mod}.c" -o "${WORK}/plain/${mod}${SUFFIX}"
done
"${CC_BIN}" -shared -fPIC -O1 -g -fsanitize=thread -I"${INC}" -undefined dynamic_lookup \
    "${SCRIPT_DIR}/ext/racy.c" -o "${WORK}/tsan/racy${SUFFIX}"

echo "== Interpreter =="
"${FT}" -c '
import sys, sysconfig
v = sys.version_info
build = "free-threading build" if "free-threading build" in sys.version else "default build"
flag = sysconfig.get_config_var("Py_GIL_DISABLED")
suffix = sysconfig.get_config_var("EXT_SUFFIX")
print(f"CPython {v.major}.{v.minor} {build}, Py_GIL_DISABLED={flag}, extension suffix {suffix}")
print(f"sys.getswitchinterval() = {sys.getswitchinterval()}")'
echo

echo "== 1. Lost updates: same binary, GIL forced on (1 us switch interval), then forced off =="
"${FT}" "${SCRIPT_DIR}/race.py" --show-bytecode
PYTHON_GIL=1 "${FT}" "${SCRIPT_DIR}/race.py" --switch-interval 1e-6
PYTHON_GIL=0 "${FT}" "${SCRIPT_DIR}/race.py"
echo

echo "== 2. One import re-enables the GIL =="
PYTHONPATH="${WORK}/plain:${SCRIPT_DIR}" "${FT}" "${SCRIPT_DIR}/gil_guard.py"
PYTHONPATH="${WORK}/plain:${SCRIPT_DIR}" PYTHON_GIL=0 "${FT}" "${SCRIPT_DIR}/gil_guard.py"
set +e
PYTHONPATH="${WORK}/plain" "${FT}" -W error::RuntimeWarning -c 'import legacy' 2>"${WORK}/werr.txt"
rc=$?
set -e
echo "-W error::RuntimeWarning -c 'import legacy': exit ${rc}, $(tail -n 1 "${WORK}/werr.txt" | sed 's/, which has not.*//')"
echo

echo "== 3. ThreadSanitizer on a C extension (interpreter not instrumented) =="
defaults="$(DYLD_INSERT_LIBRARIES="${TSAN_RT}" TSAN_OPTIONS=help=1 "${FT}" -c pass 2>&1 |
    awk '/^\t(abort_on_error|exitcode|halt_on_error)$/ {name = substr($1, 1)}
         name != "" && /Current Value/ {sub(/.*Current Value: /, ""); sub(/\)$/, ""); printf "%s=%s ", name, $0; name = ""}')"
echo "TSan runtime defaults on this host: ${defaults% }"
"${FT}" -c '
import sysconfig
sem = sysconfig.get_config_var("HAVE_SEM_TIMEDWAIT")
kind = "POSIX semaphore" if sem else "pthread mutex and condition variable"
print(f"Parking-lot semaphore in this interpreter: {kind} (HAVE_SEM_TIMEDWAIT={sem})")'
# tsan_case <label> <variant>: run one schedule of tsan_driver.py under TSan and print what TSan said.
tsan_case() {
    local label="$1" variant="$2" out rc summary reported got want parked exact
    set +e
    out="$(cd "${WORK}" && PYTHONPATH="${WORK}/tsan:${SCRIPT_DIR}" \
        DYLD_INSERT_LIBRARIES="${TSAN_RT}" TSAN_OPTIONS="halt_on_error=0 abort_on_error=0" \
        "${FT}" "${SCRIPT_DIR}/tsan_driver.py" "${variant}" 2>"${WORK}/tsan_${variant}.txt")"
    rc=$?
    set -e
    summary="$(grep -E '^SUMMARY: ThreadSanitizer' "${WORK}/tsan_${variant}.txt" | sort -u | sed 's/^SUMMARY: ThreadSanitizer: //' | paste -sd ';' - || true)"
    reported="$(grep -cE '^WARNING: ThreadSanitizer: data race' "${WORK}/tsan_${variant}.txt" || true)"
    read -r got want parked <<<"${out}"
    case "${variant}" in
    plain | turns_plain) exact="not checked (racy by design)" ;;
    *) if [ "${got}" = "${want}" ]; then exact="yes (${got})"; else exact="no (${got} of ${want})"; fi ;;
    esac
    if [ "${variant}" = handoff ]; then
        exact="${exact}; waiter had parked before the unlock: ${parked}"
    fi
    echo "${label}: exit ${rc}, data race reports ${reported}${summary:+, ${summary}}"
    echo "  final counter exact: ${exact}"
}
echo "Threads released together by a barrier, so the counter is contended:"
tsan_case "racy.bump_plain" plain
tsan_case "racy.bump_atomic" atomic
tsan_case "racy.bump_mutex" mutex
echo "Two threads in strict turns, so the increments never overlap and a PyMutex is never contended:"
tsan_case "racy.bump_turns, plain counter" turns_plain
tsan_case "racy.bump_turns, PyMutex" turns_mutex
echo "One thread holds the PyMutex until the other has parked on it, then unlocks:"
tsan_case "racy.hold_then_bump and racy.block_then_bump" handoff
echo

echo "== 4. asyncio: production mode, then debug mode =="
"${FT}" "${SCRIPT_DIR}/asyncio_debug.py"
PYTHONASYNCIODEBUG=1 "${FT}" "${SCRIPT_DIR}/asyncio_debug.py"
echo

echo "== 5. fork() while another thread holds a lock =="
(cd "${SCRIPT_DIR}" && "${FT}" "${SCRIPT_DIR}/fork_deadlock.py")
