---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# Interview Drills and Design Reviews

Provides structured interview and design-review drills calibrated across L5 (Senior), L6 (Staff), L7 (Principal), and L8+ (Distinguished) engineering bars, mirroring Folder 25 of the shell book.
Includes the complete end-to-end life of an HTTP request down to system calls and C/Rust extensions, predict-the-output drills on Pydantic v2, FastAPI, Starlette, and `asyncio`, debug-this-API exercises, OpenAPI design-review critiques, and full system-design drills.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [End-to-End Walkthrough: What Happens From curl Enter to DNS, TLS 1.3, HTTP/2 Frame, epoll, uvloop, httptools, Uvicorn, Starlette Middleware, FastAPI DI, pydantic-core, asyncpg, and Back](What-Happens-When-a-Client-Calls-Your-FastAPI-Endpoint-End-to-End.md)
2. [Predict-the-Output Drills: 15 Pydantic v2 (2.14.3) Traps in Lax vs Strict Coercion, Default Factories, Validator Ordering, and Union Dispatch](Predict-the-Output-Pydantic-v2-Coercion-Validators-and-Serialization-Edge-Cases.md)
3. [Predict-the-Output Drills: 15 FastAPI (0.162.0) and Starlette (1.10.0) Traps in Dependency Caching, Yield Cleanup Order, BaseHTTPMiddleware ContextVars, and Status Codes](Predict-the-Output-FastAPI-Dependencies-Yield-Scopes-and-Starlette-Middleware.md)
4. [Predict-the-Output Drills: 15 Asyncio and SQLAlchemy 2.x (2.1.8) Concurrency Traps (Swallowed CancelledError, Eager Tasks, MissingGreenlet, and Lost Updates)](Predict-the-Output-Asyncio-TaskGroups-Cancellation-and-SQLAlchemy-Sessions.md)
5. [Debug-This-API Drills: Diagnose and Fix 10 Broken Production FastAPI + SQLAlchemy Endpoints (Memory Leaks, N+1, Thread Starvation, BOLA, and Broken Retries)](Debug-This-API-Production-Incident-Exercises-in-FastAPI-and-SQLAlchemy.md)
6. [Staff API Design Review Drill: Find and Fix 25 Architectural, Security, and Evolvability Flaws in a Realistic E-Commerce OpenAPI Specification](API-Design-Review-Drill-Critiquing-and-Fixing-a-Flawed-REST-OpenAPI-Spec.md)
7. [System Design Drill (L6/L7): Design a Double-Entry Payment and Ledger API with Multi-Phase Idempotency-Keys, Optimistic/Pessimistic Locking, and Transactional Outbox Webhooks](System-Design-Drill-Ledger-and-Payment-Processing-API-with-Idempotency-and-Webhooks.md)
8. [System Design Drill (L6/L7): Design a Low-Latency Distributed Rate Limiter, Quota Enforcement, and Load-Shedding Gateway](System-Design-Drill-Multi-Tenant-Global-Rate-Limiter-and-Quota-Gateway.md)
9. [System Design Drill (L6/L7): Design a Multi-Tenant Webhook Delivery Platform Handling 50,000 Events/Second with Noisy-Neighbor Isolation, SSRF Guards, and Replay APIs](System-Design-Drill-High-Fan-Out-Webhook-and-Event-Delivery-Platform.md)
10. [System Design Drill (L7): Design an Enterprise AI/LLM Gateway with SSE Token Streaming, Pre-Flight TPM Admission, Semantic Caching, and Automatic Provider Failover](System-Design-Drill-Multi-Model-Streaming-AI-Gateway-with-TPM-Quotas.md)
11. [System Design Drill (L7/L8+): Design an Organizational API Versioning, Rolling Request/Response Transformation, and Zero-Downtime Database Migration Architecture](System-Design-Drill-Ten-Year-API-Versioning-and-Zero-Downtime-Schema-Evolution-Engine.md)
12. [The L6 (Staff), L7 (Principal), and L8+ (Distinguished) Bar in API Interviews: How Seniority Shows in Trade-Off Analysis, Failure Modes, and Contract Governance](The-Staff-Principal-and-Distinguished-API-Engineering-Bar.md)
