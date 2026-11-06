---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# API Gateways Proxies and Edge Architecture

Examines the infrastructure topologies sitting in front of and between API services: L4 versus L7 load balancers, reverse proxies, API gateways, Envoy Proxy (`v1.40.2`, released 2026-10-27), Backend-for-Frontend (BFF) aggregators, and service meshes.
Details how Envoy routes, filters, rate-limits, and circuit-breaks HTTP/1.1, HTTP/2, and gRPC traffic.
Analyzes multi-layer failure modes such as proxy/mesh/application retry multiplication, mismatched timeouts across hops, and HTTP/2 connection stickiness on L4 load balancers.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [L4 (TCP/UDP) vs L7 (Application) Load Balancing: Why Multiplexed HTTP/2 and gRPC Connections Pin Traffic to a Single Backend Under L4 Balancing](L4-vs-L7-Load-Balancing-for-HTTP-1-1-HTTP-2-and-gRPC.md)
2. [Load Balancing Algorithms for APIs: Round Robin, Weighted Least Request, Power of Two Random Choices (P2C), and Ring Hash Cache Locality](Load-Balancing-Algorithms-Round-Robin-Least-Request-P2C-and-Consistent-Hashing.md)
3. [API Gateway Responsibilities: Edge TLS, Auth Offloading, Rate Limiting, Routing, and Why Putting Business Logic in the Gateway Creates a Distributed Monolith](API-Gateway-Architecture-Responsibilities-and-Anti-Patterns.md)
4. [Envoy Proxy (v1.40.2) Internals for API Engineers: Listeners, HTTP Connection Manager (HCM), L7 HTTP Filters, Routes, and Upstream Clusters](Envoy-Proxy-Internals-Listeners-Filter-Chains-HCM-and-Clusters.md)
5. [Outlier Detection and Panic Thresholds in Envoy: Ejecting Failing Pods on Consecutive 5xx While Preventing Cascading Fleet Blackouts](Envoy-Outlier-Detection-Passive-Health-Checking-and-Panic-Thresholds.md)
6. [Decoupled Edge Enforcement: Envoy ext_authz and RateLimit gRPC Service Protocols](External-Authorization-Ext-Authz-and-Global-Rate-Limit-Filters.md)
7. [The Backend-for-Frontend (BFF) Pattern: Parallel Fan-Out with AnyIO TaskGroups, Hedged Timeouts, and Graceful Partial Degradation](Backend-for-Frontend-BFF-Aggregation-Fan-Out-and-Partial-Degradation.md)
8. [Service Mesh Architecture for APIs: Sidecar Proxies vs Ambient L4/L7 Tunnels, Automatic mTLS Rotation, and AuthorizationPolicy](Service-Mesh-Architecture-Sidecar-vs-Ambient-Mesh-and-East-West-Policy.md)
9. [When Gateway, Service Mesh, and Application Retries Collide: Coordinating Timeout Hierarchies Across Hops](Proxy-and-Application-Timeout-and-Retry-Interaction-Hazards.md)
10. [Header Normalization Differences Across CDN, API Gateway, and ASGI Server: Underscores, Duplicate Headers, and Case Folding](HTTP-Desync-and-Header-Normalization-Across-Multi-Tier-Proxies.md)
11. [Putting a CDN in Front of an API: Origin Shielding, Collapse-Forwarding, Signed Edge URLs, and Cache-Control: s-maxage](Edge-Caching-and-CDN-Shielding-for-Dynamic-and-Public-APIs.md)
12. [Risk-Free API Replacements: Request Mirroring (Shadow Traffic), Fire-and-Forget Response Diffing, and Mutation Safety Guards](Shadow-Traffic-Dark-Launches-and-Request-Mirroring-at-the-Gateway.md)
13. [Deployment Topologies for Critical APIs: Rolling, Blue-Green, Canary Analysis, and Cell-Based Fault Isolation (Blast Radius Containment)](Zero-Downtime-Blue-Green-Canary-and-Cell-Based-API-Deployments.md)
