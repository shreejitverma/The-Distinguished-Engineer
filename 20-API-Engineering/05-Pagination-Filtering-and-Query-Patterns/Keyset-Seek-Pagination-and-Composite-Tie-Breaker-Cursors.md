---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Keyset (Seek) Pagination: Row-Value Comparisons (created_at, id) and Composite B-Tree Index Alignment

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

Prerequisites: [[Offset-Limit-Pagination-Mechanics-Cost-and-Window-Drift]]

Next: [[Opaque-Cursor-Encoding-HMAC-Integrity-and-Filter-Binding]]

See also: [[Pagination-Strategies]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Executes EXPLAIN QUERY PLAN on tuple comparison WHERE (created_at, id) < (:t, :id) ORDER BY created_at DESC, id DESC LIMIT :n and proves zero duplicates even when multiple rows share identical timestamps.

## Further reading
