#!/usr/bin/env bash
# Drill: an intermittent segfault across a Python-to-Rust FFI boundary.
# A Rust cdylib (ticktape/) keeps a raw pointer into a Python bytes object that
# Python later frees; driver.py calls it through ctypes. The lab shows why the
# crash is intermittent (pymalloc reuse, glibc mmap threshold), then triages it
# with faulthandler, gdb, and AddressSanitizer preloaded into the interpreter.
# requires: nightly
# Also needs on PATH: python3, rustup with a nightly toolchain, and cc.
# Linux also needs gdb built with Python (Stage 3 fails loudly without it) and a shared
# ASan runtime: clang libclang_rt.asan of rustc's LLVM major version, or gcc libasan.so.
# macOS needs the nightly's librustc-nightly_rt.asan.dylib; it has no gdb, so Stage 3 only says so.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C
unset PYTHONMALLOC PYTHONFAULTHANDLER PYTHONDEVMODE ASAN_OPTIONS LD_PRELOAD DYLD_INSERT_LIBRARIES
ulimit -c 0 # the drill reads live state; no core files in the work directory

die() {
  echo "FAIL: $*" >&2
  exit 1
}

for tool in python3 rustup cc; do
  command -v "${tool}" >/dev/null 2>&1 || die "${tool} not found on PATH"
done
cargo +nightly --version >/dev/null 2>&1 || die "no nightly toolchain (rustup toolchain install nightly)"

OS="$(uname -s)"
case "${OS}" in
  Darwin) LIB_EXT="dylib" ;;
  Linux) LIB_EXT="so" ;;
  *) die "unsupported operating system: ${OS}" ;;
esac

PY="$(command -v python3)"
DRIVER="${SCRIPT_DIR}/driver.py"
DIGEST="${SCRIPT_DIR}/digest.py"
HOST="$(rustc +nightly -vV | sed -n 's/^host: //p')"

# Build from a copy so cargo's Cargo.lock and target/ never land in the lab directory.
cp -R "${SCRIPT_DIR}/ticktape" "${WORK}/ticktape"
build() { # build TARGET_DIR [RUSTFLAGS]
  CARGO_TARGET_DIR="$1" RUSTFLAGS="${2:-}" \
    cargo +nightly build --release --offline --quiet \
    --manifest-path "${WORK}/ticktape/Cargo.toml" --target "${HOST}"
}
build "${WORK}/plain"
LIB="${WORK}/plain/${HOST}/release/libticktape.${LIB_EXT}"
[ -f "${LIB}" ] || die "cargo did not produce ${LIB}"

cd "${WORK}"

# run_py OUT ERR ARGS...: run python3, keep stdout and stderr, set STATUS.
run_py() {
  local out="$1" err="$2"
  shift 2
  STATUS=0
  { "$@" >"${out}" 2>"${err}"; } 2>/dev/null || STATUS=$?
}

echo "=== Stage 1: the symptom depends on payload size and allocator history ==="
for mode in small warm large; do
  run_py "${mode}.out" "${mode}.err" "${PY}" "${DRIVER}" "${LIB}" "${mode}"
  cat "${mode}.out"
  if [ "${STATUS}" -gt 128 ]; then
    echo "${mode}: process killed by signal $((STATUS - 128))"
  else
    echo "${mode}: process exit status ${STATUS}"
  fi
done
run_py fixed.out fixed.err "${PY}" "${DRIVER}" "${LIB}" fixed
cat fixed.out
echo "fixed: process exit status ${STATUS}"
echo

echo "=== Stage 2: faulthandler names the Python caller, not the Rust callee ==="
run_py fh.out fh.err "${PY}" -X faulthandler "${DRIVER}" "${LIB}" large
"${PY}" "${DIGEST}" faulthandler fh.err "${STATUS}"
echo "--- forced fault: a read at the unmapped address 16, on any platform ---"
run_py ff.out ff.err "${PY}" -X faulthandler "${DRIVER}" "${LIB}" fault
"${PY}" "${DIGEST}" faulthandler ff.err "${STATUS}"
echo

echo "=== Stage 3: gdb stops at the faulting load and checks the address ==="
if [ "${OS}" = "Linux" ]; then
  command -v gdb >/dev/null 2>&1 || die "gdb not found on PATH (Stage 3 needs gdb built with Python)"
  gdb -nx -batch -ex 'python import gdb' >/dev/null 2>&1 || die "this gdb has no embedded Python"
  export DRILL_ADDR_FILE="${WORK}/addr.txt"
  gdb -nx -batch -iex "set debuginfod enabled off" -x "${SCRIPT_DIR}/gdb_triage.py" \
    --args "${PY}" "${DRIVER}" "${LIB}" large "${DRILL_ADDR_FILE}" >gdb.out 2>gdb.err || true
  grep '^TRIAGE|' gdb.out | sed 's/^TRIAGE|//' | grep -v '^done$' || true
  grep -qx 'TRIAGE|done' gdb.out || {
    cat gdb.err >&2
    die "gdb_triage.py did not finish"
  }
else
  echo "gdb: not available on ${OS} (gdb triage is Linux-only)"
fi
echo

echo "=== Stage 4: AddressSanitizer, preloaded into an uninstrumented python3 ==="
pick_asan_runtime() {
  if [ -n "${DRILL_ASAN_RUNTIME:-}" ]; then
    echo "${DRILL_ASAN_RUNTIME}"
    return 0
  fi
  if [ "${OS}" = "Darwin" ]; then
    local sysroot dylib
    sysroot="$(rustc +nightly --print sysroot 2>/dev/null || true)"
    if [ -n "${sysroot}" ]; then
      dylib="${sysroot}/lib/rustlib/${HOST}/lib/librustc-nightly_rt.asan.dylib"
      if [ -f "${dylib}" ]; then
        echo "${dylib}"
        return 0
      fi
      dylib="${sysroot}/lib/librustc-nightly_rt.asan.dylib"
      if [ -f "${dylib}" ]; then
        echo "${dylib}"
        return 0
      fi
    fi
    dylib="$(clang --print-file-name=libclang_rt.asan_osx_dynamic.dylib 2>/dev/null || true)"
    if [ -f "${dylib}" ]; then
      echo "${dylib}"
      return 0
    fi
    return 1
  fi

  local llvm_major arch cc name path
  llvm_major="$(rustc +nightly -vV | sed -n 's/^LLVM version: \([0-9][0-9]*\).*/\1/p')"
  arch="$(uname -m)"
  for cc in "clang-${llvm_major}" clang; do
    command -v "${cc}" >/dev/null 2>&1 || continue
    [ "$("${cc}" -dumpversion | cut -d. -f1)" = "${llvm_major}" ] || continue
    for name in "libclang_rt.asan-${arch}.so" libclang_rt.asan.so; do
      path="$("${cc}" -print-file-name="${name}")"
      [ -f "${path}" ] && {
        echo "${path}"
        return 0
      }
    done
  done
  if command -v gcc >/dev/null 2>&1; then
    path="$(gcc -print-file-name=libasan.so)"
    [ -f "${path}" ] && {
      echo "${path}"
      return 0
    }
  fi
  return 1
}

pick_asan_python() {
  if [ "${OS}" = "Darwin" ]; then
    local py_app
    py_app="$("${PY}" -c "
import sys, pathlib
p = pathlib.Path(sys.executable).resolve()
for parent in p.parents:
    cand = parent / 'Resources' / 'Python.app' / 'Contents' / 'MacOS' / 'Python'
    if cand.is_file():
        print(cand)
        break
" 2>/dev/null || true)"
    if [ -n "${py_app}" ] && [ -x "${py_app}" ]; then
      echo "${py_app}"
      return 0
    fi
  fi
  echo "${PY}"
}

ASAN_RT="$(pick_asan_runtime)" || die "no shared ASan runtime found; set DRILL_ASAN_RUNTIME"
echo "ASan runtime used: ${ASAN_RT}" >&2
ASAN_PY="$(pick_asan_python)"

build "${WORK}/asan" "-Zsanitizer=address -Cforce-frame-pointers=yes"
ALIB="${WORK}/asan/${HOST}/release/libticktape.${LIB_EXT}"
[ -f "${ALIB}" ] || die "cargo did not produce ${ALIB}"

for allocator in pymalloc malloc; do
  for mode in small large fixed; do
    tag="PYTHONMALLOC=${allocator}, ${mode}"
    if [ "${OS}" = "Darwin" ]; then
      run_py asan.out asan.err env DYLD_INSERT_LIBRARIES="${ASAN_RT}" ASAN_OPTIONS=detect_leaks=0 \
        PYTHONMALLOC="${allocator}" "${ASAN_PY}" "${DRIVER}" "${ALIB}" "${mode}"
    else
      run_py asan.out asan.err env LD_PRELOAD="${ASAN_RT}" ASAN_OPTIONS=detect_leaks=0 \
        PYTHONMALLOC="${allocator}" "${PY}" "${DRIVER}" "${ALIB}" "${mode}"
    fi
    if grep -q 'Interceptors are not working\|does not come first\|incompatible ASan runtimes\|undefined symbol: __asan' asan.err; then
      cat asan.err >&2
      die "the ASan runtime did not take over; see the message above"
    fi
    echo "--- ${tag} ---"
    if [ "${allocator}" = malloc ] && [ "${mode}" = small ]; then
      "${PY}" "${DIGEST}" asan asan.err "${STATUS}"
    else
      "${PY}" "${DIGEST}" asan asan.err "${STATUS}" | sed -n '1p;3p'
    fi
    if [ "${mode}" = fixed ] || { [ "${allocator}" = pymalloc ] && [ "${mode}" = small ]; }; then
      grep -v 'reading\|returned' asan.out || true
    fi
  done
done
