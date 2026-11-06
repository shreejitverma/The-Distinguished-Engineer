---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Sparse Fieldsets and Partial Responses (fields / read_mask, AIP-157): Pushing Projections Down to SQL

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

Prerequisites: [[Multi-Column-Sorting-Order-By-Whitelisting-and-Index-Coverage]]

Next: [[Subresource-Expansion-and-Include-Directives]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Parses a nested read_mask specification, pushes column selection down to SQLAlchemy load_only() so unrequested heavy columns are never read from disk, and filters the serialized JSON response.

## Further reading
