---
type: problem
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://www.rfc-editor.org/rfc/rfc9111.txt"
  - "https://www.rfc-editor.org/rfc/rfc9110.txt"
  - "https://www.rfc-editor.org/rfc/rfc9113.txt"
  - "https://www.rfc-editor.org/rfc/rfc9114.txt"
  - "https://www.rfc-editor.org/rfc/rfc7239.txt"
  - "https://www.rfc-editor.org/rfc/rfc9211.txt"
  - "https://portswigger.net/research/practical-web-cache-poisoning"
  - "https://portswigger.net/research/web-cache-entanglement"
  - "https://portswigger.net/research/gotta-cache-em-all"
  - "https://blackhat.com/docs/us-17/wednesday/us-17-Gil-Web-Cache-Deception-Attack-wp.pdf"
  - "https://www.usenix.org/conference/usenixsecurity20/presentation/mirheidari"
  - "https://cpdos.org/"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2021-23336"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2018-14773"
  - "https://raw.githubusercontent.com/python/cpython/v3.9.1/Lib/urllib/parse.py"
  - "https://raw.githubusercontent.com/python/cpython/v3.9.2/Lib/urllib/parse.py"
  - "https://docs.python.org/3/library/urllib.parse.html"
  - "https://developers.cloudflare.com/cache/cache-security/cache-deception-armor/"
  - "https://owasp.org/API-Security/editions/2023/en/0x11-t10/"
  - "https://arxiv.org/abs/1912.10190"
  - "https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-limits.html"
---

# Web Cache Poisoning via Unkeyed Headers and Web Cache Deception on API Endpoints

> [!summary] Claim
> A shared cache reuses a response for every request with the same cache key, so any request input outside the key that changes the response (an unkeyed header, a cloaked query parameter, a tunnelled method) lets one client choose what everyone else receives, and any cache rule that stores responses by URL shape instead of by the origin's `Cache-Control` lets one client's private response be read by anyone; both are fixed by making the key and the response agree, not by filtering payloads.

Part of [HTTP Caching and Conditional Requests](README.md) in [API Engineering](../README.md).

## TL;DR

- Mechanism: the cache key is the method and target URI plus whatever `Vary` nominates (RFC 9111 Sections 2 and 4.1); poisoning stores a response shaped by an input outside that key, and deception stores a private response under a URL the attacker can request.
- Contract rule: a shared cache must not store `no-store` or `private` responses, and must not reuse a response to a request with `Authorization` unless `public`, `s-maxage`, or `must-revalidate` allows it (RFC 9111 Sections 3 and 3.5); nothing in RFC 9111 protects cookie-authenticated responses.
- The trap: frameworks and servers read inputs you never wrote code for; Hypercorn `0.18.0` `ProxyFixMiddleware` in its default `legacy` mode copies the last `X-Forwarded-Host` value into `Host`, so Starlette's `url_for` builds links on an attacker-chosen authority, proven in the lab.
- Portability fact: on CPython `>= 3.10` (and `3.9.2`, `3.8.8`, `3.7.10`, `3.6.13`) `urllib.parse.parse_qsl` splits only on `&`, which closes the CVE-2021-23336 parameter-cloaking gap for Starlette; all 58 lab tests pass on CPython `3.12.16`, `3.13.18`, `3.14.10`, and `3.15.2` on Debian Trixie and Alpine (musl), and on `3.12.13`, `3.13.18`, `3.14.10`, `3.15.2`, and free-threaded `3.15.0b3` on macOS.
- When not to rely on it: do not "fix" poisoning with `Vary` on a header whose values the attacker controls freely if the CDN ignores or normalizes `Vary`, and do not cache authenticated API responses at a shared edge at all unless the key provably contains the principal (see [[Caching-Authenticated-and-Multi-Tenant-API-Responses]]).

## Mental model

```mermaid
sequenceDiagram
    participant A as Attacker
    participant E as Shared cache<br/>key = GET + Host + path + query
    participant O as Origin<br/>(reads X-Forwarded-Host)
    participant V as Victim
    A->>E: GET /api/products/1<br/>X-Forwarded-Host: evil.example
    Note over E: key miss (uri-miss)
    E->>O: forwards X-Forwarded-Host unchanged
    O-->>E: 200, Cache-Control: public, max-age=300<br/>"self": "https://evil.example/api/products/1"
    Note over E: storable (RFC 9111 Section 3),<br/>no Vary, so the header is not in the key
    E-->>A: 200, Cache-Status: edge; fwd=uri-miss; stored
    V->>E: GET /api/products/1 (no extra header)
    Note over E: same key, fresh: hit
    E-->>V: 200, Age: 120, Cache-Status: edge; hit<br/>"self": "https://evil.example/api/products/1"
```

The attacker sends the request a victim would send, plus one header the cache does not put in its key.
The cache misses, forwards the request with the extra header intact, and the origin uses that header to build the `self` link in its hypermedia response.
The response is explicitly cacheable and has no `Vary`, so the cache stores it under the key it computed, which does not mention the header.
For the next 300 seconds every client whose request maps to the same key, which is every ordinary client, receives the attacker's link from the cache without the origin ever seeing the request.
Web cache deception runs the same diagram in reverse: the victim sends the request (with their cookie) to a URL the attacker chose, and the attacker later reads the stored copy.

## How it works

### What is in the key, and what is not

RFC 9111 Section 2 defines the cache key as "composed from, at a minimum, the request method and target URI", and notes that many caches "only cache GET responses and therefore only use the URI as the cache key".
The target URI includes the authority, so `Host` (or `:authority` in HTTP/2 and HTTP/3, RFC 9110 Section 7.2) is in the key, while every other request header field is outside it unless the response nominates it with `Vary` (RFC 9111 Section 4.1; RFC 9110 Section 12.5.5).
A stored response may be reused only if the presented target URI matches, the method allows it, the `Vary`-nominated fields match, and it is fresh, allowed stale, or validated (RFC 9111 Section 4).
RFC 9111 Section 7.1 names the result: "Storing malicious content in a cache can extend the reach of an attacker to affect multiple users."
James Kettle's 2018 research turned this into a method: find an unkeyed input with a header-guessing tool, find a gadget that reflects it, and get the response stored ("Practical Web Cache Poisoning", PortSwigger Research, 2018-08-09).

The lab's edge computes exactly that primary key, with optional extra keyed headers and unkeyed query parameters:

```python
    def primary_key(self, scope: Scope) -> tuple[str, ...]:
        headers: HeaderList = scope["headers"]
        host = next(iter(header_values(headers, b"host")), "").lower()
        raw_path: bytes = scope.get("raw_path") or scope["path"].encode()
        query = keyed_query(scope["query_string"], self.policy.unkeyed_query_params)
        extra = tuple(", ".join(header_values(headers, n)) for n in self.policy.keyed_headers)
        return (scope["method"], host, raw_path.decode("latin-1"), query, *extra)
```

### When a shared cache may store

RFC 9111 Section 3 says a cache "MUST NOT store a response" unless, among other conditions, `no-store` is absent, a shared cache sees no unqualified `private`, the request had no `Authorization` field or the response explicitly allows shared caching, and the response carries explicit freshness or a heuristically cacheable status.
RFC 9111 Section 3.5 lists the three directives that unlock `Authorization`: `must-revalidate`, `public`, and `s-maxage`.
RFC 9110 Section 15.1 lists the heuristically cacheable status codes: `200`, `203`, `204`, `206`, `300`, `301`, `308`, `404`, `405`, `410`, `414`, and `501`; a cache may assign them heuristic freshness when no explicit expiration is present (RFC 9111 Section 4.2.2).
RFC 9111 Section 7.3 adds the line that makes cookie sessions dangerous: `Set-Cookie` "does not inhibit caching", and servers "are encouraged to emit appropriate Cache-Control response header fields".
Directive arguments may use the token or the quoted-string form, recipients "ought to accept both forms" (RFC 9111 Section 5.2), and caches "are encouraged to consider responses that have invalid freshness information (e.g., a max-age directive with non-integer content) to be stale" (RFC 9111 Section 4.2.1); the lab pins both with `max-age="60"` and `max-age=abc`.
The lab encodes these rules, plus the CDN extension rule that can override them:

```python
def decide_storage(
    policy: EdgePolicy,
    *,
    method: str,
    raw_path: str,
    request_headers: Sequence[tuple[bytes, bytes]],
    status: int,
    response_headers: Sequence[tuple[bytes, bytes]],
) -> StoreDecision:
    """RFC 9111 Section 3 for a shared cache, plus the CDN extension rule that can override it."""
    if method != "GET":
        return StoreDecision(None, "method")
    extension = static_extension(raw_path, policy.static_extensions)
    if extension is not None and policy.extension_rule_overrides_origin:
        content_type = next(iter(header_values(response_headers, b"content-type")), None)
        if policy.verify_content_type and not content_type_matches(extension, content_type):
            return StoreDecision(None, "content-type mismatch")
        return StoreDecision(policy.static_ttl, f"extension rule {extension}")

    cc = parse_cache_control(header_values(response_headers, b"cache-control"))
    for directive in ("no-store", "private", "no-cache"):
        if directive in cc:
            return StoreDecision(None, directive)
    explicitly_shared = {"public", "s-maxage", "must-revalidate"} & cc.keys()
    if header_values(request_headers, b"authorization") and not explicitly_shared:
        return StoreDecision(None, "authorization")  # RFC 9111 Section 3.5
    for directive in ("s-maxage", "max-age"):  # s-maxage overrides max-age in a shared cache
        argument = cc.get(directive)
        if argument is not None and argument.isdigit():
            return StoreDecision(float(argument), directive)
    if extension is not None:
        return StoreDecision(policy.static_ttl, f"extension rule {extension}")
    if (status in HEURISTICALLY_CACHEABLE or "public" in cc) and policy.heuristic_ttl > 0:
        return StoreDecision(policy.heuristic_ttl, "heuristic")
    return StoreDecision(None, "no explicit freshness")
```

The first branch is the configuration Omer Gil called out in 2017: a cache set "to cache files by their extensions, disregarding any caching headers" (Gil, "Web Cache Deception Attack", Black Hat USA 2017 white paper, Conditions).
RFC 9111 Section 3 lets only a cache extension directive sent in the response (Section 5.2.3) override its storage requirements, so an "Edge Cache TTL" style rule that stores a `private` or `no-store` response is local configuration that breaks the Section 3 "MUST NOT", and no origin header can stop it.

### The poisoning gadget in an API: hypermedia links

APIs that emit absolute links (`self`, `next`, `Location`, OpenAPI `servers`) need an authority, and a reverse proxy that rewrites `Host` tempts the application to read `X-Forwarded-Host` or the `host=` parameter of `Forwarded` (RFC 7239 Section 5.3).
RFC 7239 Section 8.1 is blunt: the field "cannot be relied upon to be correct, as it may be modified ... by every node on the way to the server, including the client making the request."
Kettle's Mozilla case is the API version of the attack: `normandy.cdn.mozilla.net` returned JSON whose `action-list`, `recipe-list`, and similar links were built from `X-Forwarded-Host`, nginx cached the response, and Firefox clients would have fetched recipes from the attacker's host; Mozilla patched it "in under 24 hours" (Kettle 2018, "Hijacking Mozilla SHIELD").
The lab origin chooses the authority in one function:

```python
def product_self_link(request: Request, config: OriginConfig, product_id: int) -> str:
    """The hypermedia gadget: where the `self` link's authority comes from."""
    if config.link_base == "request-host":
        return str(request.url_for("product", product_id=product_id))  # reflects `Host`
    if config.link_base == "forwarded-host":
        forwarded = request.headers.get("x-forwarded-host")
        allowed = config.allowed_forwarded_hosts
        if forwarded and (allowed is None or forwarded.lower() in allowed):
            return f"https://{forwarded}/api/products/{product_id}"
    return f"{config.canonical_base}/api/products/{product_id}"
```

Starlette builds `request.url_for` from `scope["headers"]`: `URL.__init__` takes the first `host` field, validates it with `parse_host_header`, and falls back to `scope["server"]` when the value is not a valid host `(starlette 1.10.0 starlette/datastructures.py, URL.__init__)` `(starlette 1.10.0 starlette/_utils.py, parse_host_header)`.
So a forged `Host` such as `evil.example` is reflected, but `a"><script>` is not; the lab proves both.
Reflecting `Host` is not by itself poisoning, because `Host` is in the key; the lab shows a forged `Host` lands in its own cache entry and the victim's entry stays clean.
The danger is a layer that silently moves an unkeyed value into `Host`.
Hypercorn's `ProxyFixMiddleware` defaults to `mode="legacy"` and `trusted_hops=1`; in legacy mode `_get_trusted_value` splits every `x-forwarded-host` line on `,` and returns the value `trusted_hops` from the end, and the middleware then drops every `host` field and appends that value as the new `host` `(hypercorn 0.18.0 hypercorn/middleware/proxy_fix.py, ProxyFixMiddleware.__call__)` `(hypercorn 0.18.0 hypercorn/middleware/proxy_fix.py, _get_trusted_value)`.
Trust is counted in hops, not checked against the peer address, so if the edge does not overwrite or append `X-Forwarded-Host`, the client's value is the last one.
In `mode="modern"` the middleware reads `host=` from `Forwarded` only when a `Forwarded` field is present; without one it falls through to the same `X-Forwarded-*` branch as legacy mode, which the lab also proves.
Uvicorn's `ProxyHeadersMiddleware` reads only `x-forwarded-proto` and `x-forwarded-for`, and only from peers in `trusted_hosts` `(uvicorn 0.54.0 uvicorn/middleware/proxy_headers.py, ProxyHeadersMiddleware.__call__)`; Granian's `wrap_asgi_with_proxy_headers` does the same and applies `X-Forwarded-Proto` only when `X-Forwarded-For` is also present `(granian 2.8.7 granian/utils/proxies.py, wrap_asgi_with_proxy_headers)`.
Neither touches `Host`, which the lab proves for both.

### Unkeyed inputs that are not headers

- Query parameters: caches often drop analytics parameters such as `utm_content` from the key; if the origin splits the query on a character the cache does not, a keyed parameter can hide inside the unkeyed one.
  Kettle found Ruby on Rails treats `;` like `&`, so `GET /jsonp?callback=legit&utm_content=x;callback=alert(1)//` was stored under the key of `?callback=legit` ("Web Cache Entanglement", PortSwigger Research, 2020-08-05, "Cache Parameter Cloaking").
  CPython had the same rule: `parse_qsl` in `v3.9.1` builds `pairs = [s2 for s1 in qs.split('&') for s2 in s1.split(';')]`, and `v3.9.2` adds `separator='&'` and splits only on it (CPython `v3.9.1` and `v3.9.2` `Lib/urllib/parse.py`, `parse_qsl`).
  NVD records this as CVE-2021-23336, "Web Cache Poisoning via urllib.parse.parse_qsl and urllib.parse.parse_qs by using a vector called parameter cloaking", affecting CPython before `3.6.13`, `3.7.10`, `3.8.8`, and `3.9.2`.
  Starlette's `QueryParams` calls `parse_qsl(..., keep_blank_values=True)` and `ImmutableMultiDict` keeps the last value per key `(starlette 1.10.0 starlette/datastructures.py, QueryParams.__init__)` `(starlette 1.10.0 starlette/datastructures.py, ImmutableMultiDict.__init__)`, so on a fixed CPython the cloaked value stays inside `utm_content`.
- Methods: an origin that honours `X-HTTP-Method-Override` turns a `GET` the cache keys as `GET` into a `DELETE` or `POST` the router rejects; Starlette answers `405` with an `Allow` field and no `Cache-Control` `(starlette 1.10.0 starlette/routing.py, Route.handle)`, and `405` is heuristically cacheable (RFC 9110 Section 15.1), so even an RFC-compliant cache that uses heuristic freshness stores the error.
  Nguyen, Lo Iacono, and Federrath named this HTTP Method Override (HMO) in "Your Cache Has Fallen: Cache-Poisoned Denial-of-Service Attack" (ACM CCS 2019).
- Header size: their HTTP Header Oversize (HHO) variant sends a header section the cache accepts but the origin rejects, and the origin's `400` gets stored by a cache that stores errors; RFC 9110 Section 15.1 does not list `400`, so a compliant cache never stores it without explicit freshness or a `public` directive (RFC 9111 Section 4.2.2 allows heuristics on `public` responses), and the lab pins both cases.

### Deception: path confusion between cache and origin

Gil's three conditions are the whole attack: the origin returns the content of `home.php` for `home.php/nonexistent.css`, the cache stores by extension "disregarding any caching headers", and the victim is authenticated when they follow the link (Gil 2017, Conditions).
Mirheidari et al. measured it at scale three years later, studying 340 high-profile sites from the Alexa Top 5K and finding leaks of "private user data as well as secret authentication and authorization tokens" ("Cached and Confused: Web Cache Deception in the Wild", USENIX Security 2020, abstract).
Martin Doyhenard's 2024 work generalizes the origin side to delimiter and normalization discrepancies: `;` for Spring matrix variables, `.` for Rails format suffixes, `%00` for OpenLiteSpeed, and an encoded newline in some nginx rewrite rules ("Gotta cache 'em all", PortSwigger Research, 2024-08-08, "Delimiters").
Starlette's default router is strict: `compile_path` turns `/api/me` into `^/api/me$` and `Route.matches` uses `path_regex.match(route_path)` `(starlette 1.10.0 starlette/routing.py, compile_path)` `(starlette 1.10.0 starlette/routing.py, Route.matches)`, so `/api/me/avatar.css` is a `404` unless the application adds a catch-all such as `/api/me/{rest:path}`.
One quirk survives: Python's `$` also matches before a final `\n`, so `GET /api/me%0A` reaches the `/api/me` handler (scope path `/api/me\n`) while `/api/me%0Aa.css` does not; the lab pins this, and it changes the cache key without changing the handler, which is a cache-key confusion primitive rather than a deception primitive by itself.

## Numbers and limits

| Quantity | Value | Source or measurement |
|---|---|---|
| Heuristically cacheable status codes | 12: `200`, `203`, `204`, `206`, `300`, `301`, `308`, `404`, `405`, `410`, `414`, `501` | RFC 9110 Section 15.1 |
| Directives that let a shared cache store a response to an `Authorization` request | 3: `public`, `s-maxage`, `must-revalidate` | RFC 9111 Section 3.5 |
| Hypercorn `ProxyFixMiddleware` defaults | `mode="legacy"`, `trusted_hops=1` (last `X-Forwarded-Host` value wins) | `(hypercorn 0.18.0 hypercorn/middleware/proxy_fix.py, ProxyFixMiddleware.__init__)`; proven in the lab |
| h11 incomplete-event buffer limit | `16 * 1024` bytes; a buffer longer than that raises `RemoteProtocolError` with `error_status_hint=431` | `(h11 0.16.0 h11/_connection.py, DEFAULT_MAX_INCOMPLETE_EVENT_SIZE)` `(h11 0.16.0 h11/_connection.py, Connection.next_event)` |
| Uvicorn status for that h11 error | `400 Bad Request`, `Connection: close`, no `Cache-Control` | `(uvicorn 0.54.0 uvicorn/protocols/http/h11_impl.py, H11Protocol.send_400_response)`; measured over loopback in the lab |
| Complete header sections uvicorn `h11` accepted in a probe | one header field of 1,000 to 17,000 bytes -> `200` every time; 50,000 to 200,000 bytes -> `200` or connection reset, varying between attempts; 300,000 and 1,000,000 bytes -> reset every time | 5 attempts per size in each of 2 probe runs on loopback, CPython `3.14.6`, macOS 27.2 arm64; depends on read chunking, so not pinned as a test |
| Header size limits cited for CPDoS | Apache HTTPD around 8,192 bytes, CloudFront 20,480 bytes | cpdos.org (measured February 2019) |
| CloudFront request length limit today | 32,768 bytes including headers and query strings, excluding the body (as of 2026-11) | AWS CloudFront Developer Guide, "Quotas", General quotas |
| Deception prevalence study | 340 high-profile sites among the Alexa Top 5K | Mirheidari et al., USENIX Security 2020 (abstract; also arXiv:1912.10190) |
| Mozilla Normandy fix time and bounty | under 24 hours, `$1,000` | Kettle 2018 |
| CPython releases that fixed `;` splitting | `3.6.13`, `3.7.10`, `3.8.8`, `3.9.2`; `separator` documented as "Changed in version 3.10" | NVD CVE-2021-23336; Python `urllib.parse` documentation (as of 2026-11) |
| Lab suite | 58 tests, `58 passed in 0.49s`, 97% branch coverage of `cache_poisoning.py` (250 statements, 88 branches) | `uv run --frozen pytest tests/f03_http_caching_and_conditional_requests/test_web_cache_poisoning_and_cache_deception_attacks.py -q`, coverage with `--cov=api_labs.f03_http_caching_and_conditional_requests.cache_poisoning --cov-branch`, CPython `3.14.6`, macOS 27.2 arm64 |

## Portability

| Dimension | Version | Result |
|---|---|---|
| CPython | `3.12.16` | `58 passed` on Debian GNU/Linux 13 (trixie) and on Alpine Linux v3.24 (musl), `docker run` of `python:3.12-trixie` and `python:3.12-alpine`, aarch64 under OrbStack, 24 packages pinned to their `uv.lock` versions installed with `pip`; `3.12.13` on macOS 27.2 arm64 also gives `58 passed` (`UV_PROJECT_ENVIRONMENT=../tmp/venv-3.12 uv run --frozen --python 3.12 pytest ...`) |
| CPython | `3.13.18` | `58 passed` on `python:3.13-trixie` and `python:3.13-alpine`, and on macOS (`--python /opt/homebrew/bin/python3.13`, isolated environment) |
| CPython | `3.14.10` | `58 passed` on `python:3.14-trixie` and `python:3.14-alpine`, and on macOS (`--python /opt/homebrew/bin/python3.14`, isolated environment); the workspace `.venv` (`3.14.6`) also passes |
| CPython | `3.15.2` | `58 passed` on `python:3.15-trixie` and `python:3.15-alpine`, and on macOS (`--python /opt/homebrew/bin/python3.15`, isolated environment) |
| CPython free-threaded | `3.15.0b3+freethreaded`, `sys._is_gil_enabled()` is `False`; the pinned `3.15.2t` build is not installable here (uv offers only `3.15.0b3t` and the official Docker images have no free-threaded 3.15) | `58 passed` via `PYTHONPATH=src uv run --no-project --python 3.15t --with-requirements <the 24 locked pins the test imports> pytest ...`, because `uv.lock` has no `cp315t` wheel for `psycopg-binary 3.3.8` and `aioquic 1.7.2` fails to build from source there |
| `urllib.parse.parse_qsl` | every CPython in the matrix | splits on `&` only (`parse_qsl("a=1;b=2") == [("a", "1;b=2")]`), proven on every interpreter and container above |
| Starlette | `1.10.0` | `url_for` reflects a valid forged `Host`, ignores an invalid one, `TrustedHostMiddleware` answers `400 Invalid host header`, routes match a trailing `%0A`; all proven in the lab |
| Hypercorn | `0.18.0` | `ProxyFixMiddleware` legacy mode rewrites `Host` from `X-Forwarded-Host`, proven in-process; `modern` mode reads `host=` from `Forwarded` when that field is present and otherwise falls back to `X-Forwarded-Host`, both proven in-process |
| Uvicorn | `0.54.0` | `ProxyHeadersMiddleware` never rewrites `Host`, proven in-process; `h11` protocol answers an unfinished header section over 16 KiB with `400`, proven over a `127.0.0.1:0` socket; the `httptools` protocol was not tested |
| Granian | `2.8.7` | `wrap_asgi_with_proxy_headers` never rewrites `Host`, proven in-process; the Rust server itself was not run |
| HTTP/2 | RFC 9113 | the keyed authority is `:authority`: the recipient "MUST NOT use the Host header field to determine the target URI if ':authority' is present", and an intermediary that builds an HTTP/1.1 request "MUST use the value from the ':authority' pseudo-header field as the value of the Host field" (RFC 9113 Section 8.3.1; RFC 9110 Section 7.2); the lab keys on `Host` only and runs no h2 connection |
| HTTP/3 over QUIC | RFC 9114 | a request with an `http` or `https` scheme "MUST contain either an :authority pseudo-header field or a Host header field", and if both are present "they MUST contain the same value" (RFC 9114 Section 4.3.1); the lab runs no QUIC connection |
| Containers | Debian GNU/Linux 13 (trixie), Alpine Linux v3.24 (musl) | `58 passed` on all eight images, one per CPython `3.12.16`, `3.13.18`, `3.14.10`, `3.15.2` and libc (`docker run --rm python:<version>-trixie` or `-alpine`, aarch64 under OrbStack on macOS 27.2) |

## Worked example

The [implementation](../labs/src/api_labs/f03_http_caching_and_conditional_requests/cache_poisoning.py) holds `EdgeCache`, an RFC 9111 shared cache written as an ASGI app that reports its decisions in `Cache-Status`, and `build_origin`, a Starlette API whose switches turn each vulnerable behavior on or off.
The [test](../labs/tests/f03_http_caching_and_conditional_requests/test_web_cache_poisoning_and_cache_deception_attacks.py) runs every attack and every defense through `httpx.ASGITransport`, plus one socket test against a real Uvicorn server.

Poisoning through a real library layer, verbatim from the test:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_hypercorn_proxy_fix_makes_url_for_reflect_x_forwarded_host() -> None:
    origin = build_origin(OriginConfig(link_base="request-host"))
    edge = EdgeCache(ProxyFixMiddleware(origin, mode="legacy", trusted_hops=1))  # type: ignore[arg-type]
    async with client_for(edge) as client:
        attack = await client.get(PRODUCT, headers={"x-forwarded-host": "evil.example"})
        victim = await client.get(PRODUCT)

    assert self_link(attack) == "http://evil.example/api/products/1"
    assert victim.headers["cache-status"] == "edge; hit"
    assert self_link(victim) == "http://evil.example/api/products/1"
```

Deception when the CDN's extension rule overrides `private, no-store`, verbatim:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_extension_rule_overriding_origin_caches_a_private_profile() -> None:
    policy = EdgePolicy(
        static_extensions=CSS_RULE.static_extensions, extension_rule_overrides_origin=True
    )
    edge = EdgeCache(build_origin(OriginConfig(lenient_suffix_route=True)), policy)
    async with client_for(edge) as client:
        victim = await client.get("/api/me/avatar.css", headers=ALICE_COOKIE)
        attacker = await client.get("/api/me/avatar.css")

    assert victim.headers["cache-control"] == "private, no-store"
    assert victim.headers["cache-status"] == "edge; fwd=uri-miss; stored"
    assert attacker.headers["cache-status"] == "edge; hit"
    assert attacker.json()["api_key"] == "ak_test_alice"
```

The same misconfigured `max-age=60` on `/api/me` is safe for bearer tokens and unsafe for cookies, verbatim:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_rfc9111_protects_authorization_but_not_cookie_authentication() -> None:
    edge = EdgeCache(build_origin(OriginConfig(me_cache_control="max-age=60")))
    async with client_for(edge) as client:
        bearer = await client.get("/api/me", headers={"authorization": "Bearer t-alice"})
        after_bearer = await client.get("/api/me")
        cookie = await client.get("/api/me", headers=ALICE_COOKIE)
        after_cookie = await client.get("/api/me")

    assert bearer.headers["cache-status"] == "edge; fwd=uri-miss"
    assert after_bearer.status_code == 401
    assert cookie.headers["cache-status"] == "edge; fwd=uri-miss; stored"
    assert after_cookie.headers["cache-status"] == "edge; hit"
    assert after_cookie.json()["user"] == "alice"
```

Captured on CPython `3.14.6` (the workspace interpreter), macOS 27.2 arm64, from `labs/`:

```text
$ uv run --frozen pytest tests/f03_http_caching_and_conditional_requests/test_web_cache_poisoning_and_cache_deception_attacks.py -q
..........................................................               [100%]
58 passed in 0.49s
```

Each test was checked by mutation: 25 deliberate breaks in the first pass (22 to `cache_poisoning.py`, such as keying the unkeyed parameter, ignoring `Vary`, skipping the content-type check, dropping the `Authorization` rule, adding `400` to the heuristic list, swapping the `s-maxage` precedence, and removing `Host` from the key, plus 3 to test inputs) each made at least one test fail, and four survived at first (matching `Vary: *`, storing `no-cache`, ignoring `public` for heuristics, and letting the last duplicate directive win) until the decision table and `test_vary_star_never_matches_a_stored_response` were added.
A second pass of 12 breaks run with `uv run --frozen pytest <test> -q -x` killed 5 at once (the edge keeping the client's `X-Forwarded-Host` while "overwriting", a case-sensitive allowlist, a stale entry reported as `uri-miss`, unstripped `Vary` selecting values, and an undecoded parameter name in `keyed_query`).
It found 6 survivors that are now killed: `Host` not lower-cased in the key (`test_host_is_compared_case_insensitively_in_the_cache_key`), stored variants never replaced (`test_refreshing_a_variant_replaces_it_instead_of_accumulating_entries`), a quoted `max-age` not accepted and a non-numeric `max-age` accepted (two new decision-table cases), a bare `.css` dotfile treated as an extension (`test_static_extension_reads_only_the_last_path_segment`), and `Vary` names not compared case-insensitively (`test_vary_field_names_match_case_insensitively`).
The twelfth, widening the method-override set to include `GET`, changes nothing observable because overriding `GET` with `GET` is a no-op, so it is an equivalent mutant and no test was added.
Every file was restored with an identical SHA-256 afterwards.

## Pitfalls

> [!warning] Absolute links built from `X-Forwarded-Host`
> Trigger: a JSON API builds `self`, `next`, or discovery links from `X-Forwarded-Host`, behind a cache that does not key or overwrite that header.
> Symptom: every client receives links to an attacker's host until the entry expires; Kettle's Mozilla Normandy report showed the recipe endpoints of Firefox's SHIELD system pointing at his server through nginx (Kettle 2018).
> Fix: build links from a configured canonical origin, or accept `X-Forwarded-Host` only from an allowlist (lab test `test_forwarded_host_allowlist_falls_back_to_the_canonical_origin`), and make the edge the only writer of the header (`test_edge_that_overwrites_x_forwarded_host_removes_the_input`).

> [!warning] A proxy-fix middleware you did not audit
> Trigger: Hypercorn `ProxyFixMiddleware` with its defaults in front of Starlette or FastAPI, while the CDN passes the client's `X-Forwarded-Host` through.
> Symptom: `request.url_for` and redirects use the attacker's host, and the cache stores them (lab test `test_hypercorn_proxy_fix_makes_url_for_reflect_x_forwarded_host`).
> Fix: have the edge overwrite `X-Forwarded-Host`, or use `mode="modern"` with an edge that always writes `Forwarded` and strips the client's `X-Forwarded-*`, because modern mode falls back to `X-Forwarded-Host` when no `Forwarded` field arrives (lab test `test_hypercorn_proxy_fix_modern_mode_falls_back_to_x_forwarded_host`); set `trusted_hops` to the exact number of proxies you operate.

> [!warning] Path-override headers in the framework
> Trigger: a framework that honours `X-Original-URL` or `X-Rewrite-URL`, as Symfony HttpFoundation did before the fix for CVE-2018-14773 (NVD), used by Drupal.
> Symptom: the cache keys the request line while the application serves a different path; Kettle chained it into persistent poisoning of Drupal sites (Kettle 2018).
> Fix: upgrade, and strip such headers at the edge; in general, strip every request header the application does not need before it reaches the origin.

> [!warning] Parameter cloaking through `;`
> Trigger: the cache drops `utm_content` from the key and the origin splits queries on `;`, as Rails did and as CPython did before CVE-2021-23336 was fixed.
> Symptom: `?currency=USD&utm_content=x;currency=EUR` is stored under the key of `?currency=USD` and every victim sees `EUR` prices (lab test `test_semicolon_splitting_origin_lets_a_cloaked_parameter_poison_the_entry`).
> Fix: run a fixed CPython and never pass `separator=";"`; better, rewrite the request at the edge rather than rewriting the cache key, which is Kettle's 2020 recommendation.

> [!warning] Error responses stored by the edge (CPDoS)
> Trigger: the origin honours `X-HTTP-Method-Override` and the cache applies heuristic freshness, or the cache stores `400` responses at all.
> Symptom: one request replaces a resource with `405` or `400` for every client (lab test `test_method_override_405_is_heuristically_cached_and_served_to_get_clients`); Nguyen et al. reported affected cache and server pairs including CloudFront in 2019 (cpdos.org).
> Fix: send `Cache-Control: no-store` on errors, never honour method override on `GET` or `HEAD`, disable heuristic caching of errors at the CDN, and answer oversized header sections with `431` rather than `400`.

> [!warning] Extension rules that overrule `Cache-Control`
> Trigger: a CDN rule caches `*.css`, `*.js`, or images with a fixed TTL "disregarding any caching headers", and the origin answers `/api/me/x.css` with `/api/me` (Gil 2017; PayPal's home page is the white paper's referenced demonstration).
> Symptom: the attacker reads the victim's profile, tokens, or CSRF values from the cache (lab test `test_extension_rule_overriding_origin_caches_a_private_profile`).
> Fix: let `Cache-Control` win over cache rules, restrict static caching to a static path prefix, enable a content-type check such as Cloudflare's Cache Deception Armor, and return `404` for unknown suffixes.

## Security and robustness

- Threat model: the attacker is an unauthenticated client of the same cache; for poisoning they need an unkeyed input and a gadget, for deception they need the victim to load one URL while logged in (a link, an image tag, or a redirect).
- OWASP `API8:2023 Security Misconfiguration` covers both: a cache key that omits a response-affecting input and a CDN rule that overrides origin directives are configuration, not code, defects.
- OWASP `API4:2023 Unrestricted Resource Consumption` and availability: CPDoS needs one request per cache key per freshness lifetime, so its cost to the attacker is tiny compared with the outage.
- Input validation edge cases: hostnames should match the `Host` ABNF (RFC 7239 Section 5.3 requires that of `host=`), as Starlette's `parse_host_header` enforces; values with `"`, `<`, or a newline must never reach a link builder.
- Boundary enforcement: one component, the edge, owns `X-Forwarded-*` and `Forwarded`; it deletes client copies and writes its own, and the origin trusts those fields only from the edge's address or only the last hop (see [[Forwarded-Header-RFC-7239-and-X-Forwarded-For-Spoofing]]).
- Defense in depth for authenticated responses: send `Cache-Control: private, no-store` on every response that depends on the caller, which RFC 9111 Section 3 makes unstorable for shared caches, and do not count on `Authorization` alone, because cookie sessions get no protection from RFC 9111 Section 3.5.
- Observability: emit `Cache-Status` (RFC 9211 Section 2) with `fwd` reasons and `stored`, so a sudden `hit` on a path that should be `fwd=bypass` is visible; RFC 9211 Section 6 warns this field can reveal cache internals, so restrict it to trusted clients if needed.
- Parsing differentials are the root cause of a whole family here and of request smuggling; RFC 9111 Section 7.1 names "differences in message parsing on proxies and in user agents" as a poisoning vector (see [[HTTP-Request-Smuggling-CL-TE-and-Desync-Mechanics]]).

## The principal's view

The decision is who owns the cache key, and the answer is that the API team owns it as part of the API contract, even when another team runs the CDN.
Every response-affecting input is either in the key, made irrelevant by the edge (stripped or overwritten), or declared with `Vary`; a review that cannot list the inputs for an endpoint cannot approve caching it.
That rule has an organizational cost: CDN configuration moves into the same review and deploy path as application code, and a product team loses the ability to add a "cache all `.js` for a day" rule from a dashboard.
For authenticated APIs the cheaper decision is usually not to cache at the shared edge at all, to send `private, no-store`, and to spend the effort on origin-side caching keyed by principal, because a deception bug leaks data while a missed cache hit only costs latency.
For public read APIs, prefer canonical absolute links from configuration, or relative links, so the origin never needs `X-Forwarded-Host`; choose `Vary` only for inputs with a small, closed value set, because a CDN that ignores `Vary` silently reopens the hole, and Kettle reported in 2018 that Cloudflare ignored it outright.
If the product needs per-user edge personalization, move it to edge compute that composes a cached public document with a private fragment fetched with credentials, rather than stretching one cache key over both.

## Interview questions

> [!question] L5: What is in an HTTP cache key by default, and why is `X-Forwarded-Host` dangerous while `Host` usually is not?
>
> > [!success]- Answer
> > RFC 9111 Section 2 makes the method and target URI the minimum key, and the target URI includes the authority from `Host` or `:authority`.
> > Other request fields are outside the key unless `Vary` nominates them (RFC 9111 Section 4.1).
> > So a response shaped by `X-Forwarded-Host` is stored under a key that does not mention it and served to everyone, while a forged `Host` gets its own entry.
> >
> > Follow-up: Which server-side component could turn `X-Forwarded-Host` into `Host` without any application code?

> [!question] L5: Why does RFC 9111 stop a shared cache from reusing a response to a bearer-token request but not to a cookie request?
>
> > [!success]- Answer
> > RFC 9111 Sections 3 and 3.5 special-case the `Authorization` field: reuse requires `public`, `s-maxage`, or `must-revalidate`.
> > There is no equivalent rule for `Cookie`, and RFC 9111 Section 7.3 says even `Set-Cookie` does not inhibit caching.
> > Cookie-authenticated responses therefore need `Cache-Control: private` or `no-store` from the origin.
> >
> > Follow-up: What does `Vary: Cookie` cost at a CDN, and why is it still the wrong fix?

> [!question] L5: What are Omer Gil's three conditions for web cache deception?
>
> > [!success]- Answer
> > The origin returns the content of `/home.php` for `/home.php/nonexistent.css`.
> > The cache stores by extension while disregarding caching headers.
> > The victim is authenticated when they request the crafted URL.
> >
> > Follow-up: Which condition does Starlette's default router remove, and which application pattern brings it back?

> [!question] L6 (Predict-the-output): In the lab, the edge drops `utm_content` from the key, and an attacker first sends `GET /api/prices?currency=USD&utm_content=x;currency=EUR`; what does a victim's `GET /api/prices?currency=USD` return with the Starlette parser, and with `legacy_parse_qsl`?
>
> > [!success]- Answer
> > With Starlette on CPython `3.12` to `3.15`, the victim gets `{"currency": "USD", "price": "10.00"}`, because `parse_qsl` splits only on `&`, so `currency=EUR` stays inside `utm_content`.
> > With `legacy_parse_qsl`, the origin sees a second `currency=EUR`, last value wins, and the victim gets the stored `{"currency": "EUR", "price": "9.20"}` with `Cache-Status: edge; hit`.
> > Both cases share one cache key, `currency=USD`.
> >
> > Follow-up: Why is "rewrite the request, not the key" a structural fix for this class?

> [!question] L6 (Debug-this-API): Your public catalog API behind a CDN occasionally returns `next` links on an unknown domain for windows of exactly five minutes, the origin logs show nothing unusual, and the app runs FastAPI on Hypercorn with `ProxyFixMiddleware()` - where is the bug?
>
> > [!success]- Answer
> > The five-minute windows match `max-age=300`, so a poisoned entry is being served from the CDN, which is why the origin sees nothing.
> > Hypercorn's `ProxyFixMiddleware` defaults to legacy mode with `trusted_hops=1`, so the last `X-Forwarded-Host` value, which is the client's if the CDN passes it through, replaces `Host`, and FastAPI's `url_for` builds links on it.
> > Fix the edge to overwrite `X-Forwarded-Host`, or build links from a configured base URL, then purge the affected keys.
> >
> > Follow-up: How would you find every other endpoint with the same exposure before an attacker does?

> [!question] L6: Why can an RFC-compliant cache still be used for cache-poisoned denial of service?
>
> > [!success]- Answer
> > RFC 9110 Section 15.1 makes `404` and `405` heuristically cacheable, and RFC 9111 Section 4.2.2 lets a cache assign heuristic freshness when the response has no explicit expiration.
> > An origin that honours `X-HTTP-Method-Override` returns `405` to a `GET` the cache keyed as a normal read, and the cache may store it.
> > The lab proves the stored `405` is served to the next client when heuristic freshness is enabled, and not otherwise.
> >
> > Follow-up: Which status should an origin send for an oversized header section, and why does that matter to caches?

> [!question] L7+: You own the API platform for 200 services behind one CDN; what policy prevents both poisoning and deception without a per-service security review?
>
> > [!success]- Answer
> > Make "origin `Cache-Control` wins" a CDN invariant, so no path or extension rule can store `private` or `no-store` responses.
> > Make the edge the only writer of `X-Forwarded-*` and `Forwarded`, and strip non-allowlisted request headers before the origin.
> > Give services a single platform helper for absolute links from configuration, and lint for direct reads of forwarding headers.
> > Default authenticated routes to `private, no-store` in the framework, and require an explicit, reviewed opt-in with a principal-scoped key for anything else.
> >
> > Follow-up: What does this policy cost a team that wants per-user edge caching, and what would you offer instead?

> [!question] L7+: When is `Vary` the right defense against an unkeyed input, and when is it the wrong one?
>
> > [!success]- Answer
> > `Vary` is right when the input has a small, closed set of legitimate values and every cache in the path honours it, as RFC 9111 Section 4.1 requires.
> > It is wrong when attackers can choose arbitrary values, because each value creates a new variant and fragments the cache, and wrong when a CDN ignores or normalizes `Vary`; the lab shows that an edge ignoring `Vary` reopens the poisoning.
> > For forwarding headers the better fix is to remove the input, not to key it.
> >
> > Follow-up: How would you detect at runtime that a cache in the path is ignoring `Vary`?

## Related

Prerequisites: [[Client-Side-HTTP-Caching-in-Python-Clients]]

Next: [[Fieldings-Dissertation-and-the-Six-Constraints-of-REST]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

- Within this folder: [[HTTP-Caching-Architecture-Private-Shared-and-Gateway-Caches]], [[Cache-Control-Directives-and-Freshness-Lifetime-Calculation]], [[Vary-Header-Mechanics-and-Cache-Hit-Collapse]], [[Caching-Authenticated-and-Multi-Tenant-API-Responses]], [[Cache-Status-Header-for-Observability-RFC-9211]], [[Cache-Invalidation-Upon-Unsafe-Methods-and-Purge-Patterns]].
- Across the book: [[Forwarded-Header-RFC-7239-and-X-Forwarded-For-Spoofing]], [[HTTP-Request-Smuggling-CL-TE-and-Desync-Mechanics]], [[HTTP-Desync-and-Header-Normalization-Across-Multi-Tier-Proxies]], [[Edge-Caching-and-CDN-Shielding-for-Dynamic-and-Public-APIs]], [[Case-Study-CDN-Cache-Leak-of-Authenticated-PII-Responses]], [[Richardson-Maturity-Model-HATEOAS-and-Web-Linking-RFC-8288]], [[Security-Headers-for-HTTP-APIs-HSTS-Nosniff-Cache-Control-and-Security-Txt]], [[OWASP-API-Security-Top-10-2023-Complete-Threat-Model]].
- Lab: `labs/tests/f03_http_caching_and_conditional_requests/test_web_cache_poisoning_and_cache_deception_attacks.py`

## Further reading

- RFC 9111, HTTP Caching: the cache key (Section 2), storage rules (Section 3), `Authorization` (Section 3.5), `Vary` matching (Section 4.1), and the security considerations (Section 7).
- RFC 9110, HTTP Semantics: `Host` as a poisoning target (Section 7.2), misdirected requests (Section 7.4), and the heuristically cacheable status list (Section 15.1).
- RFC 9113, HTTP/2: `:authority` and `Host` in requests (Section 8.3.1).
- RFC 9114, HTTP/3: the same pseudo-header rules for QUIC (Section 4.3.1).
- RFC 7239, Forwarded HTTP Extension: the `host=` parameter (Section 5.3) and why the field cannot be trusted (Section 8.1).
- RFC 9211, The Cache-Status HTTP Response Header Field: the `hit`, `fwd`, and `stored` vocabulary the lab emits.
- James Kettle, "Practical Web Cache Poisoning", PortSwigger Research, 2018: the unkeyed-input method and the Mozilla, Red Hat, and Drupal cases.
- James Kettle, "Web Cache Entanglement: Novel Pathways to Poisoning", PortSwigger Research, 2020: cache-key normalization, parameter cloaking, and fat `GET`.
- Martin Doyhenard, "Gotta cache 'em all: bending the rules of web cache exploitation", PortSwigger Research, 2024: delimiter and normalization discrepancies for poisoning and deception.
- Omer Gil, "Web Cache Deception Attack", Black Hat USA 2017 white paper: the original conditions and mitigations.
- Mirheidari et al., "Cached and Confused: Web Cache Deception in the Wild", USENIX Security 2020: the first large-scale measurement.
- Nguyen, Lo Iacono, and Federrath, "Your Cache Has Fallen: Cache-Poisoned Denial-of-Service Attack", ACM CCS 2019 (cpdos.org): HHO, HMC, and HMO.
- NVD CVE-2021-23336: CPython `parse_qsl` parameter cloaking, and the `v3.9.2` `Lib/urllib/parse.py` change that added `separator`.
