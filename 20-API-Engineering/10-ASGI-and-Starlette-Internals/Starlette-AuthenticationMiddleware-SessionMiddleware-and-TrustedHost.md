---
type: concept
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://www.rfc-editor.org/rfc/rfc6265.txt"
  - "https://www.rfc-editor.org/rfc/rfc9110.txt"
  - "https://www.rfc-editor.org/rfc/rfc9111.txt"
  - "https://www.rfc-editor.org/rfc/rfc9112.txt"
  - "https://www.rfc-editor.org/rfc/rfc6797.txt"
  - "https://www.rfc-editor.org/rfc/rfc6455.txt"
  - "https://www.rfc-editor.org/rfc/rfc2104.txt"
  - "https://raw.githubusercontent.com/Kludex/starlette/1.10.0/docs/release-notes.md"
  - "https://raw.githubusercontent.com/Kludex/starlette/0.46.0/starlette/middleware/sessions.py"
  - "https://raw.githubusercontent.com/Kludex/starlette/1.0.0/starlette/middleware/sessions.py"
  - "https://raw.githubusercontent.com/Kludex/starlette/1.0.0/starlette/middleware/trustedhost.py"
  - "https://raw.githubusercontent.com/Kludex/starlette/1.0.0/starlette/middleware/httpsredirect.py"
  - "https://github.com/advisories/GHSA-86qp-5c8j-p5mr"
  - "https://github.com/advisories/GHSA-jp82-jpqv-5vv3"
  - "https://api.github.com/advisories?ecosystem=pip&affects=starlette"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2023-30861"
  - "https://owasp.org/API-Security/editions/2023/en/0xa2-broken-authentication/"
  - "https://owasp.org/API-Security/editions/2023/en/0xa8-security-misconfiguration/"
  - "https://raw.githubusercontent.com/django/asgiref/3.13.0/specs/www.rst"
  - "https://raw.githubusercontent.com/Kludex/starlette/1.6.0/starlette/middleware/httpsredirect.py"
  - "https://raw.githubusercontent.com/Kludex/starlette/1.6.0/starlette/middleware/trustedhost.py"
  - "https://raw.githubusercontent.com/Kludex/starlette/1.7.0/starlette/middleware/httpsredirect.py"
  - "https://raw.githubusercontent.com/Kludex/starlette/1.7.0/starlette/middleware/trustedhost.py"
  - "https://api.github.com/advisories/GHSA-86qp-5c8j-p5mr"
  - "https://api.github.com/advisories/GHSA-jp82-jpqv-5vv3"
  - "https://www.ietf.org/archive/id/draft-ietf-httpbis-rfc6265bis-22.txt"
---

# Starlette Security Middlewares: AuthenticationMiddleware, Signed Cookie SessionMiddleware (itsdangerous), HTTPSRedirect, and TrustedHostMiddleware

> [!summary] Claim
> Starlette's four security middlewares are small pure-ASGI guards with sharp edges: `TrustedHostMiddleware` allows every host by default and enforces its wildcard rule with `assert` (so `python -O` turns `*example.com` into a suffix bypass), `HTTPSRedirectMiddleware` trusts `scope["scheme"]` and loops forever behind a TLS proxy unless the server rewrites the scheme, `SessionMiddleware` stores the whole session client-side as readable base64 JSON signed with HMAC-SHA1 (so logout cannot revoke a captured cookie and the secret key mints any identity), and `AuthenticationMiddleware` runs outside `ExceptionMiddleware`, answers failures with a `400` that echoes the exception text, and closes WebSockets with code `1000`.

Part of [ASGI and Starlette Internals](README.md) in [API Engineering](../README.md).

## TL;DR

- Mechanism: each middleware is a class with `async __call__(scope, receive, send)`; `TrustedHostMiddleware` and `HTTPSRedirectMiddleware` short-circuit with a `400` or `307` before the app runs, `SessionMiddleware` decodes the `session` cookie into `scope["session"]` and wraps `send` to append `Set-Cookie` on `http.response.start`, and `AuthenticationMiddleware` writes `scope["auth"]` and `scope["user"]` from a pluggable backend (starlette 1.10.0 `starlette/middleware/*.py`).
- Contract rule: order is part of the security model; list them outermost first as `HTTPSRedirect -> TrustedHost -> Session -> Authentication`, because the authentication backend can only read `conn.session` if `SessionMiddleware` already ran, and host checks must precede any code that builds URLs from `Host`.
- Trap: a signed cookie is not an encrypted or revocable one; the payload is `base64(json)` anyone can read, the server checks only the HMAC and a timestamp no older than `max_age` (default `1209600` seconds), and `session.clear()` only asks the browser to delete its copy, so a stolen cookie keeps working for up to 14 days (measured in the lab).
- Portability fact: behavior changed sharply across Starlette 0.x and 1.x: before `1.0.0` the session was re-signed on every response and never sent `Vary: Cookie`; `1.0.0` tracks access and modification (so nested mutations are silently lost); `1.0.1` fixed Host header poisoning of `request.url` (GHSA-86qp-5c8j-p5mr); `1.9.0` made host matching case-insensitive (starlette 1.10.0 `docs/release-notes.md`).
- When not to use it: do not use cookie sessions for anything that must be revocable, large, or confidential; use an opaque session ID with server-side storage (RFC 6265 Section 8.4), and enforce host and TLS policy at the edge proxy, keeping these middlewares as defense in depth.

## Mental model

```mermaid
flowchart LR
    C[Client request<br/>Host, Cookie, Authorization] --> SE[ServerErrorMiddleware]
    SE --> HR{HTTPSRedirect<br/>scope scheme is http or ws?}
    HR -- yes --> R307[307 Location https or wss<br/>port kept unless 80 or 443]
    HR -- no --> TH{TrustedHost<br/>parse_host_header then<br/>exact or *.suffix match}
    TH -- no match --> R400[400 Invalid host header<br/>or 307 to www.]
    TH -- match --> SM[SessionMiddleware<br/>unsign cookie -> scope session<br/>bad or expired -> empty Session]
    SM --> AM{AuthenticationMiddleware<br/>backend.authenticate conn}
    AM -- AuthenticationError --> E400[on_error: default 400 text<br/>WebSocket: close 1000]
    AM -- tuple or None --> EM[ExceptionMiddleware]
    EM --> RT[Router and endpoint<br/>requires scopes -> 403 or 303]
    RT -. http.response.start .-> SM2[send_wrapper<br/>Vary Cookie if accessed<br/>Set-Cookie if modified]
    SM2 -.-> C
```

A request enters through `ServerErrorMiddleware`, which Starlette always places outermost, then meets the user middlewares in list order.
`HTTPSRedirectMiddleware` looks only at `scope["scheme"]`, never at headers, and answers plain `http` or `ws` with a `307` to the `https` or `wss` URL.
`TrustedHostMiddleware` parses the `Host` header with a strict grammar and rejects anything outside the allow list with `400`, or redirects the bare domain to `www.` when only the `www.` form is allowed.
`SessionMiddleware` turns the signed cookie into a `dict` subclass in `scope["session"]`, falling back silently to an empty session when the signature or timestamp is bad.
`AuthenticationMiddleware` asks the backend for `(AuthCredentials, BaseUser)` and stores them in `scope`, or answers the request itself when the backend raises `AuthenticationError`.
Only after these does `ExceptionMiddleware` start catching `HTTPException`, which is why `@requires` can raise `403` cleanly but a backend cannot.
On the way out, the session middleware's `send_wrapper` intercepts `http.response.start` and appends `Vary: Cookie` and `Set-Cookie` according to the session's `accessed` and `modified` flags.

## How it works

### Where the four middlewares sit

`Starlette.build_middleware_stack` builds the list `[ServerErrorMiddleware, (RequestBodyLimitMiddleware if max_body_size), *user_middleware, ExceptionMiddleware]` and wraps the router in reverse, so the first entry in `middleware=[...]` is the outermost user layer (starlette 1.10.0 `starlette/applications.py`, `Starlette.build_middleware_stack`).
`Starlette.add_middleware` does `self.user_middleware.insert(0, ...)`, so with `add_middleware` the last call becomes the outermost layer, the reverse of the list form (starlette 1.10.0 `starlette/applications.py`, `Starlette.add_middleware`); FastAPI inherits this method.
FastAPI re-exports `TrustedHostMiddleware` and `HTTPSRedirectMiddleware` under `fastapi.middleware`, but not `SessionMiddleware` or `AuthenticationMiddleware`, which you import from `starlette.middleware` (fastapi 0.162.0 `fastapi/middleware/`).
All four are pure ASGI classes, not `BaseHTTPMiddleware` subclasses, so they do not buffer streaming bodies or break `ContextVar` propagation (see [[Why-BaseHTTPMiddleware-Hurts-Performance-Streaming-and-ContextVars]] and [[Pure-ASGI-Middleware-Onion-Architecture-and-Send-Receive-Wrapping]]).
Each passes non-`http`, non-`websocket` scopes (that is, `lifespan`) straight through.

### TrustedHostMiddleware: the Host allow list

The `Host` field carries the target URI's authority and "is a frequent target for malware seeking to poison a shared cache or redirect a request to an unintended server" (RFC 9110 Section 7.2).
A server "MUST respond with a 400 (Bad Request) status code to any HTTP/1.1 request message that lacks a Host header field and to any request message that contains more than one Host header field line or a Host header field with an invalid field value" (RFC 9112 Section 3.2).
Servers enforce only part of that: h11 raises `LocalProtocolError` for a missing `Host` on HTTP/1.1 and for more than one `Host` line (h11 0.16.0 `h11/_events.py`, `Request.__init__`).
`httptools`, uvicorn's `http="auto"` choice whenever it is importable (uvicorn 0.54.0 `uvicorn/protocols/http/auto.py`), does not: the httptools 0.9.0 `HttpRequestParser` accepted `GET / HTTP/1.1` with no `Host`, with two `Host` lines, and with `Host: a/b?c` (probe run 2026-11-14, CPython 3.14.6), and uvicorn 0.54.0 `uvicorn/protocols/http/httptools_impl.py` contains no `Host` check, so with `uvicorn[standard]` the RFC 9112 rule is not enforced by the server at all.
No server checks the value against the names you serve; that is the middleware's job.

Construction (starlette 1.10.0 `starlette/middleware/trustedhost.py`, `TrustedHostMiddleware.__init__`):

- `allowed_hosts=None` becomes `["*"]`, and `allow_any = "*" in allowed_hosts`, which makes `__call__` a pass-through: the default configuration validates nothing.
- Each pattern must have `*` only in position 0 and, if it starts with `*` and is not `*`, must start with `*.`; both rules are `assert` statements with the message `Domain wildcard patterns must be like '*.example.com'.`
- Patterns are lowercased (since 1.9.0, PR 3542 in the release notes); before that, matching was case-sensitive.

Request path (`TrustedHostMiddleware.__call__`):

1. `parse_host_header(headers.get("host"))` returns `None` for a missing header or anything that fails `_HOST_RE.fullmatch` (starlette 1.10.0 `starlette/_utils.py`, `parse_host_header`).
   The regex accepts a `reg-name` of `[a-z0-9._~%!$&'()*+,;=-]+` or a bracketed IPv6 or `vFuture` literal, then an optional `:` and digits; `/`, `?`, `#`, `@`, `\`, and spaces are rejected, and a bracketed IPv6 literal must also parse as `ipaddress.IPv6Address`.
   `None` -> `400` with body `Invalid host header`.
2. `host = parsed_host.host.lower()`; the port is dropped and never compared, so `example.com:8443` passes an `example.com` rule.
3. For each pattern: exact equality, or for a `*` pattern `host.endswith(pattern[1:])`, so `*.example.com` tests `endswith(".example.com")`, which matches `a.example.com` and `a.b.example.com` but not the apex `example.com` and not `evilexample.com`.
4. If no pattern matched but some pattern equals `"www." + host`, and `www_redirect=True` (the default), the response is `RedirectResponse(url.replace(netloc="www." + url.netloc))`, whose default status is `307` (starlette 1.10.0 `starlette/responses.py`, `RedirectResponse.__init__`); port, path, and query are kept.
5. Otherwise `PlainTextResponse("Invalid host header", status_code=400)`.

What it does not do: it does not normalize a trailing-dot FQDN (`example.com.` is rejected, which fails closed), it does not consult `X-Forwarded-Host` or `Forwarded` (Starlette reads neither), and for WebSocket scopes its `400` goes out as a WebSocket Denial Response (`websocket.http.response.start`) through `Response._wrap_websocket_denial_send` (starlette 1.10.0 `starlette/responses.py`).

Before 1.7.0 the host was taken as `headers.get("host", "").split(":")[0]` (starlette 1.0.0 and 1.6.0 `starlette/middleware/trustedhost.py`, `TrustedHostMiddleware.__call__`), which turned `[::1]:8000` into `[` and accepted any characters; 1.7.0 added IPv6 support and consistent malformed-host handling (PRs 3471 and 3472).

Why it matters beyond routing: `request.url`, `request.base_url`, and `request.url_for()` build absolute URLs from the `Host` header (starlette 1.10.0 `starlette/datastructures.py`, `URL.__init__`; `starlette/requests.py`, `HTTPConnection.url_for`), so without an allow list a password-reset link built with `url_for` points at whatever host the attacker sent.
Since 1.0.1 (PR 3279, "Ignore malformed `Host` header when constructing `request.url`") a malformed `Host` no longer rewrites `request.url.path`, which closed GHSA-86qp-5c8j-p5mr (CVE-2026-48710); in 1.10.0 the mechanism is that `URL.__init__` falls back to `scope["server"]` when `parse_host_header` fails or the port is out of range (`ParsedHost.is_valid_port`, at most five digits and `<= 65535`), and to a host-less relative URL when there is no `scope["server"]` either.

### HTTPSRedirectMiddleware: scheme upgrade by redirect

The body of `__call__` is twelve lines (starlette 1.10.0 `starlette/middleware/httpsredirect.py`, `HTTPSRedirectMiddleware.__call__`):

- Trigger: `scope["type"]` in `("http", "websocket")` and `scope["scheme"]` in `("http", "ws")`; nothing else is consulted.
- `url = URL(scope=scope)`; if `url.netloc` is empty (no usable `Host` and no `scope["server"]`), `400 Invalid host header`; this branch is absent at the 1.0.0 and 1.6.0 tags and present at 1.7.0, whose release notes credit malformed-host handling in the "redirect middleware" to PR 3472 (starlette 1.6.0 and 1.7.0 `starlette/middleware/httpsredirect.py`).
- Scheme map `http -> https`, `ws -> wss`; the netloc becomes the bare hostname when `url.port in (80, 443)`, otherwise the port is kept, so `http://h:8080/x` redirects to `https://h:8080/x`, which is almost never where TLS listens.
- `RedirectResponse(url, status_code=307)`: `307` forbids the client from changing the method on redirect (RFC 9110 Section 15.4.8), so a `POST` body is replayed to the `https` URL after having already crossed the network in cleartext.
- `307` is not heuristically cacheable (RFC 9110 Section 15.1 lists `200, 203, 204, 206, 300, 301, 308, 404, 405, 410, 414, 501`), so each plain-HTTP visit pays a round trip; RFC 6797 Section 7.2 suggests a permanent redirect such as `301` for HSTS hosts.

It sends no `Strict-Transport-Security`, and it must not on the plain-HTTP response (RFC 6797 Section 7.2: "An HSTS Host MUST NOT include the STS header field in HTTP responses conveyed over non-secure transport"); the first plain request is still exposed to the bootstrap MITM (RFC 6797 Section 14.6).
For WebSockets the `307` is sent as a Denial Response; RFC 6455 Section 4.1 says clients "are not required to follow" a `3xx`, so a `ws://` client typically fails rather than upgrading.

Behind a TLS-terminating proxy the scheme that reaches the app is `http`, so the middleware redirects every request, the client follows to `https`, the proxy terminates TLS again, and the loop never ends.
Uvicorn fixes `scope["scheme"]` only through `ProxyHeadersMiddleware`, which is on by default (`proxy_headers=True`) but trusts `X-Forwarded-Proto` only from `forwarded_allow_ips`, defaulting to `FORWARDED_ALLOW_IPS` or `127.0.0.1,::1` (uvicorn 0.54.0 `uvicorn/config.py`, `Config.__init__` and `Config.load`; `uvicorn/middleware/proxy_headers.py`, `ProxyHeadersMiddleware.__call__`).
A proxy on another host is therefore ignored unless you configure its address; see [[Forwarded-Header-RFC-7239-and-X-Forwarded-For-Spoofing]].

### SessionMiddleware: the cookie, byte by byte

Constructor (starlette 1.10.0 `starlette/middleware/sessions.py`, `SessionMiddleware.__init__`): `secret_key: str | Secret`, `session_cookie="session"`, `max_age=14 * 24 * 60 * 60`, `path="/"`, `same_site="lax"`, `https_only=False`, `domain=None`, `partitioned=False`.
It builds `itsdangerous.TimestampSigner(str(secret_key))` with the default salt and algorithm, and precomputes `security_flags = "httponly; samesite=" + same_site`, plus `; secure` only if `https_only`, `; domain=...`, and `; partitioned`.
`str(secret_key)` means a key list cannot be passed: `str(["old", "new"])` would silently become the literal key `['old', 'new']`.

The cookie value has three `.`-separated parts (itsdangerous 2.2.0 `src/itsdangerous/timed.py`, `TimestampSigner.sign`; `signer.py`, `Signer.get_signature`; `encoding.py`); this one is `{"user": "alice", "scopes": ["authenticated"]}` signed with the lab key `lab-secret-EXAMPLE-NOT-REAL` at `1700000000`, as computed by the lab's `sign_session_cookie`:

```text
session=eyJ1c2VyIjogImFsaWNlIiwgInNjb3BlcyI6IFsiYXV0aGVudGljYXRlZCJdfQ==.ZVPxAA.4bHvyYvIi6Og3Km8Adr7Efp_mu8
        |<---- standard base64 of json.dumps(session), "=" padded ---->| |ts  | |<- HMAC-SHA1, b64url ->|
```

1. Payload: `b64encode(json.dumps(session).encode("utf-8"))`, standard alphabet (`+`, `/`, `=`), which RFC 6265 Section 4.1.1 `cookie-octet` permits; this is encoding, not encryption.
2. Timestamp: `int(time.time())` packed as big-endian `>Q`, leading zero bytes stripped, base64url without padding; for any timestamp from `2**24` (July 1970) to `2**32 - 1` (February 2106) that is 4 bytes -> 6 characters (`ZVPxAA` is `1700000000`) (itsdangerous 2.2.0 `encoding.py`, `int_to_bytes`).
3. Signature: `HMAC(key, payload + b"." + ts, sha1)`, base64url without padding: 20 bytes -> 27 characters.
4. Key derivation `django-concat`: `key = sha1(b"itsdangerous.Signer" + b"signer" + secret_key).digest()` (`Signer.derive_key`); the default digest is SHA-1, loaded lazily for FIPS builds (`_lazy_sha1`).
   HMAC with SHA-1 is not broken by SHA-1 collisions (the `Signer` docstring: "the security of the hash alone doesn't apply when used intermediately in HMAC"), and RFC 2104 Section 3 asks for a random key, says "less than L bytes is strongly discouraged" (L is the hash output length, 20 bytes here), and asks for periodic refresh.

The lab reproduces this cookie byte for byte from `hashlib`, `hmac`, and `base64` alone, which is the practical meaning of "the secret key is the whole security model".

Inbound path (`SessionMiddleware.__call__`): if the cookie is present, `self.signer.unsign(data, max_age=self.max_age)`, then `Session(json.loads(b64decode(data)))`; any `BadSignature` (including its subclasses `BadTimeSignature` and `SignatureExpired`) yields an empty `Session()` with no log line and no error.
`unsign` splits on the last `.` twice, verifies the HMAC with `hmac.compare_digest` for each key from newest to oldest (`Signer.verify_signature`), then rejects `age > max_age` and `age < 0` (a future timestamp) (`TimestampSigner.unsign`); `age == max_age` is still valid.
The cookie is parsed by Starlette's own `cookie_parser`, which splits on `;`, keeps the last value for a repeated name, and merges multiple `Cookie` headers in order (starlette 1.10.0 `starlette/requests.py`, `cookie_parser` and `HTTPConnection.cookies`).

Change tracking since 1.0.0 (PR 3166): in 1.10.0 `Session` is a `dict` subclass whose `__setitem__`, `__delitem__`, `clear`, `pop` (of an existing key), `popitem`, `setdefault` (of a new key), `update`, and `|=` call `mark_modified()`, which also sets `accessed`, and `HTTPConnection.session` calls `mark_accessed()` on every property read (starlette 1.10.0 `starlette/middleware/sessions.py`, `Session`; `starlette/requests.py`, `HTTPConnection.session`).
`popitem()` and `|=` were silently not persisted until 1.7.0 (PR 3436), and `pop()` of an existing key gained `Vary: Cookie` only in 1.8.0 (PR 3186) (starlette 1.10.0 `docs/release-notes.md`).
Mutating a nested object (`request.session["cart"].append(x)`) calls none of these, so the change is never written.

Outbound path (`send_wrapper`, only for `http.response.start`):

| Condition | Header appended |
|---|---|
| `session.accessed` | `Vary: Cookie` via `MutableHeaders.add_vary_header`, which preserves existing `Vary` values since 1.9.0 |
| `session.modified and session` (non-empty) | `Set-Cookie: session=<value>; path=/; Max-Age=1209600; httponly; samesite=lax` (re-signed with a fresh timestamp) |
| `session.modified` and now empty, and the request carried a valid session | `Set-Cookie: session=null; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT; httponly; samesite=lax` |
| otherwise | nothing, so a read-only request never refreshes the expiry |

`Max-Age=` is emitted whenever `max_age is not None`, including `Max-Age=0` since 1.8.0 (PR 3651); `max_age=None` makes a browser-session cookie and disables the server-side age check entirely.

The contrast with 0.46.0 is large (starlette 0.46.0 `starlette/middleware/sessions.py`): `scope["session"]` was a plain `dict`, every response with a non-empty session re-signed and re-sent the cookie (sliding expiry, nested mutations persisted), and no `Vary: Cookie` was ever added.

WebSocket scopes get `scope["session"]` too, but `send_wrapper` only acts on `http.response.start`, so session writes made in a WebSocket handler are never persisted (measured in the lab).

### AuthenticationMiddleware and `@requires`

`AuthenticationMiddleware(app, backend, on_error=None)` (starlette 1.10.0 `starlette/middleware/authentication.py`, `AuthenticationMiddleware.__call__`):

1. Wraps `scope` in `HTTPConnection` and awaits `backend.authenticate(conn)`.
2. `AuthenticationError` -> `self.on_error(conn, exc)`; for HTTP the returned response is sent, and the default is `PlainTextResponse(str(exc), status_code=400)`, so the exception message reaches the client and the status is `400`, not `401` with a `WWW-Authenticate` challenge (RFC 9110 Section 11.6.1, Section 15.5.2).
3. For WebSocket scopes `on_error` is still called but its response is discarded and the middleware sends `{"type": "websocket.close", "code": 1000}`, which is "normal closure" rather than `1008` policy violation (RFC 6455 Section 7.4.1).
4. `None` -> `(AuthCredentials(), UnauthenticatedUser())`; then `scope["auth"], scope["user"] = auth_result` and the app runs.
   Any other exception, including `HTTPException`, propagates to `ServerErrorMiddleware` and becomes `500`, because `ExceptionMiddleware` sits inside the user middlewares.

The backend contract is `async def authenticate(self, conn) -> tuple[AuthCredentials, BaseUser] | None` (starlette 1.10.0 `starlette/authentication.py`, `AuthenticationBackend`).
`AuthCredentials.scopes` is a list of strings; `BaseUser` exposes `is_authenticated`, `display_name`, and `identity` (implemented on `SimpleUser` and `UnauthenticatedUser` since 1.7.0, PR 3271).
`request.user` and `request.auth` `assert` that the middleware ran (`"AuthenticationMiddleware must be installed to access request.user"`), and `request.session` asserts `"SessionMiddleware must be installed to access request.session"`.

`requires(scopes, status_code=403, redirect=None)` finds the parameter named `request` or `websocket` by signature inspection at decoration time and raises `Exception` if neither exists (`starlette/authentication.py`, `requires`).
At call time `has_required_scope` checks that every required scope is in `conn.auth.scopes`; on failure HTTP endpoints `raise HTTPException(status_code=status_code)` or, with `redirect`, return a `303` `RedirectResponse` to `f"{request.url_for(redirect)}?{urlencode({'next': str(request.url)})}"`, and WebSocket endpoints call `websocket.close()`, whose default code is `1000` (starlette 1.10.0 `starlette/websockets.py`, `WebSocket.close`).
The `next` value is built from `request.url`, so it is only as trustworthy as the `Host` header; validate it on the login side to avoid an open redirect.

## Numbers and limits

| Quantity | Value | Source |
|---|---|---|
| Default `max_age` | `1209600` s (14 days), checked server-side against the signed timestamp | starlette 1.10.0 `starlette/middleware/sessions.py` |
| Expiry boundary | valid while `0 <= now - ts <= max_age`; `age == max_age` passes, `max_age + 1` and `-1` fail | itsdangerous 2.2.0 `timed.py`, `TimestampSigner.unsign`; lab parametrized test |
| Signature | HMAC-SHA1, 20 bytes, 27 base64url characters | itsdangerous 2.2.0 `signer.py`; lab |
| Timestamp field | 4 bytes, 6 characters until `2**32` s (year 2106) | itsdangerous 2.2.0 `encoding.py`, `int_to_bytes`; lab |
| Cookie value length | `4 * ceil(n / 3) + 35` for an `n`-byte JSON session | lab, `test_cookie_value_length_is_4_ceil_n_over_3_plus_35` |
| Browser cookie floor | at least 4096 bytes per cookie (name, value, attributes), 50 per domain | RFC 6265 Section 6.1 |
| Largest JSON session under 4096 bytes | `3003` bytes with the default attributes (`session=` 8 + value + 49 bytes of attributes) | computed with the lab's `signed_cookie_value_length`, CPython 3.14.6, macOS 27.3 arm64 |
| Oversized session | Starlette emits it anyway; no size check exists in `send_wrapper` | starlette 1.10.0 source |
| `unsign` cost, 99-byte cookie | `4.88` us | `timeit.repeat(number=20000, repeat=5, timer=time.process_time)`, min of three runs, `itsdangerous.TimestampSigner.unsign(..., max_age=1209600)`, CPython 3.14.6, Apple M3 Pro, macOS 27.3 arm64, 2026-11-14; CPU time because the shared machine was at load average 80, where wall-clock runs gave 10 to 13 us |
| `unsign` cost, 4035-byte cookie | `7.02` us | same run |
| `sign` cost, 46-byte JSON payload | `3.58` us | same run |
| `unsign` of the 99-byte cookie against the oldest of 3 keys | `10.77` us (3 key derivations and 3 HMACs) | same run |
| Host header rejection | `400`, body `Invalid host header` (19 bytes) | starlette 1.10.0 `trustedhost.py`; lab |
| Redirect statuses | `HTTPSRedirect` `307`, `www_redirect` `307`, `@requires(redirect=...)` `303` | starlette 1.10.0 source; lab |
| Uvicorn trusted proxy default | `127.0.0.1,::1` unless `FORWARDED_ALLOW_IPS` is set | uvicorn 0.54.0 `uvicorn/config.py` |

The per-request session cost is one SHA-1 key derivation plus one HMAC per configured key tried, plus `b64decode` and `json.loads`; at about 5 us it is negligible next to any database call, but every key you keep for rotation adds one key derivation and one HMAC, about 3 us in the run above, to every request carrying an old or forged cookie.

## Portability

Only one leg ran, in the draft and again in the independent fact-check: the lab module under the workspace interpreter, CPython `3.14.6` on macOS `27.3` arm64, `47 passed`.
The other CPython legs and the Debian Trixie and Alpine musl containers were not run because lab checks were paused for this batch (2026-11-11); their cells cite source instead.

| Runtime or component | Behavior | Verification |
|---|---|---|
| CPython `3.14.6` | Every lab claim holds | Ran: `uv run --frozen pytest tests/f10_asgi_and_starlette_internals/test_starlette_authenticationmiddleware_sessionmiddleware_and_trustedhost.py -q`, `47 passed` |
| CPython `3.12.16`, `3.13.18`, `3.14.10`, `3.15.2`, `3.15.2t` | Middlewares use only `json`, `base64`, `hmac`, `hashlib`, and `re`; Starlette's only runtime branch here is `inspect.iscoroutinefunction` from 3.13 vs `asyncio.iscoroutinefunction` below, used by `requires` to pick the async wrapper | Source only: starlette 1.10.0 `starlette/_utils.py`, `is_async_callable`; not run (paused) |
| `python -O` on any CPython | `TrustedHostMiddleware` wildcard validation disappears; `*example.com` accepts `evilexample.com` with `200` | Ran on 3.14.6 in the lab (subprocess with `-O`) |
| starlette `< 1.0.0` | Plain `dict` session, re-signed on every non-empty response, no `Vary: Cookie` | starlette 0.46.0 `starlette/middleware/sessions.py` |
| starlette `1.0.0` to `1.6.x` | Access and modification tracking; nested mutations lost, and `popitem()` and `\|=` also lost until 1.7.0; `TrustedHost` splits on `:` | starlette 1.0.0 and 1.6.0 source; release notes PRs 3166 and 3436 |
| starlette `<= 1.0.0` / `< 1.3.0` | Host header poisons `request.url.path` (GHSA-86qp-5c8j-p5mr, fixed 1.0.1); a path without leading `/` poisons `request.url.hostname` (GHSA-jp82-jpqv-5vv3, fixed 1.3.0) | GitHub advisory records |
| starlette `1.7.0` / `1.9.0` / `1.10.0` | IPv6 hosts and consistent malformed-host handling; case-insensitive host match; omitted `headers` in `http.response.start` defaulted instead of `KeyError` | Release notes PRs 3471, 3472, 3542, 3721 |
| itsdangerous `2.x` | Key lists for rotation (2.0), timezone-aware timestamps (2.0), lazy SHA-1 | itsdangerous 2.2.0 `signer.py` and `timed.py` docstrings |
| Uvicorn `0.54.0` | Rewrites `scope["scheme"]` from `X-Forwarded-Proto` only for trusted client IPs; the h11 parser rejects missing or duplicate `Host`, the httptools parser (the default when installed) accepts both | Lab (`ProxyHeadersMiddleware`); h11 0.16.0 source; httptools 0.9.0 parser probe on 3.14.6 |
| Hypercorn `0.18.0` | Never rewrites `scope["scheme"]` by itself; the opt-in `hypercorn.middleware.ProxyFixMiddleware(app, mode="legacy", trusted_hops=1)` takes `X-Forwarded-Proto` (or `Forwarded` in `modern` mode) by hop count, with no client-IP allow list | hypercorn 0.18.0 `hypercorn/middleware/proxy_fix.py`, `ProxyFixMiddleware.__call__`; no proxy option in `hypercorn/config.py`; not run |
| Granian `2.8.7` | Its CLI and server apply no proxy headers; the opt-in `granian.utils.proxies.wrap_asgi_with_proxy_headers(app, trusted_hosts="127.0.0.1")` mirrors uvicorn's trusted-IP rule | granian 2.8.7 `granian/utils/proxies.py`, `wrap_asgi_with_proxy_headers`; not run |
| HTTP/1.1 | `Host` header is the source of `request.url` | RFC 9112 Section 3.2 |
| HTTP/2 and HTTP/3 | `:authority` replaces `Host` (RFC 9110 Section 7.2); the ASGI HTTP spec requires servers to put its value first in `headers` as `host`, replacing any existing `host` (asgiref 3.13.0 `specs/www.rst`, HTTP connection scope), so the middleware sees the same input | Specification only; not run |

## Worked example

Implementation: [implementation](../labs/src/api_labs/f10_asgi_and_starlette_internals/starlette_security_middlewares.py).
Test: [test](../labs/tests/f10_asgi_and_starlette_internals/test_starlette_authenticationmiddleware_sessionmiddleware_and_trustedhost.py).

The lab makes the session clock injectable and accepts a key-rotation list by replacing the middleware's public `signer` attribute (simplified excerpt):

```python
class ClockedTimestampSigner(itsdangerous.TimestampSigner):
    """TimestampSigner that reads a test clock instead of `time.time()`."""

    def __init__(self, secret_keys: Sequence[str], clock: Clock) -> None:
        super().__init__(list(secret_keys))
        self.clock = clock

    def get_timestamp(self) -> int:
        return self.clock.now


class ClockedSessionMiddleware(SessionMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        secret_keys: Sequence[str],
        clock: Clock,
        max_age: int | None = DEFAULT_MAX_AGE,
        https_only: bool = False,
    ) -> None:
        super().__init__(app, secret_key=secret_keys[-1], max_age=max_age, https_only=https_only)
        self.signer = ClockedTimestampSigner(secret_keys, clock)
```

An independent rebuild of the cookie, used both to prove the wire format and to forge sessions (simplified excerpt, comments trimmed):

```python
def sign_session_cookie(session: dict[str, Any], secret_key: str, timestamp: int) -> str:
    payload = base64.b64encode(json.dumps(session).encode("utf-8"))
    stamp = _b64url_nopad(timestamp.to_bytes(8, "big").lstrip(b"\x00"))
    signed = payload + b"." + stamp
    key = hashlib.sha1(ITSDANGEROUS_DEFAULT_SALT + b"signer" + secret_key.encode()).digest()  # noqa: S324
    signature = _b64url_nopad(hmac.new(key, signed, hashlib.sha1).digest())
    return (signed + b"." + signature).decode("ascii")
```

The application stack, outermost first:

```python
    middleware: list[Middleware] = []
    if https_redirect:
        middleware.append(Middleware(HTTPSRedirectMiddleware))
    if allowed_hosts is not None:
        middleware.append(Middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts))
    middleware.append(
        Middleware(
            ClockedSessionMiddleware,
            secret_keys=secret_keys,
            clock=clock,
            max_age=max_age,
            https_only=https_only,
        )
    )
    if authenticate:
        middleware.append(
            Middleware(
                AuthenticationMiddleware, backend=SessionOrBearerBackend(), on_error=on_error
            )
        )
    return Starlette(routes=ROUTES, middleware=middleware)
```

Three of the tests, verbatim:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_cookie_is_byte_identical_to_an_independent_hmac_sha1_rebuild() -> None:
    app = build_app(secret_keys=[KEY], clock=Clock(T0))
    async with client_for(app) as client:
        response = await client.post("/login")
    value = response.cookies["session"]
    assert value == sign_session_cookie(ALICE, KEY, T0)
    assert response.headers["set-cookie"] == (
        f"session={value}; path=/; Max-Age={DEFAULT_MAX_AGE}; httponly; samesite=lax"
    )
    assert response.headers["vary"] == "Cookie"
```

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_logout_cookie_does_not_revoke_a_captured_cookie() -> None:
    app = build_app(secret_keys=[KEY], clock=Clock(T0))
    async with client_for(app) as client:
        captured = (await client.post("/login")).cookies["session"]
        logout = await client.post("/logout")
        assert logout.headers["set-cookie"] == (
            "session=null; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT; httponly; samesite=lax"
        )
        client.cookies.clear()
        replay = await client.get("/me", headers=session_cookie(captured))
    assert (replay.status_code, replay.text) == (200, "alice")
```

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_reads_never_refresh_so_expiry_is_absolute_from_the_last_write() -> None:
    clock = Clock(T0)
    app = build_app(secret_keys=[KEY], clock=clock)
    async with client_for(app) as client:
        await client.post("/login")
        clock.now = T0 + DEFAULT_MAX_AGE - 10
        read = await client.get("/me")
        assert read.status_code == 200
        assert "set-cookie" not in read.headers  # active user, but no re-signing
        clock.now = T0 + DEFAULT_MAX_AGE + 1
        expired = await client.get("/me")
    assert expired.status_code == 403
```

The other tests cover tamper rejection, forging with the key, the `max_age` boundary, nested-mutation loss, key rotation, `Vary: Cookie`, `https_only`, WebSocket session loss, `on_error` defaults and overrides, `@requires` `403` and `303`, the WebSocket `1000` close, middleware misordering, `HTTPException` in a backend, a twelve-case `Host` matrix, `url_for` poisoning, `www` redirect, the `-O` wildcard bypass, `HTTPSRedirect` ports, the proxy loop, and the WebSocket Denial Response redirect.

Captured run (finish pass, after the type-annotation fixes to the lab), from `labs/`, CPython `3.14.6`, macOS `27.3` arm64 (Apple M3 Pro), 2026-11-14:

```text
$ uv run --frozen pytest tests/f10_asgi_and_starlette_internals/test_starlette_authenticationmiddleware_sessionmiddleware_and_trustedhost.py -q
...............................................                          [100%]
47 passed in 0.27s
```

## Pitfalls

> [!warning] The default `TrustedHostMiddleware` trusts everyone
> Trigger: `TrustedHostMiddleware(app)` or `allowed_hosts=["*"]` copied from a tutorial.
> Symptom: `allow_any` is `True` and every request passes; `url_for` in password-reset emails points at `Host: evil.example` (lab: `http://evil.example/reset/c-EXAMPLE`).
> Fix: list exact names and `*.` wildcards; fail the deploy if the list contains `*`; build security-relevant absolute URLs from configuration, not from the request.

> [!warning] `python -O` removes the wildcard validation
> Trigger: running with `-O` or `PYTHONOPTIMIZE=1` and a typo like `*example.com`.
> Symptom: the `assert` never runs, and `endswith("example.com")` admits `evilexample.com` (lab subprocess printed `200`).
> Fix: validate patterns yourself at startup with an explicit `raise`, and test the deployed interpreter flags.

> [!warning] HTTPS redirect loop behind a TLS proxy
> Trigger: `HTTPSRedirectMiddleware` behind a load balancer that terminates TLS, with uvicorn's `forwarded_allow_ips` left at `127.0.0.1,::1`.
> Symptom: every request gets `307` to the same `https` URL; browsers report too many redirects; `X-Forwarded-Proto: https` is ignored (lab: `307` from an untrusted client, `200` from a trusted one).
> Fix: set `--forwarded-allow-ips` to the proxy's addresses, or drop the middleware and redirect at the proxy, where HSTS also belongs.

> [!warning] Logout does not revoke a stateless session
> Trigger: `request.session.clear()` used as the logout and account-lock mechanism.
> Symptom: the browser deletes its cookie, but a copy captured by XSS-free means (proxy logs, a shared machine, a malicious extension) keeps authenticating until `max_age` (lab: replay after logout returned `200 alice`).
> Fix: store a session ID or a per-user `session_version` in the cookie and check it against a server-side store; rotate `secret_key` to kill every session at once.

> [!warning] Nested mutations are silently dropped since 1.0.0
> Trigger: `request.session["cart"].append(item)` code written for Starlette 0.x, where every response re-signed the whole dict.
> Symptom: the endpoint returns the updated cart, no `Set-Cookie` is sent, and the next request shows the old value (lab).
> Fix: reassign the top-level key (`session["cart"] = [*cart, item]`) or call `session.mark_modified()`; add an upgrade test for each session write path.

> [!warning] Active users are logged out at a fixed time
> Trigger: relying on activity to extend a session, as 0.x did.
> Symptom: since 1.0.0 read-only requests never refresh the cookie, so the session dies `max_age` after the last write even mid-use (lab: `200` at `max_age - 10`, `403` at `max_age + 1`).
> Fix: touch a field (for example `session["seen"] = now // 3600`) on a schedule to get sliding expiry, or keep absolute expiry deliberately and say so.

> [!warning] `Secure` is off by default
> Trigger: `SessionMiddleware(app, secret_key=...)` with the default `https_only=False`.
> Symptom: the browser sends the session over any plain-HTTP request to the host, including the one `HTTPSRedirectMiddleware` is about to redirect, and an active network attacker can also inject cookies over HTTP (RFC 6265 Section 8.6).
> Fix: `https_only=True`, and consider `session_cookie="__Host-session"` with `path="/"` and no `domain`; a `__Host-` cookie must carry `Secure`, `Path=/`, and no `Domain`, which locks it to the exact host (draft-ietf-httpbis-rfc6265bis-22 Section 4.1.3.2, an Internet-Draft as of 2026-11).

> [!warning] Cache leaks of personalized responses
> Trigger: a shared cache in front of responses that depend on the session, with no `Cache-Control: private`.
> Symptom: one user's page served to another; Flask CVE-2023-30861 is the documented case, where `Vary: Cookie` was set only when the session was accessed or modified but not when it was refreshed, letting a proxy cache one client's `Set-Cookie` (NVD CVE-2023-30861).
> Fix: Starlette 1.x adds `Vary: Cookie` on every session access and never refreshes without modifying, but code that reads `scope["session"]` directly bypasses `mark_accessed`; send `Cache-Control: private` or `no-store` on authenticated responses (see [[Caching-Authenticated-and-Multi-Tenant-API-Responses]]).

> [!warning] `AuthenticationError` text reaches the client as `400`
> Trigger: `raise AuthenticationError(f"user {name} disabled")` with the default `on_error`.
> Symptom: user enumeration and internal detail in a `400` body; clients cannot tell an auth failure from a malformed request, and there is no `WWW-Authenticate`.
> Fix: pass `on_error` returning a fixed `401` body with a challenge (lab `json_401_on_error`).

> [!warning] Raising `HTTPException` in a backend is a `500`
> Trigger: a backend that does `raise HTTPException(401)` as an endpoint would.
> Symptom: `ExceptionMiddleware` is inside the user middlewares, so `ServerErrorMiddleware` turns it into `500 Internal Server Error` (lab).
> Fix: raise `AuthenticationError` and map it in `on_error`; see [[Starlette-Exception-Hierarchy-ServerErrorMiddleware-vs-ExceptionMiddleware]].

> [!warning] Host header poisoning of `request.url` before 1.0.1
> Trigger: Starlette `<= 1.0.0` with path-based authorization reading `request.url.path`.
> Symptom: `Host: example.com/abc?bar=` made `request.url.path` `/abc` while the router dispatched `/foo` (GHSA-86qp-5c8j-p5mr, CVE-2026-48710, CVSS 6.5); a related path-carried variant affected `request.url.hostname` until 1.3.0 (GHSA-jp82-jpqv-5vv3, CVE-2026-54282, CVSS 3.7).
> Fix: upgrade, use `TrustedHostMiddleware`, and authorize on `scope["path"]` or the matched route, never on a reconstructed URL.
> On 1.10.0 `parse_host_header` rejects these values with `400`; on `<= 1.0.0` only exact-name patterns reject them, because a `*.example.com` pattern admits `Host: a/admin?x=.example.com` (it ends with `.example.com`) (starlette 1.0.0 `starlette/middleware/trustedhost.py`, `TrustedHostMiddleware.__call__`).

## Security and robustness

Threat model for the session cookie: the client is untrusted and holds the full state.
Integrity rests only on the HMAC key; confidentiality is zero (the lab decodes the payload without the key); freshness is the signed timestamp; there is no revocation, no binding to a client or channel, and no replay detection.
Never put secrets, PII, authorization decisions you would not let the user read, or anything larger than about 3 KB of JSON in it.
A leaked key is a total compromise: the lab mints `mallory` with nothing but `hashlib` and `hmac`, which is OWASP API2:2023 Broken Authentication ("Uses weak encryption keys", "Doesn't validate the authenticity of tokens").
Generate the key with `secrets.token_urlsafe(32)` or longer (at least the 20-byte SHA-1 output, RFC 2104 Section 3), load it from a secret store wrapped in `starlette.datastructures.Secret` so it does not appear in `repr`, and rotate it with a key list (`TimestampSigner([old, new])`), which today requires replacing `middleware.signer` as the lab does because the constructor stringifies the key.

Cookie integrity across sibling hosts: RFC 6265 Section 8.6 notes that `foo.example.com` can set a `Domain=example.com` cookie that `bar.example.com` cannot distinguish from its own.
With a signed session the attacker cannot forge content, but a sibling that shares the key, or the attacker's own valid session, can be planted (session swapping); Starlette's parser keeps the last `session` value, and user agents SHOULD list longer paths first and, among equal-length paths, earlier creation times first (RFC 6265 Section 5.4), so a newer planted cookie with the same path length wins.
Scope keys per host, do not set `domain=` unless needed, and prefer the `__Host-` prefix.

CSRF: `samesite=lax` withholds the cookie from cross-site subrequests and cross-site navigations with unsafe methods such as `POST`, but cross-site top-level navigations with safe methods such as `GET`, and every same-site request including those from sibling subdomains, still carry it (draft-ietf-httpbis-rfc6265bis-22 Sections 4.1.2.7 and 5.6.7.1, an Internet-Draft as of 2026-11); see [[CSRF-in-Cookie-Authenticated-APIs-SameSite-Double-Submit-and-Fetch-Metadata]].
`same_site="none"` without `https_only=True` produces `samesite=none` without `secure`, and a user agent following the draft's storage model ignores such a cookie entirely (draft-ietf-httpbis-rfc6265bis-22 Section 5.7, step 19).

Host and scheme boundary: `TrustedHostMiddleware` is a backstop for OWASP API8:2023 Security Misconfiguration ("discrepancies in the way incoming requests are processed by servers in the HTTP server chain"); the authoritative check belongs at the edge, which must also strip or overwrite `X-Forwarded-*` from clients.
HTTPS enforcement needs three layers: a redirect at the edge, HSTS on secure responses (RFC 6797 Section 7.1), and `Secure` cookies; the middleware provides only the first, in-process.

Authentication robustness: the backend runs for every request, including `OPTIONS` preflights and health checks, so it must be cheap and must not raise on missing credentials (return `None`); raise `AuthenticationError` only for malformed or invalid credentials.
Authorization belongs in `@requires` or route dependencies with deny-by-default, not in a middleware keyed on `request.url.path` (see [[Where-Authorization-Lives-Gateway-Middleware-vs-Domain-and-Query-Layer]]).
WebSocket rejections close with `1000`, so clients cannot distinguish "auth failed" from "done"; send a Denial Response or close with `1008` from your own code when the distinction matters.

## The principal's view

The decision is where session state and transport policy live, and the default answer for a multi-service API is: not in the application process.
Signed cookie sessions are the right call for a single small app with low-value, short-lived state (flash messages, CSRF tokens, OAuth `state` during a login round trip); they scale horizontally for free and need no store.
They are the wrong call once you need logout that works, admin-initiated revocation, concurrent-session limits, or audit; then you pay for a server-side store keyed by an opaque ID (RFC 6265 Section 8.4), or for a BFF that holds tokens server-side (see [[OAuth-2-0-for-Browser-Based-Applications-BFF-and-Cookies-RFC-10017]]).
The organizational trade-off is ownership: host allow lists and TLS redirects at the edge are owned by the platform team and apply uniformly, while in-app middlewares are owned by every service team and drift; keep both, but treat the edge as the control and the middleware as a test-enforced tripwire.
Treat Starlette minor upgrades as security-relevant: session semantics changed in `1.0.0` without an API change, and host parsing changed in `1.0.1`, `1.3.0`, `1.7.0`, and `1.9.0`; pin, read the release notes, and keep lab-style tests for each guarantee you depend on.
For a pure token API (mobile clients, service-to-service), skip cookie sessions entirely and use `AuthenticationMiddleware` with a bearer-token backend, or FastAPI dependencies, which give per-route control and OpenAPI security schemes.

## Interview questions

> [!question] L5: What does Starlette's `SessionMiddleware` store in the cookie, and can the user read it?
>
> > [!success]- Answer
> > The whole session, as `base64(json.dumps(session))`, followed by a base64url timestamp and a base64url HMAC-SHA1 signature, separated by dots.
> > Anyone can base64-decode and read the JSON; the signature only prevents modification without the key.
> > So it is integrity-protected, not confidential.
> >
> > Follow-up: What changes if you need confidentiality, and what does it cost?

> [!question] L5: Predict the output: `TrustedHostMiddleware(app, allowed_hosts=["example.com", "*.example.com"])` receives `Host: EXAMPLE.com:8443`, `Host: evilexample.com`, and `Host: example.com.` on Starlette 1.10.0.
>
> > [!success]- Answer
> > `EXAMPLE.com:8443` passes: the host is lowercased and the port is never compared.
> > `evilexample.com` gets `400 Invalid host header`, because the wildcard tests `endswith(".example.com")`.
> > `example.com.` gets `400`, because the trailing dot is not normalized.
> > Before 1.9.0 the first case would also have been `400`, because matching was case-sensitive.
> >
> > Follow-up: What happens to the second case if the pattern was mistyped as `*example.com` and the process runs with `-O`?

> [!question] L5: Why does `HTTPSRedirectMiddleware` use `307` rather than `301`, and what is the downside?
>
> > [!success]- Answer
> > `307` requires the client to keep the method and body (RFC 9110 Section 15.4.8), so a `POST` stays a `POST`.
> > It is temporary and not heuristically cacheable (RFC 9110 Section 15.1), so every plain-HTTP visit costs a round trip, and the request body has already crossed the network in cleartext.
> > RFC 6797 Section 7.2 suggests a permanent redirect for HSTS hosts, and HSTS itself removes the plain request after the first visit.
> >
> > Follow-up: Why can the middleware not add `Strict-Transport-Security` to the redirect?

> [!question] L6: Debug this API: after upgrading Starlette from 0.46 to 1.10, shopping carts stop saving, but the add-to-cart endpoint returns the updated cart; what happened?
>
> > [!success]- Answer
> > Starlette 1.0.0 replaced the plain `dict` with a `Session` that tracks `accessed` and `modified`, and writes `Set-Cookie` only when `modified` is set.
> > The endpoint does `request.session["cart"].append(item)`, which mutates the nested list without calling `Session.__setitem__`, so the response carries no `Set-Cookie`.
> > In 0.46 every response re-signed the entire non-empty session, which hid the bug.
> > Fix by reassigning the top-level key or calling `mark_modified()`, and add a test that asserts the cookie changes.
> >
> > Follow-up: What other user-visible behavior changed with the same release, and who will notice it first?

> [!question] L6: A user logs out, but their old session cookie, found in a proxy log, still works; is this a Starlette bug?
>
> > [!success]- Answer
> > No; it is inherent to client-side sessions.
> > `session.clear()` sends `session=null` with an expired date, which asks the browser to delete its copy; the server keeps no state to mark the old cookie invalid.
> > The old cookie has a valid HMAC and a timestamp within `max_age`, so `unsign` accepts it for up to 14 days by default.
> > Real revocation needs server-side state: an opaque session ID, or a per-user version number in the cookie checked against a store, or rotating the key to kill all sessions.
> >
> > Follow-up: How would you implement "log out all my other devices" with minimal storage?

> [!question] L6: Your app sits behind an AWS ALB that terminates TLS, and after adding `HTTPSRedirectMiddleware` every page loops; walk through the cause and the fix.
>
> > [!success]- Answer
> > The ALB forwards plain HTTP, so uvicorn sets `scope["scheme"]` to `http`, and the middleware redirects to `https`, which reaches the app as `http` again.
> > Uvicorn's `ProxyHeadersMiddleware` would rewrite the scheme from `X-Forwarded-Proto`, but only for client IPs in `forwarded_allow_ips`, which defaults to `127.0.0.1,::1`.
> > Fix by setting `--forwarded-allow-ips` to the ALB subnets, or remove the middleware and redirect at the ALB.
> > Never set it to `*` on a directly reachable server, because then any client can spoof `X-Forwarded-Proto` and `X-Forwarded-For`.
> >
> > Follow-up: Which other Starlette features depend on `scope["scheme"]` being right?

> [!question] L6: Why does an `HTTPException` raised inside an `AuthenticationBackend` produce `500`, while the same exception in an endpoint produces the intended status?
>
> > [!success]- Answer
> > `build_middleware_stack` places user middlewares between `ServerErrorMiddleware` and `ExceptionMiddleware`.
> > The backend runs in `AuthenticationMiddleware`, outside `ExceptionMiddleware`, so nothing maps the `HTTPException` and `ServerErrorMiddleware` returns `500`.
> > The supported path is `raise AuthenticationError`, which the middleware catches and converts via `on_error`.
> >
> > Follow-up: The default `on_error` returns `400` with `str(exc)`; what would you return instead and why?

> [!question] L7+: Design session handling for a product that starts as a single Starlette app and must later support "revoke all sessions", SSO, and 30 services.
>
> > [!success]- Answer
> > Start with an opaque random session ID in a `__Host-`, `Secure`, `HttpOnly`, `SameSite=Lax` cookie, backed by a store such as Redis with a TTL; the cookie can still be signed, but it carries no state.
> > Revocation, concurrent-session caps, and audit become store operations; sliding expiry is a TTL refresh.
> > For SSO and many services, move the session to a BFF or gateway that exchanges it for short-lived access tokens to downstream services, so services validate tokens, not cookies.
> > Keep `TrustedHostMiddleware` in each service as a tripwire and enforce host, TLS, and HSTS at the edge.
> > The cost is a hard dependency on the store on every request, so plan for its latency and availability.
> >
> > Follow-up: When would you still choose signed cookie sessions in that architecture?

> [!question] L7+: Security review: a team uses `TrustedHostMiddleware(allowed_hosts=["*"])`, builds password-reset links with `request.url_for`, and runs behind a CDN that forwards the client's `Host`; what is the attack and the systemic fix?
>
> > [!success]- Answer
> > Host header poisoning: the attacker requests a reset for the victim with `Host: attacker.example`, and the email contains a link to the attacker's host carrying the reset token.
> > `allowed_hosts=["*"]` sets `allow_any`, so the middleware checks nothing, and `url_for` builds the absolute URL from `Host`.
> > Systemic fixes: generate externally visible URLs from configuration, configure an explicit allow list, and have the CDN or edge enforce and normalize `Host`.
> > Add a regression test that sends a hostile `Host` and asserts `400`, as the lab does.
> >
> > Follow-up: How could the same `Host` value poison a shared cache, and which response headers limit the damage?

> [!question] L7+: How do you rotate the session signing key with zero forced logouts on Starlette 1.10.0?
>
> > [!success]- Answer
> > itsdangerous `Signer` accepts a key list, oldest first, verifies against each from newest to oldest, and always signs with the newest.
> > Starlette's constructor calls `str(secret_key)`, so you must replace `middleware.signer` with `TimestampSigner([old, new])` in a subclass, as the lab does, or pass through a custom middleware.
> > Deploy `[old, new]` everywhere first, wait at least `max_age` (or until old cookies have been rewritten), then deploy `[new]`.
> > Each extra key costs one more key derivation and HMAC for every request carrying an old or forged cookie, roughly 3 us per key on the lab machine (10.77 us for the same cookie against the oldest of three keys vs 4.88 us against one, CPU time).
> >
> > Follow-up: What breaks if some replicas sign with the new key before all replicas can verify it?

## Related

Prerequisites: [[Server-Sent-Events-SSE-Protocol-Last-Event-ID-and-SSE-Starlette]]

Next: [[In-Process-Testing-of-ASGI-Apps-with-HTTPX-ASGITransport]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]], [[Pure-ASGI-Middleware-Onion-Architecture-and-Send-Receive-Wrapping]], [[Starlette-Exception-Hierarchy-ServerErrorMiddleware-vs-ExceptionMiddleware]], [[Why-BaseHTTPMiddleware-Hurts-Performance-Streaming-and-ContextVars]], [[ASGI-WebSockets-Lifecycle-Subprotocols-and-Backpressure]], [[Forwarded-Header-RFC-7239-and-X-Forwarded-For-Spoofing]], [[Caching-Authenticated-and-Multi-Tenant-API-Responses]], [[CSRF-in-Cookie-Authenticated-APIs-SameSite-Double-Submit-and-Fetch-Metadata]], [[Security-Headers-for-HTTP-APIs-HSTS-Nosniff-Cache-Control-and-Security-Txt]], [[OAuth-2-0-for-Browser-Based-Applications-BFF-and-Cookies-RFC-10017]], [[Where-Authorization-Lives-Gateway-Middleware-vs-Domain-and-Query-Layer]], [[Uvicorn-Internals-Server-Lifecycle-Protocols-and-Flow-Control]]

- Lab: `labs/tests/f10_asgi_and_starlette_internals/test_starlette_authenticationmiddleware_sessionmiddleware_and_trustedhost.py`

## Further reading

- [RFC 6265](https://www.rfc-editor.org/rfc/rfc6265.html): cookie syntax (Section 4.1.1), `Cookie` ordering (Section 5.4), size floors (Section 6.1), session identifiers and weak integrity (Sections 8.4 and 8.6).
- [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html): `Host` and `:authority` (Section 7.2), heuristic cacheability (Section 15.1), `307` (Section 15.4.8).
- [RFC 9112](https://www.rfc-editor.org/rfc/rfc9112.html): the `400` rule for missing, duplicate, or invalid `Host` (Section 3.2).
- [RFC 6797](https://www.rfc-editor.org/rfc/rfc6797.html): HSTS, why the redirect is not enough (Sections 7.1, 7.2, 14.6).
- [RFC 6455](https://www.rfc-editor.org/rfc/rfc6455.html): WebSocket clients need not follow redirects (Section 4.1); close codes (Section 7.4.1).
- [RFC 2104](https://www.rfc-editor.org/rfc/rfc2104.html): HMAC and key-length guidance (Section 3).
- [Starlette 1.10.0 middleware source](https://github.com/Kludex/starlette/tree/1.10.0/starlette/middleware): the four middlewares are short enough to read in full.
- [Starlette release notes](https://github.com/Kludex/starlette/blob/1.10.0/docs/release-notes.md): every session and host behavior change since 0.6.
- [itsdangerous 2.2.0 source](https://github.com/pallets/itsdangerous/tree/2.2.0/src/itsdangerous): `Signer`, `TimestampSigner`, key derivation, and encoding.
- [GHSA-86qp-5c8j-p5mr](https://github.com/advisories/GHSA-86qp-5c8j-p5mr): Host header poisoning of `request.url.path` in Starlette `<= 1.0.0`.
- [GHSA-jp82-jpqv-5vv3](https://github.com/advisories/GHSA-jp82-jpqv-5vv3): path-carried authority poisoning of `request.url.hostname` before 1.3.0.
- [draft-ietf-httpbis-rfc6265bis-22](https://www.ietf.org/archive/id/draft-ietf-httpbis-rfc6265bis-22.txt): `__Host-` prefix (Section 4.1.3.2), SameSite Lax (Sections 4.1.2.7 and 5.6.7.1), and `SameSite=None` needing `Secure` (Section 5.7); still an Internet-Draft as of 2026-11.
- [CVE-2023-30861](https://nvd.nist.gov/vuln/detail/CVE-2023-30861): Flask's missing `Vary: Cookie` on session refresh, the canonical cookie-session cache leak.
