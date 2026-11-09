---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# Pagination Filtering and Query Patterns

Examines collection querying at scale: offset/limit costs, keyset (seek) pagination over composite indexes, opaque authenticated cursors (AIP-158), filtering grammars and AST evaluation (AIP-160), multi-field sorting, sparse fieldsets (AIP-157), and total count trade-offs.
Demonstrates how query surface design directly dictates database execution plans in PostgreSQL and SQLite.
Provides executable patterns that guarantee O(1) page fetch latency and prevent cursor tampering or filter injection.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [Offset/Limit Pagination: Linear Index Scan Degradation and the Concurrent Insert Window-Drift Bug](Offset-Limit-Pagination-Mechanics-Cost-and-Window-Drift.md)
2. [Keyset (Seek) Pagination: Row-Value Comparisons (created_at, id) and Composite B-Tree Index Alignment](Keyset-Seek-Pagination-and-Composite-Tie-Breaker-Cursors.md)
3. [Opaque Page Tokens: Base64URL Encoding, HMAC/AEAD Signing, and Binding Cursors to Query Filters (AIP-158)](Opaque-Cursor-Encoding-HMAC-Integrity-and-Filter-Binding.md)
4. [Bidirectional Pagination: Forward/Backward Cursors (first/after, last/before) and Relay Connection Envelopes](Bidirectional-Keyset-Pagination-and-Relay-Cursor-Connections.md)
5. [Snapshot-Consistent Pagination Across Multi-Page Scans and Point-in-Time Read Tokens](Snapshot-Consistent-Pagination-Across-Point-in-Time-Reads.md)
6. [Why Exact total_count Breaks Large Collection Pagination and How to Use Capped or Planner Estimates](Total-Count-Antipattern-and-Estimated-Counts.md)
7. [Query Parameter Filtering Patterns: LHS Brackets (price[gte]), Comma Lists, and Type-Safe Pydantic Parsing](Query-Parameter-Filtering-Brackets-RHS-Colon-and-Validation.md)
8. [Structured Filter Languages: Parsing Google AIP-160 Filter Expressions to Safe SQL ASTs](Structured-Filter-Expressions-and-AST-Compilation-AIP-160.md)
9. [Multi-Column Sorting (order_by), Nulls Ordering, Allowlist Validation, and Index-Sort Alignment](Multi-Column-Sorting-Order-By-Whitelisting-and-Index-Coverage.md)
10. [Sparse Fieldsets and Partial Responses (fields / read_mask, AIP-157): Pushing Projections Down to SQL](Sparse-Fieldsets-Partial-Responses-and-Read-Masks-AIP-157.md)
11. [Compound Documents and Resource Expansion (?include= / expand[]) Without N+1 Query Explosions](Subresource-Expansion-and-Include-Directives.md)
12. [Search vs List Endpoints: Relevance Ranking, Facets, Fuzzy Matching, and Cursor Tie-Breaking on Score](Search-Endpoints-vs-List-Endpoints-and-Full-Text-Ranking.md)
13. [When Pagination Is the Wrong Tool: Streaming Large Collections with application/x-ndjson and Server-Side Cursors](Streaming-Exports-and-NDJSON-for-Unbounded-Collections.md)
