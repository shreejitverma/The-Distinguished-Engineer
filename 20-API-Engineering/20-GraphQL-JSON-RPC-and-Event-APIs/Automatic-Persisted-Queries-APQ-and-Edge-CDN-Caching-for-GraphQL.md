---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Overcoming GraphQL's POST Caching Problem: Automatic Persisted Queries (APQ), Trusted Documents (Allowlisted Query Hashes), and GET CDN Caching

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

Prerequisites: [[GraphQL-Field-Level-Authorization-and-Partial-Error-Null-Bubbling]]

Next: [[Distributed-GraphQL-Apollo-Federation-v2-Entities-and-Subgraph-Planning]]

See also: [[GraphQL]], [[REST-APIs]]

Lab plan: Implements an APQ protocol handler (SHA-256 query hash lookup -> PersistedQueryNotFound -> registration -> GET request with ?extensions={"persistedQuery":...} and Cache-Control headers).

## Further reading
