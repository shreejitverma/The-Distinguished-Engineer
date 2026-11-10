"""Cache stampede defenses: asyncio singleflight, memcache-style leases, and XFetch."""

from __future__ import annotations

import asyncio
import itertools
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Hashable, Iterator

    from starlette.requests import Request


class SingleFlight[K: Hashable, V]:
    """Run at most one in-flight load per key; concurrent callers await the same task.

    Mirrors `golang.org/x/sync/singleflight` `Group.Do`, adapted to asyncio cancellation.
    """

    def __init__(self) -> None:
        self._calls: dict[K, asyncio.Task[V]] = {}
        self._waiters: dict[K, int] = {}

    def waiters(self, key: K) -> int:
        """Number of callers currently awaiting a load for `key` (old and new after `forget`)."""
        return self._waiters.get(key, 0)

    def in_flight(self, key: K) -> bool:
        return key in self._calls

    def forget(self, key: K) -> None:
        """Make the next `do(key)` start a new load even if one is in flight (Go's `Forget`)."""
        self._calls.pop(key, None)

    async def _run(self, key: K, fn: Callable[[], Coroutine[Any, Any, V]]) -> V:
        try:
            return await fn()
        finally:
            # Forget the call before its result is published, like Go's doCall defer,
            # so the next miss after completion starts a fresh load (no error caching).
            if self._calls.get(key) is asyncio.current_task():
                del self._calls[key]

    async def do(self, key: K, fn: Callable[[], Coroutine[Any, Any, V]]) -> tuple[V, bool]:
        """Return `(value, joined)`; `joined` is True when this caller reused another's load."""
        task = self._calls.get(key)
        joined = task is not None
        if task is None:
            task = asyncio.create_task(self._run(key, fn))
            self._calls[key] = task
        self._waiters[key] = self._waiters.get(key, 0) + 1
        try:
            # shield: cancelling one caller must not cancel the load the others share.
            value = await asyncio.shield(task)
        finally:
            remaining = self._waiters[key] - 1
            if remaining:
                self._waiters[key] = remaining
            else:
                del self._waiters[key]
        return value, joined


@dataclass(frozen=True, slots=True)
class _Entry[V]:
    value: V
    expires_at: float


class TTLCache[V]:
    """Read-through TTL cache; with `coalesce=True` misses for one key share one load."""

    def __init__(
        self,
        loader: Callable[[str], Coroutine[Any, Any, V]],
        *,
        ttl: float,
        clock: Callable[[], float],
        coalesce: bool,
    ) -> None:
        self._loader = loader
        self._ttl = ttl
        self._clock = clock
        self._coalesce = coalesce
        self._store: dict[str, _Entry[V]] = {}
        self.flight: SingleFlight[str, V] = SingleFlight()

    def put(self, key: str, value: V) -> None:
        self._store[key] = _Entry(value, self._clock() + self._ttl)

    async def _load_and_store(self, key: str) -> V:
        value = await self._loader(key)
        self.put(key, value)  # written before the flight is forgotten: no reload window
        return value

    async def get(self, key: str) -> V:
        entry = self._store.get(key)
        if entry is not None and self._clock() < entry.expires_at:
            return entry.value
        if not self._coalesce:
            return await self._load_and_store(key)
        value, _ = await self.flight.do(key, lambda: self._load_and_store(key))
        return value


def build_app(cache: TTLCache[dict[str, Any]]) -> Starlette:
    """A Starlette API whose `GET /items/{item_id}` reads through `cache`."""

    async def get_item(request: Request) -> JSONResponse:
        item_id: str = request.path_params["item_id"]
        return JSONResponse(await cache.get(f"item:{item_id}"))

    return Starlette(routes=[Route("/items/{item_id}", get_item, methods=["GET"])])


@dataclass(frozen=True, slots=True)
class Hit[V]:
    value: V


@dataclass(frozen=True, slots=True)
class LeaseGranted:
    token: int


@dataclass(frozen=True, slots=True)
class RetryLater[V]:
    stale: V | None


type LeaseResult[V] = Hit[V] | LeaseGranted | RetryLater[V]


@dataclass(frozen=True, slots=True)
class _Lease:
    token: int
    issued_at: float


class LeaseCache[V]:
    """Memcache lease protocol (Nishtala et al., NSDI 2013, Section 3.2.1).

    A miss returns a token bound to the key; only that token may fill the key.
    A delete invalidates the outstanding token (stale-set prevention), and at most
    one token per key is issued per `lease_interval` (thundering-herd prevention).
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        lease_interval: float = 10.0,
        tokens: Iterator[int] | None = None,
    ) -> None:
        self._clock = clock
        self._interval = lease_interval
        self._tokens = tokens if tokens is not None else itertools.count(1)
        self._values: dict[str, V] = {}
        self._stale: dict[str, V] = {}
        self._leases: dict[str, _Lease] = {}

    def get(self, key: str) -> LeaseResult[V]:
        if key in self._values:
            return Hit(self._values[key])
        now = self._clock()
        lease = self._leases.get(key)
        if lease is not None and now - lease.issued_at < self._interval:
            return RetryLater(self._stale.get(key))
        token = next(self._tokens)
        self._leases[key] = _Lease(token, now)
        return LeaseGranted(token)

    def set(self, key: str, value: V, token: int) -> bool:
        """Store `value` only if `token` is the key's current lease; return whether stored."""
        lease = self._leases.get(key)
        if lease is None or lease.token != token:
            return False
        del self._leases[key]
        self._values[key] = value
        self._stale.pop(key, None)
        return True

    def delete(self, key: str) -> None:
        if key in self._values:
            self._stale[key] = self._values.pop(key)
        self._leases.pop(key, None)


def unit_interval_open_at_zero(random: Callable[[], float]) -> float:
    """Map `random.random()` from [0, 1) onto (0, 1] so `math.log` never sees 0."""
    return 1.0 - random()


def xfetch_should_recompute(
    *, now: float, expiry: float, delta: float, beta: float, rand: Callable[[], float]
) -> bool:
    """XFetch test from Vattani et al., PVLDB 8(8) 2015, Figure 3.

    `now - delta * beta * log(rand()) >= expiry`, with `rand()` in (0, 1].
    """
    u = rand()
    if not 0.0 < u <= 1.0:
        raise ValueError(f"rand() must be in (0, 1], got {u!r}")
    return now - delta * beta * math.log(u) >= expiry


@dataclass(frozen=True, slots=True)
class StampedeSample:
    size: int
    early_gap: float


def simulate_cycle(
    *, arrivals_per_delta: int, ttl_in_deltas: float, beta: float, rand: Callable[[], float]
) -> StampedeSample:
    """One expiry cycle in the paper's model: time unit = recompute time `delta` = 1.

    Requests arrive every `1 / arrivals_per_delta`; the stampede size is the number of
    requests that decide to recompute in `[Z, Z + 1)`, where `Z` is the first decision.
    """
    first: int | None = None
    size = 0
    tick = 0
    while first is None or tick < first + arrivals_per_delta:
        now = tick / arrivals_per_delta
        if xfetch_should_recompute(now=now, expiry=ttl_in_deltas, delta=1.0, beta=beta, rand=rand):
            if first is None:
                first = tick
            size += 1
        tick += 1
    return StampedeSample(size=size, early_gap=max(ttl_in_deltas - first / arrivals_per_delta, 0.0))
