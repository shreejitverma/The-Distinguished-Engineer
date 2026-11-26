"""Lab: tamper-evident audit log - canonical JSON, SHA-256 hash chain, checkpoints, and an API."""

import dataclasses
import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI

from api_labs.f18_observability_profiling_and_operations.audit_account_api import (
    Principal,
    create_app,
    seed_account,
)
from api_labs.f18_observability_profiling_and_operations.audit_hash_chain import (
    DOMAIN,
    NO_DIFF_COMMIT,
    Actor,
    AuditError,
    AuditEvent,
    AuditLog,
    AuditTransaction,
    AuditWriteError,
    CanonicalizationError,
    Resource,
    canonical_json,
    compute_entry_hash,
    connect,
    field_diff,
    format_ts,
    frame,
    genesis_hash,
)

T0 = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
CHAIN_KEY = bytes(range(32))


class Clock:
    """Deterministic clock: each call advances one second."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return T0 + timedelta(seconds=self.calls)


class Salts:
    def __init__(self, fill: int = 0) -> None:
        self.calls = fill

    def __call__(self) -> bytes:
        self.calls += 1
        return bytes([self.calls % 256]) * 16


def make_log(
    chain_id: str = "acct", chain_key: bytes | None = None, path: str = ":memory:"
) -> tuple[AuditLog, sqlite3.Connection]:
    conn = connect(path)
    log = AuditLog(conn, chain_id=chain_id, clock=Clock(), salt_source=Salts(), chain_key=chain_key)
    return log, conn


def event(
    n: int = 1,
    *,
    action: str = "account.update",
    subject: str | None = "pat-1",
    diff: Mapping[str, object] | None = None,
) -> AuditEvent:
    return AuditEvent(
        actor=Actor(f"user-{n}"),
        action=action,
        resource=Resource("account", "acc-1"),
        subject=subject,
        diff=diff,
    )


def fill(log: AuditLog, count: int) -> None:
    for n in range(1, count + 1):
        log.append(event(n, diff={"changed": {"balance_minor": [100 + n, 90 + n]}}))


def unlock(conn: sqlite3.Connection) -> None:
    """Attacker with DDL rights: the triggers are a guardrail, not a security boundary."""
    conn.execute("DROP TRIGGER audit_entry_no_delete")
    conn.execute("DROP TRIGGER audit_entry_only_redact")


def forge_suffix(
    log: AuditLog,
    conn: sqlite3.Connection,
    from_seq: int,
    edit: Callable[[str], str],
    key: bytes | None = None,
) -> None:
    """Rewrite rows from `from_seq` on, recomputing every hash with the public algorithm."""
    rows = log.entries()
    prev = rows[from_seq - 2].entry_hash if from_seq > 1 else genesis_hash(log.chain_id, key)
    for row in rows[from_seq - 1 :]:
        header = edit(row.header) if row.seq == from_seq else row.header
        new_hash = compute_entry_hash(
            chain_id=row.chain_id,
            seq=row.seq,
            prev_hash=prev,
            ts=row.ts,
            header=header.encode("utf-8"),
            diff_commit=row.diff_commit,
            key=key,
        )
        conn.execute(
            "UPDATE audit_entry SET header = ?, prev_hash = ?, entry_hash = ? "
            "WHERE chain_id = ? AND seq = ?",
            (header, prev, new_hash, row.chain_id, row.seq),
        )
        prev = new_hash


def length_prefixed(part: bytes) -> bytes:
    return len(part).to_bytes(8, "big") + part


# ---------------------------------------------------------------- canonical JSON


@pytest.mark.unit
def test_canonical_json_sorts_recursively_and_emits_no_whitespace() -> None:
    value = {"b": 1, "a": {"d": [3, {"z": 1, "y": 2}], "c": None}, "s": "x\ny\u00e9", "t": True}
    assert canonical_json(value) == (
        b'{"a":{"c":null,"d":[3,{"y":2,"z":1}]},"b":1,"s":"x\\ny\xc3\xa9","t":true}'
    )


@pytest.mark.unit
def test_canonical_json_orders_keys_by_utf16_code_units_unlike_python_sort_keys() -> None:
    # RFC 8785 Section 3.2.3 test data: U+1F600 (surrogates D83D DE00) sorts before U+FB33.
    data = {
        "\u20ac": "Euro Sign",
        "\r": "Carriage Return",
        "\ufb33": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "\U0001f600": "Emoji: Grinning Face",
        "\u0080": "Control",
        "\u00f6": "Latin Small Letter O With Diaeresis",
    }
    jcs_order = ["\r", "1", "\u0080", "\u00f6", "\u20ac", "\U0001f600", "\ufb33"]
    assert list(json.loads(canonical_json(data))) == jcs_order
    python_order = list(json.loads(json.dumps(data, sort_keys=True)))
    assert python_order == ["\r", "1", "\u0080", "\u00f6", "\u20ac", "\ufb33", "\U0001f600"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [1.5, {"a": 1.0}, float("nan"), 2**53, -(2**53), {1: "x"}, "\ud800", {"\ud800": 1}, object()],
    ids=[
        "float",
        "nested-float",
        "nan",
        "big-int",
        "big-neg",
        "int-key",
        "lone-surr",
        "key-surr",
        "obj",
    ],
)
def test_canonical_json_rejects_values_without_a_canonical_form(bad: object) -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json(bad)


@pytest.mark.unit
def test_canonical_json_keeps_booleans_distinct_from_integers_and_accepts_2_pow_53_minus_1() -> (
    None
):
    assert canonical_json([True, 1, False, 0]) == b"[true,1,false,0]"
    assert canonical_json(2**53 - 1) == b"9007199254740991"


# ---------------------------------------------------------------- framing and layout


@pytest.mark.unit
def test_length_framing_separates_fields_that_plain_concatenation_collides() -> None:
    assert hashlib.sha256(b"a" + b"bc").digest() == hashlib.sha256(b"ab" + b"c").digest()
    assert frame(b"a", b"bc") != frame(b"ab", b"c")
    assert (
        hashlib.sha256(frame(b"a", b"bc")).digest() != hashlib.sha256(frame(b"ab", b"c")).digest()
    )


@pytest.mark.unit
def test_entry_hash_matches_an_independent_byte_level_recomputation() -> None:
    log, _ = make_log("acct")
    first = log.append(event(1))
    second = log.append(event(2, diff={"added": {"x": 1}}))
    genesis = hashlib.sha256(
        DOMAIN + length_prefixed(b"genesis") + length_prefixed(b"acct")
    ).digest()
    assert first.prev_hash == genesis
    expected_first = hashlib.sha256(
        DOMAIN
        + length_prefixed(b"entry")
        + length_prefixed(b"acct")
        + length_prefixed((1).to_bytes(8, "big"))
        + length_prefixed(genesis)
        + length_prefixed(first.ts.encode("ascii"))
        + length_prefixed(first.header.encode("utf-8"))
        + length_prefixed(NO_DIFF_COMMIT)
    ).digest()
    assert first.entry_hash == expected_first
    assert second.prev_hash == first.entry_hash
    assert (
        second.diff_commit
        == hashlib.sha256((second.diff_salt or b"") + b'{"added":{"x":1}}').digest()
    )


@pytest.mark.unit
def test_header_is_canonical_json_of_actor_action_resource_subject_outcome() -> None:
    log, _ = make_log()
    entry = log.append(
        AuditEvent(
            Actor("svc-1", "service", on_behalf_of="user-77"),
            "account.read",
            Resource("account", "acc-1"),
            subject="pat-1",
            request_id="req-9",
        )
    )
    assert entry.header == (
        '{"action":"account.read","actor":{"id":"svc-1","kind":"service","on_behalf_of":"user-77"},'
        '"outcome":"success","reason":null,"request_id":"req-9",'
        '"resource":{"id":"acc-1","type":"account"},"subject":"pat-1","v":1}'
    )


@pytest.mark.unit
def test_timestamps_are_rfc3339_utc_with_fixed_microseconds_and_naive_times_are_refused() -> None:
    log, _ = make_log()
    assert log.append(event()).ts == "2026-11-21T12:00:01.000000Z"
    ist = timezone(timedelta(hours=5, minutes=30))
    assert (
        format_ts(datetime(2026, 11, 21, 17, 30, 0, 5, tzinfo=ist)) == "2026-11-21T12:00:00.000005Z"
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        format_ts(datetime(2026, 11, 21, 12, 0, 0))


@pytest.mark.unit
def test_chain_links_start_at_genesis_and_verify_clean() -> None:
    log, _ = make_log()
    fill(log, 4)
    rows = log.entries()
    assert [r.seq for r in rows] == [1, 2, 3, 4]
    assert rows[0].prev_hash == genesis_hash("acct")
    assert all(rows[i].prev_hash == rows[i - 1].entry_hash for i in range(1, 4))
    report = log.verify()
    assert report.ok
    assert (report.entries, report.head_seq, report.head_hash) == (4, 4, rows[-1].entry_hash)


@pytest.mark.unit
def test_empty_chain_verifies_and_reports_the_genesis_hash_as_head() -> None:
    log, _ = make_log()
    report = log.verify()
    assert (report.ok, report.entries, report.head_seq) == (True, 0, 0)
    assert report.head_hash == genesis_hash("acct")


@pytest.mark.unit
def test_chains_in_one_table_are_independent_and_a_relabelled_row_fails() -> None:
    log_a, conn = make_log("acct-a")
    log_b = AuditLog(conn, chain_id="acct-b", clock=Clock(), salt_source=Salts())
    fill(log_a, 2)
    fill(log_b, 2)
    assert log_a.entries()[0].entry_hash != log_b.entries()[0].entry_hash
    assert log_a.verify().ok
    assert log_b.verify().ok
    unlock(conn)
    conn.execute("DELETE FROM audit_entry WHERE chain_id = 'acct-a' AND seq = 2")
    conn.execute("UPDATE audit_entry SET chain_id = 'acct-a' WHERE chain_id = 'acct-b' AND seq = 2")
    assert "entry_hash_mismatch" in log_a.verify().kinds()


# ---------------------------------------------------------------- tamper detection


@pytest.mark.unit
def test_edited_header_is_detected_at_exactly_that_row() -> None:
    log, conn = make_log()
    fill(log, 4)
    unlock(conn)
    conn.execute(
        "UPDATE audit_entry SET header = replace(header, 'account.update', 'account.read') "
        "WHERE seq = 2"
    )
    report = log.verify()
    assert not report.ok
    assert [(p.seq, p.kind) for p in report.problems] == [(2, "entry_hash_mismatch")]


@pytest.mark.unit
def test_edited_diff_is_detected_by_the_salted_commitment() -> None:
    log, conn = make_log()
    fill(log, 3)
    unlock(conn)
    conn.execute("UPDATE audit_entry SET diff = replace(diff, '[102,92]', '[102,1]') WHERE seq = 2")
    report = log.verify()
    assert [(p.seq, p.kind) for p in report.problems] == [(2, "diff_commit_mismatch")]


@pytest.mark.unit
def test_deleting_a_middle_row_shows_as_a_gap_and_a_broken_link_after_it() -> None:
    log, conn = make_log()
    fill(log, 5)
    unlock(conn)
    conn.execute("DELETE FROM audit_entry WHERE seq = 3")
    report = log.verify()
    assert {(p.seq, p.kind) for p in report.problems} == {(4, "sequence_gap"), (4, "link_broken")}


@pytest.mark.unit
def test_swapping_two_rows_is_detected() -> None:
    log, conn = make_log()
    fill(log, 4)
    unlock(conn)
    conn.execute("UPDATE audit_entry SET seq = -1 WHERE seq = 2")
    conn.execute("UPDATE audit_entry SET seq = 2 WHERE seq = 3")
    conn.execute("UPDATE audit_entry SET seq = 3 WHERE seq = -1")
    report = log.verify()
    assert not report.ok
    assert {"entry_hash_mismatch", "link_broken"} <= report.kinds()


@pytest.mark.unit
def test_triggers_refuse_update_and_delete_but_dropping_them_is_only_caught_by_the_verifier() -> (
    None
):
    log, conn = make_log()
    fill(log, 3)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE audit_entry SET header = '{}' WHERE seq = 2")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM audit_entry WHERE seq = 2")
    assert log.verify().ok
    unlock(conn)
    conn.execute("UPDATE audit_entry SET header = '{}' WHERE seq = 2")
    assert not log.verify().ok


@pytest.mark.unit
def test_tampered_index_column_hides_a_row_from_queries_and_is_flagged() -> None:
    log, conn = make_log()
    fill(log, 3)
    assert [e.seq for e in log.entries_for_subject("pat-1")] == [1, 2, 3]
    unlock(conn)
    conn.execute("UPDATE audit_entry SET subject = 'pat-9' WHERE seq = 2")
    assert [e.seq for e in log.entries_for_subject("pat-1")] == [1, 3]
    assert [(p.seq, p.kind) for p in log.verify().problems] == [(2, "index_column_mismatch")]


# ---------------------------------------------------------------- what a chain cannot see


@pytest.mark.unit
def test_tail_truncation_passes_the_chain_but_not_a_signed_checkpoint() -> None:
    log, conn = make_log()
    key = Ed25519PrivateKey.generate()
    fill(log, 5)
    anchor = log.checkpoint(key)
    fill(log, 2)
    unlock(conn)
    conn.execute("DELETE FROM audit_entry WHERE seq >= 5")
    assert log.verify().ok
    report = log.verify(checkpoint=anchor, public_key=key.public_key())
    assert [(p.seq, p.kind) for p in report.problems] == [(5, "truncated")]


@pytest.mark.unit
def test_rows_after_the_last_checkpoint_are_not_protected_by_it() -> None:
    log, conn = make_log()
    key = Ed25519PrivateKey.generate()
    fill(log, 5)
    anchor = log.checkpoint(key)
    fill(log, 2)
    unlock(conn)
    conn.execute("DELETE FROM audit_entry WHERE seq > 5")
    assert log.verify(checkpoint=anchor, public_key=key.public_key()).ok


@pytest.mark.unit
def test_suffix_rewrite_with_recomputed_hashes_passes_the_chain_but_not_the_checkpoint() -> None:
    log, conn = make_log()
    key = Ed25519PrivateKey.generate()
    fill(log, 4)
    anchor = log.checkpoint(key)
    unlock(conn)
    forge_suffix(log, conn, 3, lambda h: h.replace("account.update", "account.read"))
    assert log.verify().ok
    report = log.verify(checkpoint=anchor, public_key=key.public_key())
    assert [(p.seq, p.kind) for p in report.problems] == [(4, "history_rewritten")]


@pytest.mark.unit
def test_checkpoint_with_wrong_key_or_edited_fields_is_rejected() -> None:
    log, _ = make_log()
    key = Ed25519PrivateKey.generate()
    fill(log, 3)
    anchor = log.checkpoint(key)
    assert log.verify(checkpoint=anchor, public_key=key.public_key()).ok
    other = Ed25519PrivateKey.generate().public_key()
    assert log.verify(checkpoint=anchor, public_key=other).kinds() == {
        "checkpoint_signature_invalid"
    }
    edited = dataclasses.replace(anchor, seq=1)
    assert log.verify(checkpoint=edited, public_key=key.public_key()).kinds() == {
        "checkpoint_signature_invalid"
    }
    with pytest.raises(ValueError, match="public_key"):
        log.verify(checkpoint=anchor)


@pytest.mark.unit
def test_checkpoint_for_another_chain_is_refused() -> None:
    log_a, _ = make_log("acct-a")
    log_b, _ = make_log("acct-b")
    key = Ed25519PrivateKey.generate()
    fill(log_a, 2)
    fill(log_b, 2)
    anchor = log_b.checkpoint(key)
    assert log_a.verify(checkpoint=anchor, public_key=key.public_key()).kinds() == {
        "checkpoint_wrong_chain"
    }


@pytest.mark.unit
def test_keyed_chain_blocks_a_forger_without_the_key_but_not_one_who_has_it() -> None:
    log, conn = make_log(chain_key=CHAIN_KEY)
    fill(log, 4)
    assert log.verify().ok
    unlock(conn)
    forge_suffix(log, conn, 3, lambda h: h.replace("account.update", "account.read"), key=None)
    assert "entry_hash_mismatch" in log.verify().kinds()
    forge_suffix(log, conn, 3, lambda h: h, key=CHAIN_KEY)
    assert log.verify().ok


# ---------------------------------------------------------------- payload commitment and erasure


@pytest.mark.unit
def test_diff_commitment_is_salted_so_equal_diffs_get_different_commitments() -> None:
    diff = {"changed": {"status": ["active", "frozen"]}}
    log_a = make_log("a")[0]
    log_b = AuditLog(connect(":memory:"), chain_id="a", clock=Clock(), salt_source=Salts(fill=40))
    first, other = log_a.append(event(diff=diff)), log_b.append(event(diff=diff))
    assert first.diff == other.diff
    assert first.diff_salt != other.diff_salt
    assert first.diff_commit != other.diff_commit
    assert first.diff is not None
    assert first.diff_salt is not None
    assert first.diff_commit == hashlib.sha256(first.diff_salt + first.diff.encode()).digest()


@pytest.mark.unit
def test_redaction_erases_the_payload_keeps_the_chain_valid_and_is_itself_recorded() -> None:
    log, _ = make_log()
    fill(log, 3)
    before = log.entries()[1]
    redaction = log.redact(2, Actor("dpo-1"), "erasure request")
    after = log.entries()[1]
    assert (after.diff, after.diff_salt) == (None, None)
    assert after.diff_commit == before.diff_commit
    assert after.entry_hash == before.entry_hash
    assert redaction.seq == 4
    assert redaction.header_data()["action"] == "audit.redact"
    assert redaction.header_data()["resource"]["id"] == "2"
    assert redaction.subject == "pat-1"
    report = log.verify()
    assert report.ok
    assert report.redacted == 1
    with pytest.raises(AuditError, match="no diff"):
        log.redact(2, Actor("dpo-1"), "again")


@pytest.mark.unit
def test_silently_nulling_a_diff_without_a_redaction_event_is_detected() -> None:
    log, conn = make_log()
    fill(log, 3)
    conn.execute("UPDATE audit_entry SET diff = NULL, diff_salt = NULL WHERE seq = 2")
    assert [(p.seq, p.kind) for p in log.verify().problems] == [(2, "unrecorded_redaction")]


@pytest.mark.unit
def test_field_diff_reports_added_removed_changed_and_masks_values() -> None:
    before = {"a": 1, "b": 2, "c": 3, "ssn": "111"}
    after = {"a": 1, "b": 5, "d": 4, "ssn": "222"}
    assert field_diff(before, after, mask=frozenset({"ssn"})) == {
        "added": {"d": 4},
        "removed": {"c": 3},
        "changed": {"b": [2, 5], "ssn": "[masked]"},
    }
    assert field_diff({}, {"ssn": "z"}, mask=frozenset({"ssn"})) == {"changed": {"ssn": "[masked]"}}
    assert field_diff({"a": 1}, {"a": 1}) == {}


# ---------------------------------------------------------------- transactions and concurrency


@pytest.mark.unit
def test_failed_transaction_rolls_back_business_rows_and_leaves_no_sequence_gap() -> None:
    log, conn = make_log()
    conn.execute("CREATE TABLE note (x TEXT)")

    def failing_request() -> None:
        with log.transaction() as tx:
            tx.conn.execute("INSERT INTO note VALUES ('kept?')")
            tx.append(event(1))
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        failing_request()
    assert conn.execute("SELECT count(*) FROM note").fetchone()[0] == 0
    assert log.entries() == []
    assert log.append(event(2)).seq == 1


@pytest.mark.unit
def test_threads_sharing_one_log_produce_one_contiguous_verifiable_chain() -> None:
    log, _ = make_log()

    def worker(n: int) -> None:
        for _ in range(25):
            log.append(event(n))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))
    rows = log.entries()
    assert [r.seq for r in rows] == list(range(1, 201))
    assert log.verify().ok


@pytest.mark.unit
def test_two_connections_to_one_file_serialize_through_begin_immediate(tmp_path: Path) -> None:
    path = str(tmp_path / "audit.db")
    log_a = AuditLog(connect(path), chain_id="acct", clock=Clock(), salt_source=Salts())
    log_b = AuditLog(connect(path), chain_id="acct", clock=Clock(), salt_source=Salts())
    barrier = threading.Barrier(2)

    def worker(log: AuditLog) -> None:
        barrier.wait()
        for n in range(30):
            log.append(event(n))

    threads = [threading.Thread(target=worker, args=(log,)) for log in (log_a, log_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [r.seq for r in log_a.entries()] == list(range(1, 61))
    assert log_a.verify().ok


@pytest.mark.unit
def test_who_did_what_queries_use_the_actor_and_subject_columns() -> None:
    log, _ = make_log()
    log.append(event(1, subject="pat-1"))
    log.append(event(2, subject="pat-2"))
    log.append(event(1, subject="pat-2"))
    assert [e.seq for e in log.entries_for_subject("pat-2")] == [2, 3]
    assert [e.seq for e in log.entries_by_actor("user-1")] == [1, 3]


# ---------------------------------------------------------------- the HTTP API

TOKENS = {
    "tok-teller": Principal("teller-1", frozenset({"teller"})),
    "tok-auditor": Principal("auditor-1", frozenset({"auditor"})),
    "tok-delegate": Principal("svc-1", frozenset({"teller"}), on_behalf_of="user-77"),
}


def bearer(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {name}"}


@pytest.fixture
def world() -> tuple[AuditLog, sqlite3.Connection, FastAPI]:
    log, conn = make_log("accounts")
    seed_account(log, "acc-1", "pat-1", 100)
    return log, conn, create_app(log, TOKENS)


def balance(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT balance_minor FROM account WHERE id = 'acc-1'").fetchone()[0])


async def call(app: FastAPI, method: str, path: str, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_patch_commits_business_change_and_audit_row_together(
    world: tuple[AuditLog, sqlite3.Connection, FastAPI],
) -> None:
    log, conn, app = world
    resp = await call(
        app,
        "PATCH",
        "/accounts/acc-1",
        json={"balance_minor": 90},
        headers={**bearer("tok-teller"), "X-Request-ID": "req-1"},
    )
    assert resp.status_code == 200
    assert balance(conn) == 90
    (entry,) = log.entries()
    header = entry.header_data()
    assert (header["action"], header["actor"]["id"], header["subject"]) == (
        "account.update",
        "teller-1",
        "pat-1",
    )
    assert (header["request_id"], header["outcome"]) == ("req-1", "success")
    assert entry.diff == '{"changed":{"balance_minor":[100,90]}}'
    assert log.verify().ok


@pytest.mark.unit
@pytest.mark.asyncio
async def test_actor_cannot_be_supplied_in_the_request_body(
    world: tuple[AuditLog, sqlite3.Connection, FastAPI],
) -> None:
    log, conn, app = world
    resp = await call(
        app,
        "PATCH",
        "/accounts/acc-1",
        json={"balance_minor": 1, "actor": "root"},
        headers=bearer("tok-teller"),
    )
    assert resp.status_code == 422
    assert balance(conn) == 100
    assert log.entries() == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unauthenticated_call_is_401_and_a_wrong_role_is_403_and_audited_as_denied(
    world: tuple[AuditLog, sqlite3.Connection, FastAPI],
) -> None:
    log, conn, app = world
    anonymous = await call(app, "PATCH", "/accounts/acc-1", json={"balance_minor": 1})
    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"] == "Bearer"
    assert log.entries() == []
    denied = await call(
        app, "PATCH", "/accounts/acc-1", json={"balance_minor": 1}, headers=bearer("tok-auditor")
    )
    assert denied.status_code == 403
    assert balance(conn) == 100
    (entry,) = log.entries()
    header = entry.header_data()
    assert (header["outcome"], header["action"], header["actor"]["id"]) == (
        "denied",
        "account.update",
        "auditor-1",
    )
    assert entry.diff is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reads_are_audited_with_the_data_subject(
    world: tuple[AuditLog, sqlite3.Connection, FastAPI],
) -> None:
    log, _, app = world
    resp = await call(app, "GET", "/accounts/acc-1", headers=bearer("tok-teller"))
    assert resp.status_code == 200
    assert resp.json()["balance_minor"] == 100
    (entry,) = log.entries()
    assert (entry.header_data()["action"], entry.subject, entry.diff) == (
        "account.read",
        "pat-1",
        None,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delegated_call_records_the_acting_party_and_the_delegator(
    world: tuple[AuditLog, sqlite3.Connection, FastAPI],
) -> None:
    log, _, app = world
    await call(app, "GET", "/accounts/acc-1", headers=bearer("tok-delegate"))
    actor = log.entries()[0].header_data()["actor"]
    assert (actor["id"], actor["on_behalf_of"]) == ("svc-1", "user-77")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_fails_closed_with_503_and_no_data_when_the_audit_write_fails(
    world: tuple[AuditLog, sqlite3.Connection, FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    log, _, app = world

    def refuse(_self: AuditTransaction, _event: AuditEvent) -> None:
        raise AuditWriteError("disk full")

    monkeypatch.setattr(AuditTransaction, "append", refuse)
    resp = await call(app, "GET", "/accounts/acc-1", headers=bearer("tok-teller"))
    assert resp.status_code == 503
    assert "balance_minor" not in resp.text
    assert "pat-1" not in resp.text
    assert log.entries() == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_audit_write_rolls_back_the_business_update(
    world: tuple[AuditLog, sqlite3.Connection, FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, conn, app = world

    def refuse(_self: AuditTransaction, _event: AuditEvent) -> None:
        raise AuditWriteError("disk full")

    monkeypatch.setattr(AuditTransaction, "append", refuse)
    resp = await call(
        app, "PATCH", "/accounts/acc-1", json={"balance_minor": 5}, headers=bearer("tok-teller")
    )
    assert resp.status_code == 503
    assert balance(conn) == 100


@pytest.mark.unit
@pytest.mark.asyncio
async def test_subject_query_lists_who_did_what_and_needs_the_auditor_role(
    world: tuple[AuditLog, sqlite3.Connection, FastAPI],
) -> None:
    log, _, app = world
    await call(
        app, "PATCH", "/accounts/acc-1", json={"status": "frozen"}, headers=bearer("tok-teller")
    )
    await call(app, "GET", "/accounts/acc-1", headers=bearer("tok-delegate"))
    forbidden = await call(app, "GET", "/audit/subjects/pat-1", headers=bearer("tok-teller"))
    assert forbidden.status_code == 403
    resp = await call(app, "GET", "/audit/subjects/pat-1", headers=bearer("tok-auditor"))
    assert resp.status_code == 200
    assert [(r["seq"], r["actor"], r["action"]) for r in resp.json()] == [
        (1, "teller-1", "account.update"),
        (2, "svc-1", "account.read"),
    ]
    denied, queried = log.entries()[2:]
    assert (denied.header_data()["outcome"], denied.actor_id, denied.subject) == (
        "denied",
        "teller-1",
        None,
    )
    assert (queried.header_data()["action"], queried.actor_id, queried.subject) == (
        "audit.query",
        "auditor-1",
        "pat-1",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_verify_endpoint_is_clean_then_reports_the_tampered_sequence(
    world: tuple[AuditLog, sqlite3.Connection, FastAPI],
) -> None:
    log, conn, app = world
    await call(
        app, "PATCH", "/accounts/acc-1", json={"balance_minor": 90}, headers=bearer("tok-teller")
    )
    await call(app, "GET", "/accounts/acc-1", headers=bearer("tok-teller"))
    clean = await call(app, "GET", "/audit/verify", headers=bearer("tok-auditor"))
    assert clean.json()["ok"] is True
    assert clean.json()["head_hash"] == log.entries()[-1].entry_hash.hex()
    unlock(conn)
    conn.execute("UPDATE audit_entry SET actor_id = 'nobody' WHERE seq = 1")
    dirty = await call(app, "GET", "/audit/verify", headers=bearer("tok-auditor"))
    assert dirty.json()["ok"] is False
    assert dirty.json()["problems"] == [{"seq": 1, "kind": "index_column_mismatch"}]
