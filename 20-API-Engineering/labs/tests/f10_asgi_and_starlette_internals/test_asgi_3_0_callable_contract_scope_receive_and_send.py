"""Tests for the raw ASGI 3.0 callable contract and how real servers enforce send() order."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from asgiref.compatibility import guarantee_single_callable
from uvicorn.middleware.asgi2 import ASGI2Middleware

from api_labs.f10_asgi_and_starlette_internals.asgi_callable_contract import (
    ASGIContractError,
    ContractCheckingSend,
    LegacyASGI2App,
    Message,
    Receive,
    Scope,
    ScriptedReceive,
    Send,
    SendState,
    body_before_start_app,
    chunked_stream_app,
    classify_interface,
    contract_echo_app,
    drive_hypercorn_stream,
    drive_uvicorn_cycle,
    make_http_scope,
    marked_sync_wrapped,
    send_after_complete_app,
    sync_wrapped,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def _three_chunks() -> AsyncIterator[bytes]:
    for part in (b"alpha-", b"beta-", b"gamma"):
        yield part


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpx_transport_scope_and_streamed_request_events() -> None:
    transport = httpx.ASGITransport(app=contract_echo_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/caf%C3%A9/items?q=a%20b", content=_three_chunks())
    assert response.status_code == 200
    seen = response.json()
    assert seen["asgi_version"] == "3.0"
    assert seen["spec_version"] == "2.0"
    assert seen["method"] == "POST"
    assert seen["path"] == "/caf\u00e9/items"
    assert seen["raw_path"] == "/caf%C3%A9/items"
    assert seen["query_string"] == "q=a%20b"
    assert seen["root_path"] == ""
    assert "host" in seen["header_names"]
    assert "transfer-encoding" in seen["header_names"]
    assert all(name == name.lower() for name in seen["header_names"])
    assert seen["request_events"] == 4
    assert seen["body"] == "alpha-beta-gamma"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_direct_call_drains_more_body_and_sends_two_body_events() -> None:
    receive = ScriptedReceive([b"ab", b"cd", b"ef"])
    send = ContractCheckingSend()
    await contract_echo_app(make_http_scope("POST"), receive, send)
    assert receive.calls == 3
    assert send.state is SendState.COMPLETE
    assert send.status == 200
    assert send.body_events == 2
    assert dict(send.headers)[b"content-length"] == str(len(send.body)).encode()
    seen = json.loads(send.body)
    assert seen["request_events"] == 3
    assert seen["body"] == "abcdef"
    assert seen["spec_version"] == "2.3"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disconnect_before_body_sends_nothing() -> None:
    async def gone() -> Message:
        return {"type": "http.disconnect"}

    send = ContractCheckingSend()
    await contract_echo_app(make_http_scope("POST"), gone, send)
    assert send.state is SendState.AWAITING_START
    assert send.body_events == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_scope_type_is_rejected_with_an_exception() -> None:
    with pytest.raises(ValueError, match="unsupported ASGI scope type 'lifespan'"):
        await contract_echo_app({"type": "lifespan"}, ScriptedReceive([]), ContractCheckingSend())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uvicorn_coalesces_parser_chunks_into_one_request_event() -> None:
    run = await drive_uvicorn_cycle(
        contract_echo_app, method="POST", request_chunks=[b"ab", b"cd", b"ef"]
    )
    assert run.error is None
    head, _, body = run.output.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    assert f"content-length: {len(body)}".encode() in head
    assert b"transfer-encoding" not in head
    seen = json.loads(body)
    assert seen["request_events"] == 1
    assert seen["body"] == "abcdef"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uvicorn_chunk_encodes_a_response_without_content_length() -> None:
    run = await drive_uvicorn_cycle(chunked_stream_app)
    assert run.error is None
    assert run.output == (
        b"HTTP/1.1 200 OK\r\n"
        b"content-type: text/plain\r\n"
        b"transfer-encoding: chunked\r\n"
        b"\r\n"
        b"5\r\nhello\r\n"
        b"5\r\nworld\r\n"
        b"0\r\n\r\n"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uvicorn_raises_runtime_error_on_send_after_complete() -> None:
    run = await drive_uvicorn_cycle(send_after_complete_app)
    assert run.error == (
        "Unexpected ASGI message 'http.response.body' sent, after response already completed."
    )
    assert run.output.endswith(b"4\r\ndone\r\n0\r\n\r\n")
    assert b"late" not in run.output


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uvicorn_raises_runtime_error_on_body_before_start() -> None:
    run = await drive_uvicorn_cycle(body_before_start_app)
    assert run.error == "Expected ASGI message 'http.response.start', but got 'http.response.body'."
    assert run.output == b""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hypercorn_raises_unexpected_message_error_on_send_after_complete() -> None:
    run = await drive_hypercorn_stream(send_after_complete_app)
    assert run.events == ("Response", "Body", "EndBody", "StreamClosed")
    assert run.error == (
        "Unexpected message type, http.response.body given the state ASGIHTTPState.CLOSED"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpx_transport_asserts_on_send_after_complete() -> None:
    transport = httpx.ASGITransport(app=send_after_complete_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(AssertionError):
            await client.get("/")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_contract_send_rejects_out_of_order_events() -> None:
    scope = make_http_scope("GET")
    with pytest.raises(ASGIContractError, match="sent after the response completed"):
        await send_after_complete_app(scope, ScriptedReceive([]), ContractCheckingSend())
    with pytest.raises(ASGIContractError, match=r"expected 'http\.response\.start' first"):
        await body_before_start_app(scope, ScriptedReceive([]), ContractCheckingSend())


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "fragment"),
    [
        ({"status": 200, "headers": [(b"X-Trace", b"1")]}, "must be lowercased"),
        ({"status": 200, "headers": [(b"x-trace", b"1\r\nset-cookie: a=b")]}, "CR, LF, or NUL"),
        ({"status": 200, "headers": [(b":status", b"200")]}, "pseudo header"),
        ({"status": 200, "headers": [("x-trace", "1")]}, "must be byte strings"),
        ({"status": 103, "headers": []}, "final status must be an int"),
        ({"status": True, "headers": []}, "final status must be an int"),
        ({"status": "200", "headers": []}, "final status must be an int"),
    ],
)
async def test_contract_send_validates_response_start(
    message: dict[str, Any], fragment: str
) -> None:
    send = ContractCheckingSend()
    with pytest.raises(ASGIContractError, match=fragment):
        await send({"type": "http.response.start", **message})
    assert send.state is SendState.AWAITING_START


@pytest.mark.unit
@pytest.mark.asyncio
async def test_contract_send_rejects_non_bytes_body() -> None:
    send = ContractCheckingSend()
    await send({"type": "http.response.start", "status": 200})
    with pytest.raises(ASGIContractError, match="'body' must be bytes, got str"):
        await send({"type": "http.response.body", "body": "text"})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trailers_flag_inserts_a_trailers_state() -> None:
    send = ContractCheckingSend()
    await send({"type": "http.response.start", "status": 200, "trailers": True})
    await send({"type": "http.response.body", "body": b"x", "more_body": False})
    assert send.state is SendState.AWAITING_TRAILERS
    with pytest.raises(ASGIContractError, match=r"expected 'http\.response\.trailers'"):
        await send({"type": "http.response.body", "body": b"y"})
    await send({"type": "http.response.trailers", "headers": [(b"grpc-status", b"0")]})
    assert send.state is SendState.COMPLETE
    assert send.trailers == [(b"grpc-status", b"0")]


@pytest.mark.unit
def test_interface_detection_across_asgiref_uvicorn_and_hypercorn() -> None:
    assert classify_interface(contract_echo_app) == {
        "asgiref": "single",
        "uvicorn": "asgi3",
        "hypercorn": "asgi",
    }
    assert classify_interface(LegacyASGI2App) == {
        "asgiref": "double",
        "uvicorn": "asgi2",
        "hypercorn": "asgi",
    }
    assert classify_interface(sync_wrapped(contract_echo_app)) == {
        "asgiref": "double",
        "uvicorn": "asgi2",
        "hypercorn": "wsgi",
    }
    assert classify_interface(marked_sync_wrapped(contract_echo_app)) == {
        "asgiref": "single",
        "uvicorn": "asgi3",
        "hypercorn": "asgi",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_misdetected_sync_wrapper_breaks_under_asgi2_middleware() -> None:
    wrapped = ASGI2Middleware(sync_wrapped(contract_echo_app))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="missing 2 required positional arguments"):
        await wrapped(make_http_scope("GET"), ScriptedReceive([]), ContractCheckingSend())  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_guarantee_single_callable_adapts_legacy_double_callable() -> None:
    app = guarantee_single_callable(LegacyASGI2App)
    send = ContractCheckingSend()
    scope: Scope = make_http_scope("GET")
    receive: Receive = ScriptedReceive([])
    checked: Send = send
    await app(scope, receive, checked)
    assert send.state is SendState.COMPLETE
    assert bytes(send.body) == b"legacy:/"
