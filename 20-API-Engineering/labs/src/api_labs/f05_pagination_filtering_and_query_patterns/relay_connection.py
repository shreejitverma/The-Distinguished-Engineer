"""Bidirectional keyset pagination behind a Relay connection envelope.

The collection is ``events`` ordered newest first: ``ORDER BY created_at DESC, id DESC``.
``first``/``after`` reads toward older rows and ``last``/``before`` toward newer rows, and both
directions return edges in that same collection order (GraphQL Cursor Connections, Section 4.3).
A cursor names a position (a sort key), not a row and not a direction, so one edge cursor works
as ``after`` and as ``before``, and it stays valid after its row is deleted.
"""

from __future__ import annotations

import base64
import binascii
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator  # noqa: TC003 strawberry reads annotations at runtime
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

import strawberry
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from strawberry import relay

if TYPE_CHECKING:
    from starlette.requests import Request

type Key = tuple[int, int]
"""The full, unique sort key ``(created_at, id)`` of one row."""

DEFAULT_PAGE_SIZE: Final = 20
MAX_PAGE_SIZE: Final = 100
INT64_MAX: Final = 2**63 - 1
MAX_CURSOR_LENGTH: Final = 64

SCHEMA: Final = (
    "CREATE TABLE events ("
    " id INTEGER NOT NULL PRIMARY KEY,"
    " created_at INTEGER NOT NULL,"
    " title TEXT NOT NULL"
    ") WITHOUT ROWID",
    "CREATE INDEX ix_events_created_id ON events (created_at, id)",
)

SELECT: Final = "SELECT created_at, id, title FROM events"
OLDER_THAN_AFTER: Final = "(created_at, id) < (?, ?)"
NEWER_THAN_BEFORE: Final = "(created_at, id) > (?, ?)"
NEWEST_FIRST: Final = "ORDER BY created_at DESC, id DESC"
OLDEST_FIRST: Final = "ORDER BY created_at ASC, id ASC"
# One index probe each: is there any row at or beyond the cursor on the other side?
EXISTS_AT_OR_NEWER: Final = "SELECT 1 FROM events WHERE (created_at, id) >= (?, ?) LIMIT 1"
EXISTS_AT_OR_OLDER: Final = "SELECT 1 FROM events WHERE (created_at, id) <= (?, ?) LIMIT 1"
# The classic backward bug: flip the comparison but keep the forward ORDER BY.
NAIVE_BEFORE_SQL: Final = f"{SELECT} WHERE {NEWER_THAN_BEFORE} {NEWEST_FIRST} LIMIT ?"

_CURSOR_BODY: Final = re.compile(r"v1:(0|[1-9][0-9]{0,18}):(0|[1-9][0-9]{0,18})")
_COUNT: Final = re.compile(r"0|[1-9][0-9]{0,8}")


class PaginationError(ValueError):
    """The client sent pagination arguments that cannot name a window."""


@dataclass(frozen=True, slots=True)
class Event:
    created_at: int
    id: int
    title: str

    @property
    def key(self) -> Key:
        return (self.created_at, self.id)


@dataclass(frozen=True, slots=True)
class Window:
    """Validated Relay arguments; exactly one of ``first`` and ``last`` is set."""

    first: int | None = None
    after: Key | None = None
    last: int | None = None
    before: Key | None = None

    def __post_init__(self) -> None:
        if (self.first is None) == (self.last is None):
            raise PaginationError("exactly one of first and last is required")
        size = self.first if self.first is not None else self.last
        if size is None or not 0 <= size <= MAX_PAGE_SIZE:
            raise PaginationError(f"first and last must be between 0 and {MAX_PAGE_SIZE}")

    @property
    def backward(self) -> bool:
        return self.last is not None

    @property
    def size(self) -> int:
        return cast("int", self.last if self.backward else self.first)


@dataclass(frozen=True, slots=True)
class PageInfo:
    has_previous_page: bool
    has_next_page: bool
    start_cursor: str | None
    end_cursor: str | None


@dataclass(frozen=True, slots=True)
class Connection:
    nodes: tuple[Event, ...]
    page_info: PageInfo

    def to_json(self) -> dict[str, object]:
        """The Relay envelope: ``edges[].cursor``, ``edges[].node``, and ``pageInfo``."""
        info = self.page_info
        return {
            "edges": [
                {
                    "cursor": encode_cursor(e.key),
                    "node": {"id": e.id, "createdAt": e.created_at, "title": e.title},
                }
                for e in self.nodes
            ],
            "pageInfo": {
                "hasPreviousPage": info.has_previous_page,
                "hasNextPage": info.has_next_page,
                "startCursor": info.start_cursor,
                "endCursor": info.end_cursor,
            },
        }


def encode_cursor(key: Key) -> str:
    """Base64url without padding; opaque to clients but not signed (see the HMAC note)."""
    raw = f"v1:{key[0]}:{key[1]}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> Key:
    """Accept only the exact bytes ``encode_cursor`` produces, or raise ``PaginationError``."""
    if not cursor.isascii() or not 0 < len(cursor) <= MAX_CURSOR_LENGTH:
        raise PaginationError("malformed cursor")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        match = _CURSOR_BODY.fullmatch(raw.decode("ascii"))
    except (binascii.Error, UnicodeDecodeError):
        match = None
    if match is None:
        raise PaginationError("malformed cursor")
    key = (int(match[1]), int(match[2]))
    if max(key) > INT64_MAX or encode_cursor(key) != cursor:
        raise PaginationError("malformed cursor")
    return key


def parse_window(
    *,
    first: str | None = None,
    after: str | None = None,
    last: str | None = None,
    before: str | None = None,
) -> Window:
    """Turn raw query-string values into a ``Window``; no ``first`` or ``last`` means first=20."""

    def count(name: str, value: str | None) -> int | None:
        if value is None:
            return None
        if _COUNT.fullmatch(value) is None:
            raise PaginationError(f"{name} must be a non-negative integer")
        return int(value)

    n_first, n_last = count("first", first), count("last", last)
    if n_first is None and n_last is None:
        n_first = DEFAULT_PAGE_SIZE
    return Window(
        first=n_first,
        after=None if after is None else decode_cursor(after),
        last=n_last,
        before=None if before is None else decode_cursor(before),
    )


def page_sql(window: Window) -> tuple[str, tuple[int, ...]]:
    """Both cursors bound the range; ``last`` reads the range from its newest end."""
    clauses: list[str] = []
    params: list[int] = []
    if window.after is not None:
        clauses.append(OLDER_THAN_AFTER)
        params.extend(window.after)
    if window.before is not None:
        clauses.append(NEWER_THAN_BEFORE)
        params.extend(window.before)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    order = OLDEST_FIRST if window.backward else NEWEST_FIRST
    # Every fragment is a module constant; values travel only as ? binds.
    sql = f"{SELECT}{where} {order} LIMIT ?"
    return sql, (*params, window.size + 1)


def _exists(conn: sqlite3.Connection, sql: str, key: Key) -> bool:
    return conn.execute(sql, key).fetchone() is not None


def fetch_connection(conn: sqlite3.Connection, window: Window) -> Connection:
    """One range read of ``size + 1`` rows, plus at most one ``LIMIT 1`` probe."""
    sql, params = page_sql(window)
    rows = cast("list[tuple[int, int, str]]", conn.execute(sql, params).fetchall())
    overflow = len(rows) > window.size
    nodes = [Event(*row) for row in rows[: window.size]]
    if window.backward:
        # Rows arrive nearest-to-before first; Section 4.3 wants that edge last.
        nodes.reverse()
        has_previous = overflow
        has_next = window.before is not None and _exists(conn, EXISTS_AT_OR_OLDER, window.before)
    else:
        has_next = overflow
        has_previous = window.after is not None and _exists(conn, EXISTS_AT_OR_NEWER, window.after)
    return Connection(
        nodes=tuple(nodes),
        page_info=PageInfo(
            has_previous_page=has_previous,
            has_next_page=has_next,
            start_cursor=encode_cursor(nodes[0].key) if nodes else None,
            end_cursor=encode_cursor(nodes[-1].key) if nodes else None,
        ),
    )


def connect(events: Iterable[Event]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for ddl in SCHEMA:
        conn.execute(ddl)
    conn.executemany(
        "INSERT INTO events (created_at, id, title) VALUES (?, ?, ?)",
        ((e.created_at, e.id, e.title) for e in events),
    )
    return conn


def query_plan(conn: sqlite3.Connection, sql: str, params: tuple[int, ...]) -> list[str]:
    """The ``detail`` column of ``EXPLAIN QUERY PLAN``; for tests, not for production."""
    cursor = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)
    rows = cast("list[tuple[int, int, int, str]]", cursor.fetchall())
    return [row[3] for row in rows]


def vm_steps(conn: sqlite3.Connection, run: Callable[[], object]) -> int:
    """SQLite virtual-machine instructions executed by ``run`` (progress handler, N=1)."""
    ticks = 0

    def tick() -> int:
        nonlocal ticks
        ticks += 1
        return 0

    conn.set_progress_handler(tick, 1)
    try:
        run()
    finally:
        conn.set_progress_handler(None, 0)
    return ticks


def create_app(conn: sqlite3.Connection) -> Starlette:
    """``GET /events?first=&after=&last=&before=`` returning the Relay envelope as JSON."""

    async def events(request: Request) -> JSONResponse:
        q = request.query_params
        try:
            window = parse_window(
                first=q.get("first"),
                after=q.get("after"),
                last=q.get("last"),
                before=q.get("before"),
            )
        except PaginationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(fetch_connection(conn, window).to_json())

    return Starlette(routes=[Route("/events", events, methods=["GET"])])


# --- Contrast: the array-offset connection most Relay libraries ship by default. ---


@strawberry.type(name="Event")
class EventNode(relay.Node):
    event_id: relay.NodeID[int]
    seq: int  # the same integer as event_id, exposed as a plain field
    created_at: int


def build_offset_schema(source: Callable[[], Iterable[EventNode]]) -> strawberry.Schema:
    """A strawberry ``ListConnection``: cursors are ``base64("arrayconnection:<offset>")``."""

    @strawberry.type
    class Query:
        @relay.connection(relay.ListConnection[EventNode])  # type: ignore[untyped-decorator]
        def events(self) -> Iterable[EventNode]:
            return source()

    return strawberry.Schema(query=Query)


class CountingSource:
    """An unsliceable iterable over newest-first nodes that counts how many it yields."""

    def __init__(self, nodes: list[EventNode]) -> None:
        self.nodes = nodes
        self.pulled = 0

    def __call__(self) -> Iterator[EventNode]:
        for node in self.nodes:
            self.pulled += 1
            yield node
