"""AIP-160 filters: precedence, allowlist, bound parameters, budgets, and typed literals."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
)
from sqlalchemy.pool import StaticPool

from api_labs.f05_pagination_filtering_and_query_patterns.aip160_filter import (
    FieldSpec,
    FieldType,
    FilterError,
    Limits,
    Schema,
    build_books_app,
    canonical,
    compile_filter,
    parse_filter,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

metadata = MetaData()
books = Table(
    "books",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String, nullable=False),
    Column("price", Float, nullable=False),
    Column("pages", Integer, nullable=False),
    Column("in_print", Boolean, nullable=False),
    Column("genre", String, nullable=False),
    Column("published", DateTime, nullable=False),  # naive UTC
    Column("publisher_country", String, nullable=True),
    Column("rating", Integer, nullable=True),
    Column("internal_cost", Float, nullable=False),  # deliberately not filterable
)

SCHEMA: Schema = {
    "title": FieldSpec(books.c.title, FieldType.STRING),
    "price": FieldSpec(books.c.price, FieldType.FLOAT),
    "pages": FieldSpec(books.c.pages, FieldType.INT),
    "in_print": FieldSpec(books.c.in_print, FieldType.BOOL),
    "genre": FieldSpec(books.c.genre, FieldType.ENUM, frozenset({"FICTION", "SCIENCE", "HISTORY"})),
    "published": FieldSpec(books.c.published, FieldType.TIMESTAMP),
    "publisher.country": FieldSpec(books.c.publisher_country, FieldType.STRING),
    "rating": FieldSpec(books.c.rating, FieldType.INT),
}

ROWS = [
    (1, "100% Rust", 39.5, 320, True, "SCIENCE", dt.datetime(2024, 1, 1, 6), "US", 5),
    (2, "1000 Words", 12.0, 90, False, "FICTION", dt.datetime(2024, 1, 1, 3), "GB", None),
    (3, "rust in Action", 45.0, 456, True, "SCIENCE", dt.datetime(2023, 6, 15, 12), None, 4),
    (4, "History of_Rome", 25.0, 600, True, "HISTORY", dt.datetime(2022, 2, 2), "IT", 3),
    (5, "Histories", 18.0, 300, False, "HISTORY", dt.datetime(2021, 1, 1), "IT", None),
    (6, "Pragmatic Programmer", 49.99, 352, True, "SCIENCE", dt.datetime(2019, 9, 13), "US", 5),
]


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    metadata.create_all(eng)
    keys = [c.name for c in books.columns if c.name != "internal_cost"]
    rows = [dict(zip(keys, r, strict=True)) | {"internal_cost": 1.0} for r in ROWS]
    with eng.begin() as conn:
        conn.execute(insert(books), rows)
    yield eng
    eng.dispose()


def ids(engine: Engine, text: str) -> list[int]:
    stmt = select(books.c.id).order_by(books.c.id)
    predicate = compile_filter(parse_filter(text), SCHEMA)
    if predicate is not None:
        stmt = stmt.where(predicate)
    with engine.connect() as conn:
        return [row.id for row in conn.execute(stmt)]


def canon(text: str) -> str:
    node = parse_filter(text)
    assert node is not None
    return canonical(node)


@pytest.mark.unit
def test_or_binds_tighter_than_and(engine: Engine) -> None:
    text = "genre = HISTORY AND in_print = true OR price < 20"
    assert canon(text) == "genre = HISTORY AND (in_print = true OR price < 20)"
    # C-style precedence would give (HISTORY AND in_print) OR price < 20 -> [2, 4, 5].
    assert ids(engine, text) == [4, 5]


@pytest.mark.unit
def test_whitespace_sequence_is_an_implicit_and_looser_than_or(engine: Engine) -> None:
    assert canon("a = 1 b = 2 OR c = 3") == "a = 1 AND (b = 2 OR c = 3)"
    assert ids(engine, "genre = SCIENCE in_print = true price > 40") == [3, 6]


@pytest.mark.unit
def test_canonical_form_is_parenthesised_and_round_trips() -> None:
    text = 'title = "say \\"hi\\"*" price < 20 OR -rating = 5 AND rating:*'
    expected = 'title = "say \\"hi\\"*" AND (price < 20 OR NOT rating = 5) AND rating:*'
    assert canon(text) == expected
    assert canon(expected) == expected


@pytest.mark.unit
def test_injection_payload_is_a_bound_parameter(engine: Engine) -> None:
    payload = "x' OR '1'='1"
    predicate = compile_filter(parse_filter(f'title = "{payload}"'), SCHEMA)
    assert predicate is not None
    compiled = predicate.compile(engine)
    assert str(compiled) == "books.title = ?"
    assert list(compiled.params.values()) == [payload]
    assert ids(engine, f'title = "{payload}"') == []


@pytest.mark.unit
def test_fields_outside_the_allowlist_are_rejected(engine: Engine) -> None:
    for text in ("internal_cost > 0", "publisher.secret = 1", "books.id = 1"):
        with pytest.raises(FilterError, match="unknown field"):
            ids(engine, text)
    assert ids(engine, 'publisher.country = "IT"') == [4, 5]


@pytest.mark.unit
def test_depth_guard_rejects_deep_nesting_before_recursion_error() -> None:
    deep = "(" * 5000 + "pages = 1" + ")" * 5000
    with pytest.raises(FilterError, match="nesting depth exceeds 24"):
        parse_filter(deep, Limits(max_length=100_000))
    with pytest.raises(FilterError, match="nesting depth exceeds 24"):
        parse_filter("NOT " * 25 + "pages = 1")
    assert parse_filter("NOT " * 24 + "pages = 1") is not None
    # Depth is per nesting path, not cumulative: 30 sibling groups stay at depth 1.
    assert parse_filter(" ".join(["(pages = 1)"] * 30)) is not None
    assert parse_filter(" ".join(["NOT pages = 1"] * 30)) is not None


@pytest.mark.unit
def test_unbounded_recursive_descent_overflows_the_python_stack() -> None:
    deep = "(" * 5000 + "pages = 1" + ")" * 5000
    with pytest.raises(RecursionError):
        parse_filter(deep, Limits(max_length=100_000, max_depth=10**9))


@pytest.mark.unit
def test_length_and_restriction_budgets() -> None:
    with pytest.raises(FilterError, match="longer than 1024"):
        parse_filter("pages = 1 " + " " * 1015)
    many = " AND ".join(["pages = 1"] * 33)
    with pytest.raises(FilterError, match="more than 32 restrictions"):
        parse_filter(many, Limits(max_length=10_000))
    assert parse_filter(" AND ".join(["pages = 1"] * 32), Limits(max_length=10_000))
    # With the length cap raised, the 19-digit regex still rejects before int() runs.
    huge = parse_filter("pages > " + "9" * 5000, Limits(max_length=10_000))
    with pytest.raises(FilterError, match="not a valid int"):
        compile_filter(huge, SCHEMA)


@pytest.mark.unit
def test_wildcards_map_to_like_with_escaped_metacharacters(engine: Engine) -> None:
    assert ids(engine, 'title = "100%*"') == [1]  # unescaped '100%%' also matches 2
    assert ids(engine, 'title = "*_*"') == [4]  # unescaped '%_%' matches every row
    assert ids(engine, 'title != "Histor*"') == [1, 2, 3, 6]
    predicate = compile_filter(parse_filter('title = "100%*"'), SCHEMA)
    assert predicate is not None
    assert str(predicate.compile(engine)) == "books.title LIKE ? || '%' ESCAPE '/'"


@pytest.mark.unit
def test_sqlite_like_is_ascii_case_insensitive_but_equality_is_not(engine: Engine) -> None:
    assert ids(engine, 'title = "*rust*"') == [1, 3]
    assert ids(engine, 'title = "Rust in Action"') == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "pages = 1_000",
        "pages = \u0661\u0662",
        "pages > 99999999999999999999",
        "pages > 9223372036854775808",
        'pages = "12"',
        "price > nan",
        "price > inf",
        "price > 1e999",
        "in_print = True",
        "genre = fiction",
        "in_print > false",
        "genre < SCIENCE",
    ],
    ids=[
        "underscore",
        "arabic-indic-digits",
        "twenty-digits",
        "int64-max-plus-one",
        "quoted-int",
        "nan",
        "inf",
        "float-overflow",
        "bool-case",
        "enum-case",
        "bool-ordering",
        "enum-ordering",
    ],
)
def test_literals_are_validated_against_the_field_type(engine: Engine, text: str) -> None:
    with pytest.raises(FilterError):
        ids(engine, text)


@pytest.mark.unit
def test_valid_typed_literals(engine: Engine) -> None:
    assert ids(engine, "price < 2e1") == [2, 5]
    assert ids(engine, "pages > -1") == [1, 2, 3, 4, 5, 6]
    assert ids(engine, "genre = FICTION") == [2]
    assert ids(engine, "in_print = false") == [2, 5]
    assert ids(engine, "pages >= 600") == [4]
    assert ids(engine, "pages <= 90") == [2]


@pytest.mark.unit
def test_timestamp_offsets_are_normalised_to_utc(engine: Engine) -> None:
    # -05:00 midnight is 05:00 UTC: row 1 (06:00 UTC) matches, row 2 (03:00 UTC) does not.
    assert ids(engine, 'published >= "2024-01-01T00:00:00-05:00"') == [1]
    assert ids(engine, 'published < "2020-01-01T00:00:00Z"') == [6]
    with pytest.raises(FilterError, match="not a valid timestamp"):
        ids(engine, 'published >= "2024-01-01T00:00:00"')
    with pytest.raises(FilterError):
        ids(engine, "published >= 2024-01-01T00:00:00Z")


@pytest.mark.unit
def test_negation_and_not_equals_drop_null_rows(engine: Engine) -> None:
    assert ids(engine, "-rating = 5") == [3, 4]
    assert ids(engine, "NOT rating = 5") == [3, 4]
    assert ids(engine, "rating != 5") == [3, 4]
    assert ids(engine, "rating:*") == [1, 3, 4, 6]
    assert ids(engine, "NOT rating:*") == [2, 5]
    with pytest.raises(FilterError, match="only presence"):
        ids(engine, "rating:5")


@pytest.mark.unit
def test_functions_globals_and_bad_syntax_are_rejected() -> None:
    cases = {
        'regex(title, "x")': "functions are not supported",
        "rust": "needs a comparator",
        "pages = 1 AND": "expected TEXT",
        "(pages = 1": "expected \\)",
        'title = "open': "unterminated string",
        "pages ! 1": "unknown operator",
        "pages = 1)": "expected EOF",
        "pages = (1)": "right-hand side must be a literal",
        'title = -"x"': "right-hand side must be a literal",
        "pages = 1 and pages = 2": "global restriction 'and' needs a comparator",
    }
    for text, message in cases.items():
        with pytest.raises(FilterError, match=message):
            parse_filter(text)
    assert parse_filter("   ") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_endpoint_maps_filter_errors_to_400_invalid_argument(engine: Engine) -> None:
    app = build_books_app(engine, books, SCHEMA)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        ok = await client.get(
            "/books", params={"filter": "genre = HISTORY in_print = true OR rating = 5"}
        )
        everything = await client.get("/books")
        bad = await client.get("/books", params={"filter": "internal_cost > 0"})

    assert ok.status_code == 200
    assert ok.json() == {
        "filter": "genre = HISTORY AND (in_print = true OR rating = 5)",
        "ids": [4],
    }
    assert everything.json() == {"filter": "", "ids": [1, 2, 3, 4, 5, 6]}
    assert bad.status_code == 400
    assert bad.json() == {
        "error": {
            "code": 400,
            "message": "unknown field 'internal_cost' (at offset 0)",
            "status": "INVALID_ARGUMENT",
        }
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unencoded_plus_in_the_query_string_arrives_as_a_space(engine: Engine) -> None:
    app = build_books_app(engine, books, SCHEMA)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        raw = await client.get('/books?filter=title+=+"C++*"')
        encoded = await client.get("/books", params={"filter": 'title = "C++*"'})

    assert raw.request.url.query == b"filter=title+=+%22C++*%22"
    assert raw.json() == {"filter": 'title = "C  *"', "ids": []}
    assert encoded.request.url.query == b"filter=title+%3D+%22C%2B%2B%2A%22"
    assert encoded.json() == {"filter": 'title = "C++*"', "ids": []}
