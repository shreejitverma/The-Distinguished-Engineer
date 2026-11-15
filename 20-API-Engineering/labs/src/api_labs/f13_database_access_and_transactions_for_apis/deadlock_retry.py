"""AB/BA row-lock deadlocks, deferred wait-for-graph detection, lock ordering, and SQLSTATE retries.

The lock manager models the PostgreSQL behavior that matters to an API: a blocked transaction
waits `deadlock_timeout` before it checks the wait-for graph once, and if it closes a cycle it is
the one aborted with SQLSTATE `40P01`
(PostgreSQL 18 src/backend/storage/lmgr/proc.c, CheckDeadLock).
Only exclusive row locks are modelled, which is the `FOR UPDATE` / `FOR NO KEY UPDATE` writer case.
"""

from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, Self

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

# PostgreSQL 18 Appendix A, Class 40 - Transaction Rollback.
DEADLOCK_DETECTED: Final = "40P01"
SERIALIZATION_FAILURE: Final = "40001"
RETRYABLE_SQLSTATES: Final = frozenset({DEADLOCK_DETECTED, SERIALIZATION_FAILURE})

type LockOrder = Literal["caller", "sorted"]


class DeadlockDetectedError(Exception):
    """Raised in the transaction whose deferred check found a cycle through itself."""

    sqlstate: Final = DEADLOCK_DETECTED

    def __init__(self, cycle: tuple[int, ...], waited: float) -> None:
        self.cycle = cycle
        self.waited = waited
        path = " -> ".join(f"tx{txid}" for txid in (*cycle, cycle[0]))
        super().__init__(f"deadlock detected: {path}")


class InsufficientFundsError(Exception):
    """A business-rule failure: never retryable, because retrying cannot change the outcome."""


@dataclass
class RowLockManager:
    """Exclusive row locks with a wait-for graph checked once per wait, after `deadlock_timeout`."""

    deadlock_timeout: float
    deadlocks: int = 0
    checks: int = 0
    _owner: dict[int, int] = field(default_factory=dict[int, int])
    _waits_for: dict[int, int] = field(default_factory=dict[int, int])
    _cond: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def acquire(self, txid: int, row: int) -> None:
        loop = asyncio.get_running_loop()
        async with self._cond:
            if self._owner.get(row, txid) == txid:
                self._owner[row] = txid
                return
            started = loop.time()
            deadline = started + self.deadlock_timeout
            checked = False
            self._waits_for[txid] = row
            try:
                while row in self._owner:
                    if checked:
                        await self._cond.wait()
                        continue
                    try:
                        await asyncio.wait_for(self._cond.wait(), max(0.0, deadline - loop.time()))
                    except TimeoutError:
                        checked = True
                        self.checks += 1
                        cycle = self._find_cycle(txid)
                        if cycle is not None:
                            self.deadlocks += 1
                            raise DeadlockDetectedError(cycle, loop.time() - started) from None
                self._owner[row] = txid
            finally:
                del self._waits_for[txid]

    def _find_cycle(self, start: int) -> tuple[int, ...] | None:
        """Follow waiter -> row -> owner edges; exclusive locks make the graph a single chain."""
        path = [start]
        txid = start
        while (row := self._waits_for.get(txid)) is not None:
            owner = self._owner.get(row)
            if owner is None:
                return None
            if owner == start:
                return tuple(path)
            if owner in path:
                return None  # a cycle that does not include us: its own member will detect it
            path.append(owner)
            txid = owner
        return None

    async def release_all(self, txid: int) -> None:
        async with self._cond:
            for row in [row for row, owner in self._owner.items() if owner == txid]:
                del self._owner[row]
            self._cond.notify_all()


@dataclass
class LedgerTx:
    """A transaction: buffered writes become visible only at commit, locks are held until then."""

    ledger: Ledger
    txid: int
    writes: dict[int, int] = field(default_factory=dict[int, int])

    async def lock(self, row: int) -> None:
        """The `SELECT ... FOR UPDATE` of this model."""
        await self.ledger.locks.acquire(self.txid, row)

    def read(self, row: int) -> int:
        return self.writes.get(row, self.ledger.balances[row])

    def write(self, row: int, value: int) -> None:
        self.writes[row] = value


@dataclass
class Ledger:
    balances: dict[int, int]
    locks: RowLockManager
    commits: int = 0
    rollbacks: int = 0
    _next_txid: int = 1

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[LedgerTx]:
        tx = LedgerTx(self, self._next_txid)
        self._next_txid += 1
        try:
            yield tx
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.balances.update(tx.writes)
            self.commits += 1
        finally:
            await self.locks.release_all(tx.txid)


async def transfer(
    ledger: Ledger,
    source: int,
    destination: int,
    amount: int,
    *,
    lock_order: LockOrder,
    think_time: float = 0.0,
) -> None:
    """Move `amount`; `think_time` is the gap between the two row locks that opens the race."""
    if source == destination or amount <= 0:
        raise ValueError("a transfer needs two distinct accounts and a positive amount")
    rows = sorted((source, destination)) if lock_order == "sorted" else [source, destination]
    async with ledger.transaction() as tx:
        await tx.lock(rows[0])
        if think_time:
            await asyncio.sleep(think_time)
        await tx.lock(rows[1])
        if tx.read(source) < amount:
            raise InsufficientFundsError(f"account {source} cannot cover {amount}")
        tx.write(source, tx.read(source) - amount)
        tx.write(destination, tx.read(destination) + amount)


def sqlstate_of(exc: BaseException) -> str | None:
    """Find a SQLSTATE via SQLAlchemy's `.orig` and Python's `__cause__` / `__context__` links."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        code = getattr(current, "sqlstate", None)
        if isinstance(code, str) and len(code) == 5:
            return code
        for link in (getattr(current, "orig", None), current.__cause__, current.__context__):
            if isinstance(link, BaseException):
                stack.append(link)
    return None


def is_retryable(exc: BaseException) -> bool:
    return sqlstate_of(exc) in RETRYABLE_SQLSTATES


def full_jitter_delay(retry: int, *, base: float, cap: float, rng: random.Random) -> float:
    """Full Jitter: uniform over [0, min(cap, base * 2**retry)], `retry` counted from 0."""
    return rng.uniform(0.0, min(cap, base * 2.0**retry))


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 0.01
    max_delay: float = 0.5


@dataclass
class RetryReport:
    attempts: int = 0
    delays: list[float] = field(default_factory=list[float])
    sqlstates: list[str] = field(default_factory=list[str])


async def run_with_retries[T](
    work: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    rng: random.Random,
    report: RetryReport,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Re-run the whole unit of work (reads included) on `40P01` / `40001`, nothing else."""
    for attempt in range(1, policy.max_attempts + 1):
        report.attempts = attempt
        try:
            return await work()
        except Exception as exc:
            if not is_retryable(exc) or attempt == policy.max_attempts:
                raise
            report.sqlstates.append(sqlstate_of(exc) or "")
            delay = full_jitter_delay(
                attempt - 1, base=policy.base_delay, cap=policy.max_delay, rng=rng
            )
            report.delays.append(delay)
            await sleep(delay)
    raise AssertionError("unreachable: the loop returns or raises")


class TransferIn(BaseModel):
    source: int = Field(ge=1)
    destination: int = Field(ge=1)
    amount: int = Field(gt=0, le=1_000_000)

    @model_validator(mode="after")
    def distinct_accounts(self) -> Self:
        if self.source == self.destination:
            raise ValueError("source and destination must differ")
        return self


def create_app(
    ledger: Ledger,
    *,
    policy: RetryPolicy,
    lock_order: LockOrder,
    think_time: float,
    seed: int,
) -> FastAPI:
    app = FastAPI()
    rng = random.Random(seed)  # noqa: S311 - backoff jitter, not a secret

    @app.post("/transfers")
    async def post_transfer(body: TransferIn) -> JSONResponse:
        report = RetryReport()

        async def unit_of_work() -> None:
            await transfer(
                ledger,
                body.source,
                body.destination,
                body.amount,
                lock_order=lock_order,
                think_time=think_time,
            )

        try:
            await run_with_retries(unit_of_work, policy=policy, rng=rng, report=report)
        except InsufficientFundsError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        except Exception as exc:
            if not is_retryable(exc):
                raise
            # Retries exhausted on a transient conflict: the transaction rolled back, so the
            # client may retry - safely only if it sends the same Idempotency-Key.
            return JSONResponse(
                {"detail": "transaction aborted by a lock conflict", "sqlstate": sqlstate_of(exc)},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        return JSONResponse({"attempts": report.attempts, "sqlstates": report.sqlstates})

    return app
