"""Lab: sibling cancellation, cleanup, and except* handling in AnyIO and asyncio task groups."""

from __future__ import annotations

import asyncio
import sys

import pytest

from api_labs.f12_asgi_servers_and_async_runtime.structured_concurrency import (
    QuotaError,
    UpstreamError,
    anyio_handle_statuses,
    anyio_nested_groups,
    anyio_outer_deadline,
    anyio_sibling_cancellation,
    anyio_start_then_cancel,
    anyio_two_failures,
    asyncio_body_raises,
    asyncio_first_result_wins,
    asyncio_outer_timeout,
    asyncio_sibling_cancellation,
    leaves,
    outside_cancel_racing_child_failure,
    wrap_naked,
)


def _before(events: tuple[str, ...], first: str, then: str) -> bool:
    return events.index(first) < events.index(then)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anyio_failure_cancels_siblings_and_body_before_owner_resumes() -> None:
    report = await anyio_sibling_cancellation(shielded_cleanup=True)
    for name in ("a", "b", "body"):
        assert _before(report.events, "f:raise", f"{name}:cancelled")
        assert _before(report.events, f"{name}:cancelled", "owner:after-group")
    assert report.handled == ("UpstreamError",)
    assert report.parent_cancelling == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anyio_shielded_cleanup_completes_before_group_exit() -> None:
    report = await anyio_sibling_cancellation(shielded_cleanup=True)
    for name in ("a", "b"):
        assert _before(report.events, f"{name}:cleanup-done", "owner:after-group")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anyio_unshielded_cleanup_await_is_cancelled_again() -> None:
    report = await anyio_sibling_cancellation(shielded_cleanup=False)
    assert "a:cancelled" in report.events
    assert "a:cleanup-done" not in report.events
    assert "b:cleanup-done" not in report.events
    assert report.handled == ("UpstreamError",)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asyncio_failure_cancels_siblings_and_cleanup_await_succeeds() -> None:
    report = await asyncio_sibling_cancellation()
    for name in ("a", "b"):
        assert _before(report.events, "f:raise", f"{name}:cancelled")
        # Edge cancellation: one CancelledError per cancel(), so the finally-await runs.
        assert _before(report.events, f"{name}:cleanup-done", "owner:after-group")
    assert _before(report.events, "body:cancelled", "owner:after-group")
    assert report.handled == ("UpstreamError",)
    assert report.parent_cancelling == 0  # TaskGroup called uncancel() on its parent


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_simultaneous_failures_are_split_across_except_star_clauses() -> None:
    report = await anyio_two_failures()
    assert report.handled == ("quota-clause:QuotaError", "upstream-clause:UpstreamError")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nested_group_is_two_levels_deep_and_except_star_still_matches() -> None:
    depth, report = await anyio_nested_groups()
    assert depth == 2
    assert report.handled == ("UpstreamError",)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anyio_task_handles_record_each_child_outcome() -> None:
    statuses = await anyio_handle_statuses()
    assert statuses == {
        "finished": "FINISHED",
        "sibling": "CANCELLED",
        "failer": "FAILED",
        "finished.return_value": "42",
        "await failer": "TaskFailed from UpstreamError",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anyio_start_returns_ready_value_and_cancel_exits_cleanly() -> None:
    report = await anyio_start_then_cancel(fail_before_ready=False)
    assert report.events == (
        "service:init",
        "service:ready",
        "owner:ready on 8080",
        "owner:after-group",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anyio_start_failure_before_ready_raises_bare_exception_in_caller() -> None:
    report = await anyio_start_then_cancel(fail_before_ready=True)
    assert report.events == (
        "service:init",
        "owner:start-raised UpstreamError",
        "owner:after-group",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asyncio_body_exception_is_grouped_and_child_is_cancelled() -> None:
    report = await asyncio_body_raises()
    assert _before(report.events, "body:raise", "a:cancelled")
    assert report.handled == ("QuotaError",)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asyncio_sentinel_recipe_ends_group_early_and_cancels_losers() -> None:
    winner, report = await asyncio_first_result_wins()
    assert winner == "fast"
    assert _before(report.events, "fast:won", "slow:cancelled")
    assert report.events[-1] == "owner:terminated"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asyncio_parent_cancel_count_leaks_on_3_12_when_child_fails_after_body() -> None:
    # The body has already reached __aexit__ when the child fails. CPython 3.12 calls
    # uncancel() before the wait loop, so the cancel() it issues later is never undone;
    # gh-116720 (3.13) moved uncancel() after the loop.
    _, report = await asyncio_first_result_wins()
    assert report.parent_cancelling == (1 if sys.version_info < (3, 13) else 0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asyncio_outside_cancel_racing_child_failure_survives_from_3_13() -> None:
    log, cancelled = await outside_cancel_racing_child_failure("asyncio")
    if sys.version_info >= (3, 13):
        assert log == ("handled", "cancelled-at-next-await", "owner:victim-cancelled")
        assert cancelled is True
    else:
        assert log == ("handled", "continued with cancelling()=1")
        assert cancelled is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anyio_4_16_drops_outside_cancel_racing_child_failure() -> None:
    # AnyIO skips delivering to a host whose waiter is already cancelled, then raises the
    # child's ExceptionGroup in place of the native CancelledError and never re-arms it.
    log, cancelled = await outside_cancel_racing_child_failure("anyio")
    assert log == ("handled", "continued with cancelling()=1")
    assert cancelled is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_outer_deadline_surfaces_as_bare_timeout_error_on_both_runtimes() -> None:
    anyio_outcome, anyio_report = await anyio_outer_deadline()
    asyncio_outcome, asyncio_report = await asyncio_outer_timeout()
    assert (anyio_outcome, asyncio_outcome) == ("TimeoutError", "TimeoutError")
    assert "a:cancelled" in anyio_report.events
    assert "a:cancelled" in asyncio_report.events
    assert anyio_report.parent_cancelling == asyncio_report.parent_cancelling == 0


@pytest.mark.unit
def test_except_star_wraps_a_naked_exception_in_an_exception_group() -> None:
    group = wrap_naked(UpstreamError("x"))
    assert type(group) is ExceptionGroup
    assert group.message == ""
    assert [type(e) for e in leaves(group)] == [UpstreamError]


@pytest.mark.unit
def test_base_exception_group_constructor_returns_exception_group_for_exceptions() -> None:
    plain = BaseExceptionGroup("g", [UpstreamError("u"), QuotaError("q")])
    mixed = BaseExceptionGroup("g", [UpstreamError("u"), asyncio.CancelledError()])
    assert type(plain) is ExceptionGroup
    assert type(mixed) is BaseExceptionGroup
    match, rest = mixed.split(UpstreamError)
    assert match is not None
    assert rest is not None
    assert [type(e) for e in leaves(match)] == [UpstreamError]
    assert type(rest) is BaseExceptionGroup


@pytest.mark.unit
def test_asyncio_taskgroup_cancel_method_exists_only_from_3_15() -> None:
    assert hasattr(asyncio.TaskGroup, "cancel") is (sys.version_info >= (3, 15))
