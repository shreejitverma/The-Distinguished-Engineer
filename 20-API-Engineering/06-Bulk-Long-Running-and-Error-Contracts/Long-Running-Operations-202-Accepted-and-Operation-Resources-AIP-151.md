---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Long-Running Operations (LROs): 202 Accepted, Operation Resources, done/metadata/result, and Polling (AIP-151)

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

Prerequisites: [[Criteria-Based-Bulk-Delete-and-Purge-Operations-AIP-165]]

Next: [[Asynchronous-Job-Result-Expiration-Cancellation-and-Wait-Semantics]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]]

Lab plan: Implements an AIP-151 Operation resource lifecycle (POST returns 202 with Location and Retry-After; GET /operations/{id} returns progress metadata and final response or error; POST :cancel aborts work).

## Further reading
