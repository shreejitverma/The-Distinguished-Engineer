"""Drive one Bazel server through Bzlmod resolution, the three phases, and incremental rebuilds.

Usage: python3 drive_bazel.py <lab_dir> <scratch_dir>

The scratch directory receives a copy of ws/ and a private output base, so every run starts
from a cold server and an empty action cache.
Everything printed is a derived fact: temp paths, configuration hashes, action keys, and
object identities are replaced by stable placeholders, and timings are never printed.
Any unexpected Bazel failure raises, so a broken mechanism fails the lab instead of printing less.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

LAB = Path(sys.argv[1]).resolve()
SCRATCH = Path(sys.argv[2]).resolve()
WS = SCRATCH / "ws"
OUTPUT_BASE = SCRATCH / "ob"
EXPLAIN = SCRATCH / "explain.txt"
PROFILE = SCRATCH / "profile.json"

STARTUP = [
    "bazel",
    "--ignore_all_rc_files",
    f"--output_base={OUTPUT_BASE}",
    "--max_idle_secs=300",
]
COMMON = [
    f"--registry=file://{LAB / 'registry'}",
    "--registry=https://bcr.bazel.build",
    "--curses=no",
    "--color=no",
    "--noshow_progress",
]
BUILD_FLAGS = ["--worker_max_instances=Normalize=1"]

_cfg_names: dict[str, str] = {}


def scrub(text: str) -> str:
    """Replace run-specific strings with stable placeholders."""
    for root, name in ((str(SCRATCH), "<SCRATCH>"), (str(LAB), "<LAB>")):
        text = text.replace("/private" + root, name).replace(root, name)
    text = re.sub(r"ArtifactNestedSetKey\[(\d+)\]@\d+", r"ArtifactNestedSetKey[\1]@<id>", text)

    def cfg(match: re.Match[str]) -> str:
        full = match.group(0)
        for known, label in _cfg_names.items():
            if known.startswith(full) or full.startswith(known):
                return label
        label = f"<cfg{len(_cfg_names) + 1}>"
        _cfg_names[full] = label
        return label

    # Configuration IDs: full SHA-256 in keys, 7-hex prefixes in cquery output.
    text = re.sub(r"(?<=BuildConfigurationKey\[)[0-9a-f]{64}(?=\])", cfg, text)
    text = re.sub(r"(?<=\()[0-9a-f]{7}(?=\))", cfg, text)
    return text


def bazel(*args: str, check: bool = True, stdout_only: bool = False) -> str:
    cmd, rest = args[0], list(args[1:])
    flags = COMMON + (BUILD_FLAGS if cmd in ("build", "aquery", "cquery") else [])
    if cmd in ("dump", "info", "shutdown"):
        flags = []
    proc = subprocess.run(
        STARTUP + [cmd] + flags + rest,
        cwd=WS,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if check and proc.returncode != 0:
        raise SystemExit(f"bazel {cmd} failed ({proc.returncode}):\n{proc.stderr[-4000:]}")
    return proc.stdout if stdout_only else proc.stdout + proc.stderr


def info_lines(out: str, *prefixes: str) -> list[str]:
    keep = []
    for line in out.splitlines():
        if line.startswith(prefixes):
            keep.append(scrub(re.sub(r"Elapsed time: [0-9.]+s, Critical Path: [0-9.]+s", "", line)))
    return keep


def explained() -> list[str]:
    """Actions the execution phase ran, with Bazel's reason, from the --explain log.

    Independent actions run in parallel, so the log order varies; sort it.
    """
    return sorted(ln for ln in EXPLAIN.read_text().splitlines() if ln.startswith("Executing action"))


def build(label: str, *extra: str, explain: bool = True) -> list[str]:
    flags = [f"--explain={EXPLAIN}", "--verbose_explanations"] if explain else []
    out = bazel("build", label, *flags, *extra)
    lines = info_lines(out, "INFO: Analyzed", "INFO: 1 process", "INFO: 0 process")
    lines += [ln for ln in info_lines(out, "INFO: ") if re.match(r"INFO: \d+ processes", ln)]
    return lines + (["  " + ln for ln in explained()] if explain else [])


def grouped_deps(skykey_filter: str) -> list[str]:
    """Summarize `dump --skyframe=deps`: one line per dependency group, function(label) only."""
    dump = bazel("dump", "--skyframe=deps", f"--skykey_filter={skykey_filter}", stdout_only=True)
    # Split into one block per node; a node header is the only unindented line.
    blocks: list[list[str]] = []
    for raw in dump.splitlines():
        if raw.startswith("WARNING") or not raw.strip():
            continue
        if not raw.startswith(" "):
            blocks.append([raw])
        elif blocks:
            blocks[-1].append(raw)
    if not blocks:
        raise SystemExit(f"filter {skykey_filter!r} matched no node")
    # With several configurations in the graph, show the default one (<cfg1>, or none at all).
    chosen = next((b for b in blocks if "<cfg1>" in scrub(b[0])), blocks[0])
    nodes = [chosen[0]]
    configs = sorted({m for b in blocks for m in re.findall(r"<cfg\d+>", scrub(b[0]))})
    out_count = f"  ({len(blocks)} node(s) matched, configurations: {', '.join(configs) or 'none'})"
    groups: list[list[str]] = []
    for raw in chosen[1:]:
        line = raw.strip()
        if re.fullmatch(r"Group \d+:", line):
            groups.append([])
            continue
        fn, _, rest = line.partition(":")
        label = re.search(r"label=([^,}\]]+)", rest)
        groups[-1].append(f"{fn}({scrub(label.group(1) if label else rest)[:60]})")
    out = [scrub(nodes[0])[:100], out_count]
    out += [f"  group {i}: " + ", ".join(g) for i, g in enumerate(groups, 1)]
    return out


def phase_markers() -> list[str]:
    trace = json.loads(PROFILE.read_text())
    events = trace["traceEvents"] if isinstance(trace, dict) else trace
    return [e["name"] for e in events if e.get("cat") == "build phase marker"]


def section(title: str) -> None:
    print()
    print(f"== {title}")


def main() -> None:
    if WS.exists():
        shutil.rmtree(WS)
    shutil.copytree(LAB / "ws", WS)

    section("Bazel")
    print(bazel("info", "release", stdout_only=True).strip())

    section("Bzlmod: module graph after Minimal Version Selection")
    # mod graph draws with box characters; the notes are ASCII, so translate them.
    box = str.maketrans({"\u251c": "|", "\u2514": "`", "\u2502": "|", "\u2500": "-"})
    tree = bazel("mod", "graph").splitlines()
    tree = [ln.rstrip() for ln in tree if "\u2500" in ln or ln.startswith("<root>")]
    print("\n".join(ln.translate(box) for ln in tree))
    for line in bazel("mod", "graph", "--verbose").splitlines():
        if "was " in line:
            print("verbose:", line.strip(" \u2502\u251c\u2514\u2500"))

    section("Bzlmod: apparent -> canonical repo names")
    for repo in ("", "lib_a+"):
        mapping = json.loads(bazel("mod", "dump_repo_mapping", repo).strip().splitlines()[-1])
        shown = {k: v for k, v in sorted(mapping.items()) if k in ("lab_facts", "lib_a", "lib_b", "base")}
        print(f"seen from {repo or '<root>'!s}: {json.dumps(shown, sort_keys=True)}")
    show = bazel("mod", "show_repo", "@lab_facts")
    print("module extension repo:", re.search(r'name = "([^"]+)"', show).group(1))
    print("module extension rule:", re.search(r"^(?!load\()(\w+)\(", show, re.MULTILINE).group(1))

    section("Loading: query sees every select() branch")
    q = bazel("query", "deps(//app:c)", "--notool_deps", "--noimplicit_deps")
    print("\n".join(sorted(ln for ln in q.splitlines() if ln.startswith("//"))))

    section("Analysis: cquery resolves select() per configuration")
    for flags in ([], ["--//:flavor=loud"]):
        out = bazel("cquery", "deps(//app:c)", "--notool_deps", "--noimplicit_deps", *flags)
        rows = sorted(scrub(ln) for ln in out.splitlines() if ln.startswith("//"))
        print(f"flags {' '.join(flags) or '(none)'}:")
        print("\n".join("  " + r for r in rows))
        for ln in out.splitlines():
            if "discarding analysis cache" in ln:
                print("  " + ln.split(" (this")[0])

    section("Analysis: providers returned by //app:b")
    sources = "//rules:defs.bzl%SourcesInfo"
    expr = f'[f.short_path for f in providers(target)["{sources}"].transitive.to_list()]'
    out = bazel("cquery", "//app:b", "--output=starlark", f"--starlark:expr={expr}")
    print("SourcesInfo.transitive:", [ln for ln in out.splitlines() if ln.startswith("[")][0])
    out = bazel("cquery", "//app:b", "--output=starlark", "--starlark:expr=sorted(providers(target).keys())")
    print("provider keys:", [ln for ln in out.splitlines() if ln.startswith("[")][0])

    section("Analysis: the action graph (aquery)")
    aq = bazel("aquery", 'mnemonic("Normalize|Bundle", deps(//app:bundle))', "--output=jsonproto")
    graph = json.loads(aq[aq.index("{"):aq.rindex("}") + 1])
    for action in sorted(graph["actions"], key=lambda a: (a["mnemonic"], a["primaryOutputId"])):
        reqs = ", ".join(f"{p['key']}={p['value']}" for p in action.get("executionInfo", []))
        print(f"{action['mnemonic']:<9} args={len(action['arguments'])} exec_info=[{reqs}]")
    keys_before = {a["primaryOutputId"]: a["actionKey"] for a in graph["actions"]}

    section("Execution 1: first build (aquery already ran the analysis)")
    print("\n".join(build("//app:bundle")))
    print("bundle.txt:")
    print("\n".join("  " + ln for ln in (WS / "bazel-bin/app/bundle.txt").read_text().splitlines()))

    section("Execution 2: null build")
    print("\n".join(build("//app:bundle")))

    section("Execution 3: edit only a comment in app/a.txt")
    (WS / "app/a.txt").write_text("# header comment, edited\nalpha one\nalpha two\n")
    print("\n".join(build("//app:bundle")))

    section("Execution 4: edit real content in app/a.txt")
    (WS / "app/a.txt").write_text("# header comment, edited\nalpha one\nalpha three\n")
    print("\n".join(build("//app:bundle", f"--profile={PROFILE}")))
    print("profile phase markers with --explain:", phase_markers())

    aq = bazel("aquery", 'mnemonic("Normalize|Bundle", deps(//app:bundle))', "--output=jsonproto")
    graph = json.loads(aq[aq.index("{"):aq.rindex("}") + 1])
    same = all(keys_before.get(a["primaryOutputId"]) == a["actionKey"] for a in graph["actions"])
    print("action keys unchanged by the content edit:", same)

    section("Execution 5: edit only a comment in app/BUILD.bazel")
    with open(WS / "app/BUILD.bazel", "a", encoding="utf-8") as fh:
        fh.write("# a comment that changes no target\n")
    print("\n".join(build("//app:bundle")))

    section("Execution 6: real edit again, without --explain (Skymeld on)")
    (WS / "app/a.txt").write_text("# header comment, edited\nalpha one\nalpha four\n")
    print("\n".join(build("//app:bundle", f"--profile={PROFILE}", explain=False)))
    print("profile phase markers without --explain:", phase_markers())

    section("Persistent worker")
    logs = sorted((OUTPUT_BASE / "bazel-workers").glob("worker-*-Normalize.log"))
    served = sum(ln.startswith("normalize-worker served") for f in logs for ln in f.read_text().splitlines())
    print(f"Normalize worker processes started: {len(logs)}; requests served: {served}")

    section("Skyframe: the node behind the Bundle action")
    dump = bazel("dump", "--skyframe=deps", "--skykey_filter=^ACTION_EXECUTION.*//app:bundle,")
    print("\n".join(scrub(ln) for ln in dump.splitlines() if ln.strip() and not ln.startswith("WARNING")))

    section("Skyframe: the configured target node, dependency groups in request order")
    print("\n".join(grouped_deps(r"^CONFIGURED_TARGET:ConfiguredTargetKey\{label=//app:bundle,")))

    section("Skyframe: the package node")
    print("\n".join(grouped_deps("^PACKAGE:app$")))

    section("Skyframe: a source file is two nodes")
    dump = bazel("dump", "--skyframe=deps", "--skykey_filter=^FILE:.*app/a.txt")
    print("\n".join(scrub(ln) for ln in dump.splitlines() if ln.strip() and not ln.startswith("WARNING")))

    section("Skyframe: node counts for this workspace")
    counts = {}
    for name, pattern in (
        ("PACKAGE", r"^PACKAGE:app$"),
        ("CONFIGURED_TARGET", r"^CONFIGURED_TARGET:.*label=//app:"),
        ("ACTION_EXECUTION", r"^ACTION_EXECUTION:.*label=//app:"),
    ):
        dump = bazel("dump", "--skyframe=deps", f"--skykey_filter={pattern}")
        counts[name] = sum(1 for ln in dump.splitlines() if ln.startswith(name + ":"))
    print(counts)

    section("Aspects: a shadow graph along deps")
    out = bazel("build", "//app:b", "//app:c", "--aspects=//rules:defs.bzl%inventory_aspect", "--output_groups=inventory")
    print("\n".join(info_lines(out, "INFO: Analyzed")))
    for report in sorted((WS / "bazel-bin/app").glob("*.inventory")):
        print(report.read_text().strip())

    section("Lockfile")
    lock = json.loads((WS / "MODULE.bazel.lock").read_text())
    hashes = lock["registryFileHashes"]
    print("lockFileVersion:", lock["lockFileVersion"])
    print("registry file hashes from the local file:// registry:", sum(k.startswith("file:") for k in hashes))
    print("registry file hashes from bcr.bazel.build: nonzero =", any("bcr.bazel.build" in k for k in hashes))
    print("root extension recorded:", "//rules:ext.bzl%facts" in lock["moduleExtensions"])

    bazel("shutdown")


if __name__ == "__main__":
    main()
