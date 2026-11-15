"""Backend-for-Frontend (BFF) OAuth client after RFC 10017 Section 6.1, built on FastAPI.

The browser only ever holds one opaque `__Host-Http-` session cookie.
The BFF is a confidential client (client_secret_basic plus PKCE S256), keeps access and
refresh tokens in an AES-256-GCM encrypted server-side store, and proxies an explicit
allowlist of resource-server routes, swapping the cookie for a bearer token.
An in-process authorization server and resource server sit behind `httpx.MockTransport`,
and `SimulatedBrowser` models the cookie rules a browser applies (name prefixes, SameSite).
Everything is deterministic: time comes from an injected clock, randomness from `secrets`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

SESSION_COOKIE = "__Host-Http-bff-session"
TXN_COOKIE = "__Host-Http-bff-txn"
CSRF_HEADER = "x-bff-csrf"  # RFC 10017 Section 6.1.3.3.2: a static custom header forces a preflight
FORWARDED_REQUEST_HEADERS = ("accept", "content-type")
SameSite = Literal["lax", "strict", "none"]


# --- PKCE (RFC 7636) ---


def b64url(data: bytes) -> str:
    """Base64url without padding (RFC 7636 Appendix A)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def new_code_verifier() -> str:
    """32 random octets -> a 43-character verifier (RFC 7636 Section 4.1)."""
    return b64url(secrets.token_bytes(32))


def s256_challenge(verifier: str) -> str:
    """code_challenge = BASE64URL(SHA256(ASCII(code_verifier))) (RFC 7636 Section 4.2)."""
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


# --- Cookie rules a browser applies ---


def parse_set_cookie(header: str) -> tuple[str, str, dict[str, str]]:
    """Split a `Set-Cookie` value into name, value, and lowercased attribute names."""
    pair, *raw_attrs = header.split(";")
    name, _, value = pair.partition("=")
    attrs: dict[str, str] = {}
    for raw in raw_attrs:
        key, _, val = raw.strip().partition("=")
        attrs[key.strip().lower()] = val.strip()
    return name.strip(), value.strip(), attrs


def prefix_rejection(header: str) -> str | None:
    """Why a conforming user agent drops this cookie for its name prefix, or None.

    Prefix matching is case-insensitive (draft-ietf-httpbis-layered-cookies-02 Section 5.4.3,
    steps 13-16); requirements follow Section 4.1.3.1 to 4.1.3.4.
    """
    name, _, attrs = parse_set_cookie(header)
    lowered = name.lower()
    secure = "secure" in attrs
    host_only = secure and "domain" not in attrs and attrs.get("path") == "/"
    if lowered.startswith("__host-http-"):
        if not (host_only and "httponly" in attrs):
            return "__Host-Http- needs Secure, HttpOnly, Path=/ and no Domain"
    elif lowered.startswith("__host-"):
        if not host_only:
            return "__Host- needs Secure, Path=/ and no Domain"
    elif lowered.startswith("__http-"):
        if not (secure and "httponly" in attrs):
            return "__Http- needs Secure and HttpOnly"
    elif lowered.startswith("__secure-") and not secure:
        return "__Secure- needs Secure"
    return None


class RequestContext(StrEnum):
    """How a request was initiated, which is all SameSite enforcement looks at."""

    SAME_SITE = "same-site"
    CROSS_SITE_TOP_LEVEL_GET = "cross-site top-level GET navigation"
    CROSS_SITE_TOP_LEVEL_POST = "cross-site top-level POST navigation"
    CROSS_SITE_SUBRESOURCE = "cross-site subresource"


def samesite_allows(samesite: str, context: RequestContext) -> bool:
    """SameSite delivery rule (draft-ietf-httpbis-rfc6265bis-22 Section 5.6.7.1).

    Strict: same-site only. Lax (and an absent attribute): also cross-site top-level
    navigations with a safe method. None: every request.
    """
    if context is RequestContext.SAME_SITE or samesite == "none":
        return True
    if samesite == "strict":
        return False
    return context is RequestContext.CROSS_SITE_TOP_LEVEL_GET


@dataclass(slots=True)
class BrowserCookie:
    value: str
    samesite: str


@dataclass(slots=True)
class SimulatedBrowser:
    """A cookie jar with prefix and SameSite enforcement in front of an `httpx.AsyncClient`."""

    client: httpx.AsyncClient
    jar: dict[str, BrowserCookie] = field(default_factory=dict[str, BrowserCookie])
    rejected: list[str] = field(default_factory=list[str])

    def receive(self, response: httpx.Response) -> None:
        for header in response.headers.get_list("set-cookie"):
            reason = prefix_rejection(header)
            if reason is not None:
                self.rejected.append(reason)
                continue
            name, value, attrs = parse_set_cookie(header)
            if attrs.get("max-age") == "0":
                self.jar.pop(name, None)
            else:
                self.jar[name] = BrowserCookie(value, attrs.get("samesite", "lax").lower())

    async def request(
        self,
        method: str,
        url: str,
        *,
        context: RequestContext = RequestContext.SAME_SITE,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        sent = {k: v for k, v in (headers or {}).items()}
        cookies = [
            f"{n}={c.value}" for n, c in self.jar.items() if samesite_allows(c.samesite, context)
        ]
        if cookies:
            sent["cookie"] = "; ".join(cookies)
        response = await self.client.request(method, url, headers=sent, content=content)
        self.client.cookies.clear()  # this jar, not httpx's, decides what is sent
        self.receive(response)
        return response


# --- Encrypted server-side token store ---


@dataclass(frozen=True, slots=True)
class TokenSet:
    sub: str
    scope: str
    access_token: str
    refresh_token: str
    access_expires_at: float
    session_expires_at: float


class EncryptedSessionStore:
    """Rows keyed by SHA-256(session id), each sealed with AES-256-GCM.

    The lookup key is the associated data, so a row copied under another session's key fails
    authentication, and a dump of `rows` reveals neither tokens nor usable cookie values.
    Blob layout: `kid` ASCII, `.`, 12-byte nonce, ciphertext plus 16-byte tag.
    """

    def __init__(
        self, keys: Mapping[str, bytes], active_kid: str, rows: dict[str, bytes] | None = None
    ) -> None:
        self._keys = {kid: AESGCM(key) for kid, key in keys.items()}
        self._active_kid = active_kid
        self.rows: dict[str, bytes] = {} if rows is None else rows

    @staticmethod
    def lookup_key(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("ascii")).hexdigest()

    def put(self, session_id: str, tokens: TokenSet) -> None:
        lookup = self.lookup_key(session_id)
        nonce = os.urandom(12)
        sealed = self._keys[self._active_kid].encrypt(
            nonce, json.dumps(asdict(tokens)).encode(), lookup.encode()
        )
        self.rows[lookup] = self._active_kid.encode() + b"." + nonce + sealed

    def get(self, session_id: str) -> TokenSet | None:
        lookup = self.lookup_key(session_id)
        blob = self.rows.get(lookup)
        if blob is None:
            return None
        kid, _, rest = blob.partition(b".")
        aead = self._keys.get(kid.decode())
        if aead is None:
            return None
        try:
            plain = aead.decrypt(rest[:12], rest[12:], lookup.encode())
        except InvalidTag:
            return None
        return TokenSet(**json.loads(plain))

    def kid_of(self, session_id: str) -> str:
        return self.rows[self.lookup_key(session_id)].partition(b".")[0].decode()

    def delete(self, session_id: str) -> None:
        self.rows.pop(self.lookup_key(session_id), None)


def cached_token_reusable(cached_scope: str, requested_scope: str) -> bool:
    """Token-mediating backend rule (RFC 10017 Section 6.2.2.3).

    A cached token with a superset of the requested scopes SHOULD NOT be handed out; a subset
    cannot satisfy the request; so only an equal scope set is reusable.
    """
    return set(cached_scope.split()) == set(requested_scope.split())


# --- In-process authorization server and resource server ---


@dataclass(slots=True)
class _Code:
    client_id: str
    redirect_uri: str
    challenge: str
    sub: str
    scope: str
    used: bool = False


@dataclass(slots=True)
class _Refresh:
    family: str
    client_id: str
    sub: str
    scope: str
    absolute_expires_at: float
    active: bool = True


@dataclass(slots=True)
class _Access:
    family: str
    sub: str
    scope: str
    expires_at: float


def _oauth_error(status: int, error: str) -> httpx.Response:
    return httpx.Response(status, json={"error": error}, headers={"cache-control": "no-store"})


def basic_client_auth(client_id: str, secret: str) -> str:
    """client_secret_basic: form-urlencode both halves, then Basic (RFC 6749 Section 2.3.1)."""
    raw = f"{quote_plus(client_id)}:{quote_plus(secret)}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


class AuthorizationServer:
    """Confidential-client token endpoint with PKCE and refresh token rotation (RFC 9700)."""

    issuer = "https://as.example"

    def __init__(
        self,
        now: Callable[[], float],
        clients: Mapping[str, str],
        redirect_uris: Mapping[str, str],
        access_ttl: int = 600,
        refresh_ttl: int = 8 * 3600,
    ) -> None:
        self._now = now
        self._clients = dict(clients)
        self._redirect_uris = dict(redirect_uris)
        self.access_ttl = access_ttl
        self.refresh_ttl = refresh_ttl
        self.codes: dict[str, _Code] = {}
        self.refresh_tokens: dict[str, _Refresh] = {}
        self.access_tokens: dict[str, _Access] = {}
        self.revoked_families: set[str] = set()
        self.grants: list[str] = []
        self.reuse_detected = 0

    def authorize(self, authorize_url: str, *, sub: str, iss: str | None = None) -> str:
        """Simulate a logged-in user approving the request; return the redirect back."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        client_id = query["client_id"]
        if query.get("redirect_uri") != self._redirect_uris.get(client_id):
            raise ValueError("redirect_uri is not an exact match (RFC 9700 Section 2.1)")
        if query.get("response_type") != "code" or query.get("code_challenge_method") != "S256":
            raise ValueError("only response_type=code with PKCE S256 is accepted")
        code = secrets.token_urlsafe(24)
        self.codes[code] = _Code(
            client_id, query["redirect_uri"], query["code_challenge"], sub, query["scope"]
        )
        params = {"code": code, "state": query["state"], "iss": iss or self.issuer}
        return f"{query['redirect_uri']}?{urlencode(params)}"

    def _client(self, request: httpx.Request) -> str | None:
        scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "basic":
            return None
        try:
            raw_id, _, raw_secret = base64.b64decode(encoded, validate=True).decode().partition(":")
        except ValueError:  # binascii.Error and UnicodeDecodeError both subclass it
            return None
        client_id, secret = unquote_plus(raw_id), unquote_plus(raw_secret)
        expected = self._clients.get(client_id)
        if expected is None or not hmac.compare_digest(secret, expected):
            return None
        return client_id

    def _issue(
        self, family: str, client_id: str, sub: str, scope: str, absolute_expires_at: float
    ) -> dict[str, object]:
        access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
        self.access_tokens[access] = _Access(family, sub, scope, self._now() + self.access_ttl)
        self.refresh_tokens[refresh] = _Refresh(family, client_id, sub, scope, absolute_expires_at)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.access_ttl,
            "refresh_token": refresh,
            "scope": scope,
        }

    async def token(self, request: httpx.Request) -> httpx.Response:
        client_id = self._client(request)
        if client_id is None:
            return _oauth_error(401, "invalid_client")
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        grant = form.get("grant_type", "")
        self.grants.append(grant)
        if grant == "authorization_code":
            code = self.codes.get(form.get("code", ""))
            verifier = form.get("code_verifier", "")
            if (
                code is None
                or code.used
                or code.client_id != client_id
                or code.redirect_uri != form.get("redirect_uri")
                or not hmac.compare_digest(s256_challenge(verifier), code.challenge)
            ):
                return _oauth_error(400, "invalid_grant")
            code.used = True
            family = secrets.token_hex(8)
            body = self._issue(
                family, client_id, code.sub, code.scope, self._now() + self.refresh_ttl
            )
        elif grant == "refresh_token":
            for _ in range(3):  # a network round trip: lets concurrent refreshes interleave
                await asyncio.sleep(0)
            old = self.refresh_tokens.get(form.get("refresh_token", ""))
            if old is None or old.client_id != client_id or old.family in self.revoked_families:
                return _oauth_error(400, "invalid_grant")
            if not old.active:  # RFC 9700 Section 4.14.2: replay of a rotated token
                self.reuse_detected += 1
                self.revoked_families.add(old.family)
                return _oauth_error(400, "invalid_grant")
            if self._now() >= old.absolute_expires_at:
                return _oauth_error(400, "invalid_grant")
            old.active = False
            # RFC 10017 Section 6.3.2.3: rotation never extends the original lifetime.
            body = self._issue(old.family, client_id, old.sub, old.scope, old.absolute_expires_at)
        else:
            return _oauth_error(400, "unsupported_grant_type")
        return httpx.Response(200, json=body, headers={"cache-control": "no-store"})

    def refresh_expires_at(self, refresh_token: str) -> float:
        return self.refresh_tokens[refresh_token].absolute_expires_at

    async def revoke(self, request: httpx.Request) -> httpx.Response:
        if self._client(request) is None:
            return _oauth_error(401, "invalid_client")
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        record = self.refresh_tokens.get(form.get("token", ""))
        if record is not None:
            self.revoked_families.add(record.family)  # also kills the grant's access tokens
        return httpx.Response(200)  # RFC 7009 Section 2.2: 200 even for an unknown token

    def access_valid(self, token: str) -> _Access | None:
        record = self.access_tokens.get(token)
        if record is None or record.family in self.revoked_families:
            return None
        return record if self._now() < record.expires_at else None


@dataclass(slots=True)
class Upstreams:
    """Routes the BFF's outbound requests by host and records every one of them."""

    auth: AuthorizationServer
    sent: list[httpx.Request] = field(default_factory=list[httpx.Request])

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        host, path = request.url.host, request.url.path
        if host == "as.example" and path == "/token":
            return await self.auth.token(request)
        if host == "as.example" and path == "/revoke":
            return await self.auth.revoke(request)
        if host == "as.example" and path == "/userinfo":
            record = self._bearer(request)
            if record is None:
                return httpx.Response(401)
            return httpx.Response(200, json={"sub": record.sub})
        if host == "api.example":
            return self._resource(request)
        return httpx.Response(599, text=f"unexpected upstream {host}")

    def _bearer(self, request: httpx.Request) -> _Access | None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        return self.auth.access_valid(token) if scheme == "Bearer" else None

    def _resource(self, request: httpx.Request) -> httpx.Response:
        record = self._bearer(request)
        if record is None:
            return httpx.Response(401, headers={"www-authenticate": 'Bearer error="invalid_token"'})
        body = {
            "sub": record.sub,
            "method": request.method,
            "path": request.url.path,
            "saw_cookie": "cookie" in request.headers,
        }
        return httpx.Response(200, json=body, headers={"set-cookie": "rs-tracker=1; Path=/"})

    def to(self, host: str) -> list[httpx.Request]:
        return [r for r in self.sent if r.url.host == host]


# --- The BFF ---


@dataclass(frozen=True, slots=True)
class ProxyRoute:
    """One allowlisted path, matched with `fullmatch` against the path only (never the query)."""

    pattern: re.Pattern[str]
    methods: frozenset[str]
    upstream: str  # `str.format` template filled from the pattern's named groups


DEFAULT_ROUTES = (
    ProxyRoute(re.compile(r"orders"), frozenset({"GET", "POST"}), "https://api.example/orders"),
    ProxyRoute(
        re.compile(r"orders/(?P<order_id>[0-9]{1,12})"),
        frozenset({"GET", "DELETE"}),
        "https://api.example/orders/{order_id}",
    ),
)


@dataclass(frozen=True, slots=True)
class BffSettings:
    client_id: str = "spa-bff"
    client_secret: str = "bff-secret-EXAMPLE-NOT-REAL"  # noqa: S105 - lab placeholder
    issuer: str = "https://as.example"
    authorization_endpoint: str = "https://as.example/authorize"
    token_endpoint: str = "https://as.example/token"  # noqa: S105 - a URL, not a secret
    revocation_endpoint: str = "https://as.example/revoke"
    userinfo_endpoint: str = "https://as.example/userinfo"
    redirect_uri: str = "https://bff.example/bff/callback"
    scope: str = "openid orders:read orders:write"
    session_lifetime: int = 8 * 3600  # RFC 10017 Section 6.1.2.2: equal to the refresh token's
    txn_lifetime: int = 600
    txn_samesite: SameSite = "lax"  # must reach the cross-site callback navigation
    refresh_skew: int = 30


@dataclass(frozen=True, slots=True)
class _Txn:
    state: str
    verifier: str
    return_to: str
    expires_at: float


def safe_return_path(value: str | None) -> str | None:
    """Accept only a local absolute path, refusing open redirects (RFC 9700 Section 2.1)."""
    if value is None:
        return "/"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return None
    return value


class Bff:
    def __init__(
        self,
        settings: BffSettings,
        store: EncryptedSessionStore,
        upstream: httpx.AsyncClient,
        now: Callable[[], float],
        routes: tuple[ProxyRoute, ...] = DEFAULT_ROUTES,
    ) -> None:
        self.settings = settings
        self.store = store
        self.upstream = upstream
        self.now = now
        self.routes = routes
        self.txns: dict[str, _Txn] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.app = self._build_app()

    def _client_auth(self) -> dict[str, str]:
        return {
            "authorization": basic_client_auth(self.settings.client_id, self.settings.client_secret)
        }

    def _set_session_cookie(self, response: Response, session_id: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=self.settings.session_lifetime,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",  # RFC 10017 Section 6.1.3.2; no Domain attribute
        )

    def _clear_session_cookie(self, response: Response) -> None:
        # A deletion is a Set-Cookie too: it must satisfy the prefix or the browser ignores it.
        response.delete_cookie(
            SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict"
        )

    async def fresh_tokens(self, session_id: str) -> TokenSet | None:
        """Return usable tokens, refreshing inline at most once per session at a time."""
        tokens = self.store.get(session_id)
        if tokens is None or self.now() >= tokens.session_expires_at:
            self.store.delete(session_id)
            return None
        if self.now() < tokens.access_expires_at - self.settings.refresh_skew:
            return tokens
        lock = self.locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            tokens = self.store.get(session_id)  # another request may have refreshed meanwhile
            if tokens is None:
                return None
            if self.now() < tokens.access_expires_at - self.settings.refresh_skew:
                return tokens
            response = await self.upstream.post(
                self.settings.token_endpoint,
                content=urlencode(
                    {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token}
                ),
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    **self._client_auth(),
                },
            )
            if response.status_code != 200:
                # RFC 10017 Section 6.1.2.2: a dead refresh token ends the session.
                self.store.delete(session_id)
                self.locks.pop(session_id, None)
                return None
            body = response.json()
            refreshed = replace(
                tokens,
                access_token=str(body["access_token"]),
                refresh_token=str(body.get("refresh_token", tokens.refresh_token)),
                access_expires_at=self.now() + float(body["expires_in"]),
            )
            self.store.put(session_id, refreshed)
            return refreshed

    def _build_app(self) -> FastAPI:
        app = FastAPI()
        settings = self.settings

        @app.get("/bff/login")
        async def login(request: Request) -> Response:
            return_to = safe_return_path(request.query_params.get("return_to"))
            if return_to is None:
                return JSONResponse({"error": "return_to must be a local path"}, status_code=400)
            txn_id, verifier = secrets.token_urlsafe(32), new_code_verifier()
            state = secrets.token_urlsafe(32)
            self.txns[txn_id] = _Txn(state, verifier, return_to, self.now() + settings.txn_lifetime)
            query = urlencode(
                {
                    "response_type": "code",
                    "client_id": settings.client_id,
                    "redirect_uri": settings.redirect_uri,
                    "scope": settings.scope,
                    "state": state,
                    "code_challenge": s256_challenge(verifier),
                    "code_challenge_method": "S256",
                }
            )
            response = RedirectResponse(
                f"{settings.authorization_endpoint}?{query}", status_code=302
            )
            response.set_cookie(
                TXN_COOKIE,
                txn_id,
                max_age=settings.txn_lifetime,
                path="/",
                secure=True,
                httponly=True,
                samesite=settings.txn_samesite,
            )
            return response

        @app.get("/bff/callback")
        async def callback(request: Request) -> Response:
            txn = self.txns.pop(request.cookies.get(TXN_COOKIE, ""), None)  # single use
            if txn is None or self.now() >= txn.expires_at:
                return JSONResponse({"error": "no login transaction"}, status_code=400)
            params = request.query_params
            if not hmac.compare_digest(params.get("state", ""), txn.state):
                return JSONResponse({"error": "state mismatch"}, status_code=400)
            if params.get("iss") != settings.issuer:  # RFC 9207 Section 2.4
                return JSONResponse({"error": "issuer mismatch"}, status_code=400)
            if "error" in params or "code" not in params:
                return JSONResponse({"error": params.get("error", "no code")}, status_code=400)
            exchange = await self.upstream.post(
                settings.token_endpoint,
                content=urlencode(
                    {
                        "grant_type": "authorization_code",
                        "code": params["code"],
                        "redirect_uri": settings.redirect_uri,
                        "code_verifier": txn.verifier,
                    }
                ),
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    **self._client_auth(),
                },
            )
            if exchange.status_code != 200:
                return JSONResponse({"error": "token exchange failed"}, status_code=400)
            body = exchange.json()
            session_id = secrets.token_urlsafe(32)  # always a fresh id: no session fixation
            now = self.now()
            self.store.put(
                session_id,
                TokenSet(
                    sub=await self._userinfo_sub(str(body["access_token"])),
                    scope=str(body["scope"]),
                    access_token=str(body["access_token"]),
                    refresh_token=str(body["refresh_token"]),
                    access_expires_at=now + float(body["expires_in"]),
                    session_expires_at=now + settings.session_lifetime,
                ),
            )
            response = RedirectResponse(txn.return_to, status_code=303)
            self._set_session_cookie(response, session_id)
            response.delete_cookie(TXN_COOKIE, path="/", secure=True, httponly=True)
            return response

        @app.get("/bff/session")
        async def check_session(request: Request) -> Response:
            if request.headers.get(CSRF_HEADER) != "1":
                return JSONResponse({"error": "missing CSRF header"}, status_code=403)
            tokens = await self.fresh_tokens(request.cookies.get(SESSION_COOKIE, ""))
            body: dict[str, object] = {"authenticated": False}
            if tokens is not None:
                body = {"authenticated": True, "sub": tokens.sub, "scope": tokens.scope}
            return JSONResponse(body, headers={"cache-control": "no-store"})

        @app.post("/bff/logout")
        async def logout(request: Request) -> Response:
            if request.headers.get(CSRF_HEADER) != "1":
                return JSONResponse({"error": "missing CSRF header"}, status_code=403)
            session_id = request.cookies.get(SESSION_COOKIE, "")
            tokens = self.store.get(session_id)
            if tokens is not None:
                await self.upstream.post(
                    settings.revocation_endpoint,
                    content=urlencode(
                        {"token": tokens.refresh_token, "token_type_hint": "refresh_token"}
                    ),
                    headers={
                        "content-type": "application/x-www-form-urlencoded",
                        **self._client_auth(),
                    },
                )
            self.store.delete(session_id)
            self.locks.pop(session_id, None)
            response = Response(status_code=204)
            self._clear_session_cookie(response)
            return response

        @app.api_route("/bff/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
        async def proxy(request: Request, path: str) -> Response:
            if request.headers.get(CSRF_HEADER) != "1":
                return JSONResponse({"error": "missing CSRF header"}, status_code=403)
            route, match = self._route(path)
            if route is None or match is None:
                return JSONResponse({"error": "no such route"}, status_code=404)
            if request.method not in route.methods:
                return JSONResponse({"error": "method not allowed"}, status_code=405)
            session_id = request.cookies.get(SESSION_COOKIE, "")
            tokens = await self.fresh_tokens(session_id)
            if tokens is None:
                response: Response = JSONResponse({"error": "login required"}, status_code=401)
                self._clear_session_cookie(response)
                return response
            headers = {k: v for k in FORWARDED_REQUEST_HEADERS if (v := request.headers.get(k))}
            headers["authorization"] = f"Bearer {tokens.access_token}"  # cookie is not forwarded
            upstream = await self.upstream.request(
                request.method,
                route.upstream.format(**match.groupdict()),
                params=list(request.query_params.multi_items()),
                headers=headers,
                content=await request.body(),
            )
            media_type = upstream.headers.get("content-type")
            return Response(
                upstream.content, status_code=upstream.status_code, media_type=media_type
            )

        return app

    def _route(self, path: str) -> tuple[ProxyRoute | None, re.Match[str] | None]:
        for route in self.routes:
            match = route.pattern.fullmatch(path)
            if match is not None:
                return route, match
        return None, None

    async def _userinfo_sub(self, access_token: str) -> str:
        """Ask the UserInfo endpoint who the user is (RFC 10017 Section 6.1.2.4)."""
        response = await self.upstream.get(
            self.settings.userinfo_endpoint, headers={"authorization": f"Bearer {access_token}"}
        )
        return str(response.json()["sub"]) if response.status_code == 200 else "unknown"


@dataclass(slots=True)
class FakeClock:
    t: float = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@dataclass(slots=True)
class Lab:
    clock: FakeClock
    auth: AuthorizationServer
    upstreams: Upstreams
    bff: Bff


def build_lab(settings: BffSettings | None = None) -> Lab:
    """Wire the AS, resource server, encrypted store, and BFF together for one test."""
    settings = settings or BffSettings()
    clock = FakeClock()
    auth = AuthorizationServer(
        clock,
        {settings.client_id: settings.client_secret},
        {settings.client_id: settings.redirect_uri},
    )
    upstreams = Upstreams(auth)
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstreams.handle))
    store = EncryptedSessionStore({"k1": AESGCM.generate_key(bit_length=256)}, "k1")
    return Lab(clock, auth, upstreams, Bff(settings, store, client, clock))
