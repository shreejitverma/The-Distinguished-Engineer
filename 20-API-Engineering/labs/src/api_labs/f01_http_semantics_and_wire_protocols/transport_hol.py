"""TCP head-of-line blocking for HTTP/2 versus independent QUIC streams.

Three parts, all in memory and deterministic:

* ``TcpReceiveBuffer`` models the receive side of TCP (RFC 9293 Section 3.10): bytes above a
  sequence gap are held and reach the application only when the gap is filled.
* ``h2_response_segments`` and ``QuicLab`` drive the real ``h2`` and ``aioquic`` state machines,
  so the stall is observed in library events, not asserted by the model.
* ``completion_times_ms`` and ``stall_probability`` are the arithmetic behind the note's table.
"""

from __future__ import annotations

import datetime
import heapq
import math
import ssl
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import h2.config
import h2.connection
import h2.events
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import StreamDataReceived
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


class Transport(StrEnum):
    """Which ordering domain a lost packet blocks."""

    TCP = "tcp"  # one ordered byte stream for every HTTP/2 stream
    QUIC = "quic"  # one ordered byte stream per QUIC stream


@dataclass(frozen=True, slots=True)
class Segment:
    """One TCP segment: a sequence number and the payload bytes it carries."""

    seq: int
    payload: bytes
    streams: frozenset[int] = frozenset()  # HTTP/2 stream ids with frames in this segment


@dataclass(slots=True)
class TcpReceiveBuffer:
    """Receive side of one TCP connection: in-order delivery to the application only.

    A segment at ``rcv_nxt`` is delivered with every queued segment it makes contiguous; a
    segment above ``rcv_nxt`` is queued out of order. The model assumes retransmissions reuse
    the original segment boundaries.
    """

    rcv_nxt: int = 0
    _out_of_order: dict[int, bytes] = field(default_factory=dict[int, bytes])

    @property
    def buffered_out_of_order(self) -> int:
        """Bytes the kernel holds that the application cannot read yet."""
        return sum(len(p) for p in self._out_of_order.values())

    def receive(self, segment: Segment) -> bytes:
        """Accept *segment* and return the bytes that became readable by the application."""
        end = segment.seq + len(segment.payload)
        if end <= self.rcv_nxt:
            return b""  # duplicate of data already delivered
        if segment.seq > self.rcv_nxt:
            self._out_of_order[segment.seq] = segment.payload
            return b""
        readable = bytearray(segment.payload[self.rcv_nxt - segment.seq :])
        self.rcv_nxt = end
        while self.rcv_nxt in self._out_of_order:
            queued = self._out_of_order.pop(self.rcv_nxt)
            readable += queued
            self.rcv_nxt += len(queued)
        return bytes(readable)


def _h2_pair() -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    server = h2.connection.H2Connection(
        h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
    )
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    server.receive_data(client.data_to_send())
    return client, server


def h2_response_segments(
    bodies: Sequence[bytes],
) -> tuple[h2.connection.H2Connection, tuple[Segment, ...]]:
    """Open one request per body on one HTTP/2 connection and segment the server's answer.

    Requests use streams 1, 3, 5, ... The server writes all response ``HEADERS`` frames, then
    each body as one ``DATA`` frame with ``END_STREAM``; each write becomes one TCP segment.
    Returns the client connection (waiting for those bytes) and the segments in send order.
    """
    client, server = _h2_pair()
    stream_ids = [1 + 2 * i for i in range(len(bodies))]
    for sid in stream_ids:
        client.send_headers(
            sid,
            [(":method", "GET"), (":scheme", "https"), (":authority", "t"), (":path", f"/{sid}")],
            end_stream=True,
        )
    server.receive_data(client.data_to_send())
    for sid, body in zip(stream_ids, bodies, strict=True):
        server.send_headers(sid, [(":status", "200"), ("content-length", str(len(body)))])
    writes: list[tuple[bytes, frozenset[int]]] = [(server.data_to_send(), frozenset(stream_ids))]
    for sid, body in zip(stream_ids, bodies, strict=True):
        server.send_data(sid, body, end_stream=True)
        writes.append((server.data_to_send(), frozenset({sid})))
    segments: list[Segment] = []
    seq = 0
    for payload, streams in writes:
        segments.append(Segment(seq, payload, streams))
        seq += len(payload)
    return client, tuple(segments)


@dataclass(frozen=True, slots=True)
class StreamProgress:
    """What the application has seen per stream: body bytes and whether the stream ended."""

    body: Mapping[int, bytes]
    ended: frozenset[int]


def feed_h2(
    client: h2.connection.H2Connection, readable: bytes, progress: StreamProgress
) -> StreamProgress:
    """Hand bytes TCP made readable to the ``h2`` client and fold its events into *progress*."""
    body = dict(progress.body)
    ended = set(progress.ended)
    for event in client.receive_data(readable):
        if isinstance(event, h2.events.DataReceived):
            body[event.stream_id] = body.get(event.stream_id, b"") + event.data
        elif isinstance(event, h2.events.StreamEnded):
            ended.add(event.stream_id)
    return StreamProgress(body, frozenset(ended))


def _self_signed() -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(start)
        .not_valid_after(start + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    return cert, key


_CLIENT_ADDR = ("192.0.2.1", 4433)
_SERVER_ADDR = ("192.0.2.2", 443)
_MAX_STEPS = 10_000


@dataclass(frozen=True, slots=True)
class QuicDelivery:
    """Outcome of one batch of QUIC responses as the client application saw it.

    ``completed_s`` maps stream id to the seconds from the server's first write until the
    client could read the stream's final byte; ``bodies`` is what it read on each stream.
    ``datagram_streams`` lists, for each datagram of that first write, the streams it may carry.
    """

    completed_s: Mapping[int, float]
    bodies: Mapping[int, bytes]
    datagram_streams: tuple[frozenset[int], ...]


@dataclass(slots=True)
class QuicLab:
    """An in-memory ``aioquic`` client and server on a fake clock with a fixed one-way delay.

    Datagrams sit in a delivery queue for ``one_way_s``; each side's own timers (delayed ACK,
    loss detection, probe timeout) fire when the clock reaches them, so retransmission timing
    comes from ``aioquic``'s recovery code, not from this lab.
    """

    one_way_s: float = 0.025
    now: float = 0.0
    client: QuicConnection = field(init=False)
    server: QuicConnection = field(init=False)
    _pending: list[tuple[float, int, bool, bytes]] = field(
        init=False, default_factory=list[tuple[float, int, bool, bytes]]
    )
    _ticket: int = field(init=False, default=0)
    _server_sent: int = field(init=False, default=0)
    _drop: frozenset[int] = field(init=False, default=frozenset())

    def __post_init__(self) -> None:
        cert, key = _self_signed()
        client_cfg = QuicConfiguration(
            is_client=True, alpn_protocols=["lab"], verify_mode=ssl.CERT_NONE
        )
        server_cfg = QuicConfiguration(is_client=False, alpn_protocols=["lab"])
        server_cfg.certificate = cert
        server_cfg.private_key = key
        self.client = QuicConnection(configuration=client_cfg)
        self.server = QuicConnection(
            configuration=server_cfg,
            original_destination_connection_id=self.client.original_destination_connection_id,
        )
        self.client.connect(_SERVER_ADDR, now=self.now)
        self._emit_all()
        self._run_until(self._idle)
        while self.client.next_event() is not None:
            pass
        while self.server.next_event() is not None:
            pass

    def _emit(self, conn: QuicConnection, *, to_client: bool) -> int:
        if conn.get_peer_address() is None:
            return 0  # the server has no path until the client's first datagram arrives
        datagrams = conn.datagrams_to_send(now=self.now)
        for data, _ in datagrams:
            if to_client:
                index = self._server_sent
                self._server_sent += 1
                if index in self._drop:
                    continue  # lost in the network
            self._ticket += 1
            heapq.heappush(
                self._pending, (self.now + self.one_way_s, self._ticket, to_client, data)
            )
        return len(datagrams)

    def _emit_all(self) -> None:
        self._emit(self.client, to_client=False)
        self._emit(self.server, to_client=True)

    def _next_timer(self) -> float:
        timers = [t for t in (self.client.get_timer(), self.server.get_timer()) if t is not None]
        return min(timers, default=math.inf)

    def _idle(self) -> bool:
        """Nothing in flight and only far-off timers (the idle timeout) remain."""
        return not self._pending and self._next_timer() > self.now + 1.0

    def _run_until(self, done: Callable[[], bool]) -> None:
        for _ in range(_MAX_STEPS):
            if done():
                return
            if self._pending and self._pending[0][0] <= self._next_timer():
                arrival, _, to_client, data = heapq.heappop(self._pending)
                self.now = arrival
                if to_client:
                    self.client.receive_datagram(data, _SERVER_ADDR, now=self.now)
                else:
                    self.server.receive_datagram(data, _CLIENT_ADDR, now=self.now)
            else:
                deadline = self._next_timer()
                if math.isinf(deadline):
                    raise RuntimeError("QUIC exchange stalled: nothing in flight, no timer armed")
                self.now = max(self.now, deadline)
                for conn in (self.client, self.server):
                    timer = conn.get_timer()
                    if timer is not None and timer <= self.now:
                        conn.handle_timer(now=self.now)
            self._emit_all()
        raise RuntimeError("QUIC exchange exceeded the step budget")

    def open_requests(self, count: int) -> list[int]:
        """Open *count* client bidirectional streams (0, 4, 8, ...) as HTTP/3 requests would."""
        ids: list[int] = []
        for _ in range(count):
            sid = self.client.get_next_available_stream_id()
            self.client.send_stream_data(sid, f"GET /{sid}".encode(), end_stream=True)
            ids.append(sid)
        self._emit_all()
        self._run_until(self._idle)
        while self.server.next_event() is not None:
            pass
        return ids

    def respond(
        self,
        bodies: Mapping[int, bytes],
        *,
        packet_per_stream: bool,
        drop: frozenset[int] = frozenset(),
    ) -> QuicDelivery:
        """Send each response body, lose the server datagrams numbered in *drop*, and run.

        Server datagrams are numbered from 0 at the first response write, retransmissions
        included. With *packet_per_stream* the server builds packets after each stream write,
        so every datagram carries one stream; otherwise one build packs the streams together
        and each datagram is labelled with every stream it may carry.
        """
        start = self.now
        self._server_sent = 0
        self._drop = drop
        layout: list[frozenset[int]] = []
        if packet_per_stream:
            for sid, body in bodies.items():
                self.server.send_stream_data(sid, body, end_stream=True)
                layout += [frozenset({sid})] * self._emit(self.server, to_client=True)
        else:
            for sid, body in bodies.items():
                self.server.send_stream_data(sid, body, end_stream=True)
            layout += [frozenset(bodies)] * self._emit(self.server, to_client=True)
        received: dict[int, bytes] = {}
        completed: dict[int, float] = {}

        def all_complete() -> bool:
            while (event := self.client.next_event()) is not None:
                if isinstance(event, StreamDataReceived):
                    received[event.stream_id] = received.get(event.stream_id, b"") + event.data
                    if event.end_stream:
                        completed[event.stream_id] = round(self.now - start, 6)
            return len(completed) == len(bodies)

        try:
            self._run_until(all_complete)
        finally:
            self._drop = frozenset()
        return QuicDelivery(completed, received, tuple(layout))


def completion_times_ms(
    packet_owner: Sequence[int],
    lost: frozenset[int],
    *,
    transport: Transport,
    one_way_ms: int,
    recovery_ms: int,
) -> dict[int, int]:
    """Return when each stream's last byte becomes readable by the application.

    All packets leave at t=0 (serialization ignored). A packet arrives at *one_way_ms*; a lost
    packet's retransmission arrives *recovery_ms* later. Over TCP a packet is readable once it
    and every earlier packet have arrived; over QUIC once every earlier packet of the same
    stream has arrived.
    """
    if one_way_ms < 0 or recovery_ms < 0:
        raise ValueError("delays must be non-negative")
    if any(i < 0 or i >= len(packet_owner) for i in lost):
        raise ValueError("lost packet index out of range")
    gate: dict[int, int] = {}  # ordering domain -> latest arrival so far
    done: dict[int, int] = {}
    for i, owner in enumerate(packet_owner):
        arrival = one_way_ms + (recovery_ms if i in lost else 0)
        domain = 0 if transport is Transport.TCP else owner
        gate[domain] = max(gate.get(domain, 0), arrival)
        done[owner] = max(done.get(owner, 0), gate[domain])
    return done


def stall_probability(
    packet_owner: Sequence[int], stream: int, loss_rate: float, *, transport: Transport
) -> float:
    """Probability that *stream* waits on at least one retransmission, losses independent.

    Over TCP the stream waits on every packet up to its last one; over QUIC only on its own.
    """
    if not 0.0 <= loss_rate <= 1.0:
        raise ValueError("loss_rate must be within [0, 1]")
    positions = [i for i, owner in enumerate(packet_owner) if owner == stream]
    if not positions:
        raise ValueError(f"stream {stream} owns no packets")
    gating = positions[-1] + 1 if transport is Transport.TCP else len(positions)
    return 1.0 - (1.0 - loss_rate) ** gating


def round_robin(streams: int, packets_per_stream: int) -> tuple[int, ...]:
    """Packet owners when a multiplexer interleaves *streams* one packet at a time."""
    return tuple(s for _ in range(packets_per_stream) for s in range(streams))
