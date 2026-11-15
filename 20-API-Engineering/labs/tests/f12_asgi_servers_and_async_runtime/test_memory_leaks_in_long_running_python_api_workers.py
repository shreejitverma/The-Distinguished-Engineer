"""Each leak mechanism is shown leaking and fixed, so every test can fail."""

from __future__ import annotations

import gc
import inspect
import sysconfig
import tracemalloc
from typing import TYPE_CHECKING

import pytest

from api_labs.f12_asgi_servers_and_async_runtime import memory_leaks as ml

if TYPE_CHECKING:
    from collections.abc import Iterator

FREE_THREADED = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


@pytest.fixture(autouse=True)
def empty_caches() -> Iterator[None]:
    ml.LeakyPricer.quote.cache_clear()
    ml.base_price.cache_clear()
    yield
    ml.LeakyPricer.quote.cache_clear()
    ml.base_price.cache_clear()
    gc.collect()


def paths(n: int) -> list[str]:
    return [f"/quote/acme/sku-{i % 8}" for i in range(n)]


def scratch_line(cls: type[object]) -> str:
    lines, start = inspect.getsourcelines(cls.__init__)
    offset = next(i for i, text in enumerate(lines) if "bytearray(SCRATCH_BYTES)" in text)
    return f"memory_leaks.py:{start + offset}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_method_lru_cache_pins_every_request_instance() -> None:
    app = ml.build_pricing_app(ml.LeakyPricer)
    assert await ml.drive(app, paths(50)) == 50
    gc.collect()
    assert len(app.state.created) == 50  # nothing was freed
    assert ml.LeakyPricer.quote.cache_info().currsize == 50  # one key per self


@pytest.mark.unit
@pytest.mark.asyncio
async def test_module_cache_keyed_on_values_frees_instances() -> None:
    app = ml.build_pricing_app(ml.BoundedPricer)
    assert await ml.drive(app, paths(50)) == 50
    gc.collect()
    assert len(app.state.created) == 0
    info = ml.base_price.cache_info()
    assert (info.currsize, info.hits, info.misses) == (8, 42, 8)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tracemalloc_diff_names_the_pinned_allocation_line() -> None:
    tracemalloc.start(1)
    try:
        results: dict[str, list[ml.LineGrowth]] = {}
        for cls in (ml.LeakyPricer, ml.BoundedPricer):
            app = ml.build_pricing_app(cls)
            await ml.drive(app, paths(20))  # warm up: imports, route compile, cache fill
            gc.collect()
            before = tracemalloc.take_snapshot()
            await ml.drive(app, paths(200))
            gc.collect()
            after = tracemalloc.take_snapshot()
            results[cls.__name__] = ml.growth_by_line(before, after)
    finally:
        tracemalloc.stop()

    leaky, bounded = results["LeakyPricer"], results["BoundedPricer"]
    for name, growth in results.items():
        print(name, [(g.location, g.size_diff, g.count_diff) for g in growth[:3]])
    top = leaky[0]
    assert top.location == scratch_line(ml.LeakyPricer)
    assert top.count_diff == 2 * 200  # per request: the bytearray object and its buffer
    assert top.size_diff >= 200 * ml.SCRATCH_BYTES
    assert sum(g.size_diff for g in bounded) < ml.SCRATCH_BYTES


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpx_response_cycle_survives_del_until_collected() -> None:
    app = ml.build_pricing_app(ml.BoundedPricer)
    with ml.collector_paused():
        survival = await ml.httpx_response_survival(app, break_cycle=False)
    assert survival.after_del is True
    assert survival.after_collect is False
    assert survival.unreachable_found > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_breaking_the_stream_cycle_frees_response_by_refcount() -> None:
    app = ml.build_pricing_app(ml.BoundedPricer)
    with ml.collector_paused():
        survival = await ml.httpx_response_survival(app, break_cycle=True)
    assert survival.after_del is False
    assert survival.after_collect is False


@pytest.mark.unit
def test_stored_exception_keeps_context_alive_through_its_traceback() -> None:
    with ml.collector_paused():
        kept = ml.handle_with_stored_exception(keep_traceback=True)
        text_only = ml.handle_with_stored_exception(keep_traceback=False)
        assert kept() is not None  # cycle: ctx -> exc -> traceback -> frame -> ctx
        assert text_only() is None  # freed by refcount on return
        gc.collect()
        assert kept() is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_abandoned_async_generator_holds_lease_for_two_loop_turns() -> None:
    pool = ml.LeasePool()
    assert await ml.first_row_abandoned(pool) == 0
    assert pool.in_use == 1  # finally has not run yet
    # turn 1 runs create_task(agen.aclose()); turn 2 runs the aclose() task
    assert await ml.loop_turns_until_released(pool, limit=10) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_aclosing_releases_lease_before_returning() -> None:
    pool = ml.LeasePool()
    assert await ml.first_row_aclosing(pool) == 0
    assert pool.in_use == 0
    assert await ml.loop_turns_until_released(pool, limit=10) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cycle_held_generator_keeps_lease_until_the_collector_runs() -> None:
    pool = ml.LeasePool()
    with ml.collector_paused():
        assert await ml.first_row_from_cursor(pool) == 0
        assert await ml.loop_turns_until_released(pool, limit=50) is None
        gc.collect()  # its return value counts this resurrecting cycle differently per build
        assert pool.in_use == 1  # collection only schedules aclose()
        assert await ml.loop_turns_until_released(pool, limit=10) == 2


@pytest.mark.unit
def test_sparse_survivors_pin_pymalloc_arenas() -> None:
    assert not tracemalloc.is_tracing()
    report = ml.fragment()
    if FREE_THREADED:
        # mimalloc replaces pymalloc: there are no arenas to report
        assert report is None
        assert ml.pymalloc_arena_bytes() is None
        return
    assert report is not None
    assert report.blocks_sparse * 10 < report.blocks_peak  # over 90% of blocks freed
    assert report.arena_bytes_sparse * 10 >= report.arena_bytes_peak * 9  # arenas kept
    assert report.arena_bytes_freed * 4 < report.arena_bytes_peak  # all freed -> returned
    print(report)


@pytest.mark.unit
def test_tracemalloc_hooks_hide_pymalloc_stats() -> None:
    tracemalloc.start()
    try:
        assert ml.pymalloc_arena_bytes() is None
    finally:
        tracemalloc.stop()
    assert (ml.pymalloc_arena_bytes() is None) is FREE_THREADED
