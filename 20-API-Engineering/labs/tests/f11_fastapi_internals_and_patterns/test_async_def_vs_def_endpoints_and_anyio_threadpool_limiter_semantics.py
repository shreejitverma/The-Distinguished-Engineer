"""Unit tests: async def runs on the loop, def runs behind AnyIO's 40-token CapacityLimiter."""

import threading
from collections.abc import Callable

import anyio
import anyio.to_thread
import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi.concurrency import contextmanager_in_threadpool
from starlette.types import ASGIApp

from api_labs.f11_fastapi_internals_and_patterns.async_def_vs_def_threadpool import (
    ThreadProbe,
    build_threadpool_app,
    cancel_while_in_thread,
    naive_contextmanager_in_threadpool,
    pool_teardown_scenario,
)

WAIT_LIMIT_S = 5.0


async def wait_until(predicate: Callable[[], bool]) -> None:
    with anyio.fail_after(WAIT_LIMIT_S):
        for _ in range(1_000_000):
            if predicate():
                return
            await anyio.sleep(0.001)


def waiting_is(limiter: anyio.CapacityLimiter, count: int) -> Callable[[], bool]:
    return lambda: limiter.statistics().tasks_waiting == count


def client_for(app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_41st_blocking_def_request_queues_while_async_def_keeps_serving() -> None:
    probe = ThreadProbe()
    lab = build_threadpool_app(probe)
    limiter = anyio.to_thread.current_default_thread_limiter()
    assert limiter.total_tokens == 40
    statuses: list[int] = []

    async with client_for(lab.app) as client, anyio.create_task_group() as tg:

        async def fetch(path: str) -> None:
            statuses.append((await client.get(path)).status_code)

        for _ in range(41):
            tg.start_soon(fetch, "/blocking")
        await wait_until(lambda: probe.entered == 40 and limiter.statistics().tasks_waiting == 1)
        assert probe.in_flight == 40
        assert limiter.borrowed_tokens == 40

        ping = (await client.get("/loop-ping")).json()
        assert ping == {"ident": threading.get_ident(), "borrowed": 40, "total": 40, "waiting": 1}

        tg.start_soon(fetch, "/sync-ping")
        await wait_until(lambda: limiter.statistics().tasks_waiting == 2)
        await anyio.sleep(0.05)
        assert "sync-ping" not in probe.events
        assert probe.entered == 40

        probe.gate.set()

    assert statuses == [200] * 42
    assert probe.entered == 41
    assert probe.peak_in_flight == 40
    assert len(probe.worker_idents) == 40
    assert limiter.borrowed_tokens == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_blocking_call_inside_async_def_freezes_the_loop() -> None:
    probe = ThreadProbe()
    lab = build_threadpool_app(probe)

    async with client_for(lab.app) as client, anyio.create_task_group() as tg:

        async def blocker() -> None:
            await client.get("/async-blocker", params={"hold": 0.2})

        async def pinger() -> None:
            await wait_until(lambda: "async-blocker:enter" in probe.events)
            await client.get("/loop-ping")

        tg.start_soon(blocker)
        tg.start_soon(pinger)

    assert probe.events == [
        "async-blocker:enter",
        "async-blocker:exit(released=False)",
        "loop-ping",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_blocking_call_inside_def_leaves_the_loop_free() -> None:
    probe = ThreadProbe()
    lab = build_threadpool_app(probe)

    async with client_for(lab.app) as client, anyio.create_task_group() as tg:

        async def blocker() -> None:
            await client.get("/blocking")

        tg.start_soon(blocker)
        await wait_until(lambda: "blocking:enter" in probe.events)
        await client.get("/loop-ping")
        assert probe.events == ["blocking:enter", "loop-ping"]
        probe.gate.set()

    assert probe.events == ["blocking:enter", "loop-ping", "blocking:exit"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_def_endpoint_dependency_and_response_validation_all_run_off_the_loop() -> None:
    probe = ThreadProbe()
    lab = build_threadpool_app(probe)
    loop_ident = threading.get_ident()

    async with client_for(lab.app) as client:
        for path in ("/where/sync", "/where/async", "/where/mixed"):
            assert (await client.get(path)).status_code == 200

    on_loop = {label: ident == loop_ident for label, ident in probe.where.items()}
    assert on_loop == {
        "sync-dep": False,
        "sync-endpoint": False,
        "response-validation:sync-endpoint": False,
        "async-dep": True,
        "async-endpoint": True,
        "response-validation:async-endpoint": True,
        "mixed-endpoint": True,
        "response-validation:mixed-endpoint": True,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_contextvars_are_copied_into_threads_but_never_copied_back() -> None:
    lab = build_threadpool_app(ThreadProbe())

    bodies: dict[str, object] = {}

    async def get_in_own_task(client: httpx.AsyncClient, path: str) -> None:
        # ASGITransport runs the app in the calling task; a server gives each request its own.
        bodies[path] = (await client.get(path)).json()

    async with client_for(lab.app) as client:
        for path in ("/ctx/into-thread", "/ctx/out-of-thread"):
            async with anyio.create_task_group() as tg:
                tg.start_soon(get_in_own_task, client, path)

    assert bodies == {
        "/ctx/into-thread": {"tag": "set-in-async-dep"},
        "/ctx/out-of-thread": {"tag": "unset"},
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_total_tokens_set_in_lifespan_and_resized_at_runtime() -> None:
    probe = ThreadProbe()
    lab = build_threadpool_app(probe, thread_tokens=2)

    async with (
        LifespanManager(lab.app) as manager,
        client_for(manager.app) as client,
        anyio.create_task_group() as tg,
    ):
        limiter = anyio.to_thread.current_default_thread_limiter()
        assert limiter.total_tokens == 2

        for _ in range(3):
            tg.start_soon(client.get, "/blocking")
        await wait_until(lambda: probe.entered == 2 and limiter.statistics().tasks_waiting == 1)

        limiter.total_tokens = 3  # The setter hands the free token to the waiter synchronously.
        stats = limiter.statistics()
        assert (stats.borrowed_tokens, stats.tasks_waiting) == (3, 0)
        await wait_until(lambda: probe.entered == 3)

        limiter.total_tokens = 1  # Shrinking never preempts a running thread.
        assert limiter.borrowed_tokens == 3
        assert limiter.available_tokens == -2
        tg.start_soon(client.get, "/blocking")
        await wait_until(lambda: limiter.statistics().tasks_waiting == 1)
        assert probe.entered == 3

        probe.gate.set()

    assert probe.entered == 4
    assert probe.peak_in_flight == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dedicated_bulkhead_limiter_does_not_spend_default_tokens() -> None:
    probe = ThreadProbe()
    lab = build_threadpool_app(probe, bulkhead_tokens=2)
    limiter = anyio.to_thread.current_default_thread_limiter()

    async with client_for(lab.app) as client, anyio.create_task_group() as tg:
        for _ in range(3):
            tg.start_soon(client.get, "/report")
        bulkhead = lab.bulkhead
        await wait_until(lambda: probe.entered == 2 and bulkhead.statistics().tasks_waiting == 1)

        assert lab.bulkhead.borrowed_tokens == 2
        assert limiter.borrowed_tokens == 0
        assert (await client.get("/sync-ping")).status_code == 200

        probe.gate.set()

    assert probe.entered == 3
    assert probe.peak_in_flight == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_limiter_hands_tokens_to_waiters_in_fifo_order() -> None:
    limiter = anyio.CapacityLimiter(1)
    order: list[int] = []
    await limiter.acquire()

    async with anyio.create_task_group() as tg:

        async def waiter(n: int) -> None:
            async with limiter:
                order.append(n)

        for n in range(5):
            tg.start_soon(waiter, n)
            await wait_until(waiting_is(limiter, n + 1))
        limiter.release()

    assert order == [0, 1, 2, 3, 4]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_sync_shields_the_caller_until_the_thread_finishes() -> None:
    log = await cancel_while_in_thread(abandon_on_cancel=False)

    assert log == [
        "cancel:requested",
        "thread:released",
        "thread:exit",
        "caller:returned(done)",
        "caller:after-scope(cancelled_caught=True)",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_abandon_on_cancel_returns_immediately_and_orphans_the_thread() -> None:
    log = await cancel_while_in_thread(abandon_on_cancel=True)

    assert log == [
        "cancel:requested",
        "caller:after-scope(cancelled_caught=True)",
        "thread:released",
        "thread:exit",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_exit_limiter_lets_teardown_return_a_pooled_connection() -> None:
    log = await pool_teardown_scenario(contextmanager_in_threadpool, pool_timeout=2.0)

    assert log == [
        "a:checked-out",
        "a:endpoint-done",
        "a:returned-connection",
        "b:checkout(acquired=True)",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_teardown_on_the_shared_limiter_starves_until_the_pool_times_out() -> None:
    log = await pool_teardown_scenario(naive_contextmanager_in_threadpool, pool_timeout=0.3)

    assert log == [
        "a:checked-out",
        "a:endpoint-done",
        "b:checkout(acquired=False)",
        "a:returned-connection",
    ]


@pytest.mark.unit
def test_default_limiter_does_not_exist_outside_a_running_loop() -> None:
    with pytest.raises(anyio.NoEventLoopError):
        anyio.to_thread.current_default_thread_limiter()
