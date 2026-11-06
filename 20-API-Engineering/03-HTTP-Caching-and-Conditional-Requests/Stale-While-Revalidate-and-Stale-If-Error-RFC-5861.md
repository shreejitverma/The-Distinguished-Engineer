---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Serving Stale Content Safely: stale-while-revalidate and stale-if-error Under Origin Outages (RFC 5861)

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

Prerequisites: [[Vary-Header-Mechanics-and-Cache-Hit-Collapse]]

Next: [[Cache-Invalidation-Upon-Unsafe-Methods-and-Purge-Patterns]]

Lab plan: Implements a caching HTTP proxy middleware that serves a slightly expired response immediately while triggering an asynchronous background revalidation task, falling back to stale-if-error on 503.

## Further reading
