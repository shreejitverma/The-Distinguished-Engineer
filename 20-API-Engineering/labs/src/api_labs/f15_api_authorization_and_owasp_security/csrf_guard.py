"""CSRF in cookie-authenticated APIs: browser models, a Fetch Metadata guard, signed tokens.

Three layers, each small enough to test in isolation:

1. Browser models: ``same_site`` and ``sec_fetch_site`` follow the W3C Fetch Metadata
   algorithm (Section 2.3, including redirect chains), and ``cookie_is_sent`` follows the
   ``SameSite`` enforcement of draft-ietf-httpbis-rfc6265bis-22 Section 5.6.7.
2. ``CrossOriginPolicy`` and ``CrossOriginGuardMiddleware``: the Go 1.25
   ``net/http.CrossOriginProtection`` algorithm (``Sec-Fetch-Site`` first, ``Origin`` vs
   ``Host`` fallback) as a pure ASGI middleware, with exact-match bypass paths.
3. ``issue_csrf_token`` and ``verify_csrf_token``: an HMAC-signed double-submit token bound
   to the session, the OWASP fallback when Fetch Metadata cannot be relied on.

``build_bank_app`` wires them into a deterministic Starlette app with one cookie-authenticated
and one bearer-authenticated transfer endpoint, so tests can replay a forged browser request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_PORTS: Final[Mapping[str, int]] = {"http": 80, "https": 443}

# A deterministic subset of the Public Suffix List (https://publicsuffix.org/list/).
# Production code must use the full, regularly updated list; "github.io" is the
# classic private-section entry that makes two user pages cross-site.
PUBLIC_SUFFIXES: Final = frozenset({"com", "org", "net", "io", "example", "co.uk", "github.io"})


@dataclass(frozen=True, slots=True)
class Origin:
    """A tuple origin (scheme, host, port) per RFC 6454 Section 4."""

    scheme: str
    host: str
    port: int

    @classmethod
    def parse(cls, url: str) -> Origin:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        if scheme not in DEFAULT_PORTS or not host:
            raise ValueError(f"not a tuple origin: {url!r}")
        port = parts.port if parts.port is not None else DEFAULT_PORTS[scheme]
        return cls(scheme, host, port)

    def serialize(self) -> str:
        """ASCII serialization as sent in the ``Origin`` header (default port omitted)."""
        suffix = "" if self.port == DEFAULT_PORTS[self.scheme] else f":{self.port}"
        return f"{self.scheme}://{self.host}{suffix}"


def registrable_domain(host: str) -> str:
    """Public suffix plus one label (RFC 6265bis-22 Section 2.3); longest suffix wins."""
    labels = host.lower().rstrip(".").split(".")
    for i in range(len(labels)):
        if ".".join(labels[i:]) in PUBLIC_SUFFIXES:
            return host.lower() if i == 0 else ".".join(labels[i - 1 :])
    return host.lower()


def same_site(a: Origin, b: Origin, *, schemeful: bool = True) -> bool:
    """HTML "same site": same scheme (when schemeful) and same registrable domain."""
    if schemeful and a.scheme != b.scheme:
        return False
    return registrable_domain(a.host) == registrable_domain(b.host)


class FetchSite(StrEnum):
    SAME_ORIGIN = "same-origin"
    SAME_SITE = "same-site"
    CROSS_SITE = "cross-site"
    NONE = "none"


def sec_fetch_site(initiator: Origin | None, url_list: Sequence[str]) -> FetchSite:
    """Compute ``Sec-Fetch-Site`` per W3C Fetch Metadata Section 2.3.

    *initiator* is the request's origin, or ``None`` for a navigation the user started
    from browser UI (address bar, bookmark).  *url_list* is every URL the request
    visited, redirects included; one cross-site hop taints the whole chain.
    """
    if initiator is None:
        return FetchSite.NONE
    value = FetchSite.SAME_ORIGIN
    for url in url_list:
        target = Origin.parse(url)
        if target == initiator:
            continue
        value = FetchSite.CROSS_SITE
        if not same_site(initiator, target):
            break
        value = FetchSite.SAME_SITE
    return value


class SameSite(StrEnum):
    STRICT = "Strict"
    LAX = "Lax"
    NONE = "None"
    DEFAULT = "Default"  # no SameSite attribute on the Set-Cookie line


# RFC 6265bis-22 Section 5.6.7.2: "a cookie age of 2 minutes or less" for Lax-allowing-unsafe.
LAX_ALLOWING_UNSAFE_MAX_AGE_S: Final = 120.0
RFC9110_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


@dataclass(frozen=True, slots=True)
class BrowserRequest:
    method: str
    same_site_with_cookie_site: bool
    top_level_navigation: bool


def cookie_is_sent(
    policy: SameSite,
    request: BrowserRequest,
    *,
    cookie_age_s: float = 3600.0,
    lax_by_default: bool = True,
) -> bool:
    """Model the SameSite cookie-retrieval decision of RFC 6265bis-22 Section 5.6.7.

    ``lax_by_default=False`` models a browser that treats a missing attribute as ``None``.
    """
    if request.same_site_with_cookie_site or policy is SameSite.NONE:
        return True
    if policy is SameSite.STRICT:
        return False
    if policy is SameSite.DEFAULT and not lax_by_default:
        return True
    if not request.top_level_navigation:
        return False
    if request.method.upper() in RFC9110_SAFE_METHODS:
        return True
    # Lax-allowing-unsafe: only for cookies that did not specify SameSite explicitly.
    return policy is SameSite.DEFAULT and cookie_age_s <= LAX_ALLOWING_UNSAFE_MAX_AGE_S


# Go net/http CrossOriginProtection exempts exactly these; TRACE is deliberately not exempt.
GUARD_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


class Verdict(StrEnum):
    ALLOW_SAFE_METHOD = "allow:safe-method"
    ALLOW_FETCH_METADATA = "allow:sec-fetch-site"
    ALLOW_ORIGIN_MATCHES_HOST = "allow:origin-matches-host"
    ALLOW_NON_BROWSER = "allow:no-browser-headers"
    ALLOW_TRUSTED_ORIGIN = "allow:trusted-origin"
    ALLOW_BYPASS_PATH = "allow:bypass-path"
    DENY_SEC_FETCH_SITE = "deny:sec-fetch-site"
    DENY_ORIGIN_MISMATCH = "deny:origin-mismatch"
    DENY_NO_BROWSER_HEADERS = "deny:no-browser-headers"

    @property
    def allowed(self) -> bool:
        return self.value.startswith("allow:")


def _origin_netloc(origin: str) -> str | None:
    """``host[:port]`` of an ``Origin`` value, or ``None`` for ``null`` and garbage."""
    parts = urlsplit(origin.strip())
    if parts.scheme.lower() not in DEFAULT_PORTS or not parts.netloc:
        return None
    if parts.path or parts.query or parts.fragment:
        return None
    return parts.netloc.lower()


@dataclass(frozen=True, slots=True)
class CrossOriginPolicy:
    """Reject unsafe cross-origin browser requests (the Go 1.25 algorithm plus two knobs).

    ``allow_same_site`` accepts ``Sec-Fetch-Site: same-site``: only safe when every sibling
    subdomain is as trusted as this origin.  ``require_browser_headers`` turns the
    "neither header present" case from allow (non-browser client) into deny, for
    endpoints that only browsers should call.
    """

    trusted_origins: frozenset[str] = field(default_factory=frozenset[str])
    bypass_paths: frozenset[str] = field(default_factory=frozenset[str])
    allow_same_site: bool = False
    require_browser_headers: bool = False

    def decide(self, method: str, path: str, headers: Mapping[str, str]) -> Verdict:
        if method.upper() in GUARD_SAFE_METHODS:
            return Verdict.ALLOW_SAFE_METHOD
        site = headers.get("sec-fetch-site")
        origin = headers.get("origin")
        if site is not None:
            site = site.strip()
            if site in (FetchSite.SAME_ORIGIN, FetchSite.NONE):
                return Verdict.ALLOW_FETCH_METADATA
            if site == FetchSite.SAME_SITE and self.allow_same_site:
                return Verdict.ALLOW_FETCH_METADATA
            return self._exempt(path, origin) or Verdict.DENY_SEC_FETCH_SITE
        if origin is None:
            if self.require_browser_headers:
                return self._exempt(path, origin) or Verdict.DENY_NO_BROWSER_HEADERS
            return Verdict.ALLOW_NON_BROWSER
        netloc = _origin_netloc(origin)
        if netloc is not None and netloc == headers.get("host", "").strip().lower():
            return Verdict.ALLOW_ORIGIN_MATCHES_HOST
        return self._exempt(path, origin) or Verdict.DENY_ORIGIN_MISMATCH

    def _exempt(self, path: str, origin: str | None) -> Verdict | None:
        # Exact string match only: prefix or pattern matching is how CVE-2025-47910
        # let Go 1.25.0's AddInsecureBypassPattern exempt more paths than intended.
        if path in self.bypass_paths:
            return Verdict.ALLOW_BYPASS_PATH
        if origin is not None and origin.strip() in self.trusted_origins:
            return Verdict.ALLOW_TRUSTED_ORIGIN
        return None


WEBSOCKET_POLICY_VIOLATION: Final = 1008


class CrossOriginGuardMiddleware:
    """Pure ASGI middleware applying ``CrossOriginPolicy`` before the app runs.

    A WebSocket handshake is a ``GET`` but opens a credentialed, stateful channel, so it is
    checked as an unsafe method (cross-site WebSocket hijacking defense).
    """

    def __init__(self, app: ASGIApp, policy: CrossOriginPolicy | None = None) -> None:
        self.app = app
        self.policy = policy if policy is not None else CrossOriginPolicy()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        method = scope["method"] if scope["type"] == "http" else "WEBSOCKET"
        verdict = self.policy.decide(method, scope["path"], Headers(scope=scope))
        if verdict.allowed:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": WEBSOCKET_POLICY_VIOLATION})
            return
        body = {"detail": "cross-origin request rejected", "reason": verdict.value}
        await JSONResponse(body, status_code=403)(scope, receive, send)


CSRF_COOKIE: Final = "__Host-csrf"
CSRF_HEADER: Final = "x-csrf-token"
_NONCE_BYTES: Final = 16


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _csrf_mac(secret: bytes, session_id: str, nonce: bytes) -> bytes:
    sid = session_id.encode("utf-8")
    # Length-prefixed fields: an unambiguous encoding, so (sid, nonce) pairs cannot collide.
    message = len(sid).to_bytes(4, "big") + sid + len(nonce).to_bytes(4, "big") + nonce
    return hmac.new(secret, message, hashlib.sha256).digest()


def issue_csrf_token(secret: bytes, session_id: str, *, nonce: bytes | None = None) -> str:
    """Return ``b64url(nonce) "." b64url(HMAC-SHA256(secret, len|sid|len|nonce))``."""
    raw_nonce = secrets.token_bytes(_NONCE_BYTES) if nonce is None else nonce
    return f"{_b64(raw_nonce)}.{_b64(_csrf_mac(secret, session_id, raw_nonce))}"


def verify_csrf_token(secret: bytes, session_id: str, token: str) -> bool:
    nonce_text, sep, mac_text = token.partition(".")
    if not sep:
        return False
    try:
        nonce = _unb64(nonce_text)
        mac = _unb64(mac_text)
    except ValueError:  # binascii.Error subclasses ValueError
        return False
    if len(nonce) != _NONCE_BYTES:
        return False
    return hmac.compare_digest(mac, _csrf_mac(secret, session_id, nonce))


def naive_double_submit_ok(cookie_value: str | None, header_value: str | None) -> bool:
    """The discouraged check: header equals cookie, nothing binds either to the session."""
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value.encode(), header_value.encode())


SESSION_COOKIE: Final = "__Host-sid"


@dataclass(slots=True)
class Bank:
    """In-memory state for the demo: sessions, bearer tokens, balances."""

    sessions: dict[str, str] = field(default_factory=dict[str, str])
    bearer_tokens: dict[str, str] = field(default_factory=dict[str, str])
    balances: dict[str, int] = field(default_factory=dict[str, int])


def build_bank_app(
    bank: Bank,
    *,
    policy: CrossOriginPolicy | None = None,
    csrf_secret: bytes | None = None,
    lenient_json: bool = False,
) -> Starlette:
    """Cookie-auth and bearer-auth transfer endpoints, optionally guarded.

    ``policy=None`` leaves the app unguarded (to demonstrate the attack);
    ``csrf_secret`` additionally requires a session-bound ``X-CSRF-Token`` header;
    ``lenient_json=True`` parses any body as JSON whatever its ``Content-Type``, the
    FastAPI < 0.65.2 behavior behind CVE-2021-32677.
    """

    async def _transfer(user: str, request: Request) -> JSONResponse:
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type != "application/json" and not lenient_json:
            return JSONResponse({"detail": "expected application/json"}, status_code=415)
        payload = await request.json()
        to, amount = str(payload["to"]), int(payload["amount"])
        bank.balances[user] = bank.balances.get(user, 0) - amount
        bank.balances[to] = bank.balances.get(to, 0) + amount
        return JSONResponse({"from": user, "to": to, "amount": amount})

    async def cookie_transfer(request: Request) -> JSONResponse:
        sid = request.cookies.get(SESSION_COOKIE, "")
        user = bank.sessions.get(sid)
        if user is None:
            return JSONResponse({"detail": "no session"}, status_code=401)
        if csrf_secret is not None and not verify_csrf_token(
            csrf_secret, sid, request.headers.get(CSRF_HEADER, "")
        ):
            return JSONResponse({"detail": "bad csrf token"}, status_code=403)
        return await _transfer(user, request)

    async def bearer_transfer(request: Request) -> JSONResponse:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        user = bank.bearer_tokens.get(token) if scheme.lower() == "bearer" else None
        if user is None:
            return JSONResponse(
                {"detail": "bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await _transfer(user, request)

    middleware = [] if policy is None else [Middleware(CrossOriginGuardMiddleware, policy=policy)]
    return Starlette(
        routes=[
            Route("/cookie/transfer", cookie_transfer, methods=["POST"]),
            Route("/bearer/transfer", bearer_transfer, methods=["POST"]),
        ],
        middleware=middleware,
    )
