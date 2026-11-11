#!/usr/bin/env bash
# Valgrind Memcheck, Helgrind, and DRD on one uninstrumented C++ build.
# requires: linux
#
# Needs valgrind (with its headers in /usr/include/valgrind), a C++ compiler,
# and python3. Every bug case runs on a binary built with no sanitizer flags, so
# each report comes from Valgrind's dynamic binary translation alone.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C

if ! command -v valgrind >/dev/null; then
    # A missing tool is a failure, never a skip: the lab proves nothing without it.
    echo "valgrind not found on PATH" >&2
    echo "On Linux install the valgrind package; for the harness container add valgrind to" >&2
    echo "both package lists in code/_harness/linux-labs.Dockerfile." >&2
    exit 1
fi
CXX="${CXX:-c++}"
"${CXX}" -std=c++17 -O0 -g -o "${WORK}/memcheck_bugs" "${SCRIPT_DIR}/memcheck_bugs.cpp" 2>/dev/null
"${CXX}" -std=c++17 -O0 -g -pthread -o "${WORK}/thread_bugs" "${SCRIPT_DIR}/thread_bugs.cpp" 2>/dev/null
cd "${WORK}"

digest() { python3 -I "${SCRIPT_DIR}/digest.py" "$@"; }

# vg <log> <valgrind args...> -- prints nothing, returns valgrind's exit status
vg() {
    local log="$1"
    shift
    local rc=0
    valgrind --log-file="${log}" "$@" >/dev/null 2>&1 || rc=$?
    return "${rc}"
}

echo "== Toolchain"
valgrind --version
echo

MEMCHECK_CASES=(heap_overflow use_after_free uninit_branch bit_precision padding_to_syscall
                mismatched_delete double_free custom_pool client_check)

echo "== Native runs (no Valgrind): exit status per case"
for c in "${MEMCHECK_CASES[@]}" leaks; do
    rc=0
    ./memcheck_bugs "${c}" >/dev/null 2>&1 || rc=$?
    printf '  %-20s exit=%s\n' "${c}" "${rc}"
done
echo "  probe: $(./memcheck_bugs probe)"
echo "  probe under valgrind: $(valgrind -q ./memcheck_bugs probe)"
echo

for c in "${MEMCHECK_CASES[@]}"; do
    echo "== Memcheck: ${c} (--track-origins=yes)"
    vg "${c}.log" --tool=memcheck --track-origins=yes --leak-check=no ./memcheck_bugs "${c}" || true
    digest "${c}.log"
    echo
done

echo "== Memcheck: uninit_branch without --track-origins (no origin line)"
vg plain.log --tool=memcheck --leak-check=no ./memcheck_bugs uninit_branch || true
digest plain.log
echo

echo "== Memcheck: leaks (--leak-check=full --show-leak-kinds=all)"
vg leaks.log --tool=memcheck --leak-check=full --show-leak-kinds=all ./memcheck_bugs leaks || true
digest leaks.log
echo

echo "== Memcheck: leaks with default leak options (--leak-check=summary)"
vg leaks_default.log --tool=memcheck ./memcheck_bugs leaks || true
digest leaks_default.log
echo

echo "== Memcheck as a CI gate: exit status"
rc=0
vg gate.log --tool=memcheck --error-exitcode=99 ./memcheck_bugs heap_overflow || rc=$?
echo "  heap_overflow with --error-exitcode=99: exit=${rc}"
rc=0
vg gate0.log --tool=memcheck ./memcheck_bugs heap_overflow || rc=$?
echo "  heap_overflow with the default --error-exitcode=0: exit=${rc}"
rc=0
vg gate_leak.log --tool=memcheck --error-exitcode=99 --leak-check=full ./memcheck_bugs leaks || rc=$?
echo "  leaks with --error-exitcode=99 --leak-check=full: exit=${rc}"
echo

echo "== Memcheck: generate a suppression, then apply it"
vg gen.log --tool=memcheck --gen-suppressions=all ./memcheck_bugs uninit_branch || true
sed -E 's/^==[0-9]+== ?//' gen.log | awk '/^\{$/{on=1} on{print} /^\}$/{on=0}' \
    | sed 's/<insert_a_suppression_name_here>/lab-uninit-branch/' > lab.supp
sed 's/^/  /' lab.supp
vg sup.log --tool=memcheck --suppressions=lab.supp ./memcheck_bugs uninit_branch || true
digest sup.log
echo

THREAD_CASES=(race locked lock_order atomic_publish unlock_unlocked atomic_counter)
for tool in helgrind drd; do
    for c in "${THREAD_CASES[@]}"; do
        echo "== ${tool}: ${c}"
        anon=()
        [[ "${c}" == race ]] && anon=(--anon-threads)
        vg "${tool}_${c}.log" --tool="${tool}" ./thread_bugs "${c}" || true
        digest "${anon[@]}" "${tool}_${c}.log"
        echo
    done
done

echo "== drd: contention (--exclusive-threshold=10)"
vg drd_contention.log --tool=drd --exclusive-threshold=10 ./thread_bugs contention || true
digest --anon-threads drd_contention.log
echo

echo "== helgrind: contention (no lock-hold-time check exists)"
vg hg_contention.log --tool=helgrind ./thread_bugs contention || true
digest hg_contention.log
