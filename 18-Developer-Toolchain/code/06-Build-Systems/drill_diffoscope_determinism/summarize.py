"""Summarize one drill stage: what diffoscope found, why, and what a cache would have done.

Subcommands (run inside the lab container by drill.sh):
    summarize.py toolchain
    summarize.py stage N NAME FIXES RC REPORT_JSON ROOT_A ROOT_B

Everything printed is derived from the two builds and is stable from run to run:
wall-clock values are replaced by <time-A>/<time-B>, checkout paths by $ROOT_A/$ROOT_B,
and nothing that depends on the Rust HashMap's random order (line counts, symbol names)
is printed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
# Wall-clock renderings that the two builds can leak, longest first so each match is one value:
# ctime (gzip header via file(1)), __DATE__ " " __TIME__, ISO 8601, tar and ar listings, bare parts.
CLOCK_RE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) " + MONTH + r" [ \d]\d \d\d:\d\d:\d\d \d{4}"
    r"|" + MONTH + r" [ \d]\d \d{4}(?: \d\d:\d\d:\d\d)?"
    r"|(?<!\d)\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d:\d\d(?:\.\d+)?Z?)?"
    r"|(?<!\d)\d\d:\d\d:\d\d(?!\d)"
)
COMMAND_PREFIXES = ("readelf ", "objdump ", "strings ", "filetype ", "file list", "nm ", "zipinfo ")


def toolchain() -> int:
    print("=== Toolchain (Ubuntu 24.04 lab image) ===")
    for line in Path("/etc/drill-versions").read_text().splitlines():
        print(line)
    ar_help = subprocess.run(["ar", "--help"], capture_output=True, text=True, check=False).stdout
    d_default = any("[D]" in ln and "(default)" in ln for ln in ar_help.splitlines())
    print(f"ar default mode is deterministic (D): {'yes' if d_default else 'no'}")
    print(f"gcc -dumpfullversion: {run(['gcc', '-dumpfullversion']).stdout.strip()}")
    print(f"rustc --version: {' '.join(run(['rustc', '--version']).stdout.split()[:2])}")
    print()
    print("=== Compiler probes ===")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "probe.c"
        src.write_text('const char* d = __DATE__ " " __TIME__;\nconst char* t = __TIMESTAMP__;\n'
                       'const char* f = __FILE__;\n')
        # Give the source a fixed mtime so the __TIMESTAMP__ result is stable.
        os.utime(src, (981173106, 981173106))  # 2001-02-03 04:05:06 UTC
        env = dict(os.environ, TZ="UTC", SOURCE_DATE_EPOCH="1767225600")  # 2026-01-01 00:00:00 UTC
        out = run(["gcc", "-E", "-P", str(src)], env=env, cwd=tmp).stdout
        print(f"SOURCE_DATE_EPOCH=1767225600 gives __DATE__ __TIME__: {quoted(out, 'd')}")
        print(f"  and __TIMESTAMP__ (source mtime 2001-02-03 04:05:06 UTC): {quoted(out, 't')}")
        bad = run(["gcc", "-E", "-P", str(src)], env=dict(env, SOURCE_DATE_EPOCH="yesterday"), cwd=tmp)
        print(f"SOURCE_DATE_EPOCH=yesterday: exit {bad.returncode}, "
              f"error names the variable: {'yes' if 'SOURCE_DATE_EPOCH' in bad.stderr else 'no'}")
        out = run(["gcc", "-E", "-P", f"-ffile-prefix-map={tmp}=/first", f"-ffile-prefix-map={tmp}=/second",
                   str(src)], env=env, cwd=tmp).stdout
        print(f"two -ffile-prefix-map for the same prefix, __FILE__: {quoted(out, 'f')}")
        out = run(["gcc", "-E", "-P", "probe.c"], env=env, cwd=tmp).stdout
        print(f"source named by a relative path, __FILE__: {quoted(out, 'f')}")
        run(["gcc", "-O2", "-g", f"-ffile-prefix-map={tmp}=.", "-DPROBE=1", "-c", str(src), "-o", "probe.o"],
            env=env, cwd=tmp)
        info = run(["readelf", "--debug-dump=info", "probe.o"], cwd=tmp).stdout
        producer = next((ln for ln in info.splitlines() if "DW_AT_producer" in ln), "")
        print(f"DW_AT_producer records -O2: {'yes' if ' -O2' in producer else 'no'}, "
              f"-ffile-prefix-map: {'yes' if 'prefix-map' in producer else 'no'}, "
              f"-D: {'yes' if 'PROBE' in producer else 'no'}")
        comp_dir = next((ln.rsplit(':', 1)[-1].strip() for ln in info.splitlines() if "DW_AT_comp_dir" in ln), "")
        print(f"DW_AT_comp_dir after -ffile-prefix-map=<cwd>=.: {comp_dir}")
    print()
    return 0


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def quoted(preprocessed: str, var: str) -> str:
    """The string literal(s) assigned to `var` in preprocessed output, as written."""
    m = re.search(rf"\b{var} = (.*);", preprocessed)
    return m.group(1) if m else "(not found)"


def changed_lines(diff: str) -> tuple[list[str], list[str]]:
    removed, added = [], []
    for line in diff.splitlines():
        if line.startswith(("---", "+++", "@@")):
            continue
        if line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
    return removed, added


def is_command(name: str) -> bool:
    return name.startswith(COMMAND_PREFIXES)


def leaves(node: dict, entries: list[str]):
    """Yield (member, leaf_name, unified_diff) for every node that carries a diff."""
    name = node.get("source1") or ""
    if node.get("unified_diff"):
        if name == "filetype from file(1)" and not entries:
            member = "(gzip header)"
        elif name == "file list":
            member = (" > ".join(entries) or "(top)") + " (listing)"
        elif is_command(name):
            member = " > ".join(entries)
        else:
            member = " > ".join(entries)
        yield member, name, node["unified_diff"]
    for child in node.get("details") or []:
        child_name = child.get("source1") or ""
        nested = entries if is_command(child_name) or child_name == "file list" else entries + [child_name]
        yield from leaves(child, nested)


def tidy(member: str) -> str:
    # The tarball's single member is engine.tar; drop it so member names read as paths in the package.
    return member.removeprefix("engine.tar > ").replace("engine.tar (listing)", "(tar listing)")


def evidence(removed: list[str], added: list[str], root_a: str, root_b: str) -> tuple[bool, bool]:
    path = any(root_a in ln for ln in removed) or any(root_b in ln for ln in added)
    clock_a = {m.group(0) for ln in removed for m in CLOCK_RE.finditer(ln)}
    clock_b = {m.group(0) for ln in added for m in CLOCK_RE.finditer(ln)}
    return clock_a != clock_b, path


def scrub(line: str, root: str, label: str, side: str) -> str:
    line = line.replace(root, label)
    line = CLOCK_RE.sub(f"<time-{side}>", line)
    return " ".join(line.split())[:88]


def sample(removed: list[str], added: list[str], test, root_a: str, root_b: str) -> list[str]:
    out = []
    for ln in removed:
        if test(ln, root_a):
            out.append("    - " + scrub(ln, root_a, "$ROOT_A", "A"))
            break
    for ln in added:
        if test(ln, root_b):
            out.append("    + " + scrub(ln, root_b, "$ROOT_B", "B"))
            break
    return out


def compare_generated(root_a: Path, root_b: Path, rel: str) -> str:
    a = (root_a / rel).read_text().splitlines()
    b = (root_b / rel).read_text().splitlines()
    if a == b:
        return "identical"
    if sorted(a) == sorted(b):
        return "same lines, different order"
    # Same identifiers in a different order (numbering that follows the order changes too).
    def words(lines: list[str]) -> Counter:
        return Counter(w for ln in lines for w in re.findall(r"[A-Za-z_]+", ln))
    if words(a) == words(b):
        return "same names, different order"
    return "different content"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def actions(root: Path) -> dict[str, dict]:
    table = {}
    for line in (root / "target" / "actions.tsv").read_text().splitlines():
        name, inputs, output, env, argv = line.split("\t")
        table[name] = {"inputs": inputs.split(), "output": output, "env": env, "argv": argv}
    return table


def action_key(root: Path, act: dict, normalize: bool) -> str:
    argv, env = act["argv"], act["env"]
    if normalize:
        argv, env = argv.replace(str(root), "$ROOT"), env.replace(str(root), "$ROOT")
    h = hashlib.sha256()
    for part in [argv, env] + [f"{i}={digest(root / i)}" for i in act["inputs"]]:
        h.update(part.encode() + b"\0")
    return h.hexdigest()


def cache_table(root_a: Path, root_b: Path) -> None:
    acts_a, acts_b = actions(root_a), actions(root_b)
    print("Toy action cache, agent B looking up agent A's results:")
    print(f"  {'action':<16} {'key=argv+env+inputs':<20} {'same key, root-normalized':<26} output bytes equal")
    for name in acts_a:
        a, b = acts_a[name], acts_b[name]
        raw = action_key(root_a, a, False) == action_key(root_b, b, False)
        norm = action_key(root_a, a, True) == action_key(root_b, b, True)
        same_out = digest(root_a / a["output"]) == digest(root_b / b["output"])
        print(f"  {name:<16} {'hit' if raw else 'miss':<20} {'hit' if norm else 'miss':<26} {'yes' if same_out else 'no'}")


def stage(argv: list[str]) -> int:
    n, name, fixes, rc, report, root_a, root_b = argv
    print(f"=== Stage {n}: {name} (fixes: {fixes or 'none'}) ===")
    print(f"diffoscope exit status: {rc}")
    members: dict[str, list[bool]] = {}
    samples: dict[tuple[str, str], tuple[bool, list[str]]] = {}
    report_path = Path(report)
    tests = {
        "clock": lambda ln, _root: bool(CLOCK_RE.search(ln)),
        "path": lambda ln, root: root in ln,
    }
    if rc == "1":
        tree = json.loads(report_path.read_text())
        for member, leaf, diff in leaves(tree, []):
            member = tidy(member)
            removed, added = changed_lines(diff)
            clock, path = evidence(removed, added, root_a, root_b)
            flags = members.setdefault(member, [False, False])
            flags[0] |= clock
            flags[1] |= path
            # One sample per member and cause; strings(1) output reads best, so it wins over other leaves.
            from_strings = leaf.startswith("strings ")
            for cause, hit in (("clock", clock), ("path", path)):
                old = samples.get((member, cause))
                if hit and (old is None or (from_strings and not old[0])):
                    samples[(member, cause)] = (from_strings, sample(removed, added, tests[cause], root_a, root_b))
    elif report_path.exists() and report_path.stat().st_size:
        print("unexpected: diffoscope wrote a report although it found no differences")
        return 1
    print(f"Differing members: {len(members)}")
    if members:
        print(f"  {'member':<34} {'clock':<6} {'path':<5}")
        for member in sorted(members):
            clock, path = members[member]
            print(f"  {member:<34} {'yes' if clock else '-':<6} {'yes' if path else '-':<5}")
    if samples:
        print("Evidence (first changed line per member and cause):")
        for member, cause in sorted(samples):
            print(f"  [{cause}] {member}")
            print("\n".join(samples[(member, cause)][1]))
    ra, rb = Path(root_a), Path(root_b)
    print("Generated sources:")
    for rel in ("gen/opcodes.h", "target/out/routes.rs"):
        print(f"  {rel:<22} {compare_generated(ra, rb, rel)}")
    cache_table(ra, rb)
    same = digest(ra / "dist/engine.tar.gz") == digest(rb / "dist/engine.tar.gz")
    print(f"Release tarball sha256 equal: {'yes' if same else 'no'}")
    if same != (rc == "0"):
        print("unexpected: diffoscope and sha256 disagree")
        return 1
    print()
    return 0


def main(argv: list[str]) -> int:
    if argv[1:2] == ["toolchain"]:
        return toolchain()
    if argv[1:2] == ["stage"] and len(argv) == 9:
        return stage(argv[2:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
