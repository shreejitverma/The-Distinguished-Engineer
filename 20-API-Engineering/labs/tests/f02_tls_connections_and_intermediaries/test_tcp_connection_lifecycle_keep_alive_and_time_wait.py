"""Lab: TCP socket lifecycle for API servers (backlog, keep-alive probes, Nagle, TIME-WAIT)."""

from __future__ import annotations

import asyncio
import errno
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, ClassVar

import httpx
import pytest
import uvicorn
import uvloop

from api_labs.f02_tls_connections_and_intermediaries.tcp_socket_lifecycle import (
    IANA_DYNAMIC_PORTS,
    LINUX_DEFAULT_PORTS,
    LINUX_KEEPALIVE_DEFAULTS,
    LINUX_TIME_WAIT_SECONDS,
    XNU_KEEPALIVE_DEFAULTS,
    XNU_TIME_WAIT_SECONDS,
    KeepAlive,
    PortRange,
    TcpEvent,
    TcpState,
    asyncio_accept_gets_nodelay,
    effective_backlog,
    keepalive_option_names,
    keepalive_socket_options,
    linux_accept_queue_capacity,
    max_close_rate,
    normal_close,
    simulate_client_ports,
    simultaneous_close,
    transition,
    write_write_read_ms,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

# --- RFC 9293 teardown state machine -------------------------------------------------


@pytest.mark.unit
def test_only_the_active_closer_enters_time_wait() -> None:
    active, passive = normal_close()
    assert active == (
        TcpState.ESTABLISHED,
        TcpState.FIN_WAIT_1,
        TcpState.FIN_WAIT_2,
        TcpState.TIME_WAIT,
        TcpState.CLOSED,
    )
    assert passive == (
        TcpState.ESTABLISHED,
        TcpState.CLOSE_WAIT,
        TcpState.LAST_ACK,
        TcpState.CLOSED,
    )
    assert TcpState.TIME_WAIT not in passive


@pytest.mark.unit
def test_simultaneous_close_puts_both_peers_in_time_wait_via_closing() -> None:
    path = simultaneous_close()
    assert path[1:4] == (TcpState.FIN_WAIT_1, TcpState.CLOSING, TcpState.TIME_WAIT)
    assert path[-1] is TcpState.CLOSED


@pytest.mark.unit
def test_undrawn_transition_is_rejected() -> None:
    with pytest.raises(ValueError, match="CLOSE-WAIT on rcv FIN"):
        transition(TcpState.CLOSE_WAIT, TcpEvent.RCV_FIN)


# --- Ephemeral port budget -----------------------------------------------------------


@pytest.mark.unit
def test_default_ephemeral_ranges_and_close_rate_ceiling() -> None:
    assert LINUX_DEFAULT_PORTS.size == 28232
    assert IANA_DYNAMIC_PORTS.size == 16384
    linux_rate = max_close_rate(LINUX_DEFAULT_PORTS, LINUX_TIME_WAIT_SECONDS)
    xnu_rate = max_close_rate(IANA_DYNAMIC_PORTS, XNU_TIME_WAIT_SECONDS)
    assert linux_rate == pytest.approx(470.53, 1e-4)
    assert xnu_rate == pytest.approx(546.13, 1e-4)
    with pytest.raises(ValueError, match="invalid port range"):
        PortRange(60999, 32768)


@pytest.mark.unit
def test_connection_per_request_exhausts_ports_and_pool_does_not() -> None:
    common = {"requests_per_second": 600, "duration_ms": 60_000, "time_wait_ms": 30_000}
    churn = simulate_client_ports(**common, ports=IANA_DYNAMIC_PORTS, pooled_connections=None)
    # 16384 ports at 600 connects/s are gone after request #16384, at 27306 ms.
    assert churn.first_failure_ms == 27_306
    assert churn.peak_ports_held == IANA_DYNAMIC_PORTS.size
    assert churn.failed_connects > 0
    assert churn.connects + churn.failed_connects == churn.requests == 36_000

    pooled = simulate_client_ports(**common, ports=IANA_DYNAMIC_PORTS, pooled_connections=4)
    assert pooled.connects == 4
    assert pooled.failed_connects == 0
    assert pooled.first_failure_ms is None


@pytest.mark.unit
def test_churn_below_the_ceiling_never_fails_and_holds_rate_times_time_wait() -> None:
    usage = simulate_client_ports(
        requests_per_second=500,
        duration_ms=90_000,
        time_wait_ms=30_000,
        ports=IANA_DYNAMIC_PORTS,
        pooled_connections=None,
    )
    assert usage.failed_connects == 0
    assert usage.peak_ports_held == 500 * 30


@pytest.mark.unit
def test_model_inputs_outside_their_domain_are_rejected() -> None:
    with pytest.raises(ValueError, match="time_wait_seconds"):
        max_close_rate(IANA_DYNAMIC_PORTS, 0)
    with pytest.raises(ValueError, match="must be positive"):
        simulate_client_ports(
            requests_per_second=0,
            duration_ms=1,
            time_wait_ms=1,
            ports=IANA_DYNAMIC_PORTS,
            pooled_connections=None,
        )
    with pytest.raises(NotImplementedError):
        effective_backlog(128, 128, platform="win32")


# --- Keep-alive probes ---------------------------------------------------------------


@pytest.mark.unit
def test_default_keepalive_detects_a_dead_peer_after_more_than_two_hours() -> None:
    assert LINUX_KEEPALIVE_DEFAULTS.dead_peer_after() == 7200 + 75 * 9 == 7875
    assert XNU_KEEPALIVE_DEFAULTS.dead_peer_after() == 7200 + 75 * 8 == 7800
    tuned = KeepAlive(idle_seconds=30, interval_seconds=10, probes=3)
    assert tuned.probe_times() == (30, 40, 50)
    assert tuned.dead_peer_after() == 60


@pytest.mark.unit
@pytest.mark.parametrize(
    ("platform", "idle_name"),
    [("linux", "TCP_KEEPIDLE"), ("darwin", "TCP_KEEPALIVE")],
    ids=["linux", "darwin"],
)
def test_keepalive_idle_option_name_differs_by_os(platform: str, idle_name: str) -> None:
    assert keepalive_option_names(platform) == (idle_name, "TCP_KEEPINTVL", "TCP_KEEPCNT")


@pytest.mark.unit
def test_keepalive_option_names_refuse_unmodelled_platform() -> None:
    with pytest.raises(NotImplementedError):
        keepalive_option_names("win32")


@pytest.mark.socket
def test_keepalive_options_round_trip_through_the_kernel() -> None:
    cfg = KeepAlive(idle_seconds=30, interval_seconds=10, probes=3)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 0  # MUST-25: off
        options = keepalive_socket_options(sys.platform, cfg)
        for level, name, value in options:
            sock.setsockopt(level, name, value)
        keepalive_on = sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
        tuning = [sock.getsockopt(level, name) for level, name, _ in options[1:]]
    assert keepalive_on
    assert tuning == [30, 10, 3]


# --- Listen backlog ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("requested", "somaxconn", "platform", "expected"),
    [
        (2048, 128, "darwin", 128),
        (2048, 4096, "linux", 2048),
        (2048, 128, "linux", 128),
        (0, 128, "darwin", 128),
        (0, 4096, "linux", 0),
        (-1, 4096, "linux", 4096),
        (-1, 128, "darwin", 128),
    ],
    ids=[
        "uvicorn-on-macos",
        "uvicorn-on-linux-5.4+",
        "uvicorn-on-old-linux",
        "xnu-0",
        "linux-0",
        "linux-negative-is-unsigned",
        "xnu-negative",
    ],
)
def test_listen_backlog_is_silently_clamped(
    requested: int, somaxconn: int, platform: str, expected: int
) -> None:
    assert effective_backlog(requested, somaxconn, platform=platform) == expected


@pytest.mark.unit
def test_linux_accept_queue_holds_one_more_than_backlog() -> None:
    assert linux_accept_queue_capacity(effective_backlog(2048, 4096, platform="linux")) == 2049


@pytest.mark.unit
def test_uvicorn_defaults_backlog_2048_and_keep_alive_5s() -> None:
    async def app(scope: object, receive: object, send: object) -> None:  # pragma: no cover
        return None

    config = uvicorn.Config(app)
    assert (config.backlog, config.timeout_keep_alive) == (2048, 5)


# --- Nagle and delayed ACK -----------------------------------------------------------


@pytest.mark.unit
def test_nagle_plus_delayed_ack_adds_one_rtt_and_one_ack_delay() -> None:
    nagle = write_write_read_ms(rtt_ms=0.1, delayed_ack_ms=40.0, nodelay=False)
    nodelay = write_write_read_ms(rtt_ms=0.1, delayed_ack_ms=40.0, nodelay=True)
    assert nodelay == pytest.approx(0.1)
    assert nagle - nodelay == pytest.approx(0.1 + 40.0)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("listener_proto", "has_so_protocol", "expected"),
    [(6, True, True), (0, True, False), (6, False, False)],
    ids=["linux-getaddrinfo-listener", "linux-socket()-listener", "macos-any-listener"],
)
def test_asyncio_nodelay_on_accept_depends_on_listener_proto(
    listener_proto: int, has_so_protocol: bool, expected: bool
) -> None:
    got = asyncio_accept_gets_nodelay(
        listener_proto=listener_proto, has_so_protocol=has_so_protocol
    )
    assert got is expected


async def _accepted_nodelay(listener: socket.socket | None) -> tuple[int, int, int]:
    """Return (client TCP_NODELAY, accepted proto, accepted TCP_NODELAY) on loopback."""
    seen: list[tuple[int, int]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock: socket.socket = writer.get_extra_info("socket")
        seen.append((sock.proto, sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)))
        writer.close()
        await writer.wait_closed()

    if listener is None:
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
    else:
        server = await asyncio.start_server(handle, sock=listener)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        client_sock: socket.socket = writer.get_extra_info("socket")
        client_nodelay = client_sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
        await reader.read()
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
    assert len(seen) == 1
    return client_nodelay, seen[0][0], seen[0][1]


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("listener_proto", [None, 0], ids=["host-port", "socket()-proto-0"])
async def test_asyncio_server_side_nodelay_matches_the_proto_model(
    listener_proto: int | None,
) -> None:
    listener = None
    if listener_proto is not None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM, listener_proto)
        listener.bind(("127.0.0.1", 0))
    client_nodelay, accepted_proto, accepted_nodelay = await _accepted_nodelay(listener)
    with socket.socket() as raw:
        assert raw.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 0  # kernel default
    assert client_nodelay != 0  # create_connection sockets come from getaddrinfo (proto 6)
    expected = asyncio_accept_gets_nodelay(
        listener_proto=socket.IPPROTO_TCP if listener_proto is None else listener_proto,
        has_so_protocol=hasattr(socket, "SO_PROTOCOL"),
    )
    assert (accepted_proto == socket.IPPROTO_TCP) is expected
    assert (accepted_nodelay != 0) is expected


@pytest.mark.socket
@pytest.mark.asyncio
async def test_nodelay_set_on_the_listener_is_inherited_by_accepted_sockets() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
    listener.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # hypercorn, gunicorn
    listener.bind(("127.0.0.1", 0))
    _, accepted_proto, accepted_nodelay = await _accepted_nodelay(listener)
    assert accepted_proto != socket.IPPROTO_TCP  # asyncio's own _set_nodelay skipped it
    assert accepted_nodelay != 0


@pytest.mark.socket
def test_uvloop_sets_nodelay_on_accepted_sockets_even_for_a_proto_0_listener() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
    listener.bind(("127.0.0.1", 0))
    loop = uvloop.new_event_loop()
    try:
        _, _, accepted_nodelay = loop.run_until_complete(_accepted_nodelay(listener))
    finally:
        loop.close()
    assert accepted_nodelay != 0


@pytest.mark.socket
def test_uvicorn_bind_socket_listener_has_proto_0() -> None:
    async def app(scope: object, receive: object, send: object) -> None:  # pragma: no cover
        return None

    sock = uvicorn.Config(app, host="127.0.0.1", port=0).bind_socket()
    with sock:
        assert sock.proto == 0  # used for --workers and --reload; see the proto model above


# --- TIME-WAIT on real loopback sockets ----------------------------------------------


def _bind_error(port: int, *, reuse: bool) -> int | None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if reuse:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            return exc.errno
    return None


def _closed_listener_port(*, server_closes_first: bool, listener_reuse: bool = True) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if listener_reuse:  # asyncio.create_server and uvicorn both set this on POSIX
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port: int = listener.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port))
        accepted, _ = listener.accept()
        first, second = (accepted, client) if server_closes_first else (client, accepted)
        first.close()
        assert second.recv(1) == b""  # FIN arrived: second is now the passive closer
        second.close()
    return port


def _bind_error_after_settle(port: int, *, reuse: bool) -> int | None:
    """Poll for 0.3 s so a passive closer's brief LAST-ACK cannot pass for TIME-WAIT.

    0.3 s is far below TIME-WAIT (30 s on XNU, 60 s on Linux) and far above a loopback ACK.
    """
    deadline = time.monotonic() + 0.3
    while (err := _bind_error(port, reuse=reuse)) is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    return err


@pytest.mark.socket
def test_server_side_active_close_blocks_plain_rebind_until_so_reuseaddr() -> None:
    port = _closed_listener_port(server_closes_first=True)
    assert _bind_error_after_settle(port, reuse=False) == errno.EADDRINUSE
    assert _bind_error(port, reuse=True) is None


@pytest.mark.socket
def test_linux_also_requires_so_reuseaddr_on_the_old_listener() -> None:
    port = _closed_listener_port(server_closes_first=True, listener_reuse=False)
    expected = errno.EADDRINUSE if sys.platform.startswith("linux") else None
    assert _bind_error_after_settle(port, reuse=True) == expected


@pytest.mark.socket
def test_client_side_active_close_leaves_server_port_free() -> None:
    port = _closed_listener_port(server_closes_first=False)
    assert _bind_error_after_settle(port, reuse=False) is None


# --- httpx pooling vs connection-per-request -----------------------------------------


class _PortRecorder(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    client_ports: ClassVar[list[int]] = []

    def do_GET(self) -> None:
        type(self).client_ports.append(self.client_address[1])
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.fixture
def http_origin() -> Iterator[tuple[str, list[int]]]:
    _PortRecorder.client_ports = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PortRecorder)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/", _PortRecorder.client_ports
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.socket
def test_httpx_pool_reuses_one_ephemeral_port(http_origin: tuple[str, list[int]]) -> None:
    url, ports = http_origin
    with httpx.Client() as client:
        for _ in range(5):
            assert client.get(url).status_code == 200
    assert len(ports) == 5
    assert len(set(ports)) == 1


@pytest.mark.socket
def test_httpx_without_keepalive_burns_a_port_per_request_into_time_wait(
    http_origin: tuple[str, list[int]],
) -> None:
    url, ports = http_origin
    with httpx.Client(limits=httpx.Limits(max_keepalive_connections=0)) as client:
        for _ in range(5):
            assert client.get(url).status_code == 200
    assert len(set(ports)) == 5
    # The client closed first, so its side of every 4-tuple is in TIME-WAIT.  Settle on the
    # newest port; the older ones closed earlier and have had longer to leave LAST-ACK.
    assert _bind_error_after_settle(ports[-1], reuse=False) == errno.EADDRINUSE
    assert all(_bind_error(port, reuse=False) == errno.EADDRINUSE for port in ports[:-1])
