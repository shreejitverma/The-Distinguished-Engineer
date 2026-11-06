---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Connection Pool Sizing Math Across Replicas and PgBouncer Transaction Pooling: Why Prepared Statements Break Under Transaction Multiplexing

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Database Access and Transactions for APIs](README.md) in [API Engineering](../README.md).

## TL;DR

## Mental model

## How it works

## Numbers and limits

## Portability

## Worked example

## Pitfalls

## Security and robustness

## The principal's view

## Interview questions

## Related

Prerequisites: [[Connection-Pooling-Internals-QueuePool-AsyncAdaptedQueuePool-and-Pre-Ping]]

Next: [[Why-Sharing-an-AsyncSession-Across-Concurrent-Tasks-Corrupts-State]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Proves the connection budget formula (Pods * Workers * (pool_size + max_overflow)) and demonstrates why PgBouncer transaction pooling requires statement_cache_size=0 in asyncpg or NullPool in SQLAlchemy.

## Further reading
