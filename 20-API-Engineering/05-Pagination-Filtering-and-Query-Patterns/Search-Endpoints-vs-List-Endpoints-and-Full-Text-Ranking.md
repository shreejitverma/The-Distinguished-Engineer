---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Search vs List Endpoints: Relevance Ranking, Facets, Fuzzy Matching, and Cursor Tie-Breaking on Score

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

Prerequisites: [[Subresource-Expansion-and-Include-Directives]]

Next: [[Streaming-Exports-and-NDJSON-for-Unbounded-Collections]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]]

Lab plan: Implements a full-text search endpoint with SQLite FTS5 BM25 relevance ranking and proves how composite (score, id) cursors paginate ranked search results deterministically.

## Further reading
