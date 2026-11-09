---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Partial Updates with PATCH: JSON Merge Patch (RFC 7396) vs JSON Patch Operations (RFC 6902)

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [REST and Resource Modeling](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Full-Replacement-PUT-and-Upsert-Semantics]]

Next: [[Field-Masks-and-Update-Masks-AIP-134-and-AIP-161]]

See also: [[REST-APIs]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]], [[jq-Language]], [[jq-Paths-and-Updates]]

Lab plan: Implements both application/merge-patch+json (showing how explicit null deletes keys and arrays are replaced whole) and application/json-patch+json (atomic test, add, remove, replace operations with rollback on failure).

## Further reading
