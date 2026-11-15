"""Unit tests contrasting FastAPI in-process BackgroundTasks with durable task patterns."""

from typing import Any

import dramatiq
import httpx
import pytest
from dramatiq.brokers.stub import StubBroker

from api_labs.f11_fastapi_internals_and_patterns.background_tasks_vs_durable_queues import (
    OutboxRecord,
    OutboxRelayWorker,
    TransactionalDatabase,
    create_in_process_app,
    create_outbox_app,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_in_process_background_task_success() -> None:
    db = TransactionalDatabase()
    task_log: list[str] = []
    app = create_in_process_app(db=db, task_log=task_log)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/orders/in-process",
            json={"customer_id": "cust_101", "amount_cents": 5000},
        )

    assert response.status_code == 202
    data = response.json()
    order_id = data["order_id"]
    assert order_id in db.orders
    assert task_log == [f"task:email_sent:{order_id}"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_in_process_background_task_unhandled_exception_drops_and_short_circuits() -> None:
    """An unhandled exception in BackgroundTasks is swallowed after 202 Accepted.

    Subsequent tasks in the same BackgroundTasks container are short-circuited
    and permanently lost with zero retry.
    """
    db = TransactionalDatabase()
    task_log: list[str] = []
    app = create_in_process_app(db=db, task_log=task_log)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/orders/in-process-faulty",
            json={"customer_id": "cust_102", "amount_cents": 9900},
        )

    # Client was given a promise of acceptance (202 Accepted)
    assert response.status_code == 202
    data = response.json()
    order_id = data["order_id"]

    # Domain write succeeded
    assert order_id in db.orders

    # Task crashed after response; secondary task was never called
    assert task_log == [f"task:started:{order_id}"]
    # The task has no durability, no retry record, and no dead-letter storage
    assert len(db.outbox) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_in_process_response_background_override_silently_drops_injected_tasks() -> None:
    """FastAPI get_request_handler drops injected BackgroundTasks if Response has background."""
    db = TransactionalDatabase()
    task_log: list[str] = []
    app = create_in_process_app(db=db, task_log=task_log)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/orders/response-override",
            json={"customer_id": "cust_103", "amount_cents": 2500},
        )

    assert response.status_code == 202
    # Only the task attached to the Response object ran;
    # the injected BackgroundTasks instance was silently superseded.
    assert task_log == ["task:response_attached"]
    assert "task:dependency_injected" not in task_log


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transactional_outbox_atomic_success_and_relay() -> None:
    """Outbox records are committed atomically with domain state and dispatched reliably."""
    db = TransactionalDatabase()
    event_sink: list[dict[str, Any]] = []
    app = create_outbox_app(db=db)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/orders/outbox",
            json={"customer_id": "cust_201", "amount_cents": 12000},
        )

    assert response.status_code == 202
    data = response.json()
    order_id = data["order_id"]
    outbox_id = data["outbox_id"]

    # Both domain entity and outbox event exist in DB
    assert order_id in db.orders
    assert outbox_id in db.outbox
    assert db.outbox[outbox_id].status == "pending"

    # Relay worker runs out-of-band
    relay = OutboxRelayWorker(db=db, event_sink=event_sink)
    dispatched = relay.process_batch(batch_size=10)

    assert dispatched == 1
    assert db.outbox[outbox_id].status == "processed"
    assert db.outbox[outbox_id].processed_at is not None
    assert len(event_sink) == 1
    assert event_sink[0]["payload"]["order_id"] == order_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transactional_outbox_aborted_transaction_leaves_no_orphans() -> None:
    """When a transaction aborts, neither domain state nor outbox events persist."""
    db = TransactionalDatabase()
    app = create_outbox_app(db=db)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/orders/outbox-aborted",
            json={"customer_id": "cust_202", "amount_cents": 500, "force_abort": True},
        )

    assert response.status_code == 400
    # No orphan records created
    assert len(db.orders) == 0
    assert len(db.outbox) == 0


@pytest.mark.unit
def test_transactional_outbox_retry_on_worker_failure() -> None:
    """Relay worker retries failed outbox records up to maximum retry limit."""
    db = TransactionalDatabase()
    event_sink: list[dict[str, Any]] = []

    # Insert pending outbox record
    db.begin()
    record_id = "rec_fail_01"
    db.stage_outbox(
        OutboxRecord(
            id=record_id,
            event_type="order.payment_charge",
            payload={"charge_id": "ch_99"},
        )
    )
    db.commit()

    # Configure relay worker to fail once
    relay = OutboxRelayWorker(db=db, event_sink=event_sink, flaky_failures=1)

    # First attempt: worker fails
    first_pass = relay.process_batch()
    assert first_pass == 0
    assert db.outbox[record_id].status == "failed"
    assert db.outbox[record_id].attempts == 1
    assert db.outbox[record_id].last_error == "Transient transport timeout"
    assert len(event_sink) == 0

    # Second attempt: worker succeeds
    second_pass = relay.process_batch()
    assert second_pass == 1
    assert db.outbox[record_id].status == "processed"
    assert db.outbox[record_id].attempts == 2
    assert len(event_sink) == 1


@pytest.mark.unit
def test_dramatiq_stub_broker_durable_delivery_and_ack() -> None:
    """Dramatiq StubBroker provides deterministic in-memory worker testing with ack."""
    broker = StubBroker()
    dramatiq.set_broker(broker)

    messages_processed: list[str] = []

    @dramatiq.actor
    def process_invoice(invoice_id: str) -> None:
        messages_processed.append(invoice_id)

    worker = dramatiq.Worker(broker, worker_timeout=100)
    worker.start()

    # Enqueue message
    process_invoice.send("inv_5001")

    # Wait for message to be processed by running worker
    broker.join(process_invoice.queue_name, timeout=2000)
    worker.stop()

    assert messages_processed == ["inv_5001"]
