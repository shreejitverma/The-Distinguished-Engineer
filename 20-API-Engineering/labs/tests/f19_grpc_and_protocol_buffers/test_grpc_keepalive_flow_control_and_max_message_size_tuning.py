"""Lab for the gRPC tuning note: keepalive strikes, message limits, and stream flow control."""

from __future__ import annotations

import asyncio
import struct

import h2.errors
import pytest

from api_labs.f19_grpc_and_protocol_buffers.grpc_tuning import (
    DEFAULT_MAX_RECEIVE_BYTES,
    HTTP2_DEFAULT_WINDOW,
    UNLIMITED,
    ClientKeepalive,
    ManualClock,
    PingAbusePolicy,
    PingRatePolicy,
    ServerPingPolicy,
    check_message_size,
    drive_ping_flood,
    frame_message,
    message_limit_options,
    next_bdp_estimate,
    real_call,
    real_idle_survives,
    simulate_keepalive,
    target_initial_window,
    transfer_rounds,
    window_throughput_bytes_per_s,
)

MIB = 1024 * 1024
STRICT = ServerPingPolicy(min_recv_ping_interval_ms=10_000)
EAGER = ClientKeepalive(time_ms=50, timeout_ms=1_000, max_pings_without_data=0)
EAGER_IDLE = ClientKeepalive(
    time_ms=50, timeout_ms=1_000, max_pings_without_data=0, permit_without_calls=True
)
OVER_LIMIT = DEFAULT_MAX_RECEIVE_BYTES + 1


def _pings_until_close(
    policy: PingAbusePolicy, clock: ManualClock, *, idle: bool, step: int
) -> int:
    """Number of pings (1-based) after which the policy says close; 0 if it never does."""
    for count in range(1, 21):
        clock.advance(step)
        if policy.received_one_ping(transport_idle=idle):
            return count
    return 0


@pytest.mark.unit
def test_grpc_message_prefix_is_five_bytes_and_not_counted_against_the_limit() -> None:
    framed = frame_message(b"abc")
    assert framed == b"\x00\x00\x00\x00\x03abc"
    assert struct.unpack(">BI", framed[:5]) == (0, 3)
    limit = DEFAULT_MAX_RECEIVE_BYTES
    assert check_message_size(limit, limit, side="SERVER", direction="recv") is None
    over = check_message_size(OVER_LIMIT, limit, side="SERVER", direction="recv")
    assert over == "SERVER: Received message larger than max (4194305 vs. 4194304)"
    assert check_message_size(10**9, UNLIMITED, side="CLIENT", direction="send") is None


@pytest.mark.unit
def test_option_builders_emit_the_exact_channel_argument_keys() -> None:
    client = ClientKeepalive(time_ms=30_000, permit_without_calls=True).options()
    assert ("grpc.keepalive_time_ms", 30_000) in client
    assert ("grpc.keepalive_permit_without_calls", 1) in client
    assert ("grpc.http2.max_pings_without_data", 2) in client
    assert not any(key == "grpc.keepalive_time_ms" for key, _ in ClientKeepalive().options())
    server = dict(ServerPingPolicy().options())
    assert server["grpc.http2.min_ping_interval_without_data_ms"] == 300_000
    assert server["grpc.http2.max_ping_strikes"] == 2
    assert message_limit_options(max_receive=-1, max_send=8) == [
        ("grpc.max_receive_message_length", -1),
        ("grpc.max_send_message_length", 8),
    ]


@pytest.mark.unit
def test_server_closes_on_the_fourth_too_soon_ping_not_the_third() -> None:
    clock = ManualClock()
    policy = PingAbusePolicy(clock, STRICT)
    # Ping 1 is free (no previous ping); pings 2 and 3 are strikes 1 and 2; ping 4 is strike 3 > 2.
    assert _pings_until_close(policy, clock, idle=False, step=1_000) == 4
    assert policy.strikes == 3


@pytest.mark.unit
def test_a_ping_after_the_permitted_interval_is_not_a_strike() -> None:
    clock = ManualClock()
    policy = PingAbusePolicy(clock, STRICT)
    assert _pings_until_close(policy, clock, idle=False, step=10_000) == 0
    assert policy.strikes == 0


@pytest.mark.unit
def test_idle_transport_uses_two_hours_even_when_the_policy_interval_is_tiny() -> None:
    tiny = ServerPingPolicy(min_recv_ping_interval_ms=10)
    clock = ManualClock()
    assert _pings_until_close(PingAbusePolicy(clock, tiny), clock, idle=False, step=1_000) == 0
    clock = ManualClock()
    assert _pings_until_close(PingAbusePolicy(clock, tiny), clock, idle=True, step=1_000) == 4


@pytest.mark.unit
def test_max_ping_strikes_zero_tolerates_every_ping() -> None:
    clock = ManualClock()
    never = ServerPingPolicy(min_recv_ping_interval_ms=10_000, max_ping_strikes=0)
    policy = PingAbusePolicy(clock, never)
    assert _pings_until_close(policy, clock, idle=False, step=1) == 0
    assert policy.strikes == 19


@pytest.mark.unit
def test_server_writing_data_resets_the_strike_count() -> None:
    clock = ManualClock()
    policy = PingAbusePolicy(clock, STRICT)
    for _ in range(3):
        clock.advance(1_000)
        assert not policy.received_one_ping(transport_idle=False)
    assert policy.strikes == 2
    policy.reset_ping_strikes()
    clock.advance(1_000)
    assert not policy.received_one_ping(transport_idle=False)
    assert policy.strikes == 0


@pytest.mark.unit
def test_client_stops_after_two_data_less_pings_unless_the_limit_is_zero() -> None:
    clock = ManualClock()
    limited = PingRatePolicy(clock, max_pings_without_data=2)
    limited.reset_pings_before_data_required()
    waits: list[int] = []
    for _ in range(3):
        clock.advance(1_000)
        wait = limited.request_send_ping(1_000)
        waits.append(wait)
        if wait == 0:
            limited.sent_ping()
    assert waits == [0, 0, 59_000]
    limited.reset_pings_before_data_required()  # a HEADERS or DATA frame was written
    clock.advance(1_000)
    assert limited.request_send_ping(1_000) == 0

    open_clock = ManualClock()
    unlimited = PingRatePolicy(open_clock, max_pings_without_data=0)
    for _ in range(10):
        open_clock.advance(1_000)
        assert unlimited.request_send_ping(1_000) == 0
        unlimited.sent_ping()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("client", "server", "active", "expected_goaway_ms"),
    [
        (EAGER, STRICT, True, 3_050),
        (ClientKeepalive(time_ms=50), STRICT, True, None),
        (EAGER, ServerPingPolicy(min_recv_ping_interval_ms=10), True, None),
        (EAGER_IDLE, ServerPingPolicy(), False, 3_050),
        (
            EAGER_IDLE,
            ServerPingPolicy(min_recv_ping_interval_ms=10, permit_without_calls=True),
            False,
            None,
        ),
        (EAGER, ServerPingPolicy(), False, None),
        (ClientKeepalive(time_ms=None), STRICT, True, None),
        (ClientKeepalive(time_ms=60_000, max_pings_without_data=0), ServerPingPolicy(), True, None),
    ],
)
def test_simulator_predicts_when_the_server_sends_goaway(
    client: ClientKeepalive,
    server: ServerPingPolicy,
    active: bool,
    expected_goaway_ms: int | None,
) -> None:
    result = simulate_keepalive(client, server, call_active=active, horizon_ms=8_000)
    assert result.goaway_at_ms == expected_goaway_ms


@pytest.mark.unit
def test_keepalive_time_below_the_server_interval_strikes_on_every_ping_after_the_first() -> None:
    # 60 s keepalive against the 300 s default server interval.
    result = simulate_keepalive(
        ClientKeepalive(time_ms=60_000, max_pings_without_data=0),
        ServerPingPolicy(),
        call_active=True,
        horizon_ms=300_000,
    )
    assert result.goaway_at_ms == 240_000
    assert result.strikes == 3


@pytest.mark.unit
def test_goaway_on_the_wire_is_enhance_your_calm_with_too_many_pings_debug_data() -> None:
    event = drive_ping_flood(STRICT, ping_interval_ms=1_000, pings=4, streams_open=True)
    assert event is not None
    assert event.error_code == h2.errors.ErrorCodes.ENHANCE_YOUR_CALM
    assert int(event.error_code) == 0x0B
    assert event.additional_data == b"too_many_pings"
    assert drive_ping_flood(STRICT, ping_interval_ms=1_000, pings=3, streams_open=True) is None
    assert drive_ping_flood(STRICT, ping_interval_ms=10_000, pings=20, streams_open=True) is None


@pytest.mark.unit
def test_stream_window_and_connection_window_must_both_be_raised() -> None:
    message = 1 * MIB
    small = HTTP2_DEFAULT_WINDOW
    default = transfer_rounds(message, stream_window=small, connection_window=small)
    tuned = transfer_rounds(message, stream_window=message + 5, connection_window=message + 5)
    stream_only = transfer_rounds(message, stream_window=message + 5, connection_window=small)
    assert default.bytes_on_wire == tuned.bytes_on_wire == message + 5
    assert default.rounds >= -(-(message + 5) // small) == 17
    assert tuned.rounds == 1
    assert stream_only.rounds == default.rounds


@pytest.mark.unit
def test_window_over_rtt_bounds_stream_throughput() -> None:
    assert window_throughput_bytes_per_s(HTTP2_DEFAULT_WINDOW, 0.05) == pytest.approx(1_310_700)
    assert window_throughput_bytes_per_s(4 * MIB, 0.05) == pytest.approx(83_886_080)


@pytest.mark.unit
def test_bdp_estimate_doubles_only_while_bandwidth_keeps_improving() -> None:
    estimate, best = 65_536, 0.0
    for bandwidth in (1.0, 2.0, 4.0):
        estimate, best = next_bdp_estimate(estimate, estimate, bandwidth, best)
    assert estimate == 65_536 * 8  # three doublings
    stalled, _ = next_bdp_estimate(estimate, estimate, 3.0, best)
    assert stalled == estimate
    assert target_initial_window(65_536) == 4 * MIB
    assert target_initial_window(3 * MIB) == 8 * MIB
    assert target_initial_window(10**12) == 1 << 30


@pytest.mark.socket
@pytest.mark.asyncio
async def test_real_grpc_rejects_messages_over_the_default_4_mib_limit() -> None:
    at_limit = await real_call("Echo", b"a" * DEFAULT_MAX_RECEIVE_BYTES)
    assert at_limit.code == "OK"
    over = await real_call("Echo", b"a" * OVER_LIMIT)
    assert over.code == "RESOURCE_EXHAUSTED"
    expected = check_message_size(
        OVER_LIMIT, DEFAULT_MAX_RECEIVE_BYTES, side="SERVER", direction="recv"
    )
    assert expected is not None
    assert expected in over.details
    reply = await real_call("Blob", str(OVER_LIMIT).encode())
    assert reply.code == "RESOURCE_EXHAUSTED"
    assert "CLIENT: Received message larger than max (4194305 vs. 4194304)" in reply.details


@pytest.mark.socket
@pytest.mark.asyncio
async def test_real_grpc_send_limit_fails_locally_and_raising_one_side_moves_the_failure() -> None:
    capped = await real_call(
        "Echo", b"a" * 2000, client_options=message_limit_options(max_send=1000)
    )
    assert capped.code == "RESOURCE_EXHAUSTED"
    assert "CLIENT: Sent message larger than max (2000 vs. 1000)" in capped.details
    five_mib = b"a" * (5 * MIB)
    server_only = await real_call(
        "Echo", five_mib, server_options=message_limit_options(max_receive=UNLIMITED)
    )
    assert server_only.code == "RESOURCE_EXHAUSTED"
    assert "CLIENT: Received message larger than max" in server_only.details
    both = await real_call(
        "Echo",
        five_mib,
        server_options=message_limit_options(max_receive=UNLIMITED),
        client_options=message_limit_options(max_receive=UNLIMITED),
    )
    assert both.code == "OK"


@pytest.mark.socket
@pytest.mark.asyncio
async def test_real_grpc_keepalive_goaway_matches_the_simulator() -> None:
    hold = b"8"
    active_cases = {
        "strict-server": (EAGER, STRICT),
        "default-max-pings-without-data": (ClientKeepalive(time_ms=50, timeout_ms=1_000), STRICT),
        "permissive-server": (EAGER, ServerPingPolicy(min_recv_ping_interval_ms=10)),
    }
    idle_cases = {
        "idle-server-forbids": (EAGER_IDLE, ServerPingPolicy()),
        "idle-both-permit": (
            EAGER_IDLE,
            ServerPingPolicy(min_recv_ping_interval_ms=10, permit_without_calls=True),
        ),
        "idle-client-silent": (EAGER, ServerPingPolicy()),
    }
    active = {
        name: asyncio.create_task(
            real_call("Hold", hold, server_options=s.options(), client_options=c.options())
        )
        for name, (c, s) in active_cases.items()
    }
    idle = {
        name: asyncio.create_task(
            real_idle_survives(server_options=s.options(), client_options=c.options(), watch_s=6.0)
        )
        for name, (c, s) in idle_cases.items()
    }
    for name, task in active.items():
        c, s = active_cases[name]
        predicted = simulate_keepalive(c, s, call_active=True, horizon_ms=6_000).goaway_at_ms
        outcome = await task
        if predicted is not None:
            assert outcome.code == "UNAVAILABLE", name
            assert "too many pings" in outcome.details.lower(), name
            assert outcome.elapsed_s < 7.5, name
        else:
            assert outcome.code == "OK", name
    for name, idle_task in idle.items():
        c, s = idle_cases[name]
        predicted = simulate_keepalive(c, s, call_active=False, horizon_ms=6_000).goaway_at_ms
        assert await idle_task is (predicted is None), name
