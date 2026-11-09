---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Header Normalization Differences Across CDN, API Gateway, and ASGI Server: Underscores, Duplicate Headers, and Case Folding

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

Prerequisites: [[Proxy-and-Application-Timeout-and-Retry-Interaction-Hazards]]

Next: [[Edge-Caching-and-CDN-Shielding-for-Dynamic-and-Public-APIs]]

Lab plan: Tests how duplicate headers (e.g., two X-Tenant-Id headers) and underscore headers (X_Tenant_Id vs X-Tenant-Id) are combined or dropped across proxy and Starlette Headers lookup, preventing auth-bypass ambiguity.

## Further reading
