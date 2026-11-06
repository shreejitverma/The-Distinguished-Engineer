---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Structured Filter Languages: Parsing Google AIP-160 Filter Expressions to Safe SQL ASTs

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

Prerequisites: [[Query-Parameter-Filtering-Brackets-RHS-Colon-and-Validation]]

Next: [[Multi-Column-Sorting-Order-By-Whitelisting-and-Index-Coverage]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Builds a recursive-descent parser for a subset of AIP-160 filter syntax (boolean AND/OR/NOT, comparisons, field allowlist, and maximum AST depth) and compiles it to bound SQLAlchemy expressions.

## Further reading
