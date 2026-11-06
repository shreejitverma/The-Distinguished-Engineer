---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Isolation Levels in Practice: Read Committed, Repeatable Read, and Serializable Anomalies (Lost Updates, Non-Repeatable Reads, and Write Skew)

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

Prerequisites: [[Request-Scoped-Transaction-Patterns-Commit-As-You-Go-vs-Unit-of-Work]]

Next: [[Optimistic-vs-Pessimistic-Locking-Version-ID-Col-and-FOR-UPDATE-SKIP-LOCKED]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]]

Lab plan: Reproduces a classic double-spend / overdraft write-skew anomaly under Read Committed when two concurrent API requests check balance >= 100 and insert withdrawals, and proves how Repeatable Read / Serializable raises 40001 for retry.

## Further reading
