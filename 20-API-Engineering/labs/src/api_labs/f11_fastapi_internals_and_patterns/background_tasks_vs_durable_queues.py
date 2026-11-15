"""In-process BackgroundTasks vs durable transactional outbox task execution."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response


@dataclass
class Order:
    """Domain entity representing a placed customer order."""

    id: str
    customer_id: str
    amount_cents: int
    status: str = "created"


@dataclass
class OutboxRecord:
    """Durable outbox entry persisted in the same ACID transaction as domain state."""

    id: str
    event_type: str
    payload: dict[str, Any]
    status: str = "pending"  # "pending", "processing", "processed", "failed"
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    processed_at: float | None = None
    last_error: str | None = None


class TransactionalDatabase:
    """In-memory transactional database simulating ACID properties and row-level locks."""

    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        self.outbox: dict[str, OutboxRecord] = {}
        self._staged_orders: dict[str, Order] = {}
        self._staged_outbox: dict[str, OutboxRecord] = {}
        self._in_transaction: bool = False

    def begin(self) -> None:
        if self._in_transaction:
            raise RuntimeError("Transaction already active.")
        self._in_transaction = True
        self._staged_orders = {}
        self._staged_outbox = {}

    def stage_order(self, order: Order) -> None:
        if not self._in_transaction:
            raise RuntimeError("No active transaction.")
        self._staged_orders[order.id] = order

    def stage_outbox(self, record: OutboxRecord) -> None:
        if not self._in_transaction:
            raise RuntimeError("No active transaction.")
        self._staged_outbox[record.id] = record

    def commit(self) -> None:
        if not self._in_transaction:
            raise RuntimeError("No active transaction.")
        self.orders.update(self._staged_orders)
        self.outbox.update(self._staged_outbox)
        self._staged_orders.clear()
        self._staged_outbox.clear()
        self._in_transaction = False

    def rollback(self) -> None:
        if not self._in_transaction:
            raise RuntimeError("No active transaction.")
        self._staged_orders.clear()
        self._staged_outbox.clear()
        self._in_transaction = False

    def claim_pending_outbox(self, batch_size: int = 10) -> list[OutboxRecord]:
        """Simulate SELECT ... FOR UPDATE SKIP LOCKED over pending outbox rows."""
        claimed: list[OutboxRecord] = []
        for record in self.outbox.values():
            if record.status in ("pending", "failed") and record.attempts < 3:
                record.status = "processing"
                record.attempts += 1
                claimed.append(record)
                if len(claimed) >= batch_size:
                    break
        return claimed

    def mark_processed(self, record_id: str) -> None:
        if record_id in self.outbox:
            record = self.outbox[record_id]
            record.status = "processed"
            record.processed_at = time.time()
            record.last_error = None

    def mark_failed(self, record_id: str, error: str) -> None:
        if record_id in self.outbox:
            record = self.outbox[record_id]
            record.status = "failed"
            record.last_error = error


class OutboxRelayWorker:
    """Relay worker that dispatches outbox events with at-least-once delivery."""

    def __init__(
        self,
        db: TransactionalDatabase,
        event_sink: list[dict[str, Any]],
        flaky_failures: int = 0,
    ) -> None:
        self.db = db
        self.event_sink = event_sink
        self.flaky_failures = flaky_failures
        self.failure_count = 0

    def process_batch(self, batch_size: int = 10) -> int:
        records = self.db.claim_pending_outbox(batch_size=batch_size)
        processed_count = 0
        for record in records:
            if self.failure_count < self.flaky_failures:
                self.failure_count += 1
                self.db.mark_failed(record.id, error="Transient transport timeout")
                continue

            self.event_sink.append(
                {
                    "event_id": record.id,
                    "event_type": record.event_type,
                    "payload": record.payload,
                    "attempts": record.attempts,
                }
            )
            self.db.mark_processed(record.id)
            processed_count += 1
        return processed_count


def create_in_process_app(
    db: TransactionalDatabase,
    task_log: list[str],
) -> FastAPI:
    """Build a FastAPI app using in-process BackgroundTasks."""
    app = FastAPI()

    @app.post("/orders/in-process", status_code=202)
    async def place_order_in_process(
        payload: dict[str, Any],
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        order_id = str(uuid.uuid4())
        db.begin()
        order = Order(
            id=order_id,
            customer_id=payload["customer_id"],
            amount_cents=payload["amount_cents"],
        )
        db.stage_order(order)
        db.commit()

        def async_email_task(oid: str) -> None:
            task_log.append(f"task:email_sent:{oid}")

        background_tasks.add_task(async_email_task, order_id)
        return {"status": "accepted", "order_id": order_id}

    @app.post("/orders/in-process-faulty", status_code=202)
    async def place_order_faulty(
        payload: dict[str, Any],
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        order_id = str(uuid.uuid4())
        db.begin()
        order = Order(
            id=order_id,
            customer_id=payload["customer_id"],
            amount_cents=payload["amount_cents"],
        )
        db.stage_order(order)
        db.commit()

        def crashing_task(oid: str) -> None:
            task_log.append(f"task:started:{oid}")
            raise RuntimeError(f"Unrecoverable socket reset for {oid}")

        def secondary_task(oid: str) -> None:
            task_log.append(f"task:secondary_sent:{oid}")

        # The first task crashes; BackgroundTasks executes sequentially,
        # so secondary_task is short-circuited and lost.
        background_tasks.add_task(crashing_task, order_id)
        background_tasks.add_task(secondary_task, order_id)
        return {"status": "accepted", "order_id": order_id}

    @app.post("/orders/response-override", status_code=202)
    async def place_order_response_override(
        payload: dict[str, Any],
        background_tasks: BackgroundTasks,
    ) -> Response:
        order_id = str(uuid.uuid4())
        db.begin()
        order = Order(
            id=order_id,
            customer_id=payload["customer_id"],
            amount_cents=payload["amount_cents"],
        )
        db.stage_order(order)
        db.commit()

        def dependency_task() -> None:
            task_log.append("task:dependency_injected")

        def response_task() -> None:
            task_log.append("task:response_attached")

        background_tasks.add_task(dependency_task)

        # Bug demonstration: Returning a Response with its own background task
        # causes FastAPI to silently drop the injected background_tasks!
        return JSONResponse(
            status_code=202,
            content={"status": "accepted", "order_id": order_id},
            background=BackgroundTask(response_task),
        )

    return app


def create_outbox_app(
    db: TransactionalDatabase,
) -> FastAPI:
    """Build a FastAPI app using the Transactional Outbox pattern."""
    app = FastAPI()

    @app.post("/orders/outbox", status_code=202)
    async def place_order_outbox(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        order_id = str(uuid.uuid4())
        outbox_id = str(uuid.uuid4())

        db.begin()
        try:
            order = Order(
                id=order_id,
                customer_id=payload["customer_id"],
                amount_cents=payload["amount_cents"],
            )
            db.stage_order(order)

            outbox_entry = OutboxRecord(
                id=outbox_id,
                event_type="order.created",
                payload={"order_id": order_id, "amount_cents": order.amount_cents},
            )
            db.stage_outbox(outbox_entry)
            db.commit()
        except Exception as exc:
            db.rollback()
            raise HTTPException(status_code=500, detail="Transaction aborted") from exc

        return {"status": "accepted", "order_id": order_id, "outbox_id": outbox_id}

    @app.post("/orders/outbox-aborted", status_code=202)
    async def place_order_aborted(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        order_id = str(uuid.uuid4())
        outbox_id = str(uuid.uuid4())

        db.begin()
        try:
            order = Order(
                id=order_id,
                customer_id=payload["customer_id"],
                amount_cents=payload["amount_cents"],
            )
            db.stage_order(order)

            outbox_entry = OutboxRecord(
                id=outbox_id,
                event_type="order.created",
                payload={"order_id": order_id},
            )
            db.stage_outbox(outbox_entry)

            # Simulate database constraint violation or business rule exception
            if payload.get("force_abort"):
                raise ValueError("Credit limit exceeded; aborting transaction")

            db.commit()
        except Exception as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {"status": "accepted", "order_id": order_id, "outbox_id": outbox_id}

    return app
