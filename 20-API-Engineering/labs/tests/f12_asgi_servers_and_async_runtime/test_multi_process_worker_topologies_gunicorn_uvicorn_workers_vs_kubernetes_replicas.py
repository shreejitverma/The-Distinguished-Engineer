"""Lab: Gunicorn 26.2.0 vs uvicorn --workers vs single-process pods, and worker recycling."""

from __future__ import annotations

import gc
import random
import signal
import sys

import httpx
import pytest

from api_labs.f12_asgi_servers_and_async_runtime.worker_topologies import (
    RoundRobinWorkers,
    arbiter_murder_signals,
    frozen_object_count,
    gunicorn_request_budget,
    loop_heartbeat_gap,
    make_counter_app,
    refcount_writes,
    run_in_forked_child,
    simulate_recycling,
    uvicorn_ping_while_loop_blocked,
    uvicorn_request_budget,
    uvicorn_worker_start_method,
)

# -- Recycling budgets ---------------------------------------------------------------------


@pytest.mark.unit
def test_gunicorn_budget_is_max_requests_plus_randint_jitter() -> None:
    budgets = {gunicorn_request_budget(1000, 50, seed=seed) for seed in range(200)}
    assert min(budgets) >= 1000
    assert max(budgets) <= 1050
    assert len(budgets) > 30  # the jitter really varies per worker


@pytest.mark.unit
def test_gunicorn_budget_includes_both_ends_of_the_jitter_range() -> None:
    budgets = {gunicorn_request_budget(10, 2, seed=seed) for seed in range(100)}
    assert budgets == {10, 11, 12}


@pytest.mark.unit
def test_gunicorn_max_requests_zero_disables_recycling() -> None:
    assert gunicorn_request_budget(0, 50, seed=1) == sys.maxsize


@pytest.mark.unit
def test_uvicorn_budget_uses_the_same_formula_and_none_disables_it() -> None:
    budgets = {uvicorn_request_budget(1000, 50, seed=seed) for seed in range(200)}
    assert all(b is not None and 1000 <= b <= 1050 for b in budgets)
    assert len(budgets) > 30
    assert uvicorn_request_budget(None, 50, seed=1) is None


@pytest.mark.unit
def test_uvicorn_default_jitter_is_zero_so_every_worker_gets_the_same_budget() -> None:
    assert {uvicorn_request_budget(1000, 0, seed=seed) for seed in range(20)} == {1000}


@pytest.mark.unit
def test_zero_jitter_recycles_every_worker_in_the_same_burst() -> None:
    plan = simulate_recycling(workers=8, budget=lambda: 1000, total_requests=8000)
    assert len(plan.restarts) == 8
    assert plan.worst_burst(window=8) == 8  # all eight slots restart back to back


@pytest.mark.unit
def test_jitter_spreads_restarts_so_most_capacity_stays_up() -> None:
    rng = random.Random(7)  # noqa: S311 - simulation, not security
    # 10 budgets' worth of traffic per worker, so every slot recycles several times; with
    # only 1000 requests per worker no jittered budget (>= 1000) is ever reached.
    plan = simulate_recycling(
        workers=8,
        budget=lambda: 1000 + rng.randint(0, 500),
        total_requests=80_000,
    )
    assert len(plan.restarts) >= 50
    assert plan.worst_burst(window=8) == 2


@pytest.mark.unit
def test_worst_burst_counts_only_restarts_inside_the_window() -> None:
    plan = simulate_recycling(workers=2, budget=lambda: 3, total_requests=12)
    assert plan.restarts == [4, 5, 10, 11]
    assert plan.worst_burst(window=2) == 2
    assert plan.worst_burst(window=1) == 1
    assert plan.worst_burst(window=7) == 3  # 4..10 holds 4, 5, 10
    assert plan.worst_burst(window=8) == 4


# -- Heartbeats ----------------------------------------------------------------------------


@pytest.mark.unit
def test_arbiter_leaves_a_worker_with_a_fresh_heartbeat_alone() -> None:
    assert arbiter_murder_signals(timeout=30, silent_for=0.0, passes=2) == []


@pytest.mark.unit
def test_arbiter_sends_sigabrt_then_sigkill_to_a_silent_worker() -> None:
    sent = arbiter_murder_signals(timeout=30, silent_for=31.0, passes=2)
    assert sent == [signal.SIGABRT, signal.SIGKILL]


@pytest.mark.unit
def test_arbiter_timeout_zero_disables_the_watchdog() -> None:
    assert arbiter_murder_signals(timeout=0, silent_for=10_000.0, passes=2) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_blocking_call_starves_an_event_loop_heartbeat() -> None:
    gap = await loop_heartbeat_gap(block_seconds=0.4, interval=0.02)
    assert gap >= 0.4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_blocking_loop_keeps_heartbeats_close_to_the_interval() -> None:
    gap = await loop_heartbeat_gap(block_seconds=0.0, interval=0.02)
    assert gap < 0.3


@pytest.mark.socket
def test_uvicorn_supervisor_ping_succeeds_while_the_event_loop_is_blocked() -> None:
    result = uvicorn_ping_while_loop_blocked(1.0, started=True)
    assert result.answered
    assert result.loop_was_blocked
    assert result.ready


@pytest.mark.socket
def test_uvicorn_readiness_reports_server_started_flag() -> None:
    result = uvicorn_ping_while_loop_blocked(0.4, started=False)
    assert result.answered
    assert not result.ready


@pytest.mark.unit
def test_uvicorn_workers_use_spawn_so_nothing_is_shared_copy_on_write() -> None:
    assert uvicorn_worker_start_method() == "spawn"


# -- Fork semantics ------------------------------------------------------------------------

STATE: dict[str, int] = {"hits": 0}


def _increment_state() -> str:
    STATE["hits"] += 100
    return str(STATE["hits"])


@pytest.mark.unit
def test_forked_worker_mutations_never_reach_the_parent_or_siblings() -> None:
    STATE["hits"] = 1
    first = run_in_forked_child(_increment_state)
    second = run_in_forked_child(_increment_state)
    assert (first, second) == ("101", "101")
    assert STATE["hits"] == 1


@pytest.mark.unit
def test_private_random_instance_created_before_fork_repeats_in_every_worker() -> None:
    preloaded = random.Random()  # noqa: S311 - an app-level generator built at import time

    def draw() -> str:
        return f"{preloaded.random():.17f}"

    assert run_in_forked_child(draw) == run_in_forked_child(draw)


@pytest.mark.unit
def test_module_random_is_reseeded_in_each_forked_child() -> None:
    def draw() -> str:
        return f"{random.random():.17f}"  # noqa: S311 - observing reseeding

    assert run_in_forked_child(draw) != run_in_forked_child(draw)


@pytest.mark.unit
def test_gc_freeze_moves_tracked_objects_to_the_permanent_generation() -> None:
    gc.collect()
    frozen, after_unfreeze = frozen_object_count()
    assert frozen > 1000
    assert after_unfreeze == 0


@pytest.mark.unit
def test_ordinary_objects_take_refcount_writes_but_immortal_ones_do_not() -> None:
    payload = object()
    assert refcount_writes(payload, 1000) == 1000
    assert refcount_writes(None, 1000) == 0


CLIENT_A = {"x-client": "a"}

# -- Per-worker memory ---------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_in_memory_counter_diverges_across_workers() -> None:
    app = RoundRobinWorkers([make_counter_app() for _ in range(3)])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        bodies = [(await client.get("/hit")).json() for _ in range(6)]
    assert [b["hits"] for b in bodies] == [1, 1, 1, 2, 2, 2]
    assert len({b["worker"] for b in bodies}) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_in_memory_rate_limit_admits_workers_times_limit() -> None:
    app = RoundRobinWorkers([make_counter_app(limit=5) for _ in range(4)])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        codes = [(await client.get("/hit", headers=CLIENT_A)).status_code for _ in range(30)]
    assert codes.count(200) == 20
    assert codes.count(429) == 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shared_store_restores_the_global_rate_limit() -> None:
    shared: dict[str, int] = {}
    app = RoundRobinWorkers([make_counter_app(store=shared, limit=5) for _ in range(4)])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        codes = [(await client.get("/hit", headers=CLIENT_A)).status_code for _ in range(30)]
    assert codes.count(200) == 5
    assert codes[:5] == [200] * 5
