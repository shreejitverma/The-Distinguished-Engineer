"""Lab: pure ASGI middleware wrapping scope, receive, and send (starlette 1.10.0)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from api_labs.f10_asgi_and_starlette_internals.pure_asgi_middleware import (
    REQUEST_ID_SCOPE_KEY,
    BodyLimitMiddleware,
    BodyTooLargeError,
    InPlaceHeaderAppendMiddleware,
    RequestIdMiddleware,
    ServerTimingMiddleware,
    StatusRewriteMiddleware,
    header_values,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


# ---------------------------------------------------------------- helpers


def http_scope(
    headers: list[tuple[bytes, bytes]] | None = None, *, http_version: str = "1.1"
) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": http_version,
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers or [],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }


class FakeServer:
    """Feeds body chunks to receive() and records every sent message."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.receive_calls = 0
        self.sent: list[Message] = []

    async def receive(self) -> Message:
        self.receive_calls += 1
        if self.chunks:
            body = self.chunks.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(self.chunks)}
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        self.sent.append(message)

    def start(self) -> Message:
        starts = [m for m in self.sent if m["type"] == "http.response.start"]
        assert len(starts) == 1
        return starts[0]


def client_for(app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def read_body(receive: Receive) -> bytes:
    parts: list[bytes] = []
    while True:
        message = await receive()
        assert message["type"] == "http.request"
        parts.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(parts)


async def echo(request: Request) -> Response:
    return PlainTextResponse(await request.body())


class Tag:
    def __init__(self, app: ASGIApp, *, name: str, events: list[str]) -> None:
        self.app = app
        self.name = name
        self.events = events

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.events.append(f"{self.name}:in")
        await self.app(scope, receive, send)
        self.events.append(f"{self.name}:out")


def fixed_ids(prefix: str) -> Iterator[str]:
    n = 0
    while True:
        yield f"{prefix}{n}"
        n += 1


# ---------------------------------------------------------------- onion order


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_middleware_list_is_outermost_first_and_add_middleware_prepends() -> None:
    events: list[str] = []

    async def endpoint(request: Request) -> Response:
        events.append("endpoint")
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/", endpoint)],
        middleware=[
            Middleware(Tag, name="a", events=events),
            Middleware(Tag, name="b", events=events),
        ],
    )
    app.add_middleware(Tag, name="c", events=events)
    async with client_for(app) as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert events == ["c:in", "a:in", "b:in", "endpoint", "b:out", "a:out", "c:out"]


# ---------------------------------------------------------------- zero copy


@pytest.mark.unit
@pytest.mark.asyncio
async def test_body_chunks_pass_through_every_layer_by_identity() -> None:
    in_chunks = [bytes(range(256)) * 4, bytes(1000)]
    out_chunks = [b"x" * 70_000, b"y" * 10]
    seen: list[bytes] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            seen.append(message["body"])
            if not message["more_body"]:
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": out_chunks[0], "more_body": True})
        await send({"type": "http.response.body", "body": out_chunks[1], "more_body": False})

    stack = RequestIdMiddleware(
        ServerTimingMiddleware(
            BodyLimitMiddleware(
                StatusRewriteMiddleware(inner, mapping={404: 410}), max_body_size=4096
            )
        )
    )
    server = FakeServer(in_chunks)
    expected_in = list(in_chunks)
    await stack(http_scope(), server.receive, server.send)

    assert all(a is b for a, b in zip(seen, expected_in, strict=True))
    bodies = [m["body"] for m in server.sent if m["type"] == "http.response.body"]
    assert all(a is b for a, b in zip(bodies, out_chunks, strict=True))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_header_edits_do_not_mutate_a_reused_response_object() -> None:
    shared = PlainTextResponse("ok")
    before = list(shared.raw_headers)

    async def endpoint(request: Request) -> Response:
        return shared

    app = Starlette(routes=[Route("/", endpoint)], middleware=[Middleware(RequestIdMiddleware)])
    async with client_for(app) as client:
        responses = [await client.get("/") for _ in range(3)]
    assert [len(r.headers.get_list("x-request-id")) for r in responses] == [1, 1, 1]
    assert shared.raw_headers == before


@pytest.mark.unit
@pytest.mark.asyncio
async def test_in_place_header_append_leaks_across_requests_on_a_reused_response() -> None:
    shared = PlainTextResponse("ok")

    async def endpoint(request: Request) -> Response:
        return shared

    app = Starlette(
        routes=[Route("/", endpoint)],
        middleware=[Middleware(InPlaceHeaderAppendMiddleware, name=b"x-leak", value=b"1")],
    )
    async with client_for(app) as client:
        responses = [await client.get("/") for _ in range(3)]
    assert [len(r.headers.get_list("x-leak")) for r in responses] == [1, 2, 3]
    assert header_values(shared.raw_headers, b"x-leak") == [b"1", b"1", b"1"]


# ---------------------------------------------------------------- request id and scope


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inbound", "expected"),
    [
        ([(b"x-request-id", b"abc-123.Z_9")], b"abc-123.Z_9"),
        ([(b"x-request-id", b"evil\r\nset-cookie: a=b")], b"generated0"),
        ([(b"x-request-id", b"a" * 65)], b"generated0"),
        ([(b"x-request-id", b"one"), (b"x-request-id", b"two")], b"generated0"),
        ([], b"generated0"),
    ],
)
async def test_request_id_echoes_a_safe_inbound_id_and_replaces_anything_else(
    inbound: list[tuple[bytes, bytes]], expected: bytes
) -> None:
    ids = fixed_ids("generated")
    seen_in_scope: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen_in_scope.append(scope[REQUEST_ID_SCOPE_KEY])
        await PlainTextResponse("ok")(scope, receive, send)

    server = FakeServer([b""])
    app = RequestIdMiddleware(inner, id_factory=lambda: next(ids))
    await app(http_scope(inbound), server.receive, server.send)
    assert header_values(server.start()["headers"], b"x-request-id") == [expected]
    assert seen_in_scope == [expected.decode()]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_id_scope_copy_does_not_leak_upstream() -> None:
    scope = http_scope()
    inner_scopes: list[Scope] = []

    async def inner(child: Scope, receive: Receive, send: Send) -> None:
        inner_scopes.append(child)
        await PlainTextResponse("ok")(child, receive, send)

    server = FakeServer([b""])
    await RequestIdMiddleware(inner)(scope, server.receive, server.send)
    assert REQUEST_ID_SCOPE_KEY in inner_scopes[0]
    assert REQUEST_ID_SCOPE_KEY not in scope
    assert inner_scopes[0] is not scope


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scope_copy_hides_router_writes_from_outer_middleware() -> None:
    observed: list[object] = []

    class RouteProbe:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await self.app(scope, receive, send)
            if scope["type"] == "http":
                observed.append(scope.get("route"))

    async def endpoint(request: Request) -> Response:
        return PlainTextResponse("ok")

    route = Route("/", endpoint)
    direct = Starlette(routes=[route], middleware=[Middleware(RouteProbe)])
    copied = Starlette(
        routes=[route], middleware=[Middleware(RouteProbe), Middleware(RequestIdMiddleware)]
    )
    for app in (direct, copied):
        async with client_for(app) as client:
            assert (await client.get("/")).status_code == 200
    assert observed == [route, None]


@pytest.mark.unit
def test_starlette_headers_from_scope_rewrites_scope_headers_in_place() -> None:
    scope: dict[str, Any] = {"type": "http", "headers": ((b"a", b"1"),)}
    headers = Headers(scope=scope)
    assert headers["a"] == "1"
    assert scope["headers"] == [(b"a", b"1")]
    assert isinstance(scope["headers"], list)


# ---------------------------------------------------------------- server timing


@pytest.mark.unit
@pytest.mark.asyncio
async def test_server_timing_measures_time_to_response_start_and_keeps_app_metrics() -> None:
    ticks = iter([1_000_000, 13_500_000])

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok", headers={"server-timing": "db;dur=3"})(scope, receive, send)

    server = FakeServer([b""])
    app = ServerTimingMiddleware(inner, clock_ns=lambda: next(ticks))
    await app(http_scope(), server.receive, server.send)
    assert header_values(server.start()["headers"], b"server-timing") == [
        b"db;dur=3",
        b"app;dur=12.500",
    ]


# ---------------------------------------------------------------- body limit


@pytest.mark.unit
@pytest.mark.asyncio
async def test_declared_oversize_is_rejected_before_the_app_or_receive_runs() -> None:
    called: list[bool] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        called.append(True)

    server = FakeServer([b"x" * 11])
    scope = http_scope([(b"content-length", b"11")])
    await BodyLimitMiddleware(inner, max_body_size=10)(scope, server.receive, server.send)
    assert called == []
    assert server.receive_calls == 0
    assert server.start()["status"] == 413
    assert header_values(server.start()["headers"], b"connection") == [b"close"]
    assert server.sent[-1]["body"] == b"Content Too Large"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streamed_body_is_cut_off_before_the_app_sees_the_excess() -> None:
    delivered: list[bytes] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            delivered.append(message["body"])
            if not message["more_body"]:
                break
        await PlainTextResponse("ok")(scope, receive, send)

    server = FakeServer([b"a" * 4, b"b" * 4, b"c" * 4])
    scope = http_scope([(b"transfer-encoding", b"chunked")])
    await BodyLimitMiddleware(inner, max_body_size=10)(scope, server.receive, server.send)
    assert delivered == [b"aaaa", b"bbbb"]
    assert server.start()["status"] == 413


@pytest.mark.unit
@pytest.mark.asyncio
async def test_understated_content_length_is_still_counted() -> None:
    server = FakeServer([b"x" * 6, b"y" * 6])

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await read_body(receive)
        await PlainTextResponse("ok")(scope, receive, send)

    scope = http_scope([(b"content-length", b"4")])
    await BodyLimitMiddleware(inner, max_body_size=10)(scope, server.receive, server.send)
    assert server.start()["status"] == 413


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_that_swallows_the_limit_error_cannot_answer() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        with contextlib.suppress(BodyTooLargeError):
            await read_body(receive)
        await PlainTextResponse("partial body accepted")(scope, receive, send)

    server = FakeServer([b"x" * 8, b"y" * 8])
    await BodyLimitMiddleware(inner, max_body_size=10)(http_scope(), server.receive, server.send)
    assert server.start()["status"] == 413
    assert server.sent[-1]["body"] == b"Content Too Large"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_limit_hit_after_response_start_propagates_instead_of_a_second_start() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await read_body(receive)

    server = FakeServer([b"x" * 8, b"y" * 8])
    app = BodyLimitMiddleware(inner, max_body_size=10)
    with pytest.raises(BodyTooLargeError):
        await app(http_scope(), server.receive, server.send)
    assert [m["status"] for m in server.sent] == [200]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (b"1_0", 400),
        (b"+5", 400),
        (b"-1", 400),
        (b"\xb2", 400),
        (b"5, 6", 400),
        (b"7, 7", 200),
        (b"7", 200),
    ],
)
async def test_content_length_is_parsed_as_1_digit_not_as_python_int(
    raw: bytes, status: int
) -> None:
    server = FakeServer([b"x" * 7])

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await read_body(receive)
        await PlainTextResponse("ok")(scope, receive, send)

    scope = http_scope([(b"content-length", raw)])
    await BodyLimitMiddleware(inner, max_body_size=10)(scope, server.receive, server.send)
    assert server.start()["status"] == status


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_body_limit_trusts_python_int_for_content_length() -> None:
    assert int("1_0") == 10
    assert int("+5") == 5
    server = FakeServer([b"x" * 3])

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await read_body(receive)
        await PlainTextResponse("ok")(scope, receive, send)

    scope = http_scope([(b"content-length", b"1_0")])
    app = RequestBodyLimitMiddleware(inner, max_body_size=5)
    await app(scope, server.receive, server.send)
    assert server.start()["status"] == 413


@pytest.mark.unit
@pytest.mark.asyncio
async def test_connection_close_is_added_only_for_http_1() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("not reached")

    results: dict[str, list[bytes]] = {}
    for version in ("1.1", "2"):
        server = FakeServer([])
        scope = http_scope([(b"content-length", b"99")], http_version=version)
        await BodyLimitMiddleware(inner, max_body_size=10)(scope, server.receive, server.send)
        results[version] = header_values(server.start()["headers"], b"connection")
    assert results == {"1.1": [b"close"], "2": []}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_route_max_body_size_overrides_the_app_limit_both_ways() -> None:
    app = Starlette(
        max_body_size=8,
        routes=[
            Route("/loose", echo, methods=["POST"], max_body_size=64),
            Route("/strict", echo, methods=["POST"]),
        ],
    )
    async with client_for(app) as client:
        loose = await client.post("/loose", content=b"z" * 32)
        strict = await client.post("/strict", content=b"z" * 32)
    assert (loose.status_code, len(loose.content)) == (200, 32)
    assert strict.status_code == 413


@pytest.mark.unit
def test_fastapi_constructor_has_no_max_body_size_parameter() -> None:
    assert "max_body_size" in inspect.signature(Starlette.__init__).parameters
    assert "max_body_size" not in inspect.signature(FastAPI.__init__).parameters


# ---------------------------------------------------------------- status rewrite


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_rewrite_maps_404_to_410_and_keeps_the_body() -> None:
    app = Starlette(
        routes=[Route("/", echo)],
        middleware=[Middleware(StatusRewriteMiddleware, mapping={404: 410})],
    )
    async with client_for(app) as client:
        response = await client.get("/retired")
    assert (response.status_code, response.text) == (410, "Not Found")


@pytest.mark.unit
@pytest.mark.parametrize("target", [101, 204, 304, 600])
def test_status_rewrite_refuses_targets_that_forbid_a_body(target: int) -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("not reached")

    with pytest.raises(ValueError, match=str(target)):
        StatusRewriteMiddleware(inner, mapping={200: target})


# ---------------------------------------------------------------- pass-through rules


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
async def test_non_http_scopes_reach_the_app_with_the_original_callables(scope_type: str) -> None:
    scope: Scope = {"type": scope_type}
    server = FakeServer([])
    seen: list[tuple[object, object, object]] = []

    async def inner(s: Scope, receive: Receive, send: Send) -> None:
        seen.append((s, receive, send))

    layers: list[ASGIApp] = [
        RequestIdMiddleware(inner),
        ServerTimingMiddleware(inner),
        BodyLimitMiddleware(inner, max_body_size=0),
        StatusRewriteMiddleware(inner, mapping={404: 410}),
    ]
    for layer in layers:
        await layer(scope, server.receive, server.send)
    assert all(s is scope for s, _, _ in seen)
    assert all(r == server.receive and w == server.send for _, r, w in seen)
    assert len(seen) == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_send_events_such_as_trailers_are_forwarded() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": [], "trailers": True})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})
        await send({"type": "http.response.trailers", "headers": [(b"x-sum", b"1")]})

    stack = RequestIdMiddleware(
        ServerTimingMiddleware(
            BodyLimitMiddleware(StatusRewriteMiddleware(inner, mapping={}), max_body_size=10)
        )
    )
    server = FakeServer([b""])
    await stack(http_scope(), server.receive, server.send)
    assert [m["type"] for m in server.sent] == [
        "http.response.start",
        "http.response.body",
        "http.response.trailers",
    ]
    assert server.sent[0]["trailers"] is True


# ---------------------------------------------------------------- end to end


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_requests_keep_per_call_state_separate() -> None:
    async def slow_echo(request: Request) -> Response:
        await asyncio.sleep(0)
        return PlainTextResponse(request.scope[REQUEST_ID_SCOPE_KEY])

    app = Starlette(
        routes=[Route("/", slow_echo)],
        middleware=[Middleware(RequestIdMiddleware), Middleware(ServerTimingMiddleware)],
    )
    async with client_for(app) as client:
        responses = await asyncio.gather(
            *(client.get("/", headers={"x-request-id": f"r{i}"}) for i in range(50))
        )
    assert [r.headers["x-request-id"] for r in responses] == [f"r{i}" for i in range(50)]
    assert [r.text for r in responses] == [f"r{i}" for i in range(50)]
    assert all(r.headers["server-timing"].startswith("app;dur=") for r in responses)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_full_stack_through_httpx_tags_even_the_413_with_a_request_id() -> None:
    app = Starlette(
        routes=[Route("/", echo, methods=["POST"])],
        middleware=[
            Middleware(RequestIdMiddleware),
            Middleware(ServerTimingMiddleware),
            Middleware(BodyLimitMiddleware, max_body_size=16),
        ],
    )

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b"0123456789"

    async with client_for(app) as client:
        ok = await client.post("/", content=b"hello")
        declared = await client.post("/", content=b"x" * 17)
        streamed = await client.post("/", content=chunks())
    assert (ok.status_code, ok.text) == (200, "hello")
    for response in (declared, streamed):
        assert response.status_code == 413
        assert response.headers["connection"] == "close"
        assert len(response.headers["x-request-id"]) == 32
        assert response.headers["server-timing"].startswith("app;dur=")
