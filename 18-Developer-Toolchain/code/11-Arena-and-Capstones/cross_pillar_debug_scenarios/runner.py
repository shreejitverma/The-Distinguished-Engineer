#!/usr/bin/env python3
"""Cross-Pillar Debugging Scenario Bank lab.

Three scenarios run a real, portable check and fail the lab if the mechanism is absent:
  2: a SIGKILLed process leaves no core dump and a shell reports status 137.
  3: a lock held by a thread that does not exist in the child stays locked after fork().
  10: a seccomp-style fallback decision that depends on ENOSYS versus EPERM.
The other seven scenarios need a Linux toolchain, a debugger, or specific hardware;
they print a labelled walkthrough of the note's claim and prove nothing by themselves.
Output is identical on Linux and macOS.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import threading


def check(cond: bool, what: str) -> None:
    if not cond:
        print(f"FAIL: {what}")
        sys.exit(1)


def head(n: int, title: str) -> None:
    print(f"=== SCENARIO {n}: {title} ===")


def walkthrough(lines: list[str]) -> None:
    print("kind: walkthrough (printed text, not a reproduction)")
    for line in lines:
        print(f"  {line}")


def scenario_1() -> None:
    head(1, "COPY RELOCATION SIZE MISMATCH")
    walkthrough([
        "executable linked against a 64-byte engine_config reserves 64 bytes in .bss",
        "upgraded DSO defines a 128-byte engine_config",
        "R_X86_64_COPY copies MIN(sym st_size, refsym st_size) bytes (glibc dl-machine.h)",
        "glibc warns 'Symbol ... has different size in shared object'",
        "-fPIE alone does not remove copy relocations on x86-64; see the note for the flags that do",
    ])


def scenario_2() -> None:
    head(2, "SIGKILL LEAVES NO CORE DUMP AND SHELLS REPORT 137")
    p = subprocess.run([sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"])
    check(p.returncode == -signal.SIGKILL, "child was not killed by SIGKILL")
    pid = os.fork()
    if pid == 0:
        os.kill(os.getpid(), signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    check(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, "waitpid did not report SIGKILL")
    check(not os.WCOREDUMP(status), "a SIGKILLed process must not report a core dump")
    sh = subprocess.run(["sh", "-c", 'sh -c "kill -9 \\$\\$"; echo $?'], capture_output=True, text=True)
    check(sh.stdout.strip() == "137", "shell did not report 128 + 9")
    print("child terminated by signal 9, core dump flag clear: yes")
    print("shell status of a SIGKILLed child: 137")


def scenario_3() -> None:
    head(3, "FORK WITHOUT EXEC KEEPS A LOCK ITS OWNER THREAD NO LONGER HAS")
    lock = threading.Lock()
    held = threading.Event()
    release = threading.Event()

    def owner() -> None:
        with lock:
            held.set()
            release.wait()

    t = threading.Thread(target=owner)
    t.start()
    held.wait()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        got = lock.acquire(timeout=0.5)
        os.write(w, b"acquired" if got else b"timeout")
        os._exit(0)
    os.close(w)
    result = os.read(r, 16).decode()
    os.waitpid(pid, 0)
    release.set()
    t.join()
    check(result == "timeout", "the child acquired a lock held by another thread at fork time")
    print("child lock.acquire(timeout=0.5) while parent thread held the lock at fork: timeout")
    print("note: glibc fork() resets its own malloc locks; this hazard is for locks it does not know about")


def scenario_4() -> None:
    head(4, "NULL CHECK DELETED AFTER A DEREFERENCE")
    walkthrough([
        "p is dereferenced, then tested against NULL",
        "-fdelete-null-pointer-checks removes the test; -fno-strict-aliasing does not restore it",
        "GCC 13.2 and Clang 21 emit a scalar compare, not vector moves",
    ])


def scenario_5() -> None:
    head(5, "TLS DESTRUCTOR RUNS WITHOUT A PYTHON THREAD STATE")
    walkthrough([
        "thread exit: CPython clears its thread state, then glibc runs __call_tls_dtors",
        "a Rust or C++ destructor that calls into the C-API then finds a NULL thread state",
        "CPython aborts with a fatal error naming the NULL thread state",
    ])


def scenario_6() -> None:
    head(6, "SIGNAL-TIME UNWIND WITHOUT ASYNCHRONOUS CFI")
    walkthrough([
        "a profiling signal can land on any instruction, not only at calls",
        "tables built without asynchronous unwind tables are exact only at call sites",
        "the unwinder then computes the wrong CFA and the trace is truncated or wrong",
    ])


def scenario_7() -> None:
    head(7, "LAZY BINDING UNDER SIGNALS")
    walkthrough([
        "lazy binding resolves a PLT slot on first call inside _dl_runtime_resolve",
        "a signal handler that first-calls an unresolved function re-enters the resolver",
        "-Wl,-z,relro,-z,now resolves everything at load time",
    ])


def scenario_8() -> None:
    head(8, "STORE ORDERING ON x86-64 VERSUS AARCH64")
    walkthrough([
        "x86 TSO keeps stores in program order; AArch64 may commit them out of order",
        "the compiler may also reorder plain stores on either architecture",
        "a release store (stlr) on the index publishes the data store before it",
    ])


def scenario_9() -> None:
    head(9, "RSS FAR ABOVE LIVE HEAP")
    walkthrough([
        "glibc creates per-thread arenas on demand, up to 8 * cores by default on 64-bit",
        "glibc malloc releases memory with MADV_DONTNEED, not MADV_FREE",
        "MALLOC_ARENA_MAX bounds the arena count; malloc_trim(0) returns free pages",
    ])


def scenario_10() -> None:
    head(10, "ENOSYS VERSUS EPERM FROM A SECCOMP FILTER")

    def clone_with_fallback(rc: int) -> str:
        if rc == 0:
            return "clone3"
        if rc == errno.ENOSYS:
            return "clone (fallback)"
        return "failed"

    check(clone_with_fallback(0) == "clone3", "success path must use clone3")
    check(clone_with_fallback(errno.ENOSYS) == "clone (fallback)", "ENOSYS must trigger the fallback")
    check(clone_with_fallback(errno.EPERM) == "failed", "EPERM must not trigger the fallback")
    print("model of the glibc rule: ENOSYS from clone3 -> clone; EPERM -> thread creation fails")
    print(f"symbolic names: {errno.errorcode[errno.ENOSYS]}, {errno.errorcode[errno.EPERM]}")
    print("kind: model (the rule is read from glibc clone-internal.c; no seccomp filter is installed)")


def main() -> int:
    print("Cross-Pillar Debugging Scenario Bank lab")
    for fn in (scenario_1, scenario_2, scenario_3, scenario_4, scenario_5,
               scenario_6, scenario_7, scenario_8, scenario_9, scenario_10):
        fn()
        print()
    print("real checks passed: scenarios 2 and 3; scenario 10 is a model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
