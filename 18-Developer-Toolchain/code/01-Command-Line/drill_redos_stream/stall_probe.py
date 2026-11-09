#!/usr/bin/env python3
"""Show that the wedged filter is burning CPU in user space, not blocked in the kernel.

Runs `log_producer --mode=burst | filter_stream.py --mode=<mode>` and, after the filter has had time to hit
the pathological record, samples its /proc entries twice 0.5 s apart (Linux only).
Prints booleans, so the output is stable run to run.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CLK_TCK = os.sysconf("SC_CLK_TCK")


def cpu_ticks(pid: int) -> int:
    with open(f"/proc/{pid}/stat") as f:
        rest = f.read().rsplit(")", 1)[1].split()
    return int(rest[11]) + int(rest[12])  # utime + stime (fields 14 and 15)


def proc_state(pid: int) -> str:
    with open(f"/proc/{pid}/stat") as f:
        return f.read().rsplit(")", 1)[1].split()[0]


def proc_syscall(pid: int) -> str:
    with open(f"/proc/{pid}/syscall") as f:
        return f.read().strip().split()[0]


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "vulnerable"
    producer = subprocess.Popen([os.path.join(HERE, "log_producer"), "--mode=burst"], stdout=subprocess.PIPE)
    filt = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "filter_stream.py"), f"--mode={mode}"],
        stdin=producer.stdout, stdout=subprocess.DEVNULL,
    )
    producer.stdout.close()
    try:
        time.sleep(1.0)
        if filt.poll() is not None:
            print(f"FILTER_ALIVE_AFTER_1S: False (exit {filt.returncode})")
            return 1
        t0, c0 = time.monotonic(), cpu_ticks(filt.pid)
        state = proc_state(filt.pid)
        syscall = proc_syscall(filt.pid)
        time.sleep(0.5)
        t1, c1 = time.monotonic(), cpu_ticks(filt.pid)
        cpu_fraction = ((c1 - c0) / CLK_TCK) / (t1 - t0)
        print("FILTER_ALIVE_AFTER_1S: True")
        print(f"FILTER_STATE_IS_RUNNING: {state == 'R'}")
        print(f"FILTER_IN_SYSCALL: {syscall != 'running'}")
        print(f"FILTER_CPU_FRACTION_AT_LEAST_0.9: {cpu_fraction >= 0.9}")
    finally:
        filt.kill()
        filt.wait()
        producer.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
