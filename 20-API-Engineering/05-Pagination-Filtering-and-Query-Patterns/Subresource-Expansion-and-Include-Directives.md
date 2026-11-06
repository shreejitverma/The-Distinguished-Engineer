---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Compound Documents and Resource Expansion (?include= / expand[]) Without N+1 Query Explosions

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

Prerequisites: [[Sparse-Fieldsets-Partial-Responses-and-Read-Masks-AIP-157]]

Next: [[Search-Endpoints-vs-List-Endpoints-and-Full-Text-Ranking]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Implements Stripe-style expand[]=customer&expand[]=invoice.lines parameter parsing that dynamically attaches SQLAlchemy selectinload options up to a bounded expansion depth.

## Further reading
