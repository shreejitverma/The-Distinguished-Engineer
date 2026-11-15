"""Zero-downtime PostgreSQL migrations: a lock-queue model and an Alembic migration linter.

Part 1 models one table's heavyweight lock the way PostgreSQL 18 grants it:
a request is granted only if it conflicts with no granted lock *and* with no
mode already waited for (``lock.c`` ``LockAcquireExtended`` checks
``conflictTab[lockmode] & lock->waitMask``), and wakeups walk the wait queue in
order, accumulating ``aheadRequests`` (``proc.c`` ``ProcLockWakeup``).
That one rule is why a queued ``ACCESS EXCLUSIVE`` request blocks every later
``SELECT`` even though the lock it waits for is only a reader's ``ACCESS SHARE``.

Part 2 renders an Alembic ``upgrade()`` to PostgreSQL SQL in offline mode
(``as_sql=True``, exactly what ``alembic upgrade --sql`` emits) and lints the
statement stream for operations that are unsafe against a live API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

if TYPE_CHECKING:
    from collections.abc import Callable

    import sqlalchemy as sa

# ---------------------------------------------------------------------------
# Part 1: table-level lock modes and the wait queue
# ---------------------------------------------------------------------------


class LockMode(IntEnum):
    """The eight table-level lock modes, numbered as in PostgreSQL ``lockdefs.h``."""

    ACCESS_SHARE = 1
    ROW_SHARE = 2
    ROW_EXCLUSIVE = 3
    SHARE_UPDATE_EXCLUSIVE = 4
    SHARE = 5
    SHARE_ROW_EXCLUSIVE = 6
    EXCLUSIVE = 7
    ACCESS_EXCLUSIVE = 8


_M = LockMode
# PostgreSQL 18 documentation Table 13.2 (Conflicting Lock Modes); lock.c LockConflicts[].
CONFLICTS: dict[LockMode, frozenset[LockMode]] = {
    _M.ACCESS_SHARE: frozenset({_M.ACCESS_EXCLUSIVE}),
    _M.ROW_SHARE: frozenset({_M.EXCLUSIVE, _M.ACCESS_EXCLUSIVE}),
    _M.ROW_EXCLUSIVE: frozenset(
        {_M.SHARE, _M.SHARE_ROW_EXCLUSIVE, _M.EXCLUSIVE, _M.ACCESS_EXCLUSIVE}
    ),
    _M.SHARE_UPDATE_EXCLUSIVE: frozenset(
        {
            _M.SHARE_UPDATE_EXCLUSIVE,
            _M.SHARE,
            _M.SHARE_ROW_EXCLUSIVE,
            _M.EXCLUSIVE,
            _M.ACCESS_EXCLUSIVE,
        }
    ),
    _M.SHARE: frozenset(
        {
            _M.ROW_EXCLUSIVE,
            _M.SHARE_UPDATE_EXCLUSIVE,
            _M.SHARE_ROW_EXCLUSIVE,
            _M.EXCLUSIVE,
            _M.ACCESS_EXCLUSIVE,
        }
    ),
    _M.SHARE_ROW_EXCLUSIVE: frozenset(
        {
            _M.ROW_EXCLUSIVE,
            _M.SHARE_UPDATE_EXCLUSIVE,
            _M.SHARE,
            _M.SHARE_ROW_EXCLUSIVE,
            _M.EXCLUSIVE,
            _M.ACCESS_EXCLUSIVE,
        }
    ),
    _M.EXCLUSIVE: frozenset(set(LockMode) - {_M.ACCESS_SHARE}),
    _M.ACCESS_EXCLUSIVE: frozenset(LockMode),
}


def conflicts(requested: LockMode, other: LockMode) -> bool:
    """Return True when ``requested`` cannot coexist with ``other`` held by another backend."""
    return other in CONFLICTS[requested]


@dataclass(frozen=True, slots=True)
class LockRequest:
    """One transaction asking for one lock on the table at ``arrival_ms``.

    ``hold_ms`` is how long it keeps the lock once granted (until COMMIT).
    ``lock_timeout_ms`` of 0 means wait forever, the PostgreSQL default.
    """

    name: str
    mode: LockMode
    arrival_ms: int
    hold_ms: int
    lock_timeout_ms: int = 0


@dataclass(frozen=True, slots=True)
class LockOutcome:
    name: str
    granted_ms: int | None
    waited_ms: int
    timed_out: bool


def simulate_lock_queue(
    requests: list[LockRequest], *, respect_wait_queue: bool = True
) -> dict[str, LockOutcome]:
    """Replay lock requests against one table and report when each was granted.

    Event order at equal timestamps: releases, then lock timeouts, then arrivals
    (each followed by a wakeup pass). ``respect_wait_queue=False`` drops the
    waitMask rule so tests can show what the rule costs and buys.
    """
    pending = sorted(requests, key=lambda r: (r.arrival_ms, r.name))
    granted: dict[str, tuple[LockRequest, int]] = {}  # name -> (request, release time)
    queue: list[LockRequest] = []
    outcomes: dict[str, LockOutcome] = {}

    def grantable(req: LockRequest, ahead: set[LockMode]) -> bool:
        if any(conflicts(req.mode, g.mode) for g, _ in granted.values()):
            return False
        return not (respect_wait_queue and any(conflicts(req.mode, m) for m in ahead))

    def grant(req: LockRequest, now: int) -> None:
        granted[req.name] = (req, now + req.hold_ms)
        outcomes[req.name] = LockOutcome(req.name, now, now - req.arrival_ms, False)

    def wakeup(now: int) -> None:
        # proc.c ProcLockWakeup: walk the queue in order; a waiter that cannot be
        # granted adds its mode to aheadRequests, which later waiters must respect.
        ahead: set[LockMode] = set()
        for req in list(queue):
            if grantable(req, ahead):
                queue.remove(req)
                grant(req, now)
            else:
                ahead.add(req.mode)

    while pending or queue or granted:
        candidates: list[int] = [release for _, release in granted.values()]
        candidates += [r.arrival_ms + r.lock_timeout_ms for r in queue if r.lock_timeout_ms > 0]
        if pending:
            candidates.append(pending[0].arrival_ms)
        if not candidates:
            raise RuntimeError(f"deadlock in simulation: {[r.name for r in queue]}")
        now = min(candidates)

        for name in [n for n, (_, release) in granted.items() if release == now]:
            del granted[name]
        for req in [
            r for r in queue if r.lock_timeout_ms > 0 and r.arrival_ms + r.lock_timeout_ms == now
        ]:
            # postgres.c ProcessInterrupts: "canceling statement due to lock timeout",
            # SQLSTATE 55P03; lock.c RemoveFromWaitQueue then wakes the queue.
            queue.remove(req)
            outcomes[req.name] = LockOutcome(req.name, None, req.lock_timeout_ms, True)
        wakeup(now)

        while pending and pending[0].arrival_ms == now:
            req = pending.pop(0)
            if grantable(req, {q.mode for q in queue}):
                grant(req, now)
            else:
                queue.append(req)
    return outcomes


# ---------------------------------------------------------------------------
# Part 2: render an Alembic upgrade() offline and lint the SQL it emits
# ---------------------------------------------------------------------------


class _StatementRecorder:
    """Output buffer for Alembic offline mode: ``DefaultImpl.static_output`` writes
    exactly one statement (plus a ``"\\n\\n"`` separator) per ``write`` call."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def write(self, text: str) -> int:
        stripped = text.strip()
        if stripped:
            self.statements.append(stripped)
        return len(text)

    def flush(self) -> None:
        return None


def render_offline_sql(upgrade: Callable[[], None]) -> list[str]:
    """Run ``upgrade()`` (which uses ``from alembic import op``) in offline PostgreSQL
    mode inside one ``BEGIN``/``COMMIT`` block, as ``alembic upgrade --sql`` does."""
    recorder = _StatementRecorder()
    ctx = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": recorder, "transactional_ddl": True},
    )
    with Operations.context(ctx), ctx.begin_transaction():
        upgrade()
    return recorder.statements


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    index: int
    statement: str
    message: str


RULES: dict[str, str] = {
    "ZDM001": "missing-lock-timeout",
    "ZDM002": "add-column-not-null-without-default",
    "ZDM003": "add-column-volatile-default",
    "ZDM004": "set-not-null-without-validated-check",
    "ZDM005": "constraint-without-not-valid",
    "ZDM006": "unique-or-primary-key-builds-index",
    "ZDM007": "index-without-concurrently",
    "ZDM008": "concurrently-inside-transaction",
    "ZDM009": "column-type-change",
    "ZDM010": "rename-breaks-running-code",
    "ZDM011": "drop-still-mapped-by-app",
    "ZDM012": "long-operation-under-held-lock",
}

_NAME = r'"?([\w$]+)"?(?:\."?([\w$]+)"?)?'
_ALTER_TABLE = re.compile(rf"^ALTER TABLE (?:IF EXISTS )?(?:ONLY )?{_NAME}\s+(.*)$", re.I | re.S)
_CREATE_TABLE = re.compile(rf"^CREATE (?:UNLOGGED )?TABLE (?:IF NOT EXISTS )?{_NAME}", re.I)
_INDEX = re.compile(r"^(CREATE (?:UNIQUE )?INDEX|DROP INDEX)( CONCURRENTLY)?\b", re.I)
_INDEX_ON = re.compile(rf"\bON (?:ONLY )?{_NAME}", re.I)
_DROP_TABLE = re.compile(rf"^DROP TABLE (?:IF EXISTS )?{_NAME}", re.I)
_SET_TIMEOUT = re.compile(
    r"^SET (LOCAL |SESSION )?lock_timeout\s*(?:=|TO)\s*'?([^';]+?)'?\s*;?$", re.I
)
_RESET_TIMEOUT = re.compile(r"^RESET lock_timeout\s*;?$", re.I)
_VOLATILE = re.compile(
    r"\b(random|gen_random_uuid|uuidv4|uuidv7|clock_timestamp|timeofday|nextval)\s*\("
    r"|\b(SMALL|BIG)?SERIAL\b|\bGENERATED\b.*\b(STORED|IDENTITY)\b",
    re.I,
)
_DML = re.compile(r"^(UPDATE|INSERT|DELETE|MERGE)\b", re.I)
_ADD_COLUMN = re.compile(r'ADD (?:COLUMN )?(?:IF NOT EXISTS )?"?(\w+)"? (.*)', re.I)
_ADD_CONSTRAINT = re.compile(r'ADD CONSTRAINT "?(\w+)"? (.*)', re.I)
_NOT_NULL_CHECK = re.compile(r'(?:CHECK \(\s*"?(\w+)"? IS NOT NULL\s*\)|NOT NULL "?(\w+)"?)', re.I)
_VALIDATE = re.compile(r'VALIDATE CONSTRAINT "?(\w+)"?', re.I)
_SET_NOT_NULL = re.compile(r'ALTER (?:COLUMN )?"?(\w+)"? SET NOT NULL', re.I)
_TYPE_CHANGE = re.compile(r"ALTER (?:COLUMN )?\S+ (?:SET DATA )?TYPE ", re.I)
_DROP_COLUMN = re.compile(r'DROP (?:COLUMN )?(?:IF EXISTS )?"?(\w+)"?', re.I)
_UNITS_MS = {"us": 0.001, "ms": 1.0, "s": 1000.0, "min": 60_000.0, "h": 3_600_000.0}


def parse_timeout_ms(value: str) -> int:
    """Parse a PostgreSQL time GUC value (``'2s'``, ``100``, ``'1min'``) to milliseconds."""
    text = value.strip().lower()
    if text == "default":
        return 0
    match = re.fullmatch(r"(\d+)\s*(us|ms|s|min|h)?", text)
    if match is None:
        raise ValueError(f"unparseable lock_timeout {value!r}")
    return int(int(match.group(1)) * _UNITS_MS[match.group(2) or "ms"])


def _table(match: re.Match[str], first: int) -> str:
    """Unqualified, lower-cased table name from a ``schema.table`` or ``table`` capture."""
    name = match.group(first + 1) or match.group(first)
    return name.lower()


def statement_lock(statement: str) -> LockMode | None:
    """Strongest table lock the statement takes on an existing table (PostgreSQL 18 docs)."""
    sql = " ".join(statement.split())
    upper = sql.upper()
    if (index := _INDEX.match(sql)) is not None:
        if index.group(2):
            return LockMode.SHARE_UPDATE_EXCLUSIVE
        is_create = index.group(1).upper().startswith("CREATE")
        return LockMode.SHARE if is_create else LockMode.ACCESS_EXCLUSIVE
    if (alter := _ALTER_TABLE.match(sql)) is not None:
        action = alter.group(3).upper()
        if action.startswith("VALIDATE CONSTRAINT"):
            return LockMode.SHARE_UPDATE_EXCLUSIVE
        if re.match(r"ADD CONSTRAINT \S+ FOREIGN KEY", action):
            return LockMode.SHARE_ROW_EXCLUSIVE
        return LockMode.ACCESS_EXCLUSIVE
    if upper.startswith(("DROP TABLE", "TRUNCATE", "LOCK ", "REINDEX ", "CLUSTER ")):
        return LockMode.ACCESS_EXCLUSIVE
    if _DML.match(sql) is not None:
        return LockMode.ROW_EXCLUSIVE
    return None


@dataclass
class _State:
    in_transaction: bool = False
    session_timeout_ms: int = 0
    local_timeout_ms: int | None = None
    created_tables: set[str] = field(default_factory=set[str])
    not_null_checks: dict[str, tuple[str, str]] = field(default_factory=dict[str, tuple[str, str]])
    validated_not_null: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    blocking_locks_held: list[str] = field(default_factory=list[str])
    ignored_set_local: bool = False

    def effective_timeout_ms(self) -> int:
        if self.in_transaction and self.local_timeout_ms is not None:
            return self.local_timeout_ms
        return self.session_timeout_ms

    def end_transaction(self) -> None:
        self.in_transaction = False
        self.local_timeout_ms = None
        self.blocking_locks_held.clear()


def _mapped_columns(metadata: sa.MetaData | None) -> dict[str, set[str]]:
    """Table name -> column names the currently deployed app still maps."""
    mapped: dict[str, set[str]] = {}
    if metadata is not None:
        for name, table in metadata.tables.items():
            mapped[name.split(".")[-1].lower()] = {str(c.name).lower() for c in table.columns}
    return mapped


def lint_statements(
    statements: list[str], *, app_metadata: sa.MetaData | None = None
) -> list[Finding]:
    """Lint PostgreSQL statements in execution order, tracking transaction and GUC state."""
    findings: list[Finding] = []
    state = _State()
    mapped = _mapped_columns(app_metadata)

    for i, raw in enumerate(statements):
        sql = " ".join(raw.split()).rstrip(";")
        upper = sql.upper()

        def report(rule: str, message: str, *, _i: int = i, _raw: str = raw) -> None:
            findings.append(Finding(rule, _i, _raw, message))

        # Transaction and session state.
        if upper in ("BEGIN", "START TRANSACTION"):
            state.in_transaction = True
            continue
        if upper in ("COMMIT", "END", "ROLLBACK"):
            state.end_transaction()
            continue
        if (setting := _SET_TIMEOUT.match(sql)) is not None:
            ms = parse_timeout_ms(setting.group(2))
            if (setting.group(1) or "").strip().upper() == "LOCAL":
                if state.in_transaction:
                    state.local_timeout_ms = ms
                else:  # outside a block SET LOCAL only emits a WARNING (sql-set.html)
                    state.ignored_set_local = True
            else:
                state.session_timeout_ms = ms
                state.local_timeout_ms = None if not state.in_transaction else ms
            continue
        if _RESET_TIMEOUT.match(sql) is not None:
            state.session_timeout_ms = 0
            continue
        if (created := _CREATE_TABLE.match(sql)) is not None:
            state.created_tables.add(_table(created, 1))
            continue

        lock = statement_lock(sql)
        alter = _ALTER_TABLE.match(sql)
        index = _INDEX.match(sql)
        index_on = _INDEX_ON.search(sql) if index is not None else None
        table = (
            _table(alter, 1)
            if alter is not None
            else _table(index_on, 1)
            if index_on is not None
            else None
        )
        new_table = table is not None and table in state.created_tables

        # ZDM012: a scan or backfill while this transaction still holds a lock that
        # blocks writers keeps every API write waiting for the whole scan.
        scans = _DML.match(sql) is not None or "VALIDATE CONSTRAINT" in upper
        if scans and state.in_transaction and state.blocking_locks_held:
            held = ", ".join(sorted(set(state.blocking_locks_held)))
            report("ZDM012", f"scan or backfill runs while this transaction holds locks on {held}")

        # ZDM001: anything that queues in a mode that blocks writers needs a lock_timeout.
        blocks_writers = lock is not None and conflicts(LockMode.ROW_EXCLUSIVE, lock)
        no_timeout = state.effective_timeout_ms() <= 0
        if lock is not None and blocks_writers and not new_table and no_timeout:
            hint = " (an earlier SET LOCAL outside a transaction had no effect)"
            hint *= state.ignored_set_local and not state.in_transaction
            report("ZDM001", f"{lock.name} requested with no lock_timeout in effect{hint}")
        if blocks_writers and not new_table and state.in_transaction and table is not None:
            state.blocking_locks_held.append(table)

        if index is not None:
            if index.group(2) and state.in_transaction:
                report("ZDM008", "CONCURRENTLY cannot run inside a transaction block")
            if not index.group(2) and not new_table:
                report("ZDM007", "plain index build or drop locks out writes; use CONCURRENTLY")
            continue

        if (dropped := _DROP_TABLE.match(sql)) is not None:
            name = _table(dropped, 1)
            if name in mapped:
                report("ZDM011", f"table {name} is still mapped by the running app")
            continue

        if alter is None or new_table:
            continue
        action = alter.group(3)
        action_upper = action.upper()

        add = _ADD_COLUMN.match(action)
        if add is not None and not action_upper.startswith("ADD CONSTRAINT"):
            spec = add.group(2).upper()
            if "NOT NULL" in spec and "DEFAULT" not in spec and "GENERATED" not in spec:
                report("ZDM002", "NOT NULL column without DEFAULT fails on a non-empty table")
            if _VOLATILE.search(add.group(2)):
                report("ZDM003", "volatile default, identity, or stored generated column rewrites")

        if (named := _ADD_CONSTRAINT.match(action)) is not None:
            body = named.group(2).upper()
            not_valid = body.rstrip().endswith("NOT VALID")
            if body.startswith(("FOREIGN KEY", "CHECK")) and not not_valid:
                report("ZDM005", "constraint scans the table under lock; add NOT VALID")
            if body.startswith(("UNIQUE", "PRIMARY KEY")) and "USING INDEX" not in body:
                report("ZDM006", "builds a unique index under ACCESS EXCLUSIVE; use USING INDEX")
            null_check = _NOT_NULL_CHECK.match(named.group(2))
            if null_check is not None and not_valid:
                column = (null_check.group(1) or null_check.group(2)).lower()
                state.not_null_checks[named.group(1).lower()] = (table or "", column)

        if (validate := _VALIDATE.match(action)) is not None:
            target = state.not_null_checks.get(validate.group(1).lower())
            if target is not None:
                state.validated_not_null.add(target)

        not_null = _SET_NOT_NULL.match(action)
        if not_null is not None and (
            (table or "", not_null.group(1).lower()) not in state.validated_not_null
        ):
            report("ZDM004", "SET NOT NULL scans the table under ACCESS EXCLUSIVE")

        if _TYPE_CHANGE.match(action) is not None:
            report("ZDM009", "type change rewrites the table unless binary-coercible")

        if action_upper.startswith("RENAME "):
            report("ZDM010", "old app instances still use the old name during rollout")

        drop = _DROP_COLUMN.match(action)
        if (
            drop is not None
            and not action_upper.startswith("DROP CONSTRAINT")
            and drop.group(1).lower() in mapped.get(table or "", set[str]())
        ):
            report("ZDM011", f"column {drop.group(1)} is still mapped by the running app")
    return findings


def lint_migration(
    upgrade: Callable[[], None], *, app_metadata: sa.MetaData | None = None
) -> list[Finding]:
    """Render ``upgrade()`` offline for PostgreSQL and lint the emitted statements."""
    return lint_statements(render_offline_sql(upgrade), app_metadata=app_metadata)
