"""Tamper-evident audit log: canonical JSON, a length-framed SHA-256 hash chain, signed checkpoints.

Each row commits to the previous row's hash, so editing, deleting, or reordering a row breaks
the chain at a known sequence number.
A chain alone cannot detect truncation of its tail or a rewrite of its whole suffix by someone
who can recompute hashes; a signed `Checkpoint` (Ed25519) held outside the database can.
The diff payload is committed to by a salted hash so it can be erased later without
invalidating the chain.
"""

import hashlib
import hmac
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

DOMAIN: Final = b"audit-chain/v1\x00"
CHECKPOINT_DOMAIN: Final = b"audit-checkpoint/v1\x00"
NO_DIFF_COMMIT: Final = hashlib.sha256(b"audit-chain/v1/no-diff").digest()
MAX_SAFE_INT: Final = 2**53 - 1
REDACT_ACTION: Final = "audit.redact"

SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS audit_entry (
    chain_id   TEXT    NOT NULL,
    seq        INTEGER NOT NULL,
    ts         TEXT    NOT NULL,
    actor_id   TEXT    NOT NULL,
    subject    TEXT,
    header     TEXT    NOT NULL,
    diff       TEXT,
    diff_salt  BLOB,
    diff_commit BLOB   NOT NULL,
    prev_hash  BLOB    NOT NULL,
    entry_hash BLOB    NOT NULL,
    PRIMARY KEY (chain_id, seq)
);
CREATE INDEX IF NOT EXISTS audit_entry_subject ON audit_entry (subject, seq);
CREATE INDEX IF NOT EXISTS audit_entry_actor ON audit_entry (actor_id, seq);
CREATE TRIGGER IF NOT EXISTS audit_entry_no_delete BEFORE DELETE ON audit_entry
BEGIN SELECT RAISE(ABORT, 'audit_entry is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_entry_only_redact BEFORE UPDATE ON audit_entry
WHEN NOT (
    NEW.chain_id IS OLD.chain_id AND NEW.seq IS OLD.seq AND NEW.ts IS OLD.ts
    AND NEW.actor_id IS OLD.actor_id AND NEW.subject IS OLD.subject
    AND NEW.header IS OLD.header AND NEW.diff_commit IS OLD.diff_commit
    AND NEW.prev_hash IS OLD.prev_hash AND NEW.entry_hash IS OLD.entry_hash
    AND NEW.diff IS NULL AND NEW.diff_salt IS NULL
)
BEGIN SELECT RAISE(ABORT, 'audit_entry is append-only'); END;
"""


class AuditError(Exception):
    """Base class for audit-log failures."""


class AuditWriteError(AuditError):
    """The audit row could not be written; callers must fail closed."""


class CanonicalizationError(AuditError, ValueError):
    """A value has no canonical JSON form in this profile."""


def _canon(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            raise CanonicalizationError("integer outside +-(2**53 - 1); send it as a string")
        return str(value)
    if isinstance(value, float):
        raise CanonicalizationError("floats are not allowed; use integer minor units or strings")
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        keys: list[str] = []
        for key in mapping:
            if not isinstance(key, str):
                raise CanonicalizationError("object keys must be strings")
            keys.append(key)
        keys.sort(key=lambda k: k.encode("utf-16-be"))
        body = ",".join(f"{json.dumps(k, ensure_ascii=False)}:{_canon(mapping[k])}" for k in keys)
        return "{" + body + "}"
    if isinstance(value, list | tuple):
        return "[" + ",".join(_canon(item) for item in cast("Sequence[object]", value)) + "]"
    raise CanonicalizationError(f"unsupported type {type(value).__name__}")


def canonical_json(value: object) -> bytes:
    """Serialize `value` as RFC 8785 JCS for the subset of JSON used here (no floats)."""
    try:
        return _canon(value).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalizationError("string contains a lone surrogate") from exc


def frame(*parts: bytes) -> bytes:
    """Prefix every part with its 8-byte big-endian length so field boundaries are unambiguous."""
    return b"".join(len(part).to_bytes(8, "big") + part for part in parts)


def _digest(data: bytes, key: bytes | None) -> bytes:
    if key is None:
        return hashlib.sha256(data).digest()
    return hmac.digest(key, data, "sha256")


def genesis_hash(chain_id: str, key: bytes | None = None) -> bytes:
    return _digest(DOMAIN + frame(b"genesis", chain_id.encode("utf-8")), key)


def diff_commitment(salt: bytes, diff: bytes) -> bytes:
    return hashlib.sha256(salt + diff).digest()


def compute_entry_hash(
    *,
    chain_id: str,
    seq: int,
    prev_hash: bytes,
    ts: str,
    header: bytes,
    diff_commit: bytes,
    key: bytes | None = None,
) -> bytes:
    payload = DOMAIN + frame(
        b"entry",
        chain_id.encode("utf-8"),
        seq.to_bytes(8, "big"),
        prev_hash,
        ts.encode("ascii"),
        header,
        diff_commit,
    )
    return _digest(payload, key)


def format_ts(moment: datetime) -> str:
    """RFC 3339 Section 5.6 UTC timestamp with fixed microsecond width."""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def field_diff(
    before: Mapping[str, object],
    after: Mapping[str, object],
    mask: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Flat top-level diff; masked fields record that they changed but not their values."""
    added = {k: after[k] for k in after.keys() - before.keys() if k not in mask}
    removed = {k: before[k] for k in before.keys() - after.keys() if k not in mask}
    changed: dict[str, object] = {}
    for key in before.keys() & after.keys():
        if before[key] != after[key]:
            changed[key] = "[masked]" if key in mask else [before[key], after[key]]
    for key in (before.keys() ^ after.keys()) & mask:
        changed[key] = "[masked]"
    diff: dict[str, object] = {}
    if added:
        diff["added"] = added
    if removed:
        diff["removed"] = removed
    if changed:
        diff["changed"] = changed
    return diff


@dataclass(frozen=True, slots=True)
class Actor:
    """The authenticated actor; `on_behalf_of` is the delegating party (RFC 8693 `act` claim)."""

    id: str
    kind: Literal["user", "service", "system"] = "user"
    on_behalf_of: str | None = None


@dataclass(frozen=True, slots=True)
class Resource:
    type: str
    id: str


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Who (actor) did what (action) to which object (resource) about whom (subject)."""

    actor: Actor
    action: str
    resource: Resource
    subject: str | None = None
    outcome: Literal["success", "denied", "error"] = "success"
    request_id: str | None = None
    reason: str | None = None
    diff: Mapping[str, object] | None = None

    def header(self) -> dict[str, object]:
        return {
            "v": 1,
            "actor": {
                "id": self.actor.id,
                "kind": self.actor.kind,
                "on_behalf_of": self.actor.on_behalf_of,
            },
            "action": self.action,
            "resource": {"type": self.resource.type, "id": self.resource.id},
            "subject": self.subject,
            "outcome": self.outcome,
            "request_id": self.request_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class AuditEntry:
    chain_id: str
    seq: int
    ts: str
    actor_id: str
    subject: str | None
    header: str
    diff: str | None
    diff_salt: bytes | None
    diff_commit: bytes
    prev_hash: bytes
    entry_hash: bytes

    def header_data(self) -> dict[str, Any]:
        return cast("dict[str, Any]", json.loads(self.header))


@dataclass(frozen=True, slots=True)
class Checkpoint:
    chain_id: str
    seq: int
    entry_hash: bytes
    ts: str
    signature: bytes

    def message(self) -> bytes:
        return checkpoint_message(self.chain_id, self.seq, self.entry_hash, self.ts)


def checkpoint_message(chain_id: str, seq: int, entry_hash: bytes, ts: str) -> bytes:
    return CHECKPOINT_DOMAIN + frame(
        chain_id.encode("utf-8"), seq.to_bytes(8, "big"), entry_hash, ts.encode("ascii")
    )


@dataclass(frozen=True, slots=True)
class Problem:
    seq: int
    kind: str
    detail: str


@dataclass(frozen=True, slots=True)
class VerifyReport:
    ok: bool
    entries: int
    head_seq: int
    head_hash: bytes
    redacted: int
    problems: tuple[Problem, ...]

    def kinds(self) -> frozenset[str]:
        return frozenset(p.kind for p in self.problems)


def connect(path: str, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Autocommit connection; transactions are opened explicitly with `BEGIN IMMEDIATE`."""
    return sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=timeout)


_COLUMNS: Final = (
    "chain_id, seq, ts, actor_id, subject, header, diff, diff_salt, diff_commit, "
    "prev_hash, entry_hash"
)


def _row(values: tuple[Any, ...]) -> AuditEntry:
    return AuditEntry(*values)


class AuditTransaction:
    """Valid only inside `AuditLog.transaction()`; business SQL and the audit row share it."""

    def __init__(self, log: "AuditLog", conn: sqlite3.Connection) -> None:
        self._log = log
        self.conn = conn

    def append(self, event: AuditEvent) -> AuditEntry:
        return self._log.append_in(self.conn, event)


class AuditLog:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        chain_id: str,
        clock: Callable[[], datetime] | None = None,
        salt_source: Callable[[], bytes] | None = None,
        chain_key: bytes | None = None,
    ) -> None:
        self.chain_id = chain_id
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        self._salt_source = salt_source or (lambda: os.urandom(16))
        self._key = chain_key
        self._lock = threading.Lock()
        conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Generator[AuditTransaction]:
        """Serialize writers: process lock, then `BEGIN IMMEDIATE` for other connections."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield AuditTransaction(self, self._conn)
            except BaseException:
                with suppress(sqlite3.Error):
                    self._conn.execute("ROLLBACK")
                raise
            try:
                self._conn.execute("COMMIT")
            except sqlite3.Error as exc:
                with suppress(sqlite3.Error):
                    self._conn.execute("ROLLBACK")
                raise AuditWriteError(f"commit failed: {exc}") from exc

    def append(self, event: AuditEvent) -> AuditEntry:
        with self.transaction() as tx:
            return tx.append(event)

    def _head(self, conn: sqlite3.Connection) -> tuple[int, bytes]:
        row = conn.execute(
            "SELECT seq, entry_hash FROM audit_entry WHERE chain_id = ? ORDER BY seq DESC LIMIT 1",
            (self.chain_id,),
        ).fetchone()
        if row is None:
            return 0, genesis_hash(self.chain_id, self._key)
        return int(row[0]), bytes(row[1])

    def append_in(self, conn: sqlite3.Connection, event: AuditEvent) -> AuditEntry:
        head_seq, prev_hash = self._head(conn)
        seq = head_seq + 1
        ts = format_ts(self._clock())
        header = canonical_json(event.header())
        diff_text: str | None = None
        salt: bytes | None = None
        commit = NO_DIFF_COMMIT
        if event.diff is not None:
            diff_bytes = canonical_json(event.diff)
            diff_text = diff_bytes.decode("utf-8")
            salt = self._salt_source()
            commit = diff_commitment(salt, diff_bytes)
        entry_hash = compute_entry_hash(
            chain_id=self.chain_id,
            seq=seq,
            prev_hash=prev_hash,
            ts=ts,
            header=header,
            diff_commit=commit,
            key=self._key,
        )
        entry = AuditEntry(
            self.chain_id,
            seq,
            ts,
            event.actor.id,
            event.subject,
            header.decode("utf-8"),
            diff_text,
            salt,
            commit,
            prev_hash,
            entry_hash,
        )
        try:
            conn.execute(
                f"INSERT INTO audit_entry ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",  # noqa: S608
                (
                    entry.chain_id,
                    entry.seq,
                    entry.ts,
                    entry.actor_id,
                    entry.subject,
                    entry.header,
                    entry.diff,
                    entry.diff_salt,
                    entry.diff_commit,
                    entry.prev_hash,
                    entry.entry_hash,
                ),
            )
        except sqlite3.Error as exc:
            raise AuditWriteError(f"insert failed: {exc}") from exc
        return entry

    def entries(self) -> list[AuditEntry]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM audit_entry WHERE chain_id = ? ORDER BY seq",  # noqa: S608
                (self.chain_id,),
            ).fetchall()
        return [_row(r) for r in rows]

    def entries_for_subject(self, subject: str) -> list[AuditEntry]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM audit_entry WHERE chain_id = ? AND subject = ? "  # noqa: S608
                "ORDER BY seq",
                (self.chain_id, subject),
            ).fetchall()
        return [_row(r) for r in rows]

    def entries_by_actor(self, actor_id: str) -> list[AuditEntry]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM audit_entry WHERE chain_id = ? AND actor_id = ? "  # noqa: S608
                "ORDER BY seq",
                (self.chain_id, actor_id),
            ).fetchall()
        return [_row(r) for r in rows]

    def redact(self, seq: int, actor: Actor, reason: str) -> AuditEntry:
        """Erase one entry's diff and record that fact in the chain; the commitment stays."""
        with self.transaction() as tx:
            row = tx.conn.execute(
                "SELECT subject, diff FROM audit_entry WHERE chain_id = ? AND seq = ?",
                (self.chain_id, seq),
            ).fetchone()
            if row is None or row[1] is None:
                raise AuditError(f"entry {seq} does not exist or has no diff to redact")
            tx.conn.execute(
                "UPDATE audit_entry SET diff = NULL, diff_salt = NULL "
                "WHERE chain_id = ? AND seq = ?",
                (self.chain_id, seq),
            )
            return tx.append(
                AuditEvent(
                    actor=actor,
                    action=REDACT_ACTION,
                    resource=Resource("audit_entry", str(seq)),
                    subject=row[0],
                    reason=reason,
                )
            )

    def checkpoint(self, signing_key: Ed25519PrivateKey) -> Checkpoint:
        with self._lock:
            seq, entry_hash = self._head(self._conn)
        ts = format_ts(self._clock())
        message = checkpoint_message(self.chain_id, seq, entry_hash, ts)
        return Checkpoint(self.chain_id, seq, entry_hash, ts, signing_key.sign(message))

    def verify(
        self,
        *,
        checkpoint: Checkpoint | None = None,
        public_key: Ed25519PublicKey | None = None,
    ) -> VerifyReport:
        rows = self.entries()
        problems: list[Problem] = []
        prev_hash = genesis_hash(self.chain_id, self._key)
        expected_seq = 1
        redaction_targets: set[int] = set()
        no_diff_with_payload_gone: list[int] = []
        for entry in rows:
            if entry.seq != expected_seq:
                problems.append(
                    Problem(
                        entry.seq, "sequence_gap", f"expected seq {expected_seq}, got {entry.seq}"
                    )
                )
            expected_seq = entry.seq + 1
            if entry.prev_hash != prev_hash:
                problems.append(
                    Problem(entry.seq, "link_broken", "prev_hash is not the previous hash")
                )
            recomputed = compute_entry_hash(
                chain_id=entry.chain_id,
                seq=entry.seq,
                prev_hash=entry.prev_hash,
                ts=entry.ts,
                header=entry.header.encode("utf-8"),
                diff_commit=entry.diff_commit,
                key=self._key,
            )
            if recomputed != entry.entry_hash:
                problems.append(Problem(entry.seq, "entry_hash_mismatch", "stored hash differs"))
            prev_hash = entry.entry_hash
            self._check_diff(entry, problems, no_diff_with_payload_gone)
            self._check_header(entry, problems, redaction_targets)
        for seq in no_diff_with_payload_gone:
            if seq not in redaction_targets:
                problems.append(
                    Problem(seq, "unrecorded_redaction", "diff erased with no audit.redact")
                )
        head_seq = rows[-1].seq if rows else 0
        head_hash = rows[-1].entry_hash if rows else genesis_hash(self.chain_id, self._key)
        if checkpoint is not None:
            self._check_checkpoint(rows, checkpoint, public_key, problems)
        return VerifyReport(
            ok=not problems,
            entries=len(rows),
            head_seq=head_seq,
            head_hash=head_hash,
            redacted=len(no_diff_with_payload_gone),
            problems=tuple(problems),
        )

    @staticmethod
    def _check_diff(entry: AuditEntry, problems: list[Problem], erased: list[int]) -> None:
        if entry.diff is None:
            if entry.diff_commit != NO_DIFF_COMMIT:
                erased.append(entry.seq)
            return
        salt = entry.diff_salt
        if salt is None or diff_commitment(salt, entry.diff.encode("utf-8")) != entry.diff_commit:
            problems.append(
                Problem(entry.seq, "diff_commit_mismatch", "diff does not match commit")
            )

    @staticmethod
    def _check_header(entry: AuditEntry, problems: list[Problem], targets: set[int]) -> None:
        try:
            header = entry.header_data()
            actor_id = header["actor"]["id"]
            subject = header["subject"]
            action = header["action"]
            resource_id = header["resource"]["id"]
        except (ValueError, KeyError, TypeError):
            problems.append(Problem(entry.seq, "header_unparseable", "header is not valid"))
            return
        if actor_id != entry.actor_id or subject != entry.subject:
            problems.append(
                Problem(entry.seq, "index_column_mismatch", "column differs from header")
            )
        if action == REDACT_ACTION and isinstance(resource_id, str) and resource_id.isdigit():
            targets.add(int(resource_id))

    def _check_checkpoint(
        self,
        rows: list[AuditEntry],
        checkpoint: Checkpoint,
        public_key: Ed25519PublicKey | None,
        problems: list[Problem],
    ) -> None:
        if public_key is None:
            raise ValueError("a checkpoint needs the matching public_key")
        try:
            public_key.verify(checkpoint.signature, checkpoint.message())
        except InvalidSignature:
            problems.append(Problem(0, "checkpoint_signature_invalid", "bad signature"))
            return
        if checkpoint.chain_id != self.chain_id:
            problems.append(Problem(0, "checkpoint_wrong_chain", checkpoint.chain_id))
            return
        if checkpoint.seq == 0:
            return
        head_seq = rows[-1].seq if rows else 0
        if head_seq < checkpoint.seq:
            problems.append(
                Problem(
                    checkpoint.seq, "truncated", f"head is {head_seq}, anchored {checkpoint.seq}"
                )
            )
            return
        anchored = next((r for r in rows if r.seq == checkpoint.seq), None)
        if anchored is None:
            problems.append(
                Problem(checkpoint.seq, "checkpoint_entry_missing", "anchored row gone")
            )
        elif anchored.entry_hash != checkpoint.entry_hash:
            problems.append(
                Problem(checkpoint.seq, "history_rewritten", "hash differs from anchor")
            )
