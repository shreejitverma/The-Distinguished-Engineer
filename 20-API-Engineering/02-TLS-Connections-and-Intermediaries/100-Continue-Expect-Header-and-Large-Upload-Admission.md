---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Expect: 100-continue Mechanics: Rejecting Unauthorized or Oversized Uploads Before Body Transmission

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

Prerequisites: [[Forwarded-Header-RFC-7239-and-X-Forwarded-For-Spoofing]]

Next: [[103-Early-Hints-and-Informational-Responses]]

See also: [[curl-in-Depth]]

Lab plan: Uses h11 0.16.0 state transitions to reject an unauthenticated 50 MB upload request with 401 Unauthorized or 413 Content Too Large before the client transmits the request body.

## Further reading
