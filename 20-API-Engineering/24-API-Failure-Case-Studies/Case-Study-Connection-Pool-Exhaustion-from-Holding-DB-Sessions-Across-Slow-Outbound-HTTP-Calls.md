---
type: case-study
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Case Study: Database Connection Pool Starvation Caused by Holding a Request-Scoped SQLAlchemy Session Open While Awaiting a Slow Third-Party API

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

Prerequisites: [[Case-Study-Blocking-Call-Inside-Async-Def-Freezing-the-Entire-Worker-Fleet]]

Next: [[Case-Study-JWT-Key-Confusion-and-Unverified-Header-KID-SSRF-Breaches]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Demonstrates how holding a checked-out DB connection during a 2-second outbound httpx call exhausts QueuePool(pool_size=10) at just 5 RPS, blocking all unrelated fast endpoints, and fixes it by splitting short DB transactions around external I/O.

## Further reading
