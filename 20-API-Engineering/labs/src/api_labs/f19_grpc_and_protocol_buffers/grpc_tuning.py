"""gRPC keepalive, flow control, and message-size tuning, checked against grpcio 1.85.1.

Three layers, all deterministic:

* Pure models ported from grpc-core 1.85.1 (`ping_abuse_policy.cc`, `ping_rate_policy.cc`,
  `writing.cc` `NextAllowedPingInterval`, `bdp_estimator.cc`) driven by a `ManualClock`.
* `h2` 4.4.1 state machines for the wire view: a server that answers a ping flood with
  `GOAWAY(ENHANCE_YOUR_CALM, "too_many_pings")`, and a sender stalled by HTTP/2 flow control.
* A thin real `grpc.aio` harness (loopback sockets) that reproduces `RESOURCE_EXHAUSTED` and the
  keepalive GOAWAY so the models are checked against the real runtime.
"""

from __future__ import annotations

import asyncio
import importlib
import struct
import time
from dataclasses import dataclass
from typing import Any, Final, Literal

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.settings

# --- constants (grpc-core 1.85.1 include/grpc/impl/channel_arg_names.h unless noted) ---------
DEFAULT_MAX_RECEIVE_BYTES: Final = 4 * 1024 * 1024
UNLIMITED: Final = -1
HTTP2_DEFAULT_WINDOW: Final = 65_535  # RFC 9113 Section 6.9.2
HTTP2_MAX_WINDOW: Final = 2**31 - 1  # RFC 9113 Section 6.9.1
GRPC_PREFIX_LEN: Final = 5  # PROTOCOL-HTTP2.md: Compressed-Flag (1) + Message-Length (4)
IDLE_PING_INTERVAL_MS: Final = 2 * 60 * 60 * 1000  # ping_abuse_policy.cc, RecvPing...WithoutData
CLIENT_PING_FLOOR_MS: Final = 1_000  # writing.cc NextAllowedPingInterval (client, active)
THROTTLE_WITHOUT_DATA_MS: Final = 60_000  # ping_rate_policy.cc kThrottleIntervalWithoutDataSent
BDP_INITIAL: Final = 65_536  # bdp_estimator.h kInitialBdpDefault
MAX_INITIAL_WINDOW: Final = 1 << 30  # flow_control.h kMaxInitialWindowSize
ANYTHING_GOES_WINDOW: Final = 4 * 1024 * 1024  # flow_control.cc kAnythingGoesWindow floor


class ManualClock:
    """Millisecond clock the caller advances; replaces `Timestamp::Now()` in the models."""

    def __init__(self) -> None:
        self.now_ms = 0

    def advance(self, ms: int) -> None:
        self.now_ms += ms


# --- channel-argument builders ----------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ClientKeepalive:
    """Client-side knobs. Defaults are the grpc-core defaults (doc/keepalive.md)."""

    time_ms: int | None = None  # None: keepalive disabled (INT_MAX)
    timeout_ms: int = 20_000
    permit_without_calls: bool = False
    max_pings_without_data: int = 2

    def options(self) -> list[tuple[str, int]]:
        opts: list[tuple[str, int]] = []
        if self.time_ms is not None:
            opts.append(("grpc.keepalive_time_ms", self.time_ms))
        opts += [
            ("grpc.keepalive_timeout_ms", self.timeout_ms),
            ("grpc.keepalive_permit_without_calls", int(self.permit_without_calls)),
            ("grpc.http2.max_pings_without_data", self.max_pings_without_data),
        ]
        return opts


@dataclass(frozen=True, slots=True)
class ServerPingPolicy:
    """Server-side enforcement knobs (A8 PERMIT_KEEPALIVE_TIME and friends)."""

    min_recv_ping_interval_ms: int = 300_000
    max_ping_strikes: int = 2
    permit_without_calls: bool = False

    def options(self) -> list[tuple[str, int]]:
        return [
            ("grpc.http2.min_ping_interval_without_data_ms", self.min_recv_ping_interval_ms),
            ("grpc.http2.max_ping_strikes", self.max_ping_strikes),
            ("grpc.keepalive_permit_without_calls", int(self.permit_without_calls)),
        ]


def message_limit_options(
    max_receive: int | None = None, max_send: int | None = None
) -> list[tuple[str, int]]:
    """`grpc.max_receive_message_length` / `grpc.max_send_message_length`; -1 means unlimited."""
    opts: list[tuple[str, int]] = []
    if max_receive is not None:
        opts.append(("grpc.max_receive_message_length", max_receive))
    if max_send is not None:
        opts.append(("grpc.max_send_message_length", max_send))
    return opts


# --- ports of the grpc-core ping policies -----------------------------------------------------
class PingAbusePolicy:
    """Server side: port of `Chttp2PingAbusePolicy` (ping_abuse_policy.cc)."""

    def __init__(self, clock: ManualClock, policy: ServerPingPolicy) -> None:
        self._clock = clock
        self._min_interval_ms = max(0, policy.min_recv_ping_interval_ms)
        self._max_strikes = max(0, policy.max_ping_strikes)
        self._last_recv_ms: float = float("-inf")
        self.strikes = 0

    def received_one_ping(self, *, transport_idle: bool) -> bool:
        """Record a ping; True means close the transport (strikes exceeded)."""
        now = self._clock.now_ms
        interval = IDLE_PING_INTERVAL_MS if transport_idle else self._min_interval_ms
        next_allowed = self._last_recv_ms + interval
        self._last_recv_ms = now  # updated for good and bad pings alike
        if next_allowed <= now:
            return False
        self.strikes += 1
        return self.strikes > self._max_strikes and self._max_strikes != 0

    def reset_ping_strikes(self) -> None:
        """Called when the server writes HEADERS or DATA (`grpc_chttp2_reset_ping_clock`)."""
        self._last_recv_ms = float("-inf")
        self.strikes = 0


class PingRatePolicy:
    """Client side: port of `Chttp2PingRatePolicy::RequestSendPing` (ping_rate_policy.cc)."""

    def __init__(self, clock: ManualClock, max_pings_without_data: int) -> None:
        self._clock = clock
        self._max_without_data = max(0, max_pings_without_data)
        self._pings_before_data_required = 0
        self._last_sent_ms: float = float("-inf")

    def request_send_ping(self, next_allowed_interval_ms: int) -> int:
        """0 means send now; a positive value is the wait in ms (`TooSoon`)."""
        now = self._clock.now_ms
        wait = self._last_sent_ms + next_allowed_interval_ms - now
        if wait > 0:
            return int(wait)
        if self._max_without_data != 0 and self._pings_before_data_required == 0:
            wait = self._last_sent_ms + THROTTLE_WITHOUT_DATA_MS - now
            if wait > 0:
                return int(wait)
        return 0

    def sent_ping(self) -> None:
        self._last_sent_ms = self._clock.now_ms
        if self._pings_before_data_required > 0:
            self._pings_before_data_required -= 1

    def reset_pings_before_data_required(self) -> None:
        """Called when the client writes HEADERS or DATA."""
        self._pings_before_data_required = self._max_without_data


@dataclass(frozen=True, slots=True)
class KeepaliveSimulation:
    pings_sent: int
    strikes: int
    goaway_at_ms: int | None


def simulate_keepalive(
    client: ClientKeepalive,
    server: ServerPingPolicy,
    *,
    call_active: bool,
    horizon_ms: int,
) -> KeepaliveSimulation:
    """Predict whether the server sends `GOAWAY(ENHANCE_YOUR_CALM)` within `horizon_ms`.

    Assumes the client wrote HEADERS at t=0 when `call_active` and the server writes nothing
    (a long unary or streaming call with no response yet), so strikes are never reset.
    """
    if client.time_ms is None:
        return KeepaliveSimulation(0, 0, None)
    clock = ManualClock()
    rate = PingRatePolicy(clock, client.max_pings_without_data)
    abuse = PingAbusePolicy(clock, server)
    if call_active:
        rate.reset_pings_before_data_required()
    # The server's own permit flag, not the client's, decides "idle" (frame_ping.cc).
    transport_idle = not server.permit_without_calls and not call_active
    client_may_ping = client.permit_without_calls or call_active
    floor = CLIENT_PING_FLOOR_MS if client_may_ping else IDLE_PING_INTERVAL_MS
    time_ms = max(1, client.time_ms)  # grpc-core clamps keepalive time to >= 1 ms
    next_try = time_ms
    pings = 0
    while next_try <= horizon_ms:
        clock.now_ms = next_try
        if not client_may_ping:  # init_keepalive_ping_locked re-arms the timer, sends nothing
            next_try += time_ms
            continue
        wait = rate.request_send_ping(floor)
        if wait > 0:
            next_try += wait
            continue
        rate.sent_ping()
        pings += 1
        if abuse.received_one_ping(transport_idle=transport_idle):
            return KeepaliveSimulation(pings, abuse.strikes, next_try)
        next_try += time_ms
    return KeepaliveSimulation(pings, abuse.strikes, None)


# --- wire view: GOAWAY on a ping flood, through h2 --------------------------------------------
class PingFloodServer:
    """An `h2` server endpoint that enforces `PingAbusePolicy` like grpc-core does."""

    def __init__(self, clock: ManualClock, policy: ServerPingPolicy, *, streams_open: bool) -> None:
        self.conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
        self.conn.initiate_connection()
        self.abuse = PingAbusePolicy(clock, policy)
        self.transport_idle = not policy.permit_without_calls and not streams_open
        self.closed = False

    def receive(self, data: bytes) -> bytes:
        """Feed client bytes; return the bytes to send back (PING ACKs, then maybe GOAWAY)."""
        for event in self.conn.receive_data(data):
            if (
                isinstance(event, h2.events.PingReceived)
                and not self.closed
                and self.abuse.received_one_ping(transport_idle=self.transport_idle)
            ):
                self.conn.close_connection(
                    error_code=h2.errors.ErrorCodes.ENHANCE_YOUR_CALM,
                    additional_data=b"too_many_pings",
                )
                self.closed = True
        return self.conn.data_to_send()


def drive_ping_flood(
    policy: ServerPingPolicy,
    *,
    ping_interval_ms: int,
    pings: int,
    streams_open: bool,
) -> h2.events.ConnectionTerminated | None:
    """Send `pings` PING frames `ping_interval_ms` apart; return the GOAWAY the client sees."""
    clock = ManualClock()
    server = PingFloodServer(clock, policy, streams_open=streams_open)
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    client.initiate_connection()
    client.receive_data(server.receive(client.data_to_send()))
    for index in range(pings):
        clock.advance(ping_interval_ms)
        client.ping(index.to_bytes(8, "big"))
        reply = server.receive(client.data_to_send())
        for event in client.receive_data(reply):
            if isinstance(event, h2.events.ConnectionTerminated):
                return event
    return None


# --- message framing and limits ---------------------------------------------------------------
def frame_message(payload: bytes, *, compressed: bool = False) -> bytes:
    """gRPC Length-Prefixed-Message: 1-byte flag, 4-byte big-endian length, payload."""
    return struct.pack(">BI", int(compressed), len(payload)) + payload


def check_message_size(
    length: int, limit: int, *, side: Literal["CLIENT", "SERVER"], direction: str
) -> str | None:
    """Port of `CheckPayload` in message_size_filter.cc; `length` excludes the 5-byte prefix."""
    if limit == UNLIMITED or length <= limit:
        return None
    verb = "Sent" if direction == "send" else "Received"
    return f"{side}: {verb} message larger than max ({length} vs. {limit})"


# --- flow control -----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TransferResult:
    rounds: int  # sender stalls plus the final flight; one round is roughly one RTT
    window_updates: int
    bytes_on_wire: int


def transfer_rounds(
    message_len: int, *, stream_window: int, connection_window: int
) -> TransferResult:
    """Push one gRPC message through two `h2` endpoints and count flow-control rounds.

    The receiver advertises `stream_window` as SETTINGS_INITIAL_WINDOW_SIZE and raises the
    connection window to `connection_window`. Each round the sender writes until a window or
    the frame size stops it; the receiver then consumes and acknowledges everything.
    """
    sender = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    receiver = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    sender.initiate_connection()
    receiver.initiate_connection()
    receiver.update_settings({h2.settings.SettingCodes.INITIAL_WINDOW_SIZE: stream_window})
    if connection_window > HTTP2_DEFAULT_WINDOW:
        receiver.increment_flow_control_window(connection_window - HTTP2_DEFAULT_WINDOW)
    sender.receive_data(receiver.data_to_send())
    receiver.receive_data(sender.data_to_send())
    sender.receive_data(receiver.data_to_send())
    sender.send_headers(
        1,
        [
            (":method", "POST"),
            (":path", "/t.S/Echo"),
            (":scheme", "http"),
            (":authority", "x"),
            ("content-type", "application/grpc"),
        ],
    )
    receiver.receive_data(sender.data_to_send())
    body = frame_message(b"\x00" * message_len)
    sent = rounds = updates = 0
    while sent < len(body):
        progressed = False
        while sent < len(body):
            chunk = min(
                len(body) - sent,
                sender.local_flow_control_window(1),
                sender.max_outbound_frame_size,
            )
            if chunk <= 0:
                break
            sender.send_data(1, body[sent : sent + chunk])
            sent += chunk
            progressed = True
        if not progressed:
            raise RuntimeError("sender stalled with no window and no pending update")
        rounds += 1
        for event in receiver.receive_data(sender.data_to_send()):
            if isinstance(event, h2.events.DataReceived):
                receiver.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
        for event in sender.receive_data(receiver.data_to_send()):
            if isinstance(event, h2.events.WindowUpdated):
                updates += 1
    return TransferResult(rounds=rounds, window_updates=updates, bytes_on_wire=sent)


def window_throughput_bytes_per_s(window_bytes: int, rtt_s: float) -> float:
    """Upper bound on one stream: one window per round trip (RFC 9113 Section 5.2.3)."""
    return window_bytes / rtt_s


def next_bdp_estimate(
    accumulator: int, estimate: int, bandwidth: float, best_bandwidth: float
) -> tuple[int, float]:
    """One `BdpEstimator::CompletePing` step (bdp_estimator.cc), minus the probe-delay jitter."""
    if accumulator > 2 * estimate // 3 and bandwidth > best_bandwidth:
        return max(accumulator, estimate * 2), bandwidth
    return estimate, best_bandwidth


def target_initial_window(bdp_estimate: int) -> int:
    """Window core advertises at memory pressure < 0.2 (flow_control.cc, PeriodicUpdate)."""
    wanted = max(ANYTHING_GOES_WINDOW, 2 * bdp_estimate)
    power_of_two = 1 << (wanted - 1).bit_length()
    return min(power_of_two, MAX_INITIAL_WINDOW)


# --- real grpc.aio harness (loopback sockets) -------------------------------------------------
_grpc: Any = importlib.import_module("grpc")
_aio: Any = importlib.import_module("grpc.aio")


@dataclass(frozen=True, slots=True)
class CallOutcome:
    code: str
    details: str
    elapsed_s: float


async def _echo(request: bytes, _context: Any) -> bytes:
    return request


async def _blob(request: bytes, _context: Any) -> bytes:
    return b"x" * int(request)


async def _hold(request: bytes, _context: Any) -> bytes:
    await asyncio.sleep(float(request))
    return b"done"


def _handlers() -> Any:
    wrap = _grpc.unary_unary_rpc_method_handler
    return _grpc.method_handlers_generic_handler(
        "lab.Tuning", {"Echo": wrap(_echo), "Blob": wrap(_blob), "Hold": wrap(_hold)}
    )


async def _start(options: list[tuple[str, int]]) -> tuple[Any, int]:
    server = _aio.server(options=options)
    server.add_generic_rpc_handlers((_handlers(),))
    port: int = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return server, port


async def real_call(
    method: str,
    body: bytes,
    *,
    server_options: list[tuple[str, int]] | None = None,
    client_options: list[tuple[str, int]] | None = None,
    timeout_s: float = 30.0,
) -> CallOutcome:
    """One unary call against a fresh loopback server; never raises on an RPC error."""
    server, port = await _start(server_options or [])
    started = time.monotonic()
    try:
        async with _aio.insecure_channel(f"127.0.0.1:{port}", options=client_options or []) as ch:
            try:
                await asyncio.wait_for(ch.unary_unary(f"/lab.Tuning/{method}")(body), timeout_s)
                return CallOutcome("OK", "", time.monotonic() - started)
            except _aio.AioRpcError as err:
                return CallOutcome(err.code().name, str(err.details()), time.monotonic() - started)
    finally:
        await server.stop(None)


async def real_idle_survives(
    *,
    server_options: list[tuple[str, int]],
    client_options: list[tuple[str, int]],
    watch_s: float,
) -> bool:
    """Open a channel with no RPCs; True if it is still READY after `watch_s` seconds."""
    server, port = await _start(server_options)
    try:
        async with _aio.insecure_channel(f"127.0.0.1:{port}", options=client_options) as ch:
            await asyncio.wait_for(ch.channel_ready(), 5)
            try:
                await asyncio.wait_for(
                    ch.wait_for_state_change(_grpc.ChannelConnectivity.READY), watch_s
                )
            except TimeoutError:
                return True
            return False
    finally:
        await server.stop(None)
