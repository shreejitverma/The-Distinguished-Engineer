"""Wire-level proofs for gRPC over HTTP/2: envelopes, request rules, trailers, and status."""

from __future__ import annotations

import zlib

import h2.connection
import h2.errors
import h2.exceptions
import pytest

from api_labs.f19_grpc_and_protocol_buffers.grpc_http2_framing import (
    DEFAULT_MAX_RECV,
    Code,
    FramingError,
    Message,
    MessageDecoder,
    Shape,
    build_request_headers,
    check_request_headers,
    compress,
    decode_binary_metadata,
    decode_payload,
    decode_status_message,
    dump_frames,
    encode_binary_metadata,
    encode_message,
    encode_status_message,
    h2_pair,
    respond_ok,
    respond_trailers_only,
    run_unary,
)


def _no_len(lines: list[str]) -> list[str]:
    return [line.rsplit(" len=", 1)[0] for line in lines]


# --- the 5-byte envelope ---


@pytest.mark.unit
def test_prefix_is_flag_byte_then_big_endian_uint32_length() -> None:
    assert encode_message(b"abc") == b"\x00\x00\x00\x00\x03abc"
    assert encode_message(b"x" * 300)[:5] == b"\x00\x00\x00\x01\x2c"
    assert encode_message(b"abc", compressed=True)[:5] == b"\x01\x00\x00\x00\x03"


@pytest.mark.unit
def test_empty_message_is_exactly_five_bytes_and_still_a_message() -> None:
    wire = encode_message(b"")
    assert wire == b"\x00\x00\x00\x00\x00"
    assert MessageDecoder().feed(wire) == [Message(compressed=False, payload=b"")]


@pytest.mark.unit
def test_decoder_reassembles_messages_from_any_data_frame_split() -> None:
    wire = encode_message(b"one") + encode_message(b"") + encode_message(b"three" * 50)
    expected = [b"one", b"", b"three" * 50]
    for step in (1, 2, 4, 5, 6, 7, 64, len(wire)):
        decoder = MessageDecoder()
        got: list[bytes] = []
        for i in range(0, len(wire), step):
            got += [m.payload for m in decoder.feed(wire[i : i + step])]
        decoder.finish()
        assert got == expected, step


@pytest.mark.unit
def test_message_is_not_released_before_its_last_byte_arrives() -> None:
    wire = encode_message(b"hello")
    decoder = MessageDecoder()
    assert decoder.feed(wire[:-1]) == []
    assert decoder.buffered == len(wire) - 1
    assert [m.payload for m in decoder.feed(wire[-1:])] == [b"hello"]


@pytest.mark.unit
@pytest.mark.parametrize("flag", [2, 0x80, 0xFF])
def test_flag_other_than_0_or_1_is_an_internal_error(flag: int) -> None:
    with pytest.raises(FramingError) as info:
        MessageDecoder().feed(bytes([flag]) + b"\x00\x00\x00\x00")
    assert info.value.code is Code.INTERNAL
    assert f"0x{flag:02x}" in str(info.value)


@pytest.mark.unit
def test_oversize_length_is_rejected_from_the_prefix_alone() -> None:
    decoder = MessageDecoder()
    prefix = b"\x00" + (DEFAULT_MAX_RECV + 1).to_bytes(4, "big")
    with pytest.raises(FramingError) as info:
        decoder.feed(prefix)  # no body bytes have arrived yet
    assert info.value.code is Code.RESOURCE_EXHAUSTED
    at_limit = MessageDecoder()
    assert at_limit.feed(b"\x00" + DEFAULT_MAX_RECV.to_bytes(4, "big")) == []
    assert at_limit.buffered == 5


@pytest.mark.unit
def test_stream_ending_inside_a_message_is_detected() -> None:
    decoder = MessageDecoder()
    decoder.feed(encode_message(b"abcdef")[:-2])
    with pytest.raises(FramingError) as info:
        decoder.finish()
    assert info.value.code is Code.INTERNAL
    MessageDecoder().finish()  # an empty buffer at END_STREAM is fine


# --- Message-Encoding and the Compressed-Flag ---


@pytest.mark.unit
@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_compressed_message_round_trips_with_declared_encoding(encoding: str) -> None:
    body = b"grpc " * 200
    message = Message(compressed=True, payload=compress(body, encoding))
    assert decode_payload(message, encoding, is_server=True) == body


@pytest.mark.unit
def test_gzip_and_deflate_have_different_containers() -> None:
    assert compress(b"x", "gzip")[:2] == b"\x1f\x8b"
    assert compress(b"x", "deflate")[0] == 0x78


@pytest.mark.unit
def test_raw_deflate_is_not_accepted_as_deflate() -> None:
    comp = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = comp.compress(b"hello") + comp.flush()
    with pytest.raises(FramingError) as info:
        decode_payload(Message(True, raw), "deflate", is_server=True)
    assert info.value.code is Code.INTERNAL


@pytest.mark.unit
@pytest.mark.parametrize("encoding", [None, "identity"])
def test_compressed_flag_without_a_real_encoding_is_internal(encoding: str | None) -> None:
    with pytest.raises(FramingError) as info:
        decode_payload(Message(True, b"x"), encoding, is_server=True)
    assert info.value.code is Code.INTERNAL


@pytest.mark.unit
def test_unknown_encoding_is_unimplemented_on_server_and_internal_on_client() -> None:
    message = Message(True, b"x")
    with pytest.raises(FramingError) as server:
        decode_payload(message, "snappy", is_server=True)
    with pytest.raises(FramingError) as client:
        decode_payload(message, "snappy", is_server=False)
    assert (server.value.code, client.value.code) == (Code.UNIMPLEMENTED, Code.INTERNAL)


@pytest.mark.unit
def test_uncompressed_flag_ignores_grpc_encoding_header() -> None:
    assert decode_payload(Message(False, b"plain"), "gzip", is_server=True) == b"plain"


@pytest.mark.unit
def test_decompression_bomb_is_cut_off_at_the_receive_limit() -> None:
    bomb = compress(b"\x00" * (8 * 1024 * 1024), "gzip")
    assert len(bomb) < 20_000  # 8 MiB of zeros squeezes into a few KiB on the wire
    with pytest.raises(FramingError) as info:
        decode_payload(Message(True, bomb), "gzip", is_server=True, max_size=1024 * 1024)
    assert info.value.code is Code.RESOURCE_EXHAUSTED
    ok = decode_payload(Message(True, bomb), "gzip", is_server=True, max_size=8 * 1024 * 1024)
    assert len(ok) == 8 * 1024 * 1024


# --- grpc-message and -bin metadata ---


@pytest.mark.unit
def test_status_message_is_utf8_then_percent_encoded_with_upper_hex() -> None:
    assert encode_status_message("caf\u00e9 100%") == "caf%C3%A9 100%25"
    assert encode_status_message("line\nbreak") == "line%0Abreak"
    assert decode_status_message("caf%C3%A9 100%25") == "caf\u00e9 100%"


@pytest.mark.unit
def test_status_message_decoder_never_raises_on_bad_input() -> None:
    assert decode_status_message("bad%zzescape") == "bad%zzescape"
    assert decode_status_message("trailing%4") == "trailing%4"
    assert decode_status_message("%FF") == "\ufffd"


@pytest.mark.unit
def test_binary_metadata_is_unpadded_out_and_padding_tolerant_in() -> None:
    assert encode_binary_metadata(b"a") == "YQ"
    assert encode_binary_metadata(b"ab") == "YWI"
    assert decode_binary_metadata("YQ") == [b"a"]
    assert decode_binary_metadata("YQ==") == [b"a"]
    assert decode_binary_metadata("YQ,YWI=") == [b"a", b"ab"]


# --- Request-Headers ---


@pytest.mark.unit
def test_request_headers_put_pseudo_headers_first_and_carry_te_trailers() -> None:
    headers = build_request_headers(
        "demo.v1.Echo", "Say", authority="grpc.test", timeout="1S", metadata=[("x-id", "7")]
    )
    names = [k for k, _ in headers]
    assert names[:4] == [":method", ":scheme", ":path", ":authority"]
    assert names[4] == "grpc-timeout"
    assert names.index("x-id") > names.index("content-type")
    seen = dict(headers)
    assert (seen[":method"], seen[":path"], seen["te"]) == ("POST", "/demo.v1.Echo/Say", "trailers")


@pytest.mark.unit
def test_h2_refuses_a_pseudo_header_after_a_regular_header() -> None:
    client, _ = h2_pair()
    sid = client.get_next_available_stream_id()
    bad = [(":method", "POST"), ("content-type", "application/grpc"), (":path", "/a.B/C")]
    with pytest.raises(h2.exceptions.ProtocolError):
        client.send_headers(sid, bad)


@pytest.mark.unit
def test_h2_refuses_pseudo_headers_in_trailers() -> None:
    client, server = h2_pair()
    sid = client.get_next_available_stream_id()
    client.send_headers(sid, build_request_headers("a.B", "C", authority="x"), end_stream=True)
    server.receive_data(client.data_to_send())
    server.send_headers(sid, [(":status", "200"), ("content-type", "application/grpc")])
    with pytest.raises(h2.exceptions.ProtocolError):
        server.send_headers(sid, [(":status", "200"), ("grpc-status", "0")], end_stream=True)


@pytest.mark.unit
def test_h2_refuses_trailers_that_do_not_end_the_stream() -> None:
    client, server = h2_pair()
    sid = client.get_next_available_stream_id()
    client.send_headers(sid, build_request_headers("a.B", "C", authority="x"), end_stream=True)
    server.receive_data(client.data_to_send())
    server.send_headers(sid, [(":status", "200"), ("content-type", "application/grpc")])
    server.send_data(sid, encode_message(b"x"))
    with pytest.raises(h2.exceptions.ProtocolError):
        server.send_headers(sid, [("grpc-status", "0")])  # no END_STREAM


@pytest.mark.unit
def test_non_grpc_content_type_gets_http_415() -> None:
    abort = check_request_headers(
        build_request_headers("a.B", "C", authority="x", content_type="application/json")
    )
    assert abort is not None
    assert (abort.http_status, abort.code) == (415, Code.INVALID_ARGUMENT)


@pytest.mark.unit
@pytest.mark.parametrize(
    "ctype", ["application/grpc", "application/grpc+proto", "application/grpc;x=1"]
)
def test_grpc_content_type_forms_are_accepted(ctype: str) -> None:
    assert (
        check_request_headers(build_request_headers("a.B", "C", authority="x", content_type=ctype))
        is None
    )


@pytest.mark.unit
def test_prefix_match_alone_is_not_enough_for_a_grpc_content_type() -> None:
    abort = check_request_headers(
        build_request_headers("a.B", "C", authority="x", content_type="application/grpcfoo")
    )
    assert abort is not None
    assert abort.http_status == 415


@pytest.mark.unit
def test_non_post_and_malformed_path_are_rejected() -> None:
    get = [(":method", "GET"), (":path", "/a.B/C"), ("content-type", "application/grpc")]
    abort = check_request_headers(get)
    assert abort is not None
    assert abort.http_status == 405
    for path in ("/a.B", "a.B/C", "/a.B/C/D", "//C"):
        headers = [(":method", "POST"), (":path", path), ("content-type", "application/grpc")]
        bad = check_request_headers(headers)
        assert bad is not None, path
        assert (bad.http_status, bad.code) == (200, Code.UNIMPLEMENTED)


# --- Responses on a real h2 state machine ---


@pytest.mark.unit
def test_unary_request_is_headers_then_data_with_end_stream_on_the_last_data() -> None:
    exchange = run_unary(b"ping", respond_ok(b"pong"))
    assert _no_len(dump_frames(exchange.request_bytes)) == [
        "HEADERS END_HEADERS",
        "DATA END_STREAM",
    ]
    data_len = int(dump_frames(exchange.request_bytes)[1].rsplit("=", 1)[1])
    assert data_len == 5 + len(b"ping")


@pytest.mark.unit
def test_normal_response_is_headers_data_then_trailers_with_end_stream() -> None:
    exchange = run_unary(b"ping", respond_ok(b"pong"))
    assert _no_len(dump_frames(exchange.response_bytes)) == [
        "HEADERS END_HEADERS",
        "DATA -",
        "HEADERS END_HEADERS|END_STREAM",
    ]
    result = exchange.result
    assert (result.shape, result.code, result.messages) == (
        Shape.HEADERS_AND_TRAILERS,
        Code.OK,
        (b"pong",),
    )


@pytest.mark.unit
def test_trailers_only_is_a_single_headers_frame_carrying_the_status() -> None:
    exchange = run_unary(b"ping", respond_trailers_only(Code.NOT_FOUND, "no such item: caf\u00e9"))
    assert _no_len(dump_frames(exchange.response_bytes)) == ["HEADERS END_HEADERS|END_STREAM"]
    result = exchange.result
    assert result.shape is Shape.TRAILERS_ONLY
    assert result.code is Code.NOT_FOUND
    assert result.message == "no such item: caf\u00e9"
    assert result.messages == ()


@pytest.mark.unit
def test_a_message_spans_data_frames_and_the_receiver_still_sees_one_message() -> None:
    payload = bytes(range(256)) * 157  # 40192 bytes, plus the 5-byte prefix
    exchange = run_unary(b"q", respond_ok(payload), max_frame=16384)
    lines = dump_frames(exchange.response_bytes)
    data_lens = [int(x.rsplit("=", 1)[1]) for x in lines if x.startswith("DATA")]
    assert data_lens == [16384, 16384, len(payload) + 5 - 2 * 16384]
    assert exchange.result.messages == (payload,)
    assert exchange.result.frames_before_status == 3


@pytest.mark.unit
def test_missing_grpc_status_is_synthesized_from_the_http_status() -> None:
    def proxy_503(server: h2.connection.H2Connection, sid: int) -> None:
        server.send_headers(sid, [(":status", "503"), ("content-type", "text/html")])
        server.send_data(sid, b"<html>bad gateway</html>", end_stream=True)

    def ok_200_without_status(server: h2.connection.H2Connection, sid: int) -> None:
        server.send_headers(sid, [(":status", "200"), ("content-type", "application/grpc")])
        server.send_data(sid, encode_message(b"x"))
        server.send_headers(sid, [("x-trace", "1")], end_stream=True)

    r503 = run_unary(b"q", proxy_503).result
    assert (r503.shape, r503.code) == (Shape.SYNTHESIZED, Code.UNAVAILABLE)
    r200 = run_unary(b"q", ok_200_without_status).result
    assert (r200.shape, r200.code) == (Shape.SYNTHESIZED, Code.UNKNOWN)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("http", "code"),
    [
        (400, Code.INTERNAL),
        (401, Code.UNAUTHENTICATED),
        (403, Code.PERMISSION_DENIED),
        (404, Code.UNIMPLEMENTED),
        (429, Code.UNAVAILABLE),
        (502, Code.UNAVAILABLE),
        (504, Code.UNAVAILABLE),
        (418, Code.UNKNOWN),
    ],
)
def test_http_status_mapping_table(http: int, code: Code) -> None:
    def respond(server: h2.connection.H2Connection, sid: int) -> None:
        server.send_headers(sid, [(":status", str(http)), ("content-type", "text/plain")])
        server.send_data(sid, b"x", end_stream=True)

    assert run_unary(b"q", respond).result.code is code


@pytest.mark.unit
def test_grpc_status_wins_over_a_contradicting_http_status() -> None:
    def respond(server: h2.connection.H2Connection, sid: int) -> None:
        server.send_headers(
            sid,
            [(":status", "502"), ("content-type", "application/grpc"), ("grpc-status", "5")],
            end_stream=True,
        )

    result = run_unary(b"q", respond).result
    assert (result.shape, result.code) == (Shape.TRAILERS_ONLY, Code.NOT_FOUND)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("h2_code", "grpc_code"),
    [
        (h2.errors.ErrorCodes.REFUSED_STREAM, Code.UNAVAILABLE),
        (h2.errors.ErrorCodes.CANCEL, Code.CANCELLED),
        (h2.errors.ErrorCodes.ENHANCE_YOUR_CALM, Code.RESOURCE_EXHAUSTED),
        (h2.errors.ErrorCodes.INADEQUATE_SECURITY, Code.PERMISSION_DENIED),
        (h2.errors.ErrorCodes.NO_ERROR, Code.INTERNAL),
        (h2.errors.ErrorCodes.PROTOCOL_ERROR, Code.INTERNAL),
        (h2.errors.ErrorCodes.FLOW_CONTROL_ERROR, Code.INTERNAL),
    ],
)
def test_rst_stream_error_codes_map_to_grpc_codes(
    h2_code: h2.errors.ErrorCodes, grpc_code: Code
) -> None:
    def respond(server: h2.connection.H2Connection, sid: int) -> None:
        server.reset_stream(sid, error_code=h2_code)

    result = run_unary(b"q", respond).result
    assert (result.shape, result.code) == (Shape.SYNTHESIZED, grpc_code)
