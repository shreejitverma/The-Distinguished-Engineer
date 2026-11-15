#!/usr/bin/env bash
# Opt-in companion to run.sh: build a ThreadSanitizer-instrumented free-threaded
# CPython 3.14.6 and ask it what TSan sees in three Python-level workloads.
# requires: macos network
#
# Not run by `make verify` (the harness runs only run.sh); run it by hand:
#   bash tsan_cpython.sh            # downloads and builds CPython, about 1 to 3 minutes
#   TSAN_PYTHON=/path/python.exe bash tsan_cpython.sh   # reuse an existing TSan build
# Its recorded output is expected/tsan-cpython-darwin-arm64.txt.
# macos: recorded with Apple clang; on Linux the same configure line works, but
# CPython's CI first lowers ASLR with `sysctl -w vm.mmap_rnd_bits=28`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION=3.14.6
SHA256=74d0d71d0600e477651a077101d6e62d1e2e69b8e992ba18c993dd643b7ba222
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

unset PYTHON_GIL PYTHONPATH PYTHONWARNINGS TSAN_OPTIONS
export LC_ALL=C

SRC="${WORK}/Python-${VERSION}"
if [ -z "${TSAN_PYTHON:-}" ]; then
    curl -sSfL -o "${WORK}/py.tgz" "https://www.python.org/ftp/python/${VERSION}/Python-${VERSION}.tgz"
    echo "${SHA256}  ${WORK}/py.tgz" | shasum -a 256 -c - >/dev/null
    tar xzf "${WORK}/py.tgz" -C "${WORK}"
    mkdir "${WORK}/build"
    (cd "${WORK}/build" && "${SRC}/configure" --disable-gil --with-thread-sanitizer CC="${CC:-cc}" >configure.log 2>&1)
    (cd "${WORK}/build" && make -j"$(sysctl -n hw.ncpu 2>/dev/null || nproc)" python.exe >make.log 2>&1)
    TSAN_PYTHON="${WORK}/build/python.exe"
    SUPP="${SRC}/Tools/tsan/suppressions_free_threading.txt"
else
    SUPP="${WORK}/suppressions_free_threading.txt"
    curl -sSfL -o "${SUPP}" \
        "https://raw.githubusercontent.com/python/cpython/v${VERSION}/Tools/tsan/suppressions_free_threading.txt"
fi

echo "== TSan-instrumented CPython =="
"${TSAN_PYTHON}" -I -S -c '
import sys
v = sys.version_info
build = "free-threading build" if "free-threading build" in sys.version else "default build"
print(f"CPython {v.major}.{v.minor}.{v.micro} {build}, configured --disable-gil --with-thread-sanitizer")'

# run_case <label> <workload> <suppressions yes|no>
run_case() {
    local label="$1" workload="$2" supp="$3" opts out rc reported summary
    opts="halt_on_error=0 abort_on_error=0"
    if [ "${supp}" = yes ]; then opts="${opts} suppressions=${SUPP}"; fi
    set +e
    out="$(TSAN_OPTIONS="${opts}" "${TSAN_PYTHON}" -I -S "${SCRIPT_DIR}/tsan_cpython.py" "${workload}" 2>"${WORK}/err.txt")"
    rc=$?
    set -e
    reported="$(grep -cE '^WARNING: ThreadSanitizer: data race' "${WORK}/err.txt" || true)"
    summary="$(grep -E '^SUMMARY: ThreadSanitizer' "${WORK}/err.txt" | sed -E 's#^SUMMARY: ThreadSanitizer: ##; s#[^ ]*/##' |
        sort -u | paste -sd ';' - || true)"
    echo "${label} (upstream suppressions: ${supp}): exit ${rc}, data race reports ${reported}"
    echo "  ${out}"
    if [ -n "${summary}" ]; then echo "  ${summary}"; fi
}

run_case "box.n += 1 from 4 threads" attr no
run_case 'd["k"] += 1 from 4 threads' item no
run_case "4 threads share one range iterator" iter no
run_case "4 threads share one range iterator" iter yes
