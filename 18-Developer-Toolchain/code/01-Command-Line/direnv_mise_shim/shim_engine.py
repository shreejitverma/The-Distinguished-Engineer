#!/usr/bin/env python3
"""Deterministic lab: direnv allow-hash and env diff, shims vs PATH, devcontainer lifecycle.

Every check below runs real processes (bash, sh, the shim and the tool) and asserts on what they observe.
It models direnv, mise and a devcontainer engine; it does not invoke the real tools.

1. direnv: SHA-256 allow token over "<path>\\n<content>", a real `bash -c` evaluation of the .envrc,
   the {"p": prev, "n": next} diff, zlib + URL-safe base64 encoding, and the inverse on exit.
2. Shims vs PATH: a shim resolves the pin at every invocation; a pre-built PATH does not.
   Whether the parent shell sees a variable is observed by a parent `sh`, not asserted by a print.
3. Devcontainer lifecycle: hooks run as real processes in specification order, the object form runs
   concurrently (proved by a rendezvous that deadlocks if the tasks were serialized), and a failed
   hook stops every later hook.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

SYSTEM_PATH = "/usr/bin:/bin"


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def check(condition: bool, message: str) -> None:
    """Fail the lab loudly; a lab that cannot fail proves nothing."""
    if not condition:
        raise SystemExit(f"LAB CHECK FAILED: {message}")


# ---------------------------------------------------------------------------
# 1. direnv
# ---------------------------------------------------------------------------

VIRTUAL_ENVRC_PATH = "/workspaces/my-project/.envrc"


def allow_token(path: str, content: str) -> str:
    """direnv's fileHash: SHA-256 over the resolved .envrc path, a newline, then the file content."""
    return hashlib.sha256(f"{path}\n{content}".encode("utf-8")).hexdigest()


def is_allowed(allow_dir: Path, path: str, content: str) -> bool:
    return (allow_dir / allow_token(path, content)).exists()


def dump_env_after_sourcing(envrc: Path, cwd: Path, base_env: dict[str, str]) -> dict[str, str]:
    """Source `envrc` in a child bash (as direnv does) and dump the resulting environment as JSON."""
    dumper = "import json, os, sys; json.dump(dict(os.environ), sys.stdout)"
    script = f'. "$1" && exec "$2" -I -c "$3"'
    proc = subprocess.run(
        ["bash", "-c", script, "bash", str(envrc), sys.executable, dumper],
        env=base_env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def build_env_diff(before: dict[str, str], after: dict[str, str]) -> dict[str, dict[str, str]]:
    """Real direnv EnvDiff: p = previous value of every changed or removed key, n = new value of every added or changed key."""
    prev = {k: v for k, v in before.items() if k not in after or after[k] != v}
    nxt = {k: v for k, v in after.items() if k not in before or before[k] != v}
    return {"p": prev, "n": nxt}


def compress_diff(diff: dict[str, dict[str, str]]) -> bytes:
    return zlib.compress(json.dumps(diff, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def encode_diff(diff: dict[str, dict[str, str]]) -> str:
    return base64.urlsafe_b64encode(compress_diff(diff)).decode("ascii")


def decode_diff(token: str) -> dict[str, dict[str, str]]:
    return json.loads(zlib.decompress(base64.urlsafe_b64decode(token.encode("ascii"))))


def revert(env: dict[str, str], diff: dict[str, dict[str, str]]) -> dict[str, str]:
    """Inverse of the diff: restore every key in p, unset every key that appears only in n."""
    out = dict(env)
    for key in diff["n"]:
        if key not in diff["p"]:
            out.pop(key, None)
    out.update(diff["p"])
    return out


def test_direnv(workspace: Path) -> None:
    section("1. direnv: allow token, env diff, and exit")

    envrc = workspace / "project" / ".envrc"
    envrc.parent.mkdir(parents=True)
    content = (
        'export PROJECT_ENV="production"\n'
        'export PATH="/opt/project/bin:${PATH}"\n'
        "unset USER\n"
    )
    envrc.write_text(content, encoding="utf-8")

    allow_dir = workspace / "direnv_data" / "allow"
    allow_dir.mkdir(parents=True)
    token = allow_token(VIRTUAL_ENVRC_PATH, content)

    print(f"Initial check: .envrc authorized = {is_allowed(allow_dir, VIRTUAL_ENVRC_PATH, content)}")
    print(f"Computed SHA-256 allow token: {token}")
    (allow_dir / token).write_text(VIRTUAL_ENVRC_PATH + "\n", encoding="utf-8")
    print(f"After 'direnv allow': allow file created at allow/{token[:16]}...")
    print(f"Verification: .envrc authorized = {is_allowed(allow_dir, VIRTUAL_ENVRC_PATH, content)}")
    check(is_allowed(allow_dir, VIRTUAL_ENVRC_PATH, content), "allow file must authorize the exact content")

    edited = content.replace("production", "productioN")
    print(f"After a one-byte edit: .envrc authorized = {is_allowed(allow_dir, VIRTUAL_ENVRC_PATH, edited)}")
    check(not is_allowed(allow_dir, VIRTUAL_ENVRC_PATH, edited), "a one-byte edit must invalidate the token")
    print(
        "After moving the file: .envrc authorized = "
        f"{is_allowed(allow_dir, '/workspaces/other/.envrc', content)}"
    )
    check(not is_allowed(allow_dir, "/workspaces/other/.envrc", content), "the path is part of the token")

    base_env = {"PATH": SYSTEM_PATH, "USER": "developer", "HOME": "/home/developer"}
    empty = workspace / "empty.envrc"
    empty.write_text(":\n", encoding="utf-8")
    before = dump_env_after_sourcing(empty, workspace, base_env)  # cancels bash's own PWD, SHLVL, _ additions
    after = dump_env_after_sourcing(envrc, workspace, base_env)
    diff = build_env_diff(before, after)
    token_str = encode_diff(diff)

    print("\nEnvironment delta captured from a child bash:")
    print(f"  p (previous values): {sorted(diff['p'])}")
    print(f"  n (next values):     {sorted(diff['n'])}")
    print(f"  DIRENV_DIFF starts with: {token_str[:2]} (base64 of the zlib header 78 9c)")
    standard = base64.b64encode(compress_diff(diff)).decode("ascii")
    urlsafe_only = standard != token_str and "+" not in token_str and "/" not in token_str
    print(f"  DIRENV_DIFF uses the URL-safe alphabet ('-' and '_', never '+' or '/'): {urlsafe_only}")
    check(urlsafe_only, "the token must differ from standard base64 and avoid + and /")
    check(sorted(diff["p"]) == ["PATH", "USER"], "p must hold the modified and the removed key")
    check(sorted(diff["n"]) == ["PATH", "PROJECT_ENV"], "n must hold the added and the modified key")
    check(token_str.startswith("eJ"), "zlib then base64 must start with eJ")
    check(decode_diff(token_str) == diff, "DIRENV_DIFF roundtrip")
    print("  Roundtrip decode integrity: OK (JSON -> zlib -> URL-safe base64 -> JSON verified)")

    restored = revert(after, decode_diff(token_str))
    check(restored == before, "exit must restore the baseline environment")
    check("PROJECT_ENV" not in restored and restored["USER"] == "developer", "added key unset, removed key back")
    print("Restoration upon directory exit: OK (environment restored to baseline)")


# ---------------------------------------------------------------------------
# 2. Shims vs PATH
# ---------------------------------------------------------------------------

TOOL_BODY = """#!/bin/sh
echo "tool-cli version {ver}"
echo "tool-cli config  = ${{TOOL_CONFIG_PROFILE:-<unset>}}"
"""

SHIM_BODY = """#!{python}
import os
import sys
from pathlib import Path

version = None
for directory in [Path.cwd(), *Path.cwd().parents]:
    pin = directory / ".tool-versions"
    if pin.exists():
        for line in pin.read_text().splitlines():
            parts = line.split()
            if parts[:1] == ["tool-cli"]:
                version = parts[1]
        break
if version is None:
    sys.exit("shim: no pin for tool-cli")
target = Path("{tools}") / f"tool-cli-{{version}}" / "bin" / "tool-cli"
os.environ["TOOL_CONFIG_PROFILE"] = "attempted_from_shim"
sys.stderr.write(f"[shim-trampoline] intercepting 'tool-cli' -> dispatching to version {{version}}\\n")
os.execv(str(target), [str(target)] + sys.argv[1:])
"""


def sh(script: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", "-c", script], cwd=cwd, env=env, capture_output=True, text=True)


def lines(text: str) -> list[str]:
    return text.strip().splitlines()


def version_of(proc: subprocess.CompletedProcess[str]) -> str:
    return lines(proc.stdout)[0].removeprefix("tool-cli version ")


def test_shim_vs_path(workspace: Path) -> None:
    section("2. Runtime Tool Pinning: Shims vs Dynamic PATH")

    tools = workspace / "installed_tools"
    for ver in ("1.18.0", "2.4.0"):
        exe = tools / f"tool-cli-{ver}" / "bin" / "tool-cli"
        exe.parent.mkdir(parents=True)
        exe.write_text(TOOL_BODY.format(ver=ver), encoding="utf-8")
        exe.chmod(0o755)

    proj_a = workspace / "repo_a"
    proj_b = workspace / "repo_b"
    for proj, ver in ((proj_a, "2.4.0"), (proj_b, "1.18.0")):
        proj.mkdir()
        (proj / ".tool-versions").write_text(f"tool-cli {ver}\n", encoding="utf-8")

    shims = workspace / "shims"
    shims.mkdir()
    shim = shims / "tool-cli"
    shim.write_text(SHIM_BODY.format(python=sys.executable, tools=tools), encoding="utf-8")
    shim.chmod(0o755)

    shim_path = f"{shims}:{SYSTEM_PATH}"
    show = 'tool-cli; echo "parent shell TOOL_CONFIG_PROFILE=${TOOL_CONFIG_PROFILE:-<unset>}"'

    print("Mechanism A: Static Shim Dispatch")
    proc = sh(show, proj_a, {"PATH": shim_path})
    print(f"  Shim stderr: {proc.stderr.strip()}")
    for line in lines(proc.stdout)[:2]:
        print(f"  Shim stdout: {line}")
    print(f"  Observed by the parent: {lines(proc.stdout)[2].removeprefix('parent shell ')}")
    check(version_of(proc) == "2.4.0", "shim must dispatch to the pinned version")
    check("attempted_from_shim" in proc.stdout.splitlines()[1], "the tool sees the shim's variable")
    check(lines(proc.stdout)[2].endswith("=<unset>"), "a child must not mutate its parent's environment")
    cd_b = sh("cd ../repo_b && tool-cli", proj_a, {"PATH": shim_path})
    print(f"  Script that cd's into another project: {version_of(cd_b)}")
    check(version_of(cd_b) == "1.18.0", "a shim re-resolves the pin after cd")

    print("\nMechanism B: Dynamic PATH Modification (Prompt Hook)")
    hook_output = (
        f'export PATH="{tools}/tool-cli-2.4.0/bin:{tools}/tool-cli-1.18.0/bin:{SYSTEM_PATH}"; '
        "export TOOL_CONFIG_PROFILE=active_in_current_shell"
    )
    proc = sh(f"eval '{hook_output}'; {show}", proj_a, {"PATH": SYSTEM_PATH})
    for line in lines(proc.stdout)[:2]:
        print(f"  Direct PATH stdout: {line}")
    print(f"  Observed by the parent: {lines(proc.stdout)[2].removeprefix('parent shell ')}")
    check(version_of(proc) == "2.4.0", "the activated PATH must find the pinned version")
    check(lines(proc.stdout)[2].endswith("=active_in_current_shell"), "an eval in the shell exports into it")
    cd_b = sh(f"eval '{hook_output}'; cd ../repo_b && tool-cli", proj_a, {"PATH": SYSTEM_PATH})
    print(f"  Script that cd's into another project: {version_of(cd_b)} (the hook does not re-run)")
    check(version_of(cd_b) == "2.4.0", "without the hook, cd must not switch versions")

    print("\nMechanism C: Non-Interactive Subshell Execution (No Shell Hook)")
    default_path = f"{tools}/tool-cli-1.18.0/bin:{SYSTEM_PATH}"
    proc = sh("tool-cli", proj_a, {"PATH": default_path})
    print(f"  Unhooked subshell with default PATH only: {version_of(proc)} (misses project pin)")
    check(version_of(proc) == "1.18.0", "an unhooked process runs the PATH default")
    proc = sh("tool-cli", proj_a, {"PATH": f"{shims}:{default_path}"})
    print(f"  Unhooked subshell with shim in PATH:      {version_of(proc)} (honors project pin)")
    check(version_of(proc) == "2.4.0", "the shim must win over the PATH default")
    proc = sh("tool-cli", workspace, {"PATH": SYSTEM_PATH})
    print(f"  Unhooked subshell with no tool on PATH:   exit {proc.returncode}")
    check(proc.returncode == 127, "no tool on PATH must be command not found")


# ---------------------------------------------------------------------------
# 3. Devcontainer lifecycle
# ---------------------------------------------------------------------------

HOOK_ORDER = (
    "initializeCommand",
    "onCreateCommand",
    "updateContentCommand",
    "postCreateCommand",
    "postStartCommand",
    "postAttachCommand",
)
HOOK_CONTEXT = {"initializeCommand": "HOST"}
WAIT_FOR_ALLOWED = ("initializeCommand", "onCreateCommand", "updateContentCommand", "postCreateCommand", "postStartCommand")


def run_command(command: object, cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    """String form runs in a shell, array form is exec'd directly, object form runs every entry concurrently."""
    if isinstance(command, str):
        proc = subprocess.run(["/bin/sh", "-c", command], cwd=cwd, env=env, capture_output=True, text=True)
        return proc.returncode, proc.stdout.strip()
    if isinstance(command, list):
        proc = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
        return proc.returncode, proc.stdout.strip()
    if isinstance(command, dict):
        running = [
            subprocess.Popen(["/bin/sh", "-c", c], cwd=cwd, env=env, stdout=subprocess.PIPE, text=True)
            for c in command.values()
        ]
        outputs = [p.communicate()[0].strip() for p in running]
        # Every entry must exit 0 for the step to succeed; the first failing code is reported.
        code = next((p.returncode for p in running if p.returncode != 0), 0)
        return code, " + ".join(outputs)
    raise TypeError(f"unsupported lifecycle command type {type(command).__name__}")


def run_lifecycle(config: dict[str, object], cwd: Path, env: dict[str, str]) -> tuple[list[tuple[str, int, str]], str]:
    """Run hooks in specification order, stop at the first failure, and report where the client unblocks."""
    wait_for = str(config.get("waitFor", "updateContentCommand"))
    check(wait_for in WAIT_FOR_ALLOWED, f"waitFor {wait_for} is not allowed")
    executed: list[tuple[str, int, str]] = []
    for hook in HOOK_ORDER:
        if hook not in config:
            continue
        code, out = run_command(config[hook], cwd, env)
        executed.append((hook, code, out))
        if code != 0:
            break
    return executed, wait_for


def rendezvous(me: str, other: str) -> str:
    """Wait (bounded) for the other task's start marker; passes only if both tasks run at the same time."""
    return (
        f'touch "$D/{me}.start"; i=0; '
        f'while [ ! -f "$D/{other}.start" ] && [ $i -lt 250 ]; do sleep 0.02; i=$((i+1)); done; '
        f'[ -f "$D/{other}.start" ] && echo {me}_saw_{other}'
    )


def test_devcontainer_lifecycle(workspace: Path) -> None:
    section("3. devcontainer.json Lifecycle Hook Execution Order")

    marks = workspace / "dc"
    marks.mkdir()
    env = {"PATH": SYSTEM_PATH, "HOME": "/home/dev", "D": str(marks)}
    config: dict[str, object] = {
        "initializeCommand": "echo host-prep",
        "onCreateCommand": ["echo", "$HOME"],
        "updateContentCommand": {"a": rendezvous("a", "b"), "b": rendezvous("b", "a")},
        "postCreateCommand": "echo build",
        "postStartCommand": "echo daemon",
        "postAttachCommand": "echo attach",
    }
    executed, wait_for = run_lifecycle(config, workspace, env)

    print(f"{'Order':<5} {'Lifecycle Hook':<22} {'Context':<10} {'Exit':<5} Output")
    print(f"{'-' * 5} {'-' * 22} {'-' * 10} {'-' * 5} {'-' * 30}")
    for idx, (hook, code, out) in enumerate(executed, 1):
        print(f"{idx:<5} {hook:<22} {HOOK_CONTEXT.get(hook, 'CONTAINER'):<10} {code:<5} {out}")
    check([h for h, _, _ in executed] == list(HOOK_ORDER), "hooks must run in specification order")
    outputs = {h: out for h, _, out in executed}
    check(outputs["onCreateCommand"] == "$HOME", "array form runs without a shell, so $HOME stays literal")
    check(
        outputs["updateContentCommand"] == "a_saw_b + b_saw_a" and executed[2][1] == 0,
        "object entries must run concurrently",
    )
    print(f"Array form is exec'd without a shell: onCreateCommand printed {outputs['onCreateCommand']!r}")
    print("String form runs in /bin/sh: `echo $HOME` would print /home/dev")
    check(run_command("echo $HOME", workspace, env) == (0, "/home/dev"), "string form expands $HOME")
    print("Object form ran concurrently: each entry saw the other's start marker (a serial run times out)")

    idx = HOOK_ORDER.index(wait_for) + 1
    print(f"Client unblocked after: {wait_for} (default waitFor), hooks 1-{idx} of {len(executed)}")
    config["waitFor"] = "postCreateCommand"
    _, wait_for = run_lifecycle(config, workspace, env)
    print(f"With waitFor set: client unblocked after {wait_for}")
    check(wait_for == "postCreateCommand", "waitFor must move the barrier")
    config["waitFor"] = "postAttachCommand"
    try:
        run_lifecycle(config, workspace, env)
        rejected = False
    except SystemExit:
        rejected = True
    print(f"waitFor: postAttachCommand rejected (not in the allowed enum): {rejected}")
    check(rejected, "postAttachCommand is not an allowed waitFor value")
    config["waitFor"] = "postCreateCommand"

    config["postCreateCommand"] = "exit 3"
    executed, _ = run_lifecycle(config, workspace, env)
    ran = [h for h, _, _ in executed]
    print(f"When postCreateCommand exits 3, later hooks run: {ran[ran.index('postCreateCommand') + 1:]}")
    check(ran[-1] == "postCreateCommand" and executed[-1][1] == 3, "a failed hook must stop later hooks")

    config["updateContentCommand"] = {"ok": "true", "bad": "exit 5"}
    executed, _ = run_lifecycle(config, workspace, env)
    print(f"Object form with one failing entry: updateContentCommand exit {executed[-1][1]}, hooks run {len(executed)}")
    check(executed[-1][0] == "updateContentCommand" and executed[-1][1] == 5, "one failing entry fails the step")


def main() -> None:
    os.environ["LC_ALL"] = "C"
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()
        test_direnv(workspace)
        test_shim_vs_path(workspace)
        test_devcontainer_lifecycle(workspace)


if __name__ == "__main__":
    main()
