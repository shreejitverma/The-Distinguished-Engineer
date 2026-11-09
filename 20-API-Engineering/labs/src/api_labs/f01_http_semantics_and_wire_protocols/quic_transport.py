"""QUIC version 1 transport mechanics (RFC 9000) driven in memory with aioquic.

The module has two halves.
The pure half encodes variable-length integers (RFC 9000 Section 16), classifies stream IDs
(RFC 9000 Section 2.1), and parses the unprotected, version-independent header fields
(RFC 9000 Section 17.2 and Section 17.3).
The simulated half wires an aioquic client and server together through a fake UDP network
with a fake clock, so tests can drop datagrams, change the client address, and count bytes.
"""

from __future__ import annotations

import datetime
import ssl
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.logger import QuicLogger
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

if TYPE_CHECKING:
    from aioquic.quic.events import QuicEvent

Address = tuple[str, int]

VARINT_MAX = (1 << 62) - 1
QUIC_V1 = 0x00000001
MAX_CID_LENGTH_V1 = 20
LONG_PACKET_TYPES_V1 = {0: "initial", 1: "0rtt", 2: "handshake", 3: "retry"}
ALPN = "api-labs"


def encode_varint(value: int) -> bytes:
    """Encode *value* in the shortest QUIC variable-length integer form (RFC 9000 Section 16)."""
    if value < 0 or value > VARINT_MAX:
        raise ValueError(f"varint out of range: {value}")
    if value <= 0x3F:
        return value.to_bytes(1, "big")
    if value <= 0x3FFF:
        return (value | 0x4000).to_bytes(2, "big")
    if value <= 0x3FFF_FFFF:
        return (value | 0x8000_0000).to_bytes(4, "big")
    return (value | 0xC000_0000_0000_0000).to_bytes(8, "big")


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode one varint at *offset*; return the value and the offset just past it."""
    if offset >= len(data):
        raise ValueError("varint truncated")
    length = 1 << (data[offset] >> 6)
    end = offset + length
    if end > len(data):
        raise ValueError("varint truncated")
    value = data[offset] & 0x3F
    for byte in data[offset + 1 : end]:
        value = (value << 8) | byte
    return value, end


@dataclass(frozen=True, slots=True)
class StreamType:
    """The two low bits of a stream ID (RFC 9000 Section 2.1, Table 1)."""

    initiator: Literal["client", "server"]
    direction: Literal["bidi", "uni"]


def classify_stream_id(stream_id: int) -> StreamType:
    """Return who opened *stream_id* and whether it is bidirectional."""
    if stream_id < 0 or stream_id > VARINT_MAX:
        raise ValueError(f"stream ID out of range: {stream_id}")
    return StreamType(
        initiator="server" if stream_id & 0x01 else "client",
        direction="uni" if stream_id & 0x02 else "bidi",
    )


def nth_stream_id(n: int, *, initiator: Literal["client", "server"], unidirectional: bool) -> int:
    """Return the ID of the *n*-th (0-based) stream of one type: ``4 * n + type bits``."""
    if n < 0:
        raise ValueError("n must be non-negative")
    return 4 * n + (0x01 if initiator == "server" else 0) + (0x02 if unidirectional else 0)


@dataclass(frozen=True, slots=True)
class HeaderView:
    """Header fields readable without keys: header protection hides the rest."""

    long_header: bool
    fixed_bit: bool
    version: int | None
    long_type: str | None
    dcid: bytes
    scid: bytes | None


def parse_header(datagram: bytes, *, short_dcid_length: int) -> HeaderView:
    """Parse the first packet header of *datagram* (RFC 9000 Section 17.2 and Section 17.3).

    A short header carries no connection ID length, so the receiver must already know it.
    """
    if not datagram:
        raise ValueError("empty datagram")
    first = datagram[0]
    fixed_bit = bool(first & 0x40)
    if not first & 0x80:
        end = 1 + short_dcid_length
        if end > len(datagram):
            raise ValueError("short header truncated")
        return HeaderView(False, fixed_bit, None, None, datagram[1:end], None)
    if len(datagram) < 6:
        raise ValueError("long header truncated")
    version = int.from_bytes(datagram[1:5], "big")
    offset = 5
    cids: list[bytes] = []
    for _ in range(2):
        if offset >= len(datagram):
            raise ValueError("long header truncated")
        length = datagram[offset]
        if version == QUIC_V1 and length > MAX_CID_LENGTH_V1:
            raise ValueError(f"connection ID length {length} exceeds 20 in QUIC v1")
        offset += 1
        if offset + length > len(datagram):
            raise ValueError("long header truncated")
        cids.append(datagram[offset : offset + length])
        offset += length
    long_type = LONG_PACKET_TYPES_V1[(first & 0x30) >> 4] if version == QUIC_V1 else None
    return HeaderView(True, fixed_bit, version, long_type, cids[0], cids[1])


def make_certificate(
    *, padding_names: int = 0
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """Self-signed P-256 certificate; *padding_names* extra SANs inflate the server flight."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    sans: list[x509.GeneralName] = [x509.DNSName("localhost")]
    sans += [x509.DNSName(f"pad{i:04d}.example.test") for i in range(padding_names)]
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(start)
        .not_valid_after(start + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert, key


class QuicLab:
    """An aioquic client and server joined by a fake UDP network and a fake clock."""

    SERVER_ADDR: Address = ("192.0.2.10", 443)

    def __init__(
        self, *, client_addr: Address = ("198.51.100.7", 50000), padding_names: int = 0
    ) -> None:
        self.now = 1000.0
        self.client_addr = client_addr
        self.client_log = QuicLogger()
        self.server_log = QuicLogger()
        client_cfg = QuicConfiguration(
            is_client=True,
            alpn_protocols=[ALPN],
            server_name="localhost",
            quic_logger=self.client_log,
        )
        client_cfg.verify_mode = ssl.CERT_NONE
        self._server_cfg = QuicConfiguration(
            is_client=False, alpn_protocols=[ALPN], quic_logger=self.server_log
        )
        cert, key = make_certificate(padding_names=padding_names)
        self._server_cfg.certificate = cert
        self._server_cfg.private_key = key
        self.client = QuicConnection(configuration=client_cfg)
        self._server: QuicConnection | None = None
        self.bytes_from: defaultdict[Address, int] = defaultdict(int)
        self.bytes_to: defaultdict[Address, int] = defaultdict(int)
        self.client.connect(self.SERVER_ADDR, now=self.now)

    @property
    def server(self) -> QuicConnection:
        if self._server is None:
            raise RuntimeError("server has not received a datagram yet")
        return self._server

    def client_send(self, *, drop: bool = False) -> list[bytes]:
        """Flush client datagrams; deliver them from ``client_addr`` unless *drop*."""
        sent = [data for data, _ in self.client.datagrams_to_send(self.now)]
        if drop:
            return sent
        for data in sent:
            if self._server is None:
                # Like a server's demultiplexer: the client-chosen DCID names the connection.
                header = parse_header(data, short_dcid_length=8)
                self._server = QuicConnection(
                    configuration=self._server_cfg,
                    original_destination_connection_id=header.dcid,
                )
            self.bytes_from[self.client_addr] += len(data)
            self._server.receive_datagram(data, self.client_addr, self.now)
        return sent

    def server_send(self, *, drop: bool = False) -> list[tuple[bytes, Address]]:
        """Flush server datagrams; deliver only those addressed to ``client_addr``."""
        sent = self.server.datagrams_to_send(self.now)
        for data, addr in sent:
            self.bytes_to[addr] += len(data)
            if not drop and addr == self.client_addr:
                self.client.receive_datagram(data, self.SERVER_ADDR, self.now)
        return sent

    def pump(self, *, max_rounds: int = 200) -> None:
        """Exchange datagrams, advancing the clock 1 ms per round, until both sides are idle."""
        for _ in range(max_rounds):
            moved = len(self.client_send()) + len(self.server_send())
            if not moved:
                return
            self.advance(0.001)
        raise RuntimeError("network did not go idle")

    def advance(self, seconds: float) -> None:
        """Move the clock forward and fire every expired loss, ACK, or idle timer."""
        self.now += seconds
        for conn in (self.client, self._server):
            if conn is None:
                continue
            timer = conn.get_timer()
            if timer is not None and timer <= self.now:
                conn.handle_timer(self.now)

    def handshake(self) -> None:
        self.client_send()
        self.pump()

    def migrate_client(self, new_addr: Address, *, rotate_cid: bool = True) -> None:
        """Rebind the client to *new_addr*; RFC 9000 Section 9.5 requires a fresh CID."""
        self.client_addr = new_addr
        if rotate_cid:
            self.client.change_connection_id()


def drain_events(conn: QuicConnection) -> list[QuicEvent]:
    out: list[QuicEvent] = []
    while (event := conn.next_event()) is not None:
        out.append(event)
    return out


def frames_logged(logger: QuicLogger, direction: Literal["sent", "received"]) -> list[str]:
    """Frame types in the qlog trace of *logger*, in order, for one direction."""
    trace: dict[str, Any] = logger.to_dict()["traces"][0]
    names: list[str] = []
    for event in trace["events"]:
        if event["name"] == f"transport:packet_{direction}":
            names.extend(frame["frame_type"] for frame in event["data"]["frames"])
    return names
