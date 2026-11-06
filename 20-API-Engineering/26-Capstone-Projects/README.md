---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# Capstone Projects

Contains six comprehensive, production-grade capstone projects with complete specifications, architectural invariants, and automated acceptance test suites in `labs/tests/f26_projects/`.
Following the shell book (`19-Shell-and-Unix-Engineering`), a distinguished engineer candidate should complete at least two capstones from scratch and pass the full verification harness.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [Project 1: Build a Spec-Compliant ASGI 3.0 HTTP/1.1 Server, Lifespan Manager, and Radix-Tree Router from Raw asyncio Sockets and h11 (0.16.0)](Project-1-Minimal-ASGI-3-0-Server-and-Router-from-Asyncio-Sockets-and-H11.md)
2. [Project 2: Build a Reference Resource-Oriented API in FastAPI (0.162.0) and SQLAlchemy (2.1.8) with AIP-158 Encrypted Cursors, AIP-160 Filter ASTs, AIP-154 ETags, AIP-151 LROs, and RFC 9457 Errors](Project-2-Google-AIP-Compliant-Resource-API-with-Cursors-Filtering-ETags-and-RFC-9457.md)
3. [Project 3: Build a Stripe-Style Date-Versioned API Transformation Engine and Multi-Phase Idempotency-Key Middleware (draft-08) with Deprecation (RFC 9745) and Sunset (RFC 8594)](Project-3-Stripe-Grade-Versioned-API-and-Multi-Phase-Idempotency-Engine.md)
4. [Project 4: Build an OAuth 2.1 (draft-17) Authorization and Resource Server with S256 PKCE, Refresh Token Rotation (RFC 9700), DPoP Proof-of-Possession (RFC 9449), and Tenant-Scoped BOLA Guards](Project-4-Zero-Trust-OAuth-2-1-DPoP-and-Tenant-Isolated-Resource-Server.md)
5. [Project 5: Build a High-Throughput Transactional Outbox and Standard Webhooks / RFC 9421 Delivery Engine with GCRA Rate Limiting, SSRF Guards, and End-to-End W3C OpenTelemetry Tracing](Project-5-Transactional-Outbox-and-Signed-Webhook-Delivery-Engine-with-OpenTelemetry.md)
6. [Project 6: Build an Automated API Governance Toolchain: OpenAPI 3.2.1 / 3.1.2 Custom Linter, Semantic Breaking-Change Detector, Typed SDK Generator, and Stateful Fuzzing Gate](Project-6-Contract-First-Governance-CI-Pipeline-Linter-Breaking-Diff-and-Schemathesis-Harness.md)
