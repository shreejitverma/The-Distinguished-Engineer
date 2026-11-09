"""Protocol discovery: ALPN (RFC 7301), Alt-Svc (RFC 7838), and HTTPS records (RFC 9460).

Everything here is in-process and deterministic: TLS handshakes run over ``ssl.MemoryBIO``,
DNS records are decoded from RDATA bytes, and HTTP/2 ``ALTSVC`` frames go through ``h2``
state machines without a socket.
"""

from __future__ import annotations

import datetime
import ipaddress
import itertools
import ssl
import struct
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple

import h2.config
import h2.connection
import h2.events
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# ALPN ProtocolNameList (RFC 7301 Section 3.1) and server selection (Section 3.2)
# ---------------------------------------------------------------------------

ALPN_EXTENSION_TYPE = 16  # RFC 7301 Section 3.1
SERVER_NAME_EXTENSION_TYPE = 0  # RFC 6066 Section 3
NO_APPLICATION_PROTOCOL_ALERT = 120  # RFC 7301 Section 3.2


class ALPNError(ValueError):
    """A ProtocolNameList violates RFC 7301 Section 3.1."""


def encode_protocol_name_list(protocols: Sequence[bytes]) -> bytes:
    """Serialize ``protocol_name_list<2..2^16-1>`` of ``ProtocolName<1..2^8-1>``."""
    body = bytearray()
    for name in protocols:
        if not 1 <= len(name) <= 255:
            raise ALPNError(f"protocol name length {len(name)} outside 1..255")
        body.append(len(name))
        body.extend(name)
    if not 2 <= len(body) <= 0xFFFF:
        raise ALPNError(f"protocol_name_list length {len(body)} outside 2..65535")
    return struct.pack("!H", len(body)) + bytes(body)


def decode_protocol_name_list(data: bytes) -> tuple[bytes, ...]:
    """Parse a ProtocolNameList, rejecting empty names and length mismatches."""
    if len(data) < 2:
        raise ALPNError("truncated ProtocolNameList length")
    (list_len,) = struct.unpack_from("!H", data, 0)
    if list_len != len(data) - 2 or list_len < 2:
        raise ALPNError("ProtocolNameList length does not match its contents")
    names: list[bytes] = []
    offset = 2
    while offset < len(data):
        name_len = data[offset]
        offset += 1
        if name_len == 0:
            raise ALPNError("empty ProtocolName")
        if offset + name_len > len(data):
            raise ALPNError("truncated ProtocolName")
        names.append(data[offset : offset + name_len])
        offset += name_len
    return tuple(names)


def select_alpn(server_preference: Sequence[bytes], client_offer: Sequence[bytes]) -> bytes | None:
    """Return the server's most preferred protocol that the client offered.

    ``None`` means no overlap, where RFC 7301 Section 3.2 says the server SHALL send a
    fatal ``no_application_protocol`` alert.
    """
    offered = set(client_offer)
    for candidate in server_preference:
        if candidate in offered:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Reading ALPN and SNI out of a captured TLS ClientHello (RFC 8446 Sections 4, 4.1.2, 5.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClientHelloInfo:
    """The plaintext fields of a ClientHello that protocol discovery depends on."""

    extension_types: tuple[int, ...]
    server_name: str | None
    alpn: tuple[bytes, ...] | None


def _take(data: bytes, offset: int, size: int) -> tuple[bytes, int]:
    end = offset + size
    if end > len(data):
        raise ValueError("truncated ClientHello")
    return data[offset:end], end


def parse_client_hello(record: bytes) -> ClientHelloInfo:
    """Parse the first TLS record of a client flight and pull out SNI and ALPN."""
    header, offset = _take(record, 0, 5)
    content_type, _legacy_version, record_len = struct.unpack("!BHH", header)
    if content_type != 22:
        raise ValueError(f"expected handshake record (22), got {content_type}")
    fragment, _ = _take(record, offset, record_len)
    if fragment[0] != 1:
        raise ValueError(f"expected ClientHello (1), got handshake type {fragment[0]}")
    body_len = int.from_bytes(fragment[1:4], "big")
    body, _ = _take(fragment, 4, body_len)
    offset = 2 + 32  # legacy_version, random
    session_id_len = body[offset]
    offset += 1 + session_id_len
    (suites_len,) = struct.unpack_from("!H", body, offset)
    offset += 2 + suites_len
    compression_len = body[offset]
    offset += 1 + compression_len
    (extensions_len,) = struct.unpack_from("!H", body, offset)
    offset += 2
    extensions, _ = _take(body, offset, extensions_len)
    types: list[int] = []
    server_name: str | None = None
    alpn: tuple[bytes, ...] | None = None
    pos = 0
    while pos < len(extensions):
        ext_type, ext_len = struct.unpack_from("!HH", extensions, pos)
        ext_data, pos = _take(extensions, pos + 4, ext_len)
        types.append(ext_type)
        if ext_type == ALPN_EXTENSION_TYPE:
            alpn = decode_protocol_name_list(ext_data)
        elif ext_type == SERVER_NAME_EXTENSION_TYPE:
            # ServerNameList: uint16 length, NameType host_name(0), HostName<1..2^16-1>
            name_type = ext_data[2]
            (host_len,) = struct.unpack_from("!H", ext_data, 3)
            if name_type == 0:
                server_name = ext_data[5 : 5 + host_len].decode("ascii")
    return ClientHelloInfo(extension_types=tuple(types), server_name=server_name, alpn=alpn)


# ---------------------------------------------------------------------------
# A real TLS 1.3 handshake through CPython's ssl module, with no socket
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HandshakeResult:
    """Outcome of one in-memory handshake as each side sees it."""

    client_hello: ClientHelloInfo
    client_alpn: str | None
    server_alpn: str | None
    tls_version: str | None


def make_self_signed_cert(dns_names: Sequence[str]) -> tuple[bytes, bytes]:
    """Return ``(cert_pem, key_pem)`` for a P-256 certificate covering *dns_names*."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_names[0])])
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in dns_names]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def memory_handshake(
    client_ctx: ssl.SSLContext,
    server_ctx: ssl.SSLContext,
    server_hostname: str,
    *,
    max_flights: int = 8,
) -> HandshakeResult:
    """Drive a client and a server ``SSLObject`` to completion over ``MemoryBIO`` pairs."""
    c_in, c_out, s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_ctx.wrap_bio(c_in, c_out, server_side=False, server_hostname=server_hostname)
    server = server_ctx.wrap_bio(s_in, s_out, server_side=True)
    first_flight = b""
    client_done = server_done = False
    for _ in range(max_flights):
        if not client_done:
            try:
                client.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass
        flight = c_out.read()
        if not first_flight:
            first_flight = flight
        s_in.write(flight)
        if not server_done:
            try:
                server.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass
        c_in.write(s_out.read())
        if client_done and server_done:
            break
    else:
        raise RuntimeError("handshake did not complete")
    return HandshakeResult(
        client_hello=parse_client_hello(first_flight),
        client_alpn=client.selected_alpn_protocol(),
        server_alpn=server.selected_alpn_protocol(),
        tls_version=client.version(),
    )


# ---------------------------------------------------------------------------
# Alt-Svc field parsing (RFC 7838 Section 3) and caching (Sections 2.2, 3.1, 6)
# ---------------------------------------------------------------------------

DEFAULT_MAX_AGE = 86400  # RFC 7838 Section 3.1: fresh for 24 hours by default
DELTA_SECONDS_CAP = 2**31  # RFC 9111 Section 1.2.2
MAX_EMPTY_LIST_ELEMENTS = 8  # lab bound for RFC 9110 Section 5.6.1.2 "reasonable number"
_TCHAR = frozenset(b"!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_UPPER_HEX = frozenset("0123456789ABCDEF")


class AltSvcParseError(ValueError):
    """The field value does not match the RFC 7838 Section 3 grammar."""


@dataclass(frozen=True, slots=True)
class AltService:
    """One alternative: ALPN protocol, host (empty means the origin's host), and port."""

    alpn: bytes
    host: str
    port: int
    max_age: int = DEFAULT_MAX_AGE
    persist: bool = False


@dataclass(frozen=True, slots=True)
class AltSvcClear:
    """The ``clear`` keyword: invalidate every alternative for the origin."""


def decode_protocol_id(token: str) -> bytes:
    """Percent-decode an Alt-Svc ``protocol-id`` and enforce its single canonical form."""
    out = bytearray()
    i = 0
    raw = token.encode("ascii")
    while i < len(raw):
        octet = raw[i]
        if octet == 0x25:  # "%"
            hex_digits = token[i + 1 : i + 3]
            if len(hex_digits) != 2 or not set(hex_digits) <= _UPPER_HEX:
                raise AltSvcParseError(f"percent-encoding needs two uppercase hex digits: {token}")
            decoded = int(hex_digits, 16)
            if decoded in _TCHAR and decoded != 0x25:
                raise AltSvcParseError(f"token character must not be percent-encoded: {token}")
            out.append(decoded)
            i += 3
        else:
            out.append(octet)
            i += 1
    return bytes(out)


class _Cursor:
    """Tiny recursive-descent reader over one field value."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def at_end(self) -> bool:
        return self.pos >= len(self.text)

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def skip_ows(self) -> None:
        while self.peek() in (" ", "\t") and not self.at_end():
            self.pos += 1

    def expect(self, char: str) -> None:
        if self.peek() != char:
            raise AltSvcParseError(f"expected {char!r} at offset {self.pos} in {self.text!r}")
        self.pos += 1

    def token(self) -> str:
        start = self.pos
        while not self.at_end() and ord(self.peek()) in _TCHAR:
            self.pos += 1
        if self.pos == start:
            raise AltSvcParseError(f"expected token at offset {start} in {self.text!r}")
        return self.text[start : self.pos]

    def quoted_string(self) -> str:
        self.expect('"')
        out: list[str] = []
        while True:
            if self.at_end():
                raise AltSvcParseError("unterminated quoted-string")
            char = self.peek()
            self.pos += 1
            if char == '"':
                return "".join(out)
            if char == "\\":
                if self.at_end():
                    raise AltSvcParseError("dangling quoted-pair")
                char = self.peek()
                self.pos += 1
            # RFC 9110 Section 5.6.4: neither qdtext nor quoted-pair admits control
            # characters; rejecting obs-text as well is a lab policy.
            if char != "\t" and not 0x20 <= ord(char) <= 0x7E:
                raise AltSvcParseError("control character in quoted-string")
            out.append(char)


def _parse_authority(authority: str) -> tuple[str, int]:
    host, sep, port_text = authority.rpartition(":")
    if not sep or not port_text.isdigit() or not port_text.isascii():
        raise AltSvcParseError(f"alt-authority needs ':port': {authority!r}")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise AltSvcParseError(f"port out of range: {port}")
    return host, port


def parse_alt_svc(field_value: str) -> AltSvcClear | tuple[AltService, ...]:
    """Parse an ``Alt-Svc`` field value into ``clear`` or alternatives in preference order."""
    cursor = _Cursor(field_value)
    alternatives: list[AltService] = []
    saw_clear = False
    empty_elements = 0
    while True:
        cursor.skip_ows()
        if cursor.peek() in (",", ""):
            # RFC 9110 Section 5.6.1.2: accept and ignore a reasonable number of empty elements.
            empty_elements += 1
            if empty_elements > MAX_EMPTY_LIST_ELEMENTS:
                raise AltSvcParseError("too many empty list elements")
            if cursor.at_end():
                break
            cursor.pos += 1
            continue
        protocol_id = cursor.token()
        cursor.skip_ows()
        if protocol_id == "clear" and cursor.peek() in (",", ""):
            saw_clear = True
        else:
            cursor.expect("=")
            host, port = _parse_authority(cursor.quoted_string())
            max_age, persist = DEFAULT_MAX_AGE, False
            cursor.skip_ows()
            while cursor.peek() == ";":
                cursor.pos += 1
                cursor.skip_ows()
                name = cursor.token().lower()
                cursor.expect("=")
                value = cursor.quoted_string() if cursor.peek() == '"' else cursor.token()
                if name == "ma" and value.isdigit() and value.isascii():
                    max_age = min(int(value), DELTA_SECONDS_CAP)
                elif name == "persist":
                    persist = value == "1"
                cursor.skip_ows()
            alternatives.append(
                AltService(decode_protocol_id(protocol_id), host, port, max_age, persist)
            )
        cursor.skip_ows()
        if cursor.at_end():
            break
        cursor.expect(",")
    if saw_clear:
        return AltSvcClear()
    if not alternatives:
        raise AltSvcParseError("Alt-Svc needs at least one alt-value or clear")
    return tuple(alternatives)


class Origin(NamedTuple):
    """An RFC 6454 origin tuple."""

    scheme: str
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class CachedAlternative:
    """An alternative and the absolute time after which it may not start new connections."""

    service: AltService
    expires_at: float


@dataclass(slots=True)
class AltSvcCache:
    """Client-side alternative service cache keyed by origin."""

    entries: dict[Origin, tuple[CachedAlternative, ...]] = field(
        default_factory=lambda: dict[Origin, tuple[CachedAlternative, ...]]()
    )

    def observe_response(
        self,
        origin: Origin,
        *,
        status: int,
        alt_svc_lines: Sequence[str],
        received_at: float,
        age: int = 0,
    ) -> None:
        """Apply the ``Alt-Svc`` lines of one response received at *received_at*."""
        if status == 421 or not alt_svc_lines:
            return  # RFC 7838 Section 6: Alt-Svc in a 421 MUST be ignored
        try:
            parsed = parse_alt_svc(", ".join(alt_svc_lines))
        except AltSvcParseError:
            return  # lab policy: a malformed field is ignored as a whole
        if isinstance(parsed, AltSvcClear):
            self.entries.pop(origin, None)
            return
        generated_at = received_at - age  # ma counts from generation (Section 3.1)
        self.entries[origin] = tuple(
            CachedAlternative(alt, generated_at + alt.max_age) for alt in parsed
        )

    def candidates(
        self, origin: Origin, *, now: float, supported: Iterable[bytes]
    ) -> tuple[AltService, ...]:
        """Fresh alternatives for *origin* that this client can speak, in server order."""
        allowed = frozenset(supported)
        return tuple(
            cached.service
            for cached in self.entries.get(origin, ())
            if cached.expires_at > now and cached.service.alpn in allowed
        )

    def network_changed(self) -> None:
        """Drop every alternative that lacks ``persist=1`` (RFC 7838 Section 2.2)."""
        for origin, cached in list(self.entries.items()):
            kept = tuple(c for c in cached if c.service.persist)
            if kept:
                self.entries[origin] = kept
            else:
                del self.entries[origin]

    def misdirected(self, origin: Origin, service: AltService) -> None:
        """Remove *service* after it answered 421 (RFC 7838 Section 6)."""
        kept = tuple(c for c in self.entries.get(origin, ()) if c.service != service)
        if kept:
            self.entries[origin] = kept
        else:
            self.entries.pop(origin, None)


def alt_used_value(origin: Origin, service: AltService) -> str:
    """``Alt-Used = uri-host [ ":" port ]`` for a request sent to *service* (Section 5)."""
    return f"{service.host or origin.host}:{service.port}"


# ---------------------------------------------------------------------------
# HTTP/2 ALTSVC frame (RFC 7838 Section 4) through h2
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AltsvcExchange:
    """The raw ALTSVC frame a server wrote and what an h2 client surfaced from it."""

    wire: bytes
    advertised: tuple[tuple[bytes, bytes], ...]


def _h2_pair() -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    return client, server


def h2_altsvc_exchange(
    field_value: bytes, *, origin: bytes | None = None, authority: str = "origin.example.test"
) -> AltsvcExchange:
    """Send one ALTSVC from an h2 server, on stream 0 if *origin* is set, else on stream 1."""
    client, server = _h2_pair()
    if origin is None:
        client.send_headers(
            1,
            [(":method", "GET"), (":scheme", "https"), (":authority", authority), (":path", "/")],
            end_stream=True,
        )
        server.receive_data(client.data_to_send())
        server.data_to_send()
        server.advertise_alternative_service(field_value, stream_id=1)
    else:
        server.advertise_alternative_service(field_value, origin=origin)
    wire = server.data_to_send()
    return AltsvcExchange(wire=wire, advertised=h2_client_altsvc_events(client, wire))


def h2_client_altsvc_events(
    client: h2.connection.H2Connection, wire: bytes
) -> tuple[tuple[bytes, bytes], ...]:
    """Feed *wire* to an h2 client and return ``(origin, field_value)`` per ALTSVC event."""
    found: list[tuple[bytes, bytes]] = []
    for event in client.receive_data(wire):
        if (
            isinstance(event, h2.events.AlternativeServiceAvailable)
            and event.origin is not None
            and event.field_value is not None
        ):
            found.append((event.origin, event.field_value))
    return tuple(found)


def h2_connected_client() -> h2.connection.H2Connection:
    """An h2 client past the preface and SETTINGS exchange, ready for raw frames."""
    client, _server = _h2_pair()
    return client


# ---------------------------------------------------------------------------
# SVCB and HTTPS RDATA (RFC 9460 Sections 2.2, 7, 8, 9)
# ---------------------------------------------------------------------------

KEY_MANDATORY, KEY_ALPN, KEY_NO_DEFAULT_ALPN, KEY_PORT = 0, 1, 2, 3
KEY_IPV4HINT, KEY_ECH, KEY_IPV6HINT = 4, 5, 6
HTTPS_DEFAULT_ALPN: tuple[bytes, ...] = (b"http/1.1",)  # RFC 9460 Section 9
HTTPS_AUTOMATICALLY_MANDATORY = frozenset({KEY_NO_DEFAULT_ALPN, KEY_PORT})  # Section 9
ALPN_TRANSPORT: Mapping[bytes, str] = {b"h3": "quic", b"h2": "tcp", b"http/1.1": "tcp"}


class SvcbFormatError(ValueError):
    """Malformed RDATA: the client MUST reject the entire RRset (RFC 9460 Section 2.2)."""


@dataclass(frozen=True, slots=True)
class SvcbRecord:
    """A decoded SVCB or HTTPS record with its recognized SvcParams typed."""

    priority: int
    target: str
    params: Mapping[int, bytes]
    alpn: tuple[bytes, ...] = ()
    no_default_alpn: bool = False
    port: int | None = None
    ipv4hint: tuple[str, ...] = ()
    ipv6hint: tuple[str, ...] = ()
    mandatory: tuple[int, ...] = ()

    @property
    def alias_mode(self) -> bool:
        return self.priority == 0


def _decode_name(rdata: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    total = 0
    while True:
        if offset >= len(rdata):
            raise SvcbFormatError("TargetName runs past RDATA")
        length = rdata[offset]
        offset += 1
        if length == 0:
            break
        if length > 63:
            raise SvcbFormatError("label longer than 63 octets or a compression pointer")
        if offset + length > len(rdata):
            raise SvcbFormatError("label runs past RDATA")
        labels.append(rdata[offset : offset + length].decode("ascii"))
        offset += length
        total += length + 1
        if total > 254:
            raise SvcbFormatError("TargetName longer than 255 octets")
    return (".".join(labels) + "." if labels else "."), offset


def _decode_alpn_value(value: bytes) -> tuple[bytes, ...]:
    ids: list[bytes] = []
    pos = 0
    while pos < len(value):
        length = value[pos]
        if length == 0 or pos + 1 + length > len(value):
            raise SvcbFormatError("alpn-id pairs must exactly fill the SvcParamValue")
        ids.append(value[pos + 1 : pos + 1 + length])
        pos += 1 + length
    if not ids:
        raise SvcbFormatError("alpn needs at least one alpn-id")
    return tuple(ids)


def _decode_hints(value: bytes, size: int) -> tuple[str, ...]:
    if not value or len(value) % size:
        raise SvcbFormatError(f"address hint length {len(value)} is not a non-zero multiple")
    if size == 4:
        return tuple(str(ipaddress.IPv4Address(value[i : i + 4])) for i in range(0, len(value), 4))
    return tuple(str(ipaddress.IPv6Address(value[i : i + 16])) for i in range(0, len(value), 16))


def _decode_mandatory(value: bytes) -> tuple[int, ...]:
    if not value or len(value) % 2:
        raise SvcbFormatError("mandatory must be a non-empty list of 2-octet keys")
    keys = struct.unpack(f"!{len(value) // 2}H", value)
    if any(b <= a for a, b in itertools.pairwise(keys)):
        raise SvcbFormatError("mandatory keys must be strictly increasing")
    if KEY_MANDATORY in keys:
        raise SvcbFormatError("mandatory MUST NOT list itself")
    return keys


def decode_svcb_rdata(rdata: bytes) -> SvcbRecord:
    """Decode SVCB/HTTPS RDATA, enforcing the malformed-RR rules of RFC 9460 Section 2.2."""
    if len(rdata) < 3:
        raise SvcbFormatError("RDATA shorter than SvcPriority plus root TargetName")
    (priority,) = struct.unpack_from("!H", rdata, 0)
    target, offset = _decode_name(rdata, 2)
    params: dict[int, bytes] = {}
    last_key = -1
    while offset < len(rdata):
        if offset + 4 > len(rdata):
            raise SvcbFormatError("RDATA ends within a SvcParam header")
        key, length = struct.unpack_from("!HH", rdata, offset)
        offset += 4
        if key <= last_key:
            raise SvcbFormatError("SvcParamKeys not in strictly increasing order")
        if offset + length > len(rdata):
            raise SvcbFormatError("RDATA ends within a SvcParamValue")
        params[key] = rdata[offset : offset + length]
        offset += length
        last_key = key
    if priority == 0:
        return SvcbRecord(priority, target, params)  # AliasMode: SvcParams are ignored
    port_value = params.get(KEY_PORT)
    if port_value is not None and len(port_value) != 2:
        raise SvcbFormatError("port must be exactly 2 octets")
    if params.get(KEY_NO_DEFAULT_ALPN, b"") != b"":
        raise SvcbFormatError("no-default-alpn value must be empty")
    ech = params.get(KEY_ECH)
    if ech is not None and (len(ech) < 2 or int.from_bytes(ech[:2], "big") != len(ech) - 2):
        raise SvcbFormatError("ech must be an ECHConfigList with its length prefix")
    return SvcbRecord(
        priority=priority,
        target=target,
        params=params,
        alpn=_decode_alpn_value(params[KEY_ALPN]) if KEY_ALPN in params else (),
        no_default_alpn=KEY_NO_DEFAULT_ALPN in params,
        port=struct.unpack("!H", port_value)[0] if port_value is not None else None,
        ipv4hint=_decode_hints(params[KEY_IPV4HINT], 4) if KEY_IPV4HINT in params else (),
        ipv6hint=_decode_hints(params[KEY_IPV6HINT], 16) if KEY_IPV6HINT in params else (),
        mandatory=_decode_mandatory(params[KEY_MANDATORY]) if KEY_MANDATORY in params else (),
    )


def https_alpn_set(record: SvcbRecord) -> tuple[bytes, ...]:
    """The SVCB ALPN set: ``alpn`` plus the default set unless ``no-default-alpn``."""
    extra = () if record.no_default_alpn else HTTPS_DEFAULT_ALPN
    return record.alpn + tuple(a for a in extra if a not in record.alpn)


def is_compatible(record: SvcbRecord, understood_keys: Iterable[int]) -> bool:
    """RFC 9460 Section 8 compatibility plus Section 2.4.3 self-consistency, for HTTPS."""
    present = set(record.params)
    if record.no_default_alpn and KEY_ALPN not in present:
        return False  # Section 7.1.1: no-default-alpn requires alpn
    if not set(record.mandatory) <= present:
        return False  # Section 8: listed keys MUST also appear
    required = set(record.mandatory) | (HTTPS_AUTOMATICALLY_MANDATORY & present)
    return required <= set(understood_keys)


@dataclass(frozen=True, slots=True)
class ConnectionAttempt:
    """One TLS or QUIC connection attempt derived from a ServiceMode record."""

    priority: int
    host: str
    port: int
    transport: str
    alpn: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class SvcbResolution:
    """Either an alias to follow or the ordered list of attempts."""

    alias_target: str | None
    attempts: tuple[ConnectionAttempt, ...]


def plan_https_connections(
    owner: str,
    authority_port: int,
    rrset: Sequence[bytes],
    client_alpn: Mapping[str, Sequence[bytes]],
    *,
    understood_keys: Iterable[int] = (0, 1, 2, 3, 4, 6),
) -> SvcbResolution:
    """Turn an HTTPS RRset into ordered connection attempts (RFC 9460 Sections 3 and 7.1.2).

    ``client_alpn`` maps a transport (``"tcp"`` or ``"quic"``) to the client's own
    ProtocolNameList for it, in client preference order.  Records with equal priority keep
    RRset order here for determinism; RFC 9460 Section 2.4.1 says clients SHOULD shuffle them.
    """
    records = [decode_svcb_rdata(rdata) for rdata in rrset]  # one malformed RR fails all
    aliases = [r for r in records if r.alias_mode]
    if aliases:
        return SvcbResolution(alias_target=aliases[0].target, attempts=())
    understood = frozenset(understood_keys)
    supported = {alpn for protocols in client_alpn.values() for alpn in protocols}
    attempts: list[ConnectionAttempt] = []
    for record in sorted(records, key=lambda r: r.priority):
        if not is_compatible(record, understood):
            continue
        intersection = [a for a in https_alpn_set(record) if a in supported]
        transports = {ALPN_TRANSPORT[a] for a in intersection if a in ALPN_TRANSPORT}
        host = owner if record.target == "." else record.target
        port = record.port if record.port is not None else authority_port
        for transport, protocols in client_alpn.items():
            if transport in transports:
                attempts.append(
                    ConnectionAttempt(record.priority, host, port, transport, tuple(protocols))
                )
    return SvcbResolution(alias_target=None, attempts=tuple(attempts))
