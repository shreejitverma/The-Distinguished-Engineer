"""Lab tests: unary, server, client, and bidirectional streaming on grpc.aio 1.85.1."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUnknownArgumentType=false, reportMissingTypeArgument=false, reportAttributeAccessIssue=false

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import grpc  # type: ignore[import-untyped]
import pytest
from grpc import aio

from api_labs.f19_grpc_and_protocol_buffers.streaming_modes import (
    CalcService,
    Mode,
    Trace,
    build_handler,
    decode,
    encode,
    lab_server,
    measure_round_trips,
    mode_of,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

WAIT = 5.0  # seconds; only reached when a test is about to fail


@dataclass
class _Details:
    method: str


def _resolve(method: str) -> grpc.RpcMethodHandler | None:
    handler = build_handler(CalcService(Trace()))
    return handler.service(cast("Any", _Details(method)))


async def _collect(call: aio.UnaryStreamCall, into: list[int] | None = None) -> list[int]:
    out = [] if into is None else into
    async for message in call:
        out.append(message)
    return out


# --- unit: handler registration and codecs (no sockets) ---


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "mode"),
    [
        ("Square", Mode.UNARY),
        ("Countdown", Mode.SERVER_STREAMING),
        ("Total", Mode.CLIENT_STREAMING),
        ("RunningSum", Mode.BIDI_STREAMING),
    ],
)
def test_registration_factory_fixes_the_mode(method: str, mode: Mode) -> None:
    handler = _resolve(f"/lab.Calc/{method}")
    assert handler is not None
    assert mode_of(handler) is mode
    assert (handler.request_streaming, handler.response_streaming) == {
        Mode.UNARY: (False, False),
        Mode.SERVER_STREAMING: (False, True),
        Mode.CLIENT_STREAMING: (True, False),
        Mode.BIDI_STREAMING: (True, True),
    }[mode]


@pytest.mark.unit
def test_handler_slot_is_filled_only_for_its_own_mode() -> None:
    handler = _resolve("/lab.Calc/Total")
    assert handler is not None
    assert handler.stream_unary is not None
    assert handler.unary_unary is None
    assert handler.unary_stream is None
    assert handler.stream_stream is None


@pytest.mark.unit
def test_unknown_method_resolves_to_no_handler() -> None:
    assert _resolve("/lab.Calc/DoesNotExist") is None
    assert _resolve("/other.Service/Square") is None


@pytest.mark.unit
def test_codecs_round_trip_and_are_the_serializer_slots() -> None:
    assert decode(encode(-12)) == -12
    assert encode(7) == b"7"


# --- socket: unary ---


@pytest.mark.socket
@pytest.mark.asyncio
async def test_unary_returns_one_response_and_ok_status() -> None:
    async with lab_server(Trace()) as client:
        call = client.square(7)
        assert await call == 49
        assert await call.code() == grpc.StatusCode.OK


@pytest.mark.socket
@pytest.mark.asyncio
async def test_unary_nonok_code_discards_the_returned_response() -> None:
    async with lab_server(Trace()) as client:
        with pytest.raises(aio.AioRpcError) as caught:
            await client.square(0)
    assert caught.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert caught.value.details() == "zero input"


@pytest.mark.socket
@pytest.mark.asyncio
async def test_unary_abort_carries_code_and_details() -> None:
    async with lab_server(Trace()) as client:
        with pytest.raises(aio.AioRpcError) as caught:
            await client.square(-3)
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert caught.value.details() == "negative input"


@pytest.mark.socket
@pytest.mark.asyncio
async def test_unknown_method_returns_unimplemented_method_not_found() -> None:
    async with lab_server(Trace()) as client:
        with pytest.raises(aio.AioRpcError) as caught:
            await client.missing(1)
    assert caught.value.code() == grpc.StatusCode.UNIMPLEMENTED
    assert caught.value.details() == "Method not found!"


# --- socket: server streaming ---


@pytest.mark.socket
@pytest.mark.asyncio
async def test_server_streaming_delivers_messages_in_order_then_trailers() -> None:
    async with lab_server(Trace()) as client:
        call = client.countdown(5)
        assert await _collect(call) == [5, 4, 3, 2, 1]
        assert await call.code() == grpc.StatusCode.OK
        assert dict(await call.trailing_metadata())["x-count"] == "5"


@pytest.mark.socket
@pytest.mark.asyncio
async def test_server_streaming_first_message_arrives_before_handler_finishes() -> None:
    trace = Trace()
    async with lab_server(trace) as client:
        call = client.countdown_gated(10)
        first = await asyncio.wait_for(call.read(), WAIT)
        assert first == 10
        assert not call.done()  # the handler is parked on the gate: the stream is live
        trace.gate.set()
        assert await asyncio.wait_for(call.read(), WAIT) == 9
        assert await asyncio.wait_for(call.read(), WAIT) is aio.EOF


@pytest.mark.socket
@pytest.mark.asyncio
async def test_server_streaming_handler_error_arrives_after_the_messages_sent_so_far() -> None:
    async with lab_server(Trace()) as client:
        call = client.countdown(-1)
        received: list[int] = []
        with pytest.raises(aio.AioRpcError) as caught:
            await _collect(call, received)
    assert received == [2, 1]
    assert caught.value.code() == grpc.StatusCode.UNKNOWN
    assert "boom after two messages" in (caught.value.details() or "")


@pytest.mark.socket
@pytest.mark.asyncio
async def test_server_initial_metadata_is_sent_before_the_first_message() -> None:
    trace = Trace()
    async with lab_server(trace) as client:
        call = client.stream_with_metadata(5)
        metadata = await asyncio.wait_for(call.initial_metadata(), WAIT)
        assert dict(metadata)["x-ready"] == "1"
        assert not trace.gate.is_set()
        trace.gate.set()
        assert await _collect(call) == [5]


# --- socket: client streaming ---


@pytest.mark.socket
@pytest.mark.asyncio
async def test_client_streaming_server_answers_only_after_half_close() -> None:
    trace = Trace()
    async with lab_server(trace) as client:
        call = client.total()
        for value in (1, 2, 3):
            await call.write(value)
        for value in (1, 2, 3):
            assert await asyncio.wait_for(trace.received.get(), WAIT) == f"recv {value}"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(call), 0.2)
        assert trace.received.empty()  # no EOF seen by the handler yet
        await call.done_writing()
        assert await asyncio.wait_for(trace.received.get(), WAIT) == "eof"
        assert await asyncio.wait_for(call, WAIT) == 6


@pytest.mark.socket
@pytest.mark.asyncio
async def test_client_streaming_accepts_an_async_iterator_of_requests() -> None:
    async def requests() -> AsyncIterator[int]:
        for value in range(1, 101):
            yield value

    async with lab_server(Trace()) as client:
        assert await client.total(requests()) == 5050


@pytest.mark.socket
@pytest.mark.asyncio
async def test_write_after_done_writing_raises_invalid_state() -> None:
    async with lab_server(Trace()) as client:
        call = client.total()
        await call.write(1)
        await call.done_writing()
        await call.done_writing()  # idempotent
        with pytest.raises(asyncio.InvalidStateError, match="half closed"):
            await call.write(2)
        assert await call == 1


@pytest.mark.socket
@pytest.mark.asyncio
async def test_request_iterator_and_write_api_cannot_be_mixed() -> None:
    async def requests() -> AsyncIterator[int]:
        yield 1

    async with lab_server(Trace()) as client:
        call = client.total(requests())
        with pytest.raises(aio.UsageError, match="may not be mixed"):
            await call.write(2)
        assert await call == 1


# --- socket: bidirectional streaming ---


@pytest.mark.socket
@pytest.mark.asyncio
async def test_bidi_reader_writer_interleaves_before_the_request_stream_ends() -> None:
    async with lab_server(Trace()) as client:
        call = client.echo_double()
        for value in (1, 2, 3):
            await call.write(value)
            # The reply to `value` arrives while the client is still allowed to send more.
            assert await asyncio.wait_for(call.read(), WAIT) == value * 2
            assert not call.done()
        await call.done_writing()
        assert await asyncio.wait_for(call.read(), WAIT) is aio.EOF
        assert await call.code() == grpc.StatusCode.OK


@pytest.mark.socket
@pytest.mark.asyncio
async def test_bidi_async_generator_style_preserves_order_over_many_messages() -> None:
    async def requests() -> AsyncIterator[int]:
        for _ in range(200):
            yield 1

    async with lab_server(Trace()) as client:
        sums = [value async for value in client.running_sum(requests())]
    assert sums == list(range(1, 201))


@pytest.mark.socket
@pytest.mark.asyncio
async def test_bidi_server_may_finish_before_the_client_half_closes() -> None:
    async with lab_server(Trace()) as client:
        call = client.first_only()
        await call.write(1)
        assert await asyncio.wait_for(call.read(), WAIT) == 1001
        # The status arrived although the client never called done_writing.
        assert await asyncio.wait_for(call.read(), WAIT) is aio.EOF
        assert await call.code() == grpc.StatusCode.OK
        with pytest.raises(asyncio.InvalidStateError, match="already finished"):
            await call.write(2)


@pytest.mark.socket
@pytest.mark.asyncio
async def test_response_iterator_and_read_api_cannot_be_mixed() -> None:
    async with lab_server(Trace()) as client:
        call = client.echo_double()
        await call.write(1)
        assert await asyncio.wait_for(call.read(), WAIT) == 2
        with pytest.raises(aio.UsageError, match="may not be mixed"):
            call.__aiter__()
        await call.done_writing()
        assert await asyncio.wait_for(call.read(), WAIT) is aio.EOF


# --- socket: cancellation and load shedding ---


@pytest.mark.socket
@pytest.mark.asyncio
async def test_client_cancel_raises_cancelled_error_in_the_server_handler() -> None:
    trace = Trace()
    async with lab_server(trace) as client:
        call = client.hold()
        await call.write(1)
        await asyncio.wait_for(trace.entered.wait(), WAIT)
        assert call.cancel()
        await asyncio.wait_for(trace.cancelled.wait(), WAIT)
        assert call.cancelled()
        # aio's `_ServicerContext.cancelled()` only reads a code the handler set itself, so a
        # client cancel does not flip it: the CancelledError is the only reliable signal.
        await asyncio.sleep(0)
        assert trace.context_cancelled is False


@pytest.mark.socket
@pytest.mark.asyncio
async def test_maximum_concurrent_rpcs_sheds_the_extra_call_with_resource_exhausted() -> None:
    trace = Trace()
    async with lab_server(trace, maximum_concurrent_rpcs=1) as client:
        holder = client.hold()
        await holder.write(1)
        await asyncio.wait_for(trace.entered.wait(), WAIT)
        with pytest.raises(aio.AioRpcError) as caught:
            await client.square(3)
        holder.cancel()
    assert caught.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    assert caught.value.details() == "Concurrent RPC limit exceeded!"


@pytest.mark.socket
@pytest.mark.asyncio
async def test_without_a_limit_a_second_call_is_served_while_one_is_held() -> None:
    trace = Trace()
    async with lab_server(trace) as client:
        holder = client.hold()
        await holder.write(1)
        await asyncio.wait_for(trace.entered.wait(), WAIT)
        assert await asyncio.wait_for(client.square(3), WAIT) == 9
        holder.cancel()


# --- socket: the wire does not pin the mode ---


@pytest.mark.socket
@pytest.mark.asyncio
async def test_unary_client_can_call_client_streaming_and_bidi_methods() -> None:
    async with lab_server(Trace()) as client:
        assert await client.raw(Mode.UNARY, "Total")(41) == 41
        assert await client.raw(Mode.UNARY, "RunningSum")(6) == 6


@pytest.mark.socket
@pytest.mark.asyncio
async def test_server_streaming_client_can_call_a_unary_method() -> None:
    async with lab_server(Trace()) as client:
        assert await _collect(client.raw(Mode.SERVER_STREAMING, "Square")(6)) == [36]


@pytest.mark.socket
@pytest.mark.asyncio
async def test_unary_client_never_completes_against_a_two_message_response() -> None:
    async with lab_server(Trace()) as client:
        unary = client.raw(Mode.UNARY, "Countdown")
        assert await asyncio.wait_for(unary(1), WAIT) == 1  # one response message: fine
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(unary(2), 1.0)  # two response messages: no completion


@pytest.mark.socket
@pytest.mark.asyncio
async def test_measure_round_trips_reports_all_three_shapes() -> None:
    timings = await measure_round_trips(20)
    assert set(timings) == {"unary_sequential", "bidi_ping_pong", "bidi_pipelined"}
    assert all(seconds > 0 for seconds in timings.values())
