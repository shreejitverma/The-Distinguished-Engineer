"""Prepared statement caches and JSONB codecs in asyncpg, psycopg 3, and SQLAlchemy.

Every function here drives the real library objects (asyncpg 0.32.0, psycopg 3.3.8,
SQLAlchemy 2.1.8) in-process, without a PostgreSQL server: the drivers' cache and
codec decisions are pure client-side logic, so they can be observed deterministically.
"""

from __future__ import annotations

import asyncio
import importlib
import struct
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from psycopg import pq
from psycopg._preparing import PrepareManager  # pyright: ignore[reportPrivateUsage]
from psycopg._queries import PostgresQuery  # pyright: ignore[reportPrivateUsage]
from psycopg.adapt import AdaptersMap, Transformer

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.engine.interfaces import Dialect

# asyncpg ships no type information and SQLAlchemy's asyncpg adapter is untyped, so
# both are reached through explicit Any boundaries and wrapped in typed functions.
_asyncpg_connection: Any = importlib.import_module("asyncpg.connection")
_asyncpg_protocol: Any = importlib.import_module("asyncpg.protocol.protocol")
_sa_asyncpg: Any = importlib.import_module("sqlalchemy.dialects.postgresql.asyncpg")

JSONB_OID = 3802


def asyncpg_wire_constants() -> tuple[bytes, bytes]:
    """The Sync and Flush messages asyncpg's Cython protocol precomputes."""
    return bytes(_asyncpg_protocol.SYNC_MESSAGE), bytes(_asyncpg_protocol.FLUSH_MESSAGE)


# --- PostgreSQL frontend messages (PostgreSQL 18 docs, Section 54.7) ----------------


def frontend_message(kind: bytes, body: bytes) -> bytes:
    """Byte1 type, Int32 length that counts itself but not the type byte, then body."""
    return kind + struct.pack("!i", len(body) + 4) + body


def bind_message(
    stmt: str, values: Sequence[bytes | None], *, all_binary: bool, portal: str = ""
) -> bytes:
    """Bind with one shared format code for parameters and for result columns."""
    code = 1 if all_binary else 0
    body = portal.encode() + b"\0" + stmt.encode() + b"\0"
    body += struct.pack("!hh", 1, code)  # C = 1: one code applies to every parameter
    body += struct.pack("!h", len(values))
    for value in values:
        if value is None:
            body += struct.pack("!i", -1)  # NULL: length -1 and no value bytes
        else:
            body += struct.pack("!i", len(value)) + value
    body += struct.pack("!hh", 1, code)  # R = 1: one code for every result column
    return frontend_message(b"B", body)


SYNC = frontend_message(b"S", b"")
FLUSH = frontend_message(b"H", b"")


# --- psycopg 3: parameter typing and the automatic prepare threshold ----------------


@dataclass(frozen=True)
class BoundQuery:
    """What psycopg puts in Parse and Bind for one execute() call."""

    query: bytes
    oids: tuple[int, ...]
    formats: tuple[int, ...]
    values: tuple[bytes | None, ...]


def psycopg_bind(sql: str, params: Sequence[Any]) -> BoundQuery:
    """Convert `%s` placeholders and dump parameters with psycopg's global adapters."""
    pgq = PostgresQuery(Transformer())
    pgq.convert(sql.encode(), params)
    raw = pgq.params or ()
    return BoundQuery(
        query=pgq.query,
        oids=pgq.types,
        formats=tuple(int(f) for f in pgq.formats or ()),
        values=tuple(None if v is None else bytes(v) for v in raw),
    )


@dataclass(frozen=True)
class _Result:
    """The two PGresult attributes PrepareManager.validate reads."""

    status: int
    command_status: bytes | None


@dataclass(frozen=True)
class Execution:
    sql: str
    params: Sequence[Any] = ()
    command_status: bytes = b"SELECT 1"


@dataclass(frozen=True)
class PrepareStep:
    decision: str  # "NO", "SHOULD" (send Parse with a name now), or "YES" (reuse)
    name: str
    deallocate: tuple[str, ...] = ()


@dataclass
class PsycopgPrepareTrace:
    steps: list[PrepareStep] = field(default_factory=list[PrepareStep])
    prepared: dict[str, str] = field(default_factory=dict[str, str])


def psycopg_prepare_trace(
    executions: Sequence[Execution], *, threshold: int | None = 5, prepared_max: int = 100
) -> PsycopgPrepareTrace:
    """Replay the calls psycopg's cursor makes around PrepareManager for each execute().

    Mirrors BaseCursor._maybe_prepare_gen: get(), maybe_add_to_cache(), validate(),
    then maintain_gen(), which turns the flush queue into DEALLOCATE or Close.
    """
    manager = PrepareManager()
    manager.prepare_threshold = threshold
    manager.prepared_max = prepared_max
    trace = PsycopgPrepareTrace()
    for ex in executions:
        pgq = PostgresQuery(Transformer())
        pgq.convert(ex.sql.encode(), ex.params)
        prep, name = manager.get(pgq)
        key = manager.maybe_add_to_cache(pgq, prep, name)
        if key is not None:
            result = _Result(pq.ExecStatus.COMMAND_OK, ex.command_status)
            manager.validate(key, prep, name, cast("Any", [result]))
        flushed: list[str] = []
        while manager._to_flush:  # pyright: ignore[reportPrivateUsage]
            target = manager._to_flush.popleft()  # pyright: ignore[reportPrivateUsage]
            flushed.append("DEALLOCATE ALL" if target is None else target.decode())
        trace.steps.append(PrepareStep(prep.name, name.decode(), tuple(flushed)))
        names = manager._names  # pyright: ignore[reportPrivateUsage]
        trace.prepared = {n.decode(): k[0].decode() for k, n in names.items()}
    return trace


# --- asyncpg: the per-connection LRU statement cache --------------------------------


@dataclass
class _FakeStatement:
    query: str
    closed: bool = False


@dataclass
class AsyncpgCacheTrace:
    events: list[str] = field(default_factory=list[str])
    removed: list[str] = field(default_factory=list[str])
    cached: list[str] = field(default_factory=list[str])


async def asyncpg_cache_trace(
    queries: Sequence[str],
    *,
    max_size: int = 100,
    max_lifetime: float = 0.0,
    max_cacheable_statement_size: int = 15 * 1024,
    settle: float = 0.0,
) -> AsyncpgCacheTrace:
    """Feed queries through asyncpg's real _StatementCache.

    The hit, prepare, and size-gate logic mirrors Connection._get_statement; the
    LRU order, eviction, and lifetime timers are asyncpg's own code.
    """
    trace = AsyncpgCacheTrace()

    def on_remove(stmt: _FakeStatement) -> None:
        trace.removed.append(stmt.query)

    cache: Any = _asyncpg_connection._StatementCache(
        loop=asyncio.get_running_loop(),
        max_size=max_size,
        on_remove=on_remove,
        max_lifetime=max_lifetime,
    )
    for query in queries:
        if cache.get(query) is not None:
            trace.events.append(f"hit {query}")
            continue
        cacheable = max_size > 0 and (
            not max_cacheable_statement_size or len(query) <= max_cacheable_statement_size
        )
        trace.events.append(f"prepare {query}" + ("" if cacheable else " (anonymous)"))
        if cacheable:
            cache.put(query, _FakeStatement(query))
    if settle:
        await asyncio.sleep(settle)
    statements: list[_FakeStatement] = list(cache.iter_statements())
    trace.cached = [stmt.query for stmt in statements]
    return trace


def asyncpg_statement_name() -> str:
    """Name asyncpg gives a cached statement: a process-wide counter, in hex."""
    name: str = _asyncpg_connection.Connection._get_unique_id(None, "stmt")
    return name


# --- SQLAlchemy 2.1 asyncpg dialect: its own prepared statement LRU -----------------


@dataclass
class _FakePrepared:
    query: str
    name: str | None

    def get_attributes(self) -> tuple[()]:
        return ()


@dataclass
class FakeAsyncpg:
    """Records what SQLAlchemy asks of an asyncpg connection."""

    prepares: list[tuple[str, str | None]] = field(default_factory=list[tuple[str, str | None]])
    reloads: int = 0
    codecs: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])

    async def prepare(self, query: str, name: str | None = None) -> _FakePrepared:
        self.prepares.append((query, name))
        return _FakePrepared(query, name)

    async def reload_schema_state(self) -> None:
        self.reloads += 1

    async def set_type_codec(self, typename: str, **kw: Any) -> None:
        self.codecs[typename] = kw


def sqlalchemy_asyncpg_adapter(
    fake: FakeAsyncpg,
    *,
    prepared_statement_cache_size: int = 100,
    prepared_statement_name_func: Callable[[], str] | None = None,
) -> Any:
    """SQLAlchemy's AsyncAdapt_asyncpg_connection wrapped around a recording fake."""
    return _sa_asyncpg.AsyncAdapt_asyncpg_connection(
        None,
        fake,
        prepared_statement_cache_size=prepared_statement_cache_size,
        prepared_statement_name_func=prepared_statement_name_func,
    )


async def sqlalchemy_prepare(adapter: Any, sql: str, invalidate_timestamp: float = 0.0) -> None:
    """The call AsyncAdapt_asyncpg_cursor._prepare_and_execute makes per statement."""
    await adapter._prepare(sql, invalidate_timestamp)


@dataclass
class _AdaptedConnection:
    _connection: FakeAsyncpg


async def sqlalchemy_asyncpg_jsonb_codec(dialect: Dialect) -> dict[str, Any]:
    """Run the dialect's on-connect JSONB codec setup against a recording connection."""
    fake = FakeAsyncpg()
    setup = getattr(dialect, "setup_asyncpg_jsonb_codec")  # noqa: B009
    await setup(_AdaptedConnection(fake))
    return fake.codecs["jsonb"]


def sqlalchemy_psycopg_jsonb_load(dialect: Dialect, url: Any, payload: bytes) -> Any:
    """Decode a binary jsonb value with the AdaptersMap the dialect passes to connect()."""
    _, cparams = dialect.create_connect_args(url)
    context = cast("AdaptersMap", cparams["context"])
    loader = Transformer(context).get_loader(JSONB_OID, pq.Format.BINARY)
    return loader.load(payload)
