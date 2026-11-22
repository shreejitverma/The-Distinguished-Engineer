"""Lab: BFF fan-out with AnyIO task groups, hedging, deadlines, and partial degradation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import anyio.lowlevel
import httpx
import pytest

from api_labs.f17_api_gateways_proxies_and_edge_architecture.bff_aggregation import (
    AggregationFailed,
    Call,
    JsonObject,
    Outcome,
    Policy,
    build_app,
    fan_out,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


def ok(payload: JsonObject) -> Call:
    async def call(_attempt: int) -> JsonObject:
        return payload

    return call


def hang(trace: list[str], name: str) -> Call:
    """A call that never answers; records its start and its cancellation."""

    async def call(_attempt: int) -> JsonObject:
        trace.append(f"{name}:start")
        try:
            await anyio.sleep_forever()
        finally:
            trace.append(f"{name}:cancelled")

    return call


def boom(exc: Exception) -> Call:
    async def call(_attempt: int) -> JsonObject:
        raise exc

    return call


def optional(deadline_s: float, **kwargs: float) -> Policy:
    return Policy(deadline_s=deadline_s, fallback={"fallback": True}, **kwargs)


async def get_home(
    calls: Mapping[str, Call],
    policies: Mapping[str, Policy] | None = None,
    total_deadline_s: float = 0.6,
) -> httpx.Response:
    app = build_app(calls, policies, total_deadline_s=total_deadline_s)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/home")


# Concurrency ----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fan_out_starts_every_upstream_before_any_finishes() -> None:
    all_started = anyio.Event()
    started: list[str] = []

    def gated(name: str) -> Call:
        async def call(_attempt: int) -> JsonObject:
            started.append(name)
            if len(started) == 3:
                all_started.set()
            await all_started.wait()  # a sequential fan-out would wait here until its deadline
            return {"name": name}

        return call

    names = ("orders", "inventory", "recommendations")
    policies = {
        "orders": Policy(deadline_s=1.0, critical=True),
        "inventory": optional(1.0),
        "recommendations": optional(1.0),
    }
    with anyio.fail_after(3):
        result = await fan_out({n: gated(n) for n in names}, policies, total_deadline_s=2.0)
    assert result.degraded == []
    assert result.data["orders"] == {"name": "orders"}
    assert sorted(started) == sorted(names)


# Partial degradation --------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_slow_optional_upstream_returns_200_with_fallback_and_no_store() -> None:
    trace: list[str] = []
    calls = {
        "orders": ok({"id": 7}),
        "inventory": ok({"stock": 3}),
        "recommendations": hang(trace, "recs"),
    }
    response = await get_home(calls)  # default policies: recommendations deadline is 80 ms
    assert response.status_code == 200
    body = response.json()
    assert body["orders"] == {"id": 7}
    assert body["inventory"] == {"stock": 3}
    assert body["recommendations"] == {"items": [], "source": "fallback"}
    assert body["_meta"]["degraded"] == ["recommendations"]
    assert body["_meta"]["outcomes"]["recommendations"] == {"status": "timeout", "attempts": 1}
    assert response.headers["X-BFF-Degraded"] == "recommendations"
    assert response.headers["Cache-Control"] == "no-store"
    assert trace == ["recs:start", "recs:cancelled"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_healthy_response_carries_no_degraded_marker_and_no_cache_directive() -> None:
    calls = {
        "orders": ok({"id": 7}),
        "inventory": ok({"stock": 3}),
        "recommendations": ok({"items": ["a"]}),
    }
    response = await get_home(calls)
    assert response.status_code == 200
    assert response.json()["_meta"]["degraded"] == []
    assert "X-BFF-Degraded" not in response.headers
    assert "Cache-Control" not in response.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_optional_error_degrades_without_cancelling_the_siblings() -> None:
    recs_failed = anyio.Event()

    async def recommendations(_attempt: int) -> JsonObject:
        recs_failed.set()
        raise RuntimeError("recommendations exploded")

    async def orders(_attempt: int) -> JsonObject:
        await recs_failed.wait()
        for _ in range(20):  # let any sibling cancellation be delivered at a checkpoint
            await anyio.lowlevel.checkpoint()
        return {"id": 7}

    policies = {"orders": Policy(deadline_s=1.0, critical=True), "recs": optional(1.0)}
    result = await fan_out(
        {"orders": orders, "recs": recommendations}, policies, total_deadline_s=2.0
    )
    assert result.data["orders"] == {"id": 7}
    assert result.data["recs"] == {"fallback": True}
    assert result.outcomes["recs"] == Outcome("recs", "error", 1, "RuntimeError")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_call_deadline_marks_timeout_with_deadline_detail() -> None:
    trace: list[str] = []
    policies = {"orders": Policy(deadline_s=1.0, critical=True), "recs": optional(0.02)}
    result = await fan_out(
        {"orders": ok({"id": 1}), "recs": hang(trace, "recs")}, policies, total_deadline_s=2.0
    )
    assert result.outcomes["recs"] == Outcome("recs", "timeout", 1, "deadline")
    assert result.degraded == ["recs"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_overall_budget_caps_total_time_below_the_per_call_deadlines() -> None:
    trace: list[str] = []
    policies = {"orders": Policy(deadline_s=5.0, critical=True), "inventory": optional(5.0)}
    start = anyio.current_time()
    result = await fan_out(
        {"orders": ok({"id": 1}), "inventory": hang(trace, "inv")},
        policies,
        total_deadline_s=0.05,
    )
    assert anyio.current_time() - start < 2.0
    assert result.outcomes["inventory"] == Outcome("inventory", "timeout", 1, "budget")
    assert result.data["inventory"] == {"fallback": True}
    assert result.outcomes["orders"].status == "ok"
    assert trace == ["inv:start", "inv:cancelled"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_is_copied_so_mutating_one_page_cannot_corrupt_the_next() -> None:
    shared: JsonObject = {"items": []}
    policies = {
        "orders": Policy(deadline_s=1.0, critical=True),
        "recs": Policy(0.01, fallback=shared),
    }
    calls = {"orders": ok({"id": 1}), "recs": hang([], "recs")}
    first = await fan_out(calls, policies, total_deadline_s=1.0)
    items = first.data["recs"]["items"]
    assert isinstance(items, list)
    items.append("poison")
    second = await fan_out(calls, policies, total_deadline_s=1.0)
    assert second.data["recs"] == {"items": []}
    assert shared == {"items": []}
    assert first.data["recs"] is not shared


# Critical failures ----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_critical_failure_cancels_the_slow_sibling_instead_of_waiting_for_its_deadline() -> (
    None
):
    trace: list[str] = []
    recs_started = anyio.Event()
    recs = hang(trace, "recs")

    async def orders(_attempt: int) -> JsonObject:
        await recs_started.wait()
        raise RuntimeError("orders down")

    async def signalling_recs(attempt: int) -> JsonObject:
        recs_started.set()
        return await recs(attempt)

    policies = {"orders": Policy(deadline_s=5.0, critical=True), "recs": optional(5.0)}
    start = anyio.current_time()
    with pytest.raises(AggregationFailed) as info:
        await fan_out({"orders": orders, "recs": signalling_recs}, policies, total_deadline_s=10.0)
    assert anyio.current_time() - start < 2.0
    assert trace == ["recs:start", "recs:cancelled"]
    assert info.value.failures == (Outcome("orders", "error", 1, "RuntimeError"),)
    assert info.value.status_code == 502


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_simultaneous_critical_failures_are_both_collected_by_except_star() -> None:
    go = anyio.Event()

    def failing(exc: Exception) -> Call:
        async def call(_attempt: int) -> JsonObject:
            await go.wait()
            raise exc

        return call

    async def releaser(_attempt: int) -> JsonObject:
        go.set()
        return {"released": True}

    policies = {
        "a": Policy(deadline_s=1.0, critical=True),
        "b": Policy(deadline_s=1.0, critical=True),
        "c": optional(1.0),
    }
    calls = {"a": failing(ValueError("a")), "b": failing(KeyError("b")), "c": releaser}
    with pytest.raises(AggregationFailed) as info:
        await fan_out(calls, policies, total_deadline_s=2.0)
    assert [(f.name, f.detail) for f in info.value.failures] == [
        ("a", "ValueError"),
        ("b", "KeyError"),
    ]


@pytest.mark.unit
def test_status_code_is_504_when_any_failure_is_a_timeout_else_502() -> None:
    error = Outcome("a", "error", 1, "X")
    timeout = Outcome("b", "timeout", 1, "deadline")
    assert AggregationFailed((error,)).status_code == 502
    assert AggregationFailed((error, timeout)).status_code == 504


@pytest.mark.unit
@pytest.mark.asyncio
async def test_critical_error_maps_to_502_problem_details_without_leaking_exception_text() -> None:
    calls = {
        "orders": boom(RuntimeError("db password=hunter2 at 10.0.0.5")),
        "inventory": ok({"stock": 1}),
        "recommendations": ok({"items": []}),
    }
    response = await get_home(calls)
    assert response.status_code == 502
    assert response.headers["Content-Type"].startswith("application/problem+json")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "type": "about:blank",
        "title": "Bad Gateway",
        "status": 502,
        "failed": [{"name": "orders", "status": "error"}],
    }
    assert "hunter2" not in response.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_critical_timeout_maps_to_504() -> None:
    policies = {"orders": Policy(deadline_s=0.02, critical=True), "recs": optional(0.5)}
    calls = {"orders": hang([], "orders"), "recs": ok({"items": []})}
    response = await get_home(calls, policies)
    assert response.status_code == 504
    assert response.json()["title"] == "Gateway Timeout"
    assert response.json()["failed"] == [{"name": "orders", "status": "timeout"}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_outer_cancellation_cancels_every_child_before_returning() -> None:
    trace: list[str] = []
    policies = {
        "orders": Policy(deadline_s=30.0, critical=True),
        "inventory": optional(30.0),
        "recs": optional(30.0),
    }
    calls = {n: hang(trace, n) for n in policies}
    with anyio.move_on_after(0.05) as outer:
        await fan_out(calls, policies, total_deadline_s=60.0)
    assert outer.cancelled_caught
    assert sorted(trace) == sorted(
        [f"{n}:start" for n in policies] + [f"{n}:cancelled" for n in policies]
    )


# Hedging --------------------------------------------------------------------


def hang_then_ok(trace: list[str]) -> Call:
    async def call(attempt: int) -> JsonObject:
        trace.append(f"a{attempt}:start")
        if attempt == 0:
            try:
                await anyio.sleep_forever()
            finally:
                trace.append("a0:cancelled")
        return {"from": f"attempt-{attempt}"}

    return call


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hedge_launches_second_attempt_after_delay_and_cancels_the_loser() -> None:
    trace: list[str] = []
    policies = {"recs": optional(5.0, hedge_after_s=0.01)}
    result = await fan_out({"recs": hang_then_ok(trace)}, policies, total_deadline_s=10.0)
    assert result.data["recs"] == {"from": "attempt-1"}
    assert result.outcomes["recs"] == Outcome("recs", "ok", 2, "")
    assert trace == ["a0:start", "a1:start", "a0:cancelled"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hedge_is_not_launched_when_the_first_attempt_answers_in_time() -> None:
    seen: list[int] = []

    async def call(attempt: int) -> JsonObject:
        seen.append(attempt)
        return {"from": "attempt-0"}

    policies = {"recs": optional(5.0, hedge_after_s=1.0)}
    result = await fan_out({"recs": call}, policies, total_deadline_s=10.0)
    assert seen == [0]
    assert result.outcomes["recs"].attempts == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hedge_launches_immediately_when_the_first_attempt_fails() -> None:
    async def call(attempt: int) -> JsonObject:
        if attempt == 0:
            raise ValueError("replica 0 refused")
        return {"from": "attempt-1"}

    policies = {"recs": optional(7200.0, hedge_after_s=3600.0)}  # the delay must not be waited out
    with anyio.fail_after(3):
        result = await fan_out({"recs": call}, policies, total_deadline_s=7200.0)
    assert result.data["recs"] == {"from": "attempt-1"}
    assert result.outcomes["recs"].attempts == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hedge_with_both_attempts_failing_degrades_to_the_fallback() -> None:
    policies = {"recs": optional(7200.0, hedge_after_s=3600.0)}
    with anyio.fail_after(3):
        result = await fan_out(
            {"recs": boom(RuntimeError("down"))}, policies, total_deadline_s=7200.0
        )
    assert result.outcomes["recs"] == Outcome("recs", "error", 2, "RuntimeError")
    assert result.data["recs"] == {"fallback": True}


# Contracts ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [
        {"deadline_s": 0.0, "critical": True},
        {"deadline_s": -1.0, "critical": True},
        {"deadline_s": float("nan"), "critical": True},
        {"deadline_s": float("inf"), "critical": True},
        {"deadline_s": 1.0, "critical": True, "fallback": {"x": 1}},
        {"deadline_s": 1.0},
        {"deadline_s": 1.0, "fallback": {}, "hedge_after_s": 1.0},
        {"deadline_s": 1.0, "fallback": {}, "hedge_after_s": 0.0},
        {"deadline_s": 1.0, "fallback": {}, "hedge_after_s": float("nan")},
    ],
)
def test_policy_rejects_illegal_combinations(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"deadline_s|fallback|hedge_after_s"):
        Policy(**kwargs)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fan_out_rejects_mismatched_names_and_bad_budget() -> None:
    policies = {"orders": Policy(deadline_s=1.0, critical=True)}
    with pytest.raises(ValueError, match="same upstreams"):
        await fan_out({"other": ok({})}, policies, total_deadline_s=1.0)
    with pytest.raises(ValueError, match="total_deadline_s"):
        await fan_out({"orders": ok({})}, policies, total_deadline_s=0.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anyio_task_group_raises_exception_group_not_the_bare_exception() -> None:
    async def fail() -> None:
        raise ValueError("child")

    with pytest.raises(ExceptionGroup) as info:
        async with anyio.create_task_group() as tg:
            tg.start_soon(fail)
    assert [type(e) for e in info.value.exceptions] == [ValueError]
