---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Batch Methods: BatchGet, BatchCreate, BatchUpdate, and BatchDelete (Google AIP-231 through AIP-235)

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Bulk Long Running and Error Contracts](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Mapping-Pydantic-Validation-Errors-to-RFC-9457-Problem-Details]]

Next: [[Atomic-vs-Partial-Batch-Execution-and-207-Multi-Status]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Implements an AIP-233 BatchCreate endpoint that enforces batch size limits (max 1,000 items), preserves request order in responses, and executes a single batched SQL INSERT ... RETURNING.

## Further reading
