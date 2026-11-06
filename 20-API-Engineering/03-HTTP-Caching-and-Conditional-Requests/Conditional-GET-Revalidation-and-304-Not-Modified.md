---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Conditional GET Revalidation: If-None-Match, If-Modified-Since, and 304 Not Modified Header Merging

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

Prerequisites: [[Compression-and-Content-Encoding-Aware-ETags]]

Next: [[Optimistic-Concurrency-Control-If-Match-and-412-Precondition-Failed]]

See also: [[REST-APIs]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]]

Lab plan: Verifies RFC 9110 Section 13.2.2 precedence when both If-None-Match and If-Modified-Since are present and tests RFC 9111 Section 4.3.4 cache header updates upon receiving 304 Not Modified.

## Further reading
