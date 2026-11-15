#!/usr/bin/env python3
"""Decode Bazel compact execution logs and diagnose cache misses between builds.

The compact log (--execution_log_compact_file) is a zstd-compressed stream of
varint-length-delimited ExecLogEntry protos (src/main/protobuf/spawn.proto, Bazel 9.3.2).
Files, directories, and input sets are written once and referenced by entry ID, so a
spawn's inputs are recovered by walking its input set DAG.
This module decodes the wire format by hand, with no protobuf dependency.

Subcommands:
    summary LOG                 one line per spawn: target, cacheable, how it ran
    diff LOG1 LOG2              match spawns by first output, explain every key change
    determinism LOG1 LOG2       find spawns with identical keys but different outputs
    disk-cache LOG DISK_CACHE   check that every cache hit's action digest is in ac/
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ExecLogEntry oneof field numbers (spawn.proto, Bazel 9.3.2).
ENTRY_INVOCATION = 2
ENTRY_FILE = 3
ENTRY_DIRECTORY = 4
ENTRY_UNRESOLVED_SYMLINK = 5
ENTRY_INPUT_SET = 6
ENTRY_SPAWN = 7
ENTRY_SYMLINK_ACTION = 8
ENTRY_SYMLINK_ENTRY_SET = 9
ENTRY_RUNFILES_TREE = 10

CONFIG_DIR = re.compile(r"bazel-out/[^/]+/")


def decompress(path: Path) -> bytes:
    raw = path.read_bytes()
    try:
        from compression import zstd  # Python 3.14+

        return zstd.decompress(raw)
    except ImportError:
        pass
    tool = shutil.which("zstd")
    if tool is None:
        sys.exit("execlog.py: need Python 3.14+ (compression.zstd) or the zstd CLI")
    return subprocess.run([tool, "-dc"], input=raw, capture_output=True, check=True).stdout


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def parse_fields(buf: bytes) -> dict[int, list]:
    """Return {field_number: [values]}; varints as int, length-delimited as bytes."""
    fields: dict[int, list] = {}
    pos = 0
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = read_varint(buf, pos)
        elif wire == 2:
            length, pos = read_varint(buf, pos)
            value = buf[pos : pos + length]
            pos += length
        elif wire == 1:
            value = buf[pos : pos + 8]
            pos += 8
        elif wire == 5:
            value = buf[pos : pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        fields.setdefault(number, []).append(value)
    return fields


def text(fields: dict[int, list], number: int) -> str:
    values = fields.get(number)
    return values[-1].decode() if values else ""


def number(fields: dict[int, list], num: int) -> int:
    values = fields.get(num)
    return values[-1] if values else 0


def uint32_list(fields: dict[int, list], num: int) -> list[int]:
    """Repeated uint32, accepting both packed (proto3 default) and unpacked encodings."""
    out: list[int] = []
    for value in fields.get(num, []):
        if isinstance(value, int):
            out.append(value)
            continue
        pos = 0
        while pos < len(value):
            item, pos = read_varint(value, pos)
            out.append(item)
    return out


def digest_hash(fields: dict[int, list], num: int) -> str:
    values = fields.get(num)
    if not values:
        return ""
    return text(parse_fields(values[-1]), 1)


@dataclass
class Spawn:
    label: str
    mnemonic: str
    args: list[str]
    env: dict[str, str]
    platform: list[tuple[str, str]]
    inputs: dict[str, str]
    outputs: dict[str, str]
    runner: str
    cache_hit: bool
    cacheable: bool
    remote_cacheable: bool
    action_digest: str
    first_output: str = field(default="")


def load(path: Path) -> list[Spawn]:
    data = decompress(path)
    entries: dict[int, tuple[int, dict[int, list]]] = {}
    spawns: list[Spawn] = []
    pos = 0
    while pos < len(data):
        length, pos = read_varint(data, pos)
        entry = parse_fields(data[pos : pos + length])
        pos += length
        entry_id = number(entry, 1)
        kind = next((k for k in entry if k != 1), None)
        if kind is None:
            continue
        payload = parse_fields(entry[kind][-1])
        if entry_id:
            entries[entry_id] = (kind, payload)
        if kind == ENTRY_SPAWN:
            spawns.append(build_spawn(payload, entries))
    return spawns


def flatten(set_id: int, entries: dict, out: dict[str, str], seen: set[int]) -> None:
    """Postorder walk of an InputSet: transitive sets first, then direct entries."""
    if not set_id or set_id in seen:
        return
    seen.add(set_id)
    kind, payload = entries[set_id]
    if kind != ENTRY_INPUT_SET:
        raise ValueError(f"entry {set_id} is kind {kind}, not an input set")
    for child in uint32_list(payload, 4):
        flatten(child, entries, out, seen)
    for input_id in uint32_list(payload, 5):
        add_input(input_id, entries, out)


def add_input(input_id: int, entries: dict, out: dict[str, str]) -> None:
    kind, payload = entries[input_id]
    if kind == ENTRY_FILE:
        out[text(payload, 1)] = digest_hash(payload, 2)
    elif kind == ENTRY_DIRECTORY:
        root = text(payload, 1)
        for raw in payload.get(2, []):
            child = parse_fields(raw)
            out[f"{root}/{text(child, 1)}"] = digest_hash(child, 2)
    elif kind == ENTRY_UNRESOLVED_SYMLINK:
        out[text(payload, 1)] = "symlink->" + text(payload, 2)
    elif kind == ENTRY_RUNFILES_TREE:
        out[text(payload, 1)] = f"runfiles-tree#{input_id}"
    else:
        raise ValueError(f"unexpected input entry kind {kind}")


def build_spawn(payload: dict[int, list], entries: dict) -> Spawn:
    env = {}
    for raw in payload.get(2, []):
        pair = parse_fields(raw)
        env[text(pair, 1)] = text(pair, 2)
    platform = []
    for raw in payload.get(3, []):
        for prop_raw in parse_fields(raw).get(1, []):
            prop = parse_fields(prop_raw)
            platform.append((text(prop, 1), text(prop, 2)))
    inputs: dict[str, str] = {}
    flatten(number(payload, 4), entries, inputs, set())
    outputs: dict[str, str] = {}
    for raw in payload.get(6, []):
        out = parse_fields(raw)
        if 5 in out:
            kind, file_payload = entries[number(out, 5)]
            if kind == ENTRY_FILE:
                outputs[text(file_payload, 1)] = digest_hash(file_payload, 2)
            else:
                outputs[text(file_payload, 1)] = f"kind-{kind}"
        else:
            outputs[text(out, 4)] = "<missing>"
    spawn = Spawn(
        label=text(payload, 7),
        mnemonic=text(payload, 8),
        args=[a.decode() for a in payload.get(1, [])],
        env=env,
        platform=platform,
        inputs=inputs,
        outputs=outputs,
        runner=text(payload, 11),
        cache_hit=bool(number(payload, 12)),
        cacheable=bool(number(payload, 14)),
        remote_cacheable=bool(number(payload, 15)),
        action_digest=digest_hash(payload, 16),
    )
    spawn.first_output = min(outputs) if outputs else ""
    return spawn


def scrub(path: str) -> str:
    return CONFIG_DIR.sub("bazel-out/<cfg>/", path)


def short_label(label: str) -> str:
    return label.split(":")[-1] if ":" in label else label


def how_it_ran(spawn: Spawn) -> str:
    if spawn.cache_hit:
        return spawn.runner  # "disk cache hit" or "remote cache hit"
    return "executed"


def by_output(spawns: list[Spawn]) -> dict[str, Spawn]:
    return {s.first_output: s for s in spawns if s.first_output}


def key_differences(a: Spawn, b: Spawn) -> list[str]:
    """Every logged field that feeds the remote cache key and differs between a and b."""
    reasons: list[str] = []
    if a.args != b.args:
        reasons.append("command line differs")
    for name in sorted(set(a.env) | set(b.env)):
        if a.env.get(name) != b.env.get(name):
            reasons.append(f"env {name}: {a.env.get(name, '<unset>')} -> {b.env.get(name, '<unset>')}")
    if a.platform != b.platform:
        reasons.append("platform properties differ")
    for path in sorted(set(a.inputs) | set(b.inputs)):
        if path not in b.inputs:
            reasons.append(f"input removed: {scrub(path)}")
        elif path not in a.inputs:
            reasons.append(f"input added: {scrub(path)}")
        elif a.inputs[path] != b.inputs[path]:
            reasons.append(f"input digest changed: {scrub(path)}")
    if sorted(a.outputs) != sorted(b.outputs):
        reasons.append("declared outputs differ")
    if a.cacheable != b.cacheable:
        reasons.append(f"cacheable: {a.cacheable} -> {b.cacheable}")
    return reasons


def cmd_summary(log: Path) -> None:
    spawns = sorted(load(log), key=lambda s: s.label)
    hits = sum(s.cache_hit for s in spawns)
    print(f"spawns: {len(spawns)}, cache hits: {hits}, executed: {len(spawns) - hits}")
    for s in spawns:
        flag = "cacheable" if s.cacheable else "NOT cacheable"
        print(f"  {short_label(s.label):<10} {s.mnemonic:<8} {flag:<13} {how_it_ran(s)}")


def cmd_diff(log1: Path, log2: Path) -> None:
    first = by_output(load(log1))
    second = by_output(load(log2))
    for output in sorted(second, key=lambda o: second[o].label):
        b = second[output]
        name = short_label(b.label)
        a = first.get(output)
        if a is None:
            print(f"  {name}: only in second log")
            continue
        reasons = key_differences(a, b)
        same_digest = bool(a.action_digest) and a.action_digest == b.action_digest
        if not reasons:
            verdict = "same action digest" if same_digest else "same logged inputs"
            print(f"  {name}: {verdict}, {how_it_ran(b)}")
            continue
        print(f"  {name}: KEY CHANGED, {how_it_ran(b)}")
        for reason in reasons:
            print(f"      {reason}")
    for output in sorted(set(first) - set(second)):
        print(f"  {short_label(first[output].label)}: only in first log")


def cmd_determinism(log1: Path, log2: Path) -> None:
    first = by_output(load(log1))
    second = by_output(load(log2))
    producers = {}  # output path -> label of a non-deterministic producer
    rows = []
    for output in sorted(set(first) & set(second), key=lambda o: first[o].label):
        a, b = first[output], second[output]
        if not key_differences(a, b) and a.outputs != b.outputs:
            for path in a.outputs:
                producers[path] = short_label(a.label)
    for output in sorted(set(first) & set(second), key=lambda o: first[o].label):
        a, b = first[output], second[output]
        name = short_label(a.label)
        reasons = key_differences(a, b)
        if not reasons:
            state = "NON-DETERMINISTIC (same inputs, different output)" if a.outputs != b.outputs else "deterministic"
            rows.append(f"  {name:<10} {state}")
            continue
        poisoned = sorted({producers[p] for p in a.inputs if p in producers and a.inputs[p] != b.inputs.get(p)})
        if poisoned:
            rows.append(f"  {name:<10} key changed only because of: {', '.join(poisoned)}")
        else:
            rows.append(f"  {name:<10} key changed: {'; '.join(reasons)}")
    print("\n".join(rows))


def cmd_disk_cache(log: Path, cache: Path) -> None:
    spawns = load(log)
    hits = [s for s in spawns if s.cache_hit]
    found = 0
    for s in hits:
        h = s.action_digest
        if h and (cache / "ac" / h[:2] / h).is_file():
            found += 1
    print(f"cache hits whose action digest is a file under ac/<first 2 hex>/: {found} of {len(hits)}")
    keyless = sorted(short_label(s.label) for s in spawns if not s.action_digest)
    print(f"spawns logged without an action digest: {', '.join(keyless) if keyless else 'none'}")
    lengths = sorted({len(s.action_digest) for s in spawns if s.action_digest})
    print(f"action digest length in hex digits: {', '.join(map(str, lengths))}")


def main(argv: list[str]) -> None:
    if len(argv) < 3:
        sys.exit(__doc__)
    command, paths = argv[1], [Path(p) for p in argv[2:]]
    if command == "summary" and len(paths) == 1:
        cmd_summary(*paths)
    elif command == "diff" and len(paths) == 2:
        cmd_diff(*paths)
    elif command == "determinism" and len(paths) == 2:
        cmd_determinism(*paths)
    elif command == "disk-cache" and len(paths) == 2:
        cmd_disk_cache(*paths)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
