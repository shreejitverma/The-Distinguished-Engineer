---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Locking Strategies Behind APIs: Optimistic Version Columns (version_id_col / StaleDataError) vs Pessimistic SELECT ... FOR UPDATE NOWAIT / SKIP LOCKED

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

Prerequisites: [[Transaction-Isolation-Levels-and-Concurrency-Anomalies-in-APIs]]

Next: [[Preventing-and-Recovering-from-Database-Deadlocks-in-API-Transactions]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]]

Lab plan: Tests SQLAlchemy's mapper_args={'version_id_col': ...} raising StaleDataError on concurrent edits (mapped to HTTP 409/412) and tests with_for_update(skip_locked=True) for contention-free ticket claiming.

## Further reading
