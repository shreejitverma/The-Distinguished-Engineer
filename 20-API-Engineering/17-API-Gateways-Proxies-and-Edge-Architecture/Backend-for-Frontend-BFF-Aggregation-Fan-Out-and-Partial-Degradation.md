---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# The Backend-for-Frontend (BFF) Pattern: Parallel Fan-Out with AnyIO TaskGroups, Hedged Timeouts, and Graceful Partial Degradation

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [API Gateways Proxies and Edge Architecture](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[External-Authorization-Ext-Authz-and-Global-Rate-Limit-Filters]]

Next: [[Service-Mesh-Architecture-Sidecar-vs-Ambient-Mesh-and-East-West-Policy]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Builds a FastAPI BFF endpoint that fans out concurrently to Orders, Inventory, and Recommendations services, returning a 200 response with degraded fallback data if Recommendations times out after 80ms.

## Further reading
