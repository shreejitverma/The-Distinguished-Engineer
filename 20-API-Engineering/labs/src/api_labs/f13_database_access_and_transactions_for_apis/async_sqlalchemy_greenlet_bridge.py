"""How AsyncSession bridges sync ORM code onto asyncio, and why lazy loads raise MissingGreenlet.

Part 1 re-implements the bridge in ~40 lines on top of ``greenlet`` so every switch is visible.
Part 2 drives the real ``sqlalchemy.ext.asyncio`` stack over ``aiosqlite`` and exposes the loading
strategies an async API can use: implicit lazy load (fails), ``awaitable_attrs``, ``selectinload``,
``run_sync``, ``refresh(attribute_names=...)``, and ``raiseload`` (fails fast, no IO).
Part 3 wraps them in a FastAPI app whose error handler classifies both shapes of the failure.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Annotated, Any, Protocol, cast

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import ForeignKey, String, event, exc, select
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    raiseload,
    relationship,
    selectinload,
)
from sqlalchemy.pool import NullPool

if TYPE_CHECKING:
    import contextvars

    from sqlalchemy.pool import Pool

# ---------------------------------------------------------------------------
# Part 1: the bridge, rebuilt from scratch
# ---------------------------------------------------------------------------


class _Greenlet(Protocol):
    """The subset of ``greenlet.greenlet`` the bridge uses (greenlet ships no type stubs)."""

    parent: _Greenlet | None
    gr_context: contextvars.Context | None

    @property
    def dead(self) -> bool: ...

    def switch(self, *args: Any, **kwargs: Any) -> Any: ...

    def throw(self, *args: Any) -> Any: ...


_greenlet_module: Any = importlib.import_module("greenlet")
_getcurrent = cast("Callable[[], _Greenlet]", _greenlet_module.getcurrent)
_greenlet_init = cast("Callable[..., None]", _greenlet_module.greenlet.__init__)


class MiniMissingGreenletError(RuntimeError):
    """Raised by ``mini_await`` when the caller is not inside ``mini_spawn``."""


class _BridgeGreenlet(_greenlet_module.greenlet):  # type: ignore[misc]
    """A child greenlet that marks itself as bridge-provided and inherits the task's context."""

    is_bridge = True

    def __init__(self, fn: Callable[..., Any], driver: _Greenlet) -> None:
        _greenlet_init(self, fn, driver)
        # Without this copy a new greenlet starts with an empty contextvars.Context.
        self.gr_context = driver.gr_context


def mini_await[T](awaitable: Awaitable[T], trace: list[str] | None = None) -> T:
    """Suspend the sync caller, hand ``awaitable`` to the event loop, return its result."""
    current = _getcurrent()
    if not getattr(current, "is_bridge", False):
        if isinstance(awaitable, Coroutine):
            cast("Coroutine[Any, Any, T]", awaitable).close()  # no "never awaited" warning
        raise MiniMissingGreenletError("mini_await() called outside mini_spawn()")
    if trace is not None:
        trace.append("child: switch to parent with awaitable")
    parent = current.parent
    if parent is None:
        raise MiniMissingGreenletError("bridge greenlet has no driver parent")
    return cast("T", parent.switch(awaitable))


async def mini_spawn[T](
    fn: Callable[..., T], *args: Any, trace: list[str] | None = None, **kwargs: Any
) -> T:
    """Run sync ``fn`` in a child greenlet; await each awaitable it passes up via mini_await."""
    child = cast("_Greenlet", _BridgeGreenlet(fn, _getcurrent()))
    result: Any = child.switch(*args, **kwargs)
    while not child.dead:
        if trace is not None:
            trace.append("driver: awaiting on the event loop")
        try:
            value = await result
        except BaseException:
            # Re-raise inside the sync frame so its try/except and finally blocks run.
            result = child.throw(*sys.exc_info())
        else:
            if trace is not None:
                trace.append("driver: switch result back into child")
            result = child.switch(value)
    return cast("T", result)


def is_missing_greenlet(error: BaseException) -> bool:
    """True for both shapes: bare ``MissingGreenlet`` and ``StatementError`` wrapping it."""
    if isinstance(error, exc.MissingGreenlet):
        return True
    return isinstance(error, exc.StatementError) and isinstance(error.orig, exc.MissingGreenlet)


# ---------------------------------------------------------------------------
# Part 2: the real stack
# ---------------------------------------------------------------------------


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Author(Base):
    __tablename__ = "author"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    # Default lazy="select": first access emits SQL from inside attribute __get__.
    books: Mapped[list[Book]] = relationship(back_populates="author", order_by="Book.id")


class Book(Base):
    __tablename__ = "book"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"))
    author: Mapped[Author] = relationship(back_populates="books")


class SqlCounter:
    """Counts statements that reach the cursor (a sync-engine event, as the docs require)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.statements: list[str] = []
        event.listen(engine.sync_engine, "before_cursor_execute", self._on_execute)

    def _on_execute(self, *args: Any) -> None:
        statement = cast("str", args[2])
        self.statements.append(statement.split()[0].upper())

    @property
    def selects(self) -> int:
        return self.statements.count("SELECT")


def make_engine(url: str = "sqlite+aiosqlite://", *, null_pool: bool = False) -> AsyncEngine:
    """In-memory SQLite gets ``StaticPool``; ``null_pool=True`` connects on every checkout."""
    if null_pool:
        return create_async_engine(url, poolclass=NullPool)
    return create_async_engine(url)


def pool_class_name(engine: AsyncEngine) -> str:
    pool: Pool = engine.sync_engine.pool
    return type(pool).__name__


async def create_schema_and_seed(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as session, session.begin():
        session.add_all(
            [
                Author(id=1, name="Ada", books=[Book(id=1, title="Notes"), Book(id=2, title="G")]),
                Author(id=2, name="Brian", books=[Book(id=3, title="Unix")]),
            ]
        )


def sessions(
    engine: AsyncEngine, *, expire_on_commit: bool = False
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=expire_on_commit)


async def get_author(session: AsyncSession, author_id: int) -> Author:
    return (await session.execute(select(Author).where(Author.id == author_id))).scalar_one()


async def titles_via_implicit_lazy_load(session: AsyncSession, author_id: int) -> list[str]:
    """The bug: plain attribute access on the event loop needs IO it cannot await."""
    author = await get_author(session, author_id)
    return [book.title for book in author.books]


async def titles_via_awaitable_attrs(session: AsyncSession, author_id: int) -> list[str]:
    author = await get_author(session, author_id)
    books: list[Book] = await author.awaitable_attrs.books
    return [book.title for book in books]


async def titles_via_selectinload(session: AsyncSession, author_id: int) -> list[str]:
    stmt = select(Author).where(Author.id == author_id).options(selectinload(Author.books))
    author = (await session.execute(stmt)).scalar_one()
    return [book.title for book in author.books]


async def titles_via_run_sync(session: AsyncSession, author_id: int) -> list[str]:
    def sync_block(sync_session: Session) -> list[str]:
        author = sync_session.get_one(Author, author_id)
        return [book.title for book in author.books]  # lazy load is legal here

    return await session.run_sync(sync_block)


async def titles_via_refresh(session: AsyncSession, author_id: int) -> list[str]:
    author = await get_author(session, author_id)
    await session.refresh(author, attribute_names=["books"])
    return [book.title for book in author.books]


async def titles_via_raiseload(session: AsyncSession, author_id: int) -> list[str]:
    stmt = select(Author).where(Author.id == author_id).options(raiseload(Author.books))
    author = (await session.execute(stmt)).scalar_one()
    return [book.title for book in author.books]


async def name_after_commit(session: AsyncSession, author_id: int) -> str:
    """With ``expire_on_commit=True`` a plain column read after commit is also a lazy load."""
    author = await get_author(session, author_id)
    await session.commit()
    return author.name


# ---------------------------------------------------------------------------
# Part 3: the API surface
# ---------------------------------------------------------------------------


class BookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str


class AuthorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    books: list[BookOut]


LOADERS: dict[str, Callable[[AsyncSession, int], Awaitable[list[str]]]] = {
    "lazy": titles_via_implicit_lazy_load,
    "awaitable": titles_via_awaitable_attrs,
    "selectin": titles_via_selectinload,
    "run_sync": titles_via_run_sync,
    "refresh": titles_via_refresh,
}


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session


# Module level on purpose: with postponed annotations FastAPI resolves this name from the
# module globals, so a closure-local alias would silently turn ``session`` into a query param.
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def build_app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI()
    app.state.session_factory = factory

    @app.exception_handler(exc.SQLAlchemyError)
    async def sqlalchemy_error(request: Request, error: exc.SQLAlchemyError) -> JSONResponse:
        if is_missing_greenlet(error):
            # A programming error, not a database outage: never retry it, page the owner.
            return JSONResponse({"error": "implicit_io", "code": "xd2s"}, status_code=500)
        return JSONResponse({"error": "database"}, status_code=503)

    @app.get("/authors/{author_id}/titles/{strategy}")
    async def titles(author_id: int, strategy: str, session: SessionDep) -> dict[str, list[str]]:
        return {"titles": await LOADERS[strategy](session, author_id)}

    @app.get("/authors/{author_id}", response_model=AuthorOut)
    async def author_orm(author_id: int, session: SessionDep) -> Any:
        # Returns the ORM object: FastAPI reads .books during response validation, on the loop.
        return await get_author(session, author_id)

    @app.get("/authors/{author_id}/eager", response_model=AuthorOut)
    async def author_eager(author_id: int, session: SessionDep) -> Any:
        stmt = select(Author).where(Author.id == author_id).options(selectinload(Author.books))
        return (await session.execute(stmt)).scalar_one()

    return app
