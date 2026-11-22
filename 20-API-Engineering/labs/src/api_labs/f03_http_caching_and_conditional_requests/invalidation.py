"""Shared-cache invalidation on unsafe methods (RFC 9111 Section 4.4) and tag purging.

A deterministic in-memory shared cache wrapped around an ASGI origin:
- `GET` responses that carry `max-age` or `s-maxage` are stored under the normalized target URI;
- requests with any other method are written through to the origin (RFC 9111 Section 4);
- a non-error (`2xx` or `3xx`) response to an unsafe or unknown method invalidates the target URI
  and the same-origin `Location` and `Content-Location` URIs (RFC 9111 Section 4.4);
- stored responses are indexed by Fastly-style `Surrogate-Key` tags and by RFC 9875 `Cache-Groups`,
  so one purge or one `Cache-Group-Invalidation` signal fans out to many URIs.
Freshness lifetime, `Vary`, and `HEAD` reuse are out of scope; every stored entry is fresh until
invalidated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

# RFC 9110 Section 9.2.1: the safe methods it defines. Method tokens are case-sensitive
# (RFC 9110 Section 9.1), so `get` is an unknown method and therefore treated as unsafe.
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
DEFAULT_PORTS: Mapping[str, int] = {"http": 80, "https": 443}

# Fastly `Surrogate-Key` limits (Fastly HTTP reference, as of 2026-11).
SURROGATE_KEY_MAX_KEY_BYTES = 1024
SURROGATE_KEY_MAX_FIELD_BYTES = 16384

type Origin = tuple[str, str, int]
type Headers = list[tuple[bytes, bytes]]

_SF_KEY = re.compile(r"[a-z*][a-z0-9_\-.*]*")
_SF_TOKEN = re.compile(r"[A-Za-z*][!#$%&'*+\-.^_`|~0-9A-Za-z:/]*")
_SF_NUMBER = re.compile(r"-?[0-9]{1,15}(\.[0-9]{1,3})?")
_STRIP_FROM_CLIENT = frozenset({b"surrogate-key", b"cache-status"})


def origin_of(uri: str) -> Origin:
    """Return the RFC 9110 Section 4.3.1 origin triple (scheme, host, port) of an absolute URI."""
    parts = urlsplit(uri)
    scheme = parts.scheme.lower()
    host = parts.hostname  # urlsplit lowercases the host and strips IPv6 brackets
    if scheme not in DEFAULT_PORTS or not host:
        raise ValueError(f"not an absolute http(s) URI: {uri!r}")
    port = parts.port if parts.port is not None else DEFAULT_PORTS[scheme]
    return scheme, host, port


def normalize_uri(uri: str) -> str:
    """Cache key: lowercase scheme and host, default port elided, no fragment (RFC 9110 7.1)."""
    scheme, host, port = origin_of(uri)
    parts = urlsplit(uri)
    authority = f"[{host}]" if ":" in host else host
    if port != DEFAULT_PORTS[scheme]:
        authority = f"{authority}:{port}"
    query = f"?{parts.query}" if parts.query else ""
    return f"{scheme}://{authority}{parts.path or '/'}{query}"


def invalidation_targets(
    method: str, target_uri: str, status: int, headers: Mapping[str, str]
) -> tuple[str, ...]:
    """URIs a cache invalidates after forwarding `method target_uri` and receiving `status`.

    `headers` maps lowercase response field names to values.
    """
    if method in SAFE_METHODS or not 200 <= status < 400:
        return ()
    target = normalize_uri(target_uri)
    target_origin = origin_of(target)
    targets = [target]
    for name in ("location", "content-location"):
        value = headers.get(name)
        if value is None:
            continue
        candidate = urljoin(target, value.strip())
        try:
            same_origin = origin_of(candidate) == target_origin
        except ValueError:
            continue
        # RFC 9111 Section 4.4: MUST NOT invalidate a URI whose origin differs from the target's.
        if same_origin and (key := normalize_uri(candidate)) not in targets:
            targets.append(key)
    return tuple(targets)


def parse_surrogate_key(value: str) -> tuple[str, ...]:
    """Split a space-separated `Surrogate-Key` value, applying Fastly's documented limits.

    When a key exceeds 1024 bytes or the field exceeds 16384 bytes, the key being parsed and
    every key after it are ignored. Duplicates collapse to one key; keys are case-sensitive.
    """
    keys: dict[str, None] = {}
    for match in re.finditer(r"[^ \t]+", value):
        token = match.group()
        end_bytes = len(value[: match.end()].encode("latin-1"))
        if len(token.encode("latin-1")) > SURROGATE_KEY_MAX_KEY_BYTES:
            break
        if end_bytes > SURROGATE_KEY_MAX_FIELD_BYTES:
            break
        keys[token] = None
    return tuple(keys)


class _SFParser:
    """RFC 9651 Section 4.2 List parser restricted to String members (RFC 9875 field type)."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def fail(self) -> ValueError:
        return ValueError(f"invalid sf-list at offset {self.pos}: {self.text!r}")

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def skip(self, chars: str) -> None:
        while self.peek() and self.peek() in chars:
            self.pos += 1

    def string(self) -> str:
        if self.peek() != '"':
            raise self.fail()
        self.pos += 1
        out: list[str] = []
        while self.pos < len(self.text):
            char = self.text[self.pos]
            self.pos += 1
            if char == "\\":
                nxt = self.peek()
                if nxt not in ('"', "\\"):
                    raise self.fail()
                out.append(nxt)
                self.pos += 1
            elif char == '"':
                return "".join(out)
            elif not 0x20 <= ord(char) <= 0x7E:
                raise self.fail()
            else:
                out.append(char)
        raise self.fail()

    def regex(self, pattern: re.Pattern[str]) -> None:
        match = pattern.match(self.text, self.pos)
        if match is None:
            raise self.fail()
        self.pos = match.end()

    def parameters(self) -> None:
        """Consume and discard parameters: RFC 9875 says unrecognised ones are ignored."""
        while self.peek() == ";":
            self.pos += 1
            self.skip(" ")
            self.regex(_SF_KEY)
            if self.peek() != "=":
                continue
            self.pos += 1
            first = self.peek()
            if first == '"':
                self.string()
            elif first == "?":
                if self.text[self.pos + 1 : self.pos + 2] not in ("0", "1"):
                    raise self.fail()
                self.pos += 2
            elif first == "-" or first.isdigit():
                self.regex(_SF_NUMBER)
            else:
                self.regex(_SF_TOKEN)

    def string_list(self) -> tuple[str, ...]:
        members: list[str] = []
        self.skip(" ")
        while self.pos < len(self.text):
            members.append(self.string())
            self.parameters()
            self.skip(" \t")
            if self.pos == len(self.text):
                break
            if self.peek() != ",":
                raise self.fail()
            self.pos += 1
            self.skip(" \t")
            if self.pos == len(self.text):
                raise self.fail()  # trailing comma
        return tuple(dict.fromkeys(members))


def parse_cache_groups(value: str) -> tuple[str, ...]:
    """Parse `Cache-Groups` or `Cache-Group-Invalidation` (RFC 9875 Sections 2 and 3).

    On any parse failure the whole field is ignored (RFC 9651 Section 4.2), so this returns ().
    """
    try:
        return _SFParser(value).string_list()
    except ValueError:
        return ()


@dataclass(slots=True)
class StoredResponse:
    """One stored `GET` response and the secondary keys it is indexed under."""

    status: int
    headers: Headers
    body: bytes
    origin: Origin
    surrogate_keys: frozenset[str]
    groups: frozenset[str]
    invalid: bool = False


class SharedCache:
    """URI-keyed store with a Surrogate-Key index, a Cache-Groups index, and refill guards."""

    def __init__(self) -> None:
        self._store: dict[str, StoredResponse] = {}
        self._by_key: dict[str, set[str]] = {}
        self._by_group: dict[tuple[Origin, str], set[str]] = {}
        self._generation: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, uri: str) -> bool:
        return uri in self._store

    def uris(self) -> tuple[str, ...]:
        return tuple(sorted(self._store))

    def get(self, uri: str) -> StoredResponse | None:
        return self._store.get(uri)

    def generation(self, uri: str) -> int:
        """Invalidation counter for `uri`; a refill started at generation g is stale if it moved."""
        return self._generation.get(uri, 0)

    def index_sizes(self) -> tuple[int, int]:
        """Number of live Surrogate-Key and Cache-Groups index entries (detects index leaks)."""
        return len(self._by_key), len(self._by_group)

    def put(self, uri: str, entry: StoredResponse, *, if_generation: int | None = None) -> bool:
        """Store `entry` unless an invalidation happened since `if_generation` was read."""
        if if_generation is not None and if_generation != self.generation(uri):
            return False
        self._unlink(uri)
        self._store[uri] = entry
        for key in entry.surrogate_keys:
            self._by_key.setdefault(key, set()).add(uri)
        for group in entry.groups:
            self._by_group.setdefault((entry.origin, group), set()).add(uri)
        return True

    def invalidate(self, uri: str, *, soft: bool = False) -> bool:
        """Hard: remove the entry. Soft: mark it invalid so the next use must go forward."""
        self._generation[uri] = self.generation(uri) + 1
        entry = self._store.get(uri)
        if entry is None:
            return False
        if soft:
            entry.invalid = True
        else:
            self._unlink(uri)
        return True

    def purge_surrogate_key(self, key: str, *, soft: bool = False) -> tuple[str, ...]:
        """Invalidate every stored response tagged with `key`; returns the URIs touched."""
        uris = tuple(sorted(self._by_key.get(key, ())))
        for uri in uris:
            self.invalidate(uri, soft=soft)
        return uris

    def invalidate_groups(self, origin: Origin, groups: tuple[str, ...]) -> tuple[str, ...]:
        """Invalidate same-origin members of `groups` (RFC 9875 Section 2.1); never cascades."""
        uris = sorted({uri for g in groups for uri in self._by_group.get((origin, g), ())})
        for uri in uris:
            self.invalidate(uri)
        return tuple(uris)

    def groups_of(self, uri: str) -> tuple[str, ...]:
        entry = self._store.get(uri)
        return tuple(sorted(entry.groups)) if entry is not None else ()

    def _unlink(self, uri: str) -> None:
        entry = self._store.pop(uri, None)
        if entry is None:
            return
        for key in entry.surrogate_keys:
            members = self._by_key[key]
            members.discard(uri)
            if not members:
                del self._by_key[key]
        for group in entry.groups:
            gkey = (entry.origin, group)
            members = self._by_group[gkey]
            members.discard(uri)
            if not members:
                del self._by_group[gkey]


def target_uri(scope: Scope) -> str:
    """Reconstruct the absolute target URI (RFC 9110 Section 7.1) from an ASGI HTTP scope."""
    host = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"host"), None)
    if host is None:
        server_host, server_port = scope["server"]
        host = f"{server_host}:{server_port}"
    raw_path: bytes = scope.get("raw_path") or scope["path"].encode("utf-8")
    query: bytes = scope.get("query_string", b"")
    suffix = f"?{query.decode('latin-1')}" if query else ""
    return f"{scope['scheme']}://{host}{raw_path.decode('latin-1')}{suffix}"


def _field(headers: Headers, name: bytes) -> str | None:
    """Combine all field lines named `name` with ", " (RFC 9110 Section 5.3)."""
    values = [v.decode("latin-1") for k, v in headers if k.lower() == name]
    return ", ".join(values) if values else None


def _storable(status: int, headers: Headers) -> bool:
    directives = {
        d.strip().split("=", 1)[0].lower()
        for d in (_field(headers, b"cache-control") or "").split(",")
    }
    if directives & {"no-store", "private"}:
        return False
    return status == 200 and bool(directives & {"max-age", "s-maxage"})


async def _forward(app: ASGIApp, scope: Scope, receive: Receive) -> tuple[int, Headers, bytes]:
    """Run the origin app to completion and buffer its response."""
    start: Message | None = None
    chunks: list[bytes] = []

    async def capture(message: Message) -> None:
        nonlocal start
        if message["type"] == "http.response.start":
            start = message
        elif message["type"] == "http.response.body":
            chunks.append(bytes(message.get("body", b"")))

    await app(scope, receive, capture)
    if start is None:
        raise RuntimeError("origin returned without sending http.response.start")
    headers: Headers = [(bytes(k), bytes(v)) for k, v in start.get("headers", [])]
    return int(start["status"]), headers, b"".join(chunks)


class InvalidatingCache:
    """ASGI shared-cache middleware: store `GET`, write through unsafe methods, invalidate."""

    def __init__(
        self,
        app: ASGIApp,
        cache: SharedCache | None = None,
        *,
        name: str = "lab",
        group_fanout: bool = True,
        guard_refill: bool = True,
    ) -> None:
        self.app = app
        self.cache = cache if cache is not None else SharedCache()
        self.name = name
        self.group_fanout = group_fanout
        self.guard_refill = guard_refill

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method: str = scope["method"]
        target = target_uri(scope)
        key = normalize_uri(target)
        if method == "GET":
            stored = self.cache.get(key)
            if stored is not None and not stored.invalid:
                await self._reply(send, stored.status, stored.headers, stored.body, "hit")
                return
            generation = self.cache.generation(key)
            status, headers, body = await _forward(self.app, scope, receive)
            stored_now = _storable(status, headers) and self.cache.put(
                key,
                StoredResponse(
                    status=status,
                    headers=headers,
                    body=body,
                    origin=origin_of(key),
                    surrogate_keys=frozenset(
                        parse_surrogate_key(_field(headers, b"surrogate-key") or "")
                    ),
                    groups=frozenset(parse_cache_groups(_field(headers, b"cache-groups") or "")),
                ),
                if_generation=generation if self.guard_refill else None,
            )
            reason = "stale" if stored is not None else "uri-miss"
            await self._reply(
                send, status, headers, body, f"fwd={reason}{'; stored' if stored_now else ''}"
            )
            return
        status, headers, body = await _forward(self.app, scope, receive)
        if method not in SAFE_METHODS:
            self._invalidate_after_unsafe(method, target, status, headers)
        await self._reply(send, status, headers, body, "fwd=method")

    def _invalidate_after_unsafe(
        self, method: str, target: str, status: int, headers: Headers
    ) -> None:
        fields = {
            name: value
            for name in ("location", "content-location")
            if (value := _field(headers, name.encode())) is not None
        }
        uris = invalidation_targets(method, target, status, fields)
        if not uris:
            return
        origin = origin_of(uris[0])
        group_signal = parse_cache_groups(_field(headers, b"cache-group-invalidation") or "")
        fanout: set[str] = set(group_signal)
        for uri in uris:
            if self.group_fanout:
                fanout.update(self.cache.groups_of(uri))
            self.cache.invalidate(uri)
        if fanout:
            self.cache.invalidate_groups(origin, tuple(sorted(fanout)))

    async def _reply(
        self, send: Send, status: int, headers: Headers, body: bytes, detail: str
    ) -> None:
        out = [(k, v) for k, v in headers if k.lower() not in _STRIP_FROM_CLIENT]
        out.append((b"cache-status", f"{self.name}; {detail}".encode("latin-1")))
        await send({"type": "http.response.start", "status": status, "headers": out})
        await send({"type": "http.response.body", "body": body})
