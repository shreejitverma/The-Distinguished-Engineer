---
type: case-study
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Case Study: Serving User A's Authenticated Profile to User B Due to Missing Vary: Authorization and Misconfigured CDN Edge Caching Rules

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [API Failure Case Studies](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Case-Study-HTTP-Request-Smuggling-and-H2C-H2-Downgrade-Desync]]

Next: [[Case-Study-Database-Migration-ACCESS-EXCLUSIVE-Lock-Convoy-Taking-Down-an-API]]

See also: [[REST-APIs]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Reproduces an edge cache configuration that ignores Cache-Control: private or strips Authorization before cache lookup, causing cross-user PII leakage, and proves defense-in-depth with Cache-Control: private, no-store and tenant-keyed cache policies.

## Further reading
