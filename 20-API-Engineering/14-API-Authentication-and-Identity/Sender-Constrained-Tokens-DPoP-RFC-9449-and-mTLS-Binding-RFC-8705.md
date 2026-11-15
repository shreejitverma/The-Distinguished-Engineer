---
type: pattern
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://www.rfc-editor.org/rfc/rfc9449.html"
  - "https://www.rfc-editor.org/rfc/rfc8705.html"
  - "https://www.rfc-editor.org/rfc/rfc7638.html"
  - "https://www.rfc-editor.org/rfc/rfc6750.html"
  - "https://www.rfc-editor.org/rfc/rfc8725.html"
  - "https://www.rfc-editor.org/rfc/rfc9700.html"
  - "https://www.rfc-editor.org/rfc/rfc9110.html"
  - "https://www.rfc-editor.org/rfc/rfc8037.html"
  - "https://www.rfc-editor.org/rfc/rfc8446.html"
  - "https://www.rfc-editor.org/rfc/rfc3986.html"
  - "https://www.rfc-editor.org/rfc/rfc9421.html"
  - "https://www.rfc-editor.org/rfc/rfc9530.html"
  - "https://www.rfc-editor.org/rfc/rfc9112.html"
  - "https://www.rfc-editor.org/rfc/rfc9113.html"
  - "https://www.rfc-editor.org/rfc/rfc9114.html"
  - "https://www.rfc-editor.org/rfc/rfc7541.html"
  - "https://www.rfc-editor.org/rfc/rfc9204.html"
  - "https://www.rfc-editor.org/rfc/rfc8471.html"
  - "https://www.rfc-editor.org/rfc/rfc8473.html"
  - "https://openid.net/specs/openid-financial-api-part-2-1_0.html"
  - "https://openid.net/specs/fapi-security-profile-2_0-final.html"
  - "https://nvd.nist.gov/vuln/detail/CVE-2022-21449"
  - "https://www.envoyproxy.io/docs/envoy/latest/api-v3/extensions/filters/network/http_connection_manager/v3/http_connection_manager.proto"
  - "https://owasp.org/API-Security/editions/2023/en/0x11-t10/"
---

# Stopping Stolen Bearer Token Replay: Demonstrating Proof of Possession (DPoP, RFC 9449) and mTLS Certificate-Bound Tokens (RFC 8705)

> [!summary] Claim
> Sender-constrained tokens neutralize stolen credential replay by cryptographically binding the access token to client-held private keys, either at the HTTP layer through application-level DPoP proof signatures or at the TLS layer through mutual TLS certificate thumbprints.

Part of [API Authentication and Identity](README.md) in [API Engineering](../README.md).

## TL;DR

Bearer tokens grant full API authority to any party holding the raw token string, leaving systems vulnerable to replay attacks whenever tokens leak from access logs, browser storage, or network intermediaries.
DPoP (RFC 9449) binds every request to an ephemeral or persistent asymmetric key pair via a signed JWS in the `DPoP` HTTP header containing the HTTP method, the target URI without query and fragment, and the access token hash.
Mutual TLS (RFC 8705) binds access tokens to the client X.509 certificate presented during the TLS handshake, embedding the certificate SHA-256 thumbprint in the token confirmation claim.
Resource servers supporting both DPoP and Bearer must reject a DPoP-bound token presented as Bearer (RFC 9449 Section 7.2), or attackers simply strip the proof header and replay the stolen token.
Do not use DPoP when hardware-enforced mTLS is already established across a private service mesh, and do not use mTLS when browser-based single-page applications cannot manage client certificates.

## Mental model

```mermaid
sequenceDiagram
    autonumber
    actor Client as Legitimate Client<br/>(Holds Private Key)
    participant AS as Authorization Server
    participant Edge as Edge Gateway / Proxy
    participant RS as Resource Server
    actor Attacker as Adversary<br/>(Stole Access Token)

    rect rgb(240, 248, 255)
    Note over Client,AS: 1. Token Issuance with Key Binding
    Client->>AS: POST /oauth/token<br/>(DPoP Header JWS / mTLS Client Cert)
    AS->>AS: Compute JWK Thumbprint (RFC 7638) or Cert Thumbprint (RFC 8705)
    AS-->>Client: 200 OK {"access_token": "tok_xyz", "token_type": "DPoP"}<br/>(tok_xyz carries cnf.jkt = "0Zc...")
    end

    rect rgb(245, 255, 245)
    Note over Client,RS: 2. Authenticated Resource Access
    Client->>RS: GET /api/accounts<br/>Authorization: DPoP tok_xyz<br/>DPoP: <Signed JWS (htm=GET, htu=..., ath=H(tok_xyz))>
    RS->>RS: Verify JWS Signature, ath Hash, URI, Method, and cnf.jkt Binding
    RS-->>Client: 200 OK {"account_id": "acc-99881", "balance": 150000.0}
    end

    rect rgb(255, 240, 240)
    Note over Attacker,RS: 3. Replay Attack Prevented
    Note over Attacker: Attacker steals "tok_xyz" from proxy log or memory dump
    Attacker->>RS: GET /api/accounts<br/>Authorization: Bearer tok_xyz (Downgrade attempt)
    RS-->>Attacker: 401 Unauthorized (RFC 9449 Sec 7.2: Bearer downgrade rejected)
    Attacker->>RS: GET /api/accounts<br/>Authorization: DPoP tok_xyz<br/>DPoP: <JWS signed by Attacker's Key>
    RS-->>Attacker: 401 Unauthorized (Thumbprint mismatch: Attacker jkt != Token cnf.jkt)
    end
```

The flow traces three lifecycle phases across the authorization boundary.
In phase one, the client generates a cryptographic key pair and presents its public key during token issuance, enabling the authorization server to compute an immutable thumbprint (`jkt` or `x5t#S256`) and embed it in the access token confirmation claim (`cnf`).
In phase two, the client signs a fresh DPoP proof for each outbound HTTP request, binding the signature directly to the target URI, the HTTP method, and the access token digest (`ath`).
In phase three, an adversary who captures the raw access token attempts two distinct replay strategies: downgrading the request to the classic Bearer scheme, and signing a new DPoP proof using an attacker-controlled key pair.
The resource server halts both attacks: the downgrade check catches the missing proof on a bound token, and the thumbprint verification detects that the attacker public key does not match the token embedded `cnf` claim.

## How it works

### The Bearer Token Vulnerability

OAuth 2.0 Bearer Token Usage defines a bearer token in `(RFC 6750 Section 1.2)` as a security token that any party in possession of it can use, without demonstrating possession of a cryptographic key.
Bearer tokens make no demand that the presenter demonstrate knowledge of a secret key or ownership of the identity behind the token.
This design creates an architectural single point of failure: any compromise of the token string across its transmission path or storage lifecycle results in complete account takeover.
Common exfiltration vectors include reverse proxy access logging, browser storage inspection, Server-Side Request Forgery (SSRF) metadata exfiltration, and Transport Layer Security (TLS) termination inspection.
Sender-constrained tokens eliminate this failure mode by separating authorization state from possession proof.
Under sender-constraining, holding the token string is insufficient to execute an API call; the caller must actively prove possession of the private key bound to that token, per request with DPoP or in the TLS handshake with mTLS.

### DPoP Proof JWS Structure (RFC 9449)

Demonstrating Proof of Possession (DPoP) is an application-level mechanism `(RFC 9449 Section 1)`.
A DPoP proof is a JWT signed as a compact JSON Web Signature (JWS) and transmitted inside the `DPoP` HTTP request header `(RFC 9449 Section 4.1)`.
The JOSE header of a DPoP proof must contain at least three parameters `(RFC 9449 Section 4.2)`; the example below is the header of `(RFC 9449 Section 7.1, Figure 14)`:

```json
{
  "typ": "dpop+jwt",
  "alg": "ES256",
  "jwk": {
    "kty": "EC",
    "crv": "P-256",
    "x": "l8tFrhx-34tV3hRICRDY9zCkDlpBhF42UQUfWVAWBFs",
    "y": "9VE4jf_Ok_o64zbTTlcuNJajHmt6v9TDVrU0CdvGRDA"
  }
}
```

The `typ` parameter must be exactly `dpop+jwt`, following the explicit typing recommendation of `(RFC 8725 Section 3.11)`, and servers MUST check it so JWTs minted for other purposes cannot be swapped in `(RFC 9449 Section 11.5)`.
The `alg` parameter must declare an asymmetric digital signature algorithm from the IANA JOSE registry, such as `ES256`, `EdDSA`, or `PS256`.
Symmetric algorithms (`HS256`, `HS384`, `HS512`) and `none` are prohibited by `(RFC 9449 Section 4.2)` and again by `(RFC 9449 Section 11.6)`.
The `jwk` parameter contains the public key corresponding to the private key used to sign the proof.
The `jwk` parameter must not contain a private key `(RFC 9449 Section 4.2)`; in JWK terms that means no `d` (EC, OKP, RSA) and none of the RSA CRT members `p`, `q`, `dp`, `dq`, `qi`.

The JWS payload contains claims binding the proof to the immediate HTTP request context.
The example combines the payload of `(RFC 9449 Section 7.1, Figure 14)` with the `nonce` value of `(RFC 9449 Section 8, Figure 21)`:

```json
{
  "jti": "e1j3V_bKic8-LAEB",
  "htm": "GET",
  "htu": "https://resource.example.org/protectedresource",
  "iat": 1562262618,
  "ath": "fUHyO2r2Z3DZ53EsNrWBb0xWXoaNy59IiKCAqksmQEo",
  "nonce": "eyJ7S_zG.eyJH0-Z.HX4w-7v"
}
```

The `jti` claim provides a unique identifier built from at least 96 bits of pseudorandom data or a UUIDv4 string, enabling the server to implement single-use replay protection `(RFC 9449 Section 4.2)`.
The `htm` claim carries the HTTP request method `(RFC 9110 Section 9.1)`; method tokens are case-sensitive, so the lab compares `htm` to the request method exactly rather than case-folding.
The `htu` claim specifies the HTTP target URI without query string or fragment components, matching `(RFC 9110 Section 7.1)`.
The `iat` claim records the proof issuance time in seconds since the Unix epoch.
The `ath` claim contains the base64url-encoded SHA-256 hash of the ASCII access token string.
The `ath` claim is mandatory whenever the DPoP proof accompanies an access token to a protected resource, preventing proof transplantation across different access tokens.
The `nonce` claim reflects an opaque challenge issued by the server in a preceding `DPoP-Nonce` header, and is mandatory once a server has provided one `(RFC 9449 Sections 4.2 and 11.3)`.

### The 12-Step DPoP Verification Checklist (RFC 9449 Section 4.3)

A receiving server (authorization server or resource server) must ensure all twelve conditions of `(RFC 9449 Section 4.3)`, which "may be performed in any order":

1. Confirm that the request contains at most one `DPoP` HTTP header field.
2. Confirm that the `DPoP` header field value is a single, well-formed compact JWS string.
3. Confirm that all required claims (`jti`, `htm`, `htu`, `iat`) are present in the payload.
4. Confirm that the JOSE header `typ` parameter is exactly `dpop+jwt`.
5. Confirm that the JOSE header `alg` parameter indicates a registered asymmetric algorithm, is not `none`, is supported by the application, and is acceptable per local policy.
6. Verify the JWS cryptographic signature using the public key supplied in the `jwk` header parameter.
7. Confirm that the public key in `jwk` does not contain a private key.
8. Confirm that the `htm` claim matches the HTTP method of the current request.
9. Confirm that the `htu` claim matches the HTTP URI of the current request, ignoring query and fragment parts; servers SHOULD apply syntax-based and scheme-based normalization `(RFC 3986 Sections 6.2.2 and 6.2.3)` before comparing.
10. If the server provided a nonce to the client, confirm that the `nonce` claim matches it.
11. Confirm that the creation time, from `iat` or from a server-managed timestamp inside the nonce, is within an acceptable window `(RFC 9449 Section 11.1)`.
12. If presented to a protected resource with an access token, verify that `ath` equals the hash of that token and that the public key the token is bound to (for JWT access tokens, `cnf.jkt`) matches the proof's `jwk`.

### JWK Thumbprint Computation (RFC 7638)

The cryptographic binding between an access token and a DPoP key relies on the JSON Web Key Thumbprint defined in `(RFC 7638 Section 3)`.
The thumbprint is a hash over a canonical JSON object containing only the required members of the key, with no whitespace and members in lexicographic order `(RFC 7638 Section 3.3)`; RFC 9449 fixes the hash to SHA-256 `(RFC 9449 Section 6.1)`.
Optional members such as `kid`, `use`, `alg`, and `key_ops` are excluded `(RFC 7638 Section 3.2)`, so the thumbprint identifies the key rather than the key plus attributes `(RFC 7638 Section 3.2.2)`.
For Elliptic Curve keys (`kty: "EC"`), the required members in lexicographical order are `crv`, `kty`, `x`, and `y` `(RFC 7638 Section 3.2)`.
For RSA keys (`kty: "RSA"`), the required members are `e`, `kty`, and `n` `(RFC 7638 Section 3.2)`.
For Octet Key Pairs (`kty: "OKP"`), such as Ed25519, the required members are `crv`, `kty`, and `x` `(RFC 8037 Section 2)`.

```text
Input JWK:
{"kty":"EC","crv":"P-256","x":"l8tFrhx...","y":"9VE4jf...","kid":"key-1"}

Canonical UTF-8 JSON String (sorted, no whitespace):
{"crv":"P-256","kty":"EC","x":"l8tFrhx...","y":"9VE4jf..."}

Hash and Encoding:
jkt = base64url(SHA256(canonical_bytes))
```

The resulting 43-character base64url string (32 bytes, unpadded) is placed in a JWT access token under the confirmation claim `{"cnf": {"jkt": "<thumbprint>"}}` `(RFC 9449 Section 6.1)`, or returned by token introspection for opaque tokens `(RFC 9449 Section 6.2)`.
The full public key from Figure 14 hashes to `0ZcOCORZNYy-DWpqq30jZyJGHTN0d2HglBV3uiguA4I`, the `jkt` value of `(RFC 9449 Section 6.1, Figure 9)`; the lab test `test_rfc9449_ec_thumbprint_and_ath_test_vector` asserts exactly this value.

### Server-Provided Nonces and Replay Prevention

While the `jti` claim prevents immediate duplicate replays, it does not stop an attacker who controls client code from pre-generating valid DPoP proofs for future timestamps `(RFC 9449 Section 11.2)`.
An adversary with transient access to a client private key could pre-generate proofs with future `iat` values and exfiltrate them.
To eliminate this risk, authorization servers and resource servers supply unpredictable nonces via the `DPoP-Nonce` HTTP response header `(RFC 9449 Sections 8 and 9)`.
An authorization server signals a missing or mismatched nonce with `400 Bad Request` and the JSON error `use_dpop_nonce` `(RFC 9449 Section 8, Figure 20)`.
A resource server signals it with `401 Unauthorized` and a `WWW-Authenticate: DPoP` challenge `(RFC 9449 Section 9, Figure 24)`:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: DPoP error="use_dpop_nonce", error_description="Resource server requires nonce in DPoP proof"
DPoP-Nonce: eyJ7S_zG.eyJH0-Z.HX4w-7v
```

The client extracts the nonce from the response header, injects it into the `nonce` claim of a newly signed DPoP proof, and retries the request.
Server-issued nonces limit the validity window of any DPoP proof to the lifetime of the nonce itself, preventing pre-generation attacks; when to rotate the nonce is left entirely to the server `(RFC 9449 Section 8.2)`.
Responses carrying `DPoP-Nonce` should be uncacheable, for example `Cache-Control: no-store` `(RFC 9449 Section 8.2)`, and browser clients can read the header only if the server lists it in `Access-Control-Expose-Headers` `(RFC 9449 Section 8)`.
Servers can store the `jti` of each proof, in the context of the target URI, for the window in which the proof would be accepted; to guard against memory exhaustion they "should reject DPoP proof JWTs with unnecessarily large jti values or store only a hash thereof" `(RFC 9449 Section 11.1)`.
The lab stores the SHA-256 hex digest of `jti|htu` with a TTL equal to its clock-skew window.

### Bearer Downgrade Prevention (RFC 9449 Section 7.2)

A common architectural vulnerability during DPoP migration is protocol downgrade.
If a resource server supports both classic Bearer authentication `(RFC 6750)` and DPoP `(RFC 9449)`, an attacker holding a stolen DPoP-bound token might attempt to present it using the `Bearer` scheme:

```http
GET /api/accounts HTTP/1.1
Host: api.example.com
Authorization: Bearer dpop_bound_access_token_xyz
```

Because an unhardened bearer validator ignores unrecognized claims (JWT processing ignores unknown claims, as `(RFC 9449 Section 7.2)` notes), it would validate the token signature and grant access without checking for a DPoP proof header.
`(RFC 9449 Section 7.2)` states that a protected resource simultaneously supporting both the DPoP and Bearer schemes "MUST reject a DPoP-bound access token received as a bearer token".
For JWT access tokens the server checks for a `cnf.jkt` member (for opaque tokens, the introspection response); if present, the request fails with `401 Unauthorized` and a `Bearer error="invalid_token"` challenge, the `RFC 6750 Section 3.1` error for this case.
The check must key on `cnf.jkt` specifically: an RFC 8705 certificate-bound token carries `cnf["x5t#S256"]` and is legitimately presented with the `Bearer` scheme over mutual TLS `(RFC 8705 Section 3)`.

### Mutual TLS Certificate-Bound Tokens (RFC 8705)

Mutual TLS (mTLS) certificate binding operates at the TLS layer, below HTTP, using the client certificate presented in the TLS handshake `(RFC 8705 Section 3)`.
During the TLS 1.3 handshake, the server sends a `CertificateRequest` message `(RFC 8446 Section 4.3.2)`.
The client responds with its X.509 certificate and signs the handshake transcript using its private key in the `CertificateVerify` message `(RFC 8446 Section 4.4.3)`.
Because the token is application data that arrives only after the handshake, the resource server cannot use the token to decide whether to request a certificate; it must know in advance that mTLS is required, typically on a dedicated host and port `(RFC 8705 Section 3)`.
When the client requests an access token at the authorization server token endpoint over mTLS, the authorization server computes the SHA-256 thumbprint of the client certificate:

```text
x5t#S256 = base64url(SHA256(DER_encoded_client_certificate))
```

The authorization server embeds this fingerprint in the access token under the confirmation claim `{"cnf": {"x5t#S256": "<thumbprint>"}}` `(RFC 8705 Section 3.1)`.
The value is the unpadded base64url SHA-256 hash of the DER encoding of the certificate, with no line breaks or whitespace `(RFC 8705 Section 3.1)`; the lab's `compute_cert_thumbprint` implements exactly this.
When the client calls the protected resource, it sends the token with the ordinary `Bearer` scheme `(RFC 6750)` over a mutually authenticated TLS connection using the same certificate it used at the token endpoint `(RFC 8705 Section 3)`.
The resource server obtains the client certificate from its TLS layer, computes its DER thumbprint, and asserts that it matches `cnf["x5t#S256"]`; a mismatch must be rejected with `401` and `invalid_token` `(RFC 8705 Section 3)`.
The resource server does not need to validate the certificate's chain to a trusted CA, because mTLS here serves purely as proof of possession `(RFC 8705 Section 6.2)`.
If an adversary intercepts the access token, they cannot use it over a different TLS connection without presenting the matching private key during the handshake.
Rotating the client certificate invalidates every token bound to the old one, so clients refresh and retry as if the token had expired `(RFC 8705 Section 6.3)`.

```mermaid
flowchart TD
    Req["Incoming HTTP Request"] --> SchemeCheck{"Authorization Scheme?"}
    SchemeCheck -- "Bearer" --> CheckBound{"Token cnf member?"}
    CheckBound -- "cnf.jkt" --> RejectDowngrade["401 Unauthorized invalid_token<br/>(RFC 9449 Sec 7.2 downgrade)"]
    CheckBound -- "none" --> AllowBearer["Process as Bearer Token"]
    CheckBound -- "cnf.x5t#S256" --> ExtractCert

    SchemeCheck -- "DPoP" --> DPoPCheck["Extract DPoP Header & Access Token"]
    DPoPCheck --> VerifyJWS["Verify JWS Signature with embedded JWK<br/>(alg != none, alg != symmetric)"]
    VerifyJWS --> CheckClaims["Validate htm, htu, iat, ath, nonce"]
    CheckClaims --> ReplayCheck{"jti already in Replay Cache?"}
    ReplayCheck -- "Yes" --> RejectReplay["401 Unauthorized (invalid_dpop_proof)"]
    ReplayCheck -- "No" --> BindingCheck{"JWK Thumbprint == token.cnf.jkt?"}
    BindingCheck -- "No" --> RejectKey["401 Unauthorized (Key Mismatch)"]
    BindingCheck -- "Yes" --> RecordJti["Record jti in TTL Replay Cache"]
    RecordJti --> Authorized["200 OK: Request Authorized"]

    ExtractCert["Extract Client X.509 Cert from TLS Handshake<br/>(RFC 8705 Sec 3)"] --> CertHash["Compute SHA-256 DER Thumbprint (x5t#S256)"]
    CertHash --> MTLSMatch{"Cert Thumbprint == token.cnf.x5t#S256?"}
    MTLSMatch -- "No" --> RejectMTLS["401 Unauthorized invalid_token (Certificate Mismatch)"]
    MTLSMatch -- "Yes" --> Authorized
```

### Architectural Comparison: DPoP vs mTLS vs HTTP Signatures

| Architectural Dimension | DPoP (RFC 9449) | mTLS Binding (RFC 8705) | HTTP Signatures (RFC 9421) | Token Binding (RFC 8471, RFC 8473) |
|---|---|---|---|---|
| Protocol Layer | HTTP application layer (`DPoP` header) | TLS layer (client certificate in the handshake) | HTTP application layer (`Signature` and `Signature-Input` headers) | TLS extension plus `Sec-Token-Binding` HTTP header |
| Browser Client Viability | Good (WebCrypto non-extractable keys; RFC 9449 Section 1 names single-page apps as the target) | Poor (TLS client authentication in user agents has a "sub-par user experience", RFC 9449 Section 1) | Possible with WebCrypto; no OAuth profile binds tokens to it | Not usable ("lack of support for HTTP token binding", RFC 9449 Section 1) |
| Intermediary Survival | End-to-end through TLS-terminating proxies, provided the origin can reconstruct the public `htu` | Breaks at TLS termination unless the proxy forwards the certificate; how is out of scope (RFC 8705 Section 6.5) | End-to-end if covered components are not rewritten | Breaks at TLS termination |
| Per-Request Cost | Client signs one JWS (ES256 26 us), server verifies (ES256 109 us), 538-byte proof (lab, see Numbers) | No per-request signature; one thumbprint string comparison once the connection exists | One signature per message over the covered components | No per-request signature |
| Request Body Protection | None from DPoP itself; only `htm` and `htu` are signed (RFC 9449 Section 11.7); TLS protects each hop | TLS record protection per hop | Body covered only when `content-digest` (RFC 9530) is a covered component | TLS record protection per hop |
| Replay Protection | Proof lifetime bounded by the `iat` window, `jti` cache, and optional `DPoP-Nonce` (RFC 9449 Section 11.1) | No separate proof to replay; the token is usable only on connections authenticated with the bound key, until token expiry | Bounded by `created`/`expires` and optional `nonce` parameters | Bound to the TLS connection's exported keying material |
| Proxy Configuration Risk | Low for the proof; `htu` reconstruction behind proxies is the usual failure | High: the forwarded-certificate header must be stripped from client input and set only by the trusted proxy | Low | High |

## Numbers and limits

The operational boundaries, performance costs, and cryptographic constants governing sender-constrained tokens are detailed below.
Lab rows were re-measured on 2026-11-15 on an Apple Silicon Mac (macOS 27.3, Darwin 27.3.0 arm64) with CPython 3.14.6, `cryptography 50.0.2` (OpenSSL 4.0.3), and `PyJWT 2.15.1`.
The harness ran each operation with `timeit.repeat(number=N, repeat=7)` from `labs/` via `uv run --frozen python`, calling the lab functions shown in the Worked example; the table gives the median per-call time, and the minimum was within 3 percent of it in every row.
Proof verification is `DPoPVerifier.verify_proof` end to end (JWK parse, signature, claims, `ath`, `jkt`) with the replay cache stubbed out, and proof signing is `jwt.encode` of a proof carrying `ath`.

| Metric / Parameter | Value | Source / Benchmark Context |
|---|---|---|
| Minimum `jti` Entropy | 96 bits of pseudorandom data, or a UUIDv4 (122 random bits) | RFC 9449 Section 4.2 |
| Base64url SHA-256 Thumbprint Length | Exactly 43 characters | 32-byte digest, unpadded base64url; RFC 7638 Section 3.1 example, RFC 9449 Section 6.1 |
| Accepted DPoP Proof Age | Not fixed; "preferably only for a relatively brief period on the order of seconds or minutes" | RFC 9449 Section 11.1; the lab uses +/-120 s |
| Server Nonce Lifetime | Not fixed; the server decides when to issue a new nonce | RFC 9449 Section 8.2; the lab `NonceManager` default is 300 s |
| DPoP Proof Size (`ES256`, P-256 `jwk`, with `ath`) | 538 bytes | Lab measured (`len()` of the compact JWS) |
| DPoP Proof Size (`EdDSA`, Ed25519 `jwk`, with `ath`) | 476 bytes | Lab measured (`len()` of the compact JWS) |
| JWK Thumbprint Computation (EC P-256) | 2.10 us | Lab measured, `compute_jwk_thumbprint`, N=50000 |
| X.509 DER Certificate Thumbprint | 2.72 us | Lab measured, `compute_cert_thumbprint` on a parsed P-256 self-signed certificate, N=50000 |
| Access Token Hash (`ath`) Computation | 0.46 us | Lab measured, `compute_access_token_hash`, N=200000 |
| DPoP Proof Signing (`ES256`) | 26.22 us | Lab measured, N=5000 |
| DPoP Proof Signing (`EdDSA` / Ed25519) | 33.70 us | Lab measured, N=5000 |
| DPoP Proof Verification (`ES256`) | 109.05 us | Lab measured, N=3000 |
| DPoP Proof Verification (`EdDSA`) | 122.52 us | Lab measured, N=3000 |
| TLS 1.3 Handshake Round Trips | 1 RTT full handshake; 0-RTT early data possible with a resumption PSK | RFC 8446 Section 2 and Section 2.3 |
| mTLS Per-Request Binding Check | One string comparison of the thumbprint; not measured | The thumbprint can be computed once per connection |

## Portability

Sender-constrained tokens interact with the Python runtime, web framework ASGI abstractions, cryptographic C extensions, and HTTP wire transports.
The portability matrix across execution tiers is documented below:

| Runtime / Component | Version / Target | Compatibility Status | Notes and Upstream Evidence |
|---|---|---|---|
| CPython 3.12 | 3.12.16 | Not run | Leg not run: the lab matrix is paused (2026-11-11), so no 3.12.16 result is claimed |
| CPython 3.13 | 3.13.18 | Reported by drafter | The drafter reports 13/13 passing with `uv run --python 3.13`; not re-run in verification |
| CPython 3.14 | 3.14.6 | Supported | Workspace interpreter; 13/13 tests passed in verification (2026-11-15, output below); 3.14.10 not run |
| CPython 3.15 | 3.15.2 | Not run | Leg not run: no 3.15.2 result is claimed |
| CPython 3.15t | 3.15.2t (Free-threaded) | Not run | Leg not run: no free-threaded result is claimed; by reading the code, `InMemoryJtiReplayCache.check_and_record` is a check-then-insert without a lock, so concurrent callers could both accept the same `jti` |
| FastAPI | 0.162.0 | Supported | The lab's `verify_dpop_request` reads `request.headers.getlist("dpop")`; exercised by the ASGI tests |
| Starlette | 1.10.0 | Supported | `(starlette 1.10.0 starlette/requests.py, HTTPConnection.headers)` builds a `Headers` from the raw scope list, and `(starlette 1.10.0 starlette/datastructures.py, Headers.getlist)` returns every value, so a duplicated `DPoP` header is detectable |
| cryptography | 50.0.2 | Supported | `(cryptography 50.0.2 cryptography/hazmat/bindings/_rust/x509.pyi, Certificate.public_bytes)` returns the DER bytes hashed for `x5t#S256`; `cryptography/x509/base.py` re-exports the Rust `Certificate` |
| PyJWT | 2.15.1 | Supported | `(pyjwt 2.15.1 jwt/api_jwk.py, PyJWK.from_dict)` infers the algorithm from `kty`/`crv` when none is passed: `EC` (P-256, P-384, P-521, secp256k1), `RSA` (as `RS256`), `oct` (as `HS256`), and `OKP` only for `Ed25519`; the lab's `alg` allowlist and `compute_jwk_thumbprint` reject `oct` keys before this point |
| Uvicorn | 0.54.0 | Partial | `(uvicorn 0.54.0 uvicorn/config.py, Config)` accepts `ssl_cert_reqs` and `ssl_ca_certs` to require client certificates, but no source file exposes the peer certificate in the ASGI scope, so the app cannot compute `x5t#S256` itself; terminate mTLS at a proxy and forward the certificate |
| Hypercorn | 0.18.0 | Partial | `(hypercorn 0.18.0 hypercorn/config.py, Config)` has `verify_mode` and `ca_certs` and HTTP/3 via `quic_bind`; `hypercorn/protocol/http_stream.py` populates no TLS scope extension, so the same proxy pattern applies |
| Granian | 2.8.7 | Partial | `(granian 2.8.7 granian/cli.py, --ssl-ca and --ssl-client-verify)` verify client certificates; `(granian 2.8.7 granian/asgi.py, _callback_wrapper._runner)` adds only `root_path` and `state` to the scope, and neither `_granian.pyi` nor the strings of the compiled `_granian` extension contain a peer-certificate key, so no exposure was found; the native scope builder is Rust and was not read, so terminate mTLS at a proxy as with the other servers |
| HTTP/1.1 | RFC 9112 | Supported | The proof travels as an ordinary header of about 0.5 KB per request (Numbers) |
| HTTP/2 | RFC 9113 | Supported | HPACK (RFC 7541) indexes the repeated `dpop` field name; the value changes every request, so it gains only Huffman coding |
| HTTP/3 over QUIC | RFC 9114 | Supported | QPACK (RFC 9204) behaves like HPACK for this per-request value |

## Worked example

The implementation below is a teaching-grade sender-constrained token verifier supporting RFC 9449 DPoP and RFC 8705 mTLS certificate binding; its replay cache is single-process and unlocked, and its URI normalization covers only scheme, host, default port, and empty path.
It is linked directly to the repository lab files: [implementation](../labs/src/api_labs/f14_api_authentication_and_identity/sender_constrained_tokens.py) and [test](../labs/tests/f14_api_authentication_and_identity/test_sender_constrained_tokens_dpop_rfc_9449_and_mtls_binding_rfc_8705.py).

```python
"""Sender-constrained tokens: DPoP (RFC 9449) and mTLS binding (RFC 8705).

Provides cryptographic proof-of-possession verifiers, JWK thumbprint (RFC 7638)
and certificate thumbprint calculations, URI normalizers, and replay protection
for FastAPI and Starlette applications.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlparse, urlunparse

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException, Request, status

ALLOWED_DPOP_ALGORITHMS: frozenset[str] = frozenset(
    {"ES256", "ES384", "ES512", "EdDSA", "PS256", "PS384", "PS512", "RS256"}
)

JWK_PRIVATE_KEY_FIELDS: frozenset[str] = frozenset({"d", "p", "q", "dp", "dq", "qi"})


class DPoPVerificationError(HTTPException):
    """Exception raised when DPoP proof validation fails (RFC 9449 Section 4.3)."""

    def __init__(
        self,
        detail: str,
        error_code: str = "invalid_dpop_proof",
        status_code: int = status.HTTP_401_UNAUTHORIZED,
        dpop_nonce: str | None = None,
    ) -> None:
        headers: dict[str, str] = {
            "WWW-Authenticate": f'DPoP error="{error_code}", error_description="{detail}"'
        }
        if dpop_nonce:
            headers["DPoP-Nonce"] = dpop_nonce
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.error_code = error_code
        self.dpop_nonce = dpop_nonce


class MTLSVerificationError(HTTPException):
    """Exception raised when mTLS certificate binding fails (RFC 8705 Section 3)."""

    def __init__(
        self,
        detail: str,
        error_code: str = "invalid_token",
        status_code: int = status.HTTP_401_UNAUTHORIZED,
    ) -> None:
        headers: dict[str, str] = {
            "WWW-Authenticate": f'Bearer error="{error_code}", error_description="{detail}"'
        }
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.error_code = error_code


@dataclass(frozen=True)
class DPoPProofClaims:
    """Validated claims and metadata from an RFC 9449 DPoP proof."""

    jti: str
    htm: str
    htu: str
    iat: int
    ath: str | None
    nonce: str | None
    jwk: dict[str, Any]
    jkt: str


def compute_jwk_thumbprint(jwk_dict: dict[str, Any]) -> str:
    """Compute the SHA-256 JWK Thumbprint per RFC 7638 Section 3.

    Lexicographically orders required members and hashes canonical UTF-8 JSON.
    """
    kty = jwk_dict.get("kty")
    if kty == "EC":
        # RFC 7638 Section 3.2: crv, kty, x, y
        canonical = {
            "crv": jwk_dict["crv"],
            "kty": "EC",
            "x": jwk_dict["x"],
            "y": jwk_dict["y"],
        }
    elif kty == "RSA":
        # RFC 7638 Section 3.2: e, kty, n
        canonical = {
            "e": jwk_dict["e"],
            "kty": "RSA",
            "n": jwk_dict["n"],
        }
    elif kty == "OKP":
        # RFC 8037 Section 2: crv, kty, x
        canonical = {
            "crv": jwk_dict["crv"],
            "kty": "OKP",
            "x": jwk_dict["x"],
        }
    else:
        raise ValueError(f"Unsupported JWK key type (kty): {kty}")

    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical_json).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def compute_access_token_hash(access_token: str) -> str:
    """Compute the base64url-encoded SHA-256 hash of an access token (RFC 9449 Section 4.2)."""
    digest = hashlib.sha256(access_token.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def compute_cert_thumbprint(cert: x509.Certificate | bytes) -> str:
    """Compute the base64url-encoded SHA-256 thumbprint of an X.509 cert (RFC 8705 Section 3.1)."""
    if isinstance(cert, x509.Certificate):
        der_bytes = cert.public_bytes(serialization.Encoding.DER)
    else:
        der_bytes = cert
    digest = hashlib.sha256(der_bytes).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def normalize_target_uri(target_url: str) -> str:
    """Normalize an HTTP target URI before the RFC 9449 Section 4.3 htu comparison.

    Applies a subset of RFC 3986 Sections 6.2.2 and 6.2.3: lowercases scheme and
    host, removes default ports (80/443), maps an empty path to "/", and strips
    query and fragment parts. The path is otherwise compared verbatim.
    """
    parsed = urlparse(target_url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    if ":" in netloc:
        host, _, port_str = netloc.partition(":")
        if (scheme == "http" and port_str == "80") or (scheme == "https" and port_str == "443"):
            netloc = host

    path = parsed.path or "/"
    return urlunparse((scheme, netloc, path, "", "", ""))


class InMemoryJtiReplayCache:
    """Thread-safe in-memory cache for DPoP proof replay detection (RFC 9449 Section 11.1).

    Tracks seen (jti, htu) tuples and purges entries older than max_age_seconds.
    Stores SHA-256 hashes of jti values to prevent memory exhaustion attacks.
    """

    def __init__(self, max_age_seconds: int = 120) -> None:
        self.max_age_seconds = max_age_seconds
        self._entries: dict[str, float] = {}

    def _hash_key(self, jti: str, htu: str) -> str:
        data = f"{jti}|{htu}".encode()
        return hashlib.sha256(data).hexdigest()

    def check_and_record(self, jti: str, htu: str, current_time: float) -> bool:
        """Check if jti has been seen before for htu. Returns True if fresh, False if replay."""
        self._purge(current_time)
        key = self._hash_key(jti, htu)
        if key in self._entries:
            return False
        self._entries[key] = current_time
        return True

    def _purge(self, current_time: float) -> None:
        threshold = current_time - self.max_age_seconds
        expired = [k for k, ts in self._entries.items() if ts < threshold]
        for k in expired:
            del self._entries[k]


class NonceManager:
    """Server-provided nonce manager (RFC 9449 Sections 8 and 9)."""

    def __init__(self, secret_key: str, validity_seconds: int = 300) -> None:
        self.secret_key = secret_key.encode("utf-8")
        self.validity_seconds = validity_seconds

    def generate_nonce(self, current_time: float) -> str:
        """Generate an unpredictable nonce: timestamp plus truncated HMAC-SHA256 tag."""
        ts_bytes = str(int(current_time)).encode("ascii")
        sig = hmac.new(self.secret_key, ts_bytes, hashlib.sha256).digest()[:16]
        raw = ts_bytes + b"." + sig
        return base64.urlsafe_b64encode(raw).decode("ascii")

    def validate_nonce(self, nonce_str: str, current_time: float) -> bool:
        """Verify nonce signature and expiration."""
        try:
            raw = base64.urlsafe_b64decode(nonce_str.encode("ascii"))
            ts_bytes, sep, sig = raw.partition(b".")
            if not sep:
                return False
            expected_sig = hmac.new(self.secret_key, ts_bytes, hashlib.sha256).digest()[:16]
            if not hmac.compare_digest(sig, expected_sig):
                return False
            ts = int(ts_bytes.decode("ascii"))
            return abs(current_time - ts) <= self.validity_seconds
        except Exception:
            return False


@dataclass
class DPoPVerifier:
    """Full implementation of RFC 9449 Section 4.3 DPoP proof verification checklist."""

    replay_cache: InMemoryJtiReplayCache = field(
        default_factory=lambda: InMemoryJtiReplayCache(max_age_seconds=120)
    )
    nonce_manager: NonceManager | None = None
    require_nonce: bool = False
    max_clock_skew_seconds: int = 120

    def verify_proof(
        self,
        proof_jwt: str,
        request_method: str,
        request_url: str,
        access_token: str | None = None,
        expected_jkt: str | None = None,
        current_time: float | None = None,
    ) -> DPoPProofClaims:
        """Verify a DPoP proof against request context and access token binding."""
        now = time.time() if current_time is None else current_time

        # Step 2: Well-formed JWT
        try:
            unverified_header = jwt.get_unverified_header(proof_jwt)
        except Exception as exc:
            raise DPoPVerificationError("DPoP proof is not a well-formed JWT") from exc

        # Step 4: Header typ must be dpop+jwt
        typ = unverified_header.get("typ")
        if typ != "dpop+jwt":
            raise DPoPVerificationError(f"Invalid JOSE header typ '{typ}'; MUST be 'dpop+jwt'")

        # Step 5: alg is asymmetric, not none, not symmetric
        alg = unverified_header.get("alg")
        if not alg or alg not in ALLOWED_DPOP_ALGORITHMS:
            raise DPoPVerificationError(f"Unsupported or disallowed signature algorithm '{alg}'")

        # Step 6 & 7: Extract jwk, verify no private key
        raw_jwk = unverified_header.get("jwk")
        if not isinstance(raw_jwk, dict):
            raise DPoPVerificationError("Missing or invalid 'jwk' header parameter")

        jwk_dict: dict[str, Any] = cast("dict[str, Any]", raw_jwk)

        if any(private_field in jwk_dict for private_field in JWK_PRIVATE_KEY_FIELDS):
            raise DPoPVerificationError(
                "DPoP proof 'jwk' header parameter MUST NOT contain private key material"
            )

        # Compute JWK thumbprint per RFC 7638
        try:
            computed_jkt = compute_jwk_thumbprint(jwk_dict)
            public_key = jwt.PyJWK.from_dict(jwk_dict).key
        except Exception as exc:
            raise DPoPVerificationError(
                f"Failed to parse public key from 'jwk' header: {exc}"
            ) from exc

        # Step 6: Verify signature
        try:
            payload = jwt.decode(
                proof_jwt,
                public_key,
                algorithms=[alg],
                options={"verify_signature": True, "verify_exp": False},
            )
        except Exception as exc:
            raise DPoPVerificationError(f"DPoP proof signature verification failed: {exc}") from exc

        # Step 3: Required claims
        for required_claim in ("jti", "htm", "htu", "iat"):
            if required_claim not in payload:
                raise DPoPVerificationError(f"DPoP proof missing required claim '{required_claim}'")

        jti = str(payload["jti"])
        htm = str(payload["htm"])
        htu = str(payload["htu"])
        try:
            iat = int(payload["iat"])
        except (ValueError, TypeError) as exc:
            raise DPoPVerificationError(
                "DPoP proof 'iat' claim must be an integer timestamp"
            ) from exc

        # Step 8: htm matches HTTP method (case-sensitive, RFC 9110 Section 9.1)
        if htm != request_method:
            raise DPoPVerificationError(
                f"DPoP proof htm '{htm}' does not match request method '{request_method}'"
            )

        # Step 9: htu matches normalized target URI
        normalized_req_url = normalize_target_uri(request_url)
        normalized_htu = normalize_target_uri(htu)
        if normalized_htu != normalized_req_url:
            msg = (
                f"DPoP proof htu '{normalized_htu}' does not match "
                f"request URI '{normalized_req_url}'"
            )
            raise DPoPVerificationError(msg)

        # Step 10: Nonce validation
        nonce = payload.get("nonce")
        if self.require_nonce:
            if not nonce:
                new_nonce = (
                    self.nonce_manager.generate_nonce(now)
                    if self.nonce_manager
                    else "challenge-nonce"
                )
                raise DPoPVerificationError(
                    "Resource server requires nonce in DPoP proof",
                    error_code="use_dpop_nonce",
                    dpop_nonce=new_nonce,
                )
            if self.nonce_manager and not self.nonce_manager.validate_nonce(nonce, now):
                new_nonce = self.nonce_manager.generate_nonce(now)
                raise DPoPVerificationError(
                    "Stale or invalid DPoP nonce",
                    error_code="use_dpop_nonce",
                    dpop_nonce=new_nonce,
                )

        # Step 11: Creation time iat within acceptable window
        if abs(now - iat) > self.max_clock_skew_seconds:
            msg = (
                f"DPoP proof iat '{iat}' is outside the validity window "
                f"of +/-{self.max_clock_skew_seconds}s"
            )
            raise DPoPVerificationError(msg)

        # Step 12: Access token hash and public key binding
        ath = payload.get("ath")
        if access_token is not None:
            if not ath:
                raise DPoPVerificationError(
                    "DPoP proof missing mandatory 'ath' claim for protected resource access"
                )
            expected_ath = compute_access_token_hash(access_token)
            if ath != expected_ath:
                raise DPoPVerificationError(
                    f"DPoP proof ath '{ath}' does not match access token hash '{expected_ath}'"
                )
            if expected_jkt is not None and computed_jkt != expected_jkt:
                msg = (
                    f"DPoP proof key thumbprint '{computed_jkt}' does not "
                    f"match token binding '{expected_jkt}'"
                )
                raise DPoPVerificationError(msg)

        # Replay prevention check
        if not self.replay_cache.check_and_record(jti, normalized_req_url, now):
            raise DPoPVerificationError(
                f"DPoP proof jti '{jti}' has already been replayed for this URI"
            )

        return DPoPProofClaims(
            jti=jti,
            htm=htm,
            htu=htu,
            iat=iat,
            ath=ath,
            nonce=nonce,
            jwk=jwk_dict,
            jkt=computed_jkt,
        )


def verify_dpop_request(
    request: Request,
    verifier: DPoPVerifier,
    access_token: str | None = None,
    expected_jkt: str | None = None,
    current_time: float | None = None,
) -> DPoPProofClaims:
    """Extract and verify DPoP proof header from FastAPI/Starlette request."""
    # Step 1: Exactly one DPoP header
    dpop_headers = request.headers.getlist("dpop")
    if not dpop_headers:
        raise DPoPVerificationError(
            "Missing required DPoP header field",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    if len(dpop_headers) > 1:
        raise DPoPVerificationError(
            "Multiple DPoP header fields received",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    proof_jwt = dpop_headers[0]
    return verifier.verify_proof(
        proof_jwt=proof_jwt,
        request_method=request.method,
        request_url=str(request.url),
        access_token=access_token,
        expected_jkt=expected_jkt,
        current_time=current_time,
    )


def verify_mtls_binding(
    client_cert_thumbprint: str | None,
    expected_thumbprint: str,
) -> None:
    """Verify that client certificate thumbprint matches access token cnf['x5t#S256'].

    Follows RFC 8705 Section 3.1.
    """
    if not client_cert_thumbprint:
        raise MTLSVerificationError(
            "Client certificate missing or not presented in mutual TLS connection"
        )
    if client_cert_thumbprint != expected_thumbprint:
        msg = (
            f"Client certificate thumbprint '{client_cert_thumbprint}' does not "
            f"match token binding '{expected_thumbprint}'"
        )
        raise MTLSVerificationError(msg)
```

The captured output below is the verification run of 2026-11-15 from `labs/`, covering the RFC 7638 and RFC 9449 test vectors, replay detection, nonce challenges, downgrade blocks, and mTLS certificate binding under CPython 3.14.6 on macOS 27.3 (Darwin 27.3.0 arm64):

```text
$ uv run --frozen pytest tests/f14_api_authentication_and_identity/test_sender_constrained_tokens_dpop_rfc_9449_and_mtls_binding_rfc_8705.py -v
============================= test session starts ==============================
platform darwin -- Python 3.14.6, pytest-9.2.0, pluggy-1.7.0 -- /Users/shreejitverma/github/The-Distinguished-Engineer/build/api_notes/jobs/Sender-Constrained-Tokens-DPoP-RFC-9449-and-mTLS-Binding-RFC-8705/verify/labs/.venv/bin/python
cachedir: .pytest_cache
hypothesis profile 'default'
rootdir: /Users/shreejitverma/github/The-Distinguished-Engineer/build/api_notes/jobs/Sender-Constrained-Tokens-DPoP-RFC-9449-and-mTLS-Binding-RFC-8705/verify/labs
configfile: pyproject.toml
plugins: platformdirs-4.14.5, schemathesis-4.33.1, cov-7.1.0, hypothesis-6.170.14, inline-snapshot-0.36.1, Faker-40.44.0, locust-2.47.0, typeguard-4.6.0, anyio-4.16.0, asyncio-1.4.1.post1, respx-0.23.1
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collecting ... collected 13 items

tests/f14_api_authentication_and_identity/test_sender_constrained_tokens_dpop_rfc_9449_and_mtls_binding_rfc_8705.py::test_rfc7638_rsa_thumbprint_test_vector PASSED [  7%]
tests/f14_api_authentication_and_identity/test_sender_constrained_tokens_dpop_rfc_9449_and_mtls_binding_rfc_8705.py::test_rfc9449_ec_thumbprint_and_ath_test_vector PASSED [ 15%]
tests/f14_api_authentication_and_identity/test_sender_constrained_tokens_dpop_rfc_9449_and_mtls_binding_rfc_8705.py::test_rfc8705_cert_thumbprint_calculation PASSED [ 23%]
tests/f14_api_authentication_and_identity/test_sender_constrained_tokens_dpop_rfc_9449_and_mtls_binding_rfc_8705.py::test_normalize_target_uri_rules PASSED [ 30%]
tests/f14_api_authentication_and_identity/test_sender_constrained_tokens_dpop_rfc_9449_and_mtls_binding_rfc_8705.py::test_dpop_verifier_valid_proof PASSED [ 38%]
tests/f14_api_authentication_and_identity/test_sender_constrained_tokens_dpop_rfc_9449_and_mtls_binding_rfc_8705.py::test_dpop_verifier_rejects_replay_jti PASSED [ 46%]
tests/f14_api_authentication_and_identity/test_sender_constrained_tokens_dpop_rfc_9449_and_mtls_binding_rfc_8705.py::test_dpop_verifier_rejects_stolen_token_with_different_key PASSED [ 53%]
tests/f14_api_authentication_and_identity/test_sender_constrained_tokens_dpop_rfc_9449_and_mtls_binding_rfc_8705.py::test_dpop_verifier_rejects_method_and_uri_mismatch PASSED [ 61%]
tests/f14_api_authentication_and_identity/test_sender_constrained_tokens_dpop_rfc_9449_and_mtls_binding_rfc_8705.py::test_dpop_verifier_rejects_invalid_headers_and_private_keys PASSED [ 69%]
tests/f14_api_authentication_and_identity/test_sender_constrained_tokens_dpop_rfc_9449_and_mtls_binding_rfc_8705.py::test_dpop_nonce_challenge_workflow PASSED [ 76%]
tests/f14_api_authentication_and_identity/test_sender_constrained_tokens_dpop_rfc_9449_and_mtls_binding_rfc_8705.py::test_mtls_binding_verification PASSED [ 84%]
tests/f14_api_authentication_and_identity/test_sender_constrained_tokens_dpop_rfc_9449_and_mtls_binding_rfc_8705.py::test_fastapi_dpop_end_to_end_flow PASSED [ 92%]
tests/f14_api_authentication_and_identity/test_sender_constrained_tokens_dpop_rfc_9449_and_mtls_binding_rfc_8705.py::test_fastapi_mtls_binding_endpoint PASSED [100%]

============================== 13 passed in 0.30s ==============================
```

## Pitfalls

> [!warning] Bearer Downgrade Attack on Dual-Scheme Endpoints (RFC 9449 Section 7.2)
> **Trigger**: A resource server deploys DPoP support alongside classic Bearer authentication for backwards compatibility, but evaluates the incoming token with standard bearer validation middleware when no `DPoP` header is attached.
> **Symptom**: An adversary who exfiltrates a DPoP-bound access token from memory dumps or telemetry logs strips the `DPoP` header, presents the token under `Authorization: Bearer <token>`, and achieves unauthorized resource access without holding the client private key.
> **Fix**: Enforce the mandatory check from `(RFC 9449 Section 7.2)`: a resource server supporting both schemes must reject an access token received via the Bearer scheme if it is DPoP-bound, that is, if it carries `cnf.jkt` (or introspection reports one); do not reject on any `cnf`, because RFC 8705 certificate-bound tokens with `cnf["x5t#S256"]` are presented as Bearer by design.

> [!warning] DPoP Proof Pre-Generation and Exfiltration (RFC 9449 Section 11.2)
> **Trigger**: An API gateway or authorization server verifies DPoP proofs based solely on the client-supplied `iat` timestamp without enforcing server-provided nonces.
> **Symptom**: A compromised client machine or malicious insider pre-generates DPoP proofs with future `iat` timestamps and copies them to another machine; RFC 9449 gives the example of a bank employee doing this to bypass audit controls, at which point "all that is actually being proved ... is possession of a DPoP proof -- not of the proof-of-possession key" `(RFC 9449 Section 11.2)`.
> **Fix**: Implement the server-provided nonce mechanism from `(RFC 9449 Sections 8 and 9)` using `use_dpop_nonce` and the `DPoP-Nonce` header, and never accept a proof without a nonce once one has been issued `(RFC 9449 Section 11.3)`; without nonces, keep DPoP-bound access tokens short-lived, since `ath` limits pre-generated proofs to one token's lifetime `(RFC 9449 Section 11.2)`.

> [!warning] Reverse Proxy Header Injection on TLS Termination (RFC 8705 Section 6.5)
> **Trigger**: An edge reverse proxy (such as Envoy or NGINX) terminates client mTLS connections and forwards the client certificate or its thumbprint in an HTTP header (such as `X-Forwarded-Client-Cert` or `X-Client-Cert-Thumbprint`), but the upstream backend is reachable directly or the proxy passes client-supplied copies of that header through; RFC 8705 leaves this proxy-to-application channel out of scope `(RFC 8705 Section 6.5)`.
> **Symptom**: An external attacker who holds a stolen certificate-bound token sends a forged forwarded-certificate header containing the victim's certificate thumbprint, defeating mTLS sender-constraining without the victim's private key.
> **Fix**: Make the proxy strip the header from client input and set it only from the verified connection; Envoy's `forward_client_cert_details` defaults to `SANITIZE`, and the risky modes are `FORWARD_ONLY` and `ALWAYS_FORWARD_ONLY` (Envoy `HttpConnectionManager` documentation, as of 2026-11); also make the backend unreachable except through the proxy, ideally over internal mTLS.
> The lab's `/api/transfers` endpoint trusts an `x-ssl-client-thumbprint` header and is therefore only safe behind such a proxy.
> No public CVE for this exact misconfiguration was found (NVD search, as of 2026-11); RFC 8705 Section 6.5 names the trust boundary instead.

> [!warning] ECDSA Zero-Signature Acceptance / Psychic Signatures (CVE-2022-21449)
> **Trigger**: A verifier checks ECDSA DPoP proofs without asserting that the signature values `r` and `s` lie in `[1, n-1]`; Oracle Java SE 17.0.2 and 18 shipped exactly this bug (NVD CVE-2022-21449, CVSS 3.1 base score 7.5).
> **Symptom**: An all-zero signature (`r = 0`, `s = 0`) verifies for any payload and any public key, so an attacker can mint a DPoP proof for a victim's `jwk` without the private key, defeating the binding at every endpoint served by that runtime.
> **Fix**: Rely on vetted cryptographic libraries (the lab uses `cryptography 50.0.2`, which reports OpenSSL 4.0.3 in this environment); never write bespoke ASN.1 or elliptic curve verification; patch JVM-based gateways that verify ES256 proofs.

> [!warning] Memory Exhaustion via Unbounded `jti` Replay Caching (RFC 9449 Section 11.1)
> **Trigger**: An engineer implements DPoP single-use replay protection by caching raw `jti` string values in an in-memory set without length validation or digest hashing.
> **Symptom**: Attackers flood the API with validly signed DPoP proofs (any key works for a request that carries no access token, such as a token request) whose `jti` values are as long as the server's header size limit allows, and the cache grows by kilobytes per request until replicas run out of memory.
> **Fix**: Follow `(RFC 9449 Section 11.1)`: reject unnecessarily large `jti` values (for example above 64 bytes, a local policy choice) or store only a hash, keyed by target URI and expired after the proof acceptance window.
> The lab stores a SHA-256 hex digest of `jti|htu` but does not cap `jti` length, so the hashing is the only bound.

> [!warning] Request Body Tampering Gap in DPoP (RFC 9449 Section 11.7)
> **Trigger**: An architect assumes that DPoP guarantees end-to-end request body integrity because the request is cryptographically signed.
> **Symptom**: A compromised HTTP intermediary that terminates TLS (a CDN, gateway, or proxy) mutates the JSON body of a financial transfer (e.g., altering the recipient account number); the DPoP proof still validates because it covers only the HTTP method (`htm`) and target URI (`htu`), not the body or general headers `(RFC 9449 Section 11.7)`.
> **Fix**: Recognize the strict boundary of DPoP as a sender-constraining mechanism rather than a message signing protocol `(RFC 9449 Section 11.7)`; layer HTTP Message Signatures `(RFC 9421)` and Content-Digests `(RFC 9530)` when request payload integrity is required.

## Security and robustness

Sender-constrained tokens directly remediate OWASP API Security Top 10 vulnerabilities, specifically `API2:2023 Broken Authentication` and `API8:2023 Security Misconfiguration`.
By transforming authentication from a shared bearer secret into an asymmetric proof of possession, security architects establish defense in depth across the perimeter.

### Threat Modeling: Bearer vs DPoP vs mTLS

The matrix below maps threat vectors to mitigation effectiveness across token architectures:

| Attack Vector | Classic Bearer (RFC 6750) | DPoP (RFC 9449) | mTLS Binding (RFC 8705) |
|---|---|---|---|
| Access Token Leaked in Server Logs | Full Compromise (Replayable anywhere) | Protected (Attacker lacks private key) | Protected (Attacker lacks TLS client cert) |
| Cross-Site Scripting (XSS) in Browser | Token exfiltrated and replayed | Non-extractable key cannot be stolen, but injected code can still mint proofs while the client is online or bind fresh tokens to its own key (RFC 9449 Section 11.4) | Not applicable (mTLS rarely usable in browser JS) |
| Token Replay at Different Endpoint (e.g., by a malicious RS) | Vulnerable unless audience-restricted | Protected (`htu`/`htm` binding plus server authentication, RFC 9449 Section 11) | Protected (the receiving server never obtains the client private key) |
| Captured Proof Replayed at Same Endpoint | Not applicable (no proof) | Limited by the acceptance window, `jti` cache, and `DPoP-Nonce` (RFC 9449 Section 11.1) | Not applicable (no separate proof; token alone is useless without the key) |
| Body Tampering by a TLS-Terminating Intermediary | Vulnerable | Vulnerable (DPoP does not sign the body, RFC 9449 Section 11.7) | Vulnerable past the termination point (TLS protects each hop only) |
| Edge Proxy Header Injection | Not applicable | Protected (DPoP verified at origin) | Vulnerable if proxy does not sanitize xfcc headers |

### Cryptographic Agility and Key Storage

RFC 9449 requires that only secure asymmetric signature algorithms be accepted and that `none` never be `(RFC 9449 Section 11.6)`, and it deliberately fixes SHA-256 for `jkt`, `ath`, and `dpop_jkt`, deferring any future hash change to a new specification `(RFC 9449 Section 11.10)`.
Systems should default to modern elliptic curves (`ES256` / NIST P-256) or Edwards-curve algorithms (`EdDSA` / Ed25519) to achieve optimal performance and compact signatures.
RSA algorithms (`RS256`, `PS256`) are best reserved for clients that cannot do elliptic curves: a 2048-bit RSA signature is 256 bytes against 64 bytes for `ES256` and `EdDSA`, and the `jwk` carries a 256-byte modulus; RSA signing is also far slower, while verification is fast (not measured here).
On client runtimes, private keys must be stored in isolated, non-exportable security boundaries.
In browser applications, use the Web Cryptography API (`crypto.subtle.generateKey`) with `extractable: false`, storing the resulting `CryptoKey` reference in IndexedDB.
On mobile platforms, anchor the private key within the iOS Secure Enclave or Android Hardware-backed Keystore.
In backend microservices, anchor private keys in Hardware Security Modules (HSMs) or cloud Key Management Systems (KMS).

## The principal's view

At staff-plus scope, choosing between Bearer tokens, DPoP, and mTLS certificate binding is an architectural trade-off balancing operational complexity, client topology, and threat boundaries.
Classic bearer tokens are acceptable only in low-risk internal environments where network perimeters are tightly controlled and token lifetimes are measured in minutes.
RFC 9700 says authorization and resource servers SHOULD sender-constrain access tokens with mTLS or DPoP `(RFC 9700 Section 2.2.1)`, and for public-facing or regulated financial APIs it is mandatory in practice.
FAPI 1.0 Advanced (Part 2) requires sender-constrained access tokens and supports only mTLS for them, while the FAPI 2.0 Security Profile requires sender-constrained access tokens via either mTLS (RFC 8705) or DPoP (RFC 9449), noting that DPoP "can be easier to deploy in some scenarios" (OpenID FAPI specifications, as of 2026-11).

When designing for mobile apps and browser single-page applications, choose DPoP.
Browsers cannot manage X.509 client certificates cleanly: RFC 9449 cites the "sub-par user experience of TLS client authentication in user agents" as the reason single-page applications need DPoP `(RFC 9449 Section 1)`, and JavaScript has no API to select, rotate, or isolate client certificates per session.
DPoP works at the HTTP layer with the standard WebCrypto API and passes through existing CDNs and API gateways, provided the origin reconstructs the public `htu` correctly.

When designing for backend-to-backend communication, Kubernetes service meshes, and enterprise banking partners, choose mTLS certificate-bound tokens.
Mutual TLS moves proof of possession into the TLS handshake, which existing terminators already perform, and amortizes it across persistent HTTP/2 and HTTP/3 connections.
Once the connection is established, the per-request binding check is a string comparison of the DER thumbprint, while the lab's end-to-end `ES256` DPoP verification costs about 109 us, roughly 9,000 verifications per second per core (Numbers and limits).
The price is operational: certificate issuance and rotation (each rotation invalidates bound tokens, RFC 8705 Section 6.3) and a trusted channel from the TLS terminator to the application (RFC 8705 Section 6.5).

Do not mistake DPoP for full request signing.
DPoP answers exactly one question: "Does the presenter of this access token hold the private key associated with it?"
It does not sign query parameters, request headers, or JSON request bodies.
If your threat model demands non-repudiation of financial payloads, layer HTTP Message Signatures `(RFC 9421)` and Content-Digests `(RFC 9530)` on top of sender-constrained tokens.

## Interview questions

> [!question] L5: Explain why a stolen OAuth 2.0 Bearer token can be replayed by an attacker, and how DPoP prevents this attack at the HTTP layer.
>
> > [!success]- Answer
> > A bearer token conveys authority to any party in possession of the raw token string, meaning anyone who intercepts it can access protected resources without proving identity.
> > DPoP prevents this by requiring the client to sign an ephemeral JWS proof with its private key for every HTTP request.
> > The resource server validates that the public key in the DPoP proof matches the thumbprint embedded in the token confirmation claim (`cnf.jkt`).
> > Even if an attacker steals the access token, they cannot generate a valid DPoP proof without possessing the client private key.
> >
> > Follow-up: What prevents an attacker from stealing both the access token and the DPoP proof JWS and replaying them together?

> [!question] L6: Walk through the 12-step verification checklist of RFC 9449 Section 4.3 and explain how the `ath` claim prevents token substitution.
>
> > [!success]- Answer
> > The checklist verifies that there is not more than one DPoP header, the JWT is well-formed, required claims exist, `typ` is `dpop+jwt`, `alg` is an approved asymmetric algorithm, the signature validates against the embedded `jwk`, and no private key fields exist in `jwk`.
> > It then verifies that `htm` matches the HTTP method, `htu` matches the normalized URI, `nonce` matches the server challenge, `iat` is within the clock skew window, and the public key thumbprint matches `cnf.jkt`.
> > The `ath` claim contains the base64url-encoded SHA-256 digest of the access token string.
> > If an attacker captures a valid DPoP proof from a benign request, they cannot attach that proof to a different access token because the resource server recalculates the hash of the presented token and asserts that it matches `ath`.
> >
> > Follow-up: Why does RFC 9449 Section 4.3 recommend (SHOULD) syntax-based and scheme-based URI normalization before comparing `htu`?

> [!question] L6: Predict the output when a client sends the following request to a protected endpoint:
> ```http
> POST /api/transfer HTTP/1.1
> Host: api.example.com
> Authorization: DPoP token_abc
> DPoP: eyJhbGciOiJFUzI1NiIsInR5cCI6ImRwb3Arand0IiwiandrIjp7Imt0eSI6IkVDIiwiY3J2IjoiUC0yNTYiLCJ4IjoiLi4uIiwieSI6Ii4uLiJ9fQ.eyJqdGkiOiJ1dWlkLTEiLCJodG0iOiJQT1NUIiwiaHR1IjoiaHR0cHM6Ly9hcGkuZXhhbXBsZS5jb20vYXBpL3RyYW5zZmVyIiwiaWF0IjoxNzAwMDAwMDAwLCJhdGgiOiJpbnZhbGlkX2hhc2gifQ.signature
> ```
> Assuming the signature verifies, the clock is synchronized, and `cnf.jkt` matches the JWK thumbprint, what HTTP response status code and headers are returned?
>
> > [!success]- Answer
> > The server returns HTTP `401 Unauthorized`.
> > The response contains the header `WWW-Authenticate: DPoP error="invalid_dpop_proof", error_description="..."`, the error RFC 9449 defines for a proof that fails the Section 4.3 checks `(RFC 9449 Section 7.1)`.
> > Execution fails at RFC 9449 Section 4.3 step 12 because the client-supplied `ath` claim does not equal the SHA-256 base64url digest of the access token `token_abc`.
> >
> > Follow-up: Under what conditions would this endpoint return an HTTP `400 Bad Request` instead of `401 Unauthorized`?

> [!question] L6: Debug this API.
> An engineer reports that legitimate clients are intermittently receiving `401 Unauthorized` with `error="invalid_dpop_proof"` on their API requests after migrating to an Envoy reverse proxy cluster.
> The client logs show that the DPoP signature, method, and access token hash are all correct.
> What is the root cause?
>
> > [!success]- Answer
> > The root cause is target URI (`htu`) mismatch caused by proxy URL rewriting or port stripping.
> > When clients send requests to `https://api.example.com:443/v1/resource`, they may populate `htu` with the full URL including or excluding the port, or Envoy may terminate TLS and forward the request as `http://backend-service/v1/resource`.
> > If the resource server reconstructs the request URL using internal proxy headers (`X-Forwarded-Proto`, `Host`) without applying RFC 3986 syntax and scheme normalization, the string comparison between `htu` and the reconstructed URI fails.
> > To fix this, reconstruct the public-facing URI from trusted proxy headers only, then apply the syntax-based and scheme-based normalization that RFC 9449 Section 4.3 recommends (RFC 3986 Sections 6.2.2 and 6.2.3): lowercase scheme and host, drop default ports 80/443, and ignore query and fragment.
> >
> > Follow-up: How would you verify whether the failure was caused by URI mismatch versus clock skew?

> [!question] L6: How does RFC 8705 mTLS certificate binding work at the TLS handshake level, and how does the authorization server bind the certificate to the token?
>
> > [!success]- Answer
> > During the TLS 1.3 handshake, the server issues a `CertificateRequest` message, prompting the client to present its X.509 certificate and sign the handshake transcript with `CertificateVerify`.
> > When the client requests an access token at the token endpoint, the authorization server extracts the DER-encoded client certificate.
> > The authorization server computes the SHA-256 digest of the DER bytes and encodes it as an unpadded base64url string.
> > It embeds this string into the access token payload as `{"cnf": {"x5t#S256": "<thumbprint>"}}`.
> > When accessing protected resources over mTLS, the resource server extracts the certificate from the active connection and verifies that its thumbprint matches the token `x5t#S256` value.
> >
> > Follow-up: What architectural challenge arises when TLS is terminated at a reverse proxy before reaching the application server?

> [!question] L7: Design a replay protection subsystem for DPoP proofs across a distributed API gateway fleet handling 500,000 requests per second.
> How do you prevent replay without central database bottlenecks?
>
> > [!success]- Answer
> > Combine short clock skew windows, server-provided nonces, and a distributed in-memory cache using consistent hashing.
> > Restrict the acceptable `iat` window to +/- 30 seconds, bounding the required lifetime of cached `jti` entries to at most 60 seconds.
> > Store only the 32-byte SHA-256 digest of `(jti, htu)` in an in-memory cluster (such as Valkey or Redis) with a 60-second TTL to prevent memory exhaustion attacks.
> > Shard `jti` writes across gateway instances using consistent hashing on the access token hash, avoiding distributed locks.
> > Enforce server-provided nonces containing HMAC-signed timestamps (`DPoP-Nonce`) so that the gateway can reject expired proofs statelessly before checking the replay cache.
> >
> > Follow-up: If an attacker floods the gateway with random, validly signed DPoP proofs, how do you prevent cache exhaustion?

> [!question] L7: Why does RFC 9449 Section 7.2 explicitly mandate that a resource server supporting both DPoP and Bearer schemes MUST reject a DPoP-bound token sent as a Bearer token?
>
> > [!success]- Answer
> > If dual-scheme resource servers accepted DPoP-bound tokens via the Bearer scheme, the entire security model of sender-constraining would collapse.
> > An attacker who steals a DPoP-bound access token from memory, logs, or network traffic could simply omit the `DPoP` proof header and send `Authorization: Bearer <token>`.
> > A naive server supporting both schemes would treat the request as a standard bearer request, validate the token signature, and grant access.
> > By mandating that a DPoP-bound token (one carrying `cnf.jkt`) must be rejected when presented under the `Bearer` scheme, RFC 9449 prevents protocol downgrade attacks; the rule cannot be widened to any `cnf`, because RFC 8705 certificate-bound tokens use the `Bearer` scheme by design.
> >
> > Follow-up: How should the resource server formulate the `WWW-Authenticate` header when rejecting a downgraded token?

> [!question] L7: Compare DPoP (RFC 9449) and HTTP Message Signatures (RFC 9421).
> Under what architectural threat models is DPoP insufficient, requiring RFC 9421?
>
> > [!success]- Answer
> > DPoP is strictly a sender-constraining mechanism: it binds an access token to a key pair and asserts that the caller intended to hit a specific HTTP method and target URI.
> > DPoP explicitly does not sign HTTP request headers, query parameters, or request bodies `(RFC 9449 Section 11.7)`.
> > In threat models where untrusted proxies or intermediaries exist between client and resource server, an attacker can modify the request body (e.g., changing wire transfer amounts or recipient IDs) without invalidating the DPoP proof.
> > RFC 9421 provides full HTTP message integrity by signing selective headers, query components, and the `Content-Digest` `(RFC 9530)` of the request body.
> > For high-risk financial transactions requiring non-repudiation and payload integrity, DPoP must be augmented with RFC 9421.
> >
> > Follow-up: Could an organization use RFC 9421 to replace DPoP entirely, and what would be the trade-off?

## Related

Prerequisites: [[Refresh-Token-Rotation-Reuse-Detection-and-Token-Revocation-Architectures]]

Next: [[Service-to-Service-Identity-Client-Credentials-and-Token-Exchange-RFC-8693]]

See also: [[API-Authentication-and-Authorization]]

- Lab: `labs/tests/f14_api_authentication_and_identity/test_sender_constrained_tokens_dpop_rfc_9449_and_mtls_binding_rfc_8705.py`

## Further reading

- [RFC 9449: OAuth 2.0 Demonstrating Proof of Possession (DPoP)](https://www.rfc-editor.org/rfc/rfc9449.html)
  The definitive IETF specification for application-level sender-constrained OAuth 2.0 tokens, proof JWS syntax, and server nonces.
- [RFC 8705: OAuth 2.0 Mutual-TLS Client Authentication and Certificate-Bound Access Tokens](https://www.rfc-editor.org/rfc/rfc8705.html)
  The core standard for binding access tokens to client X.509 certificates and managing mTLS client authentication.
- [RFC 7638: JSON Web Key (JWK) Thumbprint](https://www.rfc-editor.org/rfc/rfc7638.html)
  The standard method for computing deterministic SHA-256 digests over canonical JSON Web Keys used in `cnf.jkt`.
- [RFC 6750: The OAuth 2.0 Authorization Framework: Bearer Token Usage](https://www.rfc-editor.org/rfc/rfc6750.html)
  The foundational specification defining bearer token mechanics and vulnerabilities that sender-constraining solves.
- [RFC 8725: JSON Web Token Best Current Practices](https://www.rfc-editor.org/rfc/rfc8725.html)
  Security guidelines recommending explicit typing (Section 3.11), which DPoP applies as `typ: dpop+jwt`, and algorithm verification (Section 3.1) to prevent substitution attacks.
- [RFC 9700: Best Current Practice for OAuth 2.0 Security](https://www.rfc-editor.org/rfc/rfc9700.html)
  The OAuth security BCP; Section 2.2.1 says servers SHOULD sender-constrain access tokens using RFC 8705 or RFC 9449.
- [RFC 9421: HTTP Message Signatures](https://www.rfc-editor.org/rfc/rfc9421.html)
  The complementary IETF standard for signing complete HTTP request headers and payload bodies.
- [FAPI 2.0 Security Profile](https://openid.net/specs/fapi-security-profile-2_0-final.html)
  The OpenID financial-grade profile that requires sender-constrained access tokens via either RFC 8705 or RFC 9449.
