"""Where a FastAPI request commits, and what the client sees when that COMMIT fails.

Three placements of the transaction boundary over one schema:

- `/uow`: a request-scoped Unit-of-Work dependency, `async with sessions.begin()` around
  `yield`.  COMMIT runs in dependency teardown, which FastAPI 0.162.0 runs after the
  response is sent unless the dependency is declared with `Depends(..., scope="function")`.
- `/svc`: a plain session dependency; the service method owns `async with session.begin()`,
  so COMMIT, and any commit-time failure, happens before the handler returns.
- `/cayg`: commit-as-you-go, two COMMITs in one request, so a failure in the second leaves
  the first durable.

Reads on `/svc` go to a read-only AUTOCOMMIT replica engine unless the client presents a
read token newer than the replica's applied position; such reads are served by the primary.
"""

# No `from __future__ import annotations`: FastAPI must evaluate the endpoint annotations,
# and the dependencies are local to `create_app`, so string annotations would not resolve.
import contextlib
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey, String, event, insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MAX_QUANTITY = 100
DependencyScope = Literal["request", "function"]


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Deferred: SQLite checks it at COMMIT, so the flush succeeds and the COMMIT fails.
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", deferrable=True, initially="DEFERRED")
    )
    sku: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[int]


class AuditEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(64))


class ReplicationPosition(Base):
    """Stand-in for a WAL LSN: bumped by every write transaction, copied by replication."""

    __tablename__ = "replication_position"

    id: Mapped[int] = mapped_column(primary_key=True)
    position: Mapped[int]


class OrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: int = Field(ge=1)
    sku: str = Field(min_length=1, max_length=32)
    quantity: int = Field(ge=1)


class OrderOut(BaseModel):
    id: int
    customer_id: int
    sku: str
    quantity: int


@dataclass
class TxnLog:
    """Two views of one request: what SQLAlchemy asked for, and what SQLite executed.

    `events` records `<label>:<VERB>` for every cursor statement, every `Connection`
    commit or rollback, and every pool checkout.  `engine_sql` records the statements the
    SQLite library itself ran (`sqlite3` trace callback), so a DBAPI `commit()` with no
    open transaction shows up in `events` but not in `engine_sql`.
    """

    events: list[str] = field(default_factory=list[str])
    engine_sql: list[str] = field(default_factory=list[str])

    def attach(self, engine: AsyncEngine, label: str) -> None:
        sync_engine = engine.sync_engine

        def on_cursor(_conn: Connection, _cursor: Any, statement: str, *_args: Any) -> None:
            self.events.append(f"{label}:{statement.split(None, 1)[0].upper()}")

        def on_commit(_conn: Connection) -> None:
            self.events.append(f"{label}:COMMIT")

        def on_rollback(_conn: Connection) -> None:
            self.events.append(f"{label}:ROLLBACK")

        def on_checkout(*_args: Any) -> None:
            self.events.append(f"{label}:checkout")

        def on_connect(dbapi_connection: Any, _record: Any) -> None:
            def trace(statement: str) -> None:
                self.engine_sql.append(f"{label}:{statement.split(None, 1)[0].upper()}")

            async def install(raw: Any) -> None:  # the aiosqlite.Connection
                await raw.set_trace_callback(trace)

            dbapi_connection.run_async(install)

        event.listen(sync_engine, "before_cursor_execute", on_cursor)
        event.listen(sync_engine, "commit", on_commit)
        event.listen(sync_engine, "rollback", on_rollback)
        event.listen(sync_engine, "checkout", on_checkout)
        event.listen(sync_engine, "connect", on_connect)

    def take(self) -> tuple[list[str], list[str]]:
        out = (list(self.events), list(self.engine_sql))
        self.events.clear()
        self.engine_sql.clear()
        return out


def make_primary_engine(path: str) -> AsyncEngine:
    """Read-write engine; SQLAlchemy emits BEGIN so deferred constraints and savepoints work."""
    engine = _create_async_engine(f"sqlite+aiosqlite:///{path}")

    def on_connect(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None  # disable the sqlite3 implicit BEGIN
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def on_begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN")

    event.listen(engine.sync_engine, "connect", on_connect)
    event.listen(engine.sync_engine, "begin", on_begin)
    return engine


def make_replica_engine(path: str) -> AsyncEngine:
    """Read-only AUTOCOMMIT engine: no BEGIN, no COMMIT, and writes are refused by SQLite."""
    engine = _create_async_engine(
        f"sqlite+aiosqlite:///{path}",
        isolation_level="AUTOCOMMIT",
        skip_autocommit_rollback=True,
    )

    def on_connect(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA query_only=ON")  # defense in depth, like a hot standby
        cursor.close()

    event.listen(engine.sync_engine, "connect", on_connect)
    return engine


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(insert(ReplicationPosition).values(id=1, position=0))
        await conn.execute(insert(Customer).values(id=1, name="ada"))


def replicate(primary_path: str, replica_path: str) -> None:
    """Apply everything committed on the primary to the replica (the lag ends here)."""
    with (
        contextlib.closing(sqlite3.connect(primary_path)) as src,
        contextlib.closing(sqlite3.connect(replica_path)) as dst,
    ):
        src.backup(dst)


class OrderRejected(Exception):
    """A commit-time constraint failure, surfaced by the service before the response."""


class OrderService:
    """Owns the transaction: one `begin()` per use case, at the outermost call only."""

    async def place_order(self, session: AsyncSession, order: OrderIn) -> tuple[OrderOut, int]:
        try:
            async with session.begin():
                row = Order(**order.model_dump())
                session.add(row)
                await session.flush()
                position = await bump_position(session)
                out = OrderOut.model_validate(row, from_attributes=True)
        except IntegrityError as exc:  # raised by COMMIT: the deferred foreign key
            raise OrderRejected(str(exc.orig)) from exc
        return out, position


async def bump_position(session: AsyncSession) -> int:
    stmt = (
        update(ReplicationPosition)
        .where(ReplicationPosition.id == 1)
        .values(position=ReplicationPosition.position + 1)
        .returning(ReplicationPosition.position)
    )
    return (await session.execute(stmt)).scalar_one()


async def load_order(session: AsyncSession, order_id: int) -> OrderOut:
    row = await session.get(Order, order_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "order not found")
    return OrderOut.model_validate(row, from_attributes=True)


async def close_after_failed_commit(engine: AsyncEngine, *, rollback: bool) -> str:
    """Fail a COMMIT, close the session, then use the pooled connection again.

    SQLite keeps the transaction open when COMMIT fails on a deferred foreign key, and
    `Connection.close()` treats the failed root transaction as already reset, so without an
    explicit rollback the pool hands out a connection that is still inside a transaction.
    """
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(Order(customer_id=999, sku="orphan", quantity=1))
        try:
            await session.commit()
        except IntegrityError:
            if rollback:
                await session.rollback()
    async with sessions() as session:
        try:
            await session.execute(select(Order.id))
        except OperationalError as exc:
            return str(exc.orig)
    return "ok"


def create_app(
    primary: AsyncEngine, replica: AsyncEngine, *, uow_scope: DependencyScope = "request"
) -> FastAPI:
    writer = async_sessionmaker(primary, expire_on_commit=False)
    reader = async_sessionmaker(replica, expire_on_commit=False)
    service = OrderService()
    app = FastAPI()

    @app.exception_handler(IntegrityError)
    async def integrity_error(_request: Request, _exc: IntegrityError) -> JSONResponse:
        return JSONResponse({"detail": "constraint violated"}, status.HTTP_409_CONFLICT)

    # Pattern A: the dependency is the Unit of Work; COMMIT happens in teardown.
    async def uow_session() -> AsyncIterator[AsyncSession]:
        async with writer.begin() as session:
            yield session

    # Pattern B: the dependency only scopes the session; the service owns the transaction.
    async def writer_session() -> AsyncIterator[AsyncSession]:
        async with writer() as session:
            try:
                yield session
            except BaseException:
                # close() alone skips the pool reset after a failed COMMIT; see
                # `close_after_failed_commit`.
                await session.rollback()
                raise

    async def reader_session() -> AsyncIterator[AsyncSession]:
        async with reader() as session:
            yield session

    @app.post("/uow/orders", status_code=status.HTTP_201_CREATED)
    async def uow_create(
        order: OrderIn, session: Annotated[AsyncSession, Depends(uow_session, scope=uow_scope)]
    ) -> OrderOut:
        row = Order(**order.model_dump())
        session.add(row)
        await session.flush()
        if order.quantity > MAX_QUANTITY:  # a business rule checked after the write
            raise HTTPException(status.HTTP_409_CONFLICT, "insufficient stock")
        return OrderOut.model_validate(row, from_attributes=True)

    @app.get("/uow/orders/{order_id}")
    async def uow_read(
        order_id: int, session: Annotated[AsyncSession, Depends(uow_session, scope=uow_scope)]
    ) -> OrderOut:
        return await load_order(session, order_id)

    @app.get("/uow/health")
    async def uow_health(
        session: Annotated[AsyncSession, Depends(uow_session, scope=uow_scope)],
    ) -> dict[str, bool]:
        return {"in_transaction": session.in_transaction()}

    @app.post("/svc/orders", status_code=status.HTTP_201_CREATED)
    async def svc_create(
        order: OrderIn,
        session: Annotated[AsyncSession, Depends(writer_session)],
        response: Response,
    ) -> OrderOut:
        try:
            out, position = await service.place_order(session, order)
        except OrderRejected as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "unknown customer") from exc
        response.headers["X-Read-Token"] = str(position)
        return out

    @app.get("/svc/orders/{order_id}")
    async def svc_read(
        order_id: int,
        replica_session: Annotated[AsyncSession, Depends(reader_session)],
        primary_session: Annotated[AsyncSession, Depends(writer_session)],
        response: Response,
        x_read_token: Annotated[int | None, Header(ge=0)] = None,
    ) -> OrderOut:
        session = replica_session
        if x_read_token is not None:
            applied = (
                await replica_session.execute(select(ReplicationPosition.position))
            ).scalar_one()
            if applied < x_read_token:  # replica has not replayed the client's write yet
                session = primary_session
        response.headers["X-Served-By"] = "primary" if session is primary_session else "replica"
        return await load_order(session, order_id)

    @app.post("/cayg/orders", status_code=status.HTTP_201_CREATED)
    async def cayg_create(
        order: OrderIn, session: Annotated[AsyncSession, Depends(writer_session)]
    ) -> OrderOut:
        session.add(AuditEntry(action=f"order:{order.sku}"))
        await session.commit()  # COMMIT 1: durable no matter what happens next
        row = Order(**order.model_dump())
        session.add(row)
        await session.commit()  # COMMIT 2: the deferred foreign key can fail here
        return OrderOut.model_validate(row, from_attributes=True)

    @app.get("/shared")
    async def shared(
        first: Annotated[AsyncSession, Depends(writer_session)],
        second: Annotated[AsyncSession, Depends(writer_session)],
        third: Annotated[AsyncSession, Depends(writer_session, use_cache=False)],
    ) -> dict[str, bool]:
        return {"first_is_second": first is second, "first_is_third": first is third}

    return app
