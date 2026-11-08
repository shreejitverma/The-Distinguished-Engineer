#!/usr/bin/env bash
set -euo pipefail

# Phase 2 uses $'\u0043' (Bash 4.2 or later); macOS /bin/bash is 3.2, so put a newer bash first on PATH.
if (( BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 2) )); then
    echo "run.sh needs Bash 4.2 or later, found ${BASH_VERSION}" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

cd "${WORK_DIR}"
export LC_ALL=C

echo "=== PHASE 1: STANDALONE EXPANSION ENGINE TRACE ==="
"${CC:-cc}" -std=c99 -Wall -Wextra -pedantic "${SCRIPT_DIR}/expansion_engine.c" -o "${WORK_DIR}/expansion_engine"
# The engine globs the real directory it runs in, so give it two known files.
mkdir "${WORK_DIR}/engine_fs"
touch "${WORK_DIR}/engine_fs/data_a.tmp" "${WORK_DIR}/engine_fs/data_b.tmp"
(cd "${WORK_DIR}/engine_fs" && "${WORK_DIR}/expansion_engine")

echo ""
echo "=== PHASE 2: LIVE SHELL PARSING & EXPANSION SUITE ==="

# Helper function to count and print arguments deterministically
count_args() {
    printf "ArgCount=%d\n" "$#"
    local idx=1
    for arg in "$@"; do
        printf "  arg[%d]=<%s>\n" "$idx" "$arg"
        idx=$((idx + 1))
    done
}

echo "--- 1. Brace Expansion Precedence Over Parameter Expansion ---"
N=4
printf "Direct {1..\$N}: "
count_args {1..$N}

printf "Eval-expanded {1..\$N}: "
eval count_args {1..$N}

printf "Brace multiplication a{b,c}{d,e}: "
count_args a{b,c}{d,e}

printf "Quoted braces \"{a,b}\": "
count_args "{a,b}"

echo ""
echo "--- 2. Left-to-Right Single Pass Evaluation ---"
# Arithmetic expansion containing command substitution and parameter
BASE=10
result=$(( BASE + $(echo 15) ))
printf "Compound expansion result: %d\n" "$result"

# Each $((i+=1)) mutates i as it is expanded, so the printed values expose the evaluation order.
i=0
printf "Arithmetic order: %s %s\n" "$((i+=1))" "$((i+=1))"
i=0
printf "Arithmetic then command substitution then arithmetic: %s %s %s\n" "$((i+=1))" "$(echo "$i")" "$((i+=1))"

echo ""
echo "--- 3. Parameter Expansion Precedence Over Field Splitting ---"
SPACED="alpha bravo charlie"
printf "Unquoted \$SPACED:\n"
count_args $SPACED

printf "Quoted \"\$SPACED\":\n"
count_args "$SPACED"

echo ""
echo "--- 4. Field Splitting Precedence Over Pathname Expansion ---"
# Create test files
touch test_file_1.tmp test_file_2.tmp
touch "test file with space.tmp"

PATTERN="test_file_*.tmp"
printf "Unquoted pattern variable \$PATTERN:\n"
count_args $PATTERN

printf "Quoted pattern variable \"\$PATTERN\":\n"
count_args "$PATTERN"

printf "Pathname expansion on file with spaces (*space*.tmp):\n"
for f in *space*.tmp; do
    count_args "$f"
done

echo ""
echo "--- 5. IFS Boundary Hazards: Whitespace vs Non-Whitespace ---"
printf "Default IFS whitespace splitting ('  a   b   c  '):\n"
WS_VAR="  a   b   c  "
count_args $WS_VAR

printf "Colon non-whitespace IFS ('a::b:::c'):\n"
(
    IFS=":"
    COLON_VAR="a::b:::c"
    count_args $COLON_VAR
)

printf "Mixed IFS ' :' ('  a : : b : c  '):\n"
(
    IFS=" :"
    MIXED_VAR="  a : : b : c  "
    count_args $MIXED_VAR
)

printf "Trailing non-whitespace IFS ('x:y:'):\n"
(
    IFS=":"
    TRAIL_VAR="x:y:"
    count_args $TRAIL_VAR
)

printf "Repeated trailing non-whitespace IFS ('x:y::'):\n"
(
    IFS=":"
    TRAIL2_VAR="x:y::"
    count_args $TRAIL2_VAR
)

printf "Leading non-whitespace IFS (':x'):\n"
(
    IFS=":"
    LEAD_VAR=":x"
    count_args $LEAD_VAR
)

printf "Null IFS (IFS='') with spaces ('a b c'):\n"
(
    IFS=""
    NULL_VAR="a b c"
    count_args $NULL_VAR
)

echo ""
echo "--- 6. Command Substitution Trailing Newline Stripping ---"
raw_newlines=$(printf "line1\nline2\n\n\n")
printf "Default command substitution length: %d\n" "${#raw_newlines}"

# Trailing guard character trick
guarded_raw=$(printf "line1\nline2\n\n\n"; printf "x")
preserved_newlines="${guarded_raw%x}"
printf "Guarded command substitution length: %d\n" "${#preserved_newlines}"

echo ""
echo "--- 7. Quoting Modes & Expanded Quote Invariant ---"
# Single quote literal
printf "Single quote literal: %s\n" '$VAR\n\t'

# Double quote parameter interpolation
VAR="world"
printf "Double quote evaluation: %s\n" "hello $VAR"

# POSIX.1-2024 Dollar Single Quote ($'...')
printf "Dollar single quote escapes: %s\n" $'tab:\t hex:\x41 octal:\102 unicode:\u0043'

# Quotes produced by expansion resist quote removal
INJECTED_QUOTES='"token1 token2"'
printf "Passing unquoted variable with embedded quotes (\$INJECTED_QUOTES):\n"
count_args $INJECTED_QUOTES

echo ""
echo "--- 8. Tokenization & Alias Chaining Semantics ---"
shopt -s expand_aliases
alias cmd_no_space="echo WRAPPER"
alias cmd_with_space="echo WRAPPER "
alias target="PAYLOAD"

printf "Alias without trailing space: "
cmd_no_space target

printf "Alias with trailing space: "
cmd_with_space target
