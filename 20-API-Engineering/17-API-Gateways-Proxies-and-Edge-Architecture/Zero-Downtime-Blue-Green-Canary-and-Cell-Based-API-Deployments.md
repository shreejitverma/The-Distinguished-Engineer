---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Deployment Topologies for Critical APIs: Rolling, Blue-Green, Canary Analysis, and Cell-Based Fault Isolation (Blast Radius Containment)

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

Prerequisites: [[Shadow-Traffic-Dark-Launches-and-Request-Mirroring-at-the-Gateway]]

Next: [[RED-and-USE-Metrics-and-OpenTelemetry-HTTP-Semantic-Conventions]]

Lab plan: Simulates cell-based routing where each tenant is deterministically hashed to one isolated cell (API + DB shard), proving that a poison-pill query in Cell 3 affects at most 1/N of tenants.

## Further reading
