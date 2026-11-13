"""Drill driver: one stalling service, four ways of measuring it.

Usage:
    python3 drill.py            # the full report (what run.sh records)
    python3 drill.py symptom    # only the scenario and the symptom, for a blind attempt
"""

from __future__ import annotations

import math
import sys

import loadgen_fixed
import loadgen_naive
import loadgen_resetting
from hdr import HdrHistogram
from sim import StallingService, VirtualClock

RATE_PER_S = 200
DURATION_US = 60_000_000
INTERVAL_US = 1_000_000 // RATE_PER_S
PLANNED = DURATION_US // INTERVAL_US
SERVICE_LOW_US = 800
SERVICE_HIGH_US = 1_200
STALL_US = 125_000
# One stall every 10 s, each starting 400 us after a planned send so that one
# request is frozen in mid-service.
STALLS = [(5_000_400 + i * 10_000_000, STALL_US) for i in range(6)]
SLO_P999_US = 10_000
PERCENTILES = (50.0, 90.0, 99.0, 99.9, 99.99)
# 1 us to 1 hour at 3 significant digits: the example configuration in the HdrHistogram README.
# wrk2 records at the same precision with a 24 hour ceiling.
HIST_LOWEST, HIST_HIGHEST, HIST_DIGITS = 1, 3_600_000_000, 3


class Recorder:
    """An HdrHistogram plus the raw samples, kept only to audit the histogram."""

    def __init__(self) -> None:
        self.hist = HdrHistogram(HIST_LOWEST, HIST_HIGHEST, HIST_DIGITS)
        self.raw: list[int] = []

    def record_value(self, value: int) -> None:
        self.hist.record_value(value)
        self.raw.append(value)


def new_service() -> StallingService:
    return StallingService(STALLS, SERVICE_LOW_US, SERVICE_HIGH_US)


def ms(value_us: int) -> str:
    return f"{value_us / 1000:.3f}"


def row(label: str, hist: HdrHistogram) -> str:
    cells = [ms(hist.value_at_percentile(p)) for p in PERCENTILES] + [ms(hist.max_value())]
    return f"  {label:<34}" + "".join(f"{c:>10}" for c in cells)


def header() -> str:
    names = [f"p{p:g}" for p in PERCENTILES] + ["max"]
    return f"  {'latency in ms':<34}" + "".join(f"{n:>10}" for n in names)


def exact_percentile(sorted_values: list[int], percentile: float) -> int:
    """The same rank rule as getValueAtPercentile, on the unquantized samples."""
    requested = min(max(math.nextafter(percentile, -math.inf), 0.0), 100.0)
    rank = max(math.ceil(requested * len(sorted_values) / 100.0), 1)
    return sorted_values[rank - 1]


def run_naive() -> tuple[Recorder, int, int]:
    clock, recorder = VirtualClock(), Recorder()
    sent = loadgen_naive.run(clock, new_service(), recorder, RATE_PER_S, DURATION_US)
    return recorder, sent, clock.now()


def run_callers() -> Recorder:
    """Ground truth: PLANNED independent callers, one arriving every interval."""
    service, recorder = new_service(), Recorder()
    for k in range(PLANNED):
        arrival = k * INTERVAL_US
        recorder.record_value(service.submit(arrival) - arrival)
    return recorder


def print_scenario() -> None:
    in_stall = sum(
        1 for k in range(PLANNED)
        if any(start <= k * INTERVAL_US < start + length for start, length in STALLS)
    )
    stalled = sum(length for _, length in STALLS)
    print("== Scenario ==")
    print(f"target rate: {RATE_PER_S} req/s, one request every {ms(INTERVAL_US)} ms, for {DURATION_US // 1_000_000} s")
    print(f"planned requests: {PLANNED}")
    print(f"service: one FIFO worker, {ms(SERVICE_LOW_US)} to {ms(SERVICE_HIGH_US)} ms of work per request")
    print(f"stalls: {len(STALLS)} x {ms(STALL_US)} ms, {100 * stalled / DURATION_US:.2f}% of the run")
    print(f"planned sends that fall inside a stall: {in_stall} ({100 * in_stall / PLANNED:.2f}% of the plan)")
    print(f"SLO: p99.9 under {ms(SLO_P999_US)} ms")


def verdict(p999_us: int) -> str:
    return "PASS" if p999_us < SLO_P999_US else "FAIL"


def print_symptom(naive: Recorder, callers: Recorder) -> None:
    naive_p999 = naive.hist.value_at_percentile(99.9)
    callers_p999 = callers.hist.value_at_percentile(99.9)
    print()
    print("== 1. Symptom: the load test and the callers disagree ==")
    print(header())
    print(row("load test report (loadgen_naive)", naive.hist))
    print(row("independent callers, same rate", callers.hist))
    print(f"load test p99.9 {ms(naive_p999)} ms: SLO {verdict(naive_p999)}")
    print(f"callers   p99.9 {ms(callers_p999)} ms: SLO {verdict(callers_p999)}")
    print(f"p99.9 understated by a factor of {callers_p999 / naive_p999:.0f}")


def print_evidence(naive: Recorder, sent: int, elapsed_us: int, callers: Recorder) -> None:
    slow_naive = sum(1 for v in naive.raw if v > INTERVAL_US)
    slow_callers = sum(1 for v in callers.raw if v > INTERVAL_US)
    print()
    print("== 2. Evidence inside the load test's own numbers ==")
    print(f"requests sent: {sent} of {PLANNED} planned ({PLANNED - sent} never sent)")
    print(f"achieved rate: {sent * 1_000_000 / elapsed_us:.2f} req/s against a target of {RATE_PER_S}")
    print(f"samples slower than one send interval: {slow_naive} ({100 * slow_naive / sent:.3f}% of samples)")
    print(f"callers slower than one send interval: {slow_callers} ({100 * slow_callers / PLANNED:.3f}% of callers)")
    p999, top = naive.hist.value_at_percentile(99.9), naive.hist.max_value()
    print(f"p99.9 to max: {ms(p999)} ms to {ms(top)} ms, a jump of {top / p999:.0f}x with nothing in between")


def print_correction(naive: Recorder, callers: Recorder) -> None:
    corrected = HdrHistogram(HIST_LOWEST, HIST_HIGHEST, HIST_DIGITS)
    added = sum(corrected.record_value_with_expected_interval(v, INTERVAL_US) for v in naive.raw)
    one = HdrHistogram(HIST_LOWEST, HIST_HIGHEST, HIST_DIGITS)
    one_added = one.record_value_with_expected_interval(STALL_US, INTERVAL_US)
    print()
    print("== 3. Repair the data: HdrHistogram expected-interval correction ==")
    print(f"one {ms(STALL_US)} ms sample at a {ms(INTERVAL_US)} ms expected interval backfills {one_added} samples, "
          f"{ms(STALL_US - INTERVAL_US)} ms down to {ms(STALL_US - one_added * INTERVAL_US)} ms")
    print(f"samples: {len(naive.raw)} recorded + {added} backfilled = {corrected.total_count}")
    print(header())
    print(row("naive, corrected after the fact", corrected))
    print(row("independent callers, same rate", callers.hist))
    c99, t99 = corrected.value_at_percentile(99.0), callers.hist.value_at_percentile(99.0)
    print(f"corrected p99 is {100 * c99 / t99:.0f}% of the callers' p99: "
          "backfilled samples never queue behind each other")


def print_resetting(naive: Recorder) -> tuple[Recorder, int]:
    clock, recorder = VirtualClock(), Recorder()
    sent = loadgen_resetting.run(clock, new_service(), recorder, RATE_PER_S, DURATION_US)
    print()
    print("== 4. A half-fix: intended-time clock, schedule reset when behind ==")
    print(f"requests sent: {sent} of {PLANNED} planned ({PLANNED - sent} never sent)")
    print(header())
    print(row("loadgen_resetting", recorder.hist))
    p999 = recorder.hist.value_at_percentile(99.9)
    print(f"p99.9 {ms(p999)} ms: SLO {verdict(p999)}")
    print(f"identical to the naive report, sample for sample: {'yes' if recorder.raw == naive.raw else 'no'}")
    return recorder, sent


def print_fixed(callers: Recorder) -> tuple[Recorder, Recorder, int]:
    clock, response, service = VirtualClock(), Recorder(), Recorder()
    sent = loadgen_fixed.run(clock, new_service(), response, service, RATE_PER_S, DURATION_US)
    print()
    print("== 5. Repair the generator: static schedule, latency from intended send time ==")
    print(f"requests sent: {sent} of {PLANNED} planned ({PLANNED - sent} never sent)")
    print(header())
    print(row("response time (from intended send)", response.hist))
    print(row("service time (from actual send)", service.hist))
    print(row("independent callers, same rate", callers.hist))
    p999 = response.hist.value_at_percentile(99.9)
    print(f"response time p99.9 {ms(p999)} ms: SLO {verdict(p999)}")
    late = sum(1 for r, s in zip(response.raw, service.raw, strict=True) if r != s)
    print(f"requests sent late and timed from the plan: {late}")
    return response, service, sent


def print_matrix(rows: list[tuple[str, str, int, Recorder, str]]) -> None:
    print()
    print("== 6. Schedule x clock: both must change ==")
    print(f"  {'schedule':<11}{'clock starts at':<17}{'sent':>6}{'p99.9 ms':>10}  {'SLO':<5} source")
    for schedule, clock_start, sent, recorder, source in rows:
        p999 = recorder.hist.value_at_percentile(99.9)
        print(f"  {schedule:<11}{clock_start:<17}{sent:>6}{ms(p999):>10}  {verdict(p999):<5} {source}")


def print_checks(naive: Recorder, callers: Recorder, response: Recorder) -> None:
    print()
    print("== 7. Checks ==")
    same = response.hist.counts == callers.hist.counts and response.raw == callers.raw
    print(f"fixed generator reproduces the callers' samples exactly: {'yes' if same else 'NO'}")
    worst = 0.0
    equivalent = True
    for recorder in (naive, callers, response):
        ordered = sorted(recorder.raw)
        for p in PERCENTILES + (100.0,):
            exact = exact_percentile(ordered, p)
            reported = recorder.hist.value_at_percentile(p)
            if recorder.hist.lowest_equivalent_value(reported) != recorder.hist.lowest_equivalent_value(exact):
                equivalent = False
            worst = max(worst, abs(reported - exact) / exact)
    print(f"histogram percentiles land in the same bucket as an exact sort: {'yes' if equivalent else 'NO'}")
    print(f"largest quantization error against an exact sort: {100 * worst:.3f}% (3 significant digits allow 0.1%)")
    hist = naive.hist
    print(f"histogram layout for 1 us to 1 hour at 3 digits: {hist.bucket_count} buckets x "
          f"{hist.sub_bucket_count} sub-buckets, {len(hist.counts)} counters")
    if not (same and equivalent and worst <= 0.001):
        raise SystemExit("drill self-check failed")


def main(argv: list[str]) -> int:
    if argv not in ([], ["symptom"]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    naive, sent, elapsed_us = run_naive()
    callers = run_callers()
    print_scenario()
    print_symptom(naive, callers)
    if argv == ["symptom"]:
        return 0
    print_evidence(naive, sent, elapsed_us, callers)
    print_correction(naive, callers)
    resetting, resetting_sent = print_resetting(naive)
    response, service, fixed_sent = print_fixed(callers)
    print_matrix([
        ("resetting", "actual send", sent, naive, "loadgen_naive"),
        ("resetting", "intended send", resetting_sent, resetting, "loadgen_resetting"),
        ("static", "actual send", fixed_sent, service, "loadgen_fixed, service time"),
        ("static", "intended send", fixed_sent, response, "loadgen_fixed, response time"),
    ])
    print_checks(naive, callers, response)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
