---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Hardening GraphQL in Production: AST Depth Limits, Weighted Query Complexity Cost Analysis, Alias/Batching Brute-Force Caps, and Disabling Introspection

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

Prerequisites: [[The-GraphQL-N-Plus-1-Problem-and-DataLoader-Tick-Batching-Internals]]

Next: [[GraphQL-Field-Level-Authorization-and-Partial-Error-Null-Bubbling]]

See also: [[GraphQL]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Constructs a cyclic 15-level nested query and a 100-alias login brute-force query, proving how custom AST validation extensions reject both before a single database resolver runs.

## Further reading
