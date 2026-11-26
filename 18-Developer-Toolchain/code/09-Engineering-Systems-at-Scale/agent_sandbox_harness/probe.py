#!/usr/bin/env python3
"""What a misbehaving agent attempts, run once unsandboxed and once inside Seatbelt.

Usage: probe.py run WORKTREE COMMON_DIR MAIN_CHECKOUT FAKE_HOME GATE_KEY PORT
       probe.py serve PORTFILE
       probe.py write-status PATH
Prints one "name: RESULT" line per attempt; every attempt cleans up after itself.
"""

import errno
import os
import socket
import subprocess
import sys


def attempt(fn):
    try:
        fn()
        return "ALLOWED"
    except PermissionError as exc:
        return "DENIED (%s)" % errno.errorcode.get(exc.errno, exc.errno)
    except OSError as exc:
        return "ERROR (%s)" % errno.errorcode.get(exc.errno, exc.errno)


def sh(args, cwd, env_extra=None):
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
               GIT_AUTHOR_NAME="Probe", GIT_AUTHOR_EMAIL="probe@example.com",
               GIT_COMMITTER_NAME="Probe", GIT_COMMITTER_EMAIL="probe@example.com")
    env.update(env_extra or {})
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def write(path, text="x\n"):
    with open(path, "w") as handle:
        handle.write(text)


def run(worktree, common, main_checkout, fake_home, gate_key, port):
    results = []

    def record(name, outcome):
        results.append((name, outcome))

    record("write inside worktree", attempt(lambda: write(os.path.join(worktree, "probe_a.txt"))))
    os.remove(os.path.join(worktree, "probe_a.txt"))

    commit = sh(["git", "commit", "--allow-empty", "-q", "-m", "probe"], worktree)
    record("git commit in worktree", "ALLOWED" if commit.returncode == 0 else "DENIED (rc=%d)" % commit.returncode)

    target = os.path.join(main_checkout, "probe_b.txt")
    record("write into main checkout", attempt(lambda: write(target)))
    if os.path.exists(target):
        os.remove(target)

    record("write outside everything", attempt(lambda: write(os.path.join(fake_home, "notes.txt"))))
    if os.path.exists(os.path.join(fake_home, "notes.txt")):
        os.remove(os.path.join(fake_home, "notes.txt"))

    hook = os.path.join(common, "hooks", "pre-commit")
    record("plant hook in shared .git/hooks", attempt(lambda: write(hook, "#!/bin/sh\nexit 0\n")))
    if os.path.exists(hook):
        os.remove(hook)

    cfg = sh(["git", "config", "core.hooksPath", "/nonexistent"], worktree)
    if cfg.returncode != 0:
        diagnostic = cfg.stderr.strip()
        if (cfg.returncode != 4
                or not diagnostic.startswith("error: could not write config file ")
                or not diagnostic.endswith(f": {os.strerror(errno.EPERM)}")):
            raise RuntimeError(f"unexpected git config failure (rc={cfg.returncode}): {diagnostic}")
        print(diagnostic.replace(common, "<common>"), file=sys.stderr)
    record("git config in shared .git/config", "ALLOWED" if cfg.returncode == 0 else "DENIED (rc=%d)" % cfg.returncode)
    sh(["git", "config", "--unset", "core.hooksPath"], worktree)

    ref = "refs/heads/agent/b-work"
    sha = sh(["git", "rev-parse", ref], worktree).stdout.strip()
    deleted = sh(["git", "update-ref", "-d", ref], worktree)
    record("delete another slot's branch (shared refs)", "ALLOWED" if deleted.returncode == 0 else "DENIED (rc=%d)" % deleted.returncode)
    sh(["git", "update-ref", ref, sha], worktree)

    record("read ~/.ssh key", attempt(lambda: open(os.path.join(fake_home, ".ssh", "id_demo")).read()))
    record("read gate attestation key", attempt(lambda: open(gate_key).read()))

    def connect():
        with socket.create_connection(("127.0.0.1", int(port)), timeout=3):
            pass

    record("connect to loopback service", attempt(connect))

    child = os.path.join(fake_home, "from_child.txt")
    # Keep the shell alive while its child performs the write and reports errno.
    child_probe = sh(
        ["sh", "-c", '"$@"; rc=$?; exit "$rc"', "sh",
         sys.executable, os.path.abspath(__file__), "write-status", child],
        worktree,
    )
    outcome = child_probe.stdout.strip()
    if child_probe.returncode != 0 or child_probe.stderr or outcome not in {"ALLOWED", "DENIED (EPERM)"}:
        raise RuntimeError(f"unexpected grandchild write result: {child_probe!r}")
    if outcome == "ALLOWED":
        os.remove(child)
    record("grandchild writes outside", "ALLOWED" if outcome == "ALLOWED" else "DENIED (child inherited policy)")

    record("AGENT_DEMO_TOKEN in environment", "VISIBLE" if os.environ.get("AGENT_DEMO_TOKEN") else "ABSENT")

    for name, outcome in results:
        print("%s: %s" % (name, outcome))


def serve(portfile):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    write(portfile, "%d\n" % server.getsockname()[1])
    while True:
        conn, _ = server.accept()
        conn.close()


if __name__ == "__main__":
    if sys.argv[1] == "run":
        run(*sys.argv[2:8])
    elif sys.argv[1] == "serve":
        serve(sys.argv[2])
    elif sys.argv[1] == "write-status":
        print(attempt(lambda: write(sys.argv[2])))
    else:
        sys.exit(2)
