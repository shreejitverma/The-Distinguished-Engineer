---
type: playbook
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Executing an API Sunset: Per-Tenant Version Telemetry, Automated Brownout Windows, and 410 Gone Cutover

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Versioning Evolution and API Guidelines](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Deprecation-Header-RFC-9745-and-Sunset-Header-RFC-8594]]

Next: [[API-Discovery-and-Well-Known-API-Catalog-RFC-9727]]

Lab plan: Simulates a scheduled brownout middleware that rejects deprecated v1 traffic with 410 Gone + Problem Details during a configurable 15-minute window per hour while allowing emergency opt-out headers.

## Further reading
