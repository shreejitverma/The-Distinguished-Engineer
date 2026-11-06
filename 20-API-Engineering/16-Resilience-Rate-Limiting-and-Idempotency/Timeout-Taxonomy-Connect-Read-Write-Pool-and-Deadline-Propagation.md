---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Timeouts Across the API Call Graph: Connect, Read, Write, Pool Timeouts in httpx (0.28.1) and End-to-End Deadline Budget Propagation

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

Prerequisites: [[Atomic-Idempotency-Phases-and-Failure-Recovery-Across-Network-Drops]]

Next: [[Retries-Exponential-Backoff-Full-vs-Decorrelated-Jitter-and-Stamina]]

See also: [[HTTP-Retries-and-Timeouts-with-curl]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Propagates an inbound X-Request-Deadline-Ms budget through a ContextVar so each downstream httpx call automatically clamps its timeout to min(per_call_timeout, remaining_deadline).

## Further reading
