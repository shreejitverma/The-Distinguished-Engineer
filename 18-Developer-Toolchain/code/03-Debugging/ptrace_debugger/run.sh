#!/usr/bin/env bash
# Rebuild lab: a minimal ptrace debugger with a DWARF CFI (.eh_frame) backtrace.
# Builds target.c and minidbg.cpp, then drives minidbg through launch, breakpoint, register
# read and write, single-step, CFI unwind versus frame-pointer walk, a GDB cross-check,
# an unwind of a stripped copy, and attach to a running process.
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C

for tool in "${CC:-cc}" "${CXX:-c++}" gdb nm strip readelf objdump expand; do
    command -v "${tool}" >/dev/null || { echo "run.sh: missing tool: ${tool}" >&2; exit 1; }
done

cd "${WORK}"

# No -g: the unwinder must work from .eh_frame alone (requested explicitly with
# -fasynchronous-unwind-tables); it is SHF_ALLOC, so strip keeps it.
# -fno-omit-frame-pointer gives the frame-pointer walk its best case, so its failure below is
# the prologue problem, not missing frame pointers.
# -fno-optimize-sibling-calls keeps every call a real call, so each frame exists on the stack.
"${CC:-cc}" -O2 -fno-omit-frame-pointer -fasynchronous-unwind-tables -fno-optimize-sibling-calls \
    -o target "${SCRIPT_DIR}/target.c"
"${CXX:-c++}" -std=c++17 -O2 -Wall -Wextra -Werror -o minidbg "${SCRIPT_DIR}/minidbg.cpp"

echo "=== Target sections and raw CFI (readelf) ==="
# Flags column: A means allocated (loaded at run time), so strip keeps .eh_frame; .symtab has no A.
readelf -SW target | grep -E ' \.(eh_frame|eh_frame_hdr|symtab) ' | sed -E 's/^ *//; s/ +/ /g'
mid="$(nm target | awk '$3 == "middle_layer" { print $1 }')"
[[ -n "${mid}" ]] || { echo "run.sh: middle_layer not in nm output" >&2; exit 1; }
# The FDE covering middle_layer, exactly as readelf decodes it.
readelf --debug-dump=frames target | awk -v pc="pc=${mid}" 'index($0, pc) { p = 1 } p && /^$/ { exit } p'
# The code that FDE describes, so each CFA change can be matched to the instruction that causes it.
objdump -d --no-show-raw-insn target | awk '/<middle_layer>:$/ { p = 1 } p && /^$/ { exit } p' | expand -t 8
echo

./minidbg launch ./target | tee launch.txt

echo
echo "=== Cross-check: GDB at the same instruction ==="
# set startup-with-shell off: GDB execs the target directly, as minidbg does.
# stderr carries GDB's own diagnostics (thread_db, warnings); only the frame line is compared.
gdb -nx -q -batch \
    -ex 'set startup-with-shell off' \
    -ex 'break *leaf_compute' \
    -ex 'run' \
    -x "${SCRIPT_DIR}/gdb_frames.py" \
    ./target 2>/dev/null | grep '^GDB frames:' > gdb.txt || true
if [[ ! -s gdb.txt ]]; then
    echo "run.sh: GDB produced no backtrace" >&2
    exit 1
fi
cat gdb.txt
ours="$(sed -n 's/^CFI frames: //p' launch.txt)"
theirs="$(sed -n 's/^GDB frames: //p' gdb.txt)"
echo "minidbg CFI walk equals GDB backtrace: $([[ "${ours}" == "${theirs}" ]] && echo yes || echo no)"

echo
cp target target.stripped
strip --strip-all target.stripped
offset="$(nm target | awk '$3 == "leaf_compute" { print $1 }')"
[[ -n "${offset}" ]] || { echo "run.sh: leaf_compute not in nm output" >&2; exit 1; }
echo "readelf on the stripped copy: .symtab $(readelf -S target.stripped | grep -q ' .symtab ' && echo kept || echo removed), .eh_frame $(readelf -S target.stripped | grep -q ' .eh_frame ' && echo kept || echo removed)"
./minidbg unwind ./target.stripped "${offset}"

echo
./minidbg attach ./target
