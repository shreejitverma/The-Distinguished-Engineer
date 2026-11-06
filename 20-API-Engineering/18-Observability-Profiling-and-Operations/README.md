---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# Observability Profiling and Operations

Covers observability, capacity planning, load testing, and production profiling for HTTP APIs.
Details OpenTelemetry (`1.45.2`, released 2026-10-29) distributed tracing with W3C Trace Context and Baggage, semantic conventions for HTTP spans and metrics, high-cardinality label traps, and canonical structured log lines (`structlog 26.1.0`).
Examines latency histograms (`prometheus-client 0.26.0`), liveness versus readiness probes, Locust (`2.47.0`) load testing, the coordinated omission problem, Little's Law capacity planning, CPU/memory profiling, and SLO error-budget burn-rate alerting.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [RED (Rate, Errors, Duration) and USE Metrics for APIs, and OpenTelemetry HTTP Semantic Conventions (http.request.method, http.route, http.response.status_code)](RED-and-USE-Metrics-and-OpenTelemetry-HTTP-Semantic-Conventions.md)
2. [W3C Trace Context (traceparent, tracestate) and Baggage Propagation Across FastAPI, httpx (0.28.1), and SQLAlchemy (2.1.8)](Distributed-Tracing-W3C-Trace-Context-and-Baggage-Propagation.md)
3. [Trace Sampling at High QPS: ParentBased Head Sampling vs Collector Tail-Based Sampling (Always Keeping 5xx Errors and High-Latency Outliers)](Trace-Sampling-Strategies-Head-Based-vs-Tail-Based-Sampling.md)
4. [Structured JSON Logging with structlog (26.1.0) and Canonical Log Lines: Emitting One Rich Summary Event Per HTTP Request](Structured-Logging-with-Structlog-and-Stripe-Canonical-Log-Lines.md)
5. [Measuring API Latency Correctly: Prometheus Classic vs Native Histograms (prometheus-client 0.26.0) and Why Averaging p99 Across Pods Is Mathematically Invalid](Latency-Histograms-Percentiles-DDSketch-and-Why-Averaging-Percentiles-Is-Wrong.md)
6. [Health Check Architecture: Liveness (/healthz) vs Readiness (/readyz) vs Startup Probes and Why Checking Downstream DBs in Liveness Causes Cascading Outages](Health-Check-Engineering-Liveness-Readiness-Startup-and-Cascading-Restarts.md)
7. [Load Testing HTTP APIs with Locust (2.47.0): Open vs Closed Workload Models and the Coordinated Omission Problem](Load-Testing-APIs-with-Locust-and-Open-vs-Closed-Workload-Models.md)
8. [API Capacity Planning: Little's Law (L = lambda * W), Amdahl's Law, and Gunther's Universal Scalability Law (Contention and Coherency Retrograde)](Capacity-Planning-with-Littles-Law-and-the-Universal-Scalability-Law.md)
9. [Profiling Python APIs in Production: sys.monitoring, cProfile vs Wall-Clock Async Profiling, SQLAlchemy Query Attribution, and Memory Flamegraphs](Profiling-Python-API-Services-CPU-Wall-Clock-Async-Tasks-and-Memory.md)
10. [SLO Engineering for APIs: Availability and Latency SLIs, Excluding Client 4xx Errors, and Multi-Window Multi-Burn-Rate Alerts](SLIs-SLOs-Error-Budgets-and-Multi-Window-Burn-Rate-Alerting.md)
11. [Why Origin Metrics Miss Network and DNS Outages: Client-Side SLIs, W3C Network Error Logging (NEL), and Synthetic Canary Probes](Client-Side-Telemetry-Network-Error-Logging-NEL-and-Edge-vs-Origin-Gap.md)
12. [Immutable Audit Trails for Financial and Healthcare APIs: Actor/Subject/Resource/Diff Schema and Cryptographic SHA-256 Hash Chaining](Audit-Logging-for-Regulated-APIs-Tamper-Evident-Hash-Chains-and-Who-Did-What.md)
13. [Dynamic Runtime Configuration and Kill Switches: Safe Rollout Evaluation, Context-Consistent Bucketing, and Stale-Cache Fallback](Feature-Flags-Dynamic-Configuration-and-Safe-Kill-Switches-in-APIs.md)
14. [Sixty-Second API Incident Triage: Differentiating Traffic Spikes, Downstream Stalls, DB Lock Convoys, Event-Loop Blocking, and Bad Deploys](The-Sixty-Second-API-Incident-Triage-Playbook.md)
