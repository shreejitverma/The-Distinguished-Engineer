"""In-process ASGI HTTP streaming, disconnect, and zero-copy message selection."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from api_labs.f10_asgi_and_starlette_internals.asgi_http_and_disconnect_event_lifecycle import (
    ConnectionClosed,
    Downstream,
    Receive,
    ScriptedHTTPCycle,
    Send,
    echo_request_body,
    file_is_sendfile_source,
    read_request_body,
    stream_with_disconnect,
    zero_copy_or_body,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_chunks_are_joined_in_order_and_echoed() -> None:
    cycle = ScriptedHTTPCycle(
        [
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.request", "body": b"cd", "more_body": False},
        ]
    )
    downstream = Downstream((b"ab", b"cd"))
    outcome = await stream_with_disconnect(
        cycle.receive,
        cycle.send,
        downstream,
        poll_between_chunks=False,
    )
    assert outcome.request_body == b"abcd"
    assert outcome.chunks_pulled == 2
    assert outcome.stopped_on == "complete"
    assert cycle.sent == [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/octet-stream")],
        },
        {"type": "http.response.body", "body": b"ab", "more_body": True},
        {"type": "http.response.body", "body": b"cd", "more_body": True},
        {"type": "http.response.body", "body": b"", "more_body": False},
    ]
    assert await cycle.receive() == {"type": "http.disconnect"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_more_body_completes_the_request() -> None:
    cycle = ScriptedHTTPCycle([{"type": "http.request", "body": b"only"}])
    body, disconnected = await read_request_body(cycle.receive)
    assert body == b"only"
    assert disconnected is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disconnect_before_response_sends_nothing_and_pulls_nothing() -> None:
    cycle = ScriptedHTTPCycle(
        [
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    downstream = Downstream((b"should-not-run",))
    outcome = await stream_with_disconnect(
        cycle.receive,
        cycle.send,
        downstream,
        poll_between_chunks=False,
    )
    assert outcome == outcome.__class__(b"ab", 0, "http.disconnect")
    assert outcome.stopped_on == "http.disconnect"
    assert outcome.chunks_pulled == 0
    assert cycle.sent == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disconnect_between_chunks_cancels_the_rest_of_downstream() -> None:
    cycle = ScriptedHTTPCycle(
        [
            {"type": "http.request", "body": b"", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )
    downstream = Downstream((b"one", b"two", b"three"))
    outcome = await stream_with_disconnect(
        cycle.receive,
        cycle.send,
        downstream,
        poll_between_chunks=True,
    )
    assert outcome.chunks_pulled == 1
    assert outcome.stopped_on == "http.disconnect"
    assert cycle.sent == [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/octet-stream")],
        },
        {"type": "http.response.body", "body": b"one", "more_body": True},
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_spec_24_send_after_disconnect_raises_and_records_nothing() -> None:
    cycle = ScriptedHTTPCycle([{"type": "http.disconnect"}], raise_on_send_after_close=True)
    assert await cycle.receive() == {"type": "http.disconnect"}
    with pytest.raises(ConnectionClosed):
        await cycle.send({"type": "http.response.body", "body": b"x", "more_body": False})
    assert cycle.sent == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_after_close_without_spec_24_drops_the_message() -> None:
    cycle = ScriptedHTTPCycle([{"type": "http.disconnect"}], raise_on_send_after_close=False)
    assert await cycle.receive() == {"type": "http.disconnect"}
    await cycle.send({"type": "http.response.body", "body": b"x", "more_body": False})
    assert cycle.sent == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_receive_with_no_event_on_an_open_response_fails() -> None:
    cycle = ScriptedHTTPCycle([])
    with pytest.raises(RuntimeError, match="no scripted event"):
        await cycle.receive()


@pytest.mark.unit
def test_pathsend_wins_and_a_relative_path_is_rejected(tmp_path: Path) -> None:
    absolute = tmp_path / "blob.bin"
    absolute.write_bytes(b"xyz")
    buffered = io.BytesIO(b"xyz")
    chosen = zero_copy_or_body(
        {"http.response.pathsend": {}, "http.response.zerocopysend": {}},
        str(absolute),
        buffered,
    )
    assert chosen == {"type": "http.response.pathsend", "path": str(absolute)}
    assert buffered.tell() == 0
    with pytest.raises(ValueError, match="absolute"):
        zero_copy_or_body({"http.response.pathsend": {}}, "blob.bin", buffered)


@pytest.mark.unit
def test_zerocopy_needs_a_real_fd_and_bytesio_falls_back_to_body(tmp_path: Path) -> None:
    absolute = tmp_path / "blob.bin"
    absolute.write_bytes(b"xyz")
    with absolute.open("rb") as real_file:
        assert file_is_sendfile_source(real_file) is True
        chosen = zero_copy_or_body({"http.response.zerocopysend": {}}, str(absolute), real_file)
        assert chosen["type"] == "http.response.zerocopysend"
        assert chosen["file"] is real_file
        assert chosen["more_body"] is False
    memory = io.BytesIO(b"xyz")
    assert file_is_sendfile_source(memory) is False
    fallback = zero_copy_or_body({"http.response.zerocopysend": {}}, str(absolute), memory)
    assert fallback == {"type": "http.response.body", "body": b"xyz", "more_body": False}


@pytest.mark.unit
def test_no_extension_copies_the_file_into_a_body_message(tmp_path: Path) -> None:
    absolute = tmp_path / "blob.bin"
    absolute.write_bytes(b"xyz")
    with absolute.open("rb") as real_file:
        copied = zero_copy_or_body({}, str(absolute), real_file)
    assert copied == {"type": "http.response.body", "body": b"xyz", "more_body": False}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pathsend_completes_the_response_so_a_later_body_raises() -> None:
    cycle = ScriptedHTTPCycle([])
    await cycle.send({"type": "http.response.start", "status": 200, "headers": []})
    await cycle.send({"type": "http.response.pathsend", "path": "/tmp/blob.bin"})
    with pytest.raises(ConnectionClosed):
        await cycle.send({"type": "http.response.body", "body": b"more", "more_body": False})
    assert [message["type"] for message in cycle.sent] == [
        "http.response.start",
        "http.response.pathsend",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpx_asgi_transport_joins_chunks_and_omits_spec_version() -> None:
    seen: dict[str, object] = {}

    async def app(scope: dict[str, object], receive: Receive, send: Send) -> None:
        seen["asgi"] = scope["asgi"]
        await echo_request_body(scope, receive, send)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"ab"
        yield b"cd"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/upload", content=chunks())
    assert response.status_code == 200
    assert response.content == b"abcd"
    assert seen["asgi"] == {"version": "3.0"}
