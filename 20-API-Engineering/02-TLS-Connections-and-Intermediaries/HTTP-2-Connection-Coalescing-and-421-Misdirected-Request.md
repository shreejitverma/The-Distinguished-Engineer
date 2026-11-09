---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# HTTP/2 and HTTP/3 Connection Coalescing Across Wildcard Certificates and 421 Misdirected Request

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [TLS Connections and Intermediaries](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Outbound-Connection-Pools-Limits-and-DNS-TTL-Staleness]]

Next: [[Hop-by-Hop-vs-End-to-End-Headers-and-Connection-Header-Abuse]]

See also: [[HTTP-Evolution-HTTP1-HTTP2-HTTP3]]

Lab plan: Simulates a client reusing a coalesced TLS/HTTP2 connection for a second hostname covered by a SAN certificate that requires mTLS, triggering 421 Misdirected Request and retry on a fresh connection.

## Further reading
