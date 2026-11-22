"""Capacity planning for APIs: Little's Law, Amdahl's Law, and the Universal Scalability Law.

Everything here is deterministic and in-process.
Simulations take an explicit seed, and the ASGI tracker takes an injectable clock.
"""

from __future__ import annotations

import heapq
import math
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from starlette.types import ASGIApp, Receive, Scope, Send

# ---------------------------------------------------------------------------
# Little's Law
# ---------------------------------------------------------------------------


def littles_law(
    *,
    concurrency: float | None = None,
    rate: float | None = None,
    latency: float | None = None,
) -> float:
    """Solve L = lambda * W for the one argument left as ``None``.

    ``concurrency`` is L (requests in the system), ``rate`` is lambda (requests per second),
    and ``latency`` is W (seconds each request spends in the system).
    """
    given = {"concurrency": concurrency, "rate": rate, "latency": latency}
    missing = [name for name, value in given.items() if value is None]
    if len(missing) != 1:
        raise ValueError(f"pass exactly two of concurrency, rate, latency; missing {missing}")
    for name, value in given.items():
        if value is not None and value <= 0:
            raise ValueError(f"{name} must be positive, got {value}")
    if concurrency is None:
        return _need(rate, "rate") * _need(latency, "latency")
    if rate is None:
        return concurrency / _need(latency, "latency")
    return concurrency / rate


def _need(value: float | None, name: str) -> float:
    if value is None:
        raise ValueError(f"{name} is required")
    return value


@dataclass(frozen=True)
class Request:
    """One request's time in the system, in seconds on any common clock."""

    arrival: float
    departure: float

    def __post_init__(self) -> None:
        if self.departure < self.arrival:
            raise ValueError("departure precedes arrival")

    @property
    def latency(self) -> float:
        return self.departure - self.arrival


@dataclass(frozen=True)
class LittleReading:
    """The three Little's Law quantities measured over one window."""

    window: float
    arrivals: int
    mean_in_system: float
    arrival_rate: float
    mean_latency: float

    @property
    def residual(self) -> float:
        """L - lambda * W: zero when the system is empty at both ends of the window."""
        return self.mean_in_system - self.arrival_rate * self.mean_latency


def little_reading(requests: Sequence[Request], start: float, end: float) -> LittleReading:
    """Measure L, lambda, and W the way an operator does from request logs.

    L is the time average of requests in the system over ``[start, end)``, the area of every
    request's overlap with the window divided by its length.
    lambda counts arrivals inside the window.
    W is the mean full latency of those arrivals, including any time after ``end``.
    """
    if end <= start:
        raise ValueError("window must have positive length")
    window = end - start
    area = sum(
        max(0.0, min(r.departure, end) - max(r.arrival, start))
        for r in requests
        if r.departure > start and r.arrival < end
    )
    arrived = [r for r in requests if start <= r.arrival < end]
    if not arrived:
        raise ValueError("no arrivals inside the window")
    return LittleReading(
        window=window,
        arrivals=len(arrived),
        mean_in_system=area / window,
        arrival_rate=len(arrived) / window,
        mean_latency=sum(r.latency for r in arrived) / len(arrived),
    )


def gauge_mean(events: Sequence[tuple[float, int]], start: float, end: float) -> float:
    """Time average of a gauge built from ``(time, +1 or -1)`` events, over ``[start, end)``."""
    if end <= start:
        raise ValueError("window must have positive length")
    level = 0
    previous = start
    area = 0.0
    for when, delta in sorted(events):
        if when >= end:
            break
        if when > previous:
            area += level * (when - previous)
            previous = when
        level += delta
    area += level * (end - previous)
    return area / (end - start)


# ---------------------------------------------------------------------------
# Queue simulation, M/M/1 and Erlang C
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueRun:
    """Result of one FIFO multi-server simulation."""

    requests: list[Request]
    waited: int

    @property
    def wait_fraction(self) -> float:
        return self.waited / len(self.requests)


def simulate_queue(
    *, rate: float, mean_service: float, count: int, servers: int = 1, seed: int = 0
) -> QueueRun:
    """Simulate ``count`` Poisson arrivals at ``rate`` through ``servers`` FIFO servers.

    Service times are exponential with mean ``mean_service``.
    """
    rng = random.Random(seed)  # noqa: S311 - simulation, not security
    free_at = [0.0] * servers
    heapq.heapify(free_at)
    now = 0.0
    requests: list[Request] = []
    waited = 0
    for _ in range(count):
        now += rng.expovariate(rate)
        server_free = heapq.heappop(free_at)
        start = max(now, server_free)
        if start > now:
            waited += 1
        departure = start + rng.expovariate(1.0 / mean_service)
        heapq.heappush(free_at, departure)
        requests.append(Request(now, departure))
    return QueueRun(requests, waited)


def mm1_latency(rate: float, service_time: float) -> float:
    """Mean time in an M/M/1 system, S / (1 - rho), which diverges as rho -> 1."""
    rho = rate * service_time
    if not 0 <= rho < 1:
        raise ValueError(f"utilization {rho:.3f} is outside [0, 1)")
    return service_time / (1.0 - rho)


def erlang_c(servers: int, offered_load: float) -> float:
    """Probability an arrival must wait in an M/M/c queue with ``offered_load`` = lambda * S.

    Uses the Erlang B recurrence, which stays numerically stable for large ``servers``.
    """
    if servers < 1 or offered_load < 0:
        raise ValueError("need servers >= 1 and offered_load >= 0")
    if offered_load >= servers:
        return 1.0
    blocking = 1.0
    for k in range(1, servers + 1):
        blocking = offered_load * blocking / (k + offered_load * blocking)
    rho = offered_load / servers
    return blocking / (1.0 - rho * (1.0 - blocking))


def min_servers(offered_load: float, max_wait_probability: float) -> int:
    """Smallest server count whose Erlang C wait probability is at most the target."""
    if not 0 < max_wait_probability < 1:
        raise ValueError("max_wait_probability must be in (0, 1)")
    servers = max(1, math.floor(offered_load) + 1)
    while erlang_c(servers, offered_load) > max_wait_probability:
        servers += 1
    return servers


# ---------------------------------------------------------------------------
# Amdahl and the Universal Scalability Law
# ---------------------------------------------------------------------------


def usl_relative(n: float, alpha: float, beta: float) -> float:
    """Relative capacity C(N) = N / (1 + alpha (N - 1) + beta N (N - 1))."""
    if n < 1 or not 0 <= alpha <= 1 or beta < 0:
        raise ValueError("need n >= 1, 0 <= alpha <= 1, beta >= 0")
    return n / (1.0 + alpha * (n - 1.0) + beta * n * (n - 1.0))


def usl_throughput(n: float, gamma: float, alpha: float, beta: float) -> float:
    """Absolute throughput X(N) = gamma * C(N), where gamma is the throughput at N = 1."""
    return gamma * usl_relative(n, alpha, beta)


def amdahl_speedup(n: float, serial_fraction: float) -> float:
    """Amdahl's Law, which is the USL with beta = 0."""
    return usl_relative(n, serial_fraction, 0.0)


def usl_peak(alpha: float, beta: float) -> float:
    """Real-valued N* = sqrt((1 - alpha) / beta); infinity when there is no coherency cost."""
    if beta == 0:
        return math.inf
    return math.sqrt((1.0 - alpha) / beta)


def best_integer_concurrency(alpha: float, beta: float) -> int:
    """The integer N with the highest C(N), chosen from the floor and ceiling of N*."""
    peak = usl_peak(alpha, beta)
    if math.isinf(peak):
        raise ValueError("beta == 0 has no throughput maximum")
    low = max(1, math.floor(peak))
    return max((low, low + 1), key=lambda n: usl_relative(n, alpha, beta))


@dataclass(frozen=True)
class UslFit:
    """Fitted USL parameters: gamma is X(1), alpha is contention, beta is coherency."""

    gamma: float
    alpha: float
    beta: float

    def predict(self, n: float) -> float:
        return usl_throughput(n, self.gamma, self.alpha, self.beta)

    @property
    def peak(self) -> float:
        return usl_peak(self.alpha, self.beta)

    @property
    def physical(self) -> bool:
        """True when 0 <= alpha <= 1, beta >= 0, and gamma > 0, as the model requires."""
        return self.gamma > 0 and 0 <= self.alpha <= 1 and self.beta >= 0


def _solve3(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Solve a 3x3 linear system by Gaussian elimination with partial pivoting."""
    rows = [[*matrix[i], rhs[i]] for i in range(3)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-12:
            raise ValueError("singular system: need at least three distinct concurrency levels")
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(col + 1, 3):
            factor = rows[r][col] / rows[col][col]
            for c in range(col, 4):
                rows[r][c] -= factor * rows[col][c]
    solution = [0.0] * 3
    for r in (2, 1, 0):
        tail = sum(rows[r][c] * solution[c] for c in range(r + 1, 3))
        solution[r] = (rows[r][3] - tail) / rows[r][r]
    return solution


def fit_usl(samples: Sequence[tuple[float, float]]) -> UslFit:
    """Fit gamma, alpha, beta to ``(N, X)`` samples by ordinary least squares on N / X.

    Because N / X = c0 + c1 N + c2 N^2 with c0 = (1 - alpha) / gamma, c1 = (alpha - beta) / gamma,
    and c2 = beta / gamma, the fit is a quadratic regression that needs no iteration.
    It weights large-N points more, because N / X grows with N.
    """
    points = [(float(n), float(x)) for n, x in samples]
    if any(n < 1 or x <= 0 for n, x in points):
        raise ValueError("need N >= 1 and throughput > 0 in every sample")
    if len({n for n, _ in points}) < 3:
        raise ValueError("need at least three distinct concurrency levels")
    power = [sum(n**k for n, _ in points) for k in range(5)]
    moment = [sum((n / x) * n**j for n, x in points) for j in range(3)]
    c0, c1, c2 = _solve3([[power[i + j] for j in range(3)] for i in range(3)], moment)
    inverse_gamma = c0 + c1 + c2
    if inverse_gamma <= 0:
        raise ValueError("fit produced non-positive gamma; the data is not USL-shaped")
    gamma = 1.0 / inverse_gamma
    return UslFit(gamma=gamma, alpha=1.0 - c0 * gamma, beta=c2 * gamma)


@dataclass(frozen=True)
class PoolModel:
    """A worker pool where each job does parallel work, then a critical section under one lock.

    The critical section takes ``serial_time + coherency_time * waiting``, where ``waiting`` is the
    number of other workers already queued for the lock when it starts.
    That is the state-dependent server of Gunther's Section 3.3: the more workers contend, the
    longer each hold, as with a convoy or cache-line transfers between cores.
    """

    parallel_time: float
    serial_time: float
    coherency_time: float


def simulate_pool(workers: int, model: PoolModel, cycles: int, seed: int = 0) -> float:
    """Closed-loop throughput of ``workers`` looping on ``model``, in cycles per time unit.

    Parallel time is exponential, so the curve is smooth like a real system.
    The lock is FIFO, and the first tenth of the cycles is discarded as warm-up.
    """
    if workers < 1 or cycles < 20:
        raise ValueError("need workers >= 1 and cycles >= 20")
    rng = random.Random(seed)  # noqa: S311 - simulation, not security
    mean = model.parallel_time
    ready = [(rng.expovariate(1.0 / mean), w) for w in range(workers)]
    heapq.heapify(ready)
    lock_free = 0.0
    warmup = cycles // 10
    warm_clock = 0.0
    for i in range(cycles):
        arrived, worker = heapq.heappop(ready)
        start = max(arrived, lock_free)
        waiting = sum(1 for other, _ in ready if other <= start)
        lock_free = start + model.serial_time + model.coherency_time * waiting
        if i == warmup:
            warm_clock = lock_free
        heapq.heappush(ready, (lock_free + rng.expovariate(1.0 / mean), worker))
    return (cycles - 1 - warmup) / (lock_free - warm_clock)


# ---------------------------------------------------------------------------
# In-flight tracking middleware: the L gauge and the W samples from one ASGI app
# ---------------------------------------------------------------------------


class InFlightTracker:
    """Pure ASGI middleware that records the in-flight gauge events and per-request latencies."""

    def __init__(self, app: ASGIApp, clock: Callable[[], float] = time.perf_counter) -> None:
        self.app = app
        self.clock = clock
        self.events: list[tuple[float, int]] = []
        self.requests: list[Request] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        arrival = self.clock()
        self.events.append((arrival, 1))
        try:
            await self.app(scope, receive, send)
        finally:
            departure = self.clock()
            self.events.append((departure, -1))
            self.requests.append(Request(arrival, departure))

    def peak_in_flight(self) -> int:
        level = peak = 0
        for _, delta in sorted(self.events):
            level += delta
            peak = max(peak, level)
        return peak
