"""Deterministic MVCC model of PostgreSQL's three isolation levels behind a withdrawal API.

`Store` keeps every row as a list of versions stamped with the creating transaction id.
A transaction sees a version when its creator committed at or before the reader's snapshot
(or is the reader itself); Read Committed takes a fresh snapshot per statement, Repeatable Read
and Serializable take one at their first statement.
Writers follow PostgreSQL's row rules: wait (`LockWait`) on an uncommitted newer version,
re-check the newest committed version under Read Committed (EvalPlanQual), and fail with
SQLSTATE `40001` under Repeatable Read and Serializable (first updater wins).
Serializable adds SSI: SIREAD marks on rows and predicates, rw-antidependency edges between
concurrent Serializable transactions, and an abort when a dangerous structure
`T_in -rw-> pivot -rw-> T_out` exists whose `T_out` committed first.
The model is row- and prefix-granular, so it has none of PostgreSQL's page- or
relation-level false positives and none of its read-only optimizations.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated

from fastapi import FastAPI, HTTPException, Path
from pydantic import BaseModel, PositiveInt

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class Level(enum.StrEnum):
    READ_COMMITTED = "READ COMMITTED"
    REPEATABLE_READ = "REPEATABLE READ"
    SERIALIZABLE = "SERIALIZABLE"


class Status(enum.Enum):
    ACTIVE = "active"
    COMMITTED = "committed"
    ABORTED = "aborted"


class SerializationFailure(Exception):
    """SQLSTATE 40001 `serialization_failure`; the transaction is already rolled back."""

    sqlstate = "40001"

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message if detail is None else f"{message}\nDETAIL:  {detail}")
        self.message = message
        self.detail = detail


class LockWait(Exception):
    """The statement would block on a row version written by a still-active transaction."""

    def __init__(self, key: str, blocker: int) -> None:
        super().__init__(f"statement waits for transaction {blocker} on row {key!r}")
        self.key = key
        self.blocker = blocker


CONCURRENT_UPDATE = "could not serialize access due to concurrent update"
RW_DEPENDENCIES = "could not serialize access due to read/write dependencies among transactions"


@dataclass(frozen=True)
class Version:
    value: int
    xid: int


@dataclass(eq=False)
class Txn:
    store: Store
    xid: int
    level: Level
    status: Status = Status.ACTIVE
    snapshot: int | None = None  # commit clock value of the transaction snapshot
    commit_seq: int | None = None
    doomed: bool = False
    pending_snapshot: int | None = None  # snapshot of a statement that hit LockWait
    siread_keys: set[str] = field(default_factory=set[str])
    siread_prefixes: set[str] = field(default_factory=set[str])
    out_conflicts: set[int] = field(default_factory=set[int])  # self -rw-> other
    in_conflicts: set[int] = field(default_factory=set[int])  # other -rw-> self
    created: list[str] = field(default_factory=list[str])

    @property
    def serializable(self) -> bool:
        return self.level is Level.SERIALIZABLE

    def _statement(self, phase: str) -> int:
        """Begin a statement and return the snapshot it reads with."""
        if self.status is not Status.ACTIVE:
            raise RuntimeError(f"transaction {self.xid} is {self.status.value}")
        if self.doomed:
            self.abort_with(
                RW_DEPENDENCIES, f"Canceled on identification as a pivot, during {phase}."
            )
        if self.pending_snapshot is not None:
            # Re-issuing a statement that waited resumes it with its original snapshot.
            self.snapshot, self.pending_snapshot = self.pending_snapshot, None
        elif self.snapshot is None or self.level is Level.READ_COMMITTED:
            self.snapshot = self.store.clock
        return self.snapshot

    def _visible(self, key: str, snapshot: int) -> Version | None:
        for version in reversed(self.store.rows[key]):
            if version.xid == self.xid or self.store.committed_by(version.xid, snapshot):
                return version
        return None

    def _note_read(self, key: str, snapshot: int, phase: str) -> None:
        """SSI: a newer version this reader cannot see is an edge reader -rw-> writer."""
        if not self.serializable:
            return
        self.siread_keys.add(key)
        for version in self.store.rows[key]:
            if version.xid != self.xid and not self.store.committed_by(version.xid, snapshot):
                self.store.add_conflict(self, self.store.txns[version.xid], self, phase)

    def _note_write(self, key: str, phase: str) -> None:
        """SSI: every concurrent Serializable reader of `key` gets an edge reader -rw-> self."""
        if not self.serializable:
            return
        for reader in list(self.store.txns.values()):
            if reader is self or not reader.serializable or reader.status is Status.ABORTED:
                continue
            covered = key in reader.siread_keys or any(
                key.startswith(prefix) for prefix in reader.siread_prefixes
            )
            if covered and self.store.concurrent(reader, self):
                self.store.add_conflict(reader, self, self, phase)

    def read(self, key: str) -> int | None:
        """`SELECT value FROM t WHERE key = :key`."""
        snapshot = self._statement("read")
        version = self._visible(key, snapshot) if key in self.store.rows else None
        if key in self.store.rows:
            self._note_read(key, snapshot, "read")
        return None if version is None else version.value

    def scan(self, prefix: str) -> dict[str, int]:
        """`SELECT key, value FROM t WHERE key LIKE :prefix || '%'` (a predicate read)."""
        snapshot = self._statement("read")
        if self.serializable:
            self.siread_prefixes.add(prefix)
        out: dict[str, int] = {}
        for key in sorted(self.store.rows):
            if not key.startswith(prefix):
                continue
            version = self._visible(key, snapshot)
            if version is not None:
                out[key] = version.value
            self._note_read(key, snapshot, "read")
        return out

    def write(self, key: str, value: int) -> int:
        """`UPDATE t SET value = :value WHERE key = :key` (value computed by the application)."""
        return self._update(key, lambda _current: value, lambda _current: True)

    def add(self, key: str, delta: int) -> int:
        """`UPDATE t SET value = value + :delta WHERE key = :key` (computed by the database)."""
        return self._update(key, lambda current: current + delta, lambda _current: True)

    def update_where(self, key: str, delta: int, where: Callable[[int], bool]) -> int:
        """`UPDATE t SET value = value + :delta WHERE key = :key AND <where>`; returns rowcount."""
        return self._update(key, lambda current: current + delta, where)

    def _update(self, key: str, compute: Callable[[int], int], where: Callable[[int], bool]) -> int:
        snapshot = self._statement("write")
        versions = self.store.rows[key]
        newest = versions[-1]
        target = self._visible(key, snapshot)
        if target is None or not where(target.value):
            self._note_read(key, snapshot, "write")
            return 0  # not a target row in this statement's snapshot: no wait, no lock
        if newest.xid != self.xid and self.store.txns[newest.xid].status is Status.ACTIVE:
            self.pending_snapshot = snapshot
            raise LockWait(key, newest.xid)
        self._note_read(key, snapshot, "write")
        if target is not newest:
            if self.level is not Level.READ_COMMITTED:
                self.abort_with(CONCURRENT_UPDATE)
            target = newest  # EvalPlanQual: re-check WHERE against the updated version
            if not where(target.value):
                return 0
        new = Version(compute(target.value), self.xid)
        if newest.xid == self.xid:
            versions[-1] = new
        else:
            versions.append(new)
        self._note_write(key, "write")
        return 1

    def insert(self, prefix: str, value: int) -> str:
        """`INSERT INTO t (key, value) VALUES (:prefix || nextval(...), :value)`."""
        self._statement("write")
        key = f"{prefix}{self.store.next_row_id():06d}"
        self.store.rows[key] = [Version(value, self.xid)]
        self.created.append(key)
        self._note_write(key, "write")
        return key

    def commit(self) -> None:
        self._statement("commit attempt")
        if self.serializable:
            self.store.check_dangerous(self, "commit attempt")
        self.store.clock += 1
        self.commit_seq = self.store.clock
        self.status = Status.COMMITTED

    def rollback(self) -> None:
        if self.status is not Status.ACTIVE:
            return
        for versions in self.store.rows.values():
            if versions[-1].xid == self.xid:
                versions.pop()
        for key in self.created:
            del self.store.rows[key]
        self.status = Status.ABORTED

    def abort_with(self, message: str, detail: str | None = None) -> None:
        """Roll back and raise SQLSTATE 40001, as the server does on a serialization failure."""
        self.rollback()
        raise SerializationFailure(message, detail)


@dataclass
class Store:
    rows: dict[str, list[Version]] = field(default_factory=dict[str, list[Version]])
    txns: dict[int, Txn] = field(default_factory=dict[int, Txn])
    clock: int = 0
    _next_xid: int = 1
    _next_row: int = 0

    def seed(self, values: dict[str, int]) -> None:
        """Rows committed by a bootstrap transaction before any test transaction starts."""
        boot = self.begin(Level.READ_COMMITTED)
        for key, value in values.items():
            self.rows[key] = [Version(value, boot.xid)]
        boot.commit()

    def begin(self, level: Level) -> Txn:
        txn = Txn(self, self._next_xid, level)
        self.txns[txn.xid] = txn
        self._next_xid += 1
        return txn

    def next_row_id(self) -> int:
        self._next_row += 1
        return self._next_row

    def committed_by(self, xid: int, snapshot: int) -> bool:
        txn = self.txns[xid]
        return txn.commit_seq is not None and txn.commit_seq <= snapshot

    def concurrent(self, a: Txn, b: Txn) -> bool:
        """Neither transaction committed before the other took its snapshot."""
        a_first = a.commit_seq is not None and b.snapshot is not None and a.commit_seq <= b.snapshot
        b_first = b.commit_seq is not None and a.snapshot is not None and b.commit_seq <= a.snapshot
        return not (a_first or b_first)

    def add_conflict(self, reader: Txn, writer: Txn, current: Txn, phase: str) -> None:
        """Record `reader -rw-> writer` between concurrent Serializable transactions."""
        if not (reader.serializable and writer.serializable) or reader is writer:
            return
        if writer.status is Status.ABORTED or not self.concurrent(reader, writer):
            return
        reader.out_conflicts.add(writer.xid)
        writer.in_conflicts.add(reader.xid)
        self.check_dangerous(current, phase)

    def check_dangerous(self, txn: Txn, phase: str) -> None:
        """Abort `txn` (or doom an active pivot) if it closes T_in -rw-> pivot -rw-> T_out."""
        for pivot in [self.txns[x] for x in sorted(self._pivots_around(txn))]:
            for out_xid in sorted(pivot.out_conflicts):
                t_out = self.txns[out_xid]
                if t_out.commit_seq is None:
                    continue  # SSI only acts once T_out has committed first
                for in_xid in sorted(pivot.in_conflicts):
                    t_in = self.txns[in_xid]
                    if not self._out_committed_first(t_out.commit_seq, pivot, t_in):
                        continue
                    if pivot is txn:
                        txn.abort_with(
                            RW_DEPENDENCIES,
                            f"Canceled on identification as a pivot, during {phase}.",
                        )
                    if pivot.status is Status.ACTIVE:
                        pivot.doomed = True  # fails at its next statement or commit
                    elif txn is t_in:
                        txn.abort_with(
                            RW_DEPENDENCIES,
                            f"Canceled on conflict out to pivot {pivot.xid}, during {phase}.",
                        )

    def _pivots_around(self, txn: Txn) -> set[int]:
        candidates = {txn.xid} | txn.out_conflicts | txn.in_conflicts
        return {
            x
            for x in candidates
            if self.txns[x].status is not Status.ABORTED
            and self.txns[x].in_conflicts
            and self.txns[x].out_conflicts
        }

    @staticmethod
    def _out_committed_first(out_seq: int, pivot: Txn, t_in: Txn) -> bool:
        if pivot.status is Status.ABORTED or t_in.status is Status.ABORTED:
            return False
        pivot_later = pivot.commit_seq is None or pivot.commit_seq > out_seq
        in_later = t_in.commit_seq is None or t_in.commit_seq >= out_seq  # T_in may be T_out
        return pivot_later and in_later


# ---------------------------------------------------------------------------
# The API: one withdrawal endpoint, two implementations, whole-transaction retry.
# ---------------------------------------------------------------------------


class InsufficientFunds(Exception):
    pass


class RetriesExhausted(Exception):
    def __init__(self, attempts: int) -> None:
        super().__init__(f"gave up after {attempts} serialization failures")
        self.attempts = attempts


class Strategy(enum.StrEnum):
    CHECK_THEN_INSERT = "check-then-insert"  # SELECT sum(...) then INSERT a ledger row
    GUARDED_UPDATE = "guarded-update"  # UPDATE ... SET balance = balance - :a WHERE balance >= :a


@dataclass
class Bank:
    store: Store
    level: Level
    max_attempts: int = 3
    # Test hook awaited between the read and the write, with the attempt number.
    interleave: Callable[[int], Awaitable[None]] | None = None
    serialization_failures: int = 0


def ledger_prefix(account_id: int) -> str:
    return f"ledger:{account_id}:"


def account_key(account_id: int) -> str:
    return f"account:{account_id}"


async def _pause(bank: Bank, attempt: int) -> None:
    if bank.interleave is not None:
        await bank.interleave(attempt)


async def _check_then_insert(bank: Bank, txn: Txn, account_id: int, amount: int, n: int) -> int:
    balance = sum(txn.scan(ledger_prefix(account_id)).values())
    await _pause(bank, n)
    if balance < amount:
        raise InsufficientFunds
    txn.insert(ledger_prefix(account_id), -amount)
    return balance - amount


async def _guarded_update(bank: Bank, txn: Txn, account_id: int, amount: int, n: int) -> int:
    key = account_key(account_id)
    txn.read(key)  # e.g. load the account for validation; fixes the RR snapshot here
    await _pause(bank, n)
    if txn.update_where(key, -amount, lambda balance: balance >= amount) == 0:
        raise InsufficientFunds
    after = txn.read(key)
    if after is None:
        raise LookupError(key)
    return after


async def withdraw(bank: Bank, account_id: int, amount: int, strategy: Strategy) -> tuple[int, int]:
    """Run the whole transaction, decision logic included, retrying only on SQLSTATE 40001."""
    body = _check_then_insert if strategy is Strategy.CHECK_THEN_INSERT else _guarded_update
    for attempt in range(1, bank.max_attempts + 1):
        txn = bank.store.begin(bank.level)
        try:
            balance = await body(bank, txn, account_id, amount, attempt)
            txn.commit()
        except SerializationFailure:
            bank.serialization_failures += 1
            continue  # already rolled back; re-read everything on the next attempt
        except BaseException:
            txn.rollback()
            raise
        return balance, attempt
    raise RetriesExhausted(bank.max_attempts)


class WithdrawalIn(BaseModel):
    amount: PositiveInt


class WithdrawalOut(BaseModel):
    balance: int
    attempts: int


def build_app(bank: Bank) -> FastAPI:
    app = FastAPI()

    @app.post("/accounts/{account_id}/withdrawals", status_code=201)
    async def create_withdrawal(  # pyright: ignore[reportUnusedFunction]
        account_id: Annotated[int, Path()],
        body: WithdrawalIn,
        strategy: Strategy = Strategy.CHECK_THEN_INSERT,
    ) -> WithdrawalOut:
        try:
            balance, attempts = await withdraw(bank, account_id, body.amount, strategy)
        except InsufficientFunds:
            raise HTTPException(status_code=409, detail="insufficient funds") from None
        except RetriesExhausted:
            raise HTTPException(
                status_code=503,
                detail="transaction conflict, retry later",
                headers={"Retry-After": "1"},
            ) from None
        return WithdrawalOut(balance=balance, attempts=attempts)

    return app
