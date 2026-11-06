---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Multi-Phase Idempotency with Recovery Points: Handling Process Crashes Between External Side Effects and Database Commits

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

Prerequisites: [[Idempotency-Key-Header-Specification-and-State-Machine-Draft-08]]

Next: [[Timeout-Taxonomy-Connect-Read-Write-Pool-and-Deadline-Propagation]]

See also: [[API-Fundamentals]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]]

Lab plan: Simulates a crash midway through a three-step payment workflow (reserve ledger -> call gateway -> finalize) and proves that a retried Idempotency-Key resumes from the persisted recovery checkpoint without double-charging.

## Further reading
