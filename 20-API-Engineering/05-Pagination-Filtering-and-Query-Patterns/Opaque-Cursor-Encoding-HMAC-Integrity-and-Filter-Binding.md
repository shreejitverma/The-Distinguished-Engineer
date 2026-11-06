---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Opaque Page Tokens: Base64URL Encoding, HMAC/AEAD Signing, and Binding Cursors to Query Filters (AIP-158)

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

Prerequisites: [[Keyset-Seek-Pagination-and-Composite-Tie-Breaker-Cursors]]

Next: [[Bidirectional-Keyset-Pagination-and-Relay-Cursor-Connections]]

See also: [[Pagination-Strategies]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]]

Lab plan: Signs and verifies opaque page_token cursors bound to the request's filter and order_by hash, rejecting tampered cursors or cursors reused with modified filter parameters with 400 Bad Request.

## Further reading
