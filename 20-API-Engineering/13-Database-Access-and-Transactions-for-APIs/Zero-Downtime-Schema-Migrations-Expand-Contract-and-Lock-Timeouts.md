---
type: playbook
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Zero-Downtime Database Migrations for Live APIs: The Expand-and-Contract Pattern, lock_timeout, NOT VALID Constraints, and Concurrent Indexes

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

Prerequisites: [[Alembic-Internals-Revision-Graph-Autogenerate-and-Async-Env]]

Next: [[API-Authentication-Architecture-Edge-Verification-vs-Zero-Trust-Propagation]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: write-tests an Alembic lint check that rejects unsafe migration operations (adding NOT NULL without default, dropping a column still referenced by current app code, or running DDL without SET LOCAL lock_timeout).

## Further reading
