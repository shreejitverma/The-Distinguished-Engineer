---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# SQLAlchemy Loading Strategies: selectinload vs joinedload vs subqueryload, contains_eager, and Enforcing Zero N+1 in Tests with raiseload('*')

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

Prerequisites: [[Preventing-and-Recovering-from-Database-Deadlocks-in-API-Transactions]]

Next: [[High-Throughput-Bulk-Writes-Upserts-and-RETURNING-in-SQLAlchemy-2-x]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Counts executed SQL statements via SQLAlchemy Engine events (before_cursor_execute), proving that selectinload collapses 1 + N queries to exactly 2 queries and that raiseload('*') fails tests immediately on any unconfigured relationship access.

## Further reading
