"""Tests for the incident triage tree, its real-loop probes, and the lock-queue model."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace

import anyio.to_thread
import httpx
import pytest

from api_labs.f18_observability_profiling_and_operations.incident_probes import (
    LockMode,
    LockQueue,
    LockRequest,
    LoopLagMonitor,
    build_demo_app,
    burn_cpu,
    simulate_convoy,
)
from api_labs.f18_observability_profiling_and_operations.incident_triage import (
    BASELINE,
    SCENARIO_NAMES,
    Canary,
    Cause,
    Snapshot,
    Thresholds,
    diagnose,
    littles_law_gap,
    offered_load_multiplier,
    scenario,
    with_latency,
)

EXPECTED: dict[str, Cause] = {
    "healthy": Cause.HEALTHY,
    "traffic_spike": Cause.TRAFFIC_SPIKE,
    "rate_limited_tenant": Cause.TRAFFIC_SPIKE,
    "downstream_stall": Cause.DOWNSTREAM_STALL,
    "db_lock_convoy": Cause.DB_LOCK_CONVOY,
    "event_loop_cpu": Cause.EVENT_LOOP_BLOCKING,
    "event_loop_blocking_io": Cause.EVENT_LOOP_BLOCKING,
    "threadpool_saturation": Cause.DOWNSTREAM_STALL,
    "bad_deploy_canary": Cause.BAD_DEPLOY,
    "bad_deploy_full_rollout": Cause.BAD_DEPLOY,
    "deploy_during_spike": Cause.TRAFFIC_SPIKE,
    "retry_storm_over_stall": Cause.DOWNSTREAM_STALL,
}


def _warnings(snap: Snapshot) -> str:
    return " | ".join(diagnose(snap).warnings)


# --- the decision tree on simulated snapshots -----------------------------------------------


@pytest.mark.unit
def test_expected_table_covers_every_scenario() -> None:
    assert set(EXPECTED) == set(SCENARIO_NAMES)


@pytest.mark.unit
@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_scenario_is_classified_to_its_cause(name: str) -> None:
    assert diagnose(scenario(name)).cause is EXPECTED[name]


@pytest.mark.unit
@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_every_scenario_obeys_littles_law(name: str) -> None:
    assert littles_law_gap(scenario(name)) < 1e-9


@pytest.mark.unit
def test_every_unhealthy_verdict_carries_evidence_and_an_action() -> None:
    for name in SCENARIO_NAMES:
        result = diagnose(scenario(name))
        assert result.evidence
        assert result.action
        assert result.path[0] == "sanity"


@pytest.mark.unit
def test_walk_stops_at_the_first_matching_node() -> None:
    result = diagnose(scenario("event_loop_cpu"))
    assert result.path == ("sanity", "degraded", "what_changed", "traffic", "event_loop")


@pytest.mark.unit
def test_traffic_is_ruled_in_before_loop_lag() -> None:
    spike = scenario("traffic_spike")
    assert spike.loop_lag_p99_s >= Thresholds().loop_lag_s
    assert diagnose(spike).cause is Cause.TRAFFIC_SPIKE
    # the same saturated process at baseline demand and no limiter is a loop problem instead
    not_organic = replace(spike, rps=BASELINE.rps, ratio_429=0.0)
    assert diagnose(not_organic).cause is Cause.EVENT_LOOP_BLOCKING


@pytest.mark.unit
def test_loop_lag_is_ruled_in_before_slow_dependencies() -> None:
    stalled_loop = scenario("event_loop_cpu")
    assert stalled_loop.db_pool_in_use == stalled_loop.db_pool_size
    assert stalled_loop.downstream_p99_s > 3 * BASELINE.downstream_p99_s
    assert diagnose(stalled_loop).cause is Cause.EVENT_LOOP_BLOCKING
    # identical numbers with a healthy loop point at the dependency instead
    healthy_loop = replace(stalled_loop, loop_lag_p99_s=0.004, cpu_util=0.2)
    result = diagnose(healthy_loop)
    assert result.cause is Cause.DOWNSTREAM_STALL
    assert result.detail == "connections held across a slow outbound call"


@pytest.mark.unit
def test_cpu_utilisation_separates_cpu_bound_from_blocking_io_on_the_loop() -> None:
    assert diagnose(scenario("event_loop_cpu")).detail == "cpu-bound code on the loop"
    assert diagnose(scenario("event_loop_blocking_io")).detail == "blocking io on the loop"


@pytest.mark.unit
def test_downstream_stall_has_a_quiet_loop_and_low_cpu() -> None:
    stall = scenario("downstream_stall")
    assert stall.loop_lag_p99_s < Thresholds().loop_lag_s
    assert stall.cpu_util < 0.2
    assert diagnose(stall).path[-1] == "downstream"


@pytest.mark.unit
def test_lock_waiters_with_a_saturated_pool_are_a_convoy() -> None:
    convoy = scenario("db_lock_convoy")
    assert diagnose(convoy).cause is Cause.DB_LOCK_CONVOY
    no_waiters = replace(convoy, db_lock_waiters=0)
    result = diagnose(no_waiters)
    assert result.cause is Cause.DOWNSTREAM_STALL
    assert result.detail == "slow queries holding connections"


@pytest.mark.unit
def test_lock_waiters_without_pool_saturation_are_not_blamed() -> None:
    quiet_pool = replace(scenario("db_lock_convoy"), db_pool_in_use=5)
    result = diagnose(quiet_pool)
    assert result.cause is Cause.INCONCLUSIVE
    assert "not the bottleneck" in " ".join(result.warnings)


@pytest.mark.unit
def test_a_change_just_before_onset_with_normal_traffic_is_blamed() -> None:
    result = diagnose(scenario("bad_deploy_full_rollout"))
    assert result.cause is Cause.BAD_DEPLOY
    assert result.detail == "time correlation only"


@pytest.mark.unit
def test_a_change_after_the_onset_is_not_blamed() -> None:
    snap = replace(scenario("bad_deploy_full_rollout"), change_age_s=100.0, onset_age_s=240.0)
    assert diagnose(snap).cause is Cause.INCONCLUSIVE


@pytest.mark.unit
def test_a_change_long_before_the_onset_is_not_blamed() -> None:
    snap = replace(scenario("bad_deploy_full_rollout"), change_age_s=1000.0, onset_age_s=240.0)
    assert diagnose(snap).cause is Cause.INCONCLUSIVE


@pytest.mark.unit
def test_a_change_during_organic_traffic_growth_does_not_outrank_traffic() -> None:
    snap = scenario("deploy_during_spike")
    assert snap.change_age_s is not None
    assert diagnose(snap).cause is Cause.TRAFFIC_SPIKE
    assert "organic traffic is also elevated" in _warnings(snap)


@pytest.mark.unit
def test_retries_caused_by_a_bad_deploy_do_not_hide_the_deploy() -> None:
    snap = replace(scenario("bad_deploy_full_rollout"), rps=520.0, retry_fraction=0.6)
    assert diagnose(snap).cause is Cause.BAD_DEPLOY


@pytest.mark.unit
def test_a_canary_that_matches_the_old_build_exonerates_the_deploy() -> None:
    snap = replace(
        scenario("bad_deploy_canary"), canary=Canary(0.002, 0.002, 0.24, 0.24), ratio_5xx=0.06
    )
    result = diagnose(snap)
    assert result.cause is Cause.INCONCLUSIVE
    assert "deploy exonerated" in " ".join(result.warnings)


@pytest.mark.unit
def test_a_slower_canary_alone_is_enough_to_blame_the_deploy() -> None:
    snap = replace(scenario("bad_deploy_canary"), canary=Canary(0.002, 0.002, 1.5, 0.24))
    assert diagnose(snap).cause is Cause.BAD_DEPLOY


@pytest.mark.unit
def test_retry_dominated_traffic_is_a_symptom_not_a_traffic_spike() -> None:
    storm = scenario("retry_storm_over_stall")
    assert storm.rps >= 1.5 * BASELINE.rps
    result = diagnose(storm)
    assert result.cause is Cause.DOWNSTREAM_STALL
    assert "symptom" in " ".join(result.warnings)
    no_fault = replace(storm, downstream_p99_s=0.05, downstream_timeout_ratio=0.0)
    assert diagnose(no_fault).cause is Cause.INCONCLUSIVE


@pytest.mark.unit
def test_limiter_ratio_boundary_decides_healthy_versus_traffic() -> None:
    base = scenario("healthy")
    assert diagnose(replace(base, ratio_429=0.049)).cause is Cause.HEALTHY
    assert diagnose(replace(base, ratio_429=0.05)).cause is Cause.TRAFFIC_SPIKE


@pytest.mark.unit
def test_thresholds_are_tunable() -> None:
    lenient = Thresholds(loop_lag_s=1.5)
    assert diagnose(scenario("event_loop_cpu"), th=lenient).cause is Cause.DOWNSTREAM_STALL


@pytest.mark.unit
def test_inconsistent_windows_raise_a_warning_but_not_the_verdict() -> None:
    skewed = replace(scenario("downstream_stall"), in_flight=40.0)
    assert littles_law_gap(skewed) > 0.9
    result = diagnose(skewed)
    assert result.cause is Cause.DOWNSTREAM_STALL
    assert "windows disagree" in " ".join(result.warnings)
    assert not diagnose(scenario("downstream_stall")).warnings


@pytest.mark.unit
def test_with_latency_recomputes_in_flight() -> None:
    snap = with_latency(scenario("healthy"), mean_s=0.5, p99_s=1.0)
    assert snap.in_flight == pytest.approx(100.0)


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        {"ratio_5xx": 1.5},
        {"cpu_util": -0.1},
        {"rps": -1.0},
        {"db_pool_in_use": 21},
        {"threadpool_in_use": 41},
    ],
)
def test_snapshot_rejects_impossible_values(bad: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=r"must|lie|non-negative"):
        replace(scenario("healthy"), **bad)  # type: ignore[arg-type]


@pytest.mark.unit
def test_layered_retries_multiply_offered_load() -> None:
    assert offered_load_multiplier(3, 5) == 243
    assert offered_load_multiplier(1, 5) == 1
    assert offered_load_multiplier(3, 0) == 1
    with pytest.raises(ValueError, match="attempts_per_layer"):
        offered_load_multiplier(0, 2)


# --- real asyncio loop and real Starlette routes --------------------------------------------

BLOCK_S = 0.4
QUIET_LAG_S = 0.2  # well above scheduler noise seen on a busy laptop, well below BLOCK_S


def _measured_snapshot(lag_s: float, cpu_util: float) -> Snapshot:
    return replace(scenario("healthy"), ratio_5xx=0.05, loop_lag_p99_s=lag_s, cpu_util=cpu_util)


async def _run_routes(path: str, copies: int = 2) -> tuple[float, float, float]:
    """Return (max loop lag, CPU seconds, wall seconds) while *copies* requests hit *path*."""
    app = build_demo_app(BLOCK_S, threading.Event())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get(path)).status_code == 200  # warm imports and middleware first
        async with LoopLagMonitor() as monitor:
            wall0, cpu0 = time.perf_counter(), time.process_time()
            responses = await asyncio.gather(*(client.get(path) for _ in range(copies)))
            wall, cpu = time.perf_counter() - wall0, time.process_time() - cpu0
    assert [r.status_code for r in responses] == [200] * copies
    return monitor.max_lag(), cpu, wall


async def _quiet_lag(path: str) -> float:
    """Smallest max-lag over three runs: scheduler noise only ever adds lag, a stall is constant."""
    return min([(await _run_routes(path))[0] for _ in range(3)])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_blocking_sleep_in_async_handler_stalls_the_loop_at_low_cpu() -> None:
    lag, cpu_s, wall_s = await _run_routes("/blocking-io")
    assert lag >= BLOCK_S * 0.75
    assert cpu_s < 0.1 * wall_s
    result = diagnose(_measured_snapshot(lag, cpu_s / wall_s))
    assert result.cause is Cause.EVENT_LOOP_BLOCKING
    assert result.detail == "blocking io on the loop"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cpu_burn_in_async_handler_stalls_the_loop_at_high_cpu() -> None:
    lag, cpu_s, wall_s = await _run_routes("/blocking-cpu")
    assert lag >= BLOCK_S * 0.75
    # on a contended host wall time stretches, so compare CPU seconds with the work requested
    assert cpu_s >= 2 * BLOCK_S * 0.95
    cpu_util = min(1.0, cpu_s / min(wall_s, 2 * BLOCK_S * 1.05))
    assert diagnose(_measured_snapshot(lag, cpu_util)).detail == "cpu-bound code on the loop"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/awaits", "/sync-blocking"])
async def test_awaiting_and_threadpool_handlers_leave_the_loop_responsive(path: str) -> None:
    assert await _quiet_lag(path) < QUIET_LAG_S


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_threadpool_saturation_starves_sync_routes_but_not_async_ones() -> None:
    gate = threading.Event()
    app = build_demo_app(BLOCK_S, gate)
    transport = httpx.ASGITransport(app=app)
    limiter = anyio.to_thread.current_default_thread_limiter()
    total = int(limiter.total_tokens)
    assert total == 40
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            blocked = [asyncio.create_task(client.get("/sync-gate")) for _ in range(total + 5)]
            deadline = time.monotonic() + 10.0
            # the limiter exposes no event to await, so poll its statistics
            while limiter.statistics().tasks_waiting < 5 and time.monotonic() < deadline:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
            stats = limiter.statistics()
            assert (stats.borrowed_tokens, stats.tasks_waiting) == (total, 5)
            healthz = await asyncio.wait_for(client.get("/healthz"), timeout=5.0)
            assert healthz.status_code == 200
            late_sync = asyncio.create_task(client.get("/sync-gate"))
            await asyncio.sleep(0.1)
            assert not late_sync.done()
            snap = replace(
                scenario("healthy"),
                ratio_5xx=0.1,
                threadpool_limit=total,
                threadpool_in_use=stats.borrowed_tokens,
            )
            assert diagnose(snap).detail == "sync handlers parked on blocking calls"
            gate.set()
            results = await asyncio.gather(*blocked, late_sync)
            assert all(r.status_code == 200 for r in results)
    finally:
        gate.set()


@pytest.mark.unit
def test_percentile_is_nearest_rank() -> None:
    monitor = LoopLagMonitor()
    assert monitor.percentile(99) == 0.0
    monitor.samples = [float(n) for n in range(10, 0, -1)]
    assert monitor.percentile(50) == 5.0
    assert monitor.percentile(90) == 9.0
    assert monitor.percentile(100) == 10.0
    assert monitor.max_lag() == 10.0


@pytest.mark.unit
def test_burn_cpu_consumes_cpu_time_and_sleep_does_not() -> None:
    cpu0 = time.process_time()
    burn_cpu(0.1)
    assert time.process_time() - cpu0 >= 0.1
    cpu1 = time.process_time()
    time.sleep(0.2)
    assert time.process_time() - cpu1 < 0.05


# --- the PostgreSQL-style lock queue ---------------------------------------------------------


def _req(pid: int, mode: LockMode, hold_s: float = 1.0) -> LockRequest:
    return LockRequest(pid=pid, mode=mode, hold_s=hold_s, arrived_s=0.0)


@pytest.mark.unit
def test_readers_share_but_a_queued_exclusive_request_blocks_later_readers() -> None:
    queue = LockQueue()
    long_reader = _req(1, LockMode.ACCESS_SHARE)
    ddl = _req(2, LockMode.ACCESS_EXCLUSIVE)
    late_reader = _req(3, LockMode.ACCESS_SHARE)
    assert queue.request(long_reader, 0.0)
    assert queue.request(_req(4, LockMode.ACCESS_SHARE), 0.0)  # readers share
    assert not queue.request(ddl, 1.0)
    assert not queue.request(late_reader, 1.1)  # compatible with the holders, still queued
    assert queue.blocking_pids(ddl) == (1, 4)
    assert queue.blocking_pids(late_reader) == (2,)  # soft block: only the queued DDL


@pytest.mark.unit
def test_queue_drains_in_order_when_the_head_blocker_goes_away() -> None:
    queue = LockQueue()
    long_reader = _req(1, LockMode.ACCESS_SHARE)
    ddl = _req(2, LockMode.ACCESS_EXCLUSIVE)
    late_reader = _req(3, LockMode.ACCESS_SHARE)
    queue.request(long_reader, 0.0)
    queue.request(ddl, 1.0)
    queue.request(late_reader, 1.1)
    queue.release(long_reader, 5.0)
    assert ddl.granted_s == 5.0
    assert late_reader.granted_s is None
    queue.release(ddl, 5.05)
    assert late_reader.granted_s == 5.05


@pytest.mark.unit
def test_cancelling_the_exclusive_request_releases_the_readers_behind_it() -> None:
    queue = LockQueue()
    queue.request(_req(1, LockMode.ACCESS_SHARE), 0.0)
    ddl = _req(2, LockMode.ACCESS_EXCLUSIVE)
    late_reader = _req(3, LockMode.ACCESS_SHARE)
    queue.request(ddl, 1.0)
    queue.request(late_reader, 1.1)
    queue.cancel(ddl, 3.0)
    assert late_reader.granted_s == 3.0


@pytest.mark.unit
def test_row_exclusive_is_also_blocked_by_a_queued_exclusive_request() -> None:
    queue = LockQueue()
    queue.request(_req(1, LockMode.ACCESS_SHARE), 0.0)
    queue.request(_req(2, LockMode.ACCESS_EXCLUSIVE), 1.0)
    writer = _req(3, LockMode.ROW_EXCLUSIVE)
    assert not queue.request(writer, 1.1)


@pytest.mark.unit
def test_without_the_ddl_there_is_no_convoy() -> None:
    result = simulate_convoy(alter=False)
    assert result.pool_exhausted_at_s is None
    assert result.max_lock_waiters == 0
    assert result.rejected_requests == 0


@pytest.mark.unit
@pytest.mark.parametrize("pool_size", [10, 20, 40])
def test_pool_exhaustion_time_is_pool_size_over_blocked_arrival_rate(pool_size: int) -> None:
    result = simulate_convoy(pool_size=pool_size)
    blocked_rate = 100.0 / 10  # every tenth request reads the locked table
    expected = 1.0 + pool_size / blocked_rate
    assert result.pool_exhausted_at_s is not None
    assert result.pool_exhausted_at_s == pytest.approx(expected, abs=0.15)


@pytest.mark.unit
def test_the_convoy_takes_down_requests_that_never_touch_the_locked_table() -> None:
    result = simulate_convoy()
    assert result.rejected_requests > 2000
    assert result.rejected_other_table > 0.85 * result.rejected_requests


@pytest.mark.unit
def test_the_convoy_ends_only_when_the_long_reader_does() -> None:
    result = simulate_convoy()
    assert result.alter_granted_at_s == pytest.approx(30.0, abs=0.02)
    assert result.recovered_at_s == pytest.approx(30.05, abs=0.2)
    assert result.longest_lock_wait_s > 25.0


@pytest.mark.unit
def test_lock_timeout_shorter_than_the_exhaustion_time_prevents_the_outage() -> None:
    safe = simulate_convoy(lock_timeout_s=1.0)  # exhaustion would take 2.0 s
    assert safe.alter_aborted
    assert safe.pool_exhausted_at_s is None
    assert safe.rejected_requests == 0
    assert safe.longest_lock_wait_s < 1.0 + 0.05


@pytest.mark.unit
def test_lock_timeout_longer_than_the_exhaustion_time_still_hurts() -> None:
    late = simulate_convoy(lock_timeout_s=3.0)
    assert late.alter_aborted
    assert late.pool_exhausted_at_s is not None
    assert late.rejected_requests > 50
    assert late.recovered_at_s is not None
    assert late.recovered_at_s < 5.0


@pytest.mark.unit
def test_simulated_convoy_numbers_feed_the_tree_to_the_convoy_verdict() -> None:
    result = simulate_convoy()
    snap = replace(
        scenario("db_lock_convoy"),
        db_pool_in_use=20,
        db_lock_waiters=result.max_lock_waiters - 1,  # the queue holds 20 readers and the DDL
    )
    assert diagnose(snap).cause is Cause.DB_LOCK_CONVOY
