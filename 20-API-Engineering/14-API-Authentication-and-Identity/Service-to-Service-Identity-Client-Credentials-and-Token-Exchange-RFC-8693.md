---
type: pattern
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://www.rfc-editor.org/rfc/rfc8693.html"
  - "https://www.rfc-editor.org/rfc/rfc6749.html"
  - "https://www.rfc-editor.org/rfc/rfc6750.html"
  - "https://www.rfc-editor.org/rfc/rfc7519.html"
  - "https://www.rfc-editor.org/rfc/rfc7523.html"
  - "https://www.rfc-editor.org/rfc/rfc8707.html"
  - "https://www.rfc-editor.org/rfc/rfc8725.html"
  - "https://www.rfc-editor.org/rfc/rfc9068.html"
  - "https://www.rfc-editor.org/rfc/rfc9700.html"
  - "https://owasp.org/API-Security/editions/2023/en/0xa1-broken-object-level-authorization/"
  - "https://owasp.org/API-Security/editions/2023/en/0xa5-broken-function-level-authorization/"
  - "https://github.com/spiffe/spiffe/blob/main/standards/JWT-SVID.md"
  - "https://www.rfc-editor.org/rfc/rfc9113.html"
  - "https://github.com/nginx/nginx/blob/master/src/http/ngx_http_core_module.c"
  - "https://github.com/nginx/nginx/blob/master/src/http/ngx_http_special_response.c"
  - "https://www.envoyproxy.io/docs/envoy/latest/api-v3/extensions/filters/network/http_connection_manager/v3/http_connection_manager.proto"
  - "https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow"
  - "https://docs.cloud.google.com/iam/docs/reference/sts/rest/v1/TopLevel/token"
  - "https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRoleWithWebIdentity.html"
  - "https://www.keycloak.org/securing-apps/token-exchange"
  - "https://www.keycloak.org/2025/05/standard-token-exchange-kc-26-2"
  - "https://github.com/advisories/GHSA-75p6-52g3-rqc8"
  - "https://access.redhat.com/security/cve/CVE-2022-1245"
  - "https://www.rfc-editor.org/rfc/rfc9114.html"
---

# East-West Service Authentication: OAuth Client Credentials vs RFC 8693 Token Exchange (On-Behalf-Of Delegation vs Impersonation)

> [!summary] Claim
> Propagating east-west service identity via OAuth 2.0 Client Credentials destroys end-user identity and creates confused deputy vulnerabilities by substituting a machine principal for the end user, whereas RFC 8693 Token Exchange keeps the user subject, lets the STS downscope and audience-restrict each hop, and records the delegation history in the nested `act` actor claim.

Part of [API Authentication and Identity](README.md) in [API Engineering](../README.md).

## TL;DR

Mechanism: OAuth 2.0 Client Credentials Grant `(RFC 6749 Section 4.4)` authenticates machine-to-machine callers directly to issue tokens where `sub` equals the client identifier, while RFC 8693 Token Exchange `(RFC 8693 Section 2.1)` trades an inbound user token for a downscoped, audience-constrained downstream token bearing the user `sub` and an `act` claim designating the calling service as a delegated actor.
Contract rule: East-west calls serving an inbound user request should propagate user identity via token exchange rather than client credentials; RFC 8693 leaves scope policy to the authorization server, so the STS must itself enforce that the requested scope is a subset of the subject token scopes (RFC 8693 Section 5 only suggests `scope` and a limited lifetime to contain delegated rights).
Trap: Verbatim token forwarding leaks broad credentials across microservice boundaries, while naive token exchange without downstream token caching introduces synchronous network round-trips to the authorization server on every microservice hop.
Portability fact: The `urn:ietf:params:oauth:grant-type:token-exchange` grant is implemented by Keycloak (standard token exchange, officially supported since 26.2, delegation still preview) and Google Cloud STS, while Microsoft Entra ID On-Behalf-Of (RFC 7523 `jwt-bearer` grant) and AWS STS `AssumeRoleWithWebIdentity` are proprietary analogues, not RFC 8693 `(as of 2026-11)`.
When not to use it: For purely autonomous background jobs, asynchronous scheduled batch pipelines, or systems without an originating user context, use OAuth 2.0 Client Credentials or mTLS SPIFFE identities directly.

## Mental model

```mermaid
sequenceDiagram
    participant U as End User / Edge
    participant A as Service A (Edge Gateway)
    participant STS as Security Token Service (RFC 8693)
    participant B as Service B (Order Service)
    participant C as Service C (Payment Service)

    U->>A: HTTP request with User Access Token<br/>sub: alice, aud: service-a, scope: order:write payment:pay
    Note over A: Needs to invoke Service B on behalf of Alice
    A->>STS: POST /oauth/token (grant_type=token-exchange)<br/>subject_token: Alice_Token, audience: service-b, scope: order:write payment:pay
    STS-->>A: 200 OK with Exchanged Token Hop 1<br/>sub: alice, aud: service-b, scope: order:write payment:pay, act: {sub: service-a}
    A->>B: GET /orders with Exchanged Token Hop 1
    Note over B: Evaluates sub: alice for row security<br/>Logs current actor: service-a
    Note over B: Needs to invoke Service C on behalf of Alice
    B->>STS: POST /oauth/token (grant_type=token-exchange)<br/>subject_token: Hop1_Token, audience: service-c, scope: payment:pay
    STS-->>B: 200 OK with Exchanged Token Hop 2<br/>sub: alice, aud: service-c, act: {sub: service-b, act: {sub: service-a}}
    B->>C: POST /payments with Exchanged Token Hop 2
    Note over C: Evaluates sub: alice AND immediate actor service-b<br/>Prior actor service-a preserved for audit trail
    C-->>B: 201 Created
    B-->>A: 200 OK
    A-->>U: 200 OK
```

The diagram illustrates how identity transforms across a multi-hop east-west microservice call chain.
The end user sends an initial access token targeting `service-a` with broad scopes.
When Service A needs to call downstream Service B, it does not forward the broad user token verbatim.
Instead, Service A presents its own confidential client credentials alongside the user subject token to the Security Token Service (STS) using `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`.
The STS mints a new token explicitly targeted to `service-b` with narrowed scopes, preserving `sub: alice` and adding an `act` claim identifying `service-a` as the immediate actor.
When Service B subsequently invokes Service C, it performs a second exchange that narrows the Hop 1 scopes to `payment:pay` and nests the prior actor inside a new `act` structure, so the token Service C receives records the whole delegation history.
The Hop 1 exchange keeps `payment:pay` because an STS that enforces the subset rule can only narrow scopes at each hop, never add them.

## How it works

East-west microservice identity requires establishing both who requested the operation (the human user or originating tenant) and which service is currently executing the hop.
Traditional systems employ one of two extremes: verbatim token forwarding or OAuth 2.0 Client Credentials.
Both extremes introduce severe security defects.

### The Failure of Verbatim Token Forwarding

In naive microservice architectures, Service A receives an HTTP request containing `Authorization: Bearer <token>` and forwards that exact header on its outgoing HTTP requests to Service B and Service C.
This pattern violates the principle of least privilege.
The token received by Service A was issued with an audience claim `aud` intended for Service A or a broad API perimeter.
If Service B is compromised, it can replay that bearer token against any other service within the enterprise network or external third-party APIs.
Furthermore, the token cannot be downscoped per hop; Service B receives the exact same permissions granted to Service A, even if Service B only needs read-only access to a single database entity.

### The Failure of Pure Client Credentials

The standard alternative is the OAuth 2.0 Client Credentials Grant `(RFC 6749 Section 4.4)`.
Here, Service A authenticates directly to the authorization server using its own client identifier and shared secret or private key JWT `(RFC 7523 Section 2.2)`:

```http
POST /oauth/token HTTP/1.1
Host: as.example.internal
Content-Type: application/x-www-form-urlencoded
Authorization: Basic c2VydmljZS1hOnNlY3JldC1zZXJ2aWNlLWE=

grant_type=client_credentials&audience=service-b&scope=read%3Arecords
```

The authorization server issues a machine-to-machine (M2M) token.
Under the RFC 9068 JWT Profile for OAuth 2.0 Access Tokens, for grants where no resource owner is involved, such as the client credentials grant, the value of `sub` SHOULD correspond to an identifier the authorization server uses to indicate the client application `(RFC 9068 Section 2.2)`, which in practice is usually the client identifier.
RFC 9068 Section 2.2 also makes `iss`, `exp`, `aud`, `sub`, `client_id`, `iat`, and `jti` REQUIRED:

```json
{
  "iss": "https://as.example.internal",
  "sub": "service-a",
  "client_id": "service-a",
  "aud": "service-b",
  "iat": 1794734400,
  "exp": 1794738000,
  "jti": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "scope": "read:records"
}
```

Authlib implements exactly this branch `(authlib 1.9.5 authlib/oauth2/rfc9068/token.py, JWTBearerTokenGenerator.access_token_generator)`: it sets `token_data["sub"] = str(user.get_user_id())` when `if user:` is true, and otherwise sets `token_data["sub"] = client.get_client_id()`.
The fatal flaw of this approach is the total erasure of end-user identity.
When Service B receives this token, it has no cryptographic record of which user initiated the request.
Service B cannot enforce row-level security (RLS), cannot evaluate multi-tenant isolation, and cannot log an attributed audit trail.
Most critically, this creates the classic Confused Deputy vulnerability.
Because Service A possesses a generic machine credential, any caller who can persuade Service A to execute a request can access records belonging to any user on Service B.

### RFC 8693 Token Exchange Protocol

RFC 8693 defines a standardized framework for exchanging security tokens at an OAuth 2.0 token endpoint `(RFC 8693 Section 2.1)`.
The client initiates an exchange by transmitting an HTTP `POST` request formatted as `application/x-www-form-urlencoded` containing the following parameters:

```http
POST /oauth/token HTTP/1.1
Host: as.example.internal
Content-Type: application/x-www-form-urlencoded
Authorization: Basic c2VydmljZS1hOnNlY3JldC1zZXJ2aWNlLWE=

grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Atoken-exchange
&subject_token=eyJhbGciOiJSUzI1NiIsInR5cCI6ImF0K2p3dCJ9...
&subject_token_type=urn%3Aietf%3Aparams%3Aoauth%3Atoken-type%3Aaccess_token
&audience=service-b
&scope=read%3Arecords
&requested_token_type=urn%3Aietf%3Aparams%3Aoauth%3Atoken-type%3Aaccess_token
```

The request parameters adhere to strict protocol rules `(RFC 8693 Section 2.1)`:
- `grant_type`: REQUIRED; must be the registered URN `urn:ietf:params:oauth:grant-type:token-exchange`.
- `subject_token`: REQUIRED; carries the security token representing the identity on whose behalf the request is made.
- `subject_token_type`: REQUIRED; carries a URI identifying the token type, such as `urn:ietf:params:oauth:token-type:access_token`, `urn:ietf:params:oauth:token-type:refresh_token`, `urn:ietf:params:oauth:token-type:id_token`, or `urn:ietf:params:oauth:token-type:jwt` `(RFC 8693 Section 3)`.
- `audience`: OPTIONAL; logical identifier of the target service `(RFC 8693 Section 2.1)`.
- `resource`: OPTIONAL; physical URI indicating where the token will be used `(RFC 8707 Section 2)`.
- `scope`: OPTIONAL; space-delimited string of requested access rights.
- `actor_token`: OPTIONAL; security token representing the identity of the acting party.
- `actor_token_type`: REQUIRED when `actor_token` is present, and MUST NOT be included otherwise.
- `requested_token_type`: OPTIONAL; desired token type; when unspecified, the issued token type is at the discretion of the authorization server (the lab STS always issues `urn:ietf:params:oauth:token-type:access_token`).

The token endpoint uses the extension grant mechanism of `(RFC 6749 Section 4.5)`, and client authentication uses the normal OAuth mechanisms, such as HTTP Basic `(RFC 6749 Section 2.3.1)` or a JWT client assertion `(RFC 7523 Section 2.2)`.
Whether unauthenticated clients may exchange tokens is a deployment decision, but RFC 8693 Section 2.1 warns that omitting client authentication lets anyone holding a compromised token leverage it through the STS into other tokens.

### The Actor Token and the Exchange Response

`actor_token` identifies the party that will act, and `actor_token_type` is REQUIRED with it and MUST NOT appear without it `(RFC 8693 Section 2.1)`.
When no `actor_token` is sent, the lab treats the authenticated client as the current actor; when one is sent, the validated `sub` of the actor token becomes the `act` subject, and `may_act` is compared against that subject.
A malformed or tampered `actor_token` is `invalid_request`, never a silent fallback to the client identity (`test_actor_token_sets_current_actor_and_pairing_rules`).
A successful response is a normal token response whose `issued_token_type` is REQUIRED `(RFC 8693 Section 2.2.1)`.
`token_type` takes the value `N_A` when the issued token is not usable as an OAuth access token, and `scope` is OPTIONAL only if it equals the requested scope, otherwise REQUIRED `(RFC 8693 Section 2.2.1)`.
The lab always issues `Bearer` access tokens, so it never uses `N_A`, and it returns `scope` whenever one is effective.

### Token Verification and Scope Downscoping

RFC 8693 requires the authorization server to "perform the appropriate validation procedures for the indicated token type" `(RFC 8693 Section 2.1)` and leaves the rest to policy.
The lab STS, like most production STSs, executes five validation stages:
1. Client Authentication: The calling service (Service A) authenticates as a confidential OAuth client via HTTP Basic or a private key JWT; failure is `401` with `error: invalid_client` `(RFC 6749 Section 5.2)`.
2. Subject Token Validation: The STS validates the signature, issuer `iss`, and expiration `exp` of the `subject_token`; an invalid subject token MUST produce `error: invalid_request` `(RFC 8693 Section 2.2.2)`, not `invalid_grant`.
3. Target Authorization: The STS verifies whether the client is permitted to request tokens for the target `audience` or `resource`; when it is unwilling or unable to issue a token for a target, the `invalid_target` error code SHOULD be used `(RFC 8693 Section 2.2.2)`.
4. Authorized Actor Check: When the `subject_token` contains a `may_act` claim, the STS can use it to decide whether the client (or the party in `actor_token`) may act for the subject `(RFC 8693 Section 4.4)`; a subject token that is unacceptable by policy MUST be rejected with `error: invalid_request` `(RFC 8693 Section 2.2.2)`, which the lab returns as `400`.
5. Scope Downscoping: The lab STS rejects any requested scope absent from the `subject_token` with `400` `error: invalid_scope` `(RFC 6749 Section 5.2)`; this subset rule is STS policy, because RFC 8693 Section 2.1.1 only defines a request as the Cartesian product of the requested scopes at all requested targets, and Section 5 only suggests `scope` and a limited lifetime as mitigations.

Every token endpoint error is an `application/json` body with an `error` member and a `400` status unless specified otherwise; `invalid_client` may be `401`, and `unauthorized_client` is also `400`, not `403` `(RFC 6749 Section 5.2)`.

### Delegation vs Impersonation Semantics

RFC 8693 explicitly differentiates delegation from impersonation `(RFC 8693 Section 1.1)`.
In impersonation, Principal A is given all rights of Principal B within a defined context and is completely indistinguishable from B.
The resulting token contains `sub: alice` and omits any record of Principal A.
Downstream services believe they are interacting directly with Alice.
Impersonation destroys the audit trail and prevents downstream services from detecting compromised intermediate proxies.
RFC 8693 defines no request parameter for choosing impersonation over delegation; whether a composite (delegation) token is issued is at the discretion of the authorization server and its policy `(RFC 8693 Section 1.1)`, so the lab models the choice with a non-standard `want_impersonation` flag gated by a per-client `can_impersonate` permission.

In delegation (On-Behalf-Of), Principal A retains its own separate identity, and the security token explicitly asserts that actions are taken by Principal A acting on behalf of Principal B `(RFC 8693 Section 1.1)`.
In a JSON Web Token, delegation is expressed using the `act` (actor) claim `(RFC 8693 Section 4.1)`:

```json
{
  "iss": "https://as.example.internal",
  "sub": "alice",
  "client_id": "service-a",
  "aud": "service-b",
  "exp": 1794738000,
  "scope": "read:records",
  "act": {
    "sub": "service-a"
  }
}
```

### Multi-Hop Delegation Chains

When Service B subsequently invokes Service C, Service B presents the Hop 1 token as the `subject_token` in a new token exchange request.
RFC 8693 states that a chain of delegation can be expressed by nesting one `act` claim within another `(RFC 8693 Section 4.1)`.
The outermost `act` claim represents the current actor, nested `act` claims represent prior actors, and the least recent actor is the most deeply nested:

```json
{
  "iss": "https://as.example.internal",
  "sub": "alice",
  "client_id": "service-b",
  "aud": "service-c",
  "exp": 1794738000,
  "scope": "payment:pay",
  "act": {
    "sub": "service-b",
    "act": {
      "sub": "service-a"
    }
  }
}
```

Under RFC 8693 Section 4.1, the consumer of a token MUST only consider the token's top-level claims and the party identified as the current actor by the `act` claim for access control policy.
Prior actors identified by any nested `act` claims are informational only and are not to be considered in access control decisions.
This design makes Service C decide based strictly on the top-level claims (Alice's subject and the token's scope) and Service B as the current actor, while the nested history remains available for audit logging.
Claims such as `exp`, `nbf`, and `aud` are not meaningful inside an `act` or `may_act` object and are therefore not used `(RFC 8693 Section 4.1)` `(RFC 8693 Section 4.4)`.

## Numbers and limits

The following table summarizes protocol limits, measured cryptographic overheads, and wire sizes governing service authentication `(as of 2026-11)`.
Measured rows were run on an Apple M3 Pro, macOS 27.3 (Darwin 27.3.0, arm64), CPython 3.14.6, `PyJWT 2.15.1`, `cryptography 50.0.2` linked against OpenSSL 4.0.3, with `timeit.repeat(number=300, repeat=7)` against the lab `SecurityTokenService` (RSA-2048, RS256); times are the median of the seven per-call means.

| Metric / Parameter | Value / Range | Primary Source / Measurement Protocol | Architectural Impact |
|:---|:---|:---|:---|
| Nginx header buffers (defaults) | `client_header_buffer_size` 1 KiB; `large_client_header_buffers` 4 x 8 KiB | `ngx_http_core_module.c`, `ngx_http_core_merge_srv_conf` (nginx master, as of 2026-11) | One header line longer than 8 KiB (for example `Authorization: Bearer <jwt>`) is rejected with `400 Request Header Or Cookie Too Large` (internal code 494 in `ngx_http_special_response.c`); a request line over 8 KiB gets `414`. |
| Max request headers (Envoy) | 60 KiB total, default | Envoy `HttpConnectionManager.max_request_headers_kb` docs (as of 2026-11) | Requests over the limit receive `431`. |
| HTTP/2 initial max frame size | 16,384 octets | `(RFC 9113 Section 6.5.2)`, `SETTINGS_MAX_FRAME_SIZE` | An HPACK-encoded header block larger than the peer's frame size continues in `CONTINUATION` frames. |
| RS256 sign, lab `mint_token` | 0.47 ms median (0.46 ms with `jwt.encode` and a parsed key object) | Measured as above | The STS pays this per exchange; trivial next to the network round trip. |
| RS256 sign with PEM bytes as the key | 28.0 ms median | Measured as above: `jwt.encode(claims, pem_bytes, algorithm="RS256")` | Passing PEM bytes makes PyJWT re-parse the RSA key on every call, about 60x slower; the lab signs and verifies with parsed key objects for this reason. |
| RS256 verify | 0.037 ms median with a parsed public key; 0.053 ms with PEM bytes | Measured as above: `jwt.decode(..., algorithms=["RS256"], audience=...)` | Local verification at downstream services is CPU-trivial. |
| Exchanged JWT size, 0 hops (user token) | 635 bytes | Measured: lab `mint_token` with `iss`, `iat`, `nbf`, `exp`, `jti`, `sub`, `aud`, `scope`, header `typ: at+jwt` | Baseline ingress token. |
| Exchanged JWT size, 1 hop | 702 bytes (+67) | Measured: lab `issue_token_exchange`, adds `client_id` and `act: {"sub": "service-a"}` | First hop pays for `client_id` plus the first actor. |
| Exchanged JWT size, 4 hops | 806 bytes (+34 to +35 per extra hop) | Measured: actors `service-a` to `service-d` nested four deep | Growth is linear in actor identifier length; with short identifiers, 8 KiB is far away, while long URIs and per-actor `iss` claims grow it faster. |
| Exchanged token lifetime | No normative value | RFC 8693 Section 5 suggests "a limited token lifetime"; the RFC 8693 Section 2.3 example issues a token with `expires_in` of 60 | Lifetime is STS policy; the lab clamps it to the subject token's remaining lifetime. |
| Max delegation depth | No normative limit | RFC 8693 defines none; lab `ClientRegistration.max_delegation_depth` defaults to 5 (the test fixture uses 4) | STS policy that bounds audit-chain length and token growth. |

## Portability

The table below documents implementation compatibility across runtime versions, library point releases, ASGI server implementations, and cloud STS providers `(as of 2026-11)`.

| Environment / Platform | Version / Build | Status | Evidence / Upstream Verification | Notes and Failure Modes |
|:---|:---|:---|:---|:---|
| CPython 3.12.16 | pinned matrix interpreter | Not run | The pinned lab matrix was not executed for this note (lab checks paused 2026-11-11); no result is claimed | The lab is pure Python on `fastapi`, `pydantic`, `PyJWT`, and `cryptography`; any 3.12 difference is unmeasured. |
| CPython 3.13.18 | pinned matrix interpreter | Not run | Same: matrix not executed | Same. |
| CPython 3.14.6 | workspace interpreter | Supported | Run here: `uv run --frozen pytest ... -v`, 16 passed in 0.92 s (macOS 27.3, arm64) | The pinned matrix names 3.14.10; the run used the workspace's 3.14.6. |
| CPython 3.15.2 and 3.15.2t | pinned matrix interpreters | Not run | Same: matrix not executed | No claim about the free-threaded build is made. |
| PyJWT Library | 2.15.1 | Supported | Source verified: `(pyjwt 2.15.1 jwt/api_jwt.py, PyJWT.decode)` | Nested JSON object claims such as `act` round-trip unchanged. |
| Authlib Library | 1.9.5 | Supported | Source verified: `(authlib 1.9.5 authlib/oauth2/rfc9068/token.py, JWTBearerTokenGenerator.access_token_generator)` | RFC 9068 access token generator sets `sub` to the client ID when no user is present. |
| Cryptography Library | 50.0.2 | Supported | Verified: `backend.openssl_version_text()` returns OpenSSL 4.0.3 in the lab venv | RSA-2048 signing at 0.46 ms per JWT measured above. |
| Uvicorn Server | 0.54.0 | Not exercised | The lab drives apps in process through `httpx.ASGITransport`, which bypasses every server | Server-level header limits are not tested by this lab. |
| Hypercorn Server | 0.18.0 | Not exercised | Not run by this lab | Same as Uvicorn. |
| Granian Server | 2.8.7 | Not exercised | Not run by this lab | Same as Uvicorn. |
| HTTP/1.1 | text protocol | Cited | nginx `ngx_http_core_module.c` and `ngx_http_special_response.c` (rows above) | Header size limits are per implementation, not per RFC; the token travels in one `Authorization` line. |
| HTTP/2 | `(RFC 9113 Section 6.5.2)` | Cited | `SETTINGS_MAX_HEADER_LIST_SIZE` is advisory, and the initial `SETTINGS_MAX_FRAME_SIZE` is 16,384 octets | A bearer token plus other fields beyond the peer's advertised list size can be refused; the token endpoint is otherwise unchanged. |
| HTTP/3 over QUIC | `(RFC 9114 Section 4.2.2, Section 7.2.4.1)` | Cited | `SETTINGS_MAX_FIELD_SECTION_SIZE` defaults to unlimited, and a server that gets a larger header section than it will handle can send `431` | The lab does not speak HTTP/3; exchange semantics are identical because RFC 8693 sits above HTTP. |
| Keycloak | 26.2+ | Partial | Keycloak token exchange docs and 26.2 announcement (as of 2026-11) | Standard token exchange (V2) is officially supported for internal-to-internal access tokens; delegation with `actor_token` and `act` is preview and disabled by default; impersonation is not implemented; without `actor_token`, subject tokens carrying `act` or `may_act` are rejected; `resource` is unsupported. |
| Microsoft Entra ID | Cloud Service | Analogue, not RFC 8693 | Microsoft identity platform OBO docs (as of 2026-11) | OBO uses `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`, `assertion=<incoming token>`, and `requested_token_use=on_behalf_of`. |
| Google Cloud STS | Cloud Service | Supported | `sts.googleapis.com` `v1/token` reference (as of 2026-11) | `grantType` must be `urn:ietf:params:oauth:grant-type:token-exchange`; used for Workload Identity Federation. |
| AWS STS | Cloud Service | Analogue, not RFC 8693 | `AssumeRoleWithWebIdentity` API reference | Proprietary API that trades an OIDC web identity token for temporary IAM role credentials. |
| SPIFFE / SPIRE | JWT-SVID specification | Supported | SPIFFE JWT-SVID standard | Provides workload identity (X.509-SVID for mTLS, JWT-SVID for bearer use) in dual-token setups. |

## Worked example

The runnable lab implementation is located at [implementation](../labs/src/api_labs/f14_api_authentication_and_identity/service_to_service_identity.py), and its verification test suite is located at [test](../labs/tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py).

The excerpt below shows the core Security Token Service engine executing an RFC 8693 token exchange with scope downscoping and recursive actor construction:

```python
# Excerpt from labs/src/api_labs/f14_api_authentication_and_identity/service_to_service_identity.py
# (the SecurityTokenService.issue_token_exchange method, dedented by four spaces)

def issue_token_exchange(
    self,
    client: ClientRegistration,
    request: TokenExchangeRequest,
    lifetime_seconds: int = 3600,
) -> TokenResponse:
    """Execute RFC 8693 Token Exchange."""
    if GRANT_TYPE_TOKEN_EXCHANGE not in client.allowed_grant_types:
        raise OAuthError(
            ERROR_UNAUTHORIZED_CLIENT,
            "Client not authorized for token exchange grant",
        )

    # 1. Validate subject_token_type (RFC 8693 Section 2.1)
    valid_subject_types = {TOKEN_TYPE_ACCESS_TOKEN, TOKEN_TYPE_JWT}
    if request.subject_token_type not in valid_subject_types:
        raise OAuthError(
            ERROR_INVALID_REQUEST,
            f"Unsupported subject_token_type: {request.subject_token_type}",
        )

    # 2. Decode and validate subject_token; an invalid or policy-rejected
    # subject_token MUST yield invalid_request (RFC 8693 Section 2.2.2)
    try:
        subject_claims = self.decode_token(request.subject_token, audience=None)
    except OAuthError as exc:
        raise OAuthError(
            ERROR_INVALID_REQUEST,
            f"Invalid subject_token: {exc.error_description}",
        ) from exc
    subject_id = subject_claims.get("sub")
    if not subject_id:
        raise OAuthError(ERROR_INVALID_REQUEST, "subject_token missing 'sub' claim")

    # actor_token and actor_token_type travel together (RFC 8693 Section 2.1):
    # the type is REQUIRED with a token and MUST NOT appear without one.
    actor_id = client.client_id
    if request.actor_token is None and request.actor_token_type is not None:
        raise OAuthError(
            ERROR_INVALID_REQUEST,
            "actor_token_type MUST NOT be included without actor_token",
        )
    if request.actor_token is not None:
        if request.actor_token_type not in {TOKEN_TYPE_ACCESS_TOKEN, TOKEN_TYPE_JWT}:
            raise OAuthError(
                ERROR_INVALID_REQUEST,
                "actor_token requires a supported actor_token_type",
            )
        try:
            actor_claims = self.decode_token(request.actor_token, audience=None)
        except OAuthError as exc:
            raise OAuthError(
                ERROR_INVALID_REQUEST,
                f"Invalid actor_token: {exc.error_description}",
            ) from exc
        actor_sub = actor_claims.get("sub")
        if not actor_sub:
            raise OAuthError(ERROR_INVALID_REQUEST, "actor_token missing 'sub' claim")
        actor_id = str(actor_sub)

    # 3. Determine and validate target (audience / resource, RFC 8693 Section 2.1)
    target = request.audience or request.resource
    if not target:
        raise OAuthError(
            ERROR_INVALID_REQUEST,
            "Token exchange requires 'audience' or 'resource' parameter",
        )

    if client.allowed_audiences and target not in client.allowed_audiences:
        raise OAuthError(
            ERROR_INVALID_TARGET,
            f"Client not authorized to request tokens for target: {target}",
        )

    # 4. Check 'may_act' claim in subject token if present (RFC 8693 Section 4.4)
    if "may_act" in subject_claims:
        may_act = subject_claims["may_act"]
        if isinstance(may_act, dict):
            may_act_dict = cast("dict[str, Any]", may_act)
            authorized_actor: str | None = cast("str | None", may_act_dict.get("sub"))
            if authorized_actor and authorized_actor != actor_id:
                # Policy-rejected subject_token: invalid_request (RFC 8693 Section 2.2.2)
                raise OAuthError(
                    ERROR_INVALID_REQUEST,
                    f"Actor '{actor_id}' is not authorized actor '{authorized_actor}'",
                )

    # 5. Scope downscoping: subset rule is STS policy, not mandated by RFC 8693
    # (Section 5 only suggests scope to limit delegated rights)
    subject_scope_raw = subject_claims.get("scope", "")
    subject_scope_str = str(subject_scope_raw) if subject_scope_raw else ""
    subject_scopes: set[str] = set(subject_scope_str.split()) if subject_scope_str else set()

    if request.scope:
        requested_scopes = set(request.scope.split())
        if not requested_scopes.issubset(subject_scopes):
            missing = requested_scopes - subject_scopes
            raise OAuthError(
                ERROR_INVALID_SCOPE,
                f"Requested scope exceeds subject token scopes; missing: {sorted(missing)}",
            )
        effective_scope = request.scope
    else:
        effective_scope = subject_scope_str

    # 6. Delegation vs Impersonation handling (RFC 8693 Section 1.1 & 4.1)
    new_claims: dict[str, Any] = {
        "sub": subject_id,
        "client_id": client.client_id,
        "aud": target,
    }
    if effective_scope:
        new_claims["scope"] = effective_scope

    if request.want_impersonation:
        if not client.can_impersonate:
            raise OAuthError(
                ERROR_UNAUTHORIZED_CLIENT,
                f"Client '{client.client_id}' is not authorized for impersonation",
            )
        # Impersonation: omit 'act' claim entirely per RFC 8693 Section 1.1
    else:
        if not client.can_delegate:
            raise OAuthError(
                ERROR_UNAUTHORIZED_CLIENT,
                f"Client '{client.client_id}' is not authorized for delegation",
            )

        # Build recursive delegation actor chain (RFC 8693 Section 4.1)
        prior_act = subject_claims.get("act")
        depth = 1  # Count the new actor being appended
        curr: Any = prior_act
        while isinstance(curr, dict):
            depth += 1
            curr = cast("dict[str, Any]", curr).get("act")

        if depth > client.max_delegation_depth:
            raise OAuthError(
                ERROR_INVALID_REQUEST,
                f"Maximum delegation depth of {client.max_delegation_depth} "
                f"exceeded (depth: {depth})",
            )

        current_actor_claim: dict[str, Any] = {"sub": actor_id}
        if prior_act and isinstance(prior_act, dict):
            current_actor_claim["act"] = prior_act

        new_claims["act"] = current_actor_claim

    # Bound token lifetime by the remaining lifetime of the subject token
    subject_exp = subject_claims.get("exp")
    now = int(time.time())
    token_lifetime = lifetime_seconds
    if subject_exp:
        remaining_lifetime = subject_exp - now
        if remaining_lifetime < token_lifetime:
            token_lifetime = max(1, remaining_lifetime)

    token_str = self.mint_token(new_claims, lifetime_seconds=token_lifetime)
    return TokenResponse(
        access_token=token_str,
        issued_token_type=TOKEN_TYPE_ACCESS_TOKEN,
        token_type="Bearer",  # noqa: S106 - OAuth parameter
        expires_in=token_lifetime,
        scope=effective_scope or None,
    )
```

The test below demonstrates that a downstream service enforcing end-user authorization correctly admits a delegated token while rejecting a client credentials M2M token:

```python
# Simplified excerpt from labs/tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py


@pytest.mark.unit
@pytest.mark.asyncio
async def test_token_exchange_succeeds_downstream_with_actor_audit(
    sts: SecurityTokenService,
) -> None:
    inbound_token = sts.mint_token(
        {"sub": "alice", "aud": "service-a", "scope": "read:records write:records"},
        lifetime_seconds=3600,
    )

    client_a = sts.clients["service-a"]
    req = TokenExchangeRequest(
        grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
        audience="service-b",
        scope="read:records",
        subject_token=inbound_token,
        subject_token_type=TOKEN_TYPE_ACCESS_TOKEN,
    )
    exchange_resp = sts.issue_token_exchange(client_a, req)

    app_b = create_downstream_service(
        sts,
        expected_audience="service-b",
        required_scope="read:records",
    )
    transport = httpx.ASGITransport(app=app_b)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client_http:
        resp = await client_http.get(
            "/records/alice",
            headers={"Authorization": f"Bearer {exchange_resp.access_token}"},
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["owner"] == "alice"
        assert data["caller_subject"] == "alice"
        assert data["current_actor"] == "service-a"
        assert data["delegation_chain"] == {"sub": "service-a"}
```

Execution output captured on an Apple M3 Pro, macOS 27.3 (Darwin 27.3.0, arm64), CPython 3.14.6; absolute paths are shortened to `/labs`:

```text
$ uv run --frozen pytest tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py -v
============================= test session starts ==============================
platform darwin -- Python 3.14.6, pytest-9.2.0, pluggy-1.7.0 -- /labs/.venv/bin/python
cachedir: .pytest_cache
hypothesis profile 'default'
rootdir: /labs
configfile: pyproject.toml
plugins: platformdirs-4.14.5, schemathesis-4.33.1, cov-7.1.0, hypothesis-6.170.14, inline-snapshot-0.36.1, Faker-40.44.0, locust-2.47.0, typeguard-4.6.0, anyio-4.16.0, asyncio-1.4.1.post1, respx-0.23.1
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collecting ... collected 16 items

tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py::test_client_credentials_m2m_token_loss_of_user_context PASSED [  6%]
tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py::test_client_credentials_fails_downstream_user_access_control PASSED [ 12%]
tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py::test_token_exchange_delegation_preserves_subject_and_actor PASSED [ 18%]
tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py::test_token_exchange_succeeds_downstream_with_actor_audit PASSED [ 25%]
tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py::test_multi_hop_delegation_chain_nested_act_claim PASSED [ 31%]
tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py::test_scope_downscoping_and_escalation_rejection PASSED [ 37%]
tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py::test_target_audience_validation_and_invalid_target PASSED [ 43%]
tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py::test_impersonation_mode_omits_act_claim PASSED [ 50%]
tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py::test_may_act_claim_enforces_authorized_actors PASSED [ 56%]
tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py::test_actor_token_sets_current_actor_and_pairing_rules PASSED [ 62%]
tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py::test_token_lifetime_bounded_by_subject_token_expiration PASSED [ 68%]
tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py::test_delegation_depth_limit_prevents_recursion_bomb PASSED [ 75%]
tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py::test_invalid_and_expired_subject_token_rejected PASSED [ 81%]
tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py::test_unauthenticated_client_rejected PASSED [ 87%]
tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py::test_in_process_asgi_token_endpoint_wire_flow PASSED [ 93%]
tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py::test_in_process_asgi_token_endpoint_error_responses PASSED [100%]

============================== 16 passed in 0.92s ==============================
```

## Pitfalls

> [!warning] The Confused Deputy Trap of M2M Client Credentials
> Trigger: Using OAuth 2.0 Client Credentials to obtain east-west service tokens during the processing of an end-user API request.
> Symptom: Downstream services evaluate authorization against the calling microservice principal rather than the user, allowing any authenticated user of Service A to view or manipulate records of arbitrary users on Service B.
> Fix: Ban client credentials for user-initiated call flows; enforce RFC 8693 token exchange so that the user `sub` is carried through all downstream hops, allowing downstream services to enforce row-level access control.

> [!warning] Audience Laundering via Missing Resource Indicators (RFC 8707)
> Trigger: Performing token exchange without specifying an explicit `audience` or `resource` parameter, or the STS issuing tokens with wildcard audiences.
> Symptom: A compromised microservice captures a delegated token and replays it against high-value internal services (such as billing or administrative control planes) that share the same token signing issuer.
> Incident: CVE-2022-1245 (GHSA-75p6-52g3-rqc8) was a Keycloak token exchange flaw: missing authorization let a client holding a valid access token obtain tokens for any target client by passing that client's `client_id`; it affected `keycloak-services` below 18.0.0 and was fixed in 18.0.0, and Red Hat describes the feature as technology preview.
> The lab's `allowed_audiences` check (step 3 of `issue_token_exchange`, covered by `test_target_audience_validation_and_invalid_target`) is the same control, enforced per client.
> Fix: `audience` and `resource` are OPTIONAL in `(RFC 8693 Section 2.1)`, so make an explicit target mandatory by STS policy and reject unauthorized targets with `error: invalid_target` `(RFC 8693 Section 2.2.2)`; access tokens SHOULD be audience-restricted, and a resource server MUST refuse a token not meant for it `(RFC 9700 Section 2.3)`.

> [!warning] Unbounded Delegation Depth and Request Header Overflow
> Trigger: Allowing microservices to execute unconstrained recursive token exchange across deep or cyclic call graphs (A -> B -> C -> D -> E -> F), especially with long URI actor identifiers, per-actor `iss` claims, or extra claims copied into each `act`.
> Symptom: The token grows linearly with every hop (34 to 35 bytes per hop with short identifiers in the lab measurement), and once the `Authorization` line passes 8 KiB Nginx answers `400 Request Header Or Cookie Too Large` with default `large_client_header_buffers`, while Envoy answers `431` once all request headers pass its 60 KiB default.
> Fix: Impose a hard ceiling on delegation depth in the STS policy (the lab default is 5) and reject requests exceeding it with `error: invalid_request` `(RFC 8693 Section 2.2.2)`; keep actor identifiers short.

> [!warning] Blind Impersonation and Forensic Non-Repudiation Collapse
> Trigger: Granting intermediate services generic impersonation rights (in the lab, the non-standard `want_impersonation=true` flag) instead of on-behalf-of delegation.
> Symptom: The issued access token omits the `act` claim entirely, making it impossible to determine in post-incident analysis whether an action was executed directly by the user or by an intermediate compromised service, because under impersonation the token holder "is B within the context of the rights authorized by the token" `(RFC 8693 Section 1.1)`.
> Fix: Require strict administrative approval and dedicated client scopes for impersonation; default all inter-service exchange flows to delegation with mandatory `act` claims.

> [!warning] STS Thundering Herd on Cascading Downstream Calls
> Trigger: Executing a synchronous network call to the STS on every east-west RPC hop without caching exchanged tokens.
> Symptom: A single user request fanning out to 10 microservices generates 10 synchronous token exchange calls, so STS load scales with fan-out times request rate and every hop adds an STS round trip to the user-visible latency.
> Fix: Implement intermediate token caching keyed by `(sha256(subject_token), audience, scope)`; reuse the cached exchanged token until its expiration minus a safety window (for example 30 seconds), and never past the subject token's own expiry.

## Security and robustness

A zero-trust microservice mesh requires rigorous boundary defense and cryptographic isolation.
Relying solely on network-level perimeter security is insufficient.
Below is the threat model analysis governing east-west token propagation:

### Threat Modeling and Mitigation Matrix

| Threat | Defense | Lab check |
|:---|:---|:---|
| Confused deputy (attacker abuses a proxy) | Preserve `sub` and `act`; downstream checks the user's rows | `test_client_credentials_fails_downstream_user_access_control` |
| Token replay by a compromised hop | Strict `aud` validation (RFC 8707 resource indicators) | `test_target_audience_validation_and_invalid_target` |
| Privilege escalation by scope expansion | Requested scope must be a subset of the subject's (STS policy) | `test_scope_downscoping_and_escalation_rejection` |
| Revived expired user | Lifetime clamped to `min(requested, subject.exp - now)` | `test_token_lifetime_bounded_by_subject_token_expiration` |

Tokens carry privacy-sensitive information and MUST only be sent over encrypted channels such as TLS `(RFC 8693 Section 6)`.

1. Confused Deputy Prevention (OWASP API1:2023 - BOLA):
   When Service A calls Service B with Service A's machine identity, Service B has no mechanism to determine whether User Alice owns the requested entity.
   By mandating token exchange, the token delivered to Service B contains `sub: alice` and `act: {"sub": "service-a"}`.
   Service B validates that the entity identifier matches `sub: alice` and that `service-a` is an authorized caller for this operation.
2. Token Replay and Scope Containment (OWASP API5:2023 - BFLA):
   If Service B is compromised, an attacker capturing the inbound token can only use it against `service-b` because the `aud` claim is cryptographically bound to `service-b`.
   Attempting to present that token to Service C will fail audience validation.
   Furthermore, because Service A downscoped the token to `read:records`, the compromised Service B cannot invoke write operations on any API.
3. Lifetime Synchronization and Revocation:
   A critical vulnerability in delegation architectures is token lifetime inflation.
   If User Alice's original token expires in 60 seconds, an intermediate service must not be permitted to exchange it for a downstream token valid for 3,600 seconds.
   RFC 8693 only says the output token's expiration "may be influenced by that of the input token" `(RFC 8693 Section 2.1)`, so clamping is STS policy: the lab sets the issued lifetime to `min(default_ttl, subject_token.exp - now)`.
   With clamping, downstream delegated tokens expire no later than Alice's token, but revoking her token does not revoke them: the exchange "does not create a tight linkage between the input and output tokens", and propagating revocation is implementation specific `(RFC 8693 Section 2.1)`.

## The principal's view

At staff-plus engineering levels, choosing between Client Credentials and RFC 8693 Token Exchange is an architectural trade-off balancing zero-trust blast radius containment against system latency and operational complexity.

### Architectural Decision Framework

```text
Is there an active human user or originating tenant context?
  |
  +---> NO  --> Autonomous batch, scheduled cron, background daemon
  |             Use OAuth 2.0 Client Credentials Grant (RFC 6749)
  |             or Transport mTLS SPIFFE Identities.
  |             Rationale: No user identity exists to propagate;
  |             service account represents autonomous system actor.
  |
  +---> YES --> User-initiated request traversing microservice mesh
                |
                +---> Shallow topology (Single direct hop, low blast radius)?
                |     Evaluate Dual-Token Architecture:
                |     Transport mTLS for service authentication +
                |     forward user token with strict audience validation.
                |
                +---> Deep topology (Multi-hop, heterogeneous security domains)?
                      Enforce RFC 8693 Token Exchange (On-Behalf-Of Delegation).
                      Mandate scope downscoping, audience constraint,
                      and intermediate token caching.
```

### Organizational Governance and Blast Radius

In high-consequence enterprise environments (financial ledgers, healthcare platforms, multi-tenant SaaS), pure Client Credentials creates an unacceptable compliance and security risk.
Auditors examining database logs will see all transactions executed by `service-worker-backend` rather than the specific customer, destroying end-to-end auditability.
Conversely, attempting to implement RFC 8693 without a caching strategy can destabilize the authorization infrastructure.
A staff engineer must mandate that every intermediate service implements an in-memory token cache keyed by the cryptographic hash of the subject token, the target audience, and the requested scope.
With a 300-second token lifetime and a 30-second refresh threshold, each cached token is reused for 270 seconds, so a key that sees one call per second costs one exchange per 270 calls (a hit ratio near 99.6%), while a key seen once per session gains nothing; hit ratio is a function of per-key request rate, not of the TTL alone.

## Interview questions

> [!question] L5: Explain why forwarding an end-user access token verbatim to downstream microservices is considered an anti-pattern.
>
> > [!success]- Answer
> > Verbatim forwarding exposes a high-privilege bearer token to every service in the call graph.
> > The token contains an audience claim intended for the edge or first-hop service, violating audience isolation.
> > If any downstream service is compromised or malicious, it can replay that captured bearer token against unrelated services across the enterprise.
> > Furthermore, verbatim forwarding prevents downscoping permissions to match the specific needs of each downstream call.
> >
> > Follow-up: How does RFC 8693 Token Exchange prevent token replay across microservices?

> [!question] L6: What is the fundamental difference between delegation semantics and impersonation semantics under RFC 8693 Section 1.1?
>
> > [!success]- Answer
> > In impersonation, the calling service acts directly as the subject with no cryptographic record of the proxy's involvement in the token.
> > In delegation (On-Behalf-Of), the subject retains their identity while the token explicitly designates the calling service as an actor via the `act` claim.
> > Delegation preserves an STS-signed record of the actor chain for auditing and lets the resource server apply access control to the current actor as well as the subject `(RFC 8693 Section 4.1)`.
> > Impersonation should be strictly restricted because it conceals intermediate actors and destroys forensic auditability.
> >
> > Follow-up: How does a downstream resource server process nested `act` claims when evaluating authorization?

> [!question] L6: Predict the output: a client presents a subject token with `scope: read:records` and requests token exchange with `scope: read:records write:records`; what does the STS return?
>
> > [!success]- Answer
> > The lab STS rejects the request with `400 Bad Request` and an error payload containing `{"error": "invalid_scope"}` `(RFC 6749 Section 5.2)`.
> > RFC 8693 itself does not mandate the subset rule; Section 2.1.1 treats the request as the Cartesian product of scopes and targets and leaves fulfilment to policy, so the STS must enforce the rule explicitly.
> > Requesting scopes not granted in the subject token is a privilege escalation attempt.
> > The STS detects that `write:records` is absent from the subject token and halts token issuance.
> >
> > Follow-up: What error code must the STS return if the client requests an audience that is not registered or authorized?

> [!question] L6: Debug this API: an intermediate service successfully executes token exchange and receives an access token for Service B, but Service B continuously returns HTTP 403 Forbidden with `detail: Insufficient scope`.
>
> > [!success]- Answer
> > Check whether the intermediate service explicitly passed the `scope` parameter in its token exchange request or omitted it.
> > When the client omits `scope`, the authorization server MUST either use a pre-defined default or fail the request `(RFC 6749 Section 3.3)`; the lab STS carries over all subject scopes, which may not include Service B's required scope name.
> > The token response must include `scope` whenever the issued scope differs from the requested scope `(RFC 8693 Section 2.2.1)`, so compare it with what you asked for.
> > Verify also that the STS maps incoming permission scopes to the specific scope taxonomy expected by the target audience.
> > Inspect the decoded JWT claims of the exchanged token to ensure the `scope` claim contains the exact space-delimited string required by Service B.
> >
> > Follow-up: If the scope claim is correct, what other claim in the token could cause Service B to reject the call?

> [!question] L7: In a microservice mesh with 5 cascading downstream hops (A -> B -> C -> D -> E), how does RFC 8693 represent the delegation chain, and what operational risk does it introduce?
>
> > [!success]- Answer
> > RFC 8693 Section 4.1 represents multi-hop delegation through recursively nested `act` claims inside the JWT.
> > The outermost `act` object identifies the immediate caller (Service D), which nests Service C, Service B, and Service A.
> > This recursive nesting causes linear payload growth: the lab measured 34 to 35 bytes per additional hop with short identifiers such as `service-b`, more with URI identifiers or per-actor `iss` claims.
> > If unconstrained, the token can exceed proxy header limits, such as Nginx's default 8 KiB per header line (`400 Request Header Or Cookie Too Large`) or Envoy's default 60 KiB of request headers (`431`).
> > The system must enforce a maximum delegation depth limit at the STS and reject chains exceeding that threshold.
> >
> > Follow-up: How can an enterprise architecture avoid nested JWT bloat while preserving end-to-end zero-trust identity across 10 or more microservice hops?

> [!question] L7: Describe the Dual-Token pattern (SPIFFE/mTLS + User JWT) and compare its architectural trade-offs against pure RFC 8693 Token Exchange.
>
> > [!success]- Answer
> > The Dual-Token pattern separates transport-level service authentication from application-level user authorization.
> > Microservices communicate over mutual TLS where identity is established via SPIFFE X.509 or JWT SVIDs at the network layer.
> > The end-user JWT is forwarded in the HTTP `Authorization` header without undergoing token exchange at every hop.
> > This eliminates synchronous STS network calls and avoids token size bloat from nested actor claims.
> > However, it sacrifices per-hop audience isolation and scope downscoping unless combined with local cryptographic assertion minting.
> >
> > Follow-up: In what regulatory or compliance environments is the Dual-Token pattern insufficient without RFC 8693 token exchange?

> [!question] L5: Why must the expiration of an exchanged token be bounded by the expiration of the subject token?
>
> > [!success]- Answer
> > If an exchanged token had an independent lifetime, an intermediate service could mint a long-lived credential from a short-lived user token.
> > This would allow downstream services to continue acting on behalf of a user whose session has already terminated or expired.
> > RFC 8693 does not mandate it, so the authorization server should set the exchanged token lifetime to `min(default_ttl, subject_exp - now)` as policy, as the lab does.
> > This guarantees that when the user token expires, every delegated credential across the microservice mesh has expired no later; revocation of the user token, however, is not propagated by the protocol `(RFC 8693 Section 2.1)`.
> >
> > Follow-up: How do you handle asynchronous background processing that must continue after the user logs out?

> [!question] L6: What is the purpose of the `may_act` claim in RFC 8693 Section 4.4, and where is it evaluated?
>
> > [!success]- Answer
> > The `may_act` claim is an assertion inside a subject token declaring which specific party is authorized to act on behalf of the subject.
> > It contains claims identifying the authorized actor, such as `{"may_act": {"sub": "trusted-gateway"}}`.
> > The authorization server can evaluate this claim during the token exchange request to decide whether the client, or the party identified in `actor_token`, may engage in the requested delegation or impersonation.
> > If the authenticated client does not match the identity declared in `may_act`, the subject token is unacceptable by policy, so the exchange is rejected with `error: invalid_request` `(RFC 8693 Section 2.2.2)`, as the lab does with `400`.
> >
> > Follow-up: Why does RFC 8693 say claims such as `exp`, `nbf`, and `aud` are not meaningful inside the `may_act` and `act` claim objects?

> [!question] L7: What must the STS do with an `actor_token` that has no `actor_token_type` (or the reverse), and why does the actor identity come from the token rather than the authenticated client?
>
> > [!success]- Answer
> > Both are malformed: the type is REQUIRED when `actor_token` is present and MUST NOT be included otherwise `(RFC 8693 Section 2.1)`, so the lab returns `invalid_request` for each.
> > The authenticated client only proves who is calling the STS; `actor_token` lets a different party be the actor, and `may_act` in the subject token authorizes that party `(RFC 8693 Section 4.4)`.
> > Taking the actor from the validated token keeps the `act` claim truthful, and a token that fails validation must be rejected, not replaced by the client identity.
> >
> > Follow-up: What happens to the audit trail if the STS records the authenticated client as the actor while a different party presented the `actor_token`?

## Related

Prerequisites: [[Sender-Constrained-Tokens-DPoP-RFC-9449-and-mTLS-Binding-RFC-8705]]

Next: [[Pushed-Authorization-Requests-PAR-RFC-9126-and-Rich-Authorization-Requests-RAR-RFC-9396]]

See also: [[API-Authentication-and-Authorization]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]], [[JWT-JWS-JWK-Structure-and-RFC-9068-Access-Token-Profile]], [[JWT-Cryptographic-Pitfalls-and-RFC-8725-Best-Current-Practices]], [[API-Authentication-Architecture-Edge-Verification-vs-Zero-Trust-Propagation]]

- Lab: `labs/tests/f14_api_authentication_and_identity/test_service_to_service_identity_client_credentials_and_token_exchange_rfc_8693.py`

## Further reading

- [RFC 8693: OAuth 2.0 Token Exchange](https://www.rfc-editor.org/rfc/rfc8693.html)
  The core standard defining token exchange requests, responses, token types, and the `act` and `may_act` claims.
- [RFC 6749: The OAuth 2.0 Authorization Framework](https://www.rfc-editor.org/rfc/rfc6749.html)
  Foundational specification defining the Client Credentials Grant (Section 4.4) and token endpoint error semantics (Section 5.2).
- [RFC 8707: Resource Indicators for OAuth 2.0](https://www.rfc-editor.org/rfc/rfc8707.html)
  Standard mechanism for specifying target service URIs to prevent token audience ambiguity in multi-service deployments.
- [RFC 9068: JSON Web Token (JWT) Profile for OAuth 2.0 Access Tokens](https://www.rfc-editor.org/rfc/rfc9068.html)
  Specifies the standard JWT access token profile, `typ: at+jwt` header, and subject claim assignments for M2M vs user flows.
- [RFC 9700: Best Current Practice for OAuth 2.0 Security](https://www.rfc-editor.org/rfc/rfc9700.html)
  Current security recommendations for token lifecycle, audience constraint, and mitigating token leakage.
- [SPIFFE JWT-SVID Specification](https://github.com/spiffe/spiffe/blob/main/standards/JWT-SVID.md)
  Production identity standard for microservice workload identity, providing the foundation for Dual-Token mesh architectures.
