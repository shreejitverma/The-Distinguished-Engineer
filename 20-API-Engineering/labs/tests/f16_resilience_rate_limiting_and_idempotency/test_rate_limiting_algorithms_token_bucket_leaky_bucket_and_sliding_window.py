"""Fixed window, sliding log, sliding counter, token bucket, and leaky bucket on one clock."""

from __future__ import annotations

import math
import random
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx
import limits.storage.memory
import pytest
from limits import RateLimitItemPerMinute
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter, SlidingWindowCounterRateLimiter
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from api_labs.f16_resilience_rate_limiting_and_idempotency.rate_limiting_algorithms import (
    FixedWindowCounter,
    LeakyBucketMeter,
    LeakyBucketQueue,
    Limiter,
    NginxLimitReq,
    RateLimitMiddleware,
    SlidingWindowCounter,
    SlidingWindowLog,
    TokenBucket,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from starlette.requests import Request


def admitted(limiter: Limiter, times: Sequence[float]) -> list[float]:
    return [t for t in times if limiter.acquire(t).allowed]


def max_in_any_window(times: Sequence[float], window: float) -> int:
    """Largest number of events inside any half-open interval [s, s + window)."""
    ordered = sorted(times)
    best, j = 0, 0
    for i, start in enumerate(ordered):
        j = max(j, i)
        while j < len(ordered) and ordered[j] < start + window:
            j += 1
        best = max(best, j - i)
    return best


BOUNDARY = [59.9] * 100 + [60.1] * 100  # the lab plan: 100 just before and 100 just after t=60


@pytest.mark.unit
def test_fixed_window_admits_twice_the_limit_across_the_boundary() -> None:
    passed = admitted(FixedWindowCounter(limit=100, window=60), BOUNDARY)
    assert len(passed) == 200
    assert max(passed) - min(passed) == pytest.approx(0.2)


@pytest.mark.unit
@pytest.mark.parametrize(
    "make",
    [
        lambda: SlidingWindowLog(limit=100, window=60),
        lambda: SlidingWindowCounter(limit=100, window=60),
        lambda: TokenBucket(rate=100 / 60, capacity=100),
        lambda: LeakyBucketMeter(rate=100 / 60, capacity=100),
    ],
    ids=["sliding-log", "sliding-counter", "token-bucket", "leaky-meter"],
)
def test_other_algorithms_admit_only_the_limit_across_the_boundary(
    make: Callable[[], Limiter],
) -> None:
    passed = admitted(make(), BOUNDARY)
    assert passed == [59.9] * 100


@pytest.mark.unit
def test_sliding_log_memory_is_one_timestamp_per_admitted_request() -> None:
    log = SlidingWindowLog(limit=100, window=60)
    admitted(log, BOUNDARY)
    assert log.stored_timestamps == 100
    log.acquire(120.0)
    assert log.stored_timestamps == 1  # the 59.9 entries left (0.0 is outside (60, 120])


@pytest.mark.unit
def test_token_bucket_admits_capacity_plus_refill_inside_one_window() -> None:
    # Limit "64 per 64 s" as a bucket: capacity 64, rate 1/s. Spend the burst, then each token.
    arrivals = [0.0] * 64 + [float(t) for t in range(1, 65)]
    bucket = admitted(TokenBucket(rate=1.0, capacity=64), arrivals)
    log = admitted(SlidingWindowLog(limit=64, window=64), arrivals)
    assert len(bucket) == 128
    assert max_in_any_window(bucket, 64) == 127  # R*t + B, not "64 per any 64 s"
    assert max_in_any_window(log, 64) == 64


@pytest.mark.unit
def test_sliding_counter_reproduces_cloudflare_49_5_and_rejects_strictly() -> None:
    counter = SlidingWindowCounter(limit=50, window=60)
    assert admitted(counter, [10.0] * 42) == [10.0] * 42  # previous minute [0, 60)
    assert admitted(counter, [75.0] * 18) == [75.0] * 18  # 15 s into [60, 120)
    assert counter.estimate(75.0) == 49.5  # 42 * (60 - 15) / 60 + 18
    decision = counter.acquire(75.0)
    assert not decision.allowed  # 49.5 + 1 > 50


@pytest.mark.unit
def test_limits_5_8_0_sliding_counter_floors_the_estimate_and_admits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = SimpleNamespace(time=lambda: 60.0)
    monkeypatch.setattr(limits.storage.memory, "time", clock)
    limiter = SlidingWindowCounterRateLimiter(MemoryStorage())
    item = RateLimitItemPerMinute(50)
    assert all(limiter.hit(item, "k") for _ in range(42))  # window [60, 120)
    clock.time = lambda: 135.0  # 15 s into [120, 180)
    assert all(limiter.hit(item, "k") for _ in range(18))
    assert limiter.hit(item, "k")  # floor(49.5) + 1 = 50 <= 50: the 61st hit is admitted
    assert not limiter.hit(item, "k")  # floor(50.5) + 1 = 51 > 50


@pytest.mark.unit
def test_limits_5_8_0_fixed_window_starts_at_the_first_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = SimpleNamespace(time=lambda: 50.0)
    monkeypatch.setattr(limits.storage.memory, "time", clock)
    limiter = FixedWindowRateLimiter(MemoryStorage())
    item = RateLimitItemPerMinute(2)
    assert limiter.hit(item, "k")
    assert limiter.hit(item, "k")
    clock.time = lambda: 61.0
    assert not limiter.hit(item, "k")  # the key expires at 50 + 60, not at the minute mark
    assert FixedWindowCounter(limit=2, window=60).acquire(61.0).allowed  # aligned windows reset
    clock.time = lambda: 110.0
    assert limiter.hit(item, "k")


def _saturated_fixed() -> tuple[Limiter, float]:
    limiter = FixedWindowCounter(limit=4, window=8)
    admitted(limiter, [1.0] * 4)
    return limiter, 1.0


def _saturated_log() -> tuple[Limiter, float]:
    limiter = SlidingWindowLog(limit=3, window=8)
    admitted(limiter, [0.0, 1.0, 2.0])
    return limiter, 3.0


def _saturated_counter_this_window() -> tuple[Limiter, float]:
    limiter = SlidingWindowCounter(limit=8, window=64)
    admitted(limiter, [0.0] * 4 + [64.0] * 4)
    return limiter, 64.0  # previous 4, current 4: fits again at 80 when 4 * 0.75 + 4 + 1 = 8


def _saturated_counter_next_window() -> tuple[Limiter, float]:
    limiter = SlidingWindowCounter(limit=8, window=64)
    admitted(limiter, [64.0] * 8)
    return limiter, 64.0  # current 8: fits at 136 when 8 * (1 - 8 / 64) + 0 + 1 = 8


def _saturated_bucket() -> tuple[Limiter, float]:
    limiter = TokenBucket(rate=2.0, capacity=4)
    admitted(limiter, [0.0] * 4)
    return limiter, 0.0


def _saturated_meter() -> tuple[Limiter, float]:
    limiter = LeakyBucketMeter(rate=2.0, capacity=4)
    admitted(limiter, [0.0] * 4)
    return limiter, 0.0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("make", "expected"),
    [
        (_saturated_fixed, 7.0),
        (_saturated_log, 5.0),
        (_saturated_counter_this_window, 16.0),
        (_saturated_counter_next_window, 72.0),
        (_saturated_bucket, 0.5),
        (_saturated_meter, 0.5),
    ],
    ids=["fixed", "log", "counter-this-window", "counter-next-window", "bucket", "meter"],
)
def test_retry_after_is_the_exact_first_admission_time(
    make: Callable[[], tuple[Limiter, float]], expected: float
) -> None:
    limiter, now = make()
    decision = limiter.acquire(now)
    assert not decision.allowed
    assert decision.retry_after == expected
    assert not limiter.acquire(now + expected - 2**-10).allowed
    assert limiter.acquire(now + expected).allowed


@pytest.mark.unit
def test_leaky_meter_is_the_mirror_image_of_the_token_bucket() -> None:
    rng = random.Random(1616)  # noqa: S311 - a seeded, reproducible workload
    bucket, meter = TokenBucket(rate=4.0, capacity=8), LeakyBucketMeter(rate=4.0, capacity=8)
    now, rejections = 0.0, 0
    for _ in range(5000):
        now += rng.choice((0.0, 0.0, 0.0625, 0.125, 0.25, 0.5, 1.0))  # exact binary fractions
        cost = rng.choice((1, 1, 1, 2, 3))
        a, b = bucket.acquire(now, cost), meter.acquire(now, cost)
        assert (a.allowed, a.retry_after) == (b.allowed, b.retry_after), now
        rejections += not a.allowed
    assert 500 < rejections < 4500  # the stream exercises both outcomes


@pytest.mark.unit
def test_nginx_limit_req_admits_burst_plus_one_like_a_meter_of_that_capacity() -> None:
    nginx = NginxLimitReq(rate=1, burst=5)
    assert [nginx.acquire(0) for _ in range(7)] == [True] * 6 + [False]
    rng = random.Random(2017)  # noqa: S311 - a seeded, reproducible workload
    nginx, meter = NginxLimitReq(rate=2, burst=3), LeakyBucketMeter(rate=2.0, capacity=4)
    now_ms = 0
    for _ in range(5000):
        now_ms += rng.choice((0, 0, 125, 250, 500, 1000))
        assert nginx.acquire(now_ms) == meter.acquire(now_ms / 1000).allowed, now_ms


@pytest.mark.unit
def test_nginx_one_request_per_minute_truncates_to_one_per_62_5_seconds() -> None:
    nginx = NginxLimitReq(rate=1, burst=0, per="r/m")
    assert nginx.rate == 16  # 1 * 1000 // 60 milli-requests per second, not 16.67
    assert nginx.acquire(0)
    assert not nginx.acquire(60_000)
    assert not nginx.acquire(62_499)
    assert nginx.acquire(62_500)


@pytest.mark.unit
def test_nginx_clock_step_back_beyond_60_s_counts_as_one_millisecond() -> None:
    nginx = NginxLimitReq(rate=1, burst=0)
    assert nginx.acquire(100_000)
    assert not nginx.acquire(0)  # ms = -100000 < -60000 -> ms = 1, nothing drains


@pytest.mark.unit
def test_leaky_queue_spaces_departures_while_the_token_bucket_passes_the_burst() -> None:
    queue = LeakyBucketQueue(rate=2.0, max_waiting=5)
    decisions = [queue.acquire(0.0) for _ in range(7)]
    assert [d.delay for d in decisions[:6]] == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
    assert not decisions[6].allowed
    assert decisions[6].retry_after == 0.5
    assert queue.acquire(0.5).delay == 2.5  # leaves at 3.0, still 1/rate after the last one
    bucket = TokenBucket(rate=2.0, capacity=6)
    assert [bucket.acquire(0.0).allowed for _ in range(6)] == [True] * 6  # all at t = 0


@pytest.mark.unit
def test_clock_step_backwards_mints_no_tokens() -> None:
    bucket = TokenBucket(rate=1.0, capacity=2)
    admitted(bucket, [10.0, 10.0])
    assert not bucket.acquire(5.0).allowed
    assert not bucket.acquire(10.0).allowed  # a naive last = now would refill 5 tokens here
    assert bucket.acquire(11.0).allowed


@pytest.mark.unit
@pytest.mark.parametrize(
    "limiter",
    [
        FixedWindowCounter(limit=4, window=8),
        SlidingWindowLog(limit=4, window=8),
        SlidingWindowCounter(limit=4, window=8),
        TokenBucket(rate=1.0, capacity=4),
        LeakyBucketMeter(rate=1.0, capacity=4),
    ],
    ids=["fixed", "log", "counter", "bucket", "meter"],
)
def test_cost_above_capacity_can_never_be_admitted(limiter: Limiter) -> None:
    decision = limiter.acquire(0.0, cost=5)
    assert not decision.allowed
    assert math.isinf(decision.retry_after)
    assert limiter.acquire(0.0, cost=4).allowed


async def ok(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_returns_429_with_a_rounded_up_retry_after_per_client() -> None:
    now = [0.0]
    app = RateLimitMiddleware(
        Starlette(routes=[Route("/", ok)]),
        make_limiter=lambda: TokenBucket(rate=0.4, capacity=2),
        clock=lambda: now[0],
    )
    first = httpx.ASGITransport(app=app, client=("203.0.113.1", 1))
    second = httpx.ASGITransport(app=app, client=("203.0.113.2", 1))
    async with httpx.AsyncClient(transport=first, base_url="http://test") as client:
        statuses = [(await client.get("/")).status_code for _ in range(2)]
        limited = await client.get("/")
        now[0] = 3.0  # the client honored Retry-After: 0.4 tokens/s * 3 s = 1.2 tokens
        recovered = await client.get("/")
    async with httpx.AsyncClient(transport=second, base_url="http://test") as client:
        other = await client.get("/")
    assert statuses == [200, 200]
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "3"  # 2.5 s rounded up to delay-seconds
    assert limited.json() == {"detail": "rate limit exceeded", "retry_after": 3}
    assert recovered.status_code == 200
    assert other.status_code == 200  # a separate bucket per client address
