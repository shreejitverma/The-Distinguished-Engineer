"""Top-Down Microarchitecture Analysis (TMA) level 1, computed from counter values.

Every counter value in this file is an illustrative input chosen for the arithmetic,
not a measurement. The formulas are the published ones:

- Skylake-class Intel (4 slots per cycle, SMT off): Yasin 2014, Table 7, and
  tools/perf/pmu-events/arch/x86/skylake/skl-metrics.json in Linux v7.3.
- Ice Lake and later Intel: the PERF_METRICS register, four 8-bit fractions of
  0xff, decoded as in tools/perf/Documentation/topdown.txt in Linux v7.3.
- Arm (SBSA common metrics): tools/perf/pmu-events/arch/arm64/sbsa.json in Linux v7.3.

The script also shows why level 1 must come from one counter group: mixing counts
from different multiplexed time slices can drive Backend Bound below zero.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkylakeCounts:
    clks: int  # CPU_CLK_UNHALTED.THREAD
    idq_uops_not_delivered: int  # IDQ_UOPS_NOT_DELIVERED.CORE
    uops_issued: int  # UOPS_ISSUED.ANY
    uops_retired_slots: int  # UOPS_RETIRED.RETIRE_SLOTS
    recovery_cycles: int  # INT_MISC.RECOVERY_CYCLES


def add(a: SkylakeCounts, b: SkylakeCounts) -> SkylakeCounts:
    return SkylakeCounts(
        a.clks + b.clks,
        a.idq_uops_not_delivered + b.idq_uops_not_delivered,
        a.uops_issued + b.uops_issued,
        a.uops_retired_slots + b.uops_retired_slots,
        a.recovery_cycles + b.recovery_cycles,
    )


def tma_skylake(c: SkylakeCounts, width: int = 4) -> dict[str, float]:
    slots = width * c.clks
    fe = c.idq_uops_not_delivered / slots
    bs = (c.uops_issued - c.uops_retired_slots + width * c.recovery_cycles) / slots
    ret = c.uops_retired_slots / slots
    return {
        "Frontend Bound": fe,
        "Bad Speculation": bs,
        "Retiring": ret,
        "Backend Bound": 1.0 - fe - bs - ret,
    }


def show(title: str, m: dict[str, float]) -> None:
    print(f"  {title}")
    for k in ("Retiring", "Bad Speculation", "Frontend Bound", "Backend Bound"):
        print(f"    {k:<16} {100.0 * m[k]:7.2f}%")
    print(f"    {'sum':<16} {100.0 * sum(m.values()):7.2f}%")


# Illustrative profiles, 1,000,000 cycles each.
BRANCHY = SkylakeCounts(
    clks=1_000_000,
    idq_uops_not_delivered=400_000,
    uops_issued=2_600_000,
    uops_retired_slots=1_400_000,
    recovery_cycles=100_000,
)
CHASE = SkylakeCounts(
    clks=1_000_000,
    idq_uops_not_delivered=80_000,
    uops_issued=210_000,
    uops_retired_slots=200_000,
    recovery_cycles=2_000,
)
FRONTEND = SkylakeCounts(
    clks=1_000_000,
    idq_uops_not_delivered=2_400_000,
    uops_issued=1_000_000,
    uops_retired_slots=950_000,
    recovery_cycles=10_000,
)


def part_a() -> None:
    print("[A] Skylake-class level 1, slots = 4 * CPU_CLK_UNHALTED.THREAD")
    show("branchy profile (illustrative counts)", tma_skylake(BRANCHY))
    show("pointer-chase profile (illustrative counts)", tma_skylake(CHASE))
    show("frontend-starved profile (illustrative counts)", tma_skylake(FRONTEND))


def part_b() -> None:
    print("[B] multiplexing hazard: half the run is frontend-starved, half is branchy")
    whole = add(FRONTEND, BRANCHY)
    show("one group, all five events counted together", tma_skylake(whole))
    # Each general-purpose event ran in only half the time slices and was scaled by
    # enabled/running = 2. IDQ_UOPS_NOT_DELIVERED happened to run in the frontend half,
    # the other three in the branchy half; cycles sit in a fixed counter all the time.
    mixed = SkylakeCounts(
        clks=whole.clks,
        idq_uops_not_delivered=2 * FRONTEND.idq_uops_not_delivered,
        uops_issued=2 * BRANCHY.uops_issued,
        uops_retired_slots=2 * BRANCHY.uops_retired_slots,
        recovery_cycles=2 * BRANCHY.recovery_cycles,
    )
    m = tma_skylake(mixed)
    show("events from different slices, each scaled by enabled/running = 2", m)
    print(f"  Backend Bound below zero: {'yes' if m['Backend Bound'] < 0 else 'no'}")


def get_metric(value: int, i: int) -> int:
    return (value >> (8 * i)) & 0xFF


def part_c() -> None:
    print("[C] Ice Lake and later: PERF_METRICS bytes are fractions of 0xff")
    # Bytes, low to high: retiring, bad spec, FE bound, BE bound, then (Sapphire
    # Rapids and later) heavy ops, branch mispredict, fetch latency, memory bound.
    metrics_a, slots_a = 0x1E05_0A0F_7C0A_1E5B, 5_000_000
    metrics_b, slots_b = 0x5203_0614_AE05_0F3D, 15_000_000
    for name, v in (("metrics_a", metrics_a), ("metrics_b", metrics_b)):
        level1 = sum(get_metric(v, i) for i in range(4))
        print(f"  {name} = 0x{v:016X}: level-1 bytes sum to 0x{level1:02X}")
    names = (
        "Retiring",
        "Bad Speculation",
        "Frontend Bound",
        "Backend Bound",
        "Heavy Operations",
        "Branch Mispredicts",
        "Fetch Latency",
        "Memory Bound",
    )
    delta = slots_b - slots_a
    region = {}
    for i, n in enumerate(names):
        # Scale each fraction by the slots read at the same instant, then take deltas.
        s = (
            get_metric(metrics_b, i) * slots_b / 0xFF
            - get_metric(metrics_a, i) * slots_a / 0xFF
        )
        region[n] = s / delta
    print(f"  region between the two reads: {delta:,} slots")
    for n in names[:4]:
        print(f"    {n:<19} {100.0 * region[n]:6.2f}%")
    print("  level 2 by subtraction")
    print(
        f"    {'Light Operations':<19} {100.0 * (region['Retiring'] - region['Heavy Operations']):6.2f}%"
        "  = Retiring - Heavy Operations"
    )
    print(
        f"    {'Machine Clears':<19} {100.0 * (region['Bad Speculation'] - region['Branch Mispredicts']):6.2f}%"
        "  = Bad Speculation - Branch Mispredicts"
    )
    print(
        f"    {'Fetch Bandwidth':<19} {100.0 * (region['Frontend Bound'] - region['Fetch Latency']):6.2f}%"
        "  = Frontend Bound - Fetch Latency"
    )
    print(
        f"    {'Core Bound':<19} {100.0 * (region['Backend Bound'] - region['Memory Bound']):6.2f}%"
        "  = Backend Bound - Memory Bound"
    )
    naive = get_metric(metrics_b, 0) * slots_b - get_metric(metrics_a, 0) * slots_a
    print(f"  forgetting the /0xff gives Retiring = {100.0 * naive / delta:,.0f}%")


def part_d() -> None:
    print("[D] Arm SBSA level 1 (perf sbsa.json), #slots from the PMU, here 8")
    slots, cycles = 8, 1_000_000
    op_spec, op_retired = 3_000_000, 2_400_000
    stall_slot, stall_fe, stall_be = 4_000_000, 1_000_000, 3_000_000
    busy = 1 - stall_slot / (slots * cycles)
    m = {
        "Frontend Bound": stall_fe / (slots * cycles),
        "Backend Bound": stall_be / (slots * cycles),
        "Retiring": (op_retired / op_spec) * busy,
        "Bad Speculation": (1 - op_retired / op_spec) * busy,
    }
    show("illustrative counts", m)


if __name__ == "__main__":
    part_a()
    part_b()
    part_c()
    part_d()
