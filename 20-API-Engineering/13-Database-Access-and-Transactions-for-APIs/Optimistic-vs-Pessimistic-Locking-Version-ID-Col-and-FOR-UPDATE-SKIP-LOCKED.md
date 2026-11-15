---
type: pattern
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://www.postgresql.org/docs/18/sql-select.html"
  - "https://www.postgresql.org/docs/18/explicit-locking.html"
  - "https://www.postgresql.org/docs/18/transaction-iso.html"
  - "https://www.postgresql.org/docs/18/errcodes-appendix.html"
  - "https://www.postgresql.org/docs/18/runtime-config-client.html"
  - "https://www.postgresql.org/docs/9.5/release-9-5.html"
  - "https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html"
  - "https://mariadb.com/kb/en/select/"
  - "https://www.sqlite.org/lang_transaction.html"
  - "https://learn.microsoft.com/en-us/sql/t-sql/queries/hints-transact-sql-table"
  - "https://docs.sqlalchemy.org/en/21/orm/versioning.html"
  - "https://www.rfc-editor.org/rfc/rfc9110.html"
  - "https://www.rfc-editor.org/rfc/rfc6585.html"
  - "https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/"
  - "https://owasp.org/API-Security/editions/2023/en/0xa6-unrestricted-access-to-sensitive-business-flows/"
  - "https://github.com/sqlalchemy/sqlalchemy/tree/rel_2_1_8/lib/sqlalchemy/orm"
  - "https://github.com/sqlalchemy/sqlalchemy/tree/rel_2_1_8/lib/sqlalchemy/dialects"
  - "https://www.postgresql.org/docs/18/runtime-config-locks.html"
  - "https://owasp.org/API-Security/editions/2023/en/0xa1-broken-object-level-authorization/"
  - "https://github.com/sqlalchemy/sqlalchemy/issues/396"
---

# Locking Strategies Behind APIs: Optimistic Version Columns (version_id_col / StaleDataError) vs Pessimistic SELECT ... FOR UPDATE NOWAIT / SKIP LOCKED

> [!summary] Claim
> An optimistic version column turns every ORM `UPDATE` into a database-side compare-and-set (`WHERE id = ? AND version = ?`) whose zero-row result SQLAlchemy raises as `StaleDataError`, which is exactly the atomic check an HTTP `If-Match` needs; a pessimistic `SELECT ... FOR UPDATE` instead serializes writers for the length of one transaction, `NOWAIT` turns waiting into SQLSTATE `55P03`, and `SKIP LOCKED` turns a contended table into a work queue, but SQLAlchemy compiles the whole locking clause to nothing on SQLite and SQL Server, so a queue that is correct on PostgreSQL silently double-claims in a SQLite test suite.

Part of [Database Access and Transactions for APIs](README.md) in [API Engineering](../README.md).

## TL;DR

Mechanism: `__mapper_args__ = {"version_id_col": version}` makes the flush emit `UPDATE items SET quantity=?, version=? WHERE items.id = ? AND items.version = ?` and raise `StaleDataError` when the matched rowcount differs from the number of rows it meant to update `(SQLAlchemy 2.1.8 sqlalchemy/orm/persistence.py, _emit_update_statements)`, while `select(...).with_for_update(skip_locked=True)` renders `FOR UPDATE SKIP LOCKED` and holds row locks until commit.
Contract rule: expose the version as a strong `ETag`, require `If-Match` on writes (`428` without it, RFC 6585 Section 3), answer `412` when the version differs at read time or when the versioned `UPDATE` matches zero rows (RFC 9110 Section 13.1.1), and answer `409` only after a bounded server-side retry loop for unconditional commands gives up (RFC 9110 Section 15.5.10).
Trap: everything that bypasses the per-object flush also bypasses the version check, including ORM bulk `update()` (the lab loses a write this way), and a re-`SELECT ... FOR UPDATE` of an object already in the identity map locks the new row but leaves the old attribute values in Python unless you pass `populate_existing=True`.
Portability: PostgreSQL 18.7 behaved as documented in a manual probe (`NOWAIT` -> `55P03`, `lock_timeout` 50 ms fired after 52 ms, three `SKIP LOCKED` workers got `[1, 2]`, `[3, 4]`, `[5]`), MySQL 8.0+ and Oracle render the same clauses, SQLite and SQL Server render none; the 32-test lab passes on CPython 3.14.6, and the drafter's one-run matrix also reported passes on 3.12.13, 3.13.18, 3.14.10, 3.15.2, 3.14.6t free-threaded, and SQLAlchemy 2.0.57 (not re-run at verification).
When not to use either: if the write is one arithmetic statement (`UPDATE ... SET quantity = quantity - :n WHERE id = :id AND quantity >= :n`), let the row lock inside that single statement do the work, and if a queue needs fan-out, replay, or more throughput than one table's hot index, use a broker instead of `SKIP LOCKED`.

## Mental model

```mermaid
sequenceDiagram
    participant A as Client A
    participant B as Client B
    participant API as FastAPI + AsyncSession
    participant DB as PostgreSQL
    Note over A,DB: Optimistic: version column + If-Match
    A->>API: GET /items/1
    API->>DB: SELECT ... WHERE id = 1
    API-->>A: 200, ETag: "v1"
    B->>API: PATCH /items/1<br/>If-Match: "v1"
    API->>DB: UPDATE ... SET version=2<br/>WHERE id=1 AND version=1
    DB-->>API: 1 row matched
    API-->>B: 200, ETag: "v2"
    A->>API: PATCH /items/1<br/>If-Match: "v1"
    API->>DB: SELECT -> version 2
    API-->>A: 412 (version differs)
    Note over API,DB: If B commits between A's SELECT and A's UPDATE,<br/>A's UPDATE matches 0 rows -> StaleDataError -> 412
    Note over A,DB: Pessimistic: SKIP LOCKED queue workers
    API->>DB: W1: SELECT ... LIMIT 2<br/>FOR UPDATE SKIP LOCKED
    DB-->>API: tickets 1, 2 (now row-locked by W1)
    API->>DB: W2: same SELECT
    DB-->>API: tickets 3, 4 (1, 2 skipped, no wait)
    API->>DB: W1: UPDATE ...; COMMIT (locks released)
```

The first half is the optimistic path: the server never holds a lock between requests, the version is the shared clock, and the only place a conflict can be detected atomically is the database's `WHERE version = <what the client saw>`, so the HTTP precondition and the SQL predicate are the same comparison done twice, once early for a cheap `412` and once at write time for correctness.
The note between the halves is the race the early check cannot see: a competing commit lands after the `SELECT` and before the `UPDATE`, the versioned `UPDATE` matches zero rows, SQLAlchemy raises `StaleDataError`, and the API maps it to the same `412`.
The second half is the pessimistic path: worker W1 takes row locks that live until its transaction ends, and W2's `SKIP LOCKED` scan passes over those rows instead of waiting, so the two workers partition the open tickets without coordinating; a plain `FOR UPDATE` would make W2 wait, and `NOWAIT` would make W2 fail with `55P03`.

## How it works

### The anomaly both strategies exist to prevent

A lost update is a read-modify-write where two transactions read the same value, each computes a new value from it, and the second write erases the first.
PostgreSQL's default `READ COMMITTED` level does not prevent it for application-side arithmetic.
In a manual probe against PostgreSQL 18.7 (two `psycopg 3.3.8` connections, CPython 3.14.6, macOS 27.3 arm64), two sessions each read `quantity = 10` and each wrote `quantity - 1`; the final value was `9`, not `8`.
The same probe with `SELECT ... FOR UPDATE` on both reads produced `8`, because the second `SELECT` blocked until the first transaction committed and then returned the updated row.
The isolation-level view of this anomaly, and why `REPEATABLE READ` turns it into an error, belongs to [[Transaction-Isolation-Levels-and-Concurrency-Anomalies-in-APIs]]; this note is about the two application-level strategies for it.

### Optimistic: what `version_id_col` does inside SQLAlchemy

Configuration is one mapper argument; the lab model is:

```python
class Item(Base):
    """A versioned row: every ORM UPDATE or DELETE is guarded by `version`."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    quantity: Mapped[int]
    version: Mapped[int] = mapped_column(nullable=False)

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
```

The default generator is `lambda x: (x or 0) + 1` `(SQLAlchemy 2.1.8 sqlalchemy/orm/mapper.py, Mapper.__init__)`, so the first `INSERT` writes `version = 1`; a probe run of the lab model on SQLite captured `INSERT INTO items (id, name, quantity, version) VALUES (?, ?, ?, ?)` with parameters `(1, 'w', 10, 1)`.
`version_id_generator=False` disables the generator so the application or the database supplies the value, and any callable replaces it, for example `lambda v: uuid.uuid4().hex` for a string version (SQLAlchemy 2.1 documentation, Configuring a Version Counter).

At flush time `_organize_states_for_save` reads the committed (pre-change) version with `mapper._get_committed_state_attr_by_column` `(SQLAlchemy 2.1.8 sqlalchemy/orm/persistence.py, _organize_states_for_save)`, and `_collect_update_commands` stores it under the column's label key and stores `generator(old)` under the column key `(SQLAlchemy 2.1.8 sqlalchemy/orm/persistence.py, _collect_update_commands)`.
`_emit_update_statements` then builds the cached statement by appending `version_id_col == bindparam(version_id_col._label)` to the primary-key criteria, executes it, sums `cursor.rowcount`, and, when the dialect reports `supports_sane_rowcount`, raises:

```text
StaleDataError: UPDATE statement on table 'items' expected to update 1 row(s); 0 were matched.
```

The lab asserts the exact statement and parameters of a successful versioned update on SQLite: `UPDATE items SET quantity=?, version=? WHERE items.id = ? AND items.version = ?` with `(9, 2, 1, 1)`, meaning new quantity 9, new version 2, primary key 1, expected old version 1.

Five consequences of that code path matter in practice:

1. Versioned updates are never batched with `executemany`: the source sets `allow_executemany = not return_defaults and not needs_version_id`, so N dirty versioned objects cost N `UPDATE` round trips `(SQLAlchemy 2.1.8 sqlalchemy/orm/persistence.py, _emit_update_statements)`.
2. If a dialect cannot report a reliable rowcount, SQLAlchemy does not fail; it warns `Dialect ... does not support updated rowcount - versioning cannot be verified.` and carries on, so the guarantee silently degrades to none.
3. `DELETE` gets the same predicate; when the dialect lacks `supports_sane_multi_rowcount` (asyncpg sets it `False`, `(SQLAlchemy 2.1.8 sqlalchemy/dialects/postgresql/asyncpg.py, PGDialect_asyncpg.supports_sane_multi_rowcount)`), versioned deletes are executed one row at a time so each rowcount can be checked, and a mismatch raises `StaleDataError: DELETE statement on table 'items' expected to delete 1 row(s); 0 were matched.  Please set confirm_deleted_rows=False within the mapper configuration to prevent this warning.` `(SQLAlchemy 2.1.8 sqlalchemy/orm/persistence.py, _emit_delete_statements)`.
4. If no mapped attribute has a net change, no `UPDATE` is emitted at all, so "touching" an object does not bump the version; when the only changes live in another table of a multi-table mapping and `version_id_generator=False`, SQLAlchemy sets the version column to its own value so the versioned table still gets a guarded `UPDATE` (the "no net change" branch in `_collect_update_commands`).
5. MySQL drivers report "rows changed", not "rows matched", by default, which would make a no-op `UPDATE` look stale; every SQLAlchemy MySQL dialect sets the `CLIENT.FOUND_ROWS` flag on connect so that `rowcount` means matched rows `(SQLAlchemy 2.1.8 sqlalchemy/dialects/mysql/base.py, module docstring section "rowcount Support")`.

The version check also runs on reads, but only on locked primary-key loads (`Session.get()` and `Session.refresh()` with `with_for_update`): `_load_on_pk_identity` sets `version_check = True` only when `with_for_update` is present, a plain `select(Item).with_for_update()` executed through the session never sets it, and `_validate_version_id` raises `StaleDataError: Instance '<Item ...>' has version id '1' which does not match database-loaded version id '2'.` when the row's version differs from the in-memory one `(SQLAlchemy 2.1.8 sqlalchemy/orm/loading.py, _load_on_pk_identity and _validate_version_id)`.
`Session.get()` skips the identity-map shortcut whenever `with_for_update` is set `(SQLAlchemy 2.1.8 sqlalchemy/orm/session.py, Session._get_impl)`, so `session.get(Item, 1, with_for_update=True)` is the idiom that both locks the row and proves the object you hold is current.
The lab found one subtlety the hard way: the identity map holds objects weakly, so if no Python reference keeps the first-loaded object alive, the locked `get()` simply builds a fresh object and there is nothing to compare.

Server-side versions (`version_id_generator=False` with a `server_default`, a trigger, or PostgreSQL's `xmin` system column) require SQLAlchemy to fetch the new value immediately after each `INSERT` and `UPDATE`; with `RETURNING` that is one statement, without it an extra `SELECT` that can itself race (SQLAlchemy 2.1 documentation, Server Side Version Counters).
The documentation lists PostgreSQL, Oracle Database, MariaDB 10.5, SQLite 3.35, and SQL Server as the `RETURNING` backends and strongly recommends server-side counters only there; the source notes that MySQL and MariaDB lack `UPDATE ... RETURNING`.

### What the database does with `WHERE version = 1` under contention

The predicate is only half of the mechanism; the other half is how the engine treats a row that a concurrent transaction is changing.
Under PostgreSQL `READ COMMITTED`, an `UPDATE` that finds its target row being updated waits for the other transaction, and if that transaction commits, "the search condition of the command (the WHERE clause) is re-evaluated to see if the updated version of the row still matches" (PostgreSQL 18 Section 13.2.1).
The manual PostgreSQL 18.7 probe showed exactly that: session B's `UPDATE items SET quantity=8, version=2 WHERE id=1 AND version=1` blocked while session A held the row, and after A committed `version = 2`, B's `rowcount` was `0`.
That zero is what SQLAlchemy converts into `StaleDataError`, which is why version columns work at `READ COMMITTED` without any explicit lock.

Under `REPEATABLE READ` the same interleaving does not return zero rows; the probe got `SerializationFailure sqlstate=40001 msg='could not serialize access due to concurrent update'`, as documented (PostgreSQL 18 Section 13.2.2).
SQLAlchemy's documentation draws the same line: above `READ COMMITTED` the database raises first, and `version_id_col` remains useful for cross-transaction staleness, for example an object kept across commits with `expire_on_commit=False` (SQLAlchemy 2.1 documentation, Concurrent transaction updates).
An API on `REPEATABLE READ` or `SERIALIZABLE` must therefore treat `StaleDataError` and SQLSTATE `40001` as the same class of conflict.

SQLite has no row locks: a deferred transaction becomes a write transaction on its first write, and only one write transaction exists per database at a time (SQLite documentation, Transaction, Sections 2.1 and 2.2).
Because Python's `sqlite3` driver issues `BEGIN` only before DML, a session that has only read holds nothing, so on SQLite the versioned `UPDATE` sees the competitor's committed `version = 2` and matches zero rows, which is the behavior every lab test relies on.

### Mapping the version onto HTTP preconditions

The version becomes a strong entity tag, `"v<N>"`, which the lab reuses from the sibling If-Match lab (`etag_for`, `parse_if_match`, `expected_versions`), so this note does not re-derive `#entity-tag` parsing or strong comparison; see [[Optimistic-Concurrency-Control-If-Match-and-412-Precondition-Failed]].
RFC 9110 requires an origin server to evaluate `If-Match` "prior to performing the method" with the strong comparison function and to not perform the method if it is false (RFC 9110 Section 13.1.1), and to evaluate preconditions "just before it would ... perform the action associated with the request method" (RFC 9110 Section 13.2.1).
The only moment that is truly "just before" the action is inside the `UPDATE` itself, and `version_id_col` puts the client's version exactly there, because the loaded version equals the `If-Match` version once the early check passed.
So the handler compares early for a cheap `412`, mutates, and commits; a `StaleDataError` at commit means the precondition became false in the gap, and the honest response is the same `412 Precondition Failed`, "one or more conditions given in the request header fields evaluated to false when tested on the server" (RFC 9110 Section 15.5.13).
A write without `If-Match` gets `428 Precondition Required`, whose stated purpose is to avoid the lost update problem (RFC 6585 Section 3).

Commands that are not conditional on a representation, such as "reserve 2 units", have no client version to compare, so the server owns the retry: re-read, re-decide, re-write, at most `MAX_RESERVE_ATTEMPTS = 3` times, then `409 Conflict`, which RFC 9110 defines for a conflict "with the current state of the target resource" that the user might resolve and resubmit (RFC 9110 Section 15.5.10).
Each retry must use a fresh session and a fresh read; retrying the same flush with the same in-memory objects sends the same stale `WHERE version = 1` forever.
Spacing retries with jittered backoff is covered in [[Retries-Exponential-Backoff-Full-vs-Decorrelated-Jitter-and-Stamina]]; the lab retries immediately because the race is injected deterministically.

### Pessimistic: the PostgreSQL locking clause

The clause is `FOR lock_strength [ OF from_reference [, ...] ] [ NOWAIT | SKIP LOCKED ]` with strengths `UPDATE`, `NO KEY UPDATE`, `SHARE`, and `KEY SHARE` (PostgreSQL 18 SQL Commands, SELECT, The Locking Clause).
The strengths differ only in what they conflict with (PostgreSQL 18 Section 13.3.2):

| Requested by another transaction | Held `FOR KEY SHARE` | Held `FOR SHARE` | Held `FOR NO KEY UPDATE` | Held `FOR UPDATE` |
| --- | --- | --- | --- | --- |
| `FOR KEY SHARE` | allowed | allowed | allowed | blocks |
| `FOR SHARE` | allowed | allowed | blocks | blocks |
| `FOR NO KEY UPDATE` | allowed | blocks | blocks | blocks |
| `FOR UPDATE` | blocks | blocks | blocks | blocks |

Plain `UPDATE` takes `FOR NO KEY UPDATE` unless it changes a column with a unique index usable by a foreign key, and `DELETE` takes `FOR UPDATE` (PostgreSQL 18 Section 13.3.2).
Foreign keys make the difference visible: in the probe, with the parent row held `FOR UPDATE`, `INSERT INTO child` failed with `55P03 canceling statement due to lock timeout` (100 ms), and with the parent held `FOR NO KEY UPDATE` the insert succeeded.
The error's context names the cause: the foreign-key check ran `SELECT 1 FROM ONLY "public"."parent" x WHERE "id" OPERATOR(pg_catalog.=) $1 FOR KEY SHARE OF x`, and `FOR KEY SHARE` is the only strength in the table that conflicts with `FOR UPDATE` but not with `FOR NO KEY UPDATE`.
SQLAlchemy maps its keyword arguments to these strengths in the PostgreSQL compiler `(SQLAlchemy 2.1.8 sqlalchemy/dialects/postgresql/base.py, PGCompiler.for_update_clause)`:

| `with_for_update(...)` | PostgreSQL rendering (lab-asserted) |
| --- | --- |
| no arguments | `FOR UPDATE` |
| `nowait=True` | `FOR UPDATE NOWAIT` |
| `skip_locked=True` | `FOR UPDATE SKIP LOCKED` |
| `key_share=True` | `FOR NO KEY UPDATE` |
| `read=True` | `FOR SHARE` |
| `read=True, key_share=True` | `FOR KEY SHARE` |
| `of=Ticket, skip_locked=True` | `FOR UPDATE OF tickets SKIP LOCKED` |
| `nowait=True, skip_locked=True` | `FOR UPDATE NOWAIT SKIP LOCKED` (PostgreSQL rejects it: `42601 syntax error at or near "SKIP"`) |

`NOWAIT` "reports an error, rather than waiting, if a selected row cannot be locked immediately", and `SKIP LOCKED` skips such rows, which "provides an inconsistent view of the data, so this is not suitable for general purpose work, but can be used to avoid lock contention with multiple consumers accessing a queue-like table" (PostgreSQL 18 SELECT, The Locking Clause).
Both apply only to row locks; the `ROW SHARE` table lock is still taken normally, so a pending `ACCESS EXCLUSIVE` from a migration still blocks a `NOWAIT` select, which is where [[Zero-Downtime-Schema-Migrations-Expand-Contract-and-Lock-Timeouts]] and this note meet.
The probe's error texts were `LockNotAvailable sqlstate=55P03 msg='could not obtain lock on row in relation "items"'` for `NOWAIT`, and `55P03 canceling statement due to lock timeout` for a plain `FOR UPDATE` under `SET lock_timeout = '50ms'`; both share SQLSTATE `55P03` (`lock_not_available`, PostgreSQL 18 Appendix A), so the message, not the code, tells you which one fired.

Three more documented details shape queue queries:
with `LIMIT`, "locking stops once enough rows have been returned to satisfy the limit", but rows skipped by `OFFSET` are locked;
with `ORDER BY` at `READ COMMITTED`, a locking `SELECT` can return rows out of order, because sorting happens before the lock wait and the re-read row may have new sort values;
and row locks are not kept in shared memory, "so there is no limit on the number of rows locked at one time", but marking a row locked writes to the tuple, so `SELECT FOR UPDATE` causes disk writes (PostgreSQL 18 Section 13.3.2).
The probe saw the second half of that directly: holding two rows `FOR UPDATE`, `pg_locks` for the table showed only `('relation', 'RowShareLock')`, no per-row entries.
`SKIP LOCKED` arrived in PostgreSQL 9.5 (PostgreSQL 9.5 release notes, E.26).

### Pessimistic locks meet the identity map

A locking `SELECT` re-reads the row, but the ORM does not overwrite attributes of an object already in the identity map; the lab loaded a ticket as `open`, let another connection commit `claimed`, ran `select(Ticket).where(Ticket.id == 1).with_for_update()`, got back the same Python object, and read `status == "open"`.
Only `execution_options(populate_existing=True)` refreshed it to `claimed`.
On PostgreSQL that means a handler can hold the correct lock and still decide from stale data, which is worse than no lock because it looks safe in review.
A versioned mapping does not change this for `select(...).with_for_update()`: a verification probe on SQLite with the lab's `Item` returned the same object still at `version == 1` after another session committed `version = 2`, with no error, because only primary-key loads carry the version check; `session.get(Item, 1, with_for_update=True)` on the same state raised `StaleDataError`, and the stale object still cannot win at flush, where the versioned `UPDATE` matches zero rows.
With joined eager loading of a collection plus `LIMIT` or `OFFSET`, the ORM wraps the query in a subquery, puts the `FOR UPDATE` on the inner query, and also puts it on the outer query unless `of` is given (so PostgreSQL can apply `OF` inside), so that all joined rows are locked `(SQLAlchemy 2.1.8 sqlalchemy/orm/context.py, _ORMSelectCompileState._compound_eager_statement)`.

### Dialects that compile the lock away

`with_for_update()` is not validated against the backend; each compiler decides what to emit:

- PostgreSQL and Oracle render `NOWAIT` and `SKIP LOCKED` as written; Oracle omits the clause inside subqueries `(SQLAlchemy 2.1.8 sqlalchemy/dialects/oracle/base.py, OracleCompiler.for_update_clause)`.
- MySQL renders `FOR UPDATE ... NOWAIT ... SKIP LOCKED` `(SQLAlchemy 2.1.8 sqlalchemy/dialects/mysql/base.py, MySQLCompiler.for_update_clause)`, but `read=True` becomes `LOCK IN SHARE MODE` and `of=` is dropped until a live connection reports server version 8.0+ (`supports_for_update_of`) and 8.0.1+ (`use_mysql_for_share`) `(SQLAlchemy 2.1.8 sqlalchemy/dialects/mysql/base.py, MySQLDialect._initialize_mysql)`; the lab asserts both renderings.
  MySQL documents that locking reads work only with autocommit disabled, that `NOWAIT` fails with `ERROR 3572 (HY000): Do not wait for lock.`, and that `NOWAIT` and `SKIP LOCKED` are unsafe for statement-based replication (MySQL 8.4 Reference Manual, Locking Read Concurrency with NOWAIT and SKIP LOCKED).
- SQLite returns an empty string with the source comment "sqlite has no "FOR UPDATE" AFAICT" `(SQLAlchemy 2.1.8 sqlalchemy/dialects/sqlite/base.py, SQLiteCompiler.for_update_clause)`.
- SQL Server returns an empty string because "FOR UPDATE" is only allowed on "DECLARE CURSOR" `(SQLAlchemy 2.1.8 sqlalchemy/dialects/mssql/base.py, MSSQLCompiler.for_update_clause)`; the native equivalents are table hints, `WITH (UPDLOCK, ROWLOCK)` for update locks held to end of transaction and `READPAST` for queue readers that skip locked rows (Microsoft Learn, Table hints).

No warning is emitted in the SQLite or SQL Server case, and that is the failure mode the lab demonstrates next.

### The queue claim: SKIP LOCKED, its silent failure, and the portable fix

The lab's two-step claim is the textbook PostgreSQL pattern:

```python
def claimable(batch: int, *, skip_locked: bool = True, nowait: bool = False) -> Select[Ticket]:
    """The queue-claim SELECT: oldest open tickets, locked, skipping rows others hold."""
    return (
        select(Ticket)
        .where(Ticket.status == "open")
        .order_by(Ticket.id)
        .limit(batch)
        .with_for_update(skip_locked=skip_locked, nowait=nowait)
    )
```

Run unchanged against PostgreSQL 18.7 through both `postgresql+psycopg` and `postgresql+asyncpg`, two concurrent sessions calling `select_claimable(session, 2)` got `[1, 2]` and `[3, 4]`.
Run unchanged against SQLite, both sessions got `[1, 2]`, both `mark_claimed` writes succeeded, and the final owners were `{1: "w2", 2: "w2", 3: None, 4: None}`: worker w1 believes it owns tickets the database says belong to w2, and no error was raised anywhere.

The portable fix is to stop relying on the read lock and make the write itself the arbiter, the same compare-and-set idea as the version column:

```python
async def guarded_claim(
    session: AsyncSession, candidate_ids: Sequence[int], worker: str
) -> list[int]:
    """Portable compare-and-set claim: only rows still `open` at write time are taken."""
    result = await session.execute(
        update(Ticket)
        .where(Ticket.id.in_(candidate_ids), Ticket.status == "open")
        .values(status="claimed", claimed_by=worker)
        .returning(Ticket.id)
    )
    return sorted(result.scalars().all())
```

Under the identical interleaving, w1 claimed `[1, 2]` and w2 claimed `[]`.
On PostgreSQL the best form combines both into one statement, `UPDATE tickets SET status='claimed', claimed_by=%s WHERE id IN (SELECT id FROM tickets WHERE status='open' ORDER BY id LIMIT 2 FOR UPDATE SKIP LOCKED) RETURNING id`, which the probe ran from two connections and got `[1, 2]` and `[3, 4]`: no waiting from `SKIP LOCKED`, no double claim from the guarded `status`, and one round trip.
A claim that commits immediately and records `claimed_at` (a lease) survives worker crashes better than a claim that holds the row lock for the whole job, because a lock held for minutes ties up a pooled connection and an open transaction for minutes; the outbox relay in [[Transactional-Outbox-Pattern-Solving-Dual-Writes-Between-DB-and-Events]] is the same pattern applied to event publishing.

## Numbers and limits

| Quantity | Value | Source |
| --- | --- | --- |
| First version written by the default generator | `1`, then `+1` per flushed `UPDATE` | `(SQLAlchemy 2.1.8 sqlalchemy/orm/mapper.py, Mapper.__init__)`; lab `test_insert_sets_version_1_and_update_guards_and_bumps_it` |
| `UPDATE` statements for N dirty versioned rows | N (no `executemany`) | `(SQLAlchemy 2.1.8 sqlalchemy/orm/persistence.py, _emit_update_statements)` |
| Lab server-side retry budget | `MAX_RESERVE_ATTEMPTS = 3`, then `409` | lab `row_locking.py` |
| PostgreSQL `lock_timeout` default | `0` (disabled), applies per lock acquisition attempt | PostgreSQL 18 Section 19.11.1 |
| PostgreSQL `deadlock_timeout` | `1s` (default; `SHOW` output on a fresh 18.7 cluster) | PostgreSQL 18 Section 19.12; manual probe, PostgreSQL 18.7 Homebrew, macOS 27.3 arm64 |
| `lock_timeout = '50ms'` against a held row | `55P03` after 52 ms (one run at verification, psycopg 3.3.8, CPython 3.14.6; the drafter's run saw 51 ms) | manual probe, same machine |
| Row locks in shared memory | none; no limit on rows locked, but each lock writes the tuple | PostgreSQL 18 Section 13.3.2; probe `pg_locks` showed only `RowShareLock` |
| SQLSTATE for `NOWAIT` and `lock_timeout` | `55P03` `lock_not_available` | PostgreSQL 18 Appendix A |
| SQLSTATE for `REPEATABLE READ` write conflict | `40001` `serialization_failure` | PostgreSQL 18 Appendix A, Section 13.2.2; probe |
| SQLSTATE for `NOWAIT SKIP LOCKED` together | `42601` syntax error | probe |
| MySQL `NOWAIT` error | `ERROR 3572 (HY000)` | MySQL 8.4 Reference Manual |
| First PostgreSQL release with `SKIP LOCKED` | 9.5 | PostgreSQL 9.5 release notes |
| MySQL server version that enables `FOR SHARE` / `OF` in SQLAlchemy | 8.0.1 / 8.0 | `(SQLAlchemy 2.1.8 sqlalchemy/dialects/mysql/base.py, MySQLDialect._initialize_mysql)` |
| Lab size and time | 32 tests, `32 passed in 2.00s` (one run; wall time includes cold imports) | `uv run --frozen pytest <module> -q`, CPython 3.14.6, macOS 27.3 arm64 |

## Portability

| Dimension | Result | Evidence |
| --- | --- | --- |
| CPython 3.12.13 | 32 passed (drafter's run; not re-run at verification, lab-check pause; the contract's pinned leg is 3.12.16) | lab, temporary venv via `uv run --frozen --python 3.12.13` |
| CPython 3.13.18 | 32 passed (drafter's run; not re-run at verification) | lab, same method |
| CPython 3.14.6 (workspace) | 32 passed (re-run at verification) | lab, `uv run --frozen pytest` |
| CPython 3.14.10 | 32 passed (drafter's run; not re-run at verification) | lab, temporary venv |
| CPython 3.15.2 | 32 passed (drafter's run; not re-run at verification) | lab, temporary venv |
| CPython 3.14.6t free-threaded (`sys._is_gil_enabled()` `False`) | 32 passed (drafter's run; not re-run at verification) | lab, venv with only imported packages pinned from `uv export --frozen` |
| CPython 3.15.2t free-threaded | not run | not installed here; 3.15.0b3t was, and segfaulted in `greenlet 3.5.6` initialization at `import sqlalchemy.ext.asyncio`, before any lab code ran (drafter's report) |
| SQLAlchemy 2.1.8 vs 2.0.57 | 32 passed on both (2.0.57 leg is the drafter's run; not re-run at verification) | lab, `--no-group sqlalchemy21 --group sqlalchemy20`; 2.1 maps `postgresql://` to `PGDialect_psycopg` (renders `::INTEGER` casts), 2.0.57 to `PGDialect_psycopg2` (no casts), so the claim-query assertion allows both |
| SQLite 3.53.1 and 3.54.1 (bundled with the interpreters above) | no `FOR UPDATE` emitted; double claim reproduced; versioned `UPDATE` works | lab |
| PostgreSQL 18.7 | `NOWAIT`, `SKIP LOCKED`, `lock_timeout`, `READ COMMITTED` re-evaluation, `40001` under `REPEATABLE READ` all as documented | manual probe (psycopg 3.3.8 and asyncpg 0.32.0), re-run at verification on a fresh 18.7 Homebrew cluster with identical results; not part of the hermetic lab |
| MySQL 8.0+, MariaDB | same SQLAlchemy rendering; `of=` and `FOR SHARE` need a connection-detected version | SQLAlchemy source, MySQL 8.4 manual, MariaDB KB `SELECT`; not executed |
| Oracle Database | `FOR UPDATE NOWAIT` / `SKIP LOCKED` rendered | lab (compile only) |
| SQL Server | clause dropped; use `UPDLOCK`, `ROWLOCK`, `READPAST` hints | lab (compile only), Microsoft Learn |
| Uvicorn, Hypercorn, Granian | not exercised; the lab runs in-process through `httpx.ASGITransport`, and nothing in the mechanism depends on the server | design argument, not a test |
| HTTP/1.1, HTTP/2, HTTP/3 | `ETag`, `If-Match`, `412`, `428`, and `409` are version-independent HTTP semantics | RFC 9110 Sections 8.8.3, 13.1.1, 15.5.10, 15.5.13; RFC 6585 Section 3 |

Container legs (Debian Trixie, Alpine musl) were not run; the 2026-11-11 pause on lab checks applies.

## Worked example

The [implementation](../labs/src/api_labs/f13_database_access_and_transactions_for_apis/row_locking.py) holds the two models, the claim functions, and a FastAPI app; the [test](../labs/tests/f13_database_access_and_transactions_for_apis/test_optimistic_vs_pessimistic_locking_version_id_col_and_for_update_skip_locked.py) drives SQLite through `aiosqlite` with a file database so every session gets its own connection, as in production.
The race is injected, not hoped for: `build_app(sessions, race)` calls `race(item_id, attempt)` after the read and before the commit, and the tests pass a coroutine that commits a competing write through another session at exactly that point.

The conditional write handler (simplified excerpt, verbatim lines from `build_app`):

```python
    @app.patch("/items/{item_id}")
    async def patch_item(
        item_id: int,
        patch: ItemPatch,
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ) -> dict[str, int | str]:
        if if_match is None:
            raise HTTPException(428, "send If-Match with the ETag you read")
        try:
            allowed = expected_versions(parse_if_match([if_match]))
        except MalformedPreconditionError as exc:
            raise HTTPException(400, str(exc)) from exc
        async with sessions() as session:
            item = await load(session, item_id)
            if allowed is not None and item.version not in allowed:
                raise HTTPException(412, "If-Match does not match the current ETag")
            item.quantity = patch.quantity
            await race(item_id, 1)
            try:
                # The flushed UPDATE carries "AND version = <the version If-Match named>",
                # so the precondition is re-checked atomically by the database.
                await session.commit()
            except StaleDataError as exc:
                raise HTTPException(412, "the resource changed after If-Match was checked") from exc
            response.headers["ETag"] = str(etag_for(item.version))
            return _body(item)
```

The server-side retry for an unconditional command (simplified excerpt, verbatim lines from `build_app`):

```python
        for attempt in range(1, MAX_RESERVE_ATTEMPTS + 1):
            async with sessions() as session:
                item = await load(session, item_id)
                if item.quantity < reservation.amount:
                    raise HTTPException(409, "insufficient quantity")
                item.quantity -= reservation.amount
                await race(item_id, attempt)
                try:
                    await session.commit()
                except StaleDataError:
                    continue  # re-read the new version and re-apply the decision
                response.headers["ETag"] = str(etag_for(item.version))
                response.headers["X-Attempts"] = str(attempt)
                return _body(item)
        raise HTTPException(409, f"gave up after {MAX_RESERVE_ATTEMPTS} conflicting attempts")
```

The race test (simplified excerpt, verbatim lines from the test module):

```python
async def test_race_after_the_precondition_check_is_caught_by_the_versioned_update(
    sessions: Sessions,
) -> None:
    async def race(item_id: int, attempt: int) -> None:
        await bump_quantity(sessions, 7)

    async with client_for(sessions, race) as client:
        response = await client.patch(
            "/items/1", json={"quantity": 3}, headers={"If-Match": '"v1"'}
        )
        assert response.status_code == 412
        after = await client.get("/items/1")
    assert after.json()["quantity"] == 7  # the competing write survives
    assert after.headers["etag"] == '"v2"'
```

What the 32 tests prove, grouped:

- Version mechanics: first version is 1 and the `UPDATE` carries `AND items.version = ?` with `(9, 2, 1, 1)`; a concurrent commit makes the loser's flush match 0 rows and write nothing; a stale `DELETE` raises; `get(..., with_for_update=True)` detects version drift that plain `get()` hides; an ORM bulk `update()` leaves `version` at 1, so a later stale flush overwrites the bulk write (final `quantity == 5`, `version == 2`).
- Identity map: a re-select `with_for_update()` returns the same object with the stale `status == "open"`; `populate_existing=True` returns `claimed`.
- Rendering: the eight PostgreSQL strengths in the table above, the claim query's `LIMIT ... FOR UPDATE SKIP LOCKED` tail, MySQL's version-dependent `LOCK IN SHARE MODE` vs `FOR SHARE OF tickets`, and the empty clause on SQLite and SQL Server next to Oracle's `FOR UPDATE NOWAIT`.
- Claims: the two-step `SKIP LOCKED` claim double-claims on SQLite; the guarded `UPDATE ... WHERE status = 'open' RETURNING id` stays disjoint under the same interleaving.
- HTTP: strong `ETag: "v1"`; `428` without `If-Match`, `412` for a stale or weak tag, `400` for an unquoted tag, each with no write; `200` and `ETag: "v2"` for `"v1"`, a list containing `"v1"`, and `*`; `412` when the race lands after the early check; a reservation that succeeds on attempt 2 recomputing from the fresh quantity (6 - 2 = 4, `ETag: "v3"`); `409` after exactly three conflicting attempts with only the competitors' writes persisted.

Captured run, from `labs/`, CPython 3.14.6, macOS 27.3 arm64, SQLAlchemy 2.1.8, aiosqlite 0.22.1, SQLite 3.53.1:

```text
$ uv run --frozen pytest tests/f13_database_access_and_transactions_for_apis/test_optimistic_vs_pessimistic_locking_version_id_col_and_for_update_skip_locked.py -q
32 passed in 2.00s
```

## Pitfalls

> [!warning] The lock clause vanishes on SQLite and SQL Server
> Trigger: a `SKIP LOCKED` or `FOR UPDATE` design tested only against SQLite (or deployed to SQL Server) through SQLAlchemy.
> Symptom: tests pass, workers double-process jobs, or two requests both pass a "locked" check; no warning is logged because both compilers return an empty string.
> Fix: make the write the arbiter (`UPDATE ... WHERE status = 'open' RETURNING id`, or a version column), run lock-dependent tests against the production engine, and on SQL Server use `WITH (UPDLOCK, READPAST)` hints.

> [!warning] A locked re-read that does not refresh
> Trigger: an object already in the session is re-selected with `with_for_update()` to "lock and re-check" it.
> Symptom: the lock is correct, the decision is made from the old attribute values, and the lost update returns under load.
> Fix: use `session.get(Model, pk, with_for_update=True)` or add `execution_options(populate_existing=True)`; version the row so the locked `get()` raises `StaleDataError` on drift (a locked `select()` does not run that check) and a stale flush still matches zero rows.

> [!warning] Bulk statements skip the version check
> Trigger: ORM-enabled `update(Item).where(...).values(...)`, Core updates, raw SQL, or another service writing the table without touching `version`.
> Symptom: the lab's bulk `UPDATE` to `quantity = 0` left `version = 1`, and a stale ORM flush then overwrote it with `5` without error.
> Fix: every writer bumps the version (`values(version=Item.version + 1, ...)`) and, for bulk statements, includes `Item.version == expected` in `WHERE`; a database trigger enforces it for writers you do not control.
> SQLAlchemy's documentation warns that versioning "does not take effect when performing a multirow UPDATE or DELETE using Query.update() or Query.delete()", and the lab shows the 2.0-style ORM `update()` statement behaves the same way.

> [!warning] Two conflict signals, not one
> Trigger: the service runs at `REPEATABLE READ` or `SERIALIZABLE`, or some endpoints do.
> Symptom: the conflict arrives as `40001 could not serialize access due to concurrent update` from the driver, not as `StaleDataError`, and escapes as `500`.
> Fix: classify `StaleDataError` and SQLSTATE `40001` (and `40P01` deadlocks, see [[Preventing-and-Recovering-from-Database-Deadlocks-in-API-Transactions]]) as retryable conflicts, retry the whole transaction from a fresh session, and bound the attempts.

> [!warning] `FOR UPDATE` blocks unrelated child inserts
> Trigger: `SELECT ... FOR UPDATE` on a parent row (an account, an order) while other requests insert children that reference it.
> Symptom: inserts stall; in the probe they hit `lock_timeout` with `55P03`.
> Fix: lock with `with_for_update(key_share=True)` (`FOR NO KEY UPDATE`) unless you will delete the row or change its key; the probe's child insert then succeeded.

> [!warning] Unbounded lock waits
> Trigger: a plain `FOR UPDATE` on a hot row with `lock_timeout = 0`, the default; PostgreSQL waits "indefinitely" absent a deadlock (PostgreSQL 18 Section 13.3.4).
> Symptom: request latency equals the slowest lock holder's transaction, the connection pool drains, and the failure spreads to endpoints that never touch the row.
> Fix: `SET LOCAL lock_timeout` per transaction or `nowait=True` for fail-fast endpoints, `idle_in_transaction_session_timeout` so abandoned transactions release locks, and never `await` an external call while holding a row lock.

> [!warning] `NOWAIT` and `SKIP LOCKED` together
> Trigger: `with_for_update(nowait=True, skip_locked=True)`.
> Symptom: SQLAlchemy renders `FOR UPDATE NOWAIT SKIP LOCKED`; PostgreSQL 18.7 rejects it with `42601 syntax error at or near "SKIP"`.
> Fix: choose one; the grammar is `[ NOWAIT | SKIP LOCKED ]`.

No public postmortem attributing an outage specifically to SQLAlchemy `version_id_col` or to `SKIP LOCKED` misuse was found for this note (as of 2026-11), so the pitfalls above rest on vendor documentation, library source, and the lab and PostgreSQL probe, not on an incident report.
The nearest primary-source defect report is SQLAlchemy issue 396, filed in 2006 and closed: the default MySQL rowcount counts rows changed rather than rows matched, "this breaks version_id_col among other things", and the fix is the `CLIENT.FOUND_ROWS` connect flag that every SQLAlchemy MySQL dialect now sets (SQLAlchemy GitHub issue 396, "patch mysql driver rowcounting strategy"; `(SQLAlchemy 2.1.8 sqlalchemy/dialects/mysql/base.py, module docstring section "rowcount Support")`).

## Security and robustness

Lock-holding is a resource-consumption surface: any endpoint that takes a row lock and then does work proportional to attacker input lets a client pin rows and pooled connections, which is OWASP API4:2023 Unrestricted Resource Consumption; bound it with `lock_timeout`, short transactions, rate limits on the locking endpoints, and validation and authorization before the lock is taken.
Reservation and claim endpoints are sensitive business flows in OWASP API6:2023 terms: a client that can reserve inventory cheaply can hoard it, so reservations need expiry (a lease with `expires_at`) and per-principal caps, not just correct locking.
A version check is not an authorization check: `If-Match: "v3"` proves the client saw version 3, not that it may write the object, so object-level authorization (OWASP API1:2023) must run before the precondition, and RFC 9110 Section 13.2.1 already orders it that way because a request that would fail its normal checks must ignore preconditions.
A sequential version leaks how many times a resource changed; if that is sensitive, derive the `ETag` from a keyed hash of `(id, version)` and keep the integer server-side.
Every claim and lock query must carry the tenant predicate; `SKIP LOCKED` over a shared table without `tenant_id = :t` lets one tenant's workers drain another's queue.
Defense in depth: a `CHECK (quantity >= 0)` constraint turns any locking or retry bug into an integrity error rather than negative stock, and the If-Match parser should cap header size and tag count (the reused parser caps it at 16 tags).

## The principal's view

The decision is not optimistic versus pessimistic in the abstract; it is where the critical section lives and how long it lasts.
HTTP is stateless, so a database lock can never span a user's think time between `GET` and `PUT`; any "check out this record for editing" requirement is an application lease, and the database-level choice applies only within one request's transaction.
Inside one request, choose by conflict rate and conflict cost: optimistic version columns are the default for human-edited resources and low-contention aggregates because they cost one predicate and turn conflicts into a clean `412` the client can merge; pessimistic `FOR UPDATE` (or `FOR NO KEY UPDATE`) wins for short server-side critical sections on hot rows, where optimistic retries would burn work and latency on near-certain conflicts.
Before either, ask whether the operation can be one statement: a relative `UPDATE ... SET quantity = quantity - :n WHERE quantity >= :n` holds the row lock only inside that statement and needs neither a version nor an explicit lock.
`SKIP LOCKED` queues are an excellent first queue because they share the transaction and backup story of the data; move to a broker when you need fan-out, replay, cross-service consumers, or throughput beyond what one table's hot index and vacuum can sustain, and use the outbox to bridge the two.
Organizationally, standardize the conflict contract once (strong `ETag`, mandatory `If-Match` on unsafe methods, `428`, `412`, `409` with a problem body per [[Problem-Details-for-HTTP-APIs-RFC-9457]]) so that clients implement merge-and-retry once, and require lock-dependent tests to run against the production database engine in CI, because the SQLite test path is precisely where the lock disappears.
Choose a different tool when the invariant spans rows that no single lock covers (write skew): that is `SERIALIZABLE` territory, or a single-writer design such as partitioning commands by key to one consumer.

## Interview questions

> [!question] L5: What exact SQL does SQLAlchemy emit when you modify a mapped object that has `version_id_col`, and what turns a conflict into an exception?
>
> > [!success]- Answer
> > The flush emits `UPDATE items SET quantity=?, version=? WHERE items.id = ? AND items.version = ?`, binding the new version and the version loaded into the session.
> > SQLAlchemy sums the cursor's `rowcount` and, on a dialect with `supports_sane_rowcount`, raises `StaleDataError` if fewer rows matched than objects were flushed.
> > The database does the comparison; Python only reads the row count.
> >
> > Follow-up: What happens on a dialect without reliable rowcount?

> [!question] L5: Which status codes does an API return for a missing `If-Match`, a stale `If-Match`, and a server-side retry loop that keeps losing, and why each?
>
> > [!success]- Answer
> > Missing: `428 Precondition Required`, because the server requires conditional writes to prevent lost updates (RFC 6585 Section 3).
> > Stale: `412 Precondition Failed`, because the client's condition evaluated to false (RFC 9110 Section 15.5.13).
> > Exhausted retries on an unconditional command: `409 Conflict`, a conflict with current state the client may resolve and resubmit (RFC 9110 Section 15.5.10).
> >
> > Follow-up: Which of these should a client retry automatically without user involvement?

> [!question] L6: Predict the output: `ia = await a.get(Item, 1)`, then another session commits `quantity = 0` through `update(Item).where(Item.id == 1).values(quantity=0)`, then `ia.quantity = 5; await a.commit()`; what are the final `quantity` and `version`?
>
> > [!success]- Answer
> > `quantity = 5` and `version = 2`, with no exception.
> > The ORM bulk `UPDATE` never touched `version`, so the row still had `version = 1`, and the stale flush's `WHERE version = 1` matched.
> > The bulk write is lost; the lab asserts exactly this.
> >
> > Follow-up: How would you make the bulk statement participate in versioning?

> [!question] L6: Debug this API: a job queue uses `select(Job).where(Job.state == "ready").limit(10).with_for_update(skip_locked=True)` and passes all tests in CI, but in staging some jobs run twice; CI uses SQLite and staging uses SQL Server; what is happening?
>
> > [!success]- Answer
> > SQLAlchemy's SQLite and SQL Server compilers both render the locking clause as an empty string, so neither environment takes any lock, and concurrent workers select the same rows.
> > CI never ran workers concurrently enough to expose it; staging does.
> > Fix the claim by making the write conditional (`UPDATE ... WHERE id IN (...) AND state = 'ready'` with `RETURNING` and a check of the returned ids), use `WITH (UPDLOCK, READPAST)` on SQL Server, and run lock tests against the production engine.
> >
> > Follow-up: How do you detect double execution after the fact?

> [!question] L6: Your handler does `obj = await session.get(Order, 1)`, some checks, then `await session.execute(select(Order).where(Order.id == 1).with_for_update())` before writing; under load you still see lost updates on PostgreSQL. Why?
>
> > [!success]- Answer
> > The second `SELECT` locks the current row, but the ORM returns the existing identity-map object and does not overwrite its already-loaded attributes.
> > The handler holds the right lock and decides from stale values.
> > Use `session.get(Order, 1, with_for_update=True)`, which bypasses the identity-map shortcut, or `populate_existing=True`, and lock before reading rather than after.
> >
> > Follow-up: What extra protection does a version column add in this code path?

> [!question] L6: Under PostgreSQL `READ COMMITTED`, transaction B runs `UPDATE items SET version = 2 WHERE id = 1 AND version = 1` while A has already updated the row to `version = 2` but not committed; what happens to B, and what changes at `REPEATABLE READ`?
>
> > [!success]- Answer
> > B blocks on A's row lock; when A commits, PostgreSQL re-evaluates B's `WHERE` against the new row version, finds `version = 2`, and B's `UPDATE` affects 0 rows, which SQLAlchemy reports as `StaleDataError`.
> > At `REPEATABLE READ`, B instead fails with SQLSTATE `40001` `could not serialize access due to concurrent update`.
> > Both must be handled as the same retryable conflict.
> >
> > Follow-up: Why does SQLAlchemy's documentation say `version_id_col` is mostly a cross-transaction tool above `READ COMMITTED`?

> [!question] L7+: Design the work-claiming path for 200 workers on one PostgreSQL table; cover the claim statement, lock duration, crashes, and when you would abandon this design.
>
> > [!success]- Answer
> > Claim with one statement: `UPDATE jobs SET state='running', owner=:w, lease_until=now()+interval ... WHERE id IN (SELECT id FROM jobs WHERE state='ready' ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED) RETURNING id`, then commit immediately so row locks last milliseconds.
> > Crash recovery comes from the lease: a sweeper returns expired `running` jobs to `ready`, and job execution is idempotent because a lease can expire under a slow but live worker.
> > Keep the ready set small and indexed (partial index on `state = 'ready'`) because every claim scans it.
> > Abandon it for a broker when you need fan-out, replay, consumers in other services, or sustained throughput the table and vacuum cannot keep up with.
> >
> > Follow-up: Why not hold the row lock for the job's whole duration?

> [!question] L7+: A product team asks for "record locking" so two support agents cannot edit the same ticket at once; what do you build, and where do database locks fit?
>
> > [!success]- Answer
> > Database locks cannot span HTTP requests, so the edit lock is an application lease row (`ticket_id`, `holder`, `expires_at`) acquired by a conditional insert or update, renewed by heartbeat, and shown in the UI.
> > The write path still uses a version column and `If-Match`, because leases expire and clients misbehave; the lease is a courtesy, the version is the guarantee.
> > Row locks appear only inside the short transactions that acquire the lease and apply the write.
> >
> > Follow-up: What happens when the lease holder's laptop sleeps for an hour?

> [!question] L7+: When is neither optimistic nor pessimistic locking the right answer?
>
> > [!success]- Answer
> > When the change is expressible as one atomic statement (`SET balance = balance - :x WHERE balance >= :x`), the statement's own row lock is enough and there is no read-modify-write window.
> > When the invariant spans multiple rows that no single lock covers (write skew, such as "at least one doctor on call"), use `SERIALIZABLE` with retries or materialize the invariant into one lockable row.
> > When the entity has a natural single writer (a partitioned stream per account), route commands to that writer and avoid database contention entirely.
> >
> > Follow-up: What does `SERIALIZABLE` cost in retries under real contention, and how do you measure it?

## Related

Prerequisites: [[Transaction-Isolation-Levels-and-Concurrency-Anomalies-in-APIs]]

Next: [[Preventing-and-Recovering-from-Database-Deadlocks-in-API-Transactions]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]], [[Optimistic-Concurrency-Control-If-Match-and-412-Precondition-Failed]], [[Strong-vs-Weak-Validators-ETag-and-Last-Modified]], [[Session-Internals-Identity-Map-Unit-of-Work-Flush-and-Expire-On-Commit]], [[Request-Scoped-Transaction-Patterns-Commit-As-You-Go-vs-Unit-of-Work]], [[High-Throughput-Bulk-Writes-Upserts-and-RETURNING-in-SQLAlchemy-2-x]], [[Transactional-Outbox-Pattern-Solving-Dual-Writes-Between-DB-and-Events]], [[Zero-Downtime-Schema-Migrations-Expand-Contract-and-Lock-Timeouts]], [[Sizing-Connection-Pools-Across-Workers-Pods-and-PgBouncer-Transaction-Mode]], [[Retries-Exponential-Backoff-Full-vs-Decorrelated-Jitter-and-Stamina]], [[Idempotency-Key-Header-Specification-and-State-Machine-Draft-08]], [[Problem-Details-for-HTTP-APIs-RFC-9457]]

- Lab: `labs/tests/f13_database_access_and_transactions_for_apis/test_optimistic_vs_pessimistic_locking_version_id_col_and_for_update_skip_locked.py`

## Further reading

- [PostgreSQL 18: SELECT, The Locking Clause](https://www.postgresql.org/docs/18/sql-select.html) - the grammar, `NOWAIT` vs `SKIP LOCKED`, `LIMIT` and `ORDER BY` interactions, and the "inconsistent view" warning.
- [PostgreSQL 18: Explicit Locking, Section 13.3](https://www.postgresql.org/docs/18/explicit-locking.html) - the four row-lock strengths, their conflict table, why row locks have no memory limit, and deadlock behavior.
- [PostgreSQL 18: Transaction Isolation, Section 13.2](https://www.postgresql.org/docs/18/transaction-iso.html) - the `READ COMMITTED` re-evaluation rule that makes versioned updates return zero rows, and the `40001` behavior above it.
- [PostgreSQL 18: Client Connection Defaults, Section 19.11](https://www.postgresql.org/docs/18/runtime-config-client.html) - `lock_timeout`, `idle_in_transaction_session_timeout`, and `transaction_timeout`.
- [SQLAlchemy 2.1: Configuring a Version Counter](https://docs.sqlalchemy.org/en/21/orm/versioning.html) - maintainer documentation for `version_id_col`, custom and server-side counters, and the bulk-update warning.
- [SQLAlchemy `rel_2_1_8` ORM source](https://github.com/sqlalchemy/sqlalchemy/tree/rel_2_1_8/lib/sqlalchemy/orm) - `persistence.py` (`_emit_update_statements`, `_emit_delete_statements`) and `loading.py` (`_validate_version_id`) are the mechanism.
- [RFC 9110 Sections 13.1.1, 13.2, 15.5.10, 15.5.13](https://www.rfc-editor.org/rfc/rfc9110.html) - `If-Match` evaluation, when to evaluate preconditions, `409`, and `412`.
- [RFC 6585 Section 3](https://www.rfc-editor.org/rfc/rfc6585.html) - `428 Precondition Required` and its lost-update rationale.
- [MySQL 8.4: Locking Read Concurrency with NOWAIT and SKIP LOCKED](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html) - InnoDB semantics, error 3572, and the replication caveat.
- [Microsoft Learn: Table hints](https://learn.microsoft.com/en-us/sql/t-sql/queries/hints-transact-sql-table) - `UPDLOCK`, `ROWLOCK`, and `READPAST`, the SQL Server equivalents SQLAlchemy does not emit.
- [SQLite: Transaction](https://www.sqlite.org/lang_transaction.html) - deferred vs immediate transactions and the single-writer model that replaces row locks.
