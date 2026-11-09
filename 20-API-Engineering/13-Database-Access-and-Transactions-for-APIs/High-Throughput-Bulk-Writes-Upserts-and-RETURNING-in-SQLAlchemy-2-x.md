---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Bulk Inserts, ON CONFLICT DO UPDATE Upserts, and RETURNING Clauses in SQLAlchemy 2.x

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

Prerequisites: [[Eliminating-N-Plus-1-Queries-Selectinload-Joinedload-Subqueryload-and-Raiseload]]

Next: [[Transactional-Outbox-Pattern-Solving-Dual-Writes-Between-DB-and-Events]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Benchmarks 5,000-row insertion via session.add_all() vs session.execute(insert(Item).returning(Item), rows) and tests dialect-specific PostgreSQL/SQLite on_conflict_do_update atomic upserts.

## Further reading
