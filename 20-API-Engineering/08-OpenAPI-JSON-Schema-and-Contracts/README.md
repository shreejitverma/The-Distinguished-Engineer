---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# OpenAPI JSON Schema and Contracts

Covers machine-readable API contracts from JSON Schema Draft 2020-12 through OpenAPI 3.1 (`3.1.2`, released 2025-09-19) and OpenAPI 3.2 (`3.2.1`, released 2026-09-10).
Examines schema composition (`oneOf`, `anyOf`, `allOf`, `unevaluatedProperties`), YAML parsing hazards (RFC 9512), code-first versus design-first workflows, automated linting (`vacuum v0.39.0` and `Spectral v6.17.0`), SDK generation, consumer-driven contracts, and stateful property-based API fuzzing with Schemathesis (`4.33.1`) and Hypothesis (`6.170.14`).
Equips engineers to make the OpenAPI specification an executable, CI-enforced source of truth.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [JSON Schema Draft 2020-12 Internals: $schema, $id, $defs, $ref, $dynamicRef, and Validation Keywords](JSON-Schema-2020-12-Core-and-Validation-Vocabularies.md)
2. [JSON Schema Composition Traps: Why additionalProperties: false Breaks allOf and How unevaluatedProperties Fixes It](JSON-Schema-Composition-AllOf-AnyOf-OneOf-and-UnevaluatedProperties.md)
3. [OpenAPI 3.1 (v3.1.2) Architecture: Full JSON Schema 2020-12 Alignment, Type Arrays for Nullable, and Webhooks](OpenAPI-3-1-Architecture-and-JSON-Schema-Alignment.md)
4. [What Changed in OpenAPI 3.2 (v3.2.1): HTTP QUERY Method, Streaming itemSchema, Tag Hierarchies, and OAuth Metadata](OpenAPI-3-2-Additions-QUERY-Method-Streaming-and-Tag-Hierarchies.md)
5. [YAML Media Type (RFC 9512) and OpenAPI YAML Hazards: YAML 1.1 vs 1.2, Anchor Bomb Billions-Laughs, and Key Coercion](YAML-Media-Type-RFC-9512-and-OpenAPI-YAML-Parser-Hazards.md)
6. [Modeling Polymorphism in OpenAPI 3.1/3.2: oneOf with Discriminator Mapping vs anyOf Performance and Ambiguity](Polymorphism-and-Discriminators-in-OpenAPI-and-JSON-Schema.md)
7. [Design-First vs Code-First OpenAPI Workflows: Preventing Spec-Implementation Drift with Snapshot Gates](Design-First-vs-Code-First-OpenAPI-and-CI-Drift-Gates.md)
8. [Automated OpenAPI Linting: Writing Custom Rulesets for vacuum (v0.39.0) and Spectral (v6.17.0)](OpenAPI-Linting-and-Automated-Style-Governance-with-Vacuum-and-Spectral.md)
9. [Semantic OpenAPI Diffing in CI: Detecting Covariant Request Changes and Contravariant Response Changes](Breaking-Change-Detection-and-Semantic-OpenAPI-Diffing.md)
10. [Schema-First Code Generation: Compiling OpenAPI 3.1 Schemas to Strict Pydantic v2 Models (datamodel-code-generator 0.89.0)](Generating-Pydantic-v2-Models-from-OpenAPI-with-Datamodel-Code-Generator.md)
11. [Client SDK Generation: Typed httpx Clients (openapi-python-client 0.29.1) and Multi-Language SDKs (openapi-generator v7.27.0)](Generating-Typed-Async-SDKs-with-OpenAPI-Python-Client-and-OpenAPI-Generator.md)
12. [OpenAPI Specification Extensions (x-*), Overlays, and Stripping Internal Admin Routes for Public Portals](OpenAPI-Overlays-Extensions-and-Multi-Audience-Specs.md)
13. [Consumer-Driven Contract Testing (Pact) vs Bi-Directional OpenAPI Provider Verification](Consumer-Driven-Contract-Testing-vs-Bi-Directional-Contract-Verification.md)
14. [Property-Based API Fuzzing with Schemathesis (4.33.1) and Hypothesis (6.170.14): Finding 500s Automatically](Property-Based-API-Testing-with-Schemathesis-and-Hypothesis.md)
15. [Stateful Multi-Step API Fuzzing: Chaining POST -> GET -> PATCH -> DELETE via OpenAPI Links in Schemathesis](Stateful-API-State-Machine-Testing-with-OpenAPI-Links-and-Schemathesis.md)
16. [Zero-Tolerance Contract Enforcement: Asserting Every Test Response Matches the OpenAPI Schema and Status Contract](Response-Schema-Conformance-Enforcement-in-Test-Suites.md)
