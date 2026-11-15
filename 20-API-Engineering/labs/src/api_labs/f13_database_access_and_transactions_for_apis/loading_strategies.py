"""SQLAlchemy 2.1 relationship loading strategies, measured by counting cursor executions.

Every claim in the note is a statement count, a rendered SQL shape, or an exception, so the lab
records each ``cursor.execute`` through the ``before_cursor_execute`` engine event and lets the
tests assert on the list.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import Depends, FastAPI, Request
from sqlalchemy import (
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Table,
    create_engine,
    event,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    ORMExecuteState,
    Session,
    immediateload,
    joinedload,
    lazyload,
    mapped_column,
    raiseload,
    relationship,
    selectinload,
    subqueryload,
)
from sqlalchemy.pool import StaticPool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy import Engine, Select
    from sqlalchemy.sql.base import ExecutableOption

Strategy = Literal["lazy", "immediate", "selectin", "subquery", "joined"]
STRATEGIES: tuple[Strategy, ...] = ("lazy", "immediate", "selectin", "subquery", "joined")


class Base(DeclarativeBase):
    pass


author_tag = Table(
    "author_tag",
    Base.metadata,
    Column("author_id", ForeignKey("author.id"), primary_key=True),
    Column("tag_id", ForeignKey("tag.id"), primary_key=True),
)


class Author(Base):
    __tablename__ = "author"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    books: Mapped[list[Book]] = relationship(
        back_populates="author", order_by="Book.id", cascade="all, delete-orphan"
    )
    tags: Mapped[list[Tag]] = relationship(secondary=author_tag, order_by="Tag.id")


class Tag(Base):
    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(20))


class Book(Base):
    __tablename__ = "book"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"))
    author: Mapped[Author] = relationship(back_populates="books")
    reviews: Mapped[list[Review]] = relationship(
        back_populates="book", order_by="Review.id", cascade="all, delete-orphan"
    )


class Review(Base):
    __tablename__ = "review"

    id: Mapped[int] = mapped_column(primary_key=True)
    stars: Mapped[int]
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id"))
    book: Mapped[Book] = relationship(back_populates="reviews")


class Edition(Base):
    """Composite primary key: selectinload must use a tuple IN."""

    __tablename__ = "edition"

    prefix: Mapped[str] = mapped_column(String(3), primary_key=True)
    seq: Mapped[int] = mapped_column(primary_key=True)
    printings: Mapped[list[Printing]] = relationship(order_by="Printing.id")


class Printing(Base):
    __tablename__ = "printing"
    __table_args__ = (ForeignKeyConstraint(["prefix", "seq"], ["edition.prefix", "edition.seq"]),)

    id: Mapped[int] = mapped_column(primary_key=True)
    prefix: Mapped[str] = mapped_column(String(3))
    seq: Mapped[int]


# --- measuring -------------------------------------------------------------------------------

_IN_LIST = re.compile(r"IN \(((?:\?, )*\?)\)")


@dataclass
class StatementLog:
    """Every SQL string the DBAPI cursor was asked to execute, whitespace-normalized."""

    statements: list[str] = field(default_factory=list[str])

    def __len__(self) -> int:
        return len(self.statements)

    def clear(self) -> None:
        self.statements.clear()

    def in_list_sizes(self) -> list[int]:
        """Bound parameters inside each flat ``IN (?, ...)`` list, in execution order."""
        return [
            match.group(1).count("?")
            for statement in self.statements
            for match in _IN_LIST.finditer(statement)
        ]


def record_statements(engine: Engine, log: StatementLog) -> None:
    """Append every statement ``engine`` executes to ``log`` (works for ``sync_engine`` too)."""

    def before_cursor_execute(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        log.statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", before_cursor_execute)


@contextmanager
def counting_engine() -> Iterator[tuple[Engine, StatementLog]]:
    """A private in-memory SQLite engine with the schema created and a statement log attached."""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    log = StatementLog()
    record_statements(engine, log)
    try:
        yield engine, log
    finally:
        engine.dispose()


# --- data --------------------------------------------------------------------------------------


def build_graph(
    n_authors: int, books_per_author: int = 3, reviews_per_book: int = 2, tags: int = 0
) -> list[Author]:
    """Deterministic object graph; ids encode position so assertions can name rows."""
    shared_tags = [Tag(id=t + 1, label=f"tag{t + 1}") for t in range(tags)]
    authors: list[Author] = []
    for a in range(1, n_authors + 1):
        books = [
            Book(
                id=a * 100 + b,
                title=f"a{a}-b{b}",
                reviews=[
                    Review(id=(a * 100 + b) * 10 + r, stars=r + 1) for r in range(reviews_per_book)
                ],
            )
            for b in range(books_per_author)
        ]
        authors.append(Author(id=a, name=f"author-{a}", books=books, tags=list(shared_tags)))
    return authors


def seed(engine: Engine, n_authors: int, **graph: int) -> None:
    with Session(engine) as session:
        session.add_all(build_graph(n_authors, **graph))
        session.commit()


# --- the strategies ----------------------------------------------------------------------------


def books_option(strategy: Strategy) -> ExecutableOption:
    """The loader option for ``Author.books`` under each strategy."""
    if strategy == "lazy":
        return lazyload(Author.books)
    if strategy == "immediate":
        return immediateload(Author.books)
    if strategy == "selectin":
        return selectinload(Author.books)
    if strategy == "subquery":
        return subqueryload(Author.books)
    return joinedload(Author.books)


def authors_with_books(strategy: Strategy) -> Select[tuple[Author]]:
    return select(Author).order_by(Author.id).options(books_option(strategy))


def count_books(session: Session, strategy: Strategy) -> int:
    """Load every author and touch every ``books`` collection, as a serializer would."""
    result = session.scalars(authors_with_books(strategy))
    if strategy == "joined":
        result = result.unique()  # required for joined eager loads against collections
    return sum(len(author.books) for author in result)


# --- enforcement -------------------------------------------------------------------------------


def raiseload_everything(orm_execute_state: ORMExecuteState) -> None:
    """``do_orm_execute`` hook: append ``raiseload("*")`` to every top-level ORM SELECT.

    Relationship loads (lazy, selectin, subquery) and column refreshes are skipped because the
    options of the top-level statement already propagate to them.
    """
    if (
        orm_execute_state.is_select
        and not orm_execute_state.is_column_load
        and not orm_execute_state.is_relationship_load
    ):
        orm_execute_state.statement = orm_execute_state.statement.options(raiseload("*"))


class RaiseloadSession(Session):
    """A ``Session`` for tests: any relationship the query did not load raises before SQL."""


event.listen(RaiseloadSession, "do_orm_execute", raiseload_everything)


# --- the API -----------------------------------------------------------------------------------


def create_async_engine() -> AsyncEngine:
    return _create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)


async def create_schema(engine: AsyncEngine, n_authors: int) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as session:
        session.add_all(build_graph(n_authors))
        await session.commit()


def author_payload(author: Author) -> dict[str, Any]:
    return {
        "id": author.id,
        "books": [
            {"title": book.title, "stars": [review.stars for review in book.reviews]}
            for book in author.books
        ],
    }


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessions() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def create_app(engine: AsyncEngine, session_class: type[Session] = Session) -> FastAPI:
    """``session_class=RaiseloadSession`` is the test configuration; production uses ``Session``."""
    app = FastAPI()
    app.state.sessions = async_sessionmaker(
        engine, expire_on_commit=False, sync_session_class=session_class
    )

    @app.get("/authors")
    async def list_authors(session: SessionDep) -> list[dict[str, Any]]:
        stmt = (
            select(Author)
            .order_by(Author.id)
            .options(selectinload(Author.books).selectinload(Book.reviews))
        )
        return [author_payload(author) for author in await session.scalars(stmt)]

    @app.get("/authors-regressed")
    async def list_authors_regressed(session: SessionDep) -> list[dict[str, Any]]:
        # A later edit added ``stars`` to the payload but not ``Book.reviews`` to the options.
        stmt = select(Author).order_by(Author.id).options(selectinload(Author.books))
        return [author_payload(author) for author in await session.scalars(stmt)]

    return app
