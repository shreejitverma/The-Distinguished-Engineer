"""HTTP/2 connection coalescing, the ORIGIN frame, and 421 Misdirected Request.

A client pool reuses one TLS connection for a second origin when the server certificate
verifies for the new host and DNS returns the connected address (RFC 9110 Section 4.3.3,
RFC 9113 Section 9.1.1), or when an ORIGIN frame lists the origin (RFC 8336 Section 2.4).
An SNI-routing edge that requires client certificates for some hosts answers 421 when a
request for such a host arrives on a connection negotiated for another name, and the pool
then retries once on a fresh connection whose SNI is the request's host.

The TLS handshakes are real OpenSSL handshakes over memory BIOs; the HTTP/2 frames are real
`h2` state machines exchanging plaintext bytes in memory; nothing opens a socket.
"""

from __future__ import annotations

import datetime as dt
import enum
import ipaddress
import ssl
import struct
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import h2.config
import h2.connection
import h2.events
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError
from hyperframe.frame import ExtensionFrame
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

    from starlette.requests import Request
    from starlette.types import Receive, Scope, Send

NOW = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
ORIGIN_FRAME_TYPE = 0x0C  # RFC 8336 Section 2.1
_ORIGIN_RESERVED_FLAGS = 0x0F  # RFC 8336 Section 2.2: 0x1, 0x2, 0x4, 0x8 force "ignore"
_DEFAULT_PORTS = {"https": 443, "http": 80}
_MAX_HANDSHAKE_ROUNDS = 8


# --- Service identity (RFC 9525 Section 6.3) -------------------------------------------------


def dns_id_matches(presented: str, reference: str) -> bool:
    """Match a certificate DNS-ID against a host: label by label, ASCII case-insensitive.

    A `*` is honored only as the whole left-most label and matches exactly one label; any
    other use makes the presented identifier invalid, so it matches nothing.
    """
    if not (presented.isascii() and reference.isascii()):
        return False  # U-labels must be converted to A-labels before matching
    p_labels = presented.lower().split(".")
    r_labels = reference.lower().split(".")
    if "" in p_labels or "" in r_labels:
        return False
    if "*" in presented:
        if presented.count("*") != 1 or p_labels[0] != "*":
            return False
        return len(p_labels) == len(r_labels) and p_labels[1:] == r_labels[1:]
    return p_labels == r_labels


def server_cert_valid_for(
    leaf: x509.Certificate, anchors: Sequence[x509.Certificate], host: str, *, at: dt.datetime
) -> bool:
    """Run the full check a new TLS connection to *host* would run (RFC 9113 Section 9.1.1)."""
    try:
        subject = x509.DNSName(host)
        verifier = PolicyBuilder().store(Store(list(anchors))).time(at)
        verifier.build_server_verifier(subject).verify(leaf, [])
    except (ValueError, VerificationError):
        return False
    return True


# --- A private PKI with keys derived from fixed integers ------------------------------------


@dataclass(frozen=True, slots=True)
class Identity:
    """A certificate and its private key."""

    cert: x509.Certificate
    key: ec.EllipticCurvePrivateKey

    def cert_pem(self) -> str:
        return self.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")

    def write(self, directory: Path, stem: str) -> tuple[str, str]:
        """Write PEM files for `SSLContext.load_cert_chain` and return their paths."""
        cert_path, key_path = directory / f"{stem}.pem", directory / f"{stem}.key"
        cert_path.write_text(self.cert_pem())
        key_path.write_bytes(
            self.key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        return str(cert_path), str(key_path)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def make_ca(seed: int, common_name: str) -> Identity:
    key = ec.derive_private_key(seed, ec.SECP256R1())
    usage = x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=True,
        crl_sign=True,
        encipher_only=False,
        decipher_only=False,
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(_name(common_name))
        .public_key(key.public_key())
        .serial_number(seed)
        .not_valid_before(NOW - dt.timedelta(days=1))
        .not_valid_after(NOW + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(usage, critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return Identity(cert, key)


def issue(ca: Identity, seed: int, *, dns_names: Sequence[str], client: bool = False) -> Identity:
    """Issue a leaf whose subjectAltName lists *dns_names* (wildcards allowed)."""
    key = ec.derive_private_key(seed, ec.SECP256R1())
    purpose = ExtendedKeyUsageOID.CLIENT_AUTH if client else ExtendedKeyUsageOID.SERVER_AUTH
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(dns_names[0]))
        .issuer_name(ca.cert.subject)
        .public_key(key.public_key())
        .serial_number(seed)
        .not_valid_before(NOW - dt.timedelta(days=1))
        .not_valid_after(NOW + dt.timedelta(days=90))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in dns_names]), critical=False
        )
        .add_extension(x509.ExtendedKeyUsage([purpose]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.key.public_key()), critical=False
        )
        .sign(ca.key, hashes.SHA256())
    )
    return Identity(cert, key)


# --- TLS: SNI pre-read and a real handshake in memory ---------------------------------------


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ValueError("truncated TLS structure")
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def vector(self, length_bytes: int) -> bytes:
        return self.take(int.from_bytes(self.take(length_bytes)))

    def done(self) -> bool:
        return self.pos == len(self.data)


def peek_sni(first_flight: bytes) -> str | None:
    """Return the `host_name` of a ClientHello's `server_name` extension, as an SNI router does.

    Layout: TLSPlaintext (RFC 9846 Section 5.1), Handshake (Section 4), ClientHello
    (Section 4.2.2), ServerNameList (RFC 6066 Section 3).
    """
    record = _Reader(first_flight)
    if record.take(1) != b"\x16":
        raise ValueError("not a TLS handshake record")
    record.take(2)  # legacy_record_version
    handshake = _Reader(record.vector(2))
    if handshake.take(1) != b"\x01":
        raise ValueError("not a ClientHello")
    hello = _Reader(handshake.vector(3))
    hello.take(2 + 32)  # legacy_version, random
    hello.vector(1)  # legacy_session_id
    hello.vector(2)  # cipher_suites
    hello.vector(1)  # legacy_compression_methods
    if hello.done():
        return None
    extensions = _Reader(hello.vector(2))
    while not extensions.done():
        ext_type = int.from_bytes(extensions.take(2))
        ext_data = extensions.vector(2)
        if ext_type == 0:  # server_name
            names = _Reader(_Reader(ext_data).vector(2))
            while not names.done():
                name_type, host_name = names.take(1), names.vector(2)
                if name_type == b"\x00":  # host_name
                    return host_name.decode("ascii").lower()
            return None
    return None


@dataclass(frozen=True, slots=True)
class TlsSession:
    """What both ends learned from the handshake."""

    sni: str | None
    alpn: str | None
    server_cert: x509.Certificate
    client_cert_verified: bool


class TlsHandshakeError(Exception):
    """The handshake failed; the OpenSSL reason each side saw."""

    def __init__(self, *, client_reason: str | None, server_reason: str | None) -> None:
        super().__init__(f"client={client_reason} server={server_reason}")
        self.client_reason = client_reason
        self.server_reason = server_reason


def _step(obj: ssl.SSLObject) -> bool:
    try:
        obj.do_handshake()
    except ssl.SSLWantReadError:
        return False
    return True


def _client_reason(client: ssl.SSLObject, client_done: bool) -> str | None:
    try:
        if client_done:
            client.read(1)  # TLS 1.3: a rejected client learns it on its first read
        else:
            client.do_handshake()
    except ssl.SSLWantReadError:
        return None
    except ssl.SSLError as exc:
        return exc.reason
    return None


def tls_handshake(
    client_ctx: ssl.SSLContext,
    server_ctx_for: Callable[[str | None], ssl.SSLContext],
    server_hostname: str,
) -> TlsSession:
    """Handshake in memory; the server picks its context from the SNI it pre-reads."""
    c_in, c_out, s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_ctx.wrap_bio(c_in, c_out, server_hostname=server_hostname)
    client_done = _step(client)
    hello = c_out.read()
    sni = peek_sni(hello)
    server = server_ctx_for(sni).wrap_bio(s_in, s_out, server_side=True)
    s_in.write(hello)
    server_done = False
    for _ in range(_MAX_HANDSHAKE_ROUNDS):
        if not server_done:
            try:
                server_done = _step(server)
            except ssl.SSLError as exc:
                c_in.write(s_out.read())
                reason = _client_reason(client, client_done)
                raise TlsHandshakeError(client_reason=reason, server_reason=exc.reason) from exc
        c_in.write(s_out.read())
        if not client_done:
            try:
                client_done = _step(client)
            except ssl.SSLError as exc:
                raise TlsHandshakeError(client_reason=exc.reason, server_reason=None) from exc
        s_in.write(c_out.read())
        if client_done and server_done:
            break
    else:
        raise RuntimeError("TLS handshake did not converge")
    der = client.getpeercert(binary_form=True)
    if der is None:
        raise RuntimeError("server sent no certificate")
    return TlsSession(
        sni=sni,
        alpn=client.selected_alpn_protocol(),
        server_cert=x509.load_der_x509_certificate(der),
        client_cert_verified=bool(server.getpeercert()),
    )


def server_context(identity_files: tuple[str, str], *, client_ca_pem: str | None) -> ssl.SSLContext:
    """A TLS 1.3 `h2` server context; *client_ca_pem* turns on `CERT_REQUIRED`."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(*identity_files)
    ctx.set_alpn_protocols(["h2"])
    if client_ca_pem is not None:
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=client_ca_pem)
    return ctx


def client_context(ca_pem: str, identity_files: tuple[str, str] | None = None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cadata=ca_pem)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.set_alpn_protocols(["h2"])
    if identity_files is not None:
        ctx.load_cert_chain(*identity_files)
    return ctx


# --- Origins and the ORIGIN frame (RFC 6454, RFC 8336) --------------------------------------


@dataclass(frozen=True, slots=True)
class Origin:
    scheme: str
    host: str
    port: int

    @classmethod
    def from_url(cls, url: str) -> Origin:
        parts = urlsplit(url)
        if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
            raise ValueError(f"not an http(s) URL with a host: {url!r}")
        if _is_ip_literal(parts.hostname):
            # SNI carries only DNS names (RFC 6066 Section 3), so IP-literal origins never
            # get one; the lab keys every connection decision on SNI and leaves them out.
            raise ValueError(f"IP-literal origins are out of scope: {url!r}")
        return cls(parts.scheme, parts.hostname, parts.port or _DEFAULT_PORTS[parts.scheme])

    @property
    def authority(self) -> str:
        default = _DEFAULT_PORTS[self.scheme]
        return self.host if self.port == default else f"{self.host}:{self.port}"

    def serialize(self) -> str:
        """ASCII serialization; the default port is omitted (RFC 6454 Section 6.2)."""
        return f"{self.scheme}://{self.authority}"


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def parse_ascii_origin(raw: bytes) -> Origin | None:
    """Parse one ASCII-Origin; anything that is not `scheme://host[:port]` yields None."""
    try:
        parts = urlsplit(raw.decode("ascii"))
        port = parts.port
    except (UnicodeDecodeError, ValueError):
        return None
    host = parts.hostname
    if parts.scheme not in _DEFAULT_PORTS or not host or "*" in host or _is_ip_literal(host):
        return None  # RFC 8336 Section 2.2: no wildcard names in Origin-Entry
    if parts.path or parts.query or parts.fragment or "@" in parts.netloc:
        return None
    return Origin(parts.scheme, host, port or _DEFAULT_PORTS[parts.scheme])


def encode_origin_frame(origins: Iterable[str], *, flags: int = 0, stream_id: int = 0) -> bytes:
    """An HTTP/2 ORIGIN frame: 9-byte frame header, then Origin-Len (16) + ASCII-Origin.

    The header is packed by hand: hyperframe 6.1.0 `ExtensionFrame.serialize` writes
    `body_len`, which only `parse_body` sets, so a frame built in code goes out as length 0.
    """
    body = b"".join(struct.pack("!H", len(o)) + o.encode("ascii") for o in origins)
    header = len(body).to_bytes(3) + bytes([ORIGIN_FRAME_TYPE, flags])
    return header + (stream_id & 0x7FFFFFFF).to_bytes(4) + body


def parse_origin_entries(body: bytes) -> list[Origin]:
    """Origin-Entry fields; unparseable ones are ignored, a truncated tail is dropped."""
    reader = _Reader(body)
    origins: list[Origin] = []
    while not reader.done():
        try:
            raw = reader.vector(2)
        except ValueError:
            break
        origin = parse_ascii_origin(raw)
        if origin is not None:
            origins.append(origin)
    return origins


# --- The server side: a TLS-terminating, SNI-routing edge -----------------------------------


class SniPolicy(enum.Enum):
    """How the edge treats a request whose host differs from the connection's SNI."""

    MATCH_SNI_FOR_MTLS = "match-sni-for-mtls"  # nginx: 421 if the target host verifies clients
    AUTHORITY_ONLY = "authority-only"  # route by :authority alone (the CVE-2018-21246 class)


@dataclass(frozen=True, slots=True)
class VirtualHost:
    app: HostApp
    require_client_cert: bool = False


class Edge:
    """One certificate for every host, a TLS context per host, and an SNI policy."""

    def __init__(
        self,
        identity_files: tuple[str, str],
        vhosts: Mapping[str, VirtualHost],
        *,
        client_ca_pem: str,
        policy: SniPolicy = SniPolicy.MATCH_SNI_FOR_MTLS,
        origin_frame: Sequence[str] | None = None,
    ) -> None:
        self.vhosts = vhosts
        self.policy = policy
        self.origin_frame = origin_frame
        self.log: list[tuple[str | None, str, int]] = []  # (SNI, :authority, status)
        self._default = server_context(identity_files, client_ca_pem=None)
        self._contexts = {
            host: server_context(
                identity_files, client_ca_pem=client_ca_pem if vh.require_client_cert else None
            )
            for host, vh in vhosts.items()
        }

    def context_for(self, sni: str | None) -> ssl.SSLContext:
        return self._contexts.get(sni or "", self._default)

    async def respond(
        self, tls: TlsSession, method: str, authority: str, path: str, body: bytes
    ) -> tuple[int, bytes]:
        host = authority.partition(":")[0].lower()
        vhost = self.vhosts.get(host)
        if vhost is None:
            status, payload = 421, b"no origin configured for this host\n"
        elif (
            self.policy is SniPolicy.MATCH_SNI_FOR_MTLS
            and vhost.require_client_cert
            and host != tls.sni
        ):
            status, payload = 421, b"connection was negotiated for another host\n"
        else:
            transport = httpx.ASGITransport(app=vhost.app)
            async with httpx.AsyncClient(
                transport=transport, base_url=f"https://{authority}"
            ) as client:
                response = await client.request(method, path, content=body)
            status, payload = response.status_code, response.content
        self.log.append((tls.sni, authority, status))
        return status, payload


class HostApp:
    """A Starlette app that names its virtual host and records every execution."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []
        self._app = Starlette(routes=[Route("/{path:path}", self._handle, methods=["GET", "POST"])])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._app(scope, receive, send)

    async def _handle(self, request: Request) -> Response:
        body = await request.body()
        self.calls.append(f"{request.method} {request.url.path} {len(body)}")
        if request.url.path == "/echo":
            return Response(body, media_type="application/octet-stream")
        return PlainTextResponse(f"{self.name}: {request.method} {request.url.path}\n")


def _header(headers: Sequence[tuple[bytes, bytes]], name: bytes) -> str:
    for key, value in headers:
        if key == name:
            return value.decode("ascii")
    raise ValueError(f"missing {name!r}")


class _ServerConnection:
    """The edge's HTTP/2 side of one connection."""

    def __init__(self, edge: Edge, tls: TlsSession) -> None:
        self.edge, self.tls = edge, tls
        self.h2 = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
        self.h2.initiate_connection()
        self._pending: dict[int, tuple[list[tuple[bytes, bytes]], bytearray]] = {}
        self._outbound: dict[int, bytearray] = {}  # response bytes waiting for window

    def preface(self) -> bytes:
        """SETTINGS, then the ORIGIN frame if the edge sends one (it MUST go on stream 0)."""
        extra = b""
        if self.edge.origin_frame is not None:
            extra = encode_origin_frame(self.edge.origin_frame)
        return self.h2.data_to_send() + extra

    async def receive(self, data: bytes) -> bytes:
        for event in self.h2.receive_data(data):
            if isinstance(event, h2.events.RequestReceived):
                headers = [(bytes(k), bytes(v)) for k, v in event.headers]
                self._pending[event.stream_id] = (headers, bytearray())
            elif isinstance(event, h2.events.DataReceived):
                self._pending[event.stream_id][1].extend(event.data or b"")
                acked = event.flow_controlled_length or 0
                self.h2.acknowledge_received_data(acked, event.stream_id)
            elif isinstance(event, h2.events.StreamEnded):
                headers, body = self._pending.pop(event.stream_id)
                status, payload = await self.edge.respond(
                    self.tls,
                    _header(headers, b":method"),
                    _header(headers, b":authority"),
                    _header(headers, b":path"),
                    bytes(body),
                )
                response_headers = [(":status", str(status)), ("content-length", str(len(payload)))]
                self.h2.send_headers(event.stream_id, response_headers, end_stream=not payload)
                if payload:
                    self._outbound[event.stream_id] = bytearray(payload)
        self._send_pending()  # also runs when a WINDOW_UPDATE arrived
        return self.h2.data_to_send()

    def _send_pending(self) -> None:
        """Send queued response bytes in frames that fit the frame size and both windows."""
        for stream_id in list(self._outbound):
            buffer = self._outbound[stream_id]
            while buffer:
                window = self.h2.local_flow_control_window(stream_id)
                size = min(self.h2.max_outbound_frame_size, window)
                if size <= 0:
                    break
                chunk = bytes(buffer[:size])
                del buffer[:size]
                self.h2.send_data(stream_id, chunk, end_stream=not buffer)
            if not buffer:
                del self._outbound[stream_id]


# --- The client side: a coalescing connection pool ------------------------------------------


@dataclass(slots=True)
class ClientConnection:
    conn_id: int
    opened_for: Origin  # the origin whose host was sent as SNI
    peer: tuple[str, int]
    tls: TlsSession
    h2: h2.connection.H2Connection
    server: _ServerConnection
    origin_set: set[Origin] | None = None  # None: uninitialized (RFC 8336 Section 2.3)
    misdirected: set[Origin] = field(default_factory=set[Origin])
    closed: bool = False  # set by GOAWAY; no new streams


@dataclass(frozen=True, slots=True)
class Attempt:
    origin: str
    conn_id: int
    coalesced: bool
    status: int


@dataclass(frozen=True, slots=True)
class PoolResponse:
    status: int
    body: bytes
    attempts: tuple[Attempt, ...]


@dataclass(frozen=True, slots=True)
class Network:
    """Name resolution and the edges listening at each address."""

    dns: Mapping[str, tuple[str, ...]]
    edges: Mapping[tuple[str, int], Edge]


class CoalescingPool:
    """An HTTP/2 client pool that coalesces origins and retries a 421 on a fresh connection."""

    def __init__(
        self,
        network: Network,
        *,
        ca: x509.Certificate,
        client_identity_files: tuple[str, str] | None = None,
        require_dns_overlap: bool = True,
        honor_origin_frame: bool = True,
        at: dt.datetime = NOW,
    ) -> None:
        self.network = network
        self.anchors = (ca,)
        self.require_dns_overlap = require_dns_overlap
        self.honor_origin_frame = honor_origin_frame
        self.at = at
        ca_pem = ca.public_bytes(serialization.Encoding.PEM).decode("ascii")
        self._client_ctx = client_context(ca_pem, client_identity_files)
        self.connections: list[ClientConnection] = []

    def can_coalesce(self, conn: ClientConnection, origin: Origin) -> bool:
        if conn.closed or origin in conn.misdirected or origin.port != conn.peer[1]:
            return False
        if conn.origin_set is not None and origin not in conn.origin_set:
            return False  # RFC 8336 Section 2.4: MUST NOT consider it authoritative
        if not server_cert_valid_for(conn.tls.server_cert, self.anchors, origin.host, at=self.at):
            return False  # the certificate check is required with or without ORIGIN
        if conn.origin_set is not None:
            return True  # clients MAY skip DNS for origins in the Origin Set
        return not self.require_dns_overlap or conn.peer[0] in self.network.dns.get(origin.host, ())

    def _dedicated(self, origin: Origin) -> ClientConnection | None:
        """An open connection whose handshake was made for *origin* itself."""
        for conn in self.connections:
            if conn.opened_for == origin and not conn.closed and origin not in conn.misdirected:
                return conn
        return None

    def _find(self, origin: Origin) -> ClientConnection | None:
        if conn := self._dedicated(origin):  # a connection negotiated for this host first
            return conn
        for conn in self.connections:
            if self.can_coalesce(conn, origin):
                return conn
        return None

    async def _open(self, origin: Origin) -> ClientConnection:
        addresses = self.network.dns.get(origin.host, ())
        if not addresses:
            raise LookupError(f"no address for {origin.host}")
        peer = (addresses[0], origin.port)
        edge = self.network.edges[peer]
        tls = tls_handshake(self._client_ctx, edge.context_for, origin.host)
        server = _ServerConnection(edge, tls)
        client_h2 = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
        client_h2.initiate_connection()
        conn = ClientConnection(len(self.connections) + 1, origin, peer, tls, client_h2, server)
        self.absorb(conn, client_h2.receive_data(server.preface()))
        await self._flush(conn)
        self.connections.append(conn)
        return conn

    def absorb(self, conn: ClientConnection, events: Iterable[h2.events.Event]) -> None:
        """Act on connection-level events: ORIGIN frames and GOAWAY."""
        for event in events:
            if isinstance(event, h2.events.ConnectionTerminated):
                conn.closed = True
            elif isinstance(event, h2.events.UnknownFrameReceived):
                frame = event.frame
                if isinstance(frame, ExtensionFrame) and frame.type == ORIGIN_FRAME_TYPE:
                    self._on_origin_frame(conn, frame)

    def _on_origin_frame(self, conn: ClientConnection, frame: ExtensionFrame) -> None:
        if not self.honor_origin_frame or frame.stream_id != 0:
            return  # on any other stream the frame MUST be ignored (RFC 8336 Section 2.2)
        if frame.flag_byte & _ORIGIN_RESERVED_FLAGS:
            return
        if conn.origin_set is None:  # initial origin: https, SNI, remote port (Section 2.3)
            conn.origin_set = {Origin("https", conn.tls.sni or conn.peer[0], conn.peer[1])}
        conn.origin_set.update(parse_origin_entries(frame.body))

    async def _flush(self, conn: ClientConnection) -> list[h2.events.Event]:
        events: list[h2.events.Event] = []
        while data := conn.h2.data_to_send():
            new = conn.h2.receive_data(await conn.server.receive(data))
            self.absorb(conn, new)
            events.extend(new)
        return events

    async def _exchange(
        self, conn: ClientConnection, method: str, origin: Origin, path: str, body: bytes
    ) -> tuple[int, bytes]:
        stream_id = conn.h2.get_next_available_stream_id()
        headers = [
            (":method", method),
            (":scheme", origin.scheme),
            (":authority", origin.authority),
            (":path", path),
        ]
        conn.h2.send_headers(stream_id, headers, end_stream=not body)
        events = await self._flush(conn)
        remaining = memoryview(body)
        while remaining:  # frames no larger than the peer's frame size and windows
            size = min(
                conn.h2.max_outbound_frame_size, conn.h2.local_flow_control_window(stream_id)
            )
            if size <= 0:
                raise RuntimeError("flow-control window closed and the peer sent no WINDOW_UPDATE")
            chunk, remaining = remaining[:size], remaining[size:]
            conn.h2.send_data(stream_id, bytes(chunk), end_stream=not remaining)
            events += await self._flush(conn)
        status: int | None = None
        payload = bytearray()
        while True:
            for event in events:
                if getattr(event, "stream_id", None) != stream_id:
                    continue
                if isinstance(event, h2.events.ResponseReceived):
                    pairs = [(bytes(k), bytes(v)) for k, v in event.headers]
                    status = int(_header(pairs, b":status"))
                elif isinstance(event, h2.events.DataReceived):
                    payload.extend(event.data or b"")
                    conn.h2.acknowledge_received_data(event.flow_controlled_length or 0, stream_id)
                elif isinstance(event, h2.events.StreamReset):
                    raise RuntimeError(f"stream {stream_id} reset: {event.error_code!r}")
                elif isinstance(event, h2.events.StreamEnded):
                    if status is None:
                        raise RuntimeError(f"stream {stream_id} ended without a response")
                    return status, bytes(payload)
            events = await self._flush(conn)  # send WINDOW_UPDATEs, collect the rest
            if not events:
                raise RuntimeError(f"stream {stream_id} stalled without a complete response")

    async def request(self, method: str, url: str, *, body: bytes = b"") -> PoolResponse:
        origin = Origin.from_url(url)
        if origin.scheme != "https":
            raise ValueError("this pool only speaks h2 over TLS")
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        attempts: list[Attempt] = []
        conn = self._find(origin) or await self._open(origin)
        while True:
            coalesced = conn.opened_for != origin
            status, payload = await self._exchange(conn, method, origin, path, body)
            attempts.append(Attempt(origin.serialize(), conn.conn_id, coalesced, status))
            if status != 421:
                break
            conn.misdirected.add(origin)  # do not coalesce this origin here again
            if conn.origin_set is not None:
                conn.origin_set.discard(origin)  # RFC 8336 Section 2.3: MUST remove it
            if not coalesced:
                break  # already on a connection negotiated for this host: give up
            # Retry once, and only where the handshake was made for this origin.
            conn = self._dedicated(origin) or await self._open(origin)
        return PoolResponse(status, payload, tuple(attempts))
