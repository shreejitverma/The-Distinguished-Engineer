"""Cut the hot loops out of the compiler's assembly and ask llvm-mca what they should cost.

llvm-mca simulates a straight-line block of machine code as if it ran in a loop, using the
scheduling model LLVM ships for one CPU. It knows instruction latencies, micro-op counts and
which execution resources each instruction needs. It does not know about caches, branch
prediction or the front end, so its answer is a bound for code that hits L1 and predicts well.

The loops are found in the compiler's own output: the first label the compiler marks as an
inner loop header inside a function, through the first branch back to that label.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

LOOP_HEADER = re.compile(r"^(\S+):\s+;\s*=>\s*This Inner Loop Header")
MCA_FIELD = re.compile(r"^(Iterations|Instructions|Total Cycles|Total uOps|Dispatch Width|uOps Per Cycle|IPC|"
                       r"Block RThroughput):\s+(\S+)", re.MULTILINE)
PRESSURE = re.compile(r"^\s*(?:- )?(Resource Pressure|Data Dependencies:|Register Dependencies|Memory Dependencies)"
                      r"\s+\[\s*([0-9.]+)% \]", re.MULTILINE)
ITERATIONS = 1000
MODELS = ("apple-m1", "neoverse-v2")  # the first is the one compared with the measurement


class LoopError(Exception):
    """The assembly does not have the shape this lab relies on."""


def inner_loop(asm: list[str], function: str) -> list[str]:
    """Instructions of the first inner loop of `function`, label first, back edge last."""
    start = next((i for i, line in enumerate(asm) if line.startswith(f"{function}:")), None)
    if start is None:
        raise LoopError(f"function {function} not found")
    body: list[str] = []
    label = None
    for line in asm[start + 1:]:
        if line.startswith("\t.cfi_endproc"):
            break
        header = LOOP_HEADER.match(line)
        if label is None:
            if header:
                label = header.group(1)
                body.append(f"{label}:")
            continue
        text = line.split(";")[0].rstrip()
        if not text.strip():
            continue
        body.append(text)
        fields = text.split()
        if fields[0].startswith("b.") and fields[-1] == label:
            return body
    raise LoopError(f"no inner loop with a back edge found in {function}")


def run_mca(mca: str, cpu: str, body: list[str], extra: tuple[str, ...] = ()) -> str:
    cmd = [mca, "-mtriple=arm64-apple-macos", f"-mcpu={cpu}", f"-iterations={ITERATIONS}", *extra]
    proc = subprocess.run(cmd, input="\n".join(body) + "\n", capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise LoopError(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")
    return proc.stdout


def summary(report: str) -> dict[str, float]:
    fields = {name: float(value) for name, value in MCA_FIELD.findall(report)}
    missing = {"Instructions", "Total Cycles", "IPC", "Block RThroughput", "Dispatch Width"} - fields.keys()
    if missing:
        raise LoopError(f"llvm-mca summary lacks {sorted(missing)}")
    return fields


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("asm", type=Path, help="assembly from the compiler (-S)")
    ap.add_argument("--mca", required=True, help="path to llvm-mca")
    ap.add_argument("--timings", type=Path, required=True, help="phase timings written by the workload")
    args = ap.parse_args()

    asm = args.asm.read_text().splitlines()
    # (label, mangled function, mnemonic that does one unit of work, name of the unit)
    kernels = (
        ("sum_serial", "__Z10sum_serialPKdm", "fadd", "addition"),
        ("sum_unrolled4", "__Z13sum_unrolled4PKdm", "fadd", "addition"),
        ("chase", "__Z5chasePKjjl", "ldr", "hop"),
    )
    try:
        loops = {name: inner_loop(asm, mangled) for name, mangled, _, _ in kernels}
        per_unit: dict[tuple[str, str], float] = {}
        for name, _, mnemonic, unit in kernels:
            body = loops[name]
            units = sum(1 for line in body if line.split()[0] == mnemonic)
            if units == 0:
                raise LoopError(f"{name}: no {mnemonic} in the loop")
            print(f"{name}: inner loop as compiled, {len(body) - 1} instructions, {units} {unit}(s) per iteration")
            for line in body:
                print(f"    {line.strip() if line.endswith(':') else line.expandtabs(8).strip()}")
            for cpu in MODELS:
                fields = summary(run_mca(args.mca, cpu, body))
                cycles = fields["Total Cycles"] / ITERATIONS
                per_unit[(name, cpu)] = cycles / units
                print(f"  llvm-mca -mcpu={cpu}: {cycles:.2f} cycles per iteration, {cycles / units:.2f} per {unit}, "
                      f"IPC {fields['IPC']:.2f}, dispatch width {fields['Dispatch Width']:.0f}, "
                      f"block reciprocal throughput {fields['Block RThroughput']:.1f}")
            pressure = dict(PRESSURE.findall(run_mca(args.mca, MODELS[0], body, ("-bottleneck-analysis",))))
            if "Data Dependencies:" not in pressure or "Resource Pressure" not in pressure:
                raise LoopError(f"{name}: no bottleneck analysis in the llvm-mca report")
            print(f"  bottleneck analysis ({MODELS[0]}): data dependencies {pressure['Data Dependencies:']}% of cycles, "
                  f"resource pressure {pressure['Resource Pressure']}%")
    except LoopError as e:
        print(f"mca_predict: {e}", file=sys.stderr)
        return 1

    cpu_ns = {}
    for line in args.timings.read_text().splitlines():
        name, _wall_ns, ns = line.split()
        cpu_ns[name] = int(ns)
    measured = cpu_ns["serial"] / cpu_ns["unrolled"]
    ok = True
    print("Prediction against measurement")
    for cpu in MODELS:
        predicted = per_unit[("sum_serial", cpu)] / per_unit[("sum_unrolled4", cpu)]
        print(f"  predicted speedup of four accumulators over one, {cpu} model: {predicted:.2f}x")
    predicted = per_unit[("sum_serial", MODELS[0])] / per_unit[("sum_unrolled4", MODELS[0])]
    print(f"    [measured {measured:.2f}x, predicted {predicted:.2f}x]", file=sys.stderr)
    agrees = 0.5 * predicted <= measured <= 1.5 * predicted
    ok = ok and agrees
    print(f"  measured speedup is within 50% of the {MODELS[0]} prediction: {'yes' if agrees else 'NO'}")
    print(f"  predicted cost of one hop, {MODELS[0]} model: {per_unit[('chase', MODELS[0])]:.2f} cycles for either table, "
          "because the instructions are the same")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
