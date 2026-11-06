---
type: case-study
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Case Study: How a 50-Millisecond ALTER TABLE ADD COLUMN Blocked Behind a Long-Running Query and Queued Every API Request Behind an ACCESS EXCLUSIVE Lock Convoy

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [API Failure Case Studies](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Case-Study-CDN-Cache-Leak-of-Authenticated-PII-Responses]]

Next: [[Case-Study-Double-Spend-Race-Condition-from-Non-Atomic-Idempotency-and-Read-Committed]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Reproduces the PostgreSQL lock-queue convoy where an ALTER TABLE waits on one slow reader and blocks all subsequent fast SELECTs behind it, and proves how SET LOCAL lock_timeout = '2s' with retry prevents the outage.

## Further reading
