"""Roofline bounds for a few FP64 kernels on an illustrative machine.

Williams, Waterman, and Patterson (CACM 52(4), 2009) define operational intensity as
flops per byte of DRAM traffic, after the caches have filtered it, and bound a kernel by

    attainable = min(peak compute, peak DRAM bandwidth * operational intensity)

The machine below is hypothetical and chosen for round numbers; measure your own peak
with a compute microbenchmark and your own bandwidth with STREAM before trusting a roof.
The byte counts are models of DRAM traffic under the stated cache assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

PEAK_GFLOPS = 2000  # hypothetical: all cores, FMA, full SIMD width
DRAM_GBS = 200  # hypothetical: sustained STREAM-like bandwidth
SIMD_LANES = 8  # FP64 lanes per vector; the no-SIMD ceiling is peak / lanes


@dataclass(frozen=True)
class Kernel:
    name: str
    flops: Fraction  # per element or per tile step
    dram_bytes: Fraction  # DRAM traffic for the same unit of work
    note: str


KERNELS = (
    Kernel("dot   s += x[i]*y[i]", Fraction(2), Fraction(16), "read x and y"),
    Kernel("daxpy y = a*x + y", Fraction(2), Fraction(24), "read x, y; write y"),
    Kernel(
        "triad a = b + s*c",
        Fraction(2),
        Fraction(32),
        "read b, c; write a; +8 B read-for-ownership of a",
    ),
    Kernel(
        "triad, non-temporal store",
        Fraction(2),
        Fraction(24),
        "streaming store skips the read-for-ownership",
    ),
    Kernel(
        "7-point stencil",
        Fraction(8),
        Fraction(24),
        "Williams et al.: 8 flops per 24 B compulsory",
    ),
    Kernel(
        "GEMM tile b=32",
        Fraction(2 * 32**3),
        Fraction(2 * 8 * 32**2),
        "two b x b tiles from DRAM per 2b^3 flops",
    ),
    Kernel(
        "GEMM tile b=128",
        Fraction(2 * 128**3),
        Fraction(2 * 8 * 128**2),
        "same, larger tile kept in cache",
    ),
)


def main() -> None:
    ridge = Fraction(PEAK_GFLOPS, DRAM_GBS)
    print(
        f"machine (hypothetical): {PEAK_GFLOPS} GFLOP/s FP64 peak, {DRAM_GBS} GB/s DRAM"
    )
    print(f"ridge point: {float(ridge):.2f} flop/byte")
    print(
        f"no-SIMD ceiling: {PEAK_GFLOPS // SIMD_LANES} GFLOP/s ({SIMD_LANES} FP64 lanes)"
    )
    print()
    print(f"{'kernel':<27} {'flop/B':>8} {'attainable':>11}  bound")
    for k in KERNELS:
        oi = k.flops / k.dram_bytes
        memory_roof = DRAM_GBS * oi
        attainable = min(Fraction(PEAK_GFLOPS), memory_roof)
        bound = "memory" if memory_roof < PEAK_GFLOPS else "compute"
        print(f"{k.name:<27} {float(oi):8.4f} {float(attainable):8.1f} GF  {bound}")
    print()
    print("notes")
    for k in KERNELS:
        print(f"  {k.name}: {k.note}")
    print()
    # Bytes per flop needed to stay on the compute roof at peak.
    print(f"to run at peak, DRAM may supply at most {float(1 / ridge):.3f} B per flop")
    smallest_tile = next(b for b in range(8, 1025, 8) if Fraction(b, 8) >= ridge)
    print(
        f"smallest GEMM tile b (multiple of 8) whose b/8 flop/B reaches the ridge: {smallest_tile}"
    )


if __name__ == "__main__":
    main()
