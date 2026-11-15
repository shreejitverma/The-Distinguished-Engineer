"""Unit tests: database sessions used by StreamingResponse bodies under each dependency scope."""

import gc
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import httpx
import pytest
import pytest_asyncio
from sqlalchemy.exc import InvalidRequestError, SAWarning, TimeoutError
from sqlalchemy.orm.exc import DetachedInstanceError
from starlette.types import ASGIApp, Message, Scope

from api_labs.f11_fastapi_internals_and_patterns.streaming_session_scope import (
    StreamLab,
    build_streaming_app,
    make_lab,
)

TITLES_BODY = "RFC 9110\nRFC 9112\nPEP 525\n"


@pytest_asyncio.fixture
async def lab(tmp_path: Path) -> AsyncIterator[StreamLab]:
    built = await make_lab(tmp_path / "lab.sqlite3")
    yield built
    await built.close_pinned_sessions()
    await built.engine.dispose()


async def get(app: ASGIApp, path: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.get(path)


async def drive_raw(
    app: ASGIApp,
    path: str,
    sent: list[Message],
    *,
    spec_version: str,
    disconnect_after_first_chunk: bool,
) -> None:
    """Call the ASGI app directly and record every message it sends, as a server would."""
    first_chunk = anyio.Event()
    request_delivered = False

    async def receive() -> Message:
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await first_chunk.wait()
        if not disconnect_after_first_chunk:
            await anyio.sleep_forever()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk.set()

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    await app(scope, receive, send)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_function_scope_closes_session_before_stream_and_leaks_reacquired_connection(
    lab: StreamLab,
) -> None:
    response = await get(build_streaming_app(lab), "/function/titles")

    assert response.status_code == 200
    assert response.text == TITLES_BODY
    assert lab.events == [
        "session:open",
        "endpoint:return",
        "session:closed",
        "stream:start(checked_out=0)",
        "stream:end(checked_out=1)",
    ]
    assert lab.checked_out() == 1
    with pytest.raises(TimeoutError, match=r"QueuePool limit of size 1 overflow 0 reached"):
        async with lab.engine.connect():
            pass

    await lab.close_pinned_sessions()
    assert lab.checked_out() == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unpinned_leaked_connection_returns_only_through_garbage_collector(
    tmp_path: Path,
) -> None:
    lab = await make_lab(tmp_path / "gc.sqlite3", pin_sessions=False)
    gc.disable()
    try:
        response = await get(build_streaming_app(lab), "/function/titles")
        assert response.text == TITLES_BODY
        assert lab.checked_out() == 1
        with pytest.warns(SAWarning, match=r"garbage collector is trying to clean up"):
            gc.collect()
    finally:
        gc.enable()
    assert lab.checked_out() == 0
    await lab.engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_function_scope_lazy_load_fails_after_status_and_first_chunk_are_sent(
    lab: StreamLab,
) -> None:
    app = build_streaming_app(lab)
    sent: list[Message] = []

    with pytest.raises(DetachedInstanceError, match=r"is not bound to a Session"):
        await drive_raw(
            app,
            "/function/books-of-author",
            sent,
            spec_version="2.4",
            disconnect_after_first_chunk=False,
        )

    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"Fielding:\n"
    assert sent[1]["more_body"] is True

    request_scoped = await get(app, "/request/books-of-author")
    assert request_scoped.text == "Fielding:\n- RFC 9110\n- RFC 9112\n- PEP 525\n"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_resets_only_false_turns_use_after_close_into_a_loud_error(
    lab: StreamLab,
) -> None:
    with pytest.raises(InvalidRequestError, match=r"permanently closed"):
        await get(build_streaming_app(lab), "/function/strict-titles")

    assert lab.events == ["strict:closed"]
    assert lab.checked_out() == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_function_scope_stream_writes_are_never_committed(lab: StreamLab) -> None:
    response = await get(build_streaming_app(lab), "/function/audit")

    assert response.status_code == 200
    assert response.text.count("logged ") == 3
    assert lab.events == ["tx:committed", "stream:end"]
    assert lab.checked_out() == 1

    await lab.close_pinned_sessions()
    assert await lab.audit_count() == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_scope_keeps_session_open_until_response_is_sent(lab: StreamLab) -> None:
    app = build_streaming_app(lab)

    titles = await get(app, "/request/titles")
    assert titles.text == TITLES_BODY
    assert lab.events == [
        "session:open",
        "endpoint:return",
        "stream:start(checked_out=0)",
        "stream:end(checked_out=1)",
        "session:closed",
    ]
    assert lab.checked_out() == 0

    lab.events.clear()
    audit = await get(app, "/request/audit")
    assert audit.status_code == 200
    assert lab.events == ["stream:end", "tx:committed"]
    assert await lab.audit_count() == 3
    assert lab.checked_out() == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_materialized_rows_release_the_connection_before_streaming(
    lab: StreamLab,
) -> None:
    response = await get(build_streaming_app(lab), "/function/materialized")

    assert response.text == TITLES_BODY
    assert lab.events == ["session:open", "session:closed", "stream:start(checked_out=0)"]
    assert lab.checked_out() == 0


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected_events"),
    [
        ("/owned/stall", ["stream:stalled(checked_out=1)", "owned:exiting"]),
        (
            "/request/stall",
            ["session:open", "stream:stalled(checked_out=1)", "session:closed"],
        ),
    ],
)
async def test_client_disconnect_mid_stream_still_releases_the_connection(
    lab: StreamLab, path: str, expected_events: list[str]
) -> None:
    sent: list[Message] = []
    await drive_raw(
        build_streaming_app(lab),
        path,
        sent,
        spec_version="2.3",
        disconnect_after_first_chunk=True,
    )

    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[1]["body"] == b"RFC 9110\n"
    assert lab.events == expected_events
    assert lab.checked_out() == 0
