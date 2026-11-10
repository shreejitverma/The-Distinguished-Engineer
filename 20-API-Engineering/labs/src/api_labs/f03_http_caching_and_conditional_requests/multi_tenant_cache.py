"""Shared-cache rules for authenticated and multi-tenant API responses.

A minimal RFC 9111 shared cache (ASGI middleware), an edge gateway that turns a verified
bearer token into an `x-tenant-id` request header, and a Starlette origin whose cache policy
is selectable.  Together they show each way a shared cache leaks one principal's response to
another, and which header, key, or layer order prevents it.

Scope of the cache: `GET` and `200` only, explicit freshness only (no heuristic freshness),
no revalidation, whole-response buffering.  The qualified `private="field"` form is treated
as unqualified, which RFC 9111 Section 5.2.2.7 notes is how many caches behave.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

Directives = dict[str, str | None]

# RFC 9111 Section 3.5: directives that let a shared cache reuse a response to a request
# that carried `Authorization`.
AUTHORIZATION_OVERRIDES = frozenset({"public", "s-maxage", "must-revalidate"})


def split_list(value: str) -> list[str]:
    """Split a comma-separated field value, keeping commas inside quoted strings."""
    members: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for char in value:
        if escaped:
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            members.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    members.append("".join(current).strip())
    return [member for member in members if member]


def parse_cache_control(value: str) -> Directives:
    """Parse `Cache-Control` into lowercase directive names; the first occurrence wins."""
    directives: Directives = {}
    for member in split_list(value):
        name, sep, raw = member.partition("=")
        argument = raw.strip() if sep else None
        if argument is not None and len(argument) >= 2 and argument[0] == argument[-1] == '"':
            argument = argument[1:-1]
        directives.setdefault(name.strip().lower(), argument)
    return directives


def delta_seconds(argument: str | None) -> int | None:
    """RFC 9111 Section 1.2.2: `1*DIGIT`, capped at 2**31; anything else is invalid."""
    if argument is None or not argument.isascii() or not argument.isdigit():
        return None
    return min(int(argument), 2**31)


def shared_lifetime(directives: Mapping[str, str | None]) -> int:
    """Freshness lifetime for a shared cache: `s-maxage` overrides `max-age` (Section 5.2.2.10)."""
    for name in ("s-maxage", "max-age"):
        if name in directives:
            seconds = delta_seconds(directives[name])
            if seconds is not None:
                return seconds
    return 0


def refusal_reason(
    *, status: int, request_has_authorization: bool, directives: Mapping[str, str | None]
) -> str | None:
    """Return why a shared cache must not store this response, or None if it may."""
    if status != 200:
        return "status"
    if "no-store" in directives:
        return "no-store"
    if "private" in directives:
        return "private"
    if request_has_authorization and not AUTHORIZATION_OVERRIDES & directives.keys():
        return "authorization"
    if shared_lifetime(directives) <= 0:
        return "no-freshness"
    return None


def combined_headers(raw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    """Combine repeated field lines with ", " (RFC 9110 Section 5.3); names are lowercase."""
    combined: dict[str, str] = {}
    for name, value in raw:
        key = name.decode("latin-1").lower()
        text = value.decode("latin-1").strip()
        combined[key] = f"{combined[key]}, {text}" if key in combined else text
    return combined


def effective_directives(
    response_headers: Mapping[str, str], target_fields: tuple[str, ...]
) -> Directives:
    """RFC 9213 Section 2.2: the first valid, non-empty targeted field replaces Cache-Control."""
    for name in target_fields:
        parsed = parse_cache_control(response_headers.get(name, ""))
        if parsed:
            return parsed
    return parse_cache_control(response_headers.get("cache-control", ""))


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes
    stored_at: float
    lifetime: int
    vary: tuple[str, ...]
    selecting: tuple[str | None, ...]
    tags: frozenset[str]

    def is_fresh(self, now: float) -> bool:
        return now - self.stored_at < self.lifetime

    def matches(self, request_headers: Mapping[str, str]) -> bool:
        """RFC 9111 Section 4.1: every nominated field must match; `*` never matches."""
        if "*" in self.vary:
            return False
        return all(
            request_headers.get(name) == value
            for name, value in zip(self.vary, self.selecting, strict=True)
        )


@dataclass(slots=True)
class SharedCache:
    """A shared cache keyed on path and query, then on the response's `Vary` fields."""

    app: ASGIApp
    clock: Callable[[], float]
    target_fields: tuple[str, ...] = ()
    name: str = "lab"
    entries: dict[tuple[str, bytes], list[StoredResponse]] = field(default_factory=lambda: {})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "GET":
            await self.app(scope, receive, send)
            return
        request_headers = combined_headers(scope["headers"])
        key = (scope["path"], scope["query_string"])
        now = self.clock()
        candidates = self.entries.get(key, [])
        for entry in reversed(candidates):
            if entry.is_fresh(now) and entry.matches(request_headers):
                age = str(int(now - entry.stored_at)).encode()
                headers = [(n, v) for n, v in entry.headers if n != b"age"]
                headers += [(b"age", age), (b"cache-status", f"{self.name}; hit".encode())]
                await send(
                    {"type": "http.response.start", "status": entry.status, "headers": headers}
                )
                await send({"type": "http.response.body", "body": entry.body})
                return
        fwd = "vary-miss" if candidates else "uri-miss"

        start: Message = {}
        chunks: list[bytes] = []

        async def capture(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self.app(scope, receive, capture)
        status: int = start["status"]
        raw_headers: list[tuple[bytes, bytes]] = list(start.get("headers", []))
        response_headers = combined_headers(raw_headers)
        directives = effective_directives(response_headers, self.target_fields)
        reason = refusal_reason(
            status=status,
            request_has_authorization="authorization" in request_headers,
            directives=directives,
        )
        body = b"".join(chunks)
        if reason is None:
            vary = tuple(f.lower() for f in split_list(response_headers.get("vary", "")))
            entry = StoredResponse(
                status=status,
                headers=tuple(raw_headers),
                body=body,
                stored_at=now,
                lifetime=shared_lifetime(directives),
                vary=vary,
                selecting=tuple(request_headers.get(f) for f in vary),
                tags=frozenset(response_headers.get("surrogate-key", "").split()),
            )
            kept = [e for e in candidates if e.is_fresh(now) and e.selecting != entry.selecting]
            self.entries[key] = [*kept, entry]
            cache_status = f"{self.name}; fwd={fwd}; stored"
        else:
            cache_status = f"{self.name}; fwd={fwd}; detail={reason}"
        raw_headers.append((b"cache-status", cache_status.encode()))
        await send({"type": "http.response.start", "status": status, "headers": raw_headers})
        await send({"type": "http.response.body", "body": body})

    def purge_tag(self, tag: str) -> int:
        """Evict every stored response indexed under `tag`; return how many were evicted."""
        evicted = 0
        for key, stored in list(self.entries.items()):
            kept = [entry for entry in stored if tag not in entry.tags]
            evicted += len(stored) - len(kept)
            self.entries[key] = kept
        return evicted


@dataclass(frozen=True, slots=True)
class Principal:
    tenant: str
    user: str


TOKENS: dict[str, Principal] = {
    "tok-alice": Principal("acme", "alice"),
    "tok-bob": Principal("acme", "bob"),
    "tok-mallory": Principal("globex", "mallory"),
}
SESSIONS: dict[str, Principal] = {
    "sid-alice": TOKENS["tok-alice"],
    "sid-mallory": TOKENS["tok-mallory"],
}
INVOICES: dict[str, list[str]] = {"acme": ["INV-A1", "INV-A2"], "globex": ["INV-G1"]}


def bearer_principal(headers: Mapping[str, str]) -> Principal | None:
    scheme, _, token = headers.get("authorization", "").partition(" ")
    return TOKENS.get(token) if scheme.lower() == "bearer" else None


@dataclass(frozen=True, slots=True)
class CachePolicy:
    cache_control: str
    vary: str | None = None
    tag_tenant: bool = False


PUBLIC = CachePolicy("public, max-age=60")
MAX_AGE_ONLY = CachePolicy("max-age=60")
PRIVATE = CachePolicy("private, max-age=60")
TENANT_SHARED = CachePolicy("max-age=0, s-maxage=60", vary="X-Tenant-ID", tag_tenant=True)


@dataclass(slots=True)
class Origin:
    """The API server; `calls` records which principal each forwarded request carried."""

    policy: CachePolicy
    extra_headers: dict[str, str] = field(default_factory=lambda: {})
    calls: list[tuple[str, str]] = field(default_factory=lambda: [])

    def respond(self, path: str, principal: Principal, content: object) -> JSONResponse:
        self.calls.append((path, principal.user))
        headers = {"Cache-Control": self.policy.cache_control, **self.extra_headers}
        if self.policy.vary is not None:
            headers["Vary"] = self.policy.vary
        if self.policy.tag_tenant:
            headers["Surrogate-Key"] = f"tenant-{principal.tenant} invoices"
        return JSONResponse(content, headers=headers)

    async def invoices(self, request: Request) -> JSONResponse:
        principal = bearer_principal(request.headers)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        content = {"tenant": principal.tenant, "invoices": INVOICES[principal.tenant]}
        return self.respond("/v1/invoices", principal, content)

    async def me(self, request: Request) -> JSONResponse:
        principal = bearer_principal(request.headers)
        if principal is None:
            principal = SESSIONS.get(request.cookies.get("sid", ""))
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        content = {"tenant": principal.tenant, "user": principal.user}
        return self.respond("/v1/me", principal, content)

    def app(self) -> Starlette:
        return Starlette(routes=[Route("/v1/invoices", self.invoices), Route("/v1/me", self.me)])


@dataclass(slots=True)
class TenantEdge:
    """Edge gateway: drop client-sent `x-tenant-id`, then set it from the verified token."""

    app: ASGIApp

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw: list[tuple[bytes, bytes]] = list(scope["headers"])
        headers = [(n, v) for n, v in raw if n.lower() != b"x-tenant-id"]
        principal = bearer_principal(combined_headers(raw))
        if principal is not None:
            headers.append((b"x-tenant-id", principal.tenant.encode()))
        await self.app({**scope, "headers": headers}, receive, send)
