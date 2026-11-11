"""Lab: NDJSON exports streamed from an async SQLAlchemy server-side cursor through Starlette."""

from __future__ import annotations

import asyncio
import gc
import inspect
import json
import sys
import tracemalloc
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import h11
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, func, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.requests import ClientDisconnect

from api_labs.f05_pagination_filtering_and_query_patterns.ndjson_export import (
    DEFAULT_BATCH_BYTES,
    DEFAULT_YIELD_PER,
    NDJSON_MEDIA_TYPE,
    Base,
    ExportIncompleteError,
    Item,
    build_export_app,
    encode_line,
    ndjson_response,
    parse_export,
    plain_streaming_response,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable

    from sqlalchemy import Select
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.types import Message, Scope

ROWS = 50_000
TRICKY_NAMES = {7: "line\nbreak\r", 8: 'quote" back\\slash', 9: "sep\u2028", 10: "caf\u00e9"}


@dataclass
class DbProbe:
    """Counts rows the SQLite VM has stepped and pooled connections currently checked out."""

    rows_stepped: int = 0
    checked_out: int = 0
    fail_at_id: int | None = None

    def row_probe(self, item_id: int) -> int:
        self.rows_stepped += 1
        if self.fail_at_id is not None and item_id == self.fail_at_id:
            raise RuntimeError("injected storage fault")
        return 1


@dataclass
class ExportDb:
    sessions: async_sessionmaker[AsyncSession]
    probe: DbProbe


def probed(statement: Select[Item]) -> Select[Item]:
    """Route every scanned row through row_probe() so tests can see how far the cursor read."""
    return statement.where(func.row_probe(Item.id) == 1)


@pytest_asyncio.fixture
async def export_db() -> AsyncIterator[ExportDb]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    probe = DbProbe()

    def on_connect(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.create_function("row_probe", 1, probe.row_probe)

    def on_checkout(*_args: Any) -> None:
        probe.checked_out += 1

    def on_checkin(*_args: Any) -> None:
        probe.checked_out -= 1

    event.listen(engine.sync_engine, "connect", on_connect)
    event.listen(engine.sync_engine, "checkout", on_checkout)
    event.listen(engine.sync_engine, "checkin", on_checkin)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        rows = [{"id": i, "name": TRICKY_NAMES.get(i, f"item-{i:05d}")} for i in range(1, ROWS + 1)]
        await conn.execute(insert(Item), rows)
    try:
        yield ExportDb(async_sessionmaker(engine, expire_on_commit=False), probe)
    finally:
        await engine.dispose()


def make_app(
    db: ExportDb,
    *,
    batch_bytes: int = DEFAULT_BATCH_BYTES,
    make_response: Callable[[AsyncGenerator[bytes]], Response] = ndjson_response,
) -> Starlette:
    return build_export_app(
        db.sessions, batch_bytes=batch_bytes, statement_hook=probed, make_response=make_response
    )


def http_scope(query: bytes = b"", spec_version: str = "2.4") -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/items/export",
        "raw_path": b"/items/export",
        "query_string": query,
        "root_path": "",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }


@dataclass
class Capture:
    messages: list[Message] = field(default_factory=list["Message"])
    rows_stepped_at_body: list[int] = field(default_factory=list[int])
    body_bytes: int = 0

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.messages[1:])


Disconnect = Literal["none", "send-raises-oserror", "stalled-send-then-disconnect"]


async def run_asgi(
    app: Starlette,
    probe: DbProbe,
    *,
    query: bytes = b"",
    disconnect: Disconnect = "none",
    at_body: int = 2,
    keep_bodies: bool = True,
) -> Capture:
    """Drive the app with a scripted server: normal, ASGI 2.4 OSError, or 2.3 disconnect event."""
    capture = Capture()
    stalled = asyncio.Event()
    request_delivered = False

    async def receive() -> Message:
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        if disconnect == "stalled-send-then-disconnect":
            await stalled.wait()
            return {"type": "http.disconnect"}
        await asyncio.Event().wait()  # no disconnect: block until the stream cancels us
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            index = len(capture.rows_stepped_at_body) + 1
            if index == at_body and disconnect == "send-raises-oserror":
                raise OSError("client went away")  # what an ASGI spec_version 2.4 server raises
            if index == at_body and disconnect == "stalled-send-then-disconnect":
                stalled.set()
                await asyncio.Event().wait()  # socket buffer full: send never returns
            capture.rows_stepped_at_body.append(probe.rows_stepped)
            capture.body_bytes += len(message.get("body", b""))
            if not keep_bodies:
                message = {**message, "body": b""}
        capture.messages.append(message)

    spec = "2.3" if disconnect == "stalled-send-then-disconnect" else "2.4"
    await app(http_scope(query, spec_version=spec), receive, send)
    return capture


def to_http11_wire(messages: list[Message], http_version: bytes) -> tuple[bytes, list[str]]:
    """Feed captured ASGI messages into an h11 server connection; return wire bytes and headers."""
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(b"GET /items/export HTTP/" + http_version + b"\r\nHost: test\r\n\r\n")
    assert isinstance(conn.next_event(), h11.Request)
    start = messages[0]
    wire = conn.send(h11.Response(status_code=start["status"], headers=start["headers"])) or b""
    header_lines = wire.decode("ascii").lower().split("\r\n")
    for message in messages[1:]:
        body: bytes = message.get("body", b"")
        if body:
            wire += conn.send(h11.Data(data=body)) or b""
    wire += conn.send(h11.EndOfMessage()) or b""
    return wire, header_lines


@pytest.mark.unit
def test_encode_line_escapes_control_characters_and_rejects_nan() -> None:
    line = encode_line({"id": 7, "name": TRICKY_NAMES[7] + TRICKY_NAMES[9], "x": "\x00\x1f"})
    assert line.count(b"\n") == 1
    assert line.endswith(b"\n")
    assert b"\r" not in line
    expected = b'{"id":7,"name":"line\\nbreak\\r' + "sep\u2028".encode()
    assert line == expected + b'","x":"\\u0000\\u001f"}\n'
    assert json.loads(line) == {"id": 7, "name": "line\nbreak\rsep\u2028", "x": "\x00\x1f"}
    with pytest.raises(ValueError, match="not JSON compliant"):
        encode_line({"price": float("nan")})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_streams_every_row_in_id_order_with_end_record(export_db: ExportDb) -> None:
    transport = httpx.ASGITransport(app=make_app(export_db))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/items/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == NDJSON_MEDIA_TYPE
    assert "content-length" not in response.headers
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-store"
    items = parse_export(response.content)
    assert json.loads(response.content.split(b"\n")[-2]) == {
        "type": "end",
        "count": ROWS,
        "last_id": ROWS,
    }
    assert [item["id"] for item in items] == list(range(1, ROWS + 1))
    assert {i: items[i - 1]["name"] for i in TRICKY_NAMES} == TRICKY_NAMES
    assert response.content.count(b"\n") == ROWS + 1
    assert export_db.probe.checked_out == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_first_body_chunk_is_sent_before_the_cursor_reads_ahead(export_db: ExportDb) -> None:
    capture = await run_asgi(make_app(export_db), export_db.probe)
    bodies = [m["body"] for m in capture.messages[1:]]
    rows_in_first_chunk = bodies[0].count(b"\n")
    print(
        f"\nfirst body: {len(bodies[0])} B, {rows_in_first_chunk} rows;"
        f" rows stepped by SQLite at first send: {capture.rows_stepped_at_body[0]} of {ROWS}"
    )
    assert len(bodies[0]) >= DEFAULT_BATCH_BYTES
    # The database is never more than one yield_per batch (+1 sqlite3 look-ahead) ahead of the wire.
    assert capture.rows_stepped_at_body[0] <= rows_in_first_chunk + DEFAULT_YIELD_PER + 1
    assert capture.rows_stepped_at_body[0] < ROWS // 10
    assert capture.rows_stepped_at_body == sorted(capture.rows_stepped_at_body)
    assert capture.messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}


async def traced_peak(db: ExportDb, *, rows: int, batch_bytes: int) -> int:
    app = make_app(db, batch_bytes=batch_bytes)
    query = f"after_id={ROWS - rows}".encode()
    gc.collect()
    tracemalloc.start()
    try:
        capture = await run_asgi(app, db.probe, query=query, keep_bodies=False)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert capture.body_bytes > rows * 20
    return peak


@pytest.mark.unit
@pytest.mark.asyncio
async def test_peak_traced_memory_is_flat_in_row_count_only_when_streaming(
    export_db: ExportDb,
) -> None:
    streamed_5k = await traced_peak(export_db, rows=5_000, batch_bytes=DEFAULT_BATCH_BYTES)
    streamed_50k = await traced_peak(export_db, rows=ROWS, batch_bytes=DEFAULT_BATCH_BYTES)
    buffered_50k = await traced_peak(export_db, rows=ROWS, batch_bytes=sys.maxsize)
    print(f"\npeak traced bytes: streamed 5k={streamed_5k} 50k={streamed_50k}", end=" ")
    print(f"buffered 50k={buffered_50k}")
    assert streamed_50k < 1.5 * streamed_5k
    assert buffered_50k > 3 * streamed_50k


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mid_stream_database_error_is_reported_in_band_after_200(
    export_db: ExportDb,
) -> None:
    export_db.probe.fail_at_id = 30_000
    transport = httpx.ASGITransport(app=make_app(export_db))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/items/export")
    assert response.status_code == 200
    with pytest.raises(ExportIncompleteError, match="export_aborted"):
        parse_export(response.content)
    records = [json.loads(line) for line in response.content.splitlines()]
    error = records[-1]
    assert error["type"] == "error"
    assert [r["id"] for r in records[:-1]] == list(range(1, error["last_id"] + 1))
    assert error["last_id"] < 30_000
    assert all(r["type"] != "end" for r in records)
    assert export_db.probe.checked_out == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_after_last_id_completes_the_export_exactly_once(export_db: ExportDb) -> None:
    export_db.probe.fail_at_id = 30_000
    transport = httpx.ASGITransport(app=make_app(export_db))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/items/export")
        error = json.loads(first.content.splitlines()[-1])
        export_db.probe.fail_at_id = None
        second = await client.get("/items/export", params={"after_id": error["last_id"]})
        rejected = await client.get("/items/export", params={"after_id": "-1"})
        # 18 digits always fit a signed 64-bit key; 19 digits can overflow SQLite INTEGER.
        largest = await client.get("/items/export", params={"after_id": "9" * 18})
        too_long = await client.get("/items/export", params={"after_id": "9" * 19})
        # U+0661 ARABIC-INDIC DIGIT ONE passes str.isdigit() and int(), but is not ASCII.
        non_ascii = await client.get("/items/export", params={"after_id": chr(0x0661)})
    first_ids = [json.loads(line)["id"] for line in first.content.splitlines()[:-1]]
    second_ids = [item["id"] for item in parse_export(second.content)]
    assert first_ids + second_ids == list(range(1, ROWS + 1))
    assert rejected.status_code == 400
    assert largest.status_code == 200
    assert parse_export(largest.content) == []
    assert too_long.status_code == 400
    assert non_ascii.status_code == 400


@pytest.mark.unit
def test_parser_rejects_truncation_at_every_byte_offset() -> None:
    body = b"".join(encode_line({"type": "item", "id": i, "name": "n"}) for i in (1, 2, 3))
    body += encode_line({"type": "end", "count": 3, "last_id": 3})
    assert [item["id"] for item in parse_export(body)] == [1, 2, 3]
    for cut in range(len(body)):
        prefix = body[:cut]
        reason = "no terminal end record" if prefix.endswith(b"\n") else "partial"
        with pytest.raises(ExportIncompleteError, match=reason):
            parse_export(prefix)
    dropped_line = body.replace(encode_line({"type": "item", "id": 2, "name": "n"}), b"")
    with pytest.raises(ExportIncompleteError, match="count does not match"):
        parse_export(dropped_line)
    with pytest.raises(ExportIncompleteError, match="must be an object"):
        parse_export(b"[1]\n" + body)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", ["send-raises-oserror", "stalled-send-then-disconnect"])
async def test_plain_streaming_response_keeps_the_connection_checked_out_after_disconnect(
    export_db: ExportDb, disconnect: Disconnect
) -> None:
    leaked: list[AsyncGenerator[bytes]] = []

    def recording_plain_response(body: AsyncGenerator[bytes]) -> Response:
        leaked.append(body)
        return plain_streaming_response(body)

    app = make_app(export_db, make_response=recording_plain_response)
    if disconnect == "send-raises-oserror":
        with pytest.raises(ClientDisconnect):
            await run_asgi(app, export_db.probe, disconnect=disconnect)
    else:
        await run_asgi(app, export_db.probe, disconnect=disconnect)
    # starlette 1.10.0 returned without closing the generator: session and connection still held.
    assert export_db.probe.checked_out == 1
    assert inspect.getasyncgenstate(leaked[0]) == inspect.AGEN_SUSPENDED  # finally not yet run
    await leaked[0].aclose()  # the explicit close NDJSONStreamingResponse performs
    assert export_db.probe.checked_out == 0


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", ["send-raises-oserror", "stalled-send-then-disconnect"])
async def test_ndjson_response_releases_the_connection_immediately_on_disconnect(
    export_db: ExportDb, disconnect: Disconnect
) -> None:
    app = make_app(export_db)
    if disconnect == "send-raises-oserror":
        with pytest.raises(ClientDisconnect):
            await run_asgi(app, export_db.probe, disconnect=disconnect)
    else:
        await run_asgi(app, export_db.probe, disconnect=disconnect)
    assert export_db.probe.checked_out == 0
    assert export_db.probe.rows_stepped < ROWS // 10


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("http_version", [b"1.1", b"1.0"])
async def test_unknown_length_body_is_chunked_on_http11_and_close_delimited_on_http10(
    export_db: ExportDb, http_version: bytes
) -> None:
    capture = await run_asgi(make_app(export_db), export_db.probe, query=b"after_id=49990")
    wire, headers = to_http11_wire(capture.messages, http_version)
    assert not any(h.startswith("content-length:") for h in headers)
    if http_version == b"1.1":
        assert "transfer-encoding: chunked" in headers
        first_chunk = wire.split(b"\r\n\r\n", 1)[1]
        assert first_chunk.startswith(b"%x\r\n" % len(capture.body))
        assert wire.endswith(b"\r\n0\r\n\r\n")
    else:
        assert "connection: close" in headers
        assert not any(h.startswith("transfer-encoding:") for h in headers)
        assert wire.endswith(b"\r\n\r\n" + capture.body)
    assert len(parse_export(capture.body)) == 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coalescing_rows_cuts_asgi_messages_and_chunk_framing_overhead(
    export_db: ExportDb,
) -> None:
    query = b"after_id=40000"
    per_row = await run_asgi(make_app(export_db, batch_bytes=1), export_db.probe, query=query)
    batched = await run_asgi(make_app(export_db), export_db.probe, query=query)
    assert per_row.body == batched.body
    per_row_wire, _ = to_http11_wire(per_row.messages, b"1.1")
    batched_wire, _ = to_http11_wire(batched.messages, b"1.1")
    per_row_overhead = len(per_row_wire) - len(batched_wire)
    print(
        f"\nbody={len(batched.body)} B; ASGI body messages per-row={len(per_row.messages) - 1}"
        f" batched={len(batched.messages) - 1}; extra chunk framing per-row={per_row_overhead} B"
    )
    assert len(per_row.messages) - 1 == 10_000 + 2  # one per row, the end record, the EOM
    assert len(batched.messages) - 1 <= len(batched.body) // DEFAULT_BATCH_BYTES + 2
    assert per_row_overhead > 10_000 * len(b"1a\r\n\r\n") - len(batched_wire) // 100
