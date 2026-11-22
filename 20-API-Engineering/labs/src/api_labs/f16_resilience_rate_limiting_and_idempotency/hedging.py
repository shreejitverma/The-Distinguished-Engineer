"""Hedged requests: a percentile-derived hedge delay, a load budget, and loser cancellation.

``HedgedCaller.call`` sends the primary leg, waits ``LatencyTracker.hedge_delay()``, and only
then sends one backup leg (to another replica, ideally in another zone) if ``HedgeBudget`` has
a whole token.
The first leg that succeeds wins, and the AnyIO task group cancels the other leg before
``call`` returns.
The simulation helpers model the same policy on seeded latency samples so the tail and the
extra load can be measured deterministically.
"""

from __future__ import annotations

import math
import random
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

import anyio
import h2.config
import h2.connection
import h2.errors

type Leg[T] = Callable[[], Awaitable[T]]

# RFC 9110 Section 9.2.2: PUT, DELETE, and the safe methods are idempotent.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"})


def nearest_rank(samples: Sequence[float], pct: float) -> float:
    """Return the nearest-rank percentile: the smallest sample with at least pct% at or below."""
    if not samples:
        raise ValueError("no samples")
    if not 0 < pct <= 100:
        raise ValueError("pct must be in (0, 100]")
    ordered = sorted(samples)
    return ordered[math.ceil(pct / 100 * len(ordered)) - 1]


class LatencyTracker:
    """Sliding window of primary-leg latencies that yields the hedge delay.

    No delay is returned until ``min_samples`` latencies are known, so a cold client never
    hedges on a guess; the percentile is clamped to ``[floor_s, ceiling_s]``.
    """

    def __init__(
        self,
        *,
        percentile: float = 95.0,
        window: int = 1000,
        min_samples: int = 100,
        floor_s: float = 0.001,
        ceiling_s: float = 1.0,
    ) -> None:
        if not 0 < percentile < 100:
            raise ValueError("percentile must be in (0, 100)")
        if not 1 <= min_samples <= window:
            raise ValueError("need 1 <= min_samples <= window")
        if not 0 < floor_s <= ceiling_s:
            raise ValueError("need 0 < floor_s <= ceiling_s")
        self.percentile = percentile
        self.min_samples = min_samples
        self.floor_s = floor_s
        self.ceiling_s = ceiling_s
        self._samples: deque[float] = deque(maxlen=window)

    def record(self, latency_s: float) -> None:
        if not latency_s >= 0:  # also rejects NaN
            raise ValueError("latency must be a non-negative number")
        self._samples.append(latency_s)

    def hedge_delay(self) -> float | None:
        if len(self._samples) < self.min_samples:
            return None
        raw = nearest_rank(self._samples, self.percentile)
        return min(self.ceiling_s, max(self.floor_s, raw))


class HedgeBudget:
    """Token budget: every call deposits ``ratio`` of a token and every backup spends one.

    Over any run of N calls at most ``floor(N * ratio)`` backups plus the stored balance are
    sent, so extra load is capped at ``ratio`` even if every primary is slow.
    Tokens are counted in integer thousandths so the cap is exact.
    """

    _SCALE = 1000

    def __init__(self, ratio: float, *, max_tokens: int = 10) -> None:
        if not 0 <= ratio < 1:
            raise ValueError("ratio must be in [0, 1)")
        if max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        self._per_call = round(ratio * self._SCALE)
        self._cap = max_tokens * self._SCALE
        self._balance = 0

    @property
    def tokens(self) -> float:
        return self._balance / self._SCALE

    def deposit(self) -> None:
        self._balance = min(self._cap, self._balance + self._per_call)

    def try_withdraw(self) -> bool:
        if self._balance < self._SCALE:
            return False
        self._balance -= self._SCALE
        return True


def may_hedge(method: str, *, idempotency_key: str | None) -> bool:
    """Hedge only requests that are safe to execute twice (RFC 9110 Section 9.2.2)."""
    return method.upper() in IDEMPOTENT_METHODS or idempotency_key is not None


@dataclass(frozen=True, slots=True)
class HedgeOutcome[T]:
    value: T
    winner: int  # 0 = primary leg, 1 = backup leg
    legs_sent: int


@dataclass(slots=True)
class _Race[T]:
    finished: anyio.Event
    sent: int = 1
    ended: int = 0
    winner: tuple[int, T] | None = None
    errors: list[Exception] = field(default_factory=list[Exception])


class HedgedCaller:
    """Send a primary leg, then at most one budgeted backup leg; the first success wins."""

    def __init__(self, tracker: LatencyTracker, budget: HedgeBudget) -> None:
        self.tracker = tracker
        self.budget = budget

    async def call[T](
        self,
        primary: Leg[T],
        backup: Leg[T],
        *,
        method: str,
        idempotency_key: str | None = None,
    ) -> HedgeOutcome[T]:
        self.budget.deposit()
        delay = self.tracker.hedge_delay()
        if delay is None or not may_hedge(method, idempotency_key=idempotency_key):
            start = anyio.current_time()
            value = await primary()
            self.tracker.record(anyio.current_time() - start)
            return HedgeOutcome(value, winner=0, legs_sent=1)
        return await self._race(primary, backup, delay)

    async def _race[T](self, primary: Leg[T], backup: Leg[T], delay: float) -> HedgeOutcome[T]:
        race: _Race[T] = _Race(finished=anyio.Event())

        async def run(index: int, leg: Leg[T]) -> None:
            start = anyio.current_time()
            try:
                value = await leg()
            except anyio.get_cancelled_exc_class():
                if index == 0:  # censored sample: the primary took at least this long
                    self.tracker.record(anyio.current_time() - start)
                raise
            except Exception as exc:
                # Not recorded: fast failures would drag the percentile, and the delay, down.
                race.errors.append(exc)
                race.ended += 1
                if race.ended == race.sent:
                    race.finished.set()
                return
            if index == 0:
                self.tracker.record(anyio.current_time() - start)
            race.ended += 1
            if race.winner is None:
                race.winner = (index, value)
                race.finished.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(run, 0, primary)
            with anyio.move_on_after(delay):
                await race.finished.wait()
            # No await between this check and start_soon, so the primary cannot finish in between.
            if not race.finished.is_set() and self.budget.try_withdraw():
                race.sent = 2
                tg.start_soon(run, 1, backup)
            await race.finished.wait()
            tg.cancel_scope.cancel()  # the loser is cancelled; the group waits for it to unwind
        if race.winner is None:
            raise race.errors[0]
        index, value = race.winner
        return HedgeOutcome(value, winner=index, legs_sent=race.sent)


def fanout_slow_probability(p_slow: float, fanout: int) -> float:
    """P(a request that waits for all ``fanout`` leaves sees at least one slow leaf)."""
    return 1.0 - (1.0 - p_slow) ** fanout


def rst_stream_cancel_frame(stream_id: int = 1) -> bytes:
    """Return the RST_STREAM(CANCEL) frame an HTTP/2 client sends to abandon a losing leg."""
    conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    conn.initiate_connection()
    conn.send_headers(
        stream_id,
        [(":method", "GET"), (":path", "/"), (":scheme", "https"), (":authority", "t")],
        end_stream=True,
    )
    conn.data_to_send()  # discard the preface, SETTINGS, and HEADERS
    conn.reset_stream(stream_id, error_code=h2.errors.ErrorCodes.CANCEL)
    return conn.data_to_send()


@dataclass(frozen=True, slots=True)
class TailStats:
    p50_ms: float
    p99_ms: float
    p999_ms: float
    extra_load: float  # backup legs sent per primary leg


def _leg_ms(rng: random.Random, straggler_p: float, *, force_straggler: bool) -> float:
    """4 ms floor + exponential service time (mean 2 ms) + a 200 ms stall with prob straggler_p."""
    latency = 4.0 + rng.expovariate(1 / 2.0)
    if force_straggler or rng.random() < straggler_p:
        latency += 200.0
    return latency


def simulate_hedging(
    requests: int,
    *,
    seed: int,
    hedge_delay_ms: float | None,
    straggler_p: float = 0.02,
    slowdown: float = 1.0,
    cross_zone_rtt_ms: float = 0.0,
    correlation: float = 0.0,
    budget_ratio: float | None = None,
    fanout: int = 1,
) -> TailStats:
    """Simulate end-to-end latency when each of ``fanout`` leaf calls may be hedged.

    ``correlation`` is the probability that a straggling primary's backup straggles too (a
    shared cause such as the same zone or a hot key); ``slowdown`` scales every leg, modelling
    a backend that is slow for everyone; ``budget_ratio`` applies a ``HedgeBudget``.
    """
    rng = random.Random(seed)  # noqa: S311 - deterministic synthetic workload, not security
    budget = HedgeBudget(budget_ratio) if budget_ratio is not None else None
    delay = math.inf if hedge_delay_ms is None else hedge_delay_ms
    totals: list[float] = []
    backups = 0
    for _ in range(requests):
        slowest = 0.0
        for _ in range(fanout):
            before = rng.random()
            primary = _leg_ms(rng, straggler_p, force_straggler=False) * slowdown
            straggled = primary >= 200.0 * slowdown
            done = primary
            if budget is not None:
                budget.deposit()
            if primary > delay and (budget is None or budget.try_withdraw()):
                backups += 1
                shared = straggled and before < correlation
                second = _leg_ms(rng, straggler_p, force_straggler=shared) * slowdown
                done = min(primary, delay + cross_zone_rtt_ms + second)
            slowest = max(slowest, done)
        totals.append(slowest)
    return TailStats(
        p50_ms=round(nearest_rank(totals, 50), 1),
        p99_ms=round(nearest_rank(totals, 99), 1),
        p999_ms=round(nearest_rank(totals, 99.9), 1),
        extra_load=round(backups / (requests * fanout), 4),
    )
