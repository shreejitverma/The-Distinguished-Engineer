"""Run uv (pubgrub-rs), pip (resolvelib), and go (MVS) on the lab's inputs, offline.

usage: crosscheck.py WORKDIR MVS_JSON

uv and pip read hand-built pure-Python wheels from a local directory with
--no-index, so no network is touched.  go reads local modules through
directory replace directives with GOPROXY=off.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from typing import Dict, List

from lab import EXAMPLES, check
from pubgrub import Range, fmt_version, parse_constraint

# uv draws its report with box characters; print them as ASCII.
GLYPHS = {"\u00d7": "x", "\u2570": "\\", "\u2500": "-", "\u25b6": ">", "\u2502": "|"}


def ascii_only(text: str) -> str:
    for k, v in GLYPHS.items():
        text = text.replace(k, v)
    text.encode("ascii")  # raises if an unexpected non-ASCII character remains
    return text


def pep440(rng: Range) -> str:
    check(len(rng.iv) == 1, "lab wheels support single-interval constraints only")
    lo, hi = rng.iv[0]
    if lo is not None and hi == (lo[0], lo[1], lo[2] + 1):
        return "==" + fmt_version(lo)
    parts = []
    if lo is not None:
        parts.append(">=" + fmt_version(lo))
    if hi is not None:
        parts.append("<" + fmt_version(hi))
    return ",".join(parts)


def write_wheels(spec: Dict, outdir: str) -> List[str]:
    os.makedirs(outdir)
    for pkg, versions in spec.items():
        if pkg == "root":
            continue
        for ver, deps in versions.items():
            dist = "%s-%s.dist-info" % (pkg, ver)
            meta = "Metadata-Version: 2.1\nName: %s\nVersion: %s\n" % (pkg, ver)
            meta += "".join("Requires-Dist: %s%s\n" % (d, pep440(parse_constraint(c))) for d, c in deps.items())
            wheel = "Wheel-Version: 1.0\nGenerator: pubgrub-lab\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
            path = os.path.join(outdir, "%s-%s-py3-none-any.whl" % (pkg, ver))
            with zipfile.ZipFile(path, "w") as z:
                for name, body in (("METADATA", meta), ("WHEEL", wheel)):
                    info = zipfile.ZipInfo("%s/%s" % (dist, name), date_time=(2026, 11, 15, 0, 0, 0))
                    z.writestr(info, body)
                record = "".join("%s/%s,,\n" % (dist, n) for n in ("METADATA", "WHEEL", "RECORD"))
                z.writestr(zipfile.ZipInfo("%s/RECORD" % dist, date_time=(2026, 11, 15, 0, 0, 0)), record)
    return ["%s%s" % (d, pep440(parse_constraint(c))) for d, c in spec["root"]["1.0.0"].items()]


def run(cmd: List[str], cwd: str, env: Dict[str, str]) -> subprocess.CompletedProcess:
    full = dict(os.environ)
    full.update(env)
    return subprocess.run(cmd, cwd=cwd, env=full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)


def section_uv_pip(work: str) -> None:
    print("== 5. The same inputs through uv (pubgrub-rs) and pip (resolvelib) ==")
    uv_env = {"UV_CACHE_DIR": os.path.join(work, "uv-cache"), "UV_NO_CONFIG": "1", "NO_COLOR": "1"}
    ver = run(["uv", "--version"], work, uv_env).stdout.split()
    print("  uv %s" % ver[1])
    venv = os.path.join(work, "venv")
    subprocess.run([sys.executable, "-m", "venv", venv], check=True, stdout=subprocess.DEVNULL)
    pip = [os.path.join(venv, "bin", "python"), "-m", "pip"]
    pip_env = {"PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_CACHE_DIR": "1", "PIP_CONFIG_FILE": os.devnull,
               "PIP_NO_INPUT": "1"}
    print("  pip %s" % run(pip + ["--version"], work, pip_env).stdout.split()[1])
    for i, (title, spec, expect) in enumerate(EXAMPLES):
        case = os.path.join(work, "case%d" % i)
        os.makedirs(case)
        reqs = write_wheels(spec, os.path.join(case, "w"))
        with open(os.path.join(case, "req.in"), "w") as fh:
            fh.write("".join(r + "\n" for r in reqs))
        uv = run(["uv", "pip", "compile", "--no-index", "--find-links", "w", "--offline", "--python-version", "3.12",
                  "--no-header", "--no-annotate", "req.in"], case, uv_env)
        print()
        print("-- %s --" % title)
        if isinstance(expect, dict):
            pins = sorted(l.strip() for l in uv.stdout.splitlines() if "==" in l)
            want = sorted("%s==%s" % (p, v) for p, v in expect.items() if p != "root")
            check(uv.returncode == 0 and pins == want, "uv disagrees on %s: %s" % (title, pins))
            print("  uv agrees: %s" % " ".join(pins))
            continue
        check(uv.returncode != 0, "uv solved an unsatisfiable example")
        print("  uv (exit %d):" % uv.returncode)
        for line in ascii_only(uv.stdout).rstrip().splitlines():
            print("    " + line)
        if i == len(EXAMPLES) - 2:  # pip on the linear example only
            p = run(pip + ["install", "--dry-run", "--no-index", "--find-links", "w", "-r", "req.in"], case, pip_env)
            check(p.returncode != 0, "pip solved an unsatisfiable example")
            lines = p.stdout.splitlines()
            start = next(k for k, l in enumerate(lines) if l.startswith("ERROR: Cannot install"))
            end = next(k for k in range(start + 2, len(lines)) if not lines[k].strip())
            print("  pip (exit %d), from its ResolutionImpossible report:" % p.returncode)
            for line in lines[start:end]:
                print("    " + line)
    print()


def gomod(path: str, module: str, go: str, requires: List[str], extra: str = "") -> None:
    os.makedirs(path)
    with open(os.path.join(path, "go.mod"), "w") as fh:
        fh.write("module %s\n\ngo %s\n" % (module, go))
        for r in requires:
            fh.write("require %s\n" % r)
        fh.write(extra)


def section_go(work: str, mvs: Dict[str, str]) -> None:
    print("== 6. The same module graph through go list -m all ==")
    env = {"GOTOOLCHAIN": "local", "GOPROXY": "off", "GOFLAGS": "-mod=mod", "GOWORK": "off", "GOENV": "off",
           "GOCACHE": os.path.join(work, "gocache"), "GOMODCACHE": os.path.join(work, "gomodcache")}
    print("  %s" % run(["go", "version"], work, env).stdout.split()[2])
    deps = {
        "a12": ("A", ["example.com/c v1.3.0"]),
        "b12": ("B", ["example.com/c v1.4.0"]),
        "b13": ("B", ["example.com/c v1.4.0"]),
        "c13": ("C", ["example.com/d v1.2.0"]),
        "c14": ("C", ["example.com/d v1.2.0"]),
        "d12": ("D", []),
        "d13": ("D", []),
    }
    replaces = "".join("replace example.com/%s v1.%s.0 => ../%s\n" % (d[0].lower(), d[2], d) for d in sorted(deps))
    cases = [
        ("dependencies at go 1.16 (full graph)", "1.16", "", mvs["plain"]),
        ("dependencies at go 1.21 (pruned graph)", "1.21", "", None),
        ("go 1.16 graph, main module excludes C v1.4.0", "1.16", "exclude example.com/c v1.4.0\n", mvs["exclude"]),
    ]
    for k, (title, depgo, extra, want) in enumerate(cases):
        root = os.path.join(work, "go%d" % k)
        for d, (mod, reqs) in deps.items():
            gomod(os.path.join(root, d), "example.com/%s" % mod.lower(), depgo, reqs)
        gomod(os.path.join(root, "main"), "example.com/main", "1.21",
              ["example.com/a v1.2.0", "example.com/b v1.2.0"], replaces + extra)
        out = run(["go", "list", "-m", "all"], os.path.join(root, "main"), env)
        check(out.returncode == 0, "go list failed: %s" % out.stdout)
        mods = [l.split() for l in out.stdout.splitlines()[1:]]
        got = " ".join("%s %s" % (m[0].split("/")[1].upper(), m[1][1:].rsplit(".", 1)[0]) for m in mods)
        print("  %-46s %s" % (title + ":", got))
        if want is not None:
            check(got == want, "go disagrees with lab MVS: %s vs %s" % (got, want))
        else:
            check("D" not in got.split(), "graph pruning did not drop D")
    print("  lab MVS and go agree on the full-graph and exclude cases")


def main() -> None:
    work = sys.argv[1]
    with open(sys.argv[2]) as fh:
        mvs = json.load(fh)
    section_uv_pip(work)
    section_go(work, mvs)


if __name__ == "__main__":
    main()
