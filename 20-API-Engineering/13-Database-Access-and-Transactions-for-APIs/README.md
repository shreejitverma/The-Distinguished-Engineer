---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# Database Access and Transactions for APIs

Covers high-concurrency relational data access behind Python APIs using SQLAlchemy 2.x (`2.1.8` and `2.0.57`, released 2026-11-04), `asyncpg` (`0.32.0`), `psycopg` (`3.3.8`), `aiosqlite` (`0.22.1`), and Alembic (`1.20.1`).
Explains the Identity Map, Unit of Work flush mechanics, greenlet bridging (`greenlet_spawn`), `MissingGreenlet` errors, connection pool sizing across workers and PgBouncer, transaction isolation anomalies, optimistic versus pessimistic locking (`SELECT ... FOR UPDATE SKIP LOCKED`), N+1 elimination, and zero-downtime schema migrations.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [SQLAlchemy 2.x (2.1.8 / 2.0.57) Architecture: Core vs ORM, Engine, Connection, Declarative Mapped[T], and 2.0 select() Compilation](SQLAlchemy-2-x-Architecture-Core-vs-ORM-Engine-and-Unified-Select.md)
2. [Inside SQLAlchemy Session and AsyncSession: Identity Map Uniquing, Topological Unit-of-Work Flush, and Why expire_on_commit=False Is Mandatory in Async APIs](Session-Internals-Identity-Map-Unit-of-Work-Flush-and-Expire-On-Commit.md)
3. [How AsyncEngine and AsyncSession Work Internally: greenlet_spawn Bridging and Why Implicit Lazy Loading Raises MissingGreenlet](Async-SQLAlchemy-Under-the-Hood-Greenlet-Bridge-and-MissingGreenlet.md)
4. [Async PostgreSQL Drivers Compared: asyncpg (0.32.0) Binary Protocol vs psycopg 3 (3.3.8) Libpq/Pipeline Mode and Prepared Statement Caching](Async-PostgreSQL-Drivers-Compared-Asyncpg-vs-Psycopg-3.md)
5. [SQLAlchemy Connection Pooling: QueuePool, AsyncAdaptedQueuePool, pool_size, max_overflow, pool_timeout, pool_recycle, and pool_pre_ping](Connection-Pooling-Internals-QueuePool-AsyncAdaptedQueuePool-and-Pre-Ping.md)
6. [Connection Pool Sizing Math Across Replicas and PgBouncer Transaction Pooling: Why Prepared Statements Break Under Transaction Multiplexing](Sizing-Connection-Pools-Across-Workers-Pods-and-PgBouncer-Transaction-Mode.md)
7. [Concurrency Hazard: Why One AsyncSession Cannot Be Shared Across asyncio.gather / TaskGroup Sibling Tasks](Why-Sharing-an-AsyncSession-Across-Concurrent-Tasks-Corrupts-State.md)
8. [Transaction Boundaries in FastAPI: Request-Scoped Unit-of-Work Dependencies vs Explicit Service-Layer Transactions and Read Replicas](Request-Scoped-Transaction-Patterns-Commit-As-You-Go-vs-Unit-of-Work.md)
9. [Isolation Levels in Practice: Read Committed, Repeatable Read, and Serializable Anomalies (Lost Updates, Non-Repeatable Reads, and Write Skew)](Transaction-Isolation-Levels-and-Concurrency-Anomalies-in-APIs.md)
10. [Locking Strategies Behind APIs: Optimistic Version Columns (version_id_col / StaleDataError) vs Pessimistic SELECT ... FOR UPDATE NOWAIT / SKIP LOCKED](Optimistic-vs-Pessimistic-Locking-Version-ID-Col-and-FOR-UPDATE-SKIP-LOCKED.md)
11. [Database Deadlocks in Multi-Row API Updates: Deterministic Lock Ordering and Exponential-Backoff Transaction Retries](Preventing-and-Recovering-from-Database-Deadlocks-in-API-Transactions.md)
12. [SQLAlchemy Loading Strategies: selectinload vs joinedload vs subqueryload, contains_eager, and Enforcing Zero N+1 in Tests with raiseload('*')](Eliminating-N-Plus-1-Queries-Selectinload-Joinedload-Subqueryload-and-Raiseload.md)
13. [Bulk Inserts, ON CONFLICT DO UPDATE Upserts, and RETURNING Clauses in SQLAlchemy 2.x](High-Throughput-Bulk-Writes-Upserts-and-RETURNING-in-SQLAlchemy-2-x.md)
14. [The Dual-Write Problem and the Transactional Outbox Pattern: Atomic State + Event Persistence in One Database Commit](Transactional-Outbox-Pattern-Solving-Dual-Writes-Between-DB-and-Events.md)
15. [Alembic (1.20.1) Internals: Revision DAGs, Branch Merging, Autogenerate Diff Limitations, and Async env.py Configuration](Alembic-Internals-Revision-Graph-Autogenerate-and-Async-Env.md)
16. [Zero-Downtime Database Migrations for Live APIs: The Expand-and-Contract Pattern, lock_timeout, NOT VALID Constraints, and Concurrent Indexes](Zero-Downtime-Schema-Migrations-Expand-Contract-and-Lock-Timeouts.md)
