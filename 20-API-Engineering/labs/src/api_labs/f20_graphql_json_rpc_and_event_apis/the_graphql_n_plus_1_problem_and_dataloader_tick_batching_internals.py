"""Counting the GraphQL N+1 query cascade and its DataLoader fix.

The lab runs one Strawberry schema over an in-memory SQLite database through
SQLAlchemy's async engine.
Two interchangeable data sources sit behind the resolvers: ``NaiveSource`` issues one
``WHERE author_id = ?`` query per parent object, and ``BatchedSource`` routes every lookup
through a ``strawberry.dataloader.DataLoader`` so that all lookups made in one event-loop
window become a single ``WHERE author_id IN (...)`` query.
``QueryLog`` counts every statement that reaches the DBAPI cursor.
"""

from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

import strawberry
from fastapi import FastAPI
from sqlalchemy import ForeignKey, event, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from strawberry.dataloader import DataLoader
from strawberry.fastapi import BaseContext, GraphQLRouter
from strawberry.types import Info


class Base(DeclarativeBase):
    """Declarative base for the lab tables."""


class AuthorRow(Base):
    __tablename__ = "authors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class BookRow(Base):
    __tablename__ = "books"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("authors.id"), index=True)
    title: Mapped[str]


class ReviewRow(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), index=True)
    stars: Mapped[int]


@dataclass(frozen=True, slots=True)
class BookRecord:
    id: int
    author_id: int
    title: str


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    id: int
    book_id: int
    stars: int


type SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(slots=True)
class QueryLog:
    """Records every statement the DBAPI cursor executes on one engine."""

    statements: list[str] = field(default_factory=list[str])
    parameter_counts: list[int] = field(default_factory=list[int])

    def attach(self, engine: AsyncEngine) -> None:
        event.listen(engine.sync_engine, "before_cursor_execute", self._record)

    def _record(
        self,
        _conn: object,
        _cursor: object,
        statement: str,
        parameters: Sequence[object],
        _context: object,
        _executemany: bool,
    ) -> None:
        self.statements.append(" ".join(statement.split()))
        self.parameter_counts.append(len(parameters))

    def reset(self) -> None:
        self.statements.clear()
        self.parameter_counts.clear()

    def count_containing(self, fragment: str) -> int:
        return sum(1 for statement in self.statements if fragment in statement)


@dataclass(slots=True)
class Database:
    engine: AsyncEngine
    session_factory: SessionFactory
    log: QueryLog


@asynccontextmanager
async def database(
    *, authors: int = 50, books_per_author: int = 3, reviews_per_book: int = 2
) -> AsyncIterator[Database]:
    """Create and seed an in-memory SQLite database, then start counting queries.

    ``sqlite+aiosqlite://`` uses ``StaticPool``, so every session shares one DBAPI connection.
    The log is attached after seeding, so seed statements are not counted.
    """
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory: SessionFactory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        book_id = 0
        review_id = 0
        for author_id in range(1, authors + 1):
            session.add(AuthorRow(id=author_id, name=f"author-{author_id}"))
            for _ in range(books_per_author):
                book_id += 1
                session.add(BookRow(id=book_id, author_id=author_id, title=f"book-{book_id}"))
                for _ in range(reviews_per_book):
                    review_id += 1
                    session.add(ReviewRow(id=review_id, book_id=book_id, stars=review_id % 5 + 1))
    log = QueryLog()
    log.attach(engine)
    try:
        yield Database(engine=engine, session_factory=factory, log=log)
    finally:
        await engine.dispose()


def group_in_key_order[K, R](
    keys: Sequence[K], rows: Iterable[R], key_of: Callable[[R], K]
) -> list[list[R]]:
    """Return one list per key, in key order; a key with no rows gets an empty list.

    This is the DataLoader batch-function contract: same length as ``keys``, same order.
    """
    grouped: defaultdict[K, list[R]] = defaultdict(list)
    for row in rows:
        grouped[key_of(row)].append(row)
    return [grouped.get(key, []) for key in keys]


def _book_record(row: BookRow) -> BookRecord:
    return BookRecord(id=row.id, author_id=row.author_id, title=row.title)


def _review_record(row: ReviewRow) -> ReviewRecord:
    return ReviewRecord(id=row.id, book_id=row.book_id, stars=row.stars)


class DataSource(Protocol):
    async def books_for(self, author_id: int) -> list[BookRecord]: ...

    async def reviews_for(self, book_id: int) -> list[ReviewRecord]: ...


class NaiveSource:
    """One query per parent: the N+1 cascade."""

    def __init__(self, factory: SessionFactory) -> None:
        self._factory = factory

    async def books_for(self, author_id: int) -> list[BookRecord]:
        async with self._factory() as session:
            statement = select(BookRow).where(BookRow.author_id == author_id).order_by(BookRow.id)
            return [_book_record(row) for row in (await session.scalars(statement))]

    async def reviews_for(self, book_id: int) -> list[ReviewRecord]:
        async with self._factory() as session:
            statement = select(ReviewRow).where(ReviewRow.book_id == book_id).order_by(ReviewRow.id)
            return [_review_record(row) for row in (await session.scalars(statement))]


class BatchedSource:
    """Every lookup goes through a DataLoader: one ``IN`` query per batch.

    Construct one instance per request; the loaders memoize every key they have seen.
    """

    def __init__(self, factory: SessionFactory, *, max_batch_size: int | None = None) -> None:
        self._factory = factory
        self.books = DataLoader[int, list[BookRecord]](
            load_fn=self._load_books, max_batch_size=max_batch_size
        )
        self.reviews = DataLoader[int, list[ReviewRecord]](
            load_fn=self._load_reviews, max_batch_size=max_batch_size
        )

    async def _load_books(self, author_ids: list[int]) -> list[list[BookRecord]]:
        async with self._factory() as session:
            statement = (
                select(BookRow).where(BookRow.author_id.in_(author_ids)).order_by(BookRow.id)
            )
            rows = [_book_record(row) for row in (await session.scalars(statement))]
        return group_in_key_order(author_ids, rows, lambda book: book.author_id)

    async def _load_reviews(self, book_ids: list[int]) -> list[list[ReviewRecord]]:
        async with self._factory() as session:
            statement = (
                select(ReviewRow).where(ReviewRow.book_id.in_(book_ids)).order_by(ReviewRow.id)
            )
            rows = [_review_record(row) for row in (await session.scalars(statement))]
        return group_in_key_order(book_ids, rows, lambda review: review.book_id)

    async def books_for(self, author_id: int) -> list[BookRecord]:
        return await self.books.load(author_id)

    async def reviews_for(self, book_id: int) -> list[ReviewRecord]:
        return await self.reviews.load(book_id)


class Context(BaseContext):
    """Per-request context; ``strawberry.fastapi`` requires a ``BaseContext`` subclass or a dict.

    The router writes ``request``, ``background_tasks`` and ``response`` onto this object on
    every request, so one ``Context`` instance must never serve two requests.
    """

    def __init__(self, session_factory: SessionFactory, source: DataSource) -> None:
        super().__init__()
        self.session_factory = session_factory
        self.source = source


def build_context(
    factory: SessionFactory, *, batched: bool, max_batch_size: int | None = None
) -> Context:
    """Build the per-request context; call once per request so loader caches never leak."""
    source: DataSource = (
        BatchedSource(factory, max_batch_size=max_batch_size) if batched else NaiveSource(factory)
    )
    return Context(session_factory=factory, source=source)


@strawberry.type
class Review:
    id: int
    stars: int


@strawberry.type
class Book:
    id: int
    title: str

    @strawberry.field
    async def reviews(self, info: Info[Context, None]) -> list[Review]:
        records = await info.context.source.reviews_for(self.id)
        return [Review(id=record.id, stars=record.stars) for record in records]


@strawberry.type
class Author:
    id: int
    name: str

    @strawberry.field
    async def books(self, info: Info[Context, None]) -> list[Book]:
        records = await info.context.source.books_for(self.id)
        return [Book(id=record.id, title=record.title) for record in records]


@strawberry.type
class Query:
    @strawberry.field
    async def authors(self, info: Info[Context, None], limit: int | None = None) -> list[Author]:
        statement = select(AuthorRow).order_by(AuthorRow.id).limit(limit)
        async with info.context.session_factory() as session:
            rows = await session.scalars(statement)
            return [Author(id=row.id, name=row.name) for row in rows]


schema = strawberry.Schema(query=Query)

AUTHORS_AND_BOOKS = "{ authors { id name books { id title } } }"
AUTHORS_BOOKS_AND_REVIEWS = "{ authors { id books { id reviews { id stars } } } }"


async def run_query(query: str, context: Context) -> dict[str, Any]:
    """Execute ``query`` against the lab schema and raise on any GraphQL error."""
    result = await schema.execute(query, context_value=context)
    if result.errors:
        raise RuntimeError(result.errors)
    assert result.data is not None  # noqa: S101
    return result.data


def build_app(factory: SessionFactory, *, share_loaders_across_requests: bool) -> FastAPI:
    """Mount the schema on FastAPI; the flag chooses a per-request or a process-wide context.

    The shared variant is the bug the note warns about: one ``BatchedSource`` (so one pair of
    ``DataLoader`` caches) serves every request, so a later request reads values memoized by an
    earlier one.
    """
    shared = BatchedSource(factory) if share_loaders_across_requests else None

    async def get_context() -> Context:
        if shared is not None:
            return Context(session_factory=factory, source=shared)
        return build_context(factory, batched=True)

    app = FastAPI()
    app.include_router(GraphQLRouter(schema, context_getter=get_context), prefix="/graphql")
    return app
