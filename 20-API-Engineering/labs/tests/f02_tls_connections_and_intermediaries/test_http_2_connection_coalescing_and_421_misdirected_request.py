"""Lab: HTTP/2 connection coalescing across a wildcard certificate and 421 Misdirected Request."""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import TYPE_CHECKING

import h2.config
import h2.connection
import h2.events
import pytest
from httpcore import AsyncHTTPConnection
from httpcore import Origin as CoreOrigin
from hypercorn.config import Config as HypercornConfig
from hypercorn.utils import filter_pseudo_headers, valid_server_name
from hyperframe.frame import ExtensionFrame

from api_labs.f02_tls_connections_and_intermediaries.connection_coalescing import (
    NOW,
    ORIGIN_FRAME_TYPE,
    CoalescingPool,
    Edge,
    HostApp,
    Network,
    Origin,
    SniPolicy,
    TlsHandshakeError,
    TlsSession,
    VirtualHost,
    client_context,
    dns_id_matches,
    encode_origin_frame,
    issue,
    make_ca,
    parse_origin_entries,
    peek_sni,
    server_cert_valid_for,
    server_context,
    tls_handshake,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

EDGE_IP, OTHER_IP = "192.0.2.10", "192.0.2.20"
CA = make_ca(1001, "Lab Root CA")
SERVER = issue(CA, 2001, dns_names=["*.example.test", "example.test"])
CLIENT = issue(CA, 3001, dns_names=["client.example.test"], client=True)


@dataclass
class Lab:
    files: Path
    server_files: tuple[str, str]
    client_files: tuple[str, str]
    apps: dict[str, HostApp]

    def edge(
        self,
        *,
        policy: SniPolicy = SniPolicy.MATCH_SNI_FOR_MTLS,
        origin_frame: Sequence[str] | None = None,
    ) -> Edge:
        vhosts = {
            "www.example.test": VirtualHost(self.apps["www"]),
            "api.example.test": VirtualHost(self.apps["api"]),
            "admin.example.test": VirtualHost(self.apps["admin"], require_client_cert=True),
        }
        return Edge(
            self.server_files,
            vhosts,
            client_ca_pem=CA.cert_pem(),
            policy=policy,
            origin_frame=origin_frame,
        )

    def pool(
        self,
        edge: Edge,
        *,
        dns: Mapping[str, tuple[str, ...]] | None = None,
        require_dns_overlap: bool = True,
        honor_origin_frame: bool = True,
    ) -> CoalescingPool:
        names = dns or {
            "www.example.test": (EDGE_IP,),
            "api.example.test": (EDGE_IP,),
            "admin.example.test": (EDGE_IP,),
        }
        network = Network(dns=names, edges={(EDGE_IP, 443): edge, (OTHER_IP, 443): edge})
        return CoalescingPool(
            network,
            ca=CA.cert,
            client_identity_files=self.client_files,
            require_dns_overlap=require_dns_overlap,
            honor_origin_frame=honor_origin_frame,
        )


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    apps = {name: HostApp(name) for name in ("www", "api", "admin")}
    return Lab(tmp_path, SERVER.write(tmp_path, "server"), CLIENT.write(tmp_path, "client"), apps)


# --- Service identity --------------------------------------------------------------------------

MATCH_CASES = [
    ("*.example.test", "www.example.test", True),
    ("*.example.test", "WWW.Example.TEST", True),  # ASCII case-insensitive
    ("*.EXAMPLE.Test", "www.example.test", True),  # on both sides
    ("*.example.test", "example.test", False),  # the wildcard must match one label
    ("*.example.test", "a.b.example.test", False),  # and only one
    ("w*.example.test", "www.example.test", False),  # partial-label wildcard is invalid
    ("www.*.test", "www.example.test", False),  # wildcard not in the left-most label
    ("*.*.test", "a.example.test", False),  # more than one wildcard
    ("example.test", "example.test", True),
    ("example.test", "www.example.test", False),
    ("*.example.test", "b\u00fccher.example.test", False),  # U-label: compare A-labels only
    ("example.test", "example..test", False),  # empty label
]


@pytest.mark.unit
@pytest.mark.parametrize(("presented", "reference", "expected"), MATCH_CASES)
def test_wildcard_matches_exactly_one_whole_leftmost_label(
    presented: str, reference: str, expected: bool
) -> None:
    assert dns_id_matches(presented, reference) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("www.example.test", True),
        ("admin.example.test", True),
        ("example.test", True),
        ("a.b.example.test", False),
        ("example.org", False),
    ],
)
def test_matcher_agrees_with_the_cryptography_server_verifier(host: str, expected: bool) -> None:
    assert server_cert_valid_for(SERVER.cert, [CA.cert], host, at=NOW) is expected
    sans = ["*.example.test", "example.test"]
    assert any(dns_id_matches(san, host) for san in sans) is expected


@pytest.mark.unit
def test_verifier_rejects_a_covered_name_from_an_untrusted_issuer() -> None:
    rogue_ca = make_ca(9001, "Rogue CA")
    rogue = issue(rogue_ca, 9002, dns_names=["*.example.test"])
    assert not server_cert_valid_for(rogue.cert, [CA.cert], "www.example.test", at=NOW)


# --- TLS: SNI pre-read and per-host client-certificate policy ---------------------------------


def _first_flight(sni: str) -> bytes:
    ctx = client_context(CA.cert_pem())
    obj = ctx.wrap_bio(ssl.MemoryBIO(), out := ssl.MemoryBIO(), server_hostname=sni)
    with pytest.raises(ssl.SSLWantReadError):
        obj.do_handshake()
    return out.read()


@pytest.mark.unit
def test_peek_sni_reads_host_name_from_a_real_client_hello() -> None:
    hello = _first_flight("Admin.Example.Test")
    assert (hello[0], hello[5]) == (0x16, 0x01)  # handshake record carrying a ClientHello
    assert peek_sni(hello) == "admin.example.test"
    with pytest.raises(ValueError, match="truncated"):
        peek_sni(hello[:60])
    with pytest.raises(ValueError, match="not a TLS handshake record"):
        peek_sni(b"\x17" + hello[1:])


@pytest.mark.unit
def test_client_hello_without_sni_selects_the_default_context(lab: Lab) -> None:
    ctx = client_context(CA.cert_pem())
    ctx.check_hostname = False
    obj = ctx.wrap_bio(ssl.MemoryBIO(), out := ssl.MemoryBIO(), server_hostname=None)
    with pytest.raises(ssl.SSLWantReadError):
        obj.do_handshake()
    assert peek_sni(out.read()) is None
    edge = lab.edge()
    assert edge.context_for(None) is edge.context_for("unknown.example.test")
    assert edge.context_for(None) is not edge.context_for("admin.example.test")
    assert edge.context_for(None).verify_mode == ssl.CERT_NONE


@pytest.mark.unit
def test_client_rejects_a_server_cert_from_an_untrusted_issuer(lab: Lab) -> None:
    other_ca = make_ca(9101, "Other CA")
    with pytest.raises(TlsHandshakeError) as caught:
        tls_handshake(
            client_context(other_ca.cert_pem()), lab.edge().context_for, "www.example.test"
        )
    assert caught.value.client_reason == "CERTIFICATE_VERIFY_FAILED"
    assert caught.value.server_reason is None


@pytest.mark.unit
def test_cpython_sni_callback_context_switch_does_not_request_a_client_cert(lab: Lab) -> None:
    plain = server_context(lab.server_files, client_ca_pem=None)
    mtls = server_context(lab.server_files, client_ca_pem=CA.cert_pem())

    def switch(obj: ssl.SSLObject, name: str | None, ctx: ssl.SSLContext) -> None:
        if name == "admin.example.test":
            obj.context = mtls  # SSL_set_SSL_CTX: certificate swapped, verify mode is not

    plain.sni_callback = switch
    no_cert = client_context(CA.cert_pem())
    session = tls_handshake(no_cert, lambda _sni: plain, "admin.example.test")
    assert session.sni == "admin.example.test"
    assert not session.client_cert_verified  # no CertificateRequest was ever sent


@pytest.mark.unit
def test_context_chosen_from_preread_sni_enforces_the_client_cert(lab: Lab) -> None:
    edge = lab.edge()
    anonymous = client_context(CA.cert_pem())
    with pytest.raises(TlsHandshakeError) as caught:
        tls_handshake(anonymous, edge.context_for, "admin.example.test")
    assert caught.value.server_reason == "PEER_DID_NOT_RETURN_A_CERTIFICATE"
    assert caught.value.client_reason == "TLSV13_ALERT_CERTIFICATE_REQUIRED"
    with_cert = client_context(CA.cert_pem(), lab.client_files)
    admin = tls_handshake(with_cert, edge.context_for, "admin.example.test")
    www = tls_handshake(with_cert, edge.context_for, "www.example.test")
    assert (admin.alpn, admin.client_cert_verified) == ("h2", True)
    assert (www.alpn, www.client_cert_verified) == ("h2", False)  # never asked on www


# --- Coalescing --------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_second_host_coalesces_when_cert_and_dns_overlap(lab: Lab) -> None:
    pool = lab.pool(lab.edge())
    first = await pool.request("GET", "https://www.example.test/")
    second = await pool.request("GET", "https://api.example.test/v1")
    assert (first.status, second.status) == (200, 200)
    assert second.body == b"api: GET /v1\n"
    assert len(pool.connections) == 1
    assert (second.attempts[0].conn_id, second.attempts[0].coalesced) == (1, True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_coalescing_without_dns_overlap(lab: Lab) -> None:
    dns = {"www.example.test": (EDGE_IP,), "api.example.test": (OTHER_IP,)}
    pool = lab.pool(lab.edge(), dns=dns)
    await pool.request("GET", "https://www.example.test/")
    second = await pool.request("GET", "https://api.example.test/")
    assert second.status == 200
    assert (len(pool.connections), second.attempts[0].coalesced) == (2, False)
    relaxed = lab.pool(lab.edge(), dns=dns, require_dns_overlap=False)
    await relaxed.request("GET", "https://www.example.test/")
    await relaxed.request("GET", "https://api.example.test/")
    assert len(relaxed.connections) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_connection_negotiated_for_the_host_beats_a_coalescable_one(lab: Lab) -> None:
    dns: dict[str, tuple[str, ...]] = {
        "api.example.test": (EDGE_IP,),
        "www.example.test": (OTHER_IP,),
    }
    pool = lab.pool(lab.edge(), dns=dns)
    await pool.request("GET", "https://api.example.test/")  # connection 1, EDGE_IP
    await pool.request("GET", "https://www.example.test/")  # connection 2, OTHER_IP
    dns["www.example.test"] = (OTHER_IP, EDGE_IP)  # now connection 1 is coalescable too
    assert pool.can_coalesce(pool.connections[0], Origin.from_url("https://www.example.test/"))
    again = await pool.request("GET", "https://www.example.test/")
    assert [(a.conn_id, a.coalesced) for a in again.attempts] == [(2, False)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_coalescing_when_the_certificate_does_not_cover_the_host(lab: Lab) -> None:
    pool = lab.pool(
        lab.edge(), dns={"www.example.test": (EDGE_IP,), "a.b.example.test": (EDGE_IP,)}
    )
    conn = (await pool.request("GET", "https://www.example.test/"), pool.connections[0])[1]
    assert not pool.can_coalesce(conn, Origin.from_url("https://a.b.example.test/"))
    assert pool.can_coalesce(conn, Origin.from_url("https://www.example.test/"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_coalescing_across_ports(lab: Lab) -> None:
    pool = lab.pool(lab.edge())
    await pool.request("GET", "https://www.example.test/")
    assert not pool.can_coalesce(
        pool.connections[0], Origin.from_url("https://api.example.test:8443/")
    )


# --- 421 Misdirected Request ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mtls_host_on_a_coalesced_connection_gets_421_then_succeeds_fresh(lab: Lab) -> None:
    edge = lab.edge()
    pool = lab.pool(edge)
    await pool.request("GET", "https://www.example.test/")
    response = await pool.request("GET", "https://admin.example.test/keys")
    assert (response.status, response.body) == (200, b"admin: GET /keys\n")
    assert [(a.conn_id, a.coalesced, a.status) for a in response.attempts] == [
        (1, True, 421),
        (2, False, 200),
    ]
    assert pool.connections[1].tls.sni == "admin.example.test"
    assert pool.connections[1].tls.client_cert_verified
    assert lab.apps["admin"].calls == ["GET /keys 0"]  # the 421 never reached the app
    assert edge.log[-2:] == [
        ("www.example.test", "admin.example.test", 421),
        ("admin.example.test", "admin.example.test", 200),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_after_421_the_origin_goes_straight_to_its_own_connection(lab: Lab) -> None:
    pool = lab.pool(lab.edge())
    await pool.request("GET", "https://www.example.test/")
    await pool.request("GET", "https://admin.example.test/")
    again = await pool.request("GET", "https://admin.example.test/")
    assert [(a.conn_id, a.status) for a in again.attempts] == [(2, 200)]
    assert Origin.from_url("https://admin.example.test/") in pool.connections[0].misdirected
    www_again = await pool.request("GET", "https://www.example.test/")
    assert www_again.attempts[0].conn_id == 1  # the 421 did not poison other origins


@pytest.mark.unit
@pytest.mark.asyncio
async def test_421_retries_a_post_exactly_once_and_the_app_runs_once(lab: Lab) -> None:
    pool = lab.pool(lab.edge())
    await pool.request("GET", "https://www.example.test/")
    response = await pool.request("POST", "https://admin.example.test/rotate", body=b"x" * 10)
    assert [a.status for a in response.attempts] == [421, 200]
    assert lab.apps["admin"].calls == ["POST /rotate 10"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_421_on_a_dedicated_connection_is_returned_not_retried(lab: Lab) -> None:
    pool = lab.pool(lab.edge(), dns={"ghost.example.test": (EDGE_IP,)})
    response = await pool.request("GET", "https://ghost.example.test/")
    assert response.status == 421
    assert [(a.conn_id, a.coalesced) for a in response.attempts] == [(1, False)]
    assert len(pool.connections) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_421_retry_goes_to_a_fresh_connection_not_another_coalesced_one(lab: Lab) -> None:
    dns: dict[str, tuple[str, ...]] = {
        "www.example.test": (EDGE_IP,),
        "api.example.test": (OTHER_IP,),
        "admin.example.test": (EDGE_IP, OTHER_IP),
    }
    pool = lab.pool(lab.edge(), dns=dns)
    await pool.request("GET", "https://www.example.test/")  # connection 1, EDGE_IP
    await pool.request("GET", "https://api.example.test/")  # connection 2, OTHER_IP
    admin = Origin.from_url("https://admin.example.test/")
    assert pool.can_coalesce(pool.connections[1], admin)  # also a candidate
    response = await pool.request("GET", "https://admin.example.test/")
    assert [(a.conn_id, a.coalesced, a.status) for a in response.attempts] == [
        (1, True, 421),
        (3, False, 200),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("size", [20_000, 70_000])
async def test_bodies_larger_than_a_frame_and_a_window_round_trip(lab: Lab, size: int) -> None:
    pool = lab.pool(lab.edge())
    body = bytes(range(256)) * (size // 256) + b"x" * (size % 256)
    response = await pool.request("POST", "https://www.example.test/echo", body=body)
    assert (response.status, len(response.body)) == (200, size)
    assert response.body == body
    assert lab.apps["www"].calls == [f"POST /echo {size}"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_goaway_retires_the_connection(lab: Lab) -> None:
    pool = lab.pool(lab.edge())
    await pool.request("GET", "https://www.example.test/")
    conn = pool.connections[0]
    conn.server.h2.close_connection()
    pool.absorb(conn, conn.h2.receive_data(conn.server.h2.data_to_send()))
    assert conn.closed
    assert not pool.can_coalesce(conn, Origin.from_url("https://api.example.test/"))
    again = await pool.request("GET", "https://www.example.test/")
    assert [(a.conn_id, a.status) for a in again.attempts] == [(2, 200)]


@pytest.mark.unit
def test_ip_literal_origins_are_rejected() -> None:
    for url in ("https://192.0.2.10/", "https://[2001:db8::1]:8443/"):
        with pytest.raises(ValueError, match="IP-literal"):
            Origin.from_url(url)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authority_only_edge_serves_the_mtls_host_without_a_client_cert(lab: Lab) -> None:
    pool = lab.pool(lab.edge(policy=SniPolicy.AUTHORITY_ONLY))
    await pool.request("GET", "https://www.example.test/")
    leaked = await pool.request("GET", "https://admin.example.test/keys")
    assert (leaked.status, leaked.body) == (200, b"admin: GET /keys\n")
    assert [(a.conn_id, a.coalesced) for a in leaked.attempts] == [(1, True)]
    assert not pool.connections[0].tls.client_cert_verified  # mTLS bypassed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edge_lowercases_the_authority_before_the_sni_check(lab: Lab) -> None:
    edge = lab.edge()
    www_tls = TlsSession("www.example.test", "h2", SERVER.cert, client_cert_verified=False)
    status, payload = await edge.respond(www_tls, "GET", "WWW.Example.Test", "/", b"")
    assert (status, payload) == (200, b"www: GET /\n")
    status, payload = await edge.respond(www_tls, "GET", "ADMIN.Example.Test:443", "/", b"")
    assert (status, payload) == (421, b"connection was negotiated for another host\n")
    assert lab.apps["admin"].calls == []


# --- The ORIGIN frame ------------------------------------------------------------------------


@pytest.mark.unit
def test_origin_frame_wire_layout() -> None:
    frame = encode_origin_frame(["https://api.example.test", "https://x.test:8443"])
    entry1, entry2 = b"https://api.example.test", b"https://x.test:8443"
    body = len(entry1).to_bytes(2) + entry1 + len(entry2).to_bytes(2) + entry2
    assert frame == len(body).to_bytes(3) + b"\x0c\x00" + b"\x00\x00\x00\x00" + body
    assert len(body) == 2 + 24 + 2 + 19


@pytest.mark.unit
@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (b"https://API.example.test", Origin("https", "api.example.test", 443)),
        (b"https://api.example.test:8443", Origin("https", "api.example.test", 8443)),
        (b"https://*.example.test", None),  # no wildcards in ORIGIN
        (b"https://api.example.test/path", None),
        (b"api.example.test", None),
        (b"https://api.example.test:99999", None),
        (b"https://\xff.test", None),
        (b"https://192.0.2.10", None),  # IP literals are out of scope (no SNI)
    ],
)
def test_origin_entries_are_parsed_or_ignored(entry: bytes, expected: Origin | None) -> None:
    good = b"https://www.example.test"
    body = len(entry).to_bytes(2) + entry + len(good).to_bytes(2) + good + b"\x00\x30trunc"
    parsed = parse_origin_entries(body)
    assert parsed == [*([expected] if expected else []), Origin("https", "www.example.test", 443)]


@pytest.mark.unit
def test_h2_surfaces_origin_as_an_unknown_frame_event() -> None:
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    client.initiate_connection()
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    server.initiate_connection()
    events = client.receive_data(server.data_to_send() + encode_origin_frame(["https://a.test"]))
    unknown = [e for e in events if isinstance(e, h2.events.UnknownFrameReceived)]
    assert len(unknown) == 1
    frame = unknown[0].frame
    assert isinstance(frame, ExtensionFrame)
    assert (frame.type, frame.stream_id) == (ORIGIN_FRAME_TYPE, 0)
    assert parse_origin_entries(frame.body) == [Origin("https", "a.test", 443)]


@pytest.mark.unit
def test_hyperframe_extension_frame_built_in_code_serializes_with_length_zero() -> None:
    body = b"\x00\x0ehttps://a.test"
    frame = ExtensionFrame(ORIGIN_FRAME_TYPE, 0, flag_byte=0, body=body)
    assert frame.serialize() == b"\x00\x00\x00\x0c\x00\x00\x00\x00\x00" + body  # length 0
    assert encode_origin_frame(["https://a.test"])[:3] == len(body).to_bytes(3)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_origin_set_permits_coalescing_without_dns_and_excludes_unlisted(lab: Lab) -> None:
    dns = {
        "www.example.test": (EDGE_IP,),
        "api.example.test": (OTHER_IP,),
        "admin.example.test": (EDGE_IP,),
    }
    edge = lab.edge(origin_frame=["https://api.example.test"])
    pool = lab.pool(edge, dns=dns)
    await pool.request("GET", "https://www.example.test/")
    conn = pool.connections[0]
    assert conn.origin_set == {
        Origin("https", "www.example.test", 443),
        Origin("https", "api.example.test", 443),
    }
    assert pool.can_coalesce(conn, Origin.from_url("https://api.example.test/"))  # DNS skipped
    api = await pool.request("GET", "https://api.example.test/")
    assert [(a.conn_id, a.coalesced, a.status) for a in api.attempts] == [(1, True, 200)]
    assert not pool.can_coalesce(conn, Origin.from_url("https://admin.example.test/"))
    ignored = lab.pool(edge, dns=dns, honor_origin_frame=False)
    await ignored.request("GET", "https://www.example.test/")
    assert ignored.connections[0].origin_set is None
    assert not ignored.can_coalesce(
        ignored.connections[0], Origin.from_url("https://api.example.test/")
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_origin_frame_with_reserved_flag_is_ignored(lab: Lab) -> None:
    pool = lab.pool(lab.edge())
    await pool.request("GET", "https://www.example.test/")
    conn = pool.connections[0]
    for flags in (0x1, 0x8):  # reserved for backwards-incompatible changes: MUST ignore
        frame = encode_origin_frame(["https://api.example.test"], flags=flags)
        pool.absorb(conn, conn.h2.receive_data(frame))
    assert conn.origin_set is None
    pool.absorb(conn, conn.h2.receive_data(encode_origin_frame(["https://a.test"], flags=0x10)))
    assert conn.origin_set == {
        Origin("https", "www.example.test", 443),
        Origin("https", "a.test", 443),
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_origin_frame_on_a_request_stream_is_ignored(lab: Lab) -> None:
    pool = lab.pool(lab.edge())
    await pool.request("GET", "https://www.example.test/")
    conn = pool.connections[0]
    frame = encode_origin_frame(["https://api.example.test"], stream_id=1)
    pool.absorb(conn, conn.h2.receive_data(frame))
    assert conn.origin_set is None


@pytest.mark.unit
def test_origin_serialization_omits_only_the_default_port() -> None:
    assert Origin.from_url("https://WWW.Example.Test/x").serialize() == "https://www.example.test"
    assert Origin.from_url("https://www.example.test:443/").authority == "www.example.test"
    assert Origin.from_url("https://www.example.test:8443/").serialize() == (
        "https://www.example.test:8443"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_421_removes_the_origin_from_the_origin_set(lab: Lab) -> None:
    edge = lab.edge(origin_frame=["https://admin.example.test"])
    pool = lab.pool(edge)
    await pool.request("GET", "https://www.example.test/")
    admin = Origin.from_url("https://admin.example.test/")
    origin_set = pool.connections[0].origin_set
    assert origin_set is not None
    assert admin in origin_set
    response = await pool.request("GET", "https://admin.example.test/")
    assert [a.status for a in response.attempts] == [421, 200]
    assert admin not in origin_set


# --- What the Python stack does --------------------------------------------------------------


@pytest.mark.unit
def test_httpcore_never_coalesces_across_hosts() -> None:
    www = CoreOrigin(b"https", b"www.example.test", 443)
    conn = AsyncHTTPConnection(origin=www, http2=True)
    assert conn.can_handle_request(CoreOrigin(b"https", b"www.example.test", 443))
    assert not conn.can_handle_request(CoreOrigin(b"https", b"api.example.test", 443))


@pytest.mark.unit
def test_hypercorn_server_names_is_an_exact_match_on_the_authority() -> None:
    config = HypercornConfig()
    config.server_names = ["www.example.test"]

    @dataclass
    class _Request:
        headers: list[tuple[bytes, bytes]]

    def request(authority: bytes) -> _Request:
        return _Request(filter_pseudo_headers([(b":method", b"GET"), (b":authority", authority)]))

    assert valid_server_name(config, request(b"www.example.test"))  # type: ignore[arg-type]
    assert not valid_server_name(config, request(b"api.example.test"))  # type: ignore[arg-type]
    assert not valid_server_name(config, request(b"www.example.test:443"))  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pool_refuses_cleartext_and_unresolvable_origins(lab: Lab) -> None:
    pool = lab.pool(lab.edge())
    with pytest.raises(ValueError, match="h2 over TLS"):
        await pool.request("GET", "http://www.example.test/")
    with pytest.raises(LookupError, match="no address"):
        await pool.request("GET", "https://nowhere.example.test/")
    with pytest.raises(ValueError, match="not an http"):
        Origin.from_url("ftp://www.example.test/")
