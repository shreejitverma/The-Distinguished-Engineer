"""BaseHTTPMiddleware trap: tasks, loop iterations, streaming, exceptions, ContextVars.

Pinned to starlette 1.10.0 and anyio 4.16.0; exact counts change with either.
"""

from __future__ import annotations

import httpx
import pytest

from api_labs.f10_asgi_and_starlette_internals.basehttp_middleware_trap import (
    REQUEST_USER,
    BaseHTTPContextProbe,
    BaseHTTPPassthrough,
    BodyInspectingMiddleware,
    CatchingMiddleware,
    LegacyQueueMiddleware,
    NaiveSpawningMiddleware,
    PureBodyObserver,
    PureContextProbe,
    PurePassthrough,
    RaiseInDispatch,
    ReadBodyInDispatch,
    background_app,
    count_tasks_created,
    drive,
    echo_body_app,
    failing_app,
    loop_iterations,
    max_lead,
    ok_app,
    set_user_app,
    stack,
    streaming_app,
    trailers_app,
    zerocopy_app,
)


def client_for(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")  # type: ignore[arg-type]


# --- ContextVars -------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_contextvar_set_in_endpoint_does_not_reach_basehttp_dispatch() -> None:
    seen: list[str] = []
    async with client_for(BaseHTTPContextProbe(set_user_app, seen)) as client:
        response = await client.get("/")
    assert response.text == "from-dispatch"  # downward: the child task copied the context
    assert seen == ["var=from-dispatch", "bag=alice", "state=alice"]  # upward: lost


@pytest.mark.unit
@pytest.mark.asyncio
async def test_contextvar_set_in_endpoint_reaches_pure_asgi_middleware() -> None:
    seen: list[str] = []
    async with client_for(PureContextProbe(set_user_app, seen)) as client:
        response = await client.get("/")
    assert response.text == "unset"
    assert seen == ["var=alice"]
    assert REQUEST_USER.get() == "alice"  # same task, same context, all the way up


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basehttp_breaks_propagation_for_pure_middleware_outside_it() -> None:
    outer: list[str] = []
    async with client_for(PureContextProbe(BaseHTTPPassthrough(set_user_app), outer)) as client:
        await client.get("/")
    assert outer == ["var=unset"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pure_middleware_inside_basehttp_shares_the_child_task() -> None:
    inner: list[str] = []
    async with client_for(BaseHTTPPassthrough(PureContextProbe(set_user_app, inner))) as client:
        await client.get("/")
    assert inner == ["var=alice"]


# --- Task and event-loop cost ------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("depth", "expected"), [(0, 0), (1, 1), (3, 3)])
async def test_each_basehttp_layer_spawns_one_task_per_request(depth: int, expected: int) -> None:
    app = stack(ok_app, BaseHTTPPassthrough, depth)
    assert await count_tasks_created(lambda: drive(app)) == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pure_layers_spawn_no_tasks() -> None:
    app = stack(ok_app, PurePassthrough, 5)
    assert await count_tasks_created(lambda: drive(app)) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_every_receive_through_basehttp_spawns_a_watcher_task() -> None:
    assert await count_tasks_created(lambda: drive(PurePassthrough(echo_body_app))) == 0
    assert await count_tasks_created(lambda: drive(BaseHTTPPassthrough(echo_body_app))) == 2


@pytest.mark.unit
def test_basehttp_adds_event_loop_iterations_pure_adds_none() -> None:
    assert loop_iterations(ok_app) == 0
    assert loop_iterations(stack(ok_app, PurePassthrough, 5)) == 0
    assert loop_iterations(stack(ok_app, BaseHTTPPassthrough, 1)) == 8
    assert loop_iterations(stack(ok_app, BaseHTTPPassthrough, 5)) == 36


# --- Streaming and buffering -------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basehttp_passthrough_streams_with_a_rendezvous_not_a_buffer() -> None:
    bare: list[str] = []
    await drive(streaming_app(5, bare), log=bare, client_yields=3)
    wrapped: list[str] = []
    await drive(BaseHTTPPassthrough(streaming_app(5, wrapped)), log=wrapped, client_yields=3)
    assert max_lead(bare) == 1
    assert max_lead(wrapped) == 2  # one chunk held in the hand-off, never the whole body


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_unbounded_queue_lets_the_producer_run_ahead() -> None:
    log: list[str] = []
    await drive(LegacyQueueMiddleware(streaming_app(5, log)), log=log, client_yields=3)
    assert max_lead(log) == 5
    assert log[:5] == [f"produce:{index}" for index in range(5)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_draining_body_iterator_buffers_whole_body_before_first_byte() -> None:
    log: list[str] = []
    sent = await drive(BodyInspectingMiddleware(streaming_app(5, log), log), log=log)
    produced = [f"produce:{index}" for index in range(5)]
    assert log == [*produced, "inspected:5", "deliver:01234", "deliver:final"]
    assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pure_observer_sees_every_byte_without_buffering() -> None:
    log: list[str] = []
    await drive(PureBodyObserver(streaming_app(5, log), log), log=log)
    assert max_lead(log) == 1
    assert log[-2:] == ["inspected:5", "deliver:final"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_body_read_in_dispatch_is_replayed_to_endpoint() -> None:
    log: list[str] = []
    payload = b"x" * 1000
    sent = await drive(ReadBodyInDispatch(echo_body_app, log), body=payload)
    assert log == ["dispatch-read:1000"]
    assert sent[1]["body"] == payload


# --- Exceptions ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_next_reraises_the_endpoint_exception_unwrapped() -> None:
    async with client_for(CatchingMiddleware(failing_app(after_chunks=None))) as client:
        response = await client.get("/")
    assert response.status_code == 500
    assert response.text == "caught ValueError"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basehttp_collapses_the_single_exception_group() -> None:
    with pytest.raises(ValueError, match="boom before start"):
        await drive(BaseHTTPPassthrough(failing_app(after_chunks=None)))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_plain_task_group_wraps_the_same_error_in_an_exception_group() -> None:
    with pytest.RaisesGroup(pytest.RaisesExc(ValueError, match="boom before start")):
        await drive(NaiveSpawningMiddleware(failing_app(after_chunks=None)))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_errors_surface_bare_and_user_groups_survive() -> None:
    with pytest.raises(ValueError, match="from dispatch"):
        await drive(RaiseInDispatch(streaming_app(3, []), ValueError("from dispatch")))
    mine = ExceptionGroup("mine", [KeyError("k")])
    with pytest.RaisesGroup(KeyError, match="mine"):
        await drive(RaiseInDispatch(streaming_app(3, []), mine))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mid_stream_error_is_sent_as_a_clean_end_of_body() -> None:
    via_base: list[str] = []
    with pytest.raises(ValueError, match="boom mid-stream"):
        await drive(BaseHTTPPassthrough(failing_app(after_chunks=2)), log=via_base)
    via_pure: list[str] = []
    with pytest.raises(ValueError, match="boom mid-stream"):
        await drive(PurePassthrough(failing_app(after_chunks=2)), log=via_pure)
    assert via_base == ["deliver:0", "deliver:1", "deliver:final"]  # looks complete
    assert via_pure == ["deliver:0", "deliver:1"]  # server sees no final body: abort


@pytest.mark.unit
@pytest.mark.asyncio
async def test_try_except_around_call_next_cannot_catch_mid_stream_errors() -> None:
    log: list[str] = []
    with pytest.raises(ValueError, match="boom mid-stream"):
        await drive(CatchingMiddleware(failing_app(after_chunks=2)), log=log)
    assert log == ["deliver:0", "deliver:1", "deliver:final"]


# --- ASGI extensions and background tasks ------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basehttp_silently_drops_http_trailers() -> None:
    pure = await drive(PurePassthrough(trailers_app))
    base = await drive(BaseHTTPPassthrough(trailers_app))
    assert [message["type"] for message in pure][-1] == "http.response.trailers"
    assert pure[0]["trailers"] is True
    assert "trailers" not in base[0]
    assert [message["type"] for message in base] == [
        "http.response.start",
        "http.response.body",
        "http.response.body",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basehttp_rejects_zerocopysend() -> None:
    with pytest.raises(AssertionError, match=r"Unexpected message: .*zerocopysend"):
        await drive(BaseHTTPPassthrough(zerocopy_app))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_background_task_runs_after_the_final_body_is_sent() -> None:
    log: list[str] = []
    await drive(BaseHTTPPassthrough(background_app(log)), log=log)
    assert log == ["deliver:ok", "deliver:final", "background"]
