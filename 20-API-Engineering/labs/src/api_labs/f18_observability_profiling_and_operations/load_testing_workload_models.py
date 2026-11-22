"""Closed vs open workload models and Coordinated Omission, in deterministic virtual time.

A single FIFO server with a fixed service time can freeze for a while.
Three load generators drive it:

* ``run_closed_loop_pacing``: one request in flight per user, then Locust ``constant_pacing``
  semantics (the schedule is forgotten while the user is blocked).
* ``run_schedule_aware_closed_loop``: still one request in flight per user, but latency is
  measured from the intended send time (the wrk2 correction).
* ``run_open_loop``: arrivals follow a schedule that ignores completions.

Nothing here sleeps or opens a socket.
Locust itself is imported lazily, so a platform where ``gevent`` fails to import only fails the
tests that use Locust.
"""

from __future__ import annotations

import heapq
import math
import os
import random
from dataclasses import dataclass
from time import perf_counter
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal
from unittest import mock

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class Freeze:
    """A window in which the server makes no progress (a GC pause, a lock convoy, a stopped VM)."""

    start_s: float
    duration_s: float

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s


@dataclass(frozen=True, slots=True)
class Sample:
    """One request as the three clocks see it."""

    intended_s: float
    sent_s: float
    done_s: float

    @property
    def service_latency_s(self) -> float:
        """What a closed-loop tool records: completion minus actual send."""
        return self.done_s - self.sent_s

    @property
    def corrected_latency_s(self) -> float:
        """What a user on the intended schedule experienced: completion minus intended send."""
        return self.done_s - self.intended_s


class FifoServer:
    """Single-worker FIFO server with a fixed service time and an optional freeze window."""

    def __init__(self, service_s: float, freeze: Freeze | None = None) -> None:
        if service_s <= 0:
            raise ValueError("service_s must be positive")
        self._service_s = service_s
        self._freeze = freeze
        self._free_at_s = 0.0

    def serve(self, arrival_s: float) -> float:
        """Return the completion time of a request that arrives at ``arrival_s``.

        Calls must come in non-decreasing arrival order, which both generators guarantee.
        """
        start = max(arrival_s, self._free_at_s)
        end = start + self._service_s
        freeze = self._freeze
        if freeze is not None:
            if freeze.start_s <= start < freeze.end_s:
                start = freeze.end_s
                end = start + self._service_s
            elif start < freeze.start_s < end:
                end += freeze.duration_s
        self._free_at_s = end
        return end


def _check_rate(rate_per_s: float, users: int) -> None:
    if rate_per_s <= 0 or users < 1:
        raise ValueError("rate_per_s must be positive and users >= 1")


def run_closed_loop_pacing(
    server: FifoServer, *, users: int, rate_per_s: float, duration_s: float
) -> list[Sample]:
    """Locust ``constant_pacing`` users: next send is ``max(done, sent + pacing)``, no catch-up.

    ``pacing = users / rate_per_s``, so ``users`` users offer ``rate_per_s`` on a fast server.
    The tool has no schedule memory, so ``intended_s == sent_s`` for every sample.
    """
    _check_rate(rate_per_s, users)
    pacing = users / rate_per_s
    heap: list[tuple[float, int]] = [(k / rate_per_s, k) for k in range(users)]
    heapq.heapify(heap)
    samples: list[Sample] = []
    while heap:
        sent, user = heapq.heappop(heap)
        if sent >= duration_s:
            continue
        done = server.serve(sent)
        samples.append(Sample(intended_s=sent, sent_s=sent, done_s=done))
        heapq.heappush(heap, (max(done, sent + pacing), user))
    return samples


def run_schedule_aware_closed_loop(
    server: FifoServer, *, users: int, rate_per_s: float, duration_s: float
) -> list[Sample]:
    """One request in flight per user, but each request keeps its slot on the fixed schedule.

    A user that falls behind sends back to back until it catches up, and every sample records the
    slot time as ``intended_s`` (wrk2 constant-throughput mode).
    """
    _check_rate(rate_per_s, users)
    pacing = users / rate_per_s
    # heap of (send time, user, slot index); slot j of user k is intended at k/rate + j*pacing
    heap: list[tuple[float, int, int]] = [(k / rate_per_s, k, 0) for k in range(users)]
    heapq.heapify(heap)
    samples: list[Sample] = []
    while heap:
        sent, user, slot = heapq.heappop(heap)
        intended = user / rate_per_s + slot * pacing
        if intended >= duration_s:
            continue
        done = server.serve(sent)
        samples.append(Sample(intended_s=intended, sent_s=sent, done_s=done))
        next_intended = user / rate_per_s + (slot + 1) * pacing
        heapq.heappush(heap, (max(done, next_intended), user, slot + 1))
    return samples


def constant_arrivals(rate_per_s: float, duration_s: float) -> list[float]:
    """Evenly spaced arrival times in ``[0, duration_s)``."""
    _check_rate(rate_per_s, 1)
    count = math.ceil(duration_s * rate_per_s)
    return [j / rate_per_s for j in range(count)]


def poisson_arrivals(rate_per_s: float, duration_s: float, seed: int) -> list[float]:
    """Poisson process arrival times in ``[0, duration_s)`` from a seeded private generator."""
    _check_rate(rate_per_s, 1)
    rng = random.Random(seed)  # noqa: S311 - simulation, not security
    times: list[float] = []
    now = rng.expovariate(rate_per_s)
    while now < duration_s:
        times.append(now)
        now += rng.expovariate(rate_per_s)
    return times


def run_open_loop(server: FifoServer, arrivals_s: Sequence[float]) -> list[Sample]:
    """Open model: every arrival is sent at its scheduled time whatever is still in flight."""
    samples: list[Sample] = []
    for arrival in arrivals_s:
        samples.append(Sample(intended_s=arrival, sent_s=arrival, done_s=server.serve(arrival)))
    return samples


def latencies_s(samples: Sequence[Sample], *, corrected: bool) -> list[float]:
    """Per-request latencies from the actual send time, or from the intended send time."""
    if corrected:
        return [s.corrected_latency_s for s in samples]
    return [s.service_latency_s for s in samples]


def nearest_rank_percentile(values: Sequence[float], percent: float) -> float:
    """Nearest-rank percentile: the value at rank ``ceil(percent/100 * n)`` of the sorted data."""
    if not values:
        raise ValueError("no values")
    if not 0 < percent <= 100:
        raise ValueError("percent must be in (0, 100]")
    ordered = sorted(values)
    rank = max(1, math.ceil(percent / 100 * len(ordered)))
    return ordered[rank - 1]


def expected_interval_fill_us(values_us: Sequence[int], interval_us: int) -> list[int]:
    """HdrHistogram ``recordValueWithExpectedInterval``: add the samples a stall suppressed.

    For each value larger than the expected interval, record ``value - k * interval`` for
    ``k = 1, 2, ...`` while the result is still at least one interval.
    Integer microseconds keep the arithmetic exact, as the Java code uses ``long``.
    """
    if interval_us <= 0:
        return list(values_us)
    filled: list[int] = []
    for value in values_us:
        filled.append(value)
        missing = value - interval_us
        while missing >= interval_us:
            filled.append(missing)
            missing -= interval_us
    return filled


def max_in_flight(samples: Sequence[Sample]) -> int:
    """Peak requests sent and not yet completed (completions sort before sends at ties)."""
    events = [(s.sent_s, 1) for s in samples] + [(s.done_s, -1) for s in samples]
    events.sort()
    current = peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def sent_in_window(samples: Sequence[Sample], start_s: float, end_s: float) -> int:
    """Requests actually sent in ``[start_s, end_s)``: the offered load the server saw."""
    return sum(1 for s in samples if start_s <= s.sent_s < end_s)


def closed_loop_throughput(users: int, response_s: float, think_s: float) -> float:
    """Interactive response time law ``X = N / (R + Z)`` for a closed system."""
    if users < 1 or response_s < 0 or think_s < 0 or response_s + think_s == 0:
        raise ValueError("need users >= 1 and a positive response + think time")
    return users / (response_s + think_s)


SessionModel = Literal["open", "partly-open", "closed"]


def expected_session_requests(p_continue: float) -> float:
    """Mean requests per session when each response is followed by another with probability p."""
    if not 0 <= p_continue < 1:
        raise ValueError("p_continue must be in [0, 1)")
    return 1 / (1 - p_continue)


def classify_session_length(mean_requests: float) -> SessionModel:
    """Schroeder, Wierman, Harchol-Balter (NSDI 2006) Principle (vii) rules of thumb."""
    if mean_requests <= 5:
        return "open"
    if mean_requests >= 10:
        return "closed"
    return "partly-open"


def simulate_session_lengths(p_continue: float, sessions: int, seed: int) -> list[int]:
    """Geometric session lengths from a seeded generator."""
    expected_session_requests(p_continue)
    rng = random.Random(seed)  # noqa: S311 - simulation, not security
    lengths: list[int] = []
    for _ in range(sessions):
        length = 1
        while rng.random() < p_continue:
            length += 1
        lengths.append(length)
    return lengths


def locust_reported_percentile_ms(latencies: Sequence[float], percent: float) -> int:
    """Percentile as Locust 2.47.0 reports it: bucketed to about two significant digits, in ms.

    ``LOCUST_SKIP_MONKEY_PATCH`` stops ``import locust`` from running ``gevent.monkey.patch_all()``
    on the whole pytest process.
    """
    os.environ.setdefault("LOCUST_SKIP_MONKEY_PATCH", "1")
    from locust.stats import RequestStats

    stats = RequestStats(use_response_times_cache=False)
    for seconds in latencies:
        stats.log_request("GET", "/api", seconds * 1000.0, 0)  # type: ignore[arg-type]
    return stats.get("/api", "GET").get_response_time_percentile(percent / 100)


def locust_bucket_ms(response_time_ms: float) -> int:
    """Locust's response time histogram key for a measurement."""
    os.environ.setdefault("LOCUST_SKIP_MONKEY_PATCH", "1")
    from locust.stats import bucket_response_time

    return bucket_response_time(response_time_ms)


def locust_pacing_waits(pacing_s: float, task_durations_s: Sequence[float]) -> list[float]:
    """Drive Locust's real ``constant_pacing`` with a fake clock and return each wait it chose."""
    os.environ.setdefault("LOCUST_SKIP_MONKEY_PATCH", "1")
    from locust.user import wait_time

    clock = [0.0]
    user = SimpleNamespace(_cp_last_run=0.0, _cp_last_wait_time=0.0)
    wait_func = wait_time.constant_pacing(pacing_s)
    waits: list[float] = []
    with mock.patch.object(wait_time, "perf_counter", side_effect=lambda: clock[0]):
        for duration in task_durations_s:
            clock[0] += duration  # the task runs
            wait = wait_func(user)  # type: ignore[arg-type]
            waits.append(wait)
            clock[0] += wait  # the user sleeps
    return waits


def count_locust_task_runs(
    task_s: float, throughput_per_s: float, window_s: float
) -> tuple[int, float]:
    """Run one real Locust user in-process and count its task executions.

    The task sleeps ``task_s`` on the gevent hub; ``wait_time`` asks for ``throughput_per_s``.
    Returns ``(runs, elapsed_s)`` with ``elapsed_s`` measured around the whole window.
    """
    os.environ.setdefault("LOCUST_SKIP_MONKEY_PATCH", "1")
    import gevent  # type: ignore[import-untyped]  # no stubs pinned in pyproject.toml
    from locust import User, constant_throughput, task
    from locust.env import Environment

    starts: list[float] = []

    class Probe(User):
        wait_time = constant_throughput(throughput_per_s)

        @task
        def work(self) -> None:
            starts.append(perf_counter())
            gevent.sleep(task_s)

    environment = Environment(user_classes=[Probe])
    runner = environment.create_local_runner()
    began = perf_counter()
    runner.start(1, spawn_rate=1)
    gevent.sleep(window_s)
    runner.quit()
    return len(starts), perf_counter() - began
