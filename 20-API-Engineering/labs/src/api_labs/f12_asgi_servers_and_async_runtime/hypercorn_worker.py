"""Hypercorn 0.18.0 ASGI worker runtime and multiplexed protocol harness.

Provides in-memory execution adapters for Hypercorn's H2Protocol (h2 4.4.1),
QUIC datagram parsing (aioquic 1.7.2), priority tree scheduling, and
bidirectional ASGI stream multiplexing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import h2.config
import h2.connection
import h2.events
from aioquic._buffer import Buffer
from aioquic.quic.packet import PACKET_TYPE_INITIAL, pull_quic_header
from hypercorn.app_wrappers import ASGIWrapper
from hypercorn.asyncio.task_group import TaskGroup
from hypercorn.asyncio.worker_context import EventWrapper, WorkerContext
from hypercorn.config import Config
from hypercorn.events import Closed, RawData
from hypercorn.protocol.h2 import BUFFER_HIGH_WATER, BUFFER_LOW_WATER, H2Protocol, StreamBuffer
from hypercorn.typing import ConnectionState

if TYPE_CHECKING:
    from hypercorn.typing import ASGIFramework


@dataclass
class StreamResponse:
    """Captured HTTP/2 response stream events."""

    stream_id: int
    status_code: int = 0
    headers: list[tuple[bytes, bytes]] = field(default_factory=list[tuple[bytes, bytes]])
    body: bytearray = field(default_factory=bytearray)
    trailers: list[tuple[bytes, bytes]] = field(default_factory=list[tuple[bytes, bytes]])
    ended: bool = False
    completed_event: asyncio.Event = field(default_factory=asyncio.Event)


class InProcessH2Harness:
    """Deterministic in-memory test harness connecting client h2 and server H2Protocol."""

    def __init__(
        self,
        app: ASGIFramework,
        config: Config | None = None,
    ) -> None:
        self.app = app
        self.config = config or Config()
        self.context = WorkerContext(None)
        self.loop = asyncio.get_running_loop()

        self.client_conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=True, header_encoding=None)
        )
        self.responses: dict[int, StreamResponse] = {}
        self.completion_order: list[int] = []

        self._server_to_client: list[bytes] = []
        self._server_protocol: H2Protocol | None = None
        self._task_group: TaskGroup | None = None
        self._tg_context: Any = None

    async def __aenter__(self) -> InProcessH2Harness:
        self._tg_context = TaskGroup(self.loop)
        self._task_group = await self._tg_context.__aenter__()

        async def server_send(event: Any) -> None:
            if isinstance(event, RawData):
                self._server_to_client.append(event.data)

        if self._task_group is None:
            raise RuntimeError("TaskGroup initialization failed")
        self._server_protocol = H2Protocol(
            app=ASGIWrapper(self.app),
            config=self.config,
            context=self.context,
            task_group=self._task_group,
            connection_state=ConnectionState({}),
            ssl=False,
            client=("127.0.0.1", 50000),
            server=("127.0.0.1", 8000),
            send=server_send,
        )

        self.client_conn.initiate_connection()
        await self._server_protocol.initiate()
        # Feed client preface and initial settings
        await self._server_protocol.handle(RawData(self.client_conn.data_to_send()))
        self._deliver_to_client()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._server_protocol is not None:
            self._server_protocol.closed = True
            await self._server_protocol.handle(Closed())
        if self._tg_context is not None:
            await self._tg_context.__aexit__(exc_type, exc_val, exc_tb)

    @property
    def server_protocol(self) -> H2Protocol:
        """Return the active server protocol instance."""
        if self._server_protocol is None:
            raise RuntimeError("Harness not entered")
        return self._server_protocol

    def _deliver_to_client(self) -> None:
        """Flush bytes queued by the server into the client state machine."""
        buffer_copy = list(self._server_to_client)
        self._server_to_client.clear()
        for raw_bytes in buffer_copy:
            events = self.client_conn.receive_data(raw_bytes)
            for event in events:
                self._process_client_event(event)

    def _process_client_event(self, event: h2.events.Event) -> None:
        """Route client events into captured stream structures."""
        if isinstance(event, h2.events.ResponseReceived):
            resp = self.responses.setdefault(event.stream_id, StreamResponse(event.stream_id))
            status_bytes = dict(event.headers).get(b":status", b"0")
            resp.status_code = int(status_bytes.decode("ascii"))
            resp.headers = list(event.headers)
        elif isinstance(event, h2.events.DataReceived):
            resp = self.responses.setdefault(event.stream_id, StreamResponse(event.stream_id))
            resp.body.extend(event.data)
            self.client_conn.acknowledge_received_data(
                event.flow_controlled_length, event.stream_id
            )
        elif isinstance(event, h2.events.TrailersReceived):
            resp = self.responses.setdefault(event.stream_id, StreamResponse(event.stream_id))
            resp.trailers = list(event.headers)
        elif isinstance(event, h2.events.StreamEnded):
            resp = self.responses.setdefault(event.stream_id, StreamResponse(event.stream_id))
            resp.ended = True
            resp.completed_event.set()
            self.completion_order.append(event.stream_id)

    async def send_request(
        self,
        stream_id: int,
        headers: list[tuple[bytes, bytes]],
        body: bytes = b"",
        end_stream: bool = True,
    ) -> None:
        """Send an HTTP/2 request frame sequence from the client to the server."""
        self.responses[stream_id] = StreamResponse(stream_id)
        has_body = len(body) > 0 or not end_stream
        self.client_conn.send_headers(stream_id, headers, end_stream=not has_body)
        if body:
            self.client_conn.send_data(stream_id, body, end_stream=end_stream)
        elif not end_stream:
            pass  # stream remains open for subsequent data
        await self.server_protocol.handle(RawData(self.client_conn.data_to_send()))
        self._deliver_to_client()

    async def drain_server(self) -> None:
        """Poll and flush any pending server bytes and background task events."""
        # Yield to event loop tasks
        await asyncio.sleep(0.01)
        self._deliver_to_client()


def parse_quic_initial_header(datagram: bytes) -> tuple[bool, bytes, bytes, int | None]:
    """Parse and validate an inbound QUIC datagram against RFC 9000 Initial requirements.

    Returns:
        (is_valid_initial, destination_connection_id, source_connection_id, version)
    """
    if len(datagram) < 1200:
        return False, b"", b"", None

    try:
        header = pull_quic_header(Buffer(data=datagram), host_cid_length=8)
    except ValueError:
        return False, b"", b"", None

    is_initial = header.packet_type == PACKET_TYPE_INITIAL
    return is_initial, header.destination_cid, header.source_cid, header.version


class StreamBufferAudit:
    """Audits Hypercorn StreamBuffer flow-control backpressure and edge cases."""

    def __init__(self) -> None:
        self.buffer = StreamBuffer(EventWrapper)

    async def push_chunk(self, data: bytes) -> asyncio.Task[None]:
        """Push a chunk asynchronously, returning the active coroutine Task."""
        return asyncio.create_task(self.buffer.push(data))

    async def pop_chunk(self, max_length: int) -> bytes:
        """Pop up to max_length bytes from the buffer."""
        return await self.buffer.pop(max_length)

    @property
    def buffer_length(self) -> int:
        """Return the current buffer length in bytes."""
        return len(self.buffer.buffer)

    @property
    def high_water_mark(self) -> int:
        """Return the configured high water threshold (32768 bytes)."""
        return BUFFER_HIGH_WATER

    @property
    def low_water_mark(self) -> float:
        """Return the configured low water threshold (16384 bytes)."""
        return BUFFER_LOW_WATER
