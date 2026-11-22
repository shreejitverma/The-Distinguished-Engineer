"""Lab for: load testing with Locust 2.47.0, open vs closed models, Coordinated Omission."""

from __future__ import annotations

import os
import random
import statistics
import subprocess
import sys
from itertools import pairwise

import pytest

from api_labs.f18_observability_profiling_and_operations import load_testing_workload_models as m

RATE = 100.0
SERVICE_S = 0.005
DURATION_S = 60.0
FREEZE = m.Freeze(start_s=20.0, duration_s=2.0)


def _server() -> m.FifoServer:
    return m.FifoServer(SERVICE_S, FREEZE)


def _closed() -> list[m.Sample]:
    return m.run_closed_loop_pacing(_server(), users=1, rate_per_s=RATE, duration_s=DURATION_S)


def _open() -> list[m.Sample]:
    return m.run_open_loop(_server(), m.constant_arrivals(RATE, DURATION_S))


@pytest.mark.unit
def test_freeze_stalls_in_service_and_arriving_requests() -> None:
    server = m.FifoServer(0.005, m.Freeze(start_s=1.0, duration_s=2.0))
    assert server.serve(0.0) == pytest.approx(0.005)  # before the freeze
    assert server.serve(0.998) == pytest.approx(3.003)  # in service when the freeze begins
    assert server.serve(1.5) == pytest.approx(3.008)  # arrives mid-freeze, queues behind
    assert server.serve(5.0) == pytest.approx(5.005)  # long after, unaffected


@pytest.mark.unit
def test_closed_loop_hides_a_two_second_freeze_from_p99() -> None:
    closed = _closed()
    observed = m.latencies_s(closed, corrected=False)
    assert m.nearest_rank_percentile(observed, 99) == pytest.approx(0.005)
    assert m.nearest_rank_percentile(observed, 99.9) == pytest.approx(0.005)
    # The stall is in the data, but as exactly one sample.
    assert m.nearest_rank_percentile(observed, 100) == pytest.approx(2.005)
    assert sum(1 for v in observed if v > 0.1) == 1
    # And 199 scheduled sends were never made: the generator coordinated with the server.
    assert len(closed) == 5801
    assert len(_open()) == 6000


@pytest.mark.unit
def test_open_loop_exposes_the_freeze_in_p99() -> None:
    open_samples = _open()
    latencies = m.latencies_s(open_samples, corrected=False)
    assert m.nearest_rank_percentile(latencies, 99) == pytest.approx(1.705)
    assert m.nearest_rank_percentile(latencies, 50) == pytest.approx(0.005)
    # 200 arrivals land in the freeze, and the backlog keeps another 200 requests above 5 ms.
    assert sum(1 for v in latencies if v > 0.0051) == 400


@pytest.mark.unit
def test_locust_reports_the_closed_loop_artifact_through_its_own_stats() -> None:
    closed_p99 = m.locust_reported_percentile_ms(m.latencies_s(_closed(), corrected=False), 99)
    open_p99 = m.locust_reported_percentile_ms(m.latencies_s(_open(), corrected=False), 99)
    assert closed_p99 == 5
    assert open_p99 == 1700  # 1705 ms bucketed to two significant digits


@pytest.mark.unit
def test_closed_loop_offered_load_collapses_during_the_freeze() -> None:
    assert m.sent_in_window(_closed(), 30.0, 32.0) == 200  # healthy baseline: 100 per second
    assert m.sent_in_window(_open(), 30.0, 32.0) == 200
    assert m.sent_in_window(_closed(), 20.0, 22.0) == 1  # one request, then silence
    assert m.sent_in_window(_open(), 20.0, 22.0) == 200


@pytest.mark.unit
def test_in_flight_is_capped_by_users_in_a_closed_loop_but_not_in_an_open_one() -> None:
    four = m.run_closed_loop_pacing(_server(), users=4, rate_per_s=RATE, duration_s=DURATION_S)
    assert m.max_in_flight(four) <= 4
    assert m.max_in_flight(_closed()) == 1
    assert m.max_in_flight(_open()) == 201


@pytest.mark.unit
def test_schedule_aware_measurement_recovers_the_open_loop_distribution() -> None:
    aware = m.run_schedule_aware_closed_loop(
        _server(), users=1, rate_per_s=RATE, duration_s=DURATION_S
    )
    assert len(aware) == 6000  # nothing is omitted
    corrected = m.latencies_s(aware, corrected=True)
    truth = m.latencies_s(_open(), corrected=False)
    assert sorted(corrected) == pytest.approx(sorted(truth))
    # Same sender, same server, but measured from the actual send time: the freeze vanishes again.
    naive = m.latencies_s(aware, corrected=False)
    assert m.nearest_rank_percentile(naive, 99) == pytest.approx(0.005)
    assert m.nearest_rank_percentile(corrected, 99) == pytest.approx(1.705)


@pytest.mark.unit
def test_expected_interval_fill_follows_the_hdrhistogram_loop() -> None:
    assert m.expected_interval_fill_us([50], 10) == [50, 40, 30, 20, 10]
    assert m.expected_interval_fill_us([15], 10) == [15]  # 5 is below one interval
    assert m.expected_interval_fill_us([10], 10) == [10]  # equal to the interval: nothing missing
    assert m.expected_interval_fill_us([50], 0) == [50]  # disabled


@pytest.mark.unit
def test_expected_interval_fill_only_partly_corrects_a_stall_that_leaves_a_backlog() -> None:
    closed_us = [round(v * 1e6) for v in m.latencies_s(_closed(), corrected=False)]
    filled = m.expected_interval_fill_us(closed_us, 10_000)
    assert len(filled) == len(closed_us) + 199
    filled_p99 = m.nearest_rank_percentile([v / 1e6 for v in filled], 99)
    open_p99 = m.nearest_rank_percentile(m.latencies_s(_open(), corrected=False), 99)
    # Better than the raw 5 ms, but short of the truth: the fill assumes a 10 ms ramp down and
    # cannot see that the 5 ms server drains the backlog at half that slope.
    assert 1.0 < filled_p99 < open_p99


@pytest.mark.unit
def test_closed_loop_throughput_follows_the_response_time_law() -> None:
    assert m.closed_loop_throughput(100, 0.05, 0.95) == pytest.approx(100.0)
    slow = m.run_closed_loop_pacing(
        m.FifoServer(0.05), users=1, rate_per_s=RATE, duration_s=DURATION_S
    )
    # Asked for 100 per second, a user blocked on a 50 ms server delivers 1 / 0.05 = 20 per second.
    assert len(slow) / DURATION_S == pytest.approx(m.closed_loop_throughput(1, 0.05, 0.0), rel=0.01)


@pytest.mark.unit
def test_poisson_arrivals_are_seeded_and_have_the_requested_rate() -> None:
    first = m.poisson_arrivals(RATE, DURATION_S, seed=7)
    assert first == m.poisson_arrivals(RATE, DURATION_S, seed=7)
    assert first != m.poisson_arrivals(RATE, DURATION_S, seed=8)
    assert len(first) == pytest.approx(RATE * DURATION_S, rel=0.05)
    gaps = [b - a for a, b in pairwise(first)]
    assert statistics.fmean(gaps) == pytest.approx(1 / RATE, rel=0.05)
    # An exponential has standard deviation equal to its mean; constant spacing has none.
    assert statistics.pstdev(gaps) == pytest.approx(1 / RATE, rel=0.1)
    constant_gaps = [b - a for a, b in pairwise(m.constant_arrivals(RATE, 1.0))]
    assert statistics.pstdev(constant_gaps) == pytest.approx(0.0, abs=1e-12)


@pytest.mark.unit
def test_partly_open_session_length_rules_of_thumb() -> None:
    assert m.expected_session_requests(0.8) == pytest.approx(5.0)
    lengths = m.simulate_session_lengths(0.8, sessions=20_000, seed=3)
    assert statistics.fmean(lengths) == pytest.approx(5.0, rel=0.05)
    assert m.classify_session_length(1.0) == "open"
    assert m.classify_session_length(5.0) == "open"
    assert m.classify_session_length(5.01) == "partly-open"
    assert m.classify_session_length(9.99) == "partly-open"
    assert m.classify_session_length(10.0) == "closed"
    with pytest.raises(ValueError, match="p_continue"):
        m.expected_session_requests(1.0)


@pytest.mark.unit
def test_locust_buckets_response_times_to_two_significant_digits() -> None:
    assert [m.locust_bucket_ms(v) for v in (147, 3432, 58760, 99.4)] == [150, 3400, 59000, 99]
    for ms in range(100, 100_000, 7):
        assert abs(m.locust_bucket_ms(ms) - ms) / ms <= 0.05


@pytest.mark.unit
def test_locust_constant_pacing_never_catches_up_after_a_stall() -> None:
    waits = m.locust_pacing_waits(0.01, [0.002, 2.0, 0.002, 0.002])
    assert waits == pytest.approx([0.008, 0.0, 0.008, 0.008])


@pytest.mark.unit
def test_locust_poisson_wait_is_exponential_and_validates_its_rate() -> None:
    os.environ.setdefault("LOCUST_SKIP_MONKEY_PATCH", "1")
    from locust import constant_throughput, poisson

    state = random.getstate()
    try:
        random.seed(1234)
        draws = [poisson(50.0)(None) for _ in range(20_000)]  # type: ignore[arg-type]
    finally:
        random.setstate(state)
    assert statistics.fmean(draws) == pytest.approx(1 / 50.0, rel=0.05)
    assert statistics.pstdev(draws) == pytest.approx(1 / 50.0, rel=0.1)
    with pytest.raises(ValueError, match="positive rate"):
        poisson(0)
    with pytest.raises(ValueError, match="positive rate"):
        constant_throughput(0)


@pytest.mark.unit
def test_a_real_locust_user_cannot_exceed_one_over_its_task_duration() -> None:
    task_s, window_s = 0.05, 0.6
    runs, elapsed = m.count_locust_task_runs(task_s, throughput_per_s=1000.0, window_s=window_s)
    offered = int(window_s * 1000)  # what the wait_time asked for: 600 task runs
    assert runs >= 3  # the user did run
    assert runs <= int(elapsed / task_s) + 1  # a sleep never returns early: one run per 50 ms
    assert runs < offered / 10


def _stdlib_patched(*, via_lab: bool) -> str:
    lab = "api_labs.f18_observability_profiling_and_operations.load_testing_workload_models"
    body = f"import {lab} as m; m.locust_bucket_ms(1)" if via_lab else "import locust"
    code = f"{body}\nfrom gevent import monkey\nprint(monkey.is_module_patched('socket'))"
    env = {k: v for k, v in os.environ.items() if k != "LOCUST_SKIP_MONKEY_PATCH"}
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.mark.unit
def test_plain_import_locust_patches_the_stdlib_but_the_lab_import_does_not() -> None:
    assert _stdlib_patched(via_lab=False) == "True"
    assert _stdlib_patched(via_lab=True) == "False"


@pytest.mark.unit
def test_closed_loop_p99_converges_to_the_open_loop_p99_only_with_enough_users() -> None:
    open_p99 = m.nearest_rank_percentile(m.latencies_s(_open(), corrected=False), 99)
    p99_by_users: dict[int, float] = {}
    for users in (1, 10, 50, 200):
        samples = m.run_closed_loop_pacing(
            _server(), users=users, rate_per_s=RATE, duration_s=DURATION_S
        )
        p99_by_users[users] = m.nearest_rank_percentile(m.latencies_s(samples, corrected=False), 99)
    assert p99_by_users[1] == pytest.approx(0.005)
    assert p99_by_users[10] == pytest.approx(0.005)
    assert p99_by_users[50] == pytest.approx(0.25, rel=1e-3)  # 50 stuck requests, each late
    assert p99_by_users[200] == pytest.approx(open_p99, rel=1e-3)
    assert open_p99 == pytest.approx(1.705)
