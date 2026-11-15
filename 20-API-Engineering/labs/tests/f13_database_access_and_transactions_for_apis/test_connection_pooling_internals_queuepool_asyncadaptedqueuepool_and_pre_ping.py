"""Lab for Connection-Pooling-Internals-QueuePool-AsyncAdaptedQueuePool-and-Pre-Ping."""

from __future__ import annotations

import asyncio
import gc
import time
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import create_engine, event, exc, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, QueuePool
from sqlalchemy.pool import base as pool_base

from api_labs.f13_database_access_and_transactions_for_apis.connection_pooling import (
    PoolSnapshot,
    attach_ledger,
    drop_server_side,
    drop_server_side_async,
    serve_request,
    serve_request_async,
    snapshot,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Connection, Engine


def sqlite_engine(tmp_path: Path, **options: Any) -> Engine:
    return create_engine(f"sqlite:///{tmp_path / 'lab.db'}", **options)


def raw_id(conn: Connection) -> int:
    return id(conn.connection.dbapi_connection)


class FakeClock:
    """Stands in for the `time` module that `sqlalchemy.pool.base` reads."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now


@pytest.mark.unit
def test_queuepool_hard_cap_is_pool_size_plus_max_overflow(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path, pool_size=2, max_overflow=1, pool_timeout=0.05)
    held = [engine.connect() for _ in range(3)]
    started = time.monotonic()
    with pytest.raises(exc.TimeoutError) as caught:
        engine.connect()
    waited = time.monotonic() - started
    assert "QueuePool limit of size 2 overflow 1 reached" in str(caught.value)
    assert caught.value.code == "3o7r"
    assert waited >= 0.05
    for conn in held:
        conn.close()
    engine.dispose()


@pytest.mark.unit
def test_overflow_connection_is_closed_on_checkin_core_stays_open(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path, pool_size=2, max_overflow=1)
    ledger = attach_ledger(engine)
    assert snapshot(engine) == PoolSnapshot(size=2, checked_in=0, overflow=-2, checked_out=0)
    held = [engine.connect() for _ in range(3)]
    assert snapshot(engine) == PoolSnapshot(size=2, checked_in=0, overflow=1, checked_out=3)
    for conn in held:
        conn.close()
    assert snapshot(engine) == PoolSnapshot(size=2, checked_in=2, overflow=0, checked_out=0)
    assert (ledger.connects, ledger.closes) == (3, 1)
    engine.dispose()


@pytest.mark.unit
def test_pool_size_zero_is_unbounded_and_keeps_every_connection(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path, pool_size=0, max_overflow=0)
    ledger = attach_ledger(engine)
    held = [engine.connect() for _ in range(8)]
    for conn in held:
        conn.close()
    assert snapshot(engine).checked_in == 8
    assert (ledger.connects, ledger.closes) == (8, 0)
    engine.dispose()


@pytest.mark.unit
def test_pre_ping_skips_fresh_connections_and_pings_reused_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = sqlite_engine(tmp_path, pool_size=1, max_overflow=0, pool_pre_ping=True)
    pings: list[object] = []
    real_ping = engine.dialect.do_ping

    def counting_ping(dbapi_connection: Any) -> bool:
        pings.append(dbapi_connection)
        return real_ping(dbapi_connection)

    monkeypatch.setattr(engine.dialect, "do_ping", counting_ping)
    assert serve_request(engine) == 1
    assert pings == []
    assert serve_request(engine) == 1
    assert serve_request(engine) == 1
    assert len(pings) == 2
    engine.dispose()


@pytest.mark.unit
def test_pre_ping_replaces_dead_connection_and_invalidates_its_generation(
    tmp_path: Path,
) -> None:
    engine = sqlite_engine(tmp_path, pool_size=2, max_overflow=0, pool_pre_ping=True)
    ledger = attach_ledger(engine)
    first, second = engine.connect(), engine.connect()
    first.close()
    second.close()
    assert drop_server_side(ledger) == 2

    assert serve_request(engine) == 1
    assert serve_request(engine) == 1
    # One failed ping invalidated the whole generation: the second stale
    # connection was recycled on checkout without being pinged at all.
    assert ledger.invalidations == 1
    assert ledger.connects == 4
    assert snapshot(engine).checked_in == 2
    engine.dispose()


@pytest.mark.unit
def test_without_pre_ping_one_request_fails_then_the_pool_heals(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path, pool_size=2, max_overflow=0)
    ledger = attach_ledger(engine)
    first, second = engine.connect(), engine.connect()
    first.close()
    second.close()
    drop_server_side(ledger)

    with pytest.raises(exc.DBAPIError) as caught:
        serve_request(engine)
    assert caught.value.connection_invalidated is True
    assert "Cannot operate on a closed database." in str(caught.value.orig)
    assert serve_request(engine) == 1
    assert serve_request(engine) == 1
    assert ledger.invalidations == 1
    engine.dispose()


@pytest.mark.unit
def test_checkout_disconnection_error_is_retried_once_not_three_times(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path, pool_size=1, max_overflow=0)
    ledger = attach_ledger(engine)
    calls: list[int] = []

    @event.listens_for(engine, "checkout")
    def always_disconnected(dbapi_connection: Any, record: Any, proxy: Any) -> None:
        calls.append(1)
        raise exc.DisconnectionError("simulated dead socket")

    with pytest.raises(exc.InvalidRequestError, match="This connection is closed"):
        engine.connect()
    assert len(calls) == 2
    assert ledger.connects == 3
    assert snapshot(engine).checked_in == 1
    engine.dispose()


@pytest.mark.unit
def test_pool_recycle_replaces_connection_only_at_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(pool_base, "time", clock)
    engine = sqlite_engine(tmp_path, pool_size=1, max_overflow=0, pool_recycle=3600)
    ledger = attach_ledger(engine)

    with engine.connect() as conn:
        original = raw_id(conn)
    clock.now += 3600
    with engine.connect() as conn:
        assert raw_id(conn) == original
        clock.now += 1
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
        assert raw_id(conn) == original
    with engine.connect() as conn:
        assert raw_id(conn) != original
    assert (ledger.connects, ledger.closes, ledger.invalidations) == (2, 1, 0)
    engine.dispose()


@pytest.mark.unit
@pytest.mark.parametrize(("use_lifo", "expected"), [(False, "first"), (True, "second")])
def test_lifo_reuses_the_most_recent_connection_fifo_rotates(
    tmp_path: Path, use_lifo: bool, expected: str
) -> None:
    engine = sqlite_engine(tmp_path, pool_size=2, max_overflow=0, pool_use_lifo=use_lifo)
    first, second = engine.connect(), engine.connect()
    ids = {"first": raw_id(first), "second": raw_id(second)}
    first.close()
    second.close()
    with engine.connect() as conn:
        assert raw_id(conn) == ids[expected]
    engine.dispose()


@pytest.mark.unit
@pytest.mark.parametrize(("reset_on_return", "leaked_rows"), [("rollback", 0), (None, 1)])
def test_reset_on_return_decides_whether_uncommitted_work_leaks(
    tmp_path: Path, reset_on_return: str | None, leaked_rows: int
) -> None:
    engine = sqlite_engine(
        tmp_path, pool_size=1, max_overflow=0, pool_reset_on_return=reset_on_return
    )
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE audit (id INTEGER PRIMARY KEY)"))

    raw = engine.raw_connection()
    raw.cursor().execute("INSERT INTO audit (id) VALUES (1)")
    raw.close()

    raw = engine.raw_connection()
    cursor = raw.cursor()
    cursor.execute("SELECT count(*) FROM audit")
    assert cursor.fetchone() == (leaked_rows,)
    raw.rollback()
    raw.close()
    engine.dispose()


@pytest.mark.unit
def test_unclosed_sync_connection_is_returned_by_its_finalizer(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path, pool_size=1, max_overflow=0)
    ledger = attach_ledger(engine)
    raw = engine.raw_connection()
    assert snapshot(engine).checked_out == 1
    del raw
    gc.collect()
    assert snapshot(engine) == PoolSnapshot(size=1, checked_in=1, overflow=0, checked_out=0)
    assert ledger.checkins == 1
    engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_engine_defaults_to_async_adapted_queuepool(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'lab.db'}"
    engine = create_async_engine(url)
    assert type(engine.pool) is AsyncAdaptedQueuePool
    await engine.dispose()
    with pytest.raises(exc.ArgumentError) as caught:
        create_async_engine(url, poolclass=QueuePool)
    assert caught.value.code == "pcls"
    assert "Pool class QueuePool cannot be used with asyncio engine" in str(caught.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_pool_timeout_waits_without_blocking_the_loop(tmp_path: Path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'lab.db'}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    ticks = 0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    holder = await engine.connect()
    ticking = asyncio.create_task(ticker())
    with pytest.raises(exc.TimeoutError, match="QueuePool limit of size 1 overflow 0"):
        await engine.connect()
    stop.set()
    await ticking
    assert ticks >= 10
    await holder.close()
    await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_waiter_is_handed_the_released_connection(tmp_path: Path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'lab.db'}", pool_size=1, max_overflow=0
    )
    ledger = attach_ledger(engine.sync_engine)
    holder = await engine.connect()
    waiter = asyncio.create_task(serve_request_async(engine))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    await holder.close()
    assert await asyncio.wait_for(waiter, 1.0) == 1
    assert ledger.connects == 1
    await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_pre_ping_replaces_dead_aiosqlite_connection(tmp_path: Path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'lab.db'}",
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
    )
    ledger = attach_ledger(engine.sync_engine)
    assert await serve_request_async(engine) == 1
    assert await drop_server_side_async(ledger) == 1

    assert await serve_request_async(engine) == 1
    assert (ledger.connects, ledger.invalidations) == (2, 1)
    await engine.dispose()
