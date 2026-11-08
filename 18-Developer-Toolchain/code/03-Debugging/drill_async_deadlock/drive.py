"""Operator side of the asyncio half of the drill: start service.py, inspect it live, report.

Every scenario starts the service as a child process, waits for a marker line, and then
does what an on-call engineer would: send SIGUSR1 (loop-level task dump), send SIGUSR2
(faulthandler thread dump), and read the answers. Only derived facts are printed, never
paths, PIDs, addresses, or timings, so the output is the same on every run.
"""

from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICE = HERE / "service.py"
FRAME_RE = re.compile(r'^\s+File "(?P<path>[^"]+)", line \d+ in (?P<func>\S+)$')


class Child:
    def __init__(self, mode: str, *flags: str) -> None:
        cmd = [sys.executable, "-I", *flags, str(SERVICE), mode]
        # Unbuffered bytes: a buffered readline() would pull several lines into Python's
        # buffer, and the selector would then report the pipe empty while lines wait unread.
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.fd = self.proc.stdout.fileno()
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.fd, selectors.EVENT_READ)
        self.pending = b""
        self.eof = False
        self.lines: list[str] = []
        self.consumed: list[str] = []

    def read_line(self, timeout: float) -> str | None:
        """One stdout line, or None if nothing arrives within timeout seconds."""
        deadline = time.monotonic() + timeout
        while b"\n" not in self.pending:
            left = deadline - time.monotonic()
            if self.eof or left <= 0 or not self.sel.select(left):
                return None
            chunk = os.read(self.fd, 65536)
            if not chunk:
                self.eof = True
            self.pending += chunk
        line, self.pending = self.pending.split(b"\n", 1)
        text = line.decode()
        self.consumed.append(text)
        return text

    def wait_for(self, prefix: str, timeout: float = 5.0) -> str:
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            line = self.read_line(left)
            if line is None:
                break
            self.lines.append(line)
            if line.startswith(prefix):
                return line
        self.kill()
        raise SystemExit(f"FAIL: no line starting with {prefix!r} within {timeout}s; got {self.lines}")

    def signal(self, sig: int) -> None:
        os.kill(self.proc.pid, sig)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        if self.alive():
            self.proc.terminate()

    def finish(self, timeout: float = 5.0) -> tuple[int, str, str]:
        """Exit code, the whole of stdout (including lines already read), and stderr."""
        out, err = self.proc.communicate(timeout=timeout)
        before = "".join(f"{ln}\n" for ln in self.consumed)
        return self.proc.returncode, before + (self.pending + out).decode(), err.decode()


def thread_frames(stderr: str) -> list[str]:
    """Innermost-first 'func (file)' entries of the first thread in a faulthandler dump."""
    frames = []
    for line in stderr.splitlines():
        m = FRAME_RE.match(line)
        if m:
            frames.append(f"{m['func']} ({Path(m['path']).name})")
        elif frames:
            break
    return frames


def lockorder() -> None:
    print("=== Scenario 1: lock-order deadlock (asyncio.Lock) ===")
    c = Child("lockorder")
    c.wait_for("ready")
    time.sleep(0.5)  # the cycle forms after 0.05 s
    print(f"hung after 0.5 s: {'yes' if c.alive() else 'no'}")
    c.signal(signal.SIGUSR1)
    c.wait_for("=== end dump ===")
    start = c.lines.index("=== SIGUSR1 task dump (ran as an event loop callback) ===")
    print("SIGUSR1 dump answered: yes")
    for line in c.lines[start + 1 : -1]:
        print(line)
    c.kill()
    rc, _, _ = c.finish()
    print(f"terminated with SIGTERM: {'yes' if rc == -signal.SIGTERM else f'no (rc {rc})'}")
    rc, out, _ = Child("lockorder-fixed").finish()
    print(f"fix (one global lock order): exit {rc}, {out.splitlines()[-1]}")


def unawaited() -> None:
    print("\n=== Scenario 2: unawaited coroutine ===")
    for label, flags in (("default", ()), ("-X dev", ("-X", "dev"))):
        rc, out, err = Child("unawaited", *flags).finish()
        warning = next((ln for ln in err.splitlines() if "was never awaited" in ln), "")
        site = re.sub(r"^.*/", "", warning)
        origin = "Coroutine created at" in err
        frames = [m[1] for ln in err.splitlines() if (m := re.match(r'^\s+File "[^"]+", line \d+, in (\S+)$', ln))]
        print(f"[{label}] exit {rc}; {out.splitlines()[-1]}")
        print(f"[{label}] warning: {site or 'none'}")
        print(f"[{label}] creation traceback: {'yes, innermost frame ' + frames[-1] if origin else 'no'}")


def lostwake() -> None:
    print("\n=== Scenario 3: lost wakeup (Future resolved from another thread) ===")
    c = Child("lostwake")
    c.wait_for("producer:")
    print(f"producer: {c.lines[-1].split(': ', 1)[1]}")
    hung = c.read_line(0.5) is None and c.alive()
    print(f"consumer still hung 0.5 s after the result was set: {'yes' if hung else 'no'}")
    c.signal(signal.SIGUSR2)  # faulthandler's C handler does not touch the loop's wakeup fd
    hung = c.read_line(0.3) is None and c.alive()
    print(f"consumer still hung 0.3 s after SIGUSR2: {'yes' if hung else 'no'}")
    c.signal(signal.SIGUSR1)  # asyncio's handler writes the wakeup fd, which un-sticks it
    woke = c.wait_for("consumer: woke")
    rc, out, err = c.finish()
    frames = thread_frames(err)
    print(f"SIGUSR2 faulthandler, main thread innermost frames: {' <- '.join(frames[:2])}")
    print(f"after SIGUSR1: {woke.split(': ', 1)[1]}, exit {rc}")
    print(
        f"order after SIGUSR1: {' | '.join(ln for ln in out.splitlines() if ln.startswith(('consumer: woke', '===')))}"
    )

    d = Child("lostwake", "-X", "dev")
    line = d.wait_for("producer:")
    print(f"[-X dev] producer: {line.split(': ', 1)[1]}")
    d.kill()
    d.finish()
    rc, out, _ = Child("lostwake-fixed").finish()
    print(f"fix (call_soon_threadsafe): exit {rc}, {out.splitlines()[-1].split(': ', 1)[1]}")


def blocked() -> None:
    print("\n=== Scenario 4: loop thread blocked (threading.Lock held across await) ===")
    c = Child("blocked")
    c.wait_for("ready")
    time.sleep(0.5)
    c.signal(signal.SIGUSR1)
    answered = c.read_line(1.0)
    print(f"SIGUSR1 dump answered within 1 s: {'yes' if answered else 'no'}")
    c.signal(signal.SIGUSR2)
    time.sleep(0.3)
    c.kill()
    rc, _, err = c.finish()
    frames = thread_frames(err)
    print(f"SIGUSR2 faulthandler, main thread innermost frames: {' <- '.join(frames[:2])}")
    print(f"terminated with SIGTERM: {'yes' if rc == -signal.SIGTERM else f'no (rc {rc})'}")


def main() -> None:
    if sys.version_info < (3, 14):
        raise SystemExit("FAIL: needs Python 3.14+ for asyncio.capture_call_graph")
    print("python >= 3.14: yes")
    lockorder()
    unawaited()
    lostwake()
    blocked()


if __name__ == "__main__":
    main()
