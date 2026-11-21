"""Layered admission control: per-IP, global, per-tenant rate, concurrency, and cost tiers.

Every tier is evaluated before any tier is charged (check-all-then-commit), so a request
rejected by a later tier never spends an earlier tier's budget.
A request holds its concurrency permit until the response body has been fully sent, and
its estimated cost is settled against the actual cost when it finishes.
"""

from __future__ import annotations

import ipaddress
import json
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

Tier = Literal["ip", "global", "tenant-rate", "tenant-concurrency", "tenant-cost"]
Clock = Callable[[], float]


class ManualClock:
    """A deterministic monotonic clock for tests: time moves only when told to."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass(slots=True)
class TokenBucket:
    """Continuous-refill token bucket; `tokens` may go negative to record cost debt."""

    capacity: float
    rate: float
    tokens: float
    updated: float

    def _refill(self, now: float) -> None:
        # A clock that steps backwards must never mint tokens or move `updated` back.
        elapsed = max(0.0, now - self.updated)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.updated = max(self.updated, now)

    def wait_for(self, cost: float, now: float) -> float:
        """Seconds until `cost` tokens are available; 0.0 means admissible now."""
        self._refill(now)
        if self.tokens >= cost:
            return 0.0
        return (cost - self.tokens) / self.rate

    def take(self, amount: float, now: float) -> None:
        self._refill(now)
        self.tokens -= amount

    def give(self, amount: float, now: float) -> None:
        self._refill(now)
        self.tokens = min(self.capacity, self.tokens + amount)


@dataclass(frozen=True, slots=True)
class Plan:
    """Per-tenant limits: request rate, in-flight cap, and a cost budget in cost units."""

    rate: float
    burst: float
    max_in_flight: int
    cost_rate: float
    cost_capacity: float


@dataclass(frozen=True, slots=True)
class Rejection:
    """Why a request was refused; `retry_after` is None when retrying cannot succeed."""

    tier: Tier
    violated: tuple[Tier, ...]
    status: int
    retry_after: int | None


SERVER_TIERS: frozenset[Tier] = frozenset({"global"})


def client_key(address: str | None, ipv6_prefix: int = 64) -> str:
    """Normalize a peer address into a rate-limit key.

    IPv4-mapped IPv6 collapses to IPv4, and IPv6 collapses to its /`ipv6_prefix` network,
    because one subscriber is routinely delegated a whole /64 (or more) to rotate through.
    """
    if address is None:
        return "unknown"
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "unknown"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((ip, ipv6_prefix), strict=False))
    return str(ip)


@dataclass(slots=True)
class Lease:
    """An admitted request's claim on the concurrency and cost tiers."""

    tenant: str | None
    estimated_cost: int
    limiter: LayeredLimiter
    released: bool = False

    def release(self, actual_cost: int | None = None) -> None:
        self.limiter.settle(self, actual_cost)


@dataclass
class LayeredLimiter:
    """Five tiers in one critical section: evaluate all, then charge all or none."""

    ip_rate: float
    ip_burst: float
    global_rate: float
    global_burst: float
    plans: Mapping[str, Plan]
    default_plan: Plan
    clock: Clock
    concurrency_retry_after: int = 1
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _ip: dict[str, TokenBucket] = field(default_factory=dict[str, TokenBucket], init=False)
    _rate: dict[str, TokenBucket] = field(default_factory=dict[str, TokenBucket], init=False)
    _cost: dict[str, TokenBucket] = field(default_factory=dict[str, TokenBucket], init=False)
    _in_flight: dict[str, int] = field(default_factory=dict[str, int], init=False)
    _global: TokenBucket | None = field(default=None, init=False)

    def plan_for(self, tenant: str) -> Plan:
        return self.plans.get(tenant, self.default_plan)

    def _bucket(
        self, table: dict[str, TokenBucket], key: str, capacity: float, rate: float
    ) -> TokenBucket:
        bucket = table.get(key)
        if bucket is None:
            bucket = TokenBucket(capacity, rate, capacity, self.clock())
            table[key] = bucket
        return bucket

    def _global_bucket(self) -> TokenBucket:
        if self._global is None:
            self._global = TokenBucket(
                self.global_burst, self.global_rate, self.global_burst, self.clock()
            )
        return self._global

    def admit(
        self, client: str | None, tenant: str | None, estimated_cost: int = 1
    ) -> Lease | Rejection:
        if estimated_cost < 0:
            raise ValueError("estimated_cost must be >= 0")
        with self._lock:
            now = self.clock()
            ip_bucket = self._bucket(self._ip, client_key(client), self.ip_burst, self.ip_rate)
            global_bucket = self._global_bucket()
            # Phase 1: evaluate every tier without charging any of them.
            waits: dict[Tier, float] = {
                "ip": ip_bucket.wait_for(1, now),
                "global": global_bucket.wait_for(1, now),
            }
            rate_bucket: TokenBucket | None = None
            cost_bucket: TokenBucket | None = None
            if tenant is not None:
                plan = self.plan_for(tenant)
                if estimated_cost > plan.cost_capacity:
                    # No amount of waiting fills a bucket past its capacity: not retryable.
                    return Rejection("tenant-cost", ("tenant-cost",), 400, None)
                rate_bucket = self._bucket(self._rate, tenant, plan.burst, plan.rate)
                cost_bucket = self._bucket(self._cost, tenant, plan.cost_capacity, plan.cost_rate)
                waits["tenant-rate"] = rate_bucket.wait_for(1, now)
                if self._in_flight.get(tenant, 0) >= plan.max_in_flight:
                    waits["tenant-concurrency"] = float(self.concurrency_retry_after)
                waits["tenant-cost"] = cost_bucket.wait_for(estimated_cost, now)
            violated: tuple[Tier, ...] = tuple(t for t, wait in waits.items() if wait > 0.0)
            if violated:
                binding: Tier = max(violated, key=lambda t: waits[t])
                status = 503 if set(violated) <= SERVER_TIERS else 429
                retry_after = max(1, math.ceil(waits[binding]))
                return Rejection(binding, violated, status, retry_after)
            # Phase 2: every tier admits, so charge every tier.
            ip_bucket.take(1, now)
            global_bucket.take(1, now)
            if tenant is not None and rate_bucket is not None and cost_bucket is not None:
                rate_bucket.take(1, now)
                cost_bucket.take(estimated_cost, now)
                self._in_flight[tenant] = self._in_flight.get(tenant, 0) + 1
            return Lease(tenant, estimated_cost, self)

    def settle(self, lease: Lease, actual_cost: int | None) -> None:
        """Free the permit once and true up the cost reservation (refund or debt)."""
        if actual_cost is not None and actual_cost < 0:
            raise ValueError("actual_cost must be >= 0")
        with self._lock:
            if lease.released:
                return
            lease.released = True
            tenant = lease.tenant
            if tenant is None:
                return
            self._in_flight[tenant] -= 1
            if actual_cost is None:
                return
            bucket = self._cost[tenant]
            delta = actual_cost - lease.estimated_cost
            if delta < 0:
                bucket.give(-delta, self.clock())
            elif delta > 0:
                bucket.take(delta, self.clock())

    def in_flight(self, tenant: str) -> int:
        with self._lock:
            return self._in_flight.get(tenant, 0)

    def tokens(self, tier: Literal["ip", "tenant-rate", "tenant-cost"], key: str) -> float:
        """Current balance of one bucket, refilled to now; for tests and metrics."""
        table = {"ip": self._ip, "tenant-rate": self._rate, "tenant-cost": self._cost}[tier]
        with self._lock:
            bucket = table[key]
            bucket.wait_for(0, self.clock())
            return bucket.tokens


def _rejection_response(rejection: Rejection) -> tuple[list[tuple[bytes, bytes]], bytes]:
    body = json.dumps(
        {"error": "rate_limited", "tier": rejection.tier, "violated": list(rejection.violated)}
    ).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if rejection.retry_after is not None:
        headers.append((b"retry-after", str(rejection.retry_after).encode()))
    return headers, body


class LayeredLimitMiddleware:
    """Pure ASGI middleware: the permit is held until the app has sent its last body chunk."""

    def __init__(
        self,
        app: ASGIApp,
        limiter: LayeredLimiter,
        identify: Callable[[Scope], str | None],
        estimate: Callable[[Scope], int],
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.identify = identify
        self.estimate = estimate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        decision = self.limiter.admit(
            client[0] if client else None, self.identify(scope), self.estimate(scope)
        )
        if isinstance(decision, Rejection):
            headers, body = _rejection_response(decision)
            start: Message = {
                "type": "http.response.start",
                "status": decision.status,
                "headers": headers,
            }
            await send(start)
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return
        # Starlette's request.state writes into this same dict, so the app can report cost.
        state: dict[str, object] = scope.setdefault("state", {})
        try:
            await self.app(scope, receive, send)
        finally:
            reported = state.get("actual_cost")
            decision.release(reported if isinstance(reported, int) else None)
