"""Stream an unbounded collection as NDJSON from an async SQLAlchemy server-side cursor."""

from __future__ import annotations

import json
from contextlib import aclosing
from typing import TYPE_CHECKING

from sqlalchemy import Integer, Select, String, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from starlette.requests import Request
    from starlette.types import Receive, Scope, Send

NDJSON_MEDIA_TYPE = "application/x-ndjson"
DEFAULT_YIELD_PER = 500
DEFAULT_BATCH_BYTES = 64 * 1024


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


class ExportIncompleteError(ValueError):
    """The body ended without the terminal `end` record, so rows may be missing."""


def encode_line(record: Mapping[str, object]) -> bytes:
    """Encode one RFC 8259 JSON text followed by exactly one LF (0x0A)."""
    # json.dumps escapes U+0000-U+001F inside strings, so LF and CR never appear raw.
    # allow_nan=False refuses NaN and Infinity, which are not valid JSON numbers.
    text = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return text.encode("utf-8") + b"\n"


def export_statement(after_id: int = 0) -> Select[Item]:
    """Keyset-ordered SELECT so an aborted export can resume after the last id seen."""
    return select(Item).where(Item.id > after_id).order_by(Item.id)


async def export_lines(
    session_factory: async_sessionmaker[AsyncSession],
    statement: Select[Item],
    *,
    yield_per: int = DEFAULT_YIELD_PER,
    batch_bytes: int = DEFAULT_BATCH_BYTES,
) -> AsyncGenerator[bytes]:
    """Yield NDJSON bytes in batches of about `batch_bytes`, ending with an end or error record."""
    count = 0
    last_id: int | None = None
    buffer = bytearray()
    # The session lives inside the generator, so it lives exactly as long as the stream.
    async with session_factory() as session:
        try:
            result = await session.stream_scalars(statement.execution_options(yield_per=yield_per))
            async for item in result:
                buffer += encode_line({"type": "item", "id": item.id, "name": item.name})
                count += 1
                last_id = item.id
                if len(buffer) >= batch_bytes:
                    yield bytes(buffer)
                    buffer.clear()
        except SQLAlchemyError:
            # The 200 status line is already on the wire; report the failure in-band.
            buffer += encode_line({"type": "error", "code": "export_aborted", "last_id": last_id})
            yield bytes(buffer)
            return
    buffer += encode_line({"type": "end", "count": count, "last_id": last_id})
    yield bytes(buffer)


class NDJSONStreamingResponse(StreamingResponse):
    """StreamingResponse that closes its generator deterministically on every exit path."""

    media_type = NDJSON_MEDIA_TYPE

    def __init__(self, content: AsyncGenerator[bytes], headers: Mapping[str, str]) -> None:
        super().__init__(content, headers=headers)
        self._generator = content

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # starlette 1.10.0 never calls aclose() on body_iterator; without this, a client
        # disconnect leaves the generator, its session, and its pooled connection open
        # until garbage collection finalizes the generator at some later, unbounded time.
        async with aclosing(self._generator):
            await super().__call__(scope, receive, send)


EXPORT_HEADERS = {"cache-control": "no-store", "x-accel-buffering": "no"}


def ndjson_response(body: AsyncGenerator[bytes]) -> Response:
    """The fixed response: closes the generator on completion, error, and disconnect."""
    return NDJSONStreamingResponse(body, headers=EXPORT_HEADERS)


def plain_streaming_response(body: AsyncGenerator[bytes]) -> Response:
    """The trap: a bare StreamingResponse that leaves generator cleanup to the GC."""
    return StreamingResponse(body, headers=EXPORT_HEADERS, media_type=NDJSON_MEDIA_TYPE)


def build_export_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    yield_per: int = DEFAULT_YIELD_PER,
    batch_bytes: int = DEFAULT_BATCH_BYTES,
    statement_hook: Callable[[Select[Item]], Select[Item]] | None = None,
    make_response: Callable[[AsyncGenerator[bytes]], Response] = ndjson_response,
) -> Starlette:
    """Build `GET /items/export?after_id=N`; `statement_hook` lets tests instrument the query."""

    async def export(request: Request) -> Response:
        raw_after = request.query_params.get("after_id", "0")
        if not raw_after.isascii() or not raw_after.isdigit() or len(raw_after) > 18:
            return PlainTextResponse("after_id must be a non-negative integer", status_code=400)
        statement = export_statement(int(raw_after))
        if statement_hook is not None:
            statement = statement_hook(statement)
        lines = export_lines(
            session_factory, statement, yield_per=yield_per, batch_bytes=batch_bytes
        )
        return make_response(lines)

    return Starlette(routes=[Route("/items/export", export, methods=["GET"])])


def parse_export(body: bytes) -> list[dict[str, object]]:
    """Parse a complete export; raise ExportIncompleteError on truncation or an error record."""
    if not body.endswith(b"\n"):
        raise ExportIncompleteError("body does not end with LF: the last line is partial")
    records: list[dict[str, object]] = []
    for line in body.split(b"\n")[:-1]:
        record: object = json.loads(line)
        if not isinstance(record, dict):
            raise ExportIncompleteError("every NDJSON line in this export must be an object")
        records.append(record)  # pyright: ignore[reportUnknownArgumentType]
    if not records or records[-1].get("type") != "end":
        last = records[-1] if records else None
        raise ExportIncompleteError(f"no terminal end record; last record was {last!r}")
    items = records[:-1]
    if records[-1].get("count") != len(items):
        raise ExportIncompleteError("end record count does not match the number of items")
    return items
