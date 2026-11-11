"""Atomic all-or-nothing vs partial-success batch create, proven by SQL traces and row state."""

import sqlite3
import threading

import httpx
import pytest

from api_labs.f06_bulk_long_running_and_error_contracts.batch_execution import (
    MAX_BATCH_ITEMS,
    PROBLEM_BASE,
    BatchExecutor,
    BookCreate,
    build_app,
    open_catalog,
)


def book(n: int, shelf: str = "fiction", copies: int = 1) -> dict[str, object]:
    return {"isbn": f"978{n:010d}", "title": f"Book {n}", "shelf": shelf, "copies": copies}


def make_executor() -> BatchExecutor:
    return BatchExecutor(open_catalog({"fiction": 10, "tiny": 2}))


def trace(executor: BatchExecutor) -> list[str]:
    """Statements SQLite actually ran, with bound values dropped from the two item statements."""
    tags = {"INSERT INTO books": "INSERT books", "UPDATE shelves": "UPDATE shelves"}
    return [
        next((tag for prefix, tag in tags.items() if sql.startswith(prefix)), sql)
        for sql in executor.statements
    ]


def isbns(executor: BatchExecutor) -> list[str]:
    rows = executor.conn.execute("SELECT isbn FROM books ORDER BY isbn").fetchall()
    return [str(row[0]) for row in rows]


def used(executor: BatchExecutor, shelf: str) -> int:
    row = executor.conn.execute("SELECT used FROM shelves WHERE shelf = ?", (shelf,)).fetchone()
    return int(row[0])


async def post(
    executor: BatchExecutor, body: dict[str, object], path: str = "/v1/books:batchCreate"
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(executor)), base_url="http://test"
    ) as client:
        return await client.post(path, json=body)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atomic_batch_commits_every_item_in_one_transaction() -> None:
    executor = make_executor()

    response = await post(executor, {"mode": "atomic", "requests": [book(1), book(2)]})

    assert response.status_code == 200
    assert response.json() == {"books": [book(1), book(2)]}
    assert trace(executor) == [
        "BEGIN IMMEDIATE",
        "INSERT books",
        "UPDATE shelves",
        "INSERT books",
        "UPDATE shelves",
        "COMMIT",
    ]
    assert isbns(executor) == ["9780000000001", "9780000000002"]
    assert used(executor, "fiction") == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atomic_batch_rolls_back_earlier_items_when_a_later_item_fails() -> None:
    executor = make_executor()
    items = [book(1, "tiny"), book(2, "tiny"), book(3, "tiny")]

    response = await post(executor, {"mode": "atomic", "requests": items})

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": PROBLEM_BASE + "shelf-full",
        "title": "Shelf capacity exceeded.",
        "status": 409,
        "detail": "shelf tiny",
        "failed_indexes": [2],
    }
    assert trace(executor)[-3:] == ["INSERT books", "UPDATE shelves", "ROLLBACK"]
    assert isbns(executor) == []
    assert used(executor, "tiny") == 0
    assert not executor.conn.in_transaction


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atomic_batch_reports_every_invalid_item_before_any_sql_runs() -> None:
    executor = make_executor()
    items = [book(1), {**book(2), "isbn": "12"}, book(3), {**book(4), "price": 9}]

    response = await post(executor, {"requests": items})

    assert response.status_code == 422
    body = response.json()
    assert body["type"] == PROBLEM_BASE + "invalid-batch"
    assert body["failed_indexes"] == [1, 3]
    assert [err["pointer"] for err in body["errors"]] == ["#/requests/1/isbn", "#/requests/3/price"]
    assert trace(executor) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_partial_batch_returns_207_with_status_at_each_request_index() -> None:
    executor = make_executor()
    items = [
        book(1),
        book(1),
        book(2, copies=0),
        book(3, "poetry"),
        book(4, "tiny", copies=3),
        book(5, "tiny", copies=2),
    ]

    response = await post(executor, {"mode": "partial", "requests": items})

    assert response.status_code == 207
    body = response.json()
    assert [r["index"] for r in body["results"]] == [0, 1, 2, 3, 4, 5]
    assert [r["status"] for r in body["results"]] == [201, 409, 422, 422, 409, 201]
    problems = [r.get("problem", {}).get("type", "") for r in body["results"]]
    assert [p.removeprefix(PROBLEM_BASE) for p in problems] == [
        "",
        "already-exists",
        "invalid-item",
        "unknown-shelf",
        "shelf-full",
        "",
    ]
    assert body["results"][2]["problem"]["errors"][0]["pointer"] == "#/requests/2/copies"
    assert body["summary"] == {"succeeded": 2, "failed": 4}
    assert isbns(executor) == ["9780000000001", "9780000000005"]
    assert (used(executor, "fiction"), used(executor, "tiny")) == (1, 2)


@pytest.mark.unit
def test_per_item_savepoint_undoes_the_first_statement_when_the_second_fails() -> None:
    guarded, unguarded = make_executor(), make_executor()
    item: list[object] = [book(1, "tiny", copies=3)]

    guarded_results = guarded.create_partial(item)
    unguarded_results = unguarded.create_partial(item, per_item_savepoint=False)

    assert [r.status for r in guarded_results] == [409]
    assert trace(guarded) == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT item_0",
        "INSERT books",
        "UPDATE shelves",
        "ROLLBACK TO item_0",
        "RELEASE item_0",
        "COMMIT",
    ]
    assert isbns(guarded) == []
    # Same 409 to the client, but the committed book row has no shelf reservation behind it.
    assert [r.status for r in unguarded_results] == [409]
    assert trace(unguarded) == ["BEGIN IMMEDIATE", "INSERT books", "UPDATE shelves", "COMMIT"]
    assert isbns(unguarded) == ["9780000000001"]
    assert used(unguarded, "tiny") == 0


@pytest.mark.unit
def test_every_item_savepoint_is_released_whether_the_item_succeeds_or_fails() -> None:
    executor = make_executor()

    results = executor.create_partial([book(1), book(2, "tiny", copies=3)])

    assert [r.status for r in results] == [201, 409]
    assert trace(executor) == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT item_0",
        "INSERT books",
        "UPDATE shelves",
        "RELEASE item_0",
        "SAVEPOINT item_1",
        "INSERT books",
        "UPDATE shelves",
        "ROLLBACK TO item_1",
        "RELEASE item_1",
        "COMMIT",
    ]
    assert isbns(executor) == ["9780000000001"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_207_with_zero_successes_still_passes_httpx_raise_for_status() -> None:
    executor = make_executor()
    items = [book(1, "poetry"), {"isbn": "x"}]

    response = await post(executor, {"mode": "partial", "requests": items})

    assert response.status_code == 207
    assert response.is_success
    assert response.raise_for_status() is response
    assert response.json()["summary"] == {"succeeded": 0, "failed": 2}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_typed_item_list_turns_one_bad_item_into_a_whole_batch_422() -> None:
    typed, untyped = make_executor(), make_executor()
    items = [book(1), {**book(2), "isbn": "12"}]

    typed_response = await post(typed, {"requests": items}, "/v1/books:batchCreateTyped")
    untyped_response = await post(untyped, {"mode": "partial", "requests": items})

    assert typed_response.status_code == 422
    assert typed_response.json()["detail"][0]["loc"] == ["body", "requests", 1, "isbn"]
    assert trace(typed) == []
    assert untyped_response.status_code == 207
    assert isbns(untyped) == ["9780000000001"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resubmitting_a_whole_partial_batch_turns_earlier_successes_into_409() -> None:
    executor = make_executor()

    first = await post(executor, {"mode": "partial", "requests": [book(1), book(2, "tiny", 3)]})
    retry = await post(executor, {"mode": "partial", "requests": [book(1), book(2, "tiny", 2)]})

    assert [r["status"] for r in first.json()["results"]] == [201, 409]
    assert [r["status"] for r in retry.json()["results"]] == [409, 201]
    assert isbns(executor) == ["9780000000001", "9780000000002"]


@pytest.mark.unit
def test_unmapped_database_error_aborts_the_whole_partial_batch() -> None:
    executor = make_executor()
    executor.conn.execute(
        "CREATE TRIGGER audit_down BEFORE INSERT ON books WHEN NEW.title = 'Book 2' "
        "BEGIN SELECT RAISE(ABORT, 'audit log unavailable'); END"
    )
    executor.statements.clear()

    with pytest.raises(sqlite3.IntegrityError, match="audit log unavailable") as excinfo:
        executor.create_partial([book(1), book(2), book(3)])

    assert excinfo.value.sqlite_errorname == "SQLITE_CONSTRAINT_TRIGGER"
    assert trace(executor)[-1] == "ROLLBACK"
    assert isbns(executor) == []
    assert not executor.conn.in_transaction


@pytest.mark.unit
def test_concurrent_batches_on_one_connection_never_share_a_transaction() -> None:
    first_inside, second_done = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    class PausingExecutor(BatchExecutor):
        def _apply(self, item: BookCreate) -> None:
            if not first_inside.is_set():
                # Pause the first batch between statements (no SQLite call in flight) inside
                # its transaction; without the lock the second batch runs and fails meanwhile.
                first_inside.set()
                second_done.wait(timeout=0.5)
            super()._apply(item)

    executor = PausingExecutor(open_catalog({"fiction": 10}))

    def run(raw: list[object], done: threading.Event | None = None) -> None:
        try:
            executor.create_partial(raw)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if done is not None:
                done.set()

    first = threading.Thread(target=run, args=([book(1)],), daemon=True)
    second = threading.Thread(target=run, args=([book(2)], second_done), daemon=True)
    first.start()
    assert first_inside.wait(timeout=5)
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    one_batch = ["BEGIN IMMEDIATE", "SAVEPOINT item_0", "INSERT books", "UPDATE shelves"]
    assert trace(executor) == [*one_batch, "RELEASE item_0", "COMMIT"] * 2
    assert isbns(executor) == ["9780000000001", "9780000000002"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_size_limit_is_a_request_level_error_not_an_item_status() -> None:
    executor = make_executor()
    items = [book(n) for n in range(MAX_BATCH_ITEMS + 1)]

    response = await post(executor, {"mode": "partial", "requests": items})

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "too_long"
    assert trace(executor) == []
