"""Deterministic verification tests for Hypercorn 0.18.0 ASGI worker architecture.

Covers:
- Out-of-order multiplexed stream completion over a single virtual connection (h2 4.4.1)
- ASGI extension negotiation (trailers, server push, early hints)
- Flow-control window enforcement and StreamBuffer backpressure
- StreamBuffer low-water check on popped-chunk size: exact multiples and zero-window pops
- Cleartext HTTP/2 direct preface detection and dynamic protocol upgrade in ProtocolWrapper
- QUIC Initial datagram validation and anti-amplification threshold (aioquic 1.7.2)
- RFC 8441 Extended CONNECT dispatch to WSStream for WebSockets over HTTP/2
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from hypercorn.app_wrappers import ASGIWrapper
from hypercorn.asyncio.task_group import TaskGroup
from hypercorn.asyncio.worker_context import WorkerContext
from hypercorn.config import Config
from hypercorn.events import Closed, RawData
from hypercorn.protocol import ProtocolWrapper
from hypercorn.protocol.h2 import H2Protocol
from hypercorn.protocol.h11 import H11Protocol
from hypercorn.protocol.ws_stream import WSStream
from hypercorn.typing import (
    ASGIReceiveCallable,
    ASGISendCallable,
    ASGISendEvent,
    ConnectionState,
    Scope,
)

from api_labs.f12_asgi_servers_and_async_runtime.hypercorn_worker import (
    InProcessH2Harness,
    StreamBufferAudit,
    parse_quic_initial_header,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_h2_concurrent_stream_multiplexing_out_of_order_completion() -> None:
    """Verify concurrent HTTP/2 streams over a single connection complete out-of-order."""
    slow_stream_gate = asyncio.Event()

    async def multiplexed_app(
        scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable
    ) -> None:
        if scope["type"] != "http":
            return
        path = scope["path"]
        if path == "/slow":
            await slow_stream_gate.wait()

        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": f"done-{path}".encode(),
                "more_body": False,
            }
        )

    async with InProcessH2Harness(multiplexed_app) as harness:
        # Stream 1: Slow request
        await harness.send_request(
            stream_id=1,
            headers=[
                (b":method", b"GET"),
                (b":scheme", b"http"),
                (b":authority", b"api.local"),
                (b":path", b"/slow"),
            ],
            end_stream=True,
        )

        # Stream 3: Fast request on the EXACT SAME in-memory virtual connection
        await harness.send_request(
            stream_id=3,
            headers=[
                (b":method", b"GET"),
                (b":scheme", b"http"),
                (b":authority", b"api.local"),
                (b":path", b"/fast"),
            ],
            end_stream=True,
        )

        # Allow fast stream to complete while slow stream is blocked
        await harness.drain_server()
        assert 3 in harness.responses
        assert harness.responses[3].ended is True
        assert harness.responses[3].status_code == 200
        assert bytes(harness.responses[3].body) == b"done-/fast"

        # Slow stream 1 must not be finished yet
        assert harness.responses[1].ended is False
        assert harness.completion_order == [3]

        # Release slow stream gate and assert completion
        slow_stream_gate.set()
        await harness.drain_server()

        assert harness.responses[1].ended is True
        assert harness.responses[1].status_code == 200
        assert bytes(harness.responses[1].body) == b"done-/slow"
        assert harness.completion_order == [3, 1]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_extensions_injected_in_h2_scope() -> None:
    """Verify Hypercorn injects trailers, server push, and early hints into HTTP/2 scopes."""
    captured_extensions: dict[str, Any] = {}

    async def inspecting_app(
        scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable
    ) -> None:
        if scope["type"] == "http":
            for k in scope["extensions"]:
                captured_extensions[str(k)] = True
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"",
                "more_body": False,
            }
        )

    async with InProcessH2Harness(inspecting_app) as harness:
        await harness.send_request(
            stream_id=1,
            headers=[
                (b":method", b"GET"),
                (b":scheme", b"http"),
                (b":authority", b"api.local"),
                (b":path", b"/inspect"),
            ],
            end_stream=True,
        )
        await harness.drain_server()

    assert "http.response.trailers" in captured_extensions
    assert "http.response.push" in captured_extensions
    assert "http.response.early_hint" in captured_extensions


@pytest.mark.unit
@pytest.mark.asyncio
async def test_h2_extended_connect_dispatches_to_ws_stream() -> None:
    """Verify HTTP/2 Extended CONNECT (RFC 8441) routes to WSStream for WebSocket support."""

    async def dummy_app(scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
        pass

    async with InProcessH2Harness(dummy_app) as harness:
        await harness.send_request(
            stream_id=1,
            headers=[
                (b":method", b"CONNECT"),
                (b":protocol", b"websocket"),
                (b":scheme", b"https"),
                (b":authority", b"api.local"),
                (b":path", b"/ws"),
            ],
            end_stream=False,
        )
        proto = harness.server_protocol
        assert 1 in proto.streams
        assert isinstance(proto.streams[1], WSStream)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_h2_stream_buffer_backpressure_and_exact_multiple_pause() -> None:
    """StreamBuffer resumes on popped-chunk size, so an exact multiple waits for an empty pop."""
    audit = StreamBufferAudit()

    # High-water mark is 32768 bytes; push 49152 bytes (3 x 16384).
    task = await audit.push_chunk(b"x" * 49152)
    await asyncio.sleep(0.01)
    assert task.done() is False
    assert audit.buffer_length == 49152

    # pop checks `len(data) < BUFFER_LOW_WATER`; 16384 < 16384.0 is False,
    # so three full-frame pops never release the producer.
    for remaining in (32768, 16384, 0):
        chunk = await audit.pop_chunk(16384)
        assert len(chunk) == 16384
        await asyncio.sleep(0.01)
        assert task.done() is False
        assert audit.buffer_length == remaining

    # The buffer is empty but the producer is still paused; the next pop
    # returns b"" (length 0 < 16384.0) and that is what releases it.
    assert await audit.pop_chunk(16384) == b""
    await asyncio.sleep(0.01)
    assert task.done() is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_buffer_zero_length_pop_releases_producer() -> None:
    """A pop(0), issued by H2Protocol when the flow-control window is 0, releases the producer."""
    audit = StreamBufferAudit()
    task = await audit.push_chunk(b"x" * 40000)
    await asyncio.sleep(0.01)
    assert task.done() is False

    assert await audit.pop_chunk(0) == b""
    await asyncio.sleep(0.01)
    assert task.done() is True
    # Nothing drained: the producer was released above the high-water mark.
    assert audit.buffer_length == 40000


def _get_request(path: bytes) -> list[tuple[bytes, bytes]]:
    return [
        (b":method", b"GET"),
        (b":scheme", b"http"),
        (b":authority", b"api.local"),
        (b":path", path),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_h2_exact_multiple_body_completes_end_to_end() -> None:
    """A 49152-byte body (3 x 16384) does not hang: send_task's empty pop releases the producer."""
    finished = asyncio.Event()

    async def app(scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x" * 49152, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        finished.set()

    async with InProcessH2Harness(app) as harness:
        await harness.send_request(stream_id=1, headers=_get_request(b"/big"))
        await asyncio.wait_for(finished.wait(), timeout=2.0)
        await harness.drain_server()
        assert len(harness.responses[1].body) == 49152
        assert harness.responses[1].ended is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_h2_zero_window_does_not_hold_producer() -> None:
    """With the peer window at 0, every send returns and the server buffers without bound."""
    returned: list[int] = []

    async def app(scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for index in range(6):
            await send({"type": "http.response.body", "body": b"x" * 40000, "more_body": True})
            returned.append(index)

    async with InProcessH2Harness(app) as harness:
        # The harness never forwards client WINDOW_UPDATE frames after the request.
        await harness.send_request(stream_id=1, headers=_get_request(b"/stream"))
        await harness.drain_server()
        proto = harness.server_protocol
        assert proto.connection.local_flow_control_window(1) == 0
        assert len(harness.responses[1].body) == 65535
        # All 240000 bytes were accepted although only 65535 could be sent.
        assert returned == [0, 1, 2, 3, 4, 5]
        assert len(proto.stream_buffers[1].buffer) == 240000 - 65535


@pytest.mark.unit
def test_quic_initial_datagram_anti_amplification_and_parsing() -> None:
    """Verify QUIC Initial packet 1200-byte anti-amplification threshold (RFC 9000)."""
    # 1. Under-sized datagram (< 1200 bytes) must be rejected
    is_valid, _, _, _ = parse_quic_initial_header(b"\x00" * 1199)
    assert is_valid is False

    # 2. Genuine client initial datagram generated via aioquic QuicConnection
    client = QuicConnection(configuration=QuicConfiguration(is_client=True))
    client.connect(("127.0.0.1", 4433), now=0.0)
    dgrams = client.datagrams_to_send(now=0.0)
    assert len(dgrams) >= 1

    initial_datagram, _ = dgrams[0]
    assert len(initial_datagram) >= 1200

    is_valid, dest_cid, src_cid, version = parse_quic_initial_header(initial_datagram)
    assert is_valid is True
    assert len(dest_cid) > 0
    assert len(src_cid) > 0
    assert version == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_protocol_wrapper_dynamic_upgrade_on_direct_preface() -> None:
    """Verify ProtocolWrapper transitions dynamically from H11 to H2 on client preface."""

    async def dummy_app(scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
        pass

    config = Config()
    context = WorkerContext(None)
    loop = asyncio.get_running_loop()

    async def null_send(_: ASGISendEvent | None) -> None:
        await asyncio.sleep(0)

    async with TaskGroup(loop) as tg:
        wrapper = ProtocolWrapper(
            app=ASGIWrapper(dummy_app),
            config=config,
            context=context,
            task_group=tg,
            state=ConnectionState({}),
            ssl=False,
            client=("127.0.0.1", 50000),
            server=("127.0.0.1", 8000),
            send=null_send,  # type: ignore[arg-type]
            alpn_protocol=None,
        )

        assert isinstance(wrapper.protocol, H11Protocol)
        await wrapper.initiate()

        # Feed HTTP/2 direct preface
        preface = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
        await wrapper.handle(RawData(preface))

        assert isinstance(wrapper.protocol, H2Protocol)
        wrapper.protocol.closed = True
        await wrapper.handle(Closed())
