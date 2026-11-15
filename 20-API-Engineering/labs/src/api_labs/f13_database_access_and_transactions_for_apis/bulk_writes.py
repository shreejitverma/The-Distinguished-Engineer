"""Bulk INSERT, ON CONFLICT DO UPDATE upserts, and RETURNING in SQLAlchemy 2.x.

Every helper runs against in-memory SQLite and records each DBAPI cursor call,
so the tests count real round trips instead of trusting documentation.
"""

import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Self

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    Integer,
    String,
    Table,
    bindparam,
    create_engine,
    event,
    func,
    insert,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

MAX_BATCH = 1000


class Base(DeclarativeBase):
    pass


class Item(Base):
    """Server-generated integer key (SQLite ROWID) plus a natural unique key."""

    __tablename__ = "item"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(32), unique=True)
    qty: Mapped[int | None]
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.current_timestamp())


class Shipment(Base):
    """Client-generated UUID key: a sentinel that keeps ordered RETURNING batched."""

    __tablename__ = "shipment"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sku: Mapped[str] = mapped_column(String(32))


WIDE_COLUMNS = 40
wide = Table(
    "wide",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    *[Column(f"c{i}", Integer) for i in range(WIDE_COLUMNS)],
)


@dataclass(frozen=True)
class Executed:
    statement: str
    parameters: Any
    event_executemany: bool  # True for every "insertmanyvalues" batch as well

    @property
    def executemany(self) -> bool:
        """Whether the DBAPI call was `cursor.executemany()` (a list of parameter sets).

        Each "insertmanyvalues" batch is one `cursor.execute()` with a flat tuple,
        even though the event receives `executemany=True` for it.
        """
        return isinstance(self.parameters, list)

    @property
    def rows(self) -> int:
        """Parameter sets carried by this one cursor call."""
        if self.executemany:
            return len(self.parameters)
        return max(1, self.statement.count("(?"))


@dataclass
class StatementLog:
    """Every statement handed to `cursor.execute()` or `cursor.executemany()`."""

    entries: list[Executed] = field(default_factory=list[Executed])

    def record(
        self,
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        self.entries.append(Executed(statement, parameters, executemany))

    def clear(self) -> None:
        self.entries.clear()

    def rows_per_call(self) -> list[int]:
        return [e.rows for e in self.entries]


def make_engine(**kwargs: Any) -> tuple[Engine, StatementLog]:
    """One shared in-memory SQLite connection with the schema created."""
    engine = create_engine("sqlite://", poolclass=StaticPool, **kwargs)
    log = StatementLog()
    event.listen(engine, "before_cursor_execute", log.record)
    Base.metadata.create_all(engine)
    log.clear()
    return engine, log


def item_rows(n: int, start: int = 0, qty: int = 0) -> list[dict[str, Any]]:
    return [{"sku": f"sku-{i:05d}", "qty": qty + i} for i in range(start, start + n)]


def insert_unit_of_work(session: Session, rows: Sequence[dict[str, Any]]) -> list[int]:
    """`add_all()` + flush: the ORM needs every new key back, in input order."""
    items = [Item(**row) for row in rows]
    session.add_all(items)
    session.flush()
    return [item.id for item in items]


def insert_bulk(session: Session, rows: Sequence[dict[str, Any]]) -> None:
    """ORM bulk INSERT without RETURNING: one DBAPI `executemany()`."""
    session.execute(insert(Item), rows)


def insert_bulk_returning(
    session: Session, rows: Sequence[dict[str, Any]], *, ordered: bool
) -> list[Item]:
    """ORM bulk INSERT with RETURNING: "insertmanyvalues" batches."""
    stmt = insert(Item).returning(Item, sort_by_parameter_order=ordered)
    return list(session.scalars(stmt, rows))


def insert_shipments_returning(session: Session, skus: Sequence[str]) -> list[Shipment]:
    stmt = insert(Shipment).returning(Shipment, sort_by_parameter_order=True)
    return list(session.scalars(stmt, [{"sku": sku} for sku in skus]))


def upsert_items(
    session: Session, rows: Sequence[dict[str, Any]], *, populate_existing: bool
) -> list[Item]:
    """INSERT .. ON CONFLICT (sku) DO UPDATE SET qty = excluded.qty RETURNING item.*"""
    base = sqlite_insert(Item)
    stmt = base.on_conflict_do_update(
        index_elements=[Item.sku], set_={"qty": base.excluded.qty}
    ).returning(Item)
    result = session.scalars(stmt, rows, execution_options={"populate_existing": populate_existing})
    return list(result)


def upsert_items_bound_set(session: Session, rows: Sequence[dict[str, Any]]) -> list[int]:
    """Same upsert, but SET takes a per-row bound parameter instead of `excluded`."""
    base = sqlite_insert(Item)
    stmt = base.on_conflict_do_update(
        index_elements=[Item.sku], set_={"qty": bindparam("new_qty")}
    ).returning(Item.id)
    return list(session.scalars(stmt, rows))


def insert_ignore_returning(
    session: Session, rows: Sequence[dict[str, Any]], *, ordered: bool
) -> list[str]:
    """INSERT .. ON CONFLICT (sku) DO NOTHING RETURNING sku."""
    stmt = (
        sqlite_insert(Item)
        .on_conflict_do_nothing(index_elements=[Item.sku])
        .returning(Item.sku, sort_by_parameter_order=ordered)
    )
    return list(session.scalars(stmt, rows))


@dataclass(frozen=True)
class BenchResult:
    cursor_calls: int
    best_ms: float


def benchmark(n: int = 5000, repeats: int = 5) -> dict[str, BenchResult]:
    """Best-of-`repeats` wall time and cursor calls for each write strategy."""
    rows = item_rows(n)
    skus = [row["sku"] for row in rows]
    strategies: dict[str, Callable[[Session], object]] = {
        "add_all_flush": lambda s: insert_unit_of_work(s, rows),
        "bulk_executemany": lambda s: insert_bulk(s, rows),
        "bulk_returning_unordered": lambda s: insert_bulk_returning(s, rows, ordered=False),
        "bulk_returning_ordered_rowid": lambda s: insert_bulk_returning(s, rows, ordered=True),
        "bulk_returning_ordered_uuid": lambda s: insert_shipments_returning(s, skus),
    }
    out: dict[str, BenchResult] = {}
    for name, run in strategies.items():
        best = float("inf")
        calls = 0
        for _ in range(repeats):
            engine, log = make_engine()
            with Session(engine) as session:
                start = time.perf_counter()
                run(session)
                session.commit()
                best = min(best, time.perf_counter() - start)
            calls = sum(1 for e in log.entries if e.statement.startswith("INSERT"))
            engine.dispose()
        out[name] = BenchResult(calls, round(best * 1000, 1))
    return out


# --- An API that accepts a batch and upserts it in one round trip ----------------


class ItemIn(BaseModel):
    sku: str = Field(min_length=1, max_length=32)
    qty: int = Field(ge=0)


class BatchIn(BaseModel):
    items: list[ItemIn] = Field(min_length=1, max_length=MAX_BATCH)

    @model_validator(mode="after")
    def skus_are_unique(self) -> Self:
        # PostgreSQL rejects a batch that hits one row twice (SQLSTATE 21000);
        # reject it at the edge with 422 instead of a 500 from the database.
        seen: set[str] = set()
        for item in self.items:
            if item.sku in seen:
                raise ValueError(f"duplicate sku in batch: {item.sku}")
            seen.add(item.sku)
        return self


class ItemOut(BaseModel):
    id: int
    sku: str
    qty: int | None


async def make_async_engine() -> tuple[AsyncEngine, StatementLog]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    log = StatementLog()
    event.listen(engine.sync_engine, "before_cursor_execute", log.record)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.clear()
    return engine, log


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessions
    async with sessions() as session:
        yield session


def build_app(sessions: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI()
    app.state.sessions = sessions

    @app.put("/items")
    async def bulk_upsert(  # pyright: ignore[reportUnusedFunction]
        body: BatchIn, session: Annotated[AsyncSession, Depends(get_session)]
    ) -> list[ItemOut]:
        base = sqlite_insert(Item)
        stmt = base.on_conflict_do_update(
            index_elements=[Item.sku], set_={"qty": base.excluded.qty}
        ).returning(Item.id, Item.sku, Item.qty)
        result = await session.execute(stmt, [item.model_dump() for item in body.items])
        # RETURNING order is unspecified: correlate by the natural key, not position.
        by_sku = {row.sku: ItemOut(id=row.id, sku=row.sku, qty=row.qty) for row in result}
        await session.commit()
        return [by_sku[item.sku] for item in body.items]

    return app
