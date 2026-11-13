#!/usr/bin/env bash
# requires: macos
#
# Drill: one library API, three implementations, one driver.
#   kit_bloated  everything generic and force-inlined: the regression under investigation
#   kit_dyn      trait objects per record: fixes the build, breaks inlining in the hot loop
#   kit_lean     non-generic inner functions plus a small generic kernel: the fix
#
# The lab builds all three in release mode and prints only facts that repeat exactly:
# digests, retired instructions per record, unoptimized LLVM IR lines and copies, machine code
# bytes from the symbol table, counts of LLVM inlining remarks, and wide-margin comparisons
# of `cargo build --timings` durations. Seconds and cycles go to stderr, never to stdout.
#
# Why macos: the per-record instruction count comes from proc_pid_rusage(2) (ri_instructions),
# which needs no privilege on a bare-metal Mac. The same number on Linux needs perf_event_open.
# Beyond the harness vocabulary the lab needs cargo and rustc (stable), c++filt, nm, and size
# (Xcode Command Line Tools); the preflight stops with exit 2 and names what is missing.
# No network: the workspace has no external dependencies and builds with --offline --locked.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

need() { # need <command> <how to get it>
    command -v "$1" >/dev/null 2>&1 || {
        echo "drill_rust_bloat: missing prerequisite '$1' ($2)" >&2
        exit 2
    }
}
need cargo "https://rustup.rs/"
need rustc "https://rustup.rs/"
need c++filt "xcode-select --install"
need nm "xcode-select --install"
need size "xcode-select --install"
need python3 "xcode-select --install"

cd "$SCRIPT_DIR"
export CARGO_TARGET_DIR="$WORK/target"
export CARGO_INCREMENTAL=0
export CARGO_TERM_COLOR=never
export PYTHONDONTWRITEBYTECODE=1 # the tools import each other; keep the lab directory clean
unset RUSTFLAGS CARGO_ENCODED_RUSTFLAGS CARGO_BUILD_RUSTFLAGS RUSTC_WRAPPER
TOOLS="$SCRIPT_DIR/tools"
BIN="$CARGO_TARGET_DIR/release"

quiet() { # quiet <log> <command...>: run a build step, show its log only if it fails
    local log="$1"
    shift
    "$@" >"$log" 2>&1 || {
        cat "$log" >&2
        exit 1
    }
}

# --- Build everything once, cold, with the timing report -------------------------------------
quiet "$WORK/build.log" cargo build --release --offline --locked --timings

# --- Unoptimized LLVM IR per crate, exactly as cargo-llvm-lines asks for it ------------------
emit_ir() { # emit_ir <outdir> <cargo rustc target selection...>
    local out="$1"
    shift
    mkdir -p "$out"
    quiet "$out/log" cargo rustc --release --offline --locked "$@" -- \
        --emit=llvm-ir -Cno-prepopulate-passes -Cpasses=name-anon-globals -Ccodegen-units=1 -o "$out/crate"
}
for v in bloated dyn lean; do
    emit_ir "$WORK/ir/kit_$v" -p "kit_$v" --lib
    emit_ir "$WORK/ir/app_$v" -p apps --bin "app_$v"
done
ir_total() { python3 "$TOOLS/llvm_lines.py" --total-only "$WORK/ir/$1"/*.ll; }

RUSTC_MINOR="$(rustc --version | awk '{print $2}' | cut -d. -f1,2)"
MANGLING="$(python3 "$TOOLS/llvm_lines.py" --mangling "$WORK/ir/app_bloated"/*.ll)"
echo "toolchain: rustc $RUSTC_MINOR, release profile, symbol mangling $MANGLING"
echo

# --- 1. Behavior and instructions per record -------------------------------------------------
echo "== 1. Same driver, three versions of the library =="
printf '%-9s %-17s %-8s %-17s %-9s %s\n' version digest rejects reject_digest records insn/record
REFERENCE=""
AGREE=yes
for v in bloated dyn lean; do
    line="$("$BIN/app_$v" 2>"$WORK/run_$v.err")"
    sed "s/^/app_$v /" "$WORK/run_$v.err" >&2
    field() { tr ' ' '\n' <<<"$line" | sed -n "s/^$1=//p"; }
    insn="$(field insn_per_record)"
    echo "$insn" >"$WORK/insn_$v"
    printf '%-9s %-17s %-8s %-17s %-9s %s\n' "$v" "$(field digest)" "$(field rejects)" \
        "$(field reject_digest)" "$(field records)" "$insn"
    behavior="${line% insn_per_record=*}"
    [ -z "$REFERENCE" ] && REFERENCE="$behavior"
    [ "$behavior" = "$REFERENCE" ] || AGREE=no
done
echo "all three versions accept, reject, and compute the same things: $AGREE"

# Does link-time optimization rescue the trait-object version? Fat LTO puts every crate in one
# LLVM module, so the crate boundary is gone; the vtable calls are still there.
quiet "$WORK/lto.log" env CARGO_TARGET_DIR="$WORK/target_lto" CARGO_PROFILE_RELEASE_LTO=fat \
    cargo build --release --offline --locked -p apps --bin app_dyn
lto_line="$("$WORK/target_lto/release/app_dyn" 2>/dev/null)"
[ "${lto_line% insn_per_record=*}" = "$REFERENCE" ] || {
    echo "drill_rust_bloat: the fat-LTO build of app_dyn computed different results" >&2
    exit 1
}
echo "${lto_line##*insn_per_record=}" >"$WORK/insn_dyn_lto"
if [ "$(cat "$WORK/insn_lean")" = unavailable ]; then
    echo "retired-instruction counter unavailable on this host (proc_pid_rusage returned no ri_instructions)"
else
    bloated="$(cat "$WORK/insn_bloated")" dyn="$(cat "$WORK/insn_dyn")" lean="$(cat "$WORK/insn_lean")"
    awk -v b="$bloated" -v d="$dyn" -v l="$lean" 'BEGIN {
        printf "dyn retires %.1fx the instructions per record of lean; bloated retires %.1fx\n", d / l, b / l
    }'
    still=no
    [ "$(cat "$WORK/insn_dyn_lto")" -ge $((4 * lean)) ] && still=yes
    echo "dyn rebuilt with lto = \"fat\": same results, and still at least 4x lean: $still"
fi
echo

# --- 2. Where the code is generated ----------------------------------------------------------
echo "== 2. Unoptimized LLVM IR that rustc hands to LLVM, per crate =="
printf '%-9s %-12s %9s %10s\n' version crate "IR lines" functions
for v in bloated dyn lean; do
    read -r kit_lines kit_fns <<<"$(ir_total "kit_$v")"
    read -r app_lines app_fns <<<"$(ir_total "app_$v")"
    printf '%-9s %-12s %9s %10s\n' "$v" "kit_$v" "$kit_lines" "$kit_fns"
    printf '%-9s %-12s %9s %10s\n' "$v" "app_$v" "$app_lines" "$app_fns"
    echo $((kit_lines + app_lines)) >"$WORK/ir_total_$v"
done
awk -v b="$(cat "$WORK/ir_total_bloated")" -v d="$(cat "$WORK/ir_total_dyn")" -v l="$(cat "$WORK/ir_total_lean")" 'BEGIN {
    printf "library plus application: bloated %d, dyn %d, lean %d IR lines\n", b, d, l
    printf "bloated sends LLVM %.1fx the IR of lean and %.1fx the IR of dyn\n", b / l, b / d
}'
echo

# --- 3. The cargo-llvm-lines table, with copies regrouped ------------------------------------
echo "== 3. app_bloated by function: IR lines and copies, generic arguments folded =="
python3 "$TOOLS/llvm_lines.py" --top 6 "$WORK/ir/app_bloated"/*.ll
ROWS="$(python3 "$TOOLS/llvm_lines.py" --ungrouped-rows 'kit_bloated::ingest<_>' "$WORK/ir/app_bloated"/*.ll)"
echo "rows cargo-llvm-lines 0.4.48 prints for kit_bloated::ingest under $MANGLING mangling: $ROWS, each with Copies = 1"
echo
echo "== 3b. app_lean by function, same table =="
python3 "$TOOLS/llvm_lines.py" --top 4 "$WORK/ir/app_lean"/*.ll
echo

# --- 4. What survives in the binary ----------------------------------------------------------
echo "== 4. Machine code in the release binaries =="
ACCEPT='(::ingest|::dispatch|::open_frame|::close_frame|::decode_dyn|Sink>::push)'
printf '%-9s %11s %12s %12s  %s\n' version text_bytes accept_fns accept_bytes "fits 32 KiB L1i"
for v in bloated dyn lean; do
    text="$(python3 "$TOOLS/text_bytes.py" --section "$BIN/app_$v")"
    read -r fns bytes <<<"$(python3 "$TOOLS/text_bytes.py" --sum "$ACCEPT" "$BIN/app_$v")"
    fits=no
    [ "$bytes" -le 32768 ] && fits=yes
    printf '%-9s %11s %12s %12s  %s\n' "$v" "$text" "$fns" "$bytes" "$fits"
done
for v in bloated dyn lean; do
    echo "app_$v, workspace functions by size:"
    python3 "$TOOLS/text_bytes.py" --only '^<?(kit_|app_|feedcore)' --top 4 "$BIN/app_$v"
done

# The bloated library again, with `#[inline(always)]` removed from one helper (hex_head) and
# nothing else changed: how much of the growth is forced inlining, not monomorphization?
quiet "$WORK/plain.log" env CARGO_TARGET_DIR="$WORK/target_plain" \
    cargo build --release --offline --locked -p apps --bin app_bloated --features plain_helpers
PLAIN="$WORK/target_plain/release/app_bloated"
read -r plain_fns plain_bytes <<<"$(python3 "$TOOLS/text_bytes.py" --sum 'kit_bloated::ingest' "$PLAIN")"
read -r forced_fns forced_bytes <<<"$(python3 "$TOOLS/text_bytes.py" --sum 'kit_bloated::ingest' "$BIN/app_bloated")"
echo "kit_bloated::ingest with #[inline(always)] on hex_head:    $forced_fns copies, $forced_bytes bytes"
echo "kit_bloated::ingest without it, nothing else changed: $plain_fns copies, $plain_bytes bytes"
echo "text_bytes without it: $(python3 "$TOOLS/text_bytes.py" --section "$PLAIN")"
{
    echo "unrecorded: host L1i bytes, performance cores: $(sysctl -n hw.perflevel0.l1icachesize 2>/dev/null || echo unknown)"
    echo "unrecorded: host L1i bytes, efficiency cores: $(sysctl -n hw.perflevel1.l1icachesize 2>/dev/null || echo unknown)"
} >&2
echo

# --- 5. Inlining decisions, from LLVM itself -------------------------------------------------
# A second build of the dyn and lean applications with remarks on, in its own target directory
# so the binaries measured above are untouched. Costs and thresholds are stripped by remarks.py.
quiet "$WORK/remarks.log" env CARGO_TARGET_DIR="$WORK/target_remarks" RUSTFLAGS="-Cremark=inline -Cdebuginfo=1" \
    cargo build --release --offline --locked -p apps --bin app_dyn --bin app_lean
remarks() { python3 "$TOOLS/remarks.py" "$WORK/remarks.log" "$1"; }
echo "== 5. LLVM inlining remarks (-Cremark=inline), counted =="
echo "lean: decode or push inlined into kit_lean::ingest: $(remarks 'success\): <feedcore::\w+ as feedcore::(Decoder>::decode|Sink>::push) inlined into kit_lean::ingest<_>$')"
echo "lean: kit_lean::ingest inlined into app_lean::dispatch: $(remarks 'success\): kit_lean::ingest<_> inlined into app_lean::dispatch<_>$')"
echo "lean: kit_lean::open_frame not inlined, definition unavailable: $(remarks 'missed\): kit_lean::open_frame will not be inlined into kit_lean::ingest<_> because its definition is unavailable')"
echo "dyn:  decode_dyn or push inlined into kit_dyn::ingest: $(remarks '(decode_dyn|Sink>::push) inlined into kit_dyn::ingest$')"
echo "dyn:  kit_dyn::ingest not inlined into app_dyn::dispatch, definition unavailable: $(remarks 'missed\): kit_dyn::ingest will not be inlined into app_dyn::dispatch<_> because its definition is unavailable')"
echo

# --- 6. Build time ---------------------------------------------------------------------------
echo "== 6. cargo build --release --timings, cold =="
python3 "$TOOLS/timings.py" "$CARGO_TARGET_DIR/cargo-timings/cargo-timing.html"
