---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Transforming FastAPI and Pydantic v2 ValidationError Lists into RFC 9457 Problem Details

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

Prerequisites: [[Error-Taxonomy-Retryability-and-Client-Contract-Design]]

Next: [[Batch-Get-and-Bulk-Mutation-Design-AIP-231-to-AIP-235]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]]

Lab plan: Registers a custom RequestValidationError handler in FastAPI 0.162.0 that converts Pydantic loc tuples into RFC 6901 JSON Pointers (#/body/items/0/price) inside an RFC 9457 problem document.

## Further reading
