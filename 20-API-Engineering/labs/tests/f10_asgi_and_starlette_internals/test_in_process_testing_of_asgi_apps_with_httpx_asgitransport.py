"""httpx.ASGITransport runs the app on the test's loop; Starlette TestClient on a portal thread."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import httpx
import pytest
from asgi_lifespan import LifespanManager
from starlette.testclient import TestClient

from api_labs.f10_asgi_and_starlette_internals.in_process_test_clients import (
    REQUEST_TAG,
    Probe,
    ReceiveRecorder,
    build_app,
    crash_before_start,
    no_response,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def make_client(transport: httpx.ASGITransport, base_url: str = "http://test") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url=base_url)


# --- Where the app runs -------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgitransport_runs_the_app_on_the_test_thread_and_loop() -> None:
    probe = Probe()
    async with make_client(httpx.ASGITransport(app=build_app(probe))) as client:
        await client.get("/where")
        await client.get("/where")
    first, second = probe.sightings
    assert first.thread_id == second.thread_id == threading.get_ident()
    assert first.loop is asyncio.get_running_loop()
    assert second.loop is first.loop


@pytest.mark.unit
def test_testclient_without_with_starts_a_new_portal_loop_and_thread_per_request() -> None:
    probe = Probe()
    client = TestClient(build_app(probe))
    client.get("/where")
    client.get("/where")
    first, second = probe.sightings
    assert threading.get_ident() not in {first.thread_id, second.thread_id}
    assert first.loop is not second.loop
    assert first.loop.is_closed()
    assert second.loop.is_closed()
    assert probe.lifespan_loops == []


@pytest.mark.unit
def test_testclient_with_block_shares_one_portal_loop_across_lifespan_and_requests() -> None:
    probe = Probe()
    with TestClient(build_app(probe)) as client:
        client.get("/where")
        client.get("/where")
        (lifespan_loop,) = probe.lifespan_loops
        assert not lifespan_loop.is_closed()
    first, second = probe.sightings
    assert first.loop is second.loop is lifespan_loop
    assert first.thread_id != threading.get_ident()
    assert lifespan_loop.is_closed()


# --- The cross-loop bug the portal hides or creates ---------------------------------------


@pytest.mark.unit
def test_loop_bound_primitive_breaks_on_the_second_testclient_request() -> None:
    client = TestClient(build_app(Probe()))
    assert client.get("/poll").status_code == 204
    with pytest.raises(RuntimeError, match="is bound to a different event loop"):
        client.get("/poll")


@pytest.mark.unit
def test_loop_bound_primitive_survives_inside_one_testclient_with_block() -> None:
    with TestClient(build_app(Probe())) as client:
        statuses = [client.get("/poll").status_code for _ in range(3)]
    assert statuses == [204, 204, 204]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_event_bound_to_the_test_loop_fails_via_testclient_not_asgitransport() -> None:
    probe = Probe()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(probe.changed.wait(), timeout=0.001)  # binds to the test loop
    app = build_app(probe)
    async with make_client(httpx.ASGITransport(app=app)) as client:
        assert (await client.get("/poll")).status_code == 204
    with pytest.raises(RuntimeError, match="is bound to a different event loop"):
        TestClient(app).get("/poll")


# --- ContextVar propagation ---------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgitransport_shares_the_callers_context_so_app_writes_leak_back() -> None:
    token = REQUEST_TAG.set("set-by-test")
    try:
        async with make_client(httpx.ASGITransport(app=build_app(Probe()))) as client:
            response = await client.get("/where")
        assert response.json() == {"tag_seen": "set-by-test"}
        assert REQUEST_TAG.get() == "written-by-app"
    finally:
        REQUEST_TAG.reset(token)


@pytest.mark.unit
def test_testclient_copies_the_callers_context_so_app_writes_stay_in_the_portal() -> None:
    token = REQUEST_TAG.set("set-by-test")
    try:
        response = TestClient(build_app(Probe())).get("/where")
        assert response.json() == {"tag_seen": "set-by-test"}
        assert REQUEST_TAG.get() == "set-by-test"
    finally:
        REQUEST_TAG.reset(token)


# --- Response streaming -------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgitransport_buffers_the_whole_body_before_returning_the_response() -> None:
    probe = Probe()
    async with (
        make_client(httpx.ASGITransport(app=build_app(probe))) as client,
        client.stream("GET", "/stream") as response,
    ):
        assert probe.stream_events == ["start", "resumed", "finished"]
        chunks = [chunk async for chunk in response.aiter_raw()]
    assert chunks == [b"firstsecond"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgitransport_never_returns_for_an_unbounded_stream() -> None:
    probe = Probe()
    async with make_client(httpx.ASGITransport(app=build_app(probe))) as client:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await client.get("/stream?hold=1", timeout=0.01)
    assert probe.stream_events == ["start", "resumed", "cancelled"]


@pytest.mark.unit
def test_testclient_streams_and_closing_early_cancels_the_app() -> None:
    probe = Probe()
    client = TestClient(build_app(probe))
    with client.stream("GET", "/stream?hold=1") as response:
        first = next(response.iter_raw())
        assert probe.stream_resumed.wait(timeout=5)
        assert "finished" not in probe.stream_events
    assert first == b"first"
    assert probe.stream_events == ["start", "resumed", "cancelled"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgitransport_ignores_the_httpx_timeout() -> None:
    async with make_client(httpx.ASGITransport(app=build_app(Probe()))) as client:
        response = await client.get("/slow", timeout=0.001)
    assert response.content == b"late"


# --- Lifespan and scope["state"] ----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgitransport_does_not_run_lifespan() -> None:
    probe = Probe()
    async with make_client(httpx.ASGITransport(app=build_app(probe))) as client:
        response = await client.get("/state")
    assert response.status_code == 503
    assert probe.lifespan_loops == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lifespanmanager_state_reaches_requests_only_through_manager_app() -> None:
    probe = Probe()
    app = build_app(probe)
    async with LifespanManager(app) as manager:
        assert probe.lifespan_loops == [asyncio.get_running_loop()]
        async with make_client(httpx.ASGITransport(app=app)) as bare:
            assert (await bare.get("/state")).status_code == 503
        async with make_client(httpx.ASGITransport(app=manager.app)) as client:
            counters = [(await client.get("/state")).json()["counter"] for _ in range(2)]
    # asgi-lifespan 2.1.0 hands every request the same dict, not a shallow copy.
    assert counters == [1, 2]


@pytest.mark.unit
def test_testclient_hands_each_request_a_shallow_copy_of_lifespan_state() -> None:
    with TestClient(build_app(Probe())) as client:
        counters = [client.get("/state").json()["counter"] for _ in range(2)]
    assert counters == [1, 1]


# --- What each client puts in scope -------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgitransport_scope_defaults() -> None:
    async with make_client(httpx.ASGITransport(app=build_app(Probe()))) as client:
        view = (await client.get("/scope")).json()
    assert view == {
        "asgi": {"version": "3.0"},
        "client": ["127.0.0.1", 123],
        "server": ["test", None],
        "extensions": [],
        "has_state": False,
        "http_version": "1.1",
        "user_agent": f"python-httpx/{httpx.__version__}",
    }


@pytest.mark.unit
def test_testclient_scope_defaults() -> None:
    view = TestClient(build_app(Probe())).get("/scope").json()
    assert view == {
        "asgi": None,
        "client": ["testclient", 50000],
        "server": ["testserver", 80],
        "extensions": ["http.response.debug", "http.response.trailers"],
        "has_state": True,
        "http_version": "1.1",
        "user_agent": "testclient",
    }


@pytest.mark.unit
def test_testclient_returns_httpx2_responses_not_httpx_responses() -> None:
    response = TestClient(build_app(Probe())).get("/where")
    assert type(response).__module__ == "httpx2"
    assert httpx.Response not in type(response).__mro__


# --- Request body events ------------------------------------------------------------------


async def two_chunks() -> AsyncIterator[bytes]:
    yield b"ab"
    yield b"cd"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgitransport_sends_one_event_per_chunk_plus_an_empty_terminator() -> None:
    recorder = ReceiveRecorder()
    async with make_client(httpx.ASGITransport(app=recorder)) as client:
        await client.post("/", content=two_chunks())
        await client.get("/")
    post_events, get_events = recorder.events[:3], recorder.events[3:]
    assert post_events == [(b"ab", True), (b"cd", True), (b"", False)]
    assert get_events == [(b"", True), (b"", False)]


@pytest.mark.unit
def test_testclient_reads_the_whole_body_into_one_event() -> None:
    recorder = ReceiveRecorder()
    TestClient(recorder).post("/", content=iter([b"ab", b"cd"]))
    assert recorder.events == [(b"abcd", False)]
    assert "more_body" not in recorder.raw_messages[0]


# --- Errors -------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgitransport_raises_app_errors_or_synthesizes_an_empty_500() -> None:
    async with make_client(httpx.ASGITransport(app=crash_before_start)) as client:
        with pytest.raises(ValueError, match="boom before response start"):
            await client.get("/")
    quiet = httpx.ASGITransport(app=crash_before_start, raise_app_exceptions=False)
    async with make_client(quiet) as client:
        response = await client.get("/")
    assert (response.status_code, response.content, dict(response.headers)) == (500, b"", {})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_that_sends_nothing_fails_both_clients_with_assertion_error() -> None:
    async with make_client(httpx.ASGITransport(app=no_response)) as client:
        with pytest.raises(AssertionError):
            await client.get("/")
    with pytest.raises(AssertionError, match="TestClient did not receive any response"):
        TestClient(no_response).get("/")


# --- Cookies ------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_secure_cookie_round_trips_only_with_an_https_base_url() -> None:
    results: dict[str, object] = {}
    for base_url in ("http://test", "https://test"):
        transport = httpx.ASGITransport(app=build_app(Probe()))
        async with make_client(transport, base_url=base_url) as client:
            await client.post("/login")
            results[base_url] = (await client.get("/whoami")).json()["sid"]
    assert results == {"http://test": None, "https://test": "abc"}
