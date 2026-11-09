#!/usr/bin/env python3
"""Deterministic verification runner for mini_shell with job control.

mini_shell runs as a session leader on a pseudo-terminal; this script types command lines on the
master side and checks what the shell and its children actually did.
Every check is built so that it fails when the mechanism it names is absent (see the mutation
list in the note).
Typed lines are echoed back by the PTY, so output checks compare whole output lines and never
substrings of the command line.
"""

import fcntl
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time

PROMPT = r"mini_sh> "
TIMEOUT = 3.0
TIOCSCTTY = getattr(termios, "TIOCSCTTY", 0x540E)  # 0x540E on Linux
FAILED = []
DIAGS = []


def check(label, ok):
    """Print one yes/no check line and remember failures."""
    print(f"{label}: {'yes' if ok else 'no'}")
    if not ok:
        FAILED.append(label)
    return ok


def tag(label, ok, good, bad):
    """Print one check line whose text names the outcome."""
    print(f"{label}: {good if ok else bad}")
    if not ok:
        FAILED.append(label)
    return ok


class Session:
    """One mini_shell on its own PTY, with a persistent read buffer so no output is dropped."""

    def __init__(self, env_extra=None):
        self.master, slave = pty.openpty()
        self.buf = ""
        self.transcript = ""
        self.wedged = False
        self.eof = False
        env = dict(os.environ)
        env.update(env_extra or {})
        self.pid = os.fork()
        if self.pid == 0:
            try:
                os.close(self.master)
                os.setsid()
                fcntl.ioctl(slave, TIOCSCTTY, 0)
                for fd in (0, 1, 2):
                    os.dup2(slave, fd)
                if slave > 2:
                    os.close(slave)
                os.execve("./mini_shell", ["./mini_shell"], env)
            finally:
                os._exit(127)
        os.close(slave)
        self.status = None

    def send(self, line):
        os.write(self.master, (line + "\n").encode())

    def expect(self, pattern, timeout=TIMEOUT):
        """Return (text up to and including the match, True), or (everything read, False)."""
        rx = re.compile(pattern, re.MULTILINE)
        if self.wedged:
            return "", False
        deadline = time.monotonic() + timeout
        while True:
            m = rx.search(self.buf)
            if m:
                text, self.buf = self.buf[:m.end()], self.buf[m.end():]
                return text, True
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.eof:
                text, self.buf = self.buf, ""
                return text, False
            ready, _, _ = select.select([self.master], [], [], min(remaining, 0.2))
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                self.eof = True
                continue
            decoded = chunk.decode(errors="replace").replace("\r", "")
            self.buf += decoded
            self.transcript += decoded

    def prompt(self, timeout=TIMEOUT):
        """Wait for the next prompt; a missing prompt wedges the session so later waits are instant."""
        text, ok = self.expect(PROMPT, timeout)
        if not ok:
            self.wedged = True
        return text

    def run(self, line, timeout=TIMEOUT):
        """Type one line and return its output lines (echo and prompt removed) plus a prompt flag."""
        self.send(line)
        text, ok = self.expect(PROMPT, timeout)
        if not ok:
            self.wedged = True
        return output_lines(text, line), ok

    def lflag(self):
        return termios.tcgetattr(self.master)[3]

    def cooked(self):
        flags = termios.ECHO | termios.ICANON
        return (self.lflag() & flags) == flags

    def exit_shell(self):
        """Type exit and return the shell's exit status, or None if it did not exit."""
        if self.wedged:
            return None
        self.send("exit")
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                self.status = status
                return os.waitstatus_to_exitcode(status)
            self.drain(0.1)
        return None

    def drain(self, seconds):
        """Read and buffer whatever arrives within the given time."""
        ready, _, _ = select.select([self.master], [], [], seconds)
        if ready:
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                self.eof = True
                return
            decoded = chunk.decode(errors="replace").replace("\r", "")
            self.buf += decoded
            self.transcript += decoded

    def close(self):
        if self.status is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.waitpid(self.pid, 0)
            except OSError:
                pass
        os.close(self.master)

    def diagnostics(self):
        return [ln for ln in self.transcript.split("\n") if ln.startswith("mini_sh: ")]


def output_lines(text, typed):
    """Lines a command printed: drop the echoed command line, the trailing prompt, and blanks."""
    lines = [ln.rstrip() for ln in text.split("\n")]
    if typed in lines:
        lines.remove(typed)
    return [ln for ln in lines if ln and not ln.startswith(PROMPT.strip())]


def pgid_report(lines):
    for ln in lines:
        m = re.fullmatch(r"shell_pgid: (\d+), terminal_fg: (-?\d+)", ln)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def probe_lines(text):
    """Parse PROBE: pid=.. pgid=.. fg=.. is_fg=.. lines into tuples."""
    found = re.findall(r"^PROBE: pid=(\d+) pgid=(\d+) fg=(\d+) is_fg=(\w+)$", text, re.MULTILINE)
    return [(int(a), int(b), int(c), d) for a, b, c, d in found]


def race_session(mode):
    """Run one foreground child with one half of the dual setpgid() delayed by 150 ms."""
    s = Session({"MINI_SHELL_RACE_DELAY": mode})
    try:
        s.prompt()
        s.send("./job_probe")
        text = s.prompt()
        probes = probe_lines(text)
        s.send("pgid")
        report = pgid_report(output_lines(s.prompt(), "pgid"))
        ok = (
            len(probes) == 1
            and probes[0][1] == probes[0][0]
            and probes[0][3] == "yes"
            and probes[0][2] == probes[0][1]
            and report is not None
            and report[0] == report[1] == s.pid
            and not s.diagnostics()
        )
        return ok
    finally:
        s.close()


def build(script_dir):
    cc = os.environ.get("CC", "cc")
    flags = ["-Wall", "-Wextra", "-pedantic", "-Werror", "-std=c99", "-D_POSIX_C_SOURCE=200809L"]
    for name in ("mini_shell", "job_probe"):
        subprocess.run([cc, *flags, f"{name}.c", "-o", name], check=True, cwd=script_dir)


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    build(script_dir)
    work = os.path.realpath(tempfile.mkdtemp(prefix="mini_shell_lab_"))
    out_file = os.path.join(work, "out.txt")
    err_file = os.path.join(work, "err.txt")

    s = Session()
    try:
        s.prompt()

        print("=== Milestone 1: Basic Execution and Builtins ===")
        lines, ok = s.run("echo HELLO_MINI_SHELL")
        check("external command output (echo)", ok and lines == ["HELLO_MINI_SHELL"])

        s.run(f"cd {work}")
        lines, ok = s.run("pwd")
        check("cd persists in the shell process (pwd)", ok and lines == [work])

        s.run(f"cd {script_dir}")
        lines, ok = s.run("pgid")
        report = pgid_report(lines)
        shell_pgid = report[0] if report else 0
        check("shell is its own process group leader", report is not None and shell_pgid == s.pid)
        check("shell holds the terminal foreground", report is not None and report[0] == report[1])

        print("\n=== Milestone 2: Pipelines and Write-End Closure ===")
        lines, ok = s.run("echo alpha bravo charlie | tr ' ' '\\n' | sort -r")
        print(f"pipe 3 stages: {' '.join(lines).upper()}")
        if not (ok and lines == ["charlie", "bravo", "alpha"]):
            FAILED.append("pipe 3 stages")
        left, ok_jobs = s.run("jobs")
        check("prompt returned and no job left behind", ok and ok_jobs and left == [])

        print("\n=== Milestone 3: I/O Redirection ===")
        s.run(f"echo long_first_line_aaaaaaaaaaaaaaaa > {out_file}")
        s.run(f"echo second > {out_file}")
        lines, _ = s.run(f"cat {out_file}")
        check("output truncation (>)", lines == ["second"])
        s.run(f"echo third >> {out_file}")
        lines, _ = s.run(f"cat {out_file}")
        check("output append (>>)", lines == ["second", "third"])
        lines, _ = s.run(f"cat < {out_file}")
        check("input redirection (<)", lines == ["second", "third"])
        lines, _ = s.run(f"sh -c 'echo err_test >&2' > {err_file} 2>&1")
        on_terminal = lines
        lines, _ = s.run(f"cat {err_file}")
        check("stderr merge (2>&1)", on_terminal == [] and lines == ["err_test"])

        print("\n=== Milestone 4: Dual setpgid and Process Isolation ===")
        s.send("./job_probe")
        text = s.prompt()
        probes = probe_lines(text)
        one = len(probes) == 1
        pid, pgid, fg, is_fg = probes[0] if one else (0, 0, 0, "no")
        check("child in distinct process group", one and pgid == pid and pgid != shell_pgid)
        check("child has terminal foreground during execution", one and is_fg == "yes" and fg == pgid)
        lines, _ = s.run("pgid")
        report = pgid_report(lines)
        check("shell reclaims terminal foreground after child exit",
              report is not None and report[0] == report[1] == shell_pgid)
        check("child setpgid alone suffices when the parent is delayed", race_session("parent"))
        check("parent setpgid alone suffices when the child is delayed", race_session("child"))

        print("\n=== Milestone 5: Pipeline Process Group Unification ===")
        s.send("./job_probe | ./job_probe")
        text = s.prompt()
        probes = probe_lines(text)
        two = len(probes) == 2
        check("pipeline stages share one pgid", two and probes[0][1] == probes[1][1])
        check("pipeline pgid is a stage pid and differs from shell pgid",
              two and probes[0][1] in (probes[0][0], probes[1][0]) and probes[0][1] != shell_pgid)

        print("\n=== Milestone 6: Job Suspension via SIGTSTP ===")
        job = "./job_probe --raw --stop --spin=1500 | ./job_probe --stop --spin=1500"
        s.send(job)
        text = s.prompt()
        m = re.search(r"^\[(\d+)\]\+ Stopped ", text, re.MULTILINE)
        tag("job stopped notification", m is not None, "received", "missing")
        job_pgids = {p[1] for p in probe_lines(text)}
        job_pgid = job_pgids.pop() if len(job_pgids) == 1 else 0
        lines, _ = s.run("jobs")
        tag("jobs list status", any(re.fullmatch(r"\[\d+\]\+ Stopped\s+\./job_probe --raw .*", ln) for ln in lines),
            "Stopped", "fail")
        lines, _ = s.run("pgid")
        report = pgid_report(lines)
        check("shell owns terminal foreground while job is stopped",
              report is not None and report[0] == report[1] == shell_pgid)
        check("shell restores its own terminal modes on stop", s.cooked())

        print("\n=== Milestone 7: Background Continuation via bg ===")
        s.send("bg")
        text = s.prompt()
        m = re.search(r"^\[\d+\] (\d+)$", text, re.MULTILINE)
        check("bg reports the job's process group", m is not None and int(m.group(1)) == job_pgid)
        check("bg returned the prompt while the job kept running", "finished spin" not in text)
        lines, _ = s.run("jobs")
        tag("jobs list status after bg",
            any(re.fullmatch(r"\[\d+\]\+ Running\s+\./job_probe --raw .*", ln) for ln in lines),
            "Running", "fail")
        done = False
        finished = 0
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not done:
            s.send("")
            text = s.prompt()
            finished += len(re.findall(r"^PROBE: finished spin$", text, re.MULTILINE))
            done = re.search(r"^\[\d+\]\+ Done ", text, re.MULTILINE) is not None
            if not done:
                time.sleep(0.25)
        check("every pipeline stage resumed and finished", finished == 2)
        tag("asynchronous job done notification", done, "received", "missed")
        s.send("./job_probe --stop --stop")
        text = s.prompt()
        first_stop = re.search(r"^\[\d+\]\+ Stopped ", text, re.MULTILINE) is not None
        s.send("bg")
        restopped = False
        deadline = time.monotonic() + 3.0
        text = s.prompt()
        while not restopped:
            restopped = re.search(r"^\[\d+\]\+ Stopped \./job_probe --stop --stop$", text, re.MULTILINE) is not None
            if restopped or time.monotonic() >= deadline:
                break
            time.sleep(0.25)
            s.send("")
            text = s.prompt()
        check("a bg job that stops again is reported again", first_stop and restopped)
        s.send("fg")
        s.prompt()
        left, ok_jobs = s.run("jobs")
        check("re-stopped job finished under fg", ok_jobs and left == [])

        print("\n=== Milestone 8: Foreground Resumption via fg ===")
        job = "./job_probe --raw --stop | ./job_probe --stop"
        s.send(job)
        text = s.prompt()
        stopped = re.search(r"^\[\d+\]\+ Stopped ", text, re.MULTILINE) is not None
        check("job suspended again", stopped and s.cooked())
        s.send("fg")
        text = s.prompt()
        lines = output_lines(text, "fg")
        check("fg echoes the command line", job in lines)
        resumed = re.findall(r"^PROBE: resumed after SIGTSTP echo=(\w+) icanon=(\w+)$", text, re.MULTILINE)
        check("every pipeline stage resumed with the job's terminal modes", resumed == [("off", "on"), ("off", "on")])
        left, ok_jobs = s.run("jobs")
        check("job completed in the foreground", ok_jobs and left == [])
        lines, _ = s.run("pgid")
        report = pgid_report(lines)
        check("shell reclaimed terminal foreground",
              report is not None and report[0] == report[1] == shell_pgid)
        check("shell terminal modes restored after fg", s.cooked())

        print("\n=== Session hygiene ===")
        status = s.exit_shell()
        check("shell exits with status 0 on exit", status == 0)
        DIAGS.extend(s.diagnostics())
        tag("shell diagnostics on stderr", not DIAGS, "none", "present")
    finally:
        s.close()
        shutil.rmtree(work, ignore_errors=True)

    if FAILED:
        print(f"FAILED checks: {len(FAILED)}", file=sys.stderr)
        for label in FAILED:
            print(f"  failed: {label}", file=sys.stderr)
        for line in sorted(set(DIAGS)):
            print(f"  shell said: {line}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
