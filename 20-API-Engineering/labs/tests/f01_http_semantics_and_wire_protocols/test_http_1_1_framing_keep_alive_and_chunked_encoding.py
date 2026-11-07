"""Unit tests for HTTP/1.1 chunked transfer coding and h11 wire state machine framing."""

from __future__ import annotations

import h11
import pytest

from api_labs.f01_http_semantics_and_wire_protocols.chunked import (
    decode_chunked,
    encode_chunked,
)


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
def test_h11_server_reassembles_chunked_request_body() -> None:
    payload = b'{"event":"deploy","seq":42,"notes":"chunked wire test"}'
    chunked_body = encode_chunked(payload, chunk_size=11)
    raw_request = (
        b"POST /v1/events HTTP/1.1\r\n"
        b"Host: api.example.test\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Content-Type: application/json\r\n"
        b"\r\n" + chunked_body
    )

    conn = h11.Connection(h11.SERVER)
    conn.receive_data(raw_request)

    events: list[h11.Event] = []
    while True:
        event = conn.next_event()
        if event is h11.NEED_DATA:
            break
        assert isinstance(event, h11.Event)
        events.append(event)
        if isinstance(event, h11.EndOfMessage):
            break

    assert isinstance(events[0], h11.Request)
    assert events[0].method == b"POST"
    assert events[0].target == b"/v1/events"
    reassembled = b"".join(e.data for e in events if isinstance(e, h11.Data))
    assert reassembled == payload
    assert decode_chunked(chunked_body) == payload
    assert isinstance(events[-1], h11.EndOfMessage)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("bad_wire", "match"),
    [
        (b"4", "unterminated chunk size line"),
        (b"\r\n", "invalid chunk size hex digits"),
        (b"+4\r\nabcd\r\n0\r\n\r\n", "invalid chunk size hex digits"),
        (b"zz\r\nabcd\r\n0\r\n\r\n", "invalid chunk size hex digits"),
        (b"0\r\n", "missing or trailing bytes after terminating chunk"),
        (b"0\r\n\r\njunk", "missing or trailing bytes after terminating chunk"),
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
