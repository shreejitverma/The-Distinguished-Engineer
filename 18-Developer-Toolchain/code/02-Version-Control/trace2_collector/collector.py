#!/usr/bin/env python3
"""
Git Trace2 Telemetry Collector and Diagnostics Verifier.

Demonstrates and verifies:
1. Unix domain socket event streaming (af_unix:stream) with JSON schema validation.
2. Distributed process hierarchy tracking, child execution, and SID chaining.
3. Fleet telemetry metadata capture (trace2.configParams and trace2.envVars).
4. Automated credential redaction (GIT_TRACE2_REDACT).
5. Fail-safe socket disconnect handling and destination debugging.
6. Target directory sentinel bounding (trace2.maxFiles and git-trace2-discard).
7. Config isolation (repository-local and -c trace2 settings are ignored).
8. Diagnostic health bundle extraction (git diagnose).
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

SID_RE = re.compile(r"^\d{8}T\d{6}\.\d{6}Z-H[0-9a-f]{8}-P[0-9a-f]{8}")


class Trace2SocketServer:
    """Threaded Unix domain stream socket server collecting Trace2 JSON events."""

    def __init__(self, sock_path: str):
        self.sock_path = sock_path
        self.events: list[dict] = []
        self.bad_lines: list[str] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.sock_path)
        self._server.listen(64)
        self._server.settimeout(0.1)
        self._worker_threads: list[threading.Thread] = []
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def _handle_client(self, conn: socket.socket):
        buf = b""
        with conn:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
        for line in buf.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                with self._lock:
                    self.bad_lines.append(line)
                continue
            with self._lock:
                self.events.append(parsed)

    def _accept_loop(self):
        while not self._stop_event.is_set():
            try:
                conn, _ = self._server.accept()
                t = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
                t.start()
                with self._lock:
                    self._worker_threads.append(t)
            except socket.timeout:
                continue
            except OSError:
                break

    def stop(self):
        self._stop_event.set()
        self._accept_thread.join(timeout=2.0)
        with self._lock:
            workers = list(self._worker_threads)
        for t in workers:
            t.join(timeout=2.0)
        try:
            self._server.close()
        except OSError:
            pass
        if os.path.exists(self.sock_path):
            try:
                os.unlink(self.sock_path)
            except OSError:
                pass

    def get_events(self) -> list[dict]:
        with self._lock:
            return list(self.events)

    def get_bad_lines(self) -> list[str]:
        with self._lock:
            return list(self.bad_lines)


def make_clean_env(base_env: dict | None = None) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env["LC_ALL"] = "C"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["GIT_AUTHOR_NAME"] = "Telemetry Tester"
    env["GIT_AUTHOR_EMAIL"] = "tester@example.com"
    env["GIT_COMMITTER_NAME"] = "Telemetry Tester"
    env["GIT_COMMITTER_EMAIL"] = "tester@example.com"
    env["GIT_AUTHOR_DATE"] = "2026-11-08T12:00:00Z"
    env["GIT_COMMITTER_DATE"] = "2026-11-08T12:00:00Z"
    return env


def test_socket_ingestion_and_schema(tmpdir: str):
    print("[Test 1: Unix Domain Socket Ingestion & Event Schema]")
    repo = os.path.join(tmpdir, "repo1")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=repo, check=True)

    sock_path = os.path.join(tmpdir, "tr2_test1.sock")
    server = Trace2SocketServer(sock_path)

    env = make_clean_env()
    env["GIT_TRACE2_EVENT"] = f"af_unix:stream:{sock_path}"

    with open(os.path.join(repo, "sample.txt"), "w") as f:
        f.write("trace2 telemetry content\n")
    subprocess.run(["git", "add", "sample.txt"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "status"], cwd=repo, env=env, capture_output=True, check=True)

    time.sleep(0.1)
    server.stop()
    events = server.get_events()

    assert server.get_bad_lines() == [], f"Unparsable lines on socket: {server.get_bad_lines()[:3]}"
    assert len(events) > 0, "No events received on Unix domain socket"
    event_names = [e.get("event") for e in events]

    for req in ("version", "start", "cmd_name", "region_enter", "region_leave", "exit", "atexit"):
        assert req in event_names, f"Missing required event: {req}"

    first = events[0]
    assert first.get("event") == "version", f"First event was {first.get('event')}, expected version"
    evt_ver = first.get("evt")
    assert evt_ver == "4", f"Expected Trace2 event format version 4, got {evt_ver}"

    for e in events:
        sid = e.get("sid", "")
        assert SID_RE.match(sid), f"Invalid Session ID format: {sid}"
        assert "/" not in sid, f"Unexpected child SID in a root-only run: {sid}"

    # Verify region nesting and timestamps
    entered_regions = [e for e in events if e.get("event") == "region_enter"]
    left_regions = [e for e in events if e.get("event") == "region_leave"]
    assert len(entered_regions) > 0, "No region_enter events captured"
    assert len(left_regions) > 0, "No region_leave events captured"
    for lr in left_regions:
        assert "t_rel" in lr, "region_leave missing t_rel relative duration"
        assert lr["t_rel"] >= 0.0, "Relative region elapsed time must be non-negative"

    print("Socket type: af_unix (SOCK_STREAM)")
    print("Events received: OK (all lines valid JSON)")
    print(f"Base event types verified: {sorted(list(set(event_names)))}")
    print("Session ID format verified: ISO8601-UTC-H<hash>-P<pid>")
    print(f"Trace2 Event format version: {evt_ver}")
    print()


def test_process_hierarchy_and_sid_chaining(tmpdir: str):
    print("[Test 2: Process Hierarchy & SID Chaining]")
    repo = os.path.join(tmpdir, "repo2")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=repo, check=True)

    # Set up pre-commit hook that spawns a child git command
    hooks_dir = os.path.join(repo, ".git", "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    hook_path = os.path.join(hooks_dir, "pre-commit")
    with open(hook_path, "w") as f:
        f.write("#!/bin/sh\ngit status --porcelain\nexit 0\n")
    os.chmod(hook_path, 0o755)

    with open(os.path.join(repo, "tracked.txt"), "w") as f:
        f.write("tracked file content\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)

    sock_path = os.path.join(tmpdir, "tr2_test2.sock")
    server = Trace2SocketServer(sock_path)

    env = make_clean_env()
    env["GIT_TRACE2_EVENT"] = f"af_unix:stream:{sock_path}"

    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, env=env, capture_output=True, check=True)

    time.sleep(0.15)
    server.stop()
    events = server.get_events()

    # Identify parent and child commands
    parent_cmd = None
    child_cmds = []
    child_starts = []

    for e in events:
        if e.get("event") == "cmd_name":
            sid = e.get("sid", "")
            if "/" not in sid:
                parent_cmd = e
            else:
                child_cmds.append(e)
        elif e.get("event") == "child_start":
            child_starts.append(e)

    assert parent_cmd is not None, "Parent cmd_name event not found"
    assert parent_cmd.get("name") == "commit", f"Expected parent commit, got {parent_cmd.get('name')}"
    assert parent_cmd.get("hierarchy") == "commit", f"Parent hierarchy unexpected: {parent_cmd.get('hierarchy')}"

    # Verify child hook execution was logged by parent
    hook_starts = [cs for cs in child_starts if cs.get("child_class") == "hook"]
    assert len(hook_starts) > 0, "No child_start event logged for hook"
    assert hook_starts[0].get("hook_name") == "pre-commit", "Hook name was not pre-commit"

    # Verify child Git process (git status in hook)
    status_children = [c for c in child_cmds if c.get("name") == "status"]
    assert len(status_children) > 0, "Child git status command not found in telemetry"
    status_child = status_children[0]
    assert status_child.get("hierarchy") == "commit/status", f"Unexpected child hierarchy: {status_child.get('hierarchy')}"

    # Verify SID chaining
    parent_sid = parent_cmd.get("sid")
    child_sid = status_child.get("sid")
    assert child_sid.startswith(f"{parent_sid}/"), f"Child SID {child_sid} does not chain from parent {parent_sid}"

    print(f"Parent command: {parent_cmd['name']} (hierarchy: {parent_cmd['hierarchy']})")
    print(f"Child execution logged: child_class={hook_starts[0]['child_class']}, hook_name={hook_starts[0]['hook_name']}")
    print(f"Child Git command: {status_child['name']} (hierarchy: {status_child['hierarchy']})")
    print("Parent-child SID inheritance verified: <parent_sid>/<child_sid>")
    print()


def test_telemetry_parameters(tmpdir: str):
    print("[Test 3: Telemetry Parameter Capture]")
    repo = os.path.join(tmpdir, "repo3")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=repo, check=True)

    trace_file = os.path.join(tmpdir, "params.json")
    env = make_clean_env()
    env["GIT_TRACE2_EVENT"] = trace_file
    env["GIT_TRACE2_CONFIG_PARAMS"] = "core.*,user.*"
    env["GIT_TRACE2_ENV_VARS"] = "TRACE_TEST_ENV"
    env["TRACE_TEST_ENV"] = "active_fleet_telemetry"

    subprocess.run(["git", "status"], cwd=repo, env=env, capture_output=True, check=True)

    with open(trace_file, "r") as f:
        events = [json.loads(line) for line in f if line.strip()]

    def_params = [e for e in events if e.get("event") == "def_param"]
    assert len(def_params) > 0, "No def_param events emitted"

    config_scope = None
    env_scope = None
    for dp in def_params:
        if dp.get("param") == "core.bare" and dp.get("value") == "false":
            config_scope = dp.get("scope")
        if dp.get("param") == "TRACE_TEST_ENV" and dp.get("value") == "active_fleet_telemetry":
            env_scope = dp.get("scope")

    assert config_scope is not None, "core.bare def_param event missing"
    assert env_scope is not None, "TRACE_TEST_ENV def_param event missing"
    assert config_scope == "local", f"core.bare scope was {config_scope!r}, expected 'local'"
    assert env_scope == "command", f"TRACE_TEST_ENV scope was {env_scope!r}, expected 'command'"
    assert not any(dp.get("param", "").startswith("user.") for dp in def_params), "user.* leaked with no user config"

    print(f"Config telemetry captured: core.bare = false (scope: {config_scope})")
    print(f"Environment telemetry captured: TRACE_TEST_ENV = active_fleet_telemetry (scope: {env_scope})")
    print()


def test_credential_redaction(tmpdir: str):
    print("[Test 4: Credential Redaction]")
    raw_url = "https://telemetry_user:super_secret_token@example.com/repo.git"

    # 1. Redaction enabled
    trace_redacted = os.path.join(tmpdir, "redacted.json")
    env_redacted = make_clean_env()
    env_redacted["GIT_TRACE2_EVENT"] = trace_redacted
    env_redacted["GIT_TRACE2_REDACT"] = "1"
    subprocess.run(["git", "remote", "add", "origin", raw_url], cwd=tmpdir, env=env_redacted, capture_output=True)

    with open(trace_redacted, "r") as f:
        redacted_events = [json.loads(line) for line in f if line.strip()]

    redacted_argvs = [e["argv"] for e in redacted_events if "argv" in e]
    assert any("https://telemetry_user:<REDACTED>@example.com/repo.git" in argv for argv in redacted_argvs), (
        "Expected masked credential URL in argv"
    )
    assert not any("super_secret_token" in " ".join(argv) for argv in redacted_argvs), (
        "Secret token leaked into redacted trace"
    )

    # 2. Redaction disabled
    trace_unredacted = os.path.join(tmpdir, "unredacted.json")
    env_unredacted = make_clean_env()
    env_unredacted["GIT_TRACE2_EVENT"] = trace_unredacted
    env_unredacted["GIT_TRACE2_REDACT"] = "0"
    subprocess.run(["git", "remote", "set-url", "origin", raw_url], cwd=tmpdir, env=env_unredacted, capture_output=True)

    with open(trace_unredacted, "r") as f:
        unredacted_events = [json.loads(line) for line in f if line.strip()]

    unredacted_argvs = [e["argv"] for e in unredacted_events if "argv" in e]
    assert any("super_secret_token" in " ".join(argv) for argv in unredacted_argvs), (
        "Raw credential was not preserved with GIT_TRACE2_REDACT=0"
    )

    # 3. Token-only userinfo (no colon) is not masked, even with redaction on
    token_url = "https://tokenonly_abc123@example.com/repo.git"
    trace_token = os.path.join(tmpdir, "token.json")
    env_token = make_clean_env()
    env_token["GIT_TRACE2_EVENT"] = trace_token
    env_token["GIT_TRACE2_REDACT"] = "1"
    subprocess.run(["git", "remote", "set-url", "origin", token_url], cwd=tmpdir, env=env_token, capture_output=True)

    with open(trace_token, "r") as f:
        token_events = [json.loads(line) for line in f if line.strip()]

    token_argvs = [e["argv"] for e in token_events if "argv" in e]
    assert any("tokenonly_abc123" in " ".join(argv) for argv in token_argvs), (
        "Token-only URL unexpectedly masked or missing from argv"
    )
    assert not any("<REDACTED>" in " ".join(argv) for argv in token_argvs), "Token-only URL was redacted"

    print("Redaction enabled (GIT_TRACE2_REDACT=1): https://telemetry_user:<REDACTED>@example.com/repo.git")
    print("Redaction disabled (GIT_TRACE2_REDACT=0): credential preserved in raw trace")
    print("Security assertion: secret token absent when redaction active")
    print("Token-only URL (no colon): not masked even with redaction active")
    print()


def test_socket_resilience_and_debug(tmpdir: str):
    print("[Test 5: Socket Resilience & Destination Debugging]")
    bogus_sock = os.path.join(tmpdir, "nonexistent_trace.sock")

    # Silent fallback without debug
    env_silent = make_clean_env()
    env_silent["GIT_TRACE2_EVENT"] = f"af_unix:stream:{bogus_sock}"
    started = time.monotonic()
    res_silent = subprocess.run(["git", "version"], env=env_silent, capture_output=True, text=True, timeout=30)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"Git stalled for {elapsed:.1f}s on a missing socket"
    assert res_silent.returncode == 0, f"Git failed on unavailable socket: {res_silent.returncode}"
    assert res_silent.stderr == "", f"Stderr was not clean on silent fallback: {res_silent.stderr}"

    # Destination debug enabled
    env_debug = make_clean_env()
    env_debug["GIT_TRACE2_EVENT"] = f"af_unix:stream:{bogus_sock}"
    env_debug["GIT_TRACE2_DST_DEBUG"] = "1"
    res_debug = subprocess.run(["git", "version"], env=env_debug, capture_output=True, text=True)
    assert res_debug.returncode == 0, f"Git command failed under debug flag: {res_debug.returncode}"
    assert "warning: trace2: could not connect to socket" in res_debug.stderr, (
        f"Expected socket connection warning on stderr, got: {res_debug.stderr}"
    )

    print("Non-existent socket without debug: silent fallback, exit code 0, empty stderr, no stall")
    print("Non-existent socket with GIT_TRACE2_DST_DEBUG=1: diagnostic warning captured")
    print()


def test_target_directory_sentinel(tmpdir: str):
    print("[Test 6: Directory Target & Sentinel File Discard]")
    trace_dir = os.path.join(tmpdir, "dir_target")
    os.makedirs(trace_dir)

    env = make_clean_env()
    env["GIT_TRACE2_EVENT"] = trace_dir
    env["GIT_TRACE2_MAX_FILES"] = "2"

    for _ in range(5):
        subprocess.run(["git", "version"], env=env, capture_output=True, check=True)

    files = sorted(os.listdir(trace_dir))
    sentinel_name = "git-trace2-discard"
    assert sentinel_name in files, f"Expected sentinel file {sentinel_name} in {files}"

    with open(os.path.join(trace_dir, sentinel_name), "r") as f:
        sentinel_lines = [json.loads(line) for line in f if line.strip()]

    discard_events = [e for e in sentinel_lines if e.get("event") == "too_many_files"]
    assert len(discard_events) > 0, "too_many_files event missing in sentinel file"

    # Check total files are bounded
    assert len(files) <= 3, f"Directory file count exceeded maxFiles threshold: {files}"

    print("Trace directory file limit enforced: maxFiles = 2")
    print(f"Sentinel file created: {sentinel_name}")
    print("Sentinel event verified: too_many_files")
    print("Subsequent trace generation halted: directory files bounded")
    print()


def test_config_isolation(tmpdir: str):
    print("[Test 7: Config Isolation]")
    repo = os.path.join(tmpdir, "repo_iso")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=repo, check=True)

    local_target = os.path.join(tmpdir, "iso_local.json")
    cmdline_target = os.path.join(tmpdir, "iso_cmdline.json")
    system_cfg = os.path.join(tmpdir, "iso_system.gitconfig")
    system_target = os.path.join(tmpdir, "iso_system.json")

    env = make_clean_env()
    subprocess.run(["git", "config", "trace2.eventTarget", local_target], cwd=repo, env=env, check=True)
    subprocess.run(["git", "-c", f"trace2.eventTarget={cmdline_target}", "status"], cwd=repo, env=env, capture_output=True, check=True)
    assert not os.path.exists(local_target), "Repository-local trace2.eventTarget was honored"
    assert not os.path.exists(cmdline_target), "-c trace2.eventTarget was honored"

    # Control: the same key in system config is honored, so the checks above can fail
    with open(system_cfg, "w") as f:
        f.write(f"[trace2]\n\teventTarget = {system_target}\n")
    env_system = make_clean_env()
    env_system["GIT_CONFIG_SYSTEM"] = system_cfg
    subprocess.run(["git", "status"], cwd=repo, env=env_system, capture_output=True, check=True)
    assert os.path.exists(system_target), "System-config trace2.eventTarget was not honored"

    print("Repository-local trace2.eventTarget: ignored")
    print("Command-line -c trace2.eventTarget: ignored")
    print("System-config trace2.eventTarget: honored")
    print()


def test_git_diagnose_archive(tmpdir: str):
    print("[Test 8: Git Diagnostics Archive Generation]")
    repo = os.path.join(tmpdir, "repo_diag")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=repo, check=True)

    out_dir = os.path.join(tmpdir, "diag_out")
    os.makedirs(out_dir)

    subprocess.run(
        ["git", "diagnose", "-o", out_dir, "-s", "telemetry", "--mode=stats"],
        cwd=repo,
        capture_output=True,
        check=True,
    )

    expected_zip = os.path.join(out_dir, "git-diagnostics-telemetry.zip")
    assert os.path.exists(expected_zip), f"Expected diagnostic archive not found: {expected_zip}"

    with zipfile.ZipFile(expected_zip, "r") as z:
        names = sorted(z.namelist())
        assert "diagnostics.log" in names, "diagnostics.log missing from archive"
        assert "packs-local.txt" in names, "packs-local.txt missing from archive"
        assert "objects-local.txt" in names, "objects-local.txt missing from archive"

        log_content = z.read("diagnostics.log").decode("utf-8")
        assert "git version" in log_content, "Git version missing in diagnostics.log"
        assert "cpu:" in log_content, "CPU architecture missing in diagnostics.log"
        assert "Available space on" in log_content, "Filesystem disk space missing in diagnostics.log"

    print("Archive generated: git-diagnostics-telemetry.zip")
    print(f"Archive entries verified: {names}")
    print("System diagnostics extracted: Git version, CPU arch, disk space telemetry present")
    print()


def main():
    print("=== Git Trace2 Telemetry Collector Verification ===")
    print()
    with tempfile.TemporaryDirectory() as td:
        test_socket_ingestion_and_schema(td)
        test_process_hierarchy_and_sid_chaining(td)
        test_telemetry_parameters(td)
        test_credential_redaction(td)
        test_socket_resilience_and_debug(td)
        test_target_directory_sentinel(td)
        test_config_isolation(td)
        test_git_diagnose_archive(td)

    print("All 8 telemetry and diagnostic mechanisms verified successfully.")


if __name__ == "__main__":
    main()
