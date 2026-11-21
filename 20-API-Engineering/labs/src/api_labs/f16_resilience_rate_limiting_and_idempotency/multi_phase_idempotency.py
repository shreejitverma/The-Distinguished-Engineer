"""Multi-phase idempotency state machine with durable recovery points.

Handles process crashes and network partitions between external side effects
(e.g., third-party payment gateways) and local database transactions.
Partially implements the IETF draft-ietf-httpapi-idempotency-key-header-08
specification (no Structured Field parsing) with deterministic forward recovery
and lease-based fencing tokens.
"""

from __future__ import annotations

import enum
import hashlib
import json
import time
import uuid
from typing import TYPE_CHECKING, Any, cast

import aiosqlite
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request


class IdempotencyPhase(enum.StrEnum):
    """Execution phases in the multi-phase idempotency state machine."""

    STARTED = "STARTED"
    RESERVED = "RESERVED"
    EXTERNAL_EXECUTED = "EXTERNAL_EXECUTED"
    FINALIZED = "FINALIZED"


class IdempotencyError(Exception):
    """Base exception for idempotency coordination failures."""


class MissingIdempotencyKeyError(IdempotencyError):
    """Raised when an operation requires an Idempotency-Key header but none is present."""


class PayloadMismatchError(IdempotencyError):
    """Raised when an idempotency key is reused with a mutated request payload."""


class ConcurrentRequestConflictError(IdempotencyError):
    """Raised when a concurrent request attempts to execute an already locked in-flight key."""


class ZombieWorkerFencedError(IdempotencyError):
    """Raised when a worker whose lease expired attempts to commit a stale phase."""


class ExternalGatewayNetworkDrop(IdempotencyError):
    """Simulates an unacknowledged network drop during an external RPC."""


class ExternalPaymentDeclinedError(IdempotencyError):
    """Terminal business failure from the payment processor."""


def compute_request_fingerprint(method: str, path: str, payload_bytes: bytes) -> str:
    """Compute a canonical SHA-256 fingerprint over method, path, and body.

    Draft-08 Section 2.4 only says a fingerprint MAY be generated from request
    payload data; including the method and path is this lab's own choice.
    """
    body_digest = hashlib.sha256(payload_bytes).hexdigest()
    canonical_token = f"{method.upper()}|{path}|{body_digest}"
    return hashlib.sha256(canonical_token.encode("utf-8")).hexdigest()


def derive_downstream_key(upstream_key: str, step_name: str) -> str:
    """Deterministically derive a secondary idempotency key for downstream RPCs.

    Ensures that retried requests present the exact same idempotency key to external
    third parties (e.g. Stripe, Adyen) without generating key collisions.
    """
    namespace = uuid.NAMESPACE_URL
    name = f"urn:idempotency:{upstream_key}:{step_name}"
    return str(uuid.uuid5(namespace, name))


class GatewayChargeRecord:
    """Immutable record of an executed charge on the external gateway."""

    def __init__(
        self,
        charge_id: str,
        amount: int,
        currency: str,
        status: str,
        idempotency_key: str,
    ) -> None:
        self.charge_id = charge_id
        self.amount = amount
        self.currency = currency
        self.status = status
        self.idempotency_key = idempotency_key


class MockExternalPaymentGateway:
    """Simulated external gateway tracking executed charges idempotently."""

    def __init__(self) -> None:
        self.charges_by_key: dict[str, GatewayChargeRecord] = {}
        self.call_count: int = 0

    async def charge(
        self,
        downstream_key: str,
        amount: int,
        currency: str,
        crash_network_drop: bool = False,
        decline: bool = False,
    ) -> GatewayChargeRecord:
        self.call_count += 1

        if downstream_key in self.charges_by_key:
            return self.charges_by_key[downstream_key]

        if decline:
            raise ExternalPaymentDeclinedError("Card declined: insufficient funds")

        record = GatewayChargeRecord(
            charge_id=f"ch_{uuid.uuid4().hex[:16]}",
            amount=amount,
            currency=currency,
            status="succeeded",
            idempotency_key=downstream_key,
        )
        self.charges_by_key[downstream_key] = record

        if crash_network_drop:
            raise ExternalGatewayNetworkDrop(
                "Network connection severed before response packet arrived at caller"
            )

        return record


class MultiPhasePaymentCoordinator:
    """Coordinates multi-phase payment execution across local DB and external RPCs."""

    def __init__(
        self,
        db_path: str,
        gateway: MockExternalPaymentGateway,
        lease_timeout_seconds: float = 2.0,
    ) -> None:
        self.db_path = db_path
        self.gateway = gateway
        self.lease_timeout_seconds = lease_timeout_seconds

    async def init_schema(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode = WAL;")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    locked_at REAL,
                    lease_timeout_seconds REAL NOT NULL,
                    worker_id TEXT,
                    fencing_epoch INTEGER NOT NULL DEFAULT 1,
                    downstream_key TEXT NOT NULL,
                    external_reference_id TEXT,
                    response_status INTEGER,
                    response_body TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(scope, idempotency_key)
                );
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(idempotency_key)
                );
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    external_charge_id TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(idempotency_key)
                );
                """
            )
            await db.commit()

    async def execute_payment(
        self,
        idempotency_key: str,
        scope: str,
        method: str,
        path: str,
        payload: dict[str, Any],
        worker_id: str,
        crash_at: str | None = None,
        phase_hook: Callable[[IdempotencyPhase], Awaitable[None]] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if not idempotency_key:
            raise MissingIdempotencyKeyError("Idempotency-Key header is required")

        payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
        request_hash = compute_request_fingerprint(method, path, payload_bytes)
        now = time.time()
        downstream_key = derive_downstream_key(idempotency_key, "payment_charge")

        # Step 0: Admission and lock acquisition.
        # The INSERT ... ON CONFLICT DO NOTHING is one atomic statement, so exactly one of
        # several concurrent first requests inserts the row; the others read it back.
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            inserted = await db.execute(
                """
                INSERT INTO idempotency_keys (
                    idempotency_key, scope, request_hash, phase, locked_at,
                    lease_timeout_seconds, worker_id, fencing_epoch,
                    downstream_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT (scope, idempotency_key) DO NOTHING
                """,
                (
                    idempotency_key,
                    scope,
                    request_hash,
                    IdempotencyPhase.STARTED.value,
                    now,
                    self.lease_timeout_seconds,
                    worker_id,
                    downstream_key,
                    now,
                    now,
                ),
            )
            await db.commit()

            if inserted.rowcount == 1:
                current_phase = IdempotencyPhase.STARTED
                current_epoch = 1
                external_ref = None
            else:
                async with db.execute(
                    "SELECT * FROM idempotency_keys WHERE scope = ? AND idempotency_key = ?",
                    (scope, idempotency_key),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    msg = "Idempotency row vanished after a conflicting insert"
                    raise RuntimeError(msg)

                if row["request_hash"] != request_hash:
                    msg = "Idempotency-Key payload mismatch: fingerprint does not match original"
                    raise PayloadMismatchError(msg)

                if row["phase"] == IdempotencyPhase.FINALIZED.value:
                    cached_body = json.loads(row["response_body"])
                    return int(row["response_status"]), cached_body

                locked_at = row["locked_at"]
                lease_ttl = float(row["lease_timeout_seconds"])
                if locked_at is not None and (now - float(locked_at)) < lease_ttl:
                    msg = "A request with the same Idempotency-Key is actively being processed"
                    raise ConcurrentRequestConflictError(msg)

                # Conditional reclaim: only the retry that still sees the epoch it read and
                # an expired lease wins; a concurrent reclaimer matches zero rows.
                seen_epoch = int(row["fencing_epoch"])
                new_epoch = seen_epoch + 1
                reclaimed = await db.execute(
                    """
                    UPDATE idempotency_keys
                    SET locked_at = ?, worker_id = ?, fencing_epoch = ?, updated_at = ?
                    WHERE scope = ? AND idempotency_key = ? AND fencing_epoch = ?
                      AND phase != ?
                      AND (locked_at IS NULL OR ? - locked_at >= lease_timeout_seconds)
                    """,
                    (
                        now,
                        worker_id,
                        new_epoch,
                        now,
                        scope,
                        idempotency_key,
                        seen_epoch,
                        IdempotencyPhase.FINALIZED.value,
                        now,
                    ),
                )
                await db.commit()
                if reclaimed.rowcount == 0:
                    msg = "Another worker reclaimed or finalized this Idempotency-Key first"
                    raise ConcurrentRequestConflictError(msg)
                current_phase = IdempotencyPhase(row["phase"])
                current_epoch = new_epoch
                external_ref = row["external_reference_id"]
                downstream_key = row["downstream_key"]

        account_id = str(payload.get("account_id", "default_account"))
        amount = int(payload.get("amount", 0))
        currency = str(payload.get("currency", "USD"))

        # State machine forward recovery loop
        while current_phase != IdempotencyPhase.FINALIZED:
            if phase_hook is not None:
                await phase_hook(current_phase)

            if current_phase == IdempotencyPhase.STARTED:
                if crash_at == "after_started":
                    msg = "Simulated crash: worker died after phase STARTED"
                    raise RuntimeError(msg)

                async with aiosqlite.connect(self.db_path) as db:
                    cursor = await db.execute(
                        """
                        UPDATE idempotency_keys
                        SET phase = ?, updated_at = ?
                        WHERE scope = ? AND idempotency_key = ? AND fencing_epoch = ?
                        """,
                        (
                            IdempotencyPhase.RESERVED.value,
                            time.time(),
                            scope,
                            idempotency_key,
                            current_epoch,
                        ),
                    )
                    if cursor.rowcount == 0:
                        msg = "Worker epoch superseded during reservation"
                        raise ZombieWorkerFencedError(msg)

                    await db.execute(
                        """
                        INSERT INTO ledger_reservations (
                            idempotency_key, account_id, amount, currency, status, created_at
                        ) VALUES (?, ?, ?, ?, 'PENDING_HOLD', ?)
                        """,
                        (idempotency_key, account_id, amount, currency, time.time()),
                    )
                    await db.commit()
                current_phase = IdempotencyPhase.RESERVED

            elif current_phase == IdempotencyPhase.RESERVED:
                if crash_at == "after_reserved":
                    msg = "Simulated crash: worker died after phase RESERVED"
                    raise RuntimeError(msg)

                network_drop = crash_at == "during_gateway_drop"
                charge_record = await self.gateway.charge(
                    downstream_key=downstream_key,
                    amount=amount,
                    currency=currency,
                    crash_network_drop=network_drop,
                )
                external_ref = charge_record.charge_id

                async with aiosqlite.connect(self.db_path) as db:
                    cursor = await db.execute(
                        """
                        UPDATE idempotency_keys
                        SET phase = ?, external_reference_id = ?, updated_at = ?
                        WHERE scope = ? AND idempotency_key = ? AND fencing_epoch = ?
                        """,
                        (
                            IdempotencyPhase.EXTERNAL_EXECUTED.value,
                            external_ref,
                            time.time(),
                            scope,
                            idempotency_key,
                            current_epoch,
                        ),
                    )
                    if cursor.rowcount == 0:
                        msg = "Worker epoch superseded during external confirmation"
                        raise ZombieWorkerFencedError(msg)
                    await db.commit()
                current_phase = IdempotencyPhase.EXTERNAL_EXECUTED

            elif current_phase == IdempotencyPhase.EXTERNAL_EXECUTED:
                if crash_at == "after_external_executed":
                    msg = "Simulated crash: worker died after phase EXTERNAL_EXECUTED"
                    raise RuntimeError(msg)

                order_id = f"ord_{idempotency_key[:12]}"
                final_response = {
                    "order_id": order_id,
                    "account_id": account_id,
                    "amount": amount,
                    "currency": currency,
                    "charge_id": external_ref,
                    "status": "paid",
                }
                body_json = json.dumps(final_response)

                async with aiosqlite.connect(self.db_path) as db:
                    cursor = await db.execute(
                        """
                        UPDATE idempotency_keys
                        SET phase = ?, locked_at = NULL, response_status = 201,
                            response_body = ?, updated_at = ?
                        WHERE scope = ? AND idempotency_key = ? AND fencing_epoch = ?
                        """,
                        (
                            IdempotencyPhase.FINALIZED.value,
                            body_json,
                            time.time(),
                            scope,
                            idempotency_key,
                            current_epoch,
                        ),
                    )
                    if cursor.rowcount == 0:
                        msg = "Worker epoch superseded during finalization"
                        raise ZombieWorkerFencedError(msg)

                    await db.execute(
                        """
                        UPDATE ledger_reservations
                        SET status = 'CAPTURED'
                        WHERE idempotency_key = ?
                        """,
                        (idempotency_key,),
                    )
                    await db.execute(
                        """
                        INSERT INTO orders (
                            order_id, idempotency_key, account_id, amount, status,
                            external_charge_id, created_at
                        ) VALUES (?, ?, ?, ?, 'PAID', ?, ?)
                        """,
                        (
                            order_id,
                            idempotency_key,
                            account_id,
                            amount,
                            external_ref,
                            time.time(),
                        ),
                    )
                    await db.commit()

                current_phase = IdempotencyPhase.FINALIZED
                return 201, final_response

        msg = "State machine terminated without valid final response"
        raise RuntimeError(msg)


def create_idempotent_app(coordinator: MultiPhasePaymentCoordinator) -> Starlette:
    """Creates a Starlette ASGI application implementing IETF RFC 9457 error contracts."""

    async def handle_payment(request: Request) -> JSONResponse:
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return JSONResponse(
                status_code=400,
                headers={"Content-Type": "application/problem+json"},
                content={
                    "type": "https://developer.example.com/idempotency",
                    "title": "Idempotency-Key is missing",
                    "status": 400,
                    "detail": "This operation requires an Idempotency-Key request header.",
                },
            )

        try:
            raw_body = await request.json()
        except ValueError:
            raw_body = None
        if not isinstance(raw_body, dict):
            return JSONResponse(
                status_code=400,
                headers={"Content-Type": "application/problem+json"},
                content={
                    "type": "https://developer.example.com/idempotency",
                    "title": "Request body must be a JSON object",
                    "status": 400,
                    "detail": "The body is missing, malformed, or not a JSON object.",
                },
            )
        body = cast("dict[str, Any]", raw_body)

        scope = request.headers.get("X-Scope", "tenant_default")
        worker_id = f"worker_{uuid.uuid4().hex[:8]}"

        try:
            status_code, response_data = await coordinator.execute_payment(
                idempotency_key=idempotency_key,
                scope=scope,
                method=request.method,
                path=request.url.path,
                payload=body,
                worker_id=worker_id,
            )
            return JSONResponse(
                status_code=status_code,
                headers={"Idempotency-Key": idempotency_key},
                content=response_data,
            )
        except ConcurrentRequestConflictError as exc:
            return JSONResponse(
                status_code=409,
                headers={"Content-Type": "application/problem+json"},
                content={
                    "type": "https://developer.example.com/idempotency",
                    "title": "A request is outstanding for this Idempotency-Key",
                    "status": 409,
                    "detail": str(exc),
                },
            )
        except PayloadMismatchError as exc:
            return JSONResponse(
                status_code=422,
                headers={"Content-Type": "application/problem+json"},
                content={
                    "type": "https://developer.example.com/idempotency",
                    "title": "Idempotency-Key is already used",
                    "status": 422,
                    "detail": str(exc),
                },
            )

    routes = [Route("/v1/payments", handle_payment, methods=["POST"])]
    return Starlette(routes=routes)
