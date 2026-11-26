"""Lab: SSE delta framing, heartbeats, and error frames after `200 OK` for LLM token APIs."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any, Literal

import httpx
import pytest
import uvicorn

from api_labs.f10_asgi_and_starlette_internals.sse_resumption import EventStreamParser
from api_labs.f21_ai_and_llm_api_engineering.llm_token_stream import (
    HEARTBEAT_FRAME,
    GenerationError,
    Outcome,
    TokenPump,
    consume,
    create_app,
    decode_chunked,
    encode_frame,
    frame_stream,
    naive_heartbeat_frames,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence

    from starlette.applications import Starlette
    from starlette.types import ASGIApp, Message

    from api_labs.f21_ai_and_llm_api_engineering.llm_token_stream import Generate

Http = Literal["h11", "httptools"]


# --- Helpers -----------------------------------------------------------------------------


def tokens_then(items: Sequence[str], error: Exception | None = None) -> Generate:
    """A generator factory that yields `items`, then raises `error` if given."""

    async def generate(prompt: str) -> AsyncGenerator[str, None]:
        for item in items:
            yield item
        if error is not None:
            raise error

    return generate


def client_for(app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def post_prompt(app: ASGIApp, headers: dict[str, str] | None = None) -> httpx.Response:
    async with client_for(app) as client:
        return await client.post("/v1/completions", json={"prompt": "hi"}, headers=headers)


class Peer:
    """A raw ASGI peer that records every message as it is sent, in order."""

    def __init__(self, path: str = "/v1/completions") -> None:
        body = json.dumps({"prompt": "hi"}).encode()
        self.scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "server": ("test", 80),
            "client": ("127.0.0.1", 50000),
        }
        self._body = body
        self._request_sent = False
        self._disconnect = asyncio.Event()
        self.messages: list[Message] = []
        self.arrivals: asyncio.Queue[Message] = asyncio.Queue()

    async def receive(self) -> Message:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        self.messages.append(message)
        self.arrivals.put_nowait(message)

    async def run(self, app: ASGIApp) -> None:
        await app(self.scope, self.receive, self.send)

    def body_messages(self) -> list[Message]:
        return [m for m in self.messages if m["type"] == "http.response.body"]


def wire_of(peer: Peer) -> bytes:
    return b"".join(m["body"] for m in peer.body_messages())


# --- Framing -----------------------------------------------------------------------------


@pytest.mark.unit
def test_delta_frame_wire_bytes_are_exact() -> None:
    frame = encode_frame("delta", {"seq": 0, "text": "Hel"})
    assert frame == b'event: delta\ndata: {"seq":0,"text":"Hel"}\n\n'


@pytest.mark.unit
def test_line_breaks_in_token_text_cannot_split_a_frame() -> None:
    text = "a\nb\r\nc\u2028\u00e9"
    frame = encode_frame("delta", {"seq": 0, "text": text})
    assert frame.count(b"\n") == 3  # the event line, the data line, and the blank line
    assert b"\r" not in frame
    (event,) = EventStreamParser().feed(frame)
    assert event.type == "delta"
    assert json.loads(event.data)["text"] == text


@pytest.mark.unit
def test_event_name_cannot_inject_a_second_field() -> None:
    with pytest.raises(ValueError, match="invalid SSE event name"):
        encode_frame("delta\ndata: {}", {})


@pytest.mark.unit
def test_heartbeat_comment_dispatches_no_event() -> None:
    parser = EventStreamParser()
    assert parser.feed(HEARTBEAT_FRAME) == []
    assert parser.close() == []


@pytest.mark.unit
def test_cjk_costs_three_bytes_per_character_unescaped_and_six_escaped() -> None:
    raw = encode_frame("delta", {"seq": 0, "text": "\u4f60\u597d"})
    body = {"seq": 0, "text": "\u4f60\u597d"}
    payload = json.dumps(body, separators=(",", ":"))  # ensure_ascii=True escapes it
    escaped = f"event: delta\ndata: {payload}\n\n".encode()
    assert len(escaped) - len(raw) == 2 * (6 - 3)


# --- Response headers and flush behavior ---------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_headers_disable_proxy_buffering_and_transforms_and_never_promise_a_length() -> None:
    response = await post_prompt(create_app(tokens_then(["a"])))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    cache_control = {part.strip() for part in response.headers["cache-control"].split(",")}
    assert {"no-cache", "no-transform"} <= cache_control
    assert "content-length" not in response.headers
    assert "connection" not in response.headers  # forbidden in HTTP/2 (RFC 9113 Section 8.2.2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_every_delta_is_flushed_as_its_own_asgi_body_message() -> None:
    peer = Peer()
    await peer.run(create_app(tokens_then(["a", "b", "c"])))
    assert peer.messages[0]["status"] == 200
    bodies = peer.body_messages()
    assert [m["more_body"] for m in bodies] == [True, True, True, True, False]
    for message in bodies[:3]:
        assert message["body"].startswith(b"event: delta")
        assert message["body"].count(b"\n\n") == 1
    assert bodies[3]["body"].startswith(b"event: done")
    assert bodies[4]["body"] == b""


# --- Completion and failure ----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_complete_stream_ends_with_a_done_frame() -> None:
    response = await post_prompt(create_app(tokens_then(["Hello", " world"])))
    result = consume(response.content)
    assert result.outcome is Outcome.COMPLETE
    assert result.text == "Hello world"
    assert result.events == ("delta", "delta", "done")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mid_stream_failure_keeps_status_200_and_ends_with_a_problem_frame() -> None:
    error = GenerationError("overloaded", "Model overloaded", 503, "At capacity.", retry_after=5)
    app = create_app(tokens_then(["one", "two"], error))
    response = await post_prompt(app, headers={"x-request-id": "req-1"})
    assert response.status_code == 200  # committed before the failure
    result = consume(response.content)
    assert result.outcome is Outcome.FAILED
    assert result.text == "onetwo"
    assert result.events == ("delta", "delta", "error")  # no `done` after an `error`
    assert result.problem == {
        "type": "https://api.example.com/problems/overloaded",
        "title": "Model overloaded",
        "status": 503,
        "detail": "At capacity.",
        "instance": "/v1/requests/req-1",
        "retry_after": 5,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unexpected_exception_text_never_reaches_the_client() -> None:
    secret = RuntimeError("db password=hunter2 at 10.0.0.5")
    app = create_app(tokens_then(["x"], secret))
    response = await post_prompt(app, headers={"x-request-id": "req-123"})
    assert b"hunter2" not in response.content
    assert b"10.0.0.5" not in response.content
    result = consume(response.content)
    assert result.outcome is Outcome.FAILED
    assert result.problem is not None
    assert result.problem["status"] == 500
    assert result.problem["instance"] == "/v1/requests/req-123"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failure_before_the_first_token_is_a_real_status_with_problem_json() -> None:
    error = GenerationError("overloaded", "Model overloaded", 503, "At capacity.", retry_after=5)
    response = await post_prompt(create_app(tokens_then([], error)))
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["retry-after"] == "5"
    assert response.json()["type"] == "https://api.example.com/problems/overloaded"
    assert "x-accel-buffering" not in response.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_committing_headers_early_turns_the_same_failure_into_200_plus_error_frame() -> None:
    error = GenerationError("overloaded", "Model overloaded", 503, "At capacity.", retry_after=5)
    app = create_app(tokens_then([], error), commit_after_first_item=False)
    response = await post_prompt(app)
    assert response.status_code == 200
    assert "retry-after" not in response.headers  # headers are gone; the hint is in the frame
    result = consume(response.content)
    assert result.outcome is Outcome.FAILED
    assert result.problem is not None
    assert result.problem["retry_after"] == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_request_is_rejected_before_any_streaming_starts() -> None:
    calls: list[str] = []

    async def generate(prompt: str) -> AsyncGenerator[str, None]:
        calls.append(prompt)
        yield "never"

    async with client_for(create_app(generate)) as client:
        response = await client.post("/v1/completions", content=b"not json")
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"].endswith("/invalid-request")
    assert calls == []


# --- Client-side judgment ------------------------------------------------------------------


@pytest.mark.unit
def test_a_stream_with_no_terminal_frame_is_truncated_not_complete() -> None:
    wire = encode_frame("delta", {"seq": 0, "text": "a"}) + encode_frame(
        "delta", {"seq": 1, "text": "b"}
    )
    result = consume(wire)
    assert result.outcome is Outcome.TRUNCATED
    assert result.text == "ab"


@pytest.mark.unit
def test_a_frame_cut_before_its_blank_line_is_discarded() -> None:
    whole = encode_frame("delta", {"seq": 0, "text": "a"})
    cut = encode_frame("delta", {"seq": 1, "text": "b"})[:-1]  # the final LF never arrived
    result = consume(whole + cut)
    assert result.outcome is Outcome.TRUNCATED
    assert result.text == "a"


@pytest.mark.unit
def test_decode_chunked_distinguishes_a_last_chunk_from_a_cut_connection() -> None:
    assert decode_chunked(b"3\r\nabc\r\n0\r\n\r\n") == ([b"abc"], True)
    assert decode_chunked(b"3\r\nabc\r\n") == ([b"abc"], False)
    assert decode_chunked(b"3\r\nabc\r\n5\r\nab") == ([b"abc"], False)


# --- Heartbeats and producer lifecycle -----------------------------------------------------


def blocked_generator(gate: asyncio.Event, state: dict[str, bool]) -> Generate:
    """Yields once, then waits on `gate`, recording whether it was cancelled meanwhile."""

    async def generate(prompt: str) -> AsyncGenerator[str, None]:
        yield "early "
        try:
            await gate.wait()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        yield "late"

    return generate


@pytest.mark.unit
@pytest.mark.asyncio
async def test_heartbeats_flow_while_the_generator_is_blocked_and_no_token_is_lost() -> None:
    gate = asyncio.Event()
    state = {"cancelled": False}
    peer = Peer()
    app = create_app(blocked_generator(gate, state), heartbeat_interval=0.01)
    task = asyncio.create_task(peer.run(app))
    heartbeats = 0
    async with asyncio.timeout(5):
        while heartbeats < 3:
            message = await peer.arrivals.get()
            if message["type"] == "http.response.body" and message["body"] == HEARTBEAT_FRAME:
                heartbeats += 1
        gate.set()  # the model finally produces its next token
        await task
    result = consume(wire_of(peer))
    assert state["cancelled"] is False
    assert result.outcome is Outcome.COMPLETE
    assert result.text == "early late"
    assert result.heartbeats >= 3
    assert result.events == ("delta", "delta", "done")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_for_on_anext_cancels_the_generator_and_ends_the_stream_silently() -> None:
    gate = asyncio.Event()
    state = {"cancelled": False}
    frames = [
        frame async for frame in naive_heartbeat_frames(blocked_generator(gate, state)("hi"), 0.01)
    ]
    assert state["cancelled"] is True
    assert frames == [encode_frame("delta", {"seq": 0, "text": "early "}), HEARTBEAT_FRAME]
    assert consume(b"".join(frames)).outcome is Outcome.TRUNCATED  # no `done`, no `error`


@pytest.mark.unit
@pytest.mark.asyncio
async def test_closing_the_stream_early_cancels_the_producer_and_closes_upstream() -> None:
    state = {"closed": False}

    async def generate(prompt: str) -> AsyncGenerator[str, None]:
        try:
            yield "one"
            await asyncio.Event().wait()  # never finishes on its own
            yield "two"
        finally:
            state["closed"] = True

    pump = TokenPump(generate("hi"))
    stream = frame_stream(pump, request_id="r", heartbeat_interval=60)
    assert (await anext(stream)).startswith(b"event: delta")
    await stream.aclose()
    assert state["closed"] is True
    assert pump.done
    assert asyncio.all_tasks() == {asyncio.current_task()}

@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_lone_surrogate_becomes_an_error_frame_not_an_abrupt_close() -> None:
    app = create_app(tokens_then(["ok ", "\ud800", "never sent"]))
    response = await post_prompt(app, headers={"x-request-id": "req-s"})
    result = consume(response.content)
    assert response.status_code == 200
    assert result.outcome is Outcome.FAILED
    assert result.text == "ok "
    assert result.events == ("delta", "error")
    assert result.problem is not None
    assert result.problem["type"].endswith("/internal-error")
    assert "surrogate" not in response.text  # the codec message stays server-side


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_upstream_cancelled_error_ends_in_an_error_frame_instead_of_hanging() -> None:
    async def generate(prompt: str) -> AsyncGenerator[str, None]:
        yield "one"
        raise asyncio.CancelledError

    app = create_app(generate)
    async with asyncio.timeout(5):
        response = await post_prompt(app)
    result = consume(response.content)
    assert result.outcome is Outcome.FAILED
    assert result.text == "one"
    assert result.events == ("delta", "error")


# --- Over a real socket: uvicorn 0.54.0 ----------------------------------------------------


@contextlib.asynccontextmanager
async def running(app: Starlette, http: Http) -> AsyncIterator[int]:
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, lifespan="off", log_level="critical", http=http
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(5):
        while not server.started:  # noqa: ASYNC110 - Server exposes a flag, not an event
            await asyncio.sleep(0.01)
    try:
        yield int(server.servers[0].sockets[0].getsockname()[1])
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)


async def exchange(port: int, path: str) -> tuple[str, dict[str, str], bytes]:
    body = json.dumps({"prompt": "hi"}).encode()
    request = (
        f"POST {path} HTTP/1.1\r\nHost: t\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    async with asyncio.timeout(5):
        raw = await reader.read()  # until the server closes the connection
    writer.close()
    await writer.wait_closed()
    head, _, payload = raw.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode().split("\r\n")
    headers = {k.lower(): v for k, _, v in (line.partition(": ") for line in header_lines)}
    return status_line, headers, payload


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("http", ["h11", "httptools"])
async def test_http11_wire_is_chunked_with_one_chunk_per_frame_and_a_last_chunk(http: Http) -> None:
    async with running(create_app(tokens_then(["a", "bb", "ccc"])), http) as port:
        status_line, headers, payload = await exchange(port, "/v1/completions")
    assert status_line == "HTTP/1.1 200 OK"
    assert headers["transfer-encoding"] == "chunked"
    assert headers["x-accel-buffering"] == "no"
    assert "content-length" not in headers
    chunks, terminated = decode_chunked(payload)
    assert terminated is True
    assert [c.split(b"\n", 1)[0] for c in chunks] == [
        b"event: delta",
        b"event: delta",
        b"event: delta",
        b"event: done",
    ]
    assert all(c.count(b"\n\n") == 1 for c in chunks)
    framing = sum(len(f"{len(c):x}") + 4 for c in chunks) + len(b"0\r\n\r\n")
    assert len(payload) == sum(len(c) for c in chunks) + framing


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("http", ["h11", "httptools"])
async def test_handled_failure_over_the_wire_still_ends_with_a_clean_last_chunk(http: Http) -> None:
    error = GenerationError("overloaded", "Model overloaded", 503, "At capacity.")
    async with running(create_app(tokens_then(["one", "two"], error)), http) as port:
        status_line, _, payload = await exchange(port, "/v1/completions")
    chunks, terminated = decode_chunked(payload)
    assert status_line == "HTTP/1.1 200 OK"
    assert terminated is True
    result = consume(b"".join(chunks))
    assert result.outcome is Outcome.FAILED
    assert result.text == "onetwo"


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("http", ["h11", "httptools"])
async def test_unhandled_failure_closes_the_connection_with_no_last_chunk(http: Http) -> None:
    app = create_app(tokens_then(["one", "two"], RuntimeError("boom")))
    async with running(app, http) as port:
        status_line, _, payload = await exchange(port, "/v1/naive")
    chunks, terminated = decode_chunked(payload)
    assert status_line == "HTTP/1.1 200 OK"  # the status was already on the wire
    assert terminated is False  # RFC 9112 Section 8: an incomplete chunked response
    assert not payload.endswith(b"0\r\n\r\n")
    result = consume(b"".join(chunks))
    assert result.outcome is Outcome.TRUNCATED
    assert result.text == "onetwo"
