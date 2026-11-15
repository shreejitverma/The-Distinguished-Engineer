---
type: concept
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://www.rfc-editor.org/rfc/rfc9126.html"
  - "https://www.rfc-editor.org/rfc/rfc9396.html"
  - "https://www.rfc-editor.org/rfc/rfc9101.html"
  - "https://www.rfc-editor.org/rfc/rfc6749.html"
  - "https://www.rfc-editor.org/rfc/rfc7636.html"
  - "https://www.rfc-editor.org/rfc/rfc8414.html"
  - "https://www.rfc-editor.org/rfc/rfc8705.html"
  - "https://www.rfc-editor.org/rfc/rfc8707.html"
  - "https://www.rfc-editor.org/rfc/rfc9449.html"
  - "https://www.rfc-editor.org/rfc/rfc9700.html"
  - "https://openid.net/specs/fapi-security-profile-2_0.html"
  - "https://www.rfc-editor.org/rfc/rfc6750.html"
  - "https://www.rfc-editor.org/rfc/rfc9110.html"
  - "https://www.rfc-editor.org/rfc/rfc8446.html"
  - "https://www.rfc-editor.org/rfc/rfc8470.html"
  - "https://nginx.org/en/docs/http/ngx_http_core_module.html#large_client_header_buffers"
  - "https://httpd.apache.org/docs/2.4/mod/core.html#limitrequestline"
  - "https://cwe.mitre.org/data/definitions/598.html"
  - "https://owasp.org/API-Security/editions/2023/en/0x11-t10/"
  - "https://www.legislation.gov.uk/eur/2018/389/article/5/adopted"
  - "https://spring.io/security/cve-2026-41008"
---

# High-Assurance OAuth Flows: Pushed Authorization Requests (PAR, RFC 9126) and Fine-Grained Rich Authorization Requests (RAR, RFC 9396)

> [!summary] Claim
> Pushed Authorization Requests eliminate front-channel parameter tampering, query leakage, and URI length limits by moving authorization request payload submission to a direct, client-authenticated HTTPS endpoint that returns a short-lived reference URI, while Rich Authorization Requests replace flat, coarse scopes with typed JSON objects whose common fields combine as a product, which is what lets an authorization server show and bind the exact transaction a user consents to.

Part of [API Authentication and Identity](README.md) in [API Engineering](../README.md).

## TL;DR

- PAR replaces front-channel query string submission with a direct backchannel `POST` to the authorization server, issuing a single-use `request_uri` reference that the server MAY format as `urn:ietf:params:oauth:request_uri:<random>` `(RFC 9126 Section 2.2)`.
- RAR introduces the `authorization_details` parameter, structured as an array of typed JSON objects whose `actions`, `locations`, and `datatypes` combine as a product `(RFC 9396 Section 2.2)`.
- A `request_uri` MUST be bound to the client that pushed it `(RFC 9126 Section 2.2)`; an authorization endpoint that skips the `client_id` check, or lets a reference be reused, invites request URI swapping and replay `(RFC 9126 Sections 7.3 and 7.5)`.
- The lab runs on CPython 3.14.6 with FastAPI 0.162.0, Starlette 1.10.0, Pydantic 2.14.3, and PyJWT 2.15.1; no other interpreter in the matrix was run.
- Do not use RAR when authorization requirements map cleanly to static, coarse scopes, or PAR for internal microservices where direct token exchange or mTLS is already established.

## Mental model

```mermaid
sequenceDiagram
    autonumber
    participant Client as Client Application
    participant Browser as User Agent (Browser)
    participant AS_PAR as AS (PAR Endpoint)
    participant AS_Auth as AS (Authorize Endpoint)
    participant AS_Token as AS (Token Endpoint)
    participant RS as Resource Server

    Note over Client, AS_PAR: Step 1: Push Request via Backchannel TLS
    Client->>AS_PAR: POST /as/par (client_id, PKCE, redirect_uri,<br/>authorization_details=[{payment_initiation...}])
    Note over AS_PAR: Authenticate client<br/>Validate redirect_uri<br/>Validate RAR schema (RFC 9396 Sec 5)
    AS_PAR-->>Client: 201 Created {"request_uri": "urn:ietf:params:oauth:request_uri:...", "expires_in": 90}

    Note over Client, Browser: Step 2: Minimal Front-Channel Redirection
    Client->>Browser: Redirect 302 to /as/authorize?client_id=...&request_uri=urn:...
    Browser->>AS_Auth: GET /as/authorize?client_id=...&request_uri=urn:...
    Note over AS_Auth: Verify client_id matches request_uri binding<br/>Check single-use & expiry<br/>Prompt user consent with rich payment details
    AS_Auth-->>Browser: Redirect 302 to redirect_uri?code=authz_code_771
    Browser->>Client: Navigate to redirect_uri?code=authz_code_771

    Note over Client, AS_Token: Step 3: Backchannel Code & Downscope Exchange
    Client->>AS_Token: POST /as/token (code, code_verifier, [downscoped authorization_details])
    Note over AS_Token: Verify PKCE S256<br/>Verify RAR subsumption (RFC 9396 Sec 6.1)<br/>Mint JWT with authorization_details claim
    AS_Token-->>Client: 200 OK {"access_token": "ey...", "authorization_details": [...]}

    Note over Client, RS: Step 4: Fine-Grained API Invocation
    Client->>RS: POST /rs/payments (Bearer ey..., amount=150.00, iban=DE02...)
    Note over RS: Verify JWT signature & aud<br/>Enforce RAR constraints (amount <= limit, iban match)
    RS-->>Client: 201 Created {"payment_id": "pmt_489a", "status": "initiated"}
```

The client begins by opening an HTTPS connection directly to the pushed authorization request endpoint of the authorization server and authenticating itself there as it would at the token endpoint.
The full authorization payload, including PKCE challenges, redirect targets, and multi-dimensional `authorization_details` structures, is submitted in the body of an HTTP `POST` request.
The authorization server validates client credentials, redirect destinations, and schema conformance before storing the request payload and issuing a reference URI with a cryptographically random part and a short lifespan.
The client then directs the user agent to the authorization endpoint with a minimal query string consisting only of its client identifier and the reference URI.
Upon receiving the browser request, the authorization server ensures that the requesting client matches the identity bound to the reference URI, marks the reference as consumed to prevent replay, and displays a consent dialog detailing the exact transaction metadata.
Following resource owner approval, an authorization code is returned via redirection to the client, which exchanges the code and PKCE verifier at the token endpoint.
The token endpoint evaluates any optional downscoping requests against the granted permissions using type-specific comparison rules and issues an access token bearing the granted authorization details as a top-level claim.
Finally, the resource server inspects the access token claim to ensure that incoming API requests adhere strictly to the authorized actions, endpoints, amounts, and account identifiers.

## How it works

### The front-channel attack surface of classic OAuth 2.0

Under standard OAuth 2.0 `(RFC 6749 Section 4.1.1)`, an authorization transaction begins when the client directs the user agent to the authorization server's authorization endpoint via an HTTP `GET` request.
All parameters governing the transaction are encoded directly into the URI query string:

```http
GET /authorize?response_type=code
    &client_id=fintech_app_01
    &redirect_uri=https%3A%2F%2Fclient.example.org%2Fcb
    &scope=read%20write%20payments
    &state=c2VjdXJlX3JhbmRvbV9zdGF0ZQ
    &code_challenge=E9Melhoa2OwvFrGMTJguCH500MDU86298V43P_3yE6A
    &code_challenge_method=S256 HTTP/1.1
Host: as.example.com
```

This transmission model introduces four critical architectural vulnerabilities in high-assurance environments, the first three of which RFC 9126 lists as its motivation `(RFC 9126 Section 1)`:

1. Absence of request integrity and authenticity: Parameters transmitted across the front channel traverse untrusted networks, operating system inter-process communication boundaries, and browser address bars.
An adversary capable of modifying network traffic or operating malicious software on the client device can tamper with parameters such as `scope`, `redirect_uri`, or custom transaction parameters prior to submission.
2. Parameter leakage across intermediaries: Web browser address bars, browser history databases, local webview caches, and intermediate proxy or web server access logs routinely record complete request URIs.
Sensitive query parameters leak to external third parties through standard HTTP `Referer` headers whenever the authorization page loads third-party content such as remote fonts, analytics scripts, or stylesheets `(RFC 9700 Section 4.2.2, RFC 9126 Section 1)`.
3. Practical URI length limitations: Fine-grained authorization data or signed JSON Web Tokens `(JWT-Secured Authorization Request, JAR, RFC 9101)` make request URLs "quite large" `(RFC 9126 Section 1)`; one `payment_initiation` object alone produces a 673-byte request target (measured, see Numbers and limits), and several objects or a JAR Request Object multiply that.
Servers cap the request line: NGINX returns `414 Request-URI Too Large` when the line exceeds one `large_client_header_buffers` buffer (default `4 8k`), Apache's `LimitRequestLine` defaults to 8190 bytes, and RFC 9110 only recommends supporting URIs of at least 8000 octets `(RFC 9110 Section 4.1)`.
4. Authorization request parameter injection: Attackers can modify requested scopes or "swap the context of a payment transaction by changing scope values" before the request reaches the authorization server `(RFC 9126 Section 1)`.

### Pushed Authorization Requests (PAR, RFC 9126) architecture

Pushed Authorization Requests `(RFC 9126 Section 2)` decouple the transmission of authorization request parameters from the user agent's front-channel redirection.
Rather than transmitting request parameters through browser query strings, the client pushes the complete authorization payload directly to the authorization server over HTTPS (the PAR endpoint URL MUST use the `https` scheme), authenticating with whatever method it uses at the token endpoint; mutual TLS is one such method, not a requirement `(RFC 9126 Section 2)`.

The authorization server advertises support for PAR in its server metadata document `(RFC 8414 Section 2, RFC 9126 Section 5)`:

```json
{
  "issuer": "https://as.example.com",
  "pushed_authorization_request_endpoint": "https://as.example.com/as/par",
  "require_pushed_authorization_requests": true,
  "authorization_endpoint": "https://as.example.com/as/authorize",
  "token_endpoint": "https://as.example.com/as/token",
  "code_challenge_methods_supported": ["S256"]
}
```

The client transmits an HTTP `POST` request to the `pushed_authorization_request_endpoint` formatted as `application/x-www-form-urlencoded` with UTF-8 character encoding `(RFC 9126 Section 2.1)`:

```http
POST /as/par HTTP/1.1
Host: as.example.com
Authorization: Basic ZmludGVjaF9hcHBfMDE6c2VjdXJlX3NlY3JldF85OTQ=
Content-Type: application/x-www-form-urlencoded

response_type=code
&client_id=fintech_app_01
&redirect_uri=https%3A%2F%2Fclient.example.org%2Fcb
&scope=payments
&state=c2VjdXJlX3JhbmRvbV9zdGF0ZQ
&code_challenge=E9Melhoa2OwvFrGMTJguCH500MDU86298V43P_3yE6A
&code_challenge_method=S256
```

The authorization server processes the incoming pushed request through three mandatory steps `(RFC 9126 Section 2.1)`:
First, the server authenticates the client in the same way as at the token endpoint `(RFC 6749 Section 2.3)`, including HTTP Basic authentication, client assertion JWTs `(RFC 7523)`, or mutual-TLS client certificates `(RFC 8705 Section 2)`.
Public clients, which hold no credentials, can still push requests identified only by `client_id`; RFC 9126 does not itself mandate PKCE for them, but the lab's policy rejects a public client's push without `code_challenge_method=S256`, and FAPI 2.0 forbids public clients entirely `(FAPI 2.0 Security Profile Section 5.3.2.1)`.
Second, the server rejects the request if the `request_uri` parameter is present, because it MUST NOT be provided in a pushed request `(RFC 9126 Section 2.1)`; errors use the token endpoint error format `(RFC 9126 Section 2.3, RFC 6749 Section 5.2)`, which is HTTP `400 Bad Request` with `invalid_request` here, and the lab returns exactly that.
Third, the server validates the pushed request as it would an authorization request, for example the redirect URI against the client's registered URIs and the client's entitlement to the requested scopes; it MAY defer checks it cannot perform yet, but MUST then perform them at the authorization endpoint `(RFC 9126 Section 2.1)`.

Upon successful validation, the server persists the parsed request parameters within shared storage and returns a reference URI with HTTP `201` and an `application/json` body; the example below copies the RFC's own response headers `(RFC 9126 Section 2.2)`:

```http
HTTP/1.1 201 Created
Content-Type: application/json
Cache-Control: no-cache, no-store

{
  "request_uri": "urn:ietf:params:oauth:request_uri:6esc_11ACC5bwc014ltc14eY22c",
  "expires_in": 90
}
```

The `request_uri` value MUST contain a part generated by a cryptographically strong pseudorandom algorithm so that guessing is computationally infeasible `(RFC 9126 Section 2.2, RFC 6749 Section 10.10)`; RFC 6749 bounds the guessing probability at `2^(-128)` (MUST) and `2^(-160)` (SHOULD), and JAR asks for a random value of 128 bits or more `(RFC 9101 Section 10.2 (d))`.
The lifetime denoted by `expires_in` is at the server's discretion, "typically relatively short (e.g., between 5 and 600 seconds)" `(RFC 9126 Section 2.2)`; FAPI 2.0 requires `expires_in` below 600 seconds `(FAPI 2.0 Security Profile Section 5.3.2.2)` and warns that opening the link can take over 30 seconds on poor networks, and the lab issues 90 seconds.

### Wire protocols and URN referencing

The client uses the issued `request_uri` to direct the browser to the authorization endpoint:

```http
GET /as/authorize?client_id=fintech_app_01&request_uri=urn%3Aietf%3Aparams%3Aoauth%3Arequest_uri%3A6esc_11ACC5bwc014ltc14eY22c HTTP/1.1
Host: as.example.com
```

With the lab's 43-character `secrets.token_urlsafe(32)` reference, the request target is 138 bytes (measured, see Numbers and limits), independent of how large the pushed payload was, and no sensitive parameter appears in it.
When the authorization server receives this front-channel request, it retrieves the stored parameters using the reference.
The `request_uri` MUST be bound to the client that pushed it `(RFC 9126 Section 2.2, RFC 9101 Section 10.2 (d))`, so the server compares the `client_id` in the query string with the `client_id` stored with the pushed record.
If the identifiers do not match, the lab aborts with HTTP `400 Bad Request` and error code `invalid_request` rather than redirecting, because the request cannot be tied to a trusted redirect URI `(RFC 6749 Section 4.1.2.1)`.
Single use is a SHOULD, not a MUST: servers "SHOULD treat `request_uri` values as one-time use but MAY allow for duplicate requests due to a user reloading/refreshing their user agent", while the client, for its part, MUST only use a `request_uri` once, and an expired `request_uri` MUST be rejected `(RFC 9126 Section 4, Section 7.3)`.

### Rich Authorization Requests (RAR, RFC 9396) syntax and data model

OAuth 2.0 scopes `(RFC 6749 Section 3.3)` are space-delimited text strings designed for coarse-grained resource grouping (e.g., `openid profile email`).
They cannot express multi-dimensional access criteria, structured parameters, or transactional boundaries.
Engineering teams historically bypassed this limitation using brittle scope-string encoding schemes (such as `payment:150.00:EUR:DE02...`), which cause parsing failures, break authorization metadata registries, and prevent standardized consent rendering.

Rich Authorization Requests `(RFC 9396 Section 2)` resolves this structural deficit by defining the `authorization_details` parameter.
The parameter contains a JSON array of JSON objects, where each object defines authorization requirements for a specific API or resource type.
Every authorization details object MUST contain a `type` string (the field is REQUIRED) acting as a discriminator whose value determines the allowable contents of the object `(RFC 9396 Section 2)`:

```json
[
  {
    "type": "payment_initiation",
    "actions": ["initiate", "status"],
    "locations": ["https://api.example.com/payments"],
    "instructedAmount": {
      "currency": "EUR",
      "amount": "150.00"
    },
    "creditorAccount": {
      "iban": "DE02100100109307118603"
    },
    "remittanceInformationUnstructured": "Invoice 2026-991A"
  }
]
```

RFC 9396 Section 2.2 defines five common data fields as reusable, optional components; an API type is not required to use them:
- `locations`: An array of strings, typically URIs, identifying the location of the resource or resource server.
- `actions`: An array of strings denoting the kinds of actions to be taken at the resource (e.g., `read`, `write`, `initiate`, `cancel`).
- `datatypes`: An array of strings denoting the kinds of data requested (e.g., `balances`, `transactions`, `contacts`).
- `identifier`: A string identifying a specific resource available at the API (e.g., a specific bank account or subscription ID).
- `privileges`: An array of strings representing the types or levels of privilege requested at the resource.

### Cartesian product semantics vs. multi-object isolation

When common data fields are combined within a single authorization details object, the permissions requested are "the product of all the values" `(RFC 9396 Section 2.2)`:

$$\text{Permissions} = \text{actions} \times \text{locations} \times \text{datatypes}$$

Consider a request containing:
- `actions`: `["read", "write"]`
- `locations`: `["https://api.example.com/crm"]`
- `datatypes`: `["contacts", "billing"]`

Because the semantics represent a Cartesian product, this single object authorizes `write` operations against `billing` records as well as `read` operations against `contacts`.
If the client requires asymmetric privileges - such as read-only access to billing data alongside read-write access to contact records - it has to send separate objects within the array; the RFC phrases this as the client "can send multiple objects" for finer control `(RFC 9396 Section 2.2)`:

```json
[
  {
    "type": "customer_management",
    "locations": ["https://api.example.com/crm"],
    "actions": ["read", "write"],
    "datatypes": ["contacts"]
  },
  {
    "type": "customer_management",
    "locations": ["https://api.example.com/crm"],
    "actions": ["read"],
    "datatypes": ["billing"]
  }
]
```

RFC 9396 Section 5 defines strict validation rules for the authorization server.
The authorization server MUST refuse unknown types and non-conforming objects, abort processing, and respond with the error `invalid_authorization_details` if any object:
1. Contains an unknown authorization details `type` value.
2. Is of a known `type` but contains unknown fields.
3. Contains fields of the wrong type for the authorization details type.
4. Contains fields with invalid values (such as non-numeric amounts or malformed account strings).
5. Is missing fields required by the type.

Section 5 names the error code, not an HTTP status: at the authorization endpoint the error travels in the redirect `(RFC 6749 Section 4.1.2.1)`, while at the PAR and token endpoints it is a JSON error body with HTTP `400 Bad Request` `(RFC 9126 Section 2.3, RFC 6749 Section 5.2, RFC 9396 Section 8)`.

### Dynamic linking, downscoping, and privilege subsumption

In EU open banking, PSD2 strong customer authentication requires dynamic linking `(Commission Delegated Regulation (EU) 2018/389 Article 5)`.
Dynamic linking requires that the payer is made aware of the amount and payee, that the authentication code generated is specific to that amount and payee, and that any change to either invalidates the authentication code.
RAR provides the structured payload the authorization server needs to render the exact amount and payee in consent and to carry them into the token, without a separate out-of-band consent API.

At the token endpoint, for example when redeeming a code or a refresh token, clients can request a narrower access token by submitting an `authorization_details` parameter; the server checks the underlying grant and refuses with `invalid_authorization_details` if it does not cover the request `(RFC 9396 Section 6)`.
RFC 9396 states that "there is no standardized mechanism to compare two arbitrary authorization detail requests"; comparison is defined per type, and the RFC illustrates common patterns `(RFC 9396 Section 6.1)`:
- Set inclusion: requested `actions` and `locations` contained in the approved values (a subset, not necessarily a strict one).
- Value subsumption: one `actions` value can imply another (`write` covers `read`), and a `privileges` value such as `admin` can subsume read and write access, though other APIs can define privileges that do not subsume one another.
- Value limits: numeric constraints such as a maximum transaction amount are a type-specific rule; the lab's `payment_initiation` comparison requires the same currency and creditor IBAN and an amount no greater than the granted one.

If the requested details exceed the original authorization, the server refuses the request with HTTP `400 Bad Request` and error code `invalid_authorization_details` `(RFC 9396 Section 6, RFC 6749 Section 5.2)`.

### Access token claim structure and resource server enforcement

When issuing a JWT access token `(RFC 9068)`, the authorization server is RECOMMENDED to include the granted `authorization_details`, filtered to the token's audience, as a top-level claim `(RFC 9396 Section 9.1)`:

```json
{
  "iss": "https://as.example.com",
  "sub": "user_sandbox_9941",
  "aud": "https://api.example.com/payments",
  "client_id": "fintech_app_01",
  "exp": 1794960000,
  "iat": 1794956400,
  "jti": "8b4729cc-32e4-4370-8cf0-5796154d1296",
  "authorization_details": [
    {
      "type": "payment_initiation",
      "actions": ["initiate", "status"],
      "locations": ["https://api.example.com/payments"],
      "instructedAmount": {
        "currency": "EUR",
        "amount": "150.00"
      },
      "creditorAccount": {
        "iban": "DE02100100109307118603"
      }
    }
  ]
}
```

The resource server extracts and validates the token.
Before executing any business logic, the resource server verifies that:
1. The incoming HTTP request URL matches an entry in the `locations` array.
2. The requested HTTP method maps to an authorized element in `actions` (e.g., `POST` maps to `initiate`).
3. Payload parameters (e.g., transaction amount and beneficiary IBAN) match or remain within the boundaries defined in the token.
RFC 9396 defines no resource server error code; the closest standard is `insufficient_scope` with HTTP `403 Forbidden` `(RFC 6750 Section 3.1)`, and the lab returns `403` with a lab-specific code `insufficient_authorization_details`.
The lab's resource server checks `type`, the `initiate` action, currency, amount ceiling, and creditor IBAN; it does not check `locations`.

## Numbers and limits

| Parameter / Metric | Bound / Requirement | Source / Standard | Operational Implication (as of 2026-11) |
|---|---|---|---|
| PAR Endpoint Protocol | HTTPS `POST` only | `(RFC 9126 Section 2, Section 2.3)` | The endpoint URL MUST use `https`; a non-`POST` request gets `405 Method Not Allowed`. |
| PAR Content-Type | `application/x-www-form-urlencoded` | `(RFC 9126 Section 2)` | JSON bodies at `/as/par` are non-standard; form encoding is required. |
| PAR Response Status | `201 Created` | `(RFC 9126 Section 2.2)` | Clients checking exclusively for `200 OK` fail to parse successful pushed responses. |
| PAR Lifetime (`expires_in`) | e.g. 5 to 600 s; FAPI 2.0 < 600 s; lab 90 s | `(RFC 9126 Section 2.2, FAPI 2.0 Security Profile Section 5.3.2.2)` | Short windows limit replay and guessing; too short breaks users on slow devices (FAPI notes over 30 s is common). |
| Reference URI Prefix | `urn:ietf:params:oauth:request_uri:` (optional form) | `(RFC 9126 Section 2.2, Section 9.3)` | Registered URN sub-namespace; the server MAY use another format. |
| Reference Entropy | guess probability <= `2^(-128)` MUST, <= `2^(-160)` SHOULD | `(RFC 9126 Section 7.1, RFC 9101 Section 10.2 (d), RFC 6749 Section 10.10)` | The lab's `secrets.token_urlsafe(32)` draws 32 random bytes (256 bits), encoded as 43 characters. |
| Single-Use Invalidation | SHOULD be one-time; MAY allow reloads | `(RFC 9126 Section 4, Section 7.3)` | The lab rejects a replayed `request_uri` with `invalid_request`. |
| RAR Parameter Type | JSON array of JSON objects | `(RFC 9396 Section 2)` | Passing a single naked JSON object instead of an array fails schema validation. |
| RAR Error Code | `invalid_authorization_details` | `(RFC 9396 Section 5, Section 6, Section 8)` | Specific error code distinguishes RAR failures from general `invalid_request`. |
| Request-line limits without PAR | NGINX 8 KiB buffer (`414`), Apache `LimitRequestLine` 8190 bytes, RFC 9110 recommends >= 8000 octets | NGINX and Apache 2.4 docs, `(RFC 9110 Section 4.1)` | One `payment_initiation` object front-channel: 673-byte request target, measured below; several objects or a JAR Request Object approach these limits. |
| Request target with PAR | 138 bytes | Measured (command below) | Compact front-channel redirect, independent of the pushed payload size. |

The two request-target sizes come from `uv run --frozen python -I uri_len.py` (a script that URL-encodes the note's own `GET /as/authorize` parameters with `urllib.parse.urlencode`, once with the payment `authorization_details` example and once as `client_id` plus a `token_urlsafe(32)` `request_uri`), run on macOS 27.3 arm64 (`uname -r` = `27.3.0`) with CPython 3.14.6.

## Portability

The table below documents compatibility and behavior across runtime environments, server interfaces, and protocol revisions.
Verification ran the lab once, on macOS 27.3 (Darwin arm64) under CPython 3.14.6, and nowhere else; every row marked "Not run" records a leg that was not executed, because the lab matrix is paused (as of 2026-11).
No cross-version result is claimed.

| Environment / Component | Version / Release | Support Status | Verification & Upstream Notes |
|---|---|---|---|
| CPython 3.12 | `3.12.16` (pinned) | Not run | The lab declares `requires-python = ">=3.12,<3.16"` in `labs/pyproject.toml`, so 3.12 is in range, but no run is recorded. |
| CPython 3.13 | `3.13.18` (pinned) | Not run | In range per `requires-python`; no run recorded. |
| CPython 3.14 | `3.14.6` | Supported | Primary workspace interpreter (`labs/.python-version` = `3.14`); all 17 tests passed in verification (output below). |
| CPython 3.15 | `3.15.2` (pinned) | Not run | In range per `requires-python`; no run recorded. |
| CPython Free-Threaded | `3.15.2t` (pinned) | Not run | The lab's `OAuthStateStore` is plain dicts with no locking, so check-then-mark of `used` is not atomic under concurrent requests on any build; a real deployment needs an atomic compare-and-set in shared storage. |
| FastAPI | `0.162.0` | Supported | Lab defines `get_client_record` at module level so its string annotations resolve under `from __future__ import annotations`. |
| Starlette | `1.10.0` | Supported | Routing via `(starlette 1.10.0 starlette/routing.py, Route)`. |
| Pydantic / Core | `2.14.3` / `2.50.4` | Supported | Model validation with `extra="forbid"` enforces the unknown-field rule of RFC 9396 Section 5. |
| PyJWT | `2.15.1` | Supported | The lab encodes and decodes access tokens with HS256 only. |
| Uvicorn, Hypercorn, Granian | none | Not run | The lab drives the ASGI app in-process through `httpx.ASGITransport`, so no server is involved; the PAR logic is server-independent, but server behavior (request-line limits, TLS, HTTP/2) was not exercised. |
| HTTP/1.1 | RFC 9112 | Spec-agnostic | RFC 9126 defines form-encoded `POST` without tying it to an HTTP version; the lab runs in-process through `httpx.ASGITransport`, so no HTTP version is exercised. |
| HTTP/2 | RFC 9113 | Spec-agnostic | Same semantics; not exercised by the lab. |
| HTTP/3 over QUIC | RFC 9114 | Spec-agnostic | PAR is a non-idempotent `POST`; do not accept it as TLS 1.3 0-RTT early data, which is replayable `(RFC 8446 Section 8)`, and answer `425 Too Early` if it arrives that way `(RFC 8470 Section 5.2)`. |

## Worked example

The executable lab implementation provides a fully typed authorization server and resource server demonstrating PAR (RFC 9126) and RAR (RFC 9396).
The implementation is located at [implementation](../labs/src/api_labs/f14_api_authentication_and_identity/par_rar.py), and the complete test suite is located at [test](../labs/tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py).

Below is the core implementation managing PAR ingestion and RAR schema enforcement (lines 16 to 140 of `par_rar.py`, verbatim):

```python
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from decimal import Decimal
from typing import Annotated, Any, Literal, cast

import jwt
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

JWT_SECRET = "par-rar-test-secret-key-32-bytes-minimum!!"  # noqa: S105
JWT_ALGORITHM = "HS256"
ISSUER = "http://test"
DEFAULT_PAR_LIFETIME_SECONDS = 90
DEFAULT_CODE_LIFETIME_SECONDS = 60


class InvalidAuthorizationDetailsError(Exception):
    """Raised when authorization_details fails RFC 9396 Section 5 validation."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InstructedAmount(BaseModel):
    """Amount and currency specification for payment initiation (RFC 9396 Section 2)."""

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    amount: str = Field(pattern=r"^\d+(\.\d{1,4})?$")


class CreditorAccount(BaseModel):
    """Creditor bank account specification."""

    model_config = ConfigDict(extra="forbid")

    iban: str = Field(min_length=15, max_length=34, pattern=r"^[A-Z]{2}\d{2}[A-Z0-9]+$")
    bic: str | None = Field(default=None, min_length=8, max_length=11)


class PaymentInitiationDetail(BaseModel):
    """RFC 9396 authorization detail for payment initiation."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["payment_initiation"] = "payment_initiation"
    actions: list[str] = Field(default_factory=lambda: ["initiate", "status"])
    locations: list[str] = Field(default_factory=list)
    instructedAmount: InstructedAmount
    creditorAccount: CreditorAccount
    creditorName: str | None = None
    remittanceInformationUnstructured: str | None = None


class AccountInformationDetail(BaseModel):
    """RFC 9396 authorization detail for account information access."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["account_information"] = "account_information"
    actions: list[str] = Field(default_factory=lambda: ["list_accounts", "read_balances"])
    locations: list[str] = Field(default_factory=list)
    accounts: list[str] = Field(default_factory=list)


KNOWN_RAR_SCHEMAS: dict[str, type[BaseModel]] = {
    "payment_initiation": PaymentInitiationDetail,
    "account_information": AccountInformationDetail,
}


def parse_and_validate_rar(raw_details: object) -> list[dict[str, Any]]:
    """Parse and strictly validate authorization_details according to RFC 9396 Section 5.

    The AS MUST abort processing and return invalid_authorization_details if:
    - contains an unknown authorization details type value,
    - is an object of known type but containing unknown fields,
    - contains fields of the wrong type,
    - contains fields with invalid values,
    - is missing required fields.
    """
    items_raw: object
    if isinstance(raw_details, str):
        try:
            items_raw = cast("object", json.loads(raw_details))
        except json.JSONDecodeError as exc:
            raise InvalidAuthorizationDetailsError("JSON syntax error") from exc
    elif isinstance(raw_details, list):
        items_raw = cast("object", raw_details)
    else:
        raise InvalidAuthorizationDetailsError("authorization_details must be a JSON array")

    if not isinstance(items_raw, list):
        raise InvalidAuthorizationDetailsError("authorization_details must be a JSON array")

    items_list = cast("list[object]", items_raw)
    validated: list[dict[str, Any]] = []
    for elem in items_list:
        if not isinstance(elem, dict):
            raise InvalidAuthorizationDetailsError("elements must be JSON objects")
        item_dict = cast("dict[str, object]", elem)
        item_type = item_dict.get("type")
        if not item_type or not isinstance(item_type, str):
            raise InvalidAuthorizationDetailsError("missing or invalid type identifier")
        schema_cls = KNOWN_RAR_SCHEMAS.get(item_type)
        if schema_cls is None:
            raise InvalidAuthorizationDetailsError(
                f"unknown authorization details type: {item_type}"
            )
        try:
            model = schema_cls.model_validate(item_dict)
            validated.append(model.model_dump(exclude_none=True))
        except ValidationError as exc:
            raise InvalidAuthorizationDetailsError(f"schema validation failed: {exc}") from exc

    return validated
```

The test run executed in verification on macOS 27.3 (Darwin arm64) with CPython 3.14.6 (`uv run --frozen pytest tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py -v`) produced the following captured output:

```text
============================= test session starts ==============================
platform darwin -- Python 3.14.6, pytest-9.2.0, pluggy-1.7.0 -- /Users/shreejitverma/github/The-Distinguished-Engineer/build/api_notes/jobs/Pushed-Authorization-Requests-PAR-RFC-9126-and-Rich-Authorization-Requests-RAR-RFC-9396/verify/labs/.venv/bin/python
cachedir: .pytest_cache
hypothesis profile 'default'
rootdir: /Users/shreejitverma/github/The-Distinguished-Engineer/build/api_notes/jobs/Pushed-Authorization-Requests-PAR-RFC-9126-and-Rich-Authorization-Requests-RAR-RFC-9396/verify/labs
configfile: pyproject.toml
plugins: platformdirs-4.14.5, schemathesis-4.33.1, cov-7.1.0, hypothesis-6.170.14, inline-snapshot-0.36.1, Faker-40.44.0, locust-2.47.0, typeguard-4.6.0, anyio-4.16.0, asyncio-1.4.1.post1, respx-0.23.1
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collecting ... collected 17 items

tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_oauth_server_metadata_advertises_par_and_rar PASSED [  5%]
tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_par_confidential_client_success PASSED [ 11%]
tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_par_public_client_pkce_success PASSED [ 17%]
tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_par_forbids_request_uri_parameter PASSED [ 23%]
tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_par_client_authentication_failure PASSED [ 29%]
tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_par_unregistered_redirect_uri_rejected PASSED [ 35%]
tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_rar_strict_validation_rejects_unknown_type PASSED [ 41%]
tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_rar_strict_validation_rejects_unknown_fields PASSED [ 47%]
tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_rar_strict_validation_rejects_invalid_values PASSED [ 52%]
tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_authorize_rejects_direct_call_without_par PASSED [ 58%]
tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_authorize_rejects_swapped_client_id PASSED [ 64%]
tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_authorize_rejects_replayed_request_uri PASSED [ 70%]
tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_authorize_rejects_expired_request_uri PASSED [ 76%]
tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_end_to_end_par_rar_token_exchange_and_rs_enforcement PASSED [ 82%]
tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_token_downscoping_success_and_failure PASSED [ 88%]
tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_token_downscoping_rejects_privilege_escalation PASSED [ 94%]
tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py::test_token_downscoping_enforces_payment_amount_ceiling PASSED [100%]

============================== 17 passed in 0.80s ==============================
```

## Pitfalls

> [!warning] Pitfall 1: Request URI swapping via unbound authorization endpoint
> - Trigger: An authorization server retrieves cached pushed authorization parameters using only the incoming `request_uri` without verifying the accompanying `client_id` parameter at `/as/authorize`, violating the rule that a `request_uri` MUST be bound to the client that posted it `(RFC 9126 Section 2.2)`.
> - Symptom: An attacker who captures a `request_uri` substitutes it into a different authorization request, for example one under the attacker's client identifier, so the victim consents to a request the attacker chose `(RFC 9126 Section 7.5)`.
> - Fix: Match the query string's `client_id` against the client stored with the `request_uri` record and reject mismatches with `invalid_request`; clients additionally SHOULD use PKCE, a unique `state`, or the OIDC `nonce` so a swapped request fails at the client `(RFC 9126 Section 7.5)`.

> [!warning] Pitfall 2: Cartesian product explosion in combined common fields
> - Trigger: API designers combine multiple entries across `actions`, `locations`, and `datatypes` within a single `authorization_details` JSON object expecting independent field mappings.
> - Symptom: Inadvertent over-authorization occurs where a client requesting read access for contacts and write access for photos is unintentionally granted write permissions to contacts `(RFC 9396 Section 2.2)`.
> - Fix: Enforce the rule that multi-dimensional privileges within a single object represent a full Cartesian product; require clients to submit separate objects in the array whenever field relationships are asymmetric.

> [!warning] Pitfall 3: Inadvertent front-channel query string leakage via Referer and access logs
> - Trigger: Developers deploy RAR in production environments using standard front-channel `GET /as/authorize?authorization_details=...` without enabling PAR.
> - Symptom: Personally identifiable information, debtor bank accounts, transaction amounts, and clinical identifiers leak into web server access logs, reverse proxy logs, and external third-party referrers `(RFC 9396 Section 13, RFC 9126 Section 8, CWE-598)`.
> - Fix: Configure authorization servers to enforce `require_pushed_authorization_requests: true`, so the authorization endpoint refuses with `invalid_request` any request without a `request_uri` obtained from the PAR endpoint `(RFC 9126 Section 4, Section 5)`.

> [!warning] Pitfall 4: Open redirection at the PAR ingestion boundary
> - Trigger: The PAR endpoint accepts a `redirect_uri` that is not registered for the client, from a caller that has not authenticated.
> - Symptom: An attacker points the redirect at a site they control and obtains authorization codes when the user completes consent; RFC 9126 therefore allows new redirect URIs in pushed requests only from authenticated clients `(RFC 9126 Section 7.2)`.
> - Fix: Exactly match `redirect_uri` against the client's registered set at the PAR endpoint `(RFC 9126 Section 2.4)`; relax that only for authenticated clients and only within server-chosen restrictions such as a fixed prefix.

> [!warning] Pitfall 5: Lax authorization details validation permitting privilege smuggling
> - Trigger: Authorization server engineers implement permissive Pydantic or JSON Schema models that allow unknown extra fields or coerce arbitrary types.
> - Symptom: Attackers inject custom parameters into `authorization_details` that bypass authorization server consent screens but are subsequently interpreted and executed by vulnerable downstream resource servers, an instance of OWASP API3:2023 Broken Object Property Level Authorization; RFC 9396 requires refusing unknown fields `(RFC 9396 Section 5)` and sanitizing the data `(RFC 9396 Section 12)`.
> - Fix: Configure Pydantic models with `ConfigDict(extra="forbid")` and abort processing with error code `invalid_authorization_details` (HTTP `400 Bad Request` at the PAR and token endpoints) upon detecting any unexpected field or malformed type.

> [!warning] Pitfall 6: Premature request URI invalidation on browser reloads
> - Trigger: An authorization server marks `request_uri` as consumed immediately upon the first HTTP `GET /as/authorize` request rather than upon user submission of consent or code issuance.
> - Symptom: End users who refresh their browser, encounter transient network blips, or use software that preloads URLs receive `invalid_request` errors and broken authorization flows `(RFC 9126 Section 4)`.
> - Fix: Bind single-use invalidation to the point of authorization, as FAPI 2.0 recommends "at the point of authorization, not at the point of loading an authorization page" `(FAPI 2.0 Security Profile Section 5.3.2.2, NOTE 3)`; the lab issues the code in the same request, so its mark-on-first-`GET` coincides with code issuance.

> [!warning] Pitfall 7: Redirecting on an invalid `request_uri` (real advisory: CVE-2026-41008)
> - Trigger: The authorization endpoint validates `request_uri` too loosely and, on failure, still redirects to the `redirect_uri` taken from the unauthenticated query string.
> - Symptom: Spring Security Authorization Server (Spring Security 7.0.0 through 7.0.5) was reported with an open redirect because of "insufficient validation of the `request_uri` parameter" combined with an unvalidated `redirect_uri`; the advisory is rated Medium, was published 2026-06-09, and is fixed in 7.0.6 (OSS) and 7.0.5.1 (Enterprise Support) `(spring.io/security/cve-2026-41008)`.
> - Fix: Treat an unknown, expired, replayed, or client-mismatched `request_uri` as an error that is shown to the user, never redirected, because the request cannot be tied to a trusted redirect URI `(RFC 6749 Section 4.1.2.1)`; the lab returns `400 invalid_request` in each of those cases without a redirect, though the unknown-`request_uri` branch has no dedicated test.

## Security and robustness

### Threat modeling and boundary enforcement

High-assurance OAuth deployments operate under zero-trust assumptions where both the network perimeter and the end-user browser are treated as hostile execution environments.
The combination of PAR and RAR addresses key threat vectors outlined in the OAuth 2.0 Security Best Current Practice `(RFC 9700)` and the OWASP API Security Top 10 2023:

1. Mitigation of client spoofing and request tampering (OWASP API2:2023 Broken Authentication):
In standard OAuth, authorization request tampering allows attackers to manipulate parameter payloads prior to authentication.
PAR gives the request confidentiality and integrity in transit and lets the server authenticate the client before any user interaction `(RFC 9126 Section 1)`.
Because client authentication occurs on the direct connection to `/as/par`, unauthenticated attackers cannot obtain valid `request_uri` values for confidential client identifiers.
2. Neutralizing authorization code interception:
PAR is deployed alongside PKCE `(RFC 7636)`.
The `code_challenge` and `code_challenge_method` parameters are lodged over the direct HTTPS connection during the PAR push.
Even if an adversary intercepts the subsequent front-channel authorization code from the redirect URI, they cannot exchange the code at `/as/token` without the corresponding `code_verifier`.
3. Eliminating parameter tampering in transit:
Because the front-channel URL contains only `client_id` and `request_uri`, malicious browser extensions or proxy intermediaries cannot alter requested scopes, transaction limits, or recipient accounts; what remains is swapping the whole `request_uri`, which client binding and PKCE address `(RFC 9126 Section 7.5)`.
4. Object-level constraints (OWASP API1:2023 Broken Object Level Authorization):
RAR's `identifier`, `locations`, and type-specific fields such as `creditorAccount` let the resource server check the specific object a call touches, not just a coarse scope.

### Defense in depth with sender-constrained tokens

FAPI 2.0 makes PAR, PKCE with `S256`, and confidential-client authentication (MTLS or `private_key_jwt`) mandatory and requires sender-constrained access tokens via MTLS or DPoP `(FAPI 2.0 Security Profile Sections 5.3.2.1 and 5.3.2.2)`; RAR is recommended, not mandatory, when `scope` is not expressive enough `(FAPI 2.0 Security Profile Section 5.3.2.2, NOTE 5)`:
- Mutual-TLS (mTLS, RFC 8705): Clients authenticate to the PAR endpoint using X.509 client certificates `(RFC 8705 Section 2)`.
The resulting access tokens are cryptographically bound to the client certificate's SHA-256 thumbprint via the `cnf` claim's `x5t#S256` member `(RFC 8705 Section 3.1)`.
- Demonstrating Proof-of-Possession (DPoP, RFC 9449): Clients generate an asymmetric key pair, bind the authorization code to it at the PAR endpoint through the `dpop_jkt` parameter or a `DPoP` header `(RFC 9449 Section 10.1)`, and present signed proof JWTs to the token endpoint and the resource server.
This prevents stolen tokens from being replayed by unauthorized intermediaries.

## The principal's view

From the perspective of a principal or staff engineer, adopting PAR and RAR represents an architectural transition from coarse, identity-centric access control to fine-grained, transaction-centric authorization.
In early-stage architectures, simple OAuth scopes (`read`, `write`, `admin`) suffice because service boundaries are monolithic and operations lack external regulatory scrutiny.
However, as organizations scale into open banking (e.g., UK Open Banking, Open Finance Brazil, EU PSD2/PSD3), healthcare interoperability (HL7 FHIR), or cross-organization data sharing, coarse scopes collapse under operational weight.

The organizational trade-offs must be evaluated deliberately:
- State management overhead: Without PAR, the authorization request travels in the URL and the server needs no storage until the user arrives.
Enforcing PAR requires the authorization server to maintain an ephemeral, shared store (such as Redis or Valkey) for pushed requests with TTLs matching `expires_in`, plus an atomic consume operation if single use is enforced across nodes.
At high flow rates this is a stateful dependency on the login path that must be provisioned for high availability, automatic expiration, and low latency; its sizing is a capacity-planning exercise, not a number this note measured.
- Schema governance and versioning: Introducing RAR turns the authorization server into a schema registry.
Every new business domain requiring fine-grained authorization details introduces new type definitions that must be coordinated across client SDKs, identity platforms, and downstream microservices `(RFC 9396 Section 11.1)`.
Teams must establish rigid schema governance, semantic versioning, and machine-readable schema validation (e.g., JSON Schema draft 2020-12, which RFC 9396 Section 11.3 allows as a type identifier) to prevent breaking changes across federated ecosystems.
- Migration strategy: Staff engineers should avoid big-bang migrations.
RFC 9396 Section 3.1 allows combining `scope` and `authorization_details` within the same request precisely to allow incremental migration, while recommending that a given API use only one form.
Identity teams should enable PAR globally first (`require_pushed_authorization_requests: false` transitioning to `true` per client through the client metadata of RFC 9126 Section 6), securing the wire transport without altering existing scope semantics.
Once PAR is established, high-risk transactional APIs (such as payment initiation or data exports) should be migrated incrementally from legacy scopes to typed RAR models.

## Interview questions

> [!question] L5: What primary security and operational vulnerabilities in classic OAuth 2.0 does Pushed Authorization Requests (PAR, RFC 9126) resolve?
>
> > [!success]- Answer
> > Classic OAuth 2.0 transmits authorization request parameters in the browser URI query string during front-channel redirection.
> > This exposes sensitive transaction metadata and personal data to browser histories, web server logs, and HTTP `Referer` headers.
> > Furthermore, front-channel query strings lack integrity protection and can become large enough to cause request processing errors when carrying fine-grained authorization data `(RFC 9126 Section 1)`.
> > PAR resolves these vulnerabilities by transmitting the parameters directly to a client-authenticated HTTPS endpoint and returning a short-lived `request_uri` that servers should treat as one-time use.
> >
> > Follow-up: Why must the authorization endpoint verify that the `client_id` in the query string matches the client bound to the `request_uri`?

> [!question] L6: Explain the Cartesian product rule defined in RFC 9396 Section 2.2 for Rich Authorization Requests, and how a client must structure its payload to request asymmetric permissions.
>
> > [!success]- Answer
> > When multiple entries are provided for common data fields like `actions`, `locations`, and `datatypes` within a single `authorization_details` object, the requested rights are the product of all the values.
> > For example, an object specifying actions `["read", "write"]` and datatypes `["contacts", "billing"]` authorizes write access to billing data as well as contacts.
> > To request asymmetric permissions (such as read-only access to billing data and read-write access to contacts), the client must partition the requirements into separate JSON objects within the array.
> > The authorization server processes each object independently, ensuring permissions do not bleed across boundaries.
> >
> > Follow-up: What error code must the authorization server return if an authorization details object contains unrecognized extra fields?

> [!question] L6 (Predict the output): Analyze the following token request and state whether it succeeds or fails under RFC 9396 rules.
>
> ```http
> POST /as/token HTTP/1.1
> Host: as.example.com
> Content-Type: application/x-www-form-urlencoded
>
> grant_type=authorization_code
> &code=SplxlOBeZQQYbYS6WxSbIA
> &redirect_uri=https%3A%2F%2Fclient.example.org%2Fcb
> &client_id=fintech_app_01
> &code_verifier=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk
> &authorization_details=%5B%7B%22type%22%3A%22payment_initiation%22%2C%22actions%22%3A%5B%22initiate%22%5D%2C%22instructedAmount%22%3A%7B%22currency%22%3A%22EUR%22%2C%22amount%22%3A%22300.00%22%7D%2C%22creditorAccount%22%3A%7B%22iban%22%3A%22DE02100100109307118603%22%7D%7D%5D
> ```
>
> Assume client authentication succeeds and the user originally authorized a `payment_initiation` of `200.00 EUR` to the same creditor IBAN, with a type whose comparison rule treats `instructedAmount` as a ceiling.
>
> > [!success]- Answer
> > The token request fails with HTTP `400 Bad Request` and error `invalid_authorization_details`.
> > RFC 9396 Section 6 requires the authorization server to check whether the underlying grant allows the requested authorization details and otherwise refuse with `invalid_authorization_details`; Section 6.1 leaves the comparison rule to the type definition.
> > Under this type's rule, `300.00 EUR` exceeds the granted `200.00 EUR`, so the request asks for more than the grant and is refused.
> > The lab's `test_token_downscoping_enforces_payment_amount_ceiling` sends this `authorization_details` value against a 200.00 EUR grant and gets `400` with `"error_description": "requested details exceed granted rights"`.
> >
> > Follow-up: How would the server respond if the client requested `150.00 EUR` instead, and what does the lab test show for that case?

> [!question] L6 (Debug this API): An engineering team observes that single-page applications calling their new PAR endpoint receive HTTP `400 Bad Request` errors with `{"error": "invalid_request"}`.
>
> Reviewing the incoming HTTP request payload reveals:
>
> ```http
> POST /as/par HTTP/1.1
> Host: as.example.com
> Content-Type: application/x-www-form-urlencoded
>
> response_type=code
> &client_id=spa_portal_public
> &redirect_uri=https%3A%2F%2Fportal.example.com%2Fcallback
> &scope=openid%20profile
> &state=9a8b7c6d5e
> &code_challenge=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk
> &code_challenge_method=plain
> ```
>
> What is the root cause of this failure in high-assurance environments?
>
> > [!success]- Answer
> > The failure is caused by using `code_challenge_method=plain` rather than `S256`, on a server that only accepts `S256`.
> > With `plain` the challenge equals the verifier, so anyone who observes the request on the device or in transit learns the verifier; RFC 7636 says `plain` SHOULD NOT be used `(RFC 7636 Section 7.2)`, RFC 9700 says clients SHOULD use a method that does not expose the verifier and that `S256` is currently the only such method `(RFC 9700 Section 2.1.1)`, and FAPI 2.0 requires `S256` `(FAPI 2.0 Security Profile Section 5.3.2.2)`.
> > The lab's PAR endpoint returns exactly this `400 invalid_request` for a public client without `S256`; note that FAPI 2.0 would reject this client earlier, because it admits only confidential clients.
> > Updating the client to compute `code_challenge = BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))` with `code_challenge_method=S256` resolves the issue `(RFC 7636 Section 4.2)`.
> >
> > Follow-up: Why is `code_challenge_method=plain` supported in the original RFC 7636 specification?

> [!question] L7: Design an authorization architecture for an international open banking gateway that enforces strict dynamic linking and non-repudiation across multi-currency cross-border payments.
>
> > [!success]- Answer
> > The architecture mandates PAR (RFC 9126) combined with mTLS client authentication (RFC 8705) and Rich Authorization Requests (RFC 9396).
> > Non-repudiation of the request itself needs a signed JAR Request Object pushed through PAR, because PAR alone gives integrity and confidentiality but not a signature the client cannot later deny `(RFC 9126 Section 1, Section 3)`.
> > The client establishes mutual TLS with the PAR endpoint, presenting an eIDAS or equivalent regulatory X.509 certificate, and pushes a typed `payment_initiation` RAR payload containing currency, amount, debtor account, and creditor IBAN.
> > The authorization server validates the client certificate, verifies schemas, renders a consent screen displaying the amount and payee as dynamic linking requires, and issues an authorization code.
> > At the token endpoint, the server issues an access token containing the approved `authorization_details` claim bound to the client's certificate via `cnf.x5t#S256`.
> > Downstream resource servers verify the mTLS channel against the token certificate thumbprint and enforce that payment execution matches the token claims exactly.
> >
> > Follow-up: How would you prevent replay attacks if the resource server is exposed across multiple data centers?

> [!question] L6: How do Rich Authorization Requests (RFC 9396) interact with the OAuth 2.0 Resource Indicators specification (RFC 8707)?
>
> > [!success]- Answer
> > The `resource` parameter defined in RFC 8707 specifies the target resource server URI for coarse-grained scopes.
> > RFC 9396 Section 3.2 explicitly clarifies that the `resource` parameter has no effect on how the authorization server processes `authorization_details`.
> > Instead, RAR provides its own native mechanism for audience and resource restriction via the `locations` common field inside each detail object.
> > When minting access tokens, the authorization server can split or filter the `authorization_details` array such that each target resource server only receives the subset of authorization details matching its own location `(RFC 9396 Section 9.1)`.
> >
> > Follow-up: Why is audience-filtering of authorization details critical in multi-tenant resource server environments?

> [!question] L5: What HTTP status code and response body must the PAR endpoint return upon successful request validation according to RFC 9126, and what about caching?
>
> > [!success]- Answer
> > The authorization server MUST respond with HTTP status code `201` `(RFC 9126 Section 2.2)`.
> > The response body uses the `application/json` media type and contains `request_uri` and `expires_in` as top-level members.
> > RFC 9126 has no normative cache header rule, but its examples send `Cache-Control: no-cache, no-store`, and RFC 6749 Section 5.1 requires `Cache-Control: no-store` and `Pragma: no-cache` on responses carrying sensitive information, which is what the lab sends.
> >
> > Follow-up: What format does RFC 9126 suggest for the `request_uri` value?

> [!question] L7: Under what operational conditions can an authorization server allow multiple front-channel requests using the same `request_uri` without violating RFC 9126 security guarantees?
>
> > [!success]- Answer
> > RFC 9126 Section 4 specifies that authorization servers SHOULD treat `request_uri` values as one-time use but MAY allow duplicate requests caused by a user reloading or refreshing the user agent.
> > If an end-user refreshes the consent page during an active session, terminating the flow immediately degrades user experience.
> > To maintain security, the server associates the `request_uri` with an active browser session identifier or encrypted interaction cookie.
> > As long as subsequent `GET` requests originate from the same authenticated browser session and an authorization code has not yet been minted, the server re-renders the pending consent screen.
> > The moment an authorization code is issued or the `expires_in` TTL elapses, the `request_uri` is irrevocably purged from the cache, matching FAPI 2.0's advice to enforce one-time use at the point of authorization `(FAPI 2.0 Security Profile Section 5.3.2.2, NOTE 3)`.
> >
> > Follow-up: How does distributed cache replication latency affect single-use enforcement in multi-region deployments?

## Related

Prerequisites: [[Service-to-Service-Identity-Client-Credentials-and-Token-Exchange-RFC-8693]]

Next: [[HTTP-Message-Signatures-RFC-9421-and-Content-Digests-RFC-9530]]

See also: [[OAuth-2-0-to-OAuth-2-1-and-RFC-9700-Security-BCP]], [[Sender-Constrained-Tokens-DPoP-RFC-9449-and-mTLS-Binding-RFC-8705]], [[JWT-JWS-JWK-Structure-and-RFC-9068-Access-Token-Profile]], [[Selective-Disclosure-JWTs-SD-JWT-RFC-9901]], [[OAuth-2-0-for-Browser-Based-Applications-BFF-and-Cookies-RFC-10017]]

- Lab: `labs/tests/f14_api_authentication_and_identity/test_pushed_authorization_requests_par_rfc_9126_and_rich_authorization_requests_rar_rfc_9396.py`

## Further reading

- [RFC 9126: OAuth 2.0 Pushed Authorization Requests](https://www.rfc-editor.org/rfc/rfc9126.html)
  The authoritative IETF standard defining the pushed authorization request endpoint, reference URI lifecycle, and error handling.
- [RFC 9396: OAuth 2.0 Rich Authorization Requests](https://www.rfc-editor.org/rfc/rfc9396.html)
  The core specification establishing the `authorization_details` parameter syntax, common data fields, product semantics, and comparison of authorization details.
- [RFC 9101: The OAuth 2.0 Authorization Framework: JWT-Secured Authorization Request (JAR)](https://www.rfc-editor.org/rfc/rfc9101.html)
  Defines the signed and optionally encrypted Request Object that PAR can carry, and the request URI entropy and binding rules PAR inherits.
- [RFC 9700: Best Current Practice for OAuth 2.0 Security](https://www.rfc-editor.org/rfc/rfc9700.html)
  Security analysis covering Referer leakage, authorization code injection, PKCE downgrade, and sender-constrained tokens.
- [OpenID FAPI Security Profile 2.0](https://openid.net/specs/fapi-security-profile-2_0.html)
  The Final (February 2025) open banking profile mandating PAR, PKCE `S256`, confidential clients, and sender-constrained access tokens.
- [Spring Security advisory CVE-2026-41008](https://spring.io/security/cve-2026-41008)
  The vendor advisory for the open redirect through `request_uri` handling cited in Pitfall 7.
