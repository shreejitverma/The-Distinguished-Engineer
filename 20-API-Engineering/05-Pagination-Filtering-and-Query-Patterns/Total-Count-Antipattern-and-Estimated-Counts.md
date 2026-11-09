---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Why Exact total_count Breaks Large Collection Pagination and How to Use Capped or Planner Estimates

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

Prerequisites: [[Snapshot-Consistent-Pagination-Across-Point-in-Time-Reads]]

Next: [[Query-Parameter-Filtering-Brackets-RHS-Colon-and-Validation]]

See also: [[Pagination-Strategies]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Proves that adding SELECT COUNT(*) to a keyset-paginated endpoint turns an O(limit) index lookup back into an O(N) full scan, and implements a capped count (LIMIT 10001) and pg_class estimate fallback.

## Further reading
