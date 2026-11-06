---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# How AsyncEngine and AsyncSession Work Internally: greenlet_spawn Bridging and Why Implicit Lazy Loading Raises MissingGreenlet

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

Prerequisites: [[Session-Internals-Identity-Map-Unit-of-Work-Flush-and-Expire-On-Commit]]

Next: [[Async-PostgreSQL-Drivers-Compared-Asyncpg-vs-Psycopg-3]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Traces greenlet_spawn() in sqlalchemy.util.concurrency, triggers StatementError(MissingGreenlet) by accessing an unloaded relationship in an async endpoint, and demonstrates Explicit await obj.awaitable_attrs.rel vs eager loading.

## Further reading
