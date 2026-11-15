#!/usr/bin/env bash
# ccache_hit_rate: which edits keep a compiler-cache hit, which ones cost one, and why.
# Needs ccache 4.x, sccache, a C compiler, rustc, and python3 on PATH; no harness requirement applies on macOS.
# Every cache, config file, and daemon socket lives in a private temp directory, so the
# user's own ccache and sccache state is never read or written.
# Each step zeroes the counters, runs one compilation, and prints the counters it moved,
# so every line is a derived fact rather than a timing.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CC_BIN="${CC:-cc}"

for tool in ccache sccache rustc "${CC_BIN}" python3; do
    command -v "${tool}" >/dev/null 2>&1 || { echo "missing required tool: ${tool}" >&2; exit 1; }
done

# A canonical temp root (no /var -> /private/var symlink), so base_dir arithmetic is exact.
ROOT="$(mktemp -d)"
ROOT="$(cd "${ROOT}" && pwd -P)"
cleanup() {
    sccache --stop-server >/dev/null 2>&1 || true
    rm -rf "${ROOT}"
}
trap cleanup EXIT

# Hermetic environment: nothing from the caller's ccache or sccache configuration leaks in.
while IFS='=' read -r name _; do
    case "${name}" in CCACHE_* | SCCACHE_* | RUSTC_WRAPPER | RUSTC_WORKSPACE_WRAPPER) unset "${name}" ;; esac
done < <(env)
export LC_ALL=C
# rustup's rustc proxy finds its toolchains through HOME, so pin them before HOME moves.
export RUSTUP_HOME="${RUSTUP_HOME:-${HOME}/.rustup}"
export CARGO_HOME="${CARGO_HOME:-${HOME}/.cargo}"
export HOME="${ROOT}/home"
mkdir -p "${HOME}"
export CCACHE_DIR="${ROOT}/ccache"
export CCACHE_CONFIGPATH="${ROOT}/ccache.conf"
export CCACHE_STATSLOG="${ROOT}/stats.log"
: >"${CCACHE_CONFIGPATH}"

# Two checkouts of the same project at different absolute paths and depths.
ALICE="${ROOT}/alice/proj"
BOB="${ROOT}/bob/work/proj"
for tree in "${ALICE}" "${BOB}"; do
    mkdir -p "${tree}/build"
    cp -R "${LAB_DIR}/project/src" "${LAB_DIR}/project/include" "${tree}/"
done
# Sources must be older than the ccache invocation, or ccache refuses to cache them.
sleep 1

# Counters that describe an outcome; sizes, timestamps, and storage-layer totals are skipped.
OUTCOME_FILTER='^(cache_size_kibibyte|files_in_cache|max_cache_size_kibibyte|max_files_in_cache|stats_updated_timestamp|stats_zeroed_timestamp|local_storage_.*|remote_storage_.*|cleanups_performed)$'

report() {
    local label="$1" moved
    moved="$(ccache --print-stats | awk -F'\t' -v skip="${OUTCOME_FILTER}" '$1 !~ skip && $2 != 0 {printf "%s%s=%s", sep, $1, $2; sep=" "}')"
    # Every step is built to move a counter; silence means ccache never saw the call.
    [[ -n "${moved}" ]] || { echo "step '${label}': no ccache counter moved" >&2; exit 1; }
    printf '%-44s %s\n' "${label}" "${moved}"
}

# ccache_step LABEL DIR CMD...: zero the counters, run CMD in DIR, print what moved.
ccache_step() {
    local label="$1" dir="$2"
    shift 2
    ccache -z >/dev/null
    (cd "${dir}" && "$@") >/dev/null 2>&1 || true
    report "${label}"
}

compile_util() {
    # Absolute paths on purpose: this is what CMake's Makefile and Ninja generators pass.
    local tree="$1"
    shift
    ccache "${CC_BIN}" "$@" -I"${tree}/include" -c "${tree}/src/util.c" -o util.o
}

echo "== ccache $(ccache --print-version) =="
echo
echo "-- 1. Direct mode and its preprocessor-mode fallback --"
ccache_step "cold compile" "${ALICE}/build" compile_util "${ALICE}" -O1
ccache_step "same command again" "${ALICE}/build" compile_util "${ALICE}" -O1
touch "${ALICE}/src/util.c"
sleep 1
ccache_step "touch util.c (mtime only)" "${ALICE}/build" compile_util "${ALICE}" -O1
sed -i.bak 's/the translation unit the lab/the TU the lab/' "${ALICE}/src/util.c"
sleep 1
ccache_step "edit a comment in util.c" "${ALICE}/build" compile_util "${ALICE}" -O1
sed -i.bak 's/the lab edits this comment/the lab rewrites this comment/' "${ALICE}/include/config.h"
sleep 1
ccache_step "edit a comment in config.h" "${ALICE}/build" compile_util "${ALICE}" -O1
ccache_step "add unused -DLAB_UNUSED=1" "${ALICE}/build" compile_util "${ALICE}" -O1 -DLAB_UNUSED=1
ccache_step "change -O1 to -O2" "${ALICE}/build" compile_util "${ALICE}" -O2

echo
echo "-- 2. A second checkout at another absolute path --"
# Bring Bob's copy to the same bytes as Alice's edited copy.
cp "${ALICE}/src/util.c" "${BOB}/src/util.c"
cp "${ALICE}/include/config.h" "${BOB}/include/config.h"
sleep 1
ccache_step "bob, no base_dir" "${BOB}/build" compile_util "${BOB}" -O2
export CCACHE_BASEDIR="${ROOT}"
ccache_step "alice, base_dir=ROOT (first time)" "${ALICE}/build" compile_util "${ALICE}" -O2
ccache_step "bob, base_dir=ROOT" "${BOB}/build" compile_util "${BOB}" -O2
ccache_step "alice, base_dir, -g" "${ALICE}/build" compile_util "${ALICE}" -O2 -g
ccache_step "bob, base_dir, -g (CWD is hashed)" "${BOB}/build" compile_util "${BOB}" -O2 -g
ccache_step "alice, -g -fdebug-prefix-map=CWD=." "${ALICE}/build" compile_util "${ALICE}" -O2 -g -fdebug-prefix-map="${ALICE}/build=."
ccache_step "bob, -g -fdebug-prefix-map=CWD=." "${BOB}/build" compile_util "${BOB}" -O2 -g -fdebug-prefix-map="${BOB}/build=."
unset CCACHE_BASEDIR

echo
echo "-- 3. Time macros --"
LOG="${ROOT}/ccache.log"
ccache_step "stamp.c uses __TIME__" "${ALICE}/build" env CCACHE_LOGFILE="${LOG}" \
    ccache "${CC_BIN}" -c "${ALICE}/src/stamp.c" -o stamp.o
if grep -q 'Disabling direct mode' "${LOG}"; then
    echo "log: __TIME__ found, direct mode disabled for stamp.c"
else
    echo "log: direct mode was NOT disabled for stamp.c"
fi
export CCACHE_NAMESPACE=sloppy CCACHE_SLOPPINESS=time_macros
ccache_step "sloppiness=time_macros, first" "${ALICE}/build" ccache "${CC_BIN}" -c "${ALICE}/src/stamp.c" -o stamp.o
ccache_step "sloppiness=time_macros, again" "${ALICE}/build" ccache "${CC_BIN}" -c "${ALICE}/src/stamp.c" -o stamp.o
unset CCACHE_NAMESPACE CCACHE_SLOPPINESS

echo
echo "-- 4. Depend mode: no preprocessor, no fallback --"
export CCACHE_DEPEND=1 CCACHE_NAMESPACE=depend
ccache_step "depend mode, cold" "${ALICE}/build" compile_util "${ALICE}" -O1 -MD -MF util.d
ccache_step "depend mode, again" "${ALICE}/build" compile_util "${ALICE}" -O1 -MD -MF util.d
sed -i.bak 's/the TU the lab/the unit the lab/' "${ALICE}/src/util.c"
sleep 1
ccache_step "depend mode, edit a comment in util.c" "${ALICE}/build" compile_util "${ALICE}" -O1 -MD -MF util.d
unset CCACHE_DEPEND CCACHE_NAMESPACE

echo
echo "-- 5. Compiler identity is part of the key --"
export CCACHE_COMPILERCHECK=string:toolchain-1
ccache_step "compiler_check=string:toolchain-1" "${ALICE}/build" compile_util "${ALICE}" -O1
ccache_step "compiler_check=string:toolchain-1 again" "${ALICE}/build" compile_util "${ALICE}" -O1
export CCACHE_COMPILERCHECK=string:toolchain-2
ccache_step "compiler_check=string:toolchain-2" "${ALICE}/build" compile_util "${ALICE}" -O1
unset CCACHE_COMPILERCHECK

echo
echo "-- 6. Calls ccache refuses to cache --"
ccache "${CC_BIN}" -I"${ALICE}/include" -c "${ALICE}/src/main.c" -o "${ALICE}/build/main.o" >/dev/null 2>&1
ccache_step "link step" "${ALICE}/build" ccache "${CC_BIN}" util.o main.o -o app
ccache_step "two sources in one -c call" "${ALICE}/build" \
    ccache "${CC_BIN}" -I"${ALICE}/include" -c "${ALICE}/src/util.c" "${ALICE}/src/main.c"
ccache_step "preprocess only (-E)" "${ALICE}/build" \
    ccache "${CC_BIN}" -I"${ALICE}/include" -E "${ALICE}/src/util.c" -o util.i
touch -t 203001010000 "${ALICE}/src/util.c"
# The "too new" guard runs only before a store, so a lookup that hits is still served.
ccache_step "future mtime, a stored result exists" "${ALICE}/build" compile_util "${ALICE}" -O1
ccache_step "future mtime, -O3 (would store)" "${ALICE}/build" compile_util "${ALICE}" -O3

echo
echo "-- 7. Whole-run totals from the stats log --"
ccache --print-log-stats | python3 -I -c '
import sys
c = dict(line.rstrip("\n").split("\t") for line in sys.stdin if "\t" in line)
direct = int(c.get("direct_cache_hit", 0))
pre = int(c.get("preprocessed_cache_hit", 0))
misses = int(c.get("cache_miss", 0))
hits = direct + pre
print("direct hits %d, preprocessed hits %d, misses %d" % (direct, pre, misses))
print("hit rate over cacheable calls: %d/%d = %.1f%%" % (hits, hits + misses, 100.0 * hits / (hits + misses)))
'

echo
echo "== sccache $(sccache --version | awk '{print $2}') =="
export SCCACHE_DIR="${ROOT}/sccache"
export SCCACHE_CONF="${ROOT}/sccache.toml"
export SCCACHE_SERVER_UDS="${ROOT}/sccache.sock"
export SCCACHE_IDLE_TIMEOUT=120
: >"${SCCACHE_CONF}"
sccache --start-server >/dev/null 2>&1

sccache_report() {
    sccache --show-stats --stats-format json | python3 -I -c '
import json, sys
label = sys.argv[1]
s = json.load(sys.stdin)["stats"]
parts = []
for kind, name in (("cache_hits", "hit"), ("cache_misses", "miss")):
    for lang, n in sorted(s[kind]["counts"].items()):
        parts.append("%s[%s]=%d" % (name, lang, n))
for why, n in sorted(s["not_cached"].items()):
    parts.append("not_cached[%s]=%d" % (why, n))
if s["compile_requests"] != 1 or not parts:
    sys.exit("step %r: expected one classified compile request, stats: %r" % (label, s))
print("%-44s %s" % (label, " ".join(parts)))
' "$1"
}

# sccache_step LABEL DIR CMD...: same shape as ccache_step, against the private sccache daemon.
sccache_step() {
    local label="$1" dir="$2"
    shift 2
    sccache -z >/dev/null
    (cd "${dir}" && "$@") >/dev/null 2>&1 || true
    sccache_report "${label}"
}

RUST_A="${ROOT}/alice/rust"
RUST_B="${ROOT}/bob/rust"
for tree in "${RUST_A}" "${RUST_B}"; do
    mkdir -p "${tree}/out"
    cp "${LAB_DIR}/rust/lib.rs" "${LAB_DIR}/rust/main.rs" "${tree}/"
done
sleep 1

rlib() {
    sccache rustc --crate-name labscale --crate-type lib --edition=2021 \
        --emit=link,metadata --out-dir out "$@" lib.rs
}

echo "-- 8. rustc through sccache --"
sccache_step "lib crate, cold" "${RUST_A}" rlib
sccache_step "lib crate, again" "${RUST_A}" rlib
sccache_step "lib crate, same args, other CWD" "${RUST_B}" rlib
sccache_step "lib crate, -C incremental=inc" "${RUST_A}" rlib -C incremental=inc
sccache_step "bin crate" "${RUST_A}" \
    sccache rustc --crate-name labmain --crate-type bin --edition=2021 --emit=link --out-dir out main.rs
sccache_step "lib crate, --edition 2021 (space)" "${RUST_A}" \
    sccache rustc --crate-name labscale --crate-type lib --edition 2021 --emit=link,metadata --out-dir out lib.rs

echo
echo "-- 9. C through sccache (preprocessor cache mode on local disk) --"
sccache_step "C cold" "${ALICE}/build" \
    sccache "${CC_BIN}" -O1 -I"${ALICE}/include" -c "${ALICE}/src/main.c" -o main-sc.o
sccache_step "C again" "${ALICE}/build" \
    sccache "${CC_BIN}" -O1 -I"${ALICE}/include" -c "${ALICE}/src/main.c" -o main-sc.o
sccache --show-stats --stats-format json | python3 -I -c '
import json, sys
s = json.load(sys.stdin)
mode = str(s["use_preprocessor_cache_mode"]).lower()
print("use_preprocessor_cache_mode=%s max_cache_size=%d" % (mode, s["max_cache_size"]))
'
