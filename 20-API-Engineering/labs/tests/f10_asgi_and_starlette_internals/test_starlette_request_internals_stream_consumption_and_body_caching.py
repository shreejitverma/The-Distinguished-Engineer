"""Starlette 1.10.0 Request.stream() vs Request.body(): caching, starvation, and replay."""

from __future__ import annotations

import hashlib
import tracemalloc
from typing import TYPE_CHECKING

import anyio
import httpx
import pytest
from fastapi import Depends, FastAPI
from pydantic import BaseModel
from starlette.middleware import Middleware
from starlette.requests import ClientDisconnect, Request

from api_labs.f10_asgi_and_starlette_internals.request_body_replay import (
    BodyLog,
    BodyReadingMiddleware,
    BodyReplayMiddleware,
    NaiveBodyLoggerMiddleware,
    ScriptedReceive,
    StreamPeekingMiddleware,
    body_messages,
    build_app,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

PAYLOAD = b"x" * 3000


def http_scope(path: str, headers: list[tuple[bytes, bytes]] | None = None) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }


def client_for(app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def chunked(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


class CountingReceive:
    """Outermost middleware that counts receive() calls the whole stack makes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.calls = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def counted() -> Message:
            self.calls += 1
            return await receive()

        await self.app(scope, counted, send)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_body_is_cached_per_instance_and_stream_replays_it() -> None:
    receive = ScriptedReceive(body_messages(b"ab", b"cd", b"ef"))
    request = Request(http_scope("/"), receive)

    assert await request.body() == b"abcdef"
    assert await request.body() == b"abcdef"
    replayed = [chunk async for chunk in request.stream()]

    assert replayed == [b"abcdef", b""]
    assert receive.calls == 3  # one per http.request event, none for the cached reads


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_then_body_on_same_instance_raises_stream_consumed() -> None:
    request = Request(http_scope("/"), ScriptedReceive(body_messages(b"ab", b"cd")))
    seen = [chunk async for chunk in request.stream()]

    assert seen == [b"ab", b"cd", b""]
    with pytest.raises(RuntimeError, match=r"^Stream consumed$"):
        await request.body()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_abandoned_partial_stream_silently_truncates_the_next_reader() -> None:
    request = Request(http_scope("/"), ScriptedReceive(body_messages(b"ab", b"cd", b"ef")))
    async for _first in request.stream():
        break  # _stream_consumed is still False, so no error on the next read

    assert await request.body() == b"cdef"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_second_request_instance_does_not_share_the_cache() -> None:
    receive = ScriptedReceive(body_messages(b"payload"))
    scope = http_scope("/")
    assert await Request(scope, receive).body() == b"payload"

    with pytest.raises(ClientDisconnect):
        await Request(scope, receive).body()  # the channel now only has http.disconnect


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naive_middleware_starves_handler_until_the_client_gives_up() -> None:
    log = BodyLog()
    app = build_app([Middleware(NaiveBodyLoggerMiddleware, log=log)])
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    receive = ScriptedReceive(body_messages(b"ab", b"cd"))
    with pytest.raises(ClientDisconnect):
        await app(http_scope("/echo"), receive, send)

    assert log.sizes == [4]
    assert receive.calls == 3  # two body events for the logger, one disconnect for the handler
    assert sent[0]["status"] == 500  # ServerErrorMiddleware answers a client that already left


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naive_middleware_hangs_forever_under_httpx_asgitransport() -> None:
    log = BodyLog()
    app = build_app([Middleware(NaiveBodyLoggerMiddleware, log=log)])

    async with client_for(app) as client:
        with pytest.raises(TimeoutError):
            with anyio.fail_after(0.25):
                await client.post("/echo", content=PAYLOAD)

    assert log.sizes == [len(PAYLOAD)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replay_middleware_feeds_the_handler_the_full_body() -> None:
    log = BodyLog()
    app = build_app([Middleware(BodyReplayMiddleware, log=log, max_body_size=1 << 20)])

    async with client_for(app) as client:
        response = await client.post("/echo", content=PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {
        "length": len(PAYLOAD),
        "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
    }
    assert log.sizes == [len(PAYLOAD)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replay_middleware_preserves_chunk_boundaries() -> None:
    log = BodyLog()
    app = build_app([Middleware(BodyReplayMiddleware, log=log, max_body_size=1 << 20)])

    async with client_for(app) as client:
        response = await client.post("/chunks", content=chunked(b"a" * 10, b"b" * 20, b"c" * 30))

    assert response.json() == {"chunks": [10, 20, 30]}
    assert log.chunk_counts == [3]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("declare_length", [True, False])
async def test_replay_middleware_rejects_oversized_bodies_with_413(declare_length: bool) -> None:
    log = BodyLog()
    app = build_app([Middleware(BodyReplayMiddleware, log=log, max_body_size=1024)])
    content = PAYLOAD if declare_length else chunked(PAYLOAD[:1000], PAYLOAD[1000:])

    async with client_for(app) as client:
        response = await client.post("/echo", content=content)

    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert log.sizes == []  # never handed to the endpoint


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basehttpmiddleware_stream_in_dispatch_gives_endpoint_empty_body() -> None:
    log = BodyLog()
    app = build_app([Middleware(StreamPeekingMiddleware, log=log)])

    async with client_for(app) as client:
        response = await client.post("/echo", content=PAYLOAD)

    assert response.status_code == 200
    assert response.json()["length"] == 0  # silent data loss, not an error
    assert log.sizes == [len(PAYLOAD)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basehttpmiddleware_reread_after_streaming_raises_stream_consumed() -> None:
    app = build_app([Middleware(StreamPeekingMiddleware, log=BodyLog(), reread_after=True)])

    async with client_for(app) as client:
        with pytest.raises(RuntimeError, match=r"^Stream consumed$"):
            await client.post("/echo", content=PAYLOAD)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basehttpmiddleware_body_in_dispatch_is_replayed_to_endpoint() -> None:
    log = BodyLog()
    app = build_app([Middleware(BodyReadingMiddleware, log=log)])

    async with client_for(app) as client:
        response = await client.post("/echo", content=PAYLOAD)

    assert response.json()["length"] == len(PAYLOAD)
    assert log.sizes == [len(PAYLOAD)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_then_body_in_endpoint_raises_stream_consumed() -> None:
    async with client_for(build_app()) as client:
        with pytest.raises(RuntimeError, match=r"^Stream consumed$"):
            await client.post("/stream-then-body", content=PAYLOAD)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_body_then_stream_in_endpoint_replays_one_chunk() -> None:
    async with client_for(build_app()) as client:
        response = await client.post("/body-then-stream", content=chunked(b"a" * 5, b"b" * 7))

    assert response.json() == {"length": 12, "replayed": [12, 0]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_is_disconnected_discards_a_ready_body_message() -> None:
    receive = ScriptedReceive(body_messages(b"first", b"second"))
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await build_app()(http_scope("/peek-then-body"), receive, send)

    assert sent[1]["body"] == b'{"disconnected":false,"length":6}'  # b"first" was eaten


@pytest.mark.unit
@pytest.mark.asyncio
async def test_is_disconnected_loses_the_whole_single_chunk_body_under_httpx() -> None:
    async with client_for(build_app()) as client:
        response = await client.post("/peek-then-body", content=PAYLOAD)

    assert response.json() == {"disconnected": False, "length": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpx_asgitransport_sends_a_trailing_empty_body_event() -> None:
    counter = CountingReceive(build_app())

    async with client_for(counter) as client:
        response = await client.post("/echo", content=PAYLOAD)

    assert response.json()["length"] == len(PAYLOAD)
    assert counter.calls == 2  # {body: PAYLOAD, more_body: True} then {body: b"", more_body: False}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("size", "status"), [(1024, 200), (1025, 413)])
async def test_starlette_max_body_size_boundary(size: int, status: int) -> None:
    app = build_app(max_body_size=1024)

    async with client_for(app) as client:
        declared = await client.post("/echo", content=b"x" * size)
        streamed = await client.post("/echo", content=chunked(b"x" * 512, b"x" * (size - 512)))

    assert (declared.status_code, streamed.status_code) == (status, status)


class Item(BaseModel):
    name: str


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_dependency_and_body_param_share_one_request_cache() -> None:
    api = FastAPI()
    audited: list[int] = []

    async def audit(request: Request) -> None:
        audited.append(len(await request.body()))

    @api.post("/items", dependencies=[Depends(audit)])
    async def create(item: Item) -> dict[str, str]:
        return {"name": item.name}

    counter = CountingReceive(api)
    async with client_for(counter) as client:
        response = await client.post("/items", json={"name": "widget"})

    assert response.json() == {"name": "widget"}
    assert audited == [len(b'{"name":"widget"}')]
    assert counter.calls == 2  # one read off the wire, served twice from Request._body


@pytest.mark.unit
def test_body_join_holds_about_twice_the_body_at_peak() -> None:
    size = 4 * 1024 * 1024
    chunk = 64 * 1024

    async def run() -> int:
        remaining = size

        async def receive() -> Message:
            nonlocal remaining
            remaining -= chunk
            return {"type": "http.request", "body": b"x" * chunk, "more_body": remaining > 0}

        return len(await Request(http_scope("/"), receive).body())

    assert anyio.run(run) == size  # warm-up: a cold anyio.run allocates about 3.9 MB once
    tracemalloc.start()
    try:
        assert anyio.run(run) == size
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak >= 2 * size  # 64 retained chunks plus the joined copy coexist
    assert peak < 3 * size
