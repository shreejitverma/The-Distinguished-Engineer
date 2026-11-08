#!/usr/bin/env python3
"""PTY line discipline drill: raw-mode corruption, the Enter trap, and blind recovery.

Runs on a pseudo-terminal pair. A real `bash` process reads one line from the slave with the
`read` builtin, which is a canonical-mode style reader: only a line feed ends the line.
Every printed status is computed from bytes that actually crossed the PTY and is asserted.
"""

from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import termios

QUIET_S = 0.2
WAIT_S = 5.0
READER = 'echo READY; IFS= read -r x; echo "LEN=${#x}"'


def fail(message: str) -> None:
    print(f"ASSERTION FAILED: {message}", file=sys.stderr)
    sys.exit(1)


def read_until_quiet(fd: int, quiet: float = QUIET_S) -> bytes:
    """Collect whatever arrives on fd until it stays silent for `quiet` seconds."""
    data = b""
    while True:
        ready, _, _ = select.select([fd], [], [], quiet)
        if not ready:
            return data
        chunk = os.read(fd, 4096)
        if not chunk:
            return data
        data += chunk


def read_exactly(fd: int, n: int) -> bytes:
    data = b""
    while len(data) < n:
        ready, _, _ = select.select([fd], [], [], WAIT_S)
        if not ready:
            fail(f"timed out waiting for {n} bytes, got {data!r}")
        data += os.read(fd, n - len(data))
    return data


def flags(fd: int) -> dict[str, int]:
    iflag, oflag, _cflag, lflag, _ispeed, _ospeed, _cc = termios.tcgetattr(fd)
    return {
        "ECHO": int(bool(lflag & termios.ECHO)),
        "ICANON": int(bool(lflag & termios.ICANON)),
        "ISIG": int(bool(lflag & termios.ISIG)),
        "ONLCR": int(bool(oflag & termios.ONLCR)),
        "ICRNL": int(bool(iflag & termios.ICRNL)),
        "OPOST": int(bool(oflag & termios.OPOST)),
    }


def show(label: str, fd: int) -> dict[str, int]:
    state = flags(fd)
    print(f"{label}: " + " ".join(f"{k}={v}" for k, v in state.items()))
    return state


def start_reader(master: int, slave: int) -> subprocess.Popen[bytes]:
    """Start the reader and return once it has announced READY, so later waits measure the read itself."""
    proc = subprocess.Popen(
        ["bash", "--noprofile", "--norc", "-c", READER],
        stdin=slave, stdout=slave, stderr=slave, close_fds=True,
    )
    seen = b""
    while b"READY" not in seen:
        ready, _, _ = select.select([master], [], [], WAIT_S)
        if not ready:
            proc.kill()
            fail(f"reader never printed READY, got {seen!r}")
        seen += os.read(master, 4096)
    read_until_quiet(master)
    return proc


def main() -> None:
    master, slave = pty.openpty()

    print("--- 1. Baseline Canonical Terminal Discipline ---")
    base = show("Baseline flags", slave)
    if base != {"ECHO": 1, "ICANON": 1, "ISIG": 1, "ONLCR": 1, "ICRNL": 1, "OPOST": 1}:
        fail(f"unexpected baseline {base}")

    os.write(slave, b"status: ok\nnext: build\n")
    out = read_until_quiet(master)
    print("Baseline output mapping (NL -> CR+NL active):", out == b"status: ok\r\nnext: build\r\n")

    os.write(master, b"deploy\r")
    got = read_exactly(slave, len(b"deploy\n"))
    print("Baseline input mapping (Enter \\r translated to NL):", got == b"deploy\n")
    echoed = read_until_quiet(master)
    print("Baseline echo of typed input:", repr(echoed.decode("ascii", errors="replace")))

    print("\n--- 2. Corrupted Raw Mode Discipline ---")
    attrs = termios.tcgetattr(slave)
    attrs[0] &= ~termios.ICRNL
    attrs[1] &= ~(termios.OPOST | termios.ONLCR)
    attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    raw = show("Corrupted flags", slave)
    if any(raw.values()):
        fail(f"corruption did not take effect: {raw}")

    os.write(slave, b"step1\nstep2\nstep3\n")
    out = read_until_quiet(master)
    print("Corrupted output raw bytes:", repr(out.decode("ascii", errors="replace")))
    print("Staircase symptom present (raw NL without CR):", out == b"step1\nstep2\nstep3\n")

    os.write(master, b"ls\r")
    got = read_exactly(slave, 3)
    print("Corrupted input with Enter key (\\r):", repr(got))
    os.write(master, b"ls\n")
    got = read_exactly(slave, 3)
    print("Corrupted input with Ctrl+J (\\n):", repr(got))
    print("Typed input echoed back to the terminal:", read_until_quiet(master) != b"")

    # A canonical-mode style reader (bash read) sees CR as an ordinary character.
    reader = start_reader(master, slave)
    os.write(master, b"ls\r")
    try:
        reader.wait(timeout=QUIET_S * 2)
        stuck = False
    except subprocess.TimeoutExpired:
        stuck = True
    print("Reader still waiting after Enter (\\r):", stuck)
    if not stuck:
        fail("reader finished on CR alone")
    os.write(master, b"\n")
    try:
        reader.wait(timeout=WAIT_S)
    except subprocess.TimeoutExpired:
        reader.kill()
        fail("reader never finished on LF")
    reply = read_until_quiet(master)
    print("Reader result after Ctrl+J (\\n):", repr(reply.decode("ascii", errors="replace")))
    if reply != b"LEN=3\n":
        fail(f"reader should have captured 'ls\\r' (3 bytes), got {reply!r}")

    print("\n--- 3. Terminal Line Discipline Recovery ---")
    subprocess.run(["stty", "sane"], stdin=slave, check=True)
    sane = show("Recovered flags (stty sane)", slave)
    for name in ("ECHO", "ICANON", "ISIG", "ONLCR", "ICRNL", "OPOST"):
        if sane[name] != 1:
            fail(f"stty sane left {name} cleared")

    os.write(slave, b"step1\nstep2\nstep3\n")
    out = read_until_quiet(master)
    print("Recovered output formatting (CR+NL restored):", out == b"step1\r\nstep2\r\nstep3\r\n")

    reader = start_reader(master, slave)
    os.write(master, b"ls\r")
    try:
        reader.wait(timeout=WAIT_S)
    except subprocess.TimeoutExpired:
        reader.kill()
        fail("reader still stuck after stty sane")
    reply = read_until_quiet(master)
    print("Recovered reader after Enter (\\r):", repr(reply.decode("ascii", errors="replace")))
    if reply != b"ls\r\nLEN=2\r\n":
        fail(f"expected echo plus LEN=2 after recovery, got {reply!r}")

    os.close(master)
    os.close(slave)


if __name__ == "__main__":
    main()
