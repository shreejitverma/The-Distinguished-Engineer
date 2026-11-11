"""AIP-231 to AIP-235 batch methods: atomicity, order, size bounds, verbs, and URL limits."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

import httptools  # pyright: ignore[reportMissingTypeStubs]
import httpx
import pytest
import pytest_asyncio

from api_labs.f06_bulk_long_running_and_error_contracts.batch_methods import (
    MAX_BATCH_SIZE,
    Book,
    BookStore,
    align_to_request,
    batch_get_target,
    build_app,
    h11_server_parse,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

BASE = "/v1/publishers/acme/books"


@pytest.fixture
def store() -> BookStore:
    return BookStore.in_memory()


@pytest_asyncio.fixture
async def client(store: BookStore) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def seed(store: BookStore, *names: str) -> None:
    store.conn.executemany(
        "INSERT INTO books (name, title, author) VALUES (?, ?, 'seed')",
        [(name, name.rsplit("/", 1)[1]) for name in names],
    )
    store.statements.clear()


def row_names(store: BookStore) -> list[str]:
    return [row[0] for row in store.conn.execute("SELECT name FROM books ORDER BY name")]


def error_of(response: httpx.Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


def violation_field(response: httpx.Response) -> str:
    field: str = error_of(response)["details"][0]["fieldViolations"][0]["field"]
    return field


def create_body(*book_ids: str) -> dict[str, Any]:
    return {"requests": [{"bookId": b, "book": {"title": b.upper()}} for b in book_ids]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_get_returns_request_order_not_primary_key_order(
    store: BookStore, client: httpx.AsyncClient
) -> None:
    seed(store, "publishers/acme/books/a", "publishers/acme/books/b", "publishers/acme/books/c")
    names = [f"publishers/acme/books/{b}" for b in ("c", "a", "b", "a")]

    response = await client.get(f"{BASE}:batchGet", params={"names": names})

    assert response.status_code == 200
    assert [book["name"] for book in response.json()["books"]] == names
    assert [s for s in store.statements if s.startswith("SELECT")] == [
        "SELECT name, title, author FROM books WHERE name IN "
        "('publishers/acme/books/c', 'publishers/acme/books/a', 'publishers/acme/books/b')"
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_get_is_atomic_one_missing_name_fails_the_whole_call(
    store: BookStore, client: httpx.AsyncClient
) -> None:
    seed(store, "publishers/acme/books/a", "publishers/acme/books/c")
    names = ["publishers/acme/books/a", "publishers/acme/books/b", "publishers/acme/books/c"]

    response = await client.get(f"{BASE}:batchGet", params={"names": names})

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": 404,
            "message": "names[1]: publishers/acme/books/b not found",
            "status": "NOT_FOUND",
        }
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_get_parent_must_match_unless_dash_wildcard(
    store: BookStore, client: httpx.AsyncClient
) -> None:
    seed(store, "publishers/acme/books/a", "publishers/zeta/books/z")
    names = ["publishers/acme/books/a", "publishers/zeta/books/z"]

    scoped = await client.get(f"{BASE}:batchGet", params={"names": names})
    wildcard = await client.get("/v1/publishers/-/books:batchGet", params={"names": names})
    empty = await client.get(f"{BASE}:batchGet")

    assert scoped.status_code == 400
    assert error_of(scoped)["status"] == "INVALID_ARGUMENT"
    assert violation_field(scoped) == "names[1]"
    assert wildcard.status_code == 200
    assert [book["name"] for book in wildcard.json()["books"]] == names
    assert empty.status_code == 400
    assert violation_field(empty) == "names"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_create_is_one_insert_statement_in_request_order(
    store: BookStore, client: httpx.AsyncClient
) -> None:
    response = await client.post(f"{BASE}:batchCreate", json=create_body("m", "z", "a"))

    assert response.status_code == 200
    assert [book["name"] for book in response.json()["books"]] == [
        "publishers/acme/books/m",
        "publishers/acme/books/z",
        "publishers/acme/books/a",
    ]
    assert [s.split(" ", 1)[0] for s in store.statements] == ["BEGIN", "INSERT", "COMMIT"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_create_accepts_1000_and_rejects_1001_before_any_sql(
    store: BookStore, client: httpx.AsyncClient
) -> None:
    ids = [f"b{i:04d}" for i in range(MAX_BATCH_SIZE + 1)]

    rejected = await client.post(f"{BASE}:batchCreate", json=create_body(*ids))
    assert rejected.status_code == 400
    assert error_of(rejected)["message"] == "requests: has 1001 entries; the maximum is 1000"
    assert store.statements == []

    accepted = await client.post(f"{BASE}:batchCreate", json=create_body(*ids[:MAX_BATCH_SIZE]))
    assert accepted.status_code == 200
    assert len(accepted.json()["books"]) == MAX_BATCH_SIZE
    assert len(row_names(store)) == MAX_BATCH_SIZE
    assert sum(s.startswith("INSERT") for s in store.statements) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_create_conflict_rolls_back_every_row_and_names_the_index(
    store: BookStore, client: httpx.AsyncClient
) -> None:
    seed(store, "publishers/acme/books/c")

    response = await client.post(f"{BASE}:batchCreate", json=create_body("a", "b", "c", "d"))

    assert response.status_code == 409
    assert error_of(response) == {
        "code": 409,
        "message": "requests[2]: publishers/acme/books/c already exists",
        "status": "ALREADY_EXISTS",
    }
    assert row_names(store) == ["publishers/acme/books/c"]
    assert "ROLLBACK" in store.statements


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_create_rejects_mismatched_child_parent_and_duplicate_ids(
    store: BookStore, client: httpx.AsyncClient
) -> None:
    body = create_body("a", "b")
    body["requests"][1]["parent"] = "publishers/zeta"

    mismatch = await client.post(f"{BASE}:batchCreate", json=body)
    duplicate = await client.post(f"{BASE}:batchCreate", json=create_body("a", "b", "a"))
    no_parent = await client.post("/v1/publishers/-/books:batchCreate", json=create_body("a"))

    assert violation_field(mismatch) == "requests[1].parent"
    assert violation_field(duplicate) == "requests[2].bookId"
    assert error_of(duplicate)["message"] == (
        "requests[2].bookId: publishers/acme/books/a duplicates requests[0].bookId"
    )
    assert violation_field(no_parent) == "requests[0].parent"
    assert row_names(store) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pydantic_validation_error_becomes_400_invalid_argument_not_422(
    client: httpx.AsyncClient,
) -> None:
    missing_book_id: dict[str, Any] = {"requests": [{"book": {"title": "T"}}]}

    response = await client.post(f"{BASE}:batchCreate", json=missing_book_id)

    assert response.status_code == 400
    assert error_of(response)["status"] == "INVALID_ARGUMENT"
    assert violation_field(response) == "requests[0].bookId"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_update_is_atomic_a_missing_second_item_leaves_the_first_unchanged(
    store: BookStore, client: httpx.AsyncClient
) -> None:
    seed(store, "publishers/acme/books/a")
    body = {
        "updateMask": "title",
        "requests": [
            {"book": {"name": "publishers/acme/books/a", "title": "NEW"}},
            {"book": {"name": "publishers/acme/books/missing", "title": "NEW"}},
        ],
    }

    response = await client.post(f"{BASE}:batchUpdate", json=body)

    assert response.status_code == 404
    assert error_of(response)["message"] == (
        "requests[1].book.name: publishers/acme/books/missing not found"
    )
    title = store.conn.execute("SELECT title FROM books").fetchone()[0]
    assert title == "a"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_update_hoisted_mask_limits_columns_and_must_agree_with_child(
    store: BookStore, client: httpx.AsyncClient
) -> None:
    seed(store, "publishers/acme/books/a", "publishers/acme/books/b")
    book_a = {"name": "publishers/acme/books/a", "title": "T", "author": "ignored"}
    book_b = {"name": "publishers/acme/books/b", "author": "B2"}
    author_a = {"name": "publishers/acme/books/a", "author": "A2"}

    hoisted = await client.post(
        f"{BASE}:batchUpdate",
        json={"updateMask": "title", "requests": [{"book": book_a}]},
    )
    implied = await client.post(
        f"{BASE}:batchUpdate", json={"requests": [{"book": book_b}, {"book": author_a}]}
    )
    conflict = await client.post(
        f"{BASE}:batchUpdate",
        json={"updateMask": "title", "requests": [{"book": book_a, "updateMask": "author"}]},
    )

    assert hoisted.json()["books"] == [
        {"name": "publishers/acme/books/a", "title": "T", "author": "seed"}
    ]
    assert implied.json()["books"] == [
        {"name": "publishers/acme/books/b", "title": "b", "author": "B2"},
        {"name": "publishers/acme/books/a", "title": "T", "author": "A2"},
    ]
    assert violation_field(conflict) == "requests[0].updateMask"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_update_rejects_the_same_name_twice_and_changes_nothing(
    store: BookStore, client: httpx.AsyncClient
) -> None:
    seed(store, "publishers/acme/books/a")
    book = {"name": "publishers/acme/books/a", "title": "NEW"}

    response = await client.post(
        f"{BASE}:batchUpdate",
        json={"updateMask": "title", "requests": [{"book": book}, {"book": book}]},
    )

    assert response.status_code == 400
    assert violation_field(response) == "requests[1].book.name"
    assert store.conn.execute("SELECT title FROM books").fetchone()[0] == "a"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_delete_is_post_and_the_delete_verb_gets_405_allow_post(
    store: BookStore, client: httpx.AsyncClient
) -> None:
    seed(store, "publishers/acme/books/a", "publishers/acme/books/b")
    names = ["publishers/acme/books/a", "publishers/acme/books/b"]

    wrong_verb = await client.request("DELETE", f"{BASE}:batchDelete", json={"names": names})
    deleted = await client.post(f"{BASE}:batchDelete", json={"names": names})

    assert wrong_verb.status_code == 405
    assert wrong_verb.headers["allow"] == "POST"
    assert deleted.status_code == 200
    assert deleted.json() == {}
    assert row_names(store) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_delete_is_atomic_a_missing_name_restores_the_deleted_rows(
    store: BookStore, client: httpx.AsyncClient
) -> None:
    seed(store, "publishers/acme/books/a", "publishers/acme/books/c")
    names = ["publishers/acme/books/a", "publishers/acme/books/b", "publishers/acme/books/c"]

    response = await client.post(f"{BASE}:batchDelete", json={"names": names})

    assert response.status_code == 404
    assert error_of(response)["message"] == "names[1]: publishers/acme/books/b not found"
    assert row_names(store) == ["publishers/acme/books/a", "publishers/acme/books/c"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_delete_rejects_a_duplicate_name_and_deletes_nothing(
    store: BookStore, client: httpx.AsyncClient
) -> None:
    seed(store, "publishers/acme/books/a", "publishers/acme/books/b")
    names = ["publishers/acme/books/a", "publishers/acme/books/b", "publishers/acme/books/a"]

    response = await client.post(f"{BASE}:batchDelete", json={"names": names})

    assert response.status_code == 400
    assert violation_field(response) == "names[2]"
    assert row_names(store) == ["publishers/acme/books/a", "publishers/acme/books/b"]


@pytest.mark.unit
def test_transaction_takes_the_write_lock_at_begin_immediate(tmp_path: Path) -> None:
    database = tmp_path / "books.db"
    store = BookStore(sqlite3.connect(database, autocommit=True, timeout=0))
    other = sqlite3.connect(database, autocommit=True, timeout=0)
    try:
        with store.transaction(), pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
    finally:
        other.close()
        store.conn.close()


@pytest.mark.unit
def test_align_to_request_reorders_arbitrary_returning_order() -> None:
    names = ["publishers/acme/books/x", "publishers/acme/books/y", "publishers/acme/books/z"]
    shuffled = [Book(name=names[2]), Book(name=names[0]), Book(name=names[1])]

    assert [book.name for book in align_to_request(names, shuffled)] == names


def _httptools_url_length(request: bytes) -> int:
    class Sink:
        def __init__(self) -> None:
            self.url = b""
            self.complete = False

        def on_url(self, url: bytes) -> None:
            self.url += url

        def on_message_complete(self) -> None:
            self.complete = True

    sink = Sink()
    parser: Any = httptools.HttpRequestParser(sink)
    for start in range(0, len(request), 4096):
        parser.feed_data(request[start : start + 4096])
    assert sink.complete
    return len(sink.url)


@pytest.mark.unit
def test_1000_name_batch_get_url_fails_h11_only_when_fragmented_and_passes_httptools() -> None:
    names = [f"publishers/acme/books/book-{i:04d}" for i in range(MAX_BATCH_SIZE)]
    target = batch_get_target("acme", names)
    request = b"GET " + target + b" HTTP/1.1\r\nHost: test\r\n\r\n"
    h11_default = 16 * 1024

    assert len(target) == 44034
    assert h11_server_parse(request, chunk_size=4096, max_incomplete_event_size=h11_default) == 431
    assert h11_server_parse(request, chunk_size=65536, max_incomplete_event_size=h11_default) == 0
    assert h11_server_parse(request, chunk_size=4096, max_incomplete_event_size=65536) == 0
    assert _httptools_url_length(request) == len(target)


@pytest.mark.unit
def test_names_that_fit_common_request_line_budgets() -> None:
    names = [f"publishers/acme/books/book-{i:04d}" for i in range(MAX_BATCH_SIZE)]

    def fits(budget: int, extra: int) -> int:
        sizes = [len(batch_get_target("acme", names[:n])) + extra for n in range(1, 1001)]
        return sum(size <= budget for size in sizes)

    head_extra = len(b"GET ") + len(b" HTTP/1.1\r\nHost: test\r\n\r\n")
    line_extra = len(b"GET ") + len(b" HTTP/1.1\r\n")
    assert fits(16 * 1024, head_extra) == 370
    assert fits(8192, line_extra) == 185
    assert fits(8000, 0) == 181
