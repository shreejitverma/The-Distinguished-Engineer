#!/usr/bin/env bash
# trace_privileged.sh - the eBPF and perf path of the playbook, against the same workload.
#
# Not run by run.sh or by the lab harness: it needs root (or CAP_BPF, CAP_PERFMON, and
# CAP_SYS_PTRACE), a kernel with BTF, and bpftrace on PATH. perf is used when it is on PATH.
# Its output contains run-dependent numbers, so nothing here is compared against a recording.
#
# Usage: sudo bash trace_privileged.sh FAULT [SECONDS]     FAULT is runq, lock, oncpu, or none
set -euo pipefail

FAULT="${1:?usage: trace_privileged.sh FAULT [SECONDS], where FAULT is runq, lock, oncpu, or none}"
SECONDS_EACH="${2:-5}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v bpftrace >/dev/null || { echo "bpftrace is not on PATH" >&2; exit 1; }
[ -r /sys/kernel/btf/vmlinux ] || echo "warning: no /sys/kernel/btf/vmlinux; bpftrace may need kernel headers" >&2

WORK="$(mktemp -d)"
"${CC:-cc}" -O2 -g -fno-omit-frame-pointer -fno-optimize-sibling-calls -Wall -Wextra -pthread \
    -o "${WORK}/spike" "${SCRIPT_DIR}/spike.c"

# Drive the service through a FIFO so that it keeps running while the tracers attach.
mkfifo "${WORK}/ctl"
"${WORK}/spike" <"${WORK}/ctl" >"${WORK}/spike.out" &
SPIKE_PID=$!
exec 3>"${WORK}/ctl"
cleanup() {
    echo quit >&3 2>/dev/null || true
    exec 3>&-
    wait "${SPIKE_PID}" 2>/dev/null || true
    rm -rf "${WORK}"
}
trap cleanup EXIT

sleep 0.5
echo "fault ${FAULT}" >&3
sleep 0.5

# Ask the kernel which IDs it uses for the worker. Inside a container these differ from the
# IDs in /proc: sched tracepoint fields carry initial-namespace IDs, which is what the scripts
# in bpf/ compare against. A uprobe on this run's private binary can only fire in our worker,
# and the task struct holds the kernel's own IDs whatever the bpftrace version.
IDS="$(timeout 15 bpftrace -q -e "uprobe:${WORK}/spike:handle_request {
           \$task = (struct task_struct *)curtask; printf(\"%d %d\\n\", \$task->tgid, \$task->pid); exit(); }" \
       2>/dev/null | grep -E '^[0-9]+ [0-9]+$' | head -n 1 || true)"
[ -n "${IDS}" ] || { echo "bpftrace could not attach a uprobe to ${WORK}/spike" >&2; exit 1; }
KERNEL_TGID="${IDS% *}"
KERNEL_TID="${IDS#* }"
echo "spike: /proc PID ${SPIKE_PID}, kernel TGID ${KERNEL_TGID}, kernel TID of worker ${KERNEL_TID}, fault ${FAULT}, ${SECONDS_EACH} s per tool"

run_bt() {
    echo
    echo "=== $1 ==="
    shift
    # SIGINT makes bpftrace print its maps and exit 0; timeout then reports 124.
    timeout -s INT "${SECONDS_EACH}" bpftrace "$@" || [ $? -eq 124 ]
}

run_bt "run-queue latency of the worker (runqlat_tid.bt)" "${SCRIPT_DIR}/bpf/runqlat_tid.bt" "${KERNEL_TID}"
run_bt "blocked time of the worker by user stack (offcpu_tid.bt)" "${SCRIPT_DIR}/bpf/offcpu_tid.bt" "${KERNEL_TID}"
run_bt "who wakes the worker (wakers_tid.bt)" "${SCRIPT_DIR}/bpf/wakers_tid.bt" "${KERNEL_TID}"
run_bt "service time of handle_request (reqlat.bt)" "${SCRIPT_DIR}/bpf/reqlat.bt" "${WORK}/spike"
run_bt "on-CPU profile of the process (oncpu_pid.bt)" "${SCRIPT_DIR}/bpf/oncpu_pid.bt" "${KERNEL_TGID}"

if command -v perf >/dev/null; then
    echo
    echo "=== perf sched timehist summary for the worker ==="
    perf sched record -o "${WORK}/sched.data" -- sleep "${SECONDS_EACH}" >/dev/null 2>&1
    perf sched timehist -i "${WORK}/sched.data" -s 2>/dev/null | grep -E 'comm|^ +(worker|hog|flusher)\[' | head -n 20
    echo
    echo "=== perf on-CPU profile of the process, top symbols ==="
    perf record -o "${WORK}/cpu.data" -F 99 -g -p "${SPIKE_PID}" -- sleep "${SECONDS_EACH}" >/dev/null 2>&1
    perf report -i "${WORK}/cpu.data" --stdio --no-children --sort comm,symbol -g none 2>/dev/null |
        grep -E '^ +[0-9]' | head -n 12
else
    echo
    echo "perf is not on PATH: skipped perf sched and perf record"
fi
