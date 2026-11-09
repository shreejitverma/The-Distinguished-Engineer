---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Decoupled Edge Enforcement: Envoy ext_authz and RateLimit gRPC Service Protocols

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

Prerequisites: [[Envoy-Outlier-Detection-Passive-Health-Checking-and-Panic-Thresholds]]

Next: [[Backend-for-Frontend-BFF-Aggregation-Fan-Out-and-Partial-Degradation]]

See also: [[API-Authentication-and-Authorization]]

Lab plan: Implements an external authorization decision service that evaluates inbound request headers/paths and returns allow (with injected X-Auth-Context headers) or deny (403 with RFC 9457 body).

## Further reading
