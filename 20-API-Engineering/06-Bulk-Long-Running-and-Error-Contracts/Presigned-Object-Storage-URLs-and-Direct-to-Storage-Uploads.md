---
type: pattern
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://docs.aws.eu/AmazonS3/latest/developerguide/sigv4-query-string-auth.html"
  - "https://docs.aws.eu/AmazonS3/latest/developerguide/sig-v4-header-based-auth.html"
  - "https://docs.aws.eu/AmazonS3/latest/developerguide/sigv4-HTTPPOSTConstructPolicy.html"
  - "https://docs.aws.eu/AmazonS3/latest/developerguide/sigv4-post-example.html"
  - "https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.md"
  - "https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.md"
  - "https://docs.aws.amazon.com/AmazonS3/latest/userguide/EventNotifications.md"
  - "https://docs.aws.amazon.com/AmazonS3/latest/userguide/notification-how-to-event-types-and-destinations.md"
  - "https://docs.aws.amazon.com/AmazonS3/latest/userguide/notification-content-structure.md"
  - "https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.md"
  - "https://cloud.google.com/storage/docs/access-control/signed-urls"
  - "https://cloud.google.com/storage/docs/xml-api/reference-headers"
  - "https://cloud.google.com/storage/docs/authentication/signatures"
  - "https://raw.githubusercontent.com/boto/botocore/1.44.0/botocore/signers.py"
  - "https://raw.githubusercontent.com/boto/botocore/1.44.0/botocore/auth.py"
  - "https://www.rfc-editor.org/rfc/rfc2104.html"
  - "https://www.rfc-editor.org/rfc/rfc3986.html"
  - "https://www.rfc-editor.org/rfc/rfc7578.html"
  - "https://www.rfc-editor.org/rfc/rfc9110.html"
  - "https://www.rfc-editor.org/rfc/rfc9113.html"
  - "https://nvd.nist.gov/vuln/detail/CVE-2026-48768"
  - "https://nvd.nist.gov/vuln/detail/CVE-2026-50136"
  - "https://nvd.nist.gov/vuln/detail/CVE-2023-28434"
  - "https://osv.dev/vulnerability/GHSA-w5fh-f8xh-5x3p"
  - "https://owasp.org/API-Security/editions/2023/en/0xa1-broken-object-level-authorization/"
  - "https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/"
  - "https://nvd.nist.gov/vuln/detail/CVE-2026-27607"
  - "https://api.osv.dev/v1/vulns/GHSA-w5fh-f8xh-5x3p"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2026-48768"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2026-50136"
  - "https://www.rfc-editor.org/rfc/rfc2104.txt"
  - "https://www.rfc-editor.org/rfc/rfc3986.txt"
  - "https://www.rfc-editor.org/rfc/rfc9110.txt"
  - "https://www.rfc-editor.org/rfc/rfc9113.txt"
  - "https://www.rfc-editor.org/rfc/rfc9114.txt"
  - "https://docs.aws.eu/AmazonS3/latest/developerguide/ErrorResponses.html"
  - "https://docs.aws.amazon.com/AmazonS3/latest/API/API_Error.md"
---

# Offloading Large Payloads from API Servers with Presigned Upload URLs and Cryptographic Upload Policies

> [!summary] Claim
> A presigned upload URL is a short-lived bearer capability whose HMAC signature can bind the method, the object key, the expiry, and every header you choose to sign, so the API server keeps the authorization decision while the bytes go straight to object storage; but the signature covers only what you sign, never the bytes themselves unless you sign a checksum, so the upload is not accepted until a second phase re-reads the stored object and checks it against the record the API created.

Part of [Bulk Long Running and Error Contracts](README.md) in [API Engineering](../README.md).

## TL;DR

- Mechanism: the API authenticates the caller, picks the object key, stores a `pending` upload record, and returns a SigV4 query-string URL (`X-Amz-Algorithm`, `X-Amz-Credential`, `X-Amz-Date`, `X-Amz-Expires`, `X-Amz-SignedHeaders`, `X-Amz-Signature`) or a signed POST policy; storage verifies the HMAC, the client sends the bytes directly, and an `ObjectCreated` event drives finalization.
- Contract rule: sign every constraint you want enforced (`content-length`, `content-type`, `if-none-match: *`, `x-amz-checksum-sha256`), keep `X-Amz-Expires` in minutes, not the 604800-second ceiling, and treat the event only as a trigger, re-reading size, type, and leading bytes from storage before you mark the record `active`.
- Trap: presigned URLs use `UNSIGNED-PAYLOAD`, so the bytes are not covered by the signature; S3 accepts a presigned URL "multiple times, up to the expiration"; an unsigned `Content-Type` lets the client choose `text/html`; and CVE-2026-48768 (TypeBot) combined an unsigned `Content-Type` with a client-chosen key.
- Portability fact: the lab reproduces AWS's published signatures for both the presigned `GET` example and the POST-policy example byte for byte; its 105 tests pass on CPython 3.12 through 3.15 on macOS, Debian Trixie, and Alpine, free-threaded 3.14.6t and 3.15.2t included, and the same store verified real uploads behind Uvicorn, Hypercorn, and Granian over HTTP/1.1, HTTP/2, and HTTP/3, because signing is pure `hmac` and `hashlib` and the signed `host` is the request authority in every HTTP version.
- When not to use it: if the payload must be inspected, transformed, or rate-limited byte by byte inside the API, stream it through the server with hard limits ([[Multipart-Form-Streaming-and-Upload-Memory-Bounds]]); if clients on bad networks must resume a multi-gigabyte transfer, use a resumable protocol ([[Resumable-HTTP-Uploads-IETF-Draft-and-Tus-Protocol]]).

## Mental model

```mermaid
sequenceDiagram
    participant C as Client
    participant A as API server
    participant S as Object storage
    C->>A: POST /v1/uploads {content_type, size_bytes}<br/>Authorization: Bearer ...
    A->>A: authorize, choose key uploads/alice/u0001,<br/>record state=pending
    A-->>C: 201 Created, Location /v1/uploads/u0001<br/>url (SigV4, X-Amz-Expires=900) + headers to send
    C->>S: PUT /uploads/alice/u0001?X-Amz-...<br/>Content-Length, Content-Type, If-None-Match: *
    S->>S: recompute HMAC over method, path, query,<br/>signed headers; check expiry, length, precondition
    S-->>C: 200 OK, ETag
    S--)A: ObjectCreated:Put event (at least once)
    A->>S: HEAD-equivalent: size, type, leading bytes
    A->>A: pending -> active, or rejected + delete
    C->>A: GET /v1/uploads/u0001
    A-->>C: 200 {state: active, etag}
```

The first arrow is the only one that carries the user's credentials, so it is where authorization, quota, allowed types, and the object key are decided.
The API answers with `201 Created` and a `Location` for the upload resource (RFC 9110 Section 15.3.2) plus a URL whose signature binds those decisions, and it never sees the bytes.
The client's `PUT` to storage carries no user identity at all, only the signature, which storage checks by recomputing the same HMAC from the request it received; a changed header, a changed query parameter, or a late request fails before any byte is stored.
Storage then emits an event, which may arrive more than once, out of order, or not before the client polls, so the API treats it as a hint and reads the stored object back before it moves the record from `pending` to `active` or to `rejected`.
The client learns the outcome by polling the upload resource, the same shape as a long-running operation ([[Long-Running-Operations-202-Accepted-and-Operation-Resources-AIP-151]]).

## How it works

### The query-string signature (SigV4)

A presigned URL is SigV4 with the authentication parameters moved into the query string: `X-Amz-Algorithm=AWS4-HMAC-SHA256`, `X-Amz-Credential=<access-key-id>/<date>/<region>/<service>/aws4_request`, `X-Amz-Date` in `yyyyMMddTHHmmssZ`, `X-Amz-Expires` from 1 to 604800 seconds, `X-Amz-SignedHeaders`, and `X-Amz-Signature` (AWS S3, "Authenticating Requests: Using Query Parameters (AWS Signature Version 4)").
The canonical request is six lines: the method, the URI-encoded path with `/` left alone, the canonical query string, the canonical headers each as `Lowercase(name):Trim(value)\n`, the semicolon-joined signed header names, and the payload hash (AWS S3, "Signature Calculations for the Authorization Header"); because each canonical header line ends in `\n` and the six lines are joined with `\n`, an empty line always separates the headers from the signed-header list.
For a presigned URL the payload hash is the constant string `UNSIGNED-PAYLOAD`, "because when you create a presigned URL, you don't know the payload content", and the canonical query string includes every parameter except `X-Amz-Signature` (same page).
botocore implements exactly this in `S3SigV4QueryAuth.payload`, which returns `UNSIGNED_PAYLOAD` under a comment quoting an older wording of that page, "you don't know anything about the payload" (`botocore 1.44.0 botocore/auth.py, S3SigV4QueryAuth.payload`).
Parameters are sorted after encoding, names and values are encoded separately (AWS S3, "Signature Calculations for the Authorization Header"), and `UriEncode()` keeps only `A-Z`, `a-z`, `0-9`, `-`, `.`, `_`, `~`, which is RFC 3986's `unreserved` set (RFC 3986 Section 2.3), and writes everything else as `%XX` with uppercase hex, `%20` for a space and never `+`; RFC 3986 Section 2.1 says producers "should use uppercase hexadecimal digits".
AWS warns that "The standard UriEncode functions provided by your development platform may not work", which is why the lab has its own.
The lab's verifier canonicalizes the decoded ASGI `scope["path"]`, not `request.url.path`, because Starlette's `URL` rewrites `?`, `#`, and `\` in the path to `%3F`, `%23`, and `%5C` (starlette 1.10.0 starlette/datastructures.py, URL.__init__), which would encode those bytes twice and break the signature for any key that contains them; a test signs, uploads, and reads back the key `uploads/alice/r\u00e9sum\u00e9 v2+final?#.png`.

```python
def canonical_request(
    method: str, path: str, query: Mapping[str, str], headers: Mapping[str, str]
) -> str:
    lowered = {name.lower(): value.strip() for name, value in headers.items()}
    names = sorted(lowered)
    canonical_headers = "".join(f"{name}:{lowered[name]}\n" for name in names)
    return "\n".join(
        [
            method,
            uri_encode(path, encode_slash=False),
            canonical_query(query),
            canonical_headers,
            ";".join(names),
            UNSIGNED_PAYLOAD,
        ]
    )


def signing_key(secret: str, date: str, region: str) -> bytes:
    key = hmac.new(b"AWS4" + secret.encode("utf-8"), date.encode("ascii"), hashlib.sha256).digest()
    for part in (region, SERVICE, "aws4_request"):
        key = hmac.new(key, part.encode("ascii"), hashlib.sha256).digest()
    return key
```

The string to sign is `AWS4-HMAC-SHA256\n<X-Amz-Date>\n<date>/<region>/s3/aws4_request\n<hex SHA-256 of the canonical request>`, and the signature is the hex HMAC-SHA256 of it under a key derived by four chained HMACs from `"AWS4" + secret` over the date, region, service, and `aws4_request` (AWS S3 query-auth page, "An Example").
The derived key is scoped to one day, region, and service, which is why the URL cannot outlive seven days: "the signing key you use in signature calculation is valid for up to seven days" (same page).
HMAC itself is RFC 2104; keys shorter than the hash output are "strongly discouraged" (RFC 2104 Section 3), which the 40-character secret easily clears.

### What the signature does and does not bind

Storage recomputes the signature from the request it receives, so anything in the canonical request is enforced for free: the method, the path (the object key), every query parameter including `X-Amz-Expires`, and the value of every header named in `X-Amz-SignedHeaders`.
AWS requires only `host` and any `x-amz-*` headers, and adds "For added security, you should sign all the request headers that you plan to include in your request" (AWS S3 query-auth page, table of parameters).
Anything not signed is the client's choice.
The lab's API signs four headers on every `PUT` credential: `content-length` (exact size), `content-type` (exact type), `if-none-match: *` (no overwrite), and `host`.
`Content-Length` is a decimal count of octets (RFC 9110 Section 8.6), and the store also checks that the body it received has that length, which in HTTP/1.1 is a framing property and in HTTP/2 is a protocol rule: a `content-length` that differs from the sum of `DATA` frame payloads makes the request malformed (RFC 9113 Section 8.1.1).
`If-None-Match: *` makes the `PUT` conditional on the target having no current representation (RFC 9110 Section 13.1.2); S3 supports it on `PutObject` and answers `412 Precondition Failed` when the key exists, or `409 Conflict` if a concurrent delete wins the race (AWS S3, "How to prevent object overwrites with conditional writes").
Signing the header matters because the URL is otherwise replayable: S3 says "You can use the presigned URL multiple times, up to the expiration date and time" and "Amazon S3 replaces the existing object with the uploaded object" (AWS S3, "Download and upload objects with presigned URLs").
The bytes are never signed under `UNSIGNED-PAYLOAD`; to bind them, the API signs `x-amz-checksum-sha256`, "the Base64 encoded, 256-bit `SHA256` digest of the object" (AWS S3 API Reference, `PutObject`), which requires the client to hash the file before asking for a URL.
The lab's store recomputes that digest and refuses a mismatch with `400`.
It also refuses a URL presented before its own `X-Amz-Date`, with the lab reason `not_yet_valid`, and one presented after `X-Amz-Date` plus `X-Amz-Expires`, with `expired`.
The store's `error` strings reuse four S3 error codes with the statuses S3 documents for them: `AccessDenied` (`403`), `IncompleteBody` (`400`), `BadDigest` (`400`), and `PreconditionFailed` (`412`) (AWS S3, "Error responses", as of 2026-11).
S3 also lists `SignatureDoesNotMatch` (`403`), `EntityTooSmall` and `EntityTooLarge` (`400`), and `InvalidPolicyDocument` (`400`) on that page, which the lab does not model: it answers every signature and policy failure with `403` and its own `reason` field, and `MissingFile` and `MissingKey` are lab-only names.

### POST policies: a signed document of conditions

A browser form upload uses `POST` with `multipart/form-data` (RFC 7578) and a policy: a base64-encoded JSON document with an `expiration` and a `conditions` array; the base64 string is the string to sign, and the signature is the hex HMAC-SHA256 of it under the same derived key (AWS S3, "POST Policy" and "Example: Browser-Based Upload using HTTP POST").
Conditions are exact matches (`{"acl": "public-read"}` or `["eq", "$acl", "public-read"]`), prefixes (`["starts-with", "$key", "user/user1/"]`), and ranges (`["content-length-range", 1048576, 10485760]` allows 1 to 10 MiB), and every form field except `x-amz-signature`, `file`, `policy`, and fields prefixed `x-ignore-` "must appear in the list of conditions" (AWS S3, "POST Policy").
A `starts-with` on `$Content-Type` treats a comma-separated value as a list and requires every element to match, so `image/jpg,image/png,image/gif` passes `image/` and `image/jpg,text/plain` does not (same page); `check_policy` in the lab implements that rule and a test pins both examples.
The `expiration` means the policy "is not valid after" that instant (same page), so the lab accepts a form at exactly the expiration second and rejects it one second later, matching its inclusive `X-Amz-Expires` window, and it formats both `x-amz-date` and `expiration` from a UTC conversion of the clock so a non-UTC server clock cannot shift the window.
The store checks the `x-amz-credential` scope (access key, region, `s3`, `aws4_request`) before recomputing the policy signature, as the `PUT` path does.
It parses the form with `Request.form(max_files=1, max_fields=32)`, so a second file part or a thirty-third field is a `400` before any policy work, where Starlette's defaults are 1000 of each (starlette 1.10.0 starlette/requests.py, Request.form), and a signed form with no `key` field is a `400` instead of an object stored under an empty name.
The policy's advantage over a presigned `PUT` is the range: the client need not know the exact size in advance.
botocore's `generate_presigned_post` builds this policy with `expiration = now + expires_in` and `expires_in=3600` by default (`ExpiresIn=3600` on the client method), appends `x-amz-algorithm`, `x-amz-credential`, and `x-amz-date` conditions, and signs `base64(json.dumps(policy))` (`botocore 1.44.0 botocore/signers.py, S3PostPresigner.generate_presigned_post`; `botocore/signers.py, generate_presigned_post`; `botocore/auth.py, S3SigV4PostAuth.add_auth`).

### Finalization: the second phase

S3 event notifications "are designed to be delivered at least once", typically within seconds but "can sometimes take a minute or longer" (AWS S3, "Amazon S3 Event Notifications"), "aren't guaranteed to arrive in the same order", and may be duplicated (AWS S3, "Event notification types and destinations").
The message carries `Records[].s3.object.key`, `size`, `eTag`, and a `sequencer`; the key "is URL encoded", so `red flower.jpg` arrives as `red+flower.jpg` (AWS S3, "Event message structure").
The lab's `event_keys` therefore decodes with `unquote_plus`, and a test pins `a b+c.png`, which `unquote` would decode as `a+b+c.png` (swapping in `unquote` fails that test).
The event body says nothing about the stored `Content-Type` or the first bytes, and a forged or stale event can name any key, so the finalizer reads the object back from storage and decides from that:

```python
    def _finalize(self, key: str) -> None:
        upload_id = self.by_key.get(key)
        record = self.records.get(upload_id) if upload_id is not None else None
        stored = self.store.head(key)
        if record is None or stored is None:
            self.ignored_events += 1
            return
        if record.state in {UploadState.EXPIRED, UploadState.REJECTED}:
            self.store.delete(key)  # late or replayed object for a closed record: an orphan
            return
        if record.state is UploadState.ACTIVE and stored.etag == record.etag:
            return  # duplicate delivery: events are at-least-once
        reason = None
        if record.state is UploadState.ACTIVE:
            reason = "overwritten"  # a replayed credential replaced an accepted object
        elif not 1 <= len(stored.body) <= record.size_bytes:
            reason = "size"
        elif stored.content_type != record.content_type:
            reason = "content_type"
        elif not stored.body.startswith(_MAGIC[record.content_type]):
            reason = "magic_bytes"
        record.finalized_at = self.clock()
        if reason is None:
            record.state, record.etag = UploadState.ACTIVE, stored.etag
        else:
            record.state, record.reason, record.etag = UploadState.REJECTED, reason, None
            self.store.delete(key)
```

The state machine is `pending -> active | rejected | expired`; `active` is final unless a replayed credential changes the object, which flips it to `rejected` and deletes the object.
A sweeper expires `pending` records once the URL lifetime plus a grace period has passed, because S3 "checks the expiration date and time of a signed URL at the time of the HTTP request", so a transfer that starts one second before expiry can still complete afterwards (AWS S3, "Expiration time for presigned URLs"); an object that lands after the sweep is deleted when its event arrives.
The event channel itself must be authenticated; in S3 that is the SNS, SQS, or Lambda destination policy, and in the lab it is an HMAC over the body in the lab-defined header `x-lab-event-signature`, verified with `hmac.compare_digest` ([[Webhook-Consumer-Engineering-Signature-Verification-Replay-Windows-and-Idempotency]]).

## Numbers and limits

| Quantity | Value | Source or measurement |
|---|---|---|
| `X-Amz-Expires` range | 1 to 604800 s (seven days) | AWS S3 query-auth page; lab `MAX_EXPIRES_SECONDS`, both bounds tested |
| GCS V4 signed URL maximum | 604800 s | GCS "Signed URLs" (as of 2026-11) |
| S3 console presign range | 1 minute to 12 hours; CLI and SDKs up to 7 days | AWS S3 "Download and upload objects with presigned URLs" (as of 2026-11) |
| URL made with temporary credentials | expires when the credentials do; EC2 role credentials "typically 6 hours" | same page |
| botocore default lifetime | `ExpiresIn=3600` for `generate_presigned_url` and `generate_presigned_post` | `botocore 1.44.0 botocore/signers.py` |
| POST policy range example | `["content-length-range", 1048576, 10485760]`, 1 to 10 MiB | AWS S3 "POST Policy" |
| GCS `x-goog-content-length-range` | inclusive `MIN,MAX` bytes on `PUT`, `400 Bad Request` outside, "silently ignored" on other methods | GCS "HTTP headers and query string parameters for XML API" |
| Conditional `PUT` outcomes | `412` when the key exists, `409` on a racing delete | AWS S3 "conditional writes" |
| Event delivery | at least once, typically seconds, "sometimes ... a minute or longer" | AWS S3 "Amazon S3 Event Notifications" |
| Signature size | 32-byte HMAC-SHA256, 64 lowercase hex characters | AWS example signature `aeeed9bb...d404`; lab test |
| Lab PUT URL length | 347 characters (four signed headers, 19-character key `uploads/alice/u0001`, 900 s) | `len(presign_url(...))` for `PUT /uploads/alice/u0001` with the `_make_rig` store settings and the three signed headers, run with `uv run --frozen python` from `labs/`, CPython 3.14.6, macOS 27.2 arm64 (Darwin 27.2.0), Apple M3 Pro (as of 2026-11) |
| Lab `presign_url` cost | 51 to 78 us median per call across eight runs of `timeit.repeat(number=20000, repeat=7)`, lowest repeat 44.25 us, measured while the machine's load average was between 25 and 64; the earlier fact-check measured 26.0 to 33.5 us on the same machine when it was quieter, so read it as tens of microseconds | same script and machine |
| Lab store form limits | 1 file and 32 fields per `POST`; Starlette's defaults are 1000 files, 1000 fields, and 1 MiB per non-file part | lab `_post_object`, tested at 32 and 33 fields and at two files; (starlette 1.10.0 starlette/requests.py, Request.form) |
| S3 error codes the lab reuses | `AccessDenied` `403`, `IncompleteBody` `400`, `BadDigest` `400`, `PreconditionFailed` `412` | AWS S3 "Error responses" (as of 2026-11) |
| Lab policy | URL TTL 900 s, grace 600 s, `max_bytes` 1024 | `_make_rig` in the test module |

Minting a URL costs tens of microseconds on one core and no I/O, so the API's capacity for uploads becomes a function of how many records it can write, not how many bytes it can move.

## Portability

| Leg | Result | Evidence |
|---|---|---|
| CPython 3.12.13, 3.13.18, 3.14.10, and 3.15.2 on macOS 27.2 arm64 | 105 passed each | full `uv.lock` environment, `UV_PROJECT_ENVIRONMENT=<scratch venv> uv run --frozen --python <version> pytest <module> -q` (as of 2026-11) |
| CPython 3.14.6 (workspace) on macOS 27.2 arm64 | 105 passed | workspace `.venv`, `uv run --frozen pytest <module> -q` |
| CPython 3.12.16, 3.13.18, 3.14.10, and 3.15.2 on Debian Trixie (glibc 2.41, aarch64) | 105 passed each | `docker run --rm python:<version>-trixie` with `labs/` mounted read-only, the five direct dependencies (starlette, httpx, python-multipart, pytest, pytest-asyncio) installed by `pip` under `uv export` constraints, and the project pytest config |
| CPython 3.12.16, 3.13.18, 3.14.10, and 3.15.2 on Alpine 3.24 (musl, aarch64) | 105 passed each | `docker run --rm python:<version>-alpine`, same method |
| CPython 3.15.2t free-threaded on Debian Trixie | 105 passed; `sys._is_gil_enabled()` is `False` after importing the lab, Starlette, and httpx | CPython 3.15.2 built from the python.org source tarball with `./configure --disable-gil` (GCC 14.2.0) inside `python:3.15.2-trixie`, same five pinned dependencies; neither `uv python list` nor the cached images offer a 3.15.2t build |
| CPython 3.14.6t and 3.15.0b3t free-threaded on macOS | 105 passed each; `sys._is_gil_enabled()` is `False` | minimal venv with the same five pinned dependencies and the project pytest config |
| CPython 3.15.0b3 (`uv`'s pick for `--python 3.15`) on macOS | full lock: pytest plugin load fails with `No module named 'hypothesis._native'`; minimal venv: 105 passed, GIL enabled | both run here; the minimal venv shows the failure belongs to the lock's `hypothesis` install on a beta interpreter, not to the lab |
| Starlette 1.10.0, httpx 0.28.1, python-multipart 0.0.33 | the store's `POST` handler uses `Request.form(max_files=1, max_fields=32)` and `UploadFile.read` | (starlette 1.10.0 starlette/requests.py, Request.form); (starlette 1.10.0 starlette/datastructures.py, UploadFile) |
| AWS S3 SigV4 | both published vectors reproduced byte for byte | `test_presign_reproduces_the_aws_documented_presigned_get_url`, `test_post_policy_signature_matches_the_aws_example` |
| GCS | V4 query signing has the same shape with `X-Goog-*` names, `GOOG4-RSA-SHA256` with an RSA (service account) key or `GOOG4-HMAC-SHA256` with an HMAC key, a 604800 s maximum `X-Goog-Expires`, and `x-goog-content-length-range` as a size bound on `PUT` | GCS "Signed URLs" and headers reference; not run |
| Uvicorn 0.54.0, Hypercorn 0.18.0, Granian 2.8.7 | the two-phase flow passes over loopback sockets on each: signed `PUT` `200`, replay `412`, edited `Content-Type` `403`, POST policy `204`, both records `active` after their events | scratch driver run from `labs/` on CPython 3.14.6, macOS 27.2 arm64: one ASGI app routing to the lab's `UploadService` and `ObjectStore`, each server started from its CLI, httpx 0.28.1 as the client |
| HTTP/1.1 | passes on all three servers; a `PUT` whose `Host` is the listener address instead of the signed `media.storage.test` is `403` with `signature_mismatch` | same run |
| HTTP/2 | passes on Hypercorn and Granian over cleartext prior-knowledge HTTP/2 (httpx with `http1=False, http2=True`, h2 4.4.1), where the signed `host` arrives as `:authority` and clients "MUST NOT generate a request with a Host header field that differs" from it; Uvicorn's default `--http auto` reset the connection, and its HTTP/2 path (`--http zttp --http2`) was not run because `zttp` is not in the lab lock | same run; RFC 9113 Section 8.3.1; RFC 9113 Section 8.1.1 for `content-length`; (uvicorn 0.54.0 uvicorn/main.py, `--http2` option) |
| HTTP/3 | passes on Hypercorn 0.18.0 over QUIC (`--quic-bind`, a throwaway self-signed certificate, an aioquic 1.7.2 client, ALPN `h3`) with the same statuses and the signed `host` carried as `:authority` | same machine; RFC 9114 Section 4.3.1 (`:authority` and `Host` "MUST contain the same value" when both are present); RFC 9114 Section 4.1.2 (a `Content-Length` that differs from the sum of the `DATA` frame lengths is malformed) |

## Worked example

The [implementation](../labs/src/api_labs/f06_bulk_long_running_and_error_contracts/presigned_upload.py) holds the SigV4 primitives, a POST-policy evaluator, an in-process `ObjectStore` Starlette app that verifies presigned `PUT` and policy `POST` requests and emits S3-shaped events, and an `UploadService` Starlette app with `POST /v1/uploads`, `GET /v1/uploads/{upload_id}`, `POST /internal/storage-events`, and a `sweep()` method.
The [test](../labs/tests/f06_bulk_long_running_and_error_contracts/test_presigned_object_storage_urls_and_direct_to_storage_uploads.py) has 105 cases, all `@pytest.mark.unit`, with both apps reached through `httpx.ASGITransport` and a fake clock.

The AWS vector, reproduced (verbatim; `AWS_GET_URL` is the URL from the AWS page, signature `aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404`):

```python
@pytest.mark.unit
def test_presign_reproduces_the_aws_documented_presigned_get_url() -> None:
    url = presign_url(
        credentials=AWS_EXAMPLE,
        method="GET",
        host="examplebucket.s3.amazonaws.com",
        path="/test.txt",
        region="us-east-1",
        now=datetime(2013, 5, 24, tzinfo=UTC),
        expires=86400,
    )
    assert url == AWS_GET_URL
```

Replay of a `PUT` credential is stopped by the signed precondition, and only one event is emitted (verbatim):

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_signed_if_none_match_turns_replay_into_412(rig: Rig) -> None:
    upload = (await rig.create()).json()["upload"]
    assert (await _put(rig, upload, PNG)).status_code == 200
    replay = await _put(rig, upload, PNG)
    assert replay.status_code == 412
    assert len(rig.store.outbox) == 1
```

A signed `Content-Type` is only a label the client promised; HTML sent under `image/png` is stored, then rejected and deleted at finalization (verbatim):

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_signed_content_type_is_a_label_so_finalization_sniffs_the_bytes(rig: Rig) -> None:
    upload = (await rig.create()).json()["upload"]
    html = b"<script>alert(1)</script>".ljust(len(PNG), b" ")
    assert (await _put(rig, upload, html)).status_code == 200
    await rig.drain()
    state = await rig.state("u0001")
    assert (state["state"], state["reason"]) == ("rejected", "magic_bytes")
    assert rig.store.objects == {}
```

Defense in depth against a store that does not enforce the policy, the shape of GHSA-w5fh-f8xh-5x3p: a 964-byte body and an empty body under a 1 to 64 byte range are both accepted by storage and rejected by the API (verbatim; `lax_rig` is the fixture whose store is built with `enforce_post_policy=False`):

```python
@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("body", [PNG + b"0" * 900, b""], ids=["964-bytes", "empty"])
async def test_finalization_catches_a_store_that_skips_policy_checks(
    lax_rig: Rig, body: bytes
) -> None:
    fields = (await lax_rig.create(method="POST", size_bytes=64)).json()["upload"]["fields"]
    assert (await _post_form(lax_rig, fields, body)).status_code == 204
    assert len(lax_rig.store.objects["uploads/alice/u0001"].body) == len(body)
    await lax_rig.drain()
    state = await lax_rig.state("u0001")
    assert (state["state"], state["reason"]) == ("rejected", "size")
    assert lax_rig.store.objects == {}
```

Captured on CPython 3.14.6, macOS 27.2 arm64, from `labs/`:

```text
$ uv run --frozen pytest tests/f06_bulk_long_running_and_error_contracts/test_presigned_object_storage_urls_and_direct_to_storage_uploads.py -q
........................................................................ [ 68%]
.................................                                        [100%]
105 passed in 0.52s
```

Other tests pin the canonical-request hash `3bfa2928...ad04` from the AWS page, the POST-policy signature `8afdbf40...699e`, every edited query parameter (each with its reason: `signature_mismatch`, `bad_expires`, `bad_scope`, `bad_credential`, `malformed_query`), expiry at exactly 900 s and 901 s for both `PUT` URLs and POST policies, a URL refused one second before its `X-Amz-Date`, idempotent re-delivery with an unchanged `finalized_at`, a replayed POST form and a replayed `PUT` URL without a signed `If-None-Match` that each overwrite an accepted object, a non-UTC clock on both the `PUT` and the POST credential, a key with a space, `+`, `?`, `#`, and non-ASCII characters, the 32-field and one-file form limits, and the sweeper's grace boundary and its refusal to touch `active` or `rejected` records.
A group of hostile-input tests sends a missing `X-Amz-Algorithm`, a non-ASCII signature, checksum, and event-signature header, a non-numeric unsigned `Content-Length`, a malformed event body, a validly signed but malformed policy, and a validly signed form with no `key`, and requires a 4xx for each.
The module has 100% line and branch coverage (`pytest --cov --cov-branch`: 367 statements, 108 branches, none missed), and coverage was not enough: of the first 28 deliberate mutations of `presigned_upload.py` tried against the 78 tests the draft had, 18 survived, among them a URL accepted before its `X-Amz-Date`, a local-time `X-Amz-Date` on `PUT`, any `Authorization` scheme accepted as a bearer token, a sweeper that expired `active` records, an empty object finalized as `active`, and a form stored under an empty key.
Each survivor led to a new or tightened test, three led to lab fixes (`not_yet_valid`, `MissingKey`, and canonicalizing `scope["path"]`), and the 105 tests now kill 60 of 61 mutations.
The one survivor replaces `_MAGIC[record.content_type]` with `_MAGIC.get(record.content_type, b"")`, which cannot change behavior while the constructor refuses types that have no signature; removing that constructor check is one of the 60 killed.

## Pitfalls

> [!warning] Unsigned `Content-Type` and a client-chosen key
> Trigger: TypeBot 3.16.1 and earlier exposed an unauthenticated `POST /api/blocks/file-input/v3/generate-upload-url` that built `public/` S3 keys from an unsanitized `fileName` and issued "presigned PUT URLs that do not bind Content-Type" (CVE-2026-48768, NVD, published 2026-06-18, CVSS 3.1 9.3 from the CNA, CWE-22 and CWE-79).
> Symptom: "any anonymous visitor ... can upload attacker-controlled HTML, SVG, or JS to attacker-chosen subpaths, including other tenants' publicly served result paths"; NVD notes `../` was blocked by signature mismatch but "forward-slash path injection is exploitable".
> Fix: choose the key on the server from the authenticated principal and a fresh id, keep the user's filename as metadata only, sign `content-type`, and sniff the leading bytes at finalization; `test_unsigned_content_type_lets_the_client_choose_text_html` shows the store accepting `text/html` when the header is unsigned.

> [!warning] Presigning endpoint without authorization
> Trigger: Budibase before 3.39.3 generated S3 `PutObject` presigned URLs from a workspace datasource's stored credentials on an endpoint "protected only by the recaptcha middleware" (CVE-2026-50136, NVD, published 2026-06-26, CWE-306).
> Symptom: "A public caller who knows a workspace ID and S3 datasource ID can request a signed upload URL for attacker-controlled bucket and key values."
> Fix: the presigning call is the authorization decision, so it needs the same authentication and object-level checks as any write ([[Defeating-BOLA-API1-2023-Tenant-Scoped-Queries-and-PostgreSQL-RLS]]); the lab returns `401` without a known bearer token and never takes a bucket or key from the client.

> [!warning] Trusting the store to enforce the policy
> Trigger: RustFS 1.0.0-alpha.56 through 1.0.0-alpha.82 accepted presigned `POST` uploads without evaluating their conditions; 1.0.0-alpha.83 fixes it (GHSA-w5fh-f8xh-5x3p, CVE-2026-27607, CWE-863).
> Symptom: per the advisory, `content-length-range`, `starts-with`, and `Content-Type` were "not enforced", allowing "uploads exceeding size limits, uploads to arbitrary object keys, and content-type spoofing".
> Fix: the API re-checks size, type, and leading bytes from storage before `active`, so an S3-compatible backend's gap becomes a `rejected` record instead of a stored payload; the lab's `enforce_post_policy=False` store reproduces the gap.

> [!warning] Replay within the expiry window
> Trigger: a presigned `PUT` without a signed `If-None-Match: *`, or any `POST` policy, used a second time before it expires.
> Symptom: S3 accepts the URL "multiple times, up to the expiration date and time" and the second upload replaces the first, after your finalizer has already accepted it.
> Fix: sign `if-none-match: *` so a replay is `412`, keep lifetimes short, and treat an `ObjectCreated` event with a new `ETag` for an `active` record as an overwrite (`reason: "overwritten"` in the lab) rather than a duplicate; `test_url_without_a_signed_if_none_match_replays_and_overwrites` shows both `PUT` requests returning `200` and the record ending `rejected`.

> [!warning] Seven-day URLs from long-lived keys
> Trigger: `X-Amz-Expires=604800` because it is the documented maximum, signed with an IAM user's access key.
> Symptom: a URL leaked in a log, a `Referer`, or a support ticket is a bearer credential for a week; AWS calls presigned URLs "bearer tokens that grant access to those who possess them".
> Fix: minutes, not days (the lab uses 900 s); sign with role credentials so the URL dies with the session; enforce a ceiling in bucket policy with the `s3:signatureAge` condition, which AWS shows denying signatures more than 10 minutes old ([[Structured-Log-Redaction-and-Preventing-PII-and-Token-Leaks]]).

> [!warning] Treating the event as the truth
> Trigger: the finalizer copies `size` and `eTag` from the event and marks the record done.
> Symptom: duplicate deliveries double-count, out-of-order deliveries regress state, a forged or misrouted event activates a record whose object never arrived, and the event carries no content type at all.
> Fix: authenticate the channel, use the event only as a key to look up, read the object back, and make every transition idempotent; `test_finalization_trusts_storage_not_the_event_body` sends a forged event before the upload and the record stays `pending`.

> [!warning] Hand-rolled URI encoding
> Trigger: `urllib.parse.quote` with its default `safe="/"`, or `quote_plus`, used for SigV4 query values.
> Symptom: `/` in `X-Amz-Credential` left unencoded or a space sent as `+`, so the canonical query string differs from storage's and every request is `403`; AWS warns that platform encoders "may not work".
> Fix: one `uri_encode` that keeps only the unreserved set and emits uppercase `%XX`; the lab's parametrized test pins seven cases including `*` -> `%2A` and `"\u00e9"` -> `%C3%A9`.

## Security and robustness

- Authorization lives in phase 1: the presign endpoint is a write endpoint and gets authentication, object-level authorization, and quota, because OWASP API1:2023 asks to "check if the logged-in user has access to perform the requested action on the record in every function that uses an input from the client to access a record in the database".
- Resource consumption: OWASP API4:2023 lists "Maximum upload file size" and "Third-party service providers' spending limit" among limits whose absence makes an API vulnerable; a signed exact `Content-Length` or a `content-length-range` enforces the first at the storage edge, and a per-user cap on outstanding `pending` records bounds the second ([[Unrestricted-Resource-Consumption-API4-2023-Payload-Depth-and-Limit-Guards]]).
- Key choice: the server derives `uploads/<owner>/<upload_id>`; a filename is metadata, so traversal, cross-tenant writes, and overwrite-by-guess are not expressible.
- Stored XSS: signing `Content-Type` only binds a label; sniff leading bytes, serve user content from a separate origin, and set `Content-Disposition: attachment` where browsers might render it ([[Content-Negotiation-Media-Types-and-Charsets]]).
- Integrity: `UNSIGNED-PAYLOAD` means the signature never covers the bytes; sign `x-amz-checksum-sha256` when the client can hash first, and compare the final `ETag` or checksum before downstream processing.
- Constant-time checks: every secret comparison (query signature, policy signature, checksum, event HMAC) goes through one helper that calls `hmac.compare_digest` on UTF-8 bytes, because `compare_digest` raises `TypeError` for a `str` argument with non-ASCII characters (CPython 3.14.6 says `comparing strings with non-ASCII characters is not supported`), so a `\u00e9` in `X-Amz-Signature` would otherwise be an unhandled exception (calling `compare_digest` on the `str` values fails four tests); a spy test asserts all four calls.
- Malformed input: a missing parameter, a non-numeric `Content-Length`, a malformed event, a signed policy with a malformed condition, or a signed form with no `key` is a `400` or `403`, never a `500`.
- Parser bounds: the store's form parser stops at one file and 32 fields before the signature is checked; it bounds the number of parts, not their bytes, and the stand-in still reads the whole file part into memory, which is acceptable only in an in-process test double.
- Fail closed: `UploadService` refuses to start if `allowed_types` names a type it has no magic-byte signature for, because an empty prefix would match every body.
- Storage-side bugs: MinIO before `RELEASE.2023-03-20T20-16-18Z` let crafted requests "bypass metadata bucket name checking and put an object into any bucket while processing `PostPolicyBucket`" (CVE-2023-28434, CVSS 3.1 8.8); keep S3-compatible servers patched, and keep the phase-2 check so a storage bug cannot silently activate a record.
- Credential lifetime: a presigned URL "expires when the credential you used to create it is revoked, deleted, or deactivated", so revoking the signing role is the emergency stop, at the cost of every outstanding URL.
- Orphans: uploads that never finalize are deleted by the sweeper after the URL lifetime plus grace, and late arrivals are deleted on their event, so storage holds no bytes without a record; in production also add a bucket lifecycle rule on the `uploads/` prefix as a backstop.

## The principal's view

The architectural decision is to split an upload into an authorization transaction and a data transfer, and to own the state machine between them, instead of either proxying bytes through the API fleet or handing clients raw bucket access.
Proxying gives you byte-level control and a single trust boundary but makes every application server a bandwidth, memory, and timeout hotspot ([[Multipart-Form-Streaming-and-Upload-Memory-Bounds]], [[100-Continue-Expect-Header-and-Large-Upload-Admission]]); presigning moves the bytes onto storage's edge at the cost of a second, eventually consistent phase that every client must handle.
Make the upload a first-class resource with a documented lifecycle (`pending`, `active`, `rejected`, `expired`), poll it like an LRO, and reference it by id from the business object, so no consumer ever reads an object that has not been finalized.
Standardize the signing library and the allowed signed headers across teams; the CVEs above are all teams that presigned with fewer constraints than the storage API supports or skipped the server-side check.
Choose a resumable protocol when files are large enough that a dropped connection costs real money or time ([[Resumable-HTTP-Uploads-IETF-Draft-and-Tus-Protocol]]), and choose proxying when you must scan, transcode, or meter every byte inline and can pay for the fleet.
Organizationally, the hard part is the event path: it crosses team boundaries (storage, messaging, the API), it is at-least-once, and its failures are silent, so give it an owner, an alerting signal on records stuck in `pending`, and a transactional handoff to downstream consumers ([[Transactional-Outbox-Pattern-Solving-Dual-Writes-Between-DB-and-Events]]).

## Interview questions

> [!question] L5: Why does a presigned S3 upload URL not protect the bytes being uploaded, and how do you make it do so?
>
> > [!success]- Answer
> > The canonical request for a presigned URL uses the constant `UNSIGNED-PAYLOAD` instead of a payload hash, because the bytes are unknown when the URL is minted.
> > So the signature binds the method, key, query parameters, and signed headers, but any body that matches the signed `Content-Length` is accepted.
> > To bind the bytes, the client hashes the file first and the API signs `x-amz-checksum-sha256` with that base64 SHA-256, which storage verifies.
> >
> > Follow-up: What does that cost the client, and when is it impractical?

> [!question] L5: Which decisions belong in the API's presign endpoint, and which can be left to the client?
>
> > [!success]- Answer
> > The API decides who may upload, the object key, the allowed type, the maximum size, and the lifetime, and binds each into the signature or policy.
> > The client chooses only what the signature leaves open, so anything unsigned is effectively attacker-controlled.
> > CVE-2026-48768 is the counterexample: the key came from the client's filename and `Content-Type` was unsigned.
> >
> > Follow-up: Where does the user's original filename go?

> [!question] L5: Predict the status codes: the lab mints a `PUT` URL with `X-Amz-Expires=900` at 12:00:00; the client sends it at 12:15:01, then again at exactly 12:15:00, then once more.
>
> > [!success]- Answer
> > 12:15:01 is `403` with reason `expired`, because the window is inclusive and ends at 12:15:00.
> > 12:15:00 is `200`, the last valid second.
> > The third request is `412 Precondition Failed`, because the signed `If-None-Match: *` now sees an existing object.
> >
> > Follow-up: What would the third request return if `if-none-match` were not signed?

> [!question] L6: If the S3 event already says `size: 64` for your key, why do you still read the object back before marking the upload active?
>
> > [!success]- Answer
> > Events are at-least-once and unordered, so the message may be a duplicate, stale, or about an object that has since been overwritten.
> > The event carries no content type and no bytes, so it cannot prove the upload matches what was authorized.
> > A forged or misrouted event could otherwise activate a record whose object never arrived, which the lab's forged-event test shows does not happen.
> >
> > Follow-up: How would you detect that an accepted object was later replaced?

> [!question] L6: Debug this API: a new service mints presigned URLs with a hand-written signer; boto3-minted URLs work, but every URL from the new signer is `403`, and its logged canonical query contains `X-Amz-Credential=AKID/20261111/eu-west-1/s3/aws4_request`.
>
> > [!success]- Answer
> > The signer encoded values with an encoder that keeps `/`, such as `urllib.parse.quote` with its default `safe="/"`, so it hashed a canonical query with literal slashes while storage hashes `AKID%2F20261111%2F...`.
> > SigV4 encodes names and values separately, keeps only the unreserved set, uses uppercase hex, and encodes `/` everywhere except the object path.
> > Fix it with one shared `uri_encode` and a test against the AWS example vector; check also for `+` instead of `%20` and for parameters sorted before encoding.
> >
> > Follow-up: Why does the order of query parameters on the wire not matter, while their encoding does?

> [!question] L6: When do you choose a POST policy over a presigned `PUT`?
>
> > [!success]- Answer
> > Choose a POST policy when the client does not know the exact size up front, because `content-length-range` bounds it with a range, or when the upload comes from a plain HTML form.
> > Choose a presigned `PUT` when you can sign the exact `Content-Length`, a `Content-Type`, `If-None-Match: *`, and optionally a checksum.
> > Either way the policy only states rules; you still need the finalization check, as GHSA-w5fh-f8xh-5x3p showed with a store that ignored them.
> >
> > Follow-up: How do you prevent a POST policy from being replayed to overwrite an accepted object?

> [!question] L7+: Your platform presigns uploads for 40 teams; what do you standardize, and what do you leave to teams?
>
> > [!success]- Answer
> > Standardize one signing library with a fixed set of signed headers, a lifetime ceiling enforced both in code and with `s3:signatureAge` in bucket policy, server-derived keys, and a shared upload-resource state machine with finalization.
> > Leave the allowed types, size limits, and downstream processing to teams, expressed as configuration the library validates.
> > Measure records stuck in `pending`, rejected reasons, and orphan deletions, because the event path fails silently.
> >
> > Follow-up: How do you roll out a change to the signed header set without breaking URLs already issued?

> [!question] L7+: What do you tell a product team that wants seven-day upload links in emails?
>
> > [!success]- Answer
> > A seven-day presigned URL is a week-long bearer credential that can be forwarded and replayed, and it pins the signing key for that long.
> > Send a link to the application instead, and mint a short-lived presigned URL after the user authenticates, so the capability lives for minutes.
> > If unauthenticated links are a product requirement, scope each to one server-chosen key with `If-None-Match: *`, a size and type bound, and a revocable upload record the finalizer checks.
> >
> > Follow-up: How would you revoke one outstanding link without rotating the signing credentials?

## Related

Prerequisites: [[Multipart-Form-Streaming-and-Upload-Memory-Bounds]]

Next: [[Resumable-HTTP-Uploads-IETF-Draft-and-Tus-Protocol]]

See also: [[Long-Running-Operations-202-Accepted-and-Operation-Resources-AIP-151]], [[Asynchronous-Job-Result-Expiration-Cancellation-and-Wait-Semantics]], [[Idempotent-Request-Identification-and-Request-ID-Correlation-AIP-155]], [[Error-Taxonomy-Retryability-and-Client-Contract-Design]], [[Problem-Details-for-HTTP-APIs-RFC-9457]], [[100-Continue-Expect-Header-and-Large-Upload-Admission]], [[Optimistic-Concurrency-Control-If-Match-and-412-Precondition-Failed]], [[Opaque-Cursor-Encoding-HMAC-Integrity-and-Filter-Binding]], [[Webhook-Consumer-Engineering-Signature-Verification-Replay-Windows-and-Idempotency]], [[Transactional-Outbox-Pattern-Solving-Dual-Writes-Between-DB-and-Events]], [[Defeating-BOLA-API1-2023-Tenant-Scoped-Queries-and-PostgreSQL-RLS]], [[Unrestricted-Resource-Consumption-API4-2023-Payload-Depth-and-Limit-Guards]], [[Structured-Log-Redaction-and-Preventing-PII-and-Token-Leaks]], [[Content-Negotiation-Media-Types-and-Charsets]]

- Lab: `labs/tests/f06_bulk_long_running_and_error_contracts/test_presigned_object_storage_urls_and_direct_to_storage_uploads.py`

## Further reading

- AWS S3, "Authenticating Requests: Using Query Parameters (AWS Signature Version 4)": the presigned URL parameters, the 604800-second ceiling, `UNSIGNED-PAYLOAD`, and the worked example with its signature.
- AWS S3, "POST Policy" and "Example: Browser-Based Upload using HTTP POST": condition match types, `content-length-range`, the field-coverage rule, and a test vector.
- AWS S3 User Guide, "Download and upload objects with presigned URLs": reuse until expiry, credential-bound lifetimes, and `s3:signatureAge`.
- AWS S3 User Guide, "How to prevent object overwrites with conditional writes": `If-None-Match: *` semantics with `412` and `409`.
- AWS S3 User Guide, "Event message structure": the URL-encoded key, `eTag`, and `sequencer` ordering rules.
- Google Cloud Storage, "Signed URLs" and the XML API headers reference: the V4 equivalent and `x-goog-content-length-range`.
- botocore 1.44.0 `botocore/signers.py` and `botocore/auth.py`: the reference implementation of query-string signing and POST policy signing.
- RFC 2104: HMAC, the primitive under every signature here.
- RFC 3986 Sections 2.1 and 2.3: percent-encoding and the unreserved set that SigV4's `UriEncode()` follows.
- RFC 9110 Sections 8.6, 13.1.2, and 15.3.2: `Content-Length`, `If-None-Match`, and `201 Created` with `Location`.
- RFC 9113 Sections 8.1.1 and 8.3.1: `content-length` versus `DATA` frames and `:authority` versus `Host` in HTTP/2.
- RFC 9114 Sections 4.1.2 and 4.3.1: the same two rules in HTTP/3, which is why a signed `host` and a signed `content-length` survive the move to QUIC.
- AWS S3, "Error responses": the error codes and HTTP statuses that the lab store's `error` strings reuse.
- CVE-2026-48768 (TypeBot), CVE-2026-50136 (Budibase), GHSA-w5fh-f8xh-5x3p (RustFS), and CVE-2023-28434 (MinIO): the four ways presigned uploads have failed in practice.
