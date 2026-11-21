"""Unit tests for multi-phase idempotency with durable recovery points.

Proves state transitions, crash recovery across network drops, payload fingerprinting,
lease-based concurrency fencing, and ASGI error contracts.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from typing import TYPE_CHECKING

import aiosqlite
import httpx
import pytest

from api_labs.f16_resilience_rate_limiting_and_idempotency.multi_phase_idempotency import (
    ConcurrentRequestConflictError,
    ExternalGatewayNetworkDrop,
    IdempotencyPhase,
    MockExternalPaymentGateway,
    MultiPhasePaymentCoordinator,
    PayloadMismatchError,
    ZombieWorkerFencedError,
    create_idempotent_app,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def sqlite_db() -> Iterator[str]:
    """Provides a clean temporary SQLite database for hermetic unit testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        yield path
    finally:
        if os.path.exists(path):
            os.remove(path)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_happy_path_initial_execution_and_replay(sqlite_db: str) -> None:
    """Proves end-to-end execution across all phases and exact replay on retry."""
    gateway = MockExternalPaymentGateway()
    coordinator = MultiPhasePaymentCoordinator(sqlite_db, gateway, lease_timeout_seconds=5.0)
    await coordinator.init_schema()

    key = str(uuid.uuid4())
    payload = {"account_id": "acct_100", "amount": 5000, "currency": "USD"}

    # First attempt: complete execution
    status_1, data_1 = await coordinator.execute_payment(
        idempotency_key=key,
        scope="tenant_1",
        method="POST",
        path="/v1/payments",
        payload=payload,
        worker_id="worker_alpha",
    )

    assert status_1 == 201
    assert data_1["status"] == "paid"
    assert data_1["amount"] == 5000
    assert data_1["charge_id"].startswith("ch_")
    assert gateway.call_count == 1
    assert len(gateway.charges_by_key) == 1

    # Second attempt: exact replay from recovery point
    status_2, data_2 = await coordinator.execute_payment(
        idempotency_key=key,
        scope="tenant_1",
        method="POST",
        path="/v1/payments",
        payload=payload,
        worker_id="worker_beta",
    )

    assert status_2 == 201
    assert data_2 == data_1
    # External gateway was NOT called again
    assert gateway.call_count == 1

    # Verify database state
    async with aiosqlite.connect(sqlite_db) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM idempotency_keys WHERE idempotency_key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            assert row is not None
            assert row["phase"] == IdempotencyPhase.FINALIZED.value
            assert row["response_status"] == 201

        async with db.execute(
            "SELECT count(*) as c FROM ledger_reservations WHERE idempotency_key = ?", (key,)
        ) as cur:
            res = await cur.fetchone()
            assert res is not None
            assert res["c"] == 1

        async with db.execute(
            "SELECT count(*) as c FROM orders WHERE idempotency_key = ?", (key,)
        ) as cur:
            res = await cur.fetchone()
            assert res is not None
            assert res["c"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_in_flight_request_returns_409(sqlite_db: str) -> None:
    """Proves that a concurrent request for an in-flight key is rejected with conflict."""
    gateway = MockExternalPaymentGateway()
    coordinator = MultiPhasePaymentCoordinator(sqlite_db, gateway, lease_timeout_seconds=10.0)
    await coordinator.init_schema()

    key = str(uuid.uuid4())
    payload = {"account_id": "acct_200", "amount": 2500, "currency": "USD"}

    # Simulate in-flight worker holding active lease at phase STARTED
    with pytest.raises(RuntimeError, match="Simulated crash: worker died after phase STARTED"):
        await coordinator.execute_payment(
            idempotency_key=key,
            scope="tenant_1",
            method="POST",
            path="/v1/payments",
            payload=payload,
            worker_id="worker_original",
            crash_at="after_started",
        )

    # Immediate second worker tries to execute while lease is still unexpired
    with pytest.raises(ConcurrentRequestConflictError, match="actively being processed"):
        await coordinator.execute_payment(
            idempotency_key=key,
            scope="tenant_1",
            method="POST",
            path="/v1/payments",
            payload=payload,
            worker_id="worker_concurrent",
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_payload_tampering_returns_422(sqlite_db: str) -> None:
    """Proves that mutating the payload across retries triggers 422 Unprocessable Content."""
    gateway = MockExternalPaymentGateway()
    coordinator = MultiPhasePaymentCoordinator(sqlite_db, gateway, lease_timeout_seconds=5.0)
    await coordinator.init_schema()

    key = str(uuid.uuid4())
    payload_1 = {"account_id": "acct_300", "amount": 1000, "currency": "USD"}
    payload_2 = {"account_id": "acct_300", "amount": 9999, "currency": "USD"}

    # Original request commits
    await coordinator.execute_payment(
        idempotency_key=key,
        scope="tenant_1",
        method="POST",
        path="/v1/payments",
        payload=payload_1,
        worker_id="worker_1",
    )

    # Replay with modified amount
    with pytest.raises(PayloadMismatchError, match="fingerprint does not match"):
        await coordinator.execute_payment(
            idempotency_key=key,
            scope="tenant_1",
            method="POST",
            path="/v1/payments",
            payload=payload_2,
            worker_id="worker_2",
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crash_after_reservation_resumes_cleanly(sqlite_db: str) -> None:
    """Proves forward recovery when process crashes right after ledger hold is committed."""
    gateway = MockExternalPaymentGateway()
    # Short lease of 0.1s to allow fast retry without waiting long
    coordinator = MultiPhasePaymentCoordinator(sqlite_db, gateway, lease_timeout_seconds=0.1)
    await coordinator.init_schema()

    key = str(uuid.uuid4())
    payload = {"account_id": "acct_400", "amount": 7500, "currency": "USD"}

    # Attempt 1: crashes after RESERVED phase committed to DB
    with pytest.raises(RuntimeError, match="Simulated crash: worker died after phase RESERVED"):
        await coordinator.execute_payment(
            idempotency_key=key,
            scope="tenant_1",
            method="POST",
            path="/v1/payments",
            payload=payload,
            worker_id="worker_dying",
            crash_at="after_reserved",
        )

    # Gateway has NOT been called yet
    assert gateway.call_count == 0

    # Let lease expire
    await asyncio.sleep(0.12)

    # Attempt 2: retry worker picks up recovery point
    status, data = await coordinator.execute_payment(
        idempotency_key=key,
        scope="tenant_1",
        method="POST",
        path="/v1/payments",
        payload=payload,
        worker_id="worker_recovery",
    )

    assert status == 201
    assert data["status"] == "paid"
    assert gateway.call_count == 1
    assert len(gateway.charges_by_key) == 1

    # Verify ledger reservations table has only ONE entry, not two
    async with (
        aiosqlite.connect(sqlite_db) as db,
        db.execute(
            "SELECT count(*) FROM ledger_reservations WHERE idempotency_key = ?", (key,)
        ) as cur,
    ):
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crash_during_gateway_network_drop_recovers_without_double_charge(
    sqlite_db: str,
) -> None:
    """Proves that a network drop during gateway RPC does NOT produce a double charge.

    The gateway committed the charge, but the network dropped before the caller
    received the response. Upon retry, the coordinator uses the same deterministic
    downstream key to retrieve the existing charge.
    """
    gateway = MockExternalPaymentGateway()
    coordinator = MultiPhasePaymentCoordinator(sqlite_db, gateway, lease_timeout_seconds=0.1)
    await coordinator.init_schema()

    key = str(uuid.uuid4())
    payload = {"account_id": "acct_500", "amount": 12000, "currency": "USD"}

    # Attempt 1: Network drop during gateway RPC
    with pytest.raises(ExternalGatewayNetworkDrop, match="severed before response packet"):
        await coordinator.execute_payment(
            idempotency_key=key,
            scope="tenant_1",
            method="POST",
            path="/v1/payments",
            payload=payload,
            worker_id="worker_dropped",
            crash_at="during_gateway_drop",
        )

    # Gateway DID process the charge, but worker never received ACK or committed EXTERNAL_EXECUTED
    assert gateway.call_count == 1
    assert len(gateway.charges_by_key) == 1

    # Verify phase in DB is still RESERVED
    async with aiosqlite.connect(sqlite_db) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT phase FROM idempotency_keys WHERE idempotency_key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            assert row is not None
            assert row["phase"] == IdempotencyPhase.RESERVED.value

    # Wait for lease expiration
    await asyncio.sleep(0.12)

    # Attempt 2: Recovery worker executes retry
    status, data = await coordinator.execute_payment(
        idempotency_key=key,
        scope="tenant_1",
        method="POST",
        path="/v1/payments",
        payload=payload,
        worker_id="worker_recovery",
    )

    assert status == 201
    assert data["status"] == "paid"
    # Gateway was invoked a second time, but returned existing charge!
    assert gateway.call_count == 2
    assert len(gateway.charges_by_key) == 1  # Exactly ONE unique charge created!


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crash_after_external_executed_resumes_to_finalized(sqlite_db: str) -> None:
    """Proves forward recovery when crash occurs after gateway confirmation is persisted."""
    gateway = MockExternalPaymentGateway()
    coordinator = MultiPhasePaymentCoordinator(sqlite_db, gateway, lease_timeout_seconds=0.1)
    await coordinator.init_schema()

    key = str(uuid.uuid4())
    payload = {"account_id": "acct_600", "amount": 3400, "currency": "USD"}

    # Attempt 1: crashes after EXTERNAL_EXECUTED committed
    with pytest.raises(
        RuntimeError, match="Simulated crash: worker died after phase EXTERNAL_EXECUTED"
    ):
        await coordinator.execute_payment(
            idempotency_key=key,
            scope="tenant_1",
            method="POST",
            path="/v1/payments",
            payload=payload,
            worker_id="worker_1",
            crash_at="after_external_executed",
        )

    assert gateway.call_count == 1

    # Wait for lease to expire
    await asyncio.sleep(0.12)

    # Attempt 2: retry worker resumes straight from EXTERNAL_EXECUTED to FINALIZED
    status, data = await coordinator.execute_payment(
        idempotency_key=key,
        scope="tenant_1",
        method="POST",
        path="/v1/payments",
        payload=payload,
        worker_id="worker_2",
    )

    assert status == 201
    assert data["status"] == "paid"
    # Gateway was NOT called again because phase was already EXTERNAL_EXECUTED
    assert gateway.call_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zombie_worker_fenced_out_by_epoch(sqlite_db: str) -> None:
    """Proves a stalled worker whose lease was reclaimed is fenced by the coordinator itself."""
    gateway = MockExternalPaymentGateway()
    coordinator = MultiPhasePaymentCoordinator(sqlite_db, gateway, lease_timeout_seconds=0.1)
    await coordinator.init_schema()

    key = str(uuid.uuid4())
    payload = {"account_id": "acct_700", "amount": 4200, "currency": "USD"}

    reached = asyncio.Event()
    release = asyncio.Event()

    async def stall_at_started(phase: IdempotencyPhase) -> None:
        if phase == IdempotencyPhase.STARTED:
            reached.set()
            await release.wait()

    # Worker 1 holds epoch 1 and stalls (a GC pause or a frozen event loop) before reserving.
    zombie = asyncio.create_task(
        coordinator.execute_payment(
            idempotency_key=key,
            scope="tenant_1",
            method="POST",
            path="/v1/payments",
            payload=payload,
            worker_id="worker_zombie",
            phase_hook=stall_at_started,
        )
    )
    await asyncio.wait_for(reached.wait(), timeout=5)

    # The lease expires; worker 2 reclaims (epoch 2) and finishes the workflow.
    await asyncio.sleep(0.12)
    status, _data = await coordinator.execute_payment(
        idempotency_key=key,
        scope="tenant_1",
        method="POST",
        path="/v1/payments",
        payload=payload,
        worker_id="worker_active",
    )
    assert status == 201

    # Worker 1 wakes up with the stale epoch 1: its first write must match zero rows.
    release.set()
    with pytest.raises(ZombieWorkerFencedError, match="superseded during reservation"):
        await asyncio.wait_for(zombie, timeout=5)

    async with aiosqlite.connect(sqlite_db) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT phase, fencing_epoch FROM idempotency_keys WHERE idempotency_key = ?",
            (key,),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["phase"] == IdempotencyPhase.FINALIZED.value
        assert row["fencing_epoch"] == 2
        for table in ("ledger_reservations", "orders"):
            async with db.execute(
                f"SELECT count(*) FROM {table} WHERE idempotency_key = ?",  # noqa: S608
                (key,),
            ) as cur:
                count_row = await cur.fetchone()
            assert count_row is not None
            assert count_row[0] == 1
    assert gateway.call_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_first_requests_admit_exactly_one_worker(sqlite_db: str) -> None:
    """Proves atomic admission: eight simultaneous first requests yield one charge, no 500."""
    gateway = MockExternalPaymentGateway()
    coordinator = MultiPhasePaymentCoordinator(sqlite_db, gateway, lease_timeout_seconds=10.0)
    await coordinator.init_schema()

    key = str(uuid.uuid4())
    payload = {"account_id": "acct_900", "amount": 1500, "currency": "USD"}

    results = await asyncio.gather(
        *(
            coordinator.execute_payment(
                idempotency_key=key,
                scope="tenant_1",
                method="POST",
                path="/v1/payments",
                payload=payload,
                worker_id=f"worker_{i}",
            )
            for i in range(8)
        ),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, tuple)]
    conflicts = [r for r in results if isinstance(r, ConcurrentRequestConflictError)]
    # No IntegrityError or OperationalError may leak out of admission.
    assert len(successes) + len(conflicts) == len(results)
    assert len(successes) >= 1
    assert gateway.call_count == 1
    assert len(gateway.charges_by_key) == 1

    async with aiosqlite.connect(sqlite_db) as db:
        async with db.execute("SELECT count(*) FROM idempotency_keys") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1
        async with db.execute("SELECT count(*) FROM orders") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_reclaimers_share_one_epoch_winner(sqlite_db: str) -> None:
    """Proves two retries that both see an expired lease cannot both claim epoch 2."""
    gateway = MockExternalPaymentGateway()
    coordinator = MultiPhasePaymentCoordinator(sqlite_db, gateway, lease_timeout_seconds=0.1)
    await coordinator.init_schema()

    key = str(uuid.uuid4())
    payload = {"account_id": "acct_950", "amount": 2200, "currency": "USD"}

    with pytest.raises(RuntimeError, match="Simulated crash"):
        await coordinator.execute_payment(
            idempotency_key=key,
            scope="tenant_1",
            method="POST",
            path="/v1/payments",
            payload=payload,
            worker_id="worker_dying",
            crash_at="after_reserved",
        )
    await asyncio.sleep(0.12)

    results = await asyncio.gather(
        *(
            coordinator.execute_payment(
                idempotency_key=key,
                scope="tenant_1",
                method="POST",
                path="/v1/payments",
                payload=payload,
                worker_id=f"worker_retry_{i}",
            )
            for i in range(2)
        ),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, tuple)]
    conflicts = [r for r in results if isinstance(r, ConcurrentRequestConflictError)]
    assert len(successes) + len(conflicts) == len(results)
    assert len(successes) >= 1
    assert gateway.call_count == 1

    async with aiosqlite.connect(sqlite_db) as db:
        async with db.execute(
            "SELECT fencing_epoch FROM idempotency_keys WHERE idempotency_key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_http_contracts_and_problem_details(sqlite_db: str) -> None:
    """Proves RFC 9457 Problem Details and HTTP status codes via in-process ASGI client."""
    gateway = MockExternalPaymentGateway()
    coordinator = MultiPhasePaymentCoordinator(sqlite_db, gateway, lease_timeout_seconds=5.0)
    await coordinator.init_schema()

    app = create_idempotent_app(coordinator)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. Missing header -> 400 Bad Request
        resp_missing = await client.post("/v1/payments", json={"amount": 100})
        assert resp_missing.status_code == 400
        assert resp_missing.headers["content-type"] == "application/problem+json"
        assert resp_missing.json()["title"] == "Idempotency-Key is missing"

        # 1b. Malformed or non-object body -> 400 before admission
        resp_malformed = await client.post(
            "/v1/payments",
            headers={"Idempotency-Key": str(uuid.uuid4()), "Content-Type": "application/json"},
            content=b"{not json",
        )
        assert resp_malformed.status_code == 400
        assert resp_malformed.headers["content-type"] == "application/problem+json"
        assert resp_malformed.json()["title"] == "Request body must be a JSON object"

        # 2. Success -> 201 Created
        key = str(uuid.uuid4())
        resp_ok = await client.post(
            "/v1/payments",
            headers={"Idempotency-Key": key},
            json={"account_id": "acct_800", "amount": 8000, "currency": "USD"},
        )
        assert resp_ok.status_code == 201
        assert resp_ok.headers["Idempotency-Key"] == key
        assert resp_ok.json()["status"] == "paid"

        # 3. Replay -> 201 Created exact replay
        resp_replay = await client.post(
            "/v1/payments",
            headers={"Idempotency-Key": key},
            json={"account_id": "acct_800", "amount": 8000, "currency": "USD"},
        )
        assert resp_replay.status_code == 201
        assert resp_replay.json() == resp_ok.json()

        # 4. Tampered payload -> 422 Unprocessable Content
        resp_tampered = await client.post(
            "/v1/payments",
            headers={"Idempotency-Key": key},
            json={"account_id": "acct_800", "amount": 99999, "currency": "USD"},
        )
        assert resp_tampered.status_code == 422
        assert resp_tampered.headers["content-type"] == "application/problem+json"
        assert resp_tampered.json()["title"] == "Idempotency-Key is already used"
