"""Keep-alive idle timeout race between a reverse proxy and an HTTP/1.1 origin.

The proxy holds an idle upstream connection in its pool; the origin runs its own
idle timer on the same connection. HTTP/1.1 gives the origin no way to say "I am
about to close" on an idle connection, so the close is a bare FIN. If the proxy
writes a request before that FIN reaches it, the request crosses the FIN on the
wire, the origin never reads it, and the proxy sees EOF instead of a response.

All times are integer microseconds so the window arithmetic is exact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

import h11

SECOND_US = 1_000_000
MILLISECOND_US = 1_000

# RFC 9110 Section 9.2.2: PUT, DELETE, and the safe methods are idempotent.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"})


class Outcome(StrEnum):
    """What happens when the proxy wants to reuse an idle upstream connection."""

    REUSED = "reused"  # request reached the origin while its socket was open
    PROXY_EXPIRED = "proxy_expired"  # proxy's own idle timer evicted it first
    STALE_DETECTED = "stale_detected"  # origin FIN arrived before dispatch
    RACE_LOST = "race_lost"  # request crossed the origin FIN on the wire


@dataclass(frozen=True, slots=True)
class Timeouts:
    """Idle timeouts on both ends of one upstream connection, plus path delay."""

    proxy_idle_us: int
    origin_idle_us: int
    one_way_delay_us: int

    def __post_init__(self) -> None:
        if self.proxy_idle_us <= 0 or self.origin_idle_us <= 0:
            raise ValueError("idle timeouts must be positive")
        if self.one_way_delay_us < 0:
            raise ValueError("one-way delay must be non-negative")

    @property
    def rtt_us(self) -> int:
        return 2 * self.one_way_delay_us


def classify_reuse(t: Timeouts, idle_gap_us: int) -> Outcome:
    """Classify a reuse attempt `idle_gap_us` after the proxy read the last response.

    Time 0 is when the origin wrote the last response byte and armed its idle
    timer. The proxy reads that byte at d and arms its own timer then, so the
    two timers are offset by one one-way delay d. The proxy dispatches at
    d + gap; the request reaches the origin at gap + 2d. The origin FIN leaves
    at origin_idle and reaches the proxy at origin_idle + d.
    """
    if idle_gap_us < 0:
        raise ValueError("idle gap must be non-negative")
    if idle_gap_us >= t.proxy_idle_us:
        return Outcome.PROXY_EXPIRED
    if idle_gap_us >= t.origin_idle_us:
        return Outcome.STALE_DETECTED
    if idle_gap_us + t.rtt_us < t.origin_idle_us:
        return Outcome.REUSED
    # A tie (arrival exactly when the origin timer fires) counts as lost:
    # the origin's close and the arriving bytes are not ordered by anything.
    return Outcome.RACE_LOST


def race_window(t: Timeouts) -> tuple[int, int] | None:
    """Return the half-open gap interval [lo, hi) that loses the race, or None."""
    lo = max(0, t.origin_idle_us - t.rtt_us)
    hi = min(t.origin_idle_us, t.proxy_idle_us)
    return (lo, hi) if lo < hi else None


def is_race_free(t: Timeouts) -> bool:
    """The rule: origin idle >= proxy idle + one round trip closes the window."""
    return t.origin_idle_us >= t.proxy_idle_us + t.rtt_us


def poisson_race_probability(t: Timeouts, requests_per_second: float) -> float:
    """Probability that one reuse loses, for Poisson arrivals on one connection.

    With exponential inter-arrival gaps at rate r, P(lo <= gap < hi) is
    exp(-r * lo) - exp(-r * hi).
    """
    if requests_per_second <= 0:
        raise ValueError("rate must be positive")
    window = race_window(t)
    if window is None:
        return 0.0
    lo, hi = window
    rate_per_us = requests_per_second / SECOND_US
    return math.exp(-rate_per_us * lo) - math.exp(-rate_per_us * hi)


@dataclass(frozen=True, slots=True)
class ProxyResult:
    status: int
    upstream_attempts: int


def proxy_response(
    method: str, outcome: Outcome, *, retry_non_idempotent: bool = False
) -> ProxyResult:
    """Status the client sees, applying RFC 9110 Section 9.2.2 retry rules.

    A proxy MUST NOT automatically retry non-idempotent requests, so a lost
    race on `POST` surfaces as `502 Bad Gateway`. `retry_non_idempotent`
    models an override such as nginx `proxy_next_upstream non_idempotent`.
    """
    if outcome is not Outcome.RACE_LOST:
        return ProxyResult(status=200, upstream_attempts=1)
    if method.upper() in IDEMPOTENT_METHODS or retry_non_idempotent:
        return ProxyResult(status=200, upstream_attempts=2)
    return ProxyResult(status=502, upstream_attempts=1)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _send(conn: h11.Connection, event: h11.Event) -> bytes:
    data = conn.send(event)
    if data is None:
        raise RuntimeError(f"{event!r} asked to close the transport")
    return data


def _pump(sender: h11.Connection, receiver: h11.Connection, event: h11.Event) -> bytes:
    data = _send(sender, event)
    if data:  # b"" would be read as EOF by the receiver
        receiver.receive_data(data)
    return data


def _complete_one_exchange(proxy: h11.Connection, origin: h11.Connection) -> None:
    """Run one GET to completion and put both sides back in IDLE."""
    _pump(proxy, origin, h11.Request(method="GET", target="/warm", headers=[("Host", "o")]))
    _pump(proxy, origin, h11.EndOfMessage())
    _require(isinstance(origin.next_event(), h11.Request), "origin missed the request")
    _require(isinstance(origin.next_event(), h11.EndOfMessage), "origin missed end of request")
    _pump(origin, proxy, h11.Response(status_code=200, headers=[("Content-Length", "2")]))
    _pump(origin, proxy, h11.Data(data=b"ok"))
    _pump(origin, proxy, h11.EndOfMessage())
    _require(isinstance(proxy.next_event(), h11.Response), "proxy missed the response")
    _require(isinstance(proxy.next_event(), h11.Data), "proxy missed the body")
    _require(isinstance(proxy.next_event(), h11.EndOfMessage), "proxy missed end of response")
    proxy.start_next_cycle()
    origin.start_next_cycle()


@dataclass(frozen=True, slots=True)
class LostRaceTrace:
    request_bytes_written: int
    origin_events_seen: int
    proxy_error: str


def replay_lost_race(method: str, body: bytes) -> LostRaceTrace:
    """Replay the losing interleaving with real h11 state machines.

    The origin's idle timer fires first (what uvicorn's
    `timeout_keep_alive_handler` does: send `ConnectionClosed` and close the
    transport). The proxy has not seen the FIN yet, so its pool still holds an
    IDLE connection and it writes a full request. The closed origin never
    reads those bytes; the proxy's next read returns EOF.
    """
    proxy = h11.Connection(h11.CLIENT)
    origin = h11.Connection(h11.SERVER)
    _complete_one_exchange(proxy, origin)

    # `send(ConnectionClosed())` returns None, which means: close the socket.
    _require(origin.send(h11.ConnectionClosed()) is None, "origin did not close")
    _require(proxy.our_state is h11.IDLE, "proxy should still think the connection is idle")

    headers = [("Host", "o"), ("Content-Length", str(len(body)))]
    written = len(_send(proxy, h11.Request(method=method, target="/orders", headers=headers)))
    written += len(_send(proxy, h11.Data(data=body)))
    written += len(_send(proxy, h11.EndOfMessage()))
    # The origin transport is closed, so the bytes are dropped by the kernel;
    # nothing is fed to `origin`, which therefore records zero new events.
    proxy.receive_data(b"")  # the FIN finally arrives
    try:
        proxy.next_event()
    except h11.RemoteProtocolError as exc:
        return LostRaceTrace(written, origin_events_seen=0, proxy_error=str(exc))
    raise RuntimeError("h11 accepted EOF while waiting for a response")


def stale_connection_is_rejected() -> tuple[str, str, bool]:
    """When the FIN arrives before dispatch, h11 refuses to send on the connection.

    Returns (proxy our_state, proxy their_state, request refused) after the FIN
    is processed. The states are read before the send attempt, because a
    refused send moves h11's own state to ERROR.
    """
    proxy = h11.Connection(h11.CLIENT)
    origin = h11.Connection(h11.SERVER)
    _complete_one_exchange(proxy, origin)
    _require(origin.send(h11.ConnectionClosed()) is None, "origin did not close")
    proxy.receive_data(b"")
    _require(isinstance(proxy.next_event(), h11.ConnectionClosed), "proxy missed the FIN")
    states = (repr(proxy.our_state), repr(proxy.their_state))
    try:
        proxy.send(h11.Request(method="POST", target="/orders", headers=[("Host", "o")]))
    except h11.LocalProtocolError:
        return (*states, True)
    return (*states, False)
