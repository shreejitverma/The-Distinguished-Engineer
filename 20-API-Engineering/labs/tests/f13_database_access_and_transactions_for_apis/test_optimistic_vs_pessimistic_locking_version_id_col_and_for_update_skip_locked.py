"""Lab: version_id_col / StaleDataError vs SELECT ... FOR UPDATE NOWAIT / SKIP LOCKED."""

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select, update
from sqlalchemy.dialects.mysql.base import MySQLDialect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm.exc import StaleDataError

from api_labs.f13_database_access_and_transactions_for_apis.row_locking import (
    MAX_RESERVE_ATTEMPTS,
    Base,
    Item,
    Ticket,
    build_app,
    claimable,
    dialect_for,
    guarded_claim,
    mark_claimed,
    render,
    select_claimable,
)

type Sessions = async_sessionmaker[AsyncSession]


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    # A file database, so each session gets its own connection, as in production.
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lab.db'}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sessions(engine: AsyncEngine) -> Sessions:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Item(id=1, name="widget", quantity=10))
        session.add_all([Ticket(id=i) for i in range(1, 5)])
        await session.commit()
    return factory


def capture_sql(engine: AsyncEngine) -> list[tuple[str, Any]]:
    statements: list[tuple[str, Any]] = []

    def record(conn: Any, cursor: Any, sql: str, params: Any, context: Any, many: bool) -> None:
        statements.append((sql, params))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    return statements


async def bump_quantity(sessions: Sessions, quantity: int) -> None:
    """A competing writer that commits through its own connection."""
    async with sessions() as other:
        item = await other.get(Item, 1)
        assert item is not None
        item.quantity = quantity
        await other.commit()


# --- version_id_col mechanics -----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_insert_sets_version_1_and_update_guards_and_bumps_it(
    engine: AsyncEngine, sessions: Sessions
) -> None:
    statements = capture_sql(engine)
    async with sessions() as session:
        item = await session.get(Item, 1)
        assert item is not None
        assert item.version == 1
        item.quantity = 9
        await session.commit()
        assert item.version == 2
    assert statements[-1] == (
        "UPDATE items SET quantity=?, version=? WHERE items.id = ? AND items.version = ?",
        (9, 2, 1, 1),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_flush_matches_zero_rows_and_raises_stale_data_error(
    sessions: Sessions,
) -> None:
    async with sessions() as mine:
        item = await mine.get(Item, 1)
        assert item is not None
        await bump_quantity(sessions, 9)  # commits version 2 underneath us
        item.quantity = 8
        with pytest.raises(StaleDataError, match=r"expected to update 1 row\(s\); 0 were matched"):
            await mine.commit()
    async with sessions() as check:
        row = await check.get(Item, 1)
        assert row is not None
        assert (row.quantity, row.version) == (9, 2)  # the loser wrote nothing


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_delete_raises_stale_data_error(sessions: Sessions) -> None:
    async with sessions() as mine:
        item = await mine.get(Item, 1)
        assert item is not None
        await bump_quantity(sessions, 9)
        await mine.delete(item)
        with pytest.raises(StaleDataError, match=r"expected to delete 1 row\(s\); 0 were matched"):
            await mine.commit()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_with_for_update_validates_the_identity_map_version(sessions: Sessions) -> None:
    async with sessions() as mine:
        # Hold a strong reference: the identity map is weak, and a collected object would
        # simply be loaded fresh, with nothing to compare.
        item = await mine.get(Item, 1)
        assert item is not None
        await bump_quantity(sessions, 9)
        # Plain get() answers from the identity map without SQL, so it returns version 1;
        # with_for_update forces a SELECT and compares the row's version with the object's.
        assert await mine.get(Item, 1) is item
        assert item.version == 1
        with pytest.raises(StaleDataError, match="has version id '1' which does not match"):
            await mine.get(Item, 1, with_for_update=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orm_bulk_update_skips_versioning_and_reopens_the_lost_update(
    sessions: Sessions,
) -> None:
    async with sessions() as mine:
        item = await mine.get(Item, 1)
        assert item is not None
        async with sessions() as bulk:
            await bulk.execute(update(Item).where(Item.id == 1).values(quantity=0))
            await bulk.commit()
        item.quantity = 5  # decided from the stale quantity 10
        await mine.commit()  # no StaleDataError: the bulk UPDATE left version at 1
    async with sessions() as check:
        row = await check.get(Item, 1)
        assert row is not None
        assert (row.quantity, row.version) == (5, 2)  # the bulk write to 0 is lost


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reselect_keeps_stale_attributes_unless_populate_existing(sessions: Sessions) -> None:
    async with sessions() as mine:
        ticket = (await mine.scalars(select(Ticket).where(Ticket.id == 1))).one()
        async with sessions() as other:
            await other.execute(update(Ticket).where(Ticket.id == 1).values(status="claimed"))
            await other.commit()
        relocked = (
            await mine.scalars(select(Ticket).where(Ticket.id == 1).with_for_update())
        ).one()
        assert relocked is ticket
        assert relocked.status == "open"  # the row was re-read, the object was not refreshed
        fresh = await mine.scalars(
            select(Ticket).where(Ticket.id == 1).execution_options(populate_existing=True)
        )
        assert fresh.one().status == "claimed"


# --- the locking clause per dialect -----------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("options", "clause"),
    [
        ({}, "FOR UPDATE"),
        ({"nowait": True}, "FOR UPDATE NOWAIT"),
        ({"skip_locked": True}, "FOR UPDATE SKIP LOCKED"),
        ({"key_share": True}, "FOR NO KEY UPDATE"),
        ({"read": True}, "FOR SHARE"),
        ({"read": True, "key_share": True}, "FOR KEY SHARE"),
        ({"of": Ticket, "skip_locked": True}, "FOR UPDATE OF tickets SKIP LOCKED"),
        # SQLAlchemy does not validate the pair; PostgreSQL rejects it (SQLSTATE 42601).
        ({"nowait": True, "skip_locked": True}, "FOR UPDATE NOWAIT SKIP LOCKED"),
    ],
)
def test_postgresql_lock_strengths(options: dict[str, Any], clause: str) -> None:
    sql = render(select(Ticket.id).with_for_update(**options), dialect_for("postgresql"))
    head, _, tail = sql.partition(" FROM tickets ")
    assert (head, tail) == ("SELECT tickets.id", clause)


@pytest.mark.unit
def test_claim_query_renders_skip_locked_after_limit_on_postgresql() -> None:
    sql = render(claimable(2), dialect_for("postgresql"))
    # 2.1 maps postgresql:// to psycopg (typed casts); 2.0 maps it to psycopg2 (none).
    assert re.search(
        r"ORDER BY tickets\.id LIMIT %\(param_1\)s(::INTEGER)? FOR UPDATE SKIP LOCKED$", sql
    )


@pytest.mark.unit
def test_mysql_share_and_of_syntax_depend_on_the_detected_server_version() -> None:
    stmt = select(Ticket.id).with_for_update(read=True, of=Ticket, skip_locked=True)
    unconnected = dialect_for("mysql")  # pre-8.0 flags until a connection initializes
    assert (
        render(stmt, unconnected) == "SELECT tickets.id FROM tickets LOCK IN SHARE MODE SKIP LOCKED"
    )
    mysql8 = dialect_for("mysql")
    assert isinstance(mysql8, MySQLDialect)
    mysql8.use_mysql_for_share = True  # set when server_version_info >= (8, 0, 1)
    mysql8.supports_for_update_of = True  # set when server_version_info >= (8,)
    assert render(stmt, mysql8) == "SELECT tickets.id FROM tickets FOR SHARE OF tickets SKIP LOCKED"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        ("sqlite", "SELECT tickets.id FROM tickets"),
        ("mssql", "SELECT tickets.id FROM tickets"),
        ("oracle", "SELECT tickets.id FROM tickets FOR UPDATE NOWAIT"),
    ],
)
def test_sqlite_and_mssql_compile_the_lock_away_silently(backend: str, expected: str) -> None:
    assert render(select(Ticket.id).with_for_update(nowait=True), dialect_for(backend)) == expected


# --- queue claims: SKIP LOCKED vs a guarded UPDATE --------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skip_locked_claim_double_claims_where_the_lock_is_compiled_away(
    sessions: Sessions,
) -> None:
    async with sessions() as w1, sessions() as w2:
        first = await select_claimable(w1, 2)
        second = await select_claimable(w2, 2)  # PostgreSQL would skip to tickets 3 and 4
        assert [t.id for t in first] == [t.id for t in second] == [1, 2]
        await mark_claimed(w1, first, "w1")
        await w1.commit()
        await mark_claimed(w2, second, "w2")
        await w2.commit()  # silently overwrites w1's claim
    async with sessions() as check:
        owners = {t.id: t.claimed_by for t in (await check.scalars(select(Ticket))).all()}
    assert owners == {1: "w2", 2: "w2", 3: None, 4: None}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_guarded_update_claim_stays_disjoint_under_the_same_interleaving(
    sessions: Sessions,
) -> None:
    async with sessions() as w1, sessions() as w2:
        seen1 = [t.id for t in await select_claimable(w1, 2)]
        seen2 = [t.id for t in await select_claimable(w2, 2)]
        assert seen1 == seen2 == [1, 2]
        assert await guarded_claim(w1, seen1, "w1") == [1, 2]
        await w1.commit()
        assert await guarded_claim(w2, seen2, "w2") == []  # status is no longer 'open'
        await w2.commit()
    async with sessions() as check:
        owners = {t.id: t.claimed_by for t in (await check.scalars(select(Ticket))).all()}
    assert owners == {1: "w1", 2: "w1", 3: None, 4: None}


# --- HTTP mapping: If-Match -> version, StaleDataError -> 412 or retry then 409 ---------


def client_for(sessions: Sessions, race: Any = None) -> httpx.AsyncClient:
    app = build_app(sessions) if race is None else build_app(sessions, race)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_exposes_the_version_as_a_strong_etag(sessions: Sessions) -> None:
    async with client_for(sessions) as client:
        response = await client.get("/items/1")
    assert response.status_code == 200
    assert response.headers["etag"] == '"v1"'
    assert response.json()["version"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 428),
        ({"If-Match": '"v0"'}, 412),
        ({"If-Match": 'W/"v1"'}, 412),  # strong comparison: a weak tag never matches
        ({"If-Match": "v1"}, 400),
    ],
    ids=["missing", "stale", "weak", "malformed"],
)
async def test_patch_rejects_without_writing(
    sessions: Sessions, headers: dict[str, str], status: int
) -> None:
    async with client_for(sessions) as client:
        response = await client.patch("/items/1", json={"quantity": 3}, headers=headers)
        assert response.status_code == status
        after = await client.get("/items/1")
    assert after.json()["quantity"] == 10
    assert after.headers["etag"] == '"v1"'


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("if_match", ['"v1"', '"v7", "v1"', "*"])
async def test_patch_with_matching_precondition_writes_and_returns_the_new_etag(
    sessions: Sessions, if_match: str
) -> None:
    async with client_for(sessions) as client:
        response = await client.patch(
            "/items/1", json={"quantity": 3}, headers={"If-Match": if_match}
        )
    assert response.status_code == 200
    assert response.headers["etag"] == '"v2"'
    assert response.json() == {"id": 1, "name": "widget", "quantity": 3, "version": 2}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_race_after_the_precondition_check_is_caught_by_the_versioned_update(
    sessions: Sessions,
) -> None:
    async def race(item_id: int, attempt: int) -> None:
        await bump_quantity(sessions, 7)

    async with client_for(sessions, race) as client:
        response = await client.patch(
            "/items/1", json={"quantity": 3}, headers={"If-Match": '"v1"'}
        )
        assert response.status_code == 412
        after = await client.get("/items/1")
    assert after.json()["quantity"] == 7  # the competing write survives
    assert after.headers["etag"] == '"v2"'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unconditional_reservation_retries_on_stale_data_and_reapplies(
    sessions: Sessions,
) -> None:
    attempts: list[int] = []

    async def race(item_id: int, attempt: int) -> None:
        attempts.append(attempt)
        if attempt == 1:
            await bump_quantity(sessions, 6)  # someone else took 4 units

    async with client_for(sessions, race) as client:
        response = await client.post("/items/1/reservations", json={"amount": 2})
    assert response.status_code == 200
    assert response.headers["x-attempts"] == "2"
    assert attempts == [1, 2]
    assert response.json()["quantity"] == 4  # 6 - 2, recomputed from the fresh read
    assert response.headers["etag"] == '"v3"'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reservation_gives_up_with_409_after_bounded_retries(sessions: Sessions) -> None:
    attempts: list[int] = []

    async def race(item_id: int, attempt: int) -> None:
        attempts.append(attempt)
        await bump_quantity(sessions, 10 - attempt)

    async with client_for(sessions, race) as client:
        response = await client.post("/items/1/reservations", json={"amount": 1})
        assert response.status_code == 409
        after = await client.get("/items/1")
    assert attempts == list(range(1, MAX_RESERVE_ATTEMPTS + 1))
    assert after.json()["quantity"] == 10 - MAX_RESERVE_ATTEMPTS  # only competitors wrote
