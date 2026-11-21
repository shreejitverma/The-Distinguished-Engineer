"""Five rate limiting algorithms on an injected clock, plus a transcription of nginx limit_req.

Every limiter takes ``now`` (seconds, any monotonic origin) instead of reading a clock, so the
lab is deterministic and a test can place a request at exactly t = 59.9 s or t = 60.1 s.
Every rejection carries ``retry_after``: the earliest time from ``now`` at which the same request
would be admitted if nothing else arrived, which is what a ``Retry-After`` header should encode.
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True, slots=True)
class Decision:
    """Outcome of one admission check."""

    allowed: bool
    retry_after: float = 0.0  # seconds until this request would fit; 0.0 when allowed
    delay: float = 0.0  # shapers only: how long an admitted request waits before it runs


class Limiter(Protocol):
    def acquire(self, now: float, cost: int = 1) -> Decision: ...


def _positive(name: str, value: float) -> None:
    if not value > 0 or math.isinf(value):
        raise ValueError(f"{name} must be a positive finite number, got {value!r}")


def _check_cost(cost: int) -> None:
    if cost < 1:
        raise ValueError(f"cost must be >= 1, got {cost!r}")


class FixedWindowCounter:
    """One counter per aligned window [k*W, (k+1)*W); resets to zero at every boundary."""

    def __init__(self, limit: int, window: float) -> None:
        _positive("limit", limit)
        _positive("window", window)
        self.limit = limit
        self.window = window
        self._index = -1
        self._count = 0

    def acquire(self, now: float, cost: int = 1) -> Decision:
        _check_cost(cost)
        index = math.floor(now / self.window)
        if index > self._index:
            self._index, self._count = index, 0
        if cost > self.limit:
            return Decision(False, math.inf)
        if self._count + cost > self.limit:
            return Decision(False, (self._index + 1) * self.window - now)
        self._count += cost
        return Decision(True)


class SlidingWindowLog:
    """Exact: keeps one timestamp per admitted unit and counts those in (now - W, now]."""

    def __init__(self, limit: int, window: float) -> None:
        _positive("limit", limit)
        _positive("window", window)
        self.limit = limit
        self.window = window
        self._log: deque[float] = deque()

    @property
    def stored_timestamps(self) -> int:
        return len(self._log)

    def acquire(self, now: float, cost: int = 1) -> Decision:
        _check_cost(cost)
        while self._log and self._log[0] <= now - self.window:
            self._log.popleft()
        if cost > self.limit:
            return Decision(False, math.inf)
        overflow = len(self._log) + cost - self.limit
        if overflow > 0:
            # The overflow-th oldest entry must leave the window before this request fits.
            return Decision(False, self._log[overflow - 1] + self.window - now)
        self._log.extend([now] * cost)
        return Decision(True)


class SlidingWindowCounter:
    """Approximation: previous window count weighted by its remaining overlap, plus current.

    estimate(now) = previous * (1 - elapsed / W) + current, the formula in Cloudflare's 2017 post.
    Admission is strict (estimate + cost <= limit); ``limits`` 5.8.0 floors the estimate first.
    """

    def __init__(self, limit: int, window: float) -> None:
        _positive("limit", limit)
        _positive("window", window)
        self.limit = limit
        self.window = window
        self._index = 0
        self._previous = 0
        self._current = 0

    def _roll(self, now: float) -> None:
        index = math.floor(now / self.window)
        if index > self._index:
            self._previous = self._current if index == self._index + 1 else 0
            self._current = 0
            self._index = index

    def estimate(self, now: float) -> float:
        self._roll(now)
        start = self._index * self.window
        elapsed = min(max(now - start, 0.0), self.window)
        return self._previous * (1.0 - elapsed / self.window) + self._current

    def acquire(self, now: float, cost: int = 1) -> Decision:
        _check_cost(cost)
        if cost > self.limit:
            return Decision(False, math.inf)
        if self.estimate(now) + cost <= self.limit:
            self._current += cost
            return Decision(True)
        start = self._index * self.window
        room = self.limit - self._current - cost
        if room >= 0:
            # Fits later in this window once the previous window's weight has decayed enough.
            at = start + self.window * (1.0 - room / self._previous)
        else:
            # Only fits in the next window, where today's count becomes the decaying previous one.
            fraction = max(0.0, 1.0 - (self.limit - cost) / self._current)
            at = start + self.window * (1.0 + fraction)
        return Decision(False, max(0.0, at - now))


class TokenBucket:
    """Starts full; refills ``rate`` tokens per second up to ``capacity``; strict conformance."""

    def __init__(self, rate: float, capacity: float) -> None:
        _positive("rate", rate)
        _positive("capacity", capacity)
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._last: float | None = None

    def tokens(self, now: float) -> float:
        if self._last is None:
            self._last = now
        elapsed = max(0.0, now - self._last)  # a clock step backwards mints nothing
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last = max(self._last, now)
        return self._tokens

    def acquire(self, now: float, cost: int = 1) -> Decision:
        _check_cost(cost)
        available = self.tokens(now)
        if cost > self.capacity:
            return Decision(False, math.inf)
        if available >= cost:
            self._tokens = available - cost
            return Decision(True)
        return Decision(False, (cost - available) / self.rate)


class LeakyBucketMeter:
    """Leaky bucket as a meter: a level that drains at ``rate``; admit while it stays <= capacity.

    Its state is the mirror image of a token bucket (level = capacity - tokens), so with equal
    parameters it makes the same decision for every arrival sequence.
    """

    def __init__(self, rate: float, capacity: float) -> None:
        _positive("rate", rate)
        _positive("capacity", capacity)
        self.rate = rate
        self.capacity = capacity
        self._level = 0.0
        self._last: float | None = None

    def acquire(self, now: float, cost: int = 1) -> Decision:
        _check_cost(cost)
        if self._last is None:
            self._last = now
        elapsed = max(0.0, now - self._last)
        self._level = max(0.0, self._level - elapsed * self.rate)
        self._last = max(self._last, now)
        if cost > self.capacity:
            return Decision(False, math.inf)
        if self._level + cost <= self.capacity:
            self._level += cost
            return Decision(True)
        return Decision(False, (self._level + cost - self.capacity) / self.rate)


class LeakyBucketQueue:
    """Leaky bucket as a queue (a shaper): admitted requests leave exactly 1/rate apart.

    ``max_waiting`` bounds the requests scheduled in the future, like nginx ``burst`` without
    ``nodelay``; the bucket smooths output instead of passing a burst through.
    """

    def __init__(self, rate: float, max_waiting: int) -> None:
        _positive("rate", rate)
        if max_waiting < 0:
            raise ValueError("max_waiting must be >= 0")
        self.interval = 1.0 / rate
        self.max_waiting = max_waiting
        self._departures: deque[float] = deque()
        self._last_departure = -math.inf

    def acquire(self, now: float, cost: int = 1) -> Decision:
        _check_cost(cost)
        while self._departures and self._departures[0] <= now:
            self._departures.popleft()
        departure = max(now, self._last_departure + self.interval)
        slots = [departure + unit * self.interval for unit in range(cost)]
        waiting = len(self._departures) + sum(1 for slot in slots if slot > now)
        if waiting > self.max_waiting:
            if not self._departures:
                return Decision(False, math.inf)  # cost alone exceeds the queue
            return Decision(False, self._departures[0] - now)
        self._departures.extend(slots)
        self._last_departure = slots[-1]
        return Decision(True, delay=departure - now)


class NginxLimitReq:
    """Line-by-line model of nginx 1.31.8 ``ngx_http_limit_req_lookup`` for one key, ``nodelay``.

    State is integer milli-requests: ``excess`` (1 == 0.001 request) and ``last`` (ms).
    """

    def __init__(self, rate: int, burst: int, per: Literal["r/s", "r/m"] = "r/s") -> None:
        scale = 1 if per == "r/s" else 60
        self.rate = rate * 1000 // scale  # ctx->rate = rate * 1000 / scale (truncates)
        self.burst = burst * 1000  # limit->burst = burst * 1000
        self._excess: int | None = None
        self._last = 0

    def acquire(self, now_ms: int) -> bool:
        if self._excess is None:  # new node: excess = 0, last = now, NGX_OK
            self._excess, self._last = 0, now_ms
            return True
        ms = now_ms - self._last
        if ms < -60000:
            ms = 1
        elif ms < 0:
            ms = 0
        excess = max(0, self._excess - self.rate * ms // 1000 + 1000)
        if excess > self.burst:
            return False  # NGX_BUSY: state untouched
        self._excess = excess
        if ms:
            self._last = now_ms
        return True


class RateLimitMiddleware:
    """Pure ASGI middleware: one limiter per client address, ``429`` plus ``Retry-After``."""

    def __init__(
        self,
        app: ASGIApp,
        make_limiter: Callable[[], Limiter],
        clock: Callable[[], float],
    ) -> None:
        self.app = app
        self.make_limiter = make_limiter
        self.clock = clock
        self.limiters: dict[str, Limiter] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        key = client[0] if client else "unknown"
        limiter = self.limiters.setdefault(key, self.make_limiter())
        decision = limiter.acquire(self.clock())
        if decision.allowed:
            await self.app(scope, receive, send)
            return
        # RFC 9110 Section 10.2.3: delay-seconds is 1*DIGIT, so round up, never down to 0.
        retry_after = max(1, math.ceil(decision.retry_after))
        body = json.dumps({"detail": "rate limit exceeded", "retry_after": retry_after}).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"retry-after", str(retry_after).encode()),
        ]
        await send({"type": "http.response.start", "status": 429, "headers": headers})
        await send({"type": "http.response.body", "body": body})
