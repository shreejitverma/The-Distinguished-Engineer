"""Lab: AIP-157 partial responses with the mask pushed down to SQL."""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING, Any

import httpx
import pydantic
import pytest
from google.protobuf import descriptor_pb2, field_mask_pb2  # type: ignore[import-untyped]
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import DatabaseError, InvalidRequestError
from sqlalchemy.orm import Session, load_only

from api_labs.f05_pagination_filtering_and_query_patterns.read_mask import (
    MAX_MASK_LENGTH,
    MAX_MASK_PATHS,
    Author,
    Book,
    MaskError,
    break_overflow_chains,
    canonical_paths,
    create_app,
    create_schema,
    fetch_books,
    overflow_pages,
    parse_mask,
    parse_tree,
    predicted_overflow_pages,
    selection_for,
    serialize,
    single_blob_record_size,
    table_leaf_local_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine
    from starlette.applications import Starlette

BODY_LEN = 50_000
BIO_LEN = 10_000
ADMIN = frozenset({"admin"})


def _seed(engine: Engine) -> None:
    create_schema(engine)
    with Session(engine) as s:
        s.add_all(
            [
                Author(id=1, display_name="Ursula", bio="u" * BIO_LEN),
                Author(id=2, display_name="Iain", bio="i" * BIO_LEN),
            ]
        )
        s.add_all(
            [
                Book(
                    id=i,
                    title=f"Book {i}",
                    rating=i % 5,
                    labels=["sf", f"l{i}"],
                    author_id=1 + i % 2,
                    internal_notes=f"cost {i}",
                    body="b" * BODY_LEN,
                )
                for i in range(1, 4)
            ]
        )
        s.commit()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "books.db"
    engine = create_engine(f"sqlite:///{path}")
    _seed(engine)
    engine.dispose()
    return path


@pytest.fixture
def engine(db_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{db_path}")
    yield eng
    eng.dispose()


@pytest.fixture
def statements(engine: Engine) -> list[str]:
    seen: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(*args: Any) -> None:  # pyright: ignore[reportUnusedFunction]
        seen.append(str(args[2]))

    return seen


def _client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _selected_columns(sql: str) -> list[str]:
    match = re.match(r"SELECT (.*?)\s+FROM ", sql, re.DOTALL)
    assert match is not None, sql
    return [c.strip().split(" AS ")[0] for c in match.group(1).split(",")]


# ---------------------------------------------------------------- mask grammar


@pytest.mark.unit
@pytest.mark.parametrize(
    "mask",
    [
        "title,author.display_name",
        "author,author.display_name",
        "author.display_name,author",
        "author.bio,author.display_name,title,title",
        "body,author.id,labels,rating",
    ],
)
def test_canonical_form_matches_protobuf_field_mask_tree(mask: str) -> None:
    upstream = field_mask_pb2.FieldMask()
    upstream.CanonicalFormFromMask(field_mask_pb2.FieldMask(paths=mask.split(",")))
    assert canonical_paths(parse_tree(mask)) == list(upstream.paths)


@pytest.mark.unit
def test_protobuf_runtime_rejects_star_and_non_terminal_repeated_paths() -> None:
    desc = descriptor_pb2.FileDescriptorProto.DESCRIPTOR

    def valid(*paths: str) -> bool:
        return bool(field_mask_pb2.FieldMask(paths=list(paths)).IsValidForDescriptor(desc))

    assert valid("name", "options.java_package")
    assert valid("message_type")  # a repeated field as the last segment
    assert not valid("message_type.name")  # repeated field in the middle
    assert not valid("*")


@pytest.mark.unit
def test_protobuf_json_form_is_lower_camel_and_comma_joined() -> None:
    mask = field_mask_pb2.FieldMask()
    mask.FromJsonString("author.displayName,title")
    assert list(mask.paths) == ["author.display_name", "title"]
    assert mask.ToJsonString() == "author.displayName,title"
    mask.FromJsonString("")
    assert list(mask.paths) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mask", "reason"),
    [
        ("title,", "invalid field path ''"),
        (" title", "invalid field path ' title'"),
        ("Title", "invalid field path 'Title'"),
        ("author.*", "invalid field path 'author.*'"),
        ("labels.0", "invalid field path 'labels.0'"),
        ("author..bio", "invalid field path 'author..bio'"),
        ("labels.name", "field 'labels' in 'labels.name' is repeated"),
        ("title.length", "field 'title' in 'title.length' is not a message"),
        ("no_such_field", "unknown field path 'no_such_field'"),
        ("author.email", "unknown field path 'author.email'"),
    ],
)
def test_malformed_and_unknown_paths_are_rejected(mask: str, reason: str) -> None:
    with pytest.raises(MaskError) as info:
        parse_mask(mask)
    assert str(info.value).startswith(reason)


@pytest.mark.unit
def test_hidden_field_is_rejected_exactly_like_a_missing_one() -> None:
    with pytest.raises(MaskError) as hidden:
        parse_mask("internal_notes")
    with pytest.raises(MaskError) as missing:
        parse_mask("internal_notez")
    assert str(hidden.value).replace("internal_notes", "X") == str(missing.value).replace(
        "internal_notez", "X"
    )
    assert parse_mask("internal_notes", ADMIN) == {"id": None, "internal_notes": None}


@pytest.mark.unit
def test_star_expands_to_what_this_caller_may_see() -> None:
    assert "internal_notes" not in parse_mask("*")
    assert "internal_notes" in parse_mask("*", ADMIN)
    assert parse_mask("*")["author"] == {"id": None, "display_name": None, "bio": None}


@pytest.mark.unit
def test_mask_length_and_path_count_limits() -> None:
    at_limit = ",".join(["title"] * MAX_MASK_PATHS)
    assert parse_mask(at_limit) == {"id": None, "title": None}
    with pytest.raises(MaskError, match="more than 32 paths"):
        parse_mask(at_limit + ",title")
    padded = "t" * MAX_MASK_LENGTH  # one 512-character segment: too long a name, not a length error
    with pytest.raises(MaskError, match="invalid field path"):
        parse_mask(padded)
    with pytest.raises(MaskError, match="longer than 512 characters"):
        parse_mask(padded + "t")


@pytest.mark.unit
def test_views_defaults_and_conflicts() -> None:
    def keys(fields: str | None, view: str | None, default: str) -> set[str]:
        return set(selection_for(fields, view, default_view=default, roles=frozenset()))

    basic = {"id", "title", "rating", "labels", "author"}
    full = basic | {"body"}
    assert keys(None, None, "BOOK_VIEW_BASIC") == basic
    assert keys("", None, "BOOK_VIEW_BASIC") == basic  # empty mask -> default view
    assert keys(None, "BOOK_VIEW_UNSPECIFIED", "BOOK_VIEW_FULL") == full
    assert keys(None, "BOOK_VIEW_FULL", "BOOK_VIEW_BASIC") == full
    assert keys("title", None, "BOOK_VIEW_FULL") == {"id", "title"}
    with pytest.raises(MaskError, match="at most one of fields and view"):
        keys("title", "BOOK_VIEW_FULL", "BOOK_VIEW_BASIC")
    with pytest.raises(MaskError, match="unknown view 'FULL'"):
        keys(None, "FULL", "BOOK_VIEW_BASIC")


# ---------------------------------------------------------------- push-down to SQL


@pytest.mark.unit
@pytest.mark.asyncio
async def test_title_mask_selects_only_id_and_title(engine: Engine, statements: list[str]) -> None:
    async with _client(create_app(engine)) as client:
        response = await client.get("/books/2", params={"fields": "title"})
    assert response.status_code == 200
    assert response.json() == {"id": 2, "title": "Book 2"}
    assert len(statements) == 1
    assert _selected_columns(statements[0]) == ["books.id", "books.title"]
    assert "JOIN" not in statements[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nested_mask_joins_author_and_projects_one_column(
    engine: Engine, statements: list[str]
) -> None:
    async with _client(create_app(engine)) as client:
        response = await client.get("/books/1", params={"fields": "title,author.display_name"})
    assert response.json() == {"id": 1, "title": "Book 1", "author": {"display_name": "Iain"}}
    assert len(statements) == 1
    # SQLAlchemy adds the foreign key and the joined entity's primary key on its own.
    assert _selected_columns(statements[0]) == [
        "books.id",
        "books.title",
        "books.author_id",
        "authors_1.id",
        "authors_1.display_name",
    ]
    assert "LEFT OUTER JOIN authors" in statements[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basic_list_is_one_statement_without_heavy_columns(
    engine: Engine, statements: list[str]
) -> None:
    async with _client(create_app(engine)) as client:
        response = await client.get("/books")
    books = response.json()["books"]
    assert [sorted(b) for b in books] == [["author", "id", "labels", "rating", "title"]] * 3
    assert books[0]["author"] == {"display_name": "Iain"}
    assert len(statements) == 1
    columns = _selected_columns(statements[0])
    assert "books.body" not in columns
    assert "authors_1.bio" not in columns
    assert "books.internal_notes" not in columns


@pytest.mark.unit
def test_unrequested_attributes_raise_instead_of_lazy_loading(engine: Engine) -> None:
    with Session(engine) as session:
        (book,) = fetch_books(session, parse_mask("title"), book_id=1)
        with pytest.raises(InvalidRequestError, match=r"'Book\.body' is not available"):
            _ = book.body
        with pytest.raises(InvalidRequestError, match=r"'Book\.author' is not available"):
            _ = book.author
        assert serialize(book, parse_mask("title")) == {"id": 1, "title": "Book 1"}


@pytest.mark.unit
def test_nested_entity_also_raises_on_unrequested_attributes(
    engine: Engine, statements: list[str]
) -> None:
    mask = parse_mask("title,author.display_name")
    with Session(engine) as session:
        (book,) = fetch_books(session, mask, book_id=1)
        assert book.author is not None
        before = len(statements)
        with pytest.raises(InvalidRequestError, match=r"'Author\.bio' is not available"):
            _ = book.author.bio
        assert len(statements) == before  # raised before any lazy-load SELECT
        assert serialize(book, mask) == {
            "id": 1,
            "title": "Book 1",
            "author": {"display_name": "Iain"},
        }


@pytest.mark.unit
def test_load_only_without_raiseload_turns_one_query_into_n_plus_one(
    engine: Engine, statements: list[str]
) -> None:
    with Session(engine) as session:
        books = session.scalars(select(Book).options(load_only(Book.title))).all()
        assert len(statements) == 1
        assert sum(len(b.body) for b in books) == 3 * BODY_LEN
    assert len(statements) == 1 + len(books)
    assert statements[-1].startswith("SELECT books.body")


@pytest.mark.unit
def test_pydantic_from_attributes_cannot_filter_after_a_projected_load(engine: Engine) -> None:
    class BookOut(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(from_attributes=True)
        id: int
        title: str
        body: str

    with Session(engine) as session:
        (book,) = fetch_books(session, parse_mask("title"), book_id=1)
        with pytest.raises(pydantic.ValidationError) as info:
            BookOut.model_validate(book)
    (error,) = info.value.errors()
    assert error["type"] == "get_attribute_error"
    assert error["loc"] == ("body",)


# ---------------------------------------------------------------- the I/O claim


@pytest.mark.unit
@pytest.mark.asyncio
async def test_broken_overflow_chains_prove_masked_reads_never_touch_body(db_path: Path) -> None:
    pages = overflow_pages(db_path, "books")
    assert len(pages) == 3 * (BODY_LEN // 4092)  # each body spills to 12 overflow pages
    break_overflow_chains(db_path, pages)
    fresh = create_engine(f"sqlite:///{db_path}")  # no page cache from before the damage
    try:
        async with _client(create_app(fresh)) as client:
            listed = await client.get("/books")
            one = await client.get("/books/1", params={"fields": "title,rating,author"})
            assert listed.status_code == 200
            assert len(listed.json()["books"]) == 3
            assert one.json()["author"]["bio"] == "i" * BIO_LEN  # authors table is intact
            with pytest.raises(DatabaseError, match="malformed"):
                await client.get("/books/1", params={"fields": "body"})
            with pytest.raises(DatabaseError, match="malformed"):
                await client.get("/books/1")  # FULL view is the Get default
    finally:
        fresh.dispose()


def _broken_one_row_db(path: Path, columns: str) -> sqlite3.Connection:
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA page_size = 4096")
        conn.execute("PRAGMA auto_vacuum = NONE")
        conn.execute(f"CREATE TABLE t (id INTEGER PRIMARY KEY, {columns})")
        conn.execute("INSERT INTO t (id, body, title) VALUES (1, ?, 'small')", (b"\xab" * 200_000,))
    conn.close()
    break_overflow_chains(path, overflow_pages(path, "t"))
    return sqlite3.connect(path)


@pytest.mark.unit
def test_a_column_stored_after_a_large_value_walks_its_overflow_chain(tmp_path: Path) -> None:
    last = _broken_one_row_db(tmp_path / "last.db", "title TEXT, body BLOB")
    middle = _broken_one_row_db(tmp_path / "middle.db", "body BLOB, title TEXT")
    try:
        assert last.execute("SELECT title FROM t").fetchone() == ("small",)
        with pytest.raises(sqlite3.DatabaseError, match="malformed"):
            middle.execute("SELECT title FROM t").fetchone()
        # length() is answered from the record header's serial type, so no chain walk.
        for conn in (last, middle):
            assert conn.execute("SELECT length(body) FROM t").fetchone() == (200_000,)
            with pytest.raises(sqlite3.DatabaseError, match="malformed"):
                conn.execute("SELECT body FROM t").fetchone()
    finally:
        last.close()
        middle.close()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("blob_len", "local", "pages"),
    [
        (4_058, 4_061, 0),  # payload == X: the whole record stays on the leaf
        (4_059, 489, 1),  # one byte more: K > X, so only M bytes stay
        (4_486, 489, 1),  # still K > X
        (8_150, 4_061, 1),  # K == X exactly
        (8_151, 489, 2),
        (200_000, 3_588, 48),  # K <= X: K bytes stay, the rest spills
        (1_000_000, 1_556, 244),
    ],
)
def test_overflow_layout_matches_the_file_format_formula(
    tmp_path: Path, blob_len: int, local: int, pages: int
) -> None:
    path = tmp_path / "one.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA page_size = 4096")
        conn.execute("CREATE TABLE t (b BLOB)")
        conn.execute("INSERT INTO t VALUES (?)", (b"\x00" * blob_len,))
        (on_leaf,) = conn.execute(
            "SELECT payload FROM dbstat WHERE name = 't' AND pagetype = 'leaf'"
        ).fetchone()
    conn.close()
    payload = single_blob_record_size(blob_len)
    assert on_leaf == table_leaf_local_bytes(payload) == local
    assert len(overflow_pages(path, "t")) == predicted_overflow_pages(payload) == pages


# ---------------------------------------------------------------- wire contract


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basic_view_is_a_fraction_of_full_on_the_wire(
    db_path: Path, engine: Engine, statements: list[str]
) -> None:
    sizes: dict[str, tuple[int, int]] = {}  # bytes on the wire, columns in the one SELECT
    async with _client(create_app(engine)) as client:
        for label, url, params in [
            ("list_basic", "/books", {}),
            ("list_full", "/books", {"view": "BOOK_VIEW_FULL"}),
            ("get_title", "/books/1", {"fields": "title"}),
            ("get_default", "/books/1", {}),
        ]:
            statements.clear()
            response = await client.get(url, params=params)
            assert response.status_code == 200
            assert len(statements) == 1
            sizes[label] = (len(response.content), len(_selected_columns(statements[0])))
    assert len(overflow_pages(db_path, "books")) == 36
    assert len(overflow_pages(db_path, "authors")) == 4
    # Deterministic for this seed; the note's Numbers table quotes these values.
    assert sizes == {
        "list_basic": (286, 7),
        "list_full": (180_364, 9),
        "get_title": (25, 2),
        "get_default": (60_116, 9),
    }
    assert sizes["list_basic"][0] * 100 < sizes["list_full"][0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_errors_are_400_invalid_argument_and_404_not_found(engine: Engine) -> None:
    async with _client(create_app(engine)) as client:
        bad = await client.get("/books", params={"fields": "internal_notes"})
        admin = await client.get("/books/1", params={"fields": "internal_notes"})
        missing = await client.get("/books/99", params={"fields": "title"})
    assert bad.status_code == 400
    assert bad.json() == {
        "error": {
            "code": 400,
            "message": "unknown field path 'internal_notes'",
            "status": "INVALID_ARGUMENT",
        }
    }
    assert admin.status_code == 400  # the default app has no roles
    assert missing.status_code == 404
    assert missing.json()["error"]["status"] == "NOT_FOUND"
    async with _client(create_app(engine, roles=ADMIN)) as client:
        allowed = await client.get("/books/1", params={"fields": "internal_notes"})
    assert allowed.json() == {"id": 1, "internal_notes": "cost 1"}
