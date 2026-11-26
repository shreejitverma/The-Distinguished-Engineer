"""Lab for the transactional inbox, version guard, and dead-letter escalation note."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

import pytest

from api_labs.f20_graphql_json_rpc_and_event_apis.inbox_consumer import (
    Broker,
    Delivery,
    Event,
    GuardMode,
    InboxConsumer,
    Outcome,
    PermanentError,
    TransientError,
    connect,
    decode,
    encode,
    naive_handler,
    pump,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator
    from pathlib import Path

GROUP = "billing"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(str(tmp_path / "consumer.db"))
    yield connection
    connection.close()


def msg(message_id: str, version: int, amount: int, entity: str = "acct-1") -> Delivery:
    return Delivery(message_id, encode(entity, version, amount))


def lose_first_ack(message_id: str) -> Callable[[Delivery, Outcome], bool]:
    """Simulate a crash after commit and before ack, once, for one message id."""
    pending = [message_id]

    def lose(delivery: Delivery, _outcome: Outcome) -> bool:
        if pending and delivery.message_id == pending[0]:
            pending.clear()
            return True
        return False

    return lose


def raw_balance(conn: sqlite3.Connection, group: str = GROUP, entity: str = "acct-1") -> int:
    row = conn.execute(
        "SELECT balance FROM entities WHERE consumer_group = ? AND entity_id = ?",
        (group, entity),
    ).fetchone()
    return int(row[0]) if row else 0


@pytest.mark.unit
def test_duplicate_delivery_is_applied_once(conn: sqlite3.Connection) -> None:
    consumer = InboxConsumer(conn, GROUP)
    first = consumer.handle(msg("m1", 1, 10))
    second = consumer.handle(msg("m1", 1, 10))
    assert (first, second) == (Outcome.APPLIED, Outcome.DUPLICATE)
    assert consumer.state("acct-1") == (1, 10)


@pytest.mark.unit
def test_lost_ack_after_commit_redelivers_as_duplicate(conn: sqlite3.Connection) -> None:
    consumer = InboxConsumer(conn, GROUP)
    broker = Broker([msg("m1", 1, 10)])
    trace = pump(broker, consumer.handle, lose_ack=lose_first_ack("m1"))
    assert trace == [("m1", Outcome.APPLIED), ("m1", Outcome.DUPLICATE)]
    assert broker.redeliveries == 1
    assert consumer.state("acct-1") == (1, 10)


@pytest.mark.unit
def test_consumer_without_inbox_double_applies_after_lost_ack(conn: sqlite3.Connection) -> None:
    handler = naive_handler(conn, GROUP)
    pump(Broker([msg("m1", 1, 10)]), handler, lose_ack=lose_first_ack("m1"))
    assert raw_balance(conn) == 20


@pytest.mark.unit
def test_failed_handler_rolls_back_inbox_row_and_effect(conn: sqlite3.Connection) -> None:
    def failing(_event: Event) -> None:
        raise TransientError("downstream 503")

    consumer = InboxConsumer(conn, GROUP, effect=failing, max_attempts=3)
    assert consumer.handle(msg("m1", 1, 10)) is Outcome.RETRY
    assert consumer.inbox_outcome("m1") is None
    assert consumer.state("acct-1") == (0, 0)


@pytest.mark.unit
def test_early_event_is_parked_then_drained_in_version_order(conn: sqlite3.Connection) -> None:
    consumer = InboxConsumer(conn, GROUP)
    assert consumer.handle(msg("m3", 3, 100)) is Outcome.PARKED
    assert consumer.handle(msg("m2", 2, 10)) is Outcome.PARKED
    assert consumer.state("acct-1") == (0, 0)
    assert consumer.parked_count("acct-1") == 2
    assert consumer.handle(msg("m1", 1, 1)) is Outcome.APPLIED
    assert consumer.state("acct-1") == (3, 111)
    assert consumer.parked_count("acct-1") == 0
    assert {consumer.inbox_outcome(m) for m in ("m1", "m2", "m3")} == {"applied"}


@pytest.mark.unit
def test_shuffled_duplicated_stream_converges_to_exact_state(conn: sqlite3.Connection) -> None:
    rng = random.Random(20261121)  # noqa: S311 - seeded test data
    amounts = {version: rng.randint(-50, 50) for version in range(1, 61)}
    deliveries = [msg(f"m{v}", v, a) for v, a in amounts.items()]
    deliveries += [rng.choice(deliveries) for _ in range(90)]
    rng.shuffle(deliveries)
    consumer = InboxConsumer(conn, GROUP)
    trace = pump(Broker(deliveries), consumer.handle)
    outcomes = [outcome for _, outcome in trace]
    assert consumer.state("acct-1") == (60, sum(amounts.values()))
    assert outcomes.count(Outcome.APPLIED) + outcomes.count(Outcome.PARKED) == 60
    assert outcomes.count(Outcome.DUPLICATE) == 90
    assert consumer.parked_count("acct-1") == 0


@pytest.mark.unit
def test_stale_event_with_new_message_id_is_recorded_and_dropped(conn: sqlite3.Connection) -> None:
    consumer = InboxConsumer(conn, GROUP)
    consumer.handle(msg("m1", 1, 10))
    consumer.handle(msg("m2", 2, 5))
    assert consumer.handle(msg("replay-of-v1", 1, 10)) is Outcome.STALE
    assert consumer.inbox_outcome("replay-of-v1") == "stale"
    assert consumer.state("acct-1") == (2, 15)


@pytest.mark.unit
def test_second_message_claiming_a_parked_version_loses(conn: sqlite3.Connection) -> None:
    consumer = InboxConsumer(conn, GROUP)
    assert consumer.handle(msg("first", 2, 7)) is Outcome.PARKED
    assert consumer.handle(msg("second", 2, 999)) is Outcome.STALE
    consumer.handle(msg("m1", 1, 1))
    assert consumer.state("acct-1") == (2, 8)


@pytest.mark.unit
def test_snapshot_mode_keeps_the_newest_version_in_any_order(conn: sqlite3.Connection) -> None:
    consumer = InboxConsumer(conn, GROUP, mode=GuardMode.SNAPSHOT)
    assert consumer.handle(msg("s3", 3, 300)) is Outcome.APPLIED
    assert consumer.handle(msg("s1", 1, 100)) is Outcome.STALE
    assert consumer.handle(msg("s2", 2, 200)) is Outcome.STALE
    assert consumer.state("acct-1") == (3, 300)


@pytest.mark.unit
def test_gap_beyond_the_parking_bound_is_dead_lettered(conn: sqlite3.Connection) -> None:
    consumer = InboxConsumer(conn, GROUP, max_parked=2)
    assert consumer.handle(msg("m3", 3, 1)) is Outcome.PARKED
    assert consumer.handle(msg("m4", 4, 1)) is Outcome.PARKED
    assert consumer.handle(msg("m5", 5, 1)) is Outcome.DEAD_LETTERED
    (dead,) = consumer.dead_letters()
    assert dead.message_id == "m5"
    assert dead.reason == "permanent"
    assert dead.error.startswith("gap_overflow")


@pytest.mark.unit
def test_poison_body_is_dead_lettered_without_blocking_its_neighbours(
    conn: sqlite3.Connection,
) -> None:
    poison = Delivery("bad", b'{"entity_id": "acct-1", "version": "one", "amount": 5}')
    consumer = InboxConsumer(conn, GROUP)
    trace = pump(Broker([msg("m1", 1, 10), poison, msg("m2", 2, 5)]), consumer.handle)
    assert trace == [
        ("m1", Outcome.APPLIED),
        ("bad", Outcome.DEAD_LETTERED),
        ("m2", Outcome.APPLIED),
    ]
    (dead,) = consumer.dead_letters()
    assert dead.body == poison.body
    assert dead.attempts == 1
    assert consumer.state("acct-1") == (2, 15)


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        b"\xff\xfe",
        b"[1, 2]",
        b'{"entity_id": "", "version": 1, "amount": 1}',
        b'{"entity_id": "a", "version": true, "amount": 1}',
        b'{"entity_id": "a", "version": 0, "amount": 1}',
        b'{"entity_id": "a", "version": 1, "amount": 1.5}',
    ],
)
def test_decode_rejects_malformed_bodies_as_permanent(body: bytes) -> None:
    with pytest.raises(PermanentError):
        decode(body)


@pytest.mark.unit
def test_transient_failure_retries_then_dead_letters_at_max_attempts(
    conn: sqlite3.Connection,
) -> None:
    def failing(_event: Event) -> None:
        raise TransientError("downstream 503")

    consumer = InboxConsumer(conn, GROUP, effect=failing, max_attempts=3)
    trace = pump(Broker([msg("m1", 1, 10)]), consumer.handle)
    assert [o for _, o in trace] == [Outcome.RETRY, Outcome.RETRY, Outcome.DEAD_LETTERED]
    (dead,) = consumer.dead_letters()
    assert (dead.reason, dead.attempts, dead.error) == ("max_attempts", 3, "downstream 503")
    assert consumer.state("acct-1") == (0, 0)


@pytest.mark.unit
def test_transient_failure_that_recovers_clears_its_attempt_counter(
    conn: sqlite3.Connection,
) -> None:
    calls = {"n": 0}

    def flaky(_event: Event) -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise TransientError("timeout")

    consumer = InboxConsumer(conn, GROUP, effect=flaky, max_attempts=3)
    trace = pump(Broker([msg("m1", 1, 10)]), consumer.handle)
    assert [o for _, o in trace] == [Outcome.RETRY, Outcome.RETRY, Outcome.APPLIED]
    assert consumer.dead_letters() == []
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert consumer.state("acct-1") == (1, 10)


@pytest.mark.unit
def test_dead_lettered_redelivery_is_dropped_until_redrive_clears_the_inbox(
    conn: sqlite3.Connection,
) -> None:
    broken = {"on": True}

    def effect(_event: Event) -> None:
        if broken["on"]:
            raise PermanentError("schema bug")

    consumer = InboxConsumer(conn, GROUP, effect=effect)
    original = msg("m1", 1, 10)
    assert consumer.handle(original) is Outcome.DEAD_LETTERED
    assert consumer.handle(original) is Outcome.DUPLICATE
    broken["on"] = False
    redriven = consumer.redrive("m1")
    assert redriven == original
    assert consumer.dead_letters() == []
    assert consumer.handle(redriven) is Outcome.APPLIED
    assert consumer.state("acct-1") == (1, 10)
    with pytest.raises(KeyError):
        consumer.redrive("m1")


@pytest.mark.unit
def test_concurrent_writer_is_refused_by_the_database_lock_not_double_applied(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "shared.db")
    other = InboxConsumer(connect(path, busy_timeout=0.0), GROUP)
    delivery = msg("m1", 1, 10)
    seen: list[Outcome] = []

    def race(_event: Event) -> None:
        seen.append(other.handle(delivery))

    winner = InboxConsumer(connect(path), GROUP, effect=race)
    assert winner.handle(delivery) is Outcome.APPLIED
    assert seen == [Outcome.RETRY]
    assert other.handle(delivery) is Outcome.DUPLICATE
    assert winner.state("acct-1") == (1, 10)


@pytest.mark.unit
def test_inbox_key_is_scoped_by_consumer_group(conn: sqlite3.Connection) -> None:
    billing = InboxConsumer(conn, "billing")
    search = InboxConsumer(conn, "search")
    assert billing.handle(msg("m1", 1, 10)) is Outcome.APPLIED
    assert search.handle(msg("m1", 1, 10)) is Outcome.APPLIED
    assert billing.handle(msg("m1", 1, 10)) is Outcome.DUPLICATE
    assert billing.state("acct-1") == (1, 10)
    assert search.state("acct-1") == (1, 10)
