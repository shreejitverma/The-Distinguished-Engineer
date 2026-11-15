"""One AsyncSession shared by sibling asyncio tasks, and the per-task session fix.

An `AsyncSession` proxies one synchronous `Session`: one identity map, one unit of work,
one `SessionTransaction` state machine, and at most one connection per bind.  Every
`await` inside it is a `greenlet_spawn` call that suspends the calling task at the
driver's network or thread boundary, so a sibling task can re-enter the same object
mid-operation.  SQLAlchemy guards three of those windows (connection provisioning,
flush, state transitions) and raises; every other interleaving silently shares one
transaction across tasks that believe they are independent.
"""

# No `from __future__ import annotations`: FastAPI must evaluate the endpoint annotations,
# and `get_session` is local to `create_app`, so a string annotation would not resolve.
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, FastAPI, status
from pydantic import BaseModel
from sqlalchemy import String, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

type SessionOp[T] = Callable[[AsyncSession], Awaitable[T]]


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(32))


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    message: Mapped[str] = mapped_column(String(64))


class AuditUnavailableError(RuntimeError):
    """The audit sink refused the event after the row was flushed."""


def make_engine(path: str) -> AsyncEngine:
    """File-backed aiosqlite engine with `NullPool`.

    `NullPool` opens a new aiosqlite connection (a new worker thread) on every checkout, so
    the first statement of every session awaits the connect and yields to the event loop.
    That makes the connection-provisioning window deterministic instead of depending on
    whether `AsyncAdaptedQueuePool` happens to hold an idle connection.
    """
    return _create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def count_rows(engine: AsyncEngine, model: type[Base]) -> int:
    async with engine.connect() as conn:
        total = (await conn.execute(select(func.count()).select_from(model))).scalar_one()
    return int(total)


async def gather_on_shared_session[T](
    session: AsyncSession, *ops: SessionOp[T]
) -> list[T | BaseException]:
    """The bug: every sibling task receives the same `AsyncSession`."""
    return await asyncio.gather(*(op(session) for op in ops), return_exceptions=True)


async def run_in_own_session[T](maker: async_sessionmaker[AsyncSession], op: SessionOp[T]) -> T:
    """One task, one session, one transaction: commit on success, roll back on any error."""
    async with maker() as session:
        result = await op(session)
        await session.commit()
        return result


async def gather_per_task_sessions[T](
    maker: async_sessionmaker[AsyncSession], *ops: SessionOp[T]
) -> list[T | BaseException]:
    """The fix: each sibling task checks out its own session (and so its own connection)."""
    return await asyncio.gather(
        *(run_in_own_session(maker, op) for op in ops), return_exceptions=True
    )


async def insert_order(session: AsyncSession, sku: str, *, flushed: asyncio.Event) -> int:
    order = Order(sku=sku)
    session.add(order)
    await session.flush()
    flushed.set()
    return order.id


async def write_audit(
    session: AsyncSession, message: str, *, after: asyncio.Event, fail: bool
) -> None:
    """Typical helper that 'undoes its own work' on failure.

    `after` pins the interleaving in which no SQLAlchemy guard can fire: the audit write
    starts only once the order flush has finished, as a slower upstream call would.
    """
    await after.wait()
    session.add(AuditEvent(message=message))
    await session.flush()
    if fail:
        # Intended to discard only the audit row; on a shared session it ends the one
        # transaction that every sibling task is writing into.
        await session.rollback()
        raise AuditUnavailableError(message)


class OrderCreated(BaseModel):
    order_id: int | None
    audit_ok: bool


def create_app(engine: AsyncEngine, *, audit_fails: bool) -> FastAPI:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()

    async def get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    @app.post("/orders/shared", status_code=status.HTTP_201_CREATED)
    async def create_order_shared(  # pyright: ignore[reportUnusedFunction]
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> OrderCreated:
        flushed = asyncio.Event()
        order_result, audit_result = await asyncio.gather(
            insert_order(session, "sku-1", flushed=flushed),
            write_audit(session, "order created", after=flushed, fail=audit_fails),
            return_exceptions=True,
        )
        await session.commit()
        order_id = order_result if isinstance(order_result, int) else None
        return OrderCreated(order_id=order_id, audit_ok=audit_result is None)

    @app.post("/orders/per-task", status_code=status.HTTP_201_CREATED)
    async def create_order_per_task() -> OrderCreated:  # pyright: ignore[reportUnusedFunction]
        flushed = asyncio.Event()

        async def order_op(session: AsyncSession) -> int:
            return await insert_order(session, "sku-1", flushed=flushed)

        async def audit_op(session: AsyncSession) -> None:
            await write_audit(session, "order created", after=flushed, fail=audit_fails)

        order_result, audit_result = await asyncio.gather(
            run_in_own_session(maker, order_op),
            run_in_own_session(maker, audit_op),
            return_exceptions=True,
        )
        order_id = order_result if isinstance(order_result, int) else None
        return OrderCreated(order_id=order_id, audit_ok=audit_result is None)

    return app
