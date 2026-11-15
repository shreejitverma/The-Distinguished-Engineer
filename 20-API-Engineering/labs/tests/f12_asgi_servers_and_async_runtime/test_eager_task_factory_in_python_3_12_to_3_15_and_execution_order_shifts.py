"""Unit tests for asyncio.eager_task_factory in CPython 3.12-3.15.

Tests synchronous first-step coroutine execution, throughput gains from cache hits,
ordering inversions, lock interleaving, cancellation discrepancies, recursion limits,
and ContextVar isolation.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import MutableMapping

from api_labs.f12_asgi_servers_and_async_runtime.eager_task_execution import (
    CancellationProbe,
    ContextVarIsolationProbe,
    EagerCacheGateway,
    ExecutionTracer,
    LockInterleavingProbe,
    RecursionDepthProbe,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lazy_vs_eager_execution_order_shift() -> None:
    """Proves that eager task factory runs the coroutine immediately inside create_task."""
    tracer = ExecutionTracer()

    # Default lazy scheduling: coroutine waits in ready queue until caller yields
    lazy_events = await tracer.trace_lazy_vs_eager(use_eager=False, yield_in_coro=False)
    assert lazy_events == [
        "caller:before_create",
        "caller:after_create:done=False",
        "caller:before_await",
        "task1:start",
        "task1:end",
        "caller:after_await",
    ]

    # Eager task factory: coroutine runs synchronously inside create_task()
    eager_events = await tracer.trace_lazy_vs_eager(use_eager=True, yield_in_coro=False)
    assert eager_events == [
        "caller:before_create",
        "task1:start",
        "task1:end",
        "caller:after_create:done=True",
        "caller:before_await",
        "caller:after_await",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_eager_task_yielding_falls_back_to_loop_ready_queue() -> None:
    """Proves that a coroutine that blocks on await yields control and graduates to pending."""
    tracer = ExecutionTracer()
    events = await tracer.trace_lazy_vs_eager(use_eager=True, yield_in_coro=True)

    assert events == [
        "caller:before_create",
        "task1:start",
        "caller:after_create:done=False",
        "caller:before_await",
        "task1:end",
        "caller:after_await",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_eager_asgi_cache_gateway_hit_vs_miss() -> None:
    """Demonstrates zero-hop synchronous ASGI execution on cache hits versus misses."""
    gateway = EagerCacheGateway()
    gateway.set_cache("/cached-resource", b'{"status":"ok","source":"cache"}')

    scope_hit: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/cached-resource",
        "raw_path": b"/cached-resource",
        "query_string": b"",
        "headers": [],
    }
    scope_miss: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/uncached-resource",
        "raw_path": b"/uncached-resource",
        "query_string": b"",
        "headers": [],
    }

    async def dummy_receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    sent_messages: list[dict[str, Any]] = []

    async def dummy_send(message: MutableMapping[str, Any]) -> None:
        sent_messages.append(dict(message))

    # 1. Cache hit under eager_task_factory finishes synchronously inside create_task
    task_hit, was_eager_done = await gateway.handle_request_with_probe(
        scope_hit,
        dummy_receive,
        dummy_send,
        use_eager=True,
    )
    assert was_eager_done is True
    assert task_hit.done() is True
    assert gateway.cache_hits == 1

    # 2. Cache miss under eager_task_factory blocks on fetch_upstream, so not done eagerly
    task_miss, was_miss_eager_done = await gateway.handle_request_with_probe(
        scope_miss,
        dummy_receive,
        dummy_send,
        use_eager=True,
    )
    assert was_miss_eager_done is False
    assert task_miss.done() is True
    assert gateway.cache_misses == 1

    # 3. Verify standard ASGI client transport interoperability
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway),
        base_url="http://test",
    ) as client:
        resp_hit = await client.get("/cached-resource")
        assert resp_hit.status_code == 200
        assert resp_hit.json() == {"status": "ok", "source": "cache"}

        resp_miss = await client.get("/uncached-resource")
        assert resp_miss.status_code == 200
        assert resp_miss.text == "upstream_data_for_/uncached-resource"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lock_interleaving_shift_under_eager_factory() -> None:
    """Proves that eager tasks acquire and release an uncontended lock synchronously."""
    lazy_res = await LockInterleavingProbe.run_unlocked_probe(use_eager=False)
    assert lazy_res.t1_done_immediate is False
    assert lazy_res.t2_done_immediate is False
    assert lazy_res.events == [
        "main:create_t1",
        "main:create_t2",
        "main:await_tasks",
        "t1:acquire_attempt",
        "t1:in_lock",
        "t1:released",
        "t2:acquire_attempt",
        "t2:in_lock",
        "t2:released",
        "main:done",
    ]

    eager_res = await LockInterleavingProbe.run_unlocked_probe(use_eager=True)
    assert eager_res.t1_done_immediate is True
    assert eager_res.t2_done_immediate is True
    assert eager_res.events == [
        "main:create_t1",
        "t1:acquire_attempt",
        "t1:in_lock",
        "t1:released",
        "main:create_t2",
        "t2:acquire_attempt",
        "t2:in_lock",
        "t2:released",
        "main:await_tasks",
        "main:done",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancellation_semantics_difference() -> None:
    """Proves that t.cancel() on an eager completed task returns False and is a no-op."""
    # Under lazy factory, cancellation succeeds before coroutine runs
    cancel_lazy, is_cancelled_lazy, result_lazy = await CancellationProbe.cancel_synchronous_task(
        use_eager=False
    )
    assert cancel_lazy is True
    assert is_cancelled_lazy is False
    assert result_lazy == "caught_cancelled_error"

    # Under eager factory, task already executed synchronously, so cancel() fails
    (
        cancel_eager,
        is_cancelled_eager,
        result_eager,
    ) = await CancellationProbe.cancel_synchronous_task(use_eager=True)
    assert cancel_eager is False
    assert is_cancelled_eager is False
    assert result_eager == "completed_work"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recursion_limit_exhaustion_under_eager_chain() -> None:
    """Proves that chaining eager synchronous tasks exhausts Python stack depth."""
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        # Lazy task scheduling unwinds to the event loop each step, avoiding recursion error
        lazy_result = await RecursionDepthProbe.spawn_recursive_chain(250, use_eager=False)
        assert lazy_result == 250

        # Eager scheduling keeps stack frames on the caller stack, raising RecursionError
        with pytest.raises(RecursionError):
            await RecursionDepthProbe.spawn_recursive_chain(250, use_eager=True)
    finally:
        sys.setrecursionlimit(old_limit)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_var_isolation_preserved() -> None:
    """Verifies that eager execution runs in a cloned context and does not leak mutations."""
    parent_val, child_val = await ContextVarIsolationProbe.run_context_probe(use_eager=True)
    assert parent_val == "parent_context_tag"
    assert child_val == "child_mutated_tag"


@pytest.mark.unit
@pytest.mark.skipif(
    sys.version_info < (3, 14),
    reason="only the 3.14+ factory accepts eager_start; 3.13 raises TypeError, 3.12 never sees it",
)
def test_uvloop_eager_start_none_disables_eager_factory() -> None:
    """Proves uvloop passes eager_start=None, which overrides the factory default of True."""
    uvloop = pytest.importorskip("uvloop")

    async def body() -> int:
        return 1

    async def probe() -> tuple[bool, bool]:
        loop = asyncio.get_running_loop()
        loop.set_task_factory(asyncio.eager_task_factory)
        implicit = asyncio.create_task(body())
        explicit = asyncio.create_task(body(), eager_start=True)
        observed = (implicit.done(), explicit.done())
        await asyncio.gather(implicit, explicit)
        return observed

    assert uvloop.run(probe()) == (False, True)
    # The stock asyncio loop honors the factory default, so the same probe runs eagerly.
    assert asyncio.run(probe()) == (True, True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asyncio_gather_eager_completion() -> None:
    """Demonstrates that asyncio.gather completes synchronously when all children are eager."""
    loop = asyncio.get_running_loop()
    old_factory = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)

    async def quick(val: int) -> int:
        return val * 10

    try:
        gather_future = asyncio.gather(quick(1), quick(2), quick(3))
        # In Python 3.12-3.15, gathering all eager futures marks outer future done immediately
        assert gather_future.done() is True
        assert list(gather_future.result()) == [10, 20, 30]
    finally:
        loop.set_task_factory(old_factory)
