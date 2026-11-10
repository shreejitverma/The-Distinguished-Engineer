"""Content-coding-aware strong ETags: the gzip/identity collision and two fixes.

Starlette 1.10.0 `GZipMiddleware` sets `Content-Encoding: gzip` but leaves the origin's
strong `ETag` untouched, so two different byte sequences share one strong validator.
RFC 9110 Section 8.8.1 calls such a shared validator weak; this module reproduces the
damage (range resume and RFC 2616-style cache updates) and implements two fixes:
`suffix` (distinct strong tag per coding) and `weaken` (mark the tag `W/`).
"""

from __future__ import annotations

import gzip
import re
import zlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

if TYPE_CHECKING:
    from pathlib import Path

    import httpx
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

Policy = Literal["starlette", "suffix", "weaken"]
GZIP_SUFFIX = "-gzip"
PATHSEND = "http.response.pathsend"
_ETAG_RE = re.compile(r'\s*(W/)?"([^"]*)"\s*(?:,|$)')


@dataclass(frozen=True)
class EntityTag:
    """An entity-tag per RFC 9110 Section 8.8.3: opaque value plus weakness flag."""

    opaque: str
    weak: bool = False

    def __str__(self) -> str:
        return f'{"W/" if self.weak else ""}"{self.opaque}"'


def parse_etag_list(value: str) -> list[EntityTag]:
    """Parse `#entity-tag`; commas inside quotes belong to the opaque-tag."""
    tags: list[EntityTag] = []
    pos = 0
    while pos < len(value.rstrip()):
        match = _ETAG_RE.match(value, pos)
        if match is None:
            return []  # malformed list: treat as no validators, never as a match
        tags.append(EntityTag(opaque=match.group(2), weak=match.group(1) is not None))
        pos = match.end()
    return tags


def accepts_gzip(accept_encoding: str | None) -> bool:
    """True when gzip is acceptable with a non-zero qvalue (RFC 9110 Section 12.5.3).

    An absent header permits any coding, but servers are free to pick identity; this
    function returns False then, because identity is always the safe choice.
    """
    if accept_encoding is None:
        return False
    explicit: dict[str, float] = {}
    for item in accept_encoding.split(","):
        coding, _, params = item.strip().partition(";")
        coding = coding.strip().lower()
        if not coding:
            continue
        qvalue = 1.0
        for param in params.split(";"):
            name, _, raw = param.strip().partition("=")
            if name.strip().lower() == "q":
                try:
                    qvalue = float(raw.strip())
                except ValueError:
                    qvalue = 0.0
        if coding == "x-gzip":  # RFC 9110 Section 8.4.1.3
            coding = "gzip"
        explicit[coding] = max(qvalue, explicit.get(coding, 0.0))
    if "gzip" in explicit:
        return explicit["gzip"] > 0
    return explicit.get("*", 0.0) > 0


def gzip_bytes(body: bytes, level: int) -> bytes:
    """Deterministic gzip member: `mtime=0`, so equal inputs give equal outputs."""
    return gzip.compress(body, compresslevel=level, mtime=0)


class EncodingAwareGZipMiddleware:
    """Buffered gzip middleware that keeps every strong ETag unique per content coding.

    `suffix`: `"x"` -> `"x-gzip"` on compressed 200s; inbound `If-None-Match` tags ending in
    `-gzip` are mapped back so the origin can still answer 304, and the 304 is re-tagged.
    `weaken`: `"x"` -> `W/"x"`; If-None-Match keeps working through weak comparison,
    while `If-Range` (strong comparison only) can never select a range of the gzip bytes.
    Every 200 without `Content-Encoding` on the gzip path is compressed, so every 304 on that
    path (from `If-None-Match`, `*`, or `If-Modified-Since`) carries the encoded tag that the
    200 would have carried (RFC 9110 Section 15.4.5).
    """

    def __init__(self, app: ASGIApp, policy: Literal["suffix", "weaken"], level: int = 6) -> None:
        self.app = app
        self.policy = policy
        self.level = level

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Headers(scope=scope)
        if not accepts_gzip(request.get("accept-encoding")):
            await self.app(scope, receive, _vary_only(send))
            return
        scope = _without_pathsend(scope)  # a path handed to the server cannot be compressed here
        if self.policy == "suffix":
            scope = _strip_inbound_suffix(scope, request.get("if-none-match"))
        start: Message = {}
        chunks: list[bytes] = []

        async def wrapped(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                headers.add_vary_header("Accept-Encoding")
                if message["status"] == 200 and "content-encoding" not in headers:
                    start = message  # buffer until the body is complete
                    return
                if message["status"] == 304:
                    self._retag_304(headers)
                await send(message)
            elif message["type"] == "http.response.body" and start:
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    await self._send_compressed(start, b"".join(chunks), send)
            else:
                await send(message)

        await self.app(scope, receive, wrapped)

    async def _send_compressed(self, start: Message, body: bytes, send: Send) -> None:
        compressed = gzip_bytes(body, self.level)
        headers = MutableHeaders(raw=start["headers"])
        headers["content-encoding"] = "gzip"
        headers["content-length"] = str(len(compressed))
        etag = parse_etag_list(headers.get("etag", ""))
        if len(etag) == 1:
            headers["etag"] = str(self._encoded_tag(etag[0]))
        await send(start)
        await send({"type": "http.response.body", "body": compressed, "more_body": False})

    def _encoded_tag(self, tag: EntityTag) -> EntityTag:
        if self.policy == "weaken":
            return EntityTag(tag.opaque, weak=True)
        return EntityTag(tag.opaque + GZIP_SUFFIX, weak=tag.weak)

    def _retag_304(self, headers: MutableHeaders) -> None:
        etag = parse_etag_list(headers.get("etag", ""))
        if len(etag) == 1:
            headers["etag"] = str(self._encoded_tag(etag[0]))


def _strip_inbound_suffix(scope: Scope, if_none_match: str | None) -> Scope:
    """Map `"x-gzip"` -> `"x"` for the origin; drop tags that cannot name the gzip form."""
    if if_none_match is None or if_none_match.strip() == "*":
        return scope
    kept: list[str] = []
    for tag in parse_etag_list(if_none_match):
        if tag.opaque.endswith(GZIP_SUFFIX):
            base = tag.opaque.removesuffix(GZIP_SUFFIX)
            kept.append(str(EntityTag(base, weak=tag.weak)))
    raw = [(k, v) for k, v in scope["headers"] if k.lower() != b"if-none-match"]
    if kept:
        raw.append((b"if-none-match", ", ".join(kept).encode("latin-1")))
    return {**scope, "headers": raw}


def _without_pathsend(scope: Scope) -> Scope:
    """Hide `http.response.pathsend` so the origin streams the file body through this layer."""
    extensions = {k: v for k, v in scope.get("extensions", {}).items() if k != PATHSEND}
    return {**scope, "extensions": extensions}


def _vary_only(send: Send) -> Send:
    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            MutableHeaders(raw=message.setdefault("headers", [])).add_vary_header("Accept-Encoding")
        await send(message)

    return wrapped


def build_static_app(directory: Path, policy: Policy) -> Starlette:
    """`/static` served by Starlette `StaticFiles` behind the chosen gzip layer."""
    middleware = (
        Middleware(GZipMiddleware, minimum_size=500)
        if policy == "starlette"
        else Middleware(EncodingAwareGZipMiddleware, policy=policy)
    )
    return Starlette(
        routes=[Mount("/static", app=StaticFiles(directory=directory))], middleware=[middleware]
    )


@dataclass(frozen=True)
class ResumeOutcome:
    """What a resuming downloader saw and what it reassembled."""

    statuses: tuple[int, ...]
    first_etag: str
    assembled: bytes
    decoded: bytes | None
    error: str | None


async def resume_download(
    client: httpx.AsyncClient,
    url: str,
    *,
    cut: int,
    naive_if_range: bool = False,
) -> ResumeOutcome:
    """Fetch gzip, keep `cut` raw bytes, then resume with `Range` + `If-Range`.

    A compliant client resumes only with a strong tag (RFC 9110 Section 13.1.5);
    `naive_if_range=True` sends the tag even when it is weak, to probe the server.
    """
    headers = {"Accept-Encoding": "gzip"}
    async with client.stream("GET", url, headers=headers) as first:
        raw = b"".join([chunk async for chunk in first.aiter_raw()])
    prefix, etag, coding = (
        raw[:cut],
        first.headers.get("etag", ""),
        first.headers.get("content-encoding"),
    )
    parsed = parse_etag_list(etag)
    statuses = [first.status_code]
    resume_headers = dict(headers)
    if len(parsed) == 1 and (naive_if_range or not parsed[0].weak):
        resume_headers |= {"Range": f"bytes={cut}-", "If-Range": etag}
    async with client.stream("GET", url, headers=resume_headers) as second:
        rest = b"".join([chunk async for chunk in second.aiter_raw()])
    statuses.append(second.status_code)
    if second.status_code == 206:
        assembled = prefix + rest  # trusts that one strong tag means one byte sequence
    else:
        assembled, coding = rest, second.headers.get("content-encoding")
    try:
        decoded = gzip.decompress(assembled) if coding == "gzip" else assembled
    except (OSError, EOFError, zlib.error) as exc:
        return ResumeOutcome(tuple(statuses), etag, assembled, None, type(exc).__name__)
    return ResumeOutcome(tuple(statuses), etag, assembled, decoded, None)


@dataclass(frozen=True)
class RawResponse:
    """Status, lowercased headers, and undecoded body bytes as they left the origin."""

    status_code: int
    headers: dict[str, str]
    body: bytes


@dataclass
class StoredResponse:
    """One cached variant: the Vary-nominated request values and the raw response."""

    vary_values: dict[str, str | None]
    headers: dict[str, str]
    body: bytes


@dataclass
class VariantCache:
    """A shared cache for one URI that always revalidates (as if `no-cache`).

    `rfc2616`: a 304 updates any entry whose tag matches (RFC 2616 Section 13.6).
    `rfc9111`: only entries the request could have chosen via Vary (RFC 9111 4.3.4).
    """

    client: httpx.AsyncClient
    url: str
    mode: Literal["rfc2616", "rfc9111"]
    entries: list[StoredResponse] = field(default_factory=list[StoredResponse])
    origin_requests: int = 0

    async def get(self, accept_encoding: str) -> StoredResponse:
        request = {"accept-encoding": accept_encoding}
        known = [e.headers["etag"] for e in self.entries if "etag" in e.headers]
        conditional = dict(request)
        if known:  # RFC 9111 Section 4.3.1 allows tags of variants it cannot choose
            conditional["if-none-match"] = ", ".join(known)
        response = await self._fetch(conditional)
        if response.status_code == 304:
            chosen = self._select_for_update(request, response.headers.get("etag", ""))
            if chosen is not None:
                return chosen
            response = await self._fetch(request)  # MUST NOT update: refetch in full
        return self._store(request, response)

    async def _fetch(self, headers: dict[str, str]) -> RawResponse:
        self.origin_requests += 1
        async with self.client.stream("GET", self.url, headers=headers) as response:
            body = b"".join([chunk async for chunk in response.aiter_raw()])
        lowered = {k.lower(): v for k, v in response.headers.items()}
        return RawResponse(response.status_code, lowered, body)

    def _select_for_update(self, request: dict[str, str], etag: str) -> StoredResponse | None:
        tags = parse_etag_list(etag)
        if len(tags) != 1:
            return None
        new = tags[0]
        candidates = self.entries
        if self.mode == "rfc9111":
            candidates = [e for e in self.entries if _vary_matches(e, request)]
        matches: list[StoredResponse] = []
        for entry in candidates:
            stored = parse_etag_list(entry.headers.get("etag", ""))
            if not stored or stored[0].opaque != new.opaque:
                continue
            if new.weak or not stored[0].weak:  # strong 304 tag needs a strong stored tag
                matches.append(entry)
        return matches[-1] if matches else None  # most recent, RFC 9111 Section 4.3.4

    def _store(self, request: dict[str, str], response: RawResponse) -> StoredResponse:
        vary = [v.strip().lower() for v in response.headers.get("vary", "").split(",") if v]
        entry = StoredResponse(
            vary_values={name: request.get(name) for name in vary},
            headers=response.headers,
            body=response.body,
        )
        self.entries = [e for e in self.entries if e.vary_values != entry.vary_values]
        self.entries.append(entry)
        return entry


def _vary_matches(entry: StoredResponse, request: dict[str, str]) -> bool:
    return all(request.get(name) == value for name, value in entry.vary_values.items())
