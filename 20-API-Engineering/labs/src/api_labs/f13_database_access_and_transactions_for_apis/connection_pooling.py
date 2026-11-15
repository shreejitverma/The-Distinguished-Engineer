"""Connection pool lab: QueuePool and AsyncAdaptedQueuePool limits, recycle, reset, and pre-ping.

The lab drives real SQLAlchemy pools over SQLite files, so no server or socket is needed.
A database restart is simulated by closing every raw driver connection behind the pool's
back: the pool still holds the records, exactly as it holds sockets the server has reset.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, text
from sqlalchemy.pool import QueuePool

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy.engine.interfaces import DBAPIConnection
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.pool import ConnectionPoolEntry, PoolProxiedConnection


@dataclass
class PoolLedger:
    """Counts pool lifecycle events and keeps every driver connection the pool opened."""

    connects: int = 0
    checkouts: int = 0
    checkins: int = 0
    invalidations: int = 0
    closes: int = 0
    drivers: list[Any] = field(default_factory=list[Any])


def attach_ledger(engine: Engine) -> PoolLedger:
    """Listen to the engine's pool events; pass `AsyncEngine.sync_engine` for async engines."""
    ledger = PoolLedger()

    def on_connect(dbapi_connection: DBAPIConnection, record: ConnectionPoolEntry) -> None:
        ledger.connects += 1
        ledger.drivers.append(record.driver_connection)

    def on_checkout(
        dbapi_connection: DBAPIConnection,
        record: ConnectionPoolEntry,
        proxy: PoolProxiedConnection,
    ) -> None:
        ledger.checkouts += 1

    def on_checkin(dbapi_connection: DBAPIConnection | None, record: ConnectionPoolEntry) -> None:
        ledger.checkins += 1

    def on_invalidate(
        dbapi_connection: DBAPIConnection,
        record: ConnectionPoolEntry,
        exception: BaseException | None,
    ) -> None:
        ledger.invalidations += 1

    def on_close(dbapi_connection: DBAPIConnection, record: ConnectionPoolEntry) -> None:
        ledger.closes += 1

    event.listen(engine, "connect", on_connect)
    event.listen(engine, "checkout", on_checkout)
    event.listen(engine, "checkin", on_checkin)
    event.listen(engine, "invalidate", on_invalidate)
    event.listen(engine, "close", on_close)
    return ledger


@dataclass(frozen=True)
class PoolSnapshot:
    """The four counters `QueuePool.status()` prints."""

    size: int
    checked_in: int
    overflow: int
    checked_out: int


def snapshot(engine: Engine) -> PoolSnapshot:
    pool = engine.pool
    if not isinstance(pool, QueuePool):
        raise TypeError(f"expected a QueuePool subclass, got {type(pool).__name__}")
    return PoolSnapshot(pool.size(), pool.checkedin(), pool.overflow(), pool.checkedout())


def drop_server_side(ledger: PoolLedger) -> int:
    """Close every sync driver connection under the pool, as a server restart would."""
    dropped = len(ledger.drivers)
    for driver in ledger.drivers:
        driver.close()
    ledger.drivers.clear()
    return dropped


async def drop_server_side_async(ledger: PoolLedger) -> int:
    """Close every aiosqlite driver connection under the pool, as a server restart would."""
    dropped = len(ledger.drivers)
    for driver in ledger.drivers:
        await driver.close()
    ledger.drivers.clear()
    return dropped


def serve_request(engine: Engine) -> int:
    """One API request: check out, run `SELECT 1`, return the connection to the pool."""
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT 1")).scalar_one())


async def serve_request_async(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text("SELECT 1"))).scalar_one())
