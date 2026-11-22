"""Prioritized load shedding: CoDel queue timeout, adaptive LIFO, and priority admission.

Two layers share one set of rules:

- ``simulate`` is a deterministic discrete-event model of a c-server request queue in
  virtual time.  It compares FIFO, FIFO with a queue timeout, FIFO with a CoDel timeout,
  and adaptive LIFO with priority admission under the same overload.
- ``PriorityShedder`` applies the same admission and dequeue rules to live requests as a
  pure ASGI middleware with an injectable clock.

``CoDelTimeout`` is a line-by-line port of the service-queue variant of CoDel in
folly (folly/executors/Codel.cpp, Codel::overloaded_explicit_now), not the packet
control law of RFC 8289.
"""

from __future__ import annotations

import asyncio
import enum
import heapq
import math
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from starlette.types import ASGIApp, Receive, Scope, Send


class Priority(enum.IntEnum):
    """Traffic class; a lower value is more important and is dequeued first."""

    CRITICAL = 0
    DEFAULT = 1
    BACKGROUND = 2


class CoDelTimeout:
    """Service-queue CoDel: shed requests whose queueing delay exceeds 2 x target.

    Every ``interval`` the controller asks whether the *minimum* sojourn seen during
    the previous interval exceeded ``target``.  If it did, a standing queue exists and
    the controller is ``overloaded``; while overloaded, any dequeued request that waited
    longer than ``slough_timeout`` (2 x target) is dropped.  The first sample of each
    interval only resets the minimum and is never dropped, exactly as in folly.
    """

    def __init__(self, target: float = 0.005, interval: float = 0.100, start: float = 0.0):
        if not 0.0 < target < interval:
            raise ValueError("CoDel needs 0 < target < interval")
        self.target = target
        self.interval = interval
        self.overloaded = False
        self._min_delay = 0.0
        self._interval_end = start
        self._reset_pending = True

    @property
    def slough_timeout(self) -> float:
        return 2.0 * self.target

    def should_drop(self, sojourn: float, now: float) -> bool:
        """Feed one dequeued request's sojourn time; return True to shed it."""
        if now > self._interval_end and not self._reset_pending:
            self._reset_pending = True
            self._interval_end = now + self.interval
            self.overloaded = self._min_delay > self.target
        if self._reset_pending:
            self._reset_pending = False
            self._min_delay = sojourn
            return False
        self._min_delay = min(self._min_delay, sojourn)
        return self.overloaded and sojourn > self.slough_timeout


QueueOrder = Literal["fifo", "lifo", "adaptive_lifo"]
OutcomeKind = Literal["rejected", "expired", "on_time", "late"]


@dataclass(frozen=True, slots=True)
class Policy:
    """How the server admits, orders, and expires queued requests."""

    name: str
    order: QueueOrder = "fifo"
    lifo_after: float = 0.0
    codel: tuple[float, float] | None = None
    queue_timeout: float | None = None
    admit_depth: Mapping[Priority, int] = field(default_factory=dict[Priority, int])
    strict_priority: bool = False


@dataclass(frozen=True, slots=True)
class Workload:
    """Poisson arrivals in phases of (duration_s, requests_per_s) over c servers."""

    phases: tuple[tuple[float, float], ...]
    critical_share: float = 0.5
    servers: int = 8
    mean_service: float = 0.020
    client_timeout: float = 1.0
    seed: int = 7

    @property
    def capacity_rps(self) -> float:
        return self.servers / self.mean_service

    def arrivals(self) -> list[Request]:
        rng = random.Random(self.seed)  # noqa: S311 - seeded simulation, not security
        out: list[Request] = []
        start = 0.0
        for duration, rate in self.phases:
            end = start + duration
            t = start
            while True:
                t += rng.expovariate(rate)
                if t >= end:
                    break
                critical = rng.random() < self.critical_share
                prio = Priority.CRITICAL if critical else Priority.BACKGROUND
                out.append(Request(len(out), t, prio))
            start = end
        return out


@dataclass(frozen=True, slots=True)
class Request:
    rid: int
    arrival: float
    priority: Priority


@dataclass(frozen=True, slots=True)
class Outcome:
    request: Request
    kind: OutcomeKind
    latency: float | None


@dataclass(frozen=True, slots=True)
class Summary:
    offered: int
    rejected: int
    expired: int
    on_time: int
    late: int
    p50: float
    p95: float
    p99: float

    @property
    def success_ratio(self) -> float:
        return self.on_time / self.offered if self.offered else math.nan


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile; NaN for an empty list."""
    if not values:
        return math.nan
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True, slots=True)
class SimResult:
    policy: str
    outcomes: tuple[Outcome, ...]
    wasted_server_seconds: float
    max_queue_depth: int

    def summary(
        self,
        priority: Priority | None = None,
        since: float = 0.0,
        until: float = math.inf,
    ) -> Summary:
        chosen = [
            o
            for o in self.outcomes
            if (priority is None or o.request.priority is priority)
            and since <= o.request.arrival < until
        ]
        lat = [o.latency for o in chosen if o.latency is not None]
        counts = {k: sum(1 for o in chosen if o.kind == k) for k in ("rejected", "expired")}
        return Summary(
            offered=len(chosen),
            rejected=counts["rejected"],
            expired=counts["expired"],
            on_time=sum(1 for o in chosen if o.kind == "on_time"),
            late=sum(1 for o in chosen if o.kind == "late"),
            p50=percentile(lat, 50),
            p95=percentile(lat, 95),
            p99=percentile(lat, 99),
        )


class _Backlog:
    """The waiting room: one deque per lane, oldest at the left end."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.lanes: dict[Priority, deque[Request]] = {p: deque() for p in Priority}
        self.codel = CoDelTimeout(*policy.codel) if policy.codel else None

    def __len__(self) -> int:
        return sum(len(lane) for lane in self.lanes.values())

    def push(self, req: Request) -> None:
        lane = req.priority if self.policy.strict_priority else Priority.DEFAULT
        self.lanes[lane].append(req)

    def _timeout(self) -> float | None:
        limits = [self.policy.queue_timeout] if self.policy.queue_timeout is not None else []
        if self.codel is not None and self.codel.overloaded:
            limits.append(self.codel.slough_timeout)
        return min(limits) if limits else None

    def pop(self, now: float, expire: Callable[[Request], None]) -> Request | None:
        while True:
            lane = next((q for q in self.lanes.values() if q), None)
            if lane is None:
                return None
            timeout = self._timeout()
            while lane and timeout is not None and now - lane[0].arrival > timeout:
                expire(lane.popleft())
            if not lane:
                continue
            order = self.policy.order
            lifo = order == "lifo" or (
                order == "adaptive_lifo" and now - lane[0].arrival > self.policy.lifo_after
            )
            req = lane.pop() if lifo else lane.popleft()
            if self.codel is not None and self.codel.should_drop(now - req.arrival, now):
                expire(req)
                continue
            return req


def simulate(policy: Policy, workload: Workload) -> SimResult:
    """Run the workload through ``workload.servers`` non-preemptive servers.

    Service times are drawn in *start order* from a dedicated RNG stream, so every
    policy performs the same sequence of work and only ordering and shedding differ.
    """
    arrivals = workload.arrivals()
    service_rng = random.Random(workload.seed + 1)  # noqa: S311 - seeded simulation
    backlog = _Backlog(policy)
    outcomes: list[Outcome] = []
    running: list[tuple[float, int, Request, float]] = []
    busy = 0
    wasted = 0.0
    max_depth = 0

    def expire(req: Request) -> None:
        outcomes.append(Outcome(req, "expired", None))

    def start_work(now: float) -> None:
        nonlocal busy
        while busy < workload.servers:
            req = backlog.pop(now, expire)
            if req is None:
                return
            service = service_rng.expovariate(1.0 / workload.mean_service)
            heapq.heappush(running, (now + service, req.rid, req, service))
            busy += 1

    i = 0
    while i < len(arrivals) or running:
        next_arrival = arrivals[i].arrival if i < len(arrivals) else math.inf
        if running and running[0][0] <= next_arrival:
            now, _, req, service = heapq.heappop(running)
            busy -= 1
            latency = now - req.arrival
            if latency <= workload.client_timeout:
                outcomes.append(Outcome(req, "on_time", latency))
            else:
                wasted += service
                outcomes.append(Outcome(req, "late", latency))
        else:
            now, req = next_arrival, arrivals[i]
            i += 1
            limit = policy.admit_depth.get(req.priority)
            if limit is not None and len(backlog) >= limit:
                outcomes.append(Outcome(req, "rejected", None))
            else:
                backlog.push(req)
                max_depth = max(max_depth, len(backlog))
        start_work(now)
    outcomes.sort(key=lambda o: o.request.rid)
    return SimResult(policy.name, tuple(outcomes), wasted, max_depth)


CODEL = (0.005, 0.100)
OVERLOAD = Workload(phases=((10.0, 600.0), (10.0, 200.0)))
POLICIES: tuple[Policy, ...] = (
    Policy("fifo_unbounded"),
    Policy("fifo_queue_timeout", queue_timeout=1.0),
    Policy("fifo_codel", codel=CODEL),
    Policy(
        "adaptive_lifo_codel",
        order="adaptive_lifo",
        lifo_after=0.050,
        codel=CODEL,
        queue_timeout=1.0,
        admit_depth={Priority.CRITICAL: 2000, Priority.BACKGROUND: 2000},
    ),
    Policy(
        "priority_lifo_codel",
        order="adaptive_lifo",
        lifo_after=0.050,
        codel=CODEL,
        queue_timeout=1.0,
        admit_depth={Priority.CRITICAL: 2000, Priority.BACKGROUND: 40},
        strict_priority=True,
    ),
)


def report(workload: Workload = OVERLOAD) -> str:
    """Render the policy comparison table shown in the note."""
    head = (
        f"{'policy':<20} {'ok%':>5} {'crit%':>6} {'bg%':>5} {'p50_s':>6} {'p95_s':>6} "
        f"{'p99_s':>6} {'late':>5} {'wasted_s':>8} {'recov%':>6}"
    )
    rows = [head]
    spike_end = workload.phases[0][0]
    for policy in POLICIES:
        r = simulate(policy, workload)
        s = r.summary()
        rows.append(
            f"{policy.name:<20} {100 * s.success_ratio:5.1f} "
            f"{100 * r.summary(Priority.CRITICAL).success_ratio:6.1f} "
            f"{100 * r.summary(Priority.BACKGROUND).success_ratio:5.1f} "
            f"{s.p50:6.3f} {s.p95:6.3f} {s.p99:6.3f} {s.late:5d} "
            f"{r.wasted_server_seconds:8.1f} "
            f"{100 * r.summary(since=spike_end).success_ratio:6.1f}"
        )
    return "\n".join(rows)


@dataclass(slots=True, eq=False)
class _Waiter:
    future: asyncio.Future[bool]
    enqueued_at: float


class PriorityShedder:
    """Pure ASGI middleware: priority admission, adaptive LIFO, and a queue timeout.

    - At most ``max_concurrency`` requests run the inner app at once.
    - A request that cannot run joins its class lane only while the total waiting count
      is below ``queue_limits[priority]``; otherwise it gets ``503`` immediately.
    - When a slot frees, the most important non-empty lane is served.  Waiters older
      than ``queue_timeout`` are expired from the old end first (``503``); then the lane
      is served FIFO, or LIFO once its oldest waiter is older than ``lifo_after``.
    - ``queue_timeout`` is also an event-loop backstop if no slot frees at all.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        classify: Callable[[Scope], Priority],
        max_concurrency: int,
        queue_limits: Mapping[Priority, int],
        queue_timeout: float,
        lifo_after: float,
        retry_after_seconds: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_concurrency < 1 or queue_timeout <= 0 or lifo_after < 0:
            raise ValueError("invalid shedder limits")
        self.app = app
        self._classify = classify
        self._max = max_concurrency
        self._limits = {p: queue_limits.get(p, 0) for p in Priority}
        self._queue_timeout = queue_timeout
        self._lifo_after = lifo_after
        self._retry_after = str(retry_after_seconds).encode()
        self._clock = clock
        self._lanes: dict[Priority, deque[_Waiter]] = {p: deque() for p in Priority}
        self.in_flight = 0
        self.rejected = 0
        self.expired = 0

    @property
    def waiting(self) -> int:
        return sum(len(lane) for lane in self._lanes.values())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not await self._acquire(self._classify(scope)):
            await self._reject(send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self._release()

    async def _acquire(self, priority: Priority) -> bool:
        if self.in_flight < self._max and self.waiting == 0:
            self.in_flight += 1
            return True
        if self.waiting >= self._limits[priority]:
            self.rejected += 1
            return False
        waiter = _Waiter(asyncio.get_running_loop().create_future(), self._clock())
        lane = self._lanes[priority]
        lane.append(waiter)
        try:
            async with asyncio.timeout(self._queue_timeout):
                return await waiter.future
        except BaseException as exc:
            fut = waiter.future
            if fut.done() and not fut.cancelled() and fut.result():
                self._release()  # granted just as we left: hand the slot on
            elif waiter in lane:
                lane.remove(waiter)
            if isinstance(exc, TimeoutError):
                self.expired += 1
                return False
            raise

    def _release(self) -> None:
        self.in_flight -= 1
        now = self._clock()
        while self.in_flight < self._max:
            lane = next((q for q in self._lanes.values() if q), None)
            if lane is None:
                return
            while lane and now - lane[0].enqueued_at > self._queue_timeout:
                stale = lane.popleft()
                self.expired += 1
                if not stale.future.done():
                    stale.future.set_result(False)
            if not lane:
                continue
            lifo = now - lane[0].enqueued_at > self._lifo_after
            waiter = lane.pop() if lifo else lane.popleft()
            if waiter.future.done():
                continue
            self.in_flight += 1
            waiter.future.set_result(True)

    async def _reject(self, send: Send) -> None:
        body = b"Service Unavailable"
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", self._retry_after),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
