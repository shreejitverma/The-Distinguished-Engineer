"""Edge termination vs zero-trust identity propagation, end to end and in process.

The edge gateway verifies the external access token once and then propagates identity to an
internal service in one of four ways (`PropagationMode`):

* `HEADER_APPEND`: adds `X-User-ID` without removing client copies (the classic bug);
* `HEADER_SET`: strips client copies and sets `X-User-ID` (correct only if nothing but the
  gateway can reach the service, RFC 9700 Section 4.13);
* `PASSTHROUGH`: forwards the external bearer token unchanged (the audience problem);
* `TOKEN_EXCHANGE`: mints a short-lived, audience-restricted internal token whose `act`
  claim names the gateway workload (RFC 8693 Section 4.1), and calls over an mTLS hop.

The zero-trust service checks both identities on every request, as Google BeyondProd describes:
the transport peer (a SPIFFE X.509-SVID read from the ASGI TLS extension) and the end-user token
(RFC 9068 Section 4), then authorizes the object itself.
"""

from __future__ import annotations

import datetime
import enum
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    BaseUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from starlette.requests import HTTPConnection, Request
    from starlette.types import ASGIApp, Receive, Scope, Send

type Header = tuple[bytes, bytes]
type Records = Mapping[str, Mapping[str, str]]

ACCESS_TOKEN_TYPES = frozenset({"application/at+jwt"})  # RFC 9068 Section 4, compared per RFC 7515
IDENTITY_HEADERS = frozenset({b"x-user-id", b"x-user-scopes"})
REQUIRED_CLAIMS = ("iss", "exp", "aud", "sub", "client_id", "iat", "jti")  # RFC 9068 Section 2.2


class TokenRejected(AuthenticationError):
    """A credential that must not be used; `error` is the RFC 6750 Section 3.1 code."""

    def __init__(self, reason: str, error: str = "invalid_token") -> None:
        super().__init__(reason)
        self.reason = reason
        self.error = error


class SvidError(ValueError):
    """A peer certificate that is not a usable leaf X.509-SVID."""


@dataclass(frozen=True, slots=True)
class Principal:
    """What a verified access token says, reduced to what authorization needs."""

    issuer: str
    subject: str
    client_id: str
    audience: tuple[str, ...]
    scopes: frozenset[str]
    actor: str | None  # current actor only (RFC 8693 Section 4.1); nested actors are ignored


class PrincipalUser(BaseUser):
    def __init__(self, principal: Principal) -> None:
        self.principal = principal

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return self.principal.subject

    @property
    def identity(self) -> str:
        return self.principal.subject


def lab_key(seed: int) -> ec.EllipticCurvePrivateKey:
    """A deterministic P-256 key, so fixtures are stable across runs (never do this for real)."""
    return ec.derive_private_key(seed, ec.SECP256R1())


def normalize_media_type(typ: str) -> str:
    """RFC 7515 Section 4.1.9: case-insensitive, and `application/` is implied without a `/`."""
    lowered = typ.lower()
    return lowered if "/" in lowered else f"application/{lowered}"


def mint_access_token(
    key: ec.EllipticCurvePrivateKey,
    *,
    kid: str,
    issuer: str,
    subject: str,
    audience: str | list[str] | None,
    scopes: Iterable[str],
    client_id: str = "web-app",
    typ: str = "at+jwt",
    lifetime: int = 300,
    actor: str | None = None,
    now: float | None = None,
) -> str:
    """An RFC 9068 JWT access token signed with ES256; `audience=None` omits `aud`."""
    issued = int(time.time() if now is None else now)
    claims: dict[str, Any] = {
        "iss": issuer,
        "sub": subject,
        "client_id": client_id,
        "iat": issued,
        "exp": issued + lifetime,
        "jti": str(uuid.uuid4()),
        "scope": " ".join(sorted(scopes)),
    }
    if audience is not None:
        claims["aud"] = audience
    if actor is not None:
        claims["act"] = {"sub": actor}
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": kid, "typ": typ})


@dataclass(frozen=True, slots=True)
class TokenVerifier:
    """RFC 9068 Section 4 validation for one resource server (one audience)."""

    issuer: str
    audience: str
    keys: Mapping[str, ec.EllipticCurvePublicKey]
    algorithms: tuple[str, ...] = ("ES256",)
    leeway: int = 60

    def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise TokenRejected(f"malformed: {exc}") from exc
        alg, typ, kid = header.get("alg"), header.get("typ"), header.get("kid")
        if alg not in self.algorithms:  # RFC 8725 Section 3.1, never trust the header's choice
            raise TokenRejected(f"alg {alg!r} not allowed")
        if not isinstance(typ, str) or normalize_media_type(typ) not in ACCESS_TOKEN_TYPES:
            raise TokenRejected(f"typ {typ!r} is not an access token")  # RFC 8725 Section 3.11
        if not isinstance(kid, str) or kid not in self.keys:
            raise TokenRejected(f"unknown kid {kid!r}")
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self.keys[kid],
                algorithms=list(self.algorithms),
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except jwt.InvalidTokenError as exc:
            raise TokenRejected(f"{type(exc).__name__}: {exc}") from exc
        return _principal(claims)


def _principal(claims: Mapping[str, Any]) -> Principal:
    subject, client_id, scope = claims["sub"], claims["client_id"], claims.get("scope", "")
    if not (isinstance(subject, str) and isinstance(client_id, str) and isinstance(scope, str)):
        raise TokenRejected("sub, client_id, and scope must be strings")
    aud = claims["aud"]
    audience = (aud,) if isinstance(aud, str) else tuple(str(a) for a in aud)
    act: object = claims.get("act")
    actor = cast("Mapping[str, object]", act).get("sub") if isinstance(act, dict) else None
    return Principal(
        issuer=str(claims["iss"]),
        subject=subject,
        client_id=client_id,
        audience=audience,
        scopes=frozenset(scope.split()),
        actor=actor if isinstance(actor, str) else None,
    )


def extract_bearer(headers: Iterable[Header]) -> str | None:
    """The single `Authorization: Bearer` credential, or None; duplicates are `invalid_request`."""
    values = [value for name, value in headers if name.lower() == b"authorization"]
    if not values:
        return None
    if len(values) > 1:  # RFC 6750 Section 3.1: more than one method or copy is invalid_request
        raise TokenRejected("multiple Authorization fields", error="invalid_request")
    scheme, _, token = values[0].decode("latin-1").partition(" ")
    if scheme.lower() != "bearer" or not token or " " in token.strip():
        raise TokenRejected("not a Bearer credential", error="invalid_request")
    return token.strip()


def bearer_challenge(realm: str, error: str | None = None, scope: str | None = None) -> str:
    """A `WWW-Authenticate: Bearer` value (RFC 6750 Section 3); no `error` without credentials."""
    params = [f'realm="{realm}"']
    if error is not None:
        params.append(f'error="{error}"')
    if scope is not None:
        params.append(f'scope="{scope}"')
    return "Bearer " + ", ".join(params)


# --- workload identity: the transport peer -------------------------------------------------


def issue_svid(
    spiffe_ids: Iterable[str], seed: int, *, ca: bool = False, days: int = 1
) -> tuple[str, ec.EllipticCurvePrivateKey]:
    """A self-signed certificate with the given URI SANs, as PEM; a lab stand-in for SPIRE."""
    key = lab_key(seed)
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "lab")])
    start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(seed)
        .not_valid_before(start)
        .not_valid_after(start + datetime.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(u) for u in spiffe_ids]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii"), key


def spiffe_id_from_pem(pem: str) -> str:
    """The SPIFFE ID of a leaf X.509-SVID (X509-SVID Sections 2 and 5.2), chain already verified.

    Path validation against the trust bundle is the TLS server's job; this reads the identity
    the server validated and enforces the leaf rules the handshake does not.
    """
    try:
        cert = x509.load_pem_x509_certificate(pem.encode("ascii"))
        sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    except (ValueError, x509.ExtensionNotFound) as exc:
        raise SvidError(f"unusable certificate: {exc}") from exc
    uris = sans.get_values_for_type(x509.UniformResourceIdentifier)
    if len(uris) != 1:
        raise SvidError(f"expected exactly one URI SAN, got {len(uris)}")
    if constraints.ca:
        raise SvidError("CA certificates must not authenticate")
    uri = uris[0]
    scheme, sep, rest = uri.partition("://")
    trust_domain, _, path = rest.partition("/")
    if scheme != "spiffe" or not sep or not trust_domain or not path:
        raise SvidError(f"not a leaf SPIFFE ID: {uri!r}")
    return uri


def _peer_chain(scope: Scope) -> list[str]:
    """`scope["extensions"]["tls"]["client_cert_chain"]` (ASGI TLS extension 0.3), or []."""
    extensions = cast("Mapping[str, object]", scope.get("extensions") or {})
    tls = extensions.get("tls")
    if not isinstance(tls, dict):
        return []
    chain = cast("Mapping[str, object]", tls).get("client_cert_chain", [])
    return [pem for pem in cast("list[object]", chain) if isinstance(pem, str)]


def with_peer_certificate(app: ASGIApp, pem: str | None) -> ASGIApp:
    """Model an ASGI server that terminated mTLS and reports the peer per the TLS extension."""

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if pem is not None and scope["type"] in ("http", "websocket"):
            extensions = dict(scope.get("extensions") or {})
            extensions["tls"] = {"spec_version": "0.3", "client_cert_chain": [pem]}
            scope = {**scope, "extensions": extensions}
        await app(scope, receive, send)

    return wrapped


# --- the zero-trust resource server --------------------------------------------------------


class BearerBackend(AuthenticationBackend):
    def __init__(self, verifier: TokenVerifier) -> None:
        self.verifier = verifier

    async def authenticate(self, conn: HTTPConnection) -> tuple[AuthCredentials, BaseUser] | None:
        token = extract_bearer(conn.scope["headers"])
        if token is None:
            return None
        principal = self.verifier.verify(token)
        return AuthCredentials(sorted(principal.scopes)), PrincipalUser(principal)


def _auth_error(realm: str) -> Any:
    def on_error(conn: HTTPConnection, exc: AuthenticationError) -> JSONResponse:
        error = exc.error if isinstance(exc, TokenRejected) else "invalid_token"
        status = 400 if error == "invalid_request" else 401
        return JSONResponse(
            {"error": error},
            status_code=status,
            headers={"WWW-Authenticate": bearer_challenge(realm, error)},
        )

    return on_error


def build_zero_trust_service(
    resource: str,
    records: Records,
    verifier: TokenVerifier,
    *,
    allowed_callers: frozenset[str],
    bind_actor_to_peer: bool = True,
) -> Starlette:
    """GET /<resource>/{id}: peer SVID, then token, then scope, then object ownership."""
    realm = resource
    required_scope = f"{resource}:read"

    async def read(request: Request) -> JSONResponse:
        chain = _peer_chain(request.scope)
        try:
            peer = spiffe_id_from_pem(chain[0]) if chain else None
        except SvidError:
            peer = None
        if peer is None or peer not in allowed_callers:
            # Not an HTTP authentication scheme, so 403 rather than a 401 challenge.
            return JSONResponse({"error": "caller_not_allowed", "peer": peer}, status_code=403)
        if not request.user.is_authenticated:
            return JSONResponse(
                {"error": "missing_token"},
                status_code=401,
                headers={"WWW-Authenticate": bearer_challenge(realm)},
            )
        principal: Principal = request.user.principal
        if bind_actor_to_peer and principal.actor != peer:
            return JSONResponse({"error": "actor_is_not_peer", "actor": principal.actor}, 403)
        if required_scope not in principal.scopes:
            return JSONResponse(
                {"error": "insufficient_scope"},
                status_code=403,
                headers={
                    "WWW-Authenticate": bearer_challenge(
                        realm, "insufficient_scope", required_scope
                    )
                },
            )
        record = records.get(request.path_params["record_id"])
        if record is None or record["owner"] != principal.subject:
            # Object-level authorization (OWASP API1:2023); 404 hides existence (RFC 9110 15.5.4).
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse({**record, "served_to": principal.subject, "via": peer})

    return Starlette(
        routes=[Route(f"/{resource}/{{record_id}}", read)],
        middleware=[
            Middleware(
                AuthenticationMiddleware,
                backend=BearerBackend(verifier),
                on_error=_auth_error(realm),
            )
        ],
    )


def build_header_trust_service(resource: str, records: Records) -> Starlette:
    """GET /<resource>/{id} that believes `X-User-ID`, as edge-only architectures do."""

    async def read(request: Request) -> JSONResponse:
        user = request.headers.get("x-user-id")  # Starlette returns the FIRST matching line
        if user is None:
            return JSONResponse({"error": "no identity header"}, status_code=401)
        record = records.get(request.path_params["record_id"])
        if record is None or record["owner"] != user:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse({**record, "served_to": user})

    return Starlette(routes=[Route(f"/{resource}/{{record_id}}", read)])


# --- the edge gateway ----------------------------------------------------------------------


class PropagationMode(enum.Enum):
    HEADER_APPEND = "header-append"
    HEADER_SET = "header-set"
    PASSTHROUGH = "passthrough"
    TOKEN_EXCHANGE = "token-exchange"  # noqa: S105 - a mode name, not a credential


@dataclass(frozen=True, slots=True)
class Upstream:
    """One internal service behind the gateway, addressed by path prefix and audience."""

    prefix: str
    audience: str
    app: ASGIApp


@dataclass(frozen=True, slots=True)
class InternalIssuer:
    """The edge's own token minter: short lifetime, one audience, `act` = the gateway."""

    issuer: str
    kid: str
    key: ec.EllipticCurvePrivateKey
    gateway_id: str
    gateway_cert_pem: str
    lifetime: int = 60

    def mint(self, principal: Principal, audience: str) -> str:
        return mint_access_token(
            self.key,
            kid=self.kid,
            issuer=self.issuer,
            subject=principal.subject,
            audience=audience,
            scopes=principal.scopes,
            client_id=principal.client_id,
            lifetime=self.lifetime,
            actor=self.gateway_id,
        )


def strip_identity_headers(headers: Iterable[Header], extra: Iterable[bytes] = ()) -> list[Header]:
    drop = IDENTITY_HEADERS | {name.lower() for name in extra}
    return [(name, value) for name, value in headers if name.lower() not in drop]


def propagate(
    headers: list[Header],
    principal: Principal,
    mode: PropagationMode,
    *,
    audience: str,
    issuer: InternalIssuer | None,
) -> list[Header]:
    """The header list the gateway sends upstream for an already verified principal."""
    user = principal.subject.encode("latin-1")
    scopes = " ".join(sorted(principal.scopes)).encode("latin-1")
    if mode is PropagationMode.HEADER_APPEND:
        return [*headers, (b"x-user-id", user), (b"x-user-scopes", scopes)]
    if mode is PropagationMode.HEADER_SET:
        return [*strip_identity_headers(headers), (b"x-user-id", user), (b"x-user-scopes", scopes)]
    if mode is PropagationMode.PASSTHROUGH:
        return strip_identity_headers(headers)
    if issuer is None:
        raise ValueError("TOKEN_EXCHANGE needs an InternalIssuer")
    token = issuer.mint(principal, audience).encode("ascii")
    return [
        *strip_identity_headers(headers, extra=[b"authorization"]),
        (b"authorization", b"Bearer " + token),
    ]


class EdgeGateway:
    """Verify the external token once at the edge, then propagate identity per `mode`."""

    def __init__(
        self,
        upstreams: Iterable[Upstream],
        verifier: TokenVerifier,
        mode: PropagationMode,
        issuer: InternalIssuer | None = None,
    ) -> None:
        self.upstreams = tuple(upstreams)
        self.verifier = verifier
        self.mode = mode
        self.issuer = issuer
        if mode is PropagationMode.TOKEN_EXCHANGE and issuer is None:
            raise ValueError("TOKEN_EXCHANGE needs an InternalIssuer")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        upstream = next((u for u in self.upstreams if scope["path"].startswith(u.prefix)), None)
        if upstream is None:
            await JSONResponse({"error": "no_route"}, status_code=404)(scope, receive, send)
            return
        try:
            token = extract_bearer(scope["headers"])
            principal = None if token is None else self.verifier.verify(token)
        except TokenRejected as exc:
            status = 400 if exc.error == "invalid_request" else 401
            challenge = bearer_challenge("api", exc.error)
            response = JSONResponse({"error": exc.error}, status, {"WWW-Authenticate": challenge})
            await response(scope, receive, send)
            return
        if principal is None:  # no credentials: a bare challenge, no error code (RFC 6750 3.1)
            challenge = bearer_challenge("api")
            response = JSONResponse({"error": None}, 401, {"WWW-Authenticate": challenge})
            await response(scope, receive, send)
            return
        headers = propagate(
            list(scope["headers"]),
            principal,
            self.mode,
            audience=upstream.audience,
            issuer=self.issuer,
        )
        app = upstream.app
        if self.mode is PropagationMode.TOKEN_EXCHANGE and self.issuer is not None:
            app = with_peer_certificate(app, self.issuer.gateway_cert_pem)
        await app({**scope, "headers": headers}, receive, send)


def decode_unverified_claims(token: str) -> dict[str, Any]:
    """Claims without any check, for size and content inspection in tests only."""
    claims: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    return claims
