---
type: concept
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://www.rfc-editor.org/rfc/rfc9727.html"
  - "https://www.rfc-editor.org/errata/rfc9727"
  - "https://www.rfc-editor.org/rfc/rfc9264.html"
  - "https://www.rfc-editor.org/errata/rfc9264"
  - "https://www.rfc-editor.org/rfc/rfc8288.html"
  - "https://www.rfc-editor.org/errata/rfc8288"
  - "https://www.rfc-editor.org/rfc/rfc8615.html"
  - "https://www.rfc-editor.org/rfc/rfc8631.html"
  - "https://www.rfc-editor.org/rfc/rfc9110.html"
  - "https://www.iana.org/assignments/well-known-uris/well-known-uris-1.csv"
  - "https://www.iana.org/assignments/link-relations/link-relations-1.csv"
  - "https://www.iana.org/assignments/media-types/application.csv"
  - "https://www.iana.org/assignments/profile-uris/profile-uris.xhtml"
  - "https://www.iana.org/assignments/profile-uris"
  - "https://www.rfc-editor.org/rfc/rfc6573.txt"
  - "https://owasp.org/API-Security/editions/2023/en/0xa9-improper-inventory-management/"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?keywordSearch=well-known%2Fapi-catalog"
---

# Machine-Readable API Discovery: /.well-known/api-catalog and Linkset Documents (RFC 9727)

> [!summary] Claim
> RFC 9727 turns API discovery into one fixed origin-relative URL, `/.well-known/api-catalog`, that `SHALL` answer `GET` with a catalog the publisher `MUST` offer as `application/linkset+json` and `HEAD` with a `Link: <...>; rel="api-catalog"` header, so a catalog is only as correct as its conformance to the RFC 9264 Linkset JSON rules, which the RFC's own Section 5.1 example breaks.

Part of [Versioning Evolution and API Guidelines](README.md) in [API Engineering](../README.md).

## TL;DR

Mechanism: a client sends `GET /.well-known/api-catalog` with `Accept: application/linkset+json` and receives a set of typed links whose contexts are APIs and whose relations (`item`, `service-desc`, `service-doc`, `service-meta`, `status`, `api-catalog`) point to endpoints, OpenAPI files, docs, and health pages `(RFC 9727 Section 2, Section 3.1, Section 4.3, Appendix A.1)`.
Contract rule: every relation member and every extension attribute is a JSON array even when it holds one value, and `linkset` is the sole top-level member `(RFC 9264 Section 4.2.1, Section 4.2.2, Section 4.2.4.3)`.
Trap: the RFC 9727 Section 5.1 example writes `"api-catalog": "https://..."` as a bare string, which is reported as Erratum ID 9009 (status `Reported`, as of 2026-11), and a strict consumer rejects it.
Portability fact: Starlette `Route` answers `HEAD` for any `GET` route, but a FastAPI `@app.get` route returns `405 Method Not Allowed` with `Allow: GET` to `HEAD`, which violates the `HEAD` `SHALL` in RFC 9727 Section 2; the lab proves both on CPython 3.12 to 3.15.
When not to use it: do not treat the catalog as an inventory of record, an authorization boundary, or a substitute for OpenAPI; it is a signpost, and for internal-only APIs it is a reconnaissance map that needs access control `(RFC 9727 Section 8)`.

## Mental model

```mermaid
sequenceDiagram
    participant C as Client or agent
    participant W as www.example.com
    participant K as /.well-known/api-catalog
    participant N as Nested catalog
    C->>W: HEAD /
    W-->>C: 200, Link: </.well-known/api-catalog>; rel="api-catalog"
    C->>K: GET, Accept: application/linkset+json
    K-->>C: 200, Content-Type: application/linkset+json;<br/>profile="https://www.rfc-editor.org/info/rfc9727"<br/>ETag, Vary: Accept
    Note over C: validate RFC 9264 JSON rules,<br/>reject non-https and foreign hosts
    C->>N: GET each rel="api-catalog" target (visited set, depth and count budget)
    N-->>C: 200 linkset or 406 or invalid
    C->>K: GET, If-None-Match: "etag"
    K-->>C: 304 Not Modified, ETag, Vary
```

The client starts anywhere on the origin and learns the catalog location either from a `Link` header with relation `api-catalog` or by trying the fixed well-known path, which RFC 8615 roots at the top of the path hierarchy only `(RFC 8615 Section 3)`.
The catalog response declares its profile in the media type parameter so a generic Linkset reader and an API-catalog-aware reader consume the same bytes `(RFC 9264 Section 5)`.
Before trusting anything, the client validates the document against the Linkset JSON rules and applies its own origin policy, because the Linkset model allows third-party links whose anchor is somebody else's resource `(RFC 9264 Section 9)`.
Nested `api-catalog` links make the catalog a graph rather than a tree, so the client walks it breadth first with a visited set and hard budgets.
Revalidation with `If-None-Match` lets crawlers and agents poll cheaply, which matters because RFC 9727 Section 5.3 tells publishers to consider caching for large catalogs.

## How it works

### The well-known resource and its two methods

RFC 8615 defines a well-known URI as one whose path begins with `/.well-known/`, requires registration, and states that `/foo/.well-known/example` is not well-known `(RFC 8615 Section 3)`.
IANA lists `api-catalog` as `permanent`, change controller `IETF`, registered `2024-12-23` (IANA well-known URIs registry, fetched 2026-11-10).
RFC 9727 then requires two behaviors of a supporting publisher `(RFC 9727 Section 2)`:
- an HTTPS `GET` to `/.well-known/api-catalog` returns an API catalog document;
- an HTTPS `HEAD` to the same path returns a response including a `Link` header with the `api-catalog` relation.
The document location is the publisher's choice; the well-known path can resolve to any URI, and the `api-catalog` relation can point to that URI from any page `(RFC 9727 Section 3, Section 4)`.
The format is mandatory: the publisher `MUST` publish `application/linkset+json`, the Linkset `SHOULD` carry `profile="https://www.rfc-editor.org/info/rfc9727"`, and other formats such as APIs.json are allowed only in addition, via content negotiation `(RFC 9727 Section 4.2, Section 6.2)`.

`HEAD` inherits its rules from HTTP semantics: the server `MUST NOT` send content and `SHOULD` send the same header fields it would send for `GET` `(RFC 9110 Section 9.3.2)`.
If it sends `Content-Length`, the value `MUST` equal the octet count a `GET` would have carried `(RFC 9110 Section 8.6)`.

### The wire bytes

Response headers captured from the lab app for the two-API catalog used in the tests (`GET` with `Accept: application/linkset+json` through `httpx.ASGITransport`, CPython 3.14.6; the body is truncated here):

```text
GET 200
vary: Accept
link: <https://www.example.com/.well-known/api-catalog>; rel="api-catalog"
etag: "5f0c533bef8c3988d62b2c2e884398b5"
content-type: application/linkset+json; profile="https://www.rfc-editor.org/info/rfc9727"
content-length: 714
b'{"linkset":[{"anchor":"https://www.example.com/.well-known/api-catalog","item":[{"href":"https://developer.example.com/apis/foo_api"},{"href":"https://developer'

HEAD 200
vary: Accept
link: <https://www.example.com/.well-known/api-catalog>; rel="api-catalog"
etag: "5f0c533bef8c3988d62b2c2e884398b5"
content-type: application/linkset+json; profile="https://www.rfc-editor.org/info/rfc9727"
content-length: 714
b''
```

The same resource with `Accept: application/linkset` returns the `Link`-field serialization of the same six links, one per line:

```text
<https://developer.example.com/apis/foo_api>; rel="item"; anchor="https://www.example.com/.well-known/api-catalog",
<https://developer.example.com/apis/bar_api>; rel="item"; anchor="https://www.example.com/.well-known/api-catalog",
<https://developer.example.com/apis/foo_api/spec>; rel="service-desc"; anchor="https://developer.example.com/apis/foo_api"; type="application/yaml",
<https://developer.example.com/apis/foo_api/doc>; rel="service-doc"; anchor="https://developer.example.com/apis/foo_api"; type="text/html",
<https://developer.example.com/apis/foo_api/status>; rel="status"; anchor="https://developer.example.com/apis/foo_api"; type="application/json",
<https://developer.example.com/apis/bar_api/spec>; rel="service-desc"; anchor="https://developer.example.com/apis/bar_api"; type="application/vnd.oai.openapi+json"
```

### The Linkset JSON grammar

RFC 9264 maps the RFC 8288 abstract model (context, relation type, target, target attributes) onto three nested JSON levels `(RFC 8288 Section 2, RFC 9264 Section 4.2)`:
1. Set of links: an object that `MUST` contain `linkset` as its sole member, whose value is an array even for one context `(RFC 9264 Section 4.2.1)`.
2. Link context object: an optional `anchor` (a URI reference that `SHOULD NOT` be relative), plus one member per relation type whose value `MUST` be an array of link target objects, named by the registered relation name or, for extension relations, by the relation URI `(RFC 9264 Section 4.2.2)`.
3. Link target object: a mandatory `href`, with `""` meaning the link set resource itself, plus target attributes `(RFC 9264 Section 4.2.3)`.

Target attributes have fixed shapes `(RFC 9264 Section 4.2.4)`:
- `type`, `media`, `title` are strings; `hreflang` is an array of strings;
- `title*` and any other name ending in `*` is an array of `{"value": ..., "language": ...}` objects with the RFC 8187 charset stripped;
- every other extension attribute is an array of strings, because a generic reader cannot know its cardinality `(RFC 9264 Section 4.2.4.3)`.

Relation names follow `reg-rel-type = LOALPHA *( LOALPHA / DIGIT / "." / "-" )` or are absolute URIs `(RFC 8288 Section 3.3)`, and registered names are compared case-insensitively `(RFC 8288 Section 2.1.1)`.

### The relations a catalog uses

| Relation | Meaning in a catalog | Registered by |
| --- | --- | --- |
| `api-catalog` | target is a list of APIs from the publisher of the context; used for self-links and nesting | RFC 9727 Section 3, registered in Section 7.2 |
| `item` | target is an API that is a member of the catalog | RFC 6573, cited in RFC 9727 Section 3.1 |
| `service-desc` | machine-readable description, for example OpenAPI | RFC 8631 Section 4.2, registered in Section 6.2 |
| `service-doc` | human documentation | RFC 8631 Section 4.1, registered in Section 6.1 |
| `service-meta` | other machine-readable metadata such as policies | RFC 8631 Section 4.3, registered in Section 6.3 |
| `status` | status or health resource | RFC 8631 Section 5, registered in Section 6.4 |

All six appear in the IANA Link Relation Types registry (fetched 2026-11-10).

### Two serializations, one model

`application/linkset` is the `Link` header field value with newlines allowed as separators, and it is ASCII only `(RFC 9264 Section 4.1)`.
Converting it to a real `Link` field requires removing newlines or replacing them with `SP` `(RFC 9264 Section 4.1)`.
The lab serves both media types from the same model and proves the round trip: `parse_link_header(to_link_header(doc)) == list(iter_links(doc))`.
The parser follows RFC 8288 Appendix B.2 to B.4: parameter names and relation types are lowercased, a `rel` value is split on whitespace into several links, only the first `title`, `type`, `media`, and `title*` are kept, and relative targets and anchors are resolved against the representation URL.
Appendix B is advisory; where it disagrees, the body of RFC 8288 wins `(RFC 8288 Appendix B)`.

### The implementation in the pinned stack

Starlette adds `HEAD` to a route's method set whenever `GET` is listed: `if "GET" in self.methods: self.methods.add("HEAD")` `(starlette 1.10.0 starlette/routing.py, Route.__init__)`.
FastAPI builds its route method set as `{method.upper() for method in methods}` without that step `(fastapi 0.162.0 fastapi/routing.py, _populate_api_route_state)`, so `@app.get` answers `HEAD` with `405` and `Allow: GET` (proven by `test_fastapi_get_route_does_not_answer_head`).
Starlette's plain `Response.__call__` always sends `self.body`; only `FileResponse.__call__` checks for `HEAD` (`send_header_only`) `(starlette 1.10.0 starlette/responses.py, Response.__call__ and FileResponse.__call__)`.
Uvicorn's h11 path discards the body for `HEAD` with `data = b"" if self.scope["method"] == "HEAD" else body` `(uvicorn 0.54.0 uvicorn/protocols/http/h11_impl.py, RequestResponseCycle.send)`, and its httptools path writes nothing for `HEAD` after setting `self.expected_content_length = 0` `(uvicorn 0.54.0 uvicorn/protocols/http/httptools_impl.py, RequestResponseCycle.send)`.
Hypercorn suppresses it through `suppress_body`, which returns true for `method == "HEAD"` `(hypercorn 0.18.0 hypercorn/utils.py, suppress_body)` and gates the `Body` event `(hypercorn 0.18.0 hypercorn/protocol/http_stream.py, HTTPStream.app_send)`.
A loopback check with an app that wrongly sends an 11-byte body for `HEAD` confirms it: Uvicorn 0.54.0 (h11 and httptools), Hypercorn 0.18.0, and Granian 2.8.7 each answered `HEAD` over HTTP/1.1 with `Content-Length: 11` and zero body bytes (raw socket, CPython 3.14.6, macOS 27.2 arm64).
In-process tests hide a leak: `httpx.ASGITransport` drops `HEAD` body chunks with `if body and request.method != "HEAD"` `(httpx 0.28.1 httpx/_transports/asgi.py, ASGITransport.handle_async_request, inner send)`, so the lab asserts on raw ASGI `send` messages instead.

## Numbers and limits

| Quantity | Value | Source or measurement context |
| --- | --- | --- |
| RFC 9727 publication | June 2025, Standards Track | RFC 9727 header |
| `api-catalog` well-known registration | `permanent`, registered `2024-12-23` | IANA well-known URIs CSV (as of 2026-11) |
| Registered well-known URI suffixes | `107` | count of rows in the IANA CSV fetched 2026-11-10 (as of 2026-11) |
| Open errata against RFC 9727 | `1` (ID `9009`, Technical, `Reported` 2026-06-21) | RFC Editor errata page (as of 2026-11) |
| Errata against RFC 9264 | none listed | RFC Editor errata page, "No matching errata found" (as of 2026-11) |
| Catalog for `100` APIs x 4 links (`item`, `service-desc`, `service-doc`, `status`) | `400` links, `28,283` bytes compact JSON | asserted in `test_catalog_sizes_for_one_hundred_apis`; CPython 3.14.6, macOS 27.2 arm64 |
| Same catalog as one `Link` header value | `41,398` bytes | same test |
| Same JSON after `zlib.compress(body, 9)` | `1,553` bytes (the test asserts only `< body // 5`) | re-measured with a one-off script on the test's body, CPython 3.14.6, `zlib.ZLIB_RUNTIME_VERSION` 1.2.12, macOS 27.2 arm64 |
| `h11` default `max_incomplete_event_size` | `16 * 1024` bytes | `(h11 0.16.0 h11/_connection.py, DEFAULT_MAX_INCOMPLETE_EVENT_SIZE)` |
| `httpcore` HTTP/1.1 client limit on a buffered incomplete header block | `100 * 1024` bytes | `(httpcore 1.0.9 httpcore/_async/http11.py, AsyncHTTP11Connection.MAX_INCOMPLETE_EVENT_SIZE)` |
| Hypercorn h11 limit and HTTP/2 header list size | `16 * 1024` bytes; `2**16` bytes | `(hypercorn 0.18.0 hypercorn/config.py, Config.h11_max_incomplete_size and Config.h2_max_header_list_size)` |
| `qvalue` precision | at most 3 digits after the decimal point | `(RFC 9110 Section 12.4.2)` |
| Accept media ranges considered by the lab | first `32` (`MAX_ACCEPT_RANGES`) | lab policy, tested by `test_negotiate[range-cap]` |
| Lab runtime | `75 passed in 0.44s` | `uv run --frozen pytest ... -q`, CPython 3.14.6, macOS 27.2 arm64 |

The 41,398-byte `Link` header is why RFC 9264 exists: link sets move links out of response fields whose size a server cannot bound `(RFC 9264 Section 3.3)`, and that header trips the 16 KiB `h11` default but fits under `httpcore`'s 100 KiB.
The `h11` limit applies to an incomplete event: fed in 16 KiB reads, a response carrying that header raises `RemoteProtocolError("Receive buffer too long")` at the default and parses at `100 * 1024`, while a single read holding the whole header block parses at either limit `(h11 0.16.0 h11/_connection.py, Connection.next_event)`; measured with `h11.Connection(h11.CLIENT)`, CPython 3.14.6, macOS 27.2 arm64.

## Portability

| Dimension | Behavior | Proof |
| --- | --- | --- |
| CPython `3.12.13` | 75 lab tests pass; the pinned `3.12.16` leg is not installed on this machine | `uv run --frozen --python 3.12` in a separate environment, macOS 27.2 arm64 |
| CPython `3.13.18` | 75 lab tests pass | `uv run --frozen --python /opt/homebrew/bin/python3.13` |
| CPython `3.14.6` (workspace) and `3.14.10` | 75 lab tests pass on both | workspace `.venv`, and `--python /opt/homebrew/bin/python3.14` |
| CPython `3.15.2` | 75 lab tests pass | `--python /opt/homebrew/bin/python3.15` |
| CPython `3.15t` free-threaded | not run: no `3.15.2t` is installed, and the workspace cannot sync on `3.15.0b3+freethreaded` or `3.14.6+freethreaded` because `psycopg-binary==3.3.8` has no wheel for either | `uv run --frozen --python 3.15t` and `--python 3.14t`, both failing at install; free-threaded behavior is unproven |
| Debian Trixie and Alpine musl containers | not run | the workspace has no container definitions yet (`labs/README.md`) |
| Starlette `1.10.0` | `GET` route also answers `HEAD`; `POST` gets `405` with `Allow` `{GET, HEAD}` | `test_unsafe_method_gets_405_with_allow_including_head`; `(starlette 1.10.0 starlette/routing.py, Route.__init__)` |
| FastAPI `0.162.0` | `@app.get` answers `HEAD` with `405`, `Allow: GET`; `api_route(methods=["GET", "HEAD"])` answers `200` | `test_fastapi_get_route_does_not_answer_head` |
| httpx `0.28.1` client | `Response.links` keys by the raw `rel` string, collapses duplicates, and drops every parameter after one whose value contains `=` | `test_httpx_links_drops_rel_when_an_earlier_param_contains_equals`, `test_httpx_links_keys_by_raw_rel_string_and_collapses_duplicates` |
| Uvicorn `0.54.0` (h11 and httptools), Hypercorn `0.18.0` | strip `HEAD` bodies at the server and keep the app's `Content-Length` | source citations in How it works, plus a raw-socket loopback check over HTTP/1.1; the lab tests themselves open no socket |
| Granian `2.8.7` | strips the `HEAD` body and keeps the app's `Content-Length` over HTTP/1.1 | the same raw-socket loopback check, plus source: Granian passes the app's body to hyper without checking the method `(granian 2.8.7 src/asgi/io.rs, send_response)`, and hyper 1.12 forces a zero-length encoder when `Server::can_have_body` is false for `HEAD` `(hyper 1.12.0 src/proto/h1/role.rs, Server::can_chunked and Server::encode)`, read at the GitHub release tags; HTTP/2 was not exercised |
| HTTP/1.1, HTTP/2, HTTP/3 | `GET`, `HEAD`, `Link`, `Vary`, `ETag`, and content negotiation are version-independent semantics | `(RFC 9110 Abstract)`: it "defines aspects of the protocol that are shared by all versions" |

## Worked example

The [implementation](../labs/src/api_labs/f07_versioning_evolution_and_api_guidelines/api_catalog.py) builds, validates, serializes, serves, and crawls catalogs; the [test](../labs/tests/f07_versioning_evolution_and_api_guidelines/test_api_discovery_and_well_known_api_catalog_rfc_9727.py) has 75 cases, every one marked `@pytest.mark.unit`.

Building a catalog combines RFC 9727 Appendix A.2 (bookmarks under the catalog's own anchor) with Appendix A.1 (one context per API with RFC 8631 relations).
Simplified excerpt of `build_catalog`, with the input checks removed:

```python
head: dict[str, object] = {"anchor": catalog_url}
if apis:
    head["item"] = [{"href": api.anchor} for api in apis]
if nested_catalogs:
    head["api-catalog"] = [{"href": uri} for uri in nested_catalogs]
contexts: list[dict[str, object]] = [head]
for api in apis:
    rels = api.relations()
    if rels:
        ctx: dict[str, object] = {"anchor": api.anchor}
        for rel, targets in rels:
            ctx[rel] = [target.to_json() for target in targets]
        contexts.append(ctx)
return {"linkset": contexts}
```

The input checks reject relative or `http` URIs, duplicate API anchors (the duplicate-entry lint RFC 9727 Section 5.4 recommends), and catalogs with no links at all.

The server, byte-identical to the lab file:

```python
def create_app(catalog: Mapping[str, object], catalog_url: str) -> Starlette:
    """Serve one catalog at /.well-known/api-catalog with GET, HEAD, 304, and 406."""
    problems = [v for v in validate_api_catalog(catalog) if v.level == "MUST"]
    if problems:
        raise ValueError(f"refusing to serve a non-conformant catalog: {problems}")
    json_body = json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).encode()
    reps = {
        LINKSET_JSON: _representation(LINKSET_JSON, json_body),
        LINKSET_TEXT: _representation(LINKSET_TEXT, to_link_format(catalog).encode("ascii")),
    }
    self_link = f'<{catalog_url}>; rel="api-catalog"'

    async def api_catalog(request: Request) -> Response:
        common = {"vary": "Accept", "link": self_link}
        chosen = negotiate(request.headers.get("accept"))
        if chosen is None:
            body = json.dumps({"available": list(AVAILABLE_TYPES)}).encode()
            return Response(body, 406, headers=common, media_type="application/json")
        rep = reps[chosen]
        headers = {**common, "etag": rep.etag}
        if _etag_matches(request.headers.get("if-none-match", ""), rep.etag):
            return Response(status_code=304, headers=headers)
        headers["content-type"] = rep.content_type
        headers["content-length"] = str(len(rep.body))
        body = b"" if request.method == "HEAD" else rep.body
        return Response(body, 200, headers=headers)

    # Starlette's Route adds HEAD whenever GET is listed (starlette 1.10.0 routing.py).
    return Starlette(routes=[Route(WELL_KNOWN_PATH, api_catalog, methods=["GET"])])
```

Design decisions in that function, each with its rule:
- the catalog is validated once at startup, so a malformed document fails the deploy instead of every consumer;
- each representation gets its own `ETag`, because the JSON and text forms are different representations of one resource `(RFC 9110 Section 8.8.3.3)`;
- `If-None-Match` is checked after negotiation, against the selected representation, using weak comparison `(RFC 9110 Section 13.1.2)`;
- the `304` carries `ETag` and `Vary`, which a `304` `MUST` repeat `(RFC 9110 Section 15.4.5)`;
- an unacceptable `Accept` gets `406` with a list of available types, as RFC 9110 Section 15.5.7 suggests, and a missing `Accept` means no preference `(RFC 9110 Section 12.4.1)`, which the lab treats as `*/*`;
- `HEAD` sends the `GET` length and an empty body at the ASGI layer, so correctness does not depend on the server stripping it.

The validator catches the RFC's own examples.
Simplified excerpt of the relation check inside `validate_linkset`:

```python
if not (_REG_REL_TYPE.fullmatch(rel) or _is_absolute(rel)):
    out.append(Violation(r_path, "MUST", "rel MUST be reg-rel-type or a URI"))
targets = _as_list(value)
if targets is None:
    out.append(Violation(r_path, "MUST", "link targets MUST be an array (4.2.2)"))
    continue
```

Feeding it the RFC 9727 Section 5.1 example verbatim yields exactly one violation, `("/linkset/0/api-catalog", "MUST")`, matching Erratum ID 9009.
Feeding it the first `memento` target from RFC 9264 Figure 10 yields one violation at `/linkset/0/memento/0/datetime`, because `datetime` is an extension attribute written as a string where Section 4.2.4.3 requires an array; no erratum was filed against RFC 9264 as of 2026-11.

The crawler is the client half.
It walks `api-catalog` links breadth first, keeps a visited set, and rejects with a reason instead of raising: `not https`, `host not allowed`, `depth limit`, `catalog budget exhausted`, `fetch failed`, or `invalid linkset`.
Its `fetch` argument is any callable, so the lab runs it against a dictionary and never opens a socket.

Captured run (CPython 3.14.6, macOS 27.2 arm64, from `labs/`):

```text
$ uv run --frozen pytest tests/f07_versioning_evolution_and_api_guidelines/test_api_discovery_and_well_known_api_catalog_rfc_9727.py -q
........................................................................ [ 96%]
...                                                                      [100%]
75 passed in 0.44s
```

The lab was checked by mutation: the verification pass applied 54 single-change breaks to `api_catalog.py` and reran this module after each.
51 failed a named test at once; the 3 survivors (crawler relative-reference resolution, the default context of an anchor-less link, and case folding of relation names on the JSON side) are now killed by two added tests.
Making `HEAD` send the body still passes the other 74 tests and fails only the raw-ASGI test, which is how the `ASGITransport` behavior above was found.

## Pitfalls

> [!warning] Copying the RFC 9727 Section 5.1 example produces an invalid Linkset
> Trigger: a publisher pastes the nested-catalog example, `"api-catalog": "https://www.example.net/.well-known/api-catalog"`.
> Symptom: a strict consumer rejects the whole document; a lenient one iterates the string character by character or drops the link.
> Fix: wrap every relation value in an array of target objects, `"api-catalog": [{"href": "..."}]`, as Erratum ID 9009 proposes and RFC 9264 Section 4.2.2 requires; gate the catalog with a validator in CI.

> [!warning] FastAPI `@app.get` does not satisfy the `HEAD` requirement
> Trigger: the well-known route is declared with `@app.get("/.well-known/api-catalog")` in FastAPI 0.162.0.
> Symptom: `HEAD` returns `405 Method Not Allowed` with `Allow: GET`, so discovery clients that probe with `HEAD`, as RFC 9727 Section 2 and RFC 9264 Section 7.3 show, see no `Link` header.
> Fix: declare `@app.api_route(path, methods=["GET", "HEAD"])` or mount a Starlette `Route`, and test `HEAD` explicitly.

> [!warning] In-process tests cannot see a `HEAD` body leak
> Trigger: the endpoint returns the full body for `HEAD` and the suite uses `httpx.ASGITransport`.
> Symptom: tests pass because httpx 0.28.1 drops `HEAD` body chunks; production behavior then depends on the server, and although the three pinned servers strip it over HTTP/1.1, another server or an ASGI-level proxy that does not emits a body where RFC 9110 Section 9.3.2 says `MUST NOT`.
> Fix: assert on the ASGI `http.response.body` message, as `test_head_app_emits_no_body_bytes_at_the_asgi_layer` does.

> [!warning] `httpx.Response.links` silently loses relations
> Trigger: a client discovers the catalog with `response.links["api-catalog"]` and the server sends `anchor="https://www.example.com/?v=1"` before `rel`.
> Symptom: `KeyError`; httpx 0.28.1 splits each parameter on `=`, fails to unpack the three parts the second `=` produces, and stops parsing the link, so the result is keyed by the URL instead of the relation `(httpx 0.28.1 httpx/_models.py, _parse_header_links and Response.links)`.
> It also keys by the unsplit `rel` string (`"item api-catalog"`), and a second link with the same relation overwrites the first.
> Fix: parse with an RFC 8288 Appendix B parser, or consume the JSON Linkset instead of the header.

> [!warning] Content negotiation without `Vary: Accept` poisons shared caches
> Trigger: the catalog serves both `application/linkset+json` and another format but omits `Vary`.
> Symptom: a shared cache stores whichever format the first client asked for and serves it to everyone.
> Fix: send `Vary: Accept` on `200`, `304`, and `406` responses `(RFC 9110 Section 12.5.5, Section 15.4.5)`; see [[Vary-Header-Mechanics-and-Cache-Hit-Collapse]].

No CVE or public postmortem specific to `/.well-known/api-catalog` was found: NVD keyword searches for `well-known/api-catalog` and `RFC 9727` return 0 results (as of 2026-11), so the risks below are taken from the RFC security sections.

## Security and robustness

The catalog is a reconnaissance map by design.
RFC 9727 Section 8 asks for a security and privacy review before deployment, an audit that no internal-only API leaks into a public catalog, read-only external access, write access only for the roles that update the catalog or its rewrite rules, rate limiting, and TLS.
RFC 8631 Section 7 adds that service descriptions help attackers find known issues, so link only what a consumer needs.
That maps to OWASP API9:2023 Improper Inventory Management in both directions: a catalog that lists retired "zombie" APIs advertises them, and one that omits live APIs hides them from the security team; see [[Improper-Inventory-Management-API9-2023-and-Unsafe-Consumption-of-APIs-API10-2023]].

Write access to `/.well-known/` controls metadata for the entire origin, and some administrators never see dot-directories `(RFC 8615 Section 4.1, Section 4.4)`.
Deploy the catalog as a build artifact from the release pipeline, never as a writable file on a shared host.

On the client side, every link is untrusted input:
- third-party links let one party assert links whose anchor is another party's resource `(RFC 9264 Section 9)`, and RFC 8288 Section 5 suggests discarding anchored links unless the resources share an authority;
- metadata from a well-known URI should not be applied to other hosts or ports `(RFC 8615 Section 4.3)`, which is why the lab crawler takes an explicit `allowed_hosts` set instead of trusting cross-host anchors such as `apis.example.net` in RFC 9727 Appendix A.1;
- following `api-catalog` links is a server-side fetch of attacker-chosen URLs when the root is user-supplied, so a crawler needs the SSRF guards in [[Preventing-SSRF-API7-2023-DNS-Pinning-and-Private-CIDR-Socket-Guards]] in addition to the https-only rule;
- nesting can form cycles, so the crawler has a visited set, a depth limit, and a catalog-count budget, each with a test;
- descriptions can be stale or wrong, and consumers `SHOULD` handle runtime errors rather than trust them blindly `(RFC 8631 Section 7)`.

For the parser: the lab caps `Accept` at 32 media ranges, rejects malformed `qvalue`s, refuses non-ASCII in `application/linkset` `(RFC 9264 Section 4.1)`, and ends the parse at the first structural error in a `Link` value instead of guessing.
Serve the JSON with its specific media type and `X-Content-Type-Options: nosniff`, as RFC 8615 Section 4.2 suggests for well-known resources reachable from browsers.

## The principal's view

The decision is not whether to publish `/.well-known/api-catalog` (it is cheap) but what system of record generates it and who owns its correctness.
A hand-edited catalog drifts from the real estate within a release or two; a catalog generated from the same registry that drives the gateway, the OpenAPI publication, and deprecation schedules stays true, and RFC 9727 Section 5.5 describes exactly that release-pipeline integration.
The organizational trade-off is centralization: one root catalog with nested group catalogs (RFC 9727 Section 5.3) lets teams own their sub-catalogs, but the root needs an owner who enforces validation, origin policy, and removal of retired entries.
Pair it with the deprecation signals in [[Deprecation-Header-RFC-9745-and-Sunset-Header-RFC-8594]] so a crawler that finds an API also learns when it goes away, and with the estate-level practice in [[API-Estate-Inventory-Cataloging-RFC-9727-and-Shadow-API-Discovery]].

Choose something else when the problem is different:
- for a typed contract, the `service-desc` target (OpenAPI 3.1, see [[OpenAPI-3-1-Architecture-and-JSON-Schema-Alignment]]) is the artifact, and the catalog only points at it;
- for in-response navigation between resources, use links in representations, see [[Richardson-Maturity-Model-HATEOAS-and-Web-Linking-RFC-8288]];
- for LLM tools, MCP tool listing is a protocol with invocation semantics that a Linkset does not have, see [[Model-Context-Protocol-MCP-Architecture-JSON-RPC-Transports-and-Tool-Contracts]];
- for private service-to-service discovery inside a mesh, DNS or a service registry resolves endpoints at runtime, which a static document cannot.

## Interview questions

> [!question] L5: What must a server do to support RFC 9727, at minimum?
>
> > [!success]- Answer
> > Answer `GET /.well-known/api-catalog` over HTTPS with a catalog document in `application/linkset+json`.
> > Answer `HEAD` on the same path with a `Link` header carrying the `api-catalog` relation.
> > The catalog `MUST` include hyperlinks to API endpoints, and the media type `SHOULD` carry `profile="https://www.rfc-editor.org/info/rfc9727"`.
> >
> > Follow-up: Why is `HEAD` required in addition to `GET`?

> [!question] L5: Why must `"item": {"href": "..."}` be written as `"item": [{"href": "..."}]`?
>
> > [!success]- Answer
> > RFC 9264 Section 4.2.2 says the relation member's value is an array of link target objects even when there is only one.
> > A fixed shape lets generic readers iterate without type sniffing and keeps round trips with the `Link` header unambiguous.
> > The same rule applies to `linkset` itself and to extension attributes.
> >
> > Follow-up: Which target attributes are strings rather than arrays?

> [!question] L5: What do `service-desc`, `service-doc`, `service-meta`, and `status` point to?
>
> > [!success]- Answer
> > `service-desc` points to a machine-readable description such as an OpenAPI document, and `service-doc` to human documentation (RFC 8631 Section 4).
> > `service-meta` points to other machine-readable metadata such as policies (RFC 8631 Section 4.3), and `status` to a status or health resource (RFC 8631 Section 5).
> > In an API catalog they hang off a link context whose anchor is the API endpoint.
> >
> > Follow-up: Where would you put a deprecation date for an API in this model?

> [!question] L6: Predict the output: `httpx.Response(200, headers={"link": '<https://www.example.com/c>; anchor="https://www.example.com/?v=1"; rel="api-catalog"'}).links` in httpx 0.28.1.
>
> > [!success]- Answer
> > `{"https://www.example.com/c": {"url": "https://www.example.com/c"}}`.
> > The parser runs `key, value = param.split("=")`, the second `=` inside the quoted anchor yields three parts so the unpack raises `ValueError`, and the `except ValueError: break` abandons the rest of the parameters, including `rel`.
> > With no `rel`, the dictionary falls back to the URL as the key, so `links["api-catalog"]` raises `KeyError`.
> >
> > Follow-up: What does the same call return for `rel="item api-catalog"`, and what does RFC 8288 Section 3.3 say it should mean?

> [!question] L6: Debug this API: why does a discovery bot report "no api-catalog link" for your FastAPI service when `curl https://host/.well-known/api-catalog` returns the document?
>
> > [!success]- Answer
> > The bot probes with `HEAD`, and a FastAPI 0.162.0 `@app.get` route answers `HEAD` with `405` and `Allow: GET`, so there is no `Link` header to read.
> > FastAPI builds the route's method set without adding `HEAD`, unlike Starlette's `Route`, which adds it for every `GET` route.
> > Fix it with `@app.api_route(path, methods=["GET", "HEAD"])`, send the `Link` header on both methods, and add a test that sends `HEAD`.
> >
> > Follow-up: Why would an `httpx.ASGITransport` test not catch the opposite bug, a `HEAD` response with a body?

> [!question] L6: How should the catalog endpoint combine content negotiation with `ETag` and `304`?
>
> > [!success]- Answer
> > Negotiate first, then evaluate `If-None-Match` against the selected representation with weak comparison (RFC 9110 Section 13.1.2).
> > Give each representation its own `ETag`, because the JSON and text forms are different representations (RFC 9110 Section 8.8.3.3).
> > Send `Vary: Accept` on every response, including `304` and `406`, and repeat `ETag` and `Vary` on the `304` (RFC 9110 Section 15.4.5).
> >
> > Follow-up: What breaks if both representations share one `ETag`?

> [!question] L7+: What can go wrong when your service crawls customer-supplied catalogs, and what are your guards?
>
> > [!success]- Answer
> > The root URL and every nested `api-catalog` target are attacker-chosen fetches, so this is SSRF unless you pin DNS and block private and link-local ranges.
> > Nesting can form cycles or fan out without limit, so keep a visited set, a depth limit, a catalog-count budget, and per-fetch size and time limits.
> > Third-party anchors assert facts about other origins (RFC 9264 Section 9), so apply an explicit host policy instead of trusting cross-host anchors.
> > Reject `http` targets, record every rejection with a reason, and validate every document before using any link in it.
> >
> > Follow-up: Which of these guards does the RFC 9727 Appendix A.1 example force you to relax, and how do you keep it safe?

> [!question] L7+: How do you design the catalog for a company with 400 APIs across six domains and several acquisitions so that it stays correct?
>
> > [!success]- Answer
> > Generate it from the system of record that already drives the gateway and the OpenAPI publication, never by hand (RFC 9727 Section 5.5).
> > Publish one canonical root, redirect the well-known path on the other domains to it, and nest one catalog per group or domain (RFC 9727 Sections 5.1 and 5.3).
> > Gate it in CI with Linkset validation and business lint (duplicates, stale entries, internal hosts in a public catalog), and remove retired APIs as part of the release lifecycle (RFC 9727 Section 5.4).
> > Make one team own the root and its policy, and let domain teams own their sub-catalogs.
> >
> > Follow-up: How do you measure whether anyone actually discovers APIs through it?

> [!question] L7+: When is `/.well-known/api-catalog` the wrong tool?
>
> > [!success]- Answer
> > When you need a contract: that is OpenAPI behind `service-desc`, and the catalog only points at it.
> > When you need runtime endpoint resolution inside a mesh: DNS or a service registry answers per request, while the catalog is a static, cacheable document.
> > When you need tool invocation for LLM agents: MCP carries schemas and call semantics that a Linkset does not.
> > When the APIs are private and the catalog cannot be access-controlled: publishing it only gives attackers a map (RFC 9727 Section 8).
> >
> > Follow-up: What would you still publish publicly for a company whose APIs are all partner-only?

## Related

Prerequisites: [[Brownout-Schedules-Per-Client-Usage-Telemetry-and-Safe-Sunsetting]]

Next: [[Google-AIP-System-Architecture-and-Core-Tenets]]

See also: [[Deprecation-Header-RFC-9745-and-Sunset-Header-RFC-8594]], [[API-Estate-Inventory-Cataloging-RFC-9727-and-Shadow-API-Discovery]], [[Richardson-Maturity-Model-HATEOAS-and-Web-Linking-RFC-8288]], [[Content-Negotiation-Media-Types-and-Charsets]], [[Conditional-GET-Revalidation-and-304-Not-Modified]], [[Strong-vs-Weak-Validators-ETag-and-Last-Modified]], [[Vary-Header-Mechanics-and-Cache-Hit-Collapse]], [[OpenAPI-3-1-Architecture-and-JSON-Schema-Alignment]], [[Starlette-Routing-Internals-Regex-Compilation-and-Mount-Dispatch]], [[In-Process-Testing-of-ASGI-Apps-with-HTTPX-ASGITransport]], [[Improper-Inventory-Management-API9-2023-and-Unsafe-Consumption-of-APIs-API10-2023]], [[Preventing-SSRF-API7-2023-DNS-Pinning-and-Private-CIDR-Socket-Guards]], [[Model-Context-Protocol-MCP-Architecture-JSON-RPC-Transports-and-Tool-Contracts]]

- Lab: `labs/tests/f07_versioning_evolution_and_api_guidelines/test_api_discovery_and_well_known_api_catalog_rfc_9727.py`

## Further reading

- [RFC 9727: api-catalog well-known URI and link relation](https://www.rfc-editor.org/rfc/rfc9727.html) - the normative `GET`, `HEAD`, format, and security requirements this note implements.
- [RFC 9727 errata](https://www.rfc-editor.org/errata/rfc9727) - Erratum ID 9009 on the Section 5.1 example.
- [RFC 9264: Linkset media types and link relation](https://www.rfc-editor.org/rfc/rfc9264.html) - the JSON grammar every catalog must satisfy, and why link sets replace oversized `Link` headers.
- [RFC 8288: Web Linking](https://www.rfc-editor.org/rfc/rfc8288.html) - the link model, `Link` header syntax, and the Appendix B parsing algorithm.
- [RFC 8288 errata](https://www.rfc-editor.org/errata/rfc8288) - verified Erratum 5878 corrects the Appendix B star-parameter steps.
- [RFC 8615: Well-Known URIs](https://www.rfc-editor.org/rfc/rfc8615.html) - registration rules and the origin-wide security considerations of `/.well-known/`.
- [RFC 8631: Link relation types for web services](https://www.rfc-editor.org/rfc/rfc8631.html) - `service-desc`, `service-doc`, `service-meta`, and `status`.
- [RFC 9110: HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110.html) - `HEAD`, `Content-Length`, `Accept`, `Vary`, `If-None-Match`, `304`, and `406`.
- [IANA Well-Known URIs registry](https://www.iana.org/assignments/well-known-uris/well-known-uris-1.csv) - the live registration record for `api-catalog`.
