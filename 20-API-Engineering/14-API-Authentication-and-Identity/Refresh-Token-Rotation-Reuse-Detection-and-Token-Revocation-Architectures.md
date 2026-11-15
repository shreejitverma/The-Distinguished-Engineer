---
type: pattern
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://www.rfc-editor.org/rfc/rfc9700.html"
  - "https://www.rfc-editor.org/rfc/rfc7662.html"
  - "https://www.rfc-editor.org/rfc/rfc9701.html"
  - "https://www.rfc-editor.org/rfc/rfc7009.html"
  - "https://www.rfc-editor.org/rfc/rfc6749.html"
  - "https://www.rfc-editor.org/rfc/rfc9068.html"
  - "https://curity.io/resources/learn/phantom-token-pattern/"
  - "https://github.com/lepture/authlib/blob/v1.9.5/authlib/oauth2/rfc7009/revocation.py"
  - "https://github.com/lepture/authlib/blob/v1.9.5/authlib/oauth2/rfc7662/introspection.py"
  - "https://github.com/lepture/authlib/blob/v1.9.5/authlib/oauth2/rfc6749/token_endpoint.py"
  - "https://developer.okta.com/docs/guides/refresh-tokens/main/"
  - "https://auth0.com/docs/secure/tokens/refresh-tokens/configure-refresh-token-rotation"
  - "https://www.cve.org/CVERecord?id=CVE-2026-1035"
  - "https://access.redhat.com/security/cve/CVE-2026-1035"
  - "https://www.cve.org/CVERecord?id=CVE-2026-6647"
  - "https://security.docs.wso2.com/en/latest/security-announcements/security-advisories/2026/WSO2-2026-5387/"
  - "https://api-security.owasp.org/editions/2023/en/0x11-t10"
  - "https://openid.net/specs/openid-caep-1_0.html"
  - "https://asgi.readthedocs.io/en/latest/specs/www.html"
  - "https://www.rfc-editor.org/rfc/rfc9113.html"
  - "https://www.rfc-editor.org/rfc/rfc9114.html"
  - "https://docs.python.org/3/library/enum.html"
---

# Access and Refresh Token Lifecycles: Refresh Token Rotation with Reuse Family Revocation (RFC 9700), Token Introspection (RFC 7662), and Phantom Tokens

> [!summary] Claim
> Refresh Token Rotation detects refresh token replay on public clients by invalidating each refresh token on use and revoking the active token (in practice the whole grant family) when an invalidated one is replayed, while the Phantom Token Pattern keeps claims off the client by having the API gateway swap an opaque reference token for an authorization-server-signed JWT, with revocation latency bounded by the gateway cache.

Part of [API Authentication and Identity](README.md) in [API Engineering](../README.md).

## TL;DR

- The authorization server rotates the refresh token on every exchange and retains the lineage; RFC 9700 Section 4.14.2 requires revoking the active refresh token when an invalidated one is replayed, and the lab (like Okta and Auth0) revokes the whole family.
- RFC 7009 Section 2.2 answers `200` for an invalid or already-revoked token because the client cannot act on such an error, but client-authentication failures, a token issued to another client, `unsupported_token_type`, and `503` are still errors.
- Single-page applications that fire concurrent refreshes on bootstrap trigger false-positive family revocation unless the server tolerates the immediate predecessor for a short window; RFC 9700 defines no such window, Okta defaults it to 30 s (range 0 to 60 s) and Auth0 leaves it off (as of 2026-11).
- The lab is pure Python (`enum.StrEnum` needs CPython 3.11+, plus PyJWT 2.15.1 and Starlette 1.10.0) and passes on CPython 3.14.6; other interpreters were not run (the matrix is paused), and `labs/pyproject.toml` allows CPython 3.12 to 3.15.
- Do not use standalone Refresh Token Rotation when sender-constraining via DPoP (RFC 9449) or mTLS (RFC 8705) is viable, as cryptographic proof-of-possession eliminates bearer token replay before network ingestion.

## Mental model

The token lifecycle spans two distinct architectural zones: an untrusted public zone governed by opaque reference tokens and rotating refresh credentials, and a trusted internal mesh powered by short-lived, cryptographically signed JSON Web Tokens (JWTs).

```mermaid
sequenceDiagram
    autonumber
    participant Client as Public Client (SPA / Mobile)
    participant Gateway as API Gateway (Edge Reverse Proxy)
    participant AS as Authorization Server (IdP)
    participant RS as Downstream Resource Server

    Note over Client,AS: 1. Grant Issuance and Phantom Token Access
    Client->>AS: POST /token (grant_type=authorization_code)
    AS-->>Client: 200 OK (opaque access_token at_0, opaque refresh_token rt_0)
    Client->>Gateway: GET /api/v1/orders (Authorization: Bearer at_0)
    Gateway->>Gateway: Check local edge cache for at_0
    alt Cache Miss
        Gateway->>AS: POST /introspect (token=at_0, Accept: application/token-introspection+jwt)
        AS-->>Gateway: 200 OK (RFC 9701 signed JWT introspection or RFC 7662 JSON)
        Gateway->>Gateway: Cache AS-signed RFC 9068 JWT (typ: at+jwt) until exp or purge
    end
    Gateway->>RS: GET /api/v1/orders (Authorization: Bearer internal_jwt)
    RS-->>Gateway: 200 OK (Resource payload)
    Gateway-->>Client: 200 OK (Resource payload)

    Note over Client,AS: 2. Refresh Token Rotation (RTR) - Success
    Client->>AS: POST /token (grant_type=refresh_token, refresh_token=rt_0)
    AS->>AS: Verify rt_0 is ACTIVE in Family F1<br/>Mark rt_0 as ROTATED (start 10s grace window)<br/>Mint rt_1 (gen=1) and at_1
    AS-->>Client: 200 OK (access_token at_1, refresh_token rt_1)

    Note over Client,AS: 3. Token Theft and Reuse Detection (RFC 9700)
    actor Attacker as Malicious Actor (Exfiltrated rt_0)
    Attacker->>AS: POST /token (grant_type=refresh_token, refresh_token=rt_0)
    AS->>AS: Detect rt_0 is ROTATED and outside grace period!<br/>Trigger REUSE DETECTION on Family F1<br/>Revoke rt_0, rt_1, at_0, at_1 (Family F1 status = REVOKED)<br/>Emit security audit alert (TOKEN_REUSE_DETECTED)
    AS-->>Attacker: 400 Bad Request (error: invalid_grant)
    Client->>AS: POST /token (grant_type=refresh_token, refresh_token=rt_1)
    AS-->>Client: 400 Bad Request (error: invalid_grant - Family Revoked)
```

In this sequence, the client never receives internal claims, private scopes, or cryptographic signing keys.
The client holds only high-entropy opaque strings.
When the client calls downstream APIs, the API Gateway intercepts the opaque bearer token and performs token introspection against the Authorization Server.
The Gateway caches the RFC 9068 JWT that the Authorization Server signed for that token and injects it into the upstream request heading toward internal microservices; the Gateway itself holds no signing key.
When the client refreshes its access token, the Authorization Server executes Refresh Token Rotation: it invalidates `rt_0`, advances the lineage counter to generation 1, and returns `rt_1`.
If an adversary exfiltrates `rt_0` and replays it after the rotation grace window, the Authorization Server identifies that an already-rotated token has been submitted.
Because the server cannot determine whether the attacker or the legitimate client holds `rt_1` (RFC 9700 Section 4.14.2), it revokes the active refresh token; the lab goes further and terminates the entire family, invalidating all associated access and refresh tokens and recording a security alert.

## How it works

### Refresh Token Rotation and Reuse Family Revocation (RFC 9700)

Under OAuth 2.0 (RFC 6749 Section 1.5), refresh tokens are credentials used to obtain new access tokens without repeated user interaction, and they typically outlive the access tokens they mint.
On public clients such as Single-Page Applications (SPAs) and native mobile apps, client secrets cannot be securely stored.
Consequently, an exfiltrated bearer refresh token grants an adversary indefinite access.
RFC 9700 Section 2.2.2 states: "Refresh tokens for public clients MUST be sender-constrained or use refresh token rotation as described in Section 4.14."
RFC 9700 Section 4.14.2 specifies the mechanics of Refresh Token Rotation (RTR): the authorization server issues a new refresh token with every access token refresh response, invalidates the previous refresh token, and retains "information about the relationship".
Its implementation note allows the grant to be encoded into the refresh token itself (integrity-protected, for example signed) so the server can find "all refresh tokens that need to be revoked".

The authorization server maintains grant lineage by organizing tokens into families.
Each family corresponds to a distinct authorization grant binding a specific user identity, client identifier, and consented scope set.
A token record maintains metadata including its unique identifier `token_id`, `family_id`, `generation` counter, `parent_token_id`, lifecycle state `status` (`ACTIVE`, `ROTATED`, `REVOKED`), `issued_at`, `expires_at`, and `rotated_at`.

When a client submits a refresh request via `POST /token` with `grant_type=refresh_token` per RFC 6749 Section 6:
1. The server resolves the token record and verifies that `record.client_id` matches the authenticated or identified caller.
2. The server verifies that `record.expires_at > now` and that the parent family is not marked `is_revoked`.
3. If `record.status == TokenStatus.ACTIVE`:
   The server transitions `record.status` to `TokenStatus.ROTATED` and records `record.rotated_at = now`.
   The server generates a new cryptographically secure refresh token $RT_{n+1}$ with `generation = record.generation + 1` and `parent_token_id = record.token_id`.
   The server updates the family active tip pointer `family.active_refresh_token_id = RT_{n+1}`.
   The server issues a new access token $AT_{n+1}$ and stores the replacement pair references on the rotated record.
4. If `record.status == TokenStatus.ROTATED`:
   The server computes `elapsed = now - record.rotated_at`.
   If `elapsed <= grace_period_seconds` and the token is the immediate predecessor of the active tip (`family.active_refresh_token_id == record.replacement_token_id`), the request is recognized as a concurrent network retry.
   The server returns the cached replacement pair ($AT_{n+1}$, $RT_{n+1}$) without advancing the lineage or modifying family state.
   The trade-off: inside the window, a thief replaying the predecessor receives the live pair and is not detected; Auth0 likewise exempts only "the previous token" from breach detection during its reuse interval but issues a new refresh token instead of the cached one.
   If `elapsed > grace_period_seconds` or the presented token is an older ancestor ($k < n$), a breach has occurred.
   RFC 9700 Section 4.14.2 requires the authorization server to revoke the active refresh token; the lab revokes every refresh and access token in the family, as Okta (most recent refresh token plus all access tokens since authentication) and Auth0 (every refresh token issued since the reused one) do (as of 2026-11).
   The lab records a `SecurityAlert` with the `family_id`, the reused `token_id`, the user, and the client; a production server would add client IP and device context.
   The server returns HTTP `400 Bad Request` with `{"error": "invalid_grant"}` per RFC 6749 Section 5.2.

### OAuth 2.0 Token Revocation (RFC 7009)

RFC 7009 defines a standardized mechanism enabling clients to notify the authorization server that a previously obtained grant or token is no longer needed.
The client transmits an HTTP `POST` request to the revocation endpoint:

```http
POST /revoke HTTP/1.1
Host: auth.example.com
Content-Type: application/x-www-form-urlencoded
Authorization: Basic czZCaGRSa3F0MzpnWDFmQmF0M2JW

token=rt_9B2xZ1qLkP4vM8...&token_type_hint=refresh_token
```

The parameter `token` is REQUIRED.
The parameter `token_type_hint` is OPTIONAL and defines the values `access_token` (RFC 6749 Section 1.4) and `refresh_token` (RFC 6749 Section 1.5); if the server cannot locate the token using the hint, it "MUST extend its search across all of its supported token types" (RFC 7009 Section 2.1).
The server first validates client credentials (for a confidential client) and then verifies that the token was issued to the requesting client; if either check fails, "the request is refused" with an error (RFC 7009 Section 2.1).
Otherwise the server invalidates the token immediately.
Per RFC 7009 Section 2.1, revoking a refresh token SHOULD also invalidate all access tokens based on the same authorization grant, and revoking an access token MAY revoke the respective refresh token.

Per RFC 7009 Section 2.2, the authorization server responds with HTTP status code `200` if the token has been revoked successfully or if the client submitted an invalid token, because "the client cannot handle such an error in a reasonable way" and the purpose of the request "is already achieved".
Section 2.2 also says an invalid `token_type_hint` value "is ignored by the authorization server and does not influence the revocation response".
The `200` is not unconditional: errors follow RFC 6749 Section 5.2, `unsupported_token_type` reports a token type the server cannot revoke, and a `503` tells the client to assume the token still exists and retry, optionally after `Retry-After` (RFC 7009 Section 2.2.1).
The RFC does not justify the `200` as anti-enumeration: Section 5 argues that guessing requires the client's credentials and a token that belongs to that client, and an attacker who has both gains more by using the token than by revoking it; the countermeasure it mandates is DoS protection on the endpoint.
`RevocationEndpoint.create_endpoint_response` (authlib 1.9.5 authlib/oauth2/rfc7009/revocation.py, RevocationEndpoint.create_endpoint_response) calls `authenticate_endpoint_client`, then `authenticate_token` (which runs `check_params` and `query_token`), calls `revoke_token` only when a token was found, and returns `200, {}, default_json_headers`.
`authenticate_token` raises `InvalidGrantError` for a token issued to another client, and `check_params` raises `UnsupportedTokenTypeError` for a hint outside `SUPPORTED_TOKEN_TYPES = ("access_token", "refresh_token")` (authlib 1.9.5 authlib/oauth2/rfc6749/token_endpoint.py, TokenEndpoint), which departs from the Section 2.2 rule that an invalid hint is ignored.
The lab's `revoke_token` consults both token stores whatever the hint, returns quietly for an unknown token, and raises `InvalidGrantError` for a foreign client's token, matching Authlib.

### OAuth 2.0 Token Introspection (RFC 7662) and JWT Response (RFC 9701)

RFC 7662 provides a protocol for protected resources to query the authorization server regarding the active state and metadata of an incoming bearer token.
The protected resource issues an HTTP `POST` to `/introspect`:

```http
POST /introspect HTTP/1.1
Host: auth.example.com
Accept: application/json
Content-Type: application/x-www-form-urlencoded
Authorization: Bearer gateway_service_token_xyz

token=at_4Kf7s9XwQ2mL8...&token_type_hint=access_token
```

The authorization server verifies caller authorization.
If the token is valid, active, unexpired, and unrevoked, the server returns HTTP `200 OK` with JSON:

```json
{
  "active": true,
  "scope": "read write profile",
  "client_id": "client_spa_web",
  "sub": "usr_998124",
  "exp": 1763198400,
  "iat": 1763197500,
  "iss": "https://auth.example.com",
  "jti": "at_4Kf7s9XwQ2mL8"
}
```

If the token is invalid, expired, revoked, or issued to a different audience, the server returns:

```json
{
  "active": false
}
```

Per RFC 7662 Section 2.2, to avoid disclosing internal state or operational boundaries to untrusted parties, the authorization server SHOULD NOT include any additional information about an inactive token, including the specific reason why the token is inactive.
`IntrospectionEndpoint.create_introspection_payload` (authlib 1.9.5 authlib/oauth2/rfc7662/introspection.py, IntrospectionEndpoint.create_introspection_payload) returns `{"active": False}` when no token was resolved (unknown, or `check_permission` refused the caller) or when `token.is_expired()` or `token.is_revoked()` is true.
Per RFC 7662 Section 4, a protected resource MAY cache introspection responses but MUST NOT cache one beyond its `exp`, and the cache duration is an explicit performance-versus-staleness trade-off.

RFC 9701 (published January 2025) extends RFC 7662 by packaging the introspection response into a signed JWT.
The resource server requests it by sending `Accept: application/token-introspection+jwt` (RFC 9701 Section 4), and the authorization server MUST authenticate the caller.
The authorization server responds with `Content-Type: application/token-introspection+jwt` and a JWT whose `typ` header is `token-introspection+jwt`; the RFC's example header is `{"typ": "token-introspection+jwt", "alg": "RS256"}`, and RS256 is the default of the `introspection_signed_response_alg` client metadata (RFC 9701 Sections 5 and 6).
The JWT MUST carry top-level `iss`, `aud`, and `iat`, and nests the RFC 7662 members in a `token_introspection` claim "to prevent conflict and confusion with top-level JWT claims that may bear the same name" (RFC 9701 Section 5); for an inactive token, `token_introspection` MUST contain only `"active": false`.
The `typ` value and the nesting also stop an attacker from passing the introspection JWT off as an access token (RFC 9701 Section 8.1).
The motivation, per RFC 9701 Section 1, is that some resource servers need "stronger assurance that the authorization server issued the token introspection response", including cases where the authorization server assumes liability for its content.

### Access token revocation architectures

Revoking a refresh token or a family stops new access tokens, but an access token already in a client's hand stays usable until something rejects it.
The architectures differ in where that check lives and how long a revocation takes to bite.
The lab implements only the first two rows of the table below; the denylist and generation counter are described, with the one measured number, but not implemented in the lab.

| Architecture | Check location | Revocation latency | State and cost |
| :--- | :--- | :--- | :--- |
| Short-lived access token, no check | Resource server verifies signature and `exp` only | Up to the remaining access token lifetime | No revocation state; the only lever is the lifetime (see Numbers and limits). |
| Reference token with introspection (RFC 7662) | Authorization server answers `active` per call | Immediate at the authorization server, then up to the caller's cache time (RFC 7662 Section 4: MAY cache, MUST NOT cache past `exp`) | One authorization-server round trip per uncached call. |
| `jti` denylist | Gateway or resource server looks up the token's `jti` claim (RFC 9068 Section 2.2 makes `jti` a required claim) | Immediate once the entry is written and replicated | One key per revoked token, kept until the token's `exp`; 108 bytes per key measured here, so about 108 MB per $10^6$ live revocations. |
| Per-user generation or `revoked_before` counter | Token carries a generation or issue time; the check compares it with the user's current value | Immediate once the counter is replicated | One value per user, bounded by user count; revokes every session of that user, so no single-device logout. |
| Pushed revocation events | The authorization server tells subscribers to purge caches or deny a session | Event delivery time | OpenID CAEP 1.0 Section 3.1 defines a Session Revoked event ("the session identified by the subject has been revoked"); delivery guarantees and subscriber coverage are deployment properties, not in the event definition. |

The denylist and the counter are the same trade-off at different granularity: per-token precision costs memory proportional to revocations, while a per-user counter costs almost nothing and loses precision.
Both only help if every verifier consults the shared store, which puts a network dependency back on the hot path that stateless JWT verification avoided; that dependency, not the memory, is usually the real cost.
The Phantom Token Pattern below moves the same check to one place, the gateway, so downstream services stay stateless.

### The Phantom Token Pattern

The Phantom Token Pattern reconciles the conflicting requirements of public API security and microservice performance:
1. Public clients receive strictly opaque reference tokens composed of 256-bit cryptographically secure pseudorandom values (RFC 6749 Section 10.10 requires a guessing probability of at most 2^-128 and recommends at most 2^-160).
2. The browser or mobile client presents the opaque token via `Authorization: Bearer at_...`.
3. The API Gateway intercepts incoming requests at the network perimeter.
4. The Gateway checks its local or distributed cache (Redis or an in-process LRU); RFC 7662 Section 4 forbids caching past `exp`, and capping entries at 30 to 60 seconds is a design choice that bounds revocation latency.
5. On cache miss, the Gateway executes RFC 7662 introspection or RFC 9701 JWT introspection against the Authorization Server.
6. If `active == true`, the Gateway obtains an RFC 9068 access token JWT signed by the Authorization Server, with header `typ: at+jwt` (RFC 9068 Section 2.1) and the required claims `iss`, `exp`, `aud`, `sub`, `client_id`, `iat`, and `jti` (RFC 9068 Section 2.2), plus `scope`; in Curity's description the introspection response itself carries this JWT, and in the lab the gateway calls the Authorization-Server-side `TokenLifecycleManager.mint_phantom_jwt`.
7. The Gateway mutates the HTTP header list, stripping the opaque token and writing `Authorization: Bearer <signed_jwt>`.
8. Downstream microservices validate the internal JWT locally against the Authorization Server's cached JWKS without outbound calls; RFC 9068 Section 2.1 recommends asymmetric signatures and requires RS256 support, while the lab signs with HS256 and a shared test key only to stay hermetic.
9. When a token is revoked at the Authorization Server, the Gateway purges its cache entry via an event notification or awaits the short 30-second TTL expiration.
10. Subsequent requests fail immediately at the edge gateway with HTTP `401 Unauthorized`, shielding downstream services from invalid traffic.

## Numbers and limits

The following operational metrics govern production token lifecycles, memory footprints, and timing parameters (as of 2026-11):

| Parameter / Metric | Production Recommendation | RFC / Industry Reference | Failure Mode / Boundary Condition |
| :--- | :--- | :--- | :--- |
| Access token lifetime ($T_{at}$) | 300 s to 900 s (5 to 15 min); lab default 900 s | Industry practice; RFC 9700 Section 4.14 only says refresh tokens let the server issue access tokens "with a short lifetime" | Long TTL widens exposure window for leaked stateless bearer tokens. |
| Refresh token idle expiration | 14 days to 30 days | RFC 9700 Section 4.14.2: refresh tokens SHOULD expire after client inactivity, value at the server's discretion | Unbounded idle tokens permit indefinite access from abandoned client devices. |
| Refresh token absolute ceiling | 30 days to 90 days | Industry practice; RFC 9700 sets no ceiling | Missing absolute ceiling bypasses periodic user credential re-authentication. |
| Rotation grace window | 0 s to 60 s; Okta default 30 s, Auth0 off by default; lab default 10 s | Okta and Auth0 documentation (as of 2026-11); RFC 9700 defines no grace window | Zero leeway causes false-positive family revocation on SPA concurrent fetches; any leeway lets a replay inside the window go undetected. |
| Opaque token entropy | 256 bits (32 CSPRNG bytes; the lab uses `secrets.token_urlsafe(32)`) | RFC 6749 Section 10.10: guessing probability MUST be at most 2^-128 and SHOULD be at most 2^-160 | Below 128 bits violates RFC 6749 and makes online guessing feasible at scale. |
| Gateway cache TTL for introspection | 30 s to 60 s | RFC 7662 Section 4: MAY cache, MUST NOT cache past `exp`, trade-off left to the designer | High TTL delays revocation propagation; zero TTL overloads the IdP database. |
| Introspection latency (remote AS) | Not measured here: one AS round trip plus a store lookup per cache miss | None | Direct per-request introspection adds that round trip to every API call's tail latency. |
| Phantom JWT minting cost | 10.7 us per call (median of 3 best-of-5 `timeit` runs of 1,000 calls) | Measured 2026-11-15: `TokenLifecycleManager.mint_phantom_jwt` (in-memory introspection plus HS256 sign), PyJWT 2.15.1, CPython 3.14.6, macOS 27.3 arm64 (Darwin 27.3.0) | RS256 or ES256 signing costs more than HS256 and was not measured; caching the JWT per opaque token amortizes it. |
| Redis denylist memory per `jti` | 108 bytes per key (`used_memory` delta over $10^6$ keys) | Measured 2026-11-15: Redis 8.10.3 (Homebrew, libc malloc), $10^6$ `SET jti:at_<43-char token> 1 EX 900` through `redis-cli --pipe`, macOS 27.3 arm64 | Memory scales linearly with revocations: $10^6$ active revocations $\approx 108$ MB; jemalloc builds and other key shapes differ. |
| Pytest lab execution suite | 10 passed in 0.40 s | Measured 2026-11-15: `uv run --frozen pytest` on the lab test module | CPython 3.14.6, macOS 27.3 arm64 (Darwin 27.3.0). |

## Portability

The table below separates what was run from what is established by a cited source.
The lab matrix was paused by the user on 2026-11-11, so only the CPython 3.14.6 row and the in-process HTTP/1.1 row were run (macOS 27.3 arm64, Darwin 27.3.0, 2026-11-15); every other row is "Not run" and states only what a source read in this verification supports.
The middleware reads and rewrites only the ASGI `scope["headers"]` list, and the ASGI HTTP scope defines it as an iterable of `[name, value]` byte-string pairs in request order with duplicates preserved (ASGI HTTP and WebSocket spec, `headers` key), so the question per server is how it fills that list.

| Execution Target / Environment | Result | Validation Source / Command | Operational Caveats and Behavioral Differences |
| :--- | :--- | :--- | :--- |
| CPython `3.12.x` and `3.13.x` | Not run | `labs/pyproject.toml` declares `requires-python = ">=3.12,<3.16"`; the lab's only version-sensitive feature is `enum.StrEnum`, documented as "Added in version 3.11" (docs.python.org `enum`) | A pass is expected from those two facts but was not executed; the lab gained 2 tests after the drafter's earlier 8-test runs, so those runs do not count. |
| CPython `3.14.6` | Passed | `uv run --frozen pytest tests/f14_api_authentication_and_identity/test_refresh_token_rotation_reuse_detection_and_token_revocation_architectures.py` (10 passed in 0.40 s) | Workspace runtime (`labs/.python-version` is `3.14`). |
| CPython `3.15.x` (default and free-threaded builds) | Not run | Pinned hypothesis 6.170.14 declares `Requires-Python: >=3.11` and its install here contains only `hypothesis/_native.cpython-314-darwin.so` (listed in the `.venv`) | A 3.15 interpreter needs its own build of that compiled module and of every other compiled pin; free-threaded support of the lockfile was not checked. |
| Uvicorn `0.54.0` | Not run under a live server | Source read in `.venv`: `uvicorn/protocols/http/httptools_impl.py` (`on_header` appends `(name.lower(), value)` to `scope["headers"]`) and `h11_impl.py` (`[(key.lower(), value) for key, value in event.headers]`) | Both HTTP/1.1 parsers hand the middleware a lowercased byte-pair list, which is what `name.lower() != b"authorization"` expects. |
| Granian `2.8.7` | Not run under a live server | Source read at tag v2.8.7: `src/asgi/utils.rs`, `build_scope_common!` appends `(PyBytes(key), PyBytes(value))` per request header and inserts a `host` entry first when the request has no `Host` header | Rust-based ASGI/RSGI/WSGI application server, not a reverse proxy (`granian-2.8.7.dist-info/METADATA`). |
| Hypercorn `0.18.0` | Not run under a live server | Source read in `.venv`: `hypercorn/protocol/h11.py` lowercases names; `hypercorn/utils.py`, `filter_pseudo_headers`, drops pseudo-headers and puts `host` first from `:authority` or `Host` | HTTP/3 needs the `h3` extra (`aioquic>=0.9.0`, `hypercorn-0.18.0.dist-info/METADATA`); the lab pins `aioquic` 1.7.2. |
| HTTP/1.1 (RFC 9112) | Passed | In-process `httpx.ASGITransport` in the lab test | No socket: the transport builds the ASGI scope directly, so no server parser was exercised. |
| HTTP/2 (RFC 9113) | Not run | RFC 9113 Section 8.2: "Field names MUST be converted to lowercase when constructing an HTTP/2 message"; Section 8.3.1: a recipient "MUST NOT use the Host header field to determine the target URI if `:authority` is present" | Hypercorn maps `:authority` to `host` (`filter_pseudo_headers`), so the app sees the same header list shape as HTTP/1.1. |
| HTTP/3 (RFC 9114) | Not run | RFC 9114 Section 4.2: "Characters in field names MUST be converted to lowercase prior to their encoding" | Hypercorn applies the same `filter_pseudo_headers` on its HTTP/3 path (`hypercorn/protocol/h3.py`). |

## Worked example

The implementation below demonstrates complete refresh token rotation with token family tracking, concurrency grace period handling, RFC 7009 revocation, RFC 7662 introspection, RFC 9701 JWT introspection, and ASGI Phantom Token gateway translation.
The source code is located at [implementation](../labs/src/api_labs/f14_api_authentication_and_identity/token_lifecycle.py) and the test suite at [test](../labs/tests/f14_api_authentication_and_identity/test_refresh_token_rotation_reuse_detection_and_token_revocation_architectures.py).

```python
# Simplified excerpt from labs/src/api_labs/f14_api_authentication_and_identity/token_lifecycle.py

import enum
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import jwt
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


class TokenStatus(enum.StrEnum):
    ACTIVE = "active"
    ROTATED = "rotated"
    REVOKED = "revoked"


class TokenError(Exception):
    def __init__(self, error: str, description: str, status_code: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code


class InvalidGrantError(TokenError):
    def __init__(self, description: str) -> None:
        super().__init__(error="invalid_grant", description=description, status_code=400)


class InvalidTokenError(TokenError):
    def __init__(self, description: str) -> None:
        super().__init__(error="invalid_token", description=description, status_code=401)


@dataclass
class RefreshTokenRecord:
    token_id: str
    family_id: str
    user_id: str
    client_id: str
    generation: int
    parent_token_id: str | None
    status: TokenStatus
    issued_at: float
    expires_at: float
    rotated_at: float | None = None
    scopes: list[str] = field(default_factory=list[str])
    replacement_token_id: str | None = None
    replacement_access_token_id: str | None = None


@dataclass
class TokenFamily:
    family_id: str
    user_id: str
    client_id: str
    active_refresh_token_id: str
    is_revoked: bool = False
    revocation_reason: str | None = None
    created_at: float = field(default_factory=time.time)


class TokenLifecycleManager:
    def __init__(
        self,
        jwt_signing_key: str,
        jwt_issuer: str = "https://auth.example.com",
        default_access_ttl: float = 900.0,
        default_refresh_ttl: float = 86400.0,
        grace_period_seconds: float = 10.0,
    ) -> None:
        self.jwt_signing_key = jwt_signing_key
        self.jwt_issuer = jwt_issuer
        self.default_access_ttl = default_access_ttl
        self.default_refresh_ttl = default_refresh_ttl
        self.grace_period_seconds = grace_period_seconds
        self.families: dict[str, TokenFamily] = {}
        self.refresh_tokens: dict[str, RefreshTokenRecord] = {}
        self.access_tokens: dict[str, Any] = {}

    def rotate_refresh_token(
        self, presented_token_id: str, client_id: str, now: float | None = None
    ) -> tuple[str, str]:
        current_time = time.time() if now is None else now
        record = self.refresh_tokens.get(presented_token_id)
        if record is None or record.client_id != client_id:
            raise InvalidGrantError("Invalid or unknown refresh token.")

        family = self.families.get(record.family_id)
        if family is None or family.is_revoked:
            raise InvalidGrantError("Token family has been revoked.")

        if current_time >= record.expires_at:
            raise InvalidGrantError("Refresh token has expired.")

        # Replay / reuse detection
        if record.status == TokenStatus.ROTATED:
            elapsed = (
                current_time - record.rotated_at if record.rotated_at is not None else float("inf")
            )
            if (
                elapsed <= self.grace_period_seconds
                and record.replacement_token_id is not None
                and record.replacement_access_token_id is not None
                and family.active_refresh_token_id == record.replacement_token_id
            ):
                return record.replacement_access_token_id, record.replacement_token_id

            # Outside grace window or older ancestor: REUSE DETECTED! Revoke entire token family
            self.revoke_family(record.family_id, reason="reuse_detected")
            raise InvalidGrantError(
                "Refresh token reuse detected; all family tokens have been revoked."
            )

        if record.status != TokenStatus.ACTIVE:
            raise InvalidGrantError("Refresh token is not active.")

        new_rt_id = f"rt_{secrets.token_urlsafe(32)}"
        new_at_id = f"at_{secrets.token_urlsafe(32)}"

        record.status = TokenStatus.ROTATED
        record.rotated_at = current_time
        record.replacement_token_id = new_rt_id
        record.replacement_access_token_id = new_at_id

        new_rt = RefreshTokenRecord(
            token_id=new_rt_id,
            family_id=record.family_id,
            user_id=record.user_id,
            client_id=record.client_id,
            generation=record.generation + 1,
            parent_token_id=record.token_id,
            status=TokenStatus.ACTIVE,
            issued_at=current_time,
            expires_at=current_time + self.default_refresh_ttl,
            scopes=list(record.scopes),
        )
        self.refresh_tokens[new_rt_id] = new_rt
        family.active_refresh_token_id = new_rt_id
        return new_at_id, new_rt_id

    def revoke_family(self, family_id: str, reason: str) -> None:
        family = self.families.get(family_id)
        if family is None:
            return
        family.is_revoked = True
        family.revocation_reason = reason
        for rt in self.refresh_tokens.values():
            if rt.family_id == family_id:
                rt.status = TokenStatus.REVOKED
        for at in self.access_tokens.values():
            if at.family_id == family_id:
                at.status = TokenStatus.REVOKED
```

The execution output below was captured on 2026-11-15 by running pytest inside the workspace under CPython 3.14.6 on macOS 27.3 arm64 (Darwin 27.3.0):

```text
$ uv run --frozen pytest tests/f14_api_authentication_and_identity/test_refresh_token_rotation_reuse_detection_and_token_revocation_architectures.py
============================= test session starts ==============================
platform darwin -- Python 3.14.6, pytest-9.2.0, pluggy-1.7.0
rootdir: /Users/shreejitverma/github/The-Distinguished-Engineer/build/api_notes/jobs/Refresh-Token-Rotation-Reuse-Detection-and-Token-Revocation-Architectures/verify/labs
configfile: pyproject.toml
plugins: platformdirs-4.14.5, schemathesis-4.33.1, cov-7.1.0, hypothesis-6.170.14, inline-snapshot-0.36.1, Faker-40.44.0, locust-2.47.0, typeguard-4.6.0, anyio-4.16.0, asyncio-1.4.1.post1, respx-0.23.1
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collected 10 items

tests/f14_api_authentication_and_identity/test_refresh_token_rotation_reuse_detection_and_token_revocation_architectures.py . [ 10%]
.........                                                                [100%]

============================== 10 passed in 0.40s ==============================
```

## Pitfalls

> [!warning] TOCTOU Race Condition in Refresh Token Exchange (CVE-2026-1035 Pattern)
> **Trigger:** Handling `POST /token` refresh exchanges across concurrent threads or distributed worker nodes using non-atomic database checks (`SELECT status FROM tokens WHERE id = ?` followed by subsequent `UPDATE tokens SET status = 'rotated'`).
> **Symptom:** Parallel requests submitted within milliseconds using the exact same active refresh token both evaluate as active, so the authorization server issues multiple access tokens from one refresh token and branches the lineage tree.
> CVE-2026-1035 (published 2026-01-21, CWE-367) is exactly this in Keycloak's `TokenManager`: with strict refresh token rotation enabled, validation and update of refresh token usage were not atomic, so concurrent refresh requests bypassed single-use enforcement; Red Hat build of Keycloak 26.4.11 is listed as fixed.
> **Fix:** Execute token rotation atomically inside the persistence layer using conditional mutation (`UPDATE tokens SET status = 'rotated', rotated_at = now() WHERE id = ? AND status = 'active' RETURNING generation`), or leverage an atomic Redis Lua script; fail immediately if the rows-affected count equals zero.

> [!warning] False-Positive Family Revocation Caused by SPA Concurrency
> **Trigger:** Single-page applications issuing parallel asynchronous API requests on application initialization while holding an expired access token, without client-side request deduplication or server-side leeway windows.
> **Symptom:** Request A exchanges $RT_0$ for $RT_1$; Request B, dispatched concurrently before Request A completes, presents $RT_0$ 50 milliseconds later.
> The server flags $RT_0$ as rotated, triggers reuse detection, revokes the user's entire session, and forces an abrupt logout.
> **Fix:** Implement an explicit grace window on the authorization server; RFC 9700 does not define one, but Okta ("grace period for token rotation", default 30 s, range 0 to 60 s) and Auth0 ("reuse interval", off by default) ship one (as of 2026-11).
> If the immediate predecessor of the active token is received within the window, return the previously minted replacement pair without triggering family revocation, and keep the window short because a replay inside it goes undetected.

> [!warning] Silent Revocation Bypass via Username Case Sensitivity (CVE-2026-6647)
> **Trigger:** Inconsistent case normalization between the authentication layer and the revocation database lookup (e.g., authenticating `User.Name@example.com` but issuing revocation events for `user.name@example.com`).
> CVE-2026-6647 (published 2026-10-28, CWE-613, advisory WSO2-2026-5387) hit WSO2 Identity Server 7.2.0 before 7.2.0.21 configured with a case-insensitive user store and a case-sensitive identity database.
> **Symptom:** Security administrators revoke a compromised account, but the database query fails to match existing token records.
> Active refresh tokens remain valid, allowing an adversary to maintain persistent access.
> **Fix:** Enforce case-folded, canonical user identifiers (`sub` claim) across all grant tracking tables and indices before executing revocation lookups.

> [!warning] Gateway Cache Desynchronization during Phantom Token Translation
> **Trigger:** Caching RFC 7662 introspection responses or minted internal JWTs at edge API gateways using long TTLs without an active cache invalidation back-channel.
> **Symptom:** A user explicitly logs out or revokes authorization via RFC 7009, but the edge gateway continues honoring cached JWT mappings and proxying requests to internal microservices until the local cache TTL expires.
> **Fix:** Cap edge introspection cache TTLs to 30 to 60 seconds, and implement an asynchronous invalidation channel (such as Redis Pub/Sub, Kafka revocation topics, or OpenID CAEP Session Revoked events, CAEP 1.0 Section 3.1) to purge edge caches immediately upon revocation.

## Security and robustness

### Threat Modeling and Countermeasures

The security posture of token lifecycles centers on mitigating four primary threat vectors:

1. **Bearer Token Exfiltration via Cross-Site Scripting (XSS):**
   When public clients store access tokens in `localStorage` or memory, XSS vulnerabilities allow malicious scripts to read tokens.
   The Phantom Token Pattern mitigates this risk by ensuring external tokens are opaque reference strings that contain zero user claims, zero scopes, and zero internal infrastructure data.
   Pairing Phantom Tokens with Backend-For-Frontend (BFF) architecture and encrypted, `HttpOnly`, `SameSite=Strict` cookies eliminates JavaScript token access entirely.

2. **Refresh Token Interception and Long-Term Persistence:**
   If an adversary intercepts a refresh token over an insecure channel or via local device compromise, they gain standing access.
   Refresh Token Rotation eliminates static persistence by invalidating each token upon use.
   Token Family tracking guarantees that if both the legitimate client and the adversary attempt to use tokens from the same grant, the inevitable replay triggers instantaneous invalidation of all descendant tokens.

3. **Introspection Endpoint Denial of Service (DoS):**
   Naively introspecting every incoming request at the API Gateway introduces a thundering herd risk against the Authorization Server database.
   Mitigate this by employing local edge caching bounded by short TTLs (30 to 60 seconds) and mutex locking (singleflight pattern) per token identifier to prevent cache stampedes.

4. **Revocation Oracle and Timing Attacks:**
   If the RFC 7009 revocation endpoint returns `404 Not Found` for invalid tokens or exhibits measurable timing variance when querying nonexistent keys, it leaks token existence to anyone holding valid client credentials.
   RFC 7009 Section 2.2 already answers `200` for invalid tokens, and Section 5 requires DoS countermeasures on the endpoint and notes that a guessing client must also present valid client credentials for a token it owns; looking tokens up by a hash of the token string, rather than comparing raw strings, is a defense-in-depth choice beyond the RFC.

### OWASP API Security Top 10 Mapping

- **API1:2023 Broken Object Level Authorization (BOLA):**
  Internal microservices receiving rich RFC 9068 JWTs from the API Gateway must validate that the authenticated `sub` has authorization to access the target object ID, rather than assuming edge gateway admission implies universal resource access.
- **API2:2023 Broken Authentication:**
  RTR and Token Family Revocation detect stolen refresh token replay on public clients, satisfying RFC 9700 Section 2.2.2 when sender-constraining is not available.
- **API4:2023 Unrestricted Resource Consumption:**
  The introspection endpoint must be protected by client authentication or dedicated machine-to-machine bearer tokens to prevent external actors from exhausting database resources with random string lookups; RFC 9701 Section 8.2 makes authenticating every introspection request a MUST.

## The principal's view

Staff-plus engineers evaluate token architecture across three structural axes: security perimeter boundaries, operational blast radius, and network cost.
The choice between stateless JWTs, stateful reference tokens, and hybrid patterns represents a fundamental architectural compromise:

```text
+-----------------------+-----------------------------+-----------------------------+-----------------------------+
| Dimension             | Pure Stateless JWTs         | Pure Reference Tokens       | Phantom Token Pattern       |
+-----------------------+-----------------------------+-----------------------------+-----------------------------+
| Perimeter Security    | High leak risk (PII, scopes)| Zero leak risk (opaque)     | Zero external leak risk     |
| Instant Revocation    | Impossible without denylist | Instantaneous at IdP store  | Edge cache TTL or purge     |
| Downstream Latency    | Local verify (cached JWKS)  | One IdP round trip per call | Local verify (cached JWKS)  |
| IdP Query Load        | Zero query load at runtime  | Linear in request rate      | Bounded by edge cache TTL   |
| Organizational Scale  | High service coupling       | Low service coupling        | Fully decoupled perimeters  |
+-----------------------+-----------------------------+-----------------------------+-----------------------------+
```

When designing an enterprise platform:
1. **Choose Pure Stateless JWTs** only for internal, high-throughput service-to-service communication within a trusted network perimeter where token lifetimes are constrained under five minutes and user PII is absent.
2. **Choose Pure Reference Tokens** for low-volume administrative applications or highly sensitive banking portals where instant server-side revocation on every click is legally mandated and database query costs are negligible.
3. **Choose the Phantom Token Pattern** as the universal enterprise default.
   It establishes a strict separation of concerns: public clients handle meaningless opaque references, API Gateways enforce revocation and manage translation, and backend microservices consume standardized, self-contained RFC 9068 JWTs.
4. **Mandate Refresh Token Rotation with Grace Windows** for all public clients whenever sender-constrained mechanisms (DPoP or mTLS) cannot be deployed due to client ecosystem constraints.

## Interview questions

> [!question] L5: Why does the RFC 7009 Token Revocation endpoint mandate returning HTTP `200 OK` even if the submitted token does not exist or has already been revoked?
>
> > [!success]- Answer
> > RFC 7009 Section 2.2 gives the reason directly: "the client cannot handle such an error in a reasonable way", and the purpose of the request, invalidating the token, "is already achieved", so the call is effectively idempotent.
> > A side benefit is that differential codes such as `404 Not Found` would reveal token existence to anyone holding the client's credentials, although Section 5 argues that such a party gains more by using a guessed token than by revoking it.
> > The `200` is not unconditional: client-authentication failures and tokens issued to another client are refused, `unsupported_token_type` and `503` remain possible (Sections 2.1 and 2.2.1).
> >
> > Follow-up: If the client provides an invalid `token_type_hint`, does the authorization server reject the request?

> [!question] L5: What specific security risks arise when exposing signed JSON Web Tokens (JWTs) directly to browser single-page applications?
>
> > [!success]- Answer
> > Browser-accessible JWTs can be decoded by any client-side script, exposing sensitive user claims, internal authorization scopes, and organizational identifiers.
> > If stored in `localStorage` or `sessionStorage`, tokens are vulnerable to extraction via Cross-Site Scripting (XSS).
> > Furthermore, because JWTs are verified statelessly by downstream services, immediate session termination becomes complex and requires distributed denylist infrastructure.
> >
> > Follow-up: How does the Phantom Token Pattern resolve this risk without adding database latency to downstream microservices?

> [!question] L6: Predict the output of concurrent and delayed refresh token replay
> A client rotates refresh token $RT_0$ to $RT_1$ at $t=0$.
> The server enforces a 10-second grace window.
> At $t=4$, a concurrent request presents $RT_0$.
> At $t=15$, an attacker presents $RT_0$.
> At $t=16$, the client presents $RT_1$.
> What occurs at each step?
>
> > [!success]- Answer
> > At $t=4$, the server observes that $RT_0$ is marked `ROTATED` but the elapsed duration (4 seconds) is within the 10-second grace period.
> > The server treats this as an asynchronous retry and safely returns the existing replacement tokens ($AT_1$, $RT_1$) without advancing the generation or modifying family state.
> > At $t=15$, the attacker presents $RT_0$ outside the grace period (15 seconds elapsed).
> > The server triggers reuse detection, marks the entire family as `REVOKED`, invalidates $RT_1$ and all active access tokens, logs an intrusion alert, and returns HTTP `400 Bad Request` (`invalid_grant`).
> > At $t=16$, the legitimate client presents $RT_1$; because the family is revoked, the server immediately returns HTTP `400 Bad Request` (`invalid_grant`), forcing full interactive re-authentication.
> >
> > Follow-up: How should the database enforce this logic across multi-region active-active clusters?

> [!question] L6: Debug this API: Intermittent session termination under concurrent SPA dashboard requests
> An engineering team reports that their single-page application intermittently logs out users immediately after login when loading a dashboard with four parallel widget fetch requests.
> What is the root cause and how is it resolved?
>
> > [!success]- Answer
> > The dashboard initiates four simultaneous asynchronous API calls on page load.
> > If the initial access token is expired, all four requests trigger parallel token refresh calls presenting the same initial refresh token $RT_0$.
> > The first request to arrive rotates $RT_0$ to $RT_1$.
> > The subsequent three requests arrive milliseconds later presenting the now-rotated $RT_0$.
> > If the authorization server lacks a rotation grace window, it interprets the second request as an illegal token reuse attack, revokes the entire token family, and terminates the user's session.
> > The resolution is twofold: implement a server-side rotation leeway window (5 to 30 seconds) that returns the existing replacement pair for concurrent retries, and implement a client-side request queue that serializes token refresh calls behind a single in-flight promise.
> >
> > Follow-up: Why is client-side promise serialization alone insufficient to prevent this failure across multi-tab browser sessions?

> [!question] L6: How does RFC 9701 improve upon RFC 7662 Token Introspection for zero-trust service architectures?
>
> > [!success]- Answer
> > RFC 7662 returns introspection results as plain, unsigned JSON over HTTP.
> > In complex architectures traversing multiple reverse proxies, API gateways, and internal meshes, unsigned JSON provides no cryptographic guarantee of origin, authenticity, or tamper-resistance.
> > RFC 9701 encapsulates introspection claims within a digitally signed JWT returning `Content-Type: application/token-introspection+jwt`.
> > The receiving resource server or gateway can verify the signature against the authorization server's public keys, gaining what RFC 9701 Section 1 calls "stronger assurance that the authorization server issued the token introspection response" and preventing intermediate proxies from forging claim metadata.
> >
> > Follow-up: Where are the standard introspection claims placed inside an RFC 9701 JWT payload, and why?

> [!question] L6: Compare distributed access token revocation via Redis `jti` denylists versus User Generation ID (Epoch) tracking
> What are the memory, latency, and session-granularity trade-offs between the two approaches?
>
> > [!success]- Answer
> > A `jti` denylist stores individual revoked token identifiers in Redis with TTL set to the remaining token lifetime.
> > Its memory footprint is proportional to the number of explicit revocations ($O(R)$), which is negligible under normal operations but can experience memory spikes during mass revocation events.
> > User Generation ID embeds a monotonic integer (or timestamp `revoked_before`) inside the JWT claims and maintains the current valid generation per user in Redis.
> > Verification requires comparing `jwt.gen >= user.current_gen`, which requires a single integer lookup per user and keeps storage bounded at $O(U)$ where $U$ is active users.
> > However, Generation IDs lack granularity: incrementing a user generation revokes all concurrent sessions across all devices, preventing targeted single-device logout.
> >
> > Follow-up: How can an API gateway cache Generation IDs locally while preserving instant global revocation?

> [!question] L7: Under what circumstances should an enterprise architect reject Refresh Token Rotation in favor of Sender-Constrained Tokens (RFC 9449 DPoP or RFC 8705 mTLS)?
>
> > [!success]- Answer
> > Refresh Token Rotation is an optimistic, reactive detection mechanism: it detects token theft only after a replayed token has already been transmitted to the authorization server.
> > If an attacker exfiltrates a refresh token and uses it before the legitimate client does, the attacker successfully mints valid access tokens, and the legitimate client is the party whose subsequent request triggers revocation.
> > In contrast, sender-constrained tokens via DPoP or mTLS bind credentials cryptographically to a private key held in client secure storage (Hardware Security Modules, Secure Enclave, or WebCrypto non-exportable keys).
> > Even if an adversary intercepts the bearer string, the token cannot be replayed without generating a valid cryptographic proof signature.
> > Architects should mandate DPoP or mTLS for high-assurance APIs (open banking, healthcare, administrative consoles) and reserve RTR for legacy environments or runtime contexts lacking WebCrypto/mTLS support.
> >
> > Follow-up: What is the CPU and latency overhead of DPoP proof verification at the edge gateway compared to standard bearer token validation?

> [!question] L7: Design an edge token translation architecture capable of handling 500,000 requests per second across three continents while providing sub-minute revocation propagation.
>
> > [!success]- Answer
> > Deploy the Phantom Token Pattern at edge points of presence (PoPs) using Envoy or OpenResty reverse proxies.
> > Public clients present opaque reference tokens via `Authorization: Bearer <opaque_token>`.
> > Edge nodes maintain a local LRU memory cache backed by an edge Redis cluster, mapping SHA-256 token hashes to signed RFC 9068 internal JWTs.
> > The edge cache TTL is fixed to 60 seconds, which bounds the maximum revocation propagation latency under pure polling.
> > To achieve instantaneous revocation, the Authorization Server broadcasts revocation events over an event mesh (such as Redis Pub/Sub, Kafka, or AWS SNS/SQS) connecting all regional edge clusters; edge proxies subscribe to this topic and purge matching token hashes immediately.
> > Cache misses trigger an RFC 7662/9701 introspection call to regional Authorization Server read replicas, protected by edge singleflight request coalescing to eliminate thundering herd spikes against the identity database.
> >
> > Follow-up: How do you handle regional network partitions where edge PoPs cannot reach the Authorization Server event mesh?

## Related

Prerequisites: [[JWKS-Discovery-Caching-Key-Rotation-and-Thundering-Herd-Protection]]

Next: [[Sender-Constrained-Tokens-DPoP-RFC-9449-and-mTLS-Binding-RFC-8705]]

See also: [[API-Authentication-and-Authorization]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]], [[API-Authentication-Architecture-Edge-Verification-vs-Zero-Trust-Propagation]], [[OAuth-2-0-to-OAuth-2-1-and-RFC-9700-Security-BCP]], [[JWT-JWS-JWK-Structure-and-RFC-9068-Access-Token-Profile]], [[OAuth-2-0-for-Browser-Based-Applications-BFF-and-Cookies-RFC-10017]]

- Lab: `labs/tests/f14_api_authentication_and_identity/test_refresh_token_rotation_reuse_detection_and_token_revocation_architectures.py`

## Further reading

- [RFC 9700: Best Current Practice for OAuth 2.0 Security](https://www.rfc-editor.org/rfc/rfc9700.html)
  The IETF security BCP that requires sender-constrained or rotated refresh tokens for public clients (Section 2.2.2) and defines rotation and replay response in Section 4.14.2.
- [RFC 7662: OAuth 2.0 Token Introspection](https://www.rfc-editor.org/rfc/rfc7662.html)
  The foundational specification for querying token status, active validation rules, and authorization metadata at protected resources.
- [RFC 9701: JSON Web Token (JWT) Response for OAuth Token Introspection](https://www.rfc-editor.org/rfc/rfc9701.html)
  Standardizes cryptographically signed and verifiable token introspection responses using the `application/token-introspection+jwt` media type.
- [RFC 7009: OAuth 2.0 Token Revocation](https://www.rfc-editor.org/rfc/rfc7009.html)
  Specifies the `/revoke` endpoint protocol, hint handling, cascading invalidation semantics, the `200` answer for invalid tokens, and the remaining error cases.
- [RFC 9068: JSON Web Token (JWT) Profile for OAuth 2.0 Access Tokens](https://www.rfc-editor.org/rfc/rfc9068.html)
  Defines the standard internal JWT structure, `typ: at+jwt` header validation, and required authorization claims.
- [The Phantom Token Pattern (Curity)](https://curity.io/resources/learn/phantom-token-pattern/)
  Curity's description of a gateway swapping an opaque reference token for the by-value JWT the token service returns from introspection.
- [OpenID Continuous Access Evaluation Profile 1.0](https://openid.net/specs/openid-caep-1_0.html)
  Defines the Session Revoked event (Section 3.1), the standard push channel for cache purge after revocation.
- [ASGI HTTP and WebSocket specification](https://asgi.readthedocs.io/en/latest/specs/www.html)
  Defines the `headers` scope key the gateway middleware rewrites, so it fixes what every server must provide.
