---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Client Retry Engineering: Which Status Codes and Exceptions Are Safe to Retry, Full vs Decorrelated Jitter, and stamina (26.1.0) / tenacity (9.2.1)

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

Prerequisites: [[Timeout-Taxonomy-Connect-Read-Write-Pool-and-Deadline-Propagation]]

Next: [[Retry-Budgets-and-Preventing-Cascading-Retry-Amplification-Storms]]

See also: [[HTTP-Retries-and-Timeouts-with-curl]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Simulates 100 synchronized clients retrying after a 1-second outage and proves how unjittered exponential backoff creates synchronized retry spikes, while Full Jitter spreads load flat and respects Retry-After headers.

## Further reading
