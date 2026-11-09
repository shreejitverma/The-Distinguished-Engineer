---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Field-Level Authorization in GraphQL, Non-Null (!) Error Bubbling Traps, and Why Partial Success Returns HTTP 200

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

Prerequisites: [[GraphQL-Security-Query-Depth-Complexity-Cost-Analysis-and-Batch-Limits]]

Next: [[Automatic-Persisted-Queries-APQ-and-Edge-CDN-Caching-for-GraphQL]]

See also: [[GraphQL]]

Lab plan: Demonstrates how a permission failure on a non-nullable child field (salary: Int!) bubbles null up and wipes out the entire parent User object, and how nullable fields with PermissionExtension isolate partial data.

## Further reading
