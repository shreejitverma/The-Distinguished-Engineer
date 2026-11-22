# pyright: reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingTypeArgument=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
"""gRPC deadlines: the grpc-timeout codec, budget arithmetic, and a two-hop call chain.

Three layers, each small enough to read in one sitting:

1. ``encode_timeout_ms`` and ``parse_timeout`` port the algorithms in grpc-core 1.85.1
   ``src/core/lib/transport/timeout_encoding.cc`` (``Timeout::FromMillis`` and ``ParseTimeout``).
2. ``outgoing_timeout`` is the budget rule a ``grpc.aio`` handler must apply by hand, because
   ``grpc.aio`` creates every outgoing call with a NULL parent and so propagates nothing.
3. ``ChainLab`` runs a client -> hop 1 -> hop 2 chain on loopback so tests can observe which
   handler tasks are cancelled, and ``WireProbe`` shows the raw HTTP/2 bytes of the header.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import grpc  # type: ignore[import-untyped]
import grpc.aio  # type: ignore[import-untyped]
import h2.config
import h2.connection
import h2.events

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import TracebackType

# --- layer 1: the grpc-timeout wire codec -------------------------------------------------

MAX_HOURS: Final = 27000
_TEN_X: Final = ("", "0", "00")


def _ceil_div(dividend: int, divisor: int) -> int:
    return (dividend - 1 + divisor) // divisor


def _rounded(value: int, zeros: int, unit: str) -> str:
    return f"{value}{_TEN_X[zeros]}{unit}"


def _from_hours(hours: int) -> str:
    return f"{min(hours, MAX_HOURS)}H"


def _from_minutes(minutes: int) -> str:
    if minutes < 1000:
        if minutes % 60 != 0:
            return f"{minutes}M"
    elif minutes < 10000:
        value = _ceil_div(minutes, 10)
        if (value * 10) % 60 != 0:
            return _rounded(value, 1, "M")
    elif minutes < 100000:
        value = _ceil_div(minutes, 100)
        if (value * 100) % 60 != 0:
            return _rounded(value, 2, "M")
    return _from_hours(_ceil_div(minutes, 60))


def _from_seconds(seconds: int) -> str:
    if seconds < 1000:
        if seconds % 60 != 0:
            return f"{seconds}S"
    elif seconds < 10000:
        value = _ceil_div(seconds, 10)
        if (value * 10) % 60 != 0:
            return _rounded(value, 1, "S")
    elif seconds < 100000:
        value = _ceil_div(seconds, 100)
        if (value * 100) % 60 != 0:
            return _rounded(value, 2, "S")
    return _from_minutes(_ceil_div(seconds, 60))


def encode_timeout_ms(millis: int) -> str:
    """Encode a remaining duration the way grpc-core 1.85.1 does: round up, never down.

    The encoder prefers the coarsest unit that keeps the value to at most five digits and
    only ever lengthens the budget, so a peer can never expire earlier than the sender meant.
    """
    if millis <= 0:
        return "1n"
    if millis < 1000:
        return f"{millis}m"
    if millis < 10000:
        value = _ceil_div(millis, 10)
        if value % 100 != 0:
            return _rounded(value, 1, "m")
    elif millis < 100000:
        value = _ceil_div(millis, 100)
        if value % 10 != 0:
            return _rounded(value, 2, "m")
    return _from_seconds(_ceil_div(millis, 1000))


_UNIT_SECONDS: Final = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "S": 1.0, "M": 60.0, "H": 3600.0}


def parse_timeout(text: str) -> float | None:
    """Parse a ``grpc-timeout`` value to seconds; ``None`` means malformed.

    Mirrors ``ParseTimeout``: it tolerates spaces, rounds ``n`` and ``u`` up to whole
    milliseconds, and treats a value of ten or more digits as infinite.
    """
    stripped = text.lstrip(" ")
    number = 0
    index = 0
    while index < len(stripped) and stripped[index] in "0123456789":
        digit = int(stripped[index])
        if number >= 100_000_000 and (number != 100_000_000 or digit != 0):
            return math.inf
        number = number * 10 + digit
        index += 1
    if index == 0:
        return None
    rest = stripped[index:].lstrip(" ")
    if not rest or rest[0] not in _UNIT_SECONDS or rest[1:].strip(" "):
        return None
    unit = rest[0]
    if unit == "n":
        return math.ceil(number / 1_000_000) / 1000
    if unit == "u":
        return math.ceil(number / 1000) / 1000
    return number * _UNIT_SECONDS[unit]


# --- layer 2: budget arithmetic -----------------------------------------------------------


class DeadlineExhausted(Exception):
    """Raised when no budget is left to make a downstream call."""


def outgoing_timeout(
    remaining: float | None, *, reserve: float = 0.0, default: float | None = None
) -> float | None:
    """Timeout for a downstream call made while serving a call with ``remaining`` seconds left.

    ``remaining`` is ``ServicerContext.time_remaining()``: ``None`` when the caller set no
    deadline. ``reserve`` is the time this hop keeps for its own post-processing, so the
    downstream deadline fires first and this hop can still answer with a useful status.
    """
    if remaining is None:
        return default
    budget = remaining - reserve
    if budget <= 0:
        raise DeadlineExhausted(f"remaining={remaining:.4f}s reserve={reserve:.4f}s")
    return budget


# --- layer 3: a two-hop chain on loopback -------------------------------------------------


class Propagation(enum.Enum):
    """How hop 1 treats the deadline of the call it is serving."""

    DEADLINE = "forward time_remaining() as the downstream timeout"
    CANCEL_ONLY = "send no downstream timeout; rely on cancellation alone"
    DETACHED = "run the downstream call in a shielded task that outlives hop 1"
    RAW_ERROR = "forward the deadline but let the downstream AioRpcError escape the handler"
    MAPPED_ERROR = "forward the deadline and abort with the downstream status code"


@dataclass
class HopRecord:
    """What one handler observed; ``outcome`` is the only field tests should trust at rest."""

    name: str
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    remaining_at_entry: float | None = None
    outcome: str = "pending"

    def enter(self, remaining: float | None) -> None:
        self.remaining_at_entry = remaining
        self.entered.set()

    def settle(self, outcome: str) -> None:
        self.outcome = outcome
        self.settled.set()


class ChainLab:
    """client -> hop 1 (``/lab.Chain/Hop1``) -> hop 2 (``/lab.Chain/Hop2``), all on 127.0.0.1."""

    def __init__(
        self,
        propagation: Propagation,
        *,
        stall: float = 0.2,
        reserve: float = 0.0,
    ) -> None:
        self.propagation = propagation
        self.stall = stall
        self.reserve = reserve
        self.hop1 = HopRecord("hop1")
        self.hop2 = HopRecord("hop2")
        self.detached: list[asyncio.Future[bytes]] = []
        self._servers: list[grpc.aio.Server] = []
        self._channels: list[grpc.aio.Channel] = []
        self.hop1_port = 0
        self.hop2_port = 0

    async def __aenter__(self) -> ChainLab:
        self.hop2_port = await self._serve("Hop2", self._hop2)
        downstream = grpc.aio.insecure_channel(f"127.0.0.1:{self.hop2_port}")
        self._channels.append(downstream)
        self._downstream = downstream.unary_unary("/lab.Chain/Hop2")
        self.hop1_port = await self._serve("Hop1", self._hop1)
        upstream = grpc.aio.insecure_channel(f"127.0.0.1:{self.hop1_port}")
        self._channels.append(upstream)
        self._upstream = upstream.unary_unary("/lab.Chain/Hop1")
        return self

    def start_call(self, timeout: float | None) -> grpc.aio.UnaryUnaryCall:
        """Start the client call to hop 1; ``timeout=None`` means no deadline at all."""
        return self._upstream(b"ping", timeout=timeout)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for task in self.detached:
            task.cancel()
        for channel in self._channels:
            await channel.close()
        for server in self._servers:
            await server.stop(0)

    async def _serve(
        self,
        method: str,
        behavior: Callable[[bytes, grpc.aio.ServicerContext], Awaitable[bytes]],
    ) -> int:
        handler = grpc.unary_unary_rpc_method_handler(behavior)
        server = grpc.aio.server()
        server.add_generic_rpc_handlers(
            (grpc.method_handlers_generic_handler("lab.Chain", {method: handler}),)
        )
        port = int(server.add_insecure_port("127.0.0.1:0"))
        await server.start()
        self._servers.append(server)
        return port

    async def _hop2(self, request: bytes, context: grpc.aio.ServicerContext) -> bytes:
        self.hop2.enter(context.time_remaining())
        try:
            await asyncio.sleep(self.stall)
        except asyncio.CancelledError:
            self.hop2.settle("cancelled")
            raise
        self.hop2.settle("completed")
        return b"hop2-done"

    async def _hop1(self, request: bytes, context: grpc.aio.ServicerContext) -> bytes:
        self.hop1.enter(context.time_remaining())
        try:
            reply = await self._call_downstream(request, context)
        except asyncio.CancelledError:
            self.hop1.settle("cancelled")
            raise
        except grpc.aio.AioRpcError:
            self.hop1.settle("downstream-error")
            raise
        self.hop1.settle("completed")
        return reply

    async def _call_downstream(self, request: bytes, context: grpc.aio.ServicerContext) -> bytes:
        mode = self.propagation
        if mode is Propagation.CANCEL_ONLY:
            return bytes(await self._downstream(request))
        if mode is Propagation.DETACHED:
            task = asyncio.ensure_future(self._downstream(request))
            self.detached.append(task)
            return bytes(await asyncio.shield(task))
        timeout = outgoing_timeout(context.time_remaining(), reserve=self.reserve)
        if mode is Propagation.MAPPED_ERROR:
            try:
                return bytes(await self._downstream(request, timeout=timeout))
            except grpc.aio.AioRpcError as error:
                await context.abort(error.code(), f"downstream: {error.details()}")
        return bytes(await self._downstream(request, timeout=timeout))


# --- the raw HTTP/2 view -------------------------------------------------------------------


@dataclass(frozen=True)
class RawResult:
    """The response headers of one raw HTTP/2 call to a gRPC server."""

    headers: dict[str, str]
    seconds: float


class WireProbe:
    """Speaks just enough HTTP/2 to see or send a ``grpc-timeout`` header with no gRPC library."""

    @staticmethod
    async def capture_client_headers(call_timeout: float) -> dict[str, str]:
        """Run a real ``grpc.aio`` client against a fake server and return its request headers."""
        seen: list[dict[str, str]] = []

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            conn = h2.connection.H2Connection(
                config=h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
            )
            conn.initiate_connection()
            writer.write(conn.data_to_send())
            while not seen:
                data = await reader.read(65536)
                if not data:
                    break
                for event in conn.receive_data(data):
                    if isinstance(event, h2.events.RequestReceived):
                        seen.append({str(k): str(v) for k, v in event.headers})
                        conn.send_headers(
                            event.stream_id,
                            [
                                (":status", "200"),
                                ("content-type", "application/grpc"),
                                ("grpc-status", "12"),
                            ],
                            end_stream=True,
                        )
                writer.write(conn.data_to_send())
                await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            with contextlib.suppress(grpc.aio.AioRpcError):
                await channel.unary_unary("/lab.Chain/X")(b"", timeout=call_timeout)
        server.close()
        await server.wait_closed()
        return seen[0]

    @staticmethod
    async def reset_code_after_client_cancel() -> int:
        """Cancel a real ``grpc.aio`` call against a silent peer; return the RST_STREAM code."""
        codes: list[int] = []
        requested = asyncio.Event()
        reset = asyncio.Event()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            conn = h2.connection.H2Connection(
                config=h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
            )
            conn.initiate_connection()
            writer.write(conn.data_to_send())
            while not codes:
                data = await reader.read(65536)
                if not data:
                    break
                for event in conn.receive_data(data):
                    if isinstance(event, h2.events.RequestReceived):
                        requested.set()
                    if isinstance(event, h2.events.StreamReset):
                        codes.append(int(event.error_code))
                        reset.set()
                writer.write(conn.data_to_send())
                await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            call = channel.unary_unary("/lab.Chain/X")(b"")
            await asyncio.wait_for(requested.wait(), 3.0)
            call.cancel()
            await asyncio.wait_for(reset.wait(), 3.0)
        server.close()
        await server.wait_closed()
        return codes[0]

    @staticmethod
    async def raw_unary_call(port: int, path: str, grpc_timeout: str | None) -> RawResult:
        """Send an empty unary request carrying a literal ``grpc-timeout`` and read the reply."""
        loop = asyncio.get_running_loop()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=True, header_encoding="utf-8")
        )
        conn.initiate_connection()
        headers = [
            (":method", "POST"),
            (":scheme", "http"),
            (":path", path),
            (":authority", "lab"),
            ("te", "trailers"),
            ("content-type", "application/grpc"),
        ]
        if grpc_timeout is not None:
            headers.append(("grpc-timeout", grpc_timeout))
        stream_id = conn.get_next_available_stream_id()
        conn.send_headers(stream_id, headers)
        conn.send_data(stream_id, b"\x00\x00\x00\x00\x00", end_stream=True)
        writer.write(conn.data_to_send())
        started = loop.time()
        collected: dict[str, str] = {}
        try:
            while True:
                data = await asyncio.wait_for(reader.read(65536), 3.0)
                if not data:
                    break
                finished = False
                for event in conn.receive_data(data):
                    if isinstance(event, h2.events.ResponseReceived | h2.events.TrailersReceived):
                        collected.update({str(k): str(v) for k, v in event.headers or []})
                    if isinstance(event, h2.events.StreamEnded | h2.events.StreamReset):
                        finished = True
                writer.write(conn.data_to_send())
                if finished:
                    break
        finally:
            writer.close()
        return RawResult(collected, loop.time() - started)
