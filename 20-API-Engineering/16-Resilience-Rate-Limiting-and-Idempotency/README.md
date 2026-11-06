---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# Resilience Rate Limiting and Idempotency

Covers production reliability engineering for HTTP APIs both as a server protecting its capacity and as a client calling unreliable dependencies.
Details rate limiting algorithms (Token Bucket, Leaky Bucket, Sliding Window, and Generic Cell Rate Algorithm in atomic Redis/Valkey Lua scripts), the IETF `RateLimit` headers (`draft-ietf-httpapi-ratelimit-headers-11`), and the `Idempotency-Key` specification (`draft-ietf-httpapi-idempotency-key-header-08`, updated 2026-10-22).
Examines end-to-end deadline budgets, jittered retries with retry budgets (`tenacity 9.2.1` and `stamina 26.1.0`), adaptive concurrency limits, load shedding, circuit breakers, and webhook delivery engineering (`Standard Webhooks`).

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [Rate Limiting Algorithms Compared: Fixed Window Boundary Bursts, Sliding Window Log, Sliding Window Counter, Token Bucket, and Leaky Bucket](Rate-Limiting-Algorithms-Token-Bucket-Leaky-Bucket-and-Sliding-Window.md)
2. [Distributed Rate Limiting with GCRA (Generic Cell Rate Algorithm) and Atomic Redis/Valkey Lua Scripts](GCRA-Generic-Cell-Rate-Algorithm-and-Atomic-Redis-Valkey-Lua-Limiters.md)
3. [Layered Rate Limiters in Production: Per-IP, Per-Tenant, Concurrent In-Flight Request Limiters, and Query-Cost / Compute-Unit Quotas](Multi-Tier-Rate-Limiting-Quotas-Concurrency-Limits-and-Cost-Budgets.md)
4. [Standardizing Rate Limit Responses: 429 Too Many Requests, Retry-After, and IETF RateLimit and RateLimit-Policy Headers (draft-ietf-httpapi-ratelimit-headers-11)](Standard-RateLimit-Headers-IETF-Draft-and-429-Retry-After-Semantics.md)
5. [The Idempotency-Key HTTP Header Specification (draft-ietf-httpapi-idempotency-key-header-08, October 2026) and Stripe-Grade Recovery State Machines](Idempotency-Key-Header-Specification-and-State-Machine-Draft-08.md)
6. [Multi-Phase Idempotency with Recovery Points: Handling Process Crashes Between External Side Effects and Database Commits](Atomic-Idempotency-Phases-and-Failure-Recovery-Across-Network-Drops.md)
7. [Timeouts Across the API Call Graph: Connect, Read, Write, Pool Timeouts in httpx (0.28.1) and End-to-End Deadline Budget Propagation](Timeout-Taxonomy-Connect-Read-Write-Pool-and-Deadline-Propagation.md)
8. [Client Retry Engineering: Which Status Codes and Exceptions Are Safe to Retry, Full vs Decorrelated Jitter, and stamina (26.1.0) / tenacity (9.2.1)](Retries-Exponential-Backoff-Full-vs-Decorrelated-Jitter-and-Stamina.md)
9. [Why Multi-Hop Retries Multiply Traffic by K^N and How Per-Client Retry Token Ratios (Google SRE Retry Budgets) Stop Retry Storms](Retry-Budgets-and-Preventing-Cascading-Retry-Amplification-Storms.md)
10. [Circuit Breaker Internals: Closed, Open, and Half-Open State Transitions, Sliding Failure Windows, and Why Naive Circuit Breakers Cause Blackhole Outages](Circuit-Breakers-State-Machines-Half-Open-Probing-and-When-Not-to-Use-Them.md)
11. [Self-Tuning Capacity Protection: Adaptive Concurrency Limits Using Little's Law, TCP Vegas, and Gradient2 Queue-Delay Detection](Adaptive-Concurrency-Limits-Littles-Law-Vegas-and-Gradient2.md)
12. [Load Shedding Under Overload: Prioritized Admission Control (Critical vs Background Traffic), CoDel Queue Timeout, and LIFO vs FIFO Under Saturation](Load-Shedding-Priority-Traffic-Classes-and-LIFO-Queue-Management.md)
13. [The Bulkhead Pattern: Isolating Connection Pools, Threadpools, and Semaphores Per Downstream Dependency and Per Tenant Tier](Bulkhead-Isolation-and-Per-Dependency-Resource-Partitioning.md)
14. [Taming p99.9 Tail Latency with Hedged Requests and Cross-Zone Backup Calls Without Doubling Cluster Load](Hedged-Requests-and-Tail-Latency-Taming-at-Scale.md)
15. [Engineering Outbound Webhooks: Standard Webhooks Specification (webhook-id, webhook-timestamp, webhook-signature), Retries, and Dead-Endpoint Circuit Breaking](Webhook-Sender-Architecture-Standard-Webhooks-Signatures-and-Fan-Out.md)
16. [Receiving Webhooks Safely: Raw-Bytes HMAC Verification Before JSON Parsing, 5-Minute Timestamp Replay Windows, and Event-ID Deduplication](Webhook-Consumer-Engineering-Signature-Verification-Replay-Windows-and-Idempotency.md)
