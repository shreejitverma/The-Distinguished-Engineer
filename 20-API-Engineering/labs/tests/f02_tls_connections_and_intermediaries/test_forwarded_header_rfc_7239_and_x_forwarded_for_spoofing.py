"""Labs: RFC 7239 Forwarded parsing, X-Forwarded-For spoofing, and right-to-left hop peeling."""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, Any, cast

import h2.config
import h2.connection
import h2.events
import h2.exceptions
import h11
import httpx
import pytest
from granian.utils import proxies as granian_proxies
from hypercorn.middleware import ProxyFixMiddleware
from slowapi.util import get_ipaddr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from werkzeug.middleware.proxy_fix import ProxyFix

from api_labs.f02_tls_connections_and_intermediaries.forwarded_identity import (
    MAX_HOPS,
    ForwardingHeaderError,
    TrustedProxies,
    client_from_forwarded,
    client_from_xff,
    naive_leftmost_xff,
    parse_forwarded,
    parse_node,
    parse_x_forwarded_for,
    proxy_append_xff,
    resolve_client,
    xff_entry_ip,
    xff_to_forwarded,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp

IP = ipaddress.ip_address
EDGE = "10.0.0.2"
TRUSTED = TrustedProxies.from_cidrs("10.0.0.0/8", "fd00::/8")
ATTACKER = "203.0.113.50"
GRANIAN: Any = granian_proxies


def _app(key: Callable[[Request], str | None], limit: int = 3) -> Starlette:
    """A fixed-window limiter: `limit` requests per key, then `429`; `400` if no key resolves."""
    counts: dict[str, int] = {}

    async def endpoint(request: Request) -> PlainTextResponse:
        ident = key(request)
        if ident is None:
            return PlainTextResponse("unresolvable client", status_code=400)
        counts[ident] = counts.get(ident, 0) + 1
        status = 429 if counts[ident] > limit else 200
        return PlainTextResponse(ident, status_code=status)

    return Starlette(routes=[Route("/login", endpoint, methods=["POST"])])


def _peer(request: Request) -> str:
    assert request.client is not None
    return request.client.host


def _naive_key(request: Request) -> str:
    return naive_leftmost_xff(request.scope["headers"], _peer(request))


def _right_to_left_key(request: Request) -> str | None:
    resolved = client_from_xff(IP(_peer(request)), request.scope["headers"], TRUSTED)
    return None if resolved.client is None else str(resolved.client)


def _via_edge(spoofed: str) -> list[tuple[bytes, bytes]]:
    """The attacker's spoofed header after the edge proxy appended the attacker's real address."""
    return proxy_append_xff([(b"x-forwarded-for", spoofed.encode())], ATTACKER)


async def _statuses(app: ASGIApp, peer: str, spoofs: list[str]) -> list[int]:
    transport = httpx.ASGITransport(app=cast("Any", app), client=(peer, 443))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return [
            (await client.post("/login", headers=_via_edge(spoof))).status_code for spoof in spoofs
        ]


def _scope(headers: list[tuple[bytes, bytes]], peer: str = EDGE) -> dict[str, Any]:
    return {"type": "http", "client": (peer, 443), "headers": headers, "scheme": "http"}


async def _client_after(middleware: Any, scope: dict[str, Any]) -> tuple[str, int]:
    seen: list[tuple[str, int]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append(scope["client"])

    await middleware(inner)(scope, None, None)
    return seen[0]


SPOOFS = [f"198.18.0.{n}" for n in range(10)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_leftmost_xff_rate_limit_is_bypassed_by_rotating_spoofed_entries() -> None:
    statuses = await _statuses(_app(_naive_key), EDGE, SPOOFS)
    assert statuses == [200] * 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_right_to_left_rate_limit_holds_against_rotating_spoofed_entries() -> None:
    statuses = await _statuses(_app(_right_to_left_key), EDGE, SPOOFS)
    assert statuses == [200, 200, 200] + [429] * 7


@pytest.mark.unit
def test_peeling_skips_trusted_hops_right_to_left_and_ignores_everything_left() -> None:
    headers = [(b"x-forwarded-for", b"6.6.6.6, 198.51.100.7, 10.0.0.5")]
    resolved = client_from_xff(IP(EDGE), headers, TRUSTED)
    assert resolved.client == IP("198.51.100.7")
    assert resolved.peeled == 2
    assert resolved.reason == "first-untrusted-hop"


@pytest.mark.unit
def test_untrusted_peer_means_headers_are_ignored() -> None:
    headers = [(b"x-forwarded-for", b"10.0.0.9, 198.51.100.7")]
    resolved = client_from_xff(IP(ATTACKER), headers, TRUSTED)
    assert (resolved.client, resolved.reason) == (IP(ATTACKER), "peer-untrusted")


@pytest.mark.unit
def test_all_trusted_chain_returns_leftmost_hop() -> None:
    resolved = resolve_client(IP(EDGE), [IP("10.1.1.1"), IP("10.0.0.5")], TRUSTED)
    assert (resolved.client, resolved.reason) == (IP("10.1.1.1"), "all-trusted")
    assert resolve_client(IP(EDGE), [], TRUSTED).client == IP(EDGE)


@pytest.mark.unit
def test_unparseable_hop_fails_closed() -> None:
    headers = [(b"x-forwarded-for", b"198.51.100.7, not-an-ip, 10.0.0.5")]
    resolved = client_from_xff(IP(EDGE), headers, TRUSTED)
    assert (resolved.client, resolved.peeled, resolved.reason) == (None, 2, "invalid-hop")


@pytest.mark.unit
def test_ipv6_trusted_range_and_bracketed_port_entries() -> None:
    headers = [(b"x-forwarded-for", b"[2001:db8:cafe::17]:4711, fd00::5")]
    resolved = client_from_xff(IP("fd00::2"), headers, TRUSTED)
    assert resolved.client == IP("2001:db8:cafe::17")


@pytest.mark.unit
def test_multiple_xff_field_lines_are_joined_in_order() -> None:
    headers = [(b"x-forwarded-for", b"6.6.6.6"), (b"x-forwarded-for", b"198.51.100.7")]
    assert parse_x_forwarded_for(headers) == ["6.6.6.6", "198.51.100.7"]
    assert client_from_xff(IP(EDGE), headers, TRUSTED).client == IP("198.51.100.7")


@pytest.mark.unit
def test_hop_count_is_bounded() -> None:
    assert MAX_HOPS == 32  # the figure the note publishes
    value = ", ".join(["198.51.100.7"] * (MAX_HOPS + 1)).encode()
    with pytest.raises(ForwardingHeaderError, match="MAX_HOPS"):
        parse_x_forwarded_for([(b"x-forwarded-for", value)])
    assert len(parse_x_forwarded_for([(b"x-forwarded-for", value[:-14])])) == MAX_HOPS


@pytest.mark.unit
def test_rfc7239_section_7_1_list_forms_are_equivalent() -> None:
    one = [(b"forwarded", b'for=192.0.2.43,for="[2001:db8:cafe::17]",for=unknown')]
    spaced = [(b"forwarded", b'for=192.0.2.43, for="[2001:db8:cafe::17]", for=unknown')]
    split = [
        (b"Forwarded", b"for=192.0.2.43"),
        (b"Forwarded", b'for="[2001:db8:cafe::17]", for=unknown'),
    ]
    expected = [{"for": "192.0.2.43"}, {"for": "[2001:db8:cafe::17]"}, {"for": "unknown"}]
    assert parse_forwarded(one) == parse_forwarded(spaced) == parse_forwarded(split) == expected


@pytest.mark.unit
def test_rfc7239_section_7_5_example_resolves_like_its_xff_translation() -> None:
    forwarded = [
        (
            b"forwarded",
            b"for=192.0.2.43, for=198.51.100.17;by=203.0.113.60;proto=http;host=example.com",
        )
    ]
    assert parse_forwarded(forwarded)[1] == {
        "for": "198.51.100.17",
        "by": "203.0.113.60",
        "proto": "http",
        "host": "example.com",
    }
    trusted = TrustedProxies.from_cidrs("198.51.100.17/32", "203.0.113.60/32")
    xff = [(b"x-forwarded-for", b"192.0.2.43, 198.51.100.17")]
    from_forwarded = client_from_forwarded(IP("203.0.113.60"), forwarded, trusted)
    assert from_forwarded == client_from_xff(IP("203.0.113.60"), xff, trusted)
    assert from_forwarded.client == IP("192.0.2.43")


@pytest.mark.unit
def test_comma_inside_quoted_string_does_not_split_elements() -> None:
    headers = [(b"forwarded", b'for=192.0.2.1;ext="a,b", for=192.0.2.2')]
    assert parse_forwarded(headers) == [{"for": "192.0.2.1", "ext": "a,b"}, {"for": "192.0.2.2"}]


@pytest.mark.unit
def test_quoted_pair_is_unescaped_and_malformed_quoting_rejected() -> None:
    assert parse_forwarded([(b"forwarded", rb'ext="a\"b\\c"')]) == [{"ext": 'a"b\\c'}]
    for bad in (b'for="192.0.2.1', b'ext="a"b"', b"for=", b"=192.0.2.1", b"for=[::1]"):
        with pytest.raises(ForwardingHeaderError):
            parse_forwarded([(b"forwarded", bad)])


@pytest.mark.unit
def test_parameter_names_are_case_insensitive_and_unique_per_element() -> None:
    assert parse_forwarded([(b"forwarded", b'For="[2001:db8:cafe::17]:4711"')]) == [
        {"for": "[2001:db8:cafe::17]:4711"}
    ]
    with pytest.raises(ForwardingHeaderError, match="repeated"):
        parse_forwarded([(b"forwarded", b"for=192.0.2.1;FOR=192.0.2.2")])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "kind", "ip", "port"),
    [
        ("192.0.2.43:47011", "ip", "192.0.2.43", 47011),
        ("[2001:db8:cafe::17]:47011", "ip", "2001:db8:cafe::17", 47011),
        ("[2001:db8:cafe::17]", "ip", "2001:db8:cafe::17", None),
        ("unknown", "unknown", None, None),
        ("_SEVKISEK:_port-1", "obfuscated", None, None),
    ],
)
def test_node_identifier_forms(text: str, kind: str, ip: str | None, port: int | None) -> None:
    node = parse_node(text)
    assert (node.kind, node.ip, node.port) == (kind, None if ip is None else IP(ip), port)


@pytest.mark.unit
@pytest.mark.parametrize(
    "text", ["2001:db8::1", "[2001:db8::1", "192.0.2.1:", "192.0.2.1:123456", "_", "host"]
)
def test_invalid_node_identifiers_are_rejected(text: str) -> None:
    with pytest.raises(ForwardingHeaderError):
        parse_node(text)


@pytest.mark.unit
def test_xff_translation_matches_rfc7239_section_7_4() -> None:
    entries = parse_x_forwarded_for([(b"x-forwarded-for", b"192.0.2.43, 2001:db8:cafe::17")])
    assert xff_to_forwarded(entries) == 'for=192.0.2.43, for="[2001:db8:cafe::17]"'
    with pytest.raises(ForwardingHeaderError):
        xff_to_forwarded(["unknown"])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("entry", "ip"),
    [
        ("192.0.2.1", "192.0.2.1"),
        ("192.0.2.1:8080", "192.0.2.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("unknown", None),
    ],
)
def test_xff_entry_forms(entry: str, ip: str | None) -> None:
    assert xff_entry_ip(entry) == (None if ip is None else IP(ip))


@pytest.mark.unit
def test_proxy_append_matches_nginx_proxy_add_x_forwarded_for() -> None:
    assert proxy_append_xff([], ATTACKER) == [(b"x-forwarded-for", ATTACKER.encode())]
    joined = proxy_append_xff([(b"X-Forwarded-For", b"6.6.6.6"), (b"host", b"api")], ATTACKER)
    assert joined == [(b"host", b"api"), (b"x-forwarded-for", b"6.6.6.6, 203.0.113.50")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uvicorn_proxy_headers_cidr_holds_but_wildcard_trusts_leftmost() -> None:
    def build(trusted: str) -> ASGIApp:
        return cast("ASGIApp", ProxyHeadersMiddleware(cast("Any", _app(_peer)), trusted))

    assert await _statuses(build("10.0.0.0/8"), EDGE, SPOOFS) == [200] * 3 + [429] * 7
    assert await _statuses(build("*"), EDGE, SPOOFS) == [200] * 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uvicorn_ignores_forwarded_and_untrusted_peers() -> None:
    def mw(app: Any) -> Any:
        return ProxyHeadersMiddleware(app, "10.0.0.0/8")

    assert await _client_after(mw, _scope([(b"forwarded", b"for=198.51.100.7")])) == (EDGE, 443)
    spoof = [(b"x-forwarded-for", b"198.51.100.7")]
    assert await _client_after(mw, _scope(spoof, ATTACKER)) == (ATTACKER, 443)
    assert await _client_after(mw, _scope(spoof)) == ("198.51.100.7", 0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uvicorn_strips_the_port_and_wildcard_returns_the_leftmost_entry() -> None:
    def cidr(app: Any) -> Any:
        return ProxyHeadersMiddleware(app, "10.0.0.0/8")

    def wildcard(app: Any) -> Any:
        return ProxyHeadersMiddleware(app, "*")

    spoof = [(b"x-forwarded-for", b"6.6.6.6, 198.51.100.7")]
    assert await _client_after(wildcard, _scope(spoof)) == ("6.6.6.6", 0)
    assert await _client_after(cidr, _scope(spoof)) == ("198.51.100.7", 0)
    ported = [(b"x-forwarded-for", b"6.6.6.6, 198.51.100.7:5555")]
    assert await _client_after(cidr, _scope(ported)) == ("198.51.100.7", 5555)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_granian_keeps_the_port_in_the_host_and_wildcard_trusts_leftmost() -> None:
    def cidr(app: Any) -> Any:
        return GRANIAN.wrap_asgi_with_proxy_headers(app, "10.0.0.0/8")

    def wildcard(app: Any) -> Any:
        return GRANIAN.wrap_asgi_with_proxy_headers(app, "*")

    ported = _scope([(b"x-forwarded-for", b"6.6.6.6, 198.51.100.7:5555")])
    assert await _client_after(cidr, ported) == ("198.51.100.7:5555", 0)
    spoof = _scope([(b"x-forwarded-for", b"6.6.6.6, 198.51.100.7")])
    assert await _client_after(wildcard, spoof) == ("6.6.6.6", 0)
    ours = client_from_xff(IP(EDGE), ported["headers"], TRUSTED)
    assert ours.client == IP("198.51.100.7")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hypercorn_proxy_fix_trusts_hop_count_not_peer_address() -> None:
    def legacy(app: Any) -> Any:
        return ProxyFixMiddleware(app, mode="legacy", trusted_hops=1)

    direct = _scope([(b"x-forwarded-for", b"198.51.100.7")], peer=ATTACKER)
    assert await _client_after(legacy, direct) == ("198.51.100.7", 0)


@pytest.mark.unit
def test_werkzeug_proxy_fix_trusts_hop_count_not_peer_address() -> None:
    seen: list[str] = []

    def wsgi(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        seen.append(environ["REMOTE_ADDR"])
        return []

    direct = {"REMOTE_ADDR": ATTACKER, "HTTP_X_FORWARDED_FOR": "6.6.6.6, 198.51.100.7"}
    ProxyFix(cast("Any", wsgi))(direct, cast("Any", None))
    assert seen == ["198.51.100.7"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hypercorn_modern_mode_misreads_rfc7239_section_4_examples() -> None:
    def modern(app: Any) -> Any:
        return ProxyFixMiddleware(app, mode="modern", trusted_hops=1)

    capital = _scope([(b"forwarded", b'For="[2001:db8:cafe::17]:4711"')])
    assert await _client_after(modern, capital) == (EDGE, 443)
    lower = _scope([(b"forwarded", b'for="[2001:db8:cafe::17]:4711"')])
    assert await _client_after(modern, lower) == ('"[2001:db8:cafe::17]:4711"', 0)
    ours = client_from_forwarded(IP(EDGE), capital["headers"], TRUSTED)
    assert ours.client == IP("2001:db8:cafe::17")


@pytest.mark.unit
def test_starlette_headers_getitem_returns_only_the_first_field_line() -> None:
    lines = [(b"x-forwarded-for", b"6.6.6.6"), (b"x-forwarded-for", ATTACKER.encode())]
    request = Request(_scope(lines))
    assert request.headers["x-forwarded-for"].split(",")[-1].strip() == "6.6.6.6"
    assert request.headers.getlist("x-forwarded-for") == ["6.6.6.6", ATTACKER]


@pytest.mark.unit
def test_slowapi_get_ipaddr_reads_an_underscore_header_the_client_can_send() -> None:
    real = Request(_scope([(b"x-forwarded-for", b"198.51.100.7")], peer=ATTACKER))
    assert get_ipaddr(real) == ATTACKER
    forged = Request(_scope([(b"x_forwarded_for", b"6.6.6.6")], peer=ATTACKER))
    assert get_ipaddr(forged) == "6.6.6.6"


@pytest.mark.unit
def test_h11_delivers_underscore_field_names_because_underscore_is_a_tchar() -> None:
    server = h11.Connection(h11.SERVER)
    server.receive_data(b"GET / HTTP/1.1\r\nHost: api\r\nX_Forwarded_For: 6.6.6.6\r\n\r\n")
    event = server.next_event()
    assert isinstance(event, h11.Request)
    assert (b"x_forwarded_for", b"6.6.6.6") in list(event.headers)


def _h2_request_headers(name: bytes) -> list[tuple[bytes, bytes]]:
    return [
        (b":method", b"GET"),
        (b":path", b"/"),
        (b":scheme", b"https"),
        (b":authority", b"api"),
        (name, b"6.6.6.6"),
    ]


def _h2_server_receives(name: bytes) -> list[h2.events.Event]:
    """Send one `GET` whose only extra field is `name`, from a client that skips its own checks."""
    config = h2.config.H2Configuration(
        client_side=True, validate_outbound_headers=False, normalize_outbound_headers=False
    )
    client = h2.connection.H2Connection(config=config)
    client.initiate_connection()
    server = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False))
    server.initiate_connection()
    server.clear_outbound_data_buffer()
    server.receive_data(client.data_to_send())
    client.send_headers(1, _h2_request_headers(name), end_stream=True)
    return server.receive_data(client.data_to_send())


@pytest.mark.unit
def test_h2_rejects_uppercase_field_names_but_delivers_underscore_names() -> None:
    underscore = _h2_server_receives(b"x_forwarded_for")
    request = next(e for e in underscore if isinstance(e, h2.events.RequestReceived))
    assert request.headers is not None
    assert (b"x_forwarded_for", b"6.6.6.6") in request.headers
    with pytest.raises(h2.exceptions.ProtocolError, match="uppercase header name"):
        _h2_server_receives(b"X-Forwarded-For")
