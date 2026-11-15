from typing import Any

import httpx
import pytest
import sqlalchemy
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import CompileError, StatementError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from api_labs.f13_database_access_and_transactions_for_apis.bulk_writes import (
    MAX_BATCH,
    WIDE_COLUMNS,
    Item,
    benchmark,
    build_app,
    insert_bulk,
    insert_bulk_returning,
    insert_ignore_returning,
    insert_shipments_returning,
    insert_unit_of_work,
    item_rows,
    make_async_engine,
    make_engine,
    upsert_items,
    upsert_items_bound_set,
    wide,
)

SQLALCHEMY_2_0 = sqlalchemy.__version__.startswith("2.0.")


@pytest.mark.unit
def test_5000_row_strategies_round_trip_counts() -> None:
    result = benchmark(n=5000, repeats=1)
    calls = {name: r.cursor_calls for name, r in result.items()}
    assert calls == {
        "add_all_flush": 5000,  # ordered RETURNING id, no sentinel on SQLite ROWID
        "bulk_executemany": 1,  # one cursor.executemany(), nothing fetched back
        "bulk_returning_unordered": 5,  # 5 x 1000-row INSERT .. VALUES (..), (..) RETURNING
        "bulk_returning_ordered_rowid": 5000,  # downgraded to row-at-a-time
        "bulk_returning_ordered_uuid": 5,  # client-side UUID key is the sentinel
    }


@pytest.mark.unit
def test_unit_of_work_flush_returns_keys_in_input_order_one_row_per_statement() -> None:
    engine, log = make_engine()
    with Session(engine) as session:
        rows = item_rows(30)
        ids = insert_unit_of_work(session, rows)
        assert ids == list(range(1, 31))
        assert len(log.entries) == 30
        assert all(e.statement.endswith("RETURNING id") for e in log.entries)
        assert log.rows_per_call() == [1] * 30


@pytest.mark.unit
def test_bulk_insert_without_returning_is_one_executemany_and_loads_no_objects() -> None:
    engine, log = make_engine()
    with Session(engine) as session:
        insert_bulk(session, item_rows(5000))
        assert len(log.entries) == 1
        only = log.entries[0]
        assert only.executemany
        assert only.rows == 5000
        assert "RETURNING" not in only.statement
        assert len(session.identity_map) == 0
        assert session.scalar(select(func.count()).select_from(Item)) == 5000


@pytest.mark.unit
def test_bulk_returning_batches_by_page_size_and_fills_identity_map() -> None:
    engine, log = make_engine()
    with Session(engine) as session:
        rows = item_rows(2500)
        items = insert_bulk_returning(session, rows, ordered=False)
        assert log.rows_per_call() == [1000, 1000, 500]
        assert not any(e.executemany for e in log.entries)
        assert {i.sku for i in items} == {r["sku"] for r in rows}
        assert len(session.identity_map) == 2500
        assert session.get(Item, items[0].id) is items[0]


@pytest.mark.unit
def test_insertmanyvalues_page_size_execution_option_changes_batch_count() -> None:
    engine, log = make_engine(insertmanyvalues_page_size=250)
    with Session(engine) as session:
        insert_bulk_returning(session, item_rows(1000), ordered=False)
        assert log.rows_per_call() == [250, 250, 250, 250]


@pytest.mark.unit
def test_sort_by_parameter_order_degrades_to_row_at_a_time_for_server_rowid() -> None:
    engine, log = make_engine()
    with Session(engine) as session:
        rows = item_rows(50)
        items = insert_bulk_returning(session, rows, ordered=True)
        assert [i.sku for i in items] == [r["sku"] for r in rows]
        assert log.rows_per_call() == [1] * 50


@pytest.mark.unit
def test_client_side_uuid_sentinel_keeps_ordered_returning_batched() -> None:
    engine, log = make_engine()
    with Session(engine) as session:
        skus = [f"ship-{i:05d}" for i in range(2500)]
        shipments = insert_shipments_returning(session, skus)
        assert [s.sku for s in shipments] == skus
        assert log.rows_per_call() == [1000, 1000, 500]


@pytest.mark.unit
def test_parameter_cap_shrinks_batch_for_wide_rows() -> None:
    engine, log = make_engine()
    assert engine.dialect.insertmanyvalues_max_parameters == 32700
    rows = [{f"c{i}": i for i in range(WIDE_COLUMNS)} for _ in range(2000)]
    with engine.begin() as conn:
        ids = conn.execute(wide.insert().returning(wide.c.id), rows).scalars().all()
    assert len(ids) == 2000
    assert 32700 // WIDE_COLUMNS == 817
    assert log.rows_per_call() == [817, 817, 366]


@pytest.mark.unit
def test_none_values_split_batches_unless_render_nulls() -> None:
    rows: list[dict[str, Any]] = [
        {"sku": "a", "qty": 1},
        {"sku": "b", "qty": None},
        {"sku": "c", "qty": 3},
    ]
    engine, log = make_engine()
    with Session(engine) as session:
        session.execute(insert(Item), rows)
        assert [e.statement for e in log.entries] == [
            "INSERT INTO item (sku, qty) VALUES (?, ?)",
            "INSERT INTO item (sku) VALUES (?)",
            "INSERT INTO item (sku, qty) VALUES (?, ?)",
        ]
    engine, log = make_engine()
    with Session(engine) as session:
        session.execute(insert(Item).execution_options(render_nulls=True), rows)
        assert len(log.entries) == 1
        assert log.entries[0].rows == 3


@pytest.mark.unit
def test_multi_row_values_cannot_sort_returning() -> None:
    engine, _ = make_engine()
    stmt = (
        insert(Item)
        .values([{"sku": "m1"}, {"sku": "m2"}])
        .returning(Item.id, sort_by_parameter_order=True)
    )
    with (
        Session(engine) as session,
        pytest.raises(CompileError, match="cannot be deterministically sorted"),
    ):
        session.execute(stmt)


@pytest.mark.unit
def test_upsert_inserts_and_updates_in_one_batched_statement() -> None:
    engine, log = make_engine()
    with Session(engine) as session:
        before = {i.sku: i.id for i in insert_bulk_returning(session, item_rows(5), ordered=False)}
        session.commit()
        log.clear()
        out = upsert_items(session, item_rows(10, qty=100), populate_existing=True)
        assert len(log.entries) == 1
        assert "ON CONFLICT (sku) DO UPDATE SET qty = excluded.qty RETURNING" in (
            log.entries[0].statement
        )
        assert log.entries[0].rows == 10
        by_sku = {i.sku: i for i in out}
        assert {sku: by_sku[sku].id for sku in before} == before  # same rows, updated
        assert {i.qty for i in out} == set(range(100, 110))
        assert session.scalar(select(func.count()).select_from(Item)) == 10


@pytest.mark.unit
def test_upsert_returning_leaves_loaded_objects_stale_without_populate_existing() -> None:
    engine, _ = make_engine()
    with Session(engine) as session:
        session.execute(insert(Item), item_rows(1))
        loaded = session.scalars(select(Item)).one()
        assert loaded.qty == 0
        returned = upsert_items(session, [{"sku": "sku-00000", "qty": 7}], populate_existing=False)
        assert returned == [loaded]  # identity map hands back the same object
        assert loaded.qty == 0  # the row says 7; the in-memory object won
        upsert_items(session, [{"sku": "sku-00000", "qty": 8}], populate_existing=True)
        assert loaded.qty == 8


@pytest.mark.unit
def test_upsert_skips_python_onupdate_but_bulk_update_applies_it() -> None:
    engine, _ = make_engine()
    with Session(engine) as session:
        session.execute(insert(Item), item_rows(2))
        upsert_items(session, item_rows(2, qty=50), populate_existing=True)
        stamps = session.scalars(select(Item.updated_at).order_by(Item.id)).all()
        assert stamps == [None, None]  # ON CONFLICT DO UPDATE ignored Column.onupdate
        session.execute(update(Item), [{"id": 1, "qty": 1}])
        stamps = session.scalars(select(Item.updated_at).order_by(Item.id)).all()
        assert stamps[0] is not None
        assert stamps[1] is None


@pytest.mark.unit
def test_upsert_with_bound_set_parameter_runs_row_at_a_time() -> None:
    engine, log = make_engine()
    with Session(engine) as session:
        session.execute(insert(Item), item_rows(4))
        log.clear()
        rows = [{"sku": f"sku-{i:05d}", "qty": 0, "new_qty": 900 + i} for i in range(4)]
        if SQLALCHEMY_2_0:
            # 2.0.57: the ORM bulk path does not forward the non-attribute key.
            with pytest.raises(StatementError, match="A value is required for bind parameter"):
                upsert_items_bound_set(session, rows)
            return
        ids = upsert_items_bound_set(session, rows)
        assert sorted(ids) == [1, 2, 3, 4]
        assert log.rows_per_call() == [1, 1, 1, 1]  # issue #13130: one SET per row
        qtys = session.scalars(select(Item.qty).order_by(Item.id)).all()
        assert qtys == [900, 901, 902, 903]


@pytest.mark.unit
def test_do_nothing_returning_omits_conflicting_rows_even_when_ordered() -> None:
    engine, _ = make_engine()
    with Session(engine) as session:
        session.execute(insert(Item), [{"sku": "old-1", "qty": 1}, {"sku": "old-2", "qty": 1}])
        rows = [
            {"sku": "old-1", "qty": 9},
            {"sku": "new-1", "qty": 9},
            {"sku": "old-2", "qty": 9},
            {"sku": "new-2", "qty": 9},
        ]
        assert sorted(insert_ignore_returning(session, rows, ordered=False)) == ["new-1", "new-2"]
        more = [{"sku": "old-1", "qty": 9}, {"sku": "new-3", "qty": 9}]
        got = insert_ignore_returning(session, more, ordered=True)
        assert got == ["new-3"]
        # zip(input, RETURNING) silently pairs the skipped "old-1" with "new-3"
        assert next(zip(more, got, strict=False))[0]["sku"] == "old-1"


@pytest.mark.unit
def test_duplicate_keys_in_one_sqlite_batch_update_the_row_twice() -> None:
    engine, log = make_engine()
    with Session(engine) as session:
        stmt_rows = [{"sku": "dup", "qty": 1}, {"sku": "dup", "qty": 2}]
        out = upsert_items(session, stmt_rows, populate_existing=True)
        assert len(log.entries) == 1
        assert [i.id for i in out] == [1, 1]
        assert out[0] is out[1]
        assert session.scalars(select(Item.qty)).all() == [2]  # PostgreSQL: SQLSTATE 21000


@pytest.mark.unit
def test_bulk_update_by_primary_key_is_executemany_without_returning() -> None:
    engine, log = make_engine()
    with Session(engine) as session:
        session.execute(insert(Item), item_rows(3))
        log.clear()
        session.execute(update(Item), [{"id": 1, "qty": 10}, {"id": 3, "qty": 30}])
        assert len(log.entries) == 1
        assert log.entries[0].executemany
        assert log.entries[0].statement.startswith("UPDATE item SET qty=?, updated_at=")
        with pytest.raises(StatementError, match=r"UPDATE\.\.RETURNING when executemany"):
            session.execute(
                update(Item).returning(Item.id), [{"id": 1, "qty": 11}, {"id": 2, "qty": 20}]
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulk_upsert_endpoint_creates_then_updates_with_stable_ids() -> None:
    engine, log = await make_async_engine()
    app = build_app(async_sessionmaker(engine, expire_on_commit=False))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.put("/items", json={"items": item_rows(3)})
        assert first.status_code == 200
        created = first.json()
        assert [r["sku"] for r in created] == ["sku-00000", "sku-00001", "sku-00002"]
        log.clear()
        batch = [{"sku": "sku-00002", "qty": 77}, {"sku": "sku-new", "qty": 5}]
        second = await client.put("/items", json={"items": batch})
        assert second.status_code == 200
        body = second.json()
        assert body[0] == {"id": created[2]["id"], "sku": "sku-00002", "qty": 77}
        assert body[1]["sku"] == "sku-new"
        assert body[1]["id"] not in {r["id"] for r in created}
        inserts = [e for e in log.entries if e.statement.startswith("INSERT")]
        assert len(inserts) == 1
    await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulk_upsert_endpoint_rejects_oversized_and_duplicate_batches() -> None:
    engine, log = await make_async_engine()
    app = build_app(async_sessionmaker(engine, expire_on_commit=False))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        too_big = await client.put("/items", json={"items": item_rows(MAX_BATCH + 1)})
        assert too_big.status_code == 422
        dup = await client.put(
            "/items", json={"items": [{"sku": "x", "qty": 1}, {"sku": "x", "qty": 2}]}
        )
        assert dup.status_code == 422
        assert "duplicate sku in batch: x" in dup.text
        ok = await client.put("/items", json={"items": item_rows(MAX_BATCH)})
        assert ok.status_code == 200
        assert len(ok.json()) == MAX_BATCH
    assert sum(1 for e in log.entries if e.statement.startswith("INSERT")) == 1
    await engine.dispose()
