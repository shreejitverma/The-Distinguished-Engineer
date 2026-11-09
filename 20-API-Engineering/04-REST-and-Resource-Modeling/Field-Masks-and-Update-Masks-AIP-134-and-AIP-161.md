---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Update Masks and Field Masks (Google AIP-134 and AIP-161) and Pydantic exclude_unset Semantics

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

Prerequisites: [[Partial-Updates-JSON-Merge-Patch-RFC-7396-vs-JSON-Patch-RFC-6902]]

Next: [[Custom-Methods-and-State-Machine-Transitions-AIP-136]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]]

Lab plan: Compares explicit update_mask path trees against Pydantic v2 model_dump(exclude_unset=True), proving how both distinguish an omitted field from an explicit null assignment.

## Further reading
