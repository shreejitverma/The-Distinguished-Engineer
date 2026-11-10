"""Streaming Repr-Digest trailers, trailer loss at intermediaries, and gRPC Trailers-Only."""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions
import h11
import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import FileResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from api_labs.f02_tls_connections_and_intermediaries.trailer_digest import (
    ContentDigestMiddleware,
    DigestOutcome,
    GrpcCode,
    IntegrityError,
    StreamingDigest,
    accept_request,
    buffering_proxy,
    fetch_and_verify,
    frame_grpc_message,
    parse_digest_field,
    parse_grpc_messages,
    reply_proxy_error,
    reply_trailers_only,
    reply_with_trailers,
    run_unary_call,
    serialize_digest_field,
    serve_streaming_response,
    trailers_allowed,
)

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

HELLO = b'{"hello": "world"}\n'
HELLO_SHA256 = "RK/0qy18MlBSVnWgjwz6lZEWjP/lF5HF9bvEF8FabDg="
FIGURE_30_CHUNKS = (b'{"hello"', b': "world', b'"}\n')
GET_1_1 = b"GET /items/123 HTTP/1.1\r\nHost: foo.example\r\nTE: trailers\r\nConnection: TE\r\n\r\n"


def _origin_wire(chunks: tuple[bytes, ...] = FIGURE_30_CHUNKS) -> bytes:
    conn = h11.Connection(h11.SERVER)
    accept_request(conn, GET_1_1)
    return serve_streaming_response(conn, chunks)


def _raw_trailer_response(trailers: list[tuple[bytes, bytes]], body: bytes = HELLO) -> bytes:
    conn = h11.Connection(h11.SERVER)
    accept_request(conn, GET_1_1)
    names = b", ".join(name for name, _ in trailers)
    return b"".join(
        [
            conn.send(h11.Response(status_code=200, headers=[(b"trailer", names)])) or b"",
            conn.send(h11.Data(data=body)) or b"",
            conn.send(h11.EndOfMessage(headers=trailers)) or b"",
        ]
    )


@pytest.mark.unit
def test_streaming_digest_reproduces_rfc_9530_sample_values() -> None:
    whole = StreamingDigest("sha-256")
    whole.update(b'{"hello": "world"}')
    appendix_d = "X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE="
    assert base64.b64encode(whole.digest()).decode() == appendix_d

    streamed = StreamingDigest("sha-256")
    for chunk in FIGURE_30_CHUNKS:
        streamed.update(chunk)
    assert streamed.octets == 19
    assert streamed.field_value() == f"sha-256=:{HELLO_SHA256}:".encode()
    with pytest.raises(ValueError, match="unsupported digest algorithm"):
        StreamingDigest("md5")


@pytest.mark.unit
def test_digest_field_dictionary_rules() -> None:
    sha256 = hashlib.sha256(HELLO).digest()
    sha512 = hashlib.sha512(HELLO).digest()
    value = serialize_digest_field({"sha-256": sha256, "sha-512": sha512})
    assert value.startswith(f"sha-256=:{HELLO_SHA256}:, sha-512=:".encode())
    assert parse_digest_field(value) == {"sha-256": sha256, "sha-512": sha512}
    # RFC 9651 Section 4.2.2: a duplicate key keeps the last value.
    dup = f"sha-256=:{base64.b64encode(b'x' * 32).decode()}:,\tsha-256=:{HELLO_SHA256}:"
    assert parse_digest_field(dup.encode()) == {"sha-256": sha256}
    # Missing "=" padding is synthesized (RFC 9651 Section 4.2.7).
    assert parse_digest_field(f"sha-256=:{HELLO_SHA256.rstrip('=')}:".encode()) == {
        "sha-256": sha256
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "match"),
    [
        (f"sha-256=:{HELLO_SHA256}=:".encode(), "invalid base64"),
        (b"sha-256=:RK/0qy18-MlBS:", "non-base64 character"),
        (f"sha-256=:{HELLO_SHA256}:,".encode(), "trailing comma"),
        (f"sha-256=:{HELLO_SHA256}: sha-512".encode(), "expected ','"),
        (b"sha-256=10", "not a byte sequence"),
        (b"SHA-256=:AAAA:", "invalid dictionary key"),
        (b"sha-256=:AAAA", "unterminated byte sequence"),
        ("sha-256=:\u00e9:".encode(), "not ASCII"),
    ],
)
def test_digest_field_parser_rejects_malformed_values(value: bytes, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        parse_digest_field(value)


@pytest.mark.unit
def test_origin_streams_repr_digest_trailer_like_rfc_9530_figure_30() -> None:
    wire = _origin_wire()
    head, _, body = wire.partition(b"\r\n\r\n")
    assert b"\r\ntrailer: Repr-Digest\r\n" in head
    assert b"\r\nTransfer-Encoding: chunked" in head
    assert b"Content-Length" not in head
    assert body == (
        b'8\r\n{"hello"\r\n8\r\n: "world\r\n3\r\n"}\n\r\n0\r\n'
        b"repr-digest: sha-256=:" + HELLO_SHA256.encode() + b":\r\n\r\n"
    )
    result = fetch_and_verify(wire)
    assert result.outcome is DigestOutcome.VERIFIED
    assert result.digest_source == "trailer"
    assert result.announced_trailers == (b"repr-digest",)
    assert result.verified_body() == HELLO


@pytest.mark.unit
def test_corrupted_byte_in_transit_is_detected_and_content_withheld() -> None:
    wire = _origin_wire().replace(b': "world', b': "wOrld')
    result = fetch_and_verify(wire)
    assert result.body == b'{"hello": "wOrld"}\n'
    assert result.outcome is DigestOutcome.MISMATCH
    with pytest.raises(IntegrityError, match="digest mismatch"):
        result.verified_body()


@pytest.mark.unit
def test_buffering_proxy_silently_drops_the_trailer_section() -> None:
    proxied = buffering_proxy(_origin_wire(), merge_trailers=False)
    head = proxied.partition(b"\r\n\r\n")[0].lower()
    assert b"content-length: 19" in head
    assert b"transfer-encoding" not in head
    result = fetch_and_verify(proxied)
    assert result.body == HELLO
    assert result.trailers == ()
    assert result.announced_trailers == (b"repr-digest",)
    assert result.outcome is DigestOutcome.ABSENT


@pytest.mark.unit
def test_proxy_merges_only_fields_whose_definition_permits_it() -> None:
    wire = _raw_trailer_response(
        [(b"repr-digest", f"sha-256=:{HELLO_SHA256}:".encode()), (b"x-origin-cost", b"12")]
    )
    proxied = buffering_proxy(wire, merge_trailers=True)
    head = proxied.partition(b"\r\n\r\n")[0].lower()
    assert b"repr-digest: sha-256=" in head
    assert b"x-origin-cost: 12" not in proxied
    result = fetch_and_verify(proxied)
    assert result.outcome is DigestOutcome.VERIFIED
    assert result.digest_source == "header"


@pytest.mark.unit
def test_verifier_prefers_the_strongest_supported_algorithm() -> None:
    good256 = f"sha-256=:{HELLO_SHA256}:"
    good512 = serialize_digest_field({"sha-512": hashlib.sha512(HELLO).digest()}).decode()
    bad256 = serialize_digest_field({"sha-256": b"\x00" * 32}).decode()
    bad512 = serialize_digest_field({"sha-512": b"\x00" * 64}).decode()
    cases = {
        f"{bad256}, {good512}": DigestOutcome.VERIFIED,
        f"{good256}, {bad512}": DigestOutcome.MISMATCH,
        "md5=:Sd/dVLAcvNLSq16eXua5uQ==:": DigestOutcome.UNSUPPORTED,
    }
    for value, expected in cases.items():
        wire = _raw_trailer_response([(b"repr-digest", value.encode())])
        assert fetch_and_verify(wire).outcome is expected, value


@pytest.mark.unit
def test_repeated_trailer_field_lines_combine_and_the_last_duplicate_key_wins() -> None:
    # RFC 9651 Section 4.2: combine every line of the field in one section before parsing;
    # RFC 9651 Section 4.2.2: a later duplicate key overwrites an earlier one.
    stale = serialize_digest_field({"sha-256": b"\x00" * 32})
    good = f"sha-256=:{HELLO_SHA256}:".encode()
    wire = _raw_trailer_response([(b"repr-digest", stale), (b"repr-digest", good)])
    assert fetch_and_verify(wire).outcome is DigestOutcome.VERIFIED
    wire = _raw_trailer_response([(b"repr-digest", good), (b"repr-digest", stale)])
    assert fetch_and_verify(wire).outcome is DigestOutcome.MISMATCH


@pytest.mark.unit
def test_rfc_9530_figure_30_double_padded_value_fails_strict_parsing() -> None:
    figure_30_value = f"sha-256=:{HELLO_SHA256}=:".encode()
    wire = _raw_trailer_response([(b"repr-digest", figure_30_value)])
    result = fetch_and_verify(wire)
    assert result.digest_source == "trailer"
    assert result.outcome is DigestOutcome.ABSENT


@pytest.mark.unit
def test_h11_cannot_send_trailers_with_content_length_or_to_http_1_0() -> None:
    conn = h11.Connection(h11.SERVER)
    accept_request(conn, GET_1_1)
    conn.send(h11.Response(status_code=200, headers=[(b"content-length", b"19")]))
    conn.send(h11.Data(data=HELLO))
    with pytest.raises(h11.LocalProtocolError, match="Content-Length and trailers don't mix"):
        conn.send(h11.EndOfMessage(headers=[(b"repr-digest", b"sha-256=:AAAA:")]))

    old = h11.Connection(h11.SERVER)
    accept_request(old, b"GET /items/123 HTTP/1.0\r\n\r\n")
    with pytest.raises(h11.LocalProtocolError, match=r"can't send trailers to HTTP/1\.0 client"):
        serve_streaming_response(old, FIGURE_30_CHUNKS)


@pytest.mark.unit
def test_grpc_normal_response_carries_status_in_trailers() -> None:
    outcome = run_unary_call(reply_with_trailers)
    assert outcome.code is GrpcCode.OK
    assert outcome.trailers_only is False
    assert outcome.synthesized is False
    assert outcome.messages == (b"ok",)


@pytest.mark.unit
def test_grpc_trailers_only_error_is_a_single_end_stream_headers_block() -> None:
    def reply(server: h2.connection.H2Connection, stream_id: int) -> None:
        reply_trailers_only(server, stream_id, GrpcCode.NOT_FOUND, "item 42: caf\u00e9 100%")

    outcome = run_unary_call(reply)
    assert outcome.code is GrpcCode.NOT_FOUND
    assert outcome.message == "item 42: caf\u00e9 100%"
    assert outcome.trailers_only is True
    assert outcome.messages == ()


def _trailers_only_events(code: GrpcCode, message: str) -> list[h2.events.Event]:
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    client.send_headers(
        1,
        [(":method", "POST"), (":scheme", "http"), (":path", "/a.B/C"), (":authority", "t")],
        end_stream=True,
    )
    server.receive_data(client.data_to_send())
    reply_trailers_only(server, 1, code, message)
    return client.receive_data(server.data_to_send())


@pytest.mark.unit
def test_grpc_trailers_only_wire_has_no_data_frame() -> None:
    events = _trailers_only_events(GrpcCode.UNAVAILABLE, "draining")
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["ResponseReceived", "StreamEnded"]
    first = events[0]
    assert isinstance(first, h2.events.ResponseReceived)
    assert first.stream_ended is not None
    assert (b"grpc-status", b"14") in (first.headers or [])


@pytest.mark.unit
def test_grpc_message_is_utf8_then_percent_encoded_on_the_wire() -> None:
    first = _trailers_only_events(GrpcCode.NOT_FOUND, "item 42: caf\u00e9 100%")[0]
    assert isinstance(first, h2.events.ResponseReceived)
    assert (b"grpc-message", b"item 42: caf%C3%A9 100%25") in (first.headers or [])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("http_status", "expected"),
    [
        (503, GrpcCode.UNAVAILABLE),
        (502, GrpcCode.UNAVAILABLE),
        (504, GrpcCode.UNAVAILABLE),
        (429, GrpcCode.UNAVAILABLE),
        (404, GrpcCode.UNIMPLEMENTED),
        (401, GrpcCode.UNAUTHENTICATED),
        (403, GrpcCode.PERMISSION_DENIED),
        (400, GrpcCode.INTERNAL),
        (200, GrpcCode.UNKNOWN),
        (500, GrpcCode.UNKNOWN),
    ],
)
def test_grpc_status_is_synthesized_when_an_intermediary_answers(
    http_status: int, expected: GrpcCode
) -> None:
    def reply(server: h2.connection.H2Connection, stream_id: int) -> None:
        reply_proxy_error(server, stream_id, http_status)

    outcome = run_unary_call(reply)
    assert outcome.code is expected
    assert outcome.synthesized is True


@pytest.mark.unit
def test_h2_refuses_trailers_without_end_stream_and_pseudo_headers_in_trailers() -> None:
    def no_end_stream(server: h2.connection.H2Connection, stream_id: int) -> None:
        server.send_headers(stream_id, [(":status", "200"), ("content-type", "application/grpc")])
        server.send_headers(stream_id, [("grpc-status", "0")], end_stream=False)

    with pytest.raises(h2.exceptions.ProtocolError, match="Trailers must have END_STREAM set"):
        run_unary_call(no_end_stream)

    def pseudo_in_trailers(server: h2.connection.H2Connection, stream_id: int) -> None:
        server.config.validate_outbound_headers = False
        server.send_headers(stream_id, [(":status", "200"), ("content-type", "application/grpc")])
        server.send_headers(stream_id, [(":status", "500"), ("grpc-status", "0")], end_stream=True)

    with pytest.raises(h2.exceptions.ProtocolError, match="pseudo-header in trailer"):
        run_unary_call(pseudo_in_trailers)


async def _streaming_app(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    for chunk in FIGURE_30_CHUNKS * 40:
        await send({"type": "http.response.body", "body": chunk, "more_body": True})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


def _content_digest(body: bytes) -> str:
    return serialize_digest_field({"sha-256": hashlib.sha256(body).digest()}).decode()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpx_asgi_transport_has_no_trailers_extension_so_digest_is_buffered() -> None:
    app = ContentDigestMiddleware(_streaming_app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/", headers={"TE": "trailers"})
    assert response.content == HELLO * 40
    assert response.headers["content-length"] == str(19 * 40)
    assert response.headers["content-digest"] == _content_digest(HELLO * 40)
    assert "trailer" not in response.headers


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [204, 304])
async def test_buffered_digest_adds_no_content_length_to_204_or_304(status: int) -> None:
    async def endpoint(request: Request) -> Response:
        return Response(status_code=status)

    app = ContentDigestMiddleware(Starlette(routes=[Route("/", endpoint, methods=["DELETE"])]))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete("/")
    assert response.status_code == status
    assert "content-length" not in response.headers
    assert response.headers["content-digest"] == _content_digest(b"")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_buffered_digest_keeps_the_get_content_length_on_head(tmp_path: Path) -> None:
    path = tmp_path / "hello.json"
    path.write_bytes(HELLO * 40)

    async def endpoint(request: Request) -> FileResponse:
        return FileResponse(path)

    app = ContentDigestMiddleware(Starlette(routes=[Route("/", endpoint)]))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.head("/")
    assert response.content == b""
    assert response.headers["content-length"] == str(19 * 40)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpx_asgi_transport_silently_discards_trailers_messages() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        start: Message = {"type": "http.response.start", "status": 200, "trailers": True}
        await send({**start, "headers": [(b"trailer", b"Content-Digest")]})
        await send({"type": "http.response.body", "body": HELLO})
        trailer = [(b"content-digest", _content_digest(HELLO).encode())]
        await send({"type": "http.response.trailers", "headers": trailer})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")
    assert response.content == HELLO
    assert "content-digest" not in response.headers
    assert "http.response.trailers" not in response.extensions


@pytest.mark.unit
def test_starlette_testclient_receives_a_streamed_content_digest_trailer() -> None:
    client = TestClient(ContentDigestMiddleware(_streaming_app))
    streamed = client.get("/", headers={"TE": "trailers"})
    fallback = client.get("/")
    assert streamed.headers["trailer"] == "Content-Digest"
    assert "content-length" not in streamed.headers
    trailers = streamed.extensions["http.response.trailers"]
    assert trailers == [(b"content-digest", _content_digest(HELLO * 40).encode())]
    assert fallback.headers["content-digest"] == _content_digest(HELLO * 40)
    assert "http.response.trailers" not in fallback.extensions


def _http2_scope(te: bytes = b"trailers") -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "2",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "scheme": "https",
        "server": ("test", 443),
        "client": ("127.0.0.1", 50000),
        "headers": [(b"te", te), (b"accept-encoding", b"gzip")],
        "extensions": {"http.response.trailers": {}},
    }


async def _send_log(app: ASGIApp) -> list[Message]:
    """Run *app* against a trailers-capable scope and return every message it sent."""
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(_http2_scope(), receive, send)
    return sent


async def _call_asgi(app: ASGIApp) -> tuple[dict[bytes, bytes], bytes, dict[bytes, bytes]]:
    """Drive *app* like a trailers-capable HTTP/2 server and return raw wire-level parts."""
    sent = await _send_log(app)
    headers = {bytes(k): bytes(v) for k, v in sent[0]["headers"]}
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    trailers = {
        bytes(k): bytes(v)
        for m in sent
        if m["type"] == "http.response.trailers"
        for k, v in m["headers"]
    }
    return headers, body, trailers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_digest_inside_gzip_middleware_goes_stale_and_outside_matches() -> None:
    inside = GZipMiddleware(ContentDigestMiddleware(_streaming_app))
    headers, body, trailers = await _call_asgi(inside)
    assert headers[b"content-encoding"] == b"gzip"
    assert trailers[b"content-digest"].decode() == _content_digest(HELLO * 40)
    assert trailers[b"content-digest"].decode() != _content_digest(body)

    outside = ContentDigestMiddleware(GZipMiddleware(_streaming_app))
    headers, body, trailers = await _call_asgi(outside)
    assert headers[b"content-encoding"] == b"gzip"
    assert trailers[b"content-digest"].decode() == _content_digest(body)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_appends_to_app_trailers_and_passes_lifespan_through() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await send({"type": "lifespan.startup.complete"})
            return
        await send({"type": "http.response.start", "status": 200, "trailers": True})
        await send({"type": "http.response.body", "body": HELLO})
        await send(
            {"type": "http.response.trailers", "headers": [(b"x-a", b"1")], "more_trailers": True}
        )
        await send({"type": "http.response.trailers", "headers": [(b"x-b", b"2")]})

    middleware = ContentDigestMiddleware(app)
    headers, body, trailers = await _call_asgi(middleware)
    assert headers[b"trailer"] == b"Content-Digest"
    assert body == HELLO
    assert trailers == {
        b"x-a": b"1",
        b"x-b": b"2",
        b"content-digest": _content_digest(HELLO).encode(),
    }

    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware({"type": "lifespan"}, receive, send)
    assert sent == [{"type": "lifespan.startup.complete"}]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        (h2.errors.ErrorCodes.REFUSED_STREAM, GrpcCode.UNAVAILABLE),
        (h2.errors.ErrorCodes.CANCEL, GrpcCode.CANCELLED),
        (h2.errors.ErrorCodes.ENHANCE_YOUR_CALM, GrpcCode.RESOURCE_EXHAUSTED),
        (h2.errors.ErrorCodes.INADEQUATE_SECURITY, GrpcCode.PERMISSION_DENIED),
        (h2.errors.ErrorCodes.NO_ERROR, GrpcCode.INTERNAL),
    ],
)
def test_grpc_rst_stream_maps_to_status(error_code: int, expected: GrpcCode) -> None:
    def reply(server: h2.connection.H2Connection, stream_id: int) -> None:
        server.reset_stream(stream_id, error_code=error_code)

    outcome = run_unary_call(reply)
    assert outcome.code is expected
    assert outcome.synthesized is True


@pytest.mark.unit
def test_length_prefixed_message_framing_and_truncation() -> None:
    framed = frame_grpc_message(b"abc") + frame_grpc_message(b"")
    assert framed == b"\x00\x00\x00\x00\x03abc\x00\x00\x00\x00\x00"
    assert parse_grpc_messages(framed) == (b"abc", b"")
    with pytest.raises(ValueError, match="truncated gRPC message prefix"):
        parse_grpc_messages(b"\x00\x00\x00")
    with pytest.raises(ValueError, match="truncated gRPC message body"):
        parse_grpc_messages(b"\x00\x00\x00\x00\x05ab")


@pytest.mark.unit
def test_incomplete_messages_are_rejected_and_empty_field_is_empty_dictionary() -> None:
    assert parse_digest_field(b"") == {}
    with pytest.raises(ValueError, match="expected a complete request"):
        accept_request(h11.Connection(h11.SERVER), b"GET / HTTP/1.1\r\n")
    truncated = _origin_wire()[:-7]
    with pytest.raises(ValueError, match="incomplete response"):
        fetch_and_verify(truncated)


@pytest.mark.unit
def test_trailer_value_takes_precedence_over_a_stale_header_value() -> None:
    stale = serialize_digest_field({"sha-256": b"\x00" * 32})
    wire = _origin_wire().replace(
        b"Transfer-Encoding: chunked\r\n",
        b"Transfer-Encoding: chunked\r\nRepr-Digest: " + stale + b"\r\n",
    )
    result = fetch_and_verify(wire)
    assert result.digest_source == "trailer"
    assert result.outcome is DigestOutcome.VERIFIED


@pytest.mark.unit
@pytest.mark.parametrize("raw_status", ["99", "OK", "-1"])
def test_unparseable_grpc_status_is_treated_as_absent(raw_status: str) -> None:
    def reply(server: h2.connection.H2Connection, stream_id: int) -> None:
        server.send_headers(stream_id, [(":status", "200"), ("content-type", "application/grpc")])
        server.send_headers(stream_id, [("grpc-status", raw_status)], end_stream=True)

    outcome = run_unary_call(reply)
    assert outcome.code is GrpcCode.UNKNOWN
    assert outcome.synthesized is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("te", "expected"),
    [
        (b"trailers", True),
        (b"Trailers", True),
        (b"gzip, TRAILERS", True),
        (b" trailers ", True),
        (b"gzip", False),
        (b"trailers-x", False),
    ],
)
def test_trailers_are_offered_only_when_te_lists_trailers_case_insensitively(
    te: bytes, expected: bool
) -> None:
    assert trailers_allowed(_http2_scope(te)) is expected
    scope = _http2_scope(b"trailers")
    scope["extensions"] = {}
    assert trailers_allowed(scope) is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_digest_rides_only_the_final_trailers_message_and_exactly_once() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "trailers": True})
        await send({"type": "http.response.body", "body": HELLO})
        first: Message = {"type": "http.response.trailers", "headers": [(b"x-a", b"1")]}
        await send({**first, "more_trailers": True})
        await send({"type": "http.response.trailers", "headers": [(b"x-b", b"2")]})

    sent = await _send_log(ContentDigestMiddleware(app))
    trailer_msgs = [m for m in sent if m["type"] == "http.response.trailers"]
    assert trailer_msgs[0]["headers"] == [(b"x-a", b"1")]
    assert trailer_msgs[1]["headers"] == [
        (b"x-b", b"2"),
        (b"content-digest", _content_digest(HELLO).encode()),
    ]
    assert len(trailer_msgs) == 2

    plain = await _send_log(ContentDigestMiddleware(_streaming_app))
    plain_trailers = [m for m in plain if m["type"] == "http.response.trailers"]
    assert len(plain_trailers) == 1
    assert plain_trailers[0]["headers"] == [
        (b"content-digest", _content_digest(HELLO * 40).encode())
    ]
    assert plain[-1] is plain_trailers[0]
