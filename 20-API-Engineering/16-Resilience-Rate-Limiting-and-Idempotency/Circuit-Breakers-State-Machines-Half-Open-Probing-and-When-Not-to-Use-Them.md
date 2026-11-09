---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Circuit Breaker Internals: Closed, Open, and Half-Open State Transitions, Sliding Failure Windows, and Why Naive Circuit Breakers Cause Blackhole Outages

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Resilience Rate Limiting and Idempotency](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Retry-Budgets-and-Preventing-Cascading-Retry-Amplification-Storms]]

Next: [[Adaptive-Concurrency-Limits-Littles-Law-Vegas-and-Gradient2]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]]

Lab plan: Implements an async sliding-window circuit breaker with half-open probe concurrency caps and demonstrates the failure mode where tenant-agnostic circuit tripping allows one bad tenant's 500s to blackhole all healthy tenants.

## Further reading
