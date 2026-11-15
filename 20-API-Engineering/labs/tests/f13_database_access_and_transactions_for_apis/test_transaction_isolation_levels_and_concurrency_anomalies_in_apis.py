"""Isolation anomalies: a PostgreSQL MVCC model, a withdrawal API, real SQLite and SQLAlchemy."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ArgumentError, InvalidRequestError

from api_labs.f13_database_access_and_transactions_for_apis.isolation_anomalies import (
    CONCURRENT_UPDATE,
    RW_DEPENDENCIES,
    Bank,
    Level,
    LockWait,
    SerializationFailure,
    Store,
    build_app,
    ledger_prefix,
)

if TYPE_CHECKING:
    from pathlib import Path

RC, RR, SER = Level.READ_COMMITTED, Level.REPEATABLE_READ, Level.SERIALIZABLE


def balance_of(store: Store, account_id: int = 1) -> int:
    reader = store.begin(RC)
    total = sum(reader.scan(ledger_prefix(account_id)).values())
    reader.commit()
    return total


def committed_value(store: Store, key: str = "acct:1") -> int | None:
    return store.begin(RC).read(key)


# --- Read phenomena -------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(("level", "second_read"), [(RC, 0), (RR, 100), (SER, 100)])
def test_non_repeatable_read_only_under_read_committed(level: Level, second_read: int) -> None:
    store = Store()
    store.seed({"acct:1": 100})
    reader = store.begin(level)
    assert reader.read("acct:1") == 100
    writer = store.begin(RC)
    writer.write("acct:1", 0)
    writer.commit()
    assert reader.read("acct:1") == second_read  # RC: fresh snapshot per statement


@pytest.mark.unit
@pytest.mark.parametrize(("level", "second_count"), [(RC, 3), (RR, 2)])
def test_phantom_rows_appear_only_under_read_committed(level: Level, second_count: int) -> None:
    store = Store()
    store.seed({"acct:1": 100, "acct:2": 50})
    reader = store.begin(level)
    assert len(reader.scan("acct:")) == 2
    writer = store.begin(RC)
    writer.insert("acct:", 70)
    writer.commit()
    assert len(reader.scan("acct:")) == second_count


# --- Lost update and first-updater-wins ----------------------------------------------------


@pytest.mark.unit
def test_application_computed_write_loses_update_under_read_committed() -> None:
    store = Store()
    store.seed({"acct:1": 100})
    a, b = store.begin(RC), store.begin(RC)
    seen_a, seen_b = a.read("acct:1"), b.read("acct:1")
    assert seen_a is not None
    assert seen_b is not None
    assert seen_a == seen_b == 100
    a.write("acct:1", seen_a + 10)
    a.commit()
    b.write("acct:1", seen_b + 20)  # overwrites a's committed +10
    b.commit()
    assert committed_value(store) == 120  # a serial order gives 130


@pytest.mark.unit
@pytest.mark.parametrize("level", [RR, SER])
def test_first_updater_wins_raises_40001_under_snapshot_levels(level: Level) -> None:
    store = Store()
    store.seed({"acct:1": 100})
    a, b = store.begin(level), store.begin(level)
    seen_a, seen_b = a.read("acct:1"), b.read("acct:1")
    assert seen_a is not None
    assert seen_b is not None
    a.write("acct:1", seen_a + 10)
    a.commit()
    with pytest.raises(SerializationFailure) as err:
        b.write("acct:1", seen_b + 20)
    assert err.value.sqlstate == "40001"
    assert err.value.message == CONCURRENT_UPDATE
    assert committed_value(store) == 110  # b was rolled back, not merged


@pytest.mark.unit
def test_in_database_increment_waits_then_reevaluates_under_read_committed() -> None:
    store = Store()
    store.seed({"acct:1": 100})
    a, b = store.begin(RC), store.begin(RC)
    a.add("acct:1", 10)
    with pytest.raises(LockWait) as wait:
        b.add("acct:1", 20)  # the row's newest version belongs to a, still in flight
    assert wait.value.blocker == a.xid
    a.commit()
    assert b.add("acct:1", 20) == 1  # resumes against a's committed version
    b.commit()
    assert committed_value(store) == 130


@pytest.mark.unit
@pytest.mark.parametrize(("blocker_commits", "final"), [(True, 110), (False, 120)])
def test_repeatable_read_waiter_fails_only_if_blocker_commits(
    blocker_commits: bool, final: int
) -> None:
    store = Store()
    store.seed({"acct:1": 100})
    a, b = store.begin(RR), store.begin(RR)
    b.read("acct:1")  # b's snapshot is fixed here
    a.add("acct:1", 10)
    with pytest.raises(LockWait):
        b.add("acct:1", 20)
    if blocker_commits:
        a.commit()
        with pytest.raises(SerializationFailure, match="concurrent update"):
            b.add("acct:1", 20)
    else:
        a.rollback()
        assert b.add("acct:1", 20) == 1
        b.commit()
    assert committed_value(store) == final


@pytest.mark.unit
def test_eval_plan_qual_skips_rows_that_stop_matching() -> None:
    """PostgreSQL docs 13.2.1: DELETE FROM website WHERE hits = 10 deletes nothing."""
    store = Store()
    store.seed({"site:1": 9, "site:2": 10})
    a, b = store.begin(RC), store.begin(RC)
    a.add("site:1", 1)
    a.add("site:2", 1)

    def is_ten(hits: int) -> bool:
        return hits == 10

    assert b.update_where("site:1", 0, is_ten) == 0  # 9 in b's snapshot: never a target
    with pytest.raises(LockWait):
        b.update_where("site:2", 0, is_ten)  # 10 in b's snapshot: waits for a
    a.commit()
    assert b.update_where("site:2", 0, is_ten) == 0  # re-checked against 11: skipped


# --- Write skew and SSI ---------------------------------------------------------------------


def overdraft_schedule(level: Level) -> tuple[Store, list[str]]:
    """Two withdrawals each check sum(ledger) >= 100, then each inserts -100 (ACIDRain Fig. 1)."""
    store = Store()
    store.seed({"ledger:1:000000": 100})
    a, b = store.begin(level), store.begin(level)
    log: list[str] = []
    for name, txn in (("a", a), ("b", b)):
        log.append(f"{name} sees {sum(txn.scan('ledger:1:').values())}")
    a.insert("ledger:1:", -100)
    b.insert("ledger:1:", -100)
    for name, txn in (("a", a), ("b", b)):
        try:
            txn.commit()
            log.append(f"{name} committed")
        except SerializationFailure as exc:
            log.append(f"{name} {exc.sqlstate}: {exc.detail}")
    return store, log


@pytest.mark.unit
@pytest.mark.parametrize("level", [RC, RR])
def test_write_skew_overdraft_commits_below_serializable(level: Level) -> None:
    store, log = overdraft_schedule(level)
    assert log == ["a sees 100", "b sees 100", "a committed", "b committed"]
    assert balance_of(store) == -100


@pytest.mark.unit
def test_serializable_cancels_the_pivot_at_commit() -> None:
    store, log = overdraft_schedule(SER)
    assert log == [
        "a sees 100",
        "b sees 100",
        "a committed",
        "b 40001: Canceled on identification as a pivot, during commit attempt.",
    ]
    assert balance_of(store) == 0


@pytest.mark.unit
def test_serial_execution_never_raises_under_serializable() -> None:
    store = Store()
    store.seed({"ledger:1:000000": 100})
    for expected in (100, 0):
        txn = store.begin(SER)
        assert sum(txn.scan("ledger:1:").values()) == expected
        if expected >= 100:
            txn.insert("ledger:1:", -100)
        txn.commit()  # no overlap -> no rw-antidependency -> no false positive
    assert balance_of(store) == 0


@pytest.mark.unit
@pytest.mark.parametrize(("a_level", "b_level"), [(SER, RR), (RR, SER)])
def test_one_non_serializable_participant_disables_ssi(a_level: Level, b_level: Level) -> None:
    store = Store()
    store.seed({"acct:1": 500, "acct:2": 500})
    a, b = store.begin(a_level), store.begin(b_level)
    assert sum(a.scan("acct:").values()) == sum(b.scan("acct:").values()) == 1000
    a.add("acct:1", -900)
    b.add("acct:2", -900)
    a.commit()
    b.commit()  # no 40001: SSI only tracks conflicts between Serializable transactions
    reader = store.begin(RC)
    assert reader.scan("acct:") == {"acct:1": -400, "acct:2": -400}


@pytest.mark.unit
@pytest.mark.parametrize("level", [RR, SER])
def test_read_only_anomaly_deposit_report(level: Level) -> None:
    """PostgreSQL wiki SSI 'Deposit Report': a read-only report sees a closed, incomplete batch."""
    store = Store()
    store.seed({"control": 1, "receipt:1:a": 100, "receipt:1:b": 100, "receipt:1:c": 100})
    t1 = store.begin(level)  # add a receipt to the current batch
    batch = t1.read("control")
    t1.insert(f"receipt:{batch}:", 100)
    t2 = store.begin(level)  # close the batch
    current = t2.read("control")
    assert current is not None
    t2.write("control", current + 1)
    t2.commit()
    t3 = store.begin(level)  # report on the closed batch
    assert len(t3.scan("receipt:1:")) == 3
    t3.commit()
    if level is RR:
        t1.commit()  # the report printed 3 receipts for a batch that ends with 4
        assert len(store.begin(RC).scan("receipt:1:")) == 4
    else:
        with pytest.raises(SerializationFailure) as err:
            t1.commit()
        assert err.value.message == RW_DEPENDENCIES
        assert err.value.detail == "Canceled on identification as a pivot, during commit attempt."
        assert len(store.begin(RC).scan("receipt:1:")) == 3


# --- The API: two concurrent POST /accounts/1/withdrawals -----------------------------------


async def two_concurrent_withdrawals(
    bank: Bank, strategy: str = "check-then-insert"
) -> list[httpx.Response]:
    barrier = asyncio.Barrier(2)

    async def interleave(attempt: int) -> None:
        if attempt == 1:  # both requests read before either writes; retries run alone
            await barrier.wait()

    bank.interleave = interleave
    transport = httpx.ASGITransport(app=build_app(bank))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        url = f"/accounts/1/withdrawals?strategy={strategy}"
        responses = await asyncio.gather(
            client.post(url, json={"amount": 100}), client.post(url, json={"amount": 100})
        )
    return sorted(responses, key=lambda r: r.status_code)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("level", [RC, RR])
async def test_api_check_then_insert_overdraws_below_serializable(level: Level) -> None:
    store = Store()
    store.seed({"ledger:1:000000": 100})
    bank = Bank(store, level)
    responses = await two_concurrent_withdrawals(bank)
    assert [r.status_code for r in responses] == [201, 201]
    assert [r.json() for r in responses] == [{"balance": 0, "attempts": 1}] * 2
    assert balance_of(store) == -100
    assert bank.serialization_failures == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_serializable_retry_turns_overdraft_into_409() -> None:
    store = Store()
    store.seed({"ledger:1:000000": 100})
    bank = Bank(store, SER)
    ok, rejected = await two_concurrent_withdrawals(bank)
    assert ok.status_code == 201
    assert ok.json() == {"balance": 0, "attempts": 1}
    assert rejected.status_code == 409
    assert rejected.json() == {"detail": "insufficient funds"}
    assert bank.serialization_failures == 1  # the loser re-ran its read and saw 0
    assert balance_of(store) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_retries_exhausted_maps_to_503_with_retry_after() -> None:
    store = Store()
    store.seed({"ledger:1:000000": 100})
    bank = Bank(store, SER, max_attempts=1)
    ok, unavailable = await two_concurrent_withdrawals(bank)
    assert ok.status_code == 201
    assert unavailable.status_code == 503
    assert unavailable.headers["retry-after"] == "1"
    assert balance_of(store) == 0


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("level", "failures"), [(RC, 0), (RR, 1)])
async def test_api_guarded_update_never_overdraws(level: Level, failures: int) -> None:
    store = Store()
    store.seed({"account:1": 100})
    bank = Bank(store, level)
    ok, rejected = await two_concurrent_withdrawals(bank, "guarded-update")
    assert (ok.status_code, rejected.status_code) == (201, 409)
    assert bank.serialization_failures == failures  # RR pays a retry, RC re-checks in place
    assert store.begin(RC).read("account:1") == 0


# --- Real engines ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sqlite_wal_rejects_write_from_stale_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "bank.sqlite"
    setup = sqlite3.connect(db, isolation_level=None)
    assert setup.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    setup.execute("CREATE TABLE ledger(id INTEGER PRIMARY KEY, account_id INT, amount INT)")
    setup.execute("INSERT INTO ledger(account_id, amount) VALUES (1, 100)")
    setup.close()
    a = sqlite3.connect(db, isolation_level=None, timeout=2.0)
    b = sqlite3.connect(db, isolation_level=None, timeout=2.0)
    try:
        a.execute("BEGIN")
        b.execute("BEGIN")
        for conn in (a, b):
            assert conn.execute("SELECT sum(amount) FROM ledger").fetchone() == (100,)
        a.execute("INSERT INTO ledger(account_id, amount) VALUES (1, -100)")
        a.execute("COMMIT")
        started = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError, match="database is locked") as err:
            b.execute("INSERT INTO ledger(account_id, amount) VALUES (1, -100)")
        elapsed = time.perf_counter() - started
        assert err.value.sqlite_errorname == "SQLITE_BUSY_SNAPSHOT"
        assert err.value.sqlite_errorcode == 517
        assert elapsed < 1.0  # the 2 s busy timeout is not applied: waiting cannot help
        b.execute("ROLLBACK")
        assert b.execute("SELECT sum(amount) FROM ledger").fetchone() == (0,)
    finally:
        a.close()
        b.close()


@pytest.mark.unit
def test_sqlalchemy_validates_and_scopes_isolation_level() -> None:
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        assert conn.get_isolation_level() == "SERIALIZABLE"
        with pytest.raises(ArgumentError, match="Invalid value 'REPEATABLE READ'"):
            conn.execution_options(isolation_level="REPEATABLE READ")
        conn.execute(text("SELECT 1"))  # autobegin
        with pytest.raises(InvalidRequestError, match="may not be altered unless rollback"):
            conn.execution_options(isolation_level="READ UNCOMMITTED")
        conn.rollback()
        conn.execution_options(isolation_level="READ_UNCOMMITTED")  # '_' is normalized
        assert conn.get_isolation_level() == "READ UNCOMMITTED"
    with engine.connect() as conn:
        assert conn.get_isolation_level() == "SERIALIZABLE"  # reset on return to the pool
    engine.dispose()
