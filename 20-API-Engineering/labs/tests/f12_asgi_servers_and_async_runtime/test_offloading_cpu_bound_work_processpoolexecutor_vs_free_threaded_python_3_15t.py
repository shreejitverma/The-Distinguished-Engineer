"""Inline vs thread vs process vs free-threaded offload of CPU-bound work behind ASGI."""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import functools
import multiprocessing
import os
import pickle
import sys
import threading
import time
from typing import TYPE_CHECKING

import anyio
import anyio.to_thread
import httpx
import pytest

from api_labs.f12_asgi_servers_and_async_runtime.cpu_offload import (
    best_of,
    build_app,
    crash_worker,
    hash_buffer,
    make_payload,
    measure_stall,
    nap,
    offload,
    run_in_threads,
    runtime_info,
    spawn_pool,
    spin,
    transform_and_hash,
    worker_pid,
)

if TYPE_CHECKING:
    from collections.abc import Callable

SPIN_N = 3_000_000
WORKERS = 4
PAYLOAD = make_payload(2000)


@pytest.mark.unit
def test_runtime_info_reports_build_flag_and_live_gil_state() -> None:
    info = runtime_info()
    assert info.free_threaded_build == ("free-threading build" in sys.version)
    if not info.free_threaded_build:
        assert info.gil_enabled
    assert info.switch_interval_s == pytest.approx(0.005)
    expected = "spawn" if sys.platform == "darwin" else "fork"
    if sys.platform != "darwin" and sys.version_info >= (3, 14):
        expected = "forkserver"
    assert info.default_start_method == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_strategies_return_the_same_digest() -> None:
    expected = transform_and_hash(PAYLOAD)
    with spawn_pool(1) as pool:
        got = {
            strategy: await offload(strategy, transform_and_hash, PAYLOAD, executor=pool)
            for strategy in ("inline", "thread", "process")
        }
    assert set(got.values()) == {expected}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inline_cpu_work_stalls_the_loop_and_thread_offload_does_not() -> None:
    big = make_payload(20_000)

    async def inline() -> str:
        return transform_and_hash(big)

    async def threaded() -> str:
        return await asyncio.to_thread(transform_and_hash, big)

    blocked = await measure_stall(inline)
    offloaded = await measure_stall(threaded)
    assert blocked.result == offloaded.result
    assert blocked.max_gap_s > 0.02
    assert offloaded.max_gap_s < blocked.max_gap_s / 2
    assert offloaded.ticks > blocked.ticks


def thread_parallelism(fn: Callable[[], object]) -> float:
    return run_in_threads(fn, WORKERS).cpu_parallelism


@pytest.mark.unit
def test_pure_python_threads_use_more_than_one_core_only_without_the_gil() -> None:
    work = functools.partial(spin, SPIN_N)
    if runtime_info().gil_enabled:
        assert thread_parallelism(work) < 1.5
    else:
        assert best_of(lambda: thread_parallelism(work), at_least=2.0) > 2.0


@pytest.mark.unit
def test_hashlib_updates_of_at_least_2048_bytes_run_in_parallel_on_every_build() -> None:
    work = functools.partial(hash_buffer, 1 << 20, 256 << 20)
    assert best_of(lambda: thread_parallelism(work), at_least=2.0) > 2.0


@pytest.mark.unit
def test_hashlib_updates_under_2048_bytes_serialize_on_a_gil_build() -> None:
    work = functools.partial(hash_buffer, 1024, 48 << 20)
    if runtime_info().gil_enabled:
        assert thread_parallelism(work) < 1.5
    else:
        assert best_of(lambda: thread_parallelism(work), at_least=2.0) > 2.0


@pytest.mark.unit
def test_process_pool_runs_pure_python_in_parallel_on_every_build() -> None:
    results: set[int] = set()
    with spawn_pool(WORKERS) as pool:
        list(pool.map(nap, [0.3] * WORKERS))

        def speedup() -> float:
            t0 = time.perf_counter()
            results.add(spin(SPIN_N))
            serial = time.perf_counter() - t0
            t0 = time.perf_counter()
            results.update(pool.map(spin, [SPIN_N] * WORKERS))
            return (serial * WORKERS) / (time.perf_counter() - t0)

        assert best_of(speedup, at_least=2.0) > 2.0
    assert len(results) == 1


@pytest.mark.unit
def test_lambda_submit_succeeds_but_result_raises_a_pickling_error() -> None:
    with spawn_pool(1) as pool:
        future = pool.submit(lambda: 1)
        with pytest.raises((pickle.PicklingError, AttributeError)):
            future.result(timeout=30)
        assert pool.submit(worker_pid).result(timeout=30) != os.getpid()


@pytest.mark.unit
def test_one_crashed_worker_breaks_the_whole_pool() -> None:
    pool = spawn_pool(2)
    try:
        assert pool.submit(nap, 0.0).result(timeout=30) == 0.0
        with pytest.raises(cf.process.BrokenProcessPool):
            pool.submit(crash_worker).result(timeout=30)
        with pytest.raises(cf.process.BrokenProcessPool):
            pool.submit(nap, 0.0)
    finally:
        pool.shutdown(wait=True)


@pytest.mark.unit
def test_max_tasks_per_child_recycles_worker_processes() -> None:
    with spawn_pool(1, max_tasks_per_child=1) as recycled:
        recycled_pids = {recycled.submit(worker_pid).result(timeout=30) for _ in range(3)}
    with spawn_pool(1) as kept:
        kept_pids = {kept.submit(worker_pid).result(timeout=30) for _ in range(3)}
    assert len(recycled_pids) == 3
    assert len(kept_pids) == 1


@pytest.mark.unit
def test_max_tasks_per_child_is_rejected_with_the_fork_start_method() -> None:
    with pytest.raises(ValueError, match="incompatible with the 'fork'"):
        cf.ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("fork"), max_tasks_per_child=1
        )


@pytest.mark.unit
def test_a_running_process_future_cannot_be_cancelled_but_a_waiting_one_can() -> None:
    with spawn_pool(1) as pool:
        futures = [pool.submit(nap, 0.5) for _ in range(6)]
        deadline = time.monotonic() + 30
        while not futures[0].running() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert futures[0].cancel() is False
        assert futures[-1].cancel() is True
        pool.shutdown(wait=True, cancel_futures=True)
    assert futures[0].result() == 0.5
    assert futures[-1].cancelled()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_for_timeout_does_not_stop_the_worker_thread() -> None:
    release = threading.Event()
    finished = threading.Event()

    def work() -> None:
        release.wait(timeout=10)
        finished.set()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.to_thread(work), timeout=0.05)
    assert not finished.is_set()
    release.set()
    assert await asyncio.to_thread(finished.wait, 10)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anyio_run_sync_shields_the_thread_and_swallows_a_missed_deadline() -> None:
    t0 = time.perf_counter()
    with anyio.fail_after(0.05) as shielded:
        await anyio.to_thread.run_sync(time.sleep, 0.4)
    assert time.perf_counter() - t0 >= 0.4
    assert shielded.cancel_called
    assert not shielded.cancelled_caught

    t0 = time.perf_counter()
    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.05):
            await anyio.to_thread.run_sync(time.sleep, 0.4, abandon_on_cancel=True)
    assert time.perf_counter() - t0 < 0.3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anyio_default_thread_limiter_has_40_tokens() -> None:
    assert anyio.to_thread.current_default_thread_limiter().total_tokens == 40


@pytest.mark.unit
def test_default_thread_pool_size_is_cpu_count_plus_4_capped_at_32() -> None:
    cpus = os.process_cpu_count() if sys.version_info >= (3, 13) else os.cpu_count()
    with cf.ThreadPoolExecutor() as pool:
        assert getattr(pool, "_max_workers") == min(32, (cpus or 1) + 4)  # noqa: B009


@pytest.mark.unit
def test_set_default_executor_rejects_a_process_pool() -> None:
    loop = asyncio.new_event_loop()
    pool = spawn_pool(1)
    try:
        with pytest.raises(TypeError, match="ThreadPoolExecutor"):
            loop.set_default_executor(pool)
    finally:
        pool.shutdown(wait=True)
        loop.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_admission_gate_sheds_load_with_503_and_retry_after() -> None:
    release = threading.Event()
    started = threading.Semaphore(0)

    def gated(payload: bytes) -> str:
        started.release()
        release.wait(timeout=10)
        return transform_and_hash(payload)

    service = build_app(
        strategy="thread", work=gated, max_in_flight=2, limiter=anyio.CapacityLimiter(2)
    )
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = asyncio.create_task(client.post("/digest", content=PAYLOAD))
        second = asyncio.create_task(client.post("/digest", content=PAYLOAD))
        for _ in range(2):
            assert await anyio.to_thread.run_sync(started.acquire, True, 10)
        assert service.gate.in_flight == 2
        shed = await client.post("/digest", content=PAYLOAD)
        release.set()
        ok = await asyncio.gather(first, second)
    assert shed.status_code == 503
    assert shed.headers["retry-after"] == "1"
    assert [r.status_code for r in ok] == [200, 200]
    assert {r.json()["sha256"] for r in ok} == {transform_and_hash(PAYLOAD)}
    assert service.gate.in_flight == 0
    assert service.gate.rejected == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oversized_body_is_rejected_with_413_and_frees_its_slot() -> None:
    service = build_app(
        strategy="inline", work=transform_and_hash, max_in_flight=1, max_body_bytes=1024
    )
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        big = await client.post("/digest", content=PAYLOAD)
        small = await client.post("/digest", content=make_payload(2))
    assert big.status_code == 413
    assert small.status_code == 200
    assert service.gate.in_flight == 0


@pytest.mark.unit
def test_interpreter_pool_executor_exists_from_3_14_and_runs_on_separate_gils() -> None:
    pool_cls = getattr(cf, "InterpreterPoolExecutor", None)
    assert (pool_cls is not None) == (sys.version_info >= (3, 14))
    if pool_cls is None:
        return
    results: set[int] = set()
    with pool_cls(max_workers=WORKERS) as pool:
        list(pool.map(nap, [0.3] * WORKERS))

        def parallelism() -> float:
            cpu0 = time.process_time()
            wall0 = time.perf_counter()
            results.update(pool.map(spin, [SPIN_N] * WORKERS))
            return (time.process_time() - cpu0) / (time.perf_counter() - wall0)

        assert best_of(parallelism, at_least=2.0) > 2.0
    assert results == {spin(SPIN_N)}
