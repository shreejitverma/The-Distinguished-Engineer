---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# Versioning Evolution and API Guidelines

Addresses the multi-year evolution of APIs without breaking existing clients.
Defines wire, source, and semantic compatibility (Google AIP-180), compares versioning topologies (URI, header, media-type, query, and release channels in AIP-185), and dissects Stripe's date-based request/response version transformation architecture.
Covers the March 2025 Deprecation header standard (RFC 9745), the Sunset header (RFC 8594), API discovery via `/.well-known/api-catalog` (RFC 9727), and a comparative deep dive into the Google AIP, Microsoft Azure, and Zalando API guideline systems.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [Compatibility Taxonomy: Wire, Source (SDK), and Semantic Backward and Forward Compatibility (AIP-180)](Compatibility-Taxonomy-Wire-Source-and-Semantic-Compatibility-AIP-180.md)
2. [Catalog of Breaking Changes in HTTP/JSON APIs: Request Tightening, Response Loosening, Enum Additions, and Behavioral Shifts](Anatomy-of-Breaking-Changes-in-HTTP-and-JSON-APIs.md)
3. [Evolving APIs Without Versioning: Additive Fields, Optionality Invariants, and the Tolerant Reader Pattern](Additive-Schema-Evolution-and-Tolerant-Reader-Patterns.md)
4. [API Versioning Topologies Compared: URI Path (/v1), Header, Accept Media-Type, Query, and Stability Channels (AIP-185)](API-Versioning-Strategies-URI-Header-Media-Type-and-Channel-AIP-185.md)
5. [Stripe's Date-Based API Versioning Engine: Account Pinning, Stripe-Version Overrides, and Bidirectional Transformers](Stripe-Date-Based-Versioning-and-Version-Change-Gates.md)
6. [Standardized API Retirement: The Deprecation Header (@timestamp, RFC 9745) and Sunset Header (HTTP-date, RFC 8594)](Deprecation-Header-RFC-9745-and-Sunset-Header-RFC-8594.md)
7. [Executing an API Sunset: Per-Tenant Version Telemetry, Automated Brownout Windows, and 410 Gone Cutover](Brownout-Schedules-Per-Client-Usage-Telemetry-and-Safe-Sunsetting.md)
8. [Machine-Readable API Discovery: /.well-known/api-catalog and Linkset Documents (RFC 9727)](API-Discovery-and-Well-Known-API-Catalog-RFC-9727.md)
9. [Google API Improvement Proposals (AIPs) in Depth: Resource Orientation, Standard Methods, and Protobuf-HTTP Parity](Google-AIP-System-Architecture-and-Core-Tenets.md)
10. [The Microsoft and Azure REST API Guidelines: api-version Query Rules, Long-Running Operations, and Error Envelopes](Microsoft-Azure-REST-API-Guidelines-in-Depth.md)
11. [The Zalando RESTful API and Event Guidelines: snake_case JSON, Problem Details, Money Objects, and Event Envelopes](Zalando-RESTful-API-and-Event-Guidelines-in-Depth.md)
12. [Where Google, Microsoft, Zalando, and Stripe Disagree: Choosing Coherent Conventions for Your Organization](Synthesizing-an-Organizational-API-Style-Guide.md)
13. [Representing Money, Decimals, Timestamps (RFC 3339 / ISO 8601), Durations, and IANA Timezones in JSON APIs (AIP-142)](Monetary-Quantities-Timestamps-Timezones-and-Locale-Contracts.md)
14. [Modeling Multi-Tenancy, Organizations, Projects, and Data Residency Regions in Resource Hierarchy and Headers](Multi-Tenancy-and-Region-Routing-in-API-Contracts.md)
