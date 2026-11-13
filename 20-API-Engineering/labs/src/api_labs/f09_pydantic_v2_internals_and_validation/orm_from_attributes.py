"""Validate SQLAlchemy objects with from_attributes=True and refuse implicit lazy loads.

pydantic-core reads every model field with getattr(); on a mapped instance that getattr is an
InstrumentedAttribute descriptor, so an unloaded relationship or an expired column turns into a
SELECT (sync Session), a MissingGreenlet (AsyncSession), or a DetachedInstanceError (no Session).
This module provides the mapping, the response schemas, a statement log, and a guard that
inspects SQLAlchemy instance state without touching the descriptors.
"""

from __future__ import annotations

import types
from typing import TYPE_CHECKING, Annotated, Any, Union, cast, get_args, get_origin

from fastapi import Depends, FastAPI, Request
from pydantic import AliasPath, BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey, event, inspect, select
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncSession, async_sessionmaker
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    selectinload,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Iterator, Sequence

    from sqlalchemy import Engine
    from sqlalchemy.engine import Connection
    from sqlalchemy.orm import InstanceState


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base; AsyncAttrs adds the awaitable_attrs accessor."""


class Author(Base):
    __tablename__ = "author"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    # Default lazy="select": the first getattr() on an unloaded collection emits a SELECT.
    books: Mapped[list[Book]] = relationship(back_populates="author", order_by="Book.id")


class Book(Base):
    __tablename__ = "book"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"))
    author: Mapped[Author] = relationship(back_populates="books")


class BookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str


class AuthorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    books: list[BookOut]


class AuthorSummary(BaseModel):
    """Columns only: validating it can never trigger a relationship load."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class BookWithAuthorName(BaseModel):
    """AliasPath flattens book.author.name; with attributes that is two getattr() calls."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    author_name: Annotated[str, Field(validation_alias=AliasPath("author", "name"))]


class StatementLog:
    """Records every SQL statement an Engine sends to the DBAPI cursor."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.statements: list[str] = []
        event.listen(engine, "before_cursor_execute", self._record)

    def _record(
        self,
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        self.statements.append(" ".join(statement.split()))

    def mark(self) -> int:
        return len(self.statements)

    def since(self, mark: int) -> list[str]:
        return self.statements[mark:]

    def close(self) -> None:
        event.remove(self.engine, "before_cursor_execute", self._record)


def seed(session: Session, authors: int = 3, books_per_author: int = 2) -> None:
    for a in range(1, authors + 1):
        books = [Book(title=f"book-{a}-{b}") for b in range(1, books_per_author + 1)]
        session.add(Author(id=a, name=f"author-{a}", books=books))
    session.commit()


class LazyLoadRefusedError(Exception):
    """Raised instead of letting from_attributes validation touch an unloaded attribute."""

    def __init__(self, model: type[BaseModel], paths: Sequence[str]) -> None:
        self.paths = tuple(paths)
        super().__init__(f"{model.__name__} would load {', '.join(self.paths)} from the database")


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    """Return the BaseModel inside list[M], M | None, or M; None for anything else."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = get_origin(annotation)
    if origin in (list, set, frozenset, tuple, Union, types.UnionType):
        for arg in get_args(annotation):
            found = _nested_model(arg)
            if found is not None:
                return found
    return None


def _attribute_name(name: str, alias: object) -> str:
    """The first attribute pydantic-core will getattr() for this field."""
    if isinstance(alias, AliasPath) and alias.path and isinstance(alias.path[0], str):
        return alias.path[0]
    if isinstance(alias, str):
        return alias
    return name


def unloaded_paths(obj: object, model: type[BaseModel], prefix: str = "") -> list[str]:
    """Dotted paths that model.model_validate(obj, from_attributes=True) would lazy load.

    Reads InstanceState.unloaded and InstanceState.dict only, so it never emits SQL.
    """
    state: InstanceState[Any] | None = inspect(obj, raiseerr=False)
    if state is None:
        return []
    unloaded = state.unloaded
    found: list[str] = []
    for name, field in model.model_fields.items():
        attr = _attribute_name(name, field.validation_alias)
        path = f"{prefix}{attr}"
        if attr in unloaded:
            found.append(path)
            continue
        nested = _nested_model(field.annotation)
        if nested is None or attr not in state.dict:
            continue
        value: object = state.dict[attr]
        children: list[object] = [value]
        if isinstance(value, list | set | tuple):
            children = [*cast("Iterable[object]", value)]
        for index, child in enumerate(children):
            found.extend(unloaded_paths(child, nested, f"{path}.{index}."))
    return found


def validate_loaded[M: BaseModel](model: type[M], obj: object) -> M:
    """model_validate(obj, from_attributes=True), but only if no attribute needs IO."""
    paths = unloaded_paths(obj, model)
    if paths:
        raise LazyLoadRefusedError(model, paths)
    return model.model_validate(obj, from_attributes=True)


async def get_async_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    events: list[str] = request.app.state.events
    try:
        async with factory() as session:
            yield session
    finally:
        events.append("session-closed")


# scope="function" closes the session as early as FastAPI allows; the lab shows that is still
# after response validation, because serialize_response runs inside the route handler.
AsyncSessionDep = Annotated[AsyncSession, Depends(get_async_session, scope="function")]


def build_async_app(factory: async_sessionmaker[AsyncSession], events: list[str]) -> FastAPI:
    """Async endpoints whose response_model validation runs on the event loop thread."""
    app = FastAPI()
    app.state.session_factory = factory
    app.state.events = events

    @app.get("/authors/lazy", response_model=list[AuthorOut])
    async def authors_lazy(session: AsyncSessionDep) -> Sequence[Author]:
        return (await session.scalars(select(Author).order_by(Author.id))).all()

    @app.get("/authors/eager", response_model=list[AuthorOut])
    async def authors_eager(session: AsyncSessionDep) -> Sequence[Author]:
        stmt = select(Author).options(selectinload(Author.books)).order_by(Author.id)
        return (await session.scalars(stmt)).all()

    return app


def get_sync_session(request: Request) -> Iterator[Session]:
    engine: Engine = request.app.state.engine
    with Session(engine) as session:
        yield session


SyncSessionDep = Annotated[Session, Depends(get_sync_session)]


def build_sync_app(engine: Engine) -> FastAPI:
    """A def endpoint: FastAPI validates the response in a worker thread, so lazy loads succeed."""
    app = FastAPI()
    app.state.engine = engine

    @app.get("/authors/lazy", response_model=list[AuthorOut])
    def authors_lazy(session: SyncSessionDep) -> Sequence[Author]:
        return session.scalars(select(Author).order_by(Author.id)).all()

    return app
