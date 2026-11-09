"""HTTP/1.1 pipelining order and application-layer head-of-line blocking (RFC 9112 9.3.2)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import h2.config
import h2.connection
import h2.events
import h11

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


class Schedule(Enum):
    """How one connection turns a burst of requests into response completion times."""

    SERIAL = "serial"  # one request in flight at a time: h11, Uvicorn, Hypercorn
    PARALLEL_IN_ORDER = "parallel_in_order"  # RFC 9112 Section 9.3.2 parallel safe requests
    MULTIPLEXED = "multiplexed"  # HTTP/2 streams: each response completes on its own


@dataclass(frozen=True, slots=True)
class PipelineTrace:
    """What an h11 server connection did with a burst of pipelined requests."""

    dispatch_order: tuple[bytes, ...]
    paused_with_buffered_bytes: tuple[int, ...]
    wire_response: bytes


def serve_pipelined_h11(wire: bytes, respond: Callable[[h11.Request], bytes]) -> PipelineTrace:
    """Feed one burst of pipelined requests to an h11 server and answer them one cycle at a time.

    h11 returns ``PAUSED`` once the client half of a cycle is ``DONE`` while bytes of the next
    request are still buffered, so a request is released only after ``start_next_cycle``.
    """
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(wire)
    dispatched: list[bytes] = []
    paused: list[int] = []
    out: list[bytes] = []
    while True:
        event = conn.next_event()
        if not isinstance(event, h11.Request):
            break  # NEED_DATA: every buffered request has been answered
        dispatched.append(event.target)
        end = conn.next_event()
        if not isinstance(end, h11.EndOfMessage):
            raise ValueError(f"request bodies are out of scope for this lab: {end!r}")
        body = respond(event)
        out.append(conn.send(h11.Response(status_code=200, headers=_length(body))) or b"")
        out.append(conn.send(h11.Data(data=body)) or b"")
        out.append(conn.send(h11.EndOfMessage()) or b"")
        if conn.our_state is not h11.DONE or conn.their_state is not h11.DONE:
            break
        buffered = len(conn.trailing_data[0])
        if conn.next_event() is h11.PAUSED:
            paused.append(buffered)
        conn.start_next_cycle()
    return PipelineTrace(tuple(dispatched), tuple(paused), b"".join(out))


def _length(body: bytes) -> list[tuple[bytes, bytes]]:
    return [(b"content-length", str(len(body)).encode("ascii"))]


def completion_times(
    service_ms: Sequence[int], schedule: Schedule, *, connections: int = 1
) -> tuple[int, ...]:
    """Return when each response finishes for a burst sent at t=0, ignoring network transfer.

    Requests are spread round-robin over *connections*; each connection applies *schedule*.
    """
    if connections < 1:
        raise ValueError("connections must be >= 1")
    if any(s < 0 for s in service_ms):
        raise ValueError("service times must be non-negative")
    finish = [0] * len(service_ms)
    for lane in range(connections):
        indices = range(lane, len(service_ms), connections)
        clock = 0
        for i in indices:
            if schedule is Schedule.SERIAL:
                clock += service_ms[i]
            elif schedule is Schedule.PARALLEL_IN_ORDER:
                clock = max(clock, service_ms[i])
            else:
                clock = service_ms[i]
            finish[i] = clock
    return tuple(finish)


def nearest_rank(values: Sequence[int], percentile: float) -> int:
    """Nearest-rank percentile: the smallest value with at least *percentile* % at or below it."""
    if not values or not 0 < percentile <= 100:
        raise ValueError("need values and 0 < percentile <= 100")
    ordered = sorted(values)
    return ordered[math.ceil(percentile / 100 * len(ordered)) - 1]


def h2_exchange(
    targets: Sequence[bytes], server_finish_order: Sequence[int]
) -> tuple[tuple[int, bytes], ...]:
    """Send *targets* as concurrent HTTP/2 streams and let the server finish them in any order.

    *server_finish_order* lists indices into *targets*. Returns ``(stream_id, body)`` in the order
    the client saw each ``StreamEnded``; the body echoes the target so correlation is checkable.
    """
    if sorted(server_finish_order) != list(range(len(targets))):
        raise ValueError("server_finish_order must be a permutation of target indices")
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    stream_ids: list[int] = []
    for target in targets:
        stream_id = client.get_next_available_stream_id()
        client.send_headers(
            stream_id,
            [
                (b":method", b"GET"),
                (b":scheme", b"http"),
                (b":authority", b"t"),
                (b":path", target),
            ],
            end_stream=True,
        )
        stream_ids.append(stream_id)
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    for index in server_finish_order:
        body = targets[index]
        server.send_headers(stream_ids[index], [(b":status", b"200"), *_length(body)])
        server.send_data(stream_ids[index], body, end_stream=True)
    received: dict[int, bytearray] = {}
    finished: list[tuple[int, bytes]] = []
    for event in client.receive_data(server.data_to_send()):
        if isinstance(event, h2.events.DataReceived):
            received.setdefault(event.stream_id, bytearray()).extend(event.data or b"")
        elif isinstance(event, h2.events.StreamEnded):
            finished.append((event.stream_id, bytes(received.get(event.stream_id, b""))))
    return tuple(finished)
