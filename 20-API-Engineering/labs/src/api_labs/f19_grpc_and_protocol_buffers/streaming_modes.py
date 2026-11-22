"""Lab: the four gRPC communication modes on grpc.aio 1.85.1 over a loopback socket.

The service `lab.Calc` has one method per mode plus a few probes for half-close, cancellation,
metadata, and load shedding.
Messages are decimal-ASCII integers (`encode` / `decode`) instead of generated protobuf classes,
because grpc.aio only ever calls the serializer functions it is given; a generated stub passes
`Message.SerializeToString` and `Message.FromString` in exactly the same slots.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUnknownArgumentType=false, reportMissingTypeArgument=false, reportAttributeAccessIssue=false

from __future__ import annotations

import asyncio
import contextlib
import enum
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import grpc  # type: ignore[import-untyped]
from grpc import aio

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

SERVICE = "lab.Calc"
LOOPBACK = "127.0.0.1:0"


class Mode(enum.Enum):
    """The four method shapes, named by (request streaming, response streaming)."""

    UNARY = "unary-unary"
    SERVER_STREAMING = "unary-stream"
    CLIENT_STREAMING = "stream-unary"
    BIDI_STREAMING = "stream-stream"


def mode_of(handler: grpc.RpcMethodHandler) -> Mode:
    """Classify a method handler by its two streaming flags, as aio's `_handle_rpc` does."""
    if handler.request_streaming:
        return Mode.BIDI_STREAMING if handler.response_streaming else Mode.CLIENT_STREAMING
    return Mode.SERVER_STREAMING if handler.response_streaming else Mode.UNARY


def encode(value: int) -> bytes:
    """Serializer slot: what `Message.SerializeToString` would be for a generated class."""
    return str(value).encode("ascii")


def decode(raw: bytes) -> int:
    """Deserializer slot: what `Message.FromString` would be for a generated class."""
    return int(raw.decode("ascii"))


@dataclass
class Trace:
    """Handler-side observations the tests await instead of sleeping."""

    received: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    context_cancelled: bool | None = None


class CalcService:
    """One handler per mode; every handler is a native coroutine or async generator."""

    def __init__(self, trace: Trace) -> None:
        self.trace = trace

    async def square(self, request: int, context: Any) -> int:
        """UNARY: abort on negatives, set a non-OK code on zero, else square."""
        if request < 0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "negative input")
        if request == 0:
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details("zero input")
            return 99  # discarded by the server because the code is not OK
        return request * request

    async def countdown(self, request: int, context: Any) -> AsyncIterator[int]:
        """SERVER_STREAMING: yield request..1, then trailing metadata. -1 fails after two."""
        context.set_trailing_metadata((("x-count", str(abs(request))),))
        if request == -1:
            yield 2
            yield 1
            raise RuntimeError("boom after two messages")
        for value in range(request, 0, -1):
            yield value

    async def countdown_gated(self, request: int, context: Any) -> AsyncIterator[int]:
        """SERVER_STREAMING: yield one message, then block until the test opens the gate."""
        yield request
        await self.trace.gate.wait()
        yield request - 1

    async def stream_with_metadata(self, request: int, context: Any) -> AsyncIterator[int]:
        """SERVER_STREAMING: explicit initial metadata, then block before the first message."""
        await context.send_initial_metadata((("x-ready", "1"),))
        await self.trace.gate.wait()
        yield request

    async def total(self, request_iterator: AsyncIterator[int], context: Any) -> int:
        """CLIENT_STREAMING: respond only after the request stream ends (half-close)."""
        running = 0
        async for value in request_iterator:
            running += value
            self.trace.received.put_nowait(f"recv {value}")
        self.trace.received.put_nowait("eof")
        return running

    async def running_sum(
        self, request_iterator: AsyncIterator[int], context: Any
    ) -> AsyncIterator[int]:
        """BIDI_STREAMING (async generator style): one response per request."""
        running = 0
        async for value in request_iterator:
            running += value
            yield running

    async def echo_double(self, request_iterator: AsyncIterator[int], context: Any) -> None:
        """BIDI_STREAMING (reader-writer style): `context.read` / `context.write` loop."""
        while True:
            value = await context.read()
            if value is aio.EOF:
                break
            await context.write(value * 2)

    async def first_only(
        self, request_iterator: AsyncIterator[int], context: Any
    ) -> AsyncIterator[int]:
        """BIDI_STREAMING: answer the first message and finish without waiting for EOF."""
        async for value in request_iterator:
            yield value + 1000
            return

    async def hold(self, request_iterator: AsyncIterator[int], context: Any) -> None:
        """BIDI_STREAMING (coroutine style): park until the peer cancels, and record it.

        A cancel that lands while `context.read()` is pending can surface as end-of-stream first
        (aio's `_receive_message` turns a failed receive into EOF), so the handler parks after the
        loop; the server then injects `CancelledError` at that await.
        """
        try:
            async for _ in request_iterator:
                self.trace.entered.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.trace.cancelled.set()
            raise
        finally:
            self.trace.context_cancelled = bool(context.cancelled())


def build_handler(service: CalcService) -> grpc.GenericRpcHandler:
    """Register the methods; the factory chosen is what fixes each method's `Mode`."""
    kwargs: dict[str, Any] = {"request_deserializer": decode, "response_serializer": encode}
    handlers = {
        "Square": grpc.unary_unary_rpc_method_handler(service.square, **kwargs),
        "Countdown": grpc.unary_stream_rpc_method_handler(service.countdown, **kwargs),
        "CountdownGated": grpc.unary_stream_rpc_method_handler(service.countdown_gated, **kwargs),
        "StreamWithMetadata": grpc.unary_stream_rpc_method_handler(
            service.stream_with_metadata, **kwargs
        ),
        "Total": grpc.stream_unary_rpc_method_handler(service.total, **kwargs),
        "RunningSum": grpc.stream_stream_rpc_method_handler(service.running_sum, **kwargs),
        "EchoDouble": grpc.stream_stream_rpc_method_handler(service.echo_double, **kwargs),
        "FirstOnly": grpc.stream_stream_rpc_method_handler(service.first_only, **kwargs),
        "Hold": grpc.stream_stream_rpc_method_handler(service.hold, **kwargs),
    }
    return grpc.method_handlers_generic_handler(SERVICE, handlers)


class CalcClient:
    """Typed-by-convention stub: each attribute is the multicallable for one mode."""

    def __init__(self, channel: aio.Channel) -> None:
        self.channel = channel

        def path(name: str) -> str:
            return f"/{SERVICE}/{name}"

        ser: dict[str, Any] = {"request_serializer": encode, "response_deserializer": decode}
        self.square = channel.unary_unary(path("Square"), **ser)
        self.countdown = channel.unary_stream(path("Countdown"), **ser)
        self.countdown_gated = channel.unary_stream(path("CountdownGated"), **ser)
        self.stream_with_metadata = channel.unary_stream(path("StreamWithMetadata"), **ser)
        self.total = channel.stream_unary(path("Total"), **ser)
        self.running_sum = channel.stream_stream(path("RunningSum"), **ser)
        self.echo_double = channel.stream_stream(path("EchoDouble"), **ser)
        self.first_only = channel.stream_stream(path("FirstOnly"), **ser)
        self.hold = channel.stream_stream(path("Hold"), **ser)
        self.missing = channel.unary_unary(path("DoesNotExist"), **ser)

    def raw(self, mode: Mode, name: str) -> Any:
        """Open `name` through the multicallable of `mode`, whatever the server registered."""
        factory = {
            Mode.UNARY: self.channel.unary_unary,
            Mode.SERVER_STREAMING: self.channel.unary_stream,
            Mode.CLIENT_STREAMING: self.channel.stream_unary,
            Mode.BIDI_STREAMING: self.channel.stream_stream,
        }[mode]
        return factory(
            f"/{SERVICE}/{name}", request_serializer=encode, response_deserializer=decode
        )


@contextlib.asynccontextmanager
async def lab_server(
    trace: Trace, *, maximum_concurrent_rpcs: int | None = None
) -> AsyncGenerator[CalcClient]:
    """Start the service on `127.0.0.1:0`, yield a connected client, stop with grace=None."""
    server = aio.server(maximum_concurrent_rpcs=maximum_concurrent_rpcs)
    server.add_generic_rpc_handlers((build_handler(CalcService(trace)),))
    port = server.add_insecure_port(LOOPBACK)
    await server.start()
    try:
        async with aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            yield CalcClient(channel)
    finally:
        await server.stop(None)


async def _requests(count: int) -> AsyncIterator[int]:
    for value in range(1, count + 1):
        yield value


def _check(ok: bool, what: str) -> None:
    if not ok:
        raise RuntimeError(f"measurement run produced a wrong {what}")


async def measure_round_trips(count: int) -> dict[str, float]:
    """Wall-clock seconds to move `count` messages three ways over loopback (not asserted)."""
    async with lab_server(Trace()) as client:
        await client.square(1)  # warm the HTTP/2 connection before timing anything
        out: dict[str, float] = {}

        start = time.perf_counter()
        for value in range(1, count + 1):
            _check(await client.square(value) == value * value, "unary result")
        out["unary_sequential"] = time.perf_counter() - start

        start = time.perf_counter()
        call = client.echo_double()
        for value in range(1, count + 1):
            await call.write(value)
            _check(await call.read() == value * 2, "bidi result")
        await call.done_writing()
        _check(await call.read() is aio.EOF, "bidi EOF")
        out["bidi_ping_pong"] = time.perf_counter() - start

        start = time.perf_counter()
        seen = 0
        async for _ in client.running_sum(_requests(count)):
            seen += 1
        _check(seen == count, "pipelined count")
        out["bidi_pipelined"] = time.perf_counter() - start
        return out
