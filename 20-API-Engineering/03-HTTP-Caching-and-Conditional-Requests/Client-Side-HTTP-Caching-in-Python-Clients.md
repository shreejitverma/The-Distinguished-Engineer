---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Implementing RFC 9111 Compliant Client-Side Caching in httpx Transports

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

Prerequisites: [[Caching-Authenticated-and-Multi-Tenant-API-Responses]]

Next: [[Web-Cache-Poisoning-and-Cache-Deception-Attacks]]

Lab plan: Builds a custom httpx.AsyncBaseTransport wrapper implementing ETag storage, conditional If-None-Match injection, 304 response body replay, and max-age expiration.

## Further reading
