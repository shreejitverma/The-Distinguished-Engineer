---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Bidirectional Pagination: Forward/Backward Cursors (first/after, last/before) and Relay Connection Envelopes

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Pagination Filtering and Query Patterns](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Opaque-Cursor-Encoding-HMAC-Integrity-and-Filter-Binding]]

Next: [[Snapshot-Consistent-Pagination-Across-Point-in-Time-Reads]]

See also: [[Pagination-Strategies]], [[GraphQL]]

Lab plan: Implements forward and backward keyset pagination using N+1 lookahead fetching to compute has_next_page and has_previous_page without running a separate COUNT(*) query.

## Further reading
