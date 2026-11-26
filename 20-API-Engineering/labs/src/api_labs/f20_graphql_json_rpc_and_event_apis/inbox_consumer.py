"""Transactional inbox consumer with a per-entity version guard and DLQ escalation.

One SQLite database holds the consumer's business state, its inbox, a parking table for
early events, an attempt counter, and a dead-letter table, so every decision about a
delivery commits or rolls back as one unit.
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    consumer_group TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    PRIMARY KEY (consumer_group, message_id)
);
CREATE TABLE IF NOT EXISTS entities (
    consumer_group TEXT NOT NULL,
    entity_id      TEXT NOT NULL,
    version        INTEGER NOT NULL,
    balance        INTEGER NOT NULL,
    PRIMARY KEY (consumer_group, entity_id)
);
CREATE TABLE IF NOT EXISTS parked (
    consumer_group TEXT NOT NULL,
    entity_id      TEXT NOT NULL,
    version        INTEGER NOT NULL,
    message_id     TEXT NOT NULL,
    amount         INTEGER NOT NULL,
    PRIMARY KEY (consumer_group, entity_id, version)
);
CREATE TABLE IF NOT EXISTS attempts (
    consumer_group TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    failures       INTEGER NOT NULL,
    last_error     TEXT NOT NULL,
    PRIMARY KEY (consumer_group, message_id)
);
CREATE TABLE IF NOT EXISTS dead_letters (
    consumer_group TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    body           BLOB NOT NULL,
    reason         TEXT NOT NULL,
    error          TEXT NOT NULL,
    attempts       INTEGER NOT NULL,
    PRIMARY KEY (consumer_group, message_id)
);
"""


_UPSERT_PREFIX = (
    "INSERT INTO entities (consumer_group, entity_id, version, balance) "
    "VALUES (?, ?, ?, ?) ON CONFLICT (consumer_group, entity_id) DO UPDATE "
    "SET version = excluded.version, "
)
_ADD_SQL = _UPSERT_PREFIX + "balance = balance + excluded.balance"
_SET_SQL = _UPSERT_PREFIX + "balance = excluded.balance"
_REDRIVE_DELETES = (
    "DELETE FROM dead_letters WHERE consumer_group = ? AND message_id = ?",
    "DELETE FROM inbox WHERE consumer_group = ? AND message_id = ?",
    "DELETE FROM attempts WHERE consumer_group = ? AND message_id = ?",
)


class Outcome(StrEnum):
    """What the consumer decided about one delivery; the broker acks all but `RETRY`."""

    APPLIED = "applied"
    DUPLICATE = "duplicate"
    STALE = "stale"
    PARKED = "parked"
    RETRY = "retry"
    DEAD_LETTERED = "dead_lettered"


class GuardMode(StrEnum):
    """`SEQUENTIAL` events are deltas that need every version; `SNAPSHOT` events replace state."""

    SEQUENTIAL = "sequential"
    SNAPSHOT = "snapshot"


class TransientError(Exception):
    """A failure that a later delivery may not hit (timeout, lock, downstream 503)."""


class PermanentError(Exception):
    """A failure every redelivery will repeat (malformed body, violated invariant)."""


@dataclass(frozen=True, slots=True)
class Delivery:
    """One broker delivery: the stable message id from the envelope and the raw body."""

    message_id: str
    body: bytes


@dataclass(frozen=True, slots=True)
class Event:
    entity_id: str
    version: int
    amount: int


@dataclass(frozen=True, slots=True)
class DeadLetter:
    message_id: str
    body: bytes
    reason: str
    error: str
    attempts: int


Effect = Callable[[Event], None]


def encode(entity_id: str, version: int, amount: int) -> bytes:
    return json.dumps({"entity_id": entity_id, "version": version, "amount": amount}).encode()


def decode(body: bytes) -> Event:
    """Parse and validate a body; every defect is a `PermanentError` because retry cannot fix it."""
    try:
        parsed: object = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PermanentError(f"undecodable body: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PermanentError("body is not a JSON object")
    fields = cast("dict[str, object]", parsed)
    entity_id = fields.get("entity_id")
    version = fields.get("version")
    amount = fields.get("amount")
    if not isinstance(entity_id, str) or not entity_id:
        raise PermanentError("entity_id must be a non-empty string")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PermanentError("version must be an integer >= 1")
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise PermanentError("amount must be an integer")
    return Event(entity_id, version, amount)


def connect(path: str, *, busy_timeout: float = 5.0) -> sqlite3.Connection:
    """Open a connection in autocommit mode so the consumer owns every `BEGIN` and `COMMIT`."""
    conn = sqlite3.connect(path, timeout=busy_timeout, isolation_level=None)
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Generator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


class InboxConsumer:
    def __init__(
        self,
        conn: sqlite3.Connection,
        group: str,
        *,
        mode: GuardMode = GuardMode.SEQUENTIAL,
        max_attempts: int = 5,
        max_parked: int = 100,
        effect: Effect | None = None,
    ) -> None:
        self._conn = conn
        self._group = group
        self._mode = mode
        self._max_attempts = max_attempts
        self._max_parked = max_parked
        self._effect = effect

    def handle(self, delivery: Delivery) -> Outcome:
        try:
            try:
                with _transaction(self._conn):
                    return self._process(delivery)
            except PermanentError as exc:
                return self._dead_letter(delivery, "permanent", str(exc))
            except TransientError as exc:
                return self._record_failure(delivery, str(exc))
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorname == "SQLITE_BUSY":
                return Outcome.RETRY
            raise

    def _process(self, delivery: Delivery) -> Outcome:
        if not self._claim(delivery.message_id, "pending"):
            return Outcome.DUPLICATE
        outcome = self._guard(delivery.message_id, decode(delivery.body))
        self._set_outcome(delivery.message_id, outcome)
        self._conn.execute(
            "DELETE FROM attempts WHERE consumer_group = ? AND message_id = ?",
            (self._group, delivery.message_id),
        )
        return outcome

    def _claim(self, message_id: str, outcome: str) -> bool:
        cursor = self._conn.execute(
            "INSERT INTO inbox (consumer_group, message_id, outcome) VALUES (?, ?, ?) "
            "ON CONFLICT (consumer_group, message_id) DO NOTHING",
            (self._group, message_id, outcome),
        )
        return cursor.rowcount == 1

    def _set_outcome(self, message_id: str, outcome: Outcome) -> None:
        self._conn.execute(
            "UPDATE inbox SET outcome = ? WHERE consumer_group = ? AND message_id = ?",
            (outcome.value, self._group, message_id),
        )

    def _version(self, entity_id: str) -> int:
        row = self._conn.execute(
            "SELECT version FROM entities WHERE consumer_group = ? AND entity_id = ?",
            (self._group, entity_id),
        ).fetchone()
        return int(row[0]) if row else 0

    def _guard(self, message_id: str, event: Event) -> Outcome:
        current = self._version(event.entity_id)
        if event.version <= current:
            return Outcome.STALE
        if self._mode is GuardMode.SEQUENTIAL and event.version > current + 1:
            return self._park(message_id, event)
        self._apply(event)
        if self._effect is not None:
            self._effect(event)
        if self._mode is GuardMode.SEQUENTIAL:
            self._drain(event.entity_id)
        return Outcome.APPLIED

    def _apply(self, event: Event) -> None:
        sql = _ADD_SQL if self._mode is GuardMode.SEQUENTIAL else _SET_SQL
        self._conn.execute(sql, (self._group, event.entity_id, event.version, event.amount))

    def _park(self, message_id: str, event: Event) -> Outcome:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM parked WHERE consumer_group = ? AND entity_id = ?",
            (self._group, event.entity_id),
        ).fetchone()
        if int(row[0]) >= self._max_parked:
            raise PermanentError(f"gap_overflow: {self._max_parked} events already parked")
        cursor = self._conn.execute(
            "INSERT INTO parked (consumer_group, entity_id, version, message_id, amount) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (consumer_group, entity_id, version) DO NOTHING",
            (self._group, event.entity_id, event.version, message_id, event.amount),
        )
        return Outcome.PARKED if cursor.rowcount == 1 else Outcome.STALE

    def _drain(self, entity_id: str) -> None:
        """Apply parked successors of the version just applied, in the same transaction."""
        while True:
            version = self._version(entity_id) + 1
            row = self._conn.execute(
                "SELECT message_id, amount FROM parked "
                "WHERE consumer_group = ? AND entity_id = ? AND version = ?",
                (self._group, entity_id, version),
            ).fetchone()
            if row is None:
                return
            self._apply(Event(entity_id, version, int(row[1])))
            self._set_outcome(str(row[0]), Outcome.APPLIED)
            self._conn.execute(
                "DELETE FROM parked WHERE consumer_group = ? AND entity_id = ? AND version = ?",
                (self._group, entity_id, version),
            )

    def _record_failure(self, delivery: Delivery, error: str) -> Outcome:
        with _transaction(self._conn):
            if self._inbox_outcome(delivery.message_id) is not None:
                return Outcome.DUPLICATE
            self._conn.execute(
                "INSERT INTO attempts (consumer_group, message_id, failures, last_error) "
                "VALUES (?, ?, 1, ?) ON CONFLICT (consumer_group, message_id) "
                "DO UPDATE SET failures = failures + 1, last_error = excluded.last_error",
                (self._group, delivery.message_id, error),
            )
            if self._failures(delivery.message_id) < self._max_attempts:
                return Outcome.RETRY
            return self._dead_letter_in_transaction(delivery, "max_attempts", error)

    def _dead_letter(self, delivery: Delivery, reason: str, error: str) -> Outcome:
        with _transaction(self._conn):
            return self._dead_letter_in_transaction(delivery, reason, error)

    def _dead_letter_in_transaction(self, delivery: Delivery, reason: str, error: str) -> Outcome:
        if not self._claim(delivery.message_id, Outcome.DEAD_LETTERED.value):
            return Outcome.DUPLICATE
        self._conn.execute(
            "INSERT INTO dead_letters (consumer_group, message_id, body, reason, error, attempts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                self._group,
                delivery.message_id,
                delivery.body,
                reason,
                error,
                max(self._failures(delivery.message_id), 1),
            ),
        )
        self._conn.execute(
            "DELETE FROM attempts WHERE consumer_group = ? AND message_id = ?",
            (self._group, delivery.message_id),
        )
        return Outcome.DEAD_LETTERED

    def _failures(self, message_id: str) -> int:
        row = self._conn.execute(
            "SELECT failures FROM attempts WHERE consumer_group = ? AND message_id = ?",
            (self._group, message_id),
        ).fetchone()
        return int(row[0]) if row else 0

    def _inbox_outcome(self, message_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT outcome FROM inbox WHERE consumer_group = ? AND message_id = ?",
            (self._group, message_id),
        ).fetchone()
        return str(row[0]) if row else None

    def redrive(self, message_id: str) -> Delivery:
        """Remove a dead letter and its inbox row so the next delivery is processed, not dropped."""
        with _transaction(self._conn):
            row = self._conn.execute(
                "SELECT body FROM dead_letters WHERE consumer_group = ? AND message_id = ?",
                (self._group, message_id),
            ).fetchone()
            if row is None:
                raise KeyError(message_id)
            for sql in _REDRIVE_DELETES:
                self._conn.execute(sql, (self._group, message_id))
        return Delivery(message_id, bytes(row[0]))

    def inbox_outcome(self, message_id: str) -> str | None:
        return self._inbox_outcome(message_id)

    def state(self, entity_id: str) -> tuple[int, int]:
        """Return `(version, balance)`; an unseen entity is `(0, 0)`."""
        row = self._conn.execute(
            "SELECT version, balance FROM entities WHERE consumer_group = ? AND entity_id = ?",
            (self._group, entity_id),
        ).fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def parked_count(self, entity_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM parked WHERE consumer_group = ? AND entity_id = ?",
            (self._group, entity_id),
        ).fetchone()
        return int(row[0])

    def dead_letters(self) -> list[DeadLetter]:
        rows = self._conn.execute(
            "SELECT message_id, body, reason, error, attempts FROM dead_letters "
            "WHERE consumer_group = ? ORDER BY message_id",
            (self._group,),
        ).fetchall()
        return [DeadLetter(str(r[0]), bytes(r[1]), str(r[2]), str(r[3]), int(r[4])) for r in rows]


class Broker:
    """In-memory at-least-once queue: unacknowledged deliveries come back at the tail."""

    def __init__(self, deliveries: Iterable[Delivery]) -> None:
        self._queue = deque(deliveries)
        self.redeliveries = 0

    def receive(self) -> Delivery | None:
        return self._queue.popleft() if self._queue else None

    def requeue(self, delivery: Delivery) -> None:
        self.redeliveries += 1
        self._queue.append(delivery)


def pump(
    broker: Broker,
    handle: Callable[[Delivery], Outcome],
    *,
    lose_ack: Callable[[Delivery, Outcome], bool] = lambda _d, _o: False,
    max_steps: int = 10_000,
) -> list[tuple[str, Outcome]]:
    """Deliver until the queue is empty; `RETRY` or a lost ack puts the delivery back."""
    trace: list[tuple[str, Outcome]] = []
    for _ in range(max_steps):
        delivery = broker.receive()
        if delivery is None:
            return trace
        outcome = handle(delivery)
        trace.append((delivery.message_id, outcome))
        if outcome is Outcome.RETRY or lose_ack(delivery, outcome):
            broker.requeue(delivery)
    raise RuntimeError("pump did not converge")


def naive_handler(conn: sqlite3.Connection, group: str) -> Callable[[Delivery], Outcome]:
    """Baseline with no inbox: applies the amount on every delivery."""

    def handle(delivery: Delivery) -> Outcome:
        event = decode(delivery.body)
        conn.execute(_ADD_SQL, (group, event.entity_id, event.version, event.amount))
        return Outcome.APPLIED

    return handle
