"""Database sessions consumed by a StreamingResponse body: scope traps and fixes.

FastAPI 0.162.0 runs the exit code of a ``scope="function"`` yield dependency before
``await response(scope, receive, send)``, so a streaming body that touches the session
runs against a session that has already been closed.  SQLAlchemy 2.1 does not make that
loud by default: ``Session.close()`` only resets the session, the next statement
re-acquires a pooled connection, and nobody returns it.  This module builds one app that
reproduces each failure and each fix against a file-backed SQLite database.
"""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

import anyio
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from sqlalchemy import ForeignKey, String, func, select
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.pool import QueuePool

Scope = Literal["function", "request"]


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base with ``awaitable_attrs`` for explicit async lazy loads."""


class Author(Base):
    __tablename__ = "author"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    books: Mapped[list["Book"]] = relationship(back_populates="author", lazy="select")


class Book(Base):
    __tablename__ = "book"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"))
    author: Mapped[Author] = relationship(back_populates="books")


class AuditRow(Base):
    __tablename__ = "audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    note: Mapped[str] = mapped_column(String(100))


TITLES = ("RFC 9110", "RFC 9112", "PEP 525")


@dataclass
class StreamLab:
    """Engine, session factories, and a deterministic event log for one test."""

    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    strict_factory: async_sessionmaker[AsyncSession]
    events: list[str] = field(default_factory=list[str])
    sessions: list[AsyncSession] = field(default_factory=list[AsyncSession])
    pin_sessions: bool = True
    stall: anyio.Event = field(default_factory=anyio.Event)

    def record(self, event: str) -> None:
        self.events.append(event)

    def checked_out(self) -> int:
        """Connections currently checked out of the engine's queue pool."""
        pool = self.engine.pool
        if not isinstance(pool, QueuePool):
            raise TypeError(f"expected a QueuePool, got {type(pool).__name__}")
        return pool.checkedout()

    async def audit_count(self) -> int:
        async with self.factory() as session:
            count = await session.scalar(select(func.count()).select_from(AuditRow))
        return int(count or 0)

    async def close_pinned_sessions(self) -> None:
        for session in self.sessions:
            await session.close()


async def make_lab(db_path: Path, *, pin_sessions: bool = True) -> StreamLab:
    """Create a one-connection pool over a file database and seed one author."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.05,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine)
    async with factory.begin() as session:
        session.add(Author(id=1, name="Fielding", books=[Book(title=t) for t in TITLES]))
    strict_factory = async_sessionmaker(engine, close_resets_only=False)
    return StreamLab(
        engine=engine,
        factory=factory,
        strict_factory=strict_factory,
        pin_sessions=pin_sessions,
    )


def build_streaming_app(lab: StreamLab) -> FastAPI:
    """Register the same streaming endpoints once per dependency scope."""
    app = FastAPI()

    async def get_session() -> AsyncIterator[AsyncSession]:
        session = lab.factory()
        if lab.pin_sessions:
            lab.sessions.append(session)
        lab.record("session:open")
        try:
            yield session
        finally:
            await session.close()
            lab.record("session:closed")

    async def get_strict_session() -> AsyncIterator[AsyncSession]:
        async with lab.strict_factory() as session:
            yield session
        lab.record("strict:closed")

    async def get_tx_session() -> AsyncIterator[AsyncSession]:
        session = lab.factory()
        lab.sessions.append(session)
        async with session.begin():
            yield session
        lab.record("tx:committed")
        await session.close()

    def register(scope: Scope) -> None:
        @app.get(f"/{scope}/titles")
        async def titles(
            session: Annotated[AsyncSession, Depends(get_session, scope=scope)],
        ) -> StreamingResponse:
            lab.record("endpoint:return")

            async def body() -> AsyncIterator[bytes]:
                lab.record(f"stream:start(checked_out={lab.checked_out()})")
                result = await session.stream_scalars(select(Book.title).order_by(Book.id))
                async for title in result:
                    yield f"{title}\n".encode()
                lab.record(f"stream:end(checked_out={lab.checked_out()})")

            return StreamingResponse(body(), media_type="text/plain")

        @app.get(f"/{scope}/books-of-author")
        async def books_of_author(
            session: Annotated[AsyncSession, Depends(get_session, scope=scope)],
        ) -> StreamingResponse:
            author = await session.get(Author, 1)
            if author is None:
                raise LookupError("seed author 1 is missing")

            async def body() -> AsyncIterator[bytes]:
                yield f"{author.name}:\n".encode()
                for book in await author.awaitable_attrs.books:
                    yield f"- {book.title}\n".encode()

            return StreamingResponse(body(), media_type="text/plain")

        @app.get(f"/{scope}/audit")
        async def audit(
            session: Annotated[AsyncSession, Depends(get_tx_session, scope=scope)],
        ) -> StreamingResponse:
            async def body() -> AsyncIterator[bytes]:
                for title in TITLES:
                    session.add(AuditRow(note=f"streamed {title}"))
                    await session.flush()
                    yield f"logged {title}\n".encode()
                lab.record("stream:end")

            return StreamingResponse(body(), media_type="text/plain")

    register("function")
    register("request")

    @app.get("/function/strict-titles")
    async def strict_titles(
        session: Annotated[AsyncSession, Depends(get_strict_session, scope="function")],
    ) -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            yield b"header-row\n"
            for title in await session.scalars(select(Book.title)):
                yield f"{title}\n".encode()

        return StreamingResponse(body(), media_type="text/plain")

    @app.get("/function/materialized")
    async def materialized(
        session: Annotated[AsyncSession, Depends(get_session, scope="function")],
    ) -> StreamingResponse:
        titles = list(await session.scalars(select(Book.title).order_by(Book.id)))

        def body() -> Iterator[bytes]:
            lab.record(f"stream:start(checked_out={lab.checked_out()})")
            for title in titles:
                yield f"{title}\n".encode()

        return StreamingResponse(body(), media_type="text/plain")

    @app.get("/owned/stall")
    async def owned_stall() -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            async with lab.factory() as session:
                try:
                    first = await session.scalar(select(Book.title).order_by(Book.id))
                    yield f"{first}\n".encode()
                    lab.record(f"stream:stalled(checked_out={lab.checked_out()})")
                    await lab.stall.wait()
                finally:
                    lab.record("owned:exiting")

        return StreamingResponse(body(), media_type="text/plain")

    @app.get("/request/stall")
    async def request_stall(
        session: Annotated[AsyncSession, Depends(get_session, scope="request")],
    ) -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            first = await session.scalar(select(Book.title).order_by(Book.id))
            yield f"{first}\n".encode()
            lab.record(f"stream:stalled(checked_out={lab.checked_out()})")
            await lab.stall.wait()

        return StreamingResponse(body(), media_type="text/plain")

    return app
