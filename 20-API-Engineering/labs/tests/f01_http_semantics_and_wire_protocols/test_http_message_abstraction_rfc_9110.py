"""Unit tests: one RFC 9110 message abstraction, three wire serializations (h11, h2, HTTP/3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import h2.errors
import h2.events
import h2.exceptions
import h11
import hpack
import pylsqpack
import pytest
from aioquic.h3.connection import (
    FrameType,
    MessageError,
    encode_frame,
    validate_request_headers,
    validate_trailers,
)
from hyperframe.frame import Frame, GoAwayFrame, HeadersFrame

from api_labs.f01_http_semantics_and_wire_protocols.message_abstraction import (
    HttpRequest,
    HttpResponse,
    combine_field_values,
    exchange_h1,
    exchange_h2,
    exchange_h3,
    h2_connected_pair,
    h2_receive_message,
    h3_decode_stream,
    parse_h1_request,
    parse_h1_response,
    parse_h3_request,
    parse_h3_response,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from api_labs.f01_http_semantics_and_wire_protocols.message_abstraction import (
        Exchange,
        Field,
    )

ORDER_JSON = b'{"sku":"A-1","qty":3,"note":"' + b"x" * 64 + b'"}'  # > 63 bytes: 2-byte varint
REQUEST = HttpRequest(
    method=b"POST",
    scheme=b"https",
    authority=b"api.example.test",
    path=b"/v1/orders?dry_run=1",
    headers=(
        (b"content-type", b"application/json"),
        (b"x-trace", b"a"),
        (b"accept", b"application/json"),
        (b"x-trace", b"b"),
    ),
    content=ORDER_JSON,
    trailers=((b"x-checksum", b"sha256=abc123"),),
)
RESPONSE = HttpResponse(
    status=201,
    headers=((b"location", b"/v1/orders/42"), (b"content-type", b"application/json")),
    content=b'{"id":42}',
    trailers=((b"server-timing", b"db;dur=4"),),
)
EXCHANGES: dict[str, Callable[[HttpRequest, HttpResponse], Exchange]] = {
    "h11": exchange_h1,
    "h2": exchange_h2,
    "h3": exchange_h3,
}
CONTROL: list[Field] = [
    (b":method", b"GET"),
    (b":scheme", b"https"),
    (b":authority", b"api.example.test"),
    (b":path", b"/"),
]


def _raw_h2_headers_frame(block: list[Field]) -> bytes:
    """A HEADERS frame on stream 1 built without h2, so h2's outbound normalization is skipped."""
    frame = HeadersFrame(1)
    frame.data = hpack.Encoder().encode(block)
    frame.flags.add("END_HEADERS")
    frame.flags.add("END_STREAM")
    return frame.serialize()


@pytest.mark.unit
@pytest.mark.parametrize("version", list(EXCHANGES))
def test_one_abstract_exchange_has_identical_semantics_on_every_wire(version: str) -> None:
    exchange = EXCHANGES[version](REQUEST, RESPONSE)
    assert exchange.request.semantics() == REQUEST.semantics()
    assert exchange.response.semantics() == RESPONSE.semantics()
    assert exchange.request.trailers == REQUEST.trailers
    # Same-name field lines keep their relative order (RFC 9110 Section 5.3).
    assert [v for n, v in exchange.request.headers if n == b"x-trace"] == [b"a", b"b"]


@pytest.mark.unit
def test_control_data_is_a_start_line_in_h1_and_pseudo_headers_in_h2_and_h3() -> None:
    h1, h2x, h3x = (EXCHANGES[v](REQUEST, RESPONSE) for v in ("h11", "h2", "h3"))
    assert h1.request_wire.startswith(
        b"POST /v1/orders?dry_run=1 HTTP/1.1\r\nhost: api.example.test\r\n"
    )
    assert h1.response_wire.startswith(b"HTTP/1.1 201 \r\n")
    expected_control = [
        (b":method", b"POST"),
        (b":scheme", b"https"),
        (b":authority", b"api.example.test"),
        (b":path", b"/v1/orders?dry_run=1"),
    ]
    for exchange in (h2x, h3x):
        assert list(exchange.request_field_block[:4]) == expected_control
        assert b"host" not in dict(exchange.request_field_block)
        assert exchange.response_field_block[0] == (b":status", b"201")


@pytest.mark.unit
def test_h1_target_uri_takes_scheme_from_connection_and_absolute_form_overrides_host() -> None:
    origin_form = b"GET /a?b=1 HTTP/1.1\r\nHost: api.example.test\r\n\r\n"
    secure, _ = parse_h1_request(origin_form, secure=True)
    plain, _ = parse_h1_request(origin_form, secure=False)
    assert (secure.scheme, secure.authority, secure.path) == (
        b"https",
        b"api.example.test",
        b"/a?b=1",
    )
    assert plain.scheme == b"http"

    absolute_form = b"GET http://real.example.test/a?b=1 HTTP/1.1\r\nHost: decoy.example\r\n\r\n"
    proxied, raw = parse_h1_request(absolute_form, secure=True)
    assert (proxied.scheme, proxied.authority, proxied.path) == (
        b"http",
        b"real.example.test",
        b"/a?b=1",
    )
    assert (b"host", b"decoy.example") in raw  # still on the wire, ignored by RFC 9112 3.2.2

    # An absolute-form target with no path maps to "/" (RFC 9113 Section 8.3.1).
    no_path = b"GET http://real.example.test HTTP/1.1\r\nHost: x\r\n\r\n"
    bare, _ = parse_h1_request(no_path, secure=True)
    assert (bare.authority, bare.path) == (b"real.example.test", b"/")
    # Asterisk-form stays "*", the value HTTP/2 and HTTP/3 carry in :path for OPTIONS.
    star, _ = parse_h1_request(b"OPTIONS * HTTP/1.1\r\nHost: a.test\r\n\r\n", secure=True)
    assert star.path == b"*"


@pytest.mark.unit
def test_h1_reason_phrase_changes_bytes_but_not_the_message() -> None:
    ok = exchange_h1(REQUEST, RESPONSE, reason=b"Created")
    odd = exchange_h1(REQUEST, RESPONSE, reason=b"Totally Fine")
    assert ok.response_wire != odd.response_wire
    assert ok.response == odd.response
    _, _, reason = parse_h1_response(odd.response_wire, request_method=b"POST")
    assert reason == b"Totally Fine"


@pytest.mark.unit
def test_h1_response_framing_depends_on_the_request_method() -> None:
    wire = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nContent-Type: text/plain\r\n\r\n"
    head, raw, _ = parse_h1_response(wire, request_method=b"HEAD")
    assert head.content == b""
    assert (b"content-length", b"5") in raw
    with pytest.raises(ValueError, match=r"incomplete HTTP/1\.1 message"):
        parse_h1_response(wire, request_method=b"GET")


@pytest.mark.unit
def test_framing_fields_are_regenerated_per_version_and_never_stored() -> None:
    get = HttpRequest(b"GET", b"https", b"api.example.test", b"/v1/items")
    ok = HttpResponse(200, headers=((b"content-type", b"text/plain"),), content=b"hi")
    h1 = exchange_h1(get, ok)
    assert b"content-length" not in h1.request_wire  # no content, no Content-Length
    assert b"\r\ncontent-length: 2\r\n" in h1.response_wire
    assert h1.response == ok  # Content-Length was framing, not part of the header table
    assert b"content-length" not in exchange_h1(get, HttpResponse(204)).response_wire
    for exchange in (exchange_h2(get, ok), exchange_h3(get, ok)):
        assert exchange.response == ok
        assert b"content-length" not in dict(exchange.response_field_block)


@pytest.mark.unit
def test_connection_option_fields_are_removed_before_relaying_to_h2() -> None:
    wire = (
        b"GET /v1/items HTTP/1.1\r\n"
        b"Host: api.example.test\r\n"
        b"Connection: keep-alive, X-Hop\r\n"
        b"Keep-Alive: timeout=5\r\n"
        b"X-Hop: internal-token\r\n"
        b"Accept: */*\r\n\r\n"
    )
    request, _ = parse_h1_request(wire, secure=True)
    assert request.headers == ((b"accept", b"*/*"),)
    relayed = exchange_h2(request, HttpResponse(status=204))
    assert relayed.request.headers == ((b"accept", b"*/*"),)

    # h2 alone strips the fixed connection-specific set but forwards fields Connection named.
    client, server = h2_connected_pair()
    client.send_headers(
        1,
        [*CONTROL, (b"connection", b"x-hop"), (b"keep-alive", b"timeout=5"), (b"x-hop", b"t")],
        end_stream=True,
    )
    events = server.receive_data(client.data_to_send())
    received = next(e for e in events if isinstance(e, h2.events.RequestReceived))
    assert received.headers[4:] == [(b"x-hop", b"t")]


@pytest.mark.unit
def test_connection_options_also_remove_named_trailer_fields() -> None:
    # RFC 9110 Section 7.6.1: remove "any header or trailer field(s)" a connection-option names.
    body = b"3\r\nabc\r\n0\r\nX-Hop: secret\r\nX-Sum: 1\r\n\r\n"
    request, _ = parse_h1_request(
        b"POST /v1/items HTTP/1.1\r\nHost: api.example.test\r\nConnection: X-Hop\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n" + body,
        secure=True,
    )
    assert request.content == b"abc"
    assert request.trailers == ((b"x-sum", b"1"),)
    response, _, _ = parse_h1_response(
        b"HTTP/1.1 200 OK\r\nConnection: X-Hop\r\nTransfer-Encoding: chunked\r\n\r\n" + body,
        request_method=b"GET",
    )
    assert response.trailers == ((b"x-sum", b"1"),)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "match"),
    [
        ((b"Content-Type", b"text/plain"), "not a lowercase token"),
        ((b"x-a", b"v\r\nInjected: 1"), "CR, LF, or NUL"),
        ((b"x-a", b"v\x00"), "CR, LF, or NUL"),
        ((b"x-a", b" padded"), "leading or trailing whitespace"),
        ((b"x-a", b"padded\t"), "leading or trailing whitespace"),
        ((b"connection", b"close"), "not end-to-end"),
        ((b"transfer-encoding", b"chunked"), "not end-to-end"),
        ((b"host", b"api.example.test"), "not end-to-end"),
    ],
    ids=[
        "uppercase",
        "crlf",
        "nul",
        "whitespace",
        "trailing-tab",
        "connection",
        "te-chunked",
        "host",
    ],
)
def test_abstraction_rejects_fields_that_cannot_cross_versions(field: Field, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        HttpRequest(b"GET", b"https", b"a.test", b"/", headers=(field,))
    with pytest.raises(ValueError, match=match):
        HttpResponse(200, trailers=(field,))


@pytest.mark.unit
def test_h2_sender_does_not_screen_values_but_receiver_tears_down_the_connection() -> None:
    injected = (b"x-a", b"v\r\nInjected: 1")
    with pytest.raises(h11.LocalProtocolError, match="Illegal header value"):
        h11.Connection(h11.CLIENT).send(
            h11.Request(method=b"GET", target=b"/", headers=[(b"host", b"a.test"), injected])
        )

    client, server = h2_connected_pair()
    client.send_headers(1, [*CONTROL, injected], end_stream=True)  # h2 4.4.1 accepts this
    with pytest.raises(h2.exceptions.ProtocolError, match="Illegal character"):
        server.receive_data(client.data_to_send())
    reply = server.data_to_send()
    frame, length = Frame.parse_frame_header(memoryview(reply[:9]))
    frame.parse_body(memoryview(reply[9 : 9 + length]))
    assert isinstance(frame, GoAwayFrame)  # connection error, stricter than RFC 9113 8.1.1
    assert frame.error_code == h2.errors.ErrorCodes.PROTOCOL_ERROR


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "h2_match", "h3_match"),
    [
        ((b"X-Upper", b"1"), "uppercase header name", "invalid characters"),
        ((b"connection", b"close"), "Connection-specific", "connection-specific"),
        ((b"te", b"gzip"), "Invalid value for TE", "not trailers"),
    ],
    ids=["uppercase", "connection", "te-gzip"],
)
def test_h2_and_h3_receivers_treat_h1_only_fields_as_malformed(
    field: Field, h2_match: str, h3_match: str
) -> None:
    _, server = h2_connected_pair()
    with pytest.raises(h2.exceptions.ProtocolError, match=h2_match):
        server.receive_data(_raw_h2_headers_frame([*CONTROL, field]))
    with pytest.raises(MessageError, match=h3_match):
        validate_request_headers([*CONTROL, field])
    validate_request_headers([*CONTROL, (b"te", b"trailers")])  # the one TE value allowed


@pytest.mark.unit
def test_trailers_need_chunked_coding_in_h1_and_a_final_field_section_in_h2_and_h3() -> None:
    h1 = exchange_h1(REQUEST, RESPONSE)
    assert b"\r\ntransfer-encoding: chunked\r\n" in h1.request_wire
    assert h1.request_wire.endswith(b"\r\n0\r\nx-checksum: sha256=abc123\r\n\r\n")
    conn = h11.Connection(h11.CLIENT)
    framing = [(b"host", b"a"), (b"content-length", b"1")]
    conn.send(h11.Request(method=b"POST", target=b"/", headers=framing))
    conn.send(h11.Data(data=b"x"))
    with pytest.raises(h11.LocalProtocolError, match="Content-Length and trailers don't mix"):
        conn.send(h11.EndOfMessage(headers=[(b"x-checksum", b"1")]))

    client, _ = h2_connected_pair()
    client.send_headers(1, CONTROL, end_stream=False)
    with pytest.raises(h2.exceptions.ProtocolError, match="pseudo-header in trailer"):
        client.send_headers(1, [(b":status", b"200")], end_stream=True)

    _, _, trailers = h3_decode_stream(exchange_h3(REQUEST, RESPONSE).request_wire)
    assert trailers == ((b"x-checksum", b"sha256=abc123"),)
    with pytest.raises(MessageError, match="not valid"):
        validate_trailers([(b":status", b"200")])


@pytest.mark.unit
def test_h2_content_length_must_equal_the_sum_of_data_frames() -> None:
    client, server = h2_connected_pair()
    client.send_headers(1, [*CONTROL, (b"content-length", b"10")])
    client.send_data(1, b"abc", end_stream=True)
    with pytest.raises(h2.exceptions.InvalidBodyLengthError):
        server.receive_data(client.data_to_send())

    # A stream that never reaches END_STREAM is not a complete message.
    client, server = h2_connected_pair()
    client.send_headers(1, CONTROL, end_stream=False)
    with pytest.raises(ValueError, match="did not reach END_STREAM"):
        h2_receive_message(server, client.data_to_send())


@pytest.mark.unit
def test_repeated_fields_combine_with_comma_except_cookie_and_set_cookie() -> None:
    fields: list[Field] = [
        (b"cache-control", b"no-store"),
        (b"cookie", b"a=b"),
        (b"cache-control", b"private"),
        (b"cookie", b"c=d"),
        (b"set-cookie", b"s=1"),
    ]
    assert combine_field_values(fields, b"cache-control") == b"no-store, private"
    assert combine_field_values(fields, b"cookie") == b"a=b; c=d"
    assert combine_field_values(fields, b"absent") is None
    with pytest.raises(ValueError, match="Set-Cookie"):
        combine_field_values(fields, b"set-cookie")

    request = HttpRequest(
        b"GET",
        b"https",
        b"a.test",
        b"/",
        headers=((b"cookie", b"a=b; c=d"), (b"accept", b"*/*")),
    )
    relayed = exchange_h2(request, HttpResponse(204), split_outbound_cookies=True)
    # h2 rejoins the crumbs with "; " and moves cookie last; the field table is unchanged.
    assert relayed.request.headers == ((b"accept", b"*/*"), (b"cookie", b"a=b; c=d"))
    assert relayed.request.semantics() == request.semantics()


@pytest.mark.unit
def test_aioquic_1_7_2_request_pseudo_header_rules_differ_from_rfc_9114() -> None:
    no_scheme: list[Field] = [(b":method", b"GET"), (b":authority", b"a.test")]
    validate_request_headers(no_scheme)  # aioquic accepts a request with no :scheme or :path
    _, section = pylsqpack.Encoder().encode(0, no_scheme)
    with pytest.raises(ValueError, match="missing pseudo-header field :scheme"):
        parse_h3_request(encode_frame(FrameType.HEADERS, section))  # the mapping layer refuses
    with pytest.raises(MessageError, match="authority"):
        validate_request_headers(
            [(b":method", b"GET"), (b":scheme", b"https"), (b":path", b"/"), (b"host", b"a.test")]
        )
    _, server = h2_connected_pair()
    with pytest.raises(h2.exceptions.ProtocolError, match="scheme"):
        server.receive_data(_raw_h2_headers_frame(no_scheme))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("wire", "match"),
    [
        (encode_frame(FrameType.DATA, b"x"), "DATA outside the content position"),
        (encode_frame(FrameType.HEADERS, b"\x00\x00")[:-1], "truncated HTTP/3 frame payload"),
        (b"\x41", "truncated HTTP/3 frame header"),
        (b"\x01", "truncated HTTP/3 frame header"),
        (encode_frame(FrameType.SETTINGS, b""), "unexpected frame type 0x4"),
        (b"", "no HEADERS frame"),
    ],
    ids=["data-first", "short-payload", "short-varint", "no-length", "settings", "empty"],
)
def test_h3_stream_decoder_enforces_frame_sequence(wire: bytes, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        h3_decode_stream(wire)


@pytest.mark.unit
def test_h3_decoder_rejects_frames_after_trailers_and_reads_two_byte_varints() -> None:
    _, section = pylsqpack.Encoder().encode(0, [(b":status", b"200")])
    headers = encode_frame(FrameType.HEADERS, section)
    # RFC 9114 Section 4.1: HEADERS or DATA after the trailing HEADERS frame is invalid.
    with pytest.raises(ValueError, match="HEADERS after the trailer section"):
        h3_decode_stream(headers * 3)
    with pytest.raises(ValueError, match="DATA outside the content position"):
        h3_decode_stream(headers * 2 + encode_frame(FrameType.DATA, b"x"))
    big = encode_frame(FrameType.DATA, b"y" * 300)
    assert big[1:3] == b"\x41\x2c"  # 300 as a 2-byte QUIC varint (RFC 9000 Section 16)
    block, content, trailers = h3_decode_stream(headers + big)
    assert (block, content, trailers) == (((b":status", b"200"),), b"y" * 300, ())


@pytest.mark.unit
@pytest.mark.parametrize(
    "build",
    [
        lambda: HttpRequest(b"GET /", b"https", b"a.test", b"/"),
        lambda: HttpRequest(b"GET", b"https", b"a.test", b"relative"),
        lambda: HttpResponse(101),
        lambda: HttpResponse(600),
    ],
    ids=["method-space", "relative-path", "interim-status", "status-600"],
)
def test_abstraction_rejects_invalid_control_data(build: Callable[[], object]) -> None:
    with pytest.raises(ValueError, match=r"method|path|status"):
        build()


@pytest.mark.unit
def test_parsers_refuse_incomplete_heads_and_interim_responses() -> None:
    with pytest.raises(ValueError, match="expected a request head"):
        parse_h1_request(b"GET / HTTP/1.1\r\nHost: a.test\r\n", secure=True)
    with pytest.raises(ValueError, match="expected a final response head"):
        parse_h1_response(b"HTTP/1.1 100 Continue\r\n\r\n", request_method=b"GET")
    _, section = pylsqpack.Encoder().encode(0, [(b":status", b"204"), (b"Bad", b"1")])
    with pytest.raises(MessageError, match="invalid characters"):
        parse_h3_response(encode_frame(FrameType.HEADERS, section))
