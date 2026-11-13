"""RFC 9727 api-catalog: build, validate, serialize, serve, and crawl Linkset documents.

Specifications implemented here (section numbers as read in the RFC text):
- RFC 9727 Sections 2, 4.1, 4.2, 4.3, 5.1: the /.well-known/api-catalog resource, GET and
  HEAD, nesting, and the redirect from a non-canonical domain.
- RFC 9264 Sections 4.1 and 4.2.1-4.2.4: application/linkset and application/linkset+json.
- RFC 8288 Section 3 and Appendix B: Link header serialization and parsing.
- RFC 9110 Sections 8.6, 9.3.2, 12.5.1, 13.1.2, 15.4.5, 15.4.9, 15.5.7: HEAD, Accept,
  If-None-Match, 304, 308, and 406.
Everything in this module is deterministic: no sockets, no clock, no randomness.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, cast
from urllib.parse import urldefrag, urljoin, urlsplit

from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from starlette.requests import Request

WELL_KNOWN_PATH: Final = "/.well-known/api-catalog"
PROFILE_URI: Final = "https://www.rfc-editor.org/info/rfc9727"
LINKSET_JSON: Final = "application/linkset+json"
LINKSET_TEXT: Final = "application/linkset"
# Server preference order when the client weights both formats equally.
AVAILABLE_TYPES: Final = (LINKSET_JSON, LINKSET_TEXT)
MAX_ACCEPT_RANGES: Final = 32

# RFC 8288 Section 3.3: reg-rel-type = LOALPHA *( LOALPHA / DIGIT / "." / "-" )
_REG_REL_TYPE: Final = re.compile(r"[a-z][a-z0-9.\-]*")
# RFC 9264 Section 4.2.4.1: target attributes defined by Web Linking.
_STRING_ATTRS: Final = frozenset({"media", "title", "type"})
_SERVICE_RELS: Final = ("service-desc", "service-doc", "service-meta", "status")


# ---------------------------------------------------------------------------
# Building an API catalog (RFC 9727 Section 4, Appendix A.1 and A.2 combined)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    """One link target object (RFC 9264 Section 4.2.3)."""

    href: str
    type: str | None = None
    title: str | None = None

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {"href": self.href}
        if self.type is not None:
            out["type"] = self.type
        if self.title is not None:
            out["title"] = self.title
        return out


@dataclass(frozen=True, slots=True)
class ApiEntry:
    """One API: its endpoint is the link context, RFC 8631 relations are its links."""

    anchor: str
    service_desc: tuple[Target, ...] = ()
    service_doc: tuple[Target, ...] = ()
    service_meta: tuple[Target, ...] = ()
    status: tuple[Target, ...] = ()

    def relations(self) -> list[tuple[str, tuple[Target, ...]]]:
        pairs = zip(
            _SERVICE_RELS,
            (self.service_desc, self.service_doc, self.service_meta, self.status),
            strict=True,
        )
        return [(rel, targets) for rel, targets in pairs if targets]


def _require_absolute_https(uri: str, what: str) -> None:
    parts = urlsplit(uri)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError(f"{what} must be an absolute https URI, got {uri!r}")


def build_catalog(
    catalog_url: str,
    apis: Sequence[ApiEntry],
    nested_catalogs: Sequence[str] = (),
) -> dict[str, object]:
    """Return an application/linkset+json API catalog.

    The first link context object is the catalog itself: one `item` link per API
    (RFC 9727 Section 3.1) and one `api-catalog` link per nested catalog (Section 4.3).
    Each API then gets its own link context object carrying its RFC 8631 relations.
    """
    _require_absolute_https(catalog_url, "catalog_url")
    if not apis and not nested_catalogs:
        raise ValueError("RFC 9727 Section 4.1: the catalog MUST include hyperlinks to APIs")
    seen: set[str] = set()
    for api in apis:
        _require_absolute_https(api.anchor, "api anchor")
        if api.anchor in seen:
            raise ValueError(f"duplicate API anchor {api.anchor!r} (RFC 9727 Section 5.4)")
        seen.add(api.anchor)
        for _rel, targets in api.relations():
            for target in targets:
                _require_absolute_https(target.href, "target href")
    for nested in nested_catalogs:
        _require_absolute_https(nested, "nested catalog")

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


# ---------------------------------------------------------------------------
# Validating any application/linkset+json document (RFC 9264 Section 4.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Violation:
    path: str
    level: Literal["MUST", "SHOULD"]
    rule: str


def _as_dict(value: object) -> dict[str, object] | None:
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def _as_list(value: object) -> list[object] | None:
    return cast("list[object]", value) if isinstance(value, list) else None


def _is_absolute(uri: str) -> bool:
    return bool(urlsplit(uri).scheme)


def _check_uri(value: object, path: str, out: list[Violation]) -> None:
    if not isinstance(value, str):
        out.append(Violation(path, "MUST", "value MUST be a URI reference string"))
    elif value and not _is_absolute(value):
        out.append(Violation(path, "SHOULD", "SHOULD NOT be a relative reference"))


def _check_target(target: object, path: str, out: list[Violation]) -> None:
    obj = _as_dict(target)
    if obj is None:
        out.append(Violation(path, "MUST", "link target MUST be a JSON object (4.2.2)"))
        return
    if "href" not in obj:
        out.append(Violation(path, "MUST", "link target object MUST contain href (4.2.3)"))
    else:
        _check_uri(obj["href"], f"{path}/href", out)
    for name, value in obj.items():
        at = f"{path}/{name}"
        if name == "href":
            continue
        if name in _STRING_ATTRS:
            if not isinstance(value, str):
                out.append(Violation(at, "MUST", f"{name} MUST be a string (4.2.4.1)"))
            continue
        items = _as_list(value)
        if items is None:
            out.append(Violation(at, "MUST", f"{name} MUST be an array (4.2.4.1/4.2.4.3)"))
            continue
        if name.endswith("*"):
            for i, item in enumerate(items):
                entry = _as_dict(item)
                if entry is None or not isinstance(entry.get("value"), str):
                    out.append(Violation(f"{at}/{i}", "MUST", "needs a string value (4.2.4.2)"))
                elif not isinstance(entry.get("language", ""), str):
                    out.append(Violation(f"{at}/{i}", "MUST", "language is a string (4.2.4.2)"))
        elif not all(isinstance(item, str) for item in items):
            out.append(Violation(at, "MUST", f"every {name} value MUST be a string"))


def validate_linkset(doc: object) -> list[Violation]:
    """Return every RFC 9264 Section 4.2 violation; an empty list means conformant."""
    out: list[Violation] = []
    root = _as_dict(doc)
    if root is None or set(root) != {"linkset"}:
        return [Violation("", "MUST", 'object MUST contain "linkset" as its sole member (4.2.1)')]
    contexts = _as_list(root["linkset"])
    if contexts is None:
        return [Violation("/linkset", "MUST", "linkset MUST be an array (4.2.1)")]
    for c_index, raw_ctx in enumerate(contexts):
        c_path = f"/linkset/{c_index}"
        ctx = _as_dict(raw_ctx)
        if ctx is None:
            out.append(Violation(c_path, "MUST", "link context MUST be a JSON object (4.2.2)"))
            continue
        for rel, value in ctx.items():
            r_path = f"{c_path}/{rel}"
            if rel == "anchor":
                _check_uri(value, r_path, out)
                continue
            if not (_REG_REL_TYPE.fullmatch(rel) or _is_absolute(rel)):
                out.append(Violation(r_path, "MUST", "rel MUST be reg-rel-type or a URI"))
            targets = _as_list(value)
            if targets is None:
                out.append(Violation(r_path, "MUST", "link targets MUST be an array (4.2.2)"))
                continue
            for t_index, target in enumerate(targets):
                _check_target(target, f"{r_path}/{t_index}", out)
    return out


def validate_api_catalog(doc: object) -> list[Violation]:
    """RFC 9264 rules plus RFC 9727 Section 4.1: at least one hyperlink."""
    out = validate_linkset(doc)
    if not out and not list(iter_links(doc)):
        out.append(Violation("/linkset", "MUST", "catalog MUST include hyperlinks (9727 4.1)"))
    return out


# ---------------------------------------------------------------------------
# The abstract link model (RFC 8288 Section 2) and both serializations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Link:
    context: str | None
    rel: str
    target: str
    attributes: tuple[tuple[str, str], ...] = ()


def iter_links(doc: object, base: str | None = None) -> Iterable[Link]:
    """Flatten a valid linkset+json document into RFC 8288 links."""
    root = _as_dict(doc) or {}
    for raw_ctx in _as_list(root.get("linkset")) or []:
        ctx = _as_dict(raw_ctx) or {}
        anchor = ctx.get("anchor", base)
        context = anchor if isinstance(anchor, str) else None
        for rel, value in ctx.items():
            if rel == "anchor":
                continue
            for raw_target in _as_list(value) or []:
                target = _as_dict(raw_target) or {}
                attrs: list[tuple[str, str]] = []
                for name, attr in target.items():
                    if name == "href":
                        continue
                    values = _as_list(attr)
                    if values is None:
                        attrs.append((name, str(attr)))
                    else:
                        attrs.extend((name, str(v)) for v in values)
                yield Link(context, rel.lower(), str(target.get("href", "")), tuple(attrs))


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_link_format(doc: object) -> str:
    """Serialize as application/linkset (RFC 9264 Section 4.1), one link-value per line."""
    lines: list[str] = []
    for link in iter_links(doc):
        if any(name.endswith("*") for name, _ in link.attributes):
            raise ValueError("RFC 8187 star attributes are out of scope for this lab")
        parts = [f"<{link.target}>", f"rel={_quote(link.rel)}"]
        if link.context is not None:
            parts.append(f"anchor={_quote(link.context)}")
        parts.extend(f"{name}={_quote(value)}" for name, value in link.attributes)
        line = "; ".join(parts)
        if not line.isascii():
            raise ValueError("RFC 9264 Section 4.1: application/linkset is ASCII only")
        lines.append(line)
    return ",\n".join(lines)


def to_link_header(doc: object) -> str:
    """RFC 9264 Section 4.1: newlines MUST be removed or replaced by SP for a Link field."""
    return to_link_format(doc).replace("\n", " ")


# RFC 8288 Appendix B, implemented over an index into the string.


def _skip_ows(value: str, i: int) -> int:
    while i < len(value) and value[i] in " \t":
        i += 1
    return i


def _parse_quoted(value: str, i: int) -> tuple[str, int]:
    """Appendix B.4: value[i] is DQUOTE; return (unescaped, index after closing DQUOTE)."""
    out: list[str] = []
    i += 1
    while i < len(value):
        ch = value[i]
        if ch == "\\":
            if i + 1 >= len(value):
                return "".join(out), i + 1
            out.append(value[i + 1])
            i += 2
        elif ch == '"':
            return "".join(out), i + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out), i


def _parse_params(value: str, i: int) -> tuple[list[tuple[str, str]], int]:
    """Appendix B.3: parse `; name=value` pairs until "," or end of input."""
    params: list[tuple[str, str]] = []
    while i < len(value):
        i = _skip_ows(value, i)
        if i >= len(value) or value[i] != ";":
            return params, i
        i = _skip_ows(value, i + 1)
        start = i
        while i < len(value) and value[i] not in " \t=;,":
            i += 1
        name = value[start:i].lower()
        i = _skip_ows(value, i)
        param_value = ""
        if i < len(value) and value[i] == "=":
            i = _skip_ows(value, i + 1)
            if i < len(value) and value[i] == '"':
                param_value, i = _parse_quoted(value, i)
            else:
                start = i
                while i < len(value) and value[i] not in ";,":
                    i += 1
                param_value = value[start:i].rstrip(" \t")
        params.append((name, param_value))
        i = _skip_ows(value, i)
        if i >= len(value) or value[i] == ",":
            return params, i
    return params, i


def _resolve(base_url: str | None, reference: str) -> str:
    """RFC 3986 Section 5 resolution against the representation URL, when known."""
    return urljoin(base_url, reference) if base_url else reference


def parse_link_header(value: str, base_url: str | None) -> list[Link]:
    """Appendix B.2. One deviation: a "," between link-values is consumed (see note)."""
    links: list[Link] = []
    i = 0
    while i < len(value):
        i = _skip_ows(value, i)
        while i < len(value) and value[i] in ", \t\n":
            i += 1
        if i >= len(value) or value[i] != "<":
            return links
        end = value.find(">", i + 1)
        if end == -1:
            return links
        target_string = value[i + 1 : end]
        params, i = _parse_params(value, end + 1)
        target = _resolve(base_url, target_string)
        rels = next((v for n, v in params if n == "rel"), "")
        anchor = next((v for n, v in params if n == "anchor"), None)
        context = _resolve(base_url, anchor) if anchor is not None else base_url
        attrs: list[tuple[str, str]] = []
        for name, param_value in params:
            if name in ("rel", "anchor"):
                continue
            if name in ("media", "title", "title*", "type") and any(n == name for n, _ in attrs):
                continue
            attrs.append((name, param_value))
        for rel in rels.split():
            links.append(Link(context, rel.lower(), target, tuple(attrs)))
    return links


# ---------------------------------------------------------------------------
# Proactive negotiation (RFC 9110 Section 12.5.1)
# ---------------------------------------------------------------------------


def _split_params(text: str) -> tuple[str, dict[str, str]]:
    head, *rest = text.split(";")
    params: dict[str, str] = {}
    for raw in rest:
        name, sep, val = raw.strip().partition("=")
        if sep:
            params[name.strip().lower()] = val.strip().strip('"')
    return head.strip().lower(), params


_QVALUE: Final = re.compile(r"0(\.[0-9]{0,3})?|1(\.0{0,3})?")


def negotiate(accept: str | None, available: Sequence[str] = AVAILABLE_TYPES) -> str | None:
    """Return the best available media type, or None when nothing is acceptable.

    Precedence follows RFC 9110 Section 12.5.1: the most specific matching range
    decides the quality (type/subtype;params > type/subtype > type/* > */*).
    A missing Accept means no preference (Section 12.4.1). Ties keep server order.
    Parameters are compared as whole strings, so a `profile` list (RFC 9264 Section 5)
    matches only when it names exactly the one profile this resource declares (lab policy).
    """
    if accept is None or not accept.strip():
        return available[0] if available else None
    ranges: list[tuple[str, str, dict[str, str], float]] = []
    for raw in accept.split(",")[:MAX_ACCEPT_RANGES]:
        if not raw.strip():
            continue
        media, params = _split_params(raw)
        q_text = params.pop("q", "1")
        if not _QVALUE.fullmatch(q_text):
            continue
        main, _, sub = media.partition("/")
        ranges.append((main, sub, params, float(q_text)))
    best: tuple[float, int, str] | None = None
    for order, candidate in enumerate(available):
        c_media, c_params = _split_params(candidate + f'; profile="{PROFILE_URI}"')
        c_main, _, c_sub = c_media.partition("/")
        match: tuple[int, float] | None = None
        for main, sub, params, q in ranges:
            if main not in ("*", c_main) or sub not in ("*", c_sub):
                continue
            if any(c_params.get(k) != v for k, v in params.items()):
                continue
            specificity = (main != "*") + (sub != "*") + len(params)
            if match is None or specificity > match[0]:
                match = (specificity, q)
        if match is not None and match[1] > 0:
            key = (match[1], -order, candidate)
            if best is None or key > best:
                best = key
    return None if best is None else best[2]


# ---------------------------------------------------------------------------
# The /.well-known/api-catalog resource (RFC 9727 Section 2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Representation:
    content_type: str
    body: bytes
    etag: str


def _representation(media_type: str, body: bytes) -> _Representation:
    digest = hashlib.sha256(body).hexdigest()[:32]
    return _Representation(f'{media_type}; profile="{PROFILE_URI}"', body, f'"{digest}"')


def _etag_matches(if_none_match: str, etag: str) -> bool:
    """RFC 9110 Section 13.1.2 with the weak comparison function (Section 8.8.3.2)."""
    if if_none_match.strip() == "*":
        return True
    opaque = etag.removeprefix("W/")
    return any(tag.strip().removeprefix("W/") == opaque for tag in if_none_match.split(","))


def create_app(catalog: Mapping[str, object], catalog_url: str, max_age: int = 300) -> Starlette:
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
    not_acceptable = json.dumps({"available": list(AVAILABLE_TYPES)}).encode()

    async def api_catalog(request: Request) -> Response:
        common = {"vary": "Accept", "link": self_link}
        is_head = request.method == "HEAD"
        chosen = negotiate(request.headers.get("accept"))
        if chosen is None:
            headers = {**common, "content-type": "application/json"}
            headers["content-length"] = str(len(not_acceptable))
            return Response(b"" if is_head else not_acceptable, 406, headers=headers)
        rep = reps[chosen]
        headers = {**common, "etag": rep.etag, "cache-control": f"max-age={max_age}"}
        if _etag_matches(request.headers.get("if-none-match", ""), rep.etag):
            return Response(status_code=304, headers=headers)
        headers["content-type"] = rep.content_type
        headers["content-length"] = str(len(rep.body))
        return Response(b"" if is_head else rep.body, 200, headers=headers)

    # Starlette's Route adds HEAD whenever GET is listed (starlette 1.10.0 routing.py).
    return Starlette(routes=[Route(WELL_KNOWN_PATH, api_catalog, methods=["GET"])])


def create_redirect_app(canonical_url: str) -> Starlette:
    """RFC 9727 Section 5.1: a non-canonical domain redirects to the canonical well-known URI.

    308 is a method-preserving redirect (RFC 9110 Section 15.4), so a HEAD probe stays a
    HEAD; the redirect still carries the Link header that Section 2 requires of a HEAD response.
    """
    _require_absolute_https(canonical_url, "canonical_url")
    headers = {"location": canonical_url, "link": f'<{canonical_url}>; rel="api-catalog"'}

    async def redirect(_request: Request) -> Response:
        return Response(status_code=308, headers=headers)

    return Starlette(routes=[Route(WELL_KNOWN_PATH, redirect, methods=["GET"])])


# ---------------------------------------------------------------------------
# A bounded client: follow nested api-catalog links (RFC 9727 Section 4.3)
# ---------------------------------------------------------------------------


class FetchError(Exception):
    """Raised by a fetch callable when a catalog cannot be retrieved or decoded."""


@dataclass(frozen=True, slots=True)
class Redirect:
    """Returned by a fetch callable for a 3xx response; the crawler decides whether to follow."""

    location: str


@dataclass(frozen=True, slots=True)
class CrawlResult:
    catalogs: tuple[str, ...]
    apis: tuple[str, ...]
    rejected: tuple[tuple[str, str], ...]


def crawl(
    root: str,
    fetch: Callable[[str], object],
    *,
    max_catalogs: int = 16,
    max_depth: int = 3,
    max_redirects: int = 2,
    allowed_hosts: frozenset[str] | None = None,
) -> CrawlResult:
    """Breadth-first walk of nested catalogs with a visited set and hard budgets.

    `fetch` must not follow redirects itself: it returns a `Redirect`, and every hop is
    checked against the same https and host policy as a link before it is fetched.
    """
    root = urldefrag(root).url
    seen: set[str] = {root}
    queue: deque[tuple[str, int]] = deque([(root, 0)])
    catalogs: list[str] = []
    apis: dict[str, None] = {}
    rejected: list[tuple[str, str]] = []

    def admissible(uri: str) -> str | None:
        parts = urlsplit(uri)
        if parts.scheme != "https":
            return "not https (RFC 9727 Section 8)"
        if allowed_hosts is not None and parts.hostname not in allowed_hosts:
            return "host not allowed (RFC 9264 Section 9 third-party links)"
        return None

    def retrieve(url: str) -> tuple[str, object] | None:
        """Fetch url, following admissible redirects; return (final URL, document)."""
        hops = 0
        while True:
            try:
                doc = fetch(url)
            except FetchError as exc:
                rejected.append((url, f"fetch failed: {exc}"))
                return None
            if not isinstance(doc, Redirect):
                return url, doc
            target = urldefrag(urljoin(url, doc.location)).url
            hops += 1
            reason = "redirect limit" if hops > max_redirects else admissible(target)
            if reason is not None:
                rejected.append((target, reason))
                return None
            if target in seen:
                return None
            seen.add(target)
            url = target

    while queue:
        url, depth = queue.popleft()
        reason = admissible(url)
        if reason is None and len(catalogs) >= max_catalogs:
            reason = "catalog budget exhausted"
        if reason is not None:
            rejected.append((url, reason))
            continue
        retrieved = retrieve(url)
        if retrieved is None:
            continue
        url, doc = retrieved
        if any(v.level == "MUST" for v in validate_api_catalog(doc)):
            rejected.append((url, "invalid linkset"))
            continue
        catalogs.append(url)
        for link in iter_links(doc, base=url):
            target = urljoin(url, link.target)
            if link.rel == "api-catalog":
                target = urldefrag(target).url
                if target in seen:
                    continue
                seen.add(target)
                if depth + 1 > max_depth:
                    rejected.append((target, "depth limit"))
                else:
                    queue.append((target, depth + 1))
                continue
            if link.rel not in ("item", *_SERVICE_RELS):
                continue
            # iter_links was given a base, so every link here has a context.
            api = target if link.rel == "item" else urljoin(url, link.context or "")
            api_reason = admissible(api)
            if api_reason is not None:
                if (api, api_reason) not in rejected:
                    rejected.append((api, api_reason))
            else:
                apis.setdefault(api, None)
    return CrawlResult(tuple(catalogs), tuple(apis), tuple(rejected))
