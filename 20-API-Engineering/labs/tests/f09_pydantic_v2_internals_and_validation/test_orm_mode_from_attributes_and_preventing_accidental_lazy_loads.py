"""from_attributes=True reads fields with getattr(); on SQLAlchemy objects that can be IO."""

from __future__ import annotations

import gc
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import ResponseValidationError
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, raiseload, selectinload
from sqlalchemy.pool import StaticPool

from api_labs.f09_pydantic_v2_internals_and_validation.orm_from_attributes import (
    Author,
    AuthorOut,
    AuthorSummary,
    Base,
    Book,
    BookOut,
    BookWithAuthorName,
    LazyLoadRefusedError,
    StatementLog,
    build_async_app,
    build_sync_app,
    seed,
    unloaded_paths,
    validate_loaded,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy import Engine
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

# aiosqlite's cursor() awaitable is not a coroutine, so SQLAlchemy cannot close it when it
# raises MissingGreenlet; CPython reports it when the object is collected.
UNAWAITED_CURSOR = "ignore:coroutine 'Connection.cursor' was never awaited:RuntimeWarning"


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(eng)
    with Session(eng) as session:
        seed(session)
    yield eng
    eng.dispose()


@pytest.fixture
def log(engine: Engine) -> Iterator[StatementLog]:
    statement_log = StatementLog(engine)
    yield statement_log
    statement_log.close()


@pytest_asyncio.fixture
async def async_engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    async with factory() as session:
        await session.run_sync(seed)
    yield eng
    await eng.dispose()
    gc.collect()


def inspect_unloaded(obj: object) -> set[str]:
    unloaded: set[str] = inspect(obj).unloaded
    return unloaded


def _error_types(exc: ValidationError) -> list[tuple[tuple[int | str, ...], str]]:
    return [(err["loc"], err["type"]) for err in exc.errors()]


@pytest.mark.unit
def test_lazy_relationship_emits_one_select_per_parent_during_validation(
    engine: Engine, log: StatementLog
) -> None:
    with Session(engine) as session:
        authors = session.scalars(select(Author).order_by(Author.id)).all()
        before = log.mark()
        out = [AuthorOut.model_validate(a) for a in authors]
        during = log.since(before)
    assert [len(a.books) for a in out] == [2, 2, 2]
    assert len(during) == 3
    assert all(s.startswith("SELECT book.id") and "WHERE ? = book.author_id" in s for s in during)


@pytest.mark.unit
def test_selectinload_moves_all_relationship_io_before_validation(
    engine: Engine, log: StatementLog
) -> None:
    with Session(engine) as session:
        start = log.mark()
        stmt = select(Author).options(selectinload(Author.books)).order_by(Author.id)
        authors = session.scalars(stmt).all()
        loaded = log.since(start)
        before = log.mark()
        out = [AuthorOut.model_validate(a) for a in authors]
        assert log.since(before) == []
    assert len(loaded) == 2
    assert "WHERE book.author_id IN (?, ?, ?)" in loaded[1]
    assert out[2].books[1].title == "book-3-2"


@pytest.mark.unit
def test_columns_only_schema_never_touches_relationships(engine: Engine, log: StatementLog) -> None:
    with Session(engine) as session:
        authors = session.scalars(select(Author)).all()
        before = log.mark()
        out = [AuthorSummary.model_validate(a) for a in authors]
        assert log.since(before) == []
    assert out[0] == AuthorSummary(id=1, name="author-1")


@pytest.mark.unit
def test_raiseload_turns_the_lazy_load_into_a_get_attribute_error(engine: Engine) -> None:
    with Session(engine) as session:
        author = session.scalars(select(Author).options(raiseload("*"))).first()
        with pytest.raises(ValidationError) as excinfo:
            AuthorOut.model_validate(author)
    err = excinfo.value.errors()[0]
    assert err["type"] == "get_attribute_error"
    assert err["loc"] == ("books",)
    assert err["msg"] == (
        "Error extracting attribute: InvalidRequestError: "
        "'Author.books' is not available due to lazy='raise'"
    )


@pytest.mark.unit
def test_detached_instance_error_is_wrapped_in_validation_error(engine: Engine) -> None:
    with Session(engine) as session:
        author = session.scalars(select(Author)).first()
    with pytest.raises(ValidationError) as excinfo:
        AuthorOut.model_validate(author)
    assert _error_types(excinfo.value) == [(("books",), "get_attribute_error")]
    assert "DetachedInstanceError: Parent instance" in excinfo.value.errors()[0]["msg"]
    assert "lazy load operation of attribute 'books' cannot proceed" in str(excinfo.value)


@pytest.mark.unit
def test_expire_on_commit_turns_even_column_reads_into_io(
    engine: Engine, log: StatementLog
) -> None:
    with Session(engine) as session:
        stmt = select(Author).options(selectinload(Author.books)).where(Author.id == 1)
        author = session.scalars(stmt).one()
        session.commit()
        assert sorted(inspect_unloaded(author)) == ["books", "id", "name"]
        before = log.mark()
        AuthorOut.model_validate(author)
        refresh, books = log.since(before)
    # 2.1 renders "author.id, author.name"; 2.0 adds "AS author_id" labels.
    assert refresh.startswith("SELECT author.id")
    assert refresh.endswith("FROM author WHERE author.id = ?")
    assert books.startswith("SELECT book.id")
    # Commit, then close, then validate: every field fails, in declaration order.
    with Session(engine) as session:
        author = session.scalars(stmt).one()
        session.commit()
    with pytest.raises(ValidationError) as excinfo:
        AuthorOut.model_validate(author)
    assert _error_types(excinfo.value) == [
        (("id",), "get_attribute_error"),
        (("name",), "get_attribute_error"),
        (("books",), "get_attribute_error"),
    ]
    assert "attribute refresh operation cannot proceed" in excinfo.value.errors()[0]["msg"]


class BookNoConfig(BaseModel):
    id: int
    title: str


class AuthorStrictNested(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int
    name: str
    books: list[BookNoConfig]


@pytest.mark.unit
def test_runtime_flag_overrides_nested_config_but_class_config_does_not(engine: Engine) -> None:
    with Session(engine) as session:
        stmt = select(Author).options(selectinload(Author.books)).where(Author.id == 1)
        author = session.scalars(stmt).one()
        with pytest.raises(ValidationError) as excinfo:
            AuthorStrictNested.model_validate(author)
        assert _error_types(excinfo.value) == [
            (("books", 0), "model_type"),
            (("books", 1), "model_type"),
        ]
        out = AuthorStrictNested.model_validate(author, from_attributes=True)
    assert out.books == [BookNoConfig(id=1, title="book-1-1"), BookNoConfig(id=2, title="book-1-2")]


@pytest.mark.unit
def test_extra_forbid_is_not_enforced_for_attribute_input() -> None:
    class Row:
        def __init__(self) -> None:
            self.id = 7
            self.name = "row"
            self.books: list[object] = []
            self.internal_flag = "never-checked"

    out = AuthorStrictNested.model_validate(Row())
    assert out.model_dump() == {"id": 7, "name": "row", "books": []}
    with pytest.raises(ValidationError) as excinfo:
        AuthorStrictNested.model_validate({"id": 7, "name": "row", "books": [], "x": 1})
    assert _error_types(excinfo.value) == [(("x",), "extra_forbidden")]


class AuthorWithDefault(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    books: list[BookOut] = []


@pytest.mark.unit
def test_attribute_error_inside_a_property_silently_becomes_missing() -> None:
    class Broken:
        id = 1
        name = "broken"

        @property
        def books(self) -> list[object]:
            raise AttributeError("bug inside the property")

    assert AuthorWithDefault.model_validate(Broken()).books == []
    with pytest.raises(ValidationError) as excinfo:
        AuthorOut.model_validate(Broken())
    assert _error_types(excinfo.value) == [(("books",), "missing")]


@pytest.mark.unit
def test_fields_are_read_in_declaration_order_and_errors_accumulate() -> None:
    reads: list[str] = []

    class Spy:
        def __getattr__(self, name: str) -> Any:
            reads.append(name)
            raise AttributeError(name)

    with pytest.raises(ValidationError) as excinfo:
        AuthorOut.model_validate(Spy())
    assert reads == ["id", "name", "books"]
    assert excinfo.value.error_count() == 3


@pytest.mark.unit
def test_builtin_types_are_rejected_before_any_getattr() -> None:
    with pytest.raises(ValidationError) as excinfo:
        AuthorOut.model_validate(5)
    assert _error_types(excinfo.value) == [((), "model_attributes_type")]


@pytest.mark.unit
def test_raise_on_sql_allows_identity_map_hits_for_many_to_one(engine: Engine) -> None:
    with Session(engine) as session:
        stmt = select(Book).options(raiseload(Book.author, sql_only=True)).order_by(Book.id)
        book = session.scalars(stmt).first()
        assert book is not None
        with pytest.raises(ValidationError) as excinfo:
            BookWithAuthorName.model_validate(book)
        assert "lazy='raise_on_sql'" in excinfo.value.errors()[0]["msg"]
        # The identity map holds weak references: keep the parent alive or the hit is lost.
        parent = session.get(Author, 1)
        assert parent is not None
        assert BookWithAuthorName.model_validate(book).author_name == "author-1"


@pytest.mark.unit
def test_alias_path_walks_a_relationship_with_getattr(engine: Engine, log: StatementLog) -> None:
    with Session(engine) as session:
        book = session.scalars(select(Book).order_by(Book.id)).first()
        before = log.mark()
        out = BookWithAuthorName.model_validate(book)
        (author_select,) = log.since(before)
    assert out.author_name == "author-1"
    assert author_select.startswith("SELECT author.id")
    assert author_select.endswith("FROM author WHERE author.id = ?")


@pytest.mark.unit
def test_guard_reports_unloaded_paths_without_emitting_sql(
    engine: Engine, log: StatementLog
) -> None:
    with Session(engine) as session:
        authors = session.scalars(select(Author).order_by(Author.id)).all()
        before = log.mark()
        assert unloaded_paths(authors[0], AuthorOut) == ["books"]
        with pytest.raises(LazyLoadRefusedError) as excinfo:
            validate_loaded(AuthorOut, authors[0])
        assert log.since(before) == []
        assert excinfo.value.paths == ("books",)
        stmt = select(Author).options(selectinload(Author.books)).where(Author.id == 1)
        author = session.scalars(stmt).one()
        session.expire(author.books[1], ["title"])
        assert unloaded_paths(author, AuthorOut) == ["books.1.title"]
        session.refresh(author.books[1])
        assert validate_loaded(AuthorOut, author).books[1].title == "book-1-2"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.filterwarnings(UNAWAITED_CURSOR)
async def test_async_lazy_load_is_missing_greenlet_inside_validation_error(
    async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as session:
        author = (await session.scalars(select(Author).order_by(Author.id))).first()
        with pytest.raises(ValidationError) as excinfo:
            AuthorOut.model_validate(author)
    (err,) = excinfo.value.errors()
    assert err["type"] == "get_attribute_error"
    assert err["loc"] == ("books",)
    # 2.1 wraps it in StatementError and shows the SQL; 2.0 raises the bare MissingGreenlet.
    assert "MissingGreenlet" in err["msg"]
    assert "greenlet_spawn has not been called" in err["msg"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_awaitable_attrs_and_run_sync_make_async_validation_safe(
    async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as session:
        author = (await session.scalars(select(Author).order_by(Author.id))).first()
        assert author is not None
        await author.awaitable_attrs.books
        assert AuthorOut.model_validate(author).books[0].title == "book-1-1"

        def validate_in_greenlet(sync_session: Session) -> AuthorOut:
            return AuthorOut.model_validate(sync_session.get(Author, 2))

        out = await session.run_sync(validate_in_greenlet)
    assert out.books[1].title == "book-2-2"


def _client(app: Any, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.filterwarnings(UNAWAITED_CURSOR)
async def test_fastapi_async_endpoint_lazy_relationship_fails_response_validation(
    async_engine: AsyncEngine,
) -> None:
    events: list[str] = []
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        async_engine, expire_on_commit=False
    )
    app = build_async_app(factory, events)
    async with _client(app) as client:
        with pytest.raises(ResponseValidationError) as excinfo:
            await client.get("/authors/lazy")
    errors = excinfo.value.errors()
    assert [(e["loc"], e["type"]) for e in errors] == [
        (("response", i, "books"), "get_attribute_error") for i in range(3)
    ]
    # MissingGreenlet, not DetachedInstanceError: the scope="function" session was still open.
    assert all("MissingGreenlet" in e["msg"] for e in errors)
    assert events == ["session-closed"]
    async with _client(app, raise_app_exceptions=False) as client:
        response = await client.get("/authors/lazy")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_async_endpoint_with_selectinload_serializes(
    async_engine: AsyncEngine,
) -> None:
    events: list[str] = []
    app = build_async_app(async_sessionmaker(async_engine, expire_on_commit=False), events)
    async with _client(app) as client:
        response = await client.get("/authors/eager")
    assert response.status_code == 200
    body = response.json()
    assert [len(a["books"]) for a in body] == [2, 2, 2]
    assert body[0] == {
        "id": 1,
        "name": "author-1",
        "books": [{"id": 1, "title": "book-1-1"}, {"id": 2, "title": "book-1-2"}],
    }
    assert events == ["session-closed"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_sync_endpoint_hides_n_plus_one_in_the_threadpool(
    engine: Engine, log: StatementLog
) -> None:
    app = build_sync_app(engine)
    before = log.mark()
    async with _client(app) as client:
        response = await client.get("/authors/lazy")
    statements = log.since(before)
    assert response.status_code == 200
    assert [len(a["books"]) for a in response.json()] == [2, 2, 2]
    assert len(statements) == 1 + 3
    assert sum(s.startswith("SELECT book.id") for s in statements) == 3


@pytest.mark.unit
def test_jsonable_encoder_without_response_model_reads_only_the_instance_dict(
    engine: Engine,
) -> None:
    with Session(engine) as session:
        author = session.scalars(select(Author).where(Author.id == 1)).one()
        assert jsonable_encoder(author) == {"id": 1, "name": "author-1"}
        session.commit()
        assert jsonable_encoder(author) == {}
