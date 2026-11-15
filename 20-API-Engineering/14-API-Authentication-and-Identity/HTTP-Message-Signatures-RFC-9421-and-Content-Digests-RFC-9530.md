---
type: pattern
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://www.rfc-editor.org/rfc/rfc9421.txt"
  - "https://www.rfc-editor.org/errata/rfc9421"
  - "https://www.rfc-editor.org/rfc/rfc9530.txt"
  - "https://www.rfc-editor.org/errata/rfc9530"
  - "https://www.rfc-editor.org/rfc/rfc9651.txt"
  - "https://www.rfc-editor.org/rfc/rfc9110.txt"
  - "https://www.rfc-editor.org/rfc/rfc8032.txt"
  - "https://www.rfc-editor.org/rfc/rfc9449.txt"
  - "https://www.rfc-editor.org/rfc/rfc9635.txt"
  - "https://www.iana.org/assignments/http-message-signature/http-message-signature.xml"
  - "https://www.iana.org/assignments/http-digest-hash-alg/http-digest-hash-alg.xml"
  - "https://raw.githubusercontent.com/C2SP/C2SP/main/httpsig-pq.md"
  - "https://www.ietf.org/archive/id/draft-cavage-http-signatures-12.txt"
  - "https://www.ietf.org/archive/id/draft-meunier-web-bot-auth-architecture-05.txt"
  - "https://openid.net/specs/fapi-message-signing-2_0.html"
  - "https://owasp.org/API-Security/editions/2023/en/0x11-t10/"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2017-16005"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2025-59058"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2026-26275"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2026-116825"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2026-58593"
  - "https://cveawg.mitre.org/api/cve/CVE-2026-58593"
  - "https://datatracker.ietf.org/api/v1/doc/document/draft-meunier-web-bot-auth-architecture/"
  - "https://raw.githubusercontent.com/emmett-framework/granian/v2.8.7/src/asgi/utils.rs"
  - "https://pypi.org/project/zttp/0.0.35/"
---

# Cryptographic Request Non-Repudiation: Signing HTTP Requests with HTTP Message Signatures (RFC 9421) and Digest Fields (RFC 9530)

> [!summary] Claim
> RFC 9421 signs a canonical text rebuilt independently by signer and verifier from named message components (never the raw bytes), RFC 9530 turns the body into one of those components via `Content-Digest`, and the pair only proves anything when the verifier enforces an application profile: required coverage, a key-bound algorithm, a bounded `created` window, a single-use `nonce`, and a digest recomputed over the bytes it actually received.

Part of [API Authentication and Identity](README.md) in [API Engineering](../README.md).

## TL;DR

- Mechanism: the signer lists covered components (`"@method"`, `"@authority"`, `"@path"`, `"content-digest"`), each side derives a newline-joined signature base ending in a `"@signature-params"` line, and the detached signature travels in the `Signature-Input` and `Signature` Dictionary fields (RFC 9421 Sections 2.5, 4.1, 4.2).
- Contract rule: RFC 9421 is a toolkit, not a protocol, so every deployment MUST define which components are required, how `keyid` resolves to a key, which algorithms are allowed, and how the algorithm is bound to the key (RFC 9421 Section 1.4); a verifier that skips any of these accepts forgeries.
- The trap: the signature covers the `Content-Digest` field, not the body, so a verifier that checks the signature but does not recompute the digest over the received bytes accepts a swapped body (RFC 9421 Section 7.2.8); the lab proves both the attack and the fix.
- Portability fact: Ed25519 and HMAC-SHA256 are deterministic, so the lab reproduces the RFC 9421 Appendix B.2.5 and B.2.6 signatures byte for byte on CPython 3.14.6 with `cryptography` 50.0.2 (the drafter also reported 3.12.13 and 3.13.18), while ECDSA and RSA-PSS outputs differ on every run and must never be compared by re-signing (RFC 9421 Section 7.3.5).
- When not to use it: if one TLS hop already connects the two parties and you only need sender binding for a token, mTLS or DPoP is simpler (see [[Sender-Constrained-Tokens-DPoP-RFC-9449-and-mTLS-Binding-RFC-8705]]); if you need a self-contained signed document that survives storage and re-transport, sign the payload as a JWS instead of the HTTP message.

## Mental model

```mermaid
sequenceDiagram
    participant C as Client signer
    participant P as TLS-terminating proxy
    participant V as Origin verifier
    C->>C: Content-Digest = sha-256 over body bytes
    C->>C: base = "@method", "@authority", "@path",<br/>"content-digest" lines + "@signature-params"
    C->>C: Signature = Ed25519(private key, base)
    C->>P: POST + Content-Digest + Signature-Input + Signature
    P->>V: forwards; may reorder fields, combine lines,<br/>switch HTTP/1.1 to HTTP/2
    V->>V: parse Signature-Input, pick label by tag,<br/>resolve keyid, bind alg to key
    V->>V: check created window, expires, nonce unseen
    V->>V: rebuild base from the message it received
    V->>V: Ed25519 verify(public key, base, signature)
    V->>V: recompute sha-256 over received body,<br/>compare to Content-Digest
    V-->>C: 200, or 401 + Accept-Signature naming what to sign
```

The client hashes the exact content bytes into `Content-Digest`, then serializes each covered component as a `"name": value` line and appends the `"@signature-params"` line that repeats the covered list and the metadata, so the metadata is itself signed (RFC 9421 Section 3.1 step 4).
The proxy is free to make the transformations HTTP permits, such as reordering differently-named fields, combining same-named lines, or converting between HTTP versions, which moves the authority from HTTP/1.1 `Host` to HTTP/2 `:authority` (RFC 9421 Sections 1.3, 7.5.4), and the signature survives because the verifier canonicalizes again instead of hashing wire bytes.
The verifier runs the policy checks before and around the cryptography: label selection, key lookup, algorithm binding, time window, nonce, then the rebuilt base and `HTTP_VERIFY`, and finally the digest recomputation that connects the signed field to the body.
On failure it can answer with `Accept-Signature`, which tells the client exactly which components and parameters to sign next time (RFC 9421 Section 5.1).

## How it works

### Why canonicalize instead of signing bytes

TLS protects one connection, and an API request often crosses several, for example client to CDN, CDN to gateway, gateway to service, so TLS alone cannot give end-to-end integrity (RFC 9421 Section 1).
Signing the raw HTTP bytes fails for the opposite reason: intermediaries legitimately reorder fields, combine field lines, add `Via` and `Forwarded`, change transfer codings, and convert between HTTP versions (RFC 9421 Section 1.3).
Object signatures such as JWS force the application to duplicate HTTP data inside the payload and then cross-check it (RFC 9421 Section 1).
RFC 9421 instead defines a detached signature over a deterministic text that both sides rebuild from the components they agree are meaningful.
The cost is that anything not covered can be changed freely (RFC 9421 Section 7.2.1), so coverage is a security decision, not a formatting one.

### Component identifiers and values

A component identifier is a Structured Field String (the name) plus Parameters; names starting with `@` are derived components and everything else is a lowercased field name (RFC 9421 Sections 2, 2.1, 2.2).
Each identifier may appear once per signature, and parameter order is preserved on the wire but ignored for equality, so `"foo";bar;baz` and `"foo";baz;bar` cannot both be covered (RFC 9421 Section 2).
Component values MUST NOT contain a newline (RFC 9421 Section 2), and derived values are limited to printable ASCII and space by the `derived-component-value = *( VCHAR / SP )` rule (RFC 9421 Section 2.5).

Field values are canonicalized by stripping leading and trailing whitespace from each field line, replacing HTTP/1.1 obs-fold with a single space, and joining multiple lines with exactly `", "` (RFC 9421 Section 2.1).
So `Cache-Control: max-age=60` plus `Cache-Control:    must-revalidate` becomes `"cache-control": max-age=60, must-revalidate`, and an empty field signs as `"x-empty-header": ` with a trailing space (RFC 9421 Section 2.1); the lab reproduces four of that section's examples (surrounding whitespace, obs-fold, two `Cache-Control` lines, and the empty field).
Six component parameters are registered (RFC 9421 Section 6.5.2); five of them apply to fields:

| Parameter | Effect | Section |
|---|---|---|
| `sf` | re-serialize a known Structured Field strictly, collapsing optional whitespace | 2.1.1 |
| `key` | sign one Dictionary member, serialized strictly, error if absent | 2.1.2 |
| `bs` | wrap each field line as a Byte Sequence before joining, so line boundaries are signed | 2.1.3 |
| `tr` | take the value from the trailer section, never combined with a same-named header | 2.1.4 |
| `req` | in a response signature, take the value from the triggering request | 2.4 |

The sixth, `name`, selects one query parameter for `@query-param` (RFC 9421 Section 2.2.8).
`bs` exists because naive joining is ambiguous: `Example-Header: value, with, lots` plus `Example-Header: of, commas` and the single line `value, with, lots, of, commas` produce the same plain component value, but with `bs` they become `:dmFsdWUsIHdpdGgsIGxvdHM=:, :b2YsIGNvbW1hcw==:` versus `:dmFsdWUsIHdpdGgsIGxvdHMsIG9mLCBjb21tYXM=:` (RFC 9421 Section 2.1.3); the lab asserts both strings and the collision.
`bs` is incompatible with `sf` and `key`, because those need the parsed, combined structure while `bs` needs the raw lines (RFC 9421 Section 2.1).

### Derived components

| Name | Value for `POST /foo?param=Value&Pet=dog` to `example.com` over HTTPS | Section |
|---|---|---|
| `@method` | `POST`, case preserved | 2.2.1 |
| `@target-uri` | `https://example.com/foo?param=Value&Pet=dog` | 2.2.2 |
| `@authority` | `example.com`, host lowercased, default port dropped | 2.2.3 |
| `@scheme` | `https`, lowercased | 2.2.4 |
| `@request-target` | `/foo?param=Value&Pet=dog`, NOT RECOMMENDED beyond HTTP/1.1 | 2.2.5 |
| `@path` | `/foo`, percent-encoding not decoded, empty path becomes `/` | 2.2.6 |
| `@query` | `?param=Value&Pet=dog`, or a lone `?` when absent | 2.2.7 |
| `@query-param` | `dog` for `;name="Pet"`, re-encoded per WHATWG URL form rules | 2.2.8 |
| `@status` | three-digit code, responses only | 2.2.9 |

`@authority` follows RFC 9110 Section 4.2.3 normalization, which lowercases scheme and host and omits a default port (RFC 9421 Section 2.2.3), and it SHOULD be signed instead of `Host` because `Host` does not exist in HTTP/2 and HTTP/3 (RFC 9421 Sections 7.2.4, 7.5.4).
`@path` and `@query` use simple string comparison, so `%7E` and `~` are different values even though RFC 9110 Section 4.2.3 calls them equivalent for URI comparison; a proxy that normalizes percent-encoding breaks the signature.
`@query-param` MUST be omitted when the parameter name occurs more than once, and a covered parameter that is absent is an error (RFC 9421 Section 2.2.8).
Derived names are processed only as derived components, never looked up in the fields, because a lax HTTP parser could pass through a header literally named `@method` and let an attacker substitute it (RFC 9421 Section 7.5.1); the lab sends exactly such a header and shows the base still says `POST`.

### The signature base, byte by byte

The base is ASCII: each covered component serialized as an identifier, `": "`, the value, and `LF`, then the `"@signature-params"` line with no trailing newline (RFC 9421 Section 2.5).
The ABNF is `signature-base = *( signature-base-line LF ) signature-params-line` (RFC 9421 Section 2.5).
Any error (duplicate identifier, unknown parameter, missing field, `req` on a request, non-ASCII output) MUST abort without producing a base (RFC 9421 Section 2.5).
This is the RFC 9421 Appendix B.2.6 base that the lab rebuilds and signs with `test-key-ed25519`:

```text
"date": Tue, 20 Apr 2021 02:07:55 GMT
"@method": POST
"@path": /foo
"@authority": example.com
"content-type": application/json
"content-length": 18
"@signature-params": ("date" "@method" "@path" "@authority" "content-type" "content-length");created=1618884473;keyid="test-key-ed25519"
```

The last line is the serialized `Signature-Input` member value without its label: an Inner List of Strings followed by Parameters, serialized strictly per RFC 9651 (RFC 9421 Sections 2.3, 3.2 step 7).
Because the label is excluded, an intermediary may relabel a signature without breaking it, so labels carry no meaning and applications use the signed `tag` parameter instead (RFC 9421 Section 7.2.5).
Six metadata parameters are registered: `created`, `expires` (UNIX seconds, Integer, no sub-second precision), `nonce`, `alg`, `keyid`, and `tag` (Strings) (RFC 9421 Sections 2.3, 6.3.2).
`created` is RECOMMENDED, not required, and an empty covered list is legal; Appendix B.2.1 signs `()` with only `created`, `keyid`, and `nonce` and warns that such a signature can be moved onto any message (RFC 9421 Appendix B.2.1).

### Wire format and multiple signatures

`Signature-Input` and `Signature` are Dictionary Structured Fields keyed by the same labels; the first carries the Inner List plus parameters and the second a Byte Sequence (RFC 9421 Sections 4.1, 4.2).
A label present in one field and not the other is an error, labels must be unique across all field values, and sending both fields as headers rather than trailers is RECOMMENDED because intermediaries may drop trailers (RFC 9421 Sections 4, 4.1, 4.2).

```http
Signature-Input: sig-b26=("date" "@method" "@path" "@authority" "content-type" "content-length");created=1618884473;keyid="test-key-ed25519"
Signature: sig-b26=:wqcAqbmYJ2ji2glfAMaRy4gruYYnx2nEFN2HN6jrnDnQCK1u02Gb04v9EDgwUPiu4A0w6vuQv5lIp5WPpBKRCw==:
```

A message may carry several signatures from different signers, for example a client signature plus a reverse proxy signature over the rewritten `@authority` and the added `Forwarded` field (RFC 9421 Section 4.3).
Each signature has its own component context, so a verifier that checks both must evaluate `@authority` as the client saw it for one and as the proxy rewrote it for the other (RFC 9421 Section 7.4.4).

### Algorithms

| `alg` | Primitive | Output | Deterministic | Section |
|---|---|---|---|---|
| `rsa-pss-sha512` | RSASSA-PSS, MGF1 SHA-512, 64-byte salt | modulus length (256 bytes for RSA-2048) | no | 3.3.1 |
| `rsa-v1_5-sha256` | RSASSA-PKCS1-v1_5 SHA-256 | modulus length | yes | 3.3.2 |
| `hmac-sha256` | HMAC-SHA256, bytewise compare | 32 bytes | yes | 3.3.3 |
| `ecdsa-p256-sha256` | ECDSA P-256, raw `r` then `s`, 32 bytes each | 64 bytes | no | 3.3.4 |
| `ecdsa-p384-sha384` | ECDSA P-384, raw `r` then `s`, 48 bytes each | 96 bytes | no | 3.3.5 |
| `ed25519` | Ed25519 over the base, no prehash | 64 bytes | yes | 3.3.6 |

ECDSA uses the fixed-width `r || s` encoding (RFC 9421 Section 3.3.4), while `cryptography` 50.0.2 `EllipticCurvePrivateKey.sign` returns an ASN.1 DER `SEQUENCE` (200 P-256 signatures generated in verification were 70, 71, or 72 bytes, each starting with `0x30`), so an implementation must convert with `decode_dss_signature` and zero-pad `r` and `s` to 32 bytes each.
JWS algorithms may be used with the base as the entire JWS Signing Input, without a JOSE header and without base64url-encoding the input, but `none` and Prohibited algorithms are forbidden and JWA names are never put in the `alg` parameter (RFC 9421 Section 3.3.7).
The IANA "HTTP Signature Algorithms" registry, updated 2026-10-23, now also lists `ml-dsa-44`, `ml-dsa-65`, and `ml-dsa-87` as Active, referencing the C2SP `httpsig-pq` v1.0.1 specification, which signs the base with pure ML-DSA (FIPS 204), an empty context, and no prehash (as of 2026-11).
RFC 9421 Section 6.2 says identifiers are whole strings, so `rsa-pss-sha512` is not parsed into parts and registering one combination does not register others.

### Verification algorithm and the application profile

The verifier MUST parse both fields, choose a signature by policy, confirm the parameters meet the profile, dereference `keyid` to a trusted key, determine the algorithm, rebuild the base, and run `HTTP_VERIFY` (RFC 9421 Section 3.2).
Algorithm determination is the critical step: start from the application's allow-list, take the algorithm from static configuration, the key material, or the `alg` parameter, and if more than one source names an algorithm they MUST agree (RFC 9421 Section 3.2 step 6).
Section 3.2.1 lists the application rules that RFC 9421 expects deployments to add and enforce: required fields, maximum age from `created`, honouring `expires`, forbidding `alg` when the key implies it, nonce uniqueness, required `tag`, and minimum key sizes.
GNAP is a complete example of such a profile: it requires `"@method"` and `"@target-uri"`, requires `"content-digest"` when there is content and `"authorization"` when a token is bound, mandates `tag="gnap"` and `created`, recommends `nonce`, and forbids the `alg` parameter (RFC 9635 Section 7.3.1).
Web Bot Auth, an Internet-Draft at revision 05 that expired on 2026-09-03 (as of 2026-11), profiles it for crawlers: `@authority` or `@target-uri`, mandatory `created` and `expires` with expiry RECOMMENDED at most 24 hours, `keyid` as a JWK SHA-256 thumbprint, and `tag="web-bot-auth"` (draft-meunier-web-bot-auth-architecture-05 Section 4.2).

### RFC 9530 Content-Digest and Repr-Digest

RFC 9530 (February 2024) obsoletes RFC 3230 `Digest` and `Want-Digest`, because implementations disagreed on whether "instance" meant the message content or the representation (RFC 9530 Sections 1.3, Appendix E).
It replaces them with two Dictionary fields keyed by algorithm with Byte Sequence values (RFC 9530 Sections 2, 3):

| Field | Input bytes | Changes with |
|---|---|---|
| `Content-Digest` | the message content as sent (RFC 9110 Section 6.4) | range responses, `HEAD`, content coding |
| `Repr-Digest` | the whole selected representation data (RFC 9110 Section 8.1) | content coding and media type, not ranges or transfer coding |

A `206` response for `Range: bytes=10-18` of `{"hello": "world"}` plus LF has `Content-Digest: sha-256=:jjcgBDWNAtbYUXI37CVG3gRuGOAjaaDRGpIUFsdyepQ=:` over `"world"}` plus LF but `Repr-Digest: sha-256=:RK/0qy18MlBSVnWgjwz6lZEWjP/lF5HF9bvEF8FabDg=:` over the whole object (RFC 9530 Appendix B.3).
A `HEAD` response carries the digest of empty content, `47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=`, in `Content-Digest` (RFC 9530 Appendix B.2); the lab asserts this value and the full-object one.
Both fields depend on representation metadata such as `Content-Encoding`, so a gateway that recompresses the body invalidates `Content-Digest` and any signature over it, and must re-sign like the proxy in RFC 9421 Section 4.3 (RFC 9421 Section 7.2.8, RFC 9530 Section 6.3).
`Want-Content-Digest` and `Want-Repr-Digest` carry preferences from `0` (not acceptable) to `10` (most preferred) and are only hints; ignoring them is not a protocol error (RFC 9530 Section 4).

The "Hash Algorithms for HTTP Digest Fields" registry has two Active keys, `sha-256` and `sha-512`, and six Deprecated ones, `md5`, `sha`, `unixsum`, `unixcksum`, `adler`, and `crc32c`; Deprecated algorithms MUST NOT be used in an adversarial setting such as a signed digest (RFC 9530 Sections 5, 7.2; IANA registry checked 2026-11).
Digests give no protection on their own: an on-path attacker simply recomputes them, so they need TLS or a signature to mean anything (RFC 9530 Section 6.1).
Integrity fields also offer no downgrade protection, so a receiver is only as strong as the weakest algorithm it accepts (RFC 9530 Section 6.6).

### The coupling rule that makes the pair work

RFC 9421 does not cover content at all; it relies on the signer covering `content-digest` and on the verifier validating that field against the received content, because otherwise an attacker substitutes the body and leaves the signed field untouched (RFC 9421 Section 7.2.8).
The lab's verifier enforces three rules: if the request has content, `content-digest` must be covered; every Active algorithm present must match; and at least one Active algorithm must be present, so `md5=...` alone fails and a correct `sha-256` cannot launder a wrong `sha-512`.

### Implementation in the pinned Python stack

The lab reads the signature context from ASGI, not from Starlette's convenience URL.
`starlette.datastructures.URL` builds its netloc from the first `host` header as given and its path from the decoded `scope["path"]` (starlette 1.10.0 starlette/datastructures.py, URL.__init__), and `parse_host_header` only validates the host syntax, without lowercasing it or dropping the port (starlette 1.10.0 starlette/_utils.py, parse_host_header).
The lab proves that `GET /a%2Fb` with `Host: API.Example:443` yields `request.url.netloc == "API.Example:443"` and `request.url.path == "/a/b"`, while the correct values are `@authority` `api.example` and `@path` `/a%2Fb`, taken from `scope["raw_path"]`.
httpx's `ASGITransport` fills `raw_path` from `request.url.raw_path` and `path` from the decoded `request.url.path` (httpx 0.28.1 httpx/_transports/asgi.py, ASGITransport.handle_async_request).
Under HTTP/2 and HTTP/3, Hypercorn rewrites the pseudo-headers into a synthetic first `host` header that prefers `:authority` over any `host` line (hypercorn 0.18.0 hypercorn/utils.py, filter_pseudo_headers), which the lab asserts directly.
On the client, an `httpx.Auth` subclass with `requires_request_body = True` makes `sync_auth_flow` and `async_auth_flow` call `request.read()` or `await request.aread()` before `auth_flow` runs (httpx 0.28.1 httpx/_auth.py, Auth.sync_auth_flow and Auth.async_auth_flow), so a streamed upload is buffered and can be digested; the lab streams a two-chunk async body through it.
`Ed25519PrivateKey.sign` and `Ed25519PublicKey.verify` (cryptography 50.0.2 cryptography/hazmat/primitives/asymmetric/ed25519.py) are used directly on the base bytes, matching the "no prehash" rule (RFC 9421 Section 3.3.6).

## Numbers and limits

| Quantity | Value | Source |
|---|---|---|
| Ed25519 signature | 64 bytes, 88 base64 characters in `Signature` | RFC 9421 Section 3.3.6; lab `test_signature_sizes_on_the_wire` |
| HMAC-SHA256 signature | 32 bytes, 44 base64 characters | RFC 9421 Section 3.3.3; lab `test_signature_sizes_on_the_wire` |
| ECDSA P-256 / P-384 signature | 64 / 96 bytes | RFC 9421 Sections 3.3.4, 3.3.5 |
| RSA-PSS signature with the RFC 2048-bit test key | 256 bytes, 344 base64 characters | decoded from RFC 9421 Appendix B.2.1 |
| ML-DSA-44 / 65 / 87 signature | 2420 / 3309 / 4627 bytes, so 3228 / 4412 / 6172 base64 characters | C2SP httpsig-pq v1.0.1 Algorithms table; base64 length computed |
| ML-DSA-44 / 65 / 87 public key | 1312 / 1952 / 2592 bytes | C2SP httpsig-pq v1.0.1 |
| `created` / `expires` | Integer UNIX seconds, at most 15 digits, no sub-second precision | RFC 9421 Section 2.3; RFC 9651 Section 3.3.1 |
| Registry name lengths | SHOULD NOT exceed 20 characters | RFC 9421 Sections 6.2.1, 6.3.1, 6.4.1, 6.5.1 |
| `Want-*-Digest` weights | Integer `0` to `10`, `0` means not acceptable | RFC 9530 Section 4 |
| Web Bot Auth expiry | RECOMMENDED at most 24 hours; nonce RECOMMENDED 64 random bytes | draft-meunier-web-bot-auth-architecture-05 Sections 4.2, 4.2.2 |
| Ed25519 sign, 300-byte base | median 23.5 us | re-measured once in verification (2026-11-15), `uv run --frozen python ../bench.py` from `labs/`, Apple M3 Pro, macOS 27.3 arm64, CPython 3.14.6, cryptography 50.0.2 |
| Ed25519 verify, 300-byte base | median 67.4 us | same run |
| HMAC-SHA256, 300-byte base | median 1.0 us | same run |
| SHA-256 / SHA-512 over 1 MiB | median 344.8 / 603.5 us | same run |
| Lab `verify_request` end to end, 14-byte body | median 116.3 us (parse, base, Ed25519, digest) | same run |

The measured numbers come from one run of a throwaway `timeit` script (deleted after the run) with 7 repeats on one machine (2000 iterations each, 20 for the 1 MiB hashes); treat them as orders of magnitude, not a benchmark.
They show that, per request, asymmetric verification and the body hash dominate, so a 1 MiB upload costs more in `sha-256` than in the signature check itself.
SHA-512 was slower than SHA-256 on this arm64 machine, so the "SHA-512 is faster on 64-bit CPUs" rule of thumb did not hold here.

## Portability

| Dimension | Behavior | Evidence |
|---|---|---|
| CPython 3.12.13 | 30 passed | drafter's lab run, `UV_PROJECT_ENVIRONMENT=../venv-3.12 uv run --frozen --python 3.12`; not re-run in verification (lab checks paused 2026-11-11) |
| CPython 3.13.18 | 30 passed | drafter's lab run, same command with `--python 3.13`; not re-run in verification |
| CPython 3.14.6 | 30 passed (re-run in verification, 2026-11-15); drafter reported ruff, mypy, and pyright clean | lab run in the workspace `.venv` |
| CPython 3.15 (only 3.15.0b3 available locally) | not run: pytest aborts before collection with `ModuleNotFoundError: No module named 'hypothesis._native'`, raised by the hypothesis plugin that schemathesis loads | lab attempt, recorded in the report |
| CPython 3.15t free-threaded | not run: `psycopg-binary==3.3.8` has no wheel for this interpreter, so the environment cannot be built | lab attempt |
| Matrix pins 3.12.16, 3.14.10, 3.15.2 | not available to `uv` on this machine; nearest available versions ran instead | `uv python` directory listing |
| `cryptography` 50.0.2 Ed25519 | deterministic, reproduces RFC 9421 Appendix B.2.6 exactly | lab `test_b26_...`; RFC 8032 Section 8.2 |
| RSA-PSS, ECDSA | non-deterministic, never compare by re-signing | RFC 9421 Section 7.3.5 |
| HTTP/1.1 | authority in `Host`; `@request-target` well defined | RFC 9421 Sections 2.2.3, 2.2.5 |
| HTTP/2, HTTP/3 | authority in `:authority`; signing `host` breaks after translation; `@request-target` NOT RECOMMENDED | RFC 9421 Sections 2.2.5, 7.5.4 |
| Hypercorn 0.18.0 under HTTP/2 or HTTP/3 | `:authority` becomes the first `host` header in the ASGI scope | hypercorn/utils.py `filter_pseudo_headers`; lab test |
| Uvicorn 0.54.0 | HTTP/1.1 by default (`h11` or `httptools`), so `Host` is the authority source; opt-in HTTP/2 (`--http2 --http zttp`) copies the `zttp` parser's lowercased header list into the scope, Uvicorn itself never maps `:authority` to `host`, and `zttp` appends a synthesized lowercase `host` header taken from `:authority` as the last entry (a `host` line that differs from `:authority` is rejected as malformed) | uvicorn 0.54.0 uvicorn/config.py, Config.load; uvicorn/protocols/http/zttp_h2_impl.py, handle_request; zttp 0.0.35 (sdist, latest on PyPI as of 2026-11) src/core/h2/connection.zig, request collapse (the `canonical_host` block and its test "`:authority` became a synthesized host header"); read, not run, because `zttp` is not in this lab's lock |
| Granian 2.8.7 | copies request headers into the scope as-is and, only when no `host` header exists, inserts `host` from the URI authority (`:authority` under HTTP/2) at position 0, so a `host` line wins over `:authority`, the reverse of Hypercorn | granian v2.8.7 src/asgi/utils.rs, build_scope_common |
| Starlette 1.10.0 `request.url` | netloc not normalized, path decoded, so not usable as a signature context | starlette/datastructures.py `URL.__init__`; lab test |
| httpx 0.28.1 client | body available to `Auth` only with `requires_request_body = True` | httpx/_auth.py; lab streaming test |

## Worked example

The [implementation](../labs/src/api_labs/f14_api_authentication_and_identity/http_message_signatures.py) is a strict, minimal RFC 9421 signer and verifier for Ed25519 and HMAC-SHA256, an RFC 9530 `Content-Digest` producer and checker, an RFC 9651 Dictionary parser, an httpx `Auth`, and a Starlette verifier app.
The [test](../labs/tests/f14_api_authentication_and_identity/test_http_message_signatures_rfc_9421_and_content_digests_rfc_9530.py) checks it against the RFC's own vectors and then attacks it.

The signature base builder, verbatim from the lab:

```python
def signature_base(request: HttpRequest, components: Sequence[Component], params: Params) -> bytes:
    """Section 2.5: one line per component, then the @signature-params line, no final LF."""
    seen: set[tuple[str, frozenset[tuple[str, BareItem]]]] = set()
    lines: list[str] = []
    for component in components:
        if component.name == "@signature-params":
            raise SignatureBaseError("@signature-params must not be a covered component")
        key = component.equivalence_key()
        if key in seen:
            raise SignatureBaseError(f"duplicate component {component.serialize()}")
        seen.add(key)
        value = component_value(request, component)
        if "\n" in value:
            raise SignatureBaseError("component value contains a newline")
        lines.append(f"{component.serialize()}: {value}")
    lines.append(f'"@signature-params": {signature_params_value(components, params)}')
    base = "\n".join(lines)
    if not base.isascii():
        raise SignatureBaseError("signature base is not ASCII")
    return base.encode("ascii")
```

The algorithm binding that defeats the RFC 9421 Section 7.3.6 downgrade, verbatim:

```python
def resolve_algorithm(key: VerificationKey, alg_param: str | None) -> str:
    """Section 3.2 step 6: every source of the algorithm must agree, or verification fails.

    The key's own algorithm is authoritative. Letting the `alg` parameter choose instead
    is the RFC 9421 Section 7.3.6 downgrade: HMAC keyed with the public key bytes.
    """
    if alg_param is not None and alg_param != key.alg:
        raise SignatureError("alg_mismatch")
    return key.alg
```

The verifier's order of checks (simplified excerpt of `_verify_one`; the real function also type-checks every parameter):

```python
if any(r.equivalence_key() not in covered for r in policy.required):
    raise SignatureError("insufficient_coverage")
key = keys[keyid]                                  # unknown keyid -> "unknown_key"
alg = resolve_algorithm(key, _str_param(params, "alg"))
# created window, expires, nonce present and unseen
base = signature_base(request, components, sig_input.params)
if not http_verify(alg, key.material, base, signature.value):
    raise SignatureError("bad_signature")
# content present -> "content-digest" must be covered and must match the received bytes
digest_alg = verify_content_digest(_combined(request, "content-digest"), request.content)
policy.seen_nonces.add(nonce)                      # only after every other check passed
```

The client side, verbatim from `HttpSignatureAuth`:

```python
    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Content-Digest"] = content_digest(request.content)
        sig_input, signature = sign_request(
            request_from_httpx(request),
            self.key,
            self.components,
            label=self.label,
            created=self.clock(),
            nonce=self.nonce_factory(),
            tag=self.tag,
        )
        request.headers["Signature-Input"] = sig_input
        request.headers["Signature"] = signature
        yield request
```

What the tests prove:

- `test_b26_...` rebuilds the RFC 9421 Appendix B.2.6 base byte for byte and reproduces the published Ed25519 signature `wqcAqbmY...pBKRCw==`, and `test_b25_...` reproduces the Appendix B.2.5 HMAC signature `pxcQw6G3...GtE8=` exactly.
- `test_b4_...` replays five of the six Appendix B.4 messages against the published `transform` signature (all but the reordering of differently-named fields): adding unsigned fields, adding a query parameter, and collapsing two `Accept` lines keep it valid, while changing the method and authority or reordering the two `Accept` lines break it.
- `test_body_swap_with_untouched_digest_is_rejected` swaps `{"amount": 10}` for `{"amount": 99}` under an intact signature and gets `digest_mismatch`.
- `test_alg_parameter_cannot_downgrade_ed25519_key_to_hmac` forges an `hmac-sha256` signature keyed with the client's public Ed25519 bytes, shows that a verifier which dispatches on `alg` accepts it, and shows the lab returns `alg_mismatch`.
- `test_replayed_nonce_is_rejected_but_failed_attempts_do_not_burn_it` shows an attacker cannot pre-burn a nonce with a tampered copy, then that the second use of a valid one fails.
- `test_injected_signature_from_unknown_key_does_not_deny_valid_one` puts a valid signature from an unknown key in front of the real one and still accepts the real one (RFC 9421 Section 7.2.6).
- `test_e2e_tampered_body_rejected_with_accept_signature_hint` runs the whole path through `httpx.ASGITransport` and Starlette and checks the `401` carries an `Accept-Signature` listing the required components and `tag`.
- `test_erratum_8890_brotli_bytes_and_digest` confirms reported RFC 9530 erratum 8890: the printed Appendix B.4 bytes `8B 08 80 ...` fail to decompress (`brotli: decoder failed`) and hash to `MklYnI...`, while `0B 09 80 ...` decompress to `{"hello": "world"}` plus LF and hash to the published `d435Qo...`.

Captured run (re-run in verification on 2026-11-15, command from `labs/`, CPython 3.14.6, macOS 27.3 arm64, Apple M3 Pro; the test-path prefix is trimmed from each line and the rootdir, plugins, and asyncio header lines are omitted):

```text
$ uv run --frozen pytest tests/f14_api_authentication_and_identity/test_http_message_signatures_rfc_9421_and_content_digests_rfc_9530.py -v -p no:cacheprovider
platform darwin -- Python 3.14.6, pytest-9.2.0, pluggy-1.7.0
collecting ... collected 30 items

test_b26_ed25519_signature_base_and_signature_match_rfc_bytes PASSED [  3%]
test_b25_hmac_sha256_signature_matches_rfc_bytes PASSED [  6%]
test_b4_transformations_keep_or_break_the_signature[original-GET-example.org-name1=Value1&Name2=value2-fields0-True] PASSED [ 10%]
test_b4_transformations_keep_or_break_the_signature[unsigned header and query added-GET-example.org-name1=Value1&Name2=value2&param=added-fields1-True] PASSED [ 13%]
test_b4_transformations_keep_or_break_the_signature[accept lines collapsed-GET-example.org-name1=Value1&Name2=value2-fields2-True] PASSED [ 16%]
test_b4_transformations_keep_or_break_the_signature[method and authority changed-POST-example.com-name1=Value1&Name2=value2-fields3-False] PASSED [ 20%]
test_b4_transformations_keep_or_break_the_signature[accept lines reordered-GET-example.org-name1=Value1&Name2=value2-fields4-False] PASSED [ 23%]
test_section_2_1_field_canonicalization_examples PASSED [ 26%]
test_bs_parameter_separates_two_lines_from_one_combined_line PASSED [ 30%]
test_derived_names_are_never_read_from_header_fields PASSED [ 33%]
test_base_rejects_duplicates_and_self_reference PASSED [ 36%]
test_authority_normalization_lowercases_and_drops_default_port PASSED [ 40%]
test_appendix_d_and_b1_digest_values PASSED [ 43%]
test_digest_verification_rejects_weak_only_and_mixed_mismatch PASSED [ 46%]
test_erratum_8890_brotli_bytes_and_digest PASSED [ 50%]
test_valid_signature_verifies_and_reports_digest PASSED [ 53%]
test_body_swap_with_untouched_digest_is_rejected PASSED [ 56%]
test_alg_parameter_cannot_downgrade_ed25519_key_to_hmac PASSED [ 60%]
test_replayed_nonce_is_rejected_but_failed_attempts_do_not_burn_it PASSED [ 63%]
test_time_window_rejects_stale_future_and_expired PASSED [ 66%]
test_policy_enforces_coverage_nonce_and_tag PASSED [ 70%]
test_injected_signature_from_unknown_key_does_not_deny_valid_one PASSED [ 73%]
test_malformed_or_mismatched_signature_fields_fail_closed PASSED [ 76%]
test_rfc_b26_signature_without_content_digest_fails_strict_policy PASSED [ 80%]
test_signature_sizes_on_the_wire PASSED [ 83%]
test_e2e_signed_request_accepted_then_replay_rejected PASSED [ 86%]
test_e2e_tampered_body_rejected_with_accept_signature_hint PASSED [ 90%]
test_e2e_streamed_body_is_read_before_signing PASSED [ 93%]
test_starlette_url_is_not_a_signature_context PASSED [ 96%]
test_hypercorn_maps_h2_h3_authority_to_host_for_the_verifier PASSED [100%]

============================== 30 passed in 0.40s ==============================
```

Lab simplifications to remove before production: the nonce store is an unbounded in-process `set`, so it must become a shared store with a time-to-live of `max_age + max_skew` and an atomic insert-if-absent; only Ed25519 and HMAC-SHA256 are implemented; and the Structured Field parser covers only the types these fields use (no Decimal, Date, or Display String).

## Pitfalls

> [!warning] Checking the signature but not the body
> Trigger: the verifier validates the signature over `content-digest` and then hands the body to the application without recomputing the hash.
> Symptom: an on-path attacker or a buggy proxy changes the body and every signature still verifies.
> Real case: `httpsig-hyper` before 0.0.23 wrote `if matches!(digest, _expected_digest)`, which Rust treats as a pattern binding that always matches, so digest verification succeeded unconditionally (CVE-2026-26275, CVSS 3.1 7.5).
> Fix: recompute every Active digest over the exact received bytes and compare in constant time (RFC 9421 Section 7.2.8); make a body-swap test part of the suite, as RFC 9421 Section 7.1.1 asks for negative tests.

> [!warning] Signing values without the names
> Trigger: a scheme that signs only header values, or a base format where a value can contain the separator.
> Symptom: swapping two header names changes the meaning while the signature still verifies.
> Real case: Joyent `http-signature` up to 0.9.11 signed only header values, not names, allowing header forgery (CVE-2017-16005, CVSS 3.0 7.5).
> Fix: RFC 9421 puts the quoted component identifier on every line and forbids newlines in values (RFC 9421 Section 2.5); use `bs` for fields whose lines cannot be joined unambiguously (RFC 9421 Section 2.1.3).

> [!warning] Replay inside the validity window
> Trigger: the verifier checks the signature but neither bounds `created` nor tracks `nonce`.
> Symptom: a captured signed `Delete` or `Follow` can be replayed later and re-executed.
> Real case: YesWiki 4.6.2 to before 4.7.0 never validated the `Date` header nor tracked seen signatures in `HttpSignatureService::verifySignature()`, so captured ActivityPub inbox requests could be replayed (CVE-2026-116825).
> Fix: require `created`, reject outside `[now - max_age, now + skew]`, require a `nonce` and store it until it ages out, and record the nonce only after every other check passes (RFC 9421 Sections 3.2.1, 7.2.2).

> [!warning] Non-constant-time HMAC comparison
> Trigger: comparing the computed MAC with `==`.
> Symptom: an attacker learns the correct MAC byte by byte through response timing.
> Real case: `httpsig-rs` before 0.0.19 compared HMAC signatures in non-constant time (CVE-2025-59058, CVSS 3.1 5.9).
> Fix: `hmac.compare_digest` in Python; the lab uses it for both HMAC signatures and digests.

> [!warning] Verified signer is not the claimed author
> Trigger: the verifier authenticates the key, then trusts identity claims in the body.
> Symptom: a legitimately signed request impersonates someone else.
> Real case: NodeBB 4.13.2 verified the HTTP-signature actor on inbound ActivityPub objects but never checked that `attributedTo` matched the sender, so a remote actor could set `attributedTo` to a bare numeric uid such as `1` and post or send private messages as any local user, including the administrator account (CVE-2026-58593).
> Fix: a signature authenticates a key; map `keyid` to a principal and authorize every identity claim in the payload against that principal (OWASP `API1:2023`, see [[Defeating-BOLA-API1-2023-Tenant-Scoped-Queries-and-PostgreSQL-RLS]]).

> [!warning] Building the base from framework conveniences
> Trigger: deriving `@authority` from `request.url.netloc` or `@path` from `request.url.path` in Starlette.
> Symptom: signatures fail for clients that send `Host: API.Example:443` or percent-encoded paths, and teams "fix" it by disabling verification.
> Fix: derive from `scope["raw_path"]`, `scope["query_string"]`, and the `host` header, then apply RFC 9110 Section 4.2.3 normalization yourself; the lab proves Starlette does neither for you.

> [!warning] Signing `Host`, `Date`, `Via`, or `Forwarded`
> Trigger: copying a pre-RFC habit of signing `host` and `date`.
> Symptom: verification breaks after HTTP/1.1 to HTTP/2 translation, or whenever a proxy edits `Via` or `Forwarded`.
> Fix: sign `@authority` and use the `created` parameter for time (RFC 9421 Section 7.2.4); leave `Via` and `Forwarded` unsigned except in tightly coupled deployments (RFC 9421 Section 7.2.3).

> [!warning] Treating the old Cavage draft as RFC 9421
> Trigger: a library that still emits `Signature: keyId="rsa-key-1",algorithm="hs2019",created=1402170695,headers="(request-target) (created) host digest content-length",signature="..."` (draft-cavage-http-signatures-12 Section 4.1.1).
> Symptom: a verifier parses the same `Signature` field name with the wrong grammar.
> Fix: detect RFC 9421 by the presence of `Signature-Input` before interpreting `Signature` (RFC 9421 Appendix A).

> [!warning] Copying RFC example values blindly
> Trigger: using RFC text as test fixtures without recomputing them.
> Symptom: tests encode a wrong value.
> Fix: recompute; verified erratum 8102 and 8103 correct `"@signature-input"` to `"@signature-params"` in RFC 9421 Sections 7.2.8 and 7.5.3, and reported erratum 8890 says the Brotli bytes in RFC 9530 Appendix B.4 and B.6 are invalid, which the lab confirms; RFC 9530 Appendix B.5, B.6, B.11, and C.1 also print `RK/0qy18MlBSVnWgjwz6lZEWjP/lF5HF9bvEF8FabDg==:` with two padding characters where Appendix B.1 has one, and a 32-byte SHA-256 encodes to 44 characters with a single `=`.

## Security and robustness

Threat model: an on-path or in-path party (a compromised CDN, a malicious internal proxy, a TLS-inspection box) can read, replay, reorder, strip, and inject fields and bodies but does not hold the client's private key.
RFC 9421 lists seven conditions for a message to count as covered: a signature is expected, present, verifies, uses an appropriate key and algorithm, is within time bounds, covers the expected content, and its component list fits the message context (RFC 9421 Section 7).

- Insufficient coverage: anything uncovered is attacker-controlled, so require coverage in policy rather than trusting what the signer chose, and strip or ignore unsigned security-relevant fields (RFC 9421 Section 7.2.1).
- Query and form shadowing: frameworks that merge query and `application/x-www-form-urlencoded` body parameters let an unsigned form field override a signed `@query-param`, so draw values from one context and cover the content (RFC 9421 Section 7.5.8).
- Multiple-signature confusion: an attacker can add their own valid signature or invalid ones; accept only expected signers, and do not reject a message just because some other signature is bad, or injected garbage becomes a denial of service (RFC 9421 Section 7.2.6).
- `tag` is public: it selects which signatures to examine but does not authenticate anything (RFC 9421 Section 7.2.7).
- Key and algorithm confusion: the same RSA key works for PSS and PKCS1-v1_5, and an HMAC keyed with a public key is forgeable by anyone, so bind each key to one algorithm and prefer static configuration to the `alg` parameter (RFC 9421 Sections 7.3.4, 7.3.6).
- Symmetric keys: with `hmac-sha256` the verifier can forge the client's signature, so HMAC gives integrity and authentication between two parties but never non-repudiation (RFC 9421 Section 7.3.3).
- Signing signatures: covering another message's `Signature` value does not transitively cover its components; the "Seems Legit" attack lets Mallory craft a key whose signature over her request equals Alice's bytes, so response signatures with `req` should cover the request's components and its `Signature-Input`, not just `Signature` (RFC 9421 Sections 2.4, 7.3.7).
- Parser hardening: a parser that does not enforce closing quotes in `Signature-Input` lets an attacker inject lines into the base, so use a strict RFC 9651 parser (RFC 9421 Section 7.5.3, as corrected by erratum 8103); the lab rejects an unterminated string with `malformed_signature_fields`.
- Padding with duplicate fields: an attacker can add extra lines of a covered field, so validate covered field values with the application parser too, not only through the signature (RFC 9421 Section 7.5.7).
- Oracles: error responses that name the expected `keyid` leak who owns a resource (RFC 9421 Section 8.3); the lab's coarse `reason` code is a debugging aid that a public API might reduce to a single value.
- Resource exhaustion: digest validation costs CPU proportional to the body (the lab measured about 0.34 ms per MiB for SHA-256), so cap body size and the number of digests checked before hashing (RFC 9530 Section 6.7).
- Confidentiality: signatures and digests hide nothing, and a stable `keyid` tracks a client across verifiers, so keep TLS and consider per-verifier keys (RFC 9421 Sections 7.1.2, 8.1, 8.2).
- OWASP API Security Top 10 2023 mapping: `API2:2023` Broken Authentication when policy checks are missing, `API1:2023` Broken Object Level Authorization when the signing principal is not tied to the resource, and `API4:2023` Unrestricted Resource Consumption for unbounded digest work; see [[OWASP-API-Security-Top-10-2023-Complete-Threat-Model]].

### What non-repudiation actually requires

FAPI 2.0 Message Signing (Final, 25 September 2025) defines non-repudiation as assurance that the owner of a key pair able to produce a signature cannot convincingly deny having signed the data, citing NIST SP 800-133, and notes it is usually achieved by storing application-level signatures with the payload and metadata (FAPI 2.0 Message Signing Section 5.2).
It also warns that per-message signatures give no guarantee about a sequence of messages (Section 6.3) and that linking a signed message to a real-world person is out of scope (Section 6.5).
For RFC 9421 that means: asymmetric keys only, private keys the client alone controls, a key registry with validity periods, and an audit log that stores everything needed to rebuild the base later (the covered component values, the exact `Signature-Input` value, the `Signature` bytes, and the body or its digest), because the signature is detached and the canonical text exists only transiently.
FAPI 2.0 Message Signing in its Final form signs pushed authorization requests as JAR request objects (RFC 9101), authorization responses with JARM, introspection responses as JWTs per RFC 9701, and ID tokens, and its table of contents has no section on signing resource requests with RFC 9421 (FAPI 2.0 Message Signing Sections 5.3 to 5.6).

## The principal's view

Choose HTTP Message Signatures when integrity and authenticity must survive TLS termination at an edge you do not fully trust, when you need proof of who sent which body (payments, webhooks, inter-company APIs, federated protocols), or when proxies must add their own attestations over what they forwarded (RFC 9421 Section 4.3, Appendix B.3).
The organizational decision is not the algorithm but the profile: one internal document that fixes the required components, the `tag`, the key distribution and rotation model (JWKS by `keyid` with the same caching discipline as [[JWKS-Discovery-Caching-Key-Rotation-and-Thundering-Herd-Protection]]), the algorithm allow-list, `max_age`, the nonce store, the error contract, and the audit retention, then a shared library per language that implements exactly that profile.
Letting each team pick its own coverage reproduces the CVE list above.

Where to verify is the main architectural trade-off.
Verifying at the gateway centralizes keys and nonce state and keeps services simple, but services then trust the gateway, which is the edge-termination model of [[API-Authentication-Architecture-Edge-Verification-vs-Zero-Trust-Propagation]], and the gateway should add its own signature over what it forwarded.
Verifying in each service gives true end-to-end integrity, but every service needs the original authority (RFC 9421 Section 7.4.3), raw path access, the body before parsing, and a shared nonce store.

Choose something else when it is simpler and sufficient:

- mTLS or DPoP when you need sender-constrained tokens and nothing about the body; DPoP proofs carry `jti`, `htm`, `htu`, `iat`, and, when applicable, `ath` and a server `nonce`, but not the body or general headers, by design (RFC 9449 Sections 4.2, 11.7).
- A JWS-signed payload when the signed object must be stored, forwarded, or verified outside HTTP; RFC 9421 signatures are detached and meaningless without the reconstructed message.
- Standard Webhooks-style HMAC over a timestamp and the raw body when one sender talks to many receivers that each share a secret and need no non-repudiation; see [[Webhook-Sender-Architecture-Standard-Webhooks-Signatures-and-Fan-Out]].
- Plain TLS between two services you control with no intermediaries, where the added CPU, operational keys, and canonicalization bugs buy nothing.

## Interview questions

> [!question] L5: What exactly does an RFC 9421 signature protect, and what does it not?
>
> > [!success]- Answer
> > It protects the listed covered components, as canonicalized into the signature base, plus its own metadata through the final `"@signature-params"` line.
> > It does not protect uncovered fields, the label, or the body.
> > The body is protected only if `content-digest` is covered and the verifier recomputes the digest over the received bytes (RFC 9421 Section 7.2.8).
> >
> > Follow-up: Why is the label excluded from the signature base, and what does that imply for applications?

> [!question] L5: Why does RFC 9421 recommend `@authority` instead of signing the `Host` header?
>
> > [!success]- Answer
> > `Host` is an HTTP/1.1 field; HTTP/2 and HTTP/3 carry the authority in the `:authority` pseudo-header, so a translated message may lose `Host` and the signature fails (RFC 9421 Section 7.5.4).
> > `@authority` is derived from whichever the version uses and normalized per RFC 9110 Section 4.2.3: lowercase host, default port omitted.
> >
> > Follow-up: Your verifier sits behind a reverse proxy that rewrites the authority; what are your two options (RFC 9421 Sections 4.3 and 7.4.3)?

> [!question] L5 predict-the-output: a request signed over `"accept"` has the two lines `Accept: application/json` and `Accept: */*`; which delivery verifies, one proxy's single combined line `Accept: application/json, */*` or another proxy's swapped line order?
>
> > [!success]- Answer
> > The combined line verifies, because the canonical value of two lines is the values joined with `", "`, which is `application/json, */*` either way (RFC 9421 Section 2.1).
> > The swapped order fails, because the value becomes `*/*, application/json` and order of same-named fields is semantically significant (RFC 9421 Appendix B.4).
> > The lab runs both cases against the published Appendix B.4 signature.
> >
> > Follow-up: Which component parameter would make two lines and one combined line produce different values, and why would you want that?

> [!question] L6: Walk through the HMAC downgrade attack against a verifier that trusts the `alg` parameter.
>
> > [!success]- Answer
> > The client's key is an Ed25519 public key, which the attacker knows.
> > The attacker computes HMAC-SHA256 over a forged base using the public key bytes as the secret and sends `alg="hmac-sha256"` with the client's `keyid`.
> > A verifier that looks up key bytes by `keyid` and dispatches on `alg` recomputes the same HMAC and accepts.
> > The fix is to bind each key to exactly one algorithm and fail when `alg` disagrees (RFC 9421 Sections 3.2 step 6 and 7.3.6); the lab demonstrates both the naive acceptance and the `alg_mismatch` rejection.
> >
> > Follow-up: GNAP forbids the `alg` parameter entirely; what does it use instead (RFC 9635 Section 7.3.1)?

> [!question] L6 debug-this-API: a client signing `"host"`, `"@path"`, and `"content-digest"` verifies in staging, but every production request fails with `bad_signature` after the service moved behind a CDN on HTTP/2; where do you look?
>
> > [!success]- Answer
> > First, `host`: under HTTP/2 the authority arrives as `:authority`, and depending on the server stack the `host` line may be absent or synthesized, so sign `@authority` instead (RFC 9421 Section 7.5.4).
> > Second, `@path`: check whether the CDN normalizes percent-encoding, because `@path` uses the raw encoded value and `%7E` is not `~`.
> > Third, `content-digest`: check whether the CDN recompresses or changes `Content-Encoding`, which changes the content bytes.
> > Debug by having signer and verifier log their signature bases for the same request and diff them, which RFC 9421 Section 7.4.1 recommends, rather than relaxing coverage.
> >
> > Follow-up: How do you make the verifier log bases safely without creating an oracle or leaking secrets?

> [!question] L6: `Content-Digest` versus `Repr-Digest`: which should a signed `PUT` carry, and what do they each hash for a `206` response?
>
> > [!success]- Answer
> > `Content-Digest` hashes the message content as sent, so for a `206` it covers only the returned range, and for `HEAD` it is the digest of empty content (RFC 9530 Appendix B.2, B.3).
> > `Repr-Digest` hashes the whole selected representation, so it is identical across ranges and transfer codings but changes with `Content-Encoding` (RFC 9530 Section 3).
> > For signing request bodies, `Content-Digest` is the natural choice because it binds exactly the bytes the server will parse.
> >
> > Follow-up: Why did RFC 9530 obsolete RFC 3230 `Digest` instead of clarifying it?

> [!question] L7+: For a payments API with 400 client companies and a gateway tier, what goes in the signed-request profile, and where is verification done?
>
> > [!success]- Answer
> > The profile fixes required components (`@method`, `@target-uri` or `@authority` plus `@path` and `@query`, `content-digest`, `content-type`, `idempotency-key`), a required `tag`, `created` with `max_age` and skew, a mandatory `nonce`, Ed25519 or ECDSA only, and keys bound to algorithms and registered per client with rotation by `keyid`.
> > The gateway verifies, enforces nonce uniqueness in a shared store with time-to-live, and adds its own signature over what it forwarded so services can trust it without re-verifying the client.
> > For non-repudiation, the system retains the exact `Signature-Input`, `Signature`, covered values, and body digest with the payment record, for as long as disputes are possible.
> > The trade-off is gateway trust versus end-to-end verification in each service; choose the latter only if services can see the original authority, raw path, and body.
> >
> > Follow-up: A client claims it never sent a disputed payment; what evidence do you produce, and what can the client still argue (FAPI 2.0 Message Signing Sections 6.3 and 6.5)?

> [!question] L7+: Why is signing a request's `Signature` field inside a response signature not a proof that the server responded to that request?
>
> > [!success]- Answer
> > A signature value does not transitively cover the components it signed.
> > Mallory can generate a key whose signature over her own request has exactly Alice's signature bytes, get Bob to sign a response covering those bytes, and replay it to Alice, who verifies it successfully (RFC 9421 Section 7.3.7, citing Jackson et al., "Seems Legit", CCS 2019).
> > The response must instead cover the request's components with `req` and the request's `Signature-Input`, which binds nonces and timestamps (RFC 9421 Sections 2.4 and 7.3.7).
> >
> > Follow-up: Which property of a signature scheme does this attack exploit, and which schemes resist it?

> [!question] L7+: What changes when your RFC 9421 deployment adds the post-quantum `ml-dsa-65` algorithm?
>
> > [!success]- Answer
> > The signature grows from 64 bytes for Ed25519 to 3309 bytes, about 4412 base64 characters in `Signature`, and public keys to 1952 bytes (C2SP httpsig-pq v1.0.1).
> > That stresses header size limits at proxies, HPACK and QPACK tables, and log storage.
> > Migration means sending two labelled signatures, classical and post-quantum, during the transition, with verifiers configured to require the one they trust, and keys bound to one algorithm each.
> > The IANA registry already lists `ml-dsa-44`, `ml-dsa-65`, and `ml-dsa-87` as Active (as of 2026-11).
> >
> > Follow-up: How would you stop an attacker from stripping the post-quantum signature and leaving only the classical one?

## Related

Prerequisites: [[Pushed-Authorization-Requests-PAR-RFC-9126-and-Rich-Authorization-Requests-RAR-RFC-9396]]

Next: [[Selective-Disclosure-JWTs-SD-JWT-RFC-9901]]

See also: [[Structured-Field-Values-for-HTTP-RFC-9651]], [[Sender-Constrained-Tokens-DPoP-RFC-9449-and-mTLS-Binding-RFC-8705]], [[JWT-Cryptographic-Pitfalls-and-RFC-8725-Best-Current-Practices]], [[JWKS-Discovery-Caching-Key-Rotation-and-Thundering-Herd-Protection]], [[API-Authentication-Architecture-Edge-Verification-vs-Zero-Trust-Propagation]], [[Service-to-Service-Identity-Client-Credentials-and-Token-Exchange-RFC-8693]], [[Client-Cert-Header-and-TLS-Terminating-Proxies-RFC-9440]], [[Forwarded-Header-RFC-7239-and-X-Forwarded-For-Spoofing]], [[Webhook-Consumer-Engineering-Signature-Verification-Replay-Windows-and-Idempotency]], [[Webhook-Sender-Architecture-Standard-Webhooks-Signatures-and-Fan-Out]], [[Idempotency-Key-Header-Specification-and-State-Machine-Draft-08]], [[OWASP-API-Security-Top-10-2023-Complete-Threat-Model]], [[Defeating-BOLA-API1-2023-Tenant-Scoped-Queries-and-PostgreSQL-RLS]], [[Range-Requests-Partial-Content-and-206-Semantics]], [[HTTP-Compression-Gzip-Brotli-Zstandard-and-BREACH]]

- Lab: `labs/tests/f14_api_authentication_and_identity/test_http_message_signatures_rfc_9421_and_content_digests_rfc_9530.py`

## Further reading

- RFC 9421, HTTP Message Signatures (February 2024): the normative algorithm; Sections 2.5, 3.2, and 7 and Appendix B are the parts to read twice.
- RFC 9421 errata 8102 and 8103 (verified 2024-10-29): correct `"@signature-input"` to `"@signature-params"` in Sections 7.2.8 and 7.5.3.
- RFC 9530, Digest Fields (February 2024): `Content-Digest` versus `Repr-Digest`, the hash registry, and why RFC 3230 was obsoleted.
- RFC 9530 erratum 8890 (reported 2026-04-25): the invalid Brotli example bytes in Appendices B.4 and B.6.
- RFC 9651, Structured Field Values for HTTP: the parser and strict serializer that `Signature-Input`, `Signature`, and `Content-Digest` depend on.
- RFC 9635, GNAP, Section 7.3.1: a complete, normative RFC 9421 profile worth copying.
- C2SP httpsig-pq v1.0.1: the ML-DSA algorithm definitions now in the IANA registry.
- draft-cavage-http-signatures-12: the predecessor whose `Signature` field grammar still circulates.
- draft-meunier-web-bot-auth-architecture-05: an RFC 9421 profile for authenticating automated clients.
- FAPI 2.0 Message Signing (Final, September 2025): the operational definition of non-repudiation and its limits.
- RFC 9449, DPoP, Section 11.7: why proof-of-possession for tokens deliberately leaves the body unsigned.
- Jackson, Cremers, Cohn-Gordon, Sasse, "Seems Legit: Automated Analysis of Subtle Attacks on Protocols that Use Signatures", CCS 2019: the attack behind RFC 9421 Section 7.3.7.
