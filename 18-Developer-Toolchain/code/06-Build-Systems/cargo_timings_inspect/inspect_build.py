#!/usr/bin/env python3
"""Drive Cargo against the lab workspace and print derived, deterministic facts.

Every fact is computed from something Cargo or rustc reported: the JSON
`compiler-artifact` messages on stdout, the `-v` command lines and `Dirty`
reasons on stderr, the `UNIT_DATA` table embedded in the `--timings` HTML
report, and the files left in the target directory.
Absolute times, hashes, and paths are never printed, only comparisons.

Usage: inspect_build.py <scratch-dir>   (run.sh creates and removes it)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

LAB = Path(__file__).resolve().parent
JOBS = "4"
HEAVY_FUNCTIONS = 6000
# UNIT_DATA rounds times to centiseconds, so comparisons allow one tick each way.
TICK = 0.011


@dataclass
class Run:
    stdout: str
    stderr: str


class Workspace:
    def __init__(self, root: Path, env: dict[str, str]) -> None:
        self.root = root
        self.env = env

    def cargo(self, *args: str, extra_env: dict[str, str] | None = None) -> Run:
        env = dict(self.env)
        env.update(extra_env or {})
        cmd = ["cargo", *args]
        p = subprocess.run(cmd, cwd=self.root, env=env, capture_output=True, text=True, check=False)
        if p.returncode != 0:
            sys.stderr.write(f"FAILED: {' '.join(cmd)}\n{p.stderr[-4000:]}\n")
            sys.exit(1)
        return Run(p.stdout, p.stderr)

    def build(self, *args: str, extra_env: dict[str, str] | None = None) -> Run:
        return self.cargo("build", "--offline", "-j", JOBS, "-v", "--message-format=json", *args,
                          extra_env=extra_env)

    def run_bin(self, name: str) -> str:
        p = subprocess.run([str(self.root / "target" / "debug" / name)], capture_output=True, text=True,
                           check=True)
        return p.stdout.strip()


# ---------------------------------------------------------------------------
# Parsers for what Cargo reports.


@dataclass
class Artifact:
    package: str
    kind: str
    crate_types: list[str]
    features: list[str]
    fresh: bool
    filenames: list[str]


def artifacts(run: Run) -> list[Artifact]:
    out = []
    for line in run.stdout.splitlines():
        if not line.startswith("{"):
            continue
        msg = json.loads(line)
        if msg.get("reason") != "compiler-artifact":
            continue
        out.append(Artifact(
            package=Path(msg["manifest_path"]).parent.name,
            kind=msg["target"]["kind"][0],
            crate_types=msg["target"]["crate_types"],
            features=msg["features"],
            fresh=msg["fresh"],
            filenames=[Path(f).name for f in msg["filenames"]],
        ))
    return out


def label(a: Artifact) -> str:
    feats = ",".join(a.features) if a.features else "-"
    return f"{a.package}/{a.kind}[{feats}]"


def compiled(run: Run) -> list[str]:
    return sorted(label(a) for a in artifacts(run) if not a.fresh)


def fresh(run: Run) -> list[str]:
    return sorted(label(a) for a in artifacts(run) if a.fresh)


RUSTC_LINE = re.compile(r"Running `\S*rustc --crate-name (\S+) (.*)`$")


def rustc_lines(run: Run) -> dict[str, str]:
    """Map crate name to its rustc command line (last one wins)."""
    lines = {}
    for line in run.stderr.splitlines():
        m = RUSTC_LINE.search(line.strip())
        if m:
            lines[m.group(1)] = m.group(2)
    return lines


def emit_of(cmdline: str) -> str:
    m = re.search(r"--emit=(\S+)", cmdline)
    return m.group(1) if m else "?"


SCRIPT_RUN = re.compile(r"Running `[^`]*/build/([A-Za-z0-9_]+)-[0-9a-f]+/build-script-build`")


def script_runs(run: Run) -> list[str]:
    return sorted({m.group(1) for m in SCRIPT_RUN.finditer(run.stderr)})


DIRTY = re.compile(r"^\s*Dirty (\S+) v\S+ \([^)]*\): (.*)$")


def dirty(run: Run, root: Path) -> list[str]:
    out = []
    for line in run.stderr.splitlines():
        m = DIRTY.match(line)
        if not m:
            continue
        reason = m.group(2).replace(str(root) + "/", "")
        # Drop the "(<mtime>, <delta> after last build at <time>)" suffix.
        reason = re.sub(r" \([^()]*after last build[^()]*\)$", "", reason)
        out.append(f"{m.group(1)}: {reason}")
    return out


def unit_data(root: Path) -> tuple[dict[str, dict], dict[int, str]]:
    """Return UNIT_DATA keyed by crate name, plus the index-to-name map its unblock lists use."""
    html = (root / "target" / "cargo-timings" / "cargo-timing.html").read_text()
    m = re.search(r"const UNIT_DATA = (\[.*?\]);\n", html, re.S)
    if not m:
        sys.exit("no UNIT_DATA in the timings report")
    units = json.loads(m.group(1))
    return {u["name"]: u for u in units}, {u["i"]: u["name"] for u in units}


def rmeta_time(u: dict) -> float | None:
    """Offset within the unit at which codegen began, which is when the rmeta was written."""
    for name, sec in u.get("sections") or []:
        if name == "codegen":
            return sec["start"]
    return None


def yn(flag: bool) -> str:
    return "yes" if flag else "no"


def bump_mtime(path: Path, seconds_ahead: float = 5.0) -> None:
    """Set a future mtime so coarse filesystem clocks cannot hide the change."""
    t = time.time() + seconds_ahead
    os.utime(path, (t, t))


# ---------------------------------------------------------------------------
# Lab sections.


def generate_heavy(root: Path) -> None:
    lines = []
    for i in range(HEAVY_FUNCTIONS):
        lines.append(
            f"#[inline(never)] pub fn f{i}(x: u64) -> u64 {{ let mut a = x ^ {i}; "
            f"for k in 0..(x % 7) {{ a = a.wrapping_mul(6364136223846793005).wrapping_add(k ^ {i}); }} a }}"
        )
    lines.append("pub fn total(x: u64) -> u64 {\n    let mut s = 0u64;")
    lines.extend(f"    s = s.wrapping_add(f{i}(x));" for i in range(HEAVY_FUNCTIONS))
    lines.append("    s\n}")
    (root / "heavy" / "src" / "lib.rs").write_text("\n".join(lines) + "\n")


def describe_units(run: Run) -> None:
    cmd = rustc_lines(run)
    for a in sorted(artifacts(run), key=lambda a: ["heavy", "mid", "app"].index(a.package)):
        exts = sorted({Path(f).suffix or "(executable)" for f in a.filenames})
        crate = a.package
        print(f"  {a.package:5s} kind={a.kind:3s} crate-types={','.join(a.crate_types):10s} "
              f"--emit={emit_of(cmd.get(crate, '')):24s} outputs={','.join(exts)}")


def pipeline_facts(root: Path, title: str) -> None:
    units, by_index = unit_data(root)
    heavy, mid, app = units["heavy"], units["mid"], units["app"]
    heavy_end = heavy["start"] + heavy["duration"]
    rmeta = rmeta_time(heavy)
    print(f"  [{title}] facts from target/cargo-timings/cargo-timing.html UNIT_DATA:")
    if rmeta is None:
        print("  heavy reported an rmeta time: no")
    else:
        heavy_rmeta = heavy["start"] + rmeta
        print(f"  heavy wrote its .rmeta before it finished:      {yn(rmeta < heavy['duration'] - TICK)}")
        print(f"  mid started no earlier than heavy's .rmeta:     {yn(mid['start'] >= heavy_rmeta - TICK)}")
    print(f"  mid started before heavy finished:             {yn(mid['start'] < heavy_end - TICK)}")
    print(f"  app started no earlier than heavy finished:    {yn(app['start'] >= heavy_end - TICK)}")
    names = lambda ids: ",".join(sorted(by_index[i] for i in ids)) or "(none)"  # noqa: E731
    print(f"  units heavy unblocked at its .rmeta:           {names(heavy['unblocked_rmeta_units'])}")
    print(f"  units heavy unblocked when it finished:        {names(heavy['unblocked_units'])}")


def section_pipelining(base: Path, env: dict[str, str]) -> Workspace:
    print("== 1. Pipelining: lib -> lib edges wait for .rmeta, linking waits for .rlib ==")
    ws = Workspace(base / "ws_a", env)
    run = ws.build("-p", "app", "--timings")
    describe_units(run)
    pipeline_facts(ws.root, "rlib mid")
    print(f"  app prints: {ws.run_bin('app')}")
    print()

    print("== 2. Same graph, mid gains crate-type cdylib (a Python-extension shape) ==")
    wb = Workspace(base / "ws_b", env)
    manifest = wb.root / "mid" / "Cargo.toml"
    manifest.write_text(manifest.read_text().replace(
        "[dependencies]", '[lib]\ncrate-type = ["rlib", "cdylib"]\n\n[dependencies]'))
    run = wb.build("-p", "app", "--timings")
    describe_units(run)
    pipeline_facts(wb.root, "rlib+cdylib mid")
    print()

    print("== 3. Freshness: a no-op rebuild, then one edited source file ==")
    run = ws.build("-p", "app")
    print(f"  no-op rebuild compiled: {compiled(run) or '(nothing)'}; fresh: {fresh(run)}")
    bump_mtime(ws.root / "heavy" / "src" / "lib.rs")
    run = ws.build("-p", "app")
    print(f"  after touching heavy/src/lib.rs compiled: {compiled(run)}")
    for line in dirty(run, ws.root):
        print(f"    Dirty {line}")
    print()
    return ws


def section_features(ws: Workspace) -> None:
    print("== 4. Feature unification: what `-p` selects decides what gets built ==")
    run = ws.build("-p", "fa")
    print(f"  build -p fa       compiled: {compiled(run)}")
    print(f"    fa prints: {ws.run_bin('fa')}")
    run = ws.build("-p", "fb")
    print(f"  build -p fb       compiled: {compiled(run)}")
    run = ws.build("-p", "fa")
    print(f"  build -p fa again compiled: {compiled(run) or '(nothing)'}")
    run = ws.build("-p", "fa", "-p", "fb")
    print(f"  build -p fa -p fb compiled: {compiled(run)}")
    print(f"                    fresh:    {fresh(run)}")
    print(f"    fa prints: {ws.run_bin('fa')}")
    print(f"    fb prints: {ws.run_bin('fb')}")
    deps = ws.root / "target" / "debug" / "deps"
    copies = sorted(p.name for p in deps.glob("libcommon-*.rlib"))
    print(f"  distinct libcommon-<hash>.rlib files in target/debug/deps: {len(copies)}")
    print()


def section_unhashed(ws: Workspace) -> None:
    print("== 5. Artifacts without a filename hash rebuild on every flip ==")
    for feats in ("fast", "", "fast", ""):
        args = ["-p", "ext"] + ([f"--features={feats}"] if feats else [])
        run = ws.build(*args)
        reasons = dirty(run, ws.root) or ["(fresh)"]
        print(f"  features=[{feats:4s}] ext compiled: {yn(bool(compiled(run)))}; {'; '.join(reasons)}")
    names = sorted({f for a in artifacts(run) for f in a.filenames})
    print(f"  ext output names carry a -<hash> suffix: {yn(any('-' in n for n in names))} ({', '.join(names)})")
    flag = "-Cdebug-assertions=off"
    for rf in (flag, "", flag, ""):
        run = ws.build("-p", "fb", "-p", "ext", extra_env={"RUSTFLAGS": rf})
        shown = rf or "(unset)"
        print(f"  RUSTFLAGS={shown:22s} compiled: {compiled(run) or '(nothing)'}")
    print()


def section_build_scripts(ws: Workspace) -> None:
    print("== 6. Build script rerun rules ==")
    ws.build("-p", "script_old", "-p", "script_new")
    bump_mtime(ws.root / "script_old" / "notes.txt")
    bump_mtime(ws.root / "script_new" / "notes.txt")
    run = ws.build("-p", "script_old", "-p", "script_new")
    print(f"  touched notes.txt in both: build scripts run: {script_runs(run) or '(none)'}")
    print(f"    compiled: {compiled(run)}")
    for line in dirty(run, ws.root):
        print(f"    Dirty {line}")
    env = {"LAB_FLAVOR": "spicy"}
    run = ws.build("-p", "script_old", "-p", "script_new", extra_env=env)
    print(f"  LAB_FLAVOR=spicy:          build scripts run: {script_runs(run) or '(none)'}")
    for line in dirty(run, ws.root):
        print(f"    Dirty {line}")
    run = ws.build("-p", "script_old", "-p", "script_new", extra_env=env)
    print(f"  LAB_FLAVOR=spicy again:    build scripts run: {script_runs(run) or '(none)'}")
    print()


def section_native(ws: Workspace, base: Path) -> None:
    print("== 7. Handoff to the C toolchain: links, DEP_<links>_<key>, static archives ==")
    run = ws.build("-p", "cuser")
    print(f"  build scripts run: {script_runs(run)}")
    cmd = rustc_lines(run).get("cshim", "")
    print(f"  cshim rustc line has -L native=<OUT_DIR>: {yn('-L native=' in cmd)}")
    print(f"  cshim rustc line has -l static=shim:      {yn('-l static=shim' in cmd)}")
    rlib = next((ws.root / "target" / "debug" / "deps").glob("libcshim-*.rlib"))
    members = subprocess.run(["ar", "t", str(rlib)], capture_output=True, text=True, check=True).stdout.split()
    print(f"  shim.o bundled into the cshim .rlib:      {yn('shim.o' in members)}")
    print(f"  cuser prints: {ws.run_bin('cuser')}")

    print("  -- Rust staticlib consumed by a C++ link --")
    run = ws.cargo("rustc", "--offline", "-p", "rstatic", "--", "--print", "native-static-libs")
    m = re.search(r"native-static-libs: (.*)", run.stderr)
    libs = m.group(1).split() if m else []
    print(f"  rustc --print native-static-libs: {' '.join(libs) or '(none)'}")
    exe = base / "main_cpp"
    cxx = os.environ.get("CXX", "c++")
    subprocess.run([cxx, str(LAB / "cpp" / "main.cpp"), str(ws.root / "target" / "debug" / "librstatic.a"),
                    *libs, "-o", str(exe)], check=True, capture_output=True, text=True)
    print(f"  C++ program prints: {subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout.strip()}")


def main() -> None:
    base = Path(sys.argv[1]).resolve()
    env = dict(os.environ)
    env["CARGO_HOME"] = str(base / "cargo-home")
    env["CARGO_TERM_COLOR"] = "never"
    for var in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER",
                "CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR", "CARGO_BUILD_BUILD_DIR", "CARGO_INCREMENTAL",
                "CARGO_BUILD_JOBS", "CARGO_LOG", "LAB_FLAVOR"):
        env.pop(var, None)

    version = subprocess.run(["cargo", "--version"], capture_output=True, text=True, env=env,
                             check=True).stdout.split()[1]
    print(f"cargo {'.'.join(version.split('.')[:2])}, jobs={JOBS}, heavy has {HEAVY_FUNCTIONS} functions")
    print()

    src = LAB / "ws"
    shutil.copytree(src, base / "ws_a")
    generate_heavy(base / "ws_a")
    shutil.copytree(base / "ws_a", base / "ws_b")

    ws = section_pipelining(base, env)
    section_features(ws)
    section_unhashed(ws)
    section_build_scripts(ws)
    section_native(ws, base)


if __name__ == "__main__":
    main()
