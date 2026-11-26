"""Two-phase TPM limiter labs: exact reserve/refund arithmetic, atomicity, and stream settlement."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

import anyio
import fakeredis
import httpx
import pytest
import redis.exceptions
from redis.crc import key_slot
from starlette.requests import ClientDisconnect

from api_labs.f21_ai_and_llm_api_engineering.token_aware_rate_limiting import (
    CompletionsApp,
    SettleStatus,
    TpmLimiter,
    TpmPolicy,
    UpstreamChunk,
    UpstreamError,
    Usage,
    Verdict,
    bucket_key,
    estimate_prompt_tokens,
    reservation_amount,
    reservation_key,
    scripted_upstream,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, MutableMapping

EPOCH_US = 1_763_164_800_000_000  # 2025-11-15T00:00:00Z
SECOND = 1_000_000
# 6000 tokens per minute is 100 tokens per second: 1000 tokens take exactly 10 s to refill.
POLICY = TpmPolicy(capacity=6000, refill_per_minute=6000)


class FakeClock:
    def __init__(self, now_us: int = EPOCH_US) -> None:
        self.now_us = now_us

    def __call__(self) -> int:
        return self.now_us


class NetworkRedis(fakeredis.FakeAsyncRedis):
    """fakeredis answers without suspending; a real server never does, so add the checkpoint."""

    down = False

    async def evalsha(self, sha: str, numkeys: int, *keys_and_args: Any) -> Any:
        if self.down:
            raise redis.exceptions.ConnectionError("store unreachable")
        await anyio.lowlevel.checkpoint()
        return await super().evalsha(sha, numkeys, *keys_and_args)


def make_client() -> NetworkRedis:
    return NetworkRedis(server=fakeredis.FakeServer())


async def micro(client: NetworkRedis, tenant: str) -> int | None:
    raw = cast("bytes | None", await client.hget(bucket_key(tenant), "micro"))
    return None if raw is None else int(raw)


@pytest.mark.unit
def test_estimator_rounds_up_by_utf8_bytes_and_reservation_counts_every_choice() -> None:
    assert [estimate_prompt_tokens(s) for s in ("", "abcd", "abcde", "\u00e9\u00e9")] == [0, 1, 2, 1]
    assert reservation_amount(100, 50, choices=3) == 250
    with pytest.raises(ValueError, match="required"):
        reservation_amount(0, 0)


@pytest.mark.unit
def test_cached_prompt_tokens_are_billed_at_zero() -> None:
    assert Usage(1000, 200, cached_prompt_tokens=900).billable == 300
    with pytest.raises(ValueError, match="cannot exceed"):
        Usage(10, 1, cached_prompt_tokens=11)


@pytest.mark.unit
def test_reserve_debits_prompt_plus_max_tokens_exactly() -> None:
    async def run() -> tuple[int, int | None]:
        client = make_client()
        limiter = TpmLimiter(client, POLICY, clock=FakeClock())
        admission = await limiter.reserve("acme", "r1", reservation_amount(500, 1000))
        assert admission.verdict is Verdict.ADMITTED
        return admission.balance, await micro(client, "acme")

    assert asyncio.run(run()) == (4500, 4500 * 1_000_000)


@pytest.mark.unit
def test_settle_refunds_the_unused_part_of_the_reservation() -> None:
    async def run() -> tuple[int, int, int | None]:
        client = make_client()
        limiter = TpmLimiter(client, POLICY, clock=FakeClock())
        admission = await limiter.reserve("acme", "r1", 1500)
        settled = await limiter.settle(admission, Usage(300, 400).billable)
        assert settled.status is SettleStatus.SETTLED
        return settled.delta, settled.balance, await micro(client, "acme")

    assert asyncio.run(run()) == (800, 5300, 5300 * 1_000_000)


@pytest.mark.unit
def test_settling_twice_refunds_once() -> None:
    async def run() -> tuple[SettleStatus, SettleStatus, int | None]:
        client = make_client()
        limiter = TpmLimiter(client, POLICY, clock=FakeClock())
        admission = await limiter.reserve("acme", "r1", 1500)
        first = await limiter.settle(admission, 100)
        second = await limiter.settle(admission, 100)
        return first.status, second.status, await micro(client, "acme")

    assert asyncio.run(run()) == (SettleStatus.SETTLED, SettleStatus.UNKNOWN, 5900 * 1_000_000)


@pytest.mark.unit
def test_replayed_reserve_with_same_request_id_debits_once() -> None:
    async def run() -> tuple[Verdict, int | None]:
        limiter_client = make_client()
        limiter = TpmLimiter(limiter_client, POLICY, clock=FakeClock())
        await limiter.reserve("acme", "r1", 1000)
        again = await limiter.reserve("acme", "r1", 1000)
        return again.verdict, await micro(limiter_client, "acme")

    assert asyncio.run(run()) == (Verdict.ADMITTED, 5000 * 1_000_000)


@pytest.mark.unit
def test_underestimated_prompt_goes_into_debt_with_exact_retry_after() -> None:
    async def run() -> list[object]:
        clock = FakeClock()
        limiter = TpmLimiter(make_client(), TpmPolicy(1000, 1000), clock=clock)
        admission = await limiter.reserve("acme", "r1", 600)
        settled = await limiter.settle(admission, 1200)
        denied = await limiter.reserve("acme", "r2", 100)
        # 100 needed, balance -200: deficit 300 tokens at 1000/min is exactly 18 s.
        clock.now_us += 18 * SECOND - 1
        early = await limiter.reserve("acme", "r3", 100)
        clock.now_us += 1
        on_time = await limiter.reserve("acme", "r4", 100)
        return [
            settled.delta,
            settled.balance,
            denied.verdict,
            denied.retry_after_us,
            early.verdict,
            on_time.verdict,
        ]

    assert asyncio.run(run()) == [
        -600,
        -200,
        Verdict.DENIED,
        18 * SECOND,
        Verdict.DENIED,
        Verdict.ADMITTED,
    ]


@pytest.mark.unit
def test_debt_is_floored_at_minus_capacity() -> None:
    async def run() -> int:
        limiter = TpmLimiter(make_client(), TpmPolicy(1000, 1000), clock=FakeClock())
        admission = await limiter.reserve("acme", "r1", 1000)
        return (await limiter.settle(admission, 5000)).balance

    assert asyncio.run(run()) == -1000


@pytest.mark.unit
def test_refund_cannot_push_the_bucket_above_capacity() -> None:
    async def run() -> int:
        clock = FakeClock()
        limiter = TpmLimiter(make_client(), POLICY, clock=clock)
        admission = await limiter.reserve("acme", "r1", 500)
        clock.now_us += 3600 * SECOND
        return (await limiter.settle(admission, 0)).balance

    assert asyncio.run(run()) == 6000


@pytest.mark.unit
def test_denied_reserve_leaves_bucket_and_reservations_untouched() -> None:
    async def run() -> tuple[Verdict, int | None, int]:
        client = make_client()
        limiter = TpmLimiter(client, POLICY, clock=FakeClock())
        await limiter.reserve("acme", "r1", 5000)
        before = await micro(client, "acme")
        denied = await limiter.reserve("acme", "r2", 2000)
        assert await micro(client, "acme") == before
        exists = await client.exists(reservation_key("acme", "r2"))
        return denied.verdict, before, exists

    assert asyncio.run(run()) == (Verdict.DENIED, 1000 * 1_000_000, 0)


@pytest.mark.unit
def test_request_larger_than_capacity_can_never_be_admitted() -> None:
    async def run() -> tuple[Verdict, int | None, int | None]:
        client = make_client()
        limiter = TpmLimiter(client, POLICY, clock=FakeClock())
        admission = await limiter.reserve("acme", "r1", 6001)
        return admission.verdict, admission.retry_after_us, await micro(client, "acme")

    assert asyncio.run(run()) == (Verdict.TOO_LARGE, None, None)


@pytest.mark.unit
def test_refill_is_exact_and_retry_after_is_the_exact_wait() -> None:
    async def run() -> list[object]:
        clock = FakeClock()
        limiter = TpmLimiter(make_client(), POLICY, clock=clock)
        await limiter.reserve("acme", "r0", 6000)
        clock.now_us += 10 * SECOND - 10_000
        short = await limiter.reserve("acme", "r1", 1000)
        clock.now_us += 10_000
        ok = await limiter.reserve("acme", "r2", 1000)
        return [short.verdict, short.retry_after_us, ok.verdict, ok.balance]

    assert asyncio.run(run()) == [Verdict.DENIED, 10_000, Verdict.ADMITTED, 0]


@pytest.mark.unit
def test_clock_stepping_backwards_mints_no_tokens() -> None:
    async def run() -> list[Verdict]:
        clock = FakeClock()
        limiter = TpmLimiter(make_client(), POLICY, clock=clock)
        await limiter.reserve("acme", "r0", 6000)
        clock.now_us -= 5 * SECOND
        stepped_back = await limiter.reserve("acme", "r1", 1)
        clock.now_us += 6 * SECOND  # one second past the original instant
        hundred = await limiter.reserve("acme", "r2", 100)
        one_more = await limiter.reserve("acme", "r3", 1)
        return [stepped_back.verdict, hundred.verdict, one_more.verdict]

    assert asyncio.run(run()) == [Verdict.DENIED, Verdict.ADMITTED, Verdict.DENIED]


@pytest.mark.unit
def test_reservation_that_expires_before_settle_stays_fully_charged() -> None:
    async def run() -> list[object]:
        clock = FakeClock()
        policy = TpmPolicy(6000, 6000, reservation_ttl_ms=1000)
        limiter = TpmLimiter(make_client(), policy, clock=clock)
        admission = await limiter.reserve("acme", "r1", 3000)
        clock.now_us += 2 * SECOND
        late = await limiter.settle(admission, 0)
        again = await limiter.settle(admission, 0)
        return [late.status, late.delta, late.balance, again.status]

    # 3000 left plus 2 s of refill at 100 tokens/s; no refund of the 3000 reserved.
    assert asyncio.run(run()) == [SettleStatus.EXPIRED, 0, 3200, SettleStatus.UNKNOWN]


@pytest.mark.unit
def test_tenants_have_independent_buckets() -> None:
    async def run() -> list[Verdict]:
        limiter = TpmLimiter(make_client(), POLICY, clock=FakeClock())
        await limiter.reserve("acme", "r1", 6000)
        other = await limiter.reserve("globex", "r1", 6000)
        again = await limiter.reserve("acme", "r2", 1)
        return [other.verdict, again.verdict]

    assert asyncio.run(run()) == [Verdict.ADMITTED, Verdict.DENIED]


@pytest.mark.unit
def test_bucket_and_reservation_keys_share_a_cluster_slot() -> None:
    assert key_slot(bucket_key("acme").encode()) == key_slot(reservation_key("acme", "r1").encode())
    assert key_slot(bucket_key("acme").encode()) != key_slot(bucket_key("globex").encode())


async def naive_reserve(client: NetworkRedis, key: str, amount: int, capacity: int) -> bool:
    """Anti-pattern: GET, decide in Python, SET. The read-modify-write spans two round trips."""
    raw = cast("bytes | None", await client.get(key))
    balance = capacity if raw is None else int(raw)
    await asyncio.sleep(0)
    if balance < amount:
        return False
    await client.set(key, balance - amount)
    return True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_reservations_never_exceed_capacity_but_get_set_does() -> None:
    capacity, amount, callers = 1000, 100, 20
    limiter = TpmLimiter(make_client(), TpmPolicy(capacity, capacity), clock=FakeClock())
    atomic = await asyncio.gather(*(limiter.reserve("a", f"r{i}", amount) for i in range(callers)))
    assert sum(a.admitted for a in atomic) == capacity // amount

    naive_client = make_client()
    naive = await asyncio.gather(
        *(naive_reserve(naive_client, "naive", amount, capacity) for _ in range(callers))
    )
    assert sum(naive) == callers  # every caller read 1000 before any wrote: 2000 tokens admitted


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unreachable_store_follows_fail_mode_and_settle_never_raises() -> None:
    client = make_client()
    closed = TpmLimiter(client, POLICY, clock=FakeClock())
    opened = TpmLimiter(client, POLICY, clock=FakeClock(), fail_open=True)
    admitted = await closed.reserve("acme", "r1", 100)
    client.down = True
    assert (await closed.reserve("acme", "r2", 100)).verdict is Verdict.DENIED
    bypass = await opened.reserve("acme", "r3", 100)
    assert bypass.verdict is Verdict.BYPASSED
    assert bypass.admitted
    assert (await closed.settle(admitted, 10)).status is SettleStatus.UNAVAILABLE
    client.down = False
    assert (await closed.settle(admitted, 10)).status is SettleStatus.SETTLED


def post(
    client: httpx.AsyncClient, body: dict[str, Any], request_id: str = "r1"
) -> Any:  # coroutine returning httpx.Response
    headers = {"x-tenant": "acme", "x-request-id": request_id}
    return client.post("/v1/completions", json=body, headers=headers)


def app_client(app: CompletionsApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_reconciles_to_provider_usage_with_cache_discount() -> None:
    client = make_client()
    limiter = TpmLimiter(client, TpmPolicy(10_000, 10_000), clock=FakeClock())
    # 400 bytes -> 100 estimated prompt tokens; reserve 100 + 500 = 600.
    upstream = scripted_upstream(50, prompt_tokens=90, cached_prompt_tokens=40)
    app = CompletionsApp(limiter, upstream)
    async with app_client(app) as http:
        response = await post(http, {"prompt": "x" * 400, "max_tokens": 500})
    assert response.status_code == 200
    assert response.headers["x-ratelimit-remaining-tokens"] == "9400"  # headers precede settle
    assert response.text.endswith("data: [DONE]\n\n")
    assert '"completion_tokens": 50' in response.text
    # billable = (90 - 40) + 50 = 100; refund = 600 - 100.
    assert [(s.status, s.delta, s.balance) for s in app.settlements] == [
        (SettleStatus.SETTLED, 500, 9900)
    ]
    assert await micro(client, "acme") == 9900 * 1_000_000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_n_choices_multiply_the_reservation() -> None:
    limiter = TpmLimiter(make_client(), TpmPolicy(10_000, 10_000), clock=FakeClock())
    app = CompletionsApp(limiter, scripted_upstream(5))
    async with app_client(app) as http:
        response = await post(http, {"prompt": "", "max_tokens": 100, "n": 3})
    assert response.headers["x-ratelimit-remaining-tokens"] == "9700"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_over_limit_request_gets_429_with_exact_retry_after_and_no_reservation() -> None:
    limiter = TpmLimiter(make_client(), TpmPolicy(1000, 1000), clock=FakeClock())
    app = CompletionsApp(limiter, scripted_upstream(600))
    async with app_client(app) as http:
        first = await post(http, {"prompt": "", "max_tokens": 600}, "r1")
        second = await post(http, {"prompt": "", "max_tokens": 600}, "r2")
    assert first.status_code == 200
    # 400 left, 600 needed: 200 tokens at 1000/min is 12 s.
    assert second.status_code == 429
    assert second.headers["retry-after"] == "12"
    assert len(app.settlements) == 1  # the denied request reserved nothing, so it settles nothing


@pytest.mark.unit
@pytest.mark.asyncio
async def test_never_admittable_or_invalid_max_tokens_is_400_and_touches_nothing() -> None:
    client = make_client()
    limiter = TpmLimiter(client, TpmPolicy(1000, 1000), clock=FakeClock())
    app = CompletionsApp(limiter, scripted_upstream(1))
    async with app_client(app) as http:
        too_big = await post(http, {"prompt": "", "max_tokens": 1001})
        invalid = await post(http, {"prompt": "", "max_tokens": 10**6})
    assert (too_big.status_code, invalid.status_code) == (400, 400)
    assert "retry-after" not in too_big.headers
    assert await micro(client, "acme") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_stream_still_settles_what_was_generated() -> None:
    client = make_client()
    limiter = TpmLimiter(client, TpmPolicy(10_000, 10_000), clock=FakeClock())

    async def stalls_after_three(_prompt: int, _max: int) -> AsyncIterator[UpstreamChunk]:
        for _ in range(3):
            yield UpstreamChunk(tokens=1)
        await anyio.sleep(30)  # the model stops answering; the request deadline fires first
        yield UpstreamChunk(tokens=1)  # pragma: no cover

    app = CompletionsApp(limiter, stalls_after_three)
    async with app_client(app) as http:
        with anyio.move_on_after(0.05) as scope:
            await post(http, {"prompt": "x" * 400, "max_tokens": 500})
    assert scope.cancelled_caught
    # No usage chunk arrived: charge = 100 estimated prompt + 3 observed tokens.
    assert [(s.status, s.delta, s.balance) for s in app.settlements] == [
        (SettleStatus.SETTLED, 497, 9897)
    ]
    assert await micro(client, "acme") == 9897 * 1_000_000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upstream_failure_mid_stream_settles_observed_tokens_and_propagates() -> None:
    client = make_client()
    limiter = TpmLimiter(client, TpmPolicy(10_000, 10_000), clock=FakeClock())
    app = CompletionsApp(limiter, scripted_upstream(10, fail_after=4))
    async with app_client(app) as http:
        with pytest.raises(UpstreamError):
            await post(http, {"prompt": "x" * 400, "max_tokens": 500})
    assert [(s.status, s.balance) for s in app.settlements] == [(SettleStatus.SETTLED, 9896)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_send_on_spec_2_4_still_settles_in_the_request_frame() -> None:
    client = make_client()
    limiter = TpmLimiter(client, TpmPolicy(10_000, 10_000), clock=FakeClock())
    app = CompletionsApp(limiter, scripted_upstream(10))
    sent: list[MutableMapping[str, Any]] = []
    body = json.dumps({"prompt": "x" * 400, "max_tokens": 500}).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        if len(sent) == 3:  # response start, two body frames, then the peer is gone
            raise OSError("connection closed")
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "POST",
        "path": "/v1/completions",
        "query_string": b"",
        "headers": [(b"x-tenant", b"acme"), (b"x-request-id", b"r1")],
    }
    with pytest.raises(ClientDisconnect):
        await app(scope, receive, send)
    # The third body frame was observed before its send failed: charge = 100 + 3.
    assert [(s.status, s.balance) for s in app.settlements] == [(SettleStatus.SETTLED, 9897)]
