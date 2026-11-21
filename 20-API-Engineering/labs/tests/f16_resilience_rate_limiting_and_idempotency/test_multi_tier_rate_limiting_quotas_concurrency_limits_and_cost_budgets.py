"""Lab: layered per-IP, global, per-tenant rate, concurrency, and cost-budget admission."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from limits import RateLimitItemPerMinute
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from api_labs.f16_resilience_rate_limiting_and_idempotency.layered_limiter import (
    LayeredLimiter,
    LayeredLimitMiddleware,
    Lease,
    ManualClock,
    Plan,
    Rejection,
    client_key,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import Scope

PLAN = Plan(rate=1.0, burst=5.0, max_in_flight=2, cost_rate=10.0, cost_capacity=100.0)


def make_limiter(clock: ManualClock, **overrides: float) -> LayeredLimiter:
    return LayeredLimiter(
        ip_rate=overrides.get("ip_rate", 10.0),
        ip_burst=overrides.get("ip_burst", 10.0),
        global_rate=overrides.get("global_rate", 1000.0),
        global_burst=overrides.get("global_burst", 1000.0),
        plans={},
        default_plan=PLAN,
        clock=clock,
    )


def admitted(decision: Lease | Rejection) -> Lease:
    assert isinstance(decision, Lease), decision
    return decision


def rejected(decision: Lease | Rejection) -> Rejection:
    assert isinstance(decision, Rejection), decision
    return decision


@pytest.mark.unit
def test_rejected_request_does_not_spend_earlier_tiers() -> None:
    clock = ManualClock()
    limiter = make_limiter(clock)
    for _ in range(5):
        admitted(limiter.admit("198.51.100.7", "acme")).release()
    assert limiter.tokens("ip", "198.51.100.7") == 5.0
    rejection = rejected(limiter.admit("198.51.100.7", "acme"))
    assert rejection.tier == "tenant-rate"
    # The IP tier was evaluated but not charged, because a later tier said no.
    assert limiter.tokens("ip", "198.51.100.7") == 5.0


@pytest.mark.unit
def test_sequential_hits_in_limits_library_charge_earlier_limits() -> None:
    # slowapi 0.1.10 calls `hit` per limit in order and stops at the first failure.
    strategy = MovingWindowRateLimiter(MemoryStorage())
    loose, strict = RateLimitItemPerMinute(10), RateLimitItemPerMinute(1)
    assert strategy.hit(loose, "acme")
    assert strategy.hit(strict, "acme")
    assert strategy.hit(loose, "acme")
    assert not strategy.hit(strict, "acme")
    assert strategy.get_window_stats(loose, "acme").remaining == 8


@pytest.mark.unit
def test_retry_after_reports_the_slowest_violated_tier() -> None:
    clock = ManualClock()
    limiter = make_limiter(clock, ip_rate=1.0, ip_burst=1.0)
    admitted(limiter.admit("198.51.100.7", "acme", estimated_cost=100)).release()
    rejection = rejected(limiter.admit("198.51.100.7", "acme", estimated_cost=50))
    assert set(rejection.violated) == {"ip", "tenant-cost"}
    assert rejection.tier == "tenant-cost"
    assert rejection.status == 429
    assert rejection.retry_after == 5


@pytest.mark.unit
def test_global_tier_alone_returns_503_and_client_tiers_return_429() -> None:
    clock = ManualClock()
    limiter = make_limiter(clock, global_rate=1.0, global_burst=6.0)
    for _ in range(5):
        admitted(limiter.admit("198.51.100.7", "acme")).release()
    admitted(limiter.admit("203.0.113.9", "globex")).release()
    overload = rejected(limiter.admit("203.0.113.9", "globex"))
    assert overload.violated == ("global",)
    assert overload.status == 503
    both = rejected(limiter.admit("198.51.100.7", "acme"))
    assert set(both.violated) == {"global", "tenant-rate"}
    assert both.status == 429


@pytest.mark.unit
def test_concurrency_tier_counts_in_flight_not_rate() -> None:
    clock = ManualClock()
    limiter = make_limiter(clock)
    first = admitted(limiter.admit("198.51.100.7", "acme"))
    admitted(limiter.admit("198.51.100.7", "acme"))
    third = rejected(limiter.admit("198.51.100.7", "acme"))
    assert third.violated == ("tenant-concurrency",)
    assert limiter.tokens("tenant-rate", "acme") == 3.0
    first.release()
    admitted(limiter.admit("198.51.100.7", "acme"))


@pytest.mark.unit
def test_release_is_idempotent() -> None:
    clock = ManualClock()
    limiter = make_limiter(clock)
    first = admitted(limiter.admit("198.51.100.7", "acme"))
    admitted(limiter.admit("198.51.100.7", "acme"))
    first.release()
    first.release()
    assert limiter.in_flight("acme") == 1
    admitted(limiter.admit("198.51.100.7", "acme"))
    assert rejected(limiter.admit("198.51.100.7", "acme")).tier == "tenant-concurrency"


@pytest.mark.unit
def test_cost_is_reserved_then_refunded_or_charged_as_debt() -> None:
    clock = ManualClock()
    limiter = make_limiter(clock)
    admitted(limiter.admit("198.51.100.7", "acme", estimated_cost=60)).release(actual_cost=20)
    assert limiter.tokens("tenant-cost", "acme") == 80.0
    admitted(limiter.admit("198.51.100.7", "acme", estimated_cost=50)).release(actual_cost=150)
    assert limiter.tokens("tenant-cost", "acme") == -70.0
    debt = rejected(limiter.admit("198.51.100.7", "acme", estimated_cost=1))
    assert debt.tier == "tenant-cost"
    assert debt.retry_after == 8
    clock.advance(7.2)
    admitted(limiter.admit("198.51.100.7", "acme", estimated_cost=1))


@pytest.mark.unit
def test_cost_above_capacity_is_permanent_400_without_retry_after() -> None:
    clock = ManualClock()
    limiter = make_limiter(clock)
    rejection = rejected(limiter.admit("198.51.100.7", "acme", estimated_cost=101))
    assert (rejection.status, rejection.retry_after) == (400, None)
    assert limiter.tokens("ip", "198.51.100.7") == 10.0


@pytest.mark.unit
def test_ipv6_addresses_in_one_slash_64_share_a_bucket() -> None:
    assert client_key("2001:db8:1:2::a") == client_key("2001:db8:1:2:ffff::1")
    assert client_key("2001:db8:1:2::a") == "2001:db8:1:2::/64"
    assert client_key("2001:db8:1:3::a") != client_key("2001:db8:1:2::a")
    assert client_key("::ffff:198.51.100.7") == "198.51.100.7"
    clock = ManualClock()
    limiter = make_limiter(clock, ip_rate=1.0, ip_burst=1.0)
    admitted(limiter.admit("2001:db8:1:2::a", None)).release()
    assert rejected(limiter.admit("2001:db8:1:2::b", None)).tier == "ip"


@pytest.mark.unit
def test_tenants_are_isolated_but_global_caps_the_sum() -> None:
    clock = ManualClock()
    limiter = make_limiter(clock, global_rate=1.0, global_burst=8.0)
    for _ in range(5):
        admitted(limiter.admit("198.51.100.7", "acme")).release()
    assert rejected(limiter.admit("198.51.100.7", "acme")).tier == "tenant-rate"
    for _ in range(3):
        admitted(limiter.admit("203.0.113.9", "globex")).release()
    assert rejected(limiter.admit("203.0.113.9", "globex")).violated == ("global",)


@pytest.mark.unit
def test_clock_stepping_backwards_mints_no_tokens() -> None:
    clock = ManualClock(start=100.0)
    limiter = make_limiter(clock)
    admitted(limiter.admit("198.51.100.7", "acme")).release()
    clock.advance(-50.0)
    assert limiter.tokens("tenant-rate", "acme") == 4.0
    clock.advance(51.0)
    assert limiter.tokens("tenant-rate", "acme") == 5.0


def tenant_header(scope: Scope) -> str | None:
    for name, value in scope["headers"]:
        if name == b"x-tenant":
            return str(value.decode("latin-1"))
    return None


def cost_header(scope: Scope) -> int:
    for name, value in scope["headers"]:
        if name == b"x-cost":
            return int(value)
    return 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_holds_permit_until_last_body_chunk() -> None:
    limiter = make_limiter(ManualClock())
    seen: list[int] = []

    async def chunks() -> AsyncIterator[bytes]:
        for part in (b"a", b"b", b"c"):
            seen.append(limiter.in_flight("acme"))
            yield part

    async def stream(request: Request) -> StreamingResponse:
        return StreamingResponse(chunks())

    app = LayeredLimitMiddleware(
        Starlette(routes=[Route("/s", stream)]), limiter, tenant_header, cost_header
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/s", headers={"x-tenant": "acme"})
    assert response.content == b"abc"
    assert seen == [1, 1, 1]
    assert limiter.in_flight("acme") == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_base_http_middleware_release_after_call_next_frees_permit_mid_stream() -> None:
    limiter = make_limiter(ManualClock())
    seen: list[int] = []

    async def chunks() -> AsyncIterator[bytes]:
        for part in (b"a", b"b"):
            seen.append(limiter.in_flight("acme"))
            yield part
        seen.append(limiter.in_flight("acme"))

    async def stream(request: Request) -> StreamingResponse:
        return StreamingResponse(chunks())

    async def naive(request: Request, call_next: RequestResponseEndpoint) -> Response:
        lease = admitted(limiter.admit("198.51.100.7", request.headers.get("x-tenant")))
        try:
            return await call_next(request)
        finally:
            lease.release()

    inner = Starlette(routes=[Route("/s", stream)])
    app = BaseHTTPMiddleware(inner, dispatch=naive)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/s", headers={"x-tenant": "acme"})
    assert response.content == b"ab"
    # call_next returns at `http.response.start`; the rest of the body streams unprotected.
    assert seen[-2:] == [0, 0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_releases_permit_when_endpoint_raises() -> None:
    limiter = make_limiter(ManualClock())

    async def boom(request: Request) -> Response:
        raise RuntimeError("downstream exploded")

    app = LayeredLimitMiddleware(
        Starlette(routes=[Route("/boom", boom)]), limiter, tenant_header, cost_header
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(RuntimeError, match="downstream exploded"):
            await client.get("/boom", headers={"x-tenant": "acme"})
    assert limiter.in_flight("acme") == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_settles_actual_cost_and_sends_retry_after() -> None:
    limiter = make_limiter(ManualClock())

    async def search(request: Request) -> JSONResponse:
        request.state.actual_cost = 3
        return JSONResponse({"hits": 3})

    app = LayeredLimitMiddleware(
        Starlette(routes=[Route("/search", search)]), limiter, tenant_header, cost_header
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.get("/search", headers={"x-tenant": "acme", "x-cost": "90"})
        assert ok.status_code == 200
        assert limiter.tokens("tenant-cost", "acme") == 97.0
        limited = await client.get("/search", headers={"x-tenant": "acme", "x-cost": "98"})
        too_big = await client.get("/search", headers={"x-tenant": "acme", "x-cost": "101"})
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "1"
    assert limited.json() == {
        "error": "rate_limited",
        "tier": "tenant-cost",
        "violated": ["tenant-cost"],
    }
    assert too_big.status_code == 400
    assert "retry-after" not in too_big.headers
