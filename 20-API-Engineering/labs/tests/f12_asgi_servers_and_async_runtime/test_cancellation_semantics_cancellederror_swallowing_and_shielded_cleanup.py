"""Lab for Cancellation-Semantics-CancelledError-Swallowing-and-Shielded-Cleanup."""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys

import httpx
import pytest

from api_labs.f12_asgi_servers_and_async_runtime.cancellation_semantics import (
    anyio_scope_cancel_during_cleanup,
    build_app,
    call_with_deadline,
    cancel_before_first_step,
    coalesced_cancel_requests,
    double_cancel_during_cleanup,
    shielded_failure_after_outer_cancel,
    swallow_and_return,
    swallowed_cancel_then_task_group,
    uncancel_before_first_step,
)

# gh-86802 (PR #134331, in 3.14.0) logged eagerly; gh-156321 (PR #158590) reverted to
# reporting at garbage collection. First releases with the revert, from the tags:
# v3.14.9 (backport #158599) and v3.15.1 (backport #158598).
SHIELD_LOGS_EAGERLY = (3, 14) <= sys.version_info[:3] < (3, 14, 9) or (
    3,
    15,
) <= sys.version_info[:3] < (3, 15, 1)


@pytest.mark.unit
def test_cancelled_error_is_a_base_exception_not_an_exception() -> None:
    assert issubclass(asyncio.CancelledError, BaseException)
    assert not issubclass(asyncio.CancelledError, Exception)
    assert not issubclass(asyncio.CancelledError, concurrent.futures.CancelledError)
    assert issubclass(concurrent.futures.CancelledError, Exception)
    assert asyncio.TimeoutError is TimeoutError


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_cancels_count_twice_but_deliver_one_error_with_the_first_message() -> None:
    report = await coalesced_cancel_requests(("deadline", "shutdown"))
    assert report.returned == (True, True)
    assert report.deliveries == 1
    assert report.message == "deadline"
    assert report.cancelling_after == 2
    assert report.task_cancelled is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_before_first_step_skips_the_body_and_its_finally() -> None:
    body_ran, finally_ran, cancelled = await cancel_before_first_step()
    assert (body_ran, finally_ran, cancelled) == (False, False, True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uncancel_to_zero_rescinds_the_pending_throw_from_3_13() -> None:
    body_ran = await uncancel_before_first_step()
    assert body_ran is (sys.version_info >= (3, 13))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_swallowed_cancel_completes_the_task_and_leaks_the_counter() -> None:
    result, cancelled, cancelling = await swallow_and_return()
    assert (result, cancelled, cancelling) == ("value", False, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_except_exception_lets_asyncio_timeout_fire() -> None:
    report = await call_with_deadline("except-exception")
    assert report.outcome == "timeout"
    assert report.calls == ()
    assert report.cancelling_after == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_except_baseexception_swallows_the_deadline_and_keeps_calling_upstream() -> None:
    report = await call_with_deadline("except-baseexception")
    assert report.outcome == "completed"
    assert report.calls == ("swallowed:0", "ok:1", "ok:2")
    assert report.cancelling_after == 0  # timeout's __aexit__ still uncancel()ed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_except_baseexception_with_reraise_keeps_the_deadline() -> None:
    report = await call_with_deadline("except-baseexception-reraise")
    assert report.outcome == "timeout"
    assert report.calls == ("cleanup:0",)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "status", "calls"),
    [
        ("except-exception", 504, []),
        ("except-baseexception", 200, ["swallowed:0", "ok:1", "ok:2"]),
        ("except-baseexception-reraise", 504, ["cleanup:0"]),
    ],
)
async def test_asgi_endpoint_turns_a_swallowed_deadline_into_200(
    handler: str, status: int, calls: list[str]
) -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/report", params={"handler": handler})
    assert response.status_code == status
    assert response.json() == {"calls": calls}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_swallowed_outside_cancel_resurfaces_at_a_failing_task_group_from_3_13() -> None:
    report = await swallowed_cancel_then_task_group(uncancel=False)
    assert report.log == ("swallowed", "cancelling=1", "group-handled", "returning")
    if sys.version_info >= (3, 13):
        # TaskGroup re-arms the leaked request with uncancel(); cancel(), so the
        # coroutine's return value is discarded and the task ends cancelled.
        assert (report.task_cancelled, report.result) == (True, None)
    else:
        assert (report.task_cancelled, report.result) == (False, "fallback-response")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uncancel_after_swallowing_keeps_the_task_group_isolated() -> None:
    report = await swallowed_cancel_then_task_group(uncancel=True)
    assert report.log == ("swallowed", "cancelling=0", "group-handled", "returning")
    assert (report.task_cancelled, report.result) == (False, "fallback-response")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bare_await_rollback_is_interrupted_by_a_second_cancel() -> None:
    report = await double_cancel_during_cleanup("bare-await")
    assert report.trace == ("begin", "rollback:start", "release:rolling-back")
    assert report.final_state == "rolling-back"
    assert report.task_cancelled is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anyio_shield_does_not_stop_a_native_task_cancel() -> None:
    report = await double_cancel_during_cleanup("anyio-shield")
    assert report.trace == ("begin", "rollback:start", "release:rolling-back")
    assert report.final_state == "rolling-back"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asyncio_shield_releases_the_connection_before_rollback_finishes() -> None:
    report = await double_cancel_during_cleanup("asyncio-shield")
    assert report.trace == ("begin", "rollback:start", "release:rolling-back", "rollback:done")
    assert report.released == ("rolling-back",)
    assert report.final_state == "clean"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_to_completion_finishes_rollback_then_reraises_cancel() -> None:
    report = await double_cancel_during_cleanup("run-to-completion")
    assert report.trace == ("begin", "rollback:start", "rollback:done", "release:clean")
    assert report.released == ("clean",)
    assert report.task_cancelled is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anyio_level_cancel_interrupts_an_unshielded_rollback() -> None:
    report = await anyio_scope_cancel_during_cleanup("bare-await")
    assert report.trace == ("begin", "rollback:start", "release:rolling-back")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anyio_shielded_scope_lets_rollback_finish() -> None:
    report = await anyio_scope_cancel_during_cleanup("shielded")
    assert report.trace == ("begin", "rollback:start", "rollback:done", "release:clean")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bounded_shield_abandons_a_hung_rollback_and_invalidates() -> None:
    report = await anyio_scope_cancel_during_cleanup("shielded-bounded", rollback_steps=None)
    assert report.trace == ("begin", "rollback:start", "invalidate", "release:invalidated")
    assert report.released == ("invalidated",)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shielded_failure_after_caller_cancel_is_reported_per_version() -> None:
    eager, at_gc = await shielded_failure_after_outer_cancel()
    if SHIELD_LOGS_EAGERLY:
        assert (eager, at_gc) == (("RuntimeError exception in shielded future",), ())
    else:
        assert (eager, at_gc) == ((), ("Task exception was never retrieved",))
