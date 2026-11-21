"""GCRA labs: TM 4.0 equivalence, exact retry times, and atomic Lua limiting on fakeredis[lua]."""

from __future__ import annotations

import asyncio
import random
import threading

import fakeredis
import httpx
import pytest
from redis.crc import key_slot

from api_labs.f16_resilience_rate_limiting_and_idempotency.gcra import (
    US_PER_SECOND,
    GcraPolicy,
    NaiveGetSetGcraLimiter,
    RedisGcraLimiter,
    build_app,
    gcra_continuous_state,
    gcra_virtual_scheduling,
    retry_after_seconds,
)

# 10 requests per hour, burst 10: T = 360 s, so a test that runs for milliseconds sees no refill
# even on the real server clock.
HOURLY = GcraPolicy(rate=10, period_us=3600 * US_PER_SECOND, burst=10)
# 5 requests per second, burst 3: T = 200,000 us, tau = 400,000 us.
FAST = GcraPolicy(rate=5, period_us=US_PER_SECOND, burst=3)
EPOCH_US = 1_763_164_800_000_000  # 2025-11-15T00:00:00Z


class FakeClock:
    def __init__(self, now_us: int = EPOCH_US) -> None:
        self.now_us = now_us

    def __call__(self) -> int:
        return self.now_us


@pytest.mark.unit
def test_virtual_scheduling_matches_continuous_state_leaky_bucket() -> None:
    policy = GcraPolicy(rate=7, period_us=US_PER_SECOND, burst=4)
    rng = random.Random(1996)  # noqa: S311 - reproducible arrivals, not crypto
    tat: int | None = None
    bucket, lct = 0, None
    now = EPOCH_US
    verdicts: list[bool] = []
    for _ in range(5_000):
        now += rng.randrange(0, 2 * policy.emission_interval_us)
        decision, new_tat = gcra_virtual_scheduling(tat, now, policy)
        conforming, bucket, lct = gcra_continuous_state(bucket, lct, now, policy)
        assert decision.allowed is conforming
        tat = new_tat if new_tat is not None else tat
        assert lct is not None
        assert tat == bucket + lct  # TM 4.0 Section 4.4.2: TAT = X + LCT after each arrival
        verdicts.append(conforming)
    assert 0 < verdicts.count(False) < len(verdicts)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("spacing_us", "expected_burst"),
    [(0, 6), (10, 6), (40, 9), (60, 13), (75, 21), (99, 501)],
)
def test_burst_at_peak_spacing_matches_tm40_mbs_formula(
    spacing_us: int, expected_burst: int
) -> None:
    policy = GcraPolicy(rate=1, period_us=100, burst=6)  # T = 100 us, tau = 500 us
    t, tau = policy.emission_interval_us, policy.tolerance_us
    assert expected_burst == 1 + tau // (t - spacing_us)  # TM 4.0 Annex C.4
    tat: int | None = None
    admitted = 0
    for k in range(1_000):
        decision, new_tat = gcra_virtual_scheduling(tat, EPOCH_US + k * spacing_us, policy)
        if not decision.allowed:
            break
        admitted, tat = admitted + 1, new_tat
    assert admitted == expected_burst


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_after_is_exact_to_the_microsecond() -> None:
    clock = FakeClock()
    limiter = RedisGcraLimiter(fakeredis.FakeAsyncRedis(), clock=clock)
    for expected_remaining in (2, 1, 0):
        decision = await limiter.acquire("k", FAST)
        assert (decision.allowed, decision.remaining) == (True, expected_remaining)
    denied = await limiter.acquire("k", FAST)
    assert (denied.allowed, denied.retry_after_us, denied.reset_after_us) == (
        False,
        200_000,
        600_000,
    )
    clock.now_us += 199_999
    assert not (await limiter.acquire("k", FAST)).allowed
    clock.now_us += 1
    assert (await limiter.acquire("k", FAST)).allowed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lua_script_matches_python_reference_on_random_arrivals() -> None:
    clock = FakeClock()
    limiter = RedisGcraLimiter(fakeredis.FakeAsyncRedis(), clock=clock)
    rng = random.Random(4096)  # noqa: S311 - reproducible arrivals, not crypto
    tat: int | None = None
    outcomes: set[bool] = set()
    for _ in range(400):
        clock.now_us += rng.randrange(0, 300_000)
        cost = rng.choice((1, 1, 1, 2, 3))
        expected, new_tat = gcra_virtual_scheduling(tat, clock.now_us, FAST, cost)
        tat = new_tat if new_tat is not None else tat
        assert await limiter.acquire("k", FAST, cost) == expected
        outcomes.add(expected.allowed)
    assert outcomes == {True, False}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fifty_concurrent_coroutines_admit_exactly_burst_with_one_key() -> None:
    client = fakeredis.FakeAsyncRedis()
    limiter = RedisGcraLimiter(client)  # server clock: TIME inside the script
    decisions = await asyncio.gather(*(limiter.acquire("gcra:{t1}:api", HOURLY) for _ in range(50)))
    allowed = [d for d in decisions if d.allowed]
    assert len(allowed) == HOURLY.burst
    assert sorted(d.remaining for d in allowed) == list(range(HOURLY.burst))
    assert await client.eval("return redis.call('DBSIZE')", 0) == 1
    stored = await client.get("gcra:{t1}:api")
    assert stored is not None
    assert stored.isdigit()
    assert len(stored) == 16  # one integer TAT in microseconds is the whole state


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naive_get_then_set_admits_every_concurrent_request() -> None:
    limiter = NaiveGetSetGcraLimiter(fakeredis.FakeAsyncRedis(), clock=FakeClock())
    decisions = await asyncio.gather(*(limiter.acquire("k", HOURLY) for _ in range(50)))
    assert sum(d.allowed for d in decisions) == 50  # every reader saw the same empty key


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_server_clock_skew_over_admits_but_server_time_does_not() -> None:
    client = fakeredis.FakeAsyncRedis()
    honest = FakeClock()
    fast = FakeClock(EPOCH_US + 3600 * US_PER_SECOND)  # one app server's clock is an hour ahead
    a, b = RedisGcraLimiter(client, clock=honest), RedisGcraLimiter(client, clock=fast)
    first = [await a.acquire("skew", HOURLY) for _ in range(HOURLY.burst)]
    second = [await b.acquire("skew", HOURLY) for _ in range(HOURLY.burst)]
    assert all(d.allowed for d in first + second)  # 2x burst admitted for one caller

    server_a, server_b = RedisGcraLimiter(client), RedisGcraLimiter(client)
    first = [await server_a.acquire("server", HOURLY) for _ in range(HOURLY.burst)]
    second = [await server_b.acquire("server", HOURLY) for _ in range(HOURLY.burst)]
    assert all(d.allowed for d in first)
    assert not any(d.allowed for d in second)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cost_larger_than_burst_never_conforms_and_leaves_state() -> None:
    client = fakeredis.FakeAsyncRedis()
    limiter = RedisGcraLimiter(client, clock=FakeClock())
    decision = await limiter.acquire("big", FAST, cost=FAST.burst + 1)
    assert (decision.allowed, decision.retry_after_us, decision.remaining) == (False, None, 3)
    assert await client.exists("big") == 0
    assert (await limiter.acquire("big", FAST, cost=FAST.burst)).allowed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dual_gcra_is_all_or_nothing() -> None:
    client = fakeredis.FakeAsyncRedis()
    clock = FakeClock()
    limiter = RedisGcraLimiter(client, clock=clock)
    sustained = GcraPolicy(rate=100, period_us=3600 * US_PER_SECOND, burst=20)
    peak = GcraPolicy(rate=2, period_us=US_PER_SECOND, burst=1)  # GCRA(T, 0) peak-rate check
    keys = [("gcra:{t1}:sustained", sustained), ("gcra:{t1}:peak", peak)]
    assert (await limiter.acquire_all(keys)).allowed
    before = await client.get("gcra:{t1}:sustained")
    denied = await limiter.acquire_all(keys)
    assert (denied.allowed, denied.retry_after_us) == (False, 500_000)
    assert await client.get("gcra:{t1}:sustained") == before  # sustained budget not spent
    clock.now_us += 500_000
    assert (await limiter.acquire_all(keys)).allowed


@pytest.mark.unit
def test_hash_tags_put_both_policy_keys_in_one_cluster_slot() -> None:
    tagged = [b"gcra:{t1}:sustained", b"gcra:{t1}:peak"]
    untagged = [b"gcra:t1:sustained", b"gcra:t1:peak"]
    assert key_slot(tagged[0]) == key_slot(tagged[1]) == key_slot(b"t1")
    assert key_slot(untagged[0]) != key_slot(untagged[1])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_script_flush_is_recovered_by_the_evalsha_fallback() -> None:
    client = fakeredis.FakeAsyncRedis()
    limiter = RedisGcraLimiter(client, clock=FakeClock())
    assert (await limiter.acquire("k", FAST)).allowed
    await client.script_flush()
    decision = await limiter.acquire("k", FAST)
    assert (decision.allowed, decision.remaining) == (True, 1)  # state kept, script reloaded
    assert await client.script_exists(limiter.script_sha) == [True]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_number_arguments_stay_exact_but_tostring_keeps_14_digits() -> None:
    client = fakeredis.FakeAsyncRedis()
    tat = "1763164800123457"
    await client.eval("redis.call('SET', KEYS[1], tonumber(ARGV[1]))", 1, "n", tat)
    await client.eval("redis.call('SET', KEYS[1], tostring(tonumber(ARGV[1])))", 1, "s", tat)
    assert await client.get("n") == b"1763164800123457"
    assert await client.get("s") == b"1.7631648001235e+15"  # Lua 5.1 LUA_NUMBER_FMT "%.14g"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lua_number_replies_are_truncated_to_integers() -> None:
    client = fakeredis.FakeAsyncRedis()
    assert await client.eval("return {2.9, -2.9, '2.9'}", 0) == [2, -2, b"2.9"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_key_ttl_equals_reset_after_rounded_up_to_milliseconds() -> None:
    client = fakeredis.FakeAsyncRedis()
    limiter = RedisGcraLimiter(client, clock=FakeClock(EPOCH_US + 1))
    decision = await limiter.acquire("ttl", FAST)
    assert decision.reset_after_us == 200_000
    pttl = await client.pttl("ttl")
    assert 190 < pttl <= 200  # PX ceil(200000 / 1000) = 200 ms, minus elapsed real time


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unreachable_store_fails_closed_by_default_and_open_on_request() -> None:
    down = fakeredis.FakeAsyncRedis(connected=False)
    closed = await RedisGcraLimiter(down).acquire("k", FAST)
    opened = await RedisGcraLimiter(down, fail_open=True).acquire("k", FAST)
    assert (closed.allowed, closed.degraded, closed.retry_after_us) == (False, True, None)
    assert (opened.allowed, opened.degraded) == (True, True)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("retry_after_us", "header"),
    [(1, 1), (999_999, 1), (1_000_000, 1), (1_000_001, 2), (359_999_999, 360)],
)
def test_retry_after_header_rounds_up_and_never_reaches_zero(
    retry_after_us: int, header: int
) -> None:
    assert retry_after_seconds(retry_after_us) == header


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_app_returns_429_with_retry_after_then_503_when_store_down() -> None:
    clock = FakeClock()
    app = build_app(RedisGcraLimiter(fakeredis.FakeAsyncRedis(), clock=clock), FAST)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        codes = [
            (await http.get("/quota", headers={"x-client-id": "c1"})).status_code for _ in range(4)
        ]
        assert codes == [200, 200, 200, 429]
        limited = await http.get("/quota", headers={"x-client-id": "c1"})
        assert limited.headers["retry-after"] == "1"  # 200,000 us rounds up to 1 s
        assert (await http.get("/quota", headers={"x-client-id": "c2"})).status_code == 200
    down_app = build_app(RedisGcraLimiter(fakeredis.FakeAsyncRedis(connected=False)), FAST)
    down_transport = httpx.ASGITransport(app=down_app)
    async with httpx.AsyncClient(transport=down_transport, base_url="http://test") as http:
        response = await http.get("/quota")
        assert response.status_code == 503
        assert "retry-after" not in response.headers


@pytest.mark.unit
def test_threads_sharing_one_server_admit_exactly_burst() -> None:
    server = fakeredis.FakeServer()
    admitted: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        async def run() -> int:
            limiter = RedisGcraLimiter(fakeredis.FakeAsyncRedis(server=server))
            decisions = [await limiter.acquire("shared", HOURLY) for _ in range(25)]
            return sum(d.allowed for d in decisions)

        count = asyncio.run(run())
        with lock:
            admitted.append(count)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(admitted) == 4
    assert sum(admitted) == HOURLY.burst


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rate", "period_us", "burst"),
    [(0, 1, 1), (1, 0, 1), (1, 1, 0), (1_000_001, US_PER_SECOND, 1)],
)
def test_policy_rejects_degenerate_parameters(rate: int, period_us: int, burst: int) -> None:
    with pytest.raises(ValueError, match=r">= 1|rounds to 0"):
        GcraPolicy(rate=rate, period_us=period_us, burst=burst)
