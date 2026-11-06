---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# HTTP Caching and Conditional Requests

Presents a rigorous treatment of HTTP caching (RFC 9111) and conditional requests (RFC 9110 Section 13) for API engineers.
Covers private browser caches, shared CDN/gateway caches, freshness lifetime calculation, strong versus weak ETag validators, 304 Not Modified revalidation, and 412 Precondition Failed optimistic concurrency control.
Analyzes Vary header cache-key multiplication, stale-while-revalidate, RFC 9211 Cache-Status observability, surrogate-key invalidation, and cache stampede prevention.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [HTTP Caching Architecture: Private, Shared, and Gateway Caches and Cacheability Rules (RFC 9111)](HTTP-Caching-Architecture-Private-Shared-and-Gateway-Caches.md)
2. [Cache-Control Directives: max-age, s-maxage, no-cache, no-store, private, public, and immutable (RFC 9111)](Cache-Control-Directives-and-Freshness-Lifetime-Calculation.md)
3. [Strong vs Weak Validators: ETag Generation, W/ Prefix Semantics, and Last-Modified Granularity (RFC 9110)](Strong-vs-Weak-Validators-ETag-and-Last-Modified.md)
4. [Why Content-Encoding Must Change the Strong ETag: Avoiding Proxy Cache Corruption Across Gzip and Identity](Compression-and-Content-Encoding-Aware-ETags.md)
5. [Conditional GET Revalidation: If-None-Match, If-Modified-Since, and 304 Not Modified Header Merging](Conditional-GET-Revalidation-and-304-Not-Modified.md)
6. [Preventing Lost Updates with If-Match, 412 Precondition Failed, and 428 Precondition Required (RFC 9110)](Optimistic-Concurrency-Control-If-Match-and-412-Precondition-Failed.md)
7. [The Vary Header: Secondary Cache Keys, Normalization Traps, and Why Vary: User-Agent Destroys Hit Rates](Vary-Header-Mechanics-and-Cache-Hit-Collapse.md)
8. [Serving Stale Content Safely: stale-while-revalidate and stale-if-error Under Origin Outages (RFC 5861)](Stale-While-Revalidate-and-Stale-If-Error-RFC-5861.md)
9. [Automatic Cache Invalidation on POST/PUT/PATCH/DELETE and Surrogate-Key Tag Purging (RFC 9111)](Cache-Invalidation-Upon-Unsafe-Methods-and-Purge-Patterns.md)
10. [Standardized Cache Diagnostics with the Cache-Status Response Header (RFC 9211)](Cache-Status-Header-for-Observability-RFC-9211.md)
11. [Preventing Cache Stampedes: Request Coalescing (Singleflight), Lease Tokens, and XFetch Early Recomputation](Cache-Stampede-Singleflight-Coalescing-and-Probabilistic-Early-Expiration.md)
12. [Caching Authenticated and Multi-Tenant API Responses Without Cross-Tenant Data Leaks](Caching-Authenticated-and-Multi-Tenant-API-Responses.md)
13. [Implementing RFC 9111 Compliant Client-Side Caching in httpx Transports](Client-Side-HTTP-Caching-in-Python-Clients.md)
14. [Web Cache Poisoning via Unkeyed Headers and Web Cache Deception on API Endpoints](Web-Cache-Poisoning-and-Cache-Deception-Attacks.md)
