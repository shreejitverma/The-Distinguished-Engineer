---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# API Authorization and OWASP Security

Covers fine-grained API authorization models (OAuth scopes, RBAC, ABAC, and Zanzibar-style ReBAC) and every risk in the OWASP API Security Top 10 (2023 Edition).
Demonstrates how to eliminate Broken Object Level Authorization (BOLA / API1:2023) at the SQLAlchemy query and PostgreSQL Row-Level Security layer, Broken Object Property Level Authorization (BOPLA / API3:2023) via strict input/output schema separation, and SSRF (API7:2023) via DNS-rebinding-safe network transports.
Includes CORS (WHATWG Fetch), CSRF, input resource limits (API4:2023), security headers, and log scrubbing.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [API Authorization Models Compared: OAuth Coarse Scopes vs Role-Based (RBAC) vs Attribute-Based (ABAC) vs Relationship-Based (Zanzibar ReBAC)](Authorization-Models-Scopes-RBAC-ABAC-and-Zanzibar-ReBAC.md)
2. [Architectural Placement of Authorization: Why Coarse Scopes Belong at the Edge and Object-Level Checks Must Bind to the Database Query](Where-Authorization-Lives-Gateway-Middleware-vs-Domain-and-Query-Layer.md)
3. [OWASP API Security Top 10 (2023) Threat Modeling: Mapping API1 through API10 to Concrete Controls in a Python API Stack](OWASP-API-Security-Top-10-2023-Complete-Threat-Model.md)
4. [Preventing Broken Object Level Authorization (BOLA / IDOR, API1:2023): Tenant-Scoped Repository Types and PostgreSQL Row-Level Security (RLS)](Defeating-BOLA-API1-2023-Tenant-Scoped-Queries-and-PostgreSQL-RLS.md)
5. [Preventing Broken Object Property Level Authorization (BOPLA / Mass Assignment, API3:2023) with Strict Input/Output Pydantic Models](Defeating-BOPLA-and-Mass-Assignment-API3-2023-Strict-Input-Output-Schemas.md)
6. [Preventing Broken Function Level Authorization (BFLA, API5:2023): Deny-by-Default APIRouter Dependencies and Administrative Surface Isolation](Defeating-BFLA-API5-2023-Function-Level-Guards-and-Deny-By-Default-Routers.md)
7. [Defending Against Unrestricted Resource Consumption (API4:2023): Max Body Bytes, JSON Nesting Depth, Array Caps, and ReDoS-Safe Regexes](Unrestricted-Resource-Consumption-API4-2023-Payload-Depth-and-Limit-Guards.md)
8. [Defending Against Server-Side Request Forgery (SSRF, API7:2023): Why URL String Validation Fails Against DNS Rebinding and How to Build a Safe httpx Transport](Preventing-SSRF-API7-2023-DNS-Pinning-and-Private-CIDR-Socket-Guards.md)
9. [Unrestricted Access to Sensitive Business Flows (API6:2023): Scalping, Coupon Enumeration, Reservation Holds, and Behavioral Rate Limits](Protecting-Sensitive-Business-Flows-API6-2023-Anti-Automation-and-Bot-Defense.md)
10. [Improper Inventory Management (API9:2023) and Unsafe Consumption of Third-Party APIs (API10:2023): Validating Untrusted Upstream Responses](Improper-Inventory-Management-API9-2023-and-Unsafe-Consumption-of-APIs-API10-2023.md)
11. [CORS (WHATWG Fetch Standard) Internals: Simple vs Preflighted OPTIONS Requests, Access-Control-Allow-Credentials, and the Origin Reflection Vulnerability](CORS-Protocol-Internals-Preflight-Caching-and-Origin-Reflection-Exploits.md)
12. [When APIs Are Vulnerable to CSRF: Cookie Auth vs Bearer Headers, SameSite=Lax/Strict Limits, and Sec-Fetch-Site / Origin Header Enforcement](CSRF-in-Cookie-Authenticated-APIs-SameSite-Double-Submit-and-Fetch-Metadata.md)
13. [Preventing Injection in Data-Backed APIs: Why Parameterized Queries Do Not Protect Dynamic Column/Table Identifiers and How to Whitelist Them](SQL-Injection-NoSQL-Injection-and-Order-By-Identifier-Escaping.md)
14. [Security Response Headers for Pure JSON APIs vs HTML Documentation Endpoints (RFC 6797 HSTS, X-Content-Type-Options, CSP, and RFC 9116 security.txt)](Security-Headers-for-HTTP-APIs-HSTS-Nosniff-Cache-Control-and-Security-Txt.md)
15. [Defending Auth and Token Endpoints Against Brute Force and Credential Stuffing Without Enabling Account-Lockout Denial of Service](Credential-Stuffing-Brute-Force-Defense-and-Account-Lockout-Traps.md)
16. [Preventing Secret and PII Leakage in Logs, Trace Attributes, and Error Payloads: Pydantic SecretStr and structlog Redaction Processors](Structured-Log-Redaction-and-Preventing-PII-and-Token-Leaks.md)
