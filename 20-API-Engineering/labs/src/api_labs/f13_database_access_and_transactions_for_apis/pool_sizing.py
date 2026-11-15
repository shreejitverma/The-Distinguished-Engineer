"""Connection budget math across replicas, and a PgBouncer transaction-pooling model.

Part 1 is arithmetic: how many PostgreSQL backends a fleet of pods, worker processes,
and per-process SQLAlchemy pools can open at peak, including the rollout surge.

Part 2 is a deterministic model of PgBouncer transaction pooling: a server connection is
linked to a client only until the backend reports ``ReadyForQuery`` with status ``'I'``
(pgbouncer 1.26.1 src/server.c, handle_server_work), so a protocol-level named prepared
statement created in one transaction may be missing, or already taken, on the server the
next transaction lands on.  Driver models reproduce asyncpg's process-global statement
names and psycopg's per-connection names, and the ``max_prepared_statements`` model
reproduces PgBouncer's ``PGBOUNCER_<n>`` rewriting.

Part 3 drives a real SQLAlchemy ``QueuePool`` to prove the per-process ceiling.
"""

from __future__ import annotations

import enum
import itertools
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import create_engine, event, exc
from sqlalchemy.pool import NullPool, QueuePool

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

# ---------------------------------------------------------------------------
# Part 1: budget arithmetic
# ---------------------------------------------------------------------------


def surge_pods(replicas: int, max_surge: int | str) -> int:
    """Extra pods a RollingUpdate may create; a percentage rounds up (Kubernetes docs)."""
    if replicas < 0:
        raise ValueError("replicas must be >= 0")
    if isinstance(max_surge, int):
        if max_surge < 0:
            raise ValueError("max_surge must be >= 0")
        return max_surge
    if not max_surge.endswith("%"):
        raise ValueError("a string max_surge must be a percentage such as '25%'")
    percent = int(max_surge[:-1])
    if percent < 0:
        raise ValueError("max_surge must be >= 0")
    return math.ceil(replicas * percent / 100)


def per_process_ceiling(pool_size: int, max_overflow: int) -> int | None:
    """Most connections one SQLAlchemy ``QueuePool`` can hold; ``None`` means unbounded.

    ``pool_size=0`` disables the overflow limit and ``max_overflow=-1`` removes it
    (sqlalchemy 2.1.8 lib/sqlalchemy/pool/impl.py, QueuePool.__init__).
    """
    if pool_size < 0 or max_overflow < -1:
        raise ValueError("pool_size must be >= 0 and max_overflow >= -1")
    if pool_size == 0 or max_overflow == -1:
        return None
    return pool_size + max_overflow


@dataclass(frozen=True)
class AppTier:
    """One deployment that talks to the database: pods x processes x engines."""

    name: str
    replicas: int
    workers_per_pod: int
    pool_size: int = 5
    max_overflow: int = 10
    max_surge: int | str = "25%"
    engines_per_process: int = 1

    def processes_at_peak(self) -> int:
        pods = self.replicas + surge_pods(self.replicas, self.max_surge)
        return pods * self.workers_per_pod

    def peak_connections(self) -> int:
        ceiling = per_process_ceiling(self.pool_size, self.max_overflow)
        if ceiling is None:
            raise ValueError(f"{self.name}: unbounded pool has no connection ceiling")
        return self.processes_at_peak() * self.engines_per_process * ceiling


@dataclass(frozen=True)
class ServerLimits:
    """PostgreSQL slot settings; defaults are the documented defaults (PostgreSQL 18)."""

    max_connections: int = 100
    superuser_reserved_connections: int = 3
    reserved_connections: int = 0

    def usable_by_app_roles(self) -> int:
        """Slots an ordinary role can take before PostgreSQL refuses with 53300."""
        return (
            self.max_connections - self.superuser_reserved_connections - self.reserved_connections
        )


@dataclass(frozen=True)
class BudgetReport:
    demand: int
    usable: int

    @property
    def headroom(self) -> int:
        return self.usable - self.demand

    @property
    def fits(self) -> bool:
        return self.headroom >= 0


def budget(tiers: list[AppTier], limits: ServerLimits, other_clients: int = 0) -> BudgetReport:
    """Sum peak demand of direct (unpooled-by-proxy) tiers against usable slots."""
    demand = sum(t.peak_connections() for t in tiers) + other_clients
    return BudgetReport(demand=demand, usable=limits.usable_by_app_roles())


def largest_safe_ceiling(tiers: list[AppTier], limits: ServerLimits, other_clients: int = 0) -> int:
    """Largest uniform ``pool_size + max_overflow`` that keeps every tier inside the budget."""
    engines = sum(t.processes_at_peak() * t.engines_per_process for t in tiers)
    if engines == 0:
        raise ValueError("no processes")
    return (limits.usable_by_app_roles() - other_clients) // engines


def littles_law_connections(transactions_per_second: float, mean_hold_seconds: float) -> int:
    """Connections busy on average: L = lambda x W, rounded up."""
    if transactions_per_second < 0 or mean_hold_seconds < 0:
        raise ValueError("rates and durations must be >= 0")
    return math.ceil(transactions_per_second * mean_hold_seconds)


def deadlock_free_pool_size(threads: int, connections_per_thread: int) -> int:
    """HikariCP pool-sizing wiki: Tn x (Cm - 1) + 1 avoids pool-locking deadlock."""
    if threads < 1 or connections_per_thread < 1:
        raise ValueError("threads and connections_per_thread must be >= 1")
    return threads * (connections_per_thread - 1) + 1


@dataclass(frozen=True)
class BouncerTopology:
    """Server-side connections behind one or more PgBouncer processes."""

    instances: int
    pools: int  # distinct (database, user) pairs
    default_pool_size: int = 20
    reserve_pool_size: int = 0
    max_db_connections: int = 0  # 0 means unlimited; per database, assumes one database

    def server_connections(self) -> int:
        per_instance = self.pools * (self.default_pool_size + self.reserve_pool_size)
        if self.max_db_connections:
            per_instance = min(per_instance, self.max_db_connections)
        return self.instances * per_instance


# ---------------------------------------------------------------------------
# Part 2: PgBouncer transaction pooling model
# ---------------------------------------------------------------------------


class PoolMode(enum.Enum):
    SESSION = "session"
    TRANSACTION = "transaction"


class PgError(Exception):
    """A PostgreSQL ``ErrorResponse`` with its SQLSTATE."""

    def __init__(self, sqlstate: str, message: str) -> None:
        super().__init__(f"{sqlstate}: {message}")
        self.sqlstate = sqlstate
        self.message = message


@dataclass
class Backend:
    """One PostgreSQL backend process: session state lives here, not in the client."""

    pid: int
    prepared: dict[str, str] = field(default_factory=dict[str, str])
    unnamed: str | None = None
    gucs: dict[str, str] = field(default_factory=dict[str, str])
    in_transaction: bool = False
    parses: int = 0

    def parse(self, name: str, query: str) -> None:
        self.parses += 1
        if name == "":
            self.unnamed = query  # replaced freely (protocol-flow, extended query)
            return
        if name in self.prepared:
            raise PgError("42P05", f'prepared statement "{name}" already exists')
        self.prepared[name] = query

    def execute(self, name: str) -> str:
        query = self.unnamed if name == "" else self.prepared.get(name)
        if query is None:
            raise PgError("26000", f'prepared statement "{name}" does not exist')
        return query

    def close(self, name: str) -> None:
        self.prepared.pop(name, None)

    def status(self) -> str:
        """The ``ReadyForQuery`` transaction status byte."""
        return "T" if self.in_transaction else "I"


@dataclass
class _ClientLink:
    server: Backend | None = None
    names: dict[str, str] = field(default_factory=dict[str, str])  # client name -> query


class Bouncer:
    """Links client connections to backends the way PgBouncer does.

    In transaction mode the link is dropped whenever the backend reports status ``'I'``;
    the freed backend goes on top of a LIFO idle stack, PgBouncer's default reuse order
    unless ``server_round_robin`` is set (PgBouncer FAQ).
    With ``max_prepared_statements > 0`` protocol-level names are rewritten to a shared
    ``PGBOUNCER_<n>`` name per unique query and prepared on demand on whichever backend
    the client is linked to, with an LRU of that size per backend (PgBouncer config docs).
    """

    def __init__(self, mode: PoolMode, pool_size: int, max_prepared_statements: int = 0) -> None:
        if pool_size < 1:
            raise ValueError("pool_size must be >= 1")
        self.mode = mode
        self.max_prepared_statements = max_prepared_statements
        self.backends = [Backend(pid=1000 + i) for i in range(pool_size)]
        self._idle: list[Backend] = list(reversed(self.backends))  # pop() -> pid 1000 first
        self._links: dict[str, _ClientLink] = {}
        self._internal: dict[str, str] = {}  # query -> PGBOUNCER_<n>
        self._server_lru: dict[int, OrderedDict[str, None]] = {
            b.pid: OrderedDict() for b in self.backends
        }
        self._ids = itertools.count(1)

    # -- linking --------------------------------------------------------------
    def connect(self, client: str) -> None:
        self._links[client] = _ClientLink()

    def _link(self, client: str) -> Backend:
        link = self._links[client]
        if link.server is None:
            if not self._idle:
                raise RuntimeError("no idle server: the client would wait (query_wait_timeout)")
            link.server = self._idle.pop()
        return link.server

    def _after_message(self, client: str) -> None:
        link = self._links[client]
        server = link.server
        if server is None:
            return
        if self.mode is PoolMode.TRANSACTION and server.status() == "I":
            link.server = None
            self._idle.append(server)

    def server_of(self, client: str) -> int | None:
        server = self._links[client].server
        return None if server is None else server.pid

    # -- protocol messages (each ends with Sync, so each is one ReadyForQuery) --
    def begin(self, client: str) -> None:
        self._link(client).in_transaction = True
        self._after_message(client)

    def commit(self, client: str) -> None:
        self._link(client).in_transaction = False
        self._after_message(client)

    def set_guc(self, client: str, name: str, value: str) -> None:
        """``SET name = value``: session state on whatever backend is linked now."""
        self._link(client).gucs[name] = value
        self._after_message(client)

    def show_guc(self, client: str, name: str) -> str | None:
        value = self._link(client).gucs.get(name)
        self._after_message(client)
        return value

    def parse(self, client: str, name: str, query: str) -> None:
        server = self._link(client)
        try:
            if name and self.max_prepared_statements > 0:
                self._links[client].names[name] = query
                self._ensure_prepared(server, query)
            else:
                server.parse(name, query)
        finally:
            self._after_message(client)

    def execute(self, client: str, name: str) -> str:
        server = self._link(client)
        try:
            if name and self.max_prepared_statements > 0:
                query = self._links[client].names.get(name)
                if query is None:
                    raise PgError("26000", f'prepared statement "{name}" does not exist')
                return server.execute(self._ensure_prepared(server, query))
            return server.execute(name)
        finally:
            self._after_message(client)

    def parse_bind_execute_unnamed(self, client: str, query: str) -> str:
        """Parse, Bind, Execute, Sync on the unnamed statement in one round trip."""
        server = self._link(client)
        try:
            server.parse("", query)
            return server.execute("")
        finally:
            self._after_message(client)

    def _ensure_prepared(self, server: Backend, query: str) -> str:
        internal = self._internal.get(query)
        if internal is None:
            internal = f"PGBOUNCER_{next(self._ids)}"
            self._internal[query] = internal
        lru = self._server_lru[server.pid]
        if internal in lru:
            lru.move_to_end(internal)
            return internal
        if len(lru) >= self.max_prepared_statements:
            evicted, _ = lru.popitem(last=False)
            server.close(evicted)
        server.parse(internal, query)
        lru[internal] = None
        return internal


# ---------------------------------------------------------------------------
# Driver models
# ---------------------------------------------------------------------------


class DriverProcess:
    """One OS process: asyncpg's statement-name counter is a module global per process.

    asyncpg 0.32.0 asyncpg/connection.py, Connection._get_unique_id:
    ``'__asyncpg_{}_{:x}__'.format(prefix, _uid)`` with ``global _uid``.
    """

    def __init__(self) -> None:
        self._uid = 0

    def next_name(self) -> str:
        self._uid += 1
        return f"__asyncpg_stmt_{self._uid:x}__"


class AsyncpgLikeConnection:
    """Statement cache keyed by query; ``statement_cache_size=0`` sends unnamed statements."""

    def __init__(
        self, bouncer: Bouncer, client: str, process: DriverProcess, statement_cache_size: int
    ) -> None:
        self.bouncer = bouncer
        self.client = client
        self.process = process
        self.statement_cache_size = statement_cache_size
        self._cache: OrderedDict[str, str] = OrderedDict()
        bouncer.connect(client)

    def fetch(self, query: str) -> str:
        if self.statement_cache_size == 0:
            return self.bouncer.parse_bind_execute_unnamed(self.client, query)
        name = self._cache.get(query)
        if name is None:
            name = self.process.next_name()
            self.bouncer.parse(self.client, name, query)
            self._cache[query] = name
            if len(self._cache) > self.statement_cache_size:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(query)
        return self.bouncer.execute(self.client, name)

    def transaction(self, *queries: str) -> list[str]:
        self.bouncer.begin(self.client)
        try:
            return [self.fetch(q) for q in queries]
        finally:
            self.bouncer.commit(self.client)


class PsycopgLikeConnection:
    """psycopg 3.3.8 PrepareManager: names ``_pg3_<n>`` count from 0 per connection.

    A query is sent unnamed until it has been seen ``prepare_threshold`` times; ``None``
    disables preparation (psycopg 3.3.8 psycopg/_preparing.py, PrepareManager.get).
    """

    def __init__(self, bouncer: Bouncer, client: str, prepare_threshold: int | None = 5) -> None:
        self.bouncer = bouncer
        self.client = client
        self.prepare_threshold = prepare_threshold
        self._counts: dict[str, int] = {}
        self._names: dict[str, str] = {}
        self._idx = 0
        bouncer.connect(client)

    def execute(self, query: str) -> str:
        if self.prepare_threshold is None:
            return self.bouncer.parse_bind_execute_unnamed(self.client, query)
        name = self._names.get(query)
        if name is not None:
            return self.bouncer.execute(self.client, name)
        count = self._counts.get(query, 0)
        if count >= self.prepare_threshold:
            name = f"_pg3_{self._idx}"
            self._idx += 1
            self.bouncer.parse(self.client, name, query)
            self._names[query] = name
            return self.bouncer.execute(self.client, name)
        self._counts[query] = count + 1
        return self.bouncer.parse_bind_execute_unnamed(self.client, query)


# ---------------------------------------------------------------------------
# Part 3: a real SQLAlchemy QueuePool
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolProbe:
    max_checked_out: int
    dbapi_connects: int
    error: str | None


def probe_queuepool(pool_size: int, max_overflow: int, attempts: int, timeout: float) -> PoolProbe:
    """Check out up to *attempts* connections at once from a real ``QueuePool``."""
    engine = create_engine(
        "sqlite://",
        poolclass=QueuePool,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=timeout,
    )
    connects = 0

    def on_connect(*_: object) -> None:
        nonlocal connects
        connects += 1

    event.listen(engine, "connect", on_connect)
    held: list[Connection] = []
    error: str | None = None
    try:
        for _ in range(attempts):
            try:
                held.append(engine.connect())
            except exc.TimeoutError as e:
                error = str(e).split("\n")[0]
                break
        return PoolProbe(max_checked_out=len(held), dbapi_connects=connects, error=error)
    finally:
        for conn in held:
            conn.close()
        engine.dispose()


def nullpool_connects(checkouts: int) -> int:
    """DBAPI connections opened by *checkouts* sequential checkouts from ``NullPool``."""
    engine = create_engine("sqlite://", poolclass=NullPool)
    connects = 0

    def on_connect(*_: object) -> None:
        nonlocal connects
        connects += 1

    event.listen(engine, "connect", on_connect)
    try:
        for _ in range(checkouts):
            with engine.connect():
                pass
        return connects
    finally:
        engine.dispose()
