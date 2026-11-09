---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Snapshot-Consistent Pagination Across Multi-Page Scans and Point-in-Time Read Tokens

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

Prerequisites: [[Bidirectional-Keyset-Pagination-and-Relay-Cursor-Connections]]

Next: [[Total-Count-Antipattern-and-Estimated-Counts]]

See also: [[Pagination-Strategies]]

Lab plan: Embeds a snapshot high-water mark (max_created_at / transaction visibility bound) inside the page cursor so newly inserted items do not shift or pollute an in-progress multi-page export.

## Further reading
