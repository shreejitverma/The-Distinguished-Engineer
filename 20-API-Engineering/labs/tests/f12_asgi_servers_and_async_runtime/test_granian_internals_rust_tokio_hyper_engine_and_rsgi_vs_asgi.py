"""Unit tests for Granian 2.8.7 internals, scope construction, and protocol overhead."""

from __future__ import annotations

import re

import granian
import granian._granian as _granian
import granian.constants as granian_const
import httpx
import pytest

from api_labs.f12_asgi_servers_and_async_runtime.granian_protocol_overhead import (
    AllocationTracker,
    ASGIScopeBuilder,
    MockASGIProtocol,
    MockRSGIProtocol,
    RSGIHeadersWrapper,
    RSGIScopeDescriptor,
    create_granian_compatible_asgi_app,
    create_rsgi_app,
)


@pytest.mark.unit
def test_granian_metadata_and_constants() -> None:
    """Verify pinned Granian release version, GIL flag, and runtime configuration enums."""
    assert granian.__version__ == "2.8.7"
    assert isinstance(_granian.BUILD_GIL, bool)

    # Task implementation options
    assert granian_const.TaskImpl.asyncio.value == "asyncio"
    assert granian_const.TaskImpl.rust.value == "rust"

    # Runtime modes: multi-threaded vs single-threaded Tokio workers
    assert granian_const.RuntimeModes.auto.value == "auto"
    assert granian_const.RuntimeModes.mt.value == "mt"
    assert granian_const.RuntimeModes.st.value == "st"

    # HTTP version negotiation modes
    assert granian_const.HTTPModes.auto.value == "auto"
    assert granian_const.HTTPModes.http1.value == "1"
    assert granian_const.HTTPModes.http2.value == "2"

    # Supported event loops
    assert granian_const.Loops.auto.value == "auto"
    assert granian_const.Loops.asyncio.value == "asyncio"
    assert granian_const.Loops.uvloop.value == "uvloop"
    assert granian_const.Loops.rloop.value == "rloop"


@pytest.mark.unit
def test_asgi_scope_construction_fidelity() -> None:
    """Verify ASGI HTTP scope construction matches Granian's build_scope_http macro."""
    scope = ASGIScopeBuilder.build_http_scope(
        method="get",
        path="/api/v1/resource%20test",
        query_string=b"filter=active&limit=10",
        headers=[(b"user-agent", b"pytest/9.2.0")],
        authority="api.example.com",
    )

    assert scope["type"] == "http"
    assert scope["asgi"] == {"version": "3.0", "spec_version": "2.3"}
    assert scope["http_version"] == "1.1"
    assert scope["method"] == "GET"
    assert scope["path"] == "/api/v1/resource test"  # percent-decoded
    assert scope["raw_path"] == b"/api/v1/resource%20test"
    assert scope["query_string"] == b"filter=active&limit=10"
    assert scope["server"] == ("127.0.0.1", "8000")
    assert scope["client"] == ("127.0.0.1", "50000")
    assert scope["scheme"] == "http"

    # Host header automatically prepended from authority if missing
    headers = scope["headers"]
    assert headers[0] == (b"host", b"api.example.com")
    assert headers[1] == (b"user-agent", b"pytest/9.2.0")

    # Extensions matching Granian's PyOnceLock static dictionary
    assert "http.response.pathsend" in scope["extensions"]
    assert "websocket.http.response" in scope["extensions"]

    # Granian shares the PyOnceLock-cached dicts across requests instead of copying them
    other = ASGIScopeBuilder.build_http_scope(method="GET", path="/")
    assert other["asgi"] is scope["asgi"]
    assert other["extensions"] is scope["extensions"]


@pytest.mark.unit
def test_rsgi_scope_and_headers_fidelity() -> None:
    """Verify RSGI 1.6 scope descriptor and zero-copy headers mapping."""
    headers_data = [
        ("Accept", "application/json"),
        ("X-Request-Id", "req-12345"),
        ("X-Custom", "val1"),
        ("X-Custom", "val2"),
    ]
    scope = RSGIScopeDescriptor(
        method="post",
        path="/items",
        query_string="sort=desc",
        headers=headers_data,
        authority="api.internal",
    )

    assert scope.proto == "http"
    assert scope.rsgi_version == "1.6"
    assert scope.http_version == "1.1"
    assert scope.method == "POST"
    assert scope.path == "/items"
    assert scope.query_string == "sort=desc"
    assert scope.server == "127.0.0.1:8000"
    assert scope.client == "127.0.0.1:50000"
    assert scope.authority == "api.internal"

    # Header lookup semantics
    headers = scope.headers
    assert isinstance(headers, RSGIHeadersWrapper)
    assert len(headers) == 3  # accept, x-request-id, x-custom
    assert "accept" in headers
    assert "ACCEPT" in headers  # case-insensitive check
    assert headers["x-request-id"] == "req-12345"
    assert headers.get("non-existent", "default") == "default"

    # Multi-value headers via get_all()
    assert headers.get_all("x-custom") == ["val1", "val2"]
    assert headers.get_all("accept") == ["application/json"]
    assert headers.get_all("unknown") == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_protocol_state_machine_and_validation() -> None:
    """Verify ASGI message validation rules matching Granian adapt_message_type."""
    protocol = MockASGIProtocol(request_body=b"payload")

    # Inbound message delivery
    msg = await protocol.receive()
    assert msg["type"] == "http.request"
    assert msg["body"] == b"payload"
    assert not msg["more_body"]

    # Sending body before start must raise
    with pytest.raises(RuntimeError, match=re.escape("ASGI flow error: Response not started")):
        await protocol.send({"type": "http.response.body", "body": b"early"})

    # Valid start message
    await protocol.send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    assert protocol.status_code == 200

    # Duplicate start message must raise
    with pytest.raises(RuntimeError, match=re.escape("ASGI flow error: Response already started")):
        await protocol.send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            }
        )

    # Valid body completion
    await protocol.send(
        {
            "type": "http.response.body",
            "body": b"hello world",
            "more_body": False,
        }
    )
    assert protocol.response_completed
    assert bytes(protocol.response_body) == b"hello world"

    # Sending message after completion must raise
    with pytest.raises(RuntimeError, match=re.escape("ASGI flow error: Response already finished")):
        await protocol.send(
            {
                "type": "http.response.body",
                "body": b"extra",
                "more_body": False,
            }
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rsgi_protocol_state_machine_and_streaming() -> None:
    """Verify RSGI synchronous response emission and streaming transport."""
    protocol = MockRSGIProtocol(request_body=b'{"key": "val"}')

    # Reading body via direct coroutine __call__
    body = await protocol()
    assert body == b'{"key": "val"}'

    # Direct synchronous response_bytes
    protocol.response_bytes(201, [("content-type", "application/json")], b'{"created": true}')
    assert protocol.status_code == 201
    assert protocol.response_method == "response_bytes"
    assert bytes(protocol.response_body) == b'{"created": true}'
    assert protocol.response_completed

    # Streaming response test
    stream_proto = MockRSGIProtocol()
    transport = stream_proto.response_stream(200, [("content-type", "text/event-stream")])
    assert stream_proto.response_method == "response_stream"

    await transport.send_str("event: ping\n")
    await transport.send_bytes(b"data: pong\n\n")
    assert bytes(stream_proto.response_body) == b"event: ping\ndata: pong\n\n"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_allocation_overhead_asgi_vs_rsgi() -> None:
    """Verify that RSGI incurs fewer dictionary allocations than standard ASGI."""
    asgi_app = create_granian_compatible_asgi_app()
    rsgi_app = create_rsgi_app()

    # Measure ASGI request
    _, asgi_stats = await AllocationTracker.measure_asgi_request(
        asgi_app,
        method="GET",
        path="/ping",
    )

    # Measure RSGI request
    _, rsgi_stats = await AllocationTracker.measure_rsgi_request(
        rsgi_app,
        method="GET",
        path="/ping",
    )

    # ASGI requires distinct dictionary instantiations for scope and message envelopes
    # RSGI uses PyClass descriptor and synchronous method calls
    assert asgi_stats.dicts_allocated >= 3  # scope + response.start + response.body
    assert rsgi_stats.dicts_allocated < asgi_stats.dicts_allocated


@pytest.mark.unit
@pytest.mark.asyncio
async def test_granian_compatible_asgi_app_with_httpx() -> None:
    """Verify ASGI application endpoints using httpx in-process ASGITransport."""
    app = create_granian_compatible_asgi_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        # GET /ping
        res_ping = await client.get("/ping")
        assert res_ping.status_code == 200
        assert res_ping.headers["content-type"] == "application/json"
        assert int(res_ping.headers["content-length"]) == len(res_ping.content)
        assert res_ping.json() == {"ping": "pong"}

        # POST /echo
        res_echo = await client.post("/echo", content="payload-data")
        assert res_echo.status_code == 200
        assert res_echo.json() == {"received": "payload-data"}

        # 404 Not Found
        res_missing = await client.get("/missing")
        assert res_missing.status_code == 404
        assert res_missing.text == "Not Found"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_granian_rsgi_app_in_process() -> None:
    """Verify RSGI application endpoints in-process matching RSGI 1.6 specification."""
    app = create_rsgi_app()

    # GET /ping
    scope_ping = RSGIScopeDescriptor(method="GET", path="/ping")
    proto_ping = MockRSGIProtocol()
    await app(scope_ping, proto_ping)
    assert proto_ping.status_code == 200
    assert bytes(proto_ping.response_body) == b'{"ping": "pong"}'

    # POST /echo
    scope_echo = RSGIScopeDescriptor(method="POST", path="/echo")
    proto_echo = MockRSGIProtocol(request_body=b"token-stream")
    await app(scope_echo, proto_echo)
    assert proto_echo.status_code == 200
    assert bytes(proto_echo.response_body) == b'{"received": "token-stream"}'

    # 404 fallback
    scope_404 = RSGIScopeDescriptor(method="GET", path="/not-found")
    proto_404 = MockRSGIProtocol()
    await app(scope_404, proto_404)
    assert proto_404.status_code == 404
    assert bytes(proto_404.response_body) == b"Not Found"


@pytest.mark.unit
def test_granian_native_signal_and_worker_classes() -> None:
    """Verify native PyO3 class bindings in granian._granian."""
    # WorkerSignal signaling mechanism
    sig = _granian.WorkerSignal()
    callbacks_fired: list[str] = []

    def on_signal() -> None:
        callbacks_fired.append("signal_set")

    sig.add_cb(on_signal)
    sig.set()

    # Verify ASGIWorker and RSGIWorker class export methods
    asgi_methods = dir(_granian.ASGIWorker)
    rsgi_methods = dir(_granian.RSGIWorker)

    assert "serve_mtr" in asgi_methods
    assert "serve_str" in asgi_methods
    assert "serve_mtr_uds" in asgi_methods
    assert "serve_str_uds" in asgi_methods

    assert "serve_mtr" in rsgi_methods
    assert "serve_str" in rsgi_methods
