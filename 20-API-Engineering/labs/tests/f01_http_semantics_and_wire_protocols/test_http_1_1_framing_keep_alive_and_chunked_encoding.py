"""Unit tests for HTTP/1.1 chunked transfer coding and h11 wire state machine framing."""

from __future__ import annotations

from typing import TYPE_CHECKING

import h11
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from api_labs.f01_http_semantics_and_wire_protocols.chunked import (
    decode_chunked,
    decode_chunked_with_trailers,
    encode_chunked,
    step_h11_server_exchange,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.requests import Request


@pytest.mark.unit
def test_encode_and_decode_roundtrip() -> None:
    payload = b"Wikipedia in \r\n\r\nchunks."
    wire = encode_chunked(payload, chunk_size=5)
    assert wire == (
        b"5\r\nWikip\r\n5\r\nedia \r\n5\r\nin \r\n\r\n5\r\n\r\nchu\r\n4\r\nnks.\r\n0\r\n\r\n"
    )
    assert decode_chunked(wire) == payload


@pytest.mark.unit
def test_empty_body_framing() -> None:
    wire = encode_chunked(b"")
    assert wire == b"0\r\n\r\n"
    assert decode_chunked(wire) == b""


@pytest.mark.unit
def test_decode_ignores_chunk_extensions() -> None:
    wire = b"4;ext=val\r\nWiki\r\n5;foo=bar\r\npedia\r\n0;last\r\n\r\n"
    assert decode_chunked(wire) == b"Wikipedia"


@pytest.mark.unit
def test_decode_accepts_uppercase_and_lowercase_hex_chunk_sizes() -> None:
    wire = b"A\r\n0123456789\r\na\r\nabcdefghij\r\n00\r\n\r\n"
    assert decode_chunked(wire) == b"0123456789abcdefghij"


@pytest.mark.unit
def test_chunked_trailers_roundtrip_and_forbidden_field_rejection() -> None:
    wire = encode_chunked(
        b"stream-payload",
        chunk_size=6,
        trailers=[(b"Server-Timing", b"db;dur=4.2"), (b"X-Checksum-Sha256", b"deadbeef")],
    )
    decoded = decode_chunked_with_trailers(wire)
    assert decoded.body == b"stream-payload"
    assert decoded.trailers == (
        (b"server-timing", b"db;dur=4.2"),
        (b"x-checksum-sha256", b"deadbeef"),
    )

    for forbidden in (
        b"Content-Length",
        b"Transfer-Encoding",
        b"Host",
        b"Authorization",
        b"Trailer",
        b"TE",
    ):
        with pytest.raises(ValueError, match="forbidden trailer field"):
            encode_chunked(b"x", trailers=[(forbidden, b"1")])
        raw_bad_trailer = b"1\r\nx\r\n0\r\n" + forbidden + b": 1\r\n\r\n"
        with pytest.raises(ValueError, match="forbidden trailer field"):
            decode_chunked_with_trailers(raw_bad_trailer)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        (b"X-A", b"1\r\nContent-Length: 5", "contains CR, LF, or NUL"),
        (b"X-A", b"1\nContent-Length: 5", "contains CR, LF, or NUL"),
        (b"X-A", b"1\rContent-Length: 5", "contains CR, LF, or NUL"),
        (b"X-A", b"1\x00", "contains CR, LF, or NUL"),
        (b"X-A: 1\r\nContent-Length", b"5", "malformed trailer field line"),
        (b" Host", b"a", "malformed trailer field line"),
        (b"X A", b"1", "malformed trailer field line"),
        (b"", b"1", "malformed trailer field line"),
    ],
)
def test_encode_rejects_trailer_field_line_injection(name: bytes, value: bytes, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        encode_chunked(b"x", trailers=[(name, value)])


@pytest.mark.unit
def test_decode_rejects_bare_lf_smuggled_inside_trailer_value() -> None:
    wire = b"1\r\nx\r\n0\r\nX-A: 1\nContent-Length: 5\r\n\r\n"
    with pytest.raises(ValueError, match="contains CR, LF, or NUL"):
        decode_chunked_with_trailers(wire)


@pytest.mark.unit
def test_h11_server_reassembles_chunked_request_body_and_trailers() -> None:
    payload = b'{"event":"deploy","seq":42,"notes":"chunked wire test"}'
    chunked_body = encode_chunked(
        payload,
        chunk_size=11,
        trailers=[(b"X-Stream-Digest", b"sha256=abc123")],
    )
    raw_request = (
        b"POST /v1/events HTTP/1.1\r\n"
        b"Host: api.example.test\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Trailer: X-Stream-Digest\r\n"
        b"Content-Type: application/json\r\n"
        b"\r\n" + chunked_body
    )

    conn = h11.Connection(h11.SERVER)
    summary = step_h11_server_exchange(
        conn,
        raw_request,
        response_status=202,
        response_headers=[(b"content-length", b"2")],
        response_body=b"ok",
    )
    assert summary.method == b"POST"
    assert summary.target == b"/v1/events"
    assert summary.body == payload
    assert summary.trailers == ((b"x-stream-digest", b"sha256=abc123"),)
    assert summary.reusable is True
    assert conn.states == {h11.CLIENT: h11.IDLE, h11.SERVER: h11.IDLE}


@pytest.mark.unit
def test_h11_transfer_encoding_precedence_over_content_length() -> None:
    chunked_body = encode_chunked(b"trusted-chunk", chunk_size=7)
    raw_request = (
        b"POST /v1/ingest HTTP/1.1\r\n"
        b"Host: api.example.test\r\n"
        b"Content-Length: 999\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n" + chunked_body
    )
    conn = h11.Connection(h11.SERVER)
    summary = step_h11_server_exchange(
        conn,
        raw_request,
        response_status=200,
        response_headers=[(b"content-length", b"50"), (b"transfer-encoding", b"chunked")],
        response_body=b"streamed",
    )
    assert summary.body == b"trusted-chunk"
    assert b"Transfer-Encoding: chunked\r\n" in summary.wire_response
    assert b"Content-Length" not in summary.wire_response
    assert b"\r\n8\r\nstreamed\r\n0\r\n\r\n" in summary.wire_response


@pytest.mark.unit
def test_h11_keeps_dual_content_length_and_transfer_encoding_connection_reusable() -> None:
    # RFC 9112 Section 6.1: the server MUST close after responding; h11 0.16.0 does not.
    raw_request = (
        b"POST /v1/ingest HTTP/1.1\r\n"
        b"Host: api.example.test\r\n"
        b"Content-Length: 999\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n" + encode_chunked(b"x")
    )
    conn = h11.Connection(h11.SERVER)
    summary = step_h11_server_exchange(
        conn, raw_request, response_headers=[(b"content-length", b"0")]
    )
    assert summary.body == b"x"
    assert summary.reusable is True
    assert b"connection: close" not in summary.wire_response.lower()
    assert conn.states == {h11.CLIENT: h11.IDLE, h11.SERVER: h11.IDLE}


@pytest.mark.unit
def test_h11_accepts_bare_cr_in_chunk_extension_that_lab_decoder_rejects() -> None:
    chunked_body = b"5;x=\ry\r\nhello\r\n0\r\n\r\n"
    conn = h11.Connection(h11.SERVER)
    summary = step_h11_server_exchange(
        conn,
        b"POST /v1/ext HTTP/1.1\r\nHost: api.example.test\r\nTransfer-Encoding: chunked\r\n\r\n"
        + chunked_body,
    )
    assert summary.body == b"hello"
    with pytest.raises(ValueError, match="bare CR in chunk extension"):
        decode_chunked(chunked_body)


@pytest.mark.unit
def test_h11_persistent_connection_reuse_and_close_transitions() -> None:
    conn = h11.Connection(h11.SERVER)
    req1 = b"GET /v1/health HTTP/1.1\r\nHost: api.example.test\r\n\r\n"
    req2 = b"GET /v1/final HTTP/1.1\r\nHost: api.example.test\r\nConnection: close\r\n\r\n"

    first = step_h11_server_exchange(
        conn,
        req1,
        response_status=204,
    )
    assert first.reusable is True
    assert conn.states == {h11.CLIENT: h11.IDLE, h11.SERVER: h11.IDLE}

    second = step_h11_server_exchange(
        conn,
        req2,
        response_headers=[(b"content-length", b"4")],
        response_body=b"done",
    )
    assert second.reusable is False
    assert b"Connection: close\r\n" in second.wire_response
    assert conn.states == {h11.CLIENT: h11.MUST_CLOSE, h11.SERVER: h11.MUST_CLOSE}
    with pytest.raises(h11.LocalProtocolError, match="not in a reusable state"):
        conn.start_next_cycle()

    with pytest.raises(ValueError, match=r"expected h11\.Request"):
        step_h11_server_exchange(h11.Connection(h11.SERVER), b"GET /incomplete HTTP/1.1\r\n")
    with pytest.raises(ValueError, match="incomplete request body"):
        step_h11_server_exchange(
            h11.Connection(h11.SERVER),
            b"POST /partial HTTP/1.1\r\nHost: api.example.test\r\nContent-Length: 10\r\n\r\nabc",
        )


@pytest.mark.unit
def test_h11_early_response_with_unread_body_prevents_cycle_reuse() -> None:
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(
        b"POST /v1/upload HTTP/1.1\r\n"
        b"Host: api.example.test\r\n"
        b"Content-Length: 1024\r\n"
        b"\r\n"
        b"partial-bytes"
    )
    event = conn.next_event()
    assert isinstance(event, h11.Request)
    assert conn.their_state is h11.SEND_BODY

    conn.send(
        h11.Response(
            status_code=413,
            headers=[(b"content-length", b"0"), (b"connection", b"close")],
        )
    )
    conn.send(h11.EndOfMessage())
    assert conn.states == {h11.CLIENT: h11.SEND_BODY, h11.SERVER: h11.MUST_CLOSE}
    with pytest.raises(h11.LocalProtocolError, match="not in a reusable state"):
        conn.start_next_cycle()


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_footer_chunk",
    [
        b"3\r\nxxx__0\r\n\r\n",
        b"3\r\nxxx\r_0\r\n\r\n",
        b"3\r\nxxx_\n0\r\n\r\n",
    ],
)
def test_h11_rejects_cve_2025_43859_non_crlf_chunk_footers(bad_footer_chunk: bytes) -> None:
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(
        b"POST /v1/smuggle HTTP/1.1\r\n"
        b"Host: api.example.test\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n" + bad_footer_chunk
    )
    assert isinstance(conn.next_event(), h11.Request)
    assert isinstance(conn.next_event(), h11.Data)
    with pytest.raises(h11.RemoteProtocolError, match="malformed chunk footer"):
        conn.next_event()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_streaming_response_vs_fixed_content_length() -> None:
    async def fixed_endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def stream_endpoint(request: Request) -> StreamingResponse:
        async def generate() -> AsyncIterator[bytes]:
            yield b"alpha\n"
            yield b"beta\n"

        return StreamingResponse(generate(), media_type="text/plain")

    app = Starlette(
        routes=[
            Route("/fixed", fixed_endpoint),
            Route("/stream", stream_endpoint),
        ]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        fixed_resp = await client.get("/fixed")
        assert fixed_resp.status_code == 200
        assert fixed_resp.headers["content-length"] == "11"

        stream_resp = await client.get("/stream")
        assert stream_resp.status_code == 200
        assert "content-length" not in stream_resp.headers
        assert stream_resp.content == b"alpha\nbeta\n"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("bad_wire", "match"),
    [
        (b"4", "unterminated chunk size line"),
        (b"\r\n", "invalid chunk size hex digits"),
        (b"+4\r\nabcd\r\n0\r\n\r\n", "invalid chunk size hex digits"),
        (b"zz\r\nabcd\r\n0\r\n\r\n", "invalid chunk size hex digits"),
        (b"1" * 21 + b"\r\nabcd\r\n0\r\n\r\n", "invalid chunk size hex digits"),
        (b"4;ext=val\rbad\r\nabcd\r\n0\r\n\r\n", "bare CR in chunk extension"),
        (b"0\r\n", "missing or trailing bytes after terminating chunk"),
        (b"0\r\n\r\njunk", "missing or trailing bytes after terminating chunk"),
        (b"0\r\nBadHeader\r\n\r\n", "malformed trailer field line"),
        (b"0\r\nBad-Header : val\r\n\r\n", "malformed trailer field line"),
        (b"5\r\nhi\r\n", "truncated chunk data or missing chunk CRLF"),
        (b"2\r\nhiXX0\r\n\r\n", "truncated chunk data or missing chunk CRLF"),
    ],
)
def test_decode_rejects_malformed_chunked_streams(bad_wire: bytes, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        decode_chunked(bad_wire)


@pytest.mark.unit
def test_encode_rejects_non_positive_chunk_size() -> None:
    with pytest.raises(ValueError, match="chunk_size must be positive"):
        encode_chunked(b"data", chunk_size=0)
