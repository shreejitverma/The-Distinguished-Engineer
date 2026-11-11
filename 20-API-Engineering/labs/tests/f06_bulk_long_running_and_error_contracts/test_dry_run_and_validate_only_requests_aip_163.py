"""Lab: AIP-163 validate_only executed inside a SAVEPOINT and rolled back."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlalchemy import event, func, insert, select, text

from api_labs.f06_bulk_long_running_and_error_contracts.validate_only_savepoint import (
    AuditEntry,
    Base,
    Book,
    Shelf,
    TransactionControl,
    create_app,
    make_engine,
    to_http_error,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.orm import DeclarativeBase

WRITER = {"Authorization": "Bearer writer-token"}
READER = {"Authorization": "Bearer reader-token"}
BOOKS = "/v1/shelves/1/books"


@asynccontextmanager
async def lab(
    *, check_deferred_constraints: bool = True
) -> AsyncGenerator[tuple[httpx.AsyncClient, AsyncEngine]]:
    engine = make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(insert(Shelf).values(id=1, theme="systems"))
    app = create_app(engine, check_deferred_constraints=check_deferred_constraints)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, engine
    finally:
        await engine.dispose()


async def count(engine: AsyncEngine, model: type[DeclarativeBase]) -> int:
    async with engine.connect() as conn:
        total = (await conn.execute(select(func.count()).select_from(model))).scalar_one()
    return int(total)


async def autoincrement_high_water_marks(engine: AsyncEngine) -> list[int]:
    """`sqlite_sequence` rows for `books`; the table exists only if a table is AUTOINCREMENT."""
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT seq FROM sqlite_sequence WHERE name = 'books'"))
        return [int(seq) for (seq,) in rows]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_live_create_persists_and_returns_generated_name() -> None:
    async with lab() as (client, engine):
        response = await client.post(BOOKS, json={"title": "SICP"}, headers=WRITER)
        assert response.status_code == 201
        expected = {"name": "shelves/1/books/1", "shelf": "shelves/1", "title": "SICP"}
        assert response.json() == expected
        assert response.headers["Location"] == "/v1/shelves/1/books/1"
        assert await count(engine, Book) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_only_returns_live_response_minus_generated_name_and_writes_nothing() -> (
    None
):
    async with lab() as (client, engine):
        response = await client.post(
            BOOKS, params={"validate_only": "true"}, json={"title": "SICP"}, headers=WRITER
        )
        assert response.status_code == 201
        assert response.json() == {"shelf": "shelves/1", "title": "SICP"}
        assert "Location" not in response.headers
        assert await count(engine, Book) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_only_evaluates_unique_constraint_like_live_request() -> None:
    async with lab() as (client, engine):
        first = await client.post(BOOKS, json={"title": "SICP"}, headers=WRITER)
        assert first.status_code == 201
        dry = await client.post(
            BOOKS, params={"validate_only": "true"}, json={"title": "SICP"}, headers=WRITER
        )
        live = await client.post(BOOKS, json={"title": "SICP"}, headers=WRITER)
        assert (dry.status_code, dry.json()) == (live.status_code, live.json())
        assert dry.status_code == 409
        assert await count(engine, Book) == 1
        assert await count(engine, AuditEntry) == 3  # the two failed attempts are audited too


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_only_enforces_authorization_like_live_request() -> None:
    async with lab() as (client, engine):
        dry = await client.post(
            BOOKS, params={"validate_only": "true"}, json={"title": "SICP"}, headers=READER
        )
        live = await client.post(BOOKS, json={"title": "SICP"}, headers=READER)
        assert dry.status_code == live.status_code == 403
        assert dry.json() == live.json()
        assert await count(engine, AuditEntry) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_only_requires_authentication_like_live_request() -> None:
    async with lab() as (client, _engine):
        dry = await client.post(BOOKS, params={"validate_only": "true"}, json={"title": "SICP"})
        live = await client.post(BOOKS, json={"title": "SICP"})
        assert dry.status_code == live.status_code == 401
        assert dry.headers["WWW-Authenticate"] == live.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.unit
def test_unclassified_database_error_is_reraised_not_mapped() -> None:
    with pytest.raises(ValueError, match="CHECK constraint failed"):
        to_http_error(ValueError("CHECK constraint failed: books"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_only_runs_schema_validation_like_live_request() -> None:
    async with lab() as (client, _engine):
        dry = await client.post(
            BOOKS, params={"validate_only": "true"}, json={"title": ""}, headers=WRITER
        )
        live = await client.post(BOOKS, json={"title": ""}, headers=WRITER)
        assert dry.status_code == live.status_code == 422
        dry_loc, live_loc = dry.json()["detail"][0]["loc"], live.json()["detail"][0]["loc"]
        assert dry_loc == live_loc == ["body", "title"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body", "loc", "error_type"),
    [
        (BOOKS, {"title": "SICP", "isbn": "0262510871"}, ["body", "isbn"], "extra_forbidden"),
        ("/v1/shelves/0/books", {"title": "SICP"}, ["path", "shelf_id"], "greater_than_equal"),
    ],
    ids=["unknown-body-field", "shelf-id-below-one"],
)
async def test_validate_only_rejects_out_of_contract_input_like_live_request(
    path: str, body: dict[str, str], loc: list[str], error_type: str
) -> None:
    async with lab() as (client, engine):
        dry = await client.post(path, params={"validate_only": "true"}, json=body, headers=WRITER)
        live = await client.post(path, json=body, headers=WRITER)
        assert dry.status_code == live.status_code == 422
        for response in (dry, live):
            detail = response.json()["detail"][0]
            assert (detail["loc"], detail["type"]) == (loc, error_type)
        assert await count(engine, Book) == 0
        assert await count(engine, AuditEntry) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unparseable_validate_only_is_rejected_instead_of_defaulting_to_live() -> None:
    async with lab() as (client, engine):
        response = await client.post(
            BOOKS, params={"validate_only": "maybe"}, json={"title": "SICP"}, headers=WRITER
        )
        assert response.status_code == 422
        detail = response.json()["detail"][0]
        assert (detail["loc"], detail["type"]) == (["query", "validate_only"], "bool_parsing")
        assert await count(engine, Book) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_only_reports_deferred_foreign_key_violation_like_live_commit() -> None:
    async with lab() as (client, engine):
        missing = "/v1/shelves/99/books"
        dry = await client.post(
            missing, params={"validate_only": "true"}, json={"title": "SICP"}, headers=WRITER
        )
        live = await client.post(missing, json={"title": "SICP"}, headers=WRITER)
        assert dry.status_code == live.status_code == 404
        assert dry.json() == live.json()
        assert await count(engine, Book) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skipping_deferred_check_makes_dry_run_approve_a_failing_live_request() -> None:
    async with lab(check_deferred_constraints=False) as (client, engine):
        missing = "/v1/shelves/99/books"
        dry = await client.post(
            missing, params={"validate_only": "true"}, json={"title": "SICP"}, headers=WRITER
        )
        live = await client.post(missing, json={"title": "SICP"}, headers=WRITER)
        assert dry.status_code == 201  # the dry run lies: flush never checks a deferred FK
        assert live.status_code == 404  # COMMIT does
        assert await count(engine, Book) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_outer_transaction_work_survives_savepoint_rollback() -> None:
    async with lab() as (client, engine):
        response = await client.post(
            BOOKS, params={"validate_only": "true"}, json={"title": "SICP"}, headers=WRITER
        )
        assert response.status_code == 201
        async with engine.connect() as conn:
            audit = select(AuditEntry.principal, AuditEntry.validate_only)
            rows = (await conn.execute(audit)).all()
        assert [tuple(row) for row in rows] == [("alice", True)]
        assert await count(engine, Book) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_does_not_consume_the_request_id() -> None:
    async with lab() as (client, engine):
        params = {"validate_only": "true", "request_id": "r-1"}
        dry = await client.post(BOOKS, params=params, json={"title": "SICP"}, headers=WRITER)
        live = await client.post(
            BOOKS, params={"request_id": "r-1"}, json={"title": "SICP"}, headers=WRITER
        )
        replay = await client.post(
            BOOKS, params={"request_id": "r-1"}, json={"title": "SICP"}, headers=WRITER
        )
        assert "name" not in dry.json()
        assert live.json()["name"] == "shelves/1/books/1"
        assert replay.json() == live.json()
        assert await count(engine, Book) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_sql_trace_ends_in_rollback_to_savepoint() -> None:
    async with lab() as (client, engine):
        statements: list[str] = []

        def record(_conn: object, _cur: object, stmt: str, *_rest: object) -> None:
            statements.append(stmt.split("(")[0].split(" VALUES")[0].strip())

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        response = await client.post(
            BOOKS, params={"validate_only": "true"}, json={"title": "SICP"}, headers=WRITER
        )
        assert response.status_code == 201
        assert statements == [
            "BEGIN",
            "INSERT INTO audit_log",
            "SAVEPOINT sa_savepoint_1",
            "INSERT INTO books",
            "PRAGMA foreign_key_check",
            "ROLLBACK TO SAVEPOINT sa_savepoint_1",
        ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sqlite_autoincrement_value_is_not_consumed_by_dry_run() -> None:
    async with lab() as (client, engine):
        for _ in range(3):
            dry = await client.post(
                BOOKS, params={"validate_only": "true"}, json={"title": "SICP"}, headers=WRITER
            )
            assert dry.status_code == 201
        # The rolled-back inserts left no high-water mark behind.
        assert await autoincrement_high_water_marks(engine) == []
        live = await client.post(BOOKS, json={"title": "SICP"}, headers=WRITER)
        assert live.json()["name"] == "shelves/1/books/1"
        assert await autoincrement_high_water_marks(engine) == [1]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_misspelled_validate_only_is_rejected_instead_of_executed() -> None:
    async with lab() as (client, engine):
        response = await client.post(
            BOOKS, params={"validateOnly": "true"}, json={"title": "SICP"}, headers=WRITER
        )
        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "extra_forbidden"
        assert await count(engine, Book) == 0


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "foreign_keys", "rows_after_outer_rollback"),
    [("explicit-begin", 1, 0), ("autocommit-false", 0, 0), ("legacy", 1, 1)],
    ids=["explicit-begin", "autocommit-false", "legacy"],
)
async def test_sqlite3_transaction_control_modes(
    mode: TransactionControl, foreign_keys: int, rows_after_outer_rollback: int
) -> None:
    engine = make_engine(transaction_control=mode)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE t (x INTEGER)")
        async with engine.connect() as conn:
            pragma = (await conn.execute(text("PRAGMA foreign_keys"))).scalar_one()
            await conn.rollback()  # end the autobegun transaction before an explicit begin()
            outer = await conn.begin()
            inner = await conn.begin_nested()  # the first statement of the transaction
            await conn.exec_driver_sql("INSERT INTO t VALUES (1)")
            await inner.commit()  # RELEASE SAVEPOINT
            await outer.rollback()  # must undo the released savepoint
            rows = (await conn.exec_driver_sql("SELECT count(*) FROM t")).scalar_one()
        assert (pragma, rows) == (foreign_keys, rows_after_outer_rollback)
    finally:
        await engine.dispose()
