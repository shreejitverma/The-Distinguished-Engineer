"""TLS 1.3 session tickets, 0-RTT replay, and HTTP 425 Too Early (RFC 9846, RFC 8470).

Four layers, each runnable without a network:

1. Wire: the NewSessionTicket handshake message and its `early_data` extension
   (RFC 9846 Section 4.7.1), plus ticket-age obfuscation and the freshness check
   (RFC 9846 Sections 4.3.11.1 and 8.3).
2. Transport: a real TLS 1.3 0-RTT exchange driven in memory through aioquic, so a
   captured first flight can be replayed byte for byte into a second server instance.
3. Origin: an ASGI middleware that answers `425 Too Early` when a request that is not
   replay-safe arrives marked with `Early-Data` (RFC 8470 Sections 3 and 5).
4. Hops: the gateway and user-agent rules for adding `Early-Data` and handling 425
   (RFC 8470 Sections 4, 5.1, and 5.2).
"""

from __future__ import annotations

import datetime
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from aioquic.quic import events
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

if TYPE_CHECKING:
    from collections.abc import Iterable

    import httpx
    from aioquic.tls import SessionTicket
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

# ---------------------------------------------------------------------------
# 1. Wire format (RFC 9846 Sections 4, 4.3.10, 4.7.1)
# ---------------------------------------------------------------------------

HANDSHAKE_NEW_SESSION_TICKET = 4
EXTENSION_EARLY_DATA = 42
MAX_TICKET_LIFETIME_SECONDS = 604_800  # 7 days, Section 4.7.1
QUIC_MAX_EARLY_DATA_SENTINEL = 0xFFFF_FFFF  # RFC 9001 Section 4.6.1
_U32 = 1 << 32


class TicketFormatError(ValueError):
    """A NewSessionTicket violates a length or value bound of RFC 9846 Section 4.7.1."""


@dataclass(frozen=True, slots=True)
class NewSessionTicket:
    """The NewSessionTicket body; `max_early_data_size` is None when 0-RTT is not offered."""

    ticket_lifetime: int
    ticket_age_add: int
    ticket_nonce: bytes
    ticket: bytes
    max_early_data_size: int | None = None


def encode_new_session_ticket(nst: NewSessionTicket) -> bytes:
    """Serialize as Handshake(msg_type=4, uint24 length, NewSessionTicket)."""
    if not 0 <= nst.ticket_lifetime <= MAX_TICKET_LIFETIME_SECONDS:
        raise TicketFormatError("ticket_lifetime must be <= 604800 seconds")
    if not 0 <= nst.ticket_age_add < _U32:
        raise TicketFormatError("ticket_age_add is a uint32")
    if len(nst.ticket_nonce) > 255:
        raise TicketFormatError("ticket_nonce<0..255>")
    if not 1 <= len(nst.ticket) <= 0xFFFF:
        raise TicketFormatError("ticket<1..2^16-1>")
    extensions = b""
    if nst.max_early_data_size is not None:
        extensions = struct.pack("!HHI", EXTENSION_EARLY_DATA, 4, nst.max_early_data_size)
    body = (
        struct.pack("!IIB", nst.ticket_lifetime, nst.ticket_age_add, len(nst.ticket_nonce))
        + nst.ticket_nonce
        + struct.pack("!H", len(nst.ticket))
        + nst.ticket
        + struct.pack("!H", len(extensions))
        + extensions
    )
    return bytes([HANDSHAKE_NEW_SESSION_TICKET]) + len(body).to_bytes(3, "big") + body


def decode_new_session_ticket(data: bytes) -> NewSessionTicket:
    """Parse one NewSessionTicket handshake message; unknown extensions are skipped."""
    if len(data) < 4 or data[0] != HANDSHAKE_NEW_SESSION_TICKET:
        raise TicketFormatError("not a new_session_ticket(4) handshake message")
    if int.from_bytes(data[1:4], "big") != len(data) - 4:
        raise TicketFormatError("handshake length does not match the message")
    try:
        lifetime, age_add, nonce_len = struct.unpack_from("!IIB", data, 4)
        pos = 13
        nonce = data[pos : pos + nonce_len]
        pos += nonce_len
        (ticket_len,) = struct.unpack_from("!H", data, pos)
        ticket = data[pos + 2 : pos + 2 + ticket_len]
        pos += 2 + ticket_len
        (ext_len,) = struct.unpack_from("!H", data, pos)
        pos += 2
        end = pos + ext_len
        max_early: int | None = None
        while pos < end:
            ext_type, length = struct.unpack_from("!HH", data, pos)
            if ext_type == EXTENSION_EARLY_DATA:
                (max_early,) = struct.unpack_from("!I", data, pos + 4)
            pos += 4 + length  # Section 4.7.1: clients MUST ignore unrecognized extensions
    except struct.error as exc:
        raise TicketFormatError("truncated NewSessionTicket") from exc
    if pos != len(data) or len(nonce) != nonce_len or len(ticket) != ticket_len or not ticket:
        raise TicketFormatError("NewSessionTicket length fields are inconsistent")
    if lifetime > MAX_TICKET_LIFETIME_SECONDS:
        raise TicketFormatError("ticket_lifetime must be <= 604800 seconds")
    return NewSessionTicket(lifetime, age_add, nonce, ticket, max_early)


def obfuscate_ticket_age(age_ms: int, ticket_age_add: int) -> int:
    """Client side of Section 4.3.11.1: (age in ms + ticket_age_add) mod 2^32."""
    return (age_ms + ticket_age_add) % _U32


def client_ticket_age(obfuscated_ticket_age: int, ticket_age_add: int) -> int:
    """Server side of Section 4.3.10: obfuscated_ticket_age - ticket_age_add mod 2^32."""
    return (obfuscated_ticket_age - ticket_age_add) % _U32


def is_fresh_client_hello(
    *,
    adjusted_creation_ms: int,
    obfuscated_ticket_age: int,
    ticket_age_add: int,
    now_ms: int,
    window_ms: int,
) -> bool:
    """Section 8.3: accept 0-RTT only if expected_arrival_time is within window_ms of now."""
    expected_arrival_ms = adjusted_creation_ms + client_ticket_age(
        obfuscated_ticket_age, ticket_age_add
    )
    return abs(now_ms - expected_arrival_ms) <= window_ms


# ---------------------------------------------------------------------------
# 2. A real 0-RTT exchange and replay, in memory, through aioquic
# ---------------------------------------------------------------------------

CLIENT_ADDR = ("192.0.2.10", 50_000)
SERVER_ADDR = ("198.51.100.1", 443)
_START = 100.0  # aioquic treats now=0.0 as "no time yet"; start the lab clock later


@dataclass(frozen=True, slots=True)
class ServerIdentity:
    """A self-signed certificate and key shared by every instance of one origin."""

    server_name: str
    certificate: x509.Certificate
    private_key: ec.EllipticCurvePrivateKey


def make_identity(server_name: str = "api.test") -> ServerIdentity:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, server_name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(server_name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return ServerIdentity(server_name, cert, key)


@dataclass
class TicketStore:
    """Server-side ticket database; `single_use=True` is RFC 9846 Section 8.1."""

    single_use: bool
    tickets: dict[bytes, SessionTicket] = field(default_factory=dict[bytes, "SessionTicket"])

    def add(self, ticket: SessionTicket) -> None:
        self.tickets[ticket.ticket] = ticket

    def fetch(self, label: bytes) -> SessionTicket | None:
        if self.single_use:
            return self.tickets.pop(label, None)
        return self.tickets.get(label)


@dataclass(frozen=True, slots=True)
class ServerView:
    """What one server instance did with a first flight."""

    early_stream_data: tuple[bytes, ...]  # delivered before HandshakeCompleted
    one_rtt_stream_data: tuple[bytes, ...]  # delivered after it
    handshake: events.HandshakeCompleted | None


def initial_destination_cid(datagram: bytes) -> bytes:
    """Read the Destination Connection ID of a long-header packet (RFC 9000 Section 17.2)."""
    if len(datagram) < 6 or not datagram[0] & 0x80:
        raise ValueError("not a QUIC long-header packet")
    length = datagram[5]
    return datagram[6 : 6 + length]


def _server(identity: ServerIdentity, store: TicketStore, first: bytes) -> QuicConnection:
    config = QuicConfiguration(is_client=False, alpn_protocols=["h3"])
    config.certificate = identity.certificate
    config.private_key = identity.private_key
    return QuicConnection(
        configuration=config,
        original_destination_connection_id=initial_destination_cid(first),
        session_ticket_fetcher=store.fetch,
        session_ticket_handler=store.add,
    )


def _client(
    identity: ServerIdentity,
    ticket: SessionTicket | None,
    on_ticket: Callable[[SessionTicket], None] | None = None,
) -> QuicConnection:
    config = QuicConfiguration(
        is_client=True, alpn_protocols=["h3"], server_name=identity.server_name
    )
    config.verify_mode = 0  # ssl.CERT_NONE: the lab trusts its own self-signed certificate
    config.session_ticket = ticket
    return QuicConnection(configuration=config, session_ticket_handler=on_ticket)


def _drain(conn: QuicConnection) -> list[events.QuicEvent]:
    out: list[events.QuicEvent] = []
    while (event := conn.next_event()) is not None:
        out.append(event)
    return out


def _pump(client: QuicConnection, server: QuicConnection, now: float) -> list[events.QuicEvent]:
    """Shuttle datagrams both ways until quiet; return the server's events in order."""
    server_events: list[events.QuicEvent] = []
    for step in range(20):
        t = now + step * 0.001
        moved = False
        for data, _ in client.datagrams_to_send(now=t):
            server.receive_datagram(data, CLIENT_ADDR, now=t)
            moved = True
        for data, _ in server.datagrams_to_send(now=t):
            client.receive_datagram(data, SERVER_ADDR, now=t)
            moved = True
        server_events += _drain(server)
        if not moved:
            break
    return server_events


def obtain_ticket(identity: ServerIdentity, store: TicketStore) -> SessionTicket:
    """Run a full 1-RTT handshake; the server issues one NewSessionTicket after Finished."""
    received: list[SessionTicket] = []
    client = _client(identity, None, received.append)
    client.connect(SERVER_ADDR, now=_START)
    first = [data for data, _ in client.datagrams_to_send(now=_START)]
    server = _server(identity, store, first[0])
    for data in first:
        server.receive_datagram(data, CLIENT_ADDR, now=_START)
    _pump(client, server, _START)
    if len(received) != 1:
        raise RuntimeError(f"expected one ticket, got {len(received)}")
    return received[0]


def _split(server_events: list[events.QuicEvent]) -> ServerView:
    early: list[bytes] = []
    one_rtt: list[bytes] = []
    handshake: events.HandshakeCompleted | None = None
    for event in server_events:
        if isinstance(event, events.HandshakeCompleted):
            handshake = event
        elif isinstance(event, events.StreamDataReceived) and handshake is None:
            early.append(event.data)
        elif isinstance(event, events.StreamDataReceived):
            one_rtt.append(event.data)
    return ServerView(tuple(early), tuple(one_rtt), handshake)


@dataclass(frozen=True, slots=True)
class ZeroRttRun:
    flight: tuple[bytes, ...]  # the client's first flight: ClientHello + 0-RTT packets
    server: ServerView


def zero_rtt_request(
    identity: ServerIdentity,
    store: TicketStore,
    ticket: SessionTicket,
    request: bytes,
    *,
    after_handshake: bytes = b"",
) -> ZeroRttRun:
    """Resume with `ticket`, put `request` in 0-RTT, complete the handshake, then
    optionally send `after_handshake` as 1-RTT data on a second stream."""
    client = _client(identity, ticket)
    client.connect(SERVER_ADDR, now=_START + 1)
    stream_id = client.get_next_available_stream_id()
    client.send_stream_data(stream_id, request, end_stream=True)
    flight = tuple(data for data, _ in client.datagrams_to_send(now=_START + 1))
    server = _server(identity, store, flight[0])
    for data in flight:
        server.receive_datagram(data, CLIENT_ADDR, now=_START + 1)
    server_events = _drain(server) + _pump(client, server, _START + 1)
    if after_handshake:
        late_stream = client.get_next_available_stream_id()
        client.send_stream_data(late_stream, after_handshake, end_stream=True)
        server_events += _pump(client, server, _START + 1.5)
    return ZeroRttRun(flight, _split(server_events))


def replay_flight(
    identity: ServerIdentity, store: TicketStore, flight: Iterable[bytes]
) -> ServerView:
    """An on-path attacker copies the first flight to a fresh server instance.

    The attacker has no keys, so the handshake never completes; anything in
    `early_stream_data` was decrypted and handed to the application anyway.
    """
    datagrams = list(flight)
    server = _server(identity, store, datagrams[0])
    for data in datagrams:
        server.receive_datagram(data, CLIENT_ADDR, now=_START + 2)
    return _split(_drain(server))


# ---------------------------------------------------------------------------
# 3. Origin: 425 Too Early middleware (RFC 8470 Sections 3, 5.1, 5.2)
# ---------------------------------------------------------------------------

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})  # RFC 9110 Section 9.2.1
ReplayPolicy = Callable[[str, str], bool]  # (method, path) -> safe to process if replayed


def safe_method_policy(method: str, path: str) -> bool:
    """Method safety alone; RFC 8470 Section 3 warns it 'cannot be universally relied upon'."""
    return method in SAFE_METHODS


def allowlist_policy(routes: Iterable[tuple[str, str]]) -> ReplayPolicy:
    """Explicit per-resource configuration, which RFC 8470 Section 3 RECOMMENDS."""
    allowed = frozenset(routes)

    def policy(method: str, path: str) -> bool:
        return (method, path) in allowed

    return policy


def is_early_data(headers: Iterable[tuple[bytes, bytes]]) -> bool:
    """Section 5.1: any instance, with any value, is treated as `Early-Data: 1`."""
    return any(name.lower() == b"early-data" for name, _ in headers)


TOO_EARLY_BODY = b"Too Early: retry after the TLS handshake completes\n"


class TooEarlyMiddleware:
    """Answer 425 for early requests the policy does not mark replay-safe."""

    def __init__(self, app: ASGIApp, policy: ReplayPolicy = safe_method_policy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        early = is_early_data(scope["headers"])
        if early and not self.policy(scope["method"], scope["path"]):
            await send(
                {
                    "type": "http.response.start",
                    "status": 425,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(TOO_EARLY_BODY)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": TOO_EARLY_BODY})
            return

        async def send_without_early_data(message: Message) -> None:
            if message["type"] == "http.response.start":
                # Section 5.1: Early-Data MUST NOT be included in responses.
                message["headers"] = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"early-data"
                ]
            await send(message)

        await self.app(scope, receive, send_without_early_data)


# ---------------------------------------------------------------------------
# 4. Hops: gateway and user agent (RFC 8470 Sections 4, 5.1, 5.2)
# ---------------------------------------------------------------------------


def gateway_forward_headers(
    headers: Iterable[tuple[bytes, bytes]], *, handshake_complete: bool
) -> list[tuple[bytes, bytes]]:
    """Add `Early-Data: 1` when forwarding before the client handshake completes.

    An existing instance is never removed, even after the handshake completes.
    """
    out = list(headers)
    if not handshake_complete and not is_early_data(out):
        out.append((b"early-data", b"1"))
    return out


class GatewayAction(StrEnum):
    FORWARD_425 = "forward-425"
    RETRY_AFTER_HANDSHAKE = "retry-after-handshake"


def gateway_on_425(
    *, request_had_early_data_header: bool, received_in_early_data: bool
) -> GatewayAction:
    """Section 5.2: MUST forward if the inbound request carried Early-Data; else MAY retry."""
    if request_had_early_data_header or not received_in_early_data:
        return GatewayAction.FORWARD_425
    return GatewayAction.RETRY_AFTER_HANDSHAKE


@dataclass(frozen=True, slots=True)
class Attempt:
    early: bool
    status: int


async def user_agent_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    content: bytes = b"",
    early_data_available: bool = True,
) -> list[Attempt]:
    """Send a request the way RFC 8470 Section 4 requires of a user agent.

    Only safe methods go in early data. On 425 the request is retried once, and the
    retry is never sent in early data (Section 5.2). The `Early-Data` header added
    here models the TLS-terminating gateway that received the request in 0-RTT.
    """
    early = early_data_available and method in SAFE_METHODS
    attempts: list[Attempt] = []
    for _ in range(2):  # the original attempt and at most one retry
        headers = gateway_forward_headers([], handshake_complete=not early)
        response = await client.request(
            method, url, content=content, headers=[(k.decode(), v.decode()) for k, v in headers]
        )
        attempts.append(Attempt(early, response.status_code))
        if response.status_code != 425 or not early:
            break
        early = False
    return attempts
