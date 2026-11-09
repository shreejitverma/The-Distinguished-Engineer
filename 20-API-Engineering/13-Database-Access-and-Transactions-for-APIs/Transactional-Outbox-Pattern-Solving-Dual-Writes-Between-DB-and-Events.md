---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# The Dual-Write Problem and the Transactional Outbox Pattern: Atomic State + Event Persistence in One Database Commit

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

Prerequisites: [[High-Throughput-Bulk-Writes-Upserts-and-RETURNING-in-SQLAlchemy-2-x]]

Next: [[Alembic-Internals-Revision-Graph-Autogenerate-and-Async-Env]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]]

Lab plan: Proves how publishing to a broker or webhook directly inside a route handler causes state inconsistency on commit failure or process crash, and implements an atomic OutboxEvent table relayed by an async poller.

## Further reading
