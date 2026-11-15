"""OAuth 2.1 authorization code grant with the RFC 9700 hardening, in memory.

The server enforces the rules that separate OAuth 2.1 (draft-ietf-oauth-v2-1-17) and
RFC 9700 from RFC 6749:

* PKCE is required for public clients and for every OAuth 2.1 client; only ``S256`` is
  accepted, because ``plain`` puts the verifier in the authorization request
  (draft-ietf-oauth-v2-1-17 Sections 4.1.1 and 7.5.2, RFC 9700 Section 2.1.1).
* A ``code_verifier`` for a code issued without a ``code_challenge`` is rejected, which is the
  PKCE downgrade defense (RFC 9700 Section 4.8.2).
* Redirect URIs are compared by simple string comparison, except that the port of a loopback
  IP literal may vary (RFC 9700 Section 4.1.3, draft Sections 4.1.1 and 8.4.2).
* ``response_type=token`` and ``grant_type=password`` do not exist (draft Section 10).
* A code is redeemed at most once, atomically; a second *valid* redemption revokes every token
  issued from it, and an invalid one revokes nothing (draft Sections 4.1.3 and 7.5.3).
* Every authorization response carries ``iss`` (draft Section 4.1.2, RFC 9207).

Everything is in memory and deterministic apart from token values; the clock is injected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, unquote_plus, urlencode

from anyio.lowlevel import checkpoint
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from starlette.requests import Request

CODE_LIFETIME_SECONDS = 600  # "A maximum authorization code lifetime of 10 minutes is RECOMMENDED"
ACCESS_TOKEN_LIFETIME_SECONDS = 3600

# RFC 7636 Section 4.1 and draft Section 4.1.1: 43*128unreserved for both values.
_PKCE_VALUE = re.compile(r"[A-Za-z0-9\-._~]{43,128}", re.ASCII)
# Loopback IP literal redirect URI: http://127.0.0.1:{port}/{path} or http://[::1]:{port}/{path}.
_LOOPBACK = re.compile(
    r"http://(?P<host>127\.0\.0\.1|\[::1\])(?::(?P<port>[0-9]{1,5}))?(?P<rest>[/?].*)?",
    re.ASCII | re.DOTALL,
)


def s256_challenge(code_verifier: str) -> str:
    """``BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))`` without padding (RFC 7636 4.2)."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_code_verifier() -> str:
    """32 random octets, base64url-encoded to 43 characters (RFC 7636 Section 4.1 NOTE)."""
    return secrets.token_urlsafe(32)


def is_pkce_value(value: str) -> bool:
    """True when *value* matches ``43*128unreserved``."""
    return _PKCE_VALUE.fullmatch(value) is not None


def _without_loopback_port(uri: str) -> str | None:
    """Return *uri* with the port of a loopback IP literal removed, or ``None``."""
    match = _LOOPBACK.fullmatch(uri)
    if match is None:
        return None
    port = match["port"]
    if port is not None and not 1 <= int(port) <= 65535:
        return None
    return f"http://{match['host']}{match['rest'] or ''}"


def redirect_uri_matches(registered: str, presented: str) -> bool:
    """Simple string comparison (RFC 3986 Section 6.2.1), loopback port excepted.

    No normalization: case, percent-encoding, a trailing slash, or a default port all count.
    """
    if presented == registered:
        return True
    registered_base = _without_loopback_port(registered)
    return registered_base is not None and registered_base == _without_loopback_port(presented)


class RegistrationError(ValueError):
    """A redirect URI that a hardened authorization server refuses to register."""


def validate_redirect_registration(uri: str) -> None:
    """Reject redirect URIs that OAuth 2.1 and RFC 9700 do not allow to be registered."""
    scheme, sep, _ = uri.partition(":")
    if not sep or not scheme:
        raise RegistrationError("redirect URI must be absolute")
    if "#" in uri:
        raise RegistrationError("redirect URI MUST NOT include a fragment")
    if scheme == "https":
        return
    if scheme == "http":
        if _without_loopback_port(uri) is None:
            raise RegistrationError("http is allowed only for loopback IP literal redirects")
        return
    if "." not in scheme:
        raise RegistrationError("private-use scheme must be a reverse domain name")


@dataclass(frozen=True, slots=True)
class Client:
    """One client registration (draft Section 2).

    ``secret_sha256`` is ``None`` for a public client.  ``legacy_oauth20`` selects the OAuth 2.0
    rules for this client only, which is how an authorization server migrates clients one at a
    time (draft Section 1.8): PKCE stays optional for a confidential legacy client, and the
    token request must repeat ``redirect_uri`` (draft Section 10.2).
    """

    client_id: str
    redirect_uris: tuple[str, ...]
    secret_sha256: bytes | None = None
    legacy_oauth20: bool = False

    def __post_init__(self) -> None:
        if not self.redirect_uris:
            raise RegistrationError("at least one redirect URI is required")
        for uri in self.redirect_uris:
            validate_redirect_registration(uri)

    @property
    def is_public(self) -> bool:
        return self.secret_sha256 is None

    @property
    def pkce_required(self) -> bool:
        return self.is_public or not self.legacy_oauth20


def hash_client_secret(secret: str) -> bytes:
    """A fast hash is enough for a high-entropy, server-generated client secret."""
    return hashlib.sha256(secret.encode("utf-8")).digest()


class CodeState(Enum):
    ISSUED = "issued"
    REDEEMED = "redeemed"


@dataclass(slots=True)
class CodeRecord:
    client_id: str
    redirect_uri: str | None  # the value from the authorization request, if one was sent
    code_challenge: str | None
    expires_at: float
    state: CodeState = CodeState.ISSUED
    token_ids: list[str] = field(default_factory=lambda: [])


@dataclass(slots=True)
class TokenRecord:
    client_id: str
    code: str
    expires_at: float
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class ErrorPage:
    """An error shown to the user; never redirected (draft Section 4.1.2.1)."""

    status: int
    error: str


@dataclass(frozen=True, slots=True)
class Redirect:
    """A ``303 See Other`` back to the client's redirect URI."""

    location: str


@dataclass(frozen=True, slots=True)
class TokenResult:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=lambda: {})


class ParameterError(ValueError):
    """A parameter appeared more than once."""


def single_valued(pairs: Sequence[tuple[str, str]]) -> dict[str, str]:
    """Collapse form or query pairs, enforcing the protocol's parameter rules.

    Empty values are treated as omitted, and a repeated parameter is an error
    (draft Section 3.2, RFC 6749 Section 3.1).
    """
    seen: dict[str, str] = {}
    for name, value in pairs:
        if name in seen:
            raise ParameterError(name)
        seen[name] = value
    return {name: value for name, value in seen.items() if value != ""}


def _append_query(uri: str, params: Mapping[str, str]) -> str:
    """Add response parameters while retaining the redirect URI's own query."""
    separator = "&" if "?" in uri else "?"
    return f"{uri}{separator}{urlencode(params)}"


def _token_error(error: str, status: int = 400, **headers: str) -> TokenResult:
    return TokenResult(status, {"error": error}, {"Cache-Control": "no-store", **headers})


class AuthorizationServer:
    """The authorization and token endpoint logic, independent of HTTP framing."""

    def __init__(
        self,
        issuer: str,
        clients: Sequence[Client],
        *,
        clock: Callable[[], float],
        storage_io: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.issuer = issuer
        self.clients = {client.client_id: client for client in clients}
        self.codes: dict[str, CodeRecord] = {}
        self.tokens: dict[str, TokenRecord] = {}
        self._clock = clock
        # Awaited where a real server would round-trip to its database.
        self._storage_io = storage_io or checkpoint

    # Authorization endpoint (draft Section 4.1.1)

    def authorize(self, query: Sequence[tuple[str, str]], subject: str) -> ErrorPage | Redirect:
        """Validate an authorization request and approve it for *subject*."""
        del subject  # consent and login are out of scope; the request is approved
        try:
            params = single_valued(query)
        except ParameterError:
            return ErrorPage(400, "invalid_request")
        client = self.clients.get(params.get("client_id", ""))
        if client is None:
            return ErrorPage(400, "invalid_client")
        presented = params.get("redirect_uri")
        if presented is None:
            if len(client.redirect_uris) != 1:
                return ErrorPage(400, "invalid_request")
            redirect_uri = client.redirect_uris[0]
        elif any(redirect_uri_matches(r, presented) for r in client.redirect_uris):
            redirect_uri = presented
        else:
            return ErrorPage(400, "invalid_request")

        # Client and redirect URI are validated; from here errors go back to the client.
        state = params.get("state")

        def error(code: str) -> Redirect:
            reply = {"error": code, **({"state": state} if state is not None else {})}
            return Redirect(_append_query(redirect_uri, {**reply, "iss": self.issuer}))

        response_type = params.get("response_type")
        if response_type is None:
            return error("invalid_request")
        if response_type != "code":
            return error("unsupported_response_type")  # includes the removed "token"
        challenge = params.get("code_challenge")
        method = params.get("code_challenge_method")
        if challenge is None:
            if method is not None or client.pkce_required:
                return error("invalid_request")
        elif method != "S256" or not is_pkce_value(challenge):
            return error("invalid_request")  # plain, a missing method, or a malformed value

        code = secrets.token_urlsafe(32)
        self.codes[code] = CodeRecord(
            client_id=client.client_id,
            redirect_uri=presented,
            code_challenge=challenge,
            expires_at=self._clock() + CODE_LIFETIME_SECONDS,
        )
        reply = {"code": code, **({"state": state} if state is not None else {})}
        return Redirect(_append_query(redirect_uri, {**reply, "iss": self.issuer}))

    # Token endpoint (draft Sections 3.2 and 4.1.3)

    def _authenticate(
        self, params: Mapping[str, str], authorization: str | None
    ) -> Client | TokenResult:
        header = authorization or ""
        basic = header[:6].lower() == "basic "
        if basic and "client_secret" in params:
            return _token_error("invalid_request")  # one authentication method per request
        if basic:
            try:
                decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            except ValueError:
                return _token_error("invalid_client", 401, **{"WWW-Authenticate": "Basic"})
            user, sep, password = decoded.partition(":")
            # The id and secret are form-urlencoded before Basic encoding (draft 2.4.1).
            client_id = unquote_plus(user)
            secret: str | None = unquote_plus(password) if sep else None
        else:
            client_id = params.get("client_id", "")
            secret = params.get("client_secret")
        challenge_header = {"WWW-Authenticate": "Basic"} if basic else {}
        client = self.clients.get(client_id)
        if client is None:
            return _token_error("invalid_client", 401, **challenge_header)
        if client.secret_sha256 is None:
            if secret is not None:
                return _token_error("invalid_client", 401, **challenge_header)
            return client
        if secret is None or not hmac.compare_digest(
            hash_client_secret(secret), client.secret_sha256
        ):
            return _token_error("invalid_client", 401, **challenge_header)
        return client

    def _check_code(self, client: Client, record: CodeRecord, params: Mapping[str, str]) -> str:
        """Return an error code, or ``""`` when the request is valid for this code."""
        if record.client_id != client.client_id:
            return "invalid_grant"
        verifier = params.get("code_verifier")
        if record.code_challenge is None:
            if verifier is not None:
                return "invalid_request"  # RFC 9700 Section 4.8.2: PKCE downgrade
        elif verifier is None or not is_pkce_value(verifier):
            return "invalid_request"
        elif not hmac.compare_digest(s256_challenge(verifier), record.code_challenge):
            return "invalid_grant"  # RFC 7636 Section 4.6
        sent_redirect = params.get("redirect_uri")
        if record.redirect_uri is not None and client.legacy_oauth20 and sent_redirect is None:
            return "invalid_request"  # RFC 6749 Section 4.1.3 for OAuth 2.0 clients
        if sent_redirect is not None and sent_redirect != record.redirect_uri:
            return "invalid_grant"
        return ""

    async def token(
        self, form: Sequence[tuple[str, str]], authorization: str | None = None
    ) -> TokenResult:
        """Handle one token request; only ``authorization_code`` is a grant here."""
        try:
            params = single_valued(form)
        except ParameterError:
            return _token_error("invalid_request")
        authenticated = self._authenticate(params, authorization)
        if isinstance(authenticated, TokenResult):
            return authenticated
        client = authenticated
        grant_type = params.get("grant_type")
        if grant_type is None:
            return _token_error("invalid_request")
        if grant_type != "authorization_code":
            return _token_error("unsupported_grant_type")  # includes the removed "password"
        code = params.get("code")
        if code is None:
            return _token_error("invalid_request")

        await self._storage_io()  # load the code record
        record = self.codes.get(code)
        if record is None:
            return _token_error("invalid_grant")
        problem = self._check_code(client, record, params)
        if problem:
            return _token_error(problem)  # an invalid replay revokes nothing (draft 7.5.3)
        # Check-and-set with no await in between: concurrent redemptions serialize here.
        if record.state is CodeState.REDEEMED:
            for token_id in record.token_ids:
                self.tokens[token_id].revoked = True
            return _token_error("invalid_grant")
        if self._clock() >= record.expires_at:
            return _token_error("invalid_grant")
        record.state = CodeState.REDEEMED

        access_token = secrets.token_urlsafe(32)
        self.tokens[access_token] = TokenRecord(
            client.client_id, code, self._clock() + ACCESS_TOKEN_LIFETIME_SECONDS
        )
        record.token_ids.append(access_token)
        await self._storage_io()  # persist the token
        body = {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_LIFETIME_SECONDS,
        }
        return TokenResult(200, body, {"Cache-Control": "no-store"})

    def token_is_active(self, access_token: str) -> bool:
        record = self.tokens.get(access_token)
        return record is not None and not record.revoked and self._clock() < record.expires_at

    def metadata(self) -> dict[str, Any]:
        """RFC 8414 metadata advertising what this server actually enforces."""
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
                "none",
            ],
            "authorization_response_iss_parameter_supported": True,
        }


class AuthorizationResponseError(ValueError):
    """The client must discard this authorization response."""


def validate_authorization_response(
    location: str, *, expected_state: str, expected_issuer: str
) -> str:
    """Client side: check ``iss`` (RFC 9207 Section 2.4) and ``state``, then return the code."""
    _, _, query = location.partition("?")
    try:
        params = single_valued(parse_qsl(query, keep_blank_values=True))
    except ParameterError as exc:
        raise AuthorizationResponseError(f"repeated parameter {exc}") from exc
    if params.get("iss") != expected_issuer:  # simple string comparison after form-decoding
        raise AuthorizationResponseError("issuer mismatch: possible mix-up attack")
    if not hmac.compare_digest(params.get("state", "").encode(), expected_state.encode()):
        raise AuthorizationResponseError("state mismatch: possible CSRF")
    if "error" in params:
        raise AuthorizationResponseError(params["error"])
    code = params.get("code")
    if code is None:
        raise AuthorizationResponseError("no code in response")
    return code


def bearer_token(request: Request) -> str | None:
    """Only the ``Authorization`` header counts; a query ``access_token`` is ignored (5.1)."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token if scheme.lower() == "bearer" and token else None


def create_app(server: AuthorizationServer) -> Starlette:
    """ASGI wiring for the endpoints, plus one protected resource."""

    async def authorize(request: Request) -> Response:
        outcome = server.authorize(request.query_params.multi_items(), subject="alice")
        if isinstance(outcome, ErrorPage):
            return PlainTextResponse(outcome.error, status_code=outcome.status)
        return RedirectResponse(outcome.location, status_code=303)  # never 307 (RFC 9700 4.12)

    async def token(request: Request) -> Response:
        body = (await request.body()).decode("utf-8")
        result = await server.token(
            parse_qsl(body, keep_blank_values=True), request.headers.get("authorization")
        )
        return JSONResponse(result.body, status_code=result.status, headers=result.headers)

    async def metadata(request: Request) -> Response:
        del request
        return JSONResponse(server.metadata())

    async def resource(request: Request) -> Response:
        presented = bearer_token(request)
        if presented is None or not server.token_is_active(presented):
            return PlainTextResponse(
                "unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
        return PlainTextResponse("resource for alice")

    return Starlette(
        routes=[
            Route("/authorize", authorize, methods=["GET"]),
            Route("/token", token, methods=["POST"]),
            Route("/.well-known/oauth-authorization-server", metadata, methods=["GET"]),
            Route("/resource", resource, methods=["GET"]),
        ]
    )
