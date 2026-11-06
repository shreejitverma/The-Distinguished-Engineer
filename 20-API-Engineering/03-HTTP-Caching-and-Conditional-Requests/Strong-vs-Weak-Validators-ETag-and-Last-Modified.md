---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Strong vs Weak Validators: ETag Generation, W/ Prefix Semantics, and Last-Modified Granularity (RFC 9110)

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [HTTP Caching and Conditional Requests](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Cache-Control-Directives-and-Freshness-Lifetime-Calculation]]

Next: [[Compression-and-Content-Encoding-Aware-ETags]]

See also: [[REST-APIs]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]]

Lab plan: Tests strong versus weak comparison functions from RFC 9110 Section 8.8.3.2 and proves why weak ETags cannot be used for byte-range requests or If-Match write preconditions.

## Further reading
