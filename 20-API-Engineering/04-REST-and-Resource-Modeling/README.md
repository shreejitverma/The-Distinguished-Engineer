---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# REST and Resource Modeling

Grounds REST in Roy Fielding's 2000 doctoral dissertation and traces how its six architectural constraints map to modern resource-oriented HTTP APIs (Google AIP-121 through AIP-164).
Covers the Richardson Maturity Model, hypermedia controls (RFC 8288), resource hierarchy, canonical identifiers (UUIDv7 in RFC 9562), method safety and idempotency (including the HTTP QUERY method in RFC 10008), status code selection, partial updates (JSON Patch RFC 6902, Merge Patch RFC 7396, and field masks AIP-161), custom actions, and soft deletion.
Contrasts academic hypermedia purity with pragmatic production resource modeling.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [Fielding's Dissertation in Depth: Deriving the Six REST Constraints and Uniform Interface](Fieldings-Dissertation-and-the-Six-Constraints-of-REST.md)
2. [The Richardson Maturity Model, HATEOAS State Transitions, and Web Linking (RFC 8288)](Richardson-Maturity-Model-HATEOAS-and-Web-Linking-RFC-8288.md)
3. [Resource-Oriented Design: Collections, Singleton Resources, Sub-Resources, and Hierarchy Limits (AIP-121)](Resource-Oriented-Design-Collections-Resources-and-Subresources.md)
4. [Resource Identifiers: Sequential Integers vs UUIDv4 vs Time-Ordered UUIDv7 (RFC 9562) and Type-Prefixed IDs](Resource-Identifiers-UUIDv4-vs-UUIDv7-ULID-and-Public-IDs.md)
5. [HTTP Method Semantics, Safety, and Idempotency: GET, HEAD, OPTIONS, PUT, DELETE, POST, and PATCH (RFC 9110)](HTTP-Method-Semantics-Safety-and-Idempotency-Invariants.md)
6. [The HTTP QUERY Method: Safe, Idempotent, Cacheable Requests with a Request Body (RFC 10008)](The-HTTP-QUERY-Method-Safe-Requests-with-Bodies-RFC-10008.md)
7. [HTTP Status Code Selection: 200 vs 201 vs 202 vs 204, 3xx Redirects, and 400 vs 401 vs 403 vs 404 vs 409 vs 422](HTTP-Status-Code-Taxonomy-and-Selection-Decision-Trees.md)
8. [PUT Semantics: Full Replacement, Defaulting Omitted Fields, and Client-Supplied ID Creation (AIP-133)](Full-Replacement-PUT-and-Upsert-Semantics.md)
9. [Partial Updates with PATCH: JSON Merge Patch (RFC 7396) vs JSON Patch Operations (RFC 6902)](Partial-Updates-JSON-Merge-Patch-RFC-7396-vs-JSON-Patch-RFC-6902.md)
10. [Update Masks and Field Masks (Google AIP-134 and AIP-161) and Pydantic exclude_unset Semantics](Field-Masks-and-Update-Masks-AIP-134-and-AIP-161.md)
11. [Custom Methods Beyond CRUD: Modeling State Transitions and Action Endpoints (POST :cancel, AIP-136)](Custom-Methods-and-State-Machine-Transitions-AIP-136.md)
12. [Modeling Many-to-Many Associations, Sub-Resource Aliasing, and Polymorphic Resources (AIP-124)](Resource-Associations-Many-to-Many-Links-and-Polymorphism-AIP-124.md)
13. [Soft Delete, Undelete, Tombstones, and Purge Retention Windows (Google AIP-164)](Soft-Delete-Undelete-and-Expiration-Lifecycles-AIP-164.md)
14. [Immutable Resource Revisions, Snapshot History, and Rollback Operations (Google AIP-162)](Resource-Revisions-and-Historical-Snapshots-AIP-162.md)
15. [Declarative-Friendly APIs: Desired vs Reconciling State, uid, etag, and allow_missing Upserts (AIP-128)](Declarative-Friendly-APIs-and-Reconciliation-Loops-AIP-128.md)
16. [Field Behavior Contracts: REQUIRED, OUTPUT_ONLY, INPUT_ONLY, and IMMUTABLE Fields (Google AIP-203)](Field-Behavior-Semantics-Input-Only-Output-Only-and-Immutable-AIP-203.md)
