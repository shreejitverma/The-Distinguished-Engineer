"""Lab: dual writes lose or invent events; the transactional outbox does neither."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api_labs.f13_database_access_and_transactions_for_apis.transactional_outbox import (
    CrashPoint,
    Customer,
    IdempotentConsumer,
    InMemoryBroker,
    Message,
    Order,
    OutboxEvent,
    OutboxRelay,
    ProcessCrashError,
    RelayPoller,
    WatermarkRelay,
    claim_batch,
    create_app,
    create_order_publish_after_commit,
    create_order_publish_before_commit,
    create_order_with_outbox,
    create_schema,
    make_engine,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

KNOWN, UNKNOWN = 1, 999
FIXED = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


@dataclass
class SignallingBroker(InMemoryBroker):
    delivered: asyncio.Event = field(default_factory=asyncio.Event)

    async def publish(self, message: Message) -> None:
        await super().publish(message)
        self.delivered.set()


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'outbox.db'}")
    await create_schema(eng)
    async with eng.begin() as conn:
        await conn.execute(insert(Customer).values(id=KNOWN, name="Ada"))
    yield eng
    await eng.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def count(
    sessions: async_sessionmaker[AsyncSession], model: type[Order | OutboxEvent]
) -> int:
    async with sessions() as s:
        return (await s.scalar(select(func.count()).select_from(model))) or 0


async def outbox_rows(sessions: async_sessionmaker[AsyncSession]) -> list[OutboxEvent]:
    async with sessions() as s:
        return list((await s.scalars(select(OutboxEvent).order_by(OutboxEvent.id))).all())


async def add_event(
    sessions: async_sessionmaker[AsyncSession], aggregate_id: str, row_id: int | None = None
) -> str:
    """Insert one outbox row directly (explicit ``row_id`` models a late commit)."""
    event_id = f"evt-{aggregate_id}-{row_id or 'auto'}-{await count(sessions, OutboxEvent)}"
    async with sessions() as s, s.begin():
        row = OutboxEvent(
            event_id=event_id,
            aggregate_type="order",
            aggregate_id=aggregate_id,
            event_type="OrderCreated",
            payload="{}",
            created_at=FIXED,
        )
        if row_id is not None:
            row.id = row_id
        s.add(row)
    return event_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_after_commit_loses_event_on_crash(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    broker = InMemoryBroker()
    with pytest.raises(ProcessCrashError):
        await create_order_publish_after_commit(
            sessions, broker, KNOWN, 500, crash_at=CrashPoint.AFTER_COMMIT_BEFORE_PUBLISH
        )
    assert await count(sessions, Order) == 1  # the order is durable
    assert broker.published == []  # and nobody will ever hear about it


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_before_commit_emits_phantom_event_when_commit_fails(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    broker = InMemoryBroker()
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        await create_order_publish_before_commit(sessions, broker, UNKNOWN, 500)
    assert await count(sessions, Order) == 0  # the order rolled back at COMMIT
    assert len(broker.published) == 1  # but consumers already saw OrderCreated
    assert broker.published[0].payload == b'{"customer_id":999,"order_id":1,"total_cents":500}'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_outbox_commit_failure_persists_neither_state_nor_event(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as s:
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            await create_order_with_outbox(s, UNKNOWN, 500)
    assert await count(sessions, Order) == 0
    assert await count(sessions, OutboxEvent) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_outbox_commit_persists_both_and_relay_publishes_after_commit(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    broker = InMemoryBroker()
    async with sessions() as s:
        order_id, event_id = await create_order_with_outbox(s, KNOWN, 500, now=lambda: FIXED)
    assert broker.published == []  # the handler never talks to the broker
    [row] = await outbox_rows(sessions)
    assert (row.event_id, row.aggregate_id, row.published_at) == (event_id, str(order_id), None)

    result = await OutboxRelay(sessions, broker, now=lambda: FIXED).drain_once()
    assert result.published == [event_id]
    assert [m.event_id for m in broker.published] == [event_id]
    [row] = await outbox_rows(sessions)
    assert row.published_at is not None
    assert row.published_at.replace(tzinfo=UTC) == FIXED
    assert (await OutboxRelay(sessions, broker).drain_once()).published == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_relay_crash_between_publish_and_mark_redelivers_and_consumer_dedups(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    broker = InMemoryBroker()
    async with sessions() as s:
        _, event_id = await create_order_with_outbox(s, KNOWN, 500)
    crashing = OutboxRelay(sessions, broker, crash_at=CrashPoint.AFTER_PUBLISH_BEFORE_MARK)
    with pytest.raises(ProcessCrashError):
        await crashing.drain_once()
    [row] = await outbox_rows(sessions)
    assert row.published_at is None  # the mark rolled back with the crash

    assert (await OutboxRelay(sessions, broker).drain_once()).published == [event_id]
    assert [m.event_id for m in broker.published] == [event_id, event_id]  # at-least-once
    consumer = IdempotentConsumer()
    applied = [consumer.handle(m) for m in broker.published]
    assert applied == [True, False]
    assert (consumer.applied, consumer.duplicates) == ([event_id], 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lost_ack_counts_as_failure_and_is_redelivered(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as s:
        _, event_id = await create_order_with_outbox(s, KNOWN, 500)
    broker = InMemoryBroker(lose_ack_event_ids={event_id})
    relay = OutboxRelay(sessions, broker)
    first = await relay.drain_once()
    assert (first.published, first.failed) == ([], [event_id])
    [row] = await outbox_rows(sessions)
    assert (row.attempts, row.published_at) == (1, None)
    assert row.last_error == f"ack for {event_id} lost after it was stored"
    assert (await relay.drain_once()).published == [event_id]
    assert [m.event_id for m in broker.published] == [event_id, event_id]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failure_blocks_only_its_aggregate_and_preserves_per_key_order(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    a1 = await add_event(sessions, "A")
    b1 = await add_event(sessions, "B")
    a2 = await add_event(sessions, "A")
    broker = InMemoryBroker(fail_event_ids={a1: 1})
    relay = OutboxRelay(sessions, broker)

    first = await relay.drain_once()
    assert (first.published, first.failed, first.blocked_aggregates) == ([b1], [a1], ["A"])
    second = await relay.drain_once()
    assert second.published == [a1, a2]
    assert [m.event_id for m in broker.published] == [b1, a1, a2]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_poison_event_parks_after_max_attempts_and_holds_its_aggregate(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    a1 = await add_event(sessions, "A")
    a2 = await add_event(sessions, "A")
    b1 = await add_event(sessions, "B")
    broker = InMemoryBroker(fail_event_ids={a1: 100})
    relay = OutboxRelay(sessions, broker, max_attempts=3)
    for _ in range(5):
        await relay.drain_once()
    [row_a1, row_a2, _] = await outbox_rows(sessions)
    assert row_a1.attempts == 3  # two more drains did not call the broker for it
    assert broker.fail_event_ids[a1] == 97
    assert row_a2.published_at is None  # A2 must not overtake the parked A1
    assert [m.event_id for m in broker.published] == [b1]
    assert a2 not in {m.event_id for m in broker.published}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watermark_relay_skips_event_committed_out_of_id_order(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    broker = InMemoryBroker()
    watermark = WatermarkRelay(sessions, broker)
    # PostgreSQL hands out id 1 to a transaction that commits after id 2 has committed.
    second = await add_event(sessions, "B", row_id=2)
    assert await watermark.drain_once() == [second]
    late = await add_event(sessions, "A", row_id=1)
    assert await watermark.drain_once() == []  # id 1 < watermark 2: skipped forever
    assert watermark.last_id == 2

    scanning = OutboxRelay(sessions, InMemoryBroker())
    assert (await scanning.drain_once()).published == [late, second]


@pytest.mark.unit
def test_claim_query_locks_rows_on_postgresql_but_not_on_sqlite() -> None:
    pg = str(claim_batch(100).compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
    lite = str(claim_batch(100).compile(dialect=sqlite.dialect()))
    assert pg.rstrip().endswith("FOR UPDATE SKIP LOCKED")
    assert "ORDER BY outbox.id" in pg
    assert "FOR UPDATE" not in lite


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_creates_order_and_outbox_row_and_poller_publishes_on_kick(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    broker = SignallingBroker()
    poller = RelayPoller(OutboxRelay(sessions, broker), interval=30.0)
    app = create_app(sessions, poller)
    task = asyncio.create_task(poller.run())
    try:
        async with asyncio.timeout(2.0):
            await poller.idle.wait()  # first (empty) drain done, now asleep for 30 s
        assert (poller.drains, broker.published) == (1, [])
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/orders", json={"customer_id": KNOWN, "total_cents": 500})
        assert resp.status_code == 201
        body = resp.json()
        assert resp.headers["location"] == f"/orders/{body['order_id']}"
        async with asyncio.timeout(2.0):  # far below the 30 s interval: only the kick wakes it
            await broker.delivered.wait()
        assert [m.event_id for m in broker.published] == [body["event_id"]]
    finally:
        poller.stop()
        async with asyncio.timeout(2.0):
            await task


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_commit_failure_returns_422_and_writes_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    broker = InMemoryBroker()
    poller = RelayPoller(OutboxRelay(sessions, broker), interval=30.0)
    transport = httpx.ASGITransport(app=create_app(sessions, poller))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/orders", json={"customer_id": UNKNOWN, "total_cents": 500})
    assert (resp.status_code, resp.json()) == (422, {"detail": "unknown customer"})
    assert await count(sessions, Order) == 0
    assert await count(sessions, OutboxEvent) == 0


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("roll_back", "orders_after"), [(False, 1), (True, 0)])
async def test_sqlite_failed_commit_without_rollback_leaks_rows_into_a_later_commit(
    sessions: async_sessionmaker[AsyncSession], roll_back: bool, orders_after: int
) -> None:
    async with sessions() as s:
        s.add(Order(customer_id=UNKNOWN, total_cents=500))
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            await s.commit()  # SQLite keeps the transaction open after this failure
        if roll_back:
            await s.rollback()
    async with sessions() as s, s.begin():  # an unrelated request on the same pooled connection
        s.add(Customer(id=UNKNOWN, name="Grace"))
    assert await count(sessions, Order) == orders_after
