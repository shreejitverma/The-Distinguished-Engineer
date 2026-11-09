"""Deterministic models of the TCP socket lifecycle seen by HTTP API servers and clients.

Covers who enters TIME-WAIT (RFC 9293 Section 3.3.2 and Section 3.6), the ephemeral port
budget that TIME-WAIT consumes, keep-alive probe timing (RFC 9293 Section 3.8.4), listen
backlog clamping, and the Nagle plus delayed ACK stall (RFC 9293 Sections 3.7.4 and 3.8.6.3).
"""

from __future__ import annotations

import socket
from collections import deque
from dataclasses import dataclass
from enum import Enum


class TcpState(Enum):
    """Connection states named in RFC 9293 Section 3.3.2."""

    ESTABLISHED = "ESTABLISHED"
    FIN_WAIT_1 = "FIN-WAIT-1"
    FIN_WAIT_2 = "FIN-WAIT-2"
    CLOSE_WAIT = "CLOSE-WAIT"
    CLOSING = "CLOSING"
    LAST_ACK = "LAST-ACK"
    TIME_WAIT = "TIME-WAIT"
    CLOSED = "CLOSED"


class TcpEvent(Enum):
    """Teardown events from RFC 9293 Figure 5."""

    CLOSE = "user CLOSE"
    RCV_FIN = "rcv FIN"
    RCV_ACK_OF_FIN = "rcv ACK of FIN"
    TIMEOUT_2MSL = "Timeout=2MSL"


_TEARDOWN: dict[tuple[TcpState, TcpEvent], TcpState] = {
    (TcpState.ESTABLISHED, TcpEvent.CLOSE): TcpState.FIN_WAIT_1,
    (TcpState.ESTABLISHED, TcpEvent.RCV_FIN): TcpState.CLOSE_WAIT,
    (TcpState.FIN_WAIT_1, TcpEvent.RCV_ACK_OF_FIN): TcpState.FIN_WAIT_2,
    (TcpState.FIN_WAIT_1, TcpEvent.RCV_FIN): TcpState.CLOSING,
    (TcpState.FIN_WAIT_2, TcpEvent.RCV_FIN): TcpState.TIME_WAIT,
    (TcpState.CLOSING, TcpEvent.RCV_ACK_OF_FIN): TcpState.TIME_WAIT,
    (TcpState.CLOSE_WAIT, TcpEvent.CLOSE): TcpState.LAST_ACK,
    (TcpState.LAST_ACK, TcpEvent.RCV_ACK_OF_FIN): TcpState.CLOSED,
    (TcpState.TIME_WAIT, TcpEvent.TIMEOUT_2MSL): TcpState.CLOSED,
}


def transition(state: TcpState, event: TcpEvent) -> TcpState:
    """Apply one teardown event; raise on a transition Figure 5 does not draw."""
    try:
        return _TEARDOWN[(state, event)]
    except KeyError:
        raise ValueError(f"no teardown transition from {state.value} on {event.value}") from None


def _replay(events: tuple[TcpEvent, ...]) -> tuple[TcpState, ...]:
    path = [TcpState.ESTABLISHED]
    for event in events:
        path.append(transition(path[-1], event))
    return tuple(path)


def normal_close() -> tuple[tuple[TcpState, ...], tuple[TcpState, ...]]:
    """Return (active closer path, passive closer path) for RFC 9293 Figure 12."""
    active = _replay(
        (TcpEvent.CLOSE, TcpEvent.RCV_ACK_OF_FIN, TcpEvent.RCV_FIN, TcpEvent.TIMEOUT_2MSL)
    )
    passive = _replay((TcpEvent.RCV_FIN, TcpEvent.CLOSE, TcpEvent.RCV_ACK_OF_FIN))
    return active, passive


def simultaneous_close() -> tuple[TcpState, ...]:
    """Return the path both peers take in RFC 9293 Figure 13."""
    return _replay(
        (TcpEvent.CLOSE, TcpEvent.RCV_FIN, TcpEvent.RCV_ACK_OF_FIN, TcpEvent.TIMEOUT_2MSL)
    )


@dataclass(frozen=True, slots=True)
class PortRange:
    """An inclusive ephemeral port range."""

    first: int
    last: int

    def __post_init__(self) -> None:
        if not 1 <= self.first <= self.last <= 65535:
            raise ValueError(f"invalid port range {self.first}-{self.last}")

    @property
    def size(self) -> int:
        return self.last - self.first + 1


LINUX_DEFAULT_PORTS = PortRange(32768, 60999)  # ip-sysctl.rst ip_local_port_range
IANA_DYNAMIC_PORTS = PortRange(49152, 65535)  # RFC 6335 Section 6; macOS default
LINUX_TIME_WAIT_SECONDS = 60.0  # include/net/tcp.h TCP_TIMEWAIT_LEN (60*HZ)
XNU_TIME_WAIT_SECONDS = 30.0  # bsd/netinet/tcp_timer.h TCPTV_MSL 15 s, times 2


def max_close_rate(ports: PortRange, time_wait_seconds: float) -> float:
    """Sustained new connections per second one source IP can open to one destination.

    Valid when the client is the active closer: each port stays in TIME-WAIT for
    ``time_wait_seconds`` before the same 4-tuple can be reused.
    """
    if time_wait_seconds <= 0:
        raise ValueError("time_wait_seconds must be positive")
    return ports.size / time_wait_seconds


@dataclass(frozen=True, slots=True)
class PortUsage:
    """Outcome of a port budget simulation."""

    requests: int
    connects: int
    failed_connects: int
    peak_ports_held: int
    first_failure_ms: int | None


def simulate_client_ports(
    *,
    requests_per_second: int,
    duration_ms: int,
    time_wait_ms: int,
    ports: PortRange,
    pooled_connections: int | None,
) -> PortUsage:
    """Simulate one client talking to one ``(dst ip, dst port)``.

    ``pooled_connections=None`` models a client that opens and actively closes one
    connection per request, so every used port sits in TIME-WAIT for ``time_wait_ms``.
    An integer models a keep-alive pool that opens at most that many connections and
    keeps them for the whole run.  A connect fails (``EADDRNOTAVAIL`` on Linux) when no
    port in ``ports`` is free.
    """
    if requests_per_second <= 0 or duration_ms <= 0 or time_wait_ms <= 0:
        raise ValueError("rate, duration, and TIME-WAIT must be positive")
    total = duration_ms * requests_per_second // 1000
    held: deque[int] = deque()  # release time (ms) of each port in TIME-WAIT
    open_conns = 0
    connects = failed = peak = 0
    first_failure: int | None = None
    for i in range(total):
        now = i * 1000 // requests_per_second
        if pooled_connections is not None:
            if open_conns < pooled_connections:
                open_conns += 1
                connects += 1
            peak = max(peak, open_conns)
            continue
        while held and held[0] <= now:
            held.popleft()
        if len(held) >= ports.size:
            failed += 1
            if first_failure is None:
                first_failure = now
            continue
        connects += 1
        held.append(now + time_wait_ms)
        peak = max(peak, len(held))
    return PortUsage(total, connects, failed, peak, first_failure)


@dataclass(frozen=True, slots=True)
class KeepAlive:
    """TCP keep-alive tuning: idle time, probe interval, and probe count."""

    idle_seconds: int
    interval_seconds: int
    probes: int

    def probe_times(self) -> tuple[int, ...]:
        """Seconds after the last received segment at which each probe is sent."""
        return tuple(self.idle_seconds + k * self.interval_seconds for k in range(self.probes))

    def dead_peer_after(self) -> int:
        """Seconds of silence before the kernel reports ``ETIMEDOUT``."""
        return self.idle_seconds + self.interval_seconds * self.probes


LINUX_KEEPALIVE_DEFAULTS = KeepAlive(7200, 75, 9)  # include/net/tcp.h TCP_KEEPALIVE_*
XNU_KEEPALIVE_DEFAULTS = KeepAlive(7200, 75, 8)  # bsd/netinet/tcp_timer.h TCPTV_KEEP*


def keepalive_option_names(platform: str) -> tuple[str, str, str]:
    """``socket`` module names for (idle, interval, count) on ``sys.platform``."""
    if platform.startswith("linux"):
        return ("TCP_KEEPIDLE", "TCP_KEEPINTVL", "TCP_KEEPCNT")
    if platform == "darwin":
        return ("TCP_KEEPALIVE", "TCP_KEEPINTVL", "TCP_KEEPCNT")
    raise NotImplementedError(f"keep-alive option names not modelled for {platform}")


def keepalive_socket_options(platform: str, cfg: KeepAlive) -> list[tuple[int, int, int]]:
    """``setsockopt`` triples that enable keep-alive with ``cfg`` on ``platform``."""
    idle, interval, count = (getattr(socket, n) for n in keepalive_option_names(platform))
    return [
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        (socket.IPPROTO_TCP, idle, cfg.idle_seconds),
        (socket.IPPROTO_TCP, interval, cfg.interval_seconds),
        (socket.IPPROTO_TCP, count, cfg.probes),
    ]


def effective_backlog(requested: int, somaxconn: int, *, platform: str) -> int:
    """The listen queue limit the kernel actually stores for ``listen(requested)``.

    Linux compares ``(unsigned int)backlog > somaxconn``, so a negative value is capped to
    ``somaxconn`` too (net/socket.c, __sys_listen_socket); XNU maps ``backlog <= 0`` to
    ``somaxconn`` (bsd/kern/uipc_socket.c, solisten).  CPython's ``socket.listen`` raises a
    negative argument to ``0`` before the system call (Modules/socketmodule.c, sock_listen).
    """
    if platform.startswith("linux"):
        return somaxconn if requested < 0 or requested > somaxconn else requested
    if platform == "darwin":
        return somaxconn if requested <= 0 or requested > somaxconn else requested
    raise NotImplementedError(f"backlog clamping not modelled for {platform}")


def linux_accept_queue_capacity(backlog: int) -> int:
    """Linux holds ``backlog + 1`` sockets: sk_acceptq_is_full tests ``>``, not ``>=``."""
    return backlog + 1


def write_write_read_ms(*, rtt_ms: float, delayed_ack_ms: float, nodelay: bool) -> float:
    """Latency of a request sent as two small writes when the peer answers only after both.

    With Nagle on, the second write waits until the first is ACKed (RFC 9293 Section 3.7.4);
    the receiver has nothing to piggyback on, so it delays that ACK (Section 3.8.6.3).
    """
    if nodelay:
        return rtt_ms
    first_acked = rtt_ms + delayed_ack_ms
    return first_acked + rtt_ms


def asyncio_accept_gets_nodelay(*, listener_proto: int, has_so_protocol: bool) -> bool:
    """Whether a stdlib asyncio transport for an accepted socket ends up with TCP_NODELAY.

    ``asyncio.base_events._set_nodelay`` requires ``sock.proto == IPPROTO_TCP``.
    ``socket.accept`` copies the listener's ``proto``, but CPython's ``sock_initobj_impl``
    overwrites it with ``0`` when the platform lacks ``SO_PROTOCOL`` (macOS).
    """
    accepted_proto = listener_proto if has_so_protocol else 0
    return accepted_proto == socket.IPPROTO_TCP
