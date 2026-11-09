---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Preventing Lost Updates with If-Match, 412 Precondition Failed, and 428 Precondition Required (RFC 9110)

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

Prerequisites: [[Conditional-GET-Revalidation-and-304-Not-Modified]]

Next: [[Vary-Header-Mechanics-and-Cache-Hit-Collapse]]

See also: [[REST-APIs]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Runs two concurrent FastAPI PUT/PATCH clients updating the same resource and proves that requiring If-Match with an ETag version column converts a silent lost update into a deterministic 412 Precondition Failed.

## Further reading
