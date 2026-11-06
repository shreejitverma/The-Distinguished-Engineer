---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Preventing Cache Stampedes: Request Coalescing (Singleflight), Lease Tokens, and XFetch Early Recomputation

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

Prerequisites: [[Cache-Status-Header-for-Observability-RFC-9211]]

Next: [[Caching-Authenticated-and-Multi-Tenant-API-Responses]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]]

Lab plan: Fires 100 concurrent asyncio requests at an expired cache key and proves that an asyncio.Future singleflight coalescer executes the database query exactly once instead of 100 times.

## Further reading
