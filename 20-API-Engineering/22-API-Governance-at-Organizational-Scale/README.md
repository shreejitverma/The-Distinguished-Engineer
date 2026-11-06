---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# API Governance at Organizational Scale

Addresses the socio-technical challenge of governing hundreds or thousands of APIs across a large engineering organization (mirroring Folder 23 of the shell book).
Covers estate inventory (`/.well-known/api-catalog` RFC 9727), zombie API eradication, running an effective API Design Review Board, rolling out automated Spectral/vacuum and Buf lint gates without blocking delivery, golden-path service templates, internal platform SDKs, multi-year deprecation campaigns, and public developer portal engineering.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [Building an Organizational API Estate Inventory: RFC 9727 api-catalog Aggregation, Gateway Traffic Reconciliation, and Detecting Undocumented Shadow/Zombie Endpoints](API-Estate-Inventory-Cataloging-RFC-9727-and-Shadow-API-Discovery.md)
2. [Running an Effective API Design Review Board: Lightweight PR-Driven Design Reviews, Exception Waivers, and Avoiding Architecture Astronautics](Running-an-API-Design-Review-Board-Lightweight-AIPs-and-Review-Checklists.md)
3. [Rolling Out Automated OpenAPI and Protobuf Linting Across an Existing Estate: Baseline Suppression Ratchets, Severity Tiers, and Zero-Regression CI Gates](Rolling-Out-Automated-API-Linting-Across-Hundreds-of-Repositories.md)
4. [The Paved Road: Engineering an Internal FastAPI Chassis Library with Pre-Wired Auth, RFC 9457 Errors, OpenTelemetry, Idempotency, and Security Headers](Golden-Path-API-Service-Templates-and-Internal-Platform-Chassis.md)
5. [Managing Shared Schemas and Contracts Across Monorepos and Polyrepos: Central Schema Registries, Semantic Versioning, and Automated SDK Publish Pipelines](Contract-Governance-in-Monorepos-vs-Polyrepos-Central-Schema-Registries.md)
6. [Leading Multi-Year API Migrations: Strangler Fig Gateway Routing, Per-Client Endpoint Usage Dashboards, Automated Migration Codemods, and Contract Cutover](Multi-Year-API-Migrations-Strangler-Fig-Proxies-and-Client-Telemetry-Tracking.md)
7. [Engineering a World-Class Public Developer Platform: Isolated Sandbox Environments, Deterministic Test Clocks, Interactive OpenAPI Explorers, and Automated Changelogs](Public-Developer-Platform-Engineering-Sandboxes-Docs-and-Changelog-Automation.md)
8. [Why Raw Codegen Produces Mediocre SDKs and How to Engineer Idiomatic Client Libraries with Auto-Pagination Async Iterators, Typed Errors, and Built-In Retries](Multi-Language-SDK-Engineering-Idiomatic-Generators-Retries-and-Pagination-Iterators.md)
9. [Metering and Usage-Based API Billing: High-Throughput Idempotent Usage Event Ingestion, Rollup Windows, and Quota Hard-Caps vs Overages](API-Pricing-Metering-Usage-Based-Billing-and-Idempotent-Usage-Ingestion.md)
10. [Conway's Law in API Architecture: Why Exposing Internal Database Tables or Microservice Org Charts as Public APIs Guarantees Breaking Changes](Socio-Technical-API-Ownership-Conways-Law-and-Domain-Boundary-Antipatterns.md)
