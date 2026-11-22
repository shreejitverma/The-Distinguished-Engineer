"""Lab for the profiling note: task-step accounting, SQL attribution, samplers, cProfile, memory."""

from __future__ import annotations

import asyncio
import cProfile
import sys
import threading
import time
import tracemalloc
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from api_labs.f18_observability_profiling_and_operations.async_sampler import (
    ThreadSampler,
    await_chain,
    sample_in_loop,
)
from api_labs.f18_observability_profiling_and_operations.cprofile_view import cprofile_coroutine
from api_labs.f18_observability_profiling_and_operations.memory_profile import (
    folded_allocation_stacks,
    measure_call,
)
from api_labs.f18_observability_profiling_and_operations.request_profile import (
    CURRENT_PROFILE,
    RequestProfile,
    SqlAttribution,
    TaskStepMeter,
    fingerprint,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.unit

MS = 1_000_000


@dataclass
class Harness:
    client: httpx.AsyncClient
    profiles: list[RequestProfile]
    meter: TaskStepMeter
    engine: AsyncEngine
    attribution: SqlAttribution

    async def profile(self, path: str) -> RequestProfile:
        self.profiles.clear()
        response = await self.client.get(path)
        assert response.status_code == 200
        assert len(self.profiles) == 1
        return self.profiles[0]


@pytest_asyncio.fixture
async def harness() -> AsyncIterator[Harness]:
    # Imported here because greenlet crashes on import of sqlalchemy.ext.asyncio on 3.15.0b3t.
    from api_labs.f18_observability_profiling_and_operations.profiled_app import (
        attach,
        build_app,
        create_engine,
        seed,
    )

    engine = create_engine()
    await seed(engine)
    profiles: list[RequestProfile] = []
    with TaskStepMeter() as meter:
        attribution = attach(engine)
        app = build_app(engine, meter, profiles.append)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # /thread warms the default executor: the first to_thread starts a worker that takes
            # the GIL inside Thread.start(), which charges about one switch interval to blocked_ns.
            for warm in (
                "/cpu?ms=1",
                "/wait?ms=1",
                "/thread?ms=1",
                "/slow-sql?ms=1",
                "/orders?n=1",
            ):
                await client.get(warm)
            profiles.clear()
            yield Harness(client, profiles, meter, engine, attribution)
    await engine.dispose()


@pytest.fixture
def tracing() -> Iterator[None]:
    tracemalloc.start(8)
    try:
        yield
    finally:
        tracemalloc.stop()


def test_fingerprint_collapses_literals_and_in_lists() -> None:
    sql = "SELECT *  FROM t WHERE id = 42 AND name = 'it''s' AND k IN (?, ?, ?)"
    assert fingerprint(sql) == "SELECT * FROM t WHERE id = ? AND name = ? AND k IN (?+)"


@pytest.mark.asyncio
async def test_cpu_bound_handler_is_charged_to_cpu(harness: Harness) -> None:
    profile = await harness.profile("/cpu?ms=30")
    assert profile.route == "/cpu"
    assert profile.cpu_ns >= 30 * MS
    assert profile.wait_ns < 10 * MS


@pytest.mark.asyncio
async def test_awaited_sleep_is_charged_to_wait_not_cpu(harness: Harness) -> None:
    profile = await harness.profile("/wait?ms=50")
    assert profile.wait_ns >= 40 * MS
    assert profile.cpu_ns < profile.wait_ns / 2
    assert profile.blocked_ns < 10 * MS


@pytest.mark.asyncio
async def test_blocking_sleep_is_charged_to_blocked_not_wait(harness: Harness) -> None:
    profile = await harness.profile("/block?ms=50")
    assert profile.blocked_ns >= 45 * MS
    assert profile.cpu_ns < 25 * MS
    assert profile.wait_ns < 10 * MS


@pytest.mark.asyncio
async def test_cpu_in_a_worker_thread_is_invisible_to_loop_thread_cpu(harness: Harness) -> None:
    profile = await harness.profile("/thread?ms=30")
    assert profile.process_cpu_ns >= 30 * MS
    assert profile.cpu_ns < 15 * MS
    assert profile.wait_ns >= 25 * MS


@pytest.mark.asyncio
async def test_slow_query_time_is_attributed_to_its_statement(harness: Harness) -> None:
    profile = await harness.profile("/slow-sql?ms=60")
    assert profile.sql_count == 1
    stat = profile.queries["SELECT slow_ms(?)"]
    assert stat.total_ns >= 55 * MS
    assert profile.sql_ns <= profile.wall_ns
    assert profile.wait_ns >= 0.8 * profile.sql_ns


@pytest.mark.asyncio
async def test_n_plus_one_shows_as_query_count(harness: Harness) -> None:
    profile = await harness.profile("/orders?n=7")
    assert profile.sql_count == 8
    counts = sorted(stat.count for stat in profile.queries.values())
    assert counts == [1, 7]
    lookup = next(s for s in profile.queries.values() if s.count == 7)
    assert "WHERE orders.id = ?" in lookup.fingerprint or "WHERE id = ?" in lookup.fingerprint


@pytest.mark.asyncio
async def test_failed_statement_is_recorded_as_error_and_leaves_no_timer(
    harness: Harness,
) -> None:
    profile = RequestProfile()
    token = CURRENT_PROFILE.set(profile)
    try:
        async with harness.engine.connect() as conn:
            with pytest.raises(DBAPIError):
                await conn.execute(text("SELECT * FROM no_such_table"))
            await conn.rollback()
            await conn.execute(text("SELECT 1"))
    finally:
        CURRENT_PROFILE.reset(token)
    failed = profile.queries["SELECT * FROM no_such_table"]
    assert (failed.count, failed.errors) == (1, 1)
    assert profile.queries["SELECT ?"].errors == 0


@pytest.mark.asyncio
async def test_uninstall_stops_attribution(harness: Harness) -> None:
    harness.attribution.uninstall()
    profile = await harness.profile("/slow-sql?ms=5")
    assert profile.sql_count == 0
    harness.attribution.install()


@pytest.mark.asyncio
async def test_concurrent_requests_are_not_cross_charged(harness: Harness) -> None:
    harness.profiles.clear()
    await asyncio.gather(harness.client.get("/wait?ms=80"), harness.client.get("/cpu?ms=30"))
    by_route = {p.route: p for p in harness.profiles}
    assert by_route["/wait"].cpu_ns < 15 * MS
    assert by_route["/wait"].wait_ns >= 60 * MS
    assert by_route["/cpu"].cpu_ns >= 30 * MS


@pytest.mark.asyncio
async def test_step_depth_returns_to_baseline_after_cancellation(harness: Harness) -> None:
    baseline = harness.meter.open_depth()

    async def inner() -> None:
        await asyncio.sleep(10)

    async def outer() -> None:
        await inner()

    task = asyncio.ensure_future(outer())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert harness.meter.open_depth() == baseline


def test_disable_from_other_events_is_gated_on_cpython_3_15() -> None:
    monitoring = sys.monitoring
    monitoring.use_tool_id(4, "lab-disable-probe")
    try:
        monitoring.register_callback(4, monitoring.events.PY_UNWIND, lambda *_: monitoring.DISABLE)
        monitoring.set_events(4, monitoring.events.PY_UNWIND)

        def raises() -> None:
            raise KeyError

        def catches() -> None:
            try:
                raises()
            except KeyError:
                return

        if sys.version_info >= (3, 15):
            catches()
        else:
            with pytest.raises(ValueError, match="Cannot disable PY_UNWIND"):
                catches()
    finally:
        monitoring.set_events(4, monitoring.events.NO_EVENTS)
        monitoring.free_tool_id(4)


def test_returning_disable_stops_callbacks_for_a_code_location() -> None:
    monitoring = sys.monitoring
    calls = {"disabling": 0, "passive": 0}

    def work() -> int:
        return 1

    def run(mode: str, ret: object) -> None:
        def callback(*_: object) -> object:
            calls[mode] += 1
            return ret

        monitoring.use_tool_id(5, "lab-disable-count")
        try:
            monitoring.register_callback(5, monitoring.events.PY_START, callback)
            monitoring.set_events(5, monitoring.events.PY_START)
            for _ in range(200):
                work()
        finally:
            monitoring.set_events(5, monitoring.events.NO_EVENTS)
            monitoring.free_tool_id(5)
            monitoring.restart_events()

    run("passive", None)
    run("disabling", monitoring.DISABLE)
    assert calls["passive"] >= 200
    assert calls["disabling"] < 20


def test_cprofile_cannot_run_while_a_meter_holds_profiler_id_but_can_beside_the_default() -> None:
    with TaskStepMeter(tool_id=sys.monitoring.PROFILER_ID):
        clash = cProfile.Profile()
        with pytest.raises(ValueError, match=r"already (in use|active)"):
            clash.enable()
    with TaskStepMeter():
        beside = cProfile.Profile()
        beside.enable()
        beside.disable()


def test_cprofile_charges_awaited_wait_to_the_loop_not_the_handler() -> None:
    async def handler() -> None:
        await asyncio.sleep(0.1)

    view = cprofile_coroutine(handler)
    assert view.wall_s >= 0.09
    assert view.cumulative_s["handler"] < 0.25 * view.wall_s
    assert view.calls["handler"] == 2
    waiting = max(view.cumulative_s.get(n, 0.0) for n in ("control", "poll", "select"))
    assert waiting >= 0.6 * view.wall_s


@pytest.mark.asyncio
async def test_await_chain_names_what_a_suspended_task_waits_on() -> None:
    async def inner() -> None:
        await asyncio.sleep(10)

    async def outer() -> None:
        await inner()

    task = asyncio.ensure_future(outer())
    await asyncio.sleep(0.01)
    chain = await_chain(task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [name.rsplit(".", 1)[-1] for name in chain] == [
        "outer",
        "inner",
        "sleep",
        "<FutureIter>",
    ]


def blocking_handler() -> None:
    time.sleep(0.2)


@pytest.mark.asyncio
async def test_in_loop_sampler_is_blind_to_blocking_but_thread_sampler_is_not() -> None:
    with ThreadSampler(threading.get_ident()) as thread_sampler:
        in_loop = asyncio.ensure_future(sample_in_loop(0.005, 0.5))
        await asyncio.sleep(0.05)
        blocking_handler()
        _counts, largest_gap = await in_loop
    assert largest_gap >= 0.18
    assert thread_sampler.counts["blocking_handler"] >= 20


def append_chunk(sink: list[bytearray]) -> None:
    sink.append(bytearray(65536))


def leaky_handler(sink: list[bytearray]) -> None:
    append_chunk(sink)


@pytest.mark.usefixtures("tracing")
def test_folded_stacks_are_root_first_and_keep_the_call_path() -> None:
    sink: list[bytearray] = []
    leaky_handler(sink)
    stacks = folded_allocation_stacks(tracemalloc.take_snapshot(), min_bytes=65536)
    alloc_line = append_chunk.__code__.co_firstlineno + 1
    call_line = leaky_handler.__code__.co_firstlineno + 1
    suffix = f"{__file__}:{call_line};{__file__}:{alloc_line}"
    matching = [s for s in stacks if s.rpartition(" ")[0].endswith(suffix)]
    assert matching
    assert int(matching[0].rpartition(" ")[2]) >= 65536


def test_one_frame_tracebacks_lose_the_call_path() -> None:
    tracemalloc.start(1)
    try:
        sink: list[bytearray] = []
        leaky_handler(sink)
        stacks = folded_allocation_stacks(tracemalloc.take_snapshot(), min_bytes=65536)
    finally:
        tracemalloc.stop()
    alloc_line = append_chunk.__code__.co_firstlineno + 1
    assert [s.rpartition(" ")[0] for s in stacks] == [f"{__file__}:{alloc_line}"]


@pytest.mark.usefixtures("tracing")
def test_measure_call_reports_call_local_peak_and_retained() -> None:
    warmup = bytearray(4 << 20)
    del warmup

    def transient() -> int:
        scratch = bytearray(1 << 20)
        del scratch
        return 1

    kept: list[bytearray] = []

    def retained() -> int:
        kept.append(bytearray(1 << 20))
        return 1

    _, transient_retained, transient_peak = measure_call(transient)
    assert transient_retained < 64 * 1024
    assert (1 << 20) <= transient_peak < (2 << 20)
    _, kept_retained, kept_peak = measure_call(retained)
    assert kept_retained >= 1 << 20
    assert kept_peak >= kept_retained
