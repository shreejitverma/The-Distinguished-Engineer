---
type: case-study
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Case Study: Financial Double-Withdrawal Race Condition from Check-Then-Act Balance Validation and Non-Atomic Idempotency Checks

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

Prerequisites: [[Case-Study-Database-Migration-ACCESS-EXCLUSIVE-Lock-Convoy-Taking-Down-an-API]]

Next: [[Case-Study-Webhook-Self-DDoS-and-Cloud-Metadata-SSRF-Exploit]]

See also: [[API-Fundamentals]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]]

Lab plan: Fires 10 simultaneous concurrent requests with the same Idempotency-Key and with distinct keys against a naive Read Committed balance check, reproducing double-spend, and fixes it with atomic INSERT ... ON CONFLICT idempotency locking and SELECT ... FOR UPDATE.

## Further reading
