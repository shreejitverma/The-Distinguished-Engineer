---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Rate Limiting Algorithms Compared: Fixed Window Boundary Bursts, Sliding Window Log, Sliding Window Counter, Token Bucket, and Leaky Bucket

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

Prerequisites: [[Structured-Log-Redaction-and-Preventing-PII-and-Token-Leaks]]

Next: [[GCRA-Generic-Cell-Rate-Algorithm-and-Atomic-Redis-Valkey-Lua-Limiters]]

See also: [[API-Fundamentals]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]]

Lab plan: Demonstrates the 2x boundary burst flaw of Fixed Window counters at t=59.9s and t=60.1s and proves how Sliding Window Counter and Token Bucket prevent it.

## Further reading
