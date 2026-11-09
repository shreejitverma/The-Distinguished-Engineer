---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Change Validation and Dry-Run Requests (validate_only, Google AIP-163) via Database Savepoints

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

Prerequisites: [[Asynchronous-Job-Result-Expiration-Cancellation-and-Wait-Semantics]]

Next: [[Multipart-Form-Streaming-and-Upload-Memory-Bounds]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Executes full schema validation, authorization checks, and database constraint evaluation inside a SQLAlchemy nested SAVEPOINT and rolls it back cleanly when validate_only=True.

## Further reading
