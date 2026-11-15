"""Starlette 1.10.0 responses: JSON, stream disconnects, file chunking, background order."""

from __future__ import annotations

import asyncio
import gc
import math
import os
import threading
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.background import BackgroundTask, BackgroundTasks
from starlette.requests import ClientDisconnect
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from api_labs.f10_asgi_and_starlette_internals.starlette_responses import (
    ClosingStreamingResponse,
    ScriptedPeer,
    file_chunk_plan,
    http_scope,
    starlette_etag,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from starlette.requests import Request
    from starlette.types import ASGIApp, Message


def _client(app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _endless(events: list[str]) -> AsyncIterator[bytes]:
    """An infinite producer that awaits between chunks, like a DB cursor or an LLM stream."""
    try:
        while True:
            await asyncio.sleep(0)
            events.append("chunk")
            yield b"x" * 16
    finally:
        events.append("finally")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_jsonresponse_renders_compact_utf8_and_counts_bytes() -> None:
    async def endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"name": "Zo\u00eb", "tags": ["a", "b"]})

    async with _client(Starlette(routes=[Route("/", endpoint)])) as client:
        resp = await client.get("/")
    assert resp.content == b'{"name":"Zo\xc3\xab","tags":["a","b"]}'
    assert resp.headers["content-length"] == str(len(resp.content)) == "32"
    assert len(resp.text) == 31
    assert resp.headers["content-type"] == "application/json"


@pytest.mark.unit
def test_jsonresponse_rejects_nan_at_construction() -> None:
    with pytest.raises(ValueError, match="Out of range float values are not JSON compliant"):
        JSONResponse({"x": math.nan})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_response_model_fast_path_bypasses_jsonresponse() -> None:
    app = FastAPI()

    @app.get("/model", response_model=dict[str, float])
    def model() -> dict[str, float]:  # pyright: ignore[reportUnusedFunction]
        return {"x": math.nan}

    @app.get("/explicit")
    def explicit() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return JSONResponse({"x": math.nan})

    async with _client(app) as client:
        resp = await client.get("/model")
        assert (resp.status_code, resp.content) == (200, b'{"x":null}')
        with pytest.raises(ValueError, match="not JSON compliant"):
            await client.get("/explicit")


@pytest.mark.unit
def test_response_headers_follow_status_and_media_type_rules() -> None:
    assert Response(status_code=204).raw_headers == []
    assert Response(b"stale", status_code=304).raw_headers == []
    assert Response(status_code=103).raw_headers == []
    assert dict(Response("hi", media_type="text/plain").raw_headers) == {
        b"content-length": b"2",
        b"content-type": b"text/plain; charset=utf-8",
    }
    assert dict(Response(b"{}", media_type="application/json").raw_headers) == {
        b"content-length": b"2",
        b"content-type": b"application/json",
    }
    preset = Response(b"abc", headers={"Content-Length": "999"})
    assert preset.raw_headers == [(b"content-length", b"999")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streaming_response_has_no_length_and_ends_with_empty_final_body() -> None:
    async def parts() -> AsyncIterator[str | bytes]:
        yield "a"
        yield b"bc"

    peer = ScriptedPeer()
    response = StreamingResponse(parts(), media_type="text/plain")
    await response(http_scope(spec_version="2.4"), peer.receive, peer.send)
    assert dict(peer.sent[0]["headers"]) == {b"content-type": b"text/plain; charset=utf-8"}
    assert peer.body_messages() == [(1, True), (2, True), (0, False)]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", [None, "2.0", "2.3"])
async def test_pre_2_4_disconnect_cancels_generator_and_still_runs_background(
    spec_version: str | None,
) -> None:
    events: list[str] = []
    peer = ScriptedPeer(disconnect_after_bodies=3, after_disconnect="drop")
    response = StreamingResponse(
        _endless(events), background=BackgroundTask(events.append, "background")
    )
    await response(http_scope(spec_version=spec_version), peer.receive, peer.send)
    assert peer.receive_calls == 2
    assert (events.count("chunk"), len(peer.body_messages()), peer.dropped) == (3, 3, 0)
    assert events[-2:] == ["finally", "background"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", ["2.4", "2.5"])
async def test_spec_2_4_send_oserror_becomes_client_disconnect_and_skips_background(
    spec_version: str,
) -> None:
    events: list[str] = []
    peer = ScriptedPeer(disconnect_after_bodies=3, after_disconnect="raise")
    response = StreamingResponse(
        _endless(events), background=BackgroundTask(events.append, "background")
    )
    with pytest.raises(ClientDisconnect):
        await response(http_scope(spec_version=spec_version), peer.receive, peer.send)
    assert peer.receive_calls == 0
    assert "background" not in events


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generator_suspended_in_send_is_not_closed_by_starlette() -> None:
    events: list[str] = []
    peer = ScriptedPeer(disconnect_after_bodies=2, after_disconnect="hang")
    response = StreamingResponse(
        _endless(events), background=BackgroundTask(events.append, "background")
    )
    await response(http_scope(), peer.receive, peer.send)
    assert events[-1] == "background"
    assert "finally" not in events

    events.clear()
    peer = ScriptedPeer(disconnect_after_bodies=2, after_disconnect="hang")
    closing = ClosingStreamingResponse(
        _endless(events), background=BackgroundTask(events.append, "background")
    )
    await closing(http_scope(), peer.receive, peer.send)
    assert events[-2:] == ["finally", "background"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_closing_stream_response_also_closes_on_the_oserror_path() -> None:
    events: list[str] = []
    peer = ScriptedPeer(disconnect_after_bodies=2, after_disconnect="raise")
    closing = ClosingStreamingResponse(_endless(events))
    with pytest.raises(ClientDisconnect):
        await closing(http_scope(spec_version="2.4"), peer.receive, peer.send)
    assert events[-1] == "finally"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_iterator_runs_in_worker_threads_and_is_finalized_on_the_loop() -> None:
    loop_thread = threading.get_ident()
    seen: list[tuple[str, bool]] = []

    def numbers() -> Iterator[bytes]:
        try:
            for i in range(1000):
                seen.append(("next", threading.get_ident() == loop_thread))
                yield str(i).encode()
        finally:
            seen.append(("finally", threading.get_ident() == loop_thread))

    peer = ScriptedPeer(disconnect_after_bodies=2, after_disconnect="hang")
    response = StreamingResponse(numbers())
    await response(http_scope(), peer.receive, peer.send)
    assert seen[0] == ("next", False)
    assert ("finally", True) not in seen

    del response
    gc.collect()
    for _ in range(5):
        await asyncio.sleep(0)
    assert seen[-1] == ("finally", True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bytearray_chunk_is_rejected() -> None:
    async def parts() -> AsyncIterator[bytes]:
        yield bytearray(b"x")  # type: ignore[misc]  # pyright: ignore[reportReturnType]

    peer = ScriptedPeer()
    with pytest.raises(AttributeError, match="'bytearray' object has no attribute 'encode'"):
        await StreamingResponse(parts())(http_scope(spec_version="2.4"), peer.receive, peer.send)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, 1, 65535, 65536, 65537, 3 * 65536])
async def test_file_response_chunk_sequence_matches_plan(tmp_path: Path, size: int) -> None:
    path = tmp_path / "blob.bin"
    path.write_bytes(b"a" * size)
    peer = ScriptedPeer()
    await FileResponse(path)(http_scope(spec_version="2.4"), peer.receive, peer.send)
    assert peer.body_messages() == file_chunk_plan(size)


@pytest.mark.unit
def test_file_chunk_plan_adds_an_empty_tail_on_exact_multiples() -> None:
    assert file_chunk_plan(2 * 65536) == [(65536, True), (65536, True), (0, False)]
    assert file_chunk_plan(65537) == [(65536, True), (1, False)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_file_response_stat_headers_and_etag_formula(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_bytes(b'{"k":1}')
    os.utime(path, (1_800_000_000.25, 1_800_000_000.25))
    st = path.stat()

    async def endpoint(request: Request) -> FileResponse:
        return FileResponse(path)

    async with _client(Starlette(routes=[Route("/", endpoint)])) as client:
        resp = await client.get("/")
    assert resp.content == b'{"k":1}'
    assert resp.headers["content-type"] == "application/json"
    assert resp.headers["content-length"] == "7"
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["last-modified"] == "Fri, 15 Jan 2027 08:00:00 GMT"
    assert resp.headers["etag"] == starlette_etag(st.st_mtime, st.st_size)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_file_response_head_sends_headers_only_with_full_length(tmp_path: Path) -> None:
    path = tmp_path / "big.bin"
    path.write_bytes(b"z" * 100_000)
    peer = ScriptedPeer()
    await FileResponse(path)(http_scope(method="HEAD"), peer.receive, peer.send)
    assert dict(peer.sent[0]["headers"])[b"content-length"] == b"100000"
    assert peer.body_messages() == [(0, False)]
    assert peer.receive_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_file_response_uses_pathsend_when_the_server_offers_it(tmp_path: Path) -> None:
    path = tmp_path / "page.html"
    path.write_bytes(b"<p>hi</p>")
    peer = ScriptedPeer()
    scope = http_scope(extensions={"http.response.pathsend": {}})
    await FileResponse(path)(scope, peer.receive, peer.send)
    assert [m["type"] for m in peer.sent] == ["http.response.start", "http.response.pathsend"]
    assert peer.sent[1]["path"] == str(path)
    assert peer.receive_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_file_response_stops_reading_after_disconnect(tmp_path: Path) -> None:
    path = tmp_path / "video.bin"
    path.write_bytes(b"v" * (64 * 65536))
    events: list[str] = []
    peer = ScriptedPeer(disconnect_after_bodies=2, after_disconnect="drop")
    response = FileResponse(path, background=BackgroundTask(events.append, "background"))
    await response(http_scope(spec_version="2.3"), peer.receive, peer.send)
    assert (len(peer.body_messages()), peer.dropped) == (2, 0)
    assert events == ["background"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_stat_result_breaks_content_length_framing(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_bytes(b"0123456789")
    early = path.stat()
    path.write_bytes(b"L" * 70_000)
    peer = ScriptedPeer()
    await FileResponse(path, stat_result=early)(
        http_scope(spec_version="2.4"), peer.receive, peer.send
    )
    assert dict(peer.sent[0]["headers"])[b"content-length"] == b"10"
    assert sum(n for n, _ in peer.body_messages()) == 70_000


@pytest.mark.unit
@pytest.mark.parametrize(
    ("filename", "disposition", "media_type"),
    [
        ("report.pdf", 'attachment; filename="report.pdf"', "application/pdf"),
        ("r\u00e9sum\u00e9.pdf", "attachment; filename*=utf-8''r%C3%A9sum%C3%A9.pdf", "application/pdf"),
        ('a"b.txt', "attachment; filename*=utf-8''a%22b.txt", "text/plain"),
    ],
)
def test_content_disposition_switches_to_rfc_8187_when_quoting_changes_the_name(
    tmp_path: Path, filename: str, disposition: str, media_type: str
) -> None:
    response = FileResponse(tmp_path / "x", filename=filename)
    assert response.headers["content-disposition"] == disposition
    assert response.media_type == media_type


@pytest.mark.unit
@pytest.mark.asyncio
async def test_background_runs_after_final_body_sequentially_and_stops_on_error() -> None:
    events: list[str] = []
    peer = ScriptedPeer()

    def boom() -> None:
        events.append("boom")
        raise RuntimeError("task failed")

    tasks = BackgroundTasks()
    tasks.add_task(events.append, "first")
    tasks.add_task(boom)
    tasks.add_task(events.append, "never")
    response = Response(b"done", background=tasks)

    async def observe(message: Message) -> None:
        if message.get("type") == "http.response.body" and not message.get("more_body"):
            events.append("final-body")
        await peer.send(message)

    with pytest.raises(RuntimeError, match="task failed"):
        await response(http_scope(), peer.receive, observe)
    assert events == ["final-body", "first", "boom"]
    assert peer.body_messages() == [(4, False)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_transport_returns_only_after_background_finishes() -> None:
    finished = asyncio.Event()

    async def slow() -> None:
        await asyncio.sleep(0.01)
        finished.set()

    async def endpoint(request: Request) -> Response:
        return Response(b"ok", background=BackgroundTask(slow))

    async with _client(Starlette(routes=[Route("/", endpoint)])) as client:
        resp = await client.get("/")
        assert resp.content == b"ok"
        assert finished.is_set()
