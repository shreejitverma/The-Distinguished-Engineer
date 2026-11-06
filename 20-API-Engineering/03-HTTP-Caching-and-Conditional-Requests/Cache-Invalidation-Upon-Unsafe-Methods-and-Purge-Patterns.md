---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Automatic Cache Invalidation on POST/PUT/PATCH/DELETE and Surrogate-Key Tag Purging (RFC 9111)

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

Prerequisites: [[Stale-While-Revalidate-and-Stale-If-Error-RFC-5861]]

Next: [[Cache-Status-Header-for-Observability-RFC-9211]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Tests RFC 9111 Section 4.4 invalidation of target URI, Location, and Content-Location upon a non-error write response, plus tag-based Surrogate-Key fan-out eviction.

## Further reading
