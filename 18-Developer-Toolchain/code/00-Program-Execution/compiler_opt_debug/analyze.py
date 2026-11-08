#!/usr/bin/env python3
"""analyze.py - Quantitative extraction of compiler optimization debug degradation.

Parses DWARF debug information and line tables from unoptimized (-O0 -g) and
optimized (-O2 -g) object files to demonstrate inlining, SROA, tail call
elimination, line table non-monotonicity, and vectorization impact.

Every check is tied to one function or variable: DIEs are parsed into a tree,
line rows are cut by function address range, and disassembly is cut by symbol.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

DIE_RE = re.compile(r"^(0x[0-9a-f]+):(\s+)(DW_TAG_\w+|NULL)")
ATTR_RE = re.compile(r"^\s+(DW_AT_\w+)\t\((.*)$")
RANGE_RE = re.compile(r"\[(0x[0-9a-f]+),\s*(0x[0-9a-f]+)\):\s*(.*)$")
LINE_RE = re.compile(r"^(0x[0-9a-f]+)\s+(\d+)\s+\d+\s.*\bis_stmt\b")
PIECE_RE = re.compile(r"DW_OP_piece")


@dataclass
class Die:
    offset: str
    depth: int
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    loc_lines: list[str] = field(default_factory=list)
    children: list["Die"] = field(default_factory=list)
    parent: "Die | None" = None

    @property
    def name(self) -> str:
        m = re.match(r'"(.*)"\)?$', self.attrs.get("DW_AT_name", ""))
        return m.group(1) if m else ""

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def pc_range(self) -> tuple[int, int] | None:
        if "DW_AT_low_pc" not in self.attrs or "DW_AT_high_pc" not in self.attrs:
            return None
        lo = int(self.attrs["DW_AT_low_pc"].rstrip(")"), 16)
        hi = int(self.attrs["DW_AT_high_pc"].rstrip(")"), 16)
        return lo, hi


def find_dwarf_tool() -> str:
    """Return an LLVM dwarfdump; Linux `dwarfdump` is libdwarf and lacks --statistics."""
    for tool in ("llvm-dwarfdump", "dwarfdump"):
        path = shutil.which(tool)
        if not path:
            continue
        res = subprocess.run([path, "--version"], capture_output=True, text=True)
        if "LLVM" in res.stdout + res.stderr:
            return path
    sys.exit("Error: no LLVM dwarfdump (llvm-dwarfdump, or Apple dwarfdump) found on PATH.")


def get_cmd_output(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def parse_dies(text: str) -> list[Die]:
    """Parse `dwarfdump --debug-info` text into a forest keyed by indentation."""
    roots: list[Die] = []
    stack: list[Die] = []
    cur: Die | None = None
    in_loc = False
    for line in text.splitlines():
        m = DIE_RE.match(line)
        if m:
            in_loc = False
            depth = len(m.group(2))
            if m.group(3) == "NULL":
                cur = None
                continue
            die = Die(m.group(1), depth, m.group(3))
            while stack and stack[-1].depth >= depth:
                stack.pop()
            if stack:
                die.parent = stack[-1]
                stack[-1].children.append(die)
            else:
                roots.append(die)
            stack.append(die)
            cur = die
            continue
        if cur is None:
            continue
        a = ATTR_RE.match(line)
        if a:
            cur.attrs[a.group(1)] = a.group(2)
            in_loc = a.group(1) == "DW_AT_location"
            continue
        if in_loc and RANGE_RE.search(line):
            cur.loc_lines.append(line.strip().rstrip(")"))
    return roots


def all_dies(roots: list[Die]):
    for root in roots:
        yield from root.walk()


def function_die(roots: list[Die], name: str) -> Die | None:
    for d in all_dies(roots):
        if d.tag == "DW_TAG_subprogram" and d.name == name and d.pc_range():
            return d
    return None


def var_in(func: Die | None, name: str) -> Die | None:
    if func is None:
        return None
    for d in func.walk():
        if d.tag == "DW_TAG_variable" and d.name == name:
            return d
    return None


def line_rows(line_text: str) -> list[tuple[int, int]]:
    return [(int(m.group(1), 16), int(m.group(2))) for m in map(LINE_RE.match, line_text.splitlines()) if m]


def stmt_lines(rows: list[tuple[int, int]], func: Die | None) -> list[int]:
    """is_stmt lines inside the function's address range, consecutive repeats merged."""
    if func is None:
        return []
    lo, hi = func.pc_range()  # type: ignore[misc]
    out: list[int] = []
    for addr, line in rows:
        if lo <= addr < hi and (not out or out[-1] != line):
            out.append(line)
    return out


def non_monotonic(seq: list[int]) -> bool:
    return any(seq[i] > seq[i + 1] for i in range(len(seq) - 1))


def disasm_by_symbol(obj: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    sym = ""
    for line in get_cmd_output(["objdump", "-d", "-r", obj]).splitlines():
        m = re.match(r"^[0-9a-f]+ <(\w+)>:", line)
        if m:
            sym = m.group(1)
            out[sym] = []
        elif sym:
            out[sym].append(line)
    return out


def branch_to(body: list[str], target: str) -> str:
    """Mnemonic of the instruction whose relocation names `target`."""
    prev = ""
    for line in body:
        if re.search(r"RELOC_BRANCH\w*\s+" + re.escape(target) + r"$", line.strip()):
            return prev
        m = re.match(r"^\s+[0-9a-f]+:\s+[0-9a-f]+\s+(\S+)", line)
        if m:
            prev = m.group(1)
    return "none"


def entry_symbol(syms: dict[str, list[str]], real: str) -> str:
    """The first function in a Mach-O object is labelled <ltmp0>; map it to its own name."""
    return real if real in syms else "ltmp0"


def piece_slices(var: Die | None) -> list[str]:
    out = []
    if var:
        for ln in var.loc_lines:
            m = RANGE_RE.search(ln)
            if m and PIECE_RE.search(m.group(3)):
                out.append(f"[ADDR_RANGE]: {m.group(3).rstrip(')')}")
    return out


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(f"Usage: {sys.argv[0]} <obj_o0> <obj_o2>")

    obj_o0, obj_o2 = sys.argv[1], sys.argv[2]
    dtool = find_dwarf_tool()
    r0 = parse_dies(get_cmd_output([dtool, "--debug-info", obj_o0]))
    r2 = parse_dies(get_cmd_output([dtool, "--debug-info", obj_o2]))
    tp0, tp2 = function_die(r0, "transform_point"), function_die(r2, "transform_point")
    cd0 = function_die(r0, "compute_distance_sq")
    vs0, vs2 = function_die(r0, "vector_sum"), function_die(r2, "vector_sum")
    if not all((tp0, tp2, cd0, vs0, vs2)):
        sys.exit("Error: expected subprograms not found in the DWARF.")

    print("=== Compiler Pipeline Optimization and Debuggability Lab ===")
    print()

    # 1. Inlining Analysis
    inl0 = [d for d in all_dies(r0) if d.tag == "DW_TAG_inlined_subroutine"]
    inl2 = [d for d in tp2.walk() if d.tag == "DW_TAG_inlined_subroutine"]
    origin_ok = bool(inl2) and "compute_distance_sq" in inl2[0].attrs.get("DW_AT_abstract_origin", "")
    call_line = re.sub(r"\D", "", inl2[0].attrs.get("DW_AT_call_line", "")) if inl2 else "unknown"
    print("1. Inlining (Call Boundary Dissolution):")
    print(f"  - O0 inlined subroutine DIE present: {bool(inl0)}")
    print(f"  - O0 compute_distance_sq has its own address range: {cd0.pc_range() is not None}")
    print(f"  - O2 inlined subroutine DIE present: {bool(inl2)}")
    print(f"  - O2 inlined subroutine sits inside transform_point and originates from compute_distance_sq: {origin_ok}")
    print(f"  - O2 inlined call site recorded: line {call_line}")
    print("  - Debugger symptom: stepping enters inlined body without physical call frame")
    print()

    # 2. SROA (Scalar Replacement of Aggregates) Analysis
    pt0, pt2 = var_in(tp0, "pt"), var_in(tp2, "pt")
    pt0_loc = re.sub(r"\)$", "", pt0.attrs.get("DW_AT_location", "unknown")) if pt0 else "unknown"
    slices = piece_slices(pt2)
    hole = any(s.split(": ", 1)[1].startswith("DW_OP_piece") for s in slices)
    print("2. SROA (Aggregate Decomposition into Registers):")
    print(f"  - O0 'pt' location expression: {pt0_loc} (contiguous stack storage)")
    print(f"  - O2 'pt' uses DW_OP_piece composite locations: {bool(slices)}")
    if slices:
        print("  - O2 piecewise variable location slices:")
        for sl in slices:
            print(f"      {sl}")
    print(f"  - O2 'pt' has a range where piece 0 (x) has no location: {hole}")
    print("  - Debugger symptom: inspecting struct yields <optimized out> or partial fields")
    print()

    # 3. Tail Call Elimination Analysis
    cs0 = [d for d in tp0.walk() if d.tag == "DW_TAG_call_site"]
    cs2 = [d for d in tp2.walk() if d.tag == "DW_TAG_call_site"]
    tail0 = any("DW_AT_call_tail_call" in d.attrs for d in cs0)
    tail2 = any("DW_AT_call_tail_call" in d.attrs for d in cs2)
    dis0, dis2 = disasm_by_symbol(obj_o0), disasm_by_symbol(obj_o2)
    br0 = branch_to(dis0.get(entry_symbol(dis0, "_transform_point"), []), "_external_sink")
    br2 = branch_to(dis2.get(entry_symbol(dis2, "_transform_point"), []), "_external_sink")
    print("3. Tail Call Elimination (Frame Destruction):")
    print(f"  - O0 DW_AT_call_tail_call attribute in transform_point: {tail0}")
    print(f"  - O2 DW_AT_call_tail_call attribute in transform_point: {tail2}")
    print(f"  - O0 transform_point reaches external_sink with: {br0}")
    print(f"  - O2 transform_point reaches external_sink with: {br2}")
    print("  - Debugger symptom: caller frame vanishes from call stack during tail call")
    print()

    # 4. Line Table Non-Monotonicity and Statement Boundaries
    rows0 = line_rows(get_cmd_output([dtool, "--debug-line", obj_o0]))
    rows2 = line_rows(get_cmd_output([dtool, "--debug-line", obj_o2]))
    seq_tp0, seq_tp2 = stmt_lines(rows0, tp0), stmt_lines(rows2, tp2)
    seq_cd0 = stmt_lines(rows0, cd0)
    print("4. Line Table and Statement Progression (.debug_line):")
    print(f"  - O0 transform_point statement line sequence: {seq_tp0}")
    print(f"  - O0 compute_distance_sq statement line sequence: {seq_cd0}")
    print(f"  - O2 transform_point statement line sequence: {seq_tp2}")
    print(f"  - O0 transform_point non-monotonic: {non_monotonic(seq_tp0)}")
    print(f"  - O2 transform_point non-monotonic (backwards line jump): {non_monotonic(seq_tp2)}")
    print(f"  - O2 transform_point has a statement row for line 26: {26 in seq_tp2}")
    print("  - Debugger symptom: single-stepping cursor jumps backward and skips intermediate lines")
    print()

    # 5. Loop Vectorization Induction Variable Tracking
    i0, i2 = var_in(vs0, "i"), var_in(vs2, "i")
    i0_stack = bool(i0) and "DW_OP_fbreg" in i0.attrs.get("DW_AT_location", "")
    const_bytes = 0
    for ln in (i2.loc_lines if i2 else []):
        m = RANGE_RE.search(ln)
        if m and "DW_OP_consts +0, DW_OP_stack_value" in m.group(3):
            const_bytes += int(m.group(2), 16) - int(m.group(1), 16)
    vs2_lo, vs2_hi = vs2.pc_range()  # type: ignore[misc]
    vec0 = sum("fadd.4s" in ln for ln in dis0.get("_vector_sum", []))
    vec2 = sum("fadd.4s" in ln for ln in dis2.get("_vector_sum", []))
    print("5. Loop Vectorization Induction Variable Tracking:")
    print(f"  - O0 induction variable 'i' tracked in stack frame: {i0_stack}")
    print(f"  - O0 vector_sum contains fadd.4s: {vec0 > 0}")
    print(f"  - O2 vector_sum contains fadd.4s: {vec2 > 0}")
    print(f"  - O2 induction variable 'i' collapsed to constant value: {const_bytes > 0}")
    print(f"  - O2 bytes of vector_sum where 'i' is the constant 0: {const_bytes} of {vs2_hi - vs2_lo}")
    print("  - Debugger symptom: inspecting 'i' inside vectorized loop prints constant 0")
    print()

    # 6. Quantitative DWARF Location Coverage Statistics
    stat0 = json.loads(get_cmd_output([dtool, "--statistics", obj_o0]))
    stat2 = json.loads(get_cmd_output([dtool, "--statistics", obj_o2]))
    full = "#local vars with 100% of parent scope covered by DW_AT_location"
    cov = "sum_all_local_vars(#bytes in parent scope covered by DW_AT_location)"
    scope = "sum_all_local_vars(#bytes in parent scope)"
    print("6. Quantitative DWARF Quality Metrics (dwarfdump --statistics):")
    for tag, st in (("O0", stat0), ("O2", stat2)):
        c, s = st.get(cov, 0), st.get(scope, 0)
        pct = (100.0 * c / s) if s else 0.0
        print(f"  - {tag} local variables with 100% scope coverage: {st.get(full, 0)}")
        print(f"  - {tag} local variable location coverage: {c} of {s} scope bytes ({pct:.1f}%)")
    print(f"  - O2 covered bytes that are the constant-0 location of 'i': {const_bytes} of {stat2.get(cov, 0)}")


if __name__ == "__main__":
    main()
