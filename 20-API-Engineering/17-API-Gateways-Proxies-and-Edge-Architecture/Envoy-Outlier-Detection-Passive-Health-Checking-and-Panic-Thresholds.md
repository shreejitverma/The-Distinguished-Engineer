---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Outlier Detection and Panic Thresholds in Envoy: Ejecting Failing Pods on Consecutive 5xx While Preventing Cascading Fleet Blackouts

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

Prerequisites: [[Envoy-Proxy-Internals-Listeners-Filter-Chains-HCM-and-Clusters]]

Next: [[External-Authorization-Ext-Authz-and-Global-Rate-Limit-Filters]]

Lab plan: Simulates an upstream cluster where 60% of endpoints fail health checks and proves how a 50% panic threshold fails open (routing to all hosts) to prevent the remaining 40% from melting down.

## Further reading
