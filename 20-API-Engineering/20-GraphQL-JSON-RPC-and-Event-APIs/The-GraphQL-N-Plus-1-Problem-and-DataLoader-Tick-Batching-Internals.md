---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# How GraphQL Resolvers Trigger N+1 Query Cascades and How DataLoader Event-Loop Tick Coalescing Collapses Them to O(1) Queries

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [GraphQL JSON RPC and Event APIs](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Strawberry-GraphQL-with-FastAPI-Types-Context-Mutations-and-Subscriptions]]

Next: [[GraphQL-Security-Query-Depth-Complexity-Cost-Analysis-and-Batch-Limits]]

See also: [[GraphQL]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Counts SQL queries when resolving 50 authors and their books without DataLoader (51 queries) vs with a Strawberry DataLoader backed by SQLAlchemy WHERE author_id IN (...) (exactly 2 queries).

## Further reading
