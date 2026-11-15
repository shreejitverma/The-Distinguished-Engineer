#!/usr/bin/env python3
"""Collate per-source P1689R5 scan results into a Ninja dyndep file and per-source module maps.

This is a toy version of what `cmake -E cmake_ninja_dyndep` does for one target:

1. Read every `.ddi` file (P1689R5 JSON written by `clang-scan-deps -format=p1689`).
2. Map each provided logical module name to the object that provides it and to a BMI path
   named after the module (`geom:vec` -> `bmi/geom-vec.pcm`, Clang's partition convention).
3. For each object, compute the transitive closure of the modules it requires, because Clang
   needs every BMI in the import graph to be named on the command line, not just direct imports.
4. Write `modules.dd`: one `build <obj> | <bmi it provides> : dyndep | <bmis it needs>` statement
   per compile edge, so Ninja orders BMI producers before consumers.
5. Write `<obj>.modmap`: the compiler flags (`-fmodule-output=`, `-fmodule-file=name=path`)
   that the compile edge reads through a response file.

Every output is written only when its bytes change, and the collate edge has `restat = 1`, so
a rescan that discovers the same graph does not dirty any compile edge.
Three switches reproduce the design choices that matter:

- `--direct-inputs`: the dyndep file lists only directly imported BMIs as implicit inputs (the
  module map still names the whole closure for the compiler). CMake 4.5 writes this shape.
- `--restat`: each BMI-producing statement carries `restat = 1`. CMake 4.5 writes this too.
- `--copy-if-different`: the BMI goes to `<bmi>.tmp` and `bmi_commit.py` replaces the real BMI
  only when its bytes differ, so `restat` can see an unchanged mtime and prune importers.
  cmake-cxxmodules(7) lists this as a possible future enhancement.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def write_if_changed(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == text:
        return
    path.write_text(text)


def bmi_name(logical_name: str) -> str:
    return logical_name.replace(":", "-") + ".pcm"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dd", required=True)
    ap.add_argument("--bmi-dir", required=True)
    ap.add_argument("--restat", action="store_true")
    ap.add_argument("--copy-if-different", action="store_true")
    ap.add_argument("--direct-inputs", action="store_true")
    ap.add_argument("ddis", nargs="+")
    args = ap.parse_args()

    provider_of: dict[str, str] = {}  # logical name -> object
    provides: dict[str, list[str]] = {}  # object -> logical names
    requires: dict[str, list[str]] = {}  # object -> logical names
    for ddi in args.ddis:
        doc = json.loads(Path(ddi).read_text())
        if doc.get("version") != 1:
            print(f"collate: {ddi}: unsupported P1689 version {doc.get('version')}", file=sys.stderr)
            return 1
        for rule in doc["rules"]:
            obj = rule["primary-output"]
            provides[obj] = [p["logical-name"] for p in rule.get("provides", [])]
            requires[obj] = [r["logical-name"] for r in rule.get("requires", [])]
            for name in provides[obj]:
                if name in provider_of:
                    print(
                        f"collate: module '{name}' is provided by both {provider_of[name]} and {obj}",
                        file=sys.stderr,
                    )
                    return 1
                provider_of[name] = obj

    for obj, names in requires.items():
        for name in names:
            if name not in provider_of:
                print(f"collate: {obj} imports '{name}', which no scanned source provides", file=sys.stderr)
                return 1

    def closure(obj: str) -> list[str]:
        # Depth-first walk over the import graph. A cycle would make this loop forever without
        # the `seen` set; the cycle itself is left for Ninja to report, as CMake does.
        seen: list[str] = []
        stack = list(requires[obj])
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen.append(name)
            stack.extend(requires[provider_of[name]])
        return sorted(seen)

    bmi_dir = args.bmi_dir.rstrip("/")
    dd = ["ninja_dyndep_version = 1"]
    for obj in sorted(provides):
        outs = [f"{bmi_dir}/{bmi_name(n)}" for n in provides[obj]]
        # Ninja reruns an edge when any implicit input changes. Listing only direct imports is
        # safe only when every BMI records the hashes of the BMIs it was built against, which is
        # what Clang 19+ documents ("Experimental Non-Cascading Changes"); ordering still holds
        # because a direct import's BMI cannot be built before its own imports.
        listed = sorted(requires[obj]) if args.direct_inputs else closure(obj)
        ins = [f"{bmi_dir}/{bmi_name(n)}" for n in listed]
        line = f"build {obj}"
        if outs:
            line += " | " + " ".join(outs)
        line += ": dyndep"
        if ins:
            line += " | " + " ".join(ins)
        dd.append(line)
        if outs and args.restat:
            dd.append("  restat = 1")

        flags = []
        suffix = ".tmp" if args.copy_if_different else ""
        for out in outs:
            flags.append(f"-fmodule-output={out}{suffix}")
        for name in closure(obj):
            flags.append(f"-fmodule-file={name}={bmi_dir}/{bmi_name(name)}")
        write_if_changed(Path(obj + ".modmap"), "".join(f + "\n" for f in flags))

    write_if_changed(Path(args.dd), "\n".join(dd) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
