"""Tests for uvloop loop replacement and httptools C callback framing."""

from __future__ import annotations

import re
from pathlib import Path

import httptools
import pytest
import uvloop

from api_labs.f12_asgi_servers_and_async_runtime.c_http_framing import (
    H11ParserCollector,
    HttpToolsParserCollector,
    UvloopDiagnostics,
    benchmark_parser_throughput,
)


@pytest.mark.unit
def test_httptools_chunk_boundary_header_stitching() -> None:
    """Demonstrates llhttp callback accumulation across 1-byte chunk boundaries.

    Verifies that cb_on_header_field and cb_on_header_value correctly stitch
    split fragments into complete header tuples without truncation.
    """
    collector = HttpToolsParserCollector()
    raw = (
        b"GET /api/v1/stream HTTP/1.1\r\n"
        b"Host: internal.service\r\n"
        b"X-Custom-Split-Token: SecretPayloadValue1234\r\n"
        b"\r\n"
    )

    # Feed 1 byte at a time to force fragmentation across every token boundary
    collector.feed_chunks([bytes([b]) for b in raw])

    assert len(collector.messages) == 1
    msg = collector.messages[0]
    assert msg.completed is True
    assert msg.method == "GET"
    assert msg.url == b"/api/v1/stream"
    assert msg.http_version == "1.1"

    headers_dict = dict(msg.headers)
    assert headers_dict[b"host"] == b"internal.service"
    assert headers_dict[b"x-custom-split-token"] == b"SecretPayloadValue1234"


@pytest.mark.unit
def test_httptools_chunked_transfer_encoding_framing() -> None:
    """Verifies RFC 9112 Section 7.1 chunked transfer coding framing in llhttp.

    Asserts that chunk sizes, chunk data, chunk completion, and terminal 0-chunk
    emit corresponding C callbacks and reassemble a pure body stream.
    """
    collector = HttpToolsParserCollector()
    raw_chunked = (
        b"POST /upload HTTP/1.1\r\n"
        b"Host: api.example.com\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        b"5\r\n"
        b"hello\r\n"
        b"6\r\n"
        b" world\r\n"
        b"0\r\n"
        b"\r\n"
    )

    collector.feed(raw_chunked)

    assert len(collector.messages) == 1
    msg = collector.messages[0]
    assert msg.completed is True
    assert msg.method == "POST"
    assert msg.body == b"hello world"
    assert msg.chunk_count == 3  # Two data chunks + one terminal zero chunk
    assert "on_chunk_header" in collector.callback_log
    assert "on_chunk_complete" in collector.callback_log


@pytest.mark.unit
def test_httptools_pipelined_requests_batch_processing() -> None:
    """Verifies multiple HTTP/1.1 pipelined requests in a single buffer."""
    collector = HttpToolsParserCollector()
    pipelined_buffer = (
        b"GET /req-one HTTP/1.1\r\n"
        b"Host: example.com\r\n"
        b"\r\n"
        b"POST /req-two HTTP/1.1\r\n"
        b"Host: example.com\r\n"
        b"Content-Length: 4\r\n"
        b"\r\n"
        b"done"
    )

    collector.feed(pipelined_buffer)

    assert len(collector.messages) == 2
    assert collector.messages[0].method == "GET"
    assert collector.messages[0].url == b"/req-one"
    assert collector.messages[0].completed is True

    assert collector.messages[1].method == "POST"
    assert collector.messages[1].url == b"/req-two"
    assert collector.messages[1].body == b"done"
    assert collector.messages[1].completed is True


@pytest.mark.unit
def test_httptools_malformed_header_syntax_rejection() -> None:
    """Verifies llhttp strict rejection of illegal header tokens (RFC 9112 Section 5.1)."""
    collector = HttpToolsParserCollector()
    malformed_req = b"GET / HTTP/1.1\r\nInvalid Header Name: value\r\n\r\n"

    with pytest.raises(httptools.HttpParserError) as exc_info:
        collector.feed(malformed_req)

    assert "Invalid header token" in str(exc_info.value)


@pytest.mark.unit
def test_h11_equivalence_and_event_stream() -> None:
    """Asserts semantic parity between httptools and h11 parser collectors."""
    req_bytes = (
        b"POST /v1/records HTTP/1.1\r\n"
        b"Host: datastore.local\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 18\r\n"
        b"\r\n"
        b'{"metric":"score"}'
    )

    httptools_collector = HttpToolsParserCollector()
    httptools_collector.feed(req_bytes)

    h11_collector = H11ParserCollector()
    h11_collector.feed(req_bytes)

    assert len(httptools_collector.messages) == 1
    assert len(h11_collector.messages) == 1

    ht_msg = httptools_collector.messages[0]
    h11_msg = h11_collector.messages[0]

    assert ht_msg.method == h11_msg.method == "POST"
    assert ht_msg.url == h11_msg.url == b"/v1/records"
    assert ht_msg.body == h11_msg.body == b'{"metric":"score"}'
    assert dict(ht_msg.headers)[b"content-type"] == dict(h11_msg.headers)[b"content-type"]
    assert "Request" in h11_collector.event_log
    assert "Data" in h11_collector.event_log
    assert "EndOfMessage" in h11_collector.event_log


@pytest.mark.unit
def test_parser_throughput_speedup_differential() -> None:
    """Benchmarks parser throughput proving C-extension acceleration over pure Python."""
    payload = (
        b"GET /health HTTP/1.1\r\nHost: status.internal\r\nUser-Agent: Prometheus/2.45\r\n\r\n"
    )
    result = benchmark_parser_throughput(payload, iterations=1000)
    assert result["httptools_rate"] > 0
    assert result["h11_rate"] > 0
    # C-based llhttp should consistently outperform pure-Python h11 by >= 3x
    assert result["speedup_ratio"] >= 3.0


@pytest.mark.unit
def test_uvloop_libuv_runtime_diagnostics() -> None:
    """Inspects libuv embedded C version in uvloop 0.23.0."""
    info = UvloopDiagnostics.get_libuv_version_info()
    assert info["available"] is True
    assert info["version_string"] == "1.52.1"
    assert info["version_hex"] == "0x13401"
    assert info["major"] == 1
    assert info["minor"] == 52
    assert info["patch"] == 1

    loop = UvloopDiagnostics.create_loop()
    try:
        assert type(loop).__name__ == "Loop"
        assert "uvloop" in type(loop).__module__
    finally:
        loop.close()


@pytest.mark.unit
def test_uvloop_fixed_receive_buffer_constant() -> None:
    """Verifies the fixed 250 KB receive buffer against the installed uvloop source."""
    consts = Path(str(uvloop.__file__)).parent / "includes" / "consts.pxi"
    match = re.search(r"^\s*UV_STREAM_RECV_BUF_SIZE\s*=\s*(\d+)", consts.read_text(), re.M)
    assert match is not None
    assert int(match.group(1)) == 256000
    assert UvloopDiagnostics.UV_STREAM_RECV_BUF_SIZE == int(match.group(1))
