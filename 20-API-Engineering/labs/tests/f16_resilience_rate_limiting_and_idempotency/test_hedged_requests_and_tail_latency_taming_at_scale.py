"""Lab: hedged requests with a p95 delay, a load budget, and cancellation of the losing leg."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import anyio
import httpx
import pytest

from api_labs.f16_resilience_rate_limiting_and_idempotency.hedging import (
    HedgeBudget,
    HedgedCaller,
    LatencyTracker,
    fanout_slow_probability,
    nearest_rank,
    rst_stream_cancel_frame,
    simulate_hedging,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

HEDGE_DELAY_S = 0.01
LATE_S = HEDGE_DELAY_S * 5  # a primary that answers after the hedge has been sent


def make_caller(*, budget_ratio: float = 0.5, prefill: int = 2) -> HedgedCaller:
    """A warm caller whose hedge delay is pinned to HEDGE_DELAY_S and whose budget has tokens."""
    tracker = LatencyTracker(min_samples=1, floor_s=HEDGE_DELAY_S, ceiling_s=HEDGE_DELAY_S)
    tracker.record(HEDGE_DELAY_S)
    budget = HedgeBudget(budget_ratio)
    for _ in range(prefill):
        budget.deposit()
    return HedgedCaller(tracker, budget)


class Leg:
    """A fake replica leg: optionally sleeps (forever if delay is inf), records cancellation."""

    def __init__(self, name: str, *, delay: float = 0.0, error: Exception | None = None) -> None:
        self.name = name
        self.delay = delay
        self.error = error
        self.calls = 0
        self.cancelled = False

    async def __call__(self) -> str:
        self.calls += 1
        try:
            await anyio.sleep(self.delay)
        except anyio.get_cancelled_exc_class():
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error
        return self.name


@pytest.mark.unit
def test_tracker_withholds_delay_until_warm_then_uses_nearest_rank_p95() -> None:
    tracker = LatencyTracker(min_samples=100, floor_s=0.001, ceiling_s=1.0)
    for ms in range(1, 100):
        tracker.record(ms / 1000)
    assert tracker.hedge_delay() is None  # 99 samples: cold, never hedge on a guess
    tracker.record(0.1)
    assert tracker.hedge_delay() == pytest.approx(0.095)  # 95th of 1..100 ms
    assert nearest_rank([5.0, 1.0, 3.0, 2.0, 4.0], 50) == 3.0


@pytest.mark.unit
def test_tracker_clamps_delay_like_cassandra_min_max_hybrid() -> None:
    tracker = LatencyTracker(min_samples=1, floor_s=0.002, ceiling_s=0.05)
    tracker.record(0.0)
    assert tracker.hedge_delay() == 0.002  # a ~0 ms p95 must not hedge instantly
    for _ in range(100):
        tracker.record(5.0)  # one dead replica drags p95 up
    assert tracker.hedge_delay() == 0.05


@pytest.mark.unit
def test_budget_caps_backups_at_ratio_even_when_every_primary_is_slow() -> None:
    budget = HedgeBudget(0.05, max_tokens=10)
    sent = 0
    for _ in range(1000):
        budget.deposit()
        sent += budget.try_withdraw()
    assert sent == 50
    idle = HedgeBudget(0.05, max_tokens=10)
    for _ in range(10_000):
        idle.deposit()
    assert idle.tokens == 10.0  # the stored burst is bounded


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fast_primary_sends_no_backup() -> None:
    caller = make_caller()
    primary, backup = Leg("zone-a"), Leg("zone-b")
    outcome = await caller.call(primary, backup, method="GET")
    assert (outcome.value, outcome.winner, outcome.legs_sent) == ("zone-a", 0, 1)
    assert backup.calls == 0
    assert caller.budget.tokens == 1.5  # deposited, nothing spent


@pytest.mark.unit
@pytest.mark.asyncio
async def test_slow_primary_is_hedged_and_losing_leg_is_cancelled() -> None:
    caller = make_caller()
    primary = Leg("zone-a", delay=math.inf)  # never released
    backup = Leg("zone-b")
    with anyio.fail_after(2):
        outcome = await caller.call(primary, backup, method="GET")
    assert (outcome.value, outcome.winner, outcome.legs_sent) == ("zone-b", 1, 2)
    assert primary.cancelled
    assert caller.budget.tokens == 0.5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exhausted_budget_sends_no_backup_and_waits_for_primary() -> None:
    caller = make_caller(prefill=0)  # 0.5 tokens after the call's own deposit
    primary, backup = Leg("zone-a", delay=LATE_S), Leg("zone-b")
    outcome = await caller.call(primary, backup, method="GET")
    assert (outcome.value, outcome.legs_sent) == ("zone-a", 1)
    assert backup.calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_is_hedged_only_with_an_idempotency_key() -> None:
    caller = make_caller(prefill=4)
    primary, backup = Leg("zone-a", delay=LATE_S), Leg("zone-b")
    plain = await caller.call(primary, backup, method="POST")
    assert (plain.value, plain.legs_sent, backup.calls) == ("zone-a", 1, 0)

    slow = Leg("zone-a", delay=math.inf)
    keyed = await caller.call(slow, backup, method="POST", idempotency_key="order-42")
    assert (keyed.value, keyed.legs_sent, backup.calls) == ("zone-b", 2, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_backup_falls_back_to_a_later_primary_success() -> None:
    caller = make_caller()
    primary = Leg("zone-a", delay=LATE_S)
    backup = Leg("zone-b", error=ConnectionError("zone-b reset"))
    outcome = await caller.call(primary, backup, method="GET")
    assert (outcome.value, outcome.winner, outcome.legs_sent) == ("zone-a", 0, 2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_both_legs_failing_raises_the_first_failure() -> None:
    caller = make_caller()
    primary = Leg("zone-a", delay=LATE_S, error=TimeoutError("zone-a"))
    backup = Leg("zone-b", error=ConnectionError("zone-b"))
    with pytest.raises(ConnectionError, match="zone-b"):
        await caller.call(primary, backup, method="GET")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_winner_is_returned_only_after_the_loser_has_unwound() -> None:
    caller = make_caller()
    cleanup_done = False

    async def stubborn_primary() -> str:
        nonlocal cleanup_done
        try:
            await anyio.sleep_forever()
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.sleep(HEDGE_DELAY_S * 3)  # e.g. returning a connection to a pool
            cleanup_done = True
        return "unreachable"

    started = anyio.current_time()
    outcome = await caller.call(stubborn_primary, Leg("zone-b"), method="GET")
    assert outcome.value == "zone-b"
    assert cleanup_done
    assert anyio.current_time() - started >= HEDGE_DELAY_S * 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_caller_deadline_cancels_both_legs() -> None:
    caller = make_caller()
    primary = Leg("zone-a", delay=math.inf)
    backup = Leg("zone-b", delay=math.inf)
    with anyio.move_on_after(HEDGE_DELAY_S * 5) as scope:
        await caller.call(primary, backup, method="GET")
    assert scope.cancelled_caught
    assert primary.cancelled
    assert backup.cancelled


def replica(zone: str, gate: anyio.Event | None, log: list[str]) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["type"] == "http"
        log.append(f"{zone}:start {scope['method']} {scope['path']}")
        try:
            if gate is not None:
                await gate.wait()
        except anyio.get_cancelled_exc_class():
            log.append(f"{zone}:cancelled")
            raise
        body = zone.encode()
        headers = [(b"x-zone", body), (b"content-length", str(len(body)).encode())]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    return app


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_cross_zone_hedge_cancels_the_slow_replica_handler() -> None:
    log: list[str] = []
    zone_a = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=replica("zone-a", anyio.Event(), log)),
        base_url="http://test",
    )
    zone_b = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=replica("zone-b", None, log)),
        base_url="http://test",
    )
    caller = make_caller()
    async with zone_a, zone_b:

        async def primary() -> httpx.Response:
            return await zone_a.get("/items/7")

        async def backup() -> httpx.Response:
            return await zone_b.get("/items/7")

        with anyio.fail_after(2):
            outcome = await caller.call(primary, backup, method="GET")
    assert outcome.value.status_code == 200
    assert outcome.value.headers["x-zone"] == "zone-b"
    assert log == [
        "zone-a:start GET /items/7",
        "zone-b:start GET /items/7",
        "zone-a:cancelled",
    ]


@pytest.mark.unit
def test_http2_loser_cancellation_is_a_13_byte_rst_stream_cancel() -> None:
    frame = rst_stream_cancel_frame(stream_id=1)
    # length=4, type=0x03 RST_STREAM, flags=0, stream=1, error=0x8 CANCEL (RFC 9113 Section 7)
    assert frame == bytes.fromhex("00000403000000000100000008")
    assert rst_stream_cancel_frame(stream_id=5)[5:9] == b"\x00\x00\x00\x05"


@pytest.mark.unit
def test_fanout_amplification_matches_the_tail_at_scale_figures() -> None:
    assert fanout_slow_probability(0.01, 1) == pytest.approx(0.01)
    assert round(fanout_slow_probability(0.01, 100), 3) == 0.634  # "63% of user requests"
    assert round(fanout_slow_probability(0.0001, 2000), 3) == 0.181  # "almost one in five"


@pytest.mark.unit
def test_hedging_at_p95_cuts_p999_with_about_5_percent_extra_load() -> None:
    plain = simulate_hedging(100_000, seed=7, hedge_delay_ms=None)
    hedged = simulate_hedging(100_000, seed=7, hedge_delay_ms=11.0)  # leg p95 is ~11 ms
    assert plain.p999_ms > 200
    assert hedged.p999_ms < 25
    assert 0.04 < hedged.extra_load < 0.06
    assert hedged.p50_ms == plain.p50_ms
    cross_zone = simulate_hedging(100_000, seed=7, hedge_delay_ms=11.0, cross_zone_rtt_ms=2.0)
    assert hedged.p999_ms < cross_zone.p999_ms < 30


@pytest.mark.unit
def test_correlated_stragglers_defeat_hedging_but_still_cost_load() -> None:
    shared = simulate_hedging(100_000, seed=7, hedge_delay_ms=11.0, correlation=1.0)
    assert shared.p999_ms > 200
    assert shared.extra_load > 0.04


@pytest.mark.unit
def test_fixed_delay_under_global_slowdown_doubles_load_unless_budgeted() -> None:
    unbudgeted = simulate_hedging(100_000, seed=7, hedge_delay_ms=11.0, slowdown=3.0)
    budgeted = simulate_hedging(
        100_000, seed=7, hedge_delay_ms=11.0, slowdown=3.0, budget_ratio=0.10
    )
    adaptive = simulate_hedging(100_000, seed=7, hedge_delay_ms=33.0, slowdown=3.0)
    assert unbudgeted.extra_load == 1.0
    assert budgeted.extra_load == pytest.approx(0.10, abs=0.001)
    assert adaptive.extra_load < 0.06


@pytest.mark.unit
def test_budget_without_headroom_over_the_hedge_rate_lets_the_tail_back() -> None:
    tight = simulate_hedging(100_000, seed=7, hedge_delay_ms=11.0, budget_ratio=0.05)
    roomy = simulate_hedging(100_000, seed=7, hedge_delay_ms=11.0, budget_ratio=0.10)
    assert roomy.p999_ms < tight.p999_ms
    assert tight.extra_load < roomy.extra_load
