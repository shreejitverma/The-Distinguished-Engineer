---
type: concept
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://www.rfc-editor.org/rfc/rfc9700.txt"
  - "https://www.ietf.org/archive/id/draft-ietf-oauth-v2-1-17.txt"
  - "https://datatracker.ietf.org/doc/draft-ietf-oauth-v2-1/"
  - "https://www.rfc-editor.org/rfc/rfc6749.txt"
  - "https://www.rfc-editor.org/rfc/rfc6750.txt"
  - "https://www.rfc-editor.org/rfc/rfc6819.txt"
  - "https://www.rfc-editor.org/rfc/rfc7636.txt"
  - "https://www.rfc-editor.org/rfc/rfc8252.txt"
  - "https://www.rfc-editor.org/rfc/rfc8414.txt"
  - "https://www.rfc-editor.org/rfc/rfc9207.txt"
  - "https://www.rfc-editor.org/rfc/rfc9126.txt"
  - "https://www.rfc-editor.org/rfc/rfc9449.txt"
  - "https://www.rfc-editor.org/rfc/rfc9068.txt"
  - "https://www.rfc-editor.org/rfc/rfc3986.txt"
  - "https://www.rfc-editor.org/rfc/rfc9110.txt"
  - "https://www.rfc-editor.org/rfc/rfc9112.txt"
  - "https://www.rfc-editor.org/rfc/rfc8705.txt"
  - "https://www.rfc-editor.org/rfc/rfc9396.txt"
  - "https://raw.githubusercontent.com/authlib/authlib/v1.9.5/authlib/oauth2/rfc7636/challenge.py"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?keywordSearch=PKCE&resultsPerPage=60"
  - "https://owasp.org/API-Security/editions/2023/en/0xa2-broken-authentication/"
---

# From OAuth 2.0 (RFC 6749) to OAuth 2.1 (draft-ietf-oauth-v2-1-17) and OAuth Security BCP (RFC 9700): Mandatory PKCE and Removed Implicit/ROPC Grants

> [!summary] Claim
> OAuth 2.1 is OAuth 2.0 with RFC 9700's lessons made normative: every authorization code is bound to a per-transaction `S256` PKCE challenge, redeemed at most once, delivered only to a byte-for-byte registered redirect URI with the issuer named in `iss`, and the two grants that hand tokens or passwords to the wrong party (implicit and resource owner password credentials) no longer exist.

Part of [API Authentication and Identity](README.md) in [API Engineering](../README.md).

## TL;DR

- Mechanism: the client sends `code_challenge=BASE64URL(SHA256(code_verifier))` with `code_challenge_method=S256` in the front channel and the 43 to 128 character `code_verifier` only in the back-channel token request, so a stolen or injected code is useless without the verifier (RFC 7636 Sections 4.1 to 4.6, RFC 9700 Section 4.5.3.1).
- Contract rule: public clients MUST use PKCE, authorization servers MUST support it, MUST reject a `code_verifier` for a code issued without a challenge (the downgrade defense), MUST compare redirect URIs by simple string comparison, and the password grant MUST NOT be used (RFC 9700 Sections 2.1, 2.1.1, 2.4, and 4.8.2); draft-ietf-oauth-v2-1-17 extends PKCE to all clients except a confidential client for which the server has reasonable assurance of correct OpenID Connect `nonce` handling, removes `plain`, and requires `iss` in every authorization response (Sections 4.1.1, 4.1.2, 7.5.1.1, and 7.5.2).
- Trap: PKCE that is enforced only "if the client sent a challenge" is not PKCE; authentik, Spring Authorization Server, OpenAM, Gitea, and Cloudflare's `workers-oauth-provider` all shipped CVEs where an attacker removed the challenge or the verifier and the check silently skipped (NVD, as of 2026-11), and the pinned Authlib 1.9.5 still defaults an absent method to `plain` (authlib 1.9.5 authlib/oauth2/rfc7636/challenge.py, CodeChallenge.DEFAULT_CODE_CHALLENGE_METHOD).
- Portability: OAuth 2.1 is still an Internet-Draft (revision 17, 31 October 2026, expires 4 May 2027) and defines no version signal, so an authorization server applies 2.0 or 2.1 rules per client registration (draft Section 1.8); the RFC 9700 rules are already a published BCP (BCP 240, January 2025) that updates RFC 6749, 6750, and 6819.
- When not to use it: do not run a redirect-based grant at all for machine-to-machine calls (use client credentials, see the service-to-service note), and do not treat PKCE as client authentication: it proves the redeemer started the transaction, not who the client is (NocoDB CVE-2026-126276 is exactly that confusion).

## Mental model

```mermaid
sequenceDiagram
    participant U as User agent
    participant C as Client
    participant AS as Authorization server
    participant RS as Resource server
    Note over C: verifier = 32 random octets, base64url (43 chars)<br/>challenge = BASE64URL(SHA256(verifier))
    C->>U: 303 to /authorize?response_type=code&client_id&state<br/>&code_challenge&code_challenge_method=S256
    U->>AS: GET /authorize (front channel, readable by A4)
    Note over AS: redirect_uri == registered (string compare,<br/>loopback port exempt); bind challenge to code
    AS->>U: 303 Location: redirect_uri?code&state&iss
    U->>C: GET redirect_uri?code&state&iss (readable by A3)
    Note over C: iss == expected issuer, state matches
    C->>AS: POST /token grant_type=authorization_code<br/>code, code_verifier, client auth
    Note over AS: SHA256(verifier) == challenge;<br/>atomic ISSUED -> REDEEMED; replay revokes
    AS->>C: 200 access_token, Cache-Control: no-store
    C->>RS: Authorization: Bearer (never ?access_token=)
```

The client mints a fresh secret per transaction and puts only its hash on the front channel, which the RFC 9700 attacker model assumes can leak in both directions: attacker A4 reads the authorization request and attacker A3 reads the authorization response (RFC 9700 Section 3).
The authorization server refuses to send anything to a redirect URI that is not byte-identical to a registered one, binds the challenge to the code it issues, and names itself in `iss` so that a client talking to several servers can detect a mix-up (RFC 9207 Section 2.4).
The token request is the first message that carries the verifier, over a direct TLS connection the attacker cannot read, and the server checks the hash and flips the code from issued to redeemed in one atomic step, so a second valid redemption is detected and revokes what the first one minted (draft Sections 4.1.3 and 7.5.3).
The access token then travels only in the `Authorization` header, because the query-string bearer method of RFC 6750 Section 2.3 is gone (draft Section 5.1).

## How it works

### What changed, and where it is written

| Rule | RFC 6749 (October 2012) | RFC 9700 (BCP 240, January 2025) | draft-ietf-oauth-v2-1-17 (31 October 2026) |
| --- | --- | --- | --- |
| PKCE | Not defined; RFC 7636 is an optional extension | Public clients MUST, confidential RECOMMENDED, AS MUST support (2.1.1) | Required for all clients except a confidential client with assured OIDC `nonce` handling (4.1.1, 7.5.1.1) |
| `code_challenge_method` | n/a; RFC 7636 4.3 defaults to `plain` | SHOULD use a method that does not expose the verifier; only `S256` qualifies (2.1.1) | REQUIRED parameter; `plain` "removed and explicitly prohibited" (4.1.1, 7.5.2) |
| PKCE downgrade | n/a | AS MUST reject a `code_verifier` when no challenge was sent (4.8.2) | `code_verifier` "MUST NOT be used otherwise" (4.1.3) |
| Redirect URI matching | Simple string comparison only if the full URI was registered; partial registration allowed (3.1.2.2, 3.1.2.3) | MUST use exact string matching, except loopback ports (2.1, 4.1.3) | Same, stated as case-sensitive and character-by-character with no normalization (4.1.1, 2.3.1) |
| `http` redirect URIs | SHOULD require TLS (3.1.2.1) | MUST NOT allow `http` except native loopback (2.6) | Every protocol URL MUST be `https`; only loopback redirect URIs MAY use `http` (1.5, 8.5.4) |
| Implicit grant (`response_type=token`) | Defined (4.2) | SHOULD NOT be used (2.1.2) | Not defined (10, 10.1) |
| Resource owner password credentials | Defined (4.3) | MUST NOT be used (2.4) | Not defined (10) |
| Code reuse | MUST deny, SHOULD revoke tokens (4.1.2) | Restated (4.2.4) | MUST deny; revoke only if the replay is otherwise valid (4.1.3, 7.5.3) |
| `redirect_uri` in token request | REQUIRED if sent in the authorization request (4.1.3) | n/a | Removed; MUST still be accepted and enforced for OAuth 2.0 clients (10.2) |
| `iss` in authorization response | n/a | SHOULD use RFC 9207 when talking to several servers (2.1) | REQUIRED from every server, including on errors (4.1.2, 4.1.2.1) |
| Bearer token in query string | Defined in RFC 6750 2.3 | Clients MUST NOT (4.3.2) | Clients MUST NOT send it, resource servers MUST ignore it (5.1) |
| Client secret in request body | NOT RECOMMENDED; AS MUST support Basic (2.3.1) | n/a | AS MUST support `client_secret_post`; Basic is MAY (2.4.1) |
| Token response cache headers | `Cache-Control: no-store` and `Pragma: no-cache` (5.1) | n/a | `Cache-Control: no-store` only (3.2.3) |
| Refresh tokens for public clients | Implementation-specific | MUST be sender-constrained or rotated (2.2.2, 4.14.2) | Same (10) |
| Redirect status | `302` in examples, any method allowed (1.7) | MUST NOT use `307` after a credential `POST`; SHOULD use `303` (4.12) | Any method except `307` (1.6, 7.5.4) |

The draft's own list of differences is in Section 10 and is labelled non-normative; the normative text is in the sections cited above.
The draft carries `Obsoletes: 6749, 6750 (if approved)` since revision 17 (Appendix F), and revision 16 is where `plain` was removed and `iss` became mandatory for the server (Appendix F, "-16").
The datatracker page lists revision 17 as the current one (as of 2026-11).

### The attacker model that justifies every change

RFC 6819 left its attacker model implicit; RFC 9700 Section 3 names five attackers.
A1 is a web attacker that runs its own sites, clients, and even authorization servers, and can lure users to any URL.
A2 is a network attacker with full control of unprotected traffic.
A3 can read but not modify the authorization response, through open redirectors, mix-up, sloppy redirect validation, shared custom schemes on mobile, and URLs logged by browsers, proxies, and operating systems.
A4 can read but not modify the authorization request.
A5 holds an access token, for example through a compromised or counterfeit resource server.
Every OAuth 2.1 rule below closes a path for one of these: `S256` exists because of A4, PKCE and single-use codes because of A3, exact redirect matching because of A1 plus A3, `iss` because of A1 operating an authorization server, and sender-constraining because of A5.

### PKCE on the wire

The verifier is `43*128unreserved`, where `unreserved = ALPHA / DIGIT / "-" / "." / "_" / "~"` (RFC 7636 Section 4.1, repeated in draft Section 4.1.1).
The recommended construction is 32 octets from a CSPRNG, base64url-encoded without padding, which gives exactly 43 characters and 256 bits of entropy (RFC 7636 Sections 4.1 and 7.1).
The challenge is `BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))`, which is always 43 characters because SHA-256 output is 32 octets (RFC 7636 Section 4.2).
RFC 7636 Appendix B gives the vector, which the lab reproduces:

```text
code_verifier  = dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk   (43 chars, 32 octets)
code_challenge = E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM   (43 chars, SHA-256 of the ASCII verifier)

GET /authorize?response_type=code&client_id=s6BhdRkqt3&state=xyz
    &code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM&code_challenge_method=S256

POST /token
grant_type=authorization_code&code=SplxlOBeZQQYbYS6WxSbIA&client_id=s6BhdRkqt3
    &code_verifier=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk
```

The draft's own example pair is consistent too: the challenge `6fdkQaPm51l13DSukcAH3Mdx7_ntecHYd1vi3n0hMZY` in Section 4.1.1 is the `S256` of the 56-character verifier `3641a2d12d66101249cdf7a79c000c1f8c05d2aafcf14bf146497bed` in Section 4.1.3, which the lab checks.
No salt is added, because a 256-bit verifier already makes brute force infeasible and a public salt would not raise the work factor (RFC 7636 Section 7.3).
The server MUST associate `code_challenge` and `code_challenge_method` with the code it issues, either in its database or encrypted inside the code itself, and MUST NOT expose the challenge in a form other parties can extract (RFC 7636 Section 4.4, draft Section 4.1.2).
At the token endpoint it recomputes the hash and compares; a mismatch is `invalid_grant` (RFC 7636 Section 4.6), and the draft adds that a verifier MUST be present if and only if a challenge was (draft Section 4.1.3).

Why `S256` and not `plain`: with `plain` the challenge equals the verifier, so an A4 attacker that reads the authorization request has everything needed to redeem the code (RFC 7636 Section 7.2, RFC 9700 Section 2.1.1).
RFC 7636 kept `plain` for devices that could not hash; draft Section 7.5.2 retires that argument because OAuth 2.1 requires TLS 1.2 or later, which already needs SHA-256, and adds that a server supporting `plain` "cannot distinguish legitimate constrained clients from attacker-modified requests claiming to be constrained".
RFC 7636 Section 4.3 made `code_challenge_method` OPTIONAL with a default of `plain`, which is why a 2.0-era server that sees a challenge with no method treats it as `plain`; in OAuth 2.1 the method is REQUIRED (draft Section 4.1.1).

### What PKCE actually defends

PKCE was designed for native apps, where several apps can register the same custom URI scheme and one of them receives another's code (RFC 7636 Section 1, RFC 8252 Section 8.1).
RFC 9700 generalizes it to every client because of authorization code injection (Section 4.5): the attacker steals a victim's code, starts a normal flow at a legitimate confidential client from the attacker's own browser, swaps the stolen code into the response, and lets the client redeem it with its own valid client secret, so client authentication does not help and even asymmetric client authentication does not stop it (RFC 9700 Section 4.5.2).
With PKCE the client sends the verifier of the attacker's session, which does not match the challenge bound to the victim's code, and the exchange fails (RFC 9700 Section 4.5.3.1).
PKCE also gives CSRF protection, and RFC 9700 Section 4.7.1 says it is more robust than `state` against an A3 attacker, because a leaked `state` can be replayed into a forged response while a leaked code is useless without the verifier.
A client may drop `state` for CSRF only after confirming the server supports PKCE, which is why the server MUST make that support discoverable, RECOMMENDED through `code_challenge_methods_supported` in RFC 8414 metadata (RFC 9700 Sections 2.1, 2.1.1, and 4.7.1).

The confidential-client exception in OAuth 2.1 is OpenID Connect's `nonce` (RFC 9700 Section 4.5.3.2, draft Section 7.5.1.1).
It only works if the client validates the `nonce` in the ID Token from the token endpoint and ignores every token until that check passes, and draft Section 7.5.1.1 points out the asymmetry: with `nonce` the server issues tokens and trusts the client to reject them later, while with PKCE the server refuses to issue anything.
Both values MUST be transaction-specific and securely bound to the client and the user agent, and fresh values MUST be chosen after an error (draft Section 7.5.1.1); servers are "encouraged" to detect constant challenges (RFC 9700 Section 2.1.1).

PKCE has a stated limit: an attacker who can modify the victim's authorization request can substitute the challenge from the attacker's own session, and then inject the victim's code into that session (RFC 9700 Section 4.5.4).
That is why the redirect, Referer, history, mix-up, and open-redirector defenses still matter, and why Pushed Authorization Requests move the request to the back channel (see [[Pushed-Authorization-Requests-PAR-RFC-9126-and-Rich-Authorization-Requests-RAR-RFC-9396]]).

### The PKCE downgrade attack

If the server enforces PKCE only when `code_challenge` is present, the presence of that parameter is an attacker-controlled switch (RFC 9700 Section 4.8).
The attack in RFC 9700 Section 4.8.1, step by step:

1. The victim starts a flow; the client sends `code_challenge=hash(abc)` and waits.
2. The attacker starts a flow at the same client on the attacker's device, and strips the whole `code_challenge` from that request with browser developer tools.
3. The server issues a code bound to no challenge.
4. The attacker sends the victim's browser to the client's redirect URI with that code.
5. The client redeems it with `code_verifier=abc` from the victim's session.
6. The server sees no challenge, ignores the verifier, and issues a token for the attacker's account into the victim's session.

The server-side fix is one rule: if no challenge was bound to the code, a token request that carries a `code_verifier` is rejected (RFC 9700 Section 4.8.2).
A server that requires PKCE for every client gets this for free (RFC 9700 Section 4.8.2), which is the OAuth 2.1 position.
The lab keeps an OAuth 2.0 confidential client without mandatory PKCE precisely to show that the rule still matters during migration.

### Exact redirect URI matching

RFC 6749 let clients register only part of a redirect URI, and required simple string comparison only when the full URI was registered (RFC 6749 Sections 3.1.2.2 and 3.1.2.3).
Pattern matching failed in the wild: RFC 9700 Section 4.1.1 walks through a registered `https://*.somesite.example/*` that a naive matcher reads as "any character", which accepts `https://attacker.example/.somesite.example`, and notes that even a correct subdomain wildcard loses to a dangling-CNAME subdomain takeover.
Section 4.1.2 shows the implicit-grant variant that defeats even narrow patterns: an AS that allows `https://client.somesite.example/cb?*` redirects to the client's open redirector with `#access_token=...`, and because browsers reattach the fragment when the next `Location` has none (RFC 9110 Section 17.11, as cited by RFC 9700), the token lands on `https://attacker.example/#access_token=...`.

The rule is now one sentence: the two URIs MUST be equal under simple string comparison (RFC 3986 Section 6.2.1), case-sensitive and character by character with no normalization (RFC 9700 Section 4.1.3, draft Section 4.1.1).
That means `https://c.example/cb/`, `https://C.example/cb`, `https://c.example:443/cb`, and `https://c.example/%63b` are all different from `https://c.example/cb`, which the lab pins one by one.
The only exception is a native app's loopback redirect: the server MUST accept any port for `http://127.0.0.1:{port}/{path}` and `http://[::1]:{port}/{path}`, because the app binds an ephemeral port at request time (RFC 8252 Section 7.3, draft Section 8.4.2).
`http://localhost:{port}` works similarly but is NOT RECOMMENDED, because a name can resolve to a non-loopback interface or be blocked by a client firewall (draft Section 8.4.2); RFC 9700 Section 2.1 says "localhost redirection URIs" loosely, and the lab follows the draft by exempting only the two IP literals.
Registration rules that come with it: a redirect URI MUST be absolute and MUST NOT contain a fragment; its own query component MUST be retained when the server adds response parameters and MUST NOT contain response parameter names (draft Section 2.3); `http` is allowed only for loopback (RFC 9700 Section 2.6); a private-use scheme SHOULD be a reverse domain name such as `com.example.app:/oauth2redirect/example-provider`, and one without a period SHOULD be rejected (draft Sections 2.3.1 and 8.4.3).
When the client or redirect URI is invalid, the server MUST NOT redirect at all and SHOULD answer `400` to the user agent (draft Section 4.1.2.1), otherwise the authorization endpoint is itself an open redirector (RFC 9700 Section 4.11.2).
RFC 9700 Section 4.1.3 also lets a server skip the redirect check when the request's origin and integrity are verified, for example a signed request object or PAR with client authentication.

Removing `redirect_uri` from the token request follows from PKCE.
In RFC 6749 the token-endpoint comparison was the defense against a code obtained through a manipulated redirect URI (RFC 6749 Section 10.6), and RFC 9700 Section 4.5.2 reports that providers "very often ignore" it.
OAuth 2.1 makes PKCE carry that weight and drops the parameter, but a server that also serves OAuth 2.0 clients MUST still accept and enforce it for them, and an OAuth 2.1 client will not interoperate with a server that insists on it (draft Section 10.2).
Revision 17 is not fully consistent here: the step-by-step text under Figure 3 in Section 4.1 still says the client "includes the redirect URI used to obtain the authorization code" and the server "ensures that the redirect URI received matches", while Sections 4.1.3 and 10.2 removed the parameter; the lab follows Sections 4.1.3 and 10.2 and enforces the value only if a client sends it.

### Why the implicit grant is gone

The implicit grant returned `access_token` in the redirect URI fragment and was forbidden to issue refresh tokens (RFC 6749 Section 4.2.2: "The authorization server MUST NOT issue a refresh token").
RFC 9700 Section 2.1.2 lists why it cannot be made safe: the token leaks through redirect manipulation, Referer headers, and browser history (Sections 4.1, 4.2, and 4.3), it can be injected into a client with no way to detect it in pure OAuth (Section 4.6), and there is no standard way to sender-constrain a token issued in the front channel (Section 2.1.2).
Moving token issuance to the token endpoint lets the server detect code replay, keeps tokens out of URLs, and lets it bind tokens with DPoP or mTLS (RFC 9700 Section 2.1.2).
OAuth 2.1 does not define `response_type=token`; other extension response types such as OpenID Connect's `id_token` are explicitly unaffected (draft Section 10.1).
The browser replacement is the authorization code grant with PKCE, preferably behind a backend-for-frontend, which is the subject of [[OAuth-2-0-for-Browser-Based-Applications-BFF-and-Cookies-RFC-10017]].

### Why the password grant is gone

RFC 6749 Section 10.7 already called the resource owner password credentials grant a "password anti-pattern" kept "for legacy or migration reasons".
RFC 9700 Section 2.4 makes it MUST NOT: the client sees the user's credentials, they can leak in more places than the authorization server, users are trained to type passwords into third-party UIs, and the grant cannot carry multi-step authentication, multi-factor prompts, or origin-bound credentials such as WebAuthn.
Migration paths: first-party native and web apps use the authorization code grant with PKCE in the system browser (draft Section 8.3, RFC 8252), and machine identities use the client credentials grant (draft Section 4.2).
Draft Section 7.5.2 gives a migration trick, stated there for existing clients that cannot support `S256`: issue extended-lifetime refresh tokens before discontinuing "legacy authorization methods", since the refresh grant itself needs no PKCE; the same reasoning carries over to a password-grant client that cannot move before the grant is switched off, but that extension is this note's inference, not draft text.

### Single-use codes, replay revocation, and the race

The code MUST expire shortly after issue, with a RECOMMENDED maximum of 10 minutes, and is bound to the client identifier, the code challenge, and the redirect URI (draft Section 4.1.2, RFC 6749 Section 4.1.2).
The server MUST return an access token only once per code; a second valid request MUST be denied and SHOULD revoke every access and refresh token already issued from that code (draft Section 4.1.3), because the first redeemer may have been the attacker (RFC 9700 Section 4.2.4).
Draft Section 7.5.3 adds the ordering constraint that is easy to miss: revoke only when the replayed request is otherwise valid, including client authentication and the verifier, otherwise an attacker who has the code but not the verifier can send a garbage request first and revoke the legitimate client's tokens on demand.
Single use is a concurrency property, not a lookup.
NVD records CVE-2026-47386 against NocoDB, where "two concurrent token-exchange requests using the same OAuth authorization code could each mint a distinct valid (access_token, refresh_token) pair".
The fix is an atomic compare-and-set from issued to redeemed in the database (a conditional `UPDATE ... WHERE state = 'issued'` that reports one affected row, or a single-key delete-and-return), never a read followed by a write with any I/O in between.
The lab models the I/O with an awaited checkpoint before the check and after the write, and shows five concurrent redemptions produce exactly one token, which is then revoked because the four losers were valid replays.

### Mix-up and the `iss` response parameter

A mix-up needs a client that talks to more than one authorization server, one of them honest (H-AS) and one run by the attacker (A-AS), and a client that uses the same redirect URI for both (RFC 9700 Section 4.4.1).
The user picks A-AS, A-AS redirects the browser to H-AS with the client's H-AS `client_id`, the user approves at H-AS, and the client, still believing it is talking to A-AS, sends the H-AS code to A-AS's token endpoint (RFC 9700 Section 4.4.1, steps 1 to 6).
The defense is for the client to store the issuer it sent each request to and compare it with the `iss` parameter in the response by simple string comparison after form-decoding, rejecting a mismatch or a missing `iss` from a server that advertises support (RFC 9207 Section 2.4).
RFC 9700 Section 4.4.2 warns that storing the authorization endpoint URL is not enough, because an attacker can declare an honest server's authorization endpoint as its own with a token endpoint it controls; the issuer identifier names the whole endpoint tuple through metadata.
The older alternative, a distinct redirect URI per issuer, SHOULD only be used if nothing else is available, because an attacker can register a client at the honest server with the redirect URI assigned to the attacker's server (RFC 9700 Section 4.4.2.2).
OAuth 2.1 makes `iss` REQUIRED in every authorization response and error response, and servers advertise it with `authorization_response_iss_parameter_supported: true` (draft Sections 4.1.2 and 4.1.2.1, RFC 9207 Section 3).

### Smaller deltas that still break clients

- Empty parameters are treated as omitted, and every protocol parameter MUST NOT appear more than once (draft Section 3.2, RFC 6749 Section 3.1); a server that treats `client_secret=` or `code_verifier=` as "present" can skip a check, which is the shape of XenForo CVE-2026-73309 in NVD ("submitting empty values for client_secret and code_verifier").
- Client secrets: RFC 6749 Section 2.3.1 required servers to support Basic and called body credentials NOT RECOMMENDED; draft Section 2.4.1 inverts that, because the RFC 6749 rule of form-urlencoding the id and secret before Basic-encoding them "has led to many interoperability problems".
- One client authentication method per request (RFC 6749 Section 2.3), and a failed Basic attempt is `401` with `WWW-Authenticate` (draft Section 3.2.4).
- Token responses carry `Cache-Control: no-store`; the `Pragma: no-cache` of RFC 6749 Section 5.1 is gone (draft Section 3.2.3).
- A redirect that may carry a credential `POST` MUST NOT be `307`, because the user agent replays the form body to the client; `303` is the only status that unambiguously rewrites `POST` to `GET` (RFC 9700 Section 4.12, draft Section 7.5.4).
- CORS MAY be enabled on the token, metadata, JWKS, and registration endpoints, and MUST NOT be enabled on the authorization endpoint (RFC 9700 Section 2.6).
- Refresh tokens for public clients MUST be sender-constrained or rotated (RFC 9700 Section 4.14.2); rotation and reuse detection are covered in [[Refresh-Token-Rotation-Reuse-Detection-and-Token-Revocation-Architectures]], and draft Section 4.3.3 now notes that a lost rotation response strands the client, which sender-constrained refresh tokens avoid.
- Sender-constrained access tokens (RFC 8705, RFC 9449) are SHOULD for both servers (RFC 9700 Section 2.2.1); see [[Sender-Constrained-Tokens-DPoP-RFC-9449-and-mTLS-Binding-RFC-8705]].
- Access tokens SHOULD be audience-restricted, and refresh tokens MUST be bound to the consented scope and resource servers (RFC 9700 Sections 2.3 and 4.14.2).
- When client IDs and user identifiers share a namespace, the authorization server SHOULD NOT let a client influence its `client_id` or any claim that could be confused with a genuine resource owner, and where that cannot be avoided it MUST give resource servers another way to tell client tokens from user tokens (RFC 9700 Section 4.15.1); the RFC 9068 `sub` rule is what creates the collision.

### No version negotiation

There is no parameter, header, or metadata field that says "this is OAuth 2.1" (draft Section 1.8).
An authorization server decides per client, typically as a registration attribute, which lets it move clients to the 2.1 rules one at a time (draft Section 1.8).
What a client can discover is capability, not version: `code_challenge_methods_supported` (RFC 8414 Section 2, RECOMMENDED by RFC 9700 Section 2.1.1) and `authorization_response_iss_parameter_supported` (RFC 9207 Section 3).
A hardened server should advertise only what it enforces; the lab's metadata advertises `["S256"]`, `response_types_supported: ["code"]`, and `grant_types_supported: ["authorization_code"]`.

### What the pinned library does: Authlib 1.9.5

The `v1.9.5` tag source of `authlib/oauth2/rfc7636/challenge.py` is byte-identical to the installed copy in the lab environment.

- `CodeChallenge.DEFAULT_CODE_CHALLENGE_METHOD = "plain"` and `SUPPORTED_CODE_CHALLENGE_METHOD = ["plain", "S256"]` (authlib 1.9.5 authlib/oauth2/rfc7636/challenge.py, CodeChallenge); an OAuth 2.1 server must subclass and narrow both, and the lab pins the defaults so a future release that changes them fails the test.
- `CodeChallenge.validate_code_challenge` returns early when neither `code_challenge` nor `code_challenge_method` is present, even with `required=True`; the requirement is enforced later in `validate_code_verifier`, and only when `request.auth_method == "none"`, so a confidential client can complete a flow without PKCE (same file, CodeChallenge.validate_code_challenge and CodeChallenge.validate_code_verifier).
  That matches RFC 9700 (RECOMMENDED for confidential clients) but not OAuth 2.1 (REQUIRED unless `nonce` is assured).
- The downgrade check is present with an RFC 9700 Section 4.8 comment: no stored challenge plus a verifier raises `InvalidRequestError` (same file, CodeChallenge.validate_code_verifier).
- Verifier and challenge both go through `^[a-zA-Z0-9\-._~]{43,128}$` (same file, CODE_VERIFIER_PATTERN and CODE_CHALLENGE_PATTERN), which blocks the one-character verifiers of CVE-2026-41213.
- The final comparison is Python `==` (same file, compare_s256_code_challenge); NVD records the same pattern in OAuthLib as CVE-2026-49265 and itself calls it "a defense-in-depth concern" because practical exploitation needs an intercepted single-use code and enough measurements to beat network noise; the lab uses `hmac.compare_digest`.
- `AuthorizationCodeGrant.validate_token_request` enforces `redirect_uri` only when the stored code has one, and `create_token_response` calls `delete_authorization_code` after `save_token`, so single use and its atomicity are the developer's storage problem and a replayed code is simply not found; no revocation of the first redemption happens in the grant code path (authlib 1.9.5 authlib/oauth2/rfc6749/grants/authorization_code.py).
- The SQLAlchemy client mixin's `check_redirect_uri` is `redirect_uri in self.redirect_uris`, which is exact matching with no loopback port exception (authlib 1.9.5 authlib/integrations/sqla_oauth2/client_mixin.py, check_redirect_uri).
- `IssuerParameter.get_issuer` returns `None` unless overridden, so no `iss` is added by default (authlib 1.9.5 authlib/oauth2/rfc9207/parameter.py, IssuerParameter.get_issuer).
- The implicit and password grants still ship (`authlib/oauth2/rfc6749/grants/implicit.py` and `resource_owner_password_credentials.py` exist in 1.9.5); they are only active if registered.

## Numbers and limits

| Quantity | Value | Source |
| --- | --- | --- |
| `code_verifier` and `code_challenge` length | 43 to 128 characters of `[A-Za-z0-9-._~]` | RFC 7636 Sections 4.1 and 4.2; draft Section 4.1.1 |
| Recommended verifier entropy | 256 bits: 32 octets -> 43 base64url characters | RFC 7636 Sections 4.1 and 7.1 |
| `S256` challenge length | always 43 characters (32-octet SHA-256, base64url, no padding) | RFC 7636 Section 4.2; lab `test_new_code_verifier_is_43_characters_carrying_32_random_octets` |
| Authorization code lifetime | RECOMMENDED maximum 10 minutes | RFC 6749 Section 4.1.2; draft Section 4.1.2 |
| Code redemptions | exactly 1; a second valid one SHOULD revoke derived tokens | draft Sections 4.1.3 and 7.5.3 |
| Loopback redirect port | any port, 1 to 65535 in the lab | RFC 8252 Section 7.3; draft Section 8.4.2; lab policy for the range |
| Parameter value sizes | no maximum defined; implementations SHOULD support values long enough that requests stay within the 8000-octet request line RFC 9112 Section 3 recommends | draft Section 1.7.1 |
| Structured access tokens | "routinely exceed one kilobyte" | draft Section 1.7.1 |
| Authorization endpoint error status | `400`, never a redirect, when client or redirect URI is invalid | draft Section 4.1.2.1 |
| Token endpoint errors | `400`; `invalid_client` may be `401`, MUST be `401` with `WWW-Authenticate` after header auth | draft Section 3.2.4 |
| Draft status | revision 17, 31 October 2026, expires 4 May 2027 | draft-ietf-oauth-v2-1-17 header (as of 2026-11) |
| RFC 9700 | BCP 240, January 2025, updates RFC 6749, 6750, 6819 | RFC 9700 header |
| PKCE CVEs in NVD keyword search `PKCE` | 40 records | NVD API query fetched 2026-11-15 |
| Lab run | 76 tests in 0.22 s | measured 2026-11-15, `uv run --frozen pytest ...` on Apple M3 Pro, macOS 27.3 arm64, CPython 3.14.6; see Worked example |

## Portability

| Dimension | Behavior | Evidence |
| --- | --- | --- |
| CPython 3.14 | 76 lab tests pass on 3.14.6 (workspace `.venv`) | lab run, `uv run --frozen pytest ...`, macOS 27.3 arm64 |
| CPython 3.12, 3.13, 3.14.10, 3.15, 3.15t | not run: lab checks were paused by the user on 2026-11-11, so no `uv run --python <v>` leg was executed | none; recorded as not run |
| `hashlib.sha256`, `secrets.token_urlsafe`, `hmac.compare_digest` | the only primitives the PKCE path needs; all present in the 3.12 to 3.15 standard library | CPython standard library; not executed here on those versions |
| Authlib 1.9.5 | `plain` default, `S256` and `plain` supported, downgrade check, 43 to 128 regex | lab `test_authlib_1_9_5_defaults_still_accept_plain`; authlib 1.9.5 authlib/oauth2/rfc7636/challenge.py |
| Starlette 1.10.0 | `RedirectResponse(status_code=303)` and `JSONResponse` with explicit `Cache-Control` | lab ASGI tests over `httpx.ASGITransport` |
| Uvicorn, Hypercorn, Granian | not exercised; the protocol rules live in the application, and the only server-dependent limit is the maximum request-line and header size that bounds `state` and codes | draft Section 1.7.1; not measured here |
| HTTP/1.1 | the 8000-octet request-line recommendation that draft Section 1.7.1 points to is the HTTP/1.1 one | draft Section 1.7.1 citing RFC 9112 Section 3 |
| HTTP/2, HTTP/3 | redirects and `Location` are HTTP semantics, independent of the version; the draft's redirect rule (any method except `307`) is stated without reference to an HTTP version | draft Section 1.6; not exercised here |
| OAuth 2.0 vs 2.1 clients on one server | per-client rules; no version signal on the wire | draft Section 1.8; lab `legacy_oauth20` client |

## Worked example

The [implementation](../labs/src/api_labs/f14_api_authentication_and_identity/oauth21_server.py) is an in-memory authorization server with an injected clock, four registered clients (a public browser client, an OAuth 2.1 confidential client whose redirect URI has its own query, an OAuth 2.0 legacy confidential client with two redirect URIs, and a native loopback client), a client-side response validator, and a Starlette app with `/authorize`, `/token`, RFC 8414 metadata, and a protected resource.
The [test](../labs/tests/f14_api_authentication_and_identity/test_oauth_2_0_to_oauth_2_1_and_rfc_9700_security_bcp.py) has 76 cases: both published PKCE vectors, the ABNF bounds at 42, 43, 128, and 129, sixteen redirect-matching cases, registration rules, the full flow over ASGI, every PKCE rejection path, the downgrade, removed grants, replay with and without revocation, the concurrent race, expiry at 599 and 600 seconds, client authentication, mix-up detection, and the ignored query-string token.

The challenge transform, verbatim from the lab:

```python
def s256_challenge(code_verifier: str) -> str:
    """``BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))`` without padding (RFC 7636 4.2)."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
```

The PKCE decision at the token endpoint, verbatim; the first branch is the RFC 9700 Section 4.8.2 downgrade defense, and the order puts every validity check before replay handling so an invalid replay cannot revoke anything:

```python
    def _check_code(self, client: Client, record: CodeRecord, params: Mapping[str, str]) -> str:
        """Return an error code, or ``""`` when the request is valid for this code."""
        if record.client_id != client.client_id:
            return "invalid_grant"
        verifier = params.get("code_verifier")
        if record.code_challenge is None:
            if verifier is not None:
                return "invalid_request"  # RFC 9700 Section 4.8.2: PKCE downgrade
        elif verifier is None or not is_pkce_value(verifier):
            return "invalid_request"
        elif not hmac.compare_digest(s256_challenge(verifier), record.code_challenge):
            return "invalid_grant"  # RFC 7636 Section 4.6
        sent_redirect = params.get("redirect_uri")
        if record.redirect_uri is not None and client.legacy_oauth20 and sent_redirect is None:
            return "invalid_request"  # RFC 6749 Section 4.1.3 for OAuth 2.0 clients
        if sent_redirect is not None and sent_redirect != record.redirect_uri:
            return "invalid_grant"
        return ""
```

The redemption, verbatim; the `await` before the lookup stands for the database read, and there is no `await` between the state check and the state write:

```python
        await self._storage_io()  # load the code record
        record = self.codes.get(code)
        if record is None:
            return _token_error("invalid_grant")
        problem = self._check_code(client, record, params)
        if problem:
            return _token_error(problem)  # an invalid replay revokes nothing (draft 7.5.3)
        # Check-and-set with no await in between: concurrent redemptions serialize here.
        if record.state is CodeState.REDEEMED:
            for token_id in record.token_ids:
                self.tokens[token_id].revoked = True
            return _token_error("invalid_grant")
        if self._clock() >= record.expires_at:
            return _token_error("invalid_grant")
        record.state = CodeState.REDEEMED
```

Three of the tests, verbatim:

```python
async def test_pkce_downgrade_verifier_without_challenge_is_rejected(
    server: AuthorizationServer,
) -> None:
    # The legacy confidential client may skip PKCE, but a verifier then proves a downgrade.
    auth = ("client_secret", LEGACY_SECRET)
    uri = "https://legacy.example.test/cb"
    code = authorize_code(server, "legacy", None, redirect_uri=uri)
    attacked = await server.token(
        [*code_form(code, "legacy", RFC7636_VERIFIER), auth, ("redirect_uri", uri)]
    )
    assert (attacked.status, attacked.body["error"]) == (400, "invalid_request")
    honest = await server.token([*code_form(code, "legacy", None), auth, ("redirect_uri", uri)])
    assert honest.status == 200
```

```python
async def test_invalid_replay_does_not_revoke_issued_tokens(server: AuthorizationServer) -> None:
    code = authorize_code(server, "spa", RFC7636_VERIFIER)
    first = await server.token(code_form(code, "spa", RFC7636_VERIFIER))
    replay = await server.token(code_form(code, "spa", "c" * 43))
    assert replay.body == {"error": "invalid_grant"}
    assert server.token_is_active(first.body["access_token"])  # no denial-of-service lever
```

```python
async def test_concurrent_redemptions_mint_exactly_one_token(server: AuthorizationServer) -> None:
    code = authorize_code(server, "spa", RFC7636_VERIFIER)
    results = await asyncio.gather(
        *(server.token(code_form(code, "spa", RFC7636_VERIFIER)) for _ in range(5))
    )
    assert sorted(r.status for r in results) == [200, 400, 400, 400, 400]
    assert len(server.tokens) == 1
    # The losers were valid replays, so the winner's token is revoked as well.
    assert not any(server.token_is_active(t) for t in server.tokens)
```

Captured on 2026-11-15 on an Apple M3 Pro, macOS 27.3 (arm64), CPython 3.14.6, from `labs/`:

```text
$ uv run --frozen pytest tests/f14_api_authentication_and_identity/test_oauth_2_0_to_oauth_2_1_and_rfc_9700_security_bcp.py -q
........................................................................ [ 94%]
....                                                                     [100%]
76 passed in 0.22s
```

What the lab deliberately leaves out: user login and consent (the request is auto-approved for one subject), refresh tokens, scopes, PAR, and sender-constrained tokens, each of which has its own note.
One lab policy deserves a name: a wrong verifier does not consume the code, so the legitimate client can still redeem it; the 43-character minimum makes online guessing of a 256-bit verifier hopeless, which is the opposite of CVE-2026-41213, where one-character verifiers were accepted and "failed verifier attempts do not consume the authorization code".

## Pitfalls

> [!warning] Checking the verifier only when it is sent
> Trigger: the token endpoint validates `code_verifier` if present and otherwise proceeds.
> Symptom: a stolen PKCE-protected code is redeemed by simply omitting the verifier.
> Incidents: authentik before 2023.8.5 and 2023.10.4 "checks if the contents of `code_verifier` is matching only when it is provided" (CVE-2023-48228); OpenAM before 16.1.1 required a verifier only when the realm setting `codeVerifierEnforced` was on, and it is off by default (CVE-2026-48717); Gitea before 1.25.5 did not persist the `S256` method correctly, "allowing token exchange without the expected verifier check" (CVE-2026-26247, CVSS 3.1 score 9.1 from the CISA-ADP secondary scorer as listed on NVD).
> Fix: a code with a bound challenge requires a valid verifier, full stop; test the omission, the empty value, and the wrong value separately.

> [!warning] PKCE enforced only when the challenge is present
> Trigger: the authorization endpoint makes PKCE optional per request instead of per client.
> Symptom: the RFC 9700 Section 4.8 downgrade, where a code without a challenge is injected into a victim's session.
> Incidents: authentik before 2023.8.7 and 2023.10.7, "if the attacker removes the code_challenge parameter from the authorization request, authentik will not do the PKCE check" (CVE-2024-23647); Spring Authorization Server 1.0.0 to 1.0.5, 1.1.0 to 1.1.5, and 1.2.0 to 1.2.2 for confidential clients (CVE-2024-22258); Cloudflare `workers-oauth-provider`, where "an attacker could cause the check to be skipped", which NVD notes matters because the MCP specification requires OAuth 2.1 (CVE-2025-4144).
> Fix: reject a verifier without a challenge, and require the challenge for every public and every OAuth 2.1 client at the authorization endpoint.

> [!warning] PKCE applied to one response type only
> Trigger: the PKCE requirement is keyed on `response_type == "code"`.
> Symptom: codes issued through OpenID Connect hybrid flows carry no challenge; OpenAM before 16.1.3 let an attacker redeem such a code "with any non-empty code_verifier" (CVE-2026-105119).
> Fix: bind the challenge to every code the server issues, whatever response type produced it.

> [!warning] Accepting `plain`, or defaulting an absent method to `plain`
> Trigger: RFC 7636 Section 4.3 defaults a missing `code_challenge_method` to `plain`, and many libraries keep that default; Authlib 1.9.5 does (`DEFAULT_CODE_CHALLENGE_METHOD = "plain"`).
> Symptom: an A4 attacker reading the request has the verifier; better-auth before 1.6.11 "accept plain PKCE by default" (CVE-2026-67336, CVSS 4.0 score 9.4 from VulnCheck as listed on NVD); a client that sends an `S256` challenge but forgets the method gets `invalid_grant` on a server that stores the method as absent.
> Fix: advertise and accept only `S256`, and treat a missing method as `invalid_request` (draft Sections 4.1.1 and 7.5.2).

> [!warning] Treating PKCE as client authentication
> Trigger: a confidential client's secret check is skipped "because PKCE was used".
> Symptom: NocoDB before 2026.08.1 skipped client secret verification when it was omitted, so anyone holding a captured code and verifier could "mint access tokens and 60-day refresh tokens for a confidential client without knowing its secret" (CVE-2026-126276).
> Fix: authenticate every client that has credentials, then check PKCE; they answer different questions.

> [!warning] Putting the verifier where the attacker can read it
> Trigger: reusing the verifier as `state`, logging it, or storing it in a shared cookie.
> Symptom: OpenClaw before 2026.4.2 reused the PKCE verifier as `state` in one flow, "exposing it through the redirect URL", which defeats PKCE entirely (CVE-2026-34511).
> Fix: the verifier never leaves the client except in the token request body; `state` is a separate random value.

> [!warning] Non-atomic single use
> Trigger: read the code row, check `used = false`, issue tokens, then write `used = true`.
> Symptom: two concurrent exchanges both succeed (NocoDB CVE-2026-47386).
> Fix: one atomic conditional update or delete-and-return; the lab's concurrency test fails if an `await` is placed between the check and the write.

> [!warning] Revoking on any replay
> Trigger: "a reused code revokes all tokens" implemented before client authentication and PKCE checks.
> Symptom: an attacker with a leaked code but no verifier can revoke the victim's session at will (draft Section 7.5.3).
> Fix: validate the whole request first; revoke only on a replay that would otherwise have succeeded.

> [!warning] Normalizing redirect URIs before comparing
> Trigger: lowercasing hosts, resolving dot segments, stripping default ports, or decoding percent-escapes "to be helpful".
> Symptom: comparison no longer matches what the browser will request, and parser differentials reopen the RFC 9700 Section 4.1.1 class of bugs.
> Fix: register the exact string and compare exact strings; the only flexibility is the loopback port.

> [!warning] `localhost` redirects for desktop apps
> Trigger: registering `http://localhost:8080/callback` and binding a fixed port.
> Symptom: the flow fails whenever that port is busy, and if the server applies the port exception to names, `localhost` may resolve off-box (draft Section 8.4.2).
> Fix: register `http://127.0.0.1/callback` (and `http://[::1]/callback`), bind an ephemeral port, and open the listener only for the duration of the request (draft Section 8.5.4).

> [!warning] Timing-unsafe verifier comparison
> Trigger: `==` on the recomputed challenge, as in Authlib 1.9.5 and OAuthLib before 4.0.0.
> Symptom: OAuthLib's instance CVE-2026-49265 carries a CVSS 3.1 score of 6.8 from GitHub, the CNA (NVD had not added its own score as of 2026-11), and its description notes that practical remote exploitation was not established.
> Fix: `hmac.compare_digest`, which costs nothing.

## Security and robustness

| Attacker (RFC 9700 Section 3) | Path | Control in OAuth 2.1 and RFC 9700 |
| --- | --- | --- |
| A1 web attacker | Crafted `redirect_uri`, registered malicious client, own authorization server | Exact redirect matching, no redirect on invalid client or URI, `iss` comparison, CSP `frame-ancestors` against clickjacking (RFC 9700 Section 4.16) |
| A2 network attacker | Cleartext redirect | `https` redirect URIs except loopback (RFC 9700 Section 2.6) |
| A3 reads the authorization response | Referer, history, logs, shared custom schemes, open redirectors | PKCE, single-use 10-minute codes, `Referrer-Policy: no-referrer` on the callback page (RFC 9700 Section 4.2.4), no implicit grant |
| A4 reads the authorization request | Logged authorize URLs | `S256` only; PAR moves the request to the back channel |
| A5 holds an access token | Compromised or counterfeit resource server | Audience restriction and sender-constrained tokens (RFC 9700 Sections 2.2.1, 2.3, and 4.10) |

OWASP API Security Top 10 2023 files this class under API2:2023 Broken Authentication; a hand-rolled authorization server is one of the most direct ways to land there.
Input validation edge cases the lab pins: duplicate parameters are `invalid_request` and are rejected before any redirect if they could affect the redirect target; empty values are treated as absent; verifier and challenge are checked against the ABNF before hashing; a Basic header and a `client_secret` together are rejected; Basic credentials are form-decoded after base64 decoding.
Boundary enforcement: the authorization endpoint answers errors about the client or redirect URI with `400` and no `Location`, so it never becomes an open redirector (draft Section 4.1.2.1, RFC 9700 Section 4.11.2).
Clients have their own duties that no server can enforce: reject a response whose `iss` does not match the issuer the request went to, consume `state` once, and never expose an open redirector on the redirect host (RFC 9700 Sections 4.4.2, 4.2.4, and 4.11.1).
Defense in depth beyond this note: PAR with client authentication lets the server trust the redirect URI because the request's origin and integrity are verified (RFC 9700 Section 4.1.3, RFC 9126), and DPoP binds tokens to a key so a leaked token alone is not enough (RFC 9449).

## The principal's view

The decision is not "adopt OAuth 2.1" but "which clients get which rules, and how fast".
Because there is no version signal, the migration unit is the client registration: add a per-client profile, default every new client to the 2.1 profile, and move existing clients one by one while measuring how many authorization requests still arrive without `S256`, with pattern-matched redirect URIs, or with `response_type=token`.
The organizational trade-off is breakage on the client side: every partner integration that uses implicit, the password grant, wildcard redirects, or query-string tokens must change code, and the platform team has to fund that work with a deprecation timeline and telemetry rather than flip a global switch.
Do not build an authorization server unless identity is the product; the CVE list above is a catalogue of mature open-source servers getting one conditional wrong, and a managed or well-reviewed server with a strict configuration is cheaper than finding those conditionals yourself.
If you do own one, write the rules as tests against the HTTP surface (the lab's shape), because each CVE above is a missing negative test: omit the verifier, strip the challenge, send an empty value, race the redemption.
Choose something else when the shape does not fit: service-to-service calls use client credentials or token exchange (see [[Service-to-Service-Identity-Client-Credentials-and-Token-Exchange-RFC-8693]]), browser apps put the client in a backend (see [[OAuth-2-0-for-Browser-Based-Applications-BFF-and-Cookies-RFC-10017]]), and high-assurance flows add PAR and DPoP instead of relaxing any of the rules here.

## Interview questions

> [!question] L5: Why does PKCE use a hash of the verifier in the authorization request instead of the verifier itself?
>
> > [!success]- Answer
> > The authorization request travels through the browser and can be logged or read, which RFC 9700 calls attacker A4.
> > With `S256` the request carries only `BASE64URL(SHA256(verifier))`, and the verifier first appears in the back-channel token request.
> > With `plain` the challenge equals the verifier, so anyone who reads the request can redeem the code, which is why OAuth 2.1 removed `plain`.
> >
> > Follow-up: Why is no salt added to the hash?

> [!question] L5: Which grants exist in OAuth 2.1 and which were removed?
>
> > [!success]- Answer
> > The authorization code grant (with PKCE), client credentials, refresh token, and extension grants remain.
> > The implicit grant (`response_type=token`) and the resource owner password credentials grant are not defined.
> > Implicit leaked tokens through URLs and could not be sender-constrained; the password grant exposed user credentials to the client and cannot carry MFA or WebAuthn.
> >
> > Follow-up: What should a first-party mobile app that used the password grant move to?

> [!question] L5: A redirect URI is registered as `https://app.example/cb`. Which of `https://app.example/cb/`, `https://APP.example/cb`, and `https://app.example:443/cb` match?
>
> > [!success]- Answer
> > None of them.
> > OAuth 2.1 and RFC 9700 require simple string comparison per RFC 3986 Section 6.2.1, with no normalization of case, ports, or trailing slashes.
> > The only exception is the port of a loopback IP literal redirect for native apps.
> >
> > Follow-up: Why is `http://localhost:{port}` not given the same exception in the lab?

> [!question] L6: Predict the output. An Authlib 1.9.5 server with the default `CodeChallenge` stores the method exactly as received. A client sends `code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM` with no `code_challenge_method`, then redeems with `code_verifier=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk`. What does the token endpoint return?
>
> > [!success]- Answer
> > An `invalid_grant` error with the description "Code challenge failed.".
> > The stored method is `None`, so `validate_code_verifier` falls back to `DEFAULT_CODE_CHALLENGE_METHOD`, which is `"plain"`.
> > `plain` compares the verifier string to the challenge string directly, and the `S256` challenge is not equal to the verifier.
> > The same pair succeeds if the method `S256` is sent, which is the RFC 7636 Appendix B vector.
> >
> > Follow-up: How would you change the server so that this request is rejected at the authorization endpoint instead?

> [!question] L6: Walk through the PKCE downgrade attack and the single server-side rule that stops it.
>
> > [!success]- Answer
> > The attacker starts a flow at the client on their own device and strips `code_challenge`, so the server issues a code bound to nothing.
> > The attacker then delivers that code to the victim's browser at the client's redirect URI.
> > The client redeems it with the verifier from the victim's session, and a server that skips PKCE when no challenge was stored issues a token for the attacker's account into the victim's session.
> > The rule from RFC 9700 Section 4.8.2: if no challenge was bound to the code, reject any token request that carries a `code_verifier`.
> > A server that requires PKCE for every client implements this implicitly.
> >
> > Follow-up: Why does correct `state` handling also stop this, and why does RFC 9700 still put the duty on the server?

> [!question] L6: Debug this API. A desktop app's login works on developers' machines but fails for some users with `invalid_request` at `/authorize`, and support says it is worse on machines running a local web server. The app registers `http://localhost:8080/callback` and falls back to a random port when 8080 is busy.
>
> > [!success]- Answer
> > When 8080 is taken, the app sends `http://localhost:51234/callback`, which is not string-equal to the registration.
> > The server correctly applies the port exception only to loopback IP literals, as draft Section 8.4.2 describes, so the request is rejected without a redirect.
> > The fix is to register `http://127.0.0.1/callback` and `http://[::1]/callback`, always bind an ephemeral port on the loopback interface, and close it after the response.
> > Using the IP literal also avoids `localhost` resolving to a non-loopback interface.
> >
> > Follow-up: What other app on the same machine could intercept a loopback redirect, and what still protects the code if it does?

> [!question] L6: Why does OAuth 2.1 drop `redirect_uri` from the token request, and what must a server that serves both OAuth 2.0 and 2.1 clients do?
>
> > [!success]- Answer
> > In RFC 6749 that comparison was the defense against a code obtained through a manipulated redirect URI, and providers often skipped it.
> > PKCE binds the code to the transaction more strongly, so the parameter serves no purpose in OAuth 2.1 (draft Section 10.2).
> > A server with OAuth 2.0 clients MUST still accept the parameter and enforce RFC 6749 Section 4.1.3 for them, deciding per `client_id`.
> > An OAuth 2.1 client will fail against a server that insists on the parameter.
> >
> > Follow-up: Revision 17 still mentions the redirect URI in the step list under Figure 3; how would you resolve that inconsistency in an implementation?

> [!question] L7+: A second valid redemption of a code arrives. What should the server do, and why must it not do the same for an invalid one?
>
> > [!success]- Answer
> > It must refuse to issue tokens and SHOULD revoke every access and refresh token already issued from that code, because the first redeemer may have been an attacker (draft Section 4.1.3).
> > It must not revoke on a replay that fails client authentication or the verifier check, because then anyone who learns a code could revoke the legitimate session on demand (draft Section 7.5.3).
> > The state transition from issued to redeemed must be atomic, or two concurrent valid requests both succeed, as in NocoDB CVE-2026-47386.
> >
> > Follow-up: How do you implement the atomic transition in PostgreSQL and in Redis, and what do you keep after the code expires so that a late replay can still trigger revocation?

> [!question] L7+: Your platform's authorization server serves 400 third-party clients, some using implicit and wildcard redirects. Design the migration to OAuth 2.1.
>
> > [!success]- Answer
> > Add a per-client rules profile, because OAuth 2.1 has no version signal and draft Section 1.8 expects per-client configuration.
> > Instrument first: count, per client, requests without `S256`, with `response_type=token`, with password grants, with redirect URIs that only match a pattern, and with tokens in query strings.
> > Default new clients to the strict profile, publish a deprecation date, and convert pattern registrations into explicit lists of exact URIs with the owners.
> > Turn on the server-side rules that break nobody immediately: reject verifier-without-challenge, accept only `S256` for new challenges, add `iss` to every response, never redirect on invalid client or URI, use `303`.
> > For clients that cannot move in time, issue long-lived refresh tokens before switching off the legacy method; draft Section 7.5.2 states this for clients that cannot support `S256`, and the same reasoning applies to password-grant clients.
> >
> > Follow-up: Which of these changes can silently break a client that is already compliant with RFC 6749, and how do you detect that before customers do?

> [!question] L7+: When does a confidential client not need PKCE under OAuth 2.1, and why would you still require it?
>
> > [!success]- Answer
> > Only when the server has reasonable assurance that the client implements the OpenID Connect `nonce` check correctly (draft Section 7.5.1.1).
> > With `nonce` the server still issues tokens for an injected code, and only the client's later ID Token check prevents misuse; the server cannot verify the client did it.
> > With PKCE the server refuses to issue anything, and it also gets downgrade protection and CSRF protection for free.
> > Requiring PKCE uniformly removes a per-client branch, which is exactly the branch most of the CVEs above got wrong.
> >
> > Follow-up: What does PKCE not protect against, per RFC 9700 Section 4.5.4?

## Related

Prerequisites: [[Password-and-Client-Secret-Hashing-with-Argon2id-and-Pwdlib]]

Next: [[OAuth-2-0-for-Browser-Based-Applications-BFF-and-Cookies-RFC-10017]]

See also: [[API-Authentication-and-Authorization]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

- Same folder: [[API-Authentication-Architecture-Edge-Verification-vs-Zero-Trust-Propagation]], [[OpenID-Connect-OIDC-ID-Tokens-vs-Access-Tokens-and-Metadata-Discovery]], [[Refresh-Token-Rotation-Reuse-Detection-and-Token-Revocation-Architectures]], [[Sender-Constrained-Tokens-DPoP-RFC-9449-and-mTLS-Binding-RFC-8705]], [[Pushed-Authorization-Requests-PAR-RFC-9126-and-Rich-Authorization-Requests-RAR-RFC-9396]], [[Service-to-Service-Identity-Client-Credentials-and-Token-Exchange-RFC-8693]], [[JWT-JWS-JWK-Structure-and-RFC-9068-Access-Token-Profile]], [[Enterprise-SSO-Integration-Multi-Tenant-OIDC-and-SCIM-Provisioning]]
- Security: [[CSRF-in-Cookie-Authenticated-APIs-SameSite-Double-Submit-and-Fetch-Metadata]], [[CORS-Protocol-Internals-Preflight-Caching-and-Origin-Reflection-Exploits]], [[OWASP-API-Security-Top-10-2023-Complete-Threat-Model]], [[Structured-Log-Redaction-and-Preventing-PII-and-Token-Leaks]]
- Lab: `labs/tests/f14_api_authentication_and_identity/test_oauth_2_0_to_oauth_2_1_and_rfc_9700_security_bcp.py`

## Further reading

- RFC 9700, Best Current Practice for OAuth 2.0 Security: the attacker model, the attacks, and every MUST that OAuth 2.1 adopts.
- draft-ietf-oauth-v2-1-17, The OAuth 2.1 Authorization Framework: the consolidated text; Section 10 lists the differences from OAuth 2.0 and Appendix F the per-revision changes.
- RFC 6749, The OAuth 2.0 Authorization Framework: the baseline, including the implicit and password grants and the original code and redirect rules.
- RFC 7636, Proof Key for Code Exchange: the verifier and challenge construction, the `S256` vector, and why `plain` exists.
- RFC 9207, OAuth 2.0 Authorization Server Issuer Identification: the `iss` parameter and the exact client-side comparison rules for mix-up defense.
- RFC 8252, OAuth 2.0 for Native Apps: loopback, claimed `https`, and private-use scheme redirects, and why embedded user agents are unsafe.
- RFC 8414, OAuth 2.0 Authorization Server Metadata: `code_challenge_methods_supported` and the discovery that lets clients rely on PKCE.
- RFC 6819, OAuth 2.0 Threat Model and Security Considerations: the threat catalogue that RFC 9700 updates.
- RFC 6750, Bearer Token Usage: the query-parameter method that OAuth 2.1 removes.
- RFC 9126, Pushed Authorization Requests: moving the authorization request off the front channel.
- RFC 9449, DPoP: sender-constraining tokens so leaked tokens are not enough.
- Authlib 1.9.5 `authlib/oauth2/rfc7636/challenge.py` at tag `v1.9.5`: a production PKCE implementation whose defaults show what still has to be configured for OAuth 2.1.
- NVD records CVE-2023-48228, CVE-2024-23647, CVE-2024-22258, CVE-2025-4144, CVE-2026-26247, CVE-2026-47386, and CVE-2026-126276: real servers getting one PKCE or single-use conditional wrong.
