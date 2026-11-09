"""Protocol discovery labs: ALPN over real TLS, Alt-Svc parsing and caching, HTTPS records."""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING

import h2.config
import h2.connection
import h2.exceptions
import httpx
import pytest
from hypercorn.config import Config as HypercornConfig
from hyperframe.frame import AltSvcFrame
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from api_labs.f01_http_semantics_and_wire_protocols.protocol_discovery import (
    DEFAULT_MAX_AGE,
    DELTA_SECONDS_CAP,
    ALPNError,
    AltService,
    AltSvcCache,
    AltSvcClear,
    AltSvcParseError,
    ConnectionAttempt,
    Origin,
    SvcbFormatError,
    alt_used_value,
    decode_protocol_name_list,
    decode_svcb_rdata,
    encode_protocol_name_list,
    h2_altsvc_exchange,
    h2_client_altsvc_events,
    h2_connected_client,
    https_alpn_set,
    is_compatible,
    make_self_signed_cert,
    memory_handshake,
    parse_alt_svc,
    parse_client_hello,
    plan_https_connections,
    select_alpn,
)

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request

ORIGIN_HOST = "origin.example.test"
ORIGIN = Origin("https", ORIGIN_HOST, 443)


@pytest.fixture(scope="module")
def origin_cert(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    directory = tmp_path_factory.mktemp("tls")
    cert_pem, key_pem = make_self_signed_cert([ORIGIN_HOST])
    cert, key = directory / "cert.pem", directory / "key.pem"
    cert.write_bytes(cert_pem)
    key.write_bytes(key_pem)
    return cert, key


def _server_ctx(cert: tuple[Path, Path], alpn: list[str] | None) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert[0], cert[1])
    if alpn is not None:
        ctx.set_alpn_protocols(alpn)
    return ctx


def _client_ctx(cert: tuple[Path, Path], alpn: list[str] | None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(cert[0]))
    if alpn is not None:
        ctx.set_alpn_protocols(alpn)
    return ctx


# --- ALPN wire format and selection (RFC 7301) ------------------------------------------


@pytest.mark.unit
def test_protocol_name_list_wire_bytes() -> None:
    wire = encode_protocol_name_list([b"h2", b"http/1.1"])
    assert wire == b"\x00\x0c\x02h2\x08http/1.1"
    assert decode_protocol_name_list(wire) == (b"h2", b"http/1.1")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("names", "match"),
    [
        ([b""], "outside 1..255"),
        ([b"x" * 256], "outside 1..255"),
        ([], "outside 2..65535"),
    ],
)
def test_protocol_name_list_encode_rejects(names: list[bytes], match: str) -> None:
    with pytest.raises(ALPNError, match=match):
        encode_protocol_name_list(names)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("wire", "match"),
    [
        (b"\x00", "truncated ProtocolNameList length"),
        (b"\x00\x00", "does not match"),  # protocol_name_list<2..2^16-1>: never empty
        (b"\x00\x05\x02h2", "does not match"),
        (b"\x00\x03\x00h2", "empty ProtocolName"),
        (b"\x00\x03\x05h2", "truncated ProtocolName"),
    ],
)
def test_protocol_name_list_decode_rejects(wire: bytes, match: str) -> None:
    with pytest.raises(ALPNError, match=match):
        decode_protocol_name_list(wire)


@pytest.mark.unit
def test_select_alpn_is_server_preference_order() -> None:
    assert select_alpn([b"h2", b"http/1.1"], [b"http/1.1", b"h2"]) == b"h2"
    assert select_alpn([b"http/1.1", b"h2"], [b"h2", b"http/1.1"]) == b"http/1.1"
    assert select_alpn([b"h2"], [b"http/1.1"]) is None


@pytest.mark.unit
def test_real_tls_handshake_server_preference_beats_client_order(
    origin_cert: tuple[Path, Path],
) -> None:
    # httpcore 1.0.9 offers ["http/1.1", "h2"] when http2=True; the server's order decides.
    result = memory_handshake(
        _client_ctx(origin_cert, ["http/1.1", "h2"]),
        _server_ctx(origin_cert, ["h2", "http/1.1"]),
        ORIGIN_HOST,
    )
    assert result.client_hello.alpn == (b"http/1.1", b"h2")
    assert result.client_alpn == result.server_alpn == "h2"
    assert result.tls_version == "TLSv1.3"

    flipped = memory_handshake(
        _client_ctx(origin_cert, ["h2", "http/1.1"]),
        _server_ctx(origin_cert, ["http/1.1", "h2"]),
        ORIGIN_HOST,
    )
    assert flipped.client_alpn == flipped.server_alpn == "http/1.1"


@pytest.mark.unit
def test_cpython_completes_handshake_without_alpn_when_no_overlap(
    origin_cert: tuple[Path, Path],
) -> None:
    # RFC 7301 Section 3.2 says the server SHALL send no_application_protocol (120);
    # CPython's _selectALPN_cb returns SSL_TLSEXT_ERR_NOACK, so the handshake succeeds.
    result = memory_handshake(
        _client_ctx(origin_cert, ["http/1.1"]),
        _server_ctx(origin_cert, ["h2"]),
        ORIGIN_HOST,
    )
    assert result.tls_version == "TLSv1.3"
    assert result.client_alpn is None
    assert result.server_alpn is None


@pytest.mark.unit
def test_client_without_alpn_sends_no_extension_16(origin_cert: tuple[Path, Path]) -> None:
    with_alpn = memory_handshake(
        _client_ctx(origin_cert, ["h2"]), _server_ctx(origin_cert, ["h2"]), ORIGIN_HOST
    )
    without = memory_handshake(
        _client_ctx(origin_cert, None), _server_ctx(origin_cert, ["h2"]), ORIGIN_HOST
    )
    assert 16 in with_alpn.client_hello.extension_types
    assert 16 not in without.client_hello.extension_types
    assert without.client_hello.alpn is None
    assert without.server_alpn is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("record", "match"),
    [
        (b"\x16\x03\x01", "truncated ClientHello"),
        (b"\x17\x03\x03\x00\x01\x00", r"expected handshake record \(22\), got 23"),
        (b"\x16\x03\x01\x00\x04\x02\x00\x00\x00", r"expected ClientHello \(1\), got"),
        (b"\x16\x03\x01\x00\x08\x01\x00\x00\x40\x03\x03\x00\x00", "truncated ClientHello"),
    ],
)
def test_parse_client_hello_rejects_non_client_hello(record: bytes, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        parse_client_hello(record)


@pytest.mark.unit
def test_alternative_must_present_a_certificate_for_the_origin(
    origin_cert: tuple[Path, Path],
) -> None:
    # The client connects to the alternative's address but names the origin in SNI and
    # verifies the certificate against the origin (RFC 7838 Sections 2.1 and 2.3).
    ok = memory_handshake(
        _client_ctx(origin_cert, ["h2"]), _server_ctx(origin_cert, ["h2"]), ORIGIN_HOST
    )
    assert ok.client_hello.server_name == ORIGIN_HOST
    with pytest.raises(ssl.SSLCertVerificationError, match=r"alt\.example\.net"):
        memory_handshake(
            _client_ctx(origin_cert, ["h2"]),
            _server_ctx(origin_cert, ["h2"]),
            "alt.example.net",
        )


@pytest.mark.unit
def test_hypercorn_0_18_0_tls_context_prefers_h2(origin_cert: tuple[Path, Path]) -> None:
    config = HypercornConfig()
    config.certfile = str(origin_cert[0])
    config.keyfile = str(origin_cert[1])
    server_ctx = config.create_ssl_context()
    assert server_ctx is not None
    h2_offer = memory_handshake(
        _client_ctx(origin_cert, ["http/1.1", "h2"]), server_ctx, ORIGIN_HOST
    )
    h1_offer = memory_handshake(_client_ctx(origin_cert, ["http/1.1"]), server_ctx, ORIGIN_HOST)
    assert h2_offer.server_alpn == "h2"
    assert h1_offer.server_alpn == "http/1.1"


# --- Alt-Svc grammar (RFC 7838 Section 3) ------------------------------------------------


def _single(field_value: str) -> AltService:
    parsed = parse_alt_svc(field_value)
    assert isinstance(parsed, tuple)
    assert len(parsed) == 1
    return parsed[0]


@pytest.mark.unit
def test_alt_svc_rfc_examples_parse() -> None:
    assert parse_alt_svc('h2=":8000"') == (AltService(b"h2", "", 8000),)
    assert parse_alt_svc('h2="new.example.org:80"') == (AltService(b"h2", "new.example.org", 80),)
    assert parse_alt_svc('h2="alt.example.com:8000", h2=":443"') == (
        AltService(b"h2", "alt.example.com", 8000),
        AltService(b"h2", "", 443),
    )
    assert parse_alt_svc('h2=":443"; ma=2592000; persist=1') == (
        AltService(b"h2", "", 443, max_age=2592000, persist=True),
    )
    assert parse_alt_svc('h3=":50781"') == (AltService(b"h3", "", 50781, DEFAULT_MAX_AGE),)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("protocol_id", "alpn"),
    [("h2", b"h2"), ("w%3Dx%3Ay#z", b"w=x:y#z"), ("x%25y", b"x%y")],
)
def test_protocol_id_percent_decoding_table(protocol_id: str, alpn: bytes) -> None:
    alt = _single(f'{protocol_id}=":443"')
    assert alt.alpn == alpn


@pytest.mark.unit
@pytest.mark.parametrize(
    "field_value",
    [
        'w%3dx=":443"',  # lowercase hex is not the canonical form
        'h%32=":443"',  # "2" is a token character and MUST NOT be encoded
        'x%2=":443"',  # truncated escape
        "h2=:443",  # alt-authority must be a quoted-string
        'h2="example.com"',  # no port
        'h2=":0"',  # port out of range
        'h2=":65536"',  # one above the 16-bit maximum
        'h2=":443";',  # dangling parameter separator
        'h2=":443"; ma',  # parameter without a value
        'h2="\x01:443"',  # control character in quoted-string
        'h2="\\\x01:443"',  # quoted-pair admits only HTAB, SP, VCHAR (RFC 9110 5.6.4)
        'h2=":443',  # unterminated quoted-string
        'h2=":443\\',  # quoted-pair with nothing after the backslash
        "Clear",  # the keyword is case-sensitive
        "",  # at least one element
        " , ,",  # empty elements only
        "," * 9 + 'h2=":443"',  # more empty elements than the parser tolerates
    ],
)
def test_alt_svc_rejects_malformed(field_value: str) -> None:
    with pytest.raises(AltSvcParseError):
        parse_alt_svc(field_value)


@pytest.mark.unit
def test_alt_svc_parameters_unknown_persist_and_ma_rules() -> None:
    alt = _single('h3=":443"; foo="bar;baz"; MA=60; persist=yes')
    assert alt == AltService(b"h3", "", 443, max_age=60, persist=False)
    quoted = _single('h3=":443"; ma="120"; persist="1"')
    assert (quoted.max_age, quoted.persist) == (120, True)
    huge = _single('h3=":443"; ma=99999999999999999999')
    assert huge.max_age == DELTA_SECONDS_CAP
    bad_ma = _single('h3=":443"; ma=-5')
    assert bad_ma.max_age == DEFAULT_MAX_AGE
    # quoted-string is unescaped before use (RFC 7838 Section 3, RFC 9110 Section 5.6.4).
    assert _single('h2="alt\\.example.net:\\443"') == AltService(b"h2", "alt.example.net", 443)


@pytest.mark.unit
def test_alt_svc_empty_list_elements_are_ignored() -> None:
    assert parse_alt_svc(' , h2=":443" ,, h3=":443",') == (
        AltService(b"h2", "", 443),
        AltService(b"h3", "", 443),
    )
    # The bound is inclusive: eight empty elements parse, nine are rejected above.
    assert parse_alt_svc("," * 8 + 'h2=":443"') == (AltService(b"h2", "", 443),)


@pytest.mark.unit
def test_clear_wins_even_when_mixed_with_alternatives() -> None:
    assert parse_alt_svc("clear") == AltSvcClear()
    assert parse_alt_svc('h2=":443", clear') == AltSvcClear()


# --- Alt-Svc cache (RFC 7838 Sections 2.2, 3.1, 5, 6) ------------------------------------

ALL = (b"h3", b"h2", b"http/1.1")


@pytest.mark.unit
def test_cache_freshness_counts_from_generation_using_age() -> None:
    # RFC 7838 Section 3.1 example: ma=60 with Age: 30 is fresh for 30 more seconds.
    cache = AltSvcCache()
    cache.observe_response(
        ORIGIN, status=200, alt_svc_lines=['h2=":8000"; ma=60'], received_at=1000.0, age=30
    )
    assert cache.candidates(ORIGIN, now=1029.9, supported=ALL) == (AltService(b"h2", "", 8000, 60),)
    assert cache.candidates(ORIGIN, now=1030.0, supported=ALL) == ()


@pytest.mark.unit
def test_cache_default_freshness_is_24_hours() -> None:
    cache = AltSvcCache()
    cache.observe_response(ORIGIN, status=200, alt_svc_lines=['h3=":443"'], received_at=0.0)
    assert len(cache.candidates(ORIGIN, now=86399.0, supported=ALL)) == 1
    assert cache.candidates(ORIGIN, now=86400.0, supported=ALL) == ()


@pytest.mark.unit
def test_cache_replace_keep_clear_and_ignore_421() -> None:
    cache = AltSvcCache()
    cache.observe_response(
        ORIGIN, status=200, alt_svc_lines=['h3=":443"', 'h2=":443"'], received_at=0.0
    )
    assert [a.alpn for a in cache.candidates(ORIGIN, now=1.0, supported=ALL)] == [b"h3", b"h2"]

    cache.observe_response(ORIGIN, status=200, alt_svc_lines=[], received_at=1.0)
    assert len(cache.candidates(ORIGIN, now=2.0, supported=ALL)) == 2  # absence keeps

    cache.observe_response(ORIGIN, status=421, alt_svc_lines=['h2=":9"'], received_at=2.0)
    assert len(cache.candidates(ORIGIN, now=3.0, supported=ALL)) == 2  # 421 ignored

    cache.observe_response(ORIGIN, status=200, alt_svc_lines=['h2=":8443"'], received_at=3.0)
    assert cache.candidates(ORIGIN, now=4.0, supported=ALL) == (AltService(b"h2", "", 8443),)

    cache.observe_response(ORIGIN, status=200, alt_svc_lines=['h2=":443";'], received_at=4.0)
    assert cache.candidates(ORIGIN, now=5.0, supported=ALL) == (AltService(b"h2", "", 8443),)

    cache.observe_response(ORIGIN, status=200, alt_svc_lines=["clear"], received_at=5.0)
    assert cache.candidates(ORIGIN, now=6.0, supported=ALL) == ()
    assert ORIGIN not in cache.entries


@pytest.mark.unit
def test_cache_filters_protocols_the_client_cannot_speak() -> None:
    cache = AltSvcCache()
    cache.observe_response(
        ORIGIN, status=200, alt_svc_lines=['h3=":443", h2=":443"'], received_at=0.0
    )
    # httpx 0.28.1 has no QUIC transport, so h3 alternatives are unusable to it.
    assert cache.candidates(ORIGIN, now=1.0, supported=[b"h2", b"http/1.1"]) == (
        AltService(b"h2", "", 443),
    )


@pytest.mark.unit
def test_network_change_keeps_only_persist_entries() -> None:
    other = Origin("https", "static.example.test", 443)
    cache = AltSvcCache()
    cache.observe_response(
        ORIGIN,
        status=200,
        alt_svc_lines=['h3=":443"; persist=1, h2="edge-local.example.test:443"'],
        received_at=0.0,
    )
    cache.observe_response(other, status=200, alt_svc_lines=['h3=":443"'], received_at=0.0)
    cache.network_changed()
    assert cache.candidates(ORIGIN, now=1.0, supported=ALL) == (
        AltService(b"h3", "", 443, persist=True),
    )
    assert other not in cache.entries


@pytest.mark.unit
def test_misdirected_421_removes_only_that_alternative() -> None:
    cache = AltSvcCache()
    cache.observe_response(
        ORIGIN, status=200, alt_svc_lines=['h3="a.example.test:443", h2=":443"'], received_at=0.0
    )
    bad = AltService(b"h3", "a.example.test", 443)
    cache.misdirected(ORIGIN, bad)
    assert cache.candidates(ORIGIN, now=1.0, supported=ALL) == (AltService(b"h2", "", 443),)
    cache.misdirected(ORIGIN, AltService(b"h2", "", 443))
    assert ORIGIN not in cache.entries


@pytest.mark.unit
def test_alt_used_names_the_alternative_actually_used() -> None:
    assert alt_used_value(ORIGIN, AltService(b"h2", "alternate.example.net", 443)) == (
        "alternate.example.net:443"
    )
    assert alt_used_value(ORIGIN, AltService(b"h3", "", 8443)) == f"{ORIGIN_HOST}:8443"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_alt_svc_reaches_client_cache_and_421_is_ignored() -> None:
    async def home(request: Request) -> PlainTextResponse:
        return PlainTextResponse(
            "ok", headers={"Alt-Svc": 'h3=":443"; ma=86400, h2=":443"; ma=86400'}
        )

    async def wrong(request: Request) -> PlainTextResponse:
        return PlainTextResponse("no", status_code=421, headers={"Alt-Svc": "clear"})

    app = Starlette(routes=[Route("/", home), Route("/wrong", wrong)])
    cache = AltSvcCache()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path, now in (("/", 0.0), ("/wrong", 1.0)):
            response = await client.get(path)
            cache.observe_response(
                ORIGIN,
                status=response.status_code,
                alt_svc_lines=response.headers.get_list("alt-svc"),
                received_at=now,
            )
    assert cache.candidates(ORIGIN, now=2.0, supported=ALL) == (
        AltService(b"h3", "", 443, 86400),
        AltService(b"h2", "", 443, 86400),
    )


@pytest.mark.socket
def test_hypercorn_0_18_0_auto_alt_svc_for_quic_bind(origin_cert: tuple[Path, Path]) -> None:
    config = HypercornConfig()
    config.certfile = str(origin_cert[0])
    config.keyfile = str(origin_cert[1])
    config.bind = ["127.0.0.1:0"]
    config.quic_bind = ["127.0.0.1:0"]
    sockets = config.create_sockets()
    try:
        quic_port = sockets.quic_sockets[0].getsockname()[1]
        headers = config.response_headers("h3")
    finally:
        for sock in (*sockets.secure_sockets, *sockets.insecure_sockets, *sockets.quic_sockets):
            sock.close()
    alt_svc = [value for name, value in headers if name == b"alt-svc"]
    assert alt_svc == [b'h3=":%d"; ma=3600' % quic_port]
    assert parse_alt_svc(alt_svc[0].decode()) == (AltService(b"h3", "", quic_port, 3600),)


# --- HTTP/2 ALTSVC frame (RFC 7838 Section 4) via h2 4.4.1 -------------------------------


@pytest.mark.unit
def test_h2_altsvc_on_stream_zero_wire_layout() -> None:
    origin = b"https://origin.example.test"
    value = b'h3=":443"; ma=3600'
    exchange = h2_altsvc_exchange(value, origin=origin)
    payload = len(origin).to_bytes(2, "big") + origin + value
    header = len(payload).to_bytes(3, "big") + b"\x0a" + b"\x00" + b"\x00\x00\x00\x00"
    assert exchange.wire == header + payload
    assert exchange.advertised == ((origin, value),)


@pytest.mark.unit
def test_h2_altsvc_on_a_stream_inherits_the_authority_and_has_empty_origin() -> None:
    exchange = h2_altsvc_exchange(b'h2=":8443"')
    assert exchange.wire[3] == 0x0A
    assert exchange.wire[5:9] == b"\x00\x00\x00\x01"
    assert exchange.wire[9:11] == b"\x00\x00"  # Origin-Len 0
    # h2 reports the stream's :authority, not an RFC 6454 serialized origin.
    assert exchange.advertised == ((ORIGIN_HOST.encode(), b'h2=":8443"'),)


@pytest.mark.unit
def test_h2_4_4_1_does_not_check_that_the_origin_is_authoritative() -> None:
    # RFC 7838 Section 4: an origin the client does not consider authoritative for this
    # connection MUST be ignored. h2 leaves that check to the application.
    exchange = h2_altsvc_exchange(b'h3=":443"', origin=b"https://evil.example")
    assert exchange.advertised == ((b"https://evil.example", b'h3=":443"'),)


@pytest.mark.unit
def test_h2_ignores_invalid_altsvc_frames() -> None:
    stream0_no_origin = AltSvcFrame(0, origin=b"", field=b'h2=":443"').serialize()
    assert h2_client_altsvc_events(h2_connected_client(), stream0_no_origin) == ()
    unknown_stream = AltSvcFrame(7, origin=b"", field=b'h2=":443"').serialize()
    assert h2_client_altsvc_events(h2_connected_client(), unknown_stream) == ()


@pytest.mark.unit
def test_h2_refuses_implicit_altsvc_after_response_headers() -> None:
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    client.send_headers(
        1,
        [(":method", "GET"), (":scheme", "https"), (":authority", ORIGIN_HOST), (":path", "/")],
        end_stream=True,
    )
    server.receive_data(client.data_to_send())
    server.send_headers(1, [(":status", "200")])
    with pytest.raises(h2.exceptions.ProtocolError, match="after sending response headers"):
        server.advertise_alternative_service(b'h2=":443"', stream_id=1)


# --- SVCB and HTTPS records (RFC 9460) ----------------------------------------------------

FOO_COM = b"\x03foo\x07example\x03com\x00"
FOO_ORG = b"\x03foo\x07example\x03org\x00"


@pytest.mark.unit
def test_rfc_9460_appendix_d_alias_and_simple_service_vectors() -> None:
    alias = decode_svcb_rdata(b"\x00\x00" + FOO_COM)
    assert (alias.alias_mode, alias.target) == (True, "foo.example.com.")
    root = decode_svcb_rdata(b"\x00\x01\x00")
    assert (root.priority, root.target, dict(root.params)) == (1, ".", {})
    port = decode_svcb_rdata(b"\x00\x10" + FOO_COM + b"\x00\x03\x00\x02\x00\x35")
    assert (port.priority, port.port) == (16, 53)
    generic = decode_svcb_rdata(b"\x00\x01" + FOO_COM + b"\x02\x9b\x00\x09hello\xd2qoo")
    assert dict(generic.params) == {667: b"hello\xd2qoo"}


@pytest.mark.unit
def test_rfc_9460_appendix_d_hint_vectors() -> None:
    two = decode_svcb_rdata(
        b"\x00\x01"
        + FOO_COM
        + b"\x00\x06\x00\x20"
        + bytes.fromhex("20010db8000000000000000000000001")
        + bytes.fromhex("20010db8000000000000000000530001")
    )
    assert two.ipv6hint == ("2001:db8::1", "2001:db8::53:1")
    embedded = decode_svcb_rdata(
        b"\x00\x01\x07example\x03com\x00\x00\x06\x00\x10"
        + bytes.fromhex("20010db80122034400000000c0000221")
    )
    assert embedded.ipv6hint == ("2001:db8:122:344::c000:221",)


@pytest.mark.unit
def test_rfc_9460_figure_9_mandatory_alpn_ipv4hint() -> None:
    rdata = (
        b"\x00\x10"
        + FOO_ORG
        + b"\x00\x00\x00\x04\x00\x01\x00\x04"
        + b"\x00\x01\x00\x09\x02h2\x05h3-19"
        + b"\x00\x04\x00\x04\xc0\x00\x02\x01"
    )
    record = decode_svcb_rdata(rdata)
    assert record.mandatory == (1, 4)
    assert record.alpn == (b"h2", b"h3-19")
    assert record.ipv4hint == ("192.0.2.1",)
    assert https_alpn_set(record) == (b"h2", b"h3-19", b"http/1.1")
    assert is_compatible(record, {0, 1, 4})
    assert not is_compatible(record, {0, 1})


@pytest.mark.unit
def test_rfc_9460_figure_10_alpn_with_comma_and_backslash() -> None:
    record = decode_svcb_rdata(b"\x00\x10" + FOO_ORG + b"\x00\x01\x00\x0c\x08f\\oo,bar\x02h2")
    assert record.alpn == (b"f\\oo,bar", b"h2")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rdata", "match"),
    [
        (b"\x00\x01", "shorter than"),
        (b"\x00\x01\x05ab", "label runs past"),
        (b"\x00\x01\x03abc", "TargetName runs past"),
        (b"\x00\x01\xc0\x0c", "compression pointer"),
        (
            b"\x00\x01"
            + b"\x3f"
            + b"a" * 63
            + b"\x3f"
            + b"b" * 63
            + b"\x3f"
            + b"c" * 63
            + b"\x3f"
            + b"d" * 63
            + b"\x00",
            "longer than 255",
        ),
        (b"\x00\x01\x00\x00\x03\x00\x02\x00\x35\x00\x01\x00\x03\x02h2", "strictly increasing"),
        (b"\x00\x01\x00\x00\x03\x00\x02\x00\x35\x00\x03\x00\x02\x00\x36", "strictly increasing"),
        (b"\x00\x01\x00\x00\x03", "within a SvcParam header"),
        (b"\x00\x01\x00\x00\x03\x00\x04\x00\x35", "within a SvcParamValue"),
        (b"\x00\x01\x00\x00\x01\x00\x04\x02h2X", "exactly fill"),
        (b"\x00\x01\x00\x00\x01\x00\x03\x00h2", "exactly fill"),
        (b"\x00\x01\x00\x00\x01\x00\x00", "at least one alpn-id"),
        (b"\x00\x01\x00\x00\x02\x00\x03abc", "must be empty"),
        (b"\x00\x01\x00\x00\x03\x00\x03\x00\x01\xbb", "exactly 2 octets"),
        (b"\x00\x01\x00\x00\x04\x00\x03\xc0\x00\x02", "non-zero multiple"),
        (b"\x00\x01\x00\x00\x06\x00\x00", "non-zero multiple"),
        (b"\x00\x01\x00\x00\x00\x00\x02\x00\x00", "MUST NOT list itself"),
        (b"\x00\x01\x00\x00\x00\x00\x00", "non-empty list"),
        (b"\x00\x01\x00\x00\x00\x00\x04\x00\x04\x00\x01", "strictly increasing"),
        (b"\x00\x01\x00\x00\x05\x00\x03\x00\x05\x01", "ECHConfigList"),
    ],
)
def test_svcb_malformed_rdata_is_rejected(rdata: bytes, match: str) -> None:
    with pytest.raises(SvcbFormatError, match=match):
        decode_svcb_rdata(rdata)


@pytest.mark.unit
def test_target_name_limit_is_255_octets_on_the_wire() -> None:
    # RFC 1035 Section 2.3.4: names are 255 octets or less, counting every length octet.
    def name(last_label: int) -> bytes:
        labels = [b"a" * 63, b"b" * 63, b"c" * 63, b"d" * last_label]
        return b"".join(bytes([len(label)]) + label for label in labels) + b"\x00"

    assert len(name(61)) == 255
    assert decode_svcb_rdata(b"\x00\x01" + name(61)).target.endswith(".")
    assert len(name(62)) == 256
    with pytest.raises(SvcbFormatError, match="longer than 255"):
        decode_svcb_rdata(b"\x00\x01" + name(62))


@pytest.mark.unit
def test_label_limit_is_63_octets_and_64_is_not_a_length() -> None:
    # RFC 1035 Section 2.3.4: labels are 63 octets or less; 0x40 and up in the length octet
    # is a reserved label type (0x80, 0x40) or a compression pointer (0xc0).
    ok = b"\x3f" + b"a" * 63 + b"\x00"
    assert decode_svcb_rdata(b"\x00\x01" + ok).target == "a" * 63 + "."
    with pytest.raises(SvcbFormatError, match="label longer than 63"):
        decode_svcb_rdata(b"\x00\x01" + b"\x40" + b"a" * 64 + b"\x00")


@pytest.mark.unit
def test_alt_svc_port_bounds_are_1_to_65535() -> None:
    assert _single('h2=":65535"').port == 65535
    assert _single('h2=":1"').port == 1


@pytest.mark.unit
def test_alias_mode_ignores_svcparams_and_ech_prefix_is_checked() -> None:
    alias = decode_svcb_rdata(b"\x00\x00" + FOO_COM + b"\x00\x03\x00\x03bad")
    assert (alias.alias_mode, alias.port) == (True, None)
    ech = decode_svcb_rdata(b"\x00\x01\x00\x00\x05\x00\x04\x00\x02\xfe\x0d")
    assert ech.params[5] == b"\x00\x02\xfe\x0d"


@pytest.mark.unit
def test_compatibility_mandatory_and_self_consistency() -> None:
    no_default = decode_svcb_rdata(b"\x00\x01\x00\x00\x01\x00\x03\x02h3\x00\x02\x00\x00")
    assert https_alpn_set(no_default) == (b"h3",)
    assert is_compatible(no_default, {1, 2})
    assert not is_compatible(no_default, {1})  # no-default-alpn is automatically mandatory

    lonely = decode_svcb_rdata(b"\x00\x01\x00\x00\x02\x00\x00")
    assert not is_compatible(lonely, {0, 1, 2, 3})  # no-default-alpn needs alpn

    missing = decode_svcb_rdata(b"\x00\x01\x00\x00\x00\x00\x02\x00\x7b")
    assert not is_compatible(missing, {0, 123})  # mandatory key 123 absent from the RR

    port = decode_svcb_rdata(b"\x00\x01\x00\x00\x03\x00\x02\x24\xfb")
    assert is_compatible(port, {3})
    assert not is_compatible(port, {1})  # port is automatically mandatory for HTTPS

    unknown = decode_svcb_rdata(b"\x00\x01\x00\x02\x9b\x00\x01x")
    assert is_compatible(unknown, set())  # unknown, non-mandatory keys are ignored


CLIENT_ALPN = {"quic": [b"h3"], "tcp": [b"h2", b"http/1.1"]}


def _https_rr(priority: int, target: bytes, params: bytes) -> bytes:
    return priority.to_bytes(2, "big") + target + params


@pytest.mark.unit
def test_plan_follows_rfc_9460_section_7_1_2_example() -> None:
    # SVCB ALPN set ["h3"] + default ["http/1.1"]; client supports h1, h2, h3.
    rrset = [_https_rr(1, b"\x00", b"\x00\x01\x00\x03\x02h3")]
    plan = plan_https_connections("example.com.", 443, rrset, CLIENT_ALPN)
    assert plan.alias_target is None
    assert plan.attempts == (
        ConnectionAttempt(1, "example.com.", 443, "quic", (b"h3",)),
        ConnectionAttempt(1, "example.com.", 443, "tcp", (b"h2", b"http/1.1")),
    )


@pytest.mark.unit
def test_plan_orders_by_priority_and_skips_incompatible_records() -> None:
    rrset = [
        _https_rr(2, b"\x04pool\x07example\x03net\x00", b"\x00\x01\x00\x03\x02h2"),
        _https_rr(
            1,
            b"\x04fast\x07example\x03net\x00",
            b"\x00\x01\x00\x03\x02h3\x00\x02\x00\x00\x00\x03\x00\x02\x01\xbb",
        ),
        _https_rr(3, b"\x00", b"\x00\x00\x00\x02\xff\x00\xff\x00\x00\x01x"),  # mandatory 65280
    ]
    plan = plan_https_connections("example.com.", 443, rrset, CLIENT_ALPN)
    assert plan.attempts == (
        ConnectionAttempt(1, "fast.example.net.", 443, "quic", (b"h3",)),
        ConnectionAttempt(2, "pool.example.net.", 443, "tcp", (b"h2", b"http/1.1")),
    )
    tcp_only = plan_https_connections("example.com.", 443, rrset, {"tcp": [b"h2", b"http/1.1"]})
    assert [a.host for a in tcp_only.attempts] == ["pool.example.net."]


@pytest.mark.unit
def test_plan_alias_mode_and_malformed_rrset() -> None:
    alias = plan_https_connections(
        "example.com.",
        443,
        [_https_rr(0, b"\x03svc\x07example\x03net\x00", b""), _https_rr(1, b"\x00", b"")],
        CLIENT_ALPN,
    )
    assert alias.alias_target == "svc.example.net."
    assert alias.attempts == ()
    with pytest.raises(SvcbFormatError):
        plan_https_connections(
            "example.com.",
            443,
            [_https_rr(1, b"\x00", b""), _https_rr(2, b"\x00", b"\x00\x03\x00\x01\x00")],
            CLIENT_ALPN,
        )
