"""Stages 1, 6, and 7 of the drill: the same registry through the real uv resolver.

Usage:
    python3 uv_stage.py symptom WORKDIR
    python3 uv_stage.py verify WORKDIR FIXES_JSON

WORKDIR holds the wheels from make_wheels.py. uv runs offline, against a flat
--find-links directory, with --config-file pointing at a file this script writes,
which per the uv docs replaces every discovered configuration file, so a user's
uv.toml cannot change the result. Output is scrubbed of timings, and uv's three
non-ASCII glyphs are mapped to the ASCII forms the uv documentation prints.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from registry import ROOT_DEPS

GLYPHS = {"\u00d7": "x", "\u2570\u2500\u25b6": "`->"}
EDGE_RE = re.compile(r"([a-z][a-z0-9-]*)(?:\{[^}]*\})?[<>=!~,.0-9 ]* depends on ([a-z][a-z0-9-]*)")


def env() -> dict[str, str]:
    e = {k: v for k, v in os.environ.items() if not k.startswith("UV_")}
    e.update(NO_COLOR="1", COLUMNS="80", UV_PYTHON_DOWNLOADS="never")
    return e


def scrub(text: str) -> str:
    for glyph, ascii_ in GLYPHS.items():
        text = text.replace(glyph, ascii_)
    text = re.sub(r" in [0-9.]+(?:ms|s)\b", " in <time>", text)
    bad = sorted({c for c in text if ord(c) > 127})
    if bad:
        raise SystemExit(f"uv printed unmapped non-ASCII characters: {bad!r}")
    return text.rstrip("\n")


def uv_compile(work: Path, platform: str, req_lines: list[str], config: str = "",
               extra: list[str] | None = None, verbose: bool = False, tag: str = "x"):
    req = work / f"req-{tag}.in"
    req.write_text("\n".join(req_lines) + "\n", encoding="ascii")
    cfg = work / f"uv-{tag}.toml"
    cfg.write_text(config, encoding="ascii")
    cmd = ["uv", "pip", "compile", req.name, "--config-file", cfg.name, "--no-index",
           "--find-links", "wheels", "--offline", "--cache-dir", "cache", "--python", sys.executable,
           "--no-header", "--no-annotate"]
    cmd += ["--universal"] if platform == "universal" else ["--python-platform", platform]
    cmd += extra or []
    if verbose:
        cmd.append("-v")
    p = subprocess.run(cmd, cwd=work, env=env(), capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout, p.stderr


def pins(stdout: str) -> dict[str, str]:
    out = {}
    for line in stdout.splitlines():
        m = re.match(r"^([a-z0-9-]+)==([0-9.]+)", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def root_lines(replace: dict[str, str] | None = None) -> list[str]:
    replace = replace or {}
    return [f"{n}{replace.get(n, s)}" for n, s, _ in ROOT_DEPS]


def symptom(work: Path) -> int:
    print("=== Stage 1: uv resolves the same registry per platform (the observed symptom) ===")
    seen = {}
    for plat in ("macos", "linux", "universal"):
        code, out, err = uv_compile(work, plat, root_lines(), tag=f"sym-{plat}")
        text = scrub(err + out)  # on failure stdout is empty; on success stderr is the summary line
        flag = "--universal" if plat == "universal" else f"--python-platform {plat}"
        print(f"--- uv pip compile {flag} -> exit {code} ---")
        if plat == "universal":
            print(f"explanation identical to the linux one: {'yes' if text == seen['linux'] else 'no'}")
            if text != seen["linux"]:
                print(text)
        else:
            print(text)
        seen[plat] = text
    return 0


def scoped_override(fix: dict) -> str:
    req = f"{fix['dep']}{fix['spec']}"
    if fix["marker"]:
        req += f" ; sys_platform == '{fix['marker']}'"
    rows = [
        f'    {{ package = {{ name = "{fix["depender"]}", version = "{v}" }}, dependencies = ["{req}"] }},'
        for v in fix["versions"]
    ]
    return "override-dependencies = [\n" + "\n".join(rows) + "\n]\n"


def verify(work: Path, fixes_path: Path) -> int:
    fixes = json.loads(fixes_path.read_text(encoding="ascii"))
    failures = 0

    print("=== Stage 6: uv's proof names the same dependency edges as the toy proof ===")
    code, _, err = uv_compile(work, "linux", root_lines(), tag="edges")
    flat = " ".join(scrub(err).split())
    # Keep premises only: drop each "we can conclude that ..." sentence and "we know from (N) that ..." clause.
    flat = re.sub(r"we can conclude that .*?\.(?=\s|$)", "", flat)
    flat = re.sub(r"we know from \(\d+\) that .*?,", "", flat)
    uv_edges = sorted(set(EDGE_RE.findall(flat)))
    toy_edges = sorted({(f["depender"], f["dep"]) for f in fixes if f["owner"] == "third-party"})
    print(f"uv exit {code}; edges in uv's explanation: {', '.join(f'{a}->{b}' for a, b in uv_edges)}")
    print(f"edges in the toy proof:              {', '.join(f'{a}->{b}' for a, b in toy_edges)}")
    same = uv_edges == toy_edges
    failures += not same
    print(f"same edge set: {'yes' if same else 'NO'}")

    print("=== Stage 7: replay every relaxation through uv on linux ===")
    for i, fix in enumerate(fixes):
        if fix["owner"] == "project":
            how = f"edit requirement to {fix['dep']}{fix['spec']}"
            code, out, err = uv_compile(work, "linux", root_lines({fix["dep"]: fix["spec"]}), tag=f"fix{i}")
        else:
            how = (f"scoped override of {fix['depender']} {','.join(fix['versions'])}: "
                   f"{fix['dep']}{fix['spec']}")
            code, out, err = uv_compile(work, "linux", root_lines(), config=scoped_override(fix), tag=f"fix{i}")
        got = pins(out)
        agree = code == 0 and got == fix["linux"]
        failures += not agree
        print(f"{how}")
        print(f"  uv exit {code}: {' '.join(f'{k}=={v}' for k, v in sorted(got.items())) or scrub(err)}")
        print(f"  matches the toy solver: {'yes' if agree else 'NO'}")

    print("=== Stage 8: an unscoped override erases the marker ===")
    for label, line in (("global, no marker", "wire-native>=1.0.0"),
                        ("global, marker kept", "wire-native>=1.0.0 ; sys_platform == 'linux'")):
        ov = work / "override.txt"
        ov.write_text(line + "\n", encoding="ascii")
        code, out, _ = uv_compile(work, "macos", root_lines(), extra=["--override", ov.name], tag="ov")
        got = pins(out)
        print(f"{label:20} macos exit {code}: {' '.join(f'{k}=={v}' for k, v in sorted(got.items()))}")
        print(f"{'':20} wire-native installed on macOS: {'yes' if 'wire-native' in got else 'no'}")

    print("=== Stage 9: uv's own resolver log for the failing linux resolve ===")
    code, _, err = uv_compile(work, "linux", root_lines(), verbose=True, tag="log")
    keep = re.compile(r"(Selecting: |Recording |No compatible version|too many conflicts|cannot backtrack)")
    lines = [re.sub(r"^.*?DEBUG ", "", ln) for ln in scrub(err).splitlines() if "DEBUG" in ln and keep.search(ln)]
    for ln in lines[:16]:
        print(ln)
    print(f"... {len(lines)} resolver events in total:"
          f" {sum('Selecting: ' in ln for ln in lines)} selections,"
          f" {sum('Recording ' in ln for ln in lines)} recorded conflicts,"
          f" {sum('too many conflicts' in ln for ln in lines)} priority changes")

    if failures:
        print(f"FAILED: {failures} disagreement(s) between uv and the toy solver")
        return 1
    print("uv and the toy solver agree on every relaxation")
    return 0


def main() -> int:
    mode, work = sys.argv[1], Path(sys.argv[2])
    if mode == "symptom":
        return symptom(work)
    if mode == "verify":
        return verify(work, Path(sys.argv[3]))
    raise SystemExit(f"unknown mode {mode!r}")


if __name__ == "__main__":
    raise SystemExit(main())
