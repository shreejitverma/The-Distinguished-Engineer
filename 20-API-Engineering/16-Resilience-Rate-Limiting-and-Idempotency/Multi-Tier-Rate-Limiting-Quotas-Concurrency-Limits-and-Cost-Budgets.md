---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Layered Rate Limiters in Production: Per-IP, Per-Tenant, Concurrent In-Flight Request Limiters, and Query-Cost / Compute-Unit Quotas

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

Prerequisites: [[GCRA-Generic-Cell-Rate-Algorithm-and-Atomic-Redis-Valkey-Lua-Limiters]]

Next: [[Standard-RateLimit-Headers-IETF-Draft-and-429-Retry-After-Semantics]]

See also: [[API-Fundamentals]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]]

Lab plan: Implements a four-tier admission pipeline (1: global RPS, 2: per-tenant token bucket, 3: per-tenant concurrent in-flight semaphore, 4: cost-weighted query budget).

## Further reading
