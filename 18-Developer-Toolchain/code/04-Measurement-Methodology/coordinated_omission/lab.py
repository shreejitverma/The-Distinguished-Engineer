"""Coordinated omission, HdrHistogram internals, and robust benchmark statistics.

Everything is simulated in integer microseconds or with a seeded PRNG, so the
output is identical on every run and every platform. Each section ends with a
check that raises if the mechanism it demonstrates were absent.
"""

from __future__ import annotations

import random
import statistics
import sys

from hdr import Histogram

# ---------------------------------------------------------------------------
# Part 1: coordinated omission in a simulated request/response benchmark
# ---------------------------------------------------------------------------

INTERVAL_US = 10_000  # intended schedule: one request every 10 ms (100 req/s)
SERVICE_US = 1_000  # every request needs 1 ms of server work
N_REQUESTS = 10_000  # 100 s of schedule
PAUSE_START_US = 50_000_500  # the server freezes 0.5 ms into request 5000
PAUSE_US = 2_000_000  # for 2 s
PAUSE_END_US = PAUSE_START_US + PAUSE_US


def serve(start: int) -> int:
    """Completion time of 1 ms of work that starts at `start`; work is frozen during the pause."""
    done = start + SERVICE_US
    if start < PAUSE_END_US and done > PAUSE_START_US:
        done += PAUSE_END_US - max(start, PAUSE_START_US)
    return done


def open_loop() -> tuple[list[int], int]:
    """Arrivals follow the schedule whatever the server does; FIFO single server."""
    latencies, free, sent_in_pause = [], 0, 0
    for i in range(N_REQUESTS):
        intended = i * INTERVAL_US
        if PAUSE_START_US <= intended < PAUSE_END_US:
            sent_in_pause += 1
        done = serve(max(intended, free))
        free = done
        latencies.append(done - intended)
    return latencies, sent_in_pause


def closed_loop_catch_up() -> tuple[list[int], list[int], int]:
    """One connection: send at the scheduled time, or as soon as the previous reply arrives if late.

    Returns latency measured from the actual send (what wrk and most harnesses record) and
    latency measured from the intended send time (what wrk2 records).
    """
    from_send, from_intended, prev_done, sent_in_pause = [], [], 0, 0
    for i in range(N_REQUESTS):
        intended = i * INTERVAL_US
        send = max(intended, prev_done)
        if PAUSE_START_US <= send < PAUSE_END_US:
            sent_in_pause += 1
        done = serve(send)
        prev_done = done
        from_send.append(done - send)
        from_intended.append(done - intended)
    return from_send, from_intended, sent_in_pause


def closed_loop_skip() -> tuple[list[int], int, int]:
    """One connection: after each reply, sleep until the next tick; ticks that already passed are skipped."""
    latencies, tick, sent_in_pause, skipped = [], 0, 0, 0
    while tick < N_REQUESTS:
        send = tick * INTERVAL_US
        if PAUSE_START_US <= send < PAUSE_END_US:
            sent_in_pause += 1
        done = serve(send)
        latencies.append(done - send)
        next_tick = -(-done // INTERVAL_US)  # first tick at or after the reply
        skipped += max(0, next_tick - tick - 1)
        tick = max(next_tick, tick + 1)
    return latencies, sent_in_pause, skipped


def hdr_of(values: list[int], expected_interval: int = 0) -> tuple[Histogram, int]:
    h = Histogram(1, 3_600_000_000, 3)
    synthetic = 0
    for v in values:
        synthetic += h.record_with_expected_interval(v, expected_interval)
    return h, synthetic


def ms(us: int) -> str:
    return f"{us / 1000:.3f}"


PCTS = (50.0, 90.0, 99.0, 99.9, 99.99)


def part1() -> None:
    print("== Part 1: coordinated omission (simulated, integer microseconds)")
    print(
        f"schedule: {N_REQUESTS} requests, one every {INTERVAL_US // 1000} ms; service {SERVICE_US // 1000} ms;"
        f" one server pause of {PAUSE_US // 1000} ms at t=50.0005 s"
    )

    truth, open_in_pause = open_loop()
    send_lat, intended_lat, catch_in_pause = closed_loop_catch_up()
    skip_lat, skip_in_pause, skipped = closed_loop_skip()

    rows = []
    h_skip, _ = hdr_of(skip_lat)
    rows.append(("closed, skip missed ticks, raw", h_skip))
    h_skip_fix, synthetic = hdr_of(skip_lat, INTERVAL_US)
    rows.append(("closed, skip, recordValueWithExpectedInterval", h_skip_fix))
    h_send, _ = hdr_of(send_lat)
    rows.append(("closed, catch up, from actual send", h_send))
    h_intended, _ = hdr_of(intended_lat)
    rows.append(("closed, catch up, from intended send", h_intended))
    h_truth, _ = hdr_of(truth)
    rows.append(("open loop (ground truth)", h_truth))

    print()
    print("Requests sent while the server was paused:")
    print(f"  open loop: {open_in_pause}; closed catch-up: {catch_in_pause}; closed skip: {skip_in_pause}")
    print(f"  closed skip dropped {skipped} scheduled requests; the correction synthesized {synthetic} values")
    print()
    header = f"  {'generator and recording':<47}{'count':>6}" + "".join(f"{'p' + format(p, 'g'):>10}" for p in PCTS)
    print("HdrHistogram(1, 3600000000, 3) percentiles in ms (highest equivalent value):")
    print(header + f"{'max':>10}")
    for name, h in rows:
        cells = "".join(f"{ms(h.value_at_percentile(p)):>10}" for p in PCTS)
        print(f"  {name:<47}{h.total_count:>6}{cells}{ms(h.max()):>10}")

    elevated = sum(1 for v in truth if v > SERVICE_US)
    print()
    print(
        f"Ground truth: {elevated} of {N_REQUESTS} requests ({100 * elevated / N_REQUESTS:.2f}%) waited longer than"
        f" the {SERVICE_US // 1000} ms service time"
    )
    same = sum(1 for a, b in zip(intended_lat, truth) if a == b)
    print(f"Catch-up loop timed from intended send matches open loop for {same}/{N_REQUESTS} requests")
    raw_p99, true_p99 = h_send.value_at_percentile(99.0), h_truth.value_at_percentile(99.0)
    print(f"p99 understated by the raw closed loop: {true_p99 / raw_p99:.0f}x")

    # Checks that fail if coordinated omission were not happening or the fixes did not work.
    assert catch_in_pause == 0 and skip_in_pause == 0 and open_in_pause == PAUSE_US // INTERVAL_US
    assert raw_p99 == SERVICE_US and true_p99 > 100 * SERVICE_US
    assert h_skip.value_at_percentile(99.0) == SERVICE_US  # the raw skip loop hides the pause just as the catch-up loop does
    assert same == N_REQUESTS
    assert synthetic == skipped - 1 and h_skip_fix.value_at_percentile(99.0) > 100 * SERVICE_US


# ---------------------------------------------------------------------------
# Part 2: HdrHistogram bucket internals, checked exhaustively
# ---------------------------------------------------------------------------


def part2() -> None:
    print()
    print("== Part 2: HdrHistogram(lowest=1, highest=3600000000, digits=3) layout")
    h = Histogram(1, 3_600_000_000, 3)
    print(
        f"unitMagnitude={h.unit_magnitude} subBucketCount={h.sub_bucket_count}"
        f" subBucketHalfCount={h.sub_bucket_half_count} bucketCount={h.bucket_count}"
    )
    print(
        f"countsArrayLength={h.counts_length} -> {8 * h.counts_length} bytes of long counts;"
        f" README estimate {h.footprint_estimate_bytes()} bytes"
    )
    print()
    print(f"  {'value':>12}{'bucket':>8}{'sub':>6}{'index':>7}{'lowest equiv':>14}{'highest equiv':>15}{'width':>8}")
    for v in (1, 1000, 2047, 2048, 2049, 4095, 4096, 1_000_000, 2_001_000, 3_600_000_000):
        b = h.bucket_index(v)
        print(
            f"  {v:>12}{b:>8}{h.sub_bucket_index(v, b):>6}{h.counts_index(v):>7}"
            f"{h.lowest_equivalent(v):>14}{h.highest_equivalent(v):>15}{h.size_of_equivalent_range(v):>8}"
        )

    # Walk every slot: slots must tile [0, top] with no gap or overlap, the index function must
    # invert value_from_index, and every slot at or above 2048 must be narrower than 1/1024 of its value.
    worst_ratio, prev_hi = 0.0, -1
    for i in range(h.counts_length):
        lo = h.value_from_index(i)
        hi = h.highest_equivalent(lo)
        assert lo == prev_hi + 1, (i, lo, prev_hi)
        assert h.counts_index(lo) == i and h.counts_index(hi) == i
        width = hi - lo + 1
        if lo >= h.sub_bucket_count:
            worst_ratio = max(worst_ratio, width / lo)
        else:
            assert width == 1
        prev_hi = hi
    assert prev_hi >= h.highest_trackable
    print()
    print(
        f"Exhaustive slot walk: {h.counts_length} slots tile [0, {prev_hi}] with no gaps;"
        f" values below {h.sub_bucket_count} are exact;"
        f" worst slot width / value above that = 1/{round(1 / worst_ratio)}"
    )
    assert worst_ratio <= 1 / 1000

    # Quantization error of reported percentiles against exact nearest-rank percentiles.
    truth, _ = open_loop()
    exact_sorted = sorted(truth)
    ht, _ = hdr_of(truth)
    print()
    print("Reported percentile vs exact nearest-rank on the open-loop data (us):")
    worst_err = 0.0
    for basis_points in (5_000, 9_900, 9_990, 10_000):
        p = basis_points / 100
        rank = max(1, -(-basis_points * len(exact_sorted) // 10_000))  # ceil(p/100 * n), in integers
        exact = exact_sorted[rank - 1]
        reported = ht.value_at_percentile(p)
        err = (reported - exact) / exact
        worst_err = max(worst_err, err)
        print(f"  p{p:g}: exact {exact}, HdrHistogram {reported}, over by {100 * err:.3f}%")
        assert exact <= reported <= ht.highest_equivalent(exact)
    assert worst_err < 1 / 1000


# ---------------------------------------------------------------------------
# Part 3: robust statistics for a noisy microbenchmark comparison
# ---------------------------------------------------------------------------

SEED = 20261108
N_SAMPLES = 200
N_BOOT = 10_000


def noisy_timings(rng: random.Random, body_us: float, n: int) -> list[float]:
    """A tight body with +-2% noise, plus a 6% heavy tail of 1.5x to 4x (interrupts, migrations)."""
    out = []
    for _ in range(n):
        u = rng.random()
        jitter = body_us * (0.98 + 0.04 * rng.random())
        out.append(jitter * (1.5 + 2.5 * rng.random()) if u < 0.06 else jitter)
    return out


def mad(xs: list[float]) -> float:
    m = statistics.median(xs)
    return statistics.median([abs(x - m) for x in xs])


def resample(rng: random.Random, xs: list[float]) -> list[float]:
    n = len(xs)
    return [xs[int(rng.random() * n)] for _ in range(n)]


def percentile_ci(stats: list[float], level: float = 0.95) -> tuple[float, float]:
    s = sorted(stats)
    lo = s[int(((1 - level) / 2) * len(s))]
    hi = s[int((1 - (1 - level) / 2) * len(s)) - 1]
    return lo, hi


def part3() -> None:
    print()
    print(f"== Part 3: robust statistics (seeded PRNG, random.Random({SEED}).random() only)")
    rng = random.Random(SEED)
    a = noisy_timings(rng, 100.0, N_SAMPLES)
    b = noisy_timings(rng, 97.0, N_SAMPLES)  # B's body is 3% faster
    k = 1 / statistics.NormalDist().inv_cdf(0.75)
    print(f"MAD consistency constant 1/Phi^-1(0.75) = {k:.4f}")
    print()
    print(f"  {'variant':<8}{'mean':>9}{'stdev':>9}{'median':>9}{'MAD':>8}{'1.4826*MAD':>12}{'max':>9}")
    for name, xs in (("A", a), ("B", b)):
        print(
            f"  {name:<8}{statistics.fmean(xs):>9.2f}{statistics.stdev(xs):>9.2f}{statistics.median(xs):>9.2f}"
            f"{mad(xs):>8.2f}{k * mad(xs):>12.2f}{max(xs):>9.2f}"
        )

    # Normal-theory 95% interval for the difference of means (Welch standard error, z = 1.96).
    da = statistics.fmean(a) - statistics.fmean(b)
    se = (statistics.variance(a) / len(a) + statistics.variance(b) / len(b)) ** 0.5
    tail_a = sum(1 for x in a if x > 1.2 * statistics.median(a))
    tail_b = sum(1 for x in b if x > 1.2 * statistics.median(b))
    point_ratio = statistics.median(b) / statistics.median(a)
    print()
    print(f"Tail samples above 1.2x the median: A {tail_a}, B {tail_b} (both drawn with a 6% tail rate)")
    print(f"Difference of means A-B: {da:.2f} us, normal 95% CI [{da - 1.96 * se:.2f}, {da + 1.96 * se:.2f}]")
    print(
        f"Ratio of means B/A: {statistics.fmean(b) / statistics.fmean(a):.4f};"
        f" ratio of medians B/A: {point_ratio:.4f}; true body ratio 0.9700"
    )

    boot_rng = random.Random(SEED + 1)
    ratios, med_a, med_b = [], [], []
    for _ in range(N_BOOT):
        ma = statistics.median(resample(boot_rng, a))
        mb = statistics.median(resample(boot_rng, b))
        med_a.append(ma)
        med_b.append(mb)
        ratios.append(mb / ma)
    ca, cb, cr = percentile_ci(med_a), percentile_ci(med_b), percentile_ci(ratios)
    print(f"Bootstrap ({N_BOOT} resamples) 95% percentile CI of median A: [{ca[0]:.2f}, {ca[1]:.2f}]")
    print(f"Bootstrap ({N_BOOT} resamples) 95% percentile CI of median B: [{cb[0]:.2f}, {cb[1]:.2f}]")
    print(f"Bootstrap 95% CI of median(B)/median(A): [{cr[0]:.4f}, {cr[1]:.4f}]")
    mean_ci_pct = 100 * 2 * 1.96 * se / statistics.median(a)
    median_ci_pct = 100 * (cr[1] - cr[0])
    print(
        f"95% CI width as % of A's median: difference of means {mean_ci_pct:.2f}%,"
        f" ratio of medians {median_ci_pct:.2f}%"
    )
    verdict = "B faster" if cr[1] < 1.0 else ("B slower" if cr[0] > 1.0 else "no detectable change")
    print(f"Verdict from the median ratio: {verdict}")

    # The mechanism: the tail inflates stdev far beyond MAD, the median CI is far tighter than
    # the mean CI, and the median ratio contains the true 0.97 while the mean ratio does not.
    assert statistics.stdev(a) > 5 * k * mad(a)
    assert cr[0] <= 0.97 <= cr[1] < 1.0
    assert median_ci_pct < mean_ci_pct / 10
    assert abs(statistics.fmean(b) / statistics.fmean(a) - 0.97) > 0.03


def main() -> int:
    part1()
    part2()
    part3()
    return 0


if __name__ == "__main__":
    sys.exit(main())
