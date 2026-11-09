---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# SQLAlchemy Connection Pooling: QueuePool, AsyncAdaptedQueuePool, pool_size, max_overflow, pool_timeout, pool_recycle, and pool_pre_ping

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

Prerequisites: [[Async-PostgreSQL-Drivers-Compared-Asyncpg-vs-Psycopg-3]]

Next: [[Sizing-Connection-Pools-Across-Workers-Pods-and-PgBouncer-Transaction-Mode]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Simulates a database restart dropping idle TCP sockets in the pool and proves how pool_pre_ping=True transparently replaces dead connections before executing the request query.

## Further reading
