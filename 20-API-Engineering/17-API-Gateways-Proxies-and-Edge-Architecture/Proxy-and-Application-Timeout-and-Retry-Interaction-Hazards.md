---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# When Gateway, Service Mesh, and Application Retries Collide: Coordinating Timeout Hierarchies Across Hops

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

Prerequisites: [[Service-Mesh-Architecture-Sidecar-vs-Ambient-Mesh-and-East-West-Policy]]

Next: [[HTTP-Desync-and-Header-Normalization-Across-Multi-Tier-Proxies]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]]

Lab plan: Reproduces the outage where an API gateway timeout (2.0s) is shorter than the backend's 3-retry loop (3 x 1.0s = 3.0s), causing wasted backend work on already-abandoned gateway requests.

## Further reading
