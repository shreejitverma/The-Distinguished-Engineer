"""Unit tests for HTTP/1.1 request-smuggling framing: CL.TE, TE.CL, and TE.TE desync."""

from __future__ import annotations

from typing import TYPE_CHECKING

import h11
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from starlette.requests import Request

from api_labs.f01_http_semantics_and_wire_protocols.request_smuggling import (
    AmbiguousFraming,
    Framing,
    NeedMoreData,
    Prefer,
    classify_framing,
    forward_and_reparse,
    parse_one_message,
    parse_request_head,
    te_obfuscations,
)

# Classic CL.TE payload: front-end honours Content-Length (6 = len(b"0\r\n\r\nG")),
# back-end honours Transfer-Encoding and stops at the terminating chunk, so "G"
# is left as the prefix of a smuggled request.
CL_TE = (
    b"POST /a HTTP/1.1\r\n"
    b"Host: t\r\n"
    b"Content-Length: 6\r\n"
    b"Transfer-Encoding: chunked\r\n"
    b"\r\n"
    b"0\r\n\r\nG"
)

# Classic TE.CL payload: front-end honours Transfer-Encoding and forwards the
# whole chunked body, back-end honours Content-Length: 3 (b"8\r\n") and treats
# the rest as a new request beginning with "SMUGGLED".
TE_CL = (
    b"POST /a HTTP/1.1\r\n"
    b"Host: t\r\n"
    b"Content-Length: 3\r\n"
    b"Transfer-Encoding: chunked\r\n"
    b"\r\n"
    b"8\r\nSMUGGLED\r\n0\r\n\r\n"
)


@pytest.mark.unit
def test_cl_te_desync_leaves_smuggled_request_prefix() -> None:
    result = forward_and_reparse(CL_TE, front=Prefer.CL, back=Prefer.TE)
    assert result.front_body == b"0\r\n\r\nG"
    assert result.back_body == b"0\r\n\r\n"
    assert result.smuggled == b"G"
    assert result.starved is False
    assert result.rejected is False


@pytest.mark.unit
def test_te_cl_desync_smuggles_a_whole_request_line() -> None:
    result = forward_and_reparse(TE_CL, front=Prefer.TE, back=Prefer.CL)
    assert result.front_body == b"8\r\nSMUGGLED\r\n0\r\n\r\n"
    assert result.back_body == b"8\r\n"
    assert result.smuggled.startswith(b"SMUGGLED")
    assert result.starved is False


@pytest.mark.unit
def test_agreeing_recipients_do_not_desync() -> None:
    # An unambiguous Content-Length message frames identically under either policy.
    plain = b"POST /a HTTP/1.1\r\nHost: t\r\nContent-Length: 2\r\n\r\nhi"
    for front in (Prefer.CL, Prefer.TE):
        for back in (Prefer.CL, Prefer.TE):
            result = forward_and_reparse(plain, front=front, back=back)
            assert result.smuggled == b""
            assert result.starved is False
            assert result.front_body == result.back_body == b"hi"


@pytest.mark.unit
def test_unambiguous_chunked_frames_identically() -> None:
    chunked = (
        b"POST /a HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
    )
    msg = parse_one_message(chunked, prefer=Prefer.TE)
    assert msg.decision.framing is Framing.CHUNKED
    assert msg.end == len(chunked)
    result = forward_and_reparse(chunked, front=Prefer.TE, back=Prefer.CL)
    assert result.smuggled == b""
    assert result.starved is False


@pytest.mark.unit
def test_cl_preference_vs_te_preference_disagree_on_boundary() -> None:
    # The same bytes, framed two ways: the whole point of the desync.
    cl_view = parse_one_message(CL_TE, prefer=Prefer.CL)
    te_view = parse_one_message(CL_TE, prefer=Prefer.TE)
    assert cl_view.decision.framing is Framing.CONTENT_LENGTH
    assert te_view.decision.framing is Framing.CHUNKED
    assert cl_view.end != te_view.end
    assert cl_view.decision.is_ambiguous is True


@pytest.mark.unit
def test_strict_recipient_rejects_dual_content_length_and_transfer_encoding() -> None:
    _m, _t, headers = parse_request_head(CL_TE.split(b"\r\n\r\n", 1)[0])
    with pytest.raises(AmbiguousFraming, match="both Content-Length and Transfer-Encoding"):
        classify_framing(headers, strict=True)
    # The lenient recipient instead silently picks a winner, which is the hazard.
    lenient = classify_framing(headers, prefer=Prefer.TE, strict=False)
    assert lenient.framing is Framing.CHUNKED
    assert lenient.is_ambiguous is True


@pytest.mark.unit
def test_strict_recipient_rejects_conflicting_duplicate_content_length() -> None:
    headers = ((b"content-length", b"1"), (b"content-length", b"2"))
    with pytest.raises(AmbiguousFraming, match="conflicting Content-Length"):
        classify_framing(headers, strict=True)


def _strict_frame(raw_request: bytes) -> Framing:
    """Run the strict head parse plus framing classification, raising on ambiguity."""
    _m, _t, headers = parse_request_head(raw_request)
    return classify_framing(headers, strict=True).framing


@pytest.mark.unit
@pytest.mark.parametrize(("name", "te_line"), te_obfuscations())
def test_strict_pipeline_rejects_every_te_obfuscation(name: str, te_line: bytes) -> None:
    block = b"POST / HTTP/1.1\r\nHost: t\r\n" + te_line
    with pytest.raises((ValueError, AmbiguousFraming)):
        _strict_frame(block)


@pytest.mark.unit
def test_strict_head_parser_rejects_whitespace_before_colon_and_obs_fold() -> None:
    with pytest.raises(ValueError, match="whitespace before colon"):
        parse_request_head(b"GET / HTTP/1.1\r\nHost : t")
    with pytest.raises(ValueError, match="obsolete line folding"):
        parse_request_head(b"GET / HTTP/1.1\r\nHost: t\r\n Content-Length: 5")
    with pytest.raises(ValueError, match="bare CR"):
        parse_request_head(b"GET / HTTP/1.1\rHost: t")


@pytest.mark.unit
def test_strict_head_parser_rejects_control_octet_in_field_value() -> None:
    # A vertical tab (0x0b) inside a Transfer-Encoding value is a TE.TE obfuscation:
    # a lenient parser that strips it with bytes.strip() would read "chunked".
    with pytest.raises(ValueError, match="control octet in field value"):
        parse_request_head(b"POST / HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: \x0bchunked")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("te_values", "lenient_chunked"),
    [
        ((b"chunked, gzip",), False),
        ((b"gzip, chunked",), True),
        ((b"chunked, chunked",), True),
        ((b"x", b"chunked"), True),
    ],
)
def test_strict_recipient_accepts_only_a_single_chunked_coding(
    te_values: tuple[bytes, ...], lenient_chunked: bool
) -> None:
    headers = tuple((b"transfer-encoding", v) for v in te_values)
    with pytest.raises(AmbiguousFraming, match="not exactly one chunked coding"):
        classify_framing(headers, strict=True)
    # A lenient recipient frames by the final coding only and must reject a request
    # whose final coding is not chunked (RFC 9112 Section 6.3 step 4).
    if lenient_chunked:
        lenient = classify_framing(headers, prefer=Prefer.TE, strict=False)
        assert lenient.framing is Framing.CHUNKED
    else:
        with pytest.raises(AmbiguousFraming, match="final transfer coding"):
            classify_framing(headers, prefer=Prefer.TE, strict=False)
    # The token is case-insensitive, so a single "Chunked" is accepted strictly.
    single = classify_framing(((b"transfer-encoding", b"Chunked"),), strict=True)
    assert single.framing is Framing.CHUNKED


@pytest.mark.unit
def test_transfer_encoding_presence_makes_a_message_ambiguous_even_if_not_chunked() -> None:
    # The prefixed-token TE.TE obfuscation: a Transfer-Encoding is present, so step 3
    # makes it override Content-Length, but its final coding is not chunked (step 4).
    headers = ((b"content-length", b"4"), (b"transfer-encoding", b"xchunked"))
    with pytest.raises(AmbiguousFraming, match="final transfer coding"):
        classify_framing(headers, prefer=Prefer.TE)
    legacy = classify_framing(headers, prefer=Prefer.CL)
    assert legacy.framing is Framing.CONTENT_LENGTH
    assert legacy.content_length == 4
    assert legacy.is_ambiguous is True
    # A request with neither header frames a zero-length body (step 7).
    empty = parse_one_message(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
    assert empty.decision.framing is Framing.NONE
    assert empty.body == b""


@pytest.mark.unit
def test_malformed_messages_are_rejected() -> None:
    with pytest.raises(ValueError, match="no CRLFCRLF"):
        parse_one_message(b"GET / HTTP/1.1\r\nHost: t\r\n")
    with pytest.raises(ValueError, match="malformed request-line"):
        parse_request_head(b"GET /only-two-parts\r\nHost: t")
    with pytest.raises(ValueError, match="malformed field-line"):
        parse_request_head(b"GET / HTTP/1.1\r\nnot-a-header")
    with pytest.raises(AmbiguousFraming, match="invalid Content-Length"):
        classify_framing(((b"content-length", b"1_000"),))
    with pytest.raises(ValueError, match="incomplete body"):
        parse_one_message(b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 50\r\n\r\nshort")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("bad_body", "error", "match"),
    [
        (b"zz\r\nabc\r\n0\r\n\r\n", ValueError, "invalid chunk-size"),
        (b"3\r\nabc\r", NeedMoreData, "truncated chunk data"),
        (b"3\r\nabc\r\n", NeedMoreData, "unterminated chunk-size"),
        # CVE-2025-43859 shape: two garbage octets where the chunk-data CRLF belongs.
        (b"3\r\nabcXX0\r\n\r\n", ValueError, "missing chunk CRLF"),
    ],
)
def test_chunked_parser_tells_truncated_from_malformed_bodies(
    bad_body: bytes, error: type[ValueError], match: str
) -> None:
    raw = b"POST / HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n" + bad_body
    with pytest.raises(ValueError, match=match) as exc_info:
        parse_one_message(raw, prefer=Prefer.TE)
    assert exc_info.type is error


_CHUNKED_HEAD = b"POST / HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n"


@pytest.mark.unit
def test_chunk_size_is_capped_to_prevent_integer_overflow() -> None:
    # RFC 9112 Section 7.1: a recipient MUST prevent overflow on a huge chunk-size.
    # 16 hex digits is the last accepted width (the failure is then the missing data);
    # 17 digits is rejected as an invalid chunk-size before any conversion.
    with pytest.raises(NeedMoreData, match="truncated chunk data"):
        parse_one_message(_CHUNKED_HEAD + b"0000000000000010\r\nabc\r\n", prefer=Prefer.TE)
    with pytest.raises(ValueError, match="invalid chunk-size"):
        parse_one_message(_CHUNKED_HEAD + b"00000000000000010\r\nabc\r\n", prefer=Prefer.TE)


@pytest.mark.unit
def test_chunked_parser_consumes_trailers_and_ignores_chunk_extensions() -> None:
    # The terminating chunk ends only after the trailer section's blank line, so the
    # next pipelined request starts exactly at msg.end, not inside the trailer.
    follow_on = b"GET /x HTTP/1.1\r\nHost: t\r\n\r\n"
    message = _CHUNKED_HEAD + b"5;ext=1\r\nhello\r\n0\r\nX-Trailer: 1\r\n\r\n"
    msg = parse_one_message(message + follow_on, prefer=Prefer.TE)
    assert msg.end == len(message)
    assert msg.body == b"5;ext=1\r\nhello\r\n0\r\nX-Trailer: 1\r\n\r\n"


@pytest.mark.unit
def test_back_end_starves_on_a_longer_body_but_rejects_a_malformed_one() -> None:
    # Front-end frames with chunked (forwards a short terminating chunk); back-end
    # trusts a large Content-Length and blocks waiting for bytes never forwarded.
    raw = (
        b"POST /a HTTP/1.1\r\nHost: t\r\n"
        b"Content-Length: 100\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
    )
    result = forward_and_reparse(raw, front=Prefer.TE, back=Prefer.CL)
    assert result.starved is True
    assert result.rejected is False
    assert result.smuggled == b""
    # The front-end forwards all nine Content-Length octets, but "zz" is not a
    # chunk-size: a chunked back-end answers 400 and closes instead of waiting.
    malformed = (
        b"POST / HTTP/1.1\r\nHost: t\r\n"
        b"Content-Length: 9\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\nabcde"
    )
    rejected = forward_and_reparse(malformed, front=Prefer.CL, back=Prefer.TE)
    assert rejected.front_body == b"zz\r\nabcde"
    assert rejected.rejected is True
    assert rejected.starved is False
    assert rejected.smuggled == b""


@pytest.mark.unit
def test_predict_the_output_smuggled_byte_is_g() -> None:
    # Documented worked-example assertion: the leftover byte is exactly b"G".
    result = forward_and_reparse(CL_TE, front=Prefer.CL, back=Prefer.TE)
    assert result.smuggled == b"G"


@pytest.mark.unit
def test_h11_server_takes_te_and_leaves_trailing_bytes_without_closing() -> None:
    # h11 0.16.0 resolves CL+TE to Transfer-Encoding (RFC 9112 Section 6.3 step 3)
    # but does not close the connection, so the smuggled "G" stays buffered and a
    # reused connection would parse it as the next request. See h11 issue #90.
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(CL_TE)
    assert isinstance(conn.next_event(), h11.Request)
    assert isinstance(conn.next_event(), h11.EndOfMessage)
    assert conn.their_state is h11.DONE
    conn.send(h11.Response(status_code=200, headers=[(b"content-length", b"0")]))
    conn.send(h11.EndOfMessage())
    assert bytes(conn.trailing_data[0]) == b"G"
    # No framing error forces MUST_CLOSE: the cycle can be reused.
    conn.start_next_cycle()
    assert conn.next_event() is h11.NEED_DATA


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_app_frames_a_normal_request_without_a_second_boundary() -> None:
    seen: list[tuple[str, bytes]] = []

    async def endpoint(request: Request) -> PlainTextResponse:
        body = await request.body()
        seen.append((request.url.path, body))
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/ingest", endpoint, methods=["POST"])])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # The smuggling bytes carried as a legitimate request body stay one request:
        # ASGI delivers exactly one call with the body intact, no smuggled second path.
        resp = await client.post("/ingest", content=b"0\r\n\r\nG")
        assert resp.status_code == 200
    assert seen == [("/ingest", b"0\r\n\r\nG")]
