"""A minimal cmake-file-api(7) v1 client using only the Python standard library.

It follows the client rules from the manual: find the current index by name,
read only reply files the index references, and never interpret reply file
names or target ids beyond what the manual promises. The one exception is the
`names` check, which deliberately verifies an implementation detail of
cmFileAPI.cxx (content-hashed reply names) so the lab fails if it changes.

Usage:
    fileapi_client.py report  <build-dir>
    fileapi_client.py names   <build-dir>
    fileapi_client.py list    <build-dir>
    fileapi_client.py changed <before-list-file> <build-dir>
    fileapi_client.py launchers <build-dir>
    fileapi_client.py error   <build-dir>
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

INDEX_RE = re.compile(r"^(index|error)-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{4}\.json$")
HASHED_RE = re.compile(r"^(?P<stem>.+)-(?P<hash>[0-9a-f]{20})\.json$")


class FileApiError(RuntimeError):
    pass


def reply_dir(build: Path) -> Path:
    d = build / ".cmake" / "api" / "v1" / "reply"
    if not d.is_dir():
        raise FileApiError(f"no reply directory under {build}")
    return d


def current_index(build: Path, include_errors: bool = False) -> Path:
    """The manual: the index with the largest name (ignoring the index-/error- prefix) is current."""
    d = reply_dir(build)
    prefixes = ("index-", "error-") if include_errors else ("index-",)
    candidates = [p for p in d.iterdir() if p.name.startswith(prefixes) and p.suffix == ".json"]
    if not candidates:
        raise FileApiError("no reply index present")
    return max(candidates, key=lambda p: p.name.split("-", 1)[1])


def load(path: Path) -> dict:
    with path.open("rb") as f:
        return json.load(f)


def ref(index_path: Path, entry: dict) -> dict:
    if "error" in entry:
        raise FileApiError(f"reply error: {entry['error']}")
    return load(index_path.parent / entry["jsonFile"])


def version(v: dict) -> str:
    return f"{v['major']}.{v['minor']}"


def backtrace_text(graph: dict, node_index: int) -> str:
    """Resolve one backtrace node to 'file:line command', innermost frame only."""
    node = graph["nodes"][node_index]
    where = graph["files"][node["file"]]
    if "line" in node:
        where += f":{node['line']}"
    if "command" in node:
        where += f" {graph['commands'][node['command']]}()"
    return where


def report(build: Path) -> None:
    idx_path = current_index(build)
    idx = load(idx_path)
    cm = idx["cmake"]
    print(f"index: generator={cm['generator']['name']} multiConfig={cm['generator']['multiConfig']} "
          f"cmake={cm['version']['major']}.{cm['version']['minor']}")
    print("objects written:")
    for o in idx["objects"]:
        print(f"  {o['kind']} v{version(o['version'])}")

    print("reply mirror of query/:")
    for key in sorted(idx["reply"]):
        val = idx["reply"][key]
        if key.startswith("client-"):
            for sub in sorted(val):
                if sub == "query.json":
                    q = val[sub]
                    print(f"  {key}/query.json client={json.dumps(q.get('client'), sort_keys=True)}")
                    for req, resp in zip(q["requests"], q["responses"]):
                        asked = json.dumps(req.get("version"))
                        if "error" in resp:
                            got = f"error: {resp['error']}"
                        else:
                            got = f"{resp['kind']} v{version(resp['version'])}"
                        print(f"    asked {req['kind']} {asked} -> {got}")
                else:
                    print(f"  {key}/{sub}: {val[sub]}")
        elif "error" in val:
            print(f"  {key}: error: {val['error']}")
        else:
            print(f"  {key}: {val['kind']} v{version(val['version'])}")

    by_kind = {o["kind"]: o for o in idx["objects"]}

    codemodel = ref(idx_path, by_kind["codemodel"])
    cfg = codemodel["configurations"][0]
    targets = cfg["targets"]
    abstract = cfg.get("abstractTargets", [])
    print(f"codemodel: configurations={[c['name'] for c in codemodel['configurations']]} "
          f"directories={len(cfg['directories'])} projects={[p['name'] for p in cfg['projects']]}")
    print(f"  buildsystem targets: {', '.join(t['name'] for t in targets)}")
    print(f"  abstract targets: {', '.join(t['name'] for t in abstract) or '(none)'}")

    detail = {}
    for t in targets + abstract:
        obj = load(idx_path.parent / t["jsonFile"])
        if obj["id"] != t["id"]:
            raise FileApiError(f"id mismatch for {t['name']}")
        detail[t["id"]] = obj
    name_of = {tid: obj["name"] for tid, obj in detail.items()}
    for tid, obj in sorted(detail.items(), key=lambda kv: kv[1]["name"]):
        extra = f" nameOnDisk={obj['nameOnDisk']}" if "nameOnDisk" in obj else ""
        flag = " abstract" if obj.get("abstract") else ""
        print(f"  target {obj['name']}: type={obj['type']}{flag}{extra}")

    app = next(o for o in detail.values() if o["name"] == "app")
    g = app["backtraceGraph"]
    print("  app.dependencies (build order): "
          + ", ".join(name_of[d["id"]] for d in app.get("dependencies", [])))
    print("  app.linkLibraries: "
          + ", ".join(name_of.get(d.get("id"), d.get("fragment", "?")) for d in app.get("linkLibraries", [])))
    for cgi, cg in enumerate(app["compileGroups"]):
        std = cg.get("languageStandard", {})
        std_bt = ", ".join(backtrace_text(g, b) for b in std.get("backtraces", []))
        print(f"  app.compileGroups[{cgi}]: language={cg['language']} standard={std.get('standard')} (from {std_bt})")
        for d in cg.get("defines", []):
            print(f"    define {d['define']} (from {backtrace_text(g, d['backtrace'])})")
    print("  app.link.commandFragments roles: "
          + ", ".join(sorted({f['role'] for f in app['link'].get('commandFragments', [])})))
    print(f"  app.backtrace: {backtrace_text(g, app['backtrace'])}")
    for launcher in app.get("launchers", []):
        print(f"  app.launcher: type={launcher['type']} command={launcher['command']} "
              f"arguments={launcher.get('arguments', [])}")

    tc = ref(idx_path, by_kind["toolchains"])
    for t in tc["toolchains"]:
        comp = t["compiler"]
        print(f"toolchains: language={t['language']} compiler.id={comp.get('id')} "
              f"has path={'path' in comp} has version={'version' in comp} "
              f"implicit keys={sorted(comp.get('implicit', {}))}")

    cache = ref(idx_path, by_kind["cache"])
    entries = {e["name"]: e for e in cache["entries"]}
    for name in ("LAB_LEVEL", "CMAKE_GENERATOR"):
        e = entries[name]
        print(f"cache: {name}={e['value']} type={e['type']}")

    clog = ref(idx_path, by_kind["configureLog"])
    log_path = Path(clog["path"])
    rel = log_path.relative_to(build.resolve()) if log_path.is_absolute() else log_path
    print(f"configureLog: path=<build>/{rel.as_posix()} eventKindNames={clog['eventKindNames']}")
    text = log_path.read_text()
    lines = text.splitlines()
    docs = sum(1 for line in lines if line == "---")
    closed = sum(1 for line in lines if line == "...")
    kinds = re.findall(r'^    kind: "([^"]+)"$', text, re.MULTILINE)
    has_check = 'variable: "LAB_HAVE_TRIVIAL"' in text
    print(f"  YAML documents={docs} (closed with '...': {closed}) event kinds present={sorted(set(kinds))}")
    print(f"  lab message event logged: {'lab check summary' in text}")
    print(f"  try_compile event for LAB_HAVE_TRIVIAL: {has_check}")


def names(build: Path) -> None:
    """Check the naming scheme cmFileAPI.cxx uses: SHA3-256 of content, first 20 hex digits."""
    d = reply_dir(build)
    hashed = indexes = 0
    for p in sorted(d.iterdir()):
        if INDEX_RE.match(p.name):
            indexes += 1
            continue
        m = HASHED_RE.match(p.name)
        if not m:
            raise FileApiError(f"unexpected reply file name {p.name}")
        digest = hashlib.sha3_256(p.read_bytes()).hexdigest()[:20]
        if digest != m.group("hash"):
            raise FileApiError(f"{p.name}: content hash {digest} does not match its name")
        hashed += 1
    print(f"reply names: {hashed} files named by SHA3-256(content)[:20], {indexes} timestamped index")


def stem(name: str) -> str:
    if INDEX_RE.match(name):
        return name.split("-", 1)[0] + "-<time>"
    m = HASHED_RE.match(name)
    return m.group("stem") if m else name


def list_files(build: Path) -> None:
    for p in sorted(reply_dir(build).iterdir()):
        print(p.name)


def changed(before_file: Path, build: Path) -> None:
    before = set(before_file.read_text().split())
    after = {p.name for p in reply_dir(build).iterdir()}
    kept = sorted(stem(n) for n in before & after)
    added = sorted(stem(n) for n in after - before)
    removed = sorted(stem(n) for n in before - after)
    print(f"  unchanged names: {', '.join(kept) or '(none)'}")
    print(f"  new names:       {', '.join(added) or '(none)'}")
    print(f"  removed names:   {', '.join(removed) or '(none)'}")


def launchers(build: Path) -> None:
    idx_path = current_index(build)
    idx = load(idx_path)
    codemodel = ref(idx_path, next(o for o in idx["objects"] if o["kind"] == "codemodel"))
    for t in codemodel["configurations"][0]["targets"]:
        obj = load(idx_path.parent / t["jsonFile"])
        for launcher in obj.get("launchers", []):
            print(f"  {obj['name']}.launchers: type={launcher['type']} command={launcher['command']} "
                  f"arguments={launcher.get('arguments', [])}")


def error(build: Path) -> None:
    d = reply_dir(build)
    print("  reply files: " + ", ".join(sorted(stem(p.name) for p in d.iterdir())))
    cur = current_index(build, include_errors=True)
    print(f"  current index is {stem(cur.name)}")
    idx = load(cur)
    print("  objects: " + ", ".join(f"{o['kind']} v{version(o['version'])}" for o in idx["objects"]))
    for key in sorted(idx["reply"]):
        val = idx["reply"][key]
        print(f"  reply[{key}]: " + (f"error: {val['error']}" if "error" in val else f"{val['kind']} ok"))


def main(argv: list[str]) -> int:
    cmd = argv[1]
    if cmd == "report":
        report(Path(argv[2]))
    elif cmd == "names":
        names(Path(argv[2]))
    elif cmd == "list":
        list_files(Path(argv[2]))
    elif cmd == "changed":
        changed(Path(argv[2]), Path(argv[3]))
    elif cmd == "launchers":
        launchers(Path(argv[2]))
    elif cmd == "error":
        error(Path(argv[2]))
    else:
        raise SystemExit(f"unknown command {cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
