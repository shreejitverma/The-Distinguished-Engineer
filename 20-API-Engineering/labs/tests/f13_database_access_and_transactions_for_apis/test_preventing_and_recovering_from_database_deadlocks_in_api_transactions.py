"""AB/BA deadlocks, deferred detection, sorted lock order, and SQLSTATE-classified retries."""

import asyncio
import importlib
import random
from typing import Any

import httpx
import psycopg.errors
import pytest
import sqlalchemy.exc

from api_labs.f13_database_access_and_transactions_for_apis.deadlock_retry import (
    DeadlockDetectedError,
    Ledger,
    LockOrder,
    RetryPolicy,
    RetryReport,
    RowLockManager,
    create_app,
    full_jitter_delay,
    is_retryable,
    run_with_retries,
    sqlstate_of,
    transfer,
)

DEADLOCK_TIMEOUT = 0.02
THINK = 0.005
GUARD = 5.0  # a lost wakeup or a missed deadlock fails the test instead of hanging it
# asyncpg ships no type information, so its exception module is typed as Any.
ASYNCPG_EXC: Any = importlib.import_module("asyncpg.exceptions")


def new_ledger(accounts: int = 2, balance: int = 100) -> Ledger:
    return Ledger(
        balances=dict.fromkeys(range(1, accounts + 1), balance),
        locks=RowLockManager(deadlock_timeout=DEADLOCK_TIMEOUT),
    )


async def race(
    ledger: Ledger, pairs: list[tuple[int, int]], order: LockOrder
) -> list[BaseException | None]:
    jobs = [transfer(ledger, src, dst, 1, lock_order=order, think_time=THINK) for src, dst in pairs]
    return await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), GUARD)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ab_ba_in_caller_order_deadlocks_and_exactly_one_side_is_aborted() -> None:
    ledger = new_ledger()
    results = await race(ledger, [(1, 2), (2, 1)], "caller")
    errors = [r for r in results if isinstance(r, DeadlockDetectedError)]
    assert len(errors) == 1
    assert results.count(None) == 1
    assert sqlstate_of(errors[0]) == "40P01"
    assert len(errors[0].cycle) == 2
    assert (ledger.commits, ledger.rollbacks, ledger.locks.deadlocks) == (1, 1, 1)
    assert sorted(ledger.balances.values()) == [99, 101]  # only the survivor's transfer applied


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sorted_lock_order_turns_the_same_race_into_a_queue() -> None:
    ledger = new_ledger()
    results = await race(ledger, [(1, 2), (2, 1)], "sorted")
    assert results == [None, None]
    assert ledger.locks.deadlocks == 0
    assert ledger.balances == {1: 100, 2: 100}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_three_transaction_cycle_aborts_one_member_only() -> None:
    ledger = new_ledger(accounts=3)
    results = await race(ledger, [(1, 2), (2, 3), (3, 1)], "caller")
    errors = [r for r in results if isinstance(r, DeadlockDetectedError)]
    assert len(errors) == 1
    assert len(errors[0].cycle) == 3
    assert ledger.commits == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_random_workload_deadlocks_in_caller_order_but_never_when_sorted() -> None:
    rng = random.Random(13)  # noqa: S311 - workload generator
    pairs = [(a, b) for a, b in (rng.sample(range(1, 5), 2) for _ in range(40))]
    unsorted = new_ledger(accounts=4)
    await race(unsorted, pairs, "caller")
    ordered = new_ledger(accounts=4)
    results = await race(ordered, pairs, "sorted")
    assert unsorted.locks.deadlocks > 0  # the workload is capable of deadlocking
    assert ordered.locks.deadlocks == 0
    assert results == [None] * len(pairs)
    assert sum(ordered.balances.values()) == 400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detection_waits_for_deadlock_timeout_before_checking() -> None:
    ledger = new_ledger()
    results = await race(ledger, [(1, 2), (2, 1)], "caller")
    (error,) = [r for r in results if isinstance(r, DeadlockDetectedError)]
    assert DEADLOCK_TIMEOUT <= error.waited < DEADLOCK_TIMEOUT + 0.5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_plain_lock_wait_longer_than_deadlock_timeout_is_not_an_error() -> None:
    locks = RowLockManager(deadlock_timeout=DEADLOCK_TIMEOUT)
    await locks.acquire(1, row=7)

    async def holder() -> None:
        await asyncio.sleep(DEADLOCK_TIMEOUT * 4)
        await locks.release_all(1)

    release = asyncio.create_task(holder())
    await asyncio.wait_for(locks.acquire(2, row=7), GUARD)
    await release
    assert (locks.checks, locks.deadlocks) == (1, 0)  # checked once, found no cycle, kept waiting


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_recovers_the_victim_and_both_transfers_commit() -> None:
    ledger = new_ledger()
    rng = random.Random(1)  # noqa: S311 - backoff jitter
    policy = RetryPolicy(max_attempts=5, base_delay=0.005, max_delay=0.05)
    reports = [RetryReport(), RetryReport()]

    async def attempt(src: int, dst: int, report: RetryReport) -> None:
        async def work() -> None:
            await transfer(ledger, src, dst, 10, lock_order="caller", think_time=THINK)

        await run_with_retries(work, policy=policy, rng=rng, report=report)

    await asyncio.wait_for(
        asyncio.gather(attempt(1, 2, reports[0]), attempt(2, 1, reports[1])), GUARD
    )
    assert sorted(r.attempts for r in reports) == [1, 2]
    assert [s for r in reports for s in r.sqlstates] == ["40P01"]
    assert ledger.balances == {1: 100, 2: 100}
    assert (ledger.commits, ledger.rollbacks) == (2, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_reruns_the_reads_not_just_the_failed_statement() -> None:
    balance = {"value": 100}
    seen: list[int] = []

    async def work() -> int:
        observed = balance["value"]  # the read that decides what to write
        seen.append(observed)
        if len(seen) == 1:
            balance["value"] = 40  # a concurrent writer commits while we are aborted
            raise psycopg.errors.SerializationFailure("could not serialize access")
        return observed - 30

    async def no_sleep(_: float) -> None:
        return None

    result = await run_with_retries(
        work,
        policy=RetryPolicy(),
        rng=random.Random(0),  # noqa: S311 - backoff jitter
        report=RetryReport(),
        sleep=no_sleep,
    )
    assert seen == [100, 40]
    assert result == 10


@pytest.mark.unit
@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (psycopg.errors.DeadlockDetected("deadlock detected"), True),
        (psycopg.errors.SerializationFailure("could not serialize"), True),
        (ASYNCPG_EXC.DeadlockDetectedError("deadlock detected"), True),
        (
            sqlalchemy.exc.OperationalError(
                "UPDATE accounts", None, psycopg.errors.DeadlockDetected("deadlock detected")
            ),
            True,
        ),
        (
            sqlalchemy.exc.DBAPIError(
                "UPDATE accounts", None, ASYNCPG_EXC.DeadlockDetectedError("deadlock")
            ),
            True,
        ),
        (psycopg.errors.LockNotAvailable("canceling statement due to lock timeout"), False),
        (psycopg.errors.QueryCanceled("canceling statement due to statement timeout"), False),
        (psycopg.errors.UniqueViolation("duplicate key value"), False),
        (ValueError("not a database error"), False),
    ],
    ids=[
        "psycopg-40P01",
        "psycopg-40001",
        "asyncpg-40P01",
        "sqlalchemy-psycopg-40P01",
        "sqlalchemy-asyncpg-40P01",
        "55P03-lock-timeout",
        "57014-statement-timeout",
        "23505-unique",
        "plain",
    ],
)
def test_classifier_retries_only_40p01_and_40001(exc: Exception, retryable: bool) -> None:
    assert is_retryable(exc) is retryable


@pytest.mark.unit
def test_classifier_follows_exception_chaining() -> None:
    def repository() -> None:
        try:
            raise psycopg.errors.DeadlockDetected("deadlock detected")
        except psycopg.errors.DeadlockDetected as inner:
            raise RuntimeError("repository failed") from inner

    with pytest.raises(RuntimeError) as caught:
        repository()
    assert sqlstate_of(caught.value) == "40P01"
    assert is_retryable(caught.value)


@pytest.mark.unit
def test_full_jitter_stays_inside_the_capped_exponential_window() -> None:
    rng = random.Random(42)  # noqa: S311 - backoff jitter
    base, cap = 0.01, 0.2
    maxima: list[float] = []
    for retry in range(10):
        window = min(cap, base * 2**retry)
        samples = [full_jitter_delay(retry, base=base, cap=cap, rng=rng) for _ in range(500)]
        assert all(0.0 <= s <= window for s in samples)
        maxima.append(max(samples))
    assert maxima[4] > base * 2**3  # the window really grows: retry 4 exceeds retry 3's ceiling
    assert max(maxima[5:]) <= cap  # and stops growing at the cap


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retries_stop_at_max_attempts_and_reraise_the_last_error() -> None:
    calls: list[int] = []
    slept: list[float] = []

    async def work() -> None:
        calls.append(len(calls))
        raise psycopg.errors.DeadlockDetected(f"deadlock {len(calls)}")

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    report = RetryReport()
    with pytest.raises(psycopg.errors.DeadlockDetected, match="deadlock 4"):
        await run_with_retries(
            work,
            policy=RetryPolicy(max_attempts=4),
            rng=random.Random(0),  # noqa: S311 - backoff jitter
            report=report,
            sleep=fake_sleep,
        )
    assert len(calls) == 4
    assert slept == report.delays
    assert len(slept) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_retryable_error_is_raised_on_the_first_attempt() -> None:
    calls: list[int] = []

    async def work() -> None:
        calls.append(1)
        raise psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")

    with pytest.raises(psycopg.errors.UniqueViolation):
        await run_with_retries(
            work,
            policy=RetryPolicy(max_attempts=5),
            rng=random.Random(0),  # noqa: S311 - backoff jitter
            report=RetryReport(),
        )
    assert calls == [1]


async def post_pair(
    ledger: Ledger, *, policy: RetryPolicy, order: LockOrder
) -> tuple[httpx.Response, httpx.Response]:
    app = create_app(ledger, policy=policy, lock_order=order, think_time=THINK, seed=3)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await asyncio.wait_for(
            asyncio.gather(
                client.post("/transfers", json={"source": 1, "destination": 2, "amount": 5}),
                client.post("/transfers", json={"source": 2, "destination": 1, "amount": 7}),
            ),
            GUARD,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_without_retries_returns_503_retry_after_for_the_victim() -> None:
    ledger = new_ledger()
    responses = await post_pair(ledger, policy=RetryPolicy(max_attempts=1), order="caller")
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 503]
    (victim,) = [r for r in responses if r.status_code == 503]
    assert victim.headers["retry-after"] == "1"
    assert victim.json()["sqlstate"] == "40P01"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_with_retries_commits_both_requests() -> None:
    ledger = new_ledger()
    responses = await post_pair(ledger, policy=RetryPolicy(max_attempts=5), order="caller")
    assert [r.status_code for r in responses] == [200, 200]
    assert sorted(r.json()["attempts"] for r in responses) == [1, 2]
    assert ledger.balances == {1: 102, 2: 98}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_with_sorted_locks_needs_no_retry() -> None:
    ledger = new_ledger()
    responses = await post_pair(ledger, policy=RetryPolicy(max_attempts=1), order="sorted")
    assert [r.json()["attempts"] for r in responses] == [1, 1]
    assert ledger.locks.deadlocks == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_rejects_self_transfer_and_maps_business_failure_to_409() -> None:
    ledger = new_ledger()
    app = create_app(ledger, policy=RetryPolicy(), lock_order="sorted", think_time=0.0, seed=0)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        same = await client.post("/transfers", json={"source": 1, "destination": 1, "amount": 5})
        broke = await client.post("/transfers", json={"source": 1, "destination": 2, "amount": 500})
    assert same.status_code == 422
    assert broke.status_code == 409
    assert ledger.balances == {1: 100, 2: 100}
