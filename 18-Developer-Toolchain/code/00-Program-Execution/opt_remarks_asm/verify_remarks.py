#!/usr/bin/env python3
"""Verify Clang optimization remarks against the AArch64 assembly they describe.

Compiles opt_demo.c at -O3 with remarks on, then checks that each remark matches
the YAML record for the same function and the instructions the backend emitted.
Everything is built in a temporary directory, so a failed run leaves nothing behind.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PASSES = "(loop-vectorize|inline)"
FUNCTIONS = ("vector_add", "dependent_add", "call_inline", "call_noinline", "helper_noinline")
EXPECTED_RECORDS = (
    ("Passed", "inline", "Inlined", "call_inline"),
    ("Missed", "inline", "NeverInline", "call_noinline"),
    ("Passed", "loop-vectorize", "Vectorized", "vector_add"),
    ("Analysis", "loop-vectorize", "UnsafeDep", "dependent_add"),
    ("Missed", "loop-vectorize", "MissedDetails", "dependent_add"),
)

INLINED_RE = re.compile(r"'helper_inline' inlined into 'call_inline' with \(cost=(-?\d+), threshold=(\d+)\)")
NEVER_RE = re.compile(
    r"'helper_noinline' not inlined into 'call_noinline' because it should never be inlined "
    r"\(cost=never\): noinline function attribute"
)
VECTORIZED_RE = re.compile(
    r"vectorized loop \(vectorization width: (\d+), interleaved count: (\d+)\) \[-Rpass=loop-vectorize\]"
)
BACKWARD_DEP_RE = re.compile(
    r"loop not vectorized: unsafe dependent memory operations in loop\..*?"
    r"Backward loop carried data dependence\. Memory location is the same as accessed at \S+:13:16",
    re.DOTALL,
)
COMMENT_RE = re.compile(r"\s*;.*$")
LABEL_RE = re.compile(r"^[\w.$]+:$")
VECTOR_INST_RE = re.compile(r"^\w+\.\d+[bhsd]\b|\b[vq]\d+\b")
CALL_RE = re.compile(r"^(bl|blr)\s")


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def detect_compiler(cc: str) -> tuple[str, str]:
    """Return (family, target arch) from `cc -v`; the lab needs Clang targeting AArch64."""
    try:
        proc = run([cc, "-v"])
    except OSError as exc:
        sys.exit(f"cannot execute compiler {cc!r}: {exc}")
    text = proc.stdout + "\n" + proc.stderr
    family = "clang" if "clang" in text.lower() else "other"
    target = re.search(r"^Target:\s*(\S+)", text, re.MULTILINE)
    arch = target.group(1).split("-")[0] if target else "unknown"
    return family, {"arm64": "aarch64"}.get(arch, arch)


def parse_yaml_records(yaml_path: Path) -> list[tuple[str, str, str, str]]:
    """Return (kind, pass, name, function) for each document of an LLVM remarks YAML file."""
    records = []
    for doc in yaml_path.read_text(encoding="utf-8").split("\n--- "):
        doc = doc.removeprefix("--- ").strip()
        if not doc:
            continue
        kind = re.match(r"!(\w+)", doc)
        fields = [re.search(rf"^{key}:\s+(\S+)", doc, re.MULTILINE) for key in ("Pass", "Name", "Function")]
        if kind is None or not all(fields):
            sys.exit(f"unparseable remark record:\n{doc[:200]}")
        records.append((kind.group(1), *(m.group(1) for m in fields)))
    return records


def instructions(asm_lines: list[str]) -> list[str]:
    """Keep executable instructions: drop blanks, comments, directives, and labels (with or without a trailing comment)."""
    kept = []
    for line in asm_lines:
        s = COMMENT_RE.sub("", line.strip())
        if not s or s.startswith(".") or LABEL_RE.match(s):
            continue
        kept.append(s)
    return kept


def split_functions(asm_lines: list[str]) -> dict[str, list[str]]:
    """Slice the assembly at global function labels; local labels (LBB, Lfunc_end) stay in the current function."""
    bodies: dict[str, list[str]] = {}
    current = None
    for line in asm_lines:
        m = re.match(r"^_(\w+):", line)
        if m:
            current = m.group(1)
            bodies[current] = []
        elif current:
            bodies[current].extend(instructions([line]))
    return bodies


def main() -> int:
    src = Path(__file__).resolve().parent / "opt_demo.c"
    cc = os.environ.get("CC", "cc")
    family, arch = detect_compiler(cc)
    print("=== Compiler Optimization Diagnostic Verification ===")
    print(f"Compiler: {family}")
    print(f"Architecture: {arch}")
    if (family, arch) != ("clang", "aarch64"):
        print(f"FAIL: this lab needs Clang targeting AArch64, found {family} on {arch}")
        return 1

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> str:
        if not ok:
            failures.append(f"{label}: {detail}" if detail else label)
        return "PASSED" if ok else "FAILED"

    with tempfile.TemporaryDirectory(prefix="opt_remarks_") as tmp:
        work = Path(tmp)
        yaml_file = work / "opt_demo.opt.yaml"
        diag = run([
            cc, "-O3", f"-Rpass={PASSES}", f"-Rpass-missed={PASSES}", f"-Rpass-analysis={PASSES}",
            "-fsave-optimization-record", f"-foptimization-record-file={yaml_file}",
            f"-foptimization-record-passes={PASSES}", "-c", str(src), "-o", str(work / "opt_demo.o"),
        ])
        if diag.returncode != 0:
            print(diag.stderr)
            return 1
        asm_file = work / "opt_demo.s"
        if run([cc, "-O3", "-S", str(src), "-o", str(asm_file)]).returncode != 0:
            print("FAIL: assembly compile failed")
            return 1

        stderr = diag.stderr
        records = parse_yaml_records(yaml_file)
        asm_lines = asm_file.read_text(encoding="utf-8").splitlines()
        bodies = split_functions(asm_lines)
        kept = instructions(asm_lines)

    print("\n--- Diagnostic Remarks Output ---")
    inlined = INLINED_RE.findall(stderr)
    cost, threshold = (int(inlined[0][0]), int(inlined[0][1])) if len(inlined) == 1 else (0, 0)
    ok = len(inlined) == 1 and cost < threshold
    print(f"Remark [inlining success]: helper_inline into call_inline, cost {cost} < threshold {threshold} -> "
          f"{check('inlining success remark', ok, f'matches={inlined}')}")
    ok = len(NEVER_RE.findall(stderr)) == 1
    print(f"Remark [inlining inhibited]: helper_noinline into call_noinline, cost=never -> "
          f"{check('inlining inhibited remark', ok)}")
    vectorized = VECTORIZED_RE.findall(stderr)
    width, interleave = (int(vectorized[0][0]), int(vectorized[0][1])) if len(vectorized) == 1 else (0, 0)
    print(f"Remark [vectorization success]: vector_add, width {width}, interleave {interleave} -> "
          f"{check('vectorization success remark', len(vectorized) == 1, f'matches={vectorized}')}")
    ok = len(BACKWARD_DEP_RE.findall(stderr)) == 1
    print(f"Remark [vectorization missed]: dependent_add, backward loop-carried dependence -> "
          f"{check('vectorization missed remark', ok)}")

    print("\n--- Serialized Optimization Records (YAML) ---")
    print(f"Total serialized records: {len(records)}")
    for rec in sorted(records):
        print("  " + " ".join(rec))
    ok = sorted(records) == sorted(EXPECTED_RECORDS)
    print(f"Records match the remarks per function: {check('YAML records', ok, f'got={sorted(records)}')}")

    print("\n--- Assembly Structure and Filtering ---")
    print(f"Raw assembly lines: {len(asm_lines)}")
    print(f"Filtered assembly instructions: {len(kept)}")
    print(f"Directives, comments, and labels removed: {(1.0 - len(kept) / len(asm_lines)) * 100:.1f}%")
    missing = [fn for fn in FUNCTIONS if not bodies.get(fn)]
    print(f"Functions found in assembly: {'all ' + str(len(FUNCTIONS)) if not missing else 'missing ' + str(missing)}")
    check("function slicing", not missing, f"missing={missing}")
    if missing:
        print("\n".join(f"FAIL: {f}" for f in failures))
        return 1

    print("\n--- Code Generation ---")
    vadd = bodies["vector_add"]
    vector_fadds = [i for i in vadd if i.startswith("fadd.4s")]
    # One fadd.4s per interleaved copy in the main loop, plus one in the single-vector epilogue loop.
    expected_fadds = interleave + 1
    ok = bool(vectorized) and len(vector_fadds) == expected_fadds and width == 4
    print(f"vector_add 128-bit fadd.4s instructions: {len(vector_fadds)} (interleave count {interleave} + 1 epilogue) -> "
          f"{check('vector_add fadd.4s count', ok, f'fadd.4s={len(vector_fadds)} expected={expected_fadds} width={width}')}")
    dep = bodies["dependent_add"]
    dep_vector = [i for i in dep if VECTOR_INST_RE.search(i)]
    dep_scalar = [i for i in dep if re.match(r"fadd\s+s\d+, s\d+, s\d+$", i)]
    print(f"dependent_add vector instructions: {len(dep_vector)} (expected 0)")
    print(f"dependent_add scalar fadd instructions: {len(dep_scalar)} (expected more than 0)")
    check("dependent_add has no vector instructions", not dep_vector, f"vector={dep_vector}")
    check("dependent_add has scalar fadd", bool(dep_scalar))
    inl = bodies["call_inline"]
    inl_calls = [i for i in inl if CALL_RE.match(i)]
    helper_symbol_kept = any(line.startswith("_helper_inline:") for line in asm_lines)
    print(f"call_inline calls: {len(inl_calls)} (expected 0), madd present: {any(i.startswith('madd') for i in inl)}, "
          f"helper_inline symbol emitted: {helper_symbol_kept}")
    check("call_inline has no calls", not inl_calls, f"calls={inl_calls}")
    check("call_inline folds into madd", any(i.startswith("madd") for i in inl))
    check("helper_inline symbol dropped", not helper_symbol_kept)
    noinl = bodies["call_noinline"]
    noinl_calls = [" ".join(i.split()) for i in noinl if CALL_RE.match(i)]
    framed = any(re.match(r"stp\s+x29, x30, \[sp, #-16\]!$", i) for i in noinl)
    print(f"call_noinline calls: {noinl_calls} (expected ['bl _helper_noinline']), 16-byte frame setup: {framed}")
    check("call_noinline calls helper_noinline once", len(noinl_calls) == 1 and noinl_calls[0].split()[1] == "_helper_noinline",
          f"calls={noinl_calls}")
    check("call_noinline sets up a frame", framed)

    if failures:
        print("\nVerification status: FAILED")
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("\nVerification status: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
