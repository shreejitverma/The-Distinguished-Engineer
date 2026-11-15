"""Session internals lab: identity map uniquing, topological flush order, expire_on_commit.

Everything runs against in-memory SQLite (`pysqlite` for sync, `aiosqlite` for async)
with foreign keys enforced, so a flush that emitted rows in the wrong order would fail
with an `IntegrityError` instead of passing silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel
from sqlalchemy import ForeignKey, String, create_engine, event, inspect
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.pool import StaticPool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import InstanceState


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base; `AsyncAttrs` adds the `awaitable_attrs` accessor."""


class Author(Base):
    __tablename__ = "author"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    books: Mapped[list[Book]] = relationship(back_populates="author", cascade="all, delete-orphan")


class Book(Base):
    __tablename__ = "book"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"))
    author: Mapped[Author] = relationship(back_populates="books")


class Node(Base):
    """Self-referential table: a mapper that depends on itself forms a cycle."""

    __tablename__ = "node"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("node.id"))
    parent: Mapped[Node | None] = relationship(remote_side="Node.id", back_populates="children")
    children: Mapped[list[Node]] = relationship(back_populates="parent")


_DML = re.compile(r"^\s*(?:(INSERT) INTO|(UPDATE)|(DELETE) FROM)\s+(\w+)", re.IGNORECASE)


@dataclass
class StatementLog:
    """Every statement the engine sends to the DBAPI cursor, in order."""

    entries: list[tuple[str, Any]] = field(default_factory=list[tuple[str, Any]])

    def record(
        self,
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        self.entries.append((statement, parameters))

    def clear(self) -> None:
        self.entries.clear()

    def count(self, verb: str) -> int:
        """Number of statements whose first keyword is `verb` (for example `SELECT`)."""
        return sum(1 for sql, _ in self.entries if sql.lstrip().upper().startswith(verb))

    def dml(self) -> list[str]:
        """INSERT/UPDATE/DELETE statements as `"<VERB> <table>"`, in emission order."""
        out: list[str] = []
        for sql, _ in self.entries:
            match = _DML.match(sql)
            if match:
                verb = match.group(1) or match.group(2) or match.group(3)
                out.append(f"{verb.upper()} {match.group(4)}")
        return out

    def parameters_for(self, prefix: str) -> list[Any]:
        """Bound parameters of every statement starting with `prefix`."""
        return [params for sql, params in self.entries if sql.lstrip().startswith(prefix)]


def _enable_sqlite_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def make_sync_engine() -> tuple[Engine, StatementLog]:
    """One shared in-memory SQLite connection, schema created, log cleared."""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    log = StatementLog()
    event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    event.listen(engine, "before_cursor_execute", log.record)
    Base.metadata.create_all(engine)
    log.clear()
    return engine, log


async def make_async_engine() -> tuple[AsyncEngine, StatementLog]:
    """Async twin of `make_sync_engine`; events attach to the proxied sync engine."""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    log = StatementLog()
    event.listen(engine.sync_engine, "connect", _enable_sqlite_foreign_keys)
    event.listen(engine.sync_engine, "before_cursor_execute", log.record)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.clear()
    return engine, log


def lifecycle_state(obj: object) -> str:
    """Return the ORM object state: transient, pending, persistent, deleted, or detached."""
    state: InstanceState[Any] = inspect(obj)
    for name in ("transient", "pending", "persistent", "deleted", "detached"):
        if getattr(state, name):
            return name
    raise AssertionError(f"unreachable: {state!r} has no lifecycle state")


class AuthorIn(BaseModel):
    name: str


class AuthorOut(BaseModel):
    id: int
    name: str


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Request-scoped session from the factory stored on `app.state`."""
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessions
    async with sessions() as session:
        yield session


def build_app(sessions: async_sessionmaker[AsyncSession]) -> FastAPI:
    """A create endpoint that reads the ORM object after `await session.commit()`."""
    app = FastAPI()
    app.state.sessions = sessions

    @app.post("/authors", status_code=201)
    async def create_author(  # pyright: ignore[reportUnusedFunction]
        body: AuthorIn, session: Annotated[AsyncSession, Depends(get_session)]
    ) -> AuthorOut:
        author = Author(name=body.name)
        session.add(author)
        await session.commit()
        # With expire_on_commit=True this attribute read is a lazy refresh SELECT
        # outside greenlet_spawn, so it raises MissingGreenlet.
        return AuthorOut(id=author.id, name=author.name)

    return app
