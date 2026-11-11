#!/usr/bin/env bash
# requires: macos
# Lab: clang-tidy, the Clang Static Analyzer, a toy coverage-guided fuzzer, and libFuzzer
# against one length-prefixed record parser with a heap overflow.
# macOS only: the recording depends on Apple clang (checker count, exit 134 from abort_on_error),
# and the pinned Linux lab image has no clang. The harness cannot gate the tools below, so the
# script probes each one and fails with a named reason instead of recording wrong output:
#   - CXX (default c++): a clang that supports --analyze, -fsanitize=address, trace-pc-guard
#   - CLANG_TIDY (default clang-tidy, else Homebrew LLVM): clang-tidy 22 or later
#   - FUZZ_CXX: a clang++ that links -fsanitize=fuzzer; Apple clang does not ship
#     libclang_rt.fuzzer_osx.a, so the script falls back to Homebrew LLVM
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C

CXX="${CXX:-c++}"
CXXFLAGS=(-std=c++17 -g -O1)

die() {
    echo "run.sh: $*" >&2
    exit 1
}

brew_llvm_bin() {
    if command -v brew >/dev/null 2>&1; then
        local prefix
        prefix="$(brew --prefix llvm 2>/dev/null || true)"
        if [[ -n "${prefix}" && -d "${prefix}/bin" ]]; then
            echo "${prefix}/bin"
        fi
    fi
    return 0
}

links_libfuzzer() {
    printf '#include <cstddef>\n#include <cstdint>\nextern "C" int LLVMFuzzerTestOneInput(const uint8_t *, size_t) { return 0; }\n' \
        >"${WORK}/probe.cpp"
    "$1" -fsanitize=fuzzer "${WORK}/probe.cpp" -o "${WORK}/probe" >/dev/null 2>&1
}

# The analyzer, ASan, and trace-pc-guard parts need a clang driver.
cxx_version="$("${CXX}" --version 2>/dev/null || true)"
[[ "${cxx_version}" == *clang* ]] || die "CXX (${CXX}) is not clang; set CXX to a clang++"

# Resolve clang-tidy.
if [[ -z "${CLANG_TIDY:-}" ]]; then
    if command -v clang-tidy >/dev/null 2>&1; then
        CLANG_TIDY=clang-tidy
    elif [[ -n "$(brew_llvm_bin)" && -x "$(brew_llvm_bin)/clang-tidy" ]]; then
        CLANG_TIDY="$(brew_llvm_bin)/clang-tidy"
    else
        die "clang-tidy not found; set CLANG_TIDY or install LLVM (brew install llvm)"
    fi
fi
# CustomChecks and the defaults this lab records arrived in clang-tidy 22.
tidy_major="$("${CLANG_TIDY}" --version 2>/dev/null | sed -n -E 's/.*LLVM version ([0-9]+)\..*/\1/p' | head -n 1)"
[[ "${tidy_major}" =~ ^[0-9]+$ ]] || die "cannot read an LLVM version from ${CLANG_TIDY} --version"
((tidy_major >= 22)) || die "clang-tidy ${tidy_major} is too old; this lab needs clang-tidy 22 or later"

# Resolve a libFuzzer-capable compiler.
if [[ -z "${FUZZ_CXX:-}" ]]; then
    if links_libfuzzer "${CXX}"; then
        FUZZ_CXX="${CXX}"
    elif [[ -n "$(brew_llvm_bin)" ]] && links_libfuzzer "$(brew_llvm_bin)/clang++"; then
        FUZZ_CXX="$(brew_llvm_bin)/clang++"
    else
        die "no compiler links -fsanitize=fuzzer; set FUZZ_CXX to an LLVM clang++ (Apple clang lacks libFuzzer)"
    fi
elif ! links_libfuzzer "${FUZZ_CXX}"; then
    die "FUZZ_CXX (${FUZZ_CXX}) does not link -fsanitize=fuzzer; point it at an LLVM clang++"
fi

# Print diagnostics as file:line:col: [check] message, without directories.
diag_lines() {
    sed -n -E 's#^([^:]*/)?([^/:]+):([0-9]+):([0-9]+): (warning|error): (.*) \[([^]]+)\]$#\2:\3:\4: [\7] \6#p' |
        sort -t: -k1,1 -k2,2n
}

echo "== Part 1: clang-tidy"
cp "${SCRIPT_DIR}/tidy_target.cpp" "${SCRIPT_DIR}/record.cpp" "${SCRIPT_DIR}/record.h" "${WORK}/"
cd "${WORK}"
default_checks="$("${CLANG_TIDY}" --dump-config tidy_target.cpp -- -std=c++17 | sed -n 's/^Checks: *//p')"
echo "built-in default Checks: ${default_checks}"
echo "checks available with -checks='*': $("${CLANG_TIDY}" --list-checks -checks='*' | grep -c '^    '), of which clang-analyzer-*: $("${CLANG_TIDY}" --list-checks -checks='clang-analyzer-*' | grep -c '^    ')"
set +e
"${CLANG_TIDY}" tidy_target.cpp -- -std=c++17 >"${WORK}/tidy_default.out" 2>"${WORK}/tidy_default.err"
status=$?
set -e
echo "run with no configuration: exit ${status}, first stderr line: $(head -n 1 "${WORK}/tidy_default.err")"
tidy_lab=("${CLANG_TIDY}" --config-file="${SCRIPT_DIR}/lab.clang-tidy" --experimental-custom-checks)
set +e
"${tidy_lab[@]}" tidy_target.cpp record.cpp -- -std=c++17 >"${WORK}/tidy_lab.out" 2>/dev/null
status=$?
set -e
echo "run with lab.clang-tidy: exit ${status}"
diag_lines <"${WORK}/tidy_lab.out"
set +e
"${tidy_lab[@]}" --warnings-as-errors='bugprone-*' tidy_target.cpp record.cpp -- -std=c++17 >"${WORK}/tidy_wae.out" 2>/dev/null
status=$?
set -e
echo "same run with --warnings-as-errors='bugprone-*': exit ${status}, errors: $(grep -c ': error: ' "${WORK}/tidy_wae.out")"

echo
echo "== Part 2: Clang Static Analyzer"
cd "${SCRIPT_DIR}"
echo "stable checkers listed by -analyzer-checker-help: $("${CXX}" -cc1 -analyzer-checker-help 2>&1 | grep -c -E '^  [a-z]')"
printf 'void f(void) {}\n' >"${WORK}/empty.c"
echo "effective analyzer configuration (debug.ConfigDumper):"
"${CXX}" -cc1 -analyze -analyzer-checker=debug.ConfigDumper "${WORK}/empty.c" 2>&1 |
    grep -E '^(mode|ipa|max-nodes|max-inlinable-size|suppress-null-return-paths|crosscheck-with-z3|exploration_strategy) =' |
    sed 's/^/  /'
# A file that does not compile must fail here, not count as zero warnings.
"${CXX}" "${CXXFLAGS[@]}" -Wall -Wextra -fsyntax-only analyzer_target.cpp >"${WORK}/wall.out" 2>&1 ||
    die "analyzer_target.cpp does not compile: $(grep -m 1 ': error: ' "${WORK}/wall.out")"
echo "-Wall -Wextra warnings: $(grep -c ': warning: ' "${WORK}/wall.out" || true)"
# -analyzer-output=text adds the path notes to the same warnings, so one run feeds both listings.
analyze=("${CXX}" -std=c++17 --analyze -o /dev/null -Xclang -analyzer-output=text)
path_notes() {
    sed -n -E 's#^analyzer_target\.cpp:([0-9]+):([0-9]+): note: (.*)$#  \1:\2 \3#p'
}
"${analyze[@]}" analyzer_target.cpp >"${WORK}/analyze_default.out" 2>&1 ||
    die "--analyze failed: $(head -n 1 "${WORK}/analyze_default.out")"
echo "--analyze with default options:"
diag_lines <"${WORK}/analyze_default.out"
echo "path notes with default options (the leak, then the division by zero):"
path_notes <"${WORK}/analyze_default.out"
"${analyze[@]}" -Xclang -analyzer-config -Xclang suppress-null-return-paths=false analyzer_target.cpp \
    >"${WORK}/analyze_nosuppress.out" 2>&1 || die "--analyze failed: $(head -n 1 "${WORK}/analyze_nosuppress.out")"
echo "--analyze with suppress-null-return-paths=false:"
diag_lines <"${WORK}/analyze_nosuppress.out"
echo "path notes for the null dereference:"
path_notes <"${WORK}/analyze_nosuppress.out" | sed -n '1,/dereference of a null pointer/p'

echo
echo "== Part 3: toy coverage-guided fuzzer (SanitizerCoverage trace-pc-guard)"
cd "${WORK}"
"${CXX}" "${CXXFLAGS[@]}" -fsanitize=address -fsanitize-coverage=trace-pc-guard -c "${SCRIPT_DIR}/record.cpp" -o record_cov.o
"${CXX}" "${CXXFLAGS[@]}" -fsanitize=address -I "${SCRIPT_DIR}" -c "${SCRIPT_DIR}/toyfuzz.cpp" -o toyfuzz.o
"${CXX}" -fsanitize=address record_cov.o toyfuzz.o -o toyfuzz
# The guard call is unconditional: count the call sites, then count calls that arrive with a zeroed guard.
call_sites="$("${CXX}" "${CXXFLAGS[@]}" -fsanitize=address -fsanitize-coverage=trace-pc-guard -S "${SCRIPT_DIR}/record.cpp" -o - |
    grep -c -E '^[[:space:]]+(bl|callq?)[[:space:]]+_*__sanitizer_cov_trace_pc_guard$' || true)"
echo "record.cpp: call sites of __sanitizer_cov_trace_pc_guard in the assembly: ${call_sites}"
"${CXX}" "${CXXFLAGS[@]}" -fsanitize=address -I "${SCRIPT_DIR}" -c "${SCRIPT_DIR}/guard_probe.cpp" -o guard_probe.o
"${CXX}" -fsanitize=address record_cov.o guard_probe.o -o guard_probe
./guard_probe
./toyfuzz blind 1000000 "${WORK}/toy_blind.bin" 2>"${WORK}/toy_blind.err" || die "blind toy run crashed"
set +e
./toyfuzz guided 1000000 "${WORK}/toy_crash.bin" 2>"${WORK}/toy_guided.err"
status=$?
set -e
[[ ${status} -ne 0 ]] || die "guided toy run did not crash"
echo "guided: process exit ${status}"
echo "guided: $(grep -o 'AddressSanitizer: [a-z-]*' "${WORK}/toy_guided.err" | head -n 1)"
python3 - "${WORK}/toy_crash.bin" <<'PY'
import sys
data = open(sys.argv[1], "rb").read()
print(f"guided: crash input starts with {data[:4].decode('latin-1')!r}, declared length {data[4]}, "
      f"payload bytes present {len(data) - 5}, destination capacity 16")
PY

echo
echo "== Part 4: libFuzzer"
"${FUZZ_CXX}" "${CXXFLAGS[@]}" -fsanitize=fuzzer,address -I "${SCRIPT_DIR}" \
    "${SCRIPT_DIR}/fuzz_record.cpp" "${SCRIPT_DIR}/record.cpp" -o fuzz_record
"${FUZZ_CXX}" "${CXXFLAGS[@]}" -fsanitize=fuzzer,address -DRECORD_FIXED -I "${SCRIPT_DIR}" \
    "${SCRIPT_DIR}/fuzz_record.cpp" "${SCRIPT_DIR}/record.cpp" -o fuzz_record_fixed
mkdir -p artifacts corpus
set +e
./fuzz_record -seed=1 -runs=2000000 -print_final_stats=1 -artifact_prefix=artifacts/ corpus >"${WORK}/lf.out" 2>"${WORK}/lf.err"
status=$?
set -e
crash="$(find artifacts -name 'crash-*' -type f | head -n 1)"
[[ -n "${crash}" ]] || die "libFuzzer found no crash in 2000000 runs"
echo "fuzz run (-seed=1 -runs=2000000): exit ${status}, crash artifact written: yes"
echo "executions until the crash: $(sed -n -E 's/^stat::number_of_executed_units: *//p' "${WORK}/lf.err")"
# Executions until the crash for seeds 1-10, then min, median, max.
seed_sweep() {
    local label="$1"
    shift
    local counts=() seed
    for seed in 1 2 3 4 5 6 7 8 9 10; do
        rm -rf seeds && mkdir seeds
        ./fuzz_record -seed="${seed}" -runs=2000000 -print_final_stats=1 -artifact_prefix=seeds/ "$@" \
            >/dev/null 2>"${WORK}/seed.err" || true
        [[ -n "$(find seeds -name 'crash-*' -type f)" ]] || die "seed ${seed} found no crash"
        counts+=("$(sed -n -E 's/^stat::number_of_executed_units: *//p' "${WORK}/seed.err")")
    done
    echo "executions until the crash, ${label}, seeds 1-10: ${counts[*]}"
    python3 -c 'import statistics, sys; v = sorted(map(int, sys.argv[1:])); print(f"  min {v[0]}, median {statistics.median(v):.0f}, max {v[-1]}, max/min {v[-1] / v[0]:.1f}x")' "${counts[@]}"
}
seed_sweep "default flags"
seed_sweep "-use_value_profile=1" -use_value_profile=1
echo "report: $(grep -o 'AddressSanitizer: [a-z-]*' "${WORK}/lf.err" | head -n 1), $(grep -o -E '^(READ|WRITE)' "${WORK}/lf.err" | head -n 1)"
grep -o 'SUMMARY: .*' "${WORK}/lf.err" | sed -E 's/ in parse_record.*/ in parse_record/'

./fuzz_record -seed=1 -minimize_crash=1 -runs=100000 -exact_artifact_path="${WORK}/min.bin" "${crash}" >/dev/null 2>&1 || true
python3 - "${WORK}/min.bin" <<'PY'
import sys
data = open(sys.argv[1], "rb").read()
print(f"minimized crash: {len(data)} bytes, magic {data[:4].decode('latin-1')!r}, declared length {data[4]}")
PY
set +e
./fuzz_record "${WORK}/min.bin" >/dev/null 2>"${WORK}/replay.err"
status=$?
set -e
echo "replay minimized input on the buggy build: exit ${status}, $(grep -o 'AddressSanitizer: [a-z-]*' "${WORK}/replay.err" | head -n 1)"
set +e
./fuzz_record_fixed "${WORK}/min.bin" >/dev/null 2>"${WORK}/replay_fixed.err"
status=$?
set -e
echo "replay minimized input on the RECORD_FIXED build: exit ${status}, ASan reports: $(grep -c 'ERROR: AddressSanitizer' "${WORK}/replay_fixed.err" || true)"
set +e
./fuzz_record_fixed -seed=1 -runs=2000000 corpus >/dev/null 2>"${WORK}/lf_fixed.err"
status=$?
set -e
echo "fuzz the RECORD_FIXED build (-seed=1 -runs=2000000, same corpus): exit ${status}, crash: $(grep -c 'ERROR: AddressSanitizer' "${WORK}/lf_fixed.err" || true)"
