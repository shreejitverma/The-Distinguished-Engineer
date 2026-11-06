---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# API Failure Case Studies

Reconstructs twelve real-world classes of catastrophic API outages and security breaches from primary postmortems, CVEs, and engineering analyses.
Every case study includes an executable `pytest` reproduction of the exact failure mechanism in a controlled local harness and proves the architectural fix.
Covers the October 2023 HTTP/2 Rapid Reset DDoS attack (CVE-2023-44487), retry-storm metastability, threadpool/connection-pool starvation, JWT algorithm confusion, BOLA/BOPLA breaches, GraphQL complexity bombs, request smuggling, CDN cache poisoning, migration lock convoys, and SSRF via webhooks.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [Case Study: The October 2023 HTTP/2 Rapid Reset DDoS Attack (CVE-2023-44487) Bypassing SETTINGS_MAX_CONCURRENT_STREAMS](Case-Study-HTTP-2-Rapid-Reset-DDoS-CVE-2023-44487.md)
2. [Case Study: Cascading Retry Storms and Metastable Failure After a Transient Database Stall (AWS and Google SRE Overload Analyses)](Case-Study-Cascading-Retry-Storms-and-Metastable-Overload-Collapse.md)
3. [Case Study: How One Synchronous HTTP or File Call Inside an async def Endpoint Froze Event Loops, Tripped Liveness Probes, and Caused a Fleet-Wide Outage](Case-Study-Blocking-Call-Inside-Async-Def-Freezing-the-Entire-Worker-Fleet.md)
4. [Case Study: Database Connection Pool Starvation Caused by Holding a Request-Scoped SQLAlchemy Session Open While Awaiting a Slow Third-Party API](Case-Study-Connection-Pool-Exhaustion-from-Holding-DB-Sessions-Across-Slow-Outbound-HTTP-Calls.md)
5. [Case Study: Authentication Bypass via RS256-to-HS256 Key Confusion, alg: none, and Unvalidated jku / kid Header Exploits (RFC 8725)](Case-Study-JWT-Key-Confusion-and-Unverified-Header-KID-SSRF-Breaches.md)
6. [Case Study: Cross-Tenant Data Exfiltration via BOLA (API1:2023) and Admin Privilege Escalation via Mass Assignment (API3:2023)](Case-Study-Multi-Tenant-BOLA-and-Mass-Assignment-Privilege-Escalation.md)
7. [Case Study: HTTP/2-to-HTTP/1.1 Downgrade Request Smuggling (H2.CL and H2.TE) and Unauthorized H2C Upgrade Tunneling Across Reverse Proxies](Case-Study-HTTP-Request-Smuggling-and-H2C-H2-Downgrade-Desync.md)
8. [Case Study: Serving User A's Authenticated Profile to User B Due to Missing Vary: Authorization and Misconfigured CDN Edge Caching Rules](Case-Study-CDN-Cache-Leak-of-Authenticated-PII-Responses.md)
9. [Case Study: How a 50-Millisecond ALTER TABLE ADD COLUMN Blocked Behind a Long-Running Query and Queued Every API Request Behind an ACCESS EXCLUSIVE Lock Convoy](Case-Study-Database-Migration-ACCESS-EXCLUSIVE-Lock-Convoy-Taking-Down-an-API.md)
10. [Case Study: Financial Double-Withdrawal Race Condition from Check-Then-Act Balance Validation and Non-Atomic Idempotency Checks](Case-Study-Double-Spend-Race-Condition-from-Non-Atomic-Idempotency-and-Read-Committed.md)
11. [Case Study: How Unbounded Webhook Fan-Out Saturated Outbound Workers and How a User-Supplied Callback URL Extracted Cloud IAM Credentials via 169.254.169.254 SSRF](Case-Study-Webhook-Self-DDoS-and-Cloud-Metadata-SSRF-Exploit.md)
12. [Case Study: Taking Down a GraphQL Gateway with Circular Fragment Expansion, Deeply Nested Connections, and 1,000-Operation Batch Arrays](Case-Study-GraphQL-Batching-and-Circular-Fragment-DoS-Outage.md)
