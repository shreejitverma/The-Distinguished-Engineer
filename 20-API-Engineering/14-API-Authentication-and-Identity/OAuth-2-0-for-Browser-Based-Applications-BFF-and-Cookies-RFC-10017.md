---
type: pattern
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://www.rfc-editor.org/rfc/rfc10017.txt"
  - "https://www.rfc-editor.org/rfc/rfc9700.txt"
  - "https://www.rfc-editor.org/rfc/rfc7636.txt"
  - "https://www.rfc-editor.org/rfc/rfc6749.txt"
  - "https://www.rfc-editor.org/rfc/rfc9207.txt"
  - "https://www.rfc-editor.org/rfc/rfc7009.txt"
  - "https://www.rfc-editor.org/rfc/rfc9110.txt"
  - "https://www.rfc-editor.org/rfc/rfc9113.txt"
  - "https://www.rfc-editor.org/rfc/rfc9114.txt"
  - "https://www.ietf.org/archive/id/draft-ietf-httpbis-layered-cookies-02.txt"
  - "https://www.ietf.org/archive/id/draft-ietf-httpbis-rfc6265bis-22.txt"
  - "https://fetch.spec.whatwg.org/"
  - "https://github.com/encode/starlette/blob/1.10.0/starlette/middleware/sessions.py"
  - "https://github.com/encode/starlette/blob/1.10.0/starlette/responses.py"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2025-54576"
  - "https://circleci.com/blog/jan-4-2023-incident-report/"
  - "https://sec.okta.com/articles/2023/11/unauthorized-access-oktas-support-case-management-system-root-cause"
  - "https://blog.google/chromium/fighting-cookie-theft-using-device/"
  - "https://caniuse.com/mdn-http_headers_set-cookie_http_host-http_prefixes"
  - "https://raw.githubusercontent.com/mdn/browser-compat-data/main/http/headers/Set-Cookie.json"
  - "https://raw.githubusercontent.com/mdn/browser-compat-data/main/http/headers/Secure-Session-Registration.json"
  - "https://datatracker.ietf.org/doc/html/draft-ietf-httpbis-rfc6265bis-22"
  - "https://owasp.org/www-community/attacks/Session_fixation"
---

# OAuth 2.0 for Browser-Based Applications (RFC 10017, August 2026): Backend-for-Frontend (BFF) Pattern vs In-Browser Token Storage

> [!summary] Claim
> Once attacker JavaScript runs in your origin, no browser storage protects OAuth tokens, because the attacker can simply run a fresh silent authorization code flow; RFC 10017 therefore ranks the confidential-client BFF first, which keeps every token on the server behind a `Secure; HttpOnly; SameSite=Strict` `__Host-Http-` cookie and leaves the attacker only the ability to proxy requests through the victim's open tab.

Part of [API Authentication and Identity](README.md) in [API Engineering](../README.md).

## TL;DR

Mechanism: the BFF is a confidential OAuth client that runs the authorization code flow with PKCE, stores access and refresh tokens in its session, and forwards allowlisted API calls with `Authorization: Bearer`, so the browser only ever holds an opaque session cookie `(RFC 10017 Section 6.1)`.
Contract rule: the BFF MUST be a confidential client, MUST set `Secure` and `HttpOnly`, SHOULD use `SameSite=Strict`, `Path=/`, no `Domain`, and a `__Host-Http-` name, MUST implement a CSRF defense, and MUST proxy only to an explicit allowlist of hosts and paths `(RFC 10017 Sections 6.1.3.1, 6.1.3.2, 6.1.3.3, 6.1.3.6)`.
The trap: the login transaction cookie cannot be `SameSite=Strict`, because the authorization server's redirect back to the callback is a cross-site navigation that never carries `Strict` cookies; the lab's `test_a_strict_txn_cookie_breaks_the_cross_site_callback` shows the login failing with `400` before any token request.
Portability fact: the pattern is pure HTTP semantics, but `__Host-Http-` is enforced only by Chrome and Edge 140+ and Firefox 143+ and not by Safari (caniuse, as of 2026-11), and Starlette 1.10.0 raises `ValueError` for `partitioned=True` below Python 3.14; the lab's 46 tests passed on CPython 3.12.13, 3.13.18, 3.14.6, 3.14.10, and 3.15.2.
When not to use it: for a first-party single-domain app whose frontend and API share an origin, drop OAuth between them and use a plain server-side cookie session `(RFC 10017 Section 7.1)`; when even a BFF is impossible, a token-mediating backend is second best, and a pure browser client is "not recommended for business applications, sensitive applications, and applications that handle personal data" `(RFC 10017 Section 6.3.4.3)`.

## Mental model

```mermaid
sequenceDiagram
    participant B as Browser (SPA)
    participant F as BFF (confidential client)
    participant A as Authorization server
    participant R as Resource server
    B->>F: GET /bff/session + X-BFF-CSRF: 1 (B)
    F-->>B: {"authenticated": false}
    B->>F: navigate GET /bff/login (C)
    F-->>B: 302 to /authorize (state, S256 challenge)<br/>Set-Cookie __Host-Http-bff-txn; SameSite=Lax
    B->>A: GET /authorize (D)
    A-->>B: 302 to /bff/callback?code&state&iss
    B->>F: cross-site top-level GET /bff/callback (E)<br/>Lax txn cookie is sent, Strict cookies are not
    F->>A: POST /token, Basic client auth + code_verifier (F)
    A-->>F: access_token, refresh_token
    F-->>B: 303 to app<br/>Set-Cookie __Host-Http-bff-session; SameSite=Strict (G)
    B->>F: GET /bff/api/orders/42 + cookie + X-BFF-CSRF: 1 (J)
    F->>R: GET /orders/42, Authorization: Bearer, no Cookie (K)
    R-->>F: 200
    F-->>B: 200, upstream Set-Cookie dropped (L)
```

The letters are the step labels of RFC 10017 Figure 1.
The single-page app first asks the BFF whether a session exists (B); if not, it navigates the whole window to the BFF's login endpoint (C), because only a navigation can carry the user to the authorization server and back.
The BFF answers with a redirect to the authorization endpoint (D) and parks the PKCE verifier and `state` server-side under a short-lived transaction cookie.
The authorization server's redirect back (E) is a cross-site top-level `GET`, so only `Lax` or `None` cookies ride along; that is why the transaction cookie is `Lax` while the session cookie, set on the response to that same navigation (G), can be `Strict`.
The code exchange (F) is a back-channel `POST` authenticated with the BFF's client secret, which is what defeats an attacker who steals an authorization code from the browser.
Every later API call (J) carries the session cookie plus a static custom header, and the BFF translates it into a bearer token on the way out (K), stripping the cookie and filtering the response (L).

## How it works

### The threat model that orders the three patterns

RFC 10017 starts from the observation that, even with output encoding, SRI, and a nonce- or hash-based CSP, "there is a risk that the attacker might find a way to trigger the execution of malicious JavaScript", and that such code "has the same privileges as the legitimate application code" `(RFC 10017 Section 5)`.
It names four attack scenarios `(RFC 10017 Section 5.1)`:

| Scenario | What the attacker does | Section |
|---|---|---|
| Single-execution token theft | reads tokens from storage once and exfiltrates them | 5.1.1 |
| Persistent token theft | re-steals on a timer (the RFC's example is every 10 seconds), so rotation and short lifetimes stop helping | 5.1.2 |
| Acquisition and extraction of new tokens | injects a hidden iframe, runs a silent authorization code flow on the user's existing AS session, and reads the code from the same-origin frame | 5.1.3 |
| Proxying requests via the user's browser | sends API calls from the victim's tab, letting the app's own plumbing attach credentials | 5.1.4 |

The third scenario is the one that decides the architecture.
For a public browser client, "the security of public browser-based OAuth clients relies entirely on the redirect URI and application's origin" `(RFC 10017 Section 5.1.3)`, and the attacker controls both from inside the page.
So "even if the application finds a token storage mechanism that is able to completely isolate the stored tokens from the attacker, the attacker will still be able to request a new set of tokens" `(RFC 10017 Section 5.1.3)`.
DPoP does not help either: the attacker binds the fresh tokens to its own key pair `(RFC 10017 Section 5.1.3, Section 6.3.4.2.2)`.
Persistent theft also defeats refresh token rotation, because the attacker can make sure the legitimate app never presents the latest refresh token (clearing it, or waiting until the tab closes), so no reuse is ever detected `(RFC 10017 Section 5.1.2)`.

The consequences are ranked by severity: stolen refresh tokens give long-term impersonation (often valid "for multiple hours or even days"), stolen access tokens give short-term but unrestricted use with any method, URL, header, or body, and client hijacking is bounded by the app's own CORS and backend policies `(RFC 10017 Sections 5.2.1, 5.2.2, 5.2.3)`.

Which scenarios each architecture still allows `(RFC 10017 Sections 6.1.4, 6.2.4, 6.3.4, 7.1.1, 7.2.2, 7.4.1)`:

| Architecture | 5.1.1 theft | 5.1.2 persistent | 5.1.3 new flow | 5.1.4 proxying | RFC verdict |
|---|---|---|---|---|---|
| BFF (6.1) | mitigated | mitigated | mitigated (confidential client) | possible | strongly recommended for business and personal-data apps |
| Token-mediating backend (6.2) | access token only | access token only | mitigated | possible | only when a full BFF is impossible |
| Browser-based OAuth client (6.3) | possible | possible | possible | possible | not recommended for business or personal-data apps |
| Service Worker OAuth flow (7.4) | mitigated | mitigated | possible (unregister, then a new frame has no worker) | possible | not recommended |
| Implicit grant (7.2) | possible | possible | possible | possible | MUST NOT be used |
| Same-domain app without OAuth (7.1) | n/a | n/a | n/a | possible | listed under Section 7's discouraged patterns, yet 7.1 says OAuth between frontend and backend "is often not needed" |

The BFF does not stop scenario 5.1.4, and nothing at the application layer can: "this scenario cannot be stopped or prevented by application-level security measures" `(RFC 10017 Section 5.1.4)`.
What the BFF guarantees is that OAuth adds no attack surface beyond that baseline: "The only viable attack pattern is hijacking the client application in the user's browser" `(RFC 10017 Section 6.1.4.3)`.

### The BFF's three responsibilities

RFC 10017 Section 6.1 defines the BFF by three duties: it is a confidential client, it keeps access and refresh tokens "in the context of a cookie-based session", and it forwards all resource-server requests with the right access token.
It is "a component of the frontend application", not an API gateway, which matters for ownership (see The principal's view) and for naming: the vault's [[Backend-for-Frontend-BFF-Aggregation-Fan-Out-and-Partial-Degradation]] note covers the older, unrelated meaning of BFF as a per-client aggregation layer.

Endpoints `(RFC 10017 Section 6.1.2.1)`:
- check session (B, I): an API call that says whether a session exists and may return identity information, never tokens;
- login (C): reached by navigation, answers with a redirect to the authorization endpoint;
- callback (E): reached by navigation from the authorization server, exchanges the code, then redirects to the app;
- logout: optional, behavior is application-specific.

The RFC allows the check-session call to return an authorization URI instead of a separate login endpoint, but notes that "this scenario requires a custom implementation and makes it harder to use standard OAuth libraries" `(RFC 10017 Section 6.1.2.1)`.

### Front channel: authorization code, PKCE, `state`, and `iss`

The BFF MUST use the authorization code grant as profiled in RFC 9700 Section 2.1.1 `(RFC 10017 Section 6.1.3.1)`.
For confidential clients RFC 9700 makes PKCE RECOMMENDED rather than mandatory, but requires the challenge or nonce to be "transaction-specific and securely bound to the client and the user agent in which the transaction was started", and names `S256` as the only method that does not expose the verifier `(RFC 9700 Section 2.1.1)`.
The verifier is 43 to 128 characters from `[A-Z] / [a-z] / [0-9] / "-" / "." / "_" / "~"`, and 32 random octets base64url-encoded give exactly 43 `(RFC 7636 Section 4.1)`; the challenge is `BASE64URL(SHA256(ASCII(code_verifier)))` `(RFC 7636 Section 4.2)`, and the lab reproduces the Appendix B vector `dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk` -> `E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM`.
Clients that know their authorization server enforces PKCE MAY rely on it for CSRF protection of the redirect endpoint; otherwise one-time `state` values "securely bound to the user agent" MUST be used `(RFC 9700 Section 2.1)`; the lab does both and compares `state` with `hmac.compare_digest`.
With more than one authorization server, a mix-up defense is REQUIRED, preferably the `iss` response parameter `(RFC 9700 Section 2.1, Section 4.4.2.1)`, compared by simple string comparison, with the response rejected and the grant abandoned on mismatch `(RFC 9207 Section 2.4)`; the lab aborts before any token request.
Login's `return_to` must be a local path, because "Clients and authorization servers MUST NOT expose URLs that forward the user's browser to arbitrary URIs obtained from a query parameter" `(RFC 9700 Section 2.1)`.

The wire bytes of the lab's login step, captured from one run:

```text
HTTP 302
location: https://as.example/authorize?response_type=code&client_id=spa-bff&redirect_uri=https%3A%2F%2Fbff.example%2Fbff%2Fcallback&scope=openid+orders%3Aread+orders%3Awrite&state=9y8ywvrDlkZqr_Egs9N2_rfRUtm34B5Uq3cF7yNNSnQ&code_challenge=yOmkC7H0HHDz_qmAKgFReHQSipur815IORHwJsmuWEE&code_challenge_method=S256
set-cookie: __Host-Http-bff-txn=2zFfjsxW82TrFbkOMe_MwoC4-vY2kLOkjNdn12j8koE; HttpOnly; Max-Age=600; Path=/; SameSite=lax; Secure
```

(Captured 2026-11-15 by driving `build_lab()` through `SimulatedBrowser` on macOS 27.3 arm64, CPython 3.14.6; the random values differ on every run.)

### Back channel: client authentication and the code exchange

The confidential client is what defeats scenario 5.1.3: "Even when the attacker manages to obtain an authorization code, they are prevented from exchanging this code due to the lack of client credentials" `(RFC 10017 Section 6.1.4.2)`.
The lab uses `client_secret_basic`; RFC 6749 requires each half to be form-urlencoded before Base64 (`Basic czZCaGRSa3F0Mzo3RmpmcDBaQnIxS3REUmJuZlZkbUl3` is its example) `(RFC 6749 Section 2.3.1)`, which `httpx`'s tuple `auth=` does not do, because it joins the raw bytes with `:` and Base64-encodes them (httpx 0.28.1 httpx/_auth.py, BasicAuth._build_auth_header), so the lab builds the header itself in `basic_client_auth`.
The BFF runs in a controlled server environment, so "key-based client authentication and sender-constrained tokens" become practical there `(RFC 10017 Section 6.1.3.7)`; see [[Sender-Constrained-Tokens-DPoP-RFC-9449-and-mTLS-Binding-RFC-8705]].
With OpenID Connect the BFF requests `openid`, receives an ID Token in (F), and may call UserInfo with the session's access token `(RFC 10017 Section 6.1.2.4)`; the lab calls a UserInfo stand-in to learn `sub`, and full ID Token validation belongs to [[OpenID-Connect-OIDC-ID-Tokens-vs-Access-Tokens-and-Metadata-Discovery]].

### The session cookie, attribute by attribute

| Attribute | RFC 10017 Section 6.1.3.2 | What it buys | Mechanism source |
|---|---|---|---|
| `Secure` | MUST | never sent over plaintext; a non-secure origin cannot set or overlay it | layered-cookies-02 Section 5.4.3 step 10, Section 5.4.5 |
| `HttpOnly` | MUST | `document.cookie` cannot read it, so XSS cannot escalate client hijacking to session hijacking | `(RFC 10017 Section 6.1)` |
| `SameSite=Strict` | SHOULD | withheld from every cross-site request, including top-level navigations | rfc6265bis-22 Section 5.6.7.1 |
| `Path=/` | SHOULD | one cookie for the whole host, required by the `__Host-` prefix | layered-cookies-02 Section 4.1.3.2 |
| no `Domain` | SHOULD NOT set | host-only, never sent to sibling subdomains | layered-cookies-02 Section 4.1.3.2 |
| `__Host-Http-` name | SHOULD | the server can trust that the cookie was set over HTTPS by a `Set-Cookie` header with all of the above | layered-cookies-02 Section 4.1.3.4 |

The RFC explains the SHOULDs as accommodations for existing deployments, and says that in "a typical BFF deployment scenario, there is no reason to use more relaxed cookie security settings" `(RFC 10017 Section 6.1.3.2)`.

Cookie name prefixes exist because "it is impossible for a server to have confidence that a given cookie was set with a particular set of attributes" `(draft-ietf-httpbis-layered-cookies-02 Section 4.1.3)`.
A user agent drops a `__Host-` cookie unless it has `Secure`, `Path=/`, and no `Domain`, and a `__Host-Http-` cookie additionally needs `HttpOnly` `(draft-ietf-httpbis-layered-cookies-02 Section 4.1.3.2, Section 4.1.3.4)`.
Prefix matching uses the byte-lowercased name, "to protect servers that process these values in a case-insensitive manner" `(draft-ietf-httpbis-layered-cookies-02 Section 5.4.3, steps 13-16)`, so `__host-http-SID` gets the same check, and a nameless cookie whose value starts with a prefix is rejected outright (step 17).
"Ports are the only piece of the same-origin policy that __Host- cookies continue to ignore" `(draft-ietf-httpbis-layered-cookies-02 Section 4.1.3.2)`.
The `__Http-` and `__Host-Http-` prefixes appear in layered-cookies-02 (May 2026) and not in rfc6265bis-22 (December 2025); RFC 10017 cites the former as `[COOKIES]`.
Note a likely editorial slip in layered-cookies-02: its "Http-prefix compatible" definition in Section 5.1.2.1 reads "_cookie_'s http-only is false", which contradicts the prose of Section 4.1.3.3 ("set with a Secure attribute, and an HttpOnly attribute"); the lab follows the prose, the only reading consistent with the prefix's stated purpose that the cookie "is limited in scope to HTTP requests" `(draft-ietf-httpbis-layered-cookies-02 Section 4.1.3.3)`.

Graceful degradation follows from prefix matching: a browser that knows `__Host-` but not `__Host-Http-` still sees a name starting with `__Host-`, so it still enforces `Secure`, `Path=/`, and no `Domain`; only the `HttpOnly` guarantee is lost on such browsers, and the BFF sets `HttpOnly` anyway.

The captured response to the callback:

```text
HTTP 303
location: /orders
set-cookie: __Host-Http-bff-session=joIRIGolm7lsPiCdiyF1A1WWqXQUuYfJj5Qc0SbqRIU; HttpOnly; Max-Age=28800; Path=/; SameSite=strict; Secure
set-cookie: __Host-Http-bff-txn=""; expires=Sun, 15 Nov 2026 07:43:23 GMT; HttpOnly; Max-Age=0; Path=/; SameSite=lax; Secure
```

Two `Set-Cookie` field lines are deliberate: `Set-Cookie` "cannot be combined into a single field value" and is the well-known exception to the one-line-per-field-name rule `(RFC 9110 Section 5.3)`.

### Why the transaction cookie is `Lax` and the session cookie is `Strict`

`Strict` cookies "will not be sent along with top-level navigations which are triggered from a cross-site document context", while `Lax` carves out cross-site top-level navigations "if and only if" they use a safe method `(draft-ietf-httpbis-rfc6265bis-22 Section 5.6.7.1)`.
The callback (E) is exactly such a navigation, initiated by the authorization server's page, so a `Strict` transaction cookie is absent there and the BFF cannot find its `state` and verifier.
Creation is different from delivery: `Lax` and `Strict` cookies "can be set along with any top-level navigation, cross-site or otherwise" `(draft-ietf-httpbis-rfc6265bis-22 Section 4.1.2.7)`, so the response to the callback may set the `Strict` session cookie.
If the authorization server uses `response_mode=form_post`, the callback is a cross-site top-level `POST`, which `Lax` does not cover either; such a deployment needs `SameSite=None; Secure` on the transaction cookie, and the lab's `samesite_allows` model shows the `POST` case returning `False`.
The 303 to the app continues a navigation that the authorization server's page initiated, and RFC 6265bis-22 decides same-site status from the request's client's "site for cookies" `(draft-ietf-httpbis-rfc6265bis-22 Section 5.2)`, so do not assume that the redirected document request carries the `Strict` cookie; the design does not need it to, because the app shell is static (step H) and the check-session `fetch` that follows (step I) is a same-site request from the app's own document `(RFC 10017 Section 6.1.1)`.
The usability cost of `Strict` is that a user following an emailed deep link arrives without the session cookie on that first navigation; RFC 6265bis-22 suggests a two-cookie design (a `Lax` "read" cookie and a `Strict` "write" cookie) for that case `(draft-ietf-httpbis-rfc6265bis-22 Section 8.8.2)`, and for a BFF the page load itself is static, so the first API call (a same-site `fetch`) carries the `Strict` cookie normally.
Browsers may apply "Lax-allowing-unsafe" only to cookies without an explicit `SameSite`, and deployment experience suggests limiting it to cookies "2 minutes or less" old `(draft-ietf-httpbis-rfc6265bis-22 Section 5.6.7.2)`; always set the attribute explicitly.

### Where the tokens live: server-side versus client-side sessions

RFC 10017 does not mandate a storage model `(RFC 10017 Section 6.1.2.3)`.
Server-side sessions expose only an identifier and allow revocation "at will", but the RFC says they need sticky sessions or replication and are "only recommended in small-scale scenarios".
Client-side sessions push all data to the browser "in a signed, and optionally encrypted, object"; for a BFF their usual weakness (no revocation) mostly disappears, because "It suffices to revoke the user's access token and/or refresh token to prevent ongoing access".
If the cookie carries tokens, the BFF "SHOULD encrypt its cookie contents" so tokens never sit in the browser's cookie database in plaintext, while noting that "encrypting the cookie contents prevents direct access to embedded tokens, it still allows the attacker to use the encrypted cookie in a session hijacking attack" `(RFC 10017 Section 6.1.3.2)`.
A token-carrying client-side cookie also has to fit the user agent's per-cookie limit: name plus value longer than 4096 octets is a parse failure `(draft-ietf-httpbis-layered-cookies-02 Section 5.4.2, step 12)`, and JWT access tokens plus refresh tokens plus AEAD overhead can approach it, which forces chunking into several cookies.

The lab chooses an encrypted server-side store (the seed's plan) and makes the server-side rows useless on their own:
- rows are keyed by `SHA-256(session id)`, so a database dump does not contain any valid cookie value;
- each row is `kid`, `.`, a 12-byte nonce, and the AES-256-GCM ciphertext plus 16-byte tag; one captured row for a full token set was 278 bytes;
- the lookup key is the AEAD associated data, so swapping two rows makes both fail authentication instead of handing Alice's tokens to Bob;
- the key id prefix lets old rows decrypt during a key rotation while new writes use the active key.

Do not reach for Starlette's stock `SessionMiddleware` for this: it base64-encodes the JSON and signs it with `itsdangerous.TimestampSigner`, so anyone holding the cookie can read every value in it; it also defaults to `session_cookie="session"`, `max_age` of 14 days, `same_site="lax"`, and `https_only=False`, which omits `Secure` (it does always emit `httponly`) (starlette 1.10.0 starlette/middleware/sessions.py, SessionMiddleware.__init__ and SessionMiddleware.__call__).
The lab's `test_starlette_session_middleware_signs_but_does_not_encrypt` decodes a token straight out of such a cookie.

### Refresh, session lifetime, and concurrency

The BFF refreshes inline, between (J) and (K), authenticating as a confidential client `(RFC 10017 Section 6.1.2.2)`.
It should set the session lifetime equal to the refresh token's maximum lifetime and drop the session when it learns the refresh token is dead `(RFC 10017 Section 6.1.2.2)`; the lab sets `Max-Age=28800` and deletes the store row and the cookie on `invalid_grant`.
Refresh token rotation is the authorization server's job: it "issues a new refresh token with every access token refresh response", keeps the relationship, and on replay of an invalidated token "will revoke the active refresh token" `(RFC 9700 Section 4.14.2)`.
RFC 9700 mandates rotation or sender-constraining only for public clients; for a confidential client such as the BFF, RFC 6749 already binds refresh tokens to the client that authenticates, so rotation is authorization-server policy rather than a requirement `(RFC 9700 Section 4.14.2)`.
For browser clients, a rotated refresh token MUST NOT outlive the initial one; the RFC's worked example is a 10-minute access token, an 8-hour refresh token, and a 7-hour-50-minute refresh token after the first refresh `(RFC 10017 Section 6.3.2.3)`, which the lab's authorization server reproduces.

Rotation plus a multi-request frontend creates a race that RFC 10017 does not discuss: if two API calls arrive with an expired access token and both refresh with the same refresh token, the second presentation looks exactly like a replay and the authorization server revokes the whole family.
The lab serializes refresh per session with an `asyncio.Lock` and re-reads the store after acquiring it (single flight); `test_concurrent_calls_refresh_once_and_never_trip_reuse_detection` fires five concurrent calls and asserts one refresh grant and zero reuse detections.
An `asyncio.Lock` is per process: with N BFF replicas behind a load balancer, the same race reappears across replicas unless the store provides an atomic compare-and-set or lease, or the authorization server tolerates a short reuse grace window (a vendor-specific relaxation of RFC 9700, so check your AS).

### The proxy: CSRF defense and outbound restrictions

Every BFF endpoint is cookie-authenticated, so "The BFF MUST implement a proper CSRF defense" `(RFC 10017 Section 6.1.3.3)`.
`SameSite=Strict` alone is enough only if nothing else shares the site (eTLD+1); a takeover of `b.example.com` makes CSRF against `a.example.com`'s BFF possible because they are same-site though cross-origin `(RFC 10017 Section 6.1.3.3.1)`.
CORS is a CSRF defense only for requests that need a preflight; a CORS-safelisted request (method `GET`, `HEAD`, or `POST` with only safelisted headers) is sent without one, so the RFC says the BFF SHOULD require a static custom header and then MUST check it on every request `(RFC 10017 Section 6.1.3.3.2)`.
In the Fetch Standard, any header outside the safelist (`accept`, `accept-language`, `content-language`, `content-type` with three MIME types, and `range`) makes a request non-safelisted, as does any safelisted value over 128 bytes or a safelisted total over 1024 bytes (Fetch Standard, CORS-safelisted request-header, as of 2026-11).
The lab requires `X-BFF-CSRF: 1` on `/bff/session`, `/bff/logout`, and `/bff/api/*`, and rejects a missing header with `403` before any upstream call; login and callback are navigations and are protected by `state`, PKCE, and the transaction cookie instead.
Double-submit anti-forgery cookies are an acceptable alternative where the framework provides them, "not necessarily recommended over the CORS approach" `(RFC 10017 Section 6.1.3.3.3)`; the vault's [[CSRF-in-Cookie-Authenticated-APIs-SameSite-Double-Submit-and-Fetch-Metadata]] compares the mechanisms in depth.

The proxy is the BFF's sharpest edge: an attacker who can steer the outbound URL receives the user's access token `(RFC 10017 Section 6.1.3.6)`.
The BFF MUST keep "an explicit allowlist of approved resource servers", MUST apply strict validation to dynamic path routing, and the RFC adds (without a normative keyword) that restricting methods per endpoint further reduces attack vectors `(RFC 10017 Section 6.1.3.6)`.
The lab's `ProxyRoute` table holds a compiled pattern, an allowed method set, and an upstream template; matching uses `re.fullmatch` on the decoded path only (the query string never reaches the matcher), so `orders/1@evil.example`, `orders%2F..%2Fadmin`, a 13-digit id, and `admin?next=orders` all return `404` with zero upstream requests.
The `admin?next=orders` case would be forwarded only by a matcher that applied an unanchored pattern to path plus query; the anchoring itself is what the `orders/1@evil.example` and 13-digit cases exercise.
Matching the query string too was the root cause of oauth2-proxy's CVE-2025-54576 (see Pitfalls).
Outbound, the BFF forwards only `accept` and `content-type`, never `Cookie`, and adds `Authorization: Bearer`; inbound, it copies only the status, body, and `content-type`, so a resource server's `Set-Cookie` cannot land on the BFF's origin.
Because the BFF sees every request, it is "the ideal place" for anomaly detection and rate limiting, but the resource server must know it exists, or per-IP rate limits will throttle all users behind the BFF's address `(RFC 10017 Sections 6.1.4.1, 6.1.3.5)`.

### Logout

The lab revokes the refresh token at the RFC 7009 endpoint (the server answers `200` "if the token has been revoked successfully or if the client submitted an invalid token" `(RFC 7009 Section 2.2)`), deletes the row, and clears the cookie.
Clearing a prefixed cookie is itself a `Set-Cookie` and must satisfy the prefix: Starlette's `delete_cookie(SESSION_COOKIE)` with defaults emits `__Host-Http-bff-session=""; expires=<now>; Max-Age=0; Path=/; SameSite=lax` without `Secure` or `HttpOnly` (captured 2026-11-15), which a conforming browser drops, leaving the old cookie in place (starlette 1.10.0 starlette/responses.py, Response.delete_cookie and Response.set_cookie); `test_starlette_delete_cookie_defaults_fail_the_host_http_prefix` proves it.

### The other two patterns and browser storage

A token-mediating backend is a confidential client that hands the access token to the browser and keeps only the refresh token `(RFC 10017 Section 6.2)`.
It stops refresh-token theft and the new-flow attack, but access tokens remain stealable `(RFC 10017 Section 6.2.4.1)`, sender-constraining them with DPoP is "out of the scope" because the key holder and the token requester would differ `(RFC 10017 Section 6.2.4.3.2)`, and a cached token with a superset of the requested scopes SHOULD NOT be returned `(RFC 10017 Section 6.2.2.3)`, which the lab encodes as `cached_token_reusable` (equal scope sets only).

A browser-based client is a public client: PKCE MUST be used and enforced, shared secrets MUST NOT be required because they serve "no value beyond client identification", exact redirect URI matching MUST be used, `postMessage` origins MUST be verified, and the authorization and resource servers MUST send CORS headers `(RFC 10017 Sections 6.3.2.1, 6.3.3.1, 6.3.3.2.1, 6.3.3.3, 6.3.3.4)`.
Its refresh tokens MUST be rotated or sender-constrained, MUST have a maximum or idle lifetime, and the authorization server SHOULD tie them to the user's AS session for single-logout behavior `(RFC 10017 Section 6.3.2.3)`.

Storage options for clients that do hold tokens `(RFC 10017 Section 8)`:
- JavaScript-set cookies are NOT RECOMMENDED for tokens, because the browser also sends them to the app's server on every request `(Section 8.1)`;
- a Service Worker isolates tokens but MUST NOT persist them in IndexedDB, which the main window shares, so it has no isolated persistent storage `(Section 8.2)`;
- a Web Worker can hold the refresh token in memory and hand out access tokens `(Section 8.3)`;
- in-memory closures fall to prototype poisoning of the functions they call `(Section 8.4)`;
- `localStorage` is origin-wide, persistent, and synchronous; `sessionStorage` is per tab; IndexedDB is shared with workers `(Section 8.5)`;
- none of them is guaranteed encrypted at rest, and a WebCrypto non-exportable key gives no guarantee about how the OS stores it `(Section 8.6)`.
None of these protects against scenario 5.1.3, which is the point of the whole document.

## Numbers and limits

| Quantity | Value | Source |
|---|---|---|
| PKCE `code_verifier` length | 43 to 128 unreserved characters; 32 random octets -> 43 | `(RFC 7636 Section 4.1)`; lab `test_code_verifier_is_43_unreserved_characters` |
| Example lifetimes in RFC 10017 | access 10 min, refresh 8 h absolute, 7 h 50 min left after the first rotation | `(RFC 10017 Section 6.3.2.3)`; lab `test_rotation_never_extends_the_absolute_refresh_lifetime` |
| Persistent theft polling interval used as example | 10 s | `(RFC 10017 Section 5.1.2)` |
| Cookie name + value | parse failure above 4096 octets | `(draft-ietf-httpbis-layered-cookies-02 Section 5.4.2, step 12)` |
| Cookie attribute value | ignored above 1024 octets | `(draft-ietf-httpbis-layered-cookies-02 Section 5.4.2, step 15.12)` |
| Cookies per host / total | SHOULD be >= 50 / >= 3000 | `(draft-ietf-httpbis-layered-cookies-02 Section 5.1.1)` |
| Cookie age limit | SHOULD be 400 days or less | `(draft-ietf-httpbis-layered-cookies-02 Section 5.1.1)` |
| Lax-allowing-unsafe window | 2 minutes or less, cookies without explicit `SameSite` only | `(draft-ietf-httpbis-rfc6265bis-22 Section 5.6.7.2)` |
| CORS-safelisted header value / total | 128 bytes / 1024 bytes | Fetch Standard, CORS-safelisted request-header (as of 2026-11) |
| Starlette `SessionMiddleware` default `max_age` | 1209600 s (14 days), `Secure` off | starlette 1.10.0 source; lab asserts `max-age` `1209600` |
| Lab session cookie | 43-character id from 32 random octets, `Max-Age=28800` | captured run, macOS 27.3 arm64, CPython 3.14.6 |
| Lab encrypted row for one token set | 278 bytes (2-byte kid + 1 + 12 nonce + JSON + 16 tag) | captured run, same machine, re-measured 2026-11-15 |
| `__Host-Http-` browser support | Chrome and Edge 140+, Firefox 143+ (142 partial, as `__HostHttp-`), Safari not supported | MDN browser-compat-data `http_host-http_prefixes`, which caniuse mirrors (as of 2026-11) |
| CVE-2025-54576 (oauth2-proxy) | CVSS 3.1 base 9.1 (GitHub CNA score, shown by NVD), versions <= 7.10.0, fixed in 7.11.0 | NVD CVE-2025-54576 |
| Okta support-system breach | files associated with 134 customers accessed, some HAR files with session tokens; 5 customers' sessions hijacked | Okta root cause report, 2023-11-03 |
| Lab run time | `46 passed in 0.71s` | `uv run --frozen pytest <test> -q` from `labs/`, CPython 3.14.6, macOS 27.3 arm64, Darwin 27.3.0 (verifier's run, 2026-11-15) |

## Portability

| Runtime, library, or layer | Behavior | Evidence |
|---|---|---|
| CPython 3.12.13 (macOS) | 46 passed; `set_cookie(partitioned=True)` raises `ValueError` | drafter's lab run 2026-11-15, `UV_PROJECT_ENVIRONMENT=<tmp> uv run --frozen --python 3.12.13 pytest <test> -q`; not re-run in verification (matrix paused); the `ValueError` follows from the `sys.version_info < (3, 14)` guard in Starlette 1.10.0 |
| CPython 3.13.18 (macOS) | 46 passed; `partitioned=True` raises `ValueError` | drafter's lab run, Homebrew 3.13.18, full `uv.lock` env, same command; not re-run in verification |
| CPython 3.14.6 (workspace `.venv`) | 46 passed; `Partitioned` emitted | lab, `uv run --frozen pytest <test> -q`, re-run by the verifier 2026-11-15 |
| CPython 3.14.10 (macOS) | 46 passed | drafter's lab run, Homebrew 3.14.10, full `uv.lock` env; not re-run in verification |
| CPython 3.15.2 (macOS) | 46 passed | drafter's lab run, Homebrew 3.15.2, full `uv.lock` env; not re-run in verification |
| CPython 3.12.16 | not run: `uv` here provides 3.12.13 only | none |
| CPython 3.15.2t free-threaded | not run: `uv` offered 3.15.0b3t, and `uv sync --frozen` of the lock failed because `psycopg-binary==3.3.8` has no wheel or sdist for that ABI | attempted 2026-11-15 |
| Debian Trixie and Alpine containers | not run (containers paused for this note) | none |
| Starlette 1.10.0 `Response.set_cookie` | `partitioned=True` raises below 3.14 (`sys.version_info < (3, 14)` guard); defaults `secure=False`, `httponly=False`, `samesite="lax"` | starlette 1.10.0 starlette/responses.py, Response.set_cookie; lab `test_starlette_partitioned_cookie_needs_python_3_14` |
| FastAPI 0.162.0 + httpx 0.28.1 `ASGITransport` | drives the BFF in-process over `https://bff.example`, with `httpx.MockTransport` standing in for the AS and resource server | every async test |
| Uvicorn 0.54.0, Hypercorn 0.18.0, Granian 2.8.7 | not exercised; the BFF logic is application-level, but how each server emits several `Set-Cookie` lines on the wire was not tested here | none |
| HTTP/1.1, HTTP/2, HTTP/3 | identical cookie semantics; HTTP/2 and HTTP/3 MAY split `Cookie` into several field lines that MUST be rejoined with `"; "` before reaching an HTTP/1.1 hop or application | `(RFC 9113 Section 8.2.3)`, `(RFC 9114 Section 4.2.1)` |
| Browsers and `__Host-Http-` | enforced by Chrome and Edge 140+ and Firefox 143+; Safari (which has supported `__Host-` since 13) treats it as a `__Host-` cookie | MDN browser-compat-data `Set-Cookie` entries `http_host-http_prefixes` and `host_secure_prefixes` (as of 2026-11); degradation follows from prefix matching in layered-cookies-02 Section 5.4.3 |

## Worked example

The [implementation](../labs/src/api_labs/f14_api_authentication_and_identity/bff_token_mediation.py) contains the BFF (`Bff`), an encrypted store (`EncryptedSessionStore`), an in-process authorization server with PKCE, client authentication, single-use codes, rotation with family revocation, and RFC 7009 revocation (`AuthorizationServer`), a resource server and router (`Upstreams`), and a cookie-jar model that enforces name prefixes and `SameSite` (`SimulatedBrowser`).
The [test](../labs/tests/f14_api_authentication_and_identity/test_oauth_2_0_for_browser_based_applications_bff_and_cookies_rfc_10017.py) has 46 cases covering the RFC 7636 vector, the layered-cookies-02 prefix examples, the `Lax` versus `Strict` callback, `state` and `iss` checks, open-redirect refusal, session fixation, proxy allowlisting, CSRF, refresh, rotation, logout, and three stock-Starlette behaviors.

Simplified excerpt of the implementation (verbatim lines 548-586 of `bff_token_mediation.py`, the single-flight refresh):

```python
    async def fresh_tokens(self, session_id: str) -> TokenSet | None:
        """Return usable tokens, refreshing inline at most once per session at a time."""
        tokens = self.store.get(session_id)
        if tokens is None or self.now() >= tokens.session_expires_at:
            self.store.delete(session_id)
            return None
        if self.now() < tokens.access_expires_at - self.settings.refresh_skew:
            return tokens
        lock = self.locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            tokens = self.store.get(session_id)  # another request may have refreshed meanwhile
            if tokens is None:
                return None
            if self.now() < tokens.access_expires_at - self.settings.refresh_skew:
                return tokens
            response = await self.upstream.post(
                self.settings.token_endpoint,
                content=urlencode(
                    {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token}
                ),
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    **self._client_auth(),
                },
            )
            if response.status_code != 200:
                # RFC 10017 Section 6.1.2.2: a dead refresh token ends the session.
                self.store.delete(session_id)
                self.locks.pop(session_id, None)
                return None
            body = response.json()
            refreshed = replace(
                tokens,
                access_token=str(body["access_token"]),
                refresh_token=str(body.get("refresh_token", tokens.refresh_token)),
                access_expires_at=self.now() + float(body["expires_in"]),
            )
            self.store.put(session_id, refreshed)
            return refreshed
```

Simplified excerpt (verbatim lines 706-733, the proxy handler inside `Bff._build_app`):

```python
        @app.api_route("/bff/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
        async def proxy(request: Request, path: str) -> Response:
            if request.headers.get(CSRF_HEADER) != "1":
                return JSONResponse({"error": "missing CSRF header"}, status_code=403)
            route, match = self._route(path)
            if route is None or match is None:
                return JSONResponse({"error": "no such route"}, status_code=404)
            if request.method not in route.methods:
                return JSONResponse({"error": "method not allowed"}, status_code=405)
            session_id = request.cookies.get(SESSION_COOKIE, "")
            tokens = await self.fresh_tokens(session_id)
            if tokens is None:
                response: Response = JSONResponse({"error": "login required"}, status_code=401)
                self._clear_session_cookie(response)
                return response
            headers = {k: v for k in FORWARDED_REQUEST_HEADERS if (v := request.headers.get(k))}
            headers["authorization"] = f"Bearer {tokens.access_token}"  # cookie is not forwarded
            upstream = await self.upstream.request(
                request.method,
                route.upstream.format(**match.groupdict()),
                params=list(request.query_params.multi_items()),
                headers=headers,
                content=await request.body(),
            )
            media_type = upstream.headers.get("content-type")
            return Response(
                upstream.content, status_code=upstream.status_code, media_type=media_type
            )
```

Simplified excerpt of the test (verbatim lines 324-333 of the test module; the `@pytest.mark.unit` and `@pytest.mark.asyncio` lines above it are omitted):

```python
async def test_concurrent_calls_refresh_once_and_never_trip_reuse_detection() -> None:
    async with lab_browser() as (lab, browser):
        await login(lab, browser)
        lab.clock.advance(600)
        responses = await asyncio.gather(
            *(browser.request("GET", "/bff/api/orders", headers=CSRF) for _ in range(5))
        )
        assert [r.status_code for r in responses] == [200] * 5
        assert lab.auth.grants.count("refresh_token") == 1
        assert lab.auth.reuse_detected == 0
```

Run from `labs/` on macOS 27.3 arm64 (Darwin 27.3.0) with CPython 3.14.6, 2026-11-15:

```text
$ uv run --frozen pytest tests/f14_api_authentication_and_identity/test_oauth_2_0_for_browser_based_applications_bff_and_cookies_rfc_10017.py -q
..............................................                           [100%]
46 passed in 0.71s
```

The check-session body the browser sees after login, captured from one run, contains identity but no token:

```text
{"authenticated":true,"sub":"alice","scope":"openid orders:read orders:write"}
```

## Pitfalls

> [!warning] A `Strict` login-transaction cookie
> Trigger: the BFF stores `state` and the PKCE verifier under a cookie that copies the session cookie's `SameSite=Strict`.
> Symptom: every login fails at the callback with "no login transaction" (or the framework's correlation-failed error), because the authorization server's redirect is a cross-site navigation `(draft-ietf-httpbis-rfc6265bis-22 Section 5.6.7.1)`.
> Fix: make the transaction cookie `Lax` (or `None; Secure` for `form_post`), short-lived, `__Host-Http-`, and single-use; keep only the session cookie `Strict`; lab `test_a_strict_txn_cookie_breaks_the_cross_site_callback`.

> [!warning] Regex route matching against the full URI (CVE-2025-54576)
> Trigger: an authenticating proxy evaluates its route patterns against path plus query string.
> Symptom: in oauth2-proxy 7.10.0 and earlier, "Attackers can bypass authentication by crafting URLs with query parameters that satisfy configured regex patterns" in `skip_auth_routes`, because they matched "against the full request URI"; the GitHub CNA's CVSS 3.1 base score shown by NVD is 9.1, fixed in 7.11.0 (NVD CVE-2025-54576).
> Fix: match anchored patterns against the decoded path only, as `ProxyRoute` does with `re.fullmatch`; lab `test_unlisted_paths_are_never_forwarded[admin?next=orders]`.

> [!warning] Signed is not encrypted
> Trigger: putting tokens into Starlette's `SessionMiddleware` (or any signed-cookie session) to "be a BFF".
> Symptom: the cookie is base64 JSON plus an `itsdangerous` signature, readable by anyone who gets it, and with defaults it lacks `Secure` and lives 14 days (starlette 1.10.0 starlette/middleware/sessions.py).
> Fix: keep tokens server-side, or encrypt the cookie as RFC 10017 Section 6.1.3.2 says the BFF SHOULD; lab `test_starlette_session_middleware_signs_but_does_not_encrypt`.

> [!warning] A logout that never deletes the cookie
> Trigger: `response.delete_cookie("__Host-Http-...")` with framework defaults.
> Symptom: the deletion lacks `Secure` (and `HttpOnly`), the browser rejects it under the prefix rules, and the old session cookie survives logout.
> Fix: repeat the prefix's attributes on deletion; lab `test_starlette_delete_cookie_defaults_fail_the_host_http_prefix`.

> [!warning] Concurrent refreshes trip reuse detection
> Trigger: two API calls race past an expired access token and both send the same rotating refresh token.
> Symptom: the authorization server sees a replay, revokes the family per RFC 9700 Section 4.14.2, and the user is logged out at random under load.
> Fix: single-flight refresh per session (lock plus re-read), and a distributed lease once the BFF has replicas; lab `test_concurrent_calls_refresh_once_and_never_trip_reuse_detection`.

> [!warning] Stolen session cookies bypass the BFF's guarantees
> Trigger: malware or a leaked HTTP archive yields the session cookie itself.
> Symptom: CircleCI reported that malware on an engineer's laptop, compromised on 2022-12-16, "was able to execute session cookie theft", stealing "a valid, 2FA-backed SSO session", with exfiltration on 2022-12-22 (CircleCI incident report, 2023-01-12); Okta reported HAR files "that contained session tokens which could in turn be used for session hijacking attacks", used to "hijack the legitimate Okta sessions of 5 customers" (Okta root cause report, 2023-11-03).
> Fix: RFC 10017 already notes that an encrypted cookie still works for session hijacking `(RFC 10017 Section 6.1.3.2)`; bound the damage with short session lifetimes tied to refresh token lifetime, revocation on logout, server-side anomaly detection at the BFF, and device-bound session credentials where browsers support them (see Security and robustness).

> [!warning] An open redirect at login
> Trigger: `/bff/login?return_to=https://evil.example` or `//evil.example`, accepted and replayed after the callback.
> Symptom: the BFF becomes an open redirector, which RFC 9700 Section 2.1 forbids because it can leak codes and tokens.
> Fix: accept only local absolute paths, rejecting `//` and backslashes; lab `test_login_refuses_open_redirect_return_targets`.

## Security and robustness

Threat model, in RFC 10017 terms: the BFF removes scenarios 5.1.1 to 5.1.3 and the token-theft consequences, and leaves 5.1.4 (client hijacking), which only CSP, secure coding, and server-side anomaly detection can address `(RFC 10017 Section 6.1.4.1)`.
New attack surface it adds is server-side: SSRF-style token leakage through the proxy (OWASP API Security 2023 API7, see [[Preventing-SSRF-API7-2023-DNS-Pinning-and-Private-CIDR-Socket-Guards]]), CSRF against cookie-authenticated endpoints, and session-store compromise.

Defense in depth, as built in the lab:
- route allowlist with `fullmatch`, bounded `{1,12}` digit ids, per-route methods, fixed upstream hosts in templates, and no client-controlled host anywhere in the outbound URL;
- static `X-BFF-CSRF: 1` header on every API-style endpoint, checked before session lookup and before any upstream call;
- `state` compared in constant time, `iss` compared exactly, transaction single-use and expiring after 600 s;
- a new 32-byte session id on every login, so a planted cookie value is never adopted (session fixation, OWASP reference in RFC 10017 Section 5.2.3);
- tokens never in responses: check-session returns `sub` and `scope` only, with `Cache-Control: no-store`;
- upstream `Set-Cookie` and all response headers except `content-type` dropped, so a resource server cannot plant cookies on the BFF's origin;
- encrypted, AAD-bound, hash-keyed store rows with key ids for rotation.

Not built in the lab, and needed in production:
- sender-constrained tokens (DPoP or mTLS) between BFF and resource server, which RFC 10017 Section 6.1.3.7 calls easier at the BFF;
- private-key JWT client authentication instead of a shared secret;
- redaction of `Authorization`, `Cookie`, and token fields from logs and traces ([[Structured-Log-Redaction-and-Preventing-PII-and-Token-Leaks]]);
- CSP with nonces or hashes and SRI on the static app, which aim to stop the malicious code from running at all, the only way to prevent 5.1.4 `(RFC 10017 Sections 5, 6.1.4.1)`;
- one application per origin `(RFC 10017 Section 9.4)`, and no untrusted siblings within the site, or `SameSite` stops being a CSRF boundary `(RFC 10017 Section 6.1.3.3.1)`;
- device-bound session credentials: Chrome's DBSC creates a key pair whose private key is stored with the operating system, using "facilities such as Trusted Platform Modules (TPMs)", and "uses these keys to maintain the freshness of short-lived cookies" (Google, "Fighting cookie theft using device bound sessions", 2024-04-02); MDN browser-compat-data lists the `Secure-Session-Registration` header as experimental, partially supported in Chrome 145 (Windows only) and Chrome 147+ (Windows and macOS), mirrored by Edge, and unsupported in Firefox and Safari (as of 2026-11).

Privacy: the BFF sees every request and response, which is minor when the same party builds both, and must be weighed when the BFF is a third-party product `(RFC 10017 Section 6.1.3.4)`.
Minimize ID Token claims to those the client uses `(RFC 10017 Section 9.1)`.

## The principal's view

The decision is not "cookies versus tokens"; it is which component is the OAuth client.
RFC 10017 makes the frontend's own server the client and turns the browser into a dumb session holder, which converts an unbounded token-theft problem into the bounded, well-understood problem of cookie-session security.
The cost is an always-on, stateful-ish proxy on the critical path of every API call, owned by the frontend team: latency of one extra hop, a store for sessions and locks, a CSRF contract with the SPA, and an allowlist that must change whenever the frontend calls a new endpoint.

Choose deliberately among:
- a same-origin app with no OAuth between frontend and API, when one team owns both and only federated login is needed `(RFC 10017 Section 7.1)`; it has the smallest surface of all;
- a BFF, for business, sensitive, or personal-data apps, and whenever the APIs belong to someone else `(RFC 10017 Section 6.1.4.3)`;
- a token-mediating backend, when proxying is impossible (for example, high-volume direct uploads to a third-party API), accepting access-token theft and adding tight scopes, audiences via RFC 8707, and short lifetimes `(RFC 10017 Sections 6.2.4.4, 9.1)`;
- a pure browser client only for low-sensitivity apps, with DPoP and a non-exportable WebCrypto key to limit exfiltration, knowing it cannot stop scenario 5.1.3 `(RFC 10017 Sections 6.3.4.2.2, 9.2)`.

Organizationally, put the BFF in the frontend's deployment unit (same origin, same release train), not in a central API gateway team: the RFC is explicit that it is part of the frontend application, and a shared gateway that holds every app's tokens concentrates blast radius and blurs which client a token was issued to.
Platform teams should instead ship a hardened BFF library or sidecar with the cookie, CSRF, allowlist, and refresh rules baked in, because every rule in this note is easy to get subtly wrong per team.
Scale questions to settle early: server-side sessions need a shared store (the RFC's "small-scale" caution), and rotation needs cross-replica single-flight; client-side encrypted sessions avoid the store but carry the 4096-octet ceiling and lose server-side revocation of the cookie itself.

## Interview questions

> [!question] L5: Why is storing an access token in `localStorage` not fixed by moving it into an in-memory closure or a Web Worker?
>
> > [!success]- Answer
> > Those mechanisms only change how hard it is to read existing tokens.
> > Attacker script in the origin can run a new silent authorization code flow in a hidden iframe and read the code from the same-origin frame `(RFC 10017 Section 5.1.3)`.
> > A public client has no secret, so the attacker exchanges that code for a fresh, independent token set.
> > Closures also fall to prototype poisoning `(RFC 10017 Section 8.4)`.
> >
> > Follow-up: Which property of the BFF specifically defeats the silent-flow attack, and why does DPoP not?

> [!question] L5: List every attribute of the BFF session cookie and the requirement level for each.
>
> > [!success]- Answer
> > `Secure` MUST and `HttpOnly` MUST.
> > `SameSite=Strict` SHOULD, `Path=/` SHOULD, no `Domain` SHOULD NOT be set, and a `__Host-Http-` name prefix SHOULD `(RFC 10017 Section 6.1.3.2)`.
> > The prefix lets the server trust that a browser enforced the rest.
> >
> > Follow-up: What happens on a browser that supports `__Host-` but not `__Host-Http-`?

> [!question] L6: Predict the output: the BFF sets the login-transaction cookie with `SameSite=Strict`; the user logs in at `https://as.example`, which redirects to `https://bff.example/bff/callback?code=...&state=...`; what does the callback return?
>
> > [!success]- Answer
> > `400` with "no login transaction", and no token request is sent.
> > The redirect is a cross-site top-level navigation, and `Strict` cookies are not sent on those `(draft-ietf-httpbis-rfc6265bis-22 Section 5.6.7.1)`.
> > With `Lax` the same `GET` navigation carries the cookie; with `form_post` even `Lax` fails because the method is not safe.
> > The lab's `test_a_strict_txn_cookie_breaks_the_cross_site_callback` asserts exactly this.
> >
> > Follow-up: Why may the response to that same callback still set a `Strict` session cookie?

> [!question] L6: Debug this API: users of a BFF-backed dashboard are logged out at random, only at peak load, and the authorization server logs "refresh token reuse detected" for legitimate users; what is happening and how do you fix it?
>
> > [!success]- Answer
> > Several concurrent API calls find the access token expired and each refreshes with the same rotating refresh token.
> > The second presentation is indistinguishable from replay, so the server revokes the family `(RFC 9700 Section 4.14.2)`, and the BFF drops the session on `invalid_grant` `(RFC 10017 Section 6.1.2.2)`.
> > Fix it with single-flight refresh per session (lock, then re-read the store), plus a distributed lease or compare-and-set when there are several BFF replicas.
> > Refreshing slightly before expiry (a skew) narrows but does not remove the window.
> >
> > Follow-up: What would you log to prove this diagnosis without logging any token?

> [!question] L6: Why does RFC 10017 recommend a custom request header for CSRF when the session cookie is already `SameSite=Strict`?
>
> > [!success]- Answer
> > `SameSite` is site-scoped, so a compromised sibling subdomain in the same eTLD+1 is same-site and gets the cookie attached `(RFC 10017 Section 6.1.3.3.1)`.
> > A static custom header makes every cross-origin request non-safelisted, which forces a CORS preflight that the BFF will not approve `(RFC 10017 Section 6.1.3.3.2)`.
> > Without the header, safelisted requests like a bodiless `POST` go out with no preflight at all.
> >
> > Follow-up: Which requests stay safelisted under the Fetch Standard, and what size limits apply?

> [!question] L6: A teammate proposes storing tokens in a signed cookie with Starlette's `SessionMiddleware` and calling that a stateless BFF. What is wrong and what would you change?
>
> > [!success]- Answer
> > The payload is base64 JSON with an `itsdangerous` signature, so it is integrity-protected but readable by anyone holding the cookie.
> > Its defaults omit `Secure` and use `SameSite=lax`, a 14-day lifetime, and the unprefixed name `session` (starlette 1.10.0 starlette/middleware/sessions.py).
> > RFC 10017 says a token-carrying client-side session SHOULD be encrypted `(RFC 10017 Section 6.1.3.2)`.
> > Encrypt with an AEAD, set the full attribute set and prefix, and check the 4096-octet cookie limit, or keep tokens server-side.
> >
> > Follow-up: Does encryption stop session hijacking with the stolen cookie?

> [!question] L7+: Your company runs 40 SPAs on subdomains of one registrable domain, each calling shared APIs; design the token architecture.
>
> > [!success]- Answer
> > Give each SPA its own BFF on its own origin and its own OAuth client registration, so cookies are host-only and tokens are per client `(RFC 10017 Section 9.4)`.
> > Do not rely on `SameSite` alone, since all 40 apps are same-site; require the custom header and strict CORS on every BFF.
> > Ship the BFF as a platform library or sidecar with prefix cookies, single-flight refresh, route allowlists, and log redaction built in.
> > Scope tokens per BFF with RFC 8707 resource indicators and minimal scopes `(RFC 10017 Section 9.1)`, and tell resource servers about BFF egress addresses for rate limiting `(RFC 10017 Section 6.1.3.5)`.
> >
> > Follow-up: When would you choose one central BFF instead, and what blast radius would you accept?

> [!question] L7+: The security team asks for DPoP in the browser instead of a BFF because "proof of possession stops token theft". How do you respond?
>
> > [!success]- Answer
> > DPoP with a non-exportable key does stop exfiltration of existing tokens `(RFC 10017 Section 6.3.4.2.2)`.
> > It does not stop the attacker from running a new flow and binding fresh tokens to its own key `(RFC 10017 Section 5.1.3)`, and online use from the victim's browser remains possible `(RFC 10017 Section 5.2.2)`.
> > RFC 10017 says sender-constraining "does not solve the security limitations of browser-only OAuth clients" `(RFC 10017 Section 9.2)`.
> > So DPoP is a hardening step for an accepted browser-client design, and the BFF remains the recommendation for sensitive apps; DPoP can still be used between the BFF and the resource server.
> >
> > Follow-up: What does a browser DPoP key still lose to malware that reads the filesystem?

> [!question] L5: Why must the BFF's proxy match routes against the path only, and anchored?
>
> > [!success]- Answer
> > The access token goes wherever the outbound URL points, so a loose match leaks tokens or reaches unintended endpoints `(RFC 10017 Section 6.1.3.6)`.
> > oauth2-proxy CVE-2025-54576 matched regexes against path plus query, letting query parameters satisfy a pattern (NVD CVE-2025-54576).
> > Anchored `fullmatch` on the decoded path, bounded parameter patterns, and fixed upstream hosts close both issues.
> >
> > Follow-up: Why test with percent-encoded `%2F` segments as well as literal ones?

## Related

Prerequisites: [[OAuth-2-0-to-OAuth-2-1-and-RFC-9700-Security-BCP]]

Next: [[OpenID-Connect-OIDC-ID-Tokens-vs-Access-Tokens-and-Metadata-Discovery]]

See also: [[API-Authentication-and-Authorization]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]], [[Refresh-Token-Rotation-Reuse-Detection-and-Token-Revocation-Architectures]], [[Sender-Constrained-Tokens-DPoP-RFC-9449-and-mTLS-Binding-RFC-8705]], [[Pushed-Authorization-Requests-PAR-RFC-9126-and-Rich-Authorization-Requests-RAR-RFC-9396]], [[CSRF-in-Cookie-Authenticated-APIs-SameSite-Double-Submit-and-Fetch-Metadata]], [[Backend-for-Frontend-BFF-Aggregation-Fan-Out-and-Partial-Degradation]], [[Preventing-SSRF-API7-2023-DNS-Pinning-and-Private-CIDR-Socket-Guards]], [[Structured-Log-Redaction-and-Preventing-PII-and-Token-Leaks]]

- Lab: `labs/tests/f14_api_authentication_and_identity/test_oauth_2_0_for_browser_based_applications_bff_and_cookies_rfc_10017.py`

## Further reading

- RFC 10017, OAuth 2.0 for Browser-Based Applications (BCP 212, August 2026): the threat model, the three patterns, and the cookie, CSRF, and proxy rules this note implements.
- RFC 9700, Best Current Practice for OAuth 2.0 Security: PKCE, `state`, mix-up defense, open-redirect ban, and refresh token rotation that RFC 10017 builds on.
- RFC 7636, Proof Key for Code Exchange: verifier grammar and the `S256` test vector.
- RFC 9207, OAuth 2.0 Authorization Server Issuer Identification: the `iss` response parameter and its comparison rule.
- RFC 6749, The OAuth 2.0 Authorization Framework: confidential clients and `client_secret_basic` encoding.
- RFC 7009, OAuth 2.0 Token Revocation: the logout back channel and its always-`200` response.
- draft-ietf-httpbis-layered-cookies-02: the `__Host-`, `__Http-`, and `__Host-Http-` prefixes and the store algorithm that enforces them.
- draft-ietf-httpbis-rfc6265bis-22: `SameSite` semantics, `Lax-allowing-unsafe`, and the top-level navigation trade-off.
- WHATWG Fetch Standard: CORS-safelisted methods and request headers, which decide when a preflight protects the BFF.
- NVD CVE-2025-54576: a real authenticating-proxy bypass caused by matching routes against the full URI.
- CircleCI January 4, 2023 incident report and Okta's 2023-11-03 root cause report: session-cookie and session-token theft in practice.
- Google, "Fighting cookie theft using device bound sessions" (2024-04-02): the direction for binding session cookies to a device key.
