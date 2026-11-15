"""Starlette security middlewares: signed-cookie sessions, authentication, host and scheme guards.

Lab for the note Starlette-AuthenticationMiddleware-SessionMiddleware-and-TrustedHost.
Pinned: starlette 1.10.0, itsdangerous 2.2.0, uvicorn 0.54.0.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import itsdangerous
from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    BaseUser,
    SimpleUser,
    requires,
)
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_MAX_AGE = 14 * 24 * 60 * 60  # SessionMiddleware default, in seconds
ITSDANGEROUS_DEFAULT_SALT = b"itsdangerous.Signer"
HMAC_SHA1_B64_LEN = 27  # 20 raw bytes -> 27 base64url characters without padding

# Obviously fake demo credential; never a real token format.
DEMO_BEARER: dict[str, tuple[str, tuple[str, ...]]] = {
    "demo-token-EXAMPLE-NOT-REAL": ("carol", ("authenticated", "admin")),
}


@dataclass
class Clock:
    """Mutable wall clock in integer seconds, shared by the signer and the test."""

    now: int


class ClockedTimestampSigner(itsdangerous.TimestampSigner):
    """TimestampSigner that reads a test clock instead of `time.time()`."""

    def __init__(self, secret_keys: Sequence[str], clock: Clock) -> None:
        super().__init__(list(secret_keys))
        self.clock = clock

    def get_timestamp(self) -> int:
        return self.clock.now


class ClockedSessionMiddleware(SessionMiddleware):
    """SessionMiddleware with an injectable clock and a rotation list of keys, oldest first.

    Stock SessionMiddleware calls `str(secret_key)`, so it cannot take a key list;
    replacing `self.signer` is the only way to verify with several keys.
    """

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


def _b64url_nopad(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_session_cookie(session: dict[str, Any], secret_key: str, timestamp: int) -> str:
    """Rebuild the exact cookie value SessionMiddleware emits, from hashlib, hmac, and base64 only.

    Layout: base64(json) "." base64url(timestamp) "." base64url(HMAC-SHA1(key, first two parts)),
    where key = SHA1(salt + b"signer" + secret) (itsdangerous "django-concat" derivation).
    Anyone holding `secret_key` can mint a session this way: the key is the whole security model.
    """
    payload = base64.b64encode(json.dumps(session).encode("utf-8"))
    stamp = _b64url_nopad(timestamp.to_bytes(8, "big").lstrip(b"\x00"))
    signed = payload + b"." + stamp
    # SHA-1 mirrors the itsdangerous 2.2.0 default; this function exists to reproduce it.
    key = hashlib.sha1(ITSDANGEROUS_DEFAULT_SALT + b"signer" + secret_key.encode()).digest()  # noqa: S324
    signature = _b64url_nopad(hmac.new(key, signed, hashlib.sha1).digest())
    return (signed + b"." + signature).decode("ascii")


@dataclass(frozen=True)
class DecodedCookie:
    session: dict[str, Any]
    timestamp: int
    signature: bytes


def decode_session_cookie(value: str) -> DecodedCookie:
    """Read a session cookie without the key: the payload is signed, not encrypted."""
    payload, stamp, signature = value.split(".")
    session = cast("dict[str, Any]", json.loads(base64.b64decode(payload)))
    timestamp = int.from_bytes(_b64url_decode(stamp), "big")
    return DecodedCookie(session, timestamp, _b64url_decode(signature))


def signed_cookie_value_length(json_bytes: int, timestamp: int) -> int:
    """Length of the cookie value for a JSON document of `json_bytes` bytes."""
    payload = 4 * -(-json_bytes // 3)  # standard base64 keeps its "=" padding
    stamp = len(_b64url_nopad(timestamp.to_bytes(8, "big").lstrip(b"\x00")))
    return payload + 1 + stamp + 1 + HMAC_SHA1_B64_LEN


class SessionOrBearerBackend(AuthenticationBackend):
    """`Authorization: Bearer` wins; otherwise the user comes from the signed session."""

    async def authenticate(self, conn: HTTPConnection) -> tuple[AuthCredentials, BaseUser] | None:
        authorization = conn.headers.get("authorization")
        if authorization is not None:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or token not in DEMO_BEARER:
                raise AuthenticationError("invalid bearer token")
            username, scopes = DEMO_BEARER[token]
            return AuthCredentials(scopes), SimpleUser(username)
        session_user = conn.session.get("user")  # marks the session accessed -> Vary: Cookie
        if not isinstance(session_user, str):
            return None
        raw_scopes: object = conn.session.get("scopes", [])
        session_scopes: list[str] = []
        if isinstance(raw_scopes, list):
            session_scopes = [s for s in cast("list[object]", raw_scopes) if isinstance(s, str)]
        return AuthCredentials(session_scopes), SimpleUser(session_user)


class HTTPExceptionBackend(AuthenticationBackend):
    """Raises HTTPException from the backend, which runs outside ExceptionMiddleware."""

    async def authenticate(self, conn: HTTPConnection) -> tuple[AuthCredentials, BaseUser] | None:
        raise HTTPException(status_code=401, detail="no")


def json_401_on_error(conn: HTTPConnection, exc: AuthenticationError) -> Response:
    """A non-leaking `on_error`: fixed body, `401`, and a challenge (RFC 9110 Section 11.6.1)."""
    return JSONResponse(
        {"detail": "invalid credentials"},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def login(request: Request) -> Response:
    request.session["user"] = "alice"
    request.session["scopes"] = ["authenticated"]
    return PlainTextResponse("logged in")


async def logout(request: Request) -> Response:
    request.session.clear()
    return PlainTextResponse("logged out")


@requires("authenticated")
async def me(request: Request) -> Response:
    return PlainTextResponse(request.user.display_name)


@requires("authenticated", redirect="login_page")
async def dashboard(request: Request) -> Response:
    return PlainTextResponse("dashboard")


async def login_page(request: Request) -> Response:
    return PlainTextResponse("login form")


async def peek(request: Request) -> Response:
    return JSONResponse({"user": request.session.get("user")})


async def public(request: Request) -> Response:
    return PlainTextResponse("public")


async def cart_init(request: Request) -> Response:
    request.session["cart"] = []
    return JSONResponse(request.session["cart"])


async def cart_append_in_place(request: Request) -> Response:
    cart = cast("list[str]", request.session["cart"])
    cart.append("book")  # mutates the nested list; Session.__setitem__ never runs
    return JSONResponse(cart)


async def cart_reassign(request: Request) -> Response:
    cart = cast("list[str]", request.session["cart"])
    request.session["cart"] = [*cart, "book"]  # top-level assignment marks the session modified
    return JSONResponse(request.session["cart"])


async def reset_link(request: Request) -> Response:
    return PlainTextResponse(str(request.url_for("reset", code="c-EXAMPLE")))


async def reset(request: Request) -> Response:
    return PlainTextResponse("reset form")


ROUTES = [
    Route("/login", login, methods=["POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/me", me),
    Route("/dashboard", dashboard),
    Route("/login-page", login_page, name="login_page"),
    Route("/peek", peek),
    Route("/public", public),
    Route("/cart/init", cart_init, methods=["POST"]),
    Route("/cart/append-in-place", cart_append_in_place, methods=["POST"]),
    Route("/cart/reassign", cart_reassign, methods=["POST"]),
    Route("/reset-link", reset_link),
    Route("/reset/{code}", reset, name="reset"),
]

OnError = Callable[[HTTPConnection, AuthenticationError], Response]


def build_app(
    *,
    secret_keys: Sequence[str],
    clock: Clock,
    allowed_hosts: Sequence[str] | None = None,
    https_redirect: bool = False,
    authenticate: bool = True,
    on_error: OnError | None = None,
    max_age: int | None = DEFAULT_MAX_AGE,
    https_only: bool = False,
) -> Starlette:
    """Outermost first: HTTPSRedirect -> TrustedHost -> Session -> Authentication -> routes."""
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


def build_misordered_app(*, secret_keys: Sequence[str], clock: Clock) -> Starlette:
    """Authentication outside Session: the backend runs before `scope["session"]` exists."""
    return Starlette(
        routes=ROUTES,
        middleware=[
            Middleware(AuthenticationMiddleware, backend=SessionOrBearerBackend()),
            Middleware(ClockedSessionMiddleware, secret_keys=secret_keys, clock=clock),
        ],
    )


def build_http_exception_backend_app() -> Starlette:
    return Starlette(
        routes=ROUTES,
        middleware=[Middleware(AuthenticationMiddleware, backend=HTTPExceptionBackend())],
    )


def http_scope(path: str, headers: Sequence[tuple[bytes, bytes]], scheme: str = "http") -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": list(headers),
        "client": ("127.0.0.1", 50000),
        "server": ("10.0.0.5", 8000),
    }


def websocket_scope(path: str, headers: Sequence[tuple[bytes, bytes]], scheme: str = "ws") -> Scope:
    scope = http_scope(path, headers, scheme)
    del scope["method"]
    scope["type"] = "websocket"
    scope["subprotocols"] = []
    return scope


async def drive_asgi(app: ASGIApp, scope: Scope, inbound: Sequence[Message] = ()) -> list[Message]:
    """Call an ASGI app once with scripted `receive()` events and record every `send()`."""
    pending = list(inbound)
    sent: list[Message] = []

    async def receive() -> Message:
        if pending:
            return pending.pop(0)
        if scope["type"] == "websocket":
            return {"type": "websocket.disconnect", "code": 1000}
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


async def websocket_session_writer(scope: Scope, receive: Receive, send: Send) -> None:
    """A WebSocket endpoint that writes to the session; nothing can ever persist it."""
    await receive()  # websocket.connect
    scope["session"]["seen"] = True
    await send({"type": "websocket.accept", "headers": []})
    await send({"type": "websocket.close", "code": 1000})


async def ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse("ok")(scope, receive, send)


async def trusted_host_status(allowed_hosts: Sequence[str], host: str | None) -> int:
    """Status TrustedHostMiddleware returns for one `Host` value (None means no header)."""
    guard = TrustedHostMiddleware(ok_app, allowed_hosts=allowed_hosts)
    headers = [] if host is None else [(b"host", host.encode("latin-1"))]
    sent = await drive_asgi(guard, http_scope("/", headers))
    return int(sent[0]["status"])
