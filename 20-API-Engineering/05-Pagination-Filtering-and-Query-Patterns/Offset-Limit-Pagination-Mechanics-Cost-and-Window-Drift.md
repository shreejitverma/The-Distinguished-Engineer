---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Offset/Limit Pagination: Linear Index Scan Degradation and the Concurrent Insert Window-Drift Bug

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Pagination Filtering and Query Patterns](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Field-Behavior-Semantics-Input-Only-Output-Only-and-Immutable-AIP-203]]

Next: [[Keyset-Seek-Pagination-and-Composite-Tie-Breaker-Cursors]]

See also: [[Pagination-Strategies]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]], [[sqlite3-from-the-Shell]], [[DuckDB-from-the-Shell]]

Lab plan: Demonstrates on SQLite/PostgreSQL how OFFSET 50000 scans and discards 50,000 rows and reproduces duplicate/skipped items when a concurrent row is inserted between page 1 and page 2.

## Further reading
