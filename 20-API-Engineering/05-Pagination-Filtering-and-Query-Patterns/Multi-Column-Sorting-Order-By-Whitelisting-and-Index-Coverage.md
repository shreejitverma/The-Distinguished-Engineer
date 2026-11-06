---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Multi-Column Sorting (order_by), Nulls Ordering, Allowlist Validation, and Index-Sort Alignment

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Pagination Filtering and Query Patterns](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Structured-Filter-Expressions-and-AST-Compilation-AIP-160]]

Next: [[Sparse-Fieldsets-Partial-Responses-and-Read-Masks-AIP-157]]

See also: [[Pagination-Strategies]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]]

Lab plan: Validates order_by=-created_at,priority syntax against an indexed column allowlist, prevents unindexed filesort DoS, and guarantees deterministic tie-breaking by appending the primary key.

## Further reading
