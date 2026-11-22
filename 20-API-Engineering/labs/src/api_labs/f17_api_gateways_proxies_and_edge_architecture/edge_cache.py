"""A shared-cache tier for APIs: s-maxage, collapse-forwarding, shielding, purge, signed URLs.

Models what a CDN POP or an origin shield does with an API response, in process and on a fake
clock: RFC 9111 freshness with `s-maxage`, RFC 9213 targeted `CDN-Cache-Control`, RFC 5861 stale
controls, request collapsing with hit-for-pass, `Surrogate-Key` purge, Age propagation across
tiers, RFC 9211 `Cache-Status`, and HMAC signed URLs verified before the cache lookup.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qsl, urlencode

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterable, Mapping

    from starlette.types import Receive, Scope, Send

Clock = Callable[[], float]
Pairs = Sequence[tuple[str, str]]

# Connection-specific fields (RFC 9110 Section 7.6.1) plus framing and routing fields that each
# tier regenerates; none is forwarded or stored.
HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer"}
    | {"transfer-encoding", "upgrade", "content-length", "host"}
)
# Targeted fields in priority order (RFC 9213 Section 2.2): most specific first.
DEFAULT_TARGETS = ("Surrogate-Control", "CDN-Cache-Control")
SIGNATURE_PARAMS = frozenset({"exp", "kid", "sig"})
MAX_KEY_BYTES = 1024
MAX_KEY_HEADER_BYTES = 16 * 1024


# --------------------------------------------------------------------------------------------
# Cache-Control parsing and the shared-cache policy
# --------------------------------------------------------------------------------------------


def parse_directives(value: str) -> dict[str, str | None]:
    """Split a `Cache-Control` value into directives; first occurrence wins, names lower-cased."""
    out: dict[str, str | None] = {}
    for part in _split_commas(value):
        name, sep, arg = part.partition("=")
        name = name.strip().lower()
        if name and name not in out:
            out[name] = arg.strip().strip('"') if sep else None
    return out


def _split_commas(value: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quoted = False
    for ch in value:
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p for p in (s.strip() for s in parts) if p]


def delta_seconds(value: str | None) -> int | None:
    """`delta-seconds` is ASCII digits only (RFC 9111 Section 1.2.2); anything else is None."""
    if value is not None and value.isascii() and value.isdigit():
        return int(value)
    return None


@dataclass(frozen=True, slots=True)
class Policy:
    """What one shared cache may do with a response."""

    storable: bool
    ttl: int = 0
    swr: int = 0
    sie: int = 0
    source: str = "none"
    auth_ok: bool = False  # RFC 9111 Section 3.5: may be reused for Authorization requests


UNCACHEABLE = Policy(storable=False)


def _policy_from(d: Mapping[str, str | None], source: str, *, targeted: bool) -> Policy | None:
    """Return None when a targeted field is unusable so the caller falls back (RFC 9213 2.1)."""
    if "no-store" in d or "private" in d:
        return Policy(storable=False, source=source)
    ttl_raw = d["s-maxage"] if "s-maxage" in d else d.get("max-age")
    has_ttl = "s-maxage" in d or "max-age" in d
    ttl = delta_seconds(ttl_raw)
    if targeted and has_ttl and ttl is None:
        return None  # invalid value (RFC 9213 Section 2.1): the lab ignores the whole field
    if not has_ttl or ttl is None:
        # No freshness (or invalid freshness, which RFC 9111 Section 4.2.1 says to treat as
        # stale). The lab applies no heuristic freshness (Section 4.2.2).
        return (
            Policy(storable=False, source=source) if not has_ttl else Policy(True, 0, source=source)
        )
    if "no-cache" in d:
        ttl = 0
    return Policy(
        storable=True,
        ttl=ttl,
        swr=0 if "no-cache" in d else delta_seconds(d.get("stale-while-revalidate")) or 0,
        sie=delta_seconds(d.get("stale-if-error")) or 0,
        source=source,
        auth_ok="public" in d or "s-maxage" in d or "must-revalidate" in d,
    )


def select_policy(headers: Mapping[str, str], targets: Sequence[str] = DEFAULT_TARGETS) -> Policy:
    """Policy for a shared cache: first valid targeted field wins and `Cache-Control` is ignored."""
    lowered = {k.lower(): v for k, v in headers.items()}
    for name in targets:
        raw = lowered.get(name.lower())
        if raw is None or not raw.strip():
            continue
        policy = _policy_from(parse_directives(raw), name, targeted=True)
        if policy is not None:
            return policy
    raw_cc = lowered.get("cache-control")
    if raw_cc is None:
        return UNCACHEABLE
    return _policy_from(parse_directives(raw_cc), "Cache-Control", targeted=False) or UNCACHEABLE


@dataclass(frozen=True, slots=True)
class BrowserView:
    """What a private cache (a browser) does with `Cache-Control`: it ignores `s-maxage`."""

    fresh_for: int
    stale_window: int


def browser_freshness(cache_control: str) -> BrowserView:
    d = parse_directives(cache_control)
    if "no-store" in d:
        return BrowserView(0, 0)
    fresh = delta_seconds(d.get("max-age")) or 0
    swr = delta_seconds(d.get("stale-while-revalidate")) or 0
    return BrowserView(0 if "no-cache" in d else fresh, 0 if "no-cache" in d else swr)


def parse_surrogate_keys(
    value: str, *, max_token: int = MAX_KEY_BYTES, max_header: int = MAX_KEY_HEADER_BYTES
) -> frozenset[str]:
    """Fastly semantics: tokens split on spaces only; an oversize token and all after it drop."""
    keys: list[str] = []
    used = 0
    for token in value.split(" "):
        if not token:
            continue
        used += len(token.encode()) + (1 if keys else 0)
        if len(token.encode()) > max_token or used > max_header:
            break
        keys.append(token)
    return frozenset(keys)


# --------------------------------------------------------------------------------------------
# Signed edge URLs
# --------------------------------------------------------------------------------------------


class SignedUrlVerifier:
    """HMAC-SHA256 over `GET \\n path \\n canonical-query \\n exp`, with key rotation by `kid`."""

    def __init__(self, keys: Mapping[str, bytes], clock: Clock) -> None:
        self._keys = dict(keys)
        self._clock = clock

    @staticmethod
    def _canonical(path: str, pairs: Pairs, exp: int) -> bytes:
        kept = sorted((k, v) for k, v in pairs if k not in SIGNATURE_PARAMS)
        return f"GET\n{path}\n{urlencode(kept)}\n{exp}".encode()

    def sign(self, path: str, pairs: Pairs, *, exp: int, kid: str) -> str:
        mac = hmac.new(self._keys[kid], self._canonical(path, pairs, exp), hashlib.sha256)
        kept = [(k, v) for k, v in pairs if k not in SIGNATURE_PARAMS]
        query = urlencode([*kept, ("exp", str(exp)), ("kid", kid), ("sig", mac.hexdigest())])
        return f"{path}?{query}"

    def check(self, path: str, pairs: Pairs) -> str | None:
        """Return None when valid, else a short reason; runs before any cache lookup."""
        counts = Counter(k for k, _ in pairs if k in SIGNATURE_PARAMS)
        if frozenset(counts) != SIGNATURE_PARAMS:
            return "missing"
        if any(n > 1 for n in counts.values()):
            return "duplicate-parameter"  # parameter pollution: refuse the ambiguity
        params = dict(pairs)
        exp = delta_seconds(params["exp"])
        key = self._keys.get(params["kid"])
        if exp is None:
            return "malformed"
        if key is None:
            return "unknown-kid"
        want = hmac.new(key, self._canonical(path, pairs, exp), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want.encode(), params["sig"].encode()):
            return "bad-signature"
        return "expired" if self._clock() >= exp else None


# --------------------------------------------------------------------------------------------
# One cache tier
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class Entry:
    body: bytes
    headers: list[tuple[str, str]]
    policy: Policy
    stored_at: float
    initial_age: float
    fresh_until: float
    vary: tuple[tuple[str, str | None], ...]
    tags: frozenset[str]
    etag: str | None

    def age(self, now: float) -> float:
        return self.initial_age + (now - self.stored_at)

    def stale_for(self, now: float) -> float:
        return now - self.fresh_until

    def vary_matches(self, req: Mapping[str, str]) -> bool:
        return all(name != "*" and req.get(name) == value for name, value in self.vary)


@dataclass(slots=True)
class Fetched:
    """Result of one upstream fetch, shared with collapsed followers when `entry` is set."""

    status: int
    headers: list[tuple[str, str]]
    body: bytes
    entry: Entry | None = None
    upstream_status: Literal["ok", "error"] = "ok"


@dataclass(frozen=True, slots=True)
class UpstreamRequest:
    target: str
    headers: tuple[tuple[str, str], ...]


def cache_key(path: str, pairs: Pairs, ignore: Iterable[str] = ()) -> str:
    skip = frozenset(ignore)
    return f"GET {path}?{urlencode(sorted((k, v) for k, v in pairs if k not in skip))}"


def _etag_matches(header: str, etag: str) -> bool:
    def opaque(tag: str) -> str:
        return tag.strip().removeprefix("W/")

    wanted = opaque(etag)
    return any(t.strip() == "*" or opaque(t) == wanted for t in header.split(","))


class EdgeCache:
    """A caching reverse proxy tier; an ASGI app whose upstream is another ASGI app via httpx."""

    def __init__(
        self,
        name: str,
        upstream: httpx.AsyncClient,
        *,
        clock: Clock,
        collapse: bool = True,
        hit_for_pass_ttl: float = 120.0,
        outermost: bool = True,
        verifier: SignedUrlVerifier | None = None,
        targets: Sequence[str] = DEFAULT_TARGETS,
        use_upstream_age: bool = True,
        verify_first: bool = True,
        cache_signature_params: bool = False,
    ) -> None:
        self.name = name
        self.stats: Counter[str] = Counter()
        self._upstream = upstream
        self._clock = clock
        self._collapse = collapse
        self._hfp_ttl = hit_for_pass_ttl
        self._outermost = outermost
        self._verifier = verifier
        self._targets = tuple(targets)
        self._use_upstream_age = use_upstream_age
        self._verify_first = verify_first
        self._ignored: frozenset[str] = (
            frozenset() if cache_signature_params or verifier is None else SIGNATURE_PARAMS
        )
        self._store: dict[str, Entry] = {}
        self._flights: dict[str, asyncio.Future[Fetched]] = {}
        self._hfp: dict[str, float] = {}
        self._refreshing: set[str] = set()
        self._background: set[asyncio.Task[Fetched]] = set()

    # ---- ASGI ---------------------------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = await self.handle(Request(scope, receive))
        await response(scope, receive, send)

    async def drain(self) -> None:
        """Wait for background revalidations (tests call this instead of sleeping)."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    # ---- request path -------------------------------------------------------------------

    async def handle(self, req: Request) -> Response:
        pairs = parse_qsl(req.url.query, keep_blank_values=True)
        path = req.url.path
        denial = self._verifier.check(path, pairs) if self._verifier is not None else None
        if denial is not None and self._verify_first:
            self.stats["denied"] += 1
            return self._plain(403, f"{self.name}: {denial}")
        if req.method != "GET":
            return await self._pass_through(req, path, pairs)
        key = cache_key(path, pairs, self._ignored)
        up = UpstreamRequest(self._upstream_target(path, pairs), self._upstream_headers(req))
        has_auth = "authorization" in req.headers
        entry = self._store.get(key)
        if entry is not None and not entry.vary_matches(req.headers):
            entry = None
            reason = "vary-miss"
        else:
            reason = "uri-miss" if entry is None else "stale"
        if entry is not None and has_auth and not entry.policy.auth_ok:
            self.stats["bypass"] += 1
            return await self._pass_through(req, path, pairs)
        if entry is not None:
            state = entry.stale_for(self._clock())
            if state < 0:
                self.stats["hit"] += 1
                return self._from_entry(req, entry, f"{self.name}; hit; ttl={int(-state)}")
            if state <= entry.policy.swr:
                if key not in self._refreshing:
                    self._refreshing.add(key)
                    task = asyncio.create_task(self._refresh(key, up, entry))
                    self._background.add(task)
                    task.add_done_callback(self._background.discard)
                self.stats["stale_hit"] += 1
                return self._from_entry(req, entry, f"{self.name}; hit; ttl={int(-state)}")
        if self._verifier is not None and denial is not None:
            self.stats["denied"] += 1  # verify_first=False models the late-auth bug
            return self._plain(403, f"{self.name}: {denial}")
        result, collapsed = await self._fill(key, up, entry, has_auth, req.headers)
        if (
            result.status >= 500
            and entry is not None
            and entry.stale_for(self._clock()) <= entry.policy.sie
        ):
            self.stats["stale_if_error"] += 1
            return self._from_entry(
                req, entry, f"{self.name}; fwd={reason}; fwd-status={result.status}"
            )
        return self._from_fetched(req, result, collapsed, reason, has_auth)

    async def _refresh(self, key: str, up: UpstreamRequest, entry: Entry) -> Fetched:
        try:
            self.stats["revalidate"] += 1
            result, _ = await self._fill(key, up, entry, False, {})
        finally:
            self._refreshing.discard(key)
        return result

    async def _pass_through(self, req: Request, path: str, pairs: Pairs) -> Response:
        self.stats["pass"] += 1
        up_headers = self._upstream_headers(req)
        try:
            resp = await self._upstream.request(
                req.method,
                self._upstream_target(path, pairs),
                headers=up_headers,
                content=await req.body(),
            )
        except httpx.HTTPError:
            return self._plain(502, "upstream unreachable")
        if req.method not in {"GET", "HEAD", "OPTIONS"} and resp.status_code < 400:
            self.purge_path(path)  # RFC 9111 Section 4.4: invalidate the target URI
        return self._build(resp.status_code, list(resp.headers.multi_items()), resp.content)

    # ---- collapse-forwarding ------------------------------------------------------------

    async def _fill(
        self,
        key: str,
        up: UpstreamRequest,
        stale: Entry | None,
        has_auth: bool,
        req_headers: Mapping[str, str],
    ) -> tuple[Fetched, bool]:
        collapsed = False
        while True:
            if not self._collapse or self._hfp.get(key, 0.0) > self._clock():
                return await self._fetch(key, up, stale, has_auth), collapsed
            flight = self._flights.get(key)
            if flight is None:
                loop = asyncio.get_running_loop()
                flight = self._flights[key] = loop.create_future()
                result = Fetched(502, [], b"", upstream_status="error")
                try:
                    result = await self._fetch(key, up, stale, has_auth)
                finally:
                    # Forget the flight before waking followers so the next miss starts fresh;
                    # a cancelled or crashed leader wakes them with the 502 placeholder.
                    del self._flights[key]
                    flight.set_result(result)
                return result, collapsed
            result = await asyncio.shield(flight)
            if result.entry is not None and result.entry.vary_matches(req_headers) and not has_auth:
                self.stats["collapsed"] += 1
                return result, True
            if result.entry is not None and has_auth and result.entry.policy.auth_ok:
                self.stats["collapsed"] += 1
                return result, True
            collapsed = False  # unusable for this request: queue again behind the next leader

    async def _fetch(
        self, key: str, up: UpstreamRequest, stale: Entry | None, has_auth: bool
    ) -> Fetched:
        headers = dict(up.headers)
        req_hdrs = {k.lower(): v for k, v in up.headers}
        if stale is not None and stale.etag is not None:
            headers["if-none-match"] = stale.etag
        sent_at = self._clock()
        self.stats["upstream"] += 1
        try:
            resp = await self._upstream.get(up.target, headers=headers)
        except httpx.HTTPError:
            return Fetched(502, [], b"", upstream_status="error")
        received_at = self._clock()
        items = list(resp.headers.multi_items())
        if resp.status_code == 304 and stale is not None:
            merged = self._merge(stale.headers, items)
            policy = select_policy(dict(merged), self._targets)
            fresh = self._make_entry(
                stale.body, merged, policy, sent_at, received_at, httpx.Headers(merged), req_hdrs
            )
            self._index(key, fresh)
            return Fetched(200, merged, stale.body, fresh)
        policy = select_policy(dict(items), self._targets)
        usable = (
            resp.status_code == 200
            and policy.storable
            and resp.headers.get("set-cookie") is None
            and resp.headers.get("vary", "").strip() != "*"
            and (not has_auth or policy.auth_ok)
        )
        if not usable:
            if self._hfp_ttl > 0 and resp.status_code < 500:
                self._hfp[key] = self._clock() + self._hfp_ttl  # hit-for-pass marker
            return Fetched(resp.status_code, items, resp.content)
        entry = self._make_entry(
            resp.content, items, policy, sent_at, received_at, resp.headers, req_hdrs
        )
        self._index(key, entry)
        return Fetched(resp.status_code, items, resp.content, entry)

    def _make_entry(
        self,
        body: bytes,
        items: list[tuple[str, str]],
        policy: Policy,
        sent_at: float,
        received_at: float,
        raw: httpx.Headers,
        request_headers: Mapping[str, str],
    ) -> Entry:
        age_value = delta_seconds(raw.get("age")) if self._use_upstream_age else None
        # RFC 9111 Section 4.2.3: corrected_age_value = age_value + response_delay.
        initial_age = float((age_value or 0) + (received_at - sent_at))
        kept = [(k, v) for k, v in items if k.lower() not in HOP_BY_HOP | {"age", "cache-status"}]
        names = [n.strip().lower() for n in raw.get("vary", "").split(",") if n.strip()]
        vary = tuple((n, request_headers.get(n)) for n in names)
        return Entry(
            body=body,
            headers=kept,
            policy=policy,
            stored_at=received_at,
            initial_age=initial_age,
            fresh_until=received_at + max(0.0, policy.ttl - initial_age),
            vary=vary,
            tags=parse_surrogate_keys(raw.get("surrogate-key", "")),
            etag=raw.get("etag"),
        )

    def _index(self, key: str, entry: Entry) -> None:
        self._store[key] = entry

    @staticmethod
    def _merge(old: list[tuple[str, str]], new: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """RFC 9111 Section 3.2 and 4.3.4: a 304 updates stored header fields it carries."""
        replaced = {k.lower() for k, _ in new} - HOP_BY_HOP
        return [(k, v) for k, v in old if k.lower() not in replaced] + [
            (k, v) for k, v in new if k.lower() not in HOP_BY_HOP
        ]

    # ---- purge --------------------------------------------------------------------------

    def purge_tag(self, tag: str, *, soft: bool = False) -> int:
        """Purge every entry carrying `tag`; soft purge keeps it servable as stale (SWR)."""
        hits = [k for k, e in self._store.items() if tag in e.tags]
        self._apply_purge(hits, soft)
        return len(hits)

    def purge_path(self, path: str, *, soft: bool = False) -> int:
        hits = [k for k in self._store if k.split("?", 1)[0] == f"GET {path}"]
        self._apply_purge(hits, soft)
        return len(hits)

    def _apply_purge(self, keys: Iterable[str], soft: bool) -> None:
        now = self._clock()
        for k in keys:
            if soft:
                self._store[k].fresh_until = min(self._store[k].fresh_until, now)
            else:
                del self._store[k]
        if not soft:
            self._hfp.clear()

    # ---- response construction ----------------------------------------------------------

    def _upstream_target(self, path: str, pairs: Pairs) -> str:
        kept = [(k, v) for k, v in pairs if k not in self._ignored]
        return f"{path}?{urlencode(kept)}" if kept else path

    @staticmethod
    def _upstream_headers(req: Request) -> tuple[tuple[str, str], ...]:
        drop = HOP_BY_HOP | {"if-none-match", "if-modified-since", "cache-control", "pragma"}
        return tuple((k, v) for k, v in req.headers.items() if k.lower() not in drop)

    def _from_entry(self, req: Request, entry: Entry, status_line: str) -> Response:
        age = int(entry.age(self._clock()))
        inm = req.headers.get("if-none-match")
        if inm is not None and entry.etag is not None and _etag_matches(inm, entry.etag):
            return self._build(304, entry.headers, b"", age=age, cache_status=status_line)
        return self._build(200, entry.headers, entry.body, age=age, cache_status=status_line)

    def _from_fetched(
        self, req: Request, result: Fetched, collapsed: bool, reason: str, has_auth: bool
    ) -> Response:
        stored = result.entry is not None
        member = f"{self.name}; fwd={reason}"
        if stored and not collapsed:
            member += "; stored"
        if collapsed:
            member += "; collapsed"
        if result.status != 200:
            member += f"; fwd-status={result.status}"
        age = int(result.entry.age(self._clock())) if result.entry is not None else None
        inm = req.headers.get("if-none-match")
        if (
            result.entry is not None
            and inm
            and result.entry.etag
            and _etag_matches(inm, result.entry.etag)
        ):
            return self._build(304, result.headers, b"", age=age, cache_status=member)
        return self._build(result.status, result.headers, result.body, age=age, cache_status=member)

    def _build(
        self,
        status: int,
        headers: Sequence[tuple[str, str]],
        body: bytes,
        *,
        age: int | None = None,
        cache_status: str | None = None,
    ) -> Response:
        out = [(k, v) for k, v in headers if k.lower() not in HOP_BY_HOP | {"age", "cache-status"}]
        if self._outermost:
            out = [
                (k, v) for k, v in out if k.lower() not in {"surrogate-key", "surrogate-control"}
            ]
        upstream_chain = [v for k, v in headers if k.lower() == "cache-status"]
        if age is not None:
            out.append(("Age", str(age)))
        if cache_status is not None:
            out.append(("Cache-Status", ", ".join([*upstream_chain, cache_status])))
        response = Response(content=body if status != 304 else b"", status_code=status)
        raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in out]
        if status != 304:
            raw.append((b"content-length", str(len(body)).encode()))
        response.raw_headers = raw
        return response

    @staticmethod
    def _plain(status: int, text: str) -> Response:
        return Response(text, status_code=status, media_type="text/plain")


# --------------------------------------------------------------------------------------------
# Origin and topology
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class RecordedRequest:
    method: str
    target: str
    if_none_match: str | None
    authorization: str | None


class Origin:
    """A deterministic API origin: versioned products, a private endpoint, gated latency."""

    def __init__(
        self,
        *,
        gated: bool = False,
        fail_with: int | None = None,
        orders_headers: dict[str, str] | None = None,
    ) -> None:
        self.gated = gated
        self.fail_with = fail_with
        self.product_headers: dict[str, str] = {
            "Cache-Control": "max-age=0",
            "CDN-Cache-Control": "max-age=60, stale-while-revalidate=300, stale-if-error=3600",
            "Surrogate-Key": "product-{pid} catalog",
        }
        self.orders_headers = orders_headers or {"Cache-Control": "private"}
        self.versions: dict[str, int] = {}
        self.requests: list[RecordedRequest] = []
        self.pending: list[asyncio.Future[None]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.cookies_issued = 0

    def release(self, count: int | None = None) -> None:
        """Let the first `count` gated requests (all when None) complete."""
        take = self.pending if count is None else self.pending[:count]
        for fut in list(take):
            self.pending.remove(fut)
            fut.set_result(None)

    @contextlib.asynccontextmanager
    async def _tracked(self, request: Request) -> AsyncGenerator[None]:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        self.requests.append(
            RecordedRequest(
                request.method,
                target,
                request.headers.get("if-none-match"),
                request.headers.get("authorization"),
            )
        )
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.gated:
                fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                self.pending.append(fut)
                await fut
            yield
        finally:
            self.in_flight -= 1

    async def product(self, request: Request) -> Response:
        async with self._tracked(request):
            if self.fail_with is not None:
                return Response(status_code=self.fail_with)
            pid: str = request.path_params["pid"]
            version = self.versions.setdefault(pid, 1)
            etag = f'"v{version}"'
            headers = {k: v.format(pid=pid) for k, v in self.product_headers.items()}
            headers["ETag"] = etag
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers=headers)
            return JSONResponse({"id": pid, "version": version}, headers=headers)

    async def update_product(self, request: Request) -> Response:
        async with self._tracked(request):
            pid: str = request.path_params["pid"]
            self.versions[pid] = self.versions.get(pid, 1) + 1
            return JSONResponse({"id": pid, "version": self.versions[pid]})

    async def me(self, request: Request) -> Response:
        async with self._tracked(request):
            self.cookies_issued += 1
            response = JSONResponse(
                {"session": self.cookies_issued}, headers={"Cache-Control": "private"}
            )
            response.set_cookie("session", f"s{self.cookies_issued}")
            return response

    async def orders(self, request: Request) -> Response:
        async with self._tracked(request):
            who = request.headers.get("authorization", "anonymous")
            return JSONResponse({"owner": who}, headers=dict(self.orders_headers))

    @property
    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/products/{pid}", self.product, methods=["GET"]),
                Route("/products/{pid}", self.update_product, methods=["POST"]),
                Route("/me", self.me, methods=["GET"]),
                Route("/orders", self.orders, methods=["GET"]),
            ]
        )


@dataclass(slots=True)
class Topology:
    """Edge POPs in front of an optional shield in front of the origin."""

    origin: Origin
    pops: dict[str, EdgeCache]
    shield: EdgeCache | None

    def client(self, pop: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.pops[pop]), base_url="http://test"
        )

    async def purge_tag(
        self,
        tag: str,
        *,
        order: Literal["upstream-first", "edge-first"] = "upstream-first",
        between: Callable[[], object] | None = None,
        soft: bool = False,
    ) -> None:
        """Purge each tier; `between` runs after the first tier so a test can race a refill."""
        edges = list(self.pops.values())
        upper = [self.shield] if self.shield is not None else []
        tiers = [*edges, *upper] if order == "edge-first" else [*upper, *edges]
        tiers[0].purge_tag(tag, soft=soft)
        if between is not None:
            result = between()
            if asyncio.iscoroutine(result):
                await result
        for tier in tiers[1:]:
            tier.purge_tag(tag, soft=soft)


def build_topology(
    origin: Origin,
    clock: Clock,
    *,
    pops: int = 2,
    shield: bool = True,
    collapse: bool = True,
    use_upstream_age: bool = True,
    hit_for_pass_ttl: float = 120.0,
    verifier: SignedUrlVerifier | None = None,
    verify_first: bool = True,
    cache_signature_params: bool = False,
) -> Topology:
    origin_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=origin.app), base_url="http://origin"
    )
    shield_tier: EdgeCache | None = None
    upstream = origin_client
    if shield:
        shield_tier = EdgeCache(
            "shield",
            origin_client,
            clock=clock,
            collapse=collapse,
            hit_for_pass_ttl=hit_for_pass_ttl,
            outermost=False,
            use_upstream_age=use_upstream_age,
        )
        upstream = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=shield_tier), base_url="http://shield"
        )
    edges = {
        f"edge-{i}": EdgeCache(
            f"edge-{i}",
            upstream,
            clock=clock,
            collapse=collapse,
            use_upstream_age=use_upstream_age,
            hit_for_pass_ttl=hit_for_pass_ttl,
            verifier=verifier,
            verify_first=verify_first,
            cache_signature_params=cache_signature_params,
        )
        for i in range(pops)
    }
    return Topology(origin=origin, pops=edges, shield=shield_tier)
