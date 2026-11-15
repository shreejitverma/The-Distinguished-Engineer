"""Dual writes versus the transactional outbox, on SQLAlchemy 2.x async and SQLite.

The two naive handlers write the order and publish the event as two separate
operations, so a crash or a failed commit between them leaves the database and
the broker disagreeing.  The outbox handler writes the order and an
``OutboxEvent`` row in one transaction; ``OutboxRelay`` later copies committed
rows to the broker with at-least-once delivery, and ``IdempotentConsumer``
absorbs the duplicates by event id.

``orders.customer_id`` is a ``DEFERRABLE INITIALLY DEFERRED`` foreign key, so an
unknown customer passes ``INSERT`` and ``flush()`` and fails only at ``COMMIT``:
a real commit-time failure, the case that publish-before-commit cannot survive.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Any, Protocol

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import ForeignKey, Integer, String, Text, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Select


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", deferrable=True, initially="DEFERRED")
    )
    total_cents: Mapped[int] = mapped_column(Integer)


class OutboxEvent(Base):
    """One row per event, inserted in the same transaction as the state change."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True)
    aggregate_type: Mapped[str] = mapped_column(String(50))
    aggregate_id: Mapped[str] = mapped_column(String(50), index=True)
    event_type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column()
    published_at: Mapped[datetime | None] = mapped_column(default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)


@dataclass(frozen=True, slots=True)
class Message:
    """What the broker carries: the outbox row minus relay bookkeeping."""

    event_id: str
    key: str
    event_type: str
    payload: bytes


class BrokerUnavailableError(Exception):
    """The publish call failed; the broker may or may not have stored the message."""


class ProcessCrashError(Exception):
    """Stands in for SIGKILL or an OOM kill at a chosen point in a handler."""


class CrashPoint(Enum):
    AFTER_COMMIT_BEFORE_PUBLISH = "after_commit_before_publish"
    AFTER_PUBLISH_BEFORE_MARK = "after_publish_before_mark"


class Broker(Protocol):
    async def publish(self, message: Message) -> None: ...


@dataclass
class InMemoryBroker:
    """A broker with fault injection, keyed the way Kafka keys a partition."""

    published: list[Message] = field(default_factory=list[Message])
    fail_event_ids: dict[str, int] = field(default_factory=dict[str, int])
    lose_ack_event_ids: set[str] = field(default_factory=set[str])

    async def publish(self, message: Message) -> None:
        remaining = self.fail_event_ids.get(message.event_id, 0)
        if remaining:
            self.fail_event_ids[message.event_id] = remaining - 1
            raise BrokerUnavailableError(f"broker refused {message.event_id}")
        self.published.append(message)
        if message.event_id in self.lose_ack_event_ids:
            self.lose_ack_event_ids.discard(message.event_id)
            raise BrokerUnavailableError(f"ack for {message.event_id} lost after it was stored")


@dataclass
class IdempotentConsumer:
    """Applies each event id once; in production the seen set is an inbox table."""

    seen: set[str] = field(default_factory=set[str])
    applied: list[str] = field(default_factory=list[str])
    duplicates: int = 0

    def handle(self, message: Message) -> bool:
        if message.event_id in self.seen:
            self.duplicates += 1
            return False
        self.seen.add(message.event_id)
        self.applied.append(message.event_id)
        return True


def make_engine(url: str) -> AsyncEngine:
    """Create an engine whose SQLite connections enforce foreign keys."""
    engine = create_async_engine(url)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:  # pyright: ignore[reportUnusedFunction]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def order_message(order: Order, event_id: str) -> Message:
    body = {
        "order_id": order.id,
        "customer_id": order.customer_id,
        "total_cents": order.total_cents,
    }
    return Message(
        event_id=event_id,
        key=str(order.id),
        event_type="OrderCreated",
        payload=json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
    )


async def create_order_publish_after_commit(
    sessions: async_sessionmaker[AsyncSession],
    broker: Broker,
    customer_id: int,
    total_cents: int,
    crash_at: CrashPoint | None = None,
) -> int:
    """Dual write, database first: a crash after COMMIT loses the event."""
    async with sessions() as session:
        order = Order(customer_id=customer_id, total_cents=total_cents)
        session.add(order)
        await session.commit()  # durable from here on
        if crash_at is CrashPoint.AFTER_COMMIT_BEFORE_PUBLISH:
            raise ProcessCrashError(crash_at.value)
        await broker.publish(order_message(order, str(uuid.uuid4())))
        return order.id


async def create_order_publish_before_commit(
    sessions: async_sessionmaker[AsyncSession],
    broker: Broker,
    customer_id: int,
    total_cents: int,
) -> int:
    """Dual write, broker first: a failed COMMIT leaves a phantom event."""
    async with sessions() as session, session.begin():  # COMMIT runs on block exit
        order = Order(customer_id=customer_id, total_cents=total_cents)
        session.add(order)
        await session.flush()  # assigns order.id; the deferred FK is not checked yet
        await broker.publish(order_message(order, str(uuid.uuid4())))
    return order.id  # unreachable for an unknown customer: COMMIT raised IntegrityError


async def create_order_with_outbox(
    session: AsyncSession,
    customer_id: int,
    total_cents: int,
    now: Callable[[], datetime] = utc_now,
) -> tuple[int, str]:
    """State change and event in one transaction: both commit or neither does."""
    async with session.begin():
        order = Order(customer_id=customer_id, total_cents=total_cents)
        session.add(order)
        await session.flush()
        message = order_message(order, str(uuid.uuid4()))
        session.add(
            OutboxEvent(
                event_id=message.event_id,
                aggregate_type="order",
                aggregate_id=message.key,
                event_type=message.event_type,
                payload=message.payload.decode(),
                created_at=now(),
            )
        )
    return order.id, message.event_id


def claim_batch(batch_size: int) -> Select[OutboxEvent]:
    """Oldest unpublished rows; on PostgreSQL this renders FOR UPDATE SKIP LOCKED."""
    return (
        select(OutboxEvent)
        .where(OutboxEvent.published_at.is_(None))
        .order_by(OutboxEvent.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )


@dataclass(frozen=True, slots=True)
class DrainResult:
    published: list[str]
    failed: list[str]
    blocked_aggregates: list[str]


def to_message(row: OutboxEvent) -> Message:
    return Message(row.event_id, row.aggregate_id, row.event_type, row.payload.encode())


@dataclass
class OutboxRelay:
    """Polling publisher: copy committed outbox rows to the broker, then mark them.

    Delivery is at-least-once: the mark commits after the publish returns, so a
    crash or a lost ack between the two republishes the row on the next drain.
    A failed or parked event blocks later events of the same aggregate only, which
    keeps per-key order without stalling unrelated aggregates.
    """

    sessions: async_sessionmaker[AsyncSession]
    broker: Broker
    batch_size: int = 100
    max_attempts: int = 5
    now: Callable[[], datetime] = utc_now
    crash_at: CrashPoint | None = None

    async def drain_once(self) -> DrainResult:
        published: list[str] = []
        failed: list[str] = []
        blocked: set[str] = set()
        async with self.sessions() as session, session.begin():
            rows = (await session.scalars(claim_batch(self.batch_size))).all()
            for row in rows:
                if row.aggregate_id in blocked:
                    continue
                if row.attempts >= self.max_attempts:
                    blocked.add(row.aggregate_id)  # parked: needs an operator
                    continue
                try:
                    await self.broker.publish(to_message(row))
                except BrokerUnavailableError as exc:
                    row.attempts += 1
                    row.last_error = str(exc)
                    failed.append(row.event_id)
                    blocked.add(row.aggregate_id)
                    continue
                if self.crash_at is CrashPoint.AFTER_PUBLISH_BEFORE_MARK:
                    raise ProcessCrashError(self.crash_at.value)
                row.published_at = self.now()
                published.append(row.event_id)
        return DrainResult(published, failed, sorted(blocked))

    async def drain_until_idle(self) -> int:
        total = 0
        while True:
            result = await self.drain_once()
            total += len(result.published)
            if not result.published:
                return total


@dataclass
class WatermarkRelay:
    """The broken variant: remembers the highest id it has published."""

    sessions: async_sessionmaker[AsyncSession]
    broker: Broker
    last_id: int = 0

    async def drain_once(self) -> list[str]:
        sent: list[str] = []
        async with self.sessions() as session:
            stmt = select(OutboxEvent).where(OutboxEvent.id > self.last_id).order_by(OutboxEvent.id)
            for row in (await session.scalars(stmt)).all():
                await self.broker.publish(to_message(row))
                self.last_id = row.id
                sent.append(row.event_id)
        return sent


class RelayPoller:
    """Drive a relay on a timer, woken early by ``kick()`` after each commit.

    The wake event is cleared before draining, so a kick that lands while a drain
    is running is not lost; the interval bounds latency if a kick is never sent.
    """

    def __init__(self, relay: OutboxRelay, interval: float) -> None:
        self.relay = relay
        self.interval = interval
        self.drains = 0
        self.idle = asyncio.Event()  # set while sleeping between drains
        self._wake = asyncio.Event()
        self._stopping = False

    def kick(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    async def run(self) -> None:
        while not self._stopping:
            self._wake.clear()
            await self.relay.drain_until_idle()
            self.drains += 1
            self.idle.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.interval)
            self.idle.clear()


class OrderIn(BaseModel):
    customer_id: int
    total_cents: Annotated[int, Field(gt=0)]


class OrderCreated(BaseModel):
    order_id: int
    event_id: str


def create_app(sessions: async_sessionmaker[AsyncSession], poller: RelayPoller) -> FastAPI:
    app = FastAPI()

    @app.post("/orders", status_code=status.HTTP_201_CREATED)
    async def create_order(body: OrderIn, response: Response) -> OrderCreated:  # pyright: ignore[reportUnusedFunction]
        async with sessions() as session:
            try:
                order_id, event_id = await create_order_with_outbox(
                    session, body.customer_id, body.total_cents
                )
            except IntegrityError:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown customer"
                ) from None
        poller.kick()  # a hint only: losing it delays the event, never drops it
        response.headers["Location"] = f"/orders/{order_id}"
        return OrderCreated(order_id=order_id, event_id=event_id)

    return app
