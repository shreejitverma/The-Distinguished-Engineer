#!/usr/bin/env bash
# requires: macos
# Output is specific to macOS: BSD xargs exit status 1 and the 163-argument -s 4096 split.
# Needs fd (sharkdp/fd) on PATH; the harness cannot declare it, so it is checked below.
set -euo pipefail

command -v fd >/dev/null || { echo "lab needs fd on PATH (brew install fd)" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

cd "${WORK_DIR}"
export LC_ALL=C

# Compile C helpers
"${CC:-cc}" -O2 -o arg_counter "${SCRIPT_DIR}/arg_counter.c"
"${CC:-cc}" -O2 -o worker "${SCRIPT_DIR}/worker.c"
"${CC:-cc}" -O2 -o slow_worker "${SCRIPT_DIR}/slow_worker.c"

echo "=== Part 1: Filename Hazard Handling and Delimiter Safety ==="

mkdir -p hazard_test
touch "hazard_test/clean_sample.txt"
touch "hazard_test/file with space.txt"
touch "hazard_test/file	with	tab.txt"
touch "hazard_test/file
with
newline.txt"
touch "hazard_test/quote'single.txt"
touch 'hazard_test/quote"double.txt'
touch "hazard_test/-rf_leading_dash.txt"

# 1. Traditional whitespace-delimited xargs (fails on quotes or spaces)
set +e
raw_out=$(find hazard_test -type f | xargs -n 1 printf "[%s]\n" 2>&1)
raw_rc=$?
set -e
if [ "${raw_rc}" -ne 0 ] && echo "${raw_out}" | grep -q "unterminated quote"; then
    echo "Raw xargs without -0: FAILED (aborted on unescaped quote in filename)"
else
    echo "Raw xargs without -0: no failure seen (rc=${raw_rc})"
fi

# 2. Safe find -print0 | xargs -0
rm -f worker_processed.log
find hazard_test -type f -print0 | xargs -0 -n 1 ./worker
safe_count=$(wc -l < worker_processed.log | tr -d ' ')
echo "find -print0 | xargs -0: PROCESSED ${safe_count}/7 hazardous files successfully"
rm -f worker_processed.log

# 3. Modern fd -0 | xargs -0
fd -u . hazard_test -0 | xargs -0 -n 1 ./worker
fd_count=$(wc -l < worker_processed.log | tr -d ' ')
echo "fd -0 | xargs -0: PROCESSED ${fd_count}/7 hazardous files successfully"
rm -f worker_processed.log

# 4. Modern fd -X batch execution
rm -f batch_counts.log
fd -u . hazard_test -X ./arg_counter
batch_execs=$(wc -l < batch_counts.log | tr -d ' ')
batch_args=$(tr '\n' '+' < batch_counts.log | sed 's/+$//')
echo "fd -X batch dispatch: received ${batch_args} files in ${batch_execs} execution"
rm -f batch_counts.log

# 5. fd prefixes ./ to relative paths handed to a command, so a leading-dash name is never an option
mkdir -p prefix_test
touch -- "prefix_test/-rf.txt" "prefix_test/ok.txt"
prefix_default=$(cd prefix_test && fd -u . -X printf '%s\n' | sort | tr '\n' ' ' | sed 's/ $//')
echo "fd -X from the current directory: arguments are ${prefix_default}"
prefix_strip=$(cd prefix_test && fd -u --strip-cwd-prefix=always . -X printf '%s\n' | sort | tr '\n' ' ' | sed 's/ $//')
echo "fd -X --strip-cwd-prefix=always: arguments are ${prefix_strip}"

echo ""
echo "=== Part 2: Argument Batching and Kernel Limits ==="

arg_max=$(getconf ARG_MAX)
echo "System ARG_MAX: ${arg_max} bytes"

mkdir -p batch_test
for i in $(seq -f "%04g" 0 999); do
    touch "batch_test/item_${i}.dat"
done

# Test find -exec ... ; (1 process per file)
rm -f batch_counts.log
find batch_test -name "item_*.dat" -exec ./arg_counter {} \;
exec_semi_invocations=$(wc -l < batch_counts.log | tr -d ' ')
echo "find -exec ... ; invocations: ${exec_semi_invocations} (files per process: $((1000 / exec_semi_invocations)))"

# Test find -exec ... + (batched up to ARG_MAX)
rm -f batch_counts.log
find batch_test -name "item_*.dat" -exec ./arg_counter {} +
exec_plus_invocations=$(wc -l < batch_counts.log | tr -d ' ')
exec_plus_args=$(head -n 1 batch_counts.log | tr -d ' ')
echo "find -exec ... + invocations: ${exec_plus_invocations} (batched ${exec_plus_args} files into ${exec_plus_invocations} execve)"

# Test xargs -n 250 (argument count batching)
rm -f batch_counts.log
find batch_test -name "item_*.dat" -print0 | xargs -0 -n 250 ./arg_counter
xargs_n_invocations=$(wc -l < batch_counts.log | tr -d ' ')
xargs_n_summary=$(tr '\n' ' ' < batch_counts.log | sed 's/ $//')
echo "xargs -n 250 invocations: ${xargs_n_invocations} (chunks: ${xargs_n_summary})"

# Test xargs -s 4096 (size-bounded batching)
rm -f batch_counts.log
find batch_test -name "item_*.dat" -print0 | xargs -0 -s 4096 ./arg_counter
xargs_s_invocations=$(wc -l < batch_counts.log | tr -d ' ')
xargs_s_summary=$(tr '\n' ' ' < batch_counts.log | sed 's/ $//')
echo "xargs -s 4096 invocations: ${xargs_s_invocations} (chunks: ${xargs_s_summary})"

# Test fd -X --batch-size 250
rm -f batch_counts.log
fd -g "item_*.dat" batch_test --batch-size 250 -X ./arg_counter
fd_batch_invocations=$(wc -l < batch_counts.log | tr -d ' ')
fd_batch_summary=$(tr '\n' ' ' < batch_counts.log | sed 's/ $//')
echo "fd -X --batch-size 250 invocations: ${fd_batch_invocations} (chunks: ${fd_batch_summary})"

echo ""
echo "=== Part 3: Concurrency and Exit Code Discipline ==="

# Concurrency with -P 4
rm -f worker_processed.log concurrency.log
printf "%s\n" task1 task2 task3 task4 task5 task6 task7 task8 | xargs -n 1 -P 4 ./worker
parallel_processed=$(wc -l < worker_processed.log | tr -d ' ')
echo "xargs -P 4 concurrent pool: completed ${parallel_processed}/8 tasks"
rm -f worker_processed.log

# Peak overlap of 8 slow workers under -P 4: replay the S/E event log in order
printf "%s\n" 1 2 3 4 5 6 7 8 | xargs -n 1 -P 4 ./slow_worker 4 >/dev/null
peak=$(awk '$1=="S"{n++; if(n>m)m=n} $1=="E"{n--} END{print m}' concurrency.log)
echo "xargs -P 4 peak concurrent workers: ${peak}"
rm -f concurrency.log

# Fast abort on exit 255
rm -f worker_processed.log
set +e
abort_stderr=$(printf "%s\n" task1 abort task2 task3 task4 | xargs -n 1 ./worker 2>&1)
abort_rc=$?
set -e
abort_tasks=$(tr '\n' ' ' < worker_processed.log 2>/dev/null | sed 's/ $//' || echo "")
aborted_remaining=0
for t in task2 task3 task4; do
    grep -qx "${t}" worker_processed.log || aborted_remaining=$((aborted_remaining + 1))
done
echo "xargs exit 255 abort: exit_code=${abort_rc}, executed=[${abort_tasks}], aborted_remaining=${aborted_remaining}"
rm -f worker_processed.log

# Non-zero exit code propagation
set +e
printf "%s\n" task1 fail task2 | xargs -n 1 ./worker >/dev/null 2>&1
fail_rc=$?
set -e
if [ "${fail_rc}" -ne 0 ]; then
    echo "xargs error propagation: exit_code=${fail_rc} (non-zero error detected and propagated)"
fi
