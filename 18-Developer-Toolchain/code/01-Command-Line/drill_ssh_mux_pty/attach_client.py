#!/usr/bin/env python3
"""Attach a real tmux client to a session, wait for update-environment, then detach.

usage: attach_client.py TMUX_SOCKET SESSION VAR EXPECTED_VALUE

The client runs on a pseudo-terminal and inherits this process's environment, so tmux copies
VAR from it into the session environment exactly as `tmux attach` does after an SSH login.
Exit status: 0 when the session environment shows VAR=EXPECTED_VALUE, 1 on timeout.
"""

from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import time

DEADLINE_S = 10.0


def session_value(sock: str, session: str, var: str) -> str:
    done = subprocess.run(
        ["tmux", "-S", sock, "show-environment", "-t", session, var],
        capture_output=True, text=True, check=False,
    )
    return done.stdout.strip()


def pump(fd: int, seconds: float) -> bool:
    """Discard terminal output for a short while. Return False once the client side hung up."""
    ready, _, _ = select.select([fd], [], [], seconds)
    if not ready:
        return True
    try:
        return bool(os.read(fd, 4096))
    except OSError:
        return False


def main() -> int:
    sock, session, var, want = sys.argv[1:5]
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe("tmux", ["tmux", "-S", sock, "attach-session", "-t", session], env)
    ok = False
    alive = True
    deadline = time.monotonic() + DEADLINE_S
    while alive and time.monotonic() < deadline:
        alive = pump(fd, 0.05)
        if session_value(sock, session, var) == f"{var}={want}":
            ok = True
            break
    subprocess.run(["tmux", "-S", sock, "detach-client", "-s", session], capture_output=True, check=False)
    end = time.monotonic() + 5.0
    while time.monotonic() < end:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        pump(fd, 0.05)
    else:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    os.close(fd)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
