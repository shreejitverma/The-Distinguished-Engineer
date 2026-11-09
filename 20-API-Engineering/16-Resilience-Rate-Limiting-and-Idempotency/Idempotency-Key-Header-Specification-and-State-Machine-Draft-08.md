---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# The Idempotency-Key HTTP Header Specification (draft-ietf-httpapi-idempotency-key-header-08, October 2026) and Stripe-Grade Recovery State Machines

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

Prerequisites: [[Standard-RateLimit-Headers-IETF-Draft-and-429-Retry-After-Semantics]]

Next: [[Atomic-Idempotency-Phases-and-Failure-Recovery-Across-Network-Drops]]

See also: [[API-Fundamentals]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]]

Lab plan: Implements the full Idempotency-Key state machine: missing key -> 400 Bad Request, payload fingerprint mismatch on reused key -> 422 Unprocessable Content, concurrent in-flight duplicate -> 409 Conflict, and completed key -> exact status/header/body replay.

## Further reading
