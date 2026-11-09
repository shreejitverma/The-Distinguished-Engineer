---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# The Bulkhead Pattern: Isolating Connection Pools, Threadpools, and Semaphores Per Downstream Dependency and Per Tenant Tier

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

Prerequisites: [[Load-Shedding-Priority-Traffic-Classes-and-LIFO-Queue-Management]]

Next: [[Hedged-Requests-and-Tail-Latency-Taming-at-Scale]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]]

Lab plan: Demonstrates how a slow non-critical recommendation microservice exhausts a shared outbound pool and takes down checkout, and fixes it using isolated httpx clients and per-dependency AnyIO CapacityLimiters.

## Further reading
