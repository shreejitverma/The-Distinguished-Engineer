---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# The Vary Header: Secondary Cache Keys, Normalization Traps, and Why Vary: User-Agent Destroys Hit Rates

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

Prerequisites: [[Optimistic-Concurrency-Control-If-Match-and-412-Precondition-Failed]]

Next: [[Stale-While-Revalidate-and-Stale-If-Error-RFC-5861]]

Lab plan: Simulates a shared cache indexing responses by (URI, Vary headers) and demonstrates how unnormalized Accept-Language or Authorization headers collapse cache hit ratios to near zero.

## Further reading
