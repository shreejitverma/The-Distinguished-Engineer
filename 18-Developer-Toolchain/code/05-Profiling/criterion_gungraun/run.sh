#!/usr/bin/env bash
# requires: network
# Rust benchmarking lab: Criterion.rs 0.8.2, Divan 0.1.21, dhat 0.3.3, Gungraun 0.20.0.
# Needs cargo (rustc >= 1.88) and crates.io access. The Criterion, Divan, and dhat stages run on
# macOS and Linux. The Gungraun stage runs only on Linux, needs valgrind on PATH, and builds
# gungraun-runner 0.20.0 into a temporary root; on other systems it prints one skip line.
# Every printed line is a derived fact (yes/no, a count, a verdict), never a raw timing.
set -euo pipefail

export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1
export CARGO_TERM_COLOR=never
export GUNGRAUN_COLOR=never

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

cp -R "${SCRIPT_DIR}/rbench" "${WORK}/rbench"
export CARGO_TARGET_DIR="${WORK}/target"
LOG="${WORK}/logs"
mkdir -p "${LOG}"
MANIFEST="${WORK}/rbench/Cargo.toml"

# run NAME CMD...: stdout to logs/NAME.out, stderr to logs/NAME.err; on failure show stderr.
run() {
    local name="$1"
    shift
    if ! "$@" >"${LOG}/${name}.out" 2>"${LOG}/${name}.err"; then
        echo "step ${name} failed: $*" >&2
        tail -n 40 "${LOG}/${name}.err" >&2
        exit 1
    fi
}

# Stage 1: Criterion. Save a baseline from the fast build, then compare the 4x slower
# `slow` build against it with --baseline, which must not overwrite the baseline.
CRIT=(--warm-up-time 1 --measurement-time 2 --noplot --color never)
run crit_base cargo bench --locked --manifest-path "${MANIFEST}" --bench crit -- "${CRIT[@]}" --save-baseline base
python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' \
    "${CARGO_TARGET_DIR}/criterion/checksum/n4096/base/sample.json" >"${LOG}/base_hash_before"
run crit_slow cargo bench --locked --manifest-path "${MANIFEST}" --features slow --bench crit -- "${CRIT[@]}" --baseline base

# Stage 2: Divan with AllocProfiler.
run divan cargo bench --locked --manifest-path "${MANIFEST}" --bench div -- --color never

# Stage 3: dhat heap usage testing, one process per measurement.
cargo build --locked --quiet --release --manifest-path "${MANIFEST}" --example heap 2>"${LOG}/heap_build.err" || {
    tail -n 40 "${LOG}/heap_build.err" >&2
    exit 1
}
HEAP="${CARGO_TARGET_DIR}/release/examples/heap"
run dhat_grow "${HEAP}" grow
run dhat_reserved "${HEAP}" reserved
mkdir -p "${WORK}/dhat_assert"
set +e
(cd "${WORK}/dhat_assert" && "${HEAP}" assert >"${LOG}/dhat_assert.out" 2>"${LOG}/dhat_assert.err")
echo "$?" >"${LOG}/dhat_assert.code"
set -e
if [ -f "${WORK}/dhat_assert/dhat-heap.json" ]; then echo yes >"${LOG}/dhat_assert.json"; else echo no >"${LOG}/dhat_assert.json"; fi

# Stage 4: Gungraun under Callgrind (Linux only).
if [ "$(uname -s)" = "Linux" ]; then
    command -v valgrind >/dev/null || {
        echo "valgrind is not on PATH; the Gungraun stage needs it (apt-get install valgrind)" >&2
        exit 1
    }
    run runner_install cargo install --locked --quiet --version 0.20.0 --root "${WORK}/runner" gungraun-runner
    export GUNGRAUN_RUNNER="${WORK}/runner/bin/gungraun-runner"
    run gung_first cargo bench --locked --manifest-path "${MANIFEST}" --bench gung
    run gung_second cargo bench --locked --manifest-path "${MANIFEST}" --bench gung
    set +e
    cargo bench --locked --manifest-path "${MANIFEST}" --features slow --bench gung -- --callgrind-limits='ir=5%' \
        >"${LOG}/gung_slow.out" 2>"${LOG}/gung_slow.err"
    echo "$?" >"${LOG}/gung_slow.code"
    set -e
fi

python3 "${SCRIPT_DIR}/check.py" "${LOG}" "${CARGO_TARGET_DIR}"
