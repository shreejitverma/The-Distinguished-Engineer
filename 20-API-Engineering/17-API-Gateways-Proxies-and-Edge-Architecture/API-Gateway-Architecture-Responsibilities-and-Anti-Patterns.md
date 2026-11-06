---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# API Gateway Responsibilities: Edge TLS, Auth Offloading, Rate Limiting, Routing, and Why Putting Business Logic in the Gateway Creates a Distributed Monolith

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

Prerequisites: [[Load-Balancing-Algorithms-Round-Robin-Least-Request-P2C-and-Consistent-Hashing]]

Next: [[Envoy-Proxy-Internals-Listeners-Filter-Chains-HCM-and-Clusters]]

See also: [[04-System-Design/04-APIs-and-Protocols/README]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]]

Lab plan: Tests an edge gateway routing table that performs JWT verification, strips external internal-only headers, injects verified identity claims, and routes by path prefix and version.

## Further reading
