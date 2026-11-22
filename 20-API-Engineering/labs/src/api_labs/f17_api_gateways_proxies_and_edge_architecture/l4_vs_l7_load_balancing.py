"""L4 vs L7 load balancing for HTTP/1.1, HTTP/2, and gRPC.

Two layers of simulation, both in-process and deterministic:

1. A counting model (``BackendPod``, ``L4LoadBalancer``, ``L7LoadBalancer``,
   ``ClientSideLoadBalancer``, ``PickFirstClient``) that shows where the balancing
   decision is made and what that does to per-pod request counts.
2. A wire model built on real ``h2`` state machines (``WireBackend``, ``L4WireFlow``,
   ``L7WireProxy``) that shows the same facts in actual HTTP/2 frames: stream ids
   are per connection, an L4 relay forwards bytes it never parses, and an L7 proxy
   terminates the client connection and renumbers streams onto upstream connections.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import RequestReceived, ResponseReceived

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from h2.events import Event

L4Policy = Literal["round_robin", "least_conn"]

# RFC 9113 Section 5.1.1: stream identifiers are 31-bit; a GOAWAY that carries this
# value as its last stream identifier means "no stream has been refused yet".
MAX_STREAM_ID = 2**31 - 1

# RFC 9113 Section 3.4: the first bytes a client sends on every HTTP/2 connection.
CLIENT_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"


class Protocol(StrEnum):
    """Wire protocols with differing connection multiplexing semantics."""

    HTTP_1_1 = "HTTP/1.1"
    HTTP_2 = "HTTP/2"
    GRPC = "gRPC"


@dataclass
class BackendPod:
    """A backend server instance handling incoming connections and requests."""

    pod_id: str
    ip: str
    active_connections: int = 0
    active_streams: int = 0
    total_requests: int = 0
    max_requests_per_connection: int | None = None
    is_healthy: bool = True

    def reset_metrics(self) -> None:
        """Reset operational counters for simulation runs."""
        self.active_connections = 0
        self.active_streams = 0
        self.total_requests = 0


@dataclass
class ClientConnection:
    """A transport connection (TCP 5-tuple) established by a client."""

    conn_id: str
    client_id: str
    protocol: Protocol
    backend: BackendPod | None = None
    stream_count: int = 0
    is_draining: bool = False
    last_stream_id: int = 0


class L4LoadBalancer:
    """Layer 4 (TCP/UDP) Load Balancer.

    Operates strictly at the transport layer (IP:port 5-tuple).
    Assigns each new TCP connection to an upstream backend at handshake time.
    Once established, all subsequent packets/frames/streams on that connection
    are forwarded to the identical backend pod without application awareness.
    The only signal it can balance on is the connection count (``least_conn``)
    or a rotation (``round_robin``); request volume is invisible to it.
    """

    def __init__(self, backends: Sequence[BackendPod], policy: L4Policy = "round_robin") -> None:
        self.backends: list[BackendPod] = list(backends)
        self.policy: L4Policy = policy
        self._next_backend_idx: int = 0
        self.active_connections: list[ClientConnection] = []

    def add_backend(self, backend: BackendPod) -> None:
        """Dynamically register an autoscaled or newly healthy backend pod."""
        self.backends.append(backend)

    def remove_backend(self, pod_id: str) -> None:
        """Deregister an unhealthy or terminated pod."""
        self.backends = [b for b in self.backends if b.pod_id != pod_id]

    def _select(self) -> BackendPod:
        healthy_backends = [b for b in self.backends if b.is_healthy]
        if not healthy_backends:
            msg = "No healthy backends available for L4 connection"
            raise RuntimeError(msg)
        if self.policy == "least_conn":
            return min(healthy_backends, key=lambda b: b.active_connections)
        selected = healthy_backends[self._next_backend_idx % len(healthy_backends)]
        self._next_backend_idx += 1
        return selected

    def connect(self, client_id: str, protocol: Protocol) -> ClientConnection:
        """Establish a new L4 TCP connection; the backend is chosen once, here."""
        selected = self._select()
        selected.active_connections += 1
        conn = ClientConnection(
            conn_id=f"conn-{client_id}-{selected.pod_id}-{selected.active_connections}",
            client_id=client_id,
            protocol=protocol,
            backend=selected,
        )
        self.active_connections.append(conn)
        return conn

    def send_request(self, conn: ClientConnection, stream_id: int) -> tuple[str, bool]:
        """Forward an application request down an established L4 connection.

        Returns (assigned_pod_id, goaway_emitted).
        """
        if conn.backend is None:
            msg = "Cannot send request on unassigned connection"
            raise ValueError(msg)

        if conn.is_draining:
            msg = f"Connection {conn.conn_id} is draining; cannot accept new stream {stream_id}"
            raise RuntimeError(msg)

        conn.stream_count += 1
        conn.last_stream_id = stream_id
        target = conn.backend
        target.total_requests += 1

        goaway_emitted = False
        if (
            target.max_requests_per_connection is not None
            and conn.stream_count >= target.max_requests_per_connection
        ):
            conn.is_draining = True
            goaway_emitted = True

        return target.pod_id, goaway_emitted

    def close_connection(self, conn: ClientConnection) -> None:
        """Terminate a client connection and decrement backend connection count."""
        if conn.backend is not None:
            conn.backend.active_connections = max(0, conn.backend.active_connections - 1)
        if conn in self.active_connections:
            self.active_connections.remove(conn)


class L7LoadBalancer:
    """Layer 7 (Application) Load Balancer / Reverse Proxy.

    Terminates client transport connections and parses HTTP/2 frames or gRPC RPCs.
    Selects an upstream backend independently for every individual request/stream.
    """

    def __init__(self, backends: Sequence[BackendPod]) -> None:
        self.backends: list[BackendPod] = list(backends)
        self._next_backend_idx: int = 0
        self.client_connections: list[ClientConnection] = []

    def add_backend(self, backend: BackendPod) -> None:
        """Register an autoscaled backend pod in the upstream cluster."""
        self.backends.append(backend)

    def remove_backend(self, pod_id: str) -> None:
        """Remove a pod from the upstream cluster."""
        self.backends = [b for b in self.backends if b.pod_id != pod_id]

    def connect(self, client_id: str, protocol: Protocol) -> ClientConnection:
        """Accept a downstream client connection terminated at the L7 proxy."""
        conn = ClientConnection(
            conn_id=f"l7-downstream-{client_id}",
            client_id=client_id,
            protocol=protocol,
            backend=None,  # Terminated at proxy; upstream picked per request
        )
        self.client_connections.append(conn)
        return conn

    def send_request(self, conn: ClientConnection, stream_id: int) -> str:
        """Route an individual HTTP/2 stream or gRPC call across the upstream cluster."""
        healthy_backends = [b for b in self.backends if b.is_healthy]
        if not healthy_backends:
            msg = "No healthy upstream backends available for L7 request"
            raise RuntimeError(msg)

        # L7 Round-Robin selection per request/stream
        selected = healthy_backends[self._next_backend_idx % len(healthy_backends)]
        self._next_backend_idx += 1

        conn.stream_count += 1
        conn.last_stream_id = stream_id
        selected.total_requests += 1
        return selected.pod_id


class ClientSideLoadBalancer:
    """Client-side load balancer (gRPC subchannel pool with round-robin policy).

    Resolves all backend pod endpoints (e.g. via Kubernetes Headless Service DNS)
    and maintains distinct transport connections (subchannels) to each pod.
    Balances individual RPCs across subchannels directly from the client process.
    """

    def __init__(self, client_id: str, backend_endpoints: Sequence[BackendPod]) -> None:
        self.client_id = client_id
        self.subchannels: dict[str, tuple[BackendPod, ClientConnection]] = {}
        self._subchannel_keys: list[str] = []
        self._next_rpc_idx: int = 0

        for pod in backend_endpoints:
            self.add_subchannel(pod)

    def add_subchannel(self, pod: BackendPod) -> None:
        """Open a dedicated transport subchannel to a discovered backend pod."""
        conn = ClientConnection(
            conn_id=f"grpc-subchannel-{self.client_id}->{pod.pod_id}",
            client_id=self.client_id,
            protocol=Protocol.GRPC,
            backend=pod,
        )
        pod.active_connections += 1
        self.subchannels[pod.pod_id] = (pod, conn)
        self._subchannel_keys.append(pod.pod_id)

    def send_rpc(self, rpc_id: int) -> str:
        """Dispatch a single gRPC RPC across the client-managed subchannel pool."""
        if not self._subchannel_keys:
            msg = "No subchannels configured"
            raise RuntimeError(msg)

        pod_id = self._subchannel_keys[self._next_rpc_idx % len(self._subchannel_keys)]
        self._next_rpc_idx += 1

        pod, conn = self.subchannels[pod_id]
        conn.stream_count += 1
        conn.last_stream_id = rpc_id
        pod.total_requests += 1
        return pod.pod_id


class PickFirstClient:
    """gRPC ``pick_first`` policy: the default when no service config asks for another.

    The resolver may return many addresses, but the policy connects to them one at a
    time, in order, and sends every RPC on the first one that is reachable
    (gRPC ``doc/load-balancing.md``, ``pick_first``).
    ``order`` models the per-client address shuffle that DNS or the client applies.
    """

    def __init__(
        self,
        client_id: str,
        resolved: Sequence[BackendPod],
        order: Sequence[int] | None = None,
    ) -> None:
        self.client_id = client_id
        indexes = list(order) if order is not None else list(range(len(resolved)))
        candidates = [resolved[i] for i in indexes]
        self.subchannel: ClientConnection | None = None
        for pod in candidates:
            if pod.is_healthy:
                pod.active_connections += 1
                self.subchannel = ClientConnection(
                    conn_id=f"grpc-pick-first-{client_id}->{pod.pod_id}",
                    client_id=client_id,
                    protocol=Protocol.GRPC,
                    backend=pod,
                )
                break

    def send_rpc(self, rpc_id: int) -> str:
        """Send one RPC on the single connected subchannel."""
        if self.subchannel is None or self.subchannel.backend is None:
            msg = "pick_first found no reachable address"
            raise RuntimeError(msg)
        pod = self.subchannel.backend
        self.subchannel.stream_count += 1
        self.subchannel.last_stream_id = rpc_id
        pod.total_requests += 1
        return pod.pod_id


def calculate_load_skew(pods: Sequence[BackendPod]) -> float:
    """Compute the coefficient of variation (stddev / mean) across pod request counts.

    Returns 0.0 for perfectly balanced distribution, and higher values for skewed load.
    If all request counts are 0, returns 0.0.
    """
    counts = [p.total_requests for p in pods]
    n = len(counts)
    if n == 0:
        return 0.0
    mean = sum(counts) / n
    if mean == 0.0:
        return 0.0
    variance = sum((x - mean) ** 2 for x in counts) / n
    stddev = math.sqrt(variance)
    return stddev / mean


def summarize_distribution(pods: Sequence[BackendPod]) -> dict[str, int]:
    """Return a mapping of pod_id -> total_requests."""
    return {p.pod_id: p.total_requests for p in pods}


# ---------------------------------------------------------------------------
# Wire model: real HTTP/2 frames through ``h2`` state machines
# ---------------------------------------------------------------------------


def new_h2(*, client_side: bool) -> H2Connection:
    """Create an ``h2`` connection with its preface and initial SETTINGS queued."""
    conn = H2Connection(config=H2Configuration(client_side=client_side, header_encoding="utf-8"))
    conn.initiate_connection()
    return conn


def request_headers(path: str) -> list[tuple[str, str]]:
    """Headers of a header-only unary gRPC-style request (no DATA frames needed to route)."""
    return [
        (":method", "POST"),
        (":scheme", "http"),
        (":authority", "svc.internal"),
        (":path", path),
        ("content-type", "application/grpc"),
    ]


def send_requests(client: H2Connection, count: int, path: str = "/pkg.Svc/Call") -> list[int]:
    """Open ``count`` client-initiated streams (odd ids) and return their ids."""
    stream_ids: list[int] = []
    for _ in range(count):
        stream_id = client.get_next_available_stream_id()
        client.send_headers(stream_id, request_headers(path), end_stream=True)
        stream_ids.append(stream_id)
    return stream_ids


def header_value(headers: Iterable[tuple[bytes | str, bytes | str]] | None, name: str) -> str:
    """Return the first value of ``name`` from an ``h2`` header list, decoded to ``str``."""
    for key, value in headers or ():
        key_text = key.decode("ascii") if isinstance(key, bytes) else key
        if key_text == name:
            return value.decode("ascii") if isinstance(value, bytes) else value
    msg = f"header {name} not present"
    raise KeyError(msg)


def unprocessed_streams(sent_stream_ids: Sequence[int], last_stream_id: int) -> list[int]:
    """Streams a GOAWAY proves were not processed, hence safe to retry elsewhere.

    RFC 9113 Section 6.8 and 8.7: streams with an identifier above the GOAWAY
    ``Last-Stream-ID`` are not or will not be processed.
    """
    return [s for s in sent_stream_ids if s > last_stream_id]


class WireBackend:
    """An HTTP/2 origin that records every (connection, stream) pair that reaches it."""

    def __init__(self, pod_id: str) -> None:
        self.pod_id = pod_id
        self.seen: list[tuple[int, int]] = []
        self._conns: list[H2Connection] = []

    @property
    def connections_accepted(self) -> int:
        """Number of transport connections this origin has accepted."""
        return len(self._conns)

    def accept(self) -> int:
        """Accept a new transport connection and return its local number."""
        self._conns.append(new_h2(client_side=False))
        return len(self._conns) - 1

    def receive(self, conn_no: int, data: bytes) -> bytes:
        """Feed bytes from one connection; answer each request with an empty ``200``."""
        conn = self._conns[conn_no]
        for event in conn.receive_data(data):
            if isinstance(event, RequestReceived):
                self.seen.append((conn_no, event.stream_id))
                conn.send_headers(event.stream_id, [(":status", "200")], end_stream=True)
        return conn.data_to_send()

    def goaway(self, conn_no: int, last_stream_id: int, debug: bytes = b"max_age") -> bytes:
        """Emit a GOAWAY with ``NO_ERROR`` on one connection and return the bytes."""
        conn = self._conns[conn_no]
        conn.close_connection(error_code=0, additional_data=debug, last_stream_id=last_stream_id)
        return conn.data_to_send()


class L4WireFlow:
    """One TCP connection through an L4 balancer: opaque bytes to a single backend.

    The class never decodes a frame; it only appends the bytes it moves, which is
    the whole contract of a transport-level relay.
    """

    def __init__(self, backend: WireBackend) -> None:
        self.backend = backend
        self.conn_no = backend.accept()
        self.bytes_up: list[bytes] = []

    def pump(self, client: H2Connection) -> list[Event]:
        """Move bytes both ways until quiescent; return the events the client saw."""
        events: list[Event] = []
        while True:
            up = client.data_to_send()
            if up:
                self.bytes_up.append(up)
            down = self.backend.receive(self.conn_no, up)
            if down:
                events.extend(client.receive_data(down))
            if not up and not down:
                return events


class L7WireProxy:
    """An L7 HTTP/2 proxy: terminates the client connection, picks a backend per stream.

    One upstream HTTP/2 connection per backend is opened lazily and reused, as in
    Envoy's HTTP/2 connection pool. Upstream stream ids are allocated by the
    upstream connection, so they differ from the ids the client chose.
    """

    def __init__(self, backends: Sequence[WireBackend]) -> None:
        self.backends = list(backends)
        self.downstream = new_h2(client_side=False)
        self.stream_map: list[tuple[str, int, int]] = []
        self._next = 0
        self._upstream: dict[str, tuple[H2Connection, int]] = {}
        self._response_for: dict[tuple[str, int], int] = {}

    def _upstream_for(self, backend: WireBackend) -> tuple[H2Connection, int]:
        if backend.pod_id not in self._upstream:
            self._upstream[backend.pod_id] = (new_h2(client_side=True), backend.accept())
        return self._upstream[backend.pod_id]

    def _flush_upstream(self, backend: WireBackend) -> None:
        conn, conn_no = self._upstream_for(backend)
        while True:
            up = conn.data_to_send()
            down = backend.receive(conn_no, up)
            if not up and not down:
                return
            for event in conn.receive_data(down) if down else ():
                if isinstance(event, ResponseReceived):
                    downstream_id = self._response_for[(backend.pod_id, event.stream_id)]
                    self.downstream.send_headers(
                        downstream_id, [(":status", "200")], end_stream=True
                    )

    def pump(self, client: H2Connection) -> list[Event]:
        """Move bytes both ways until quiescent; return the events the client saw."""
        events: list[Event] = []
        while True:
            up = client.data_to_send()
            for event in self.downstream.receive_data(up) if up else ():
                if isinstance(event, RequestReceived):
                    backend = self.backends[self._next % len(self.backends)]
                    self._next += 1
                    conn, _ = self._upstream_for(backend)
                    upstream_id = conn.get_next_available_stream_id()
                    path = header_value(event.headers, ":path")
                    conn.send_headers(upstream_id, request_headers(path), end_stream=True)
                    self._response_for[(backend.pod_id, upstream_id)] = event.stream_id
                    self.stream_map.append((backend.pod_id, event.stream_id, upstream_id))
            for backend in self.backends:
                if backend.pod_id in self._upstream:
                    self._flush_upstream(backend)
            down = self.downstream.data_to_send()
            if down:
                events.extend(client.receive_data(down))
            if not up and not down:
                return events
