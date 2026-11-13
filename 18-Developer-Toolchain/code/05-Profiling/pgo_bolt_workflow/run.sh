#!/usr/bin/env bash
# PGO, AutoFDO, and BOLT on one workload: build, profile, rebuild, rewrite, compare.
# requires: linux, pmu
#
# Needs: Clang, llvm-profdata, llvm-profgen, llvm-readelf, llvm-objdump, llvm-nm,
# perf with branch-stack sampling (Intel LBR, AMD BRS or LBRv2, Arm BRBE), and
# llvm-bolt plus perf2bolt. Point LLVM_BIN at one LLVM bin directory so that the
# compiler, llvm-profdata, and BOLT come from the same release; mixing releases
# breaks the indexed profile format.
#
# stdout carries only facts that do not change between runs; timings and the
# BOLT dyno-stats go to stderr, because they are sampled or measured.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C

TRAIN_N=2000000          # instrumented training run (exact counts)
PERF_N=100000000         # sampled runs: about a second of work on a current core
EVAL_N=50000000          # measured runs
PERF_EVENT="${PERF_EVENT:-cycles:u}"

die() { echo "error: $*" >&2; exit 1; }

find_tool() {
    local name="$1"
    if [ -n "${LLVM_BIN:-}" ] && [ -x "${LLVM_BIN}/${name}" ]; then
        echo "${LLVM_BIN}/${name}"
    elif command -v "${name}" >/dev/null 2>&1; then
        command -v "${name}"
    else
        return 1
    fi
}

# Clang only: GCC has -fprofile-use and -fauto-profile, but not llvm-profgen,
# section-prefix placement, or -fsplit-machine-functions as used below.
if [ -z "${CXX:-}" ]; then
    if [ -n "${LLVM_BIN:-}" ] && [ -x "${LLVM_BIN}/clang++" ]; then
        CXX="${LLVM_BIN}/clang++"
    elif c++ --version 2>/dev/null | grep -qi clang; then
        CXX=c++
    else
        CXX=clang++
    fi
fi
"${CXX}" --version 2>/dev/null | grep -qi clang || die "CXX=${CXX} is not Clang; run with CXX=clang++ or LLVM_BIN=<llvm>/bin"

PROFDATA="$(find_tool llvm-profdata)" || die "llvm-profdata not found"
PROFGEN="$(find_tool llvm-profgen)" || die "llvm-profgen not found"
READELF="$(find_tool llvm-readelf)" || die "llvm-readelf not found"
OBJDUMP="$(find_tool llvm-objdump)" || die "llvm-objdump not found"
NM="$(find_tool llvm-nm)" || die "llvm-nm not found"
BOLT="$(find_tool llvm-bolt)" || die "llvm-bolt not found"
PERF2BOLT="$(find_tool perf2bolt)" || die "perf2bolt not found"
command -v perf >/dev/null 2>&1 || die "perf not found"

SRC=("${SCRIPT_DIR}/workload.cpp" "${SCRIPT_DIR}/main.cpp")
BASE_FLAGS=(-O2 -std=c++17)
# BOLT input: keep static relocations (--emit-relocs, also spelled -q) so BOLT can
# run in relocation mode and move functions; a non-PIE link keeps the lab simple.
BOLT_LINK=(-no-pie "-Wl,--emit-relocs")

cd "${WORK}"

checksum() { "$@" | sed -n 's/^checksum=\([0-9a-f]*\) .*/\1/p'; }

# section_class <object> <function>: which .text.<prefix>.<function> holds it.
section_class() {
    local secs
    secs="$("${READELF}" -S -W "$1" | grep -o -E ' \.text\.[A-Za-z0-9_.]+' | sed 's/^ //')"
    if grep -qx ".text.hot.$2" <<<"${secs}"; then echo hot
    elif grep -qx ".text.unlikely.$2" <<<"${secs}"; then echo unlikely
    elif grep -qx ".text.$2" <<<"${secs}"; then echo plain
    else echo absent
    fi
}

# prefix_counts <object>: how many function sections carry each prefix.
prefix_counts() {
    local secs
    secs="$("${READELF}" -S -W "$1" | grep -o -E ' \.text\.[A-Za-z0-9_.]+' | sed 's/^ //')"
    printf 'hot=%s unlikely=%s split=%s\n' \
        "$(grep -c '^\.text\.hot\.' <<<"${secs}" || true)" \
        "$(grep -c '^\.text\.unlikely\.' <<<"${secs}" || true)" \
        "$(grep -c '^\.text\.split\.' <<<"${secs}" || true)"
}

# calls_direct <object>: does run_batch contain a direct call to handle_3?
calls_direct() {
    if "${OBJDUMP}" -d -r --disassemble-symbols=run_batch "$1" | grep -q -E 'R_[A-Z0-9_]+[[:space:]]+handle_3([^0-9_]|$)'; then
        echo yes
    else
        echo no
    fi
}

measure() {
    # Timing is noise-prone; it goes to stderr and never into the expected file.
    local label="$1"; shift
    perf stat -x, -r 5 -e "${PERF_EVENT},instructions:u,branch-misses:u,L1-icache-load-misses:u" \
        -o "${WORK}/stat-${label}.csv" -- "$@" >/dev/null 2>&1 || true
    echo "[measure] ${label}:" >&2
    sed -n '/^[0-9<]/p' "${WORK}/stat-${label}.csv" | cut -d, -f1,3 >&2 || true
}

echo "== 0. baseline: -O2, no profile"
"${CXX}" "${BASE_FLAGS[@]}" "${SRC[@]}" "${BOLT_LINK[@]}" -o base
BASE_SUM="$(checksum ./base eval "${EVAL_N}")"
echo "baseline eval checksum: ${BASE_SUM}"
"${CXX}" "${BASE_FLAGS[@]}" -ffunction-sections -c "${SCRIPT_DIR}/workload.cpp" -o base.o
echo "baseline sections: $(prefix_counts base.o)"
echo "baseline run_batch calls handle_3 directly: $(calls_direct base.o)"

echo
echo "== 1. instrumentation PGO: -fprofile-generate, train, merge, -fprofile-use"
mkdir -p prof
"${CXX}" "${BASE_FLAGS[@]}" -fprofile-generate="${WORK}/prof" "${SRC[@]}" -o instr
LLVM_PROFILE_FILE="${WORK}/prof/train-%p.profraw" ./instr train "${TRAIN_N}" >/dev/null
RAWS=(prof/*.profraw)
echo "raw profiles written: ${#RAWS[@]}"
"${PROFDATA}" merge -o pgo.profdata prof/*.profraw
"${PROFDATA}" show pgo.profdata | grep -E '^(Total functions|Maximum function count|Total count):'
"${PROFDATA}" show --topn=3 pgo.profdata | sed -n '/^Top 3/,$p'
echo "indirect-call targets of run_batch (top 2):"
"${PROFDATA}" show --function=run_batch --ic-targets pgo.profdata \
    | grep -E '^[[:space:]]*\[' | head -n 2 | sed 's/^[[:space:]]*/  /'
"${CXX}" "${BASE_FLAGS[@]}" -fprofile-use=pgo.profdata "${SRC[@]}" "${BOLT_LINK[@]}" -o pgo
PGO_SUM="$(checksum ./pgo eval "${EVAL_N}")"
[ "${PGO_SUM}" = "${BASE_SUM}" ] && echo "pgo eval checksum matches baseline: yes" || echo "pgo eval checksum matches baseline: NO"
"${CXX}" "${BASE_FLAGS[@]}" -ffunction-sections -fprofile-use=pgo.profdata -c "${SCRIPT_DIR}/workload.cpp" -o pgo.o
echo "pgo sections: $(prefix_counts pgo.o)"
for f in handle_3 run_batch report_bad dump_state; do
    echo "  ${f}: $(section_class pgo.o "${f}")"
done
echo "pgo run_batch calls handle_3 directly: $(calls_direct pgo.o)"
"${CXX}" "${BASE_FLAGS[@]}" -ffunction-sections -fprofile-use=pgo.profdata -fsplit-machine-functions \
    -c "${SCRIPT_DIR}/workload.cpp" -o mfs.o
echo "pgo + -fsplit-machine-functions sections: $(prefix_counts mfs.o)"
echo "  handle_3 split: $("${NM}" mfs.o | grep -q -E ' handle_3\.cold$' && echo yes || echo no)"
echo "  handle_0 split: $("${NM}" mfs.o | grep -q -E ' handle_0\.cold$' && echo yes || echo no)"

echo
echo "== 2. AutoFDO: sample a debug-info build with branch stacks, llvm-profgen, -fprofile-sample-use"
AFDO_FLAGS=(-gline-tables-only -fdebug-info-for-profiling -funique-internal-linkage-names)
"${CXX}" "${BASE_FLAGS[@]}" "${AFDO_FLAGS[@]}" "${SRC[@]}" -o afdo_train
perf record -q -e "${PERF_EVENT}" -j any,u -o afdo.data -- ./afdo_train train "${PERF_N}" >/dev/null 2>perf-afdo.err \
    || { cat perf-afdo.err >&2; die "perf record -j any,u failed: no branch-stack sampling here"; }
"${PROFGEN}" --binary=afdo_train --perfdata=afdo.data --output=afdo.prof 2>profgen.err \
    || { cat profgen.err >&2; die "llvm-profgen failed"; }
SAMPLE_FUNCS="$("${PROFDATA}" show --sample afdo.prof | sed -n 's/^Function: \([^:]*\):.*/\1/p')"
for f in run_batch handle_3 dump_state; do
    echo "  ${f} in sample profile: $(grep -qx "${f}" <<<"${SAMPLE_FUNCS}" && echo yes || echo no)"
done
"${CXX}" "${BASE_FLAGS[@]}" "${AFDO_FLAGS[@]}" -fprofile-sample-use=afdo.prof "${SRC[@]}" -o afdo
AFDO_SUM="$(checksum ./afdo eval "${EVAL_N}")"
[ "${AFDO_SUM}" = "${BASE_SUM}" ] && echo "autofdo eval checksum matches baseline: yes" || echo "autofdo eval checksum matches baseline: NO"
for acc in "" "-fprofile-sample-accurate"; do
    "${CXX}" "${BASE_FLAGS[@]}" "${AFDO_FLAGS[@]}" -ffunction-sections -fprofile-sample-use=afdo.prof ${acc} \
        -c "${SCRIPT_DIR}/workload.cpp" -o afdo.o
    echo "autofdo ${acc:-(default)}: dump_state=$(section_class afdo.o dump_state) handle_3=$(section_class afdo.o handle_3)"
done

echo
echo "== 3. BOLT on top of the PGO binary: perf -j any,u, perf2bolt, llvm-bolt"
echo "pgo binary keeps .rela.text: $("${READELF}" -S -W pgo | grep -q ' \.rela\.text ' && echo yes || echo no)"
perf record -q -e "${PERF_EVENT}" -j any,u -o bolt.data -- ./pgo train "${PERF_N}" >/dev/null 2>perf-bolt.err \
    || { cat perf-bolt.err >&2; die "perf record -j any,u failed"; }
"${PERF2BOLT}" -p bolt.data -o bolt.fdata pgo >perf2bolt.log 2>&1 || { cat perf2bolt.log >&2; die "perf2bolt failed"; }
"${BOLT}" pgo -o pgo.bolt -data=bolt.fdata \
    -reorder-blocks=ext-tsp -reorder-functions=cdsort \
    -split-functions -split-all-cold -split-eh -dyno-stats >bolt.log 2>&1 \
    || { cat bolt.log >&2; die "llvm-bolt failed"; }
grep -E 'taken (conditional )?branches|executed instructions' bolt.log >&2 || true
echo "llvm-bolt ran in relocation mode: $(grep -q 'BOLT-INFO: enabling relocation mode' bolt.log && echo yes || echo no)"
echo ".note.bolt_info present: $("${READELF}" -n pgo.bolt | grep -q 'BOLT revision' && echo yes || echo no)"
echo ".text.cold section present: $("${READELF}" -S -W pgo.bolt | grep -q ' \.text\.cold ' && echo yes || echo no)"
echo "cold fragments (<fn>.cold.0 symbols) present: $("${NM}" pgo.bolt | grep -q -E '\.cold\.0$' && echo yes || echo no)"
BOLT_SUM="$(checksum ./pgo.bolt eval "${EVAL_N}")"
[ "${BOLT_SUM}" = "${BASE_SUM}" ] && echo "pgo+bolt eval checksum matches baseline: yes" || echo "pgo+bolt eval checksum matches baseline: NO"

echo
echo "== 4. measurement (stderr only)"
measure base ./base eval "${EVAL_N}"
measure pgo ./pgo eval "${EVAL_N}"
measure autofdo ./afdo eval "${EVAL_N}"
measure pgo-bolt ./pgo.bolt eval "${EVAL_N}"
echo "measurements written to stderr: 4 variants"
