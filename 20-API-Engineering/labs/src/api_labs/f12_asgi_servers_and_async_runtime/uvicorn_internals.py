"""Drive Uvicorn 0.54.0 HTTP protocol classes in-process over an in-memory transport.

The lab instantiates ``H11Protocol`` and ``HttpToolsProtocol`` exactly as ``Server.startup``
does (``config``, ``server_state``, ``app_state``, ``_loop``), feeds raw request bytes into
``data_received``, and records every byte, read pause, and close the protocol performs.
No socket is opened, so ``--limit-concurrency``, ``FlowControl`` watermarks, pipelining,
keep-alive, and graceful-shutdown behavior are observable deterministically.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from uvicorn.config import Config
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from uvicorn.server import ServerState

ProtocolKind = Literal["h11", "httptools"]
HttpProtocol = H11Protocol | HttpToolsProtocol

PROTOCOLS: dict[ProtocolKind, type[HttpProtocol]] = {
    "h11": H11Protocol,
    "httptools": HttpToolsProtocol,
}


class MemoryTransport(asyncio.Transport):
    """An ``asyncio.Transport`` that keeps written bytes and counts flow-control calls."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self.loop = loop
        self.protocol: asyncio.BaseProtocol | None = None
        self.output = bytearray()
        self.closed = False
        self.pause_reading_calls = 0
        self.resume_reading_calls = 0

    def write(self, data: bytes | bytearray | memoryview[int]) -> None:
        self.output += data

    def close(self) -> None:
        # Like a selector transport: mark closing now, deliver connection_lost on the next turn.
        if self.closed:
            return
        self.closed = True
        if self.protocol is not None:
            self.loop.call_soon(self.protocol.connection_lost, None)

    def is_closing(self) -> bool:
        return self.closed

    def pause_reading(self) -> None:
        self.pause_reading_calls += 1

    def resume_reading(self) -> None:
        self.resume_reading_calls += 1

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return default  # no socket, peername, or sslcontext: scope client/server are None

    def set_protocol(self, protocol: asyncio.BaseProtocol) -> None:
        self.protocol = protocol


@dataclass(slots=True)
class Connection:
    """One accepted connection: the protocol instance and the transport it writes to."""

    protocol: HttpProtocol
    transport: MemoryTransport

    def feed(self, data: bytes) -> None:
        self.protocol.data_received(data)

    def lose(self) -> None:
        self.protocol.connection_lost(None)


def build_config(app: Callable[..., Any], kind: ProtocolKind, **overrides: Any) -> Config:
    """A loaded ``Config`` with lifespan off, as ``Server._serve`` would load it."""
    config = Config(app=app, http=kind, lifespan="off", log_level="critical", **overrides)
    config.load()
    return config


def open_connection(
    config: Config, state: ServerState, loop: asyncio.AbstractEventLoop
) -> Connection:
    """Mirror ``Server.startup.create_protocol`` followed by the loop's ``connection_made``."""
    # Config.load resolved --http through HTTP_PROTOCOLS to the class Server.startup instantiates.
    protocol_class = next(c for c in PROTOCOLS.values() if c is config.http_protocol_class)
    protocol = protocol_class(config=config, server_state=state, app_state={}, _loop=loop)
    transport = MemoryTransport(loop)
    transport.set_protocol(protocol)
    protocol.connection_made(transport)
    return Connection(protocol=protocol, transport=transport)


async def settle(turns: int = 20) -> None:
    """Yield to the event loop enough times for spawned ASGI tasks to run to a blocking await."""
    for _ in range(turns):
        await asyncio.sleep(0)


def status_codes(wire: bytes | bytearray) -> list[int]:
    """Every HTTP/1.1 status code that appears at the start of a response line in ``wire``."""
    codes: list[int] = []
    for line in bytes(wire).split(b"\r\n"):
        if line.startswith(b"HTTP/1.1 ") and line[9:12].isdigit():
            codes.append(int(line[9:12]))
    return codes


def response_head(wire: bytes | bytearray) -> bytes:
    """The status line and header block of the first response, lowercased for comparison."""
    head, _, _ = bytes(wire).partition(b"\r\n\r\n")
    return head.lower()


def get_request(path: str = "/", version: str = "1.1") -> bytes:
    host = b"Host: test\r\n" if version == "1.1" else b""
    return f"GET {path} HTTP/{version}\r\n".encode() + host + b"\r\n"
