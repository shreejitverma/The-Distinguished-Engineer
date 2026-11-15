"""Which Starlette layer converts an exception into a response, and what happens once none can."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.middleware.errors import ServerErrorMiddleware
from starlette.routing import Route, Router

from api_labs.f10_asgi_and_starlette_internals.exception_onion import (
    LateFailure,
    Recorder,
    StreamBoom,
    TeapotError,
    build_app,
    describe_stack,
    raw_http11_exchange,
    serve_uvicorn,
    server_error_handler,
    unhandled,
)

if TYPE_CHECKING:
    from starlette.applications import Starlette


def client(app: Starlette, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.unit
def test_stack_order_server_error_outermost_exception_middleware_innermost() -> None:
    assert describe_stack(build_app([])) == [
        "ServerErrorMiddleware",
        "Recorder(outer)",
        "Recorder(inner)",
        "ExceptionMiddleware",
        "Router",
    ]


@pytest.mark.unit
def test_add_middleware_lands_just_inside_server_error_middleware() -> None:
    app = build_app([])
    app.add_middleware(Recorder, name="late", log=[])
    assert describe_stack(app)[:3] == ["ServerErrorMiddleware", "Recorder(late)", "Recorder(outer)"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_exception_is_a_response_by_the_time_user_middleware_sees_it() -> None:
    log: list[str] = []
    async with client(build_app(log)) as c:
        response = await c.get("/http-exception")
    assert (response.status_code, response.text) == (418, "short and stout")
    assert log == [
        "outer:enter",
        "inner:enter",
        "inner:start:418",
        "outer:start:418",
        "inner:exit",
        "outer:exit",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registered_class_handler_converts_domain_exception() -> None:
    log: list[str] = []
    async with client(build_app(log)) as c:
        response = await c.get("/handled")
    assert response.status_code == 409
    assert response.json() == {"error": "teapot", "detail": "brewing"}
    assert "outer:raised:TeapotError" not in log


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unhandled_exception_crosses_user_middleware_then_becomes_500() -> None:
    log: list[str] = []
    async with client(build_app(log), raise_app_exceptions=False) as c:
        response = await c.get("/unhandled")
    assert (response.status_code, response.text) == (500, "Internal Server Error")
    assert log == [
        "outer:enter",
        "inner:enter",
        "inner:raised:ValueError",
        "outer:raised:ValueError",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_server_error_middleware_reraises_after_sending_500() -> None:
    async with client(build_app([])) as c:
        with pytest.raises(ValueError, match="boom"):
            await c.get("/unhandled")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exception_key_handler_is_moved_to_server_error_middleware() -> None:
    log: list[str] = []
    app = build_app(log, custom_500=True)
    outermost = app.build_middleware_stack()
    assert isinstance(outermost, ServerErrorMiddleware)
    assert outermost.handler is server_error_handler
    async with client(app, raise_app_exceptions=False) as c:
        response = await c.get("/unhandled")
    assert response.status_code == 500
    assert response.json() == {"error": "internal", "type": "ValueError"}
    assert "outer:raised:ValueError" in log


@pytest.mark.unit
@pytest.mark.asyncio
async def test_500_bypasses_cors_but_handled_errors_keep_cors_headers() -> None:
    app = build_app([], cors=True)
    headers = {"Origin": "https://app.example"}
    async with client(app, raise_app_exceptions=False) as c:
        handled_response = await c.get("/http-exception", headers=headers)
        crashed_response = await c.get("/unhandled", headers=headers)
    assert handled_response.headers["access-control-allow-origin"] == "https://app.example"
    assert crashed_response.status_code == 500
    assert "access-control-allow-origin" not in crashed_response.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mid_stream_unhandled_exception_keeps_status_200_and_partial_body() -> None:
    log: list[str] = []
    async with client(build_app(log), raise_app_exceptions=False) as c:
        response = await c.get("/stream-unhandled")
    assert (response.status_code, response.content) == (200, b"chunk-1\n")
    assert log[-2:] == ["inner:raised:StreamBoom", "outer:raised:StreamBoom"]
    assert "outer:start:200" in log


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mid_stream_unhandled_exception_propagates_unwrapped() -> None:
    async with client(build_app([])) as c:
        with pytest.raises(StreamBoom, match="generator failed") as info:
            await c.get("/stream-unhandled")
    assert type(info.value) is StreamBoom


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mid_stream_handled_exception_becomes_runtime_error() -> None:
    async with client(build_app([])) as c:
        with pytest.raises(RuntimeError, match="response already started") as info:
            await c.get("/stream-handled")
    assert isinstance(info.value.__cause__, TeapotError)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_background_task_failure_cannot_change_completed_response() -> None:
    log: list[str] = []
    async with client(build_app(log), raise_app_exceptions=False) as c:
        response = await c.get("/background-fails")
    assert (response.status_code, response.text) == (200, "accepted")
    assert log[-1] == "outer:raised:LateFailure"
    async with client(build_app([])) as c:
        with pytest.raises(LateFailure):
            await c.get("/background-fails")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_debug_mode_leaks_exception_text_production_mode_does_not() -> None:
    async with client(build_app([], debug=True), raise_app_exceptions=False) as c:
        debug_response = await c.get("/leaky")
    async with client(build_app([]), raise_app_exceptions=False) as c:
        prod_response = await c.get("/leaky")
    assert "example-password" in debug_response.text
    assert debug_response.headers["content-type"].startswith("text/plain")
    assert prod_response.text == "Internal Server Error"


@pytest.mark.socket
@pytest.mark.asyncio
async def test_uvicorn_wire_unhandled_before_start_is_starlette_500() -> None:
    async with serve_uvicorn(build_app([])) as (host, port):
        raw = await raw_http11_exchange(host, port, "/unhandled")
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 500 Internal Server Error\r\n")
    assert b"content-length: 21" in head
    assert body == b"Internal Server Error"


@pytest.mark.socket
@pytest.mark.asyncio
async def test_uvicorn_wire_mid_stream_exception_truncates_chunked_body() -> None:
    async with serve_uvicorn(build_app([])) as (host, port):
        raw = await raw_http11_exchange(host, port, "/stream-unhandled")
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Transfer-Encoding: chunked" in head
    assert body == b"8\r\nchunk-1\n\r\n"
    assert not body.endswith(b"0\r\n\r\n")


@pytest.mark.socket
@pytest.mark.asyncio
async def test_uvicorn_writes_its_own_chunked_500_when_no_server_error_middleware() -> None:
    async with serve_uvicorn(Router([Route("/unhandled", unhandled)])) as (host, port):
        raw = await raw_http11_exchange(host, port, "/unhandled")
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 500 Internal Server Error\r\n")
    assert b"content-length" not in head
    assert body == b"15\r\nInternal Server Error\r\n0\r\n\r\n"


@pytest.mark.socket
@pytest.mark.asyncio
async def test_real_client_sees_protocol_error_not_a_status() -> None:
    async with (
        serve_uvicorn(build_app([])) as (host, port),
        httpx.AsyncClient(base_url=f"http://{host}:{port}") as c,
    ):
        with pytest.raises(httpx.RemoteProtocolError, match="incomplete chunked read"):
            await c.get("/stream-unhandled")
