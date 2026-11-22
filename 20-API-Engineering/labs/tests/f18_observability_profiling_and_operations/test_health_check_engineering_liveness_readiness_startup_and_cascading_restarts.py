"""Lab for Health-Check-Engineering-Liveness-Readiness-Startup-and-Cascading-Restarts."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import httpx
import pytest
from asgi_lifespan import LifespanManager

from api_labs.f18_observability_profiling_and_operations.health_probes import (
    CachedDependencyCheck,
    DbMode,
    Fault,
    HealthState,
    Heartbeat,
    LivenessKind,
    Phase,
    ProbeSpec,
    ProbeWorker,
    ReadinessKind,
    ReadinessMode,
    Scenario,
    blip,
    build_app,
    restart_window,
    simulate,
)

if TYPE_CHECKING:
    from starlette.applications import Starlette

SECRET = "db at 10.0.0.5 refused password=hunter2"  # noqa: S105 - obviously fake


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeDb:
    def __init__(self, *, healthy: bool = True, latency_s: float = 0.0) -> None:
        self.healthy = healthy
        self.latency_s = latency_s
        self.calls = 0

    async def ping(self) -> None:
        self.calls += 1
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if not self.healthy:
            raise ConnectionError(SECRET)


def phase_of(state: HealthState) -> Phase:
    return state.phase


def client_for(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def make(
    db: FakeDb,
    *,
    mode: ReadinessMode = ReadinessMode.LOCAL,
    deep_liveness: bool = False,
    started: bool = True,
    ttl_s: float = 5.0,
    clock: FakeClock | None = None,
) -> tuple[Starlette, HealthState, CachedDependencyCheck]:
    state = HealthState()
    if started:
        state.mark_started()
    dep = CachedDependencyCheck(
        "db", db.ping, timeout_s=0.5, ttl_s=ttl_s, clock=clock or FakeClock()
    )
    app = build_app(state, dep, readiness=mode, deep_liveness=deep_liveness)
    return app, state, dep


# --------------------------------------------------------------------------------------
# Part A: probe endpoints
# --------------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shallow_liveness_stays_200_and_never_touches_a_down_dependency() -> None:
    db = FakeDb(healthy=False)
    app, _, _ = make(db)
    async with client_for(app) as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "pass"}
    assert resp.headers["content-type"] == "application/health+json"
    assert resp.headers["cache-control"] == "no-store"
    assert db.calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deep_liveness_returns_503_when_the_dependency_is_down() -> None:
    db = FakeDb(healthy=False)
    app, _, _ = make(db, deep_liveness=True)
    async with client_for(app) as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "fail", "reason": "db-down"}
    assert db.calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_readiness_and_startup_are_503_until_started_while_liveness_is_200() -> None:
    app, state, _ = make(FakeDb(), started=False)
    async with client_for(app) as client:
        before = [(await client.get(p)).status_code for p in ("/healthz", "/startupz", "/readyz")]
        state.mark_started()
        after = [(await client.get(p)).status_code for p in ("/healthz", "/startupz", "/readyz")]
    assert before == [200, 503, 503]
    assert after == [200, 200, 200]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_draining_removes_readiness_but_liveness_stays_200() -> None:
    app, state, _ = make(FakeDb())
    state.begin_drain()
    async with client_for(app) as client:
        ready = await client.get("/readyz")
        live = await client.get("/healthz")
    assert ready.status_code == 503
    assert ready.json() == {"status": "fail", "reason": "draining"}
    assert live.status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lifespan_marks_started_after_warmup_and_draining_on_shutdown() -> None:
    state = HealthState()
    warmed = asyncio.Event()

    async def warmup() -> None:
        await asyncio.sleep(0)
        warmed.set()

    app = build_app(state, None, warmup=warmup)
    assert phase_of(state) is Phase.STARTING
    async with LifespanManager(app):
        assert warmed.is_set()
        assert phase_of(state) is Phase.SERVING
    assert phase_of(state) is Phase.DRAINING


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "status", "body_status"),
    [(ReadinessMode.LOCAL, 200, "warn"), (ReadinessMode.DEPENDENCY, 503, "fail")],
)
async def test_readiness_mode_decides_whether_a_down_dependency_removes_the_pod(
    mode: ReadinessMode, status: int, body_status: str
) -> None:
    app, _, _ = make(FakeDb(healthy=False), mode=mode)
    async with client_for(app) as client:
        resp = await client.get("/readyz")
    assert resp.status_code == status
    assert resp.json() == {"status": body_status, "checks": {"db": "fail"}}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_probes_share_one_dependency_call() -> None:
    db = FakeDb(latency_s=0.05)
    app, _, _ = make(db, mode=ReadinessMode.DEPENDENCY)
    async with client_for(app) as client:
        responses = await asyncio.gather(*(client.get("/readyz") for _ in range(50)))
    assert {r.status_code for r in responses} == {200}
    assert db.calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dependency_result_is_reused_until_the_ttl_expires() -> None:
    clock = FakeClock()
    db = FakeDb()
    dep = CachedDependencyCheck("db", db.ping, timeout_s=0.5, ttl_s=5.0, clock=clock)
    await dep.status()
    clock.now = 4.9
    await dep.status()
    assert db.calls == 1
    clock.now = 5.1
    await dep.status()
    assert db.calls == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_hung_dependency_times_out_and_counts_as_failure() -> None:
    db = FakeDb(latency_s=5.0)
    dep = CachedDependencyCheck("db", db.ping, timeout_s=0.01, ttl_s=5.0)
    started = time.perf_counter()
    result = await dep.status()
    assert time.perf_counter() - started < 1.0
    assert not result.ok
    assert result.error == "TimeoutError"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_body_never_leaks_dependency_error_text() -> None:
    app, _, dep = make(FakeDb(healthy=False), mode=ReadinessMode.DEPENDENCY)
    async with client_for(app) as client:
        resp = await client.get("/readyz")
    assert "hunter2" not in resp.text
    assert "10.0.0.5" not in resp.text
    result = await dep.status()
    assert result.error == "ConnectionError"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_heartbeat_fails_liveness_and_a_beat_recovers_it() -> None:
    clock = FakeClock()
    beat = Heartbeat(max_age_s=10.0, clock=clock)
    app = build_app(HealthState(), None, heartbeat=beat)
    async with client_for(app) as client:
        clock.now = 9.0
        assert (await client.get("/healthz")).status_code == 200
        clock.now = 11.0
        stale = await client.get("/healthz")
        beat.beat()
        fresh = await client.get("/healthz")
    assert stale.status_code == 503
    assert stale.json() == {"status": "fail", "reason": "heartbeat-stale"}
    assert fresh.status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_blocking_call_on_the_loop_delays_even_the_cheapest_liveness_probe() -> None:
    app, _, dep = make(FakeDb(healthy=False))

    async def blocker() -> None:
        time.sleep(0.2)  # noqa: ASYNC251 - the bug under test: a synchronous call on the loop

    async with client_for(app) as client:
        started = time.perf_counter()
        task = asyncio.create_task(blocker())
        await asyncio.sleep(0)  # blocker runs now and holds the loop for 0.2 s
        resp = await client.get("/healthz")
        elapsed = time.perf_counter() - started
        await task
    assert resp.status_code == 200
    assert dep.calls == 0
    assert elapsed >= 0.2


# --------------------------------------------------------------------------------------
# Part B: kubelet worker and fleet simulation
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_default_probe_values_match_kubelet_defaults() -> None:
    spec = ProbeSpec()
    assert (spec.period_s, spec.timeout_s, spec.success_threshold, spec.failure_threshold) == (
        10,
        1,
        1,
        3,
    )
    assert spec.initial_delay_s == 0


@pytest.mark.unit
def test_worker_flips_only_after_a_run_of_equal_results() -> None:
    live = ProbeWorker(ProbeSpec(failure_threshold=3), initial=True, phase_s=0)
    assert [live.record(False), live.record(False)] == [None, None]
    assert live.state is True
    assert live.record(True) is True  # successThreshold 1: re-sets Success, resets the failure run
    assert [live.record(False), live.record(False)] == [None, None]
    assert live.record(False) is False
    assert live.state is False

    ready = ProbeWorker(ProbeSpec(success_threshold=2), initial=False, phase_s=0)
    assert ready.record(True) is None
    assert ready.state is False
    assert ready.record(True) is True


@pytest.mark.unit
def test_pods_are_not_ready_before_the_app_listens() -> None:
    out = simulate(Scenario(warmup_s=0, duration_s=10))
    assert out.zero_endpoint_s == 10
    assert out.fail_other == 10 * 50
    assert out.ok_other == 0


@pytest.mark.unit
def test_restart_window_formula_matches_the_simulation() -> None:
    spec = ProbeSpec()
    low, high = restart_window(spec)
    assert (low, high) == (21, 30)
    for length in range(1, 41):
        out = simulate(
            Scenario(
                liveness=LivenessKind.DEEP,
                liveness_spec=ProbeSpec(initial_delay_s=30),
                readiness_spec=ProbeSpec(),
                db_windows=blip(length),
            )
        )
        if length < low:
            assert out.restarts == 0, length
        elif length < high:
            assert 0 < out.restarts < 20, length
        else:
            assert out.restarts == 20, length


@pytest.mark.unit
def test_deep_liveness_turns_a_five_second_blip_into_a_fleet_wide_restart() -> None:
    out = simulate(Scenario(liveness=LivenessKind.DEEP, db_windows=blip(5)))
    assert out.restarts == 20
    assert out.zero_endpoint_s == 20  # four times the database outage
    assert out.fail_other == 20 * 50  # requests that never needed the database failed
    assert out.recovery_s is not None
    assert out.recovery_s >= 20  # the boot time, long after the database came back


@pytest.mark.unit
def test_shallow_liveness_with_local_readiness_rides_out_the_blip() -> None:
    out = simulate(Scenario(db_windows=blip(5)))
    assert out.restarts == 0
    assert out.min_ready == 20
    assert out.fail_other == 0  # requests that do not need the database are untouched
    assert out.fail_db == 5 * 50  # exactly the database requests of the outage, failing fast
    assert out.recovery_s == 0


@pytest.mark.unit
def test_dependency_readiness_on_a_shared_database_removes_every_pod() -> None:
    out = simulate(Scenario(readiness=ReadinessKind.DEPENDENCY, db_windows=blip(5)))
    assert out.restarts == 0
    assert out.min_ready == 0
    assert out.zero_endpoint_s == 3  # failureThreshold 3 at period 1
    assert out.fail_other == 3 * 50  # collateral damage: non-database traffic loses all endpoints


@pytest.mark.unit
def test_a_slow_but_answering_database_kills_pods_unless_the_probe_timeout_exceeds_it() -> None:
    def run(timeout_s: int) -> tuple[int, int, int]:
        spec = ProbeSpec(period_s=1, timeout_s=timeout_s, initial_delay_s=30)
        out = simulate(
            Scenario(
                liveness=LivenessKind.DEEP,
                liveness_spec=spec,
                db_windows=blip(5, DbMode.SLOW),
            )
        )
        return out.restarts, out.fail_other, out.fail_db

    assert run(timeout_s=1)[0] == 20  # 1.5 s ping > 1 s timeout: killed although it answers
    assert run(timeout_s=1)[1] > 0
    assert run(timeout_s=2) == (0, 0, 0)  # timeout above the dependency latency: no effect


@pytest.mark.unit
def test_startup_probe_lets_a_slow_boot_finish_instead_of_crash_looping() -> None:
    slow = Scenario(boot_s=45, warmup_s=60, liveness_spec=ProbeSpec(initial_delay_s=0))
    without = simulate(slow)
    with_startup = simulate(
        Scenario(
            boot_s=45,
            warmup_s=60,
            liveness_spec=ProbeSpec(initial_delay_s=0),
            readiness_spec=ProbeSpec(period_s=1),
            startup_spec=ProbeSpec(period_s=10, failure_threshold=30),
        )
    )
    assert without.restarts >= 2 * 20
    assert without.min_ready == 0
    assert with_startup.restarts == 0
    assert with_startup.min_ready == 20


@pytest.mark.unit
def test_a_pod_local_pool_fault_is_the_case_for_dependency_readiness() -> None:
    faults = (Fault(3, 60, 180), Fault(7, 60, 180))
    local = simulate(Scenario(pool_faults=faults))
    dependency = simulate(Scenario(pool_faults=faults, readiness=ReadinessKind.DEPENDENCY))
    assert local.fail_db > 400  # two pods black-hole their share of database requests for 120 s
    assert dependency.fail_db < 50  # ejected after failureThreshold probes
    assert dependency.min_ready == 18  # only the two sick pods leave the Service
    assert dependency.fail_other == 0
    assert dependency.restarts == 0


@pytest.mark.unit
def test_deep_liveness_is_safe_for_one_sick_pod_and_lethal_for_a_correlated_fault() -> None:
    faults = (Fault(3, 60, 180), Fault(7, 60, 180))
    one_pod = simulate(Scenario(liveness=LivenessKind.DEEP, pool_faults=faults))
    shared = simulate(Scenario(liveness=LivenessKind.DEEP, db_windows=blip(120)))
    assert one_pod.restarts >= 2
    assert one_pod.fail_other == 0  # the healthy majority keeps serving
    assert shared.min_ready == 0  # the same check on a shared dependency empties the Service


@pytest.mark.unit
def test_shallow_liveness_restarts_a_wedged_loop_but_ignores_a_short_stall() -> None:
    brief = simulate(Scenario(stalls=(Fault(5, 60, 62),)))
    wedged = simulate(Scenario(stalls=(Fault(5, 60, 120),)))
    assert brief.restarts == 0  # 2 stalled probes < failureThreshold 3
    assert wedged.restarts >= 1
    assert wedged.fail_other > 0  # requests routed to the wedged pod fail until it is removed
