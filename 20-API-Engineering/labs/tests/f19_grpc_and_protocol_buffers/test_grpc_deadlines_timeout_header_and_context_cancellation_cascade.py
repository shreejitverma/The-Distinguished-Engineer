# pyright: reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingTypeArgument=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Lab for the gRPC deadline note: grpc-timeout wire codec, budget rule, and the abort cascade."""

from __future__ import annotations

import asyncio
import math
import re

import grpc  # type: ignore[import-untyped]
import grpc.aio  # type: ignore[import-untyped]
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from api_labs.f19_grpc_and_protocol_buffers.grpc_deadlines import (
    ChainLab,
    DeadlineExhausted,
    HopRecord,
    Propagation,
    WireProbe,
    encode_timeout_ms,
    outgoing_timeout,
    parse_timeout,
)

SETTLE_LIMIT = 3.0


async def settled(record: HopRecord) -> str:
    """Wait for a handler to finish or be cancelled, and return its outcome."""
    await asyncio.wait_for(record.settled.wait(), SETTLE_LIMIT)
    return record.outcome


async def fail_with(lab: ChainLab, budget: float | None) -> grpc.aio.AioRpcError:
    call = lab.start_call(budget)
    with pytest.raises(grpc.aio.AioRpcError) as info:
        await call
    error: grpc.aio.AioRpcError = info.value
    return error


# --- the codec (pure) ----------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("millis", "wire"),
    [
        (0, "1n"),
        (-5, "1n"),
        (1, "1m"),
        (100, "100m"),
        (999, "999m"),
        (1000, "1S"),
        (1234, "1240m"),
        (9999, "10S"),
        (10000, "10S"),
        (12345, "12400m"),
        (60000, "1M"),
        (90000, "90S"),
        (3_600_000, "1H"),
        (7_200_000, "2H"),
        (10**18, "27000H"),
    ],
)
def test_encode_timeout_matches_core_vectors(millis: int, wire: str) -> None:
    assert encode_timeout_ms(millis) == wire


@pytest.mark.unit
@settings(max_examples=400, deadline=None, derandomize=True)
@given(millis=st.integers(min_value=1, max_value=27000 * 3_600_000))
def test_encode_never_shortens_and_fits_the_spec_grammar(millis: int) -> None:
    wire = encode_timeout_ms(millis)
    assert re.fullmatch(r"[0-9]{1,8}[HMSmun]", wire)
    decoded = parse_timeout(wire)
    assert decoded is not None
    assert decoded >= millis / 1000
    assert decoded <= millis / 1000 * 1.10 + 0.001


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("100m", 0.1),
        ("1H", 3600.0),
        ("30S", 30.0),
        ("2M", 120.0),
        ("5n", 0.001),
        ("1500u", 0.002),
        (" 7 S ", 7.0),
        ("999999999S", 999999999.0),
        ("1000000000S", 1e9),
        ("10000000000S", math.inf),
        ("99999999999m", math.inf),
    ],
)
def test_parse_timeout_accepts_what_core_accepts(text: str, seconds: float) -> None:
    assert parse_timeout(text) == seconds


@pytest.mark.unit
@pytest.mark.parametrize("text", ["", "S", "100", "5x", "1 0S", "10Sx", "-5S", "1.5S"])
def test_parse_timeout_rejects_malformed(text: str) -> None:
    assert parse_timeout(text) is None


@pytest.mark.unit
def test_outgoing_timeout_subtracts_the_reserve() -> None:
    assert outgoing_timeout(1.0, reserve=0.25) == pytest.approx(0.75)


@pytest.mark.unit
def test_outgoing_timeout_without_a_deadline_uses_the_default() -> None:
    assert outgoing_timeout(None) is None
    assert outgoing_timeout(None, default=5.0) == 5.0


@pytest.mark.unit
@pytest.mark.parametrize(("remaining", "reserve"), [(0.0, 0.0), (0.05, 0.05), (0.01, 0.2)])
def test_outgoing_timeout_refuses_an_exhausted_budget(remaining: float, reserve: float) -> None:
    with pytest.raises(DeadlineExhausted):
        outgoing_timeout(remaining, reserve=reserve)


# --- the header on the wire ----------------------------------------------------------------


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize(("requested", "units"), [(30.0, "Sm"), (3600.0, "HS"), (7200.0, "HS")])
async def test_client_sends_remaining_time_in_a_coarse_unit(requested: float, units: str) -> None:
    headers = await WireProbe.capture_client_headers(requested)
    wire = headers["grpc-timeout"]
    # The wire value is the remaining time at send, rounded up, so a millisecond of clock
    # jitter can turn "30S" into "30100m"; assert the shape and the bounds, not one string.
    assert re.fullmatch(r"[0-9]{1,8}[HMSmun]", wire)
    assert wire[-1] in units
    sent = parse_timeout(wire)
    assert sent is not None
    assert requested - 1.0 <= sent <= requested * 1.01 + 0.01
    assert headers["te"] == "trailers"
    assert headers["content-type"].startswith("application/grpc")


@pytest.mark.socket
@pytest.mark.asyncio
async def test_sub_second_timeout_loses_connection_setup_time() -> None:
    headers = await WireProbe.capture_client_headers(0.5)
    sent = parse_timeout(headers["grpc-timeout"])
    assert sent is not None
    assert 0.3 < sent <= 0.51


@pytest.mark.socket
@pytest.mark.asyncio
async def test_client_cancel_is_rst_stream_cancel_on_the_wire() -> None:
    assert await WireProbe.reset_code_after_client_cancel() == 0x08  # RFC 9113 Section 7: CANCEL


async def _slow_server() -> tuple[grpc.aio.Server, int, list[float | None], list[str]]:
    remaining: list[float | None] = []
    events: list[str] = []

    async def slow(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
        remaining.append(context.time_remaining())
        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            events.append("cancelled")
            raise
        events.append("completed")
        return b""

    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "lab.Chain", {"Slow": grpc.unary_unary_rpc_method_handler(slow)}
            ),
        )
    )
    port = int(server.add_insecure_port("127.0.0.1:0"))
    await server.start()
    return server, port, remaining, events


@pytest.mark.socket
@pytest.mark.asyncio
async def test_server_enforces_the_header_without_any_client_library() -> None:
    server, port, remaining, events = await _slow_server()
    try:
        result = await WireProbe.raw_unary_call(port, "/lab.Chain/Slow", "50m")
        await asyncio.sleep(0.05)
    finally:
        await server.stop(0)
    assert result.headers["grpc-status"] == "4"
    assert result.headers[":status"] == "200"
    assert result.seconds < 0.5
    assert events == ["cancelled"]
    assert remaining[0] is not None
    assert remaining[0] <= 0.05


@pytest.mark.socket
@pytest.mark.asyncio
async def test_absent_header_means_no_deadline() -> None:
    server, port, remaining, events = await _slow_server()
    try:
        result = await WireProbe.raw_unary_call(port, "/lab.Chain/Slow", None)
    finally:
        await server.stop(0)
    assert result.headers["grpc-status"] == "0"
    assert remaining == [None]
    assert events == ["completed"]
    assert result.seconds >= 1.0


@pytest.mark.socket
@pytest.mark.asyncio
async def test_malformed_header_fails_the_call_with_internal_in_1_85_1() -> None:
    server, port, remaining, _ = await _slow_server()
    try:
        result = await WireProbe.raw_unary_call(port, "/lab.Chain/Slow", "5x")
    finally:
        await server.stop(0)
    assert result.headers["grpc-status"] == "13"
    assert "grpc-timeout" in result.headers["grpc-message"]
    assert remaining == []


@pytest.mark.socket
@pytest.mark.asyncio
async def test_nine_digit_header_is_accepted_as_a_very_long_deadline() -> None:
    server, port, remaining, _ = await _slow_server()
    try:
        await WireProbe.raw_unary_call(port, "/lab.Chain/Slow", "123456789S")
    finally:
        await server.stop(0)
    assert remaining[0] is not None
    assert remaining[0] > 1.2e8


# --- the cascade ---------------------------------------------------------------------------


@pytest.mark.socket
@pytest.mark.asyncio
async def test_deadline_propagates_and_both_hops_abort() -> None:
    async with ChainLab(Propagation.DEADLINE, stall=0.2) as lab:
        error = await fail_with(lab, 0.1)
        assert error.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        assert await settled(lab.hop2) == "cancelled"
        assert await settled(lab.hop1) in {"cancelled", "downstream-error"}
    assert lab.hop1.remaining_at_entry is not None
    assert lab.hop2.remaining_at_entry is not None
    # core clocks tick in whole milliseconds, so allow 5 ms of rounding slack
    assert lab.hop2.remaining_at_entry <= lab.hop1.remaining_at_entry + 0.005
    assert lab.hop1.remaining_at_entry <= 0.105


@pytest.mark.socket
@pytest.mark.asyncio
async def test_without_propagation_hop2_has_no_deadline_but_is_still_cancelled() -> None:
    async with ChainLab(Propagation.CANCEL_ONLY, stall=0.2) as lab:
        error = await fail_with(lab, 0.1)
        assert error.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        assert await settled(lab.hop1) == "cancelled"
        assert await settled(lab.hop2) == "cancelled"
    assert lab.hop2.remaining_at_entry is None


@pytest.mark.socket
@pytest.mark.asyncio
async def test_detached_downstream_task_survives_and_wastes_the_work() -> None:
    async with ChainLab(Propagation.DETACHED, stall=0.2) as lab:
        error = await fail_with(lab, 0.1)
        assert error.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        assert await settled(lab.hop1) == "cancelled"
        assert await settled(lab.hop2) == "completed"


@pytest.mark.socket
@pytest.mark.asyncio
async def test_client_cancel_cascades_through_both_hops() -> None:
    async with ChainLab(Propagation.DEADLINE, stall=2.0) as lab:
        call = lab.start_call(None)
        await asyncio.wait_for(lab.hop2.entered.wait(), SETTLE_LIMIT)
        assert lab.hop2.remaining_at_entry is None
        call.cancel()
        assert await call.code() == grpc.StatusCode.CANCELLED
        assert await settled(lab.hop1) == "cancelled"
        assert await settled(lab.hop2) == "cancelled"


@pytest.mark.socket
@pytest.mark.asyncio
async def test_a_call_that_fits_the_budget_completes() -> None:
    async with ChainLab(Propagation.DEADLINE, stall=0.05) as lab:
        reply = await lab.start_call(5.0)
        assert reply == b"hop2-done"
        assert await settled(lab.hop1) == "completed"
        assert await settled(lab.hop2) == "completed"
    assert lab.hop2.remaining_at_entry is not None
    # time_remaining() mixes core's millisecond clock with time.time(); allow 50 ms of skew
    assert lab.hop2.remaining_at_entry < 5.05


@pytest.mark.socket
@pytest.mark.asyncio
async def test_downstream_error_escaping_the_handler_becomes_unknown() -> None:
    async with ChainLab(Propagation.RAW_ERROR, stall=0.2, reserve=0.03) as lab:
        error = await fail_with(lab, 0.1)
        assert error.code() == grpc.StatusCode.UNKNOWN
        assert "AioRpcError" in (error.details() or "")
        assert await settled(lab.hop1) == "downstream-error"
        assert await settled(lab.hop2) == "cancelled"


@pytest.mark.socket
@pytest.mark.asyncio
async def test_mapping_the_downstream_status_preserves_deadline_exceeded() -> None:
    async with ChainLab(Propagation.MAPPED_ERROR, stall=0.2, reserve=0.03) as lab:
        error = await fail_with(lab, 0.1)
        assert error.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        assert (error.details() or "").startswith("downstream: ")
        assert await settled(lab.hop2) == "cancelled"
