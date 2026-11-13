#!/usr/bin/env bash
# Miri under Stacked Borrows and Tree Borrows, against AddressSanitizer, ThreadSanitizer, and the
# standard library's own precondition checks, on eleven small programs that each break one rule.
# requires: nightly
#
# Needs the nightly toolchain with the miri and rust-src components and a built Miri sysroot:
#   rustup toolchain install nightly --component miri rust-src
#   cargo +nightly miri setup    (once, online: it downloads the standard library's dependencies)
# After that setup the lab never touches the network. Every cargo call is --offline, and the
# -Zbuild-std step takes std's dependencies from the vendor directory that rust-src ships, so it
# does not depend on what happens to be in the cargo registry cache.
#
# The recording is tied to one nightly, because Miri's and rustc's diagnostic wording follows the
# compiler. To reproduce it exactly, install that nightly and name it:
#   rustup toolchain install nightly-2026-10-25 --component miri rust-src
#   MIRI_LAB_TOOLCHAIN=nightly-2026-10-25 bash run.sh
set -euo pipefail

RECORDED_RUSTC="rustc 1.101.0-nightly (12433c307 2026-10-24)"
TOOLCHAIN="${MIRI_LAB_TOOLCHAIN:-nightly}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/miri_tree_borrows.XXXXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
cd "${SCRIPT_DIR}"
export LC_ALL=C
export CARGO_TERM_COLOR=never
unset RUSTFLAGS MIRIFLAGS CARGO_BUILD_TARGET CARGO_ENCODED_RUSTFLAGS RUSTC_WRAPPER
unset ASAN_OPTIONS TSAN_OPTIONS LSAN_OPTIONS RUST_BACKTRACE

die() { # die <exit status> <message>: stop with a message on stderr, which the harness shows
    echo "miri_tree_borrows: $2" >&2
    exit "$1"
}

if ! cargo "+${TOOLCHAIN}" miri --version >/dev/null 2>&1; then
    die 2 "Miri is not installed for '${TOOLCHAIN}': rustup component add --toolchain ${TOOLCHAIN} miri rust-src"
fi
# With a sysroot already built this returns at once; without one it would need the network.
if ! CARGO_NET_OFFLINE=true cargo "+${TOOLCHAIN}" miri setup >"${WORK}/miri-setup.log" 2>&1; then
    cat "${WORK}/miri-setup.log" >&2
    die 2 "no Miri sysroot, and it cannot be built offline; run once online: cargo +${TOOLCHAIN} miri setup"
fi
ACTUAL_RUSTC="$(rustc "+${TOOLCHAIN}" --version)"
if [[ "${ACTUAL_RUSTC}" != "${RECORDED_RUSTC}" ]]; then
    echo "miri_tree_borrows: recorded with '${RECORDED_RUSTC}', running '${ACTUAL_RUSTC}';" \
        "a diff in diagnostic wording alone means re-record, not a changed mechanism" >&2
fi

HOST="$(rustc "+${TOOLCHAIN}" -vV | sed -n 's/^host: //p')"
SYSROOT="$(rustc "+${TOOLCHAIN}" --print sysroot)"
STD_VENDOR="${SYSROOT}/lib/rustlib/src/rust/library/vendor"
# rust-src vendors every crate the standard library depends on. Replacing crates.io with that
# directory lets -Zbuild-std resolve offline on a machine whose registry cache is empty.
BUILD_STD=(-Zbuild-std)
if [[ -d "${STD_VENDOR}" ]]; then
    BUILD_STD+=(--config 'source.crates-io.replace-with="rust-src-vendor"'
        --config "source.rust-src-vendor.directory='${STD_VENDOR}'")
fi

BIN=miri_tree_borrows
ALIASING_CASES=(reserved_foreign_read reserved_foreign_write protected_foreign_write
    write_past_element write_through_shared)
CASES=("${ALIASING_CASES[@]}" use_after_free uninit_read leak data_race lost_update)
# One schedule by construction for the two threaded cases: with preemption off, main runs until it
# blocks in join and only then does the spawned thread run, so the race report names the same two
# accesses whatever the seed. Section 5 turns preemption back on and sweeps seeds.
MIRI_BASE="-Zmiri-preemption-rate=0"

# build <name> <cargo build args...>: a build that must succeed; its stderr is shown if it does not.
build() {
    local name="$1"
    shift
    if ! cargo "+${TOOLCHAIN}" build -q --offline "$@" 2>"${WORK}/${name}.log"; then
        cat "${WORK}/${name}.log" >&2
        die 1 "cargo build for ${name} failed (see above)"
    fi
}

# Miri prints tag and allocation numbers that shift with any change to the program or std.
scrub_ids() {
    sed -E -e 's/<[0-9]+>/<TAG>/g' -e 's/alloc[0-9]+/ALLOC/g' -e 's/^ *= *//'
}

# miri_run <MIRIFLAGS> <cargo miri run args...>: sets STATUS, writes ${WORK}/miri.out and miri.log.
miri_run() {
    local flags="$1"
    shift
    STATUS=0
    MIRIFLAGS="${flags}" CARGO_TARGET_DIR="${WORK}/miri-target" \
        cargo "+${TOOLCHAIN}" miri run -q --offline "$@" >"${WORK}/miri.out" 2>"${WORK}/miri.log" || STATUS=$?
}

# Run one case under Miri with the given extra MIRIFLAGS and print the verdict plus the key notes.
miri_case() {
    local flags="$1" case="$2" log="${WORK}/miri.log"
    miri_run "${MIRI_BASE} ${flags}" -- "${case}"
    if [[ ${STATUS} -eq 0 ]]; then
        # "accepted" must mean the program ran to its last line, not that nothing ran.
        grep -q "^${case} returned " "${WORK}/miri.out" || die 1 "Miri exited 0 without running ${case}"
        echo "  ${case}: accepted (exit 0)"
        return
    fi
    # A nonzero exit counts as a verdict only if Miri itself reported the program.
    if ! grep -qE '^error: (Undefined Behavior|memory leaked)' "${log}"; then
        cat "${log}" >&2
        die 1 "Miri failed to run ${case} (see above)"
    fi
    echo "  ${case}: exit ${STATUS}"
    grep -m1 -E '^error: ' "${log}" | scrub_ids | sed 's/^/    /'
    grep -E 'was created by|was later invalidated|is this argument|has state|in the initial state|later transitioned|is foreign to the protected tag|would cause the protected tag|protected tags must never' "${log}" |
        scrub_ids | sed 's/^/    /' || true
}

# native_run <log> <binary> <case>: sets STATUS; stdout must hold the case's last line on exit 0.
# The group's redirection absorbs bash's "Abort trap" notice when the program dies of SIGABRT.
native_run() {
    local log="$1" bin="$2" case="$3"
    STATUS=0
    { "${bin}" "${case}" >"${WORK}/native.out" 2>"${log}" || STATUS=$?; } 2>/dev/null
    if [[ ${STATUS} -eq 0 ]] && ! grep -q "^${case} returned " "${WORK}/native.out"; then
        die 1 "${bin} ${case} exited 0 without reaching its last line"
    fi
}

echo "== 1. Native debug build: no checker, every case runs to completion"
CARGO_TARGET_DIR="${WORK}/native-target" build native-debug
NATIVE_DEBUG="${WORK}/native-target/debug/${BIN}"
for c in "${CASES[@]}"; do
    native_run "${WORK}/native.log" "${NATIVE_DEBUG}" "${c}"
    echo "  ${c}: exit ${STATUS}"
done
echo

echo "== 2. Miri, default aliasing model (Stacked Borrows)"
for c in "${CASES[@]}"; do miri_case "" "${c}"; done
echo

echo "== 3. Miri, -Zmiri-tree-borrows"
for c in "${CASES[@]}"; do miri_case "-Zmiri-tree-borrows" "${c}"; done
echo

echo "== 4. Controls: Miri with the responsible check switched off"
echo "  -Zmiri-disable-stacked-borrows (no aliasing model):"
for c in "${ALIASING_CASES[@]}"; do miri_case "-Zmiri-disable-stacked-borrows" "${c}" | sed 's/^/  /'; done
echo "  -Zmiri-ignore-leaks:"
miri_case "-Zmiri-ignore-leaks" leak | sed 's/^/  /'
echo "  -Zmiri-disable-data-race-detector:"
miri_case "-Zmiri-disable-data-race-detector" data_race | sed 's/^/  /'
echo

echo "== 5. Miri across schedules: -Zmiri-many-seeds=0..8 at the default preemption rate"
for c in data_race lost_update; do
    miri_run "-Zmiri-many-seeds=0..8 -Zmiri-many-seeds-keep-going" -- "${c}"
    tried="$(grep -c '^Trying seed: ' "${WORK}/miri.log" || true)"
    races="$(grep -c '^error: Undefined Behavior: Data race detected' "${WORK}/miri.log" || true)"
    errors="$(grep -c '^error: Undefined Behavior' "${WORK}/miri.log" || true)"
    echo "  ${c}: ${tried} seeds tried, ${races} report a data race, ${errors} report any undefined behavior"
done
echo

echo "== 6. AddressSanitizer (-Zsanitizer=address, std not rebuilt, ASAN_OPTIONS=detect_leaks=1)"
RUSTFLAGS="-Zsanitizer=address" CARGO_TARGET_DIR="${WORK}/asan-target" build asan-debug --target "${HOST}"
ASAN_DEBUG="${WORK}/asan-target/${HOST}/debug/${BIN}"
asan_report() { # the first sanitizer banner in a log, or "no report"
    grep -m1 -oE 'ERROR: AddressSanitizer: [a-z-]+|ERROR: LeakSanitizer: [a-z ]+' "$1" || echo "no report"
}
for c in "${CASES[@]}"; do
    # On Darwin the sanitizer runtimes default to abort_on_error=1, so a report ends in SIGABRT
    # (exit 134) instead of _exit(1).
    ASAN_OPTIONS=detect_leaks=1 native_run "${WORK}/asan.log" "${ASAN_DEBUG}" "${c}"
    echo "  ${c}: exit ${STATUS}, $(asan_report "${WORK}/asan.log")"
done
echo

echo "== 7. ThreadSanitizer (-Zsanitizer=thread)"
echo "  without -Zbuild-std:"
STATUS=0
RUSTFLAGS="-Zsanitizer=thread" CARGO_TARGET_DIR="${WORK}/tsan-mixed-target" \
    cargo "+${TOOLCHAIN}" build -q --offline --target "${HOST}" 2>"${WORK}/tsan-mixed.log" || STATUS=$?
echo "    build exit ${STATUS}"
MIXED="${WORK}/tsan-mixed.log"
# The backticks in these patterns are literal: rustc quotes flag and crate names with them.
# shellcheck disable=SC2016
{
    grep -m1 -E '^error: mixing' "${MIXED}" || echo "(no ABI mismatch error)"
    grep -m1 -E 'in dependency `std`' "${MIXED}" | scrub_ids || echo "(std is not named)"
    # One error per prebuilt sysroot crate; the count moves with the sysroot, these three do not.
    named="$(grep -oE 'in dependency `(std|core|alloc)`' "${MIXED}" | sed -E 's/.*`(.*)`/\1/' | sort -u | xargs || true)"
    echo "one such error per prebuilt sysroot crate, among them: ${named:-none of alloc, core, std}"
    grep -m1 -E 'unsafe-allow-abi-mismatch' "${MIXED}" | scrub_ids || echo "(no escape hatch offered)"
} | sed 's/^/    /'
echo "  with -Cunsafe-allow-abi-mismatch=sanitizer (std still not instrumented):"
STATUS=0
RUSTFLAGS="-Zsanitizer=thread -Cunsafe-allow-abi-mismatch=sanitizer" CARGO_TARGET_DIR="${WORK}/tsan-hatch-target" \
    cargo "+${TOOLCHAIN}" build -q --offline --target "${HOST}" 2>"${WORK}/tsan-hatch.log" || STATUS=$?
echo "    build exit ${STATUS}"
echo "  with -Zbuild-std (std rebuilt with TSan):"
RUSTFLAGS="-Zsanitizer=thread" CARGO_TARGET_DIR="${WORK}/tsan-target" build tsan "${BUILD_STD[@]}" --target "${HOST}"
TSAN="${WORK}/tsan-target/${HOST}/debug/${BIN}"
tsan_runs() { # tsan_runs <case>: five runs; prints how many reported a race and the last exit status
    local case="$1" detected=0
    for _ in 1 2 3 4 5; do
        native_run "${WORK}/tsan.log" "${TSAN}" "${case}"
        if grep -q 'WARNING: ThreadSanitizer: data race' "${WORK}/tsan.log"; then
            detected=$((detected + 1))
        fi
    done
    echo "    ${case}: ${detected}/5 runs report 'WARNING: ThreadSanitizer: data race', last exit ${STATUS}"
}
tsan_runs data_race
TSAN_OPTIONS=abort_on_error=0 native_run "${WORK}/tsan.log" "${TSAN}" data_race
echo "    data_race with TSAN_OPTIONS=abort_on_error=0: exit ${STATUS}"
tsan_runs lost_update
native_run "${WORK}/tsan.log" "${TSAN}" reserved_foreign_read
echo "    control, single-threaded reserved_foreign_read: exit ${STATUS}, $(grep -c 'WARNING: ThreadSanitizer' "${WORK}/tsan.log" || true) reports"
echo

echo "== 8. Standard library precondition checks: unchecked_index reads v[3] of a 3-byte Vec"
precondition() { # the library's precondition panic line in a log, or "no precondition panic"
    grep -m1 -oE 'unsafe precondition\(s\) violated: [^.]*' "$1" || echo "no precondition panic"
}
native_run "${WORK}/pre.log" "${NATIVE_DEBUG}" unchecked_index
echo "  native debug: exit ${STATUS}, $(precondition "${WORK}/pre.log")"
CARGO_TARGET_DIR="${WORK}/native-target" build native-release --release
native_run "${WORK}/pre.log" "${WORK}/native-target/release/${BIN}" unchecked_index
echo "  native release: exit ${STATUS}, $(precondition "${WORK}/pre.log")"
RUSTFLAGS="-Zub-checks=no" CARGO_TARGET_DIR="${WORK}/nochecks-target" build native-nochecks
native_run "${WORK}/pre.log" "${WORK}/nochecks-target/debug/${BIN}" unchecked_index
echo "  native debug with -Zub-checks=no: exit ${STATUS}, $(precondition "${WORK}/pre.log")"
miri_first_error() { grep -m1 -E '^error: ' "${WORK}/miri.log" | scrub_ids || echo "no Miri error"; }
miri_run "${MIRI_BASE}" -- unchecked_index
echo "  Miri, debug profile: exit ${STATUS}, $(precondition "${WORK}/miri.log")"
echo "    $(miri_first_error)"
miri_run "${MIRI_BASE}" --release -- unchecked_index
echo "  Miri, release profile: exit ${STATUS}, $(precondition "${WORK}/miri.log")"
echo "    $(miri_first_error)"
native_run "${WORK}/asan.log" "${ASAN_DEBUG}" unchecked_index
echo "  ASan debug: exit ${STATUS}, $(precondition "${WORK}/asan.log"), $(asan_report "${WORK}/asan.log")"
RUSTFLAGS="-Zsanitizer=address" CARGO_TARGET_DIR="${WORK}/asan-target" build asan-release --release --target "${HOST}"
native_run "${WORK}/asan.log" "${WORK}/asan-target/${HOST}/release/${BIN}" unchecked_index
echo "  ASan release: exit ${STATUS}, $(precondition "${WORK}/asan.log"), $(asan_report "${WORK}/asan.log")"
