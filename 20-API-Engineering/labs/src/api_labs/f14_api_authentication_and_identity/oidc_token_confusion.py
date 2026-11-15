"""OIDC ID Tokens vs OAuth access tokens, and RFC 8414 / RFC 9728 metadata discovery.

The lab models one OpenID Provider that issues two kinds of JWT with the same key:

- ID Tokens (OIDC Core 1.0 Section 2): `typ: JWT`, `aud` = the client's `client_id`.
- JWT access tokens (RFC 9068 Section 2): `typ: at+jwt`, `aud` = the resource indicator.

`naive_validate` is the bug: signature, `iss`, and `exp` only, so any ID Token the
provider ever minted for any client is accepted as an API credential.
`validate_access_token` and `validate_id_token` are mutually exclusive rule sets
(RFC 8725 Section 3.12): each rejects the other kind of token.

The metadata half derives well-known URLs (RFC 8414 Section 3.1, OIDC Discovery
Section 4.1, RFC 9728 Section 3.1) and enforces the exact identifier match that
stops metadata impersonation (RFC 8414 Section 3.3, RFC 9728 Section 3.3).
Everything is in-process and deterministic: callers pass `now` explicitly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import urlsplit

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import ECKey, KeySet
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from starlette.requests import Request

# RFC 9068 Section 4: "at+jwt" or "application/at+jwt"; media types are
# case-insensitive (RFC 7515 Section 4.1.9), and RFC 9068 Figure 2 itself uses "at+JWT".
ACCESS_TOKEN_TYPES: Final = frozenset({"at+jwt", "application/at+jwt"})
ALLOWED_ALGORITHMS: Final = ["ES256"]
# RFC 9068 Section 2.2: every one of these is REQUIRED in a JWT access token.
ACCESS_TOKEN_REQUIRED: Final = ("iss", "exp", "aud", "sub", "client_id", "iat", "jti")
# OIDC Core Section 2: REQUIRED in every ID Token.
ID_TOKEN_REQUIRED: Final = ("iss", "sub", "aud", "exp", "iat")
# OIDC Core Section 3.1.3.6: hash named by the JWS alg; EdDSA is not defined there.
_HALF_HASH: Final = {"256": hashlib.sha256, "384": hashlib.sha384, "512": hashlib.sha512}


class TokenRejected(Exception):
    """A token failed validation; `reason` names the first failed rule."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MetadataRejected(Exception):
    """A metadata document must not be used (RFC 8414 Section 3.3, RFC 9728 Section 3.3)."""


# --------------------------------------------------------------------------- issuer side


@dataclass(frozen=True, slots=True)
class OpenIDProvider:
    """One issuer, one signing key, two token kinds."""

    issuer: str
    signing_key: ECKey

    @classmethod
    def generate(cls, issuer: str, kid: str = "k1") -> OpenIDProvider:
        return cls(issuer, ECKey.generate_key("P-256", parameters={"kid": kid}))

    def jwks(self) -> KeySet:
        """The public JWK Set a verifier fetches from `jwks_uri`."""
        public = ECKey.import_key(self.signing_key.as_dict(private=False))
        return KeySet([public])

    def _sign(self, typ: str | None, claims: Mapping[str, object]) -> str:
        header: dict[str, object] = {"alg": "ES256", "kid": self.signing_key.kid}
        if typ is not None:
            header["typ"] = typ
        return jwt.encode(
            header, dict(claims), self.signing_key, algorithms=ALLOWED_ALGORITHMS, default_type=None
        )

    def issue_id_token(
        self,
        *,
        client_id: str,
        sub: str,
        nonce: str,
        now: int,
        access_token: str | None = None,
        extra_audiences: Sequence[str] = (),
        azp: str | None = None,
        lifetime: int = 300,
    ) -> str:
        """An ID Token: `aud` is the Relying Party (OIDC Core Section 2), typed `JWT`."""
        aud: str | list[str] = [client_id, *extra_audiences] if extra_audiences else client_id
        claims: dict[str, object] = {
            "iss": self.issuer,
            "sub": sub,
            "aud": aud,
            "exp": now + lifetime,
            "iat": now,
            "nonce": nonce,
        }
        if azp is not None:
            claims["azp"] = azp
        if access_token is not None:
            claims["at_hash"] = half_hash(access_token, "ES256")
        return self._sign("JWT", claims)

    def issue_access_token(
        self,
        *,
        client_id: str,
        sub: str,
        resource: str,
        scope: str,
        jti: str,
        now: int,
        lifetime: int = 300,
        typ: str | None = "at+jwt",
    ) -> str:
        """An RFC 9068 access token: `aud` is the RFC 8707 resource, typed `at+jwt`."""
        claims = {
            "iss": self.issuer,
            "exp": now + lifetime,
            "aud": resource,
            "sub": sub,
            "client_id": client_id,
            "iat": now,
            "jti": jti,
            "scope": scope,
        }
        return self._sign(typ, claims)


def half_hash(value: str, alg: str) -> str:
    """`at_hash` / `c_hash`: base64url of the left half of the alg's hash (OIDC Core 3.1.3.6)."""
    digest_fn = _HALF_HASH.get(alg[2:])
    if alg[:2] not in {"RS", "PS", "ES", "HS"} or digest_fn is None:
        raise ValueError(f"no half-hash defined for alg {alg!r}")
    digest = digest_fn(value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest[: len(digest) // 2]).rstrip(b"=").decode("ascii")


# --------------------------------------------------------------------------- verifier side


@dataclass(frozen=True, slots=True)
class Principal:
    """What a resource server learns from an accepted token."""

    sub: str
    client_id: str
    scopes: frozenset[str]


def _decode(token: str, jwks: KeySet) -> jwt.Token:
    try:
        return jwt.decode(token, jwks, algorithms=ALLOWED_ALGORITHMS)
    except JoseError as exc:
        raise TokenRejected("signature") from exc


def _audiences(claims: Mapping[str, object]) -> tuple[str, ...]:
    aud = claims.get("aud")
    if isinstance(aud, str):
        return (aud,)
    if isinstance(aud, list):
        items = cast("list[object]", aud)
        if items and all(isinstance(a, str) for a in items):
            return tuple(str(a) for a in items)
    raise TokenRejected("aud")


def _require(claims: Mapping[str, object], names: Sequence[str]) -> None:
    for name in names:
        if claims.get(name) is None:
            raise TokenRejected(f"missing:{name}")


def _check_exp(claims: Mapping[str, object], now: int, leeway: int) -> None:
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        raise TokenRejected("exp")
    if now >= exp + leeway:
        raise TokenRejected("exp")


def naive_validate(token: str, *, jwks: KeySet, issuer: str, now: int) -> Principal:
    """THE BUG: signature + `iss` + `exp`, no `typ`, no `aud`. Do not ship this."""
    claims = _decode(token, jwks).claims
    if claims.get("iss") != issuer:
        raise TokenRejected("iss")
    _check_exp(claims, now, 0)
    scope = claims.get("scope")
    return Principal(
        sub=str(claims.get("sub")),
        client_id=str(claims.get("client_id", claims.get("aud"))),
        scopes=frozenset(scope.split()) if isinstance(scope, str) else frozenset(),
    )


def validate_access_token(
    token: str, *, jwks: KeySet, issuer: str, resource: str, now: int, leeway: int = 60
) -> Principal:
    """RFC 9068 Section 4, in its order: `typ`, `iss`, `aud`, signature/alg, `exp`.

    The signature is checked first here only because `jwt.decode` returns the header and
    claims together; every header value read below is integrity-protected by it.
    """
    decoded = _decode(token, jwks)
    typ = decoded.header.get("typ")
    if not isinstance(typ, str) or typ.lower() not in ACCESS_TOKEN_TYPES:
        raise TokenRejected("typ")
    claims = decoded.claims
    _require(claims, ACCESS_TOKEN_REQUIRED)
    if claims["iss"] != issuer:  # exact code-point match (RFC 8414 Section 4)
        raise TokenRejected("iss")
    if resource not in _audiences(claims):
        raise TokenRejected("aud")
    _check_exp(claims, now, leeway)
    scope = claims.get("scope", "")
    return Principal(
        sub=str(claims["sub"]),
        client_id=str(claims["client_id"]),
        scopes=frozenset(scope.split()) if isinstance(scope, str) else frozenset(),
    )


def validate_id_token(
    token: str,
    *,
    jwks: KeySet,
    issuer: str,
    client_id: str,
    nonce: str,
    now: int,
    leeway: int = 60,
    trusted_audiences: frozenset[str] = frozenset(),
    access_token: str | None = None,
) -> Mapping[str, object]:
    """OIDC Core Section 3.1.3.7 for a Relying Party, plus RFC 8725 Section 3.12 exclusivity."""
    decoded = _decode(token, jwks)
    typ = decoded.header.get("typ")
    if isinstance(typ, str) and typ.lower() in ACCESS_TOKEN_TYPES:
        raise TokenRejected("typ")  # an access token is never a login assertion
    claims = decoded.claims
    _require(claims, ID_TOKEN_REQUIRED)
    if claims["iss"] != issuer:
        raise TokenRejected("iss")
    audiences = _audiences(claims)
    if client_id not in audiences:
        raise TokenRejected("aud")
    if any(a != client_id and a not in trusted_audiences for a in audiences):
        raise TokenRejected("aud:untrusted")
    azp = claims.get("azp")
    if azp is not None and azp != client_id:
        raise TokenRejected("azp")
    _check_exp(claims, now, leeway)
    got_nonce = claims.get("nonce")
    if not isinstance(got_nonce, str) or not hmac.compare_digest(got_nonce, nonce):
        raise TokenRejected("nonce")
    at_hash = claims.get("at_hash")
    if access_token is not None and at_hash is not None:
        alg = str(decoded.header["alg"])
        if not hmac.compare_digest(str(at_hash), half_hash(access_token, alg)):
            raise TokenRejected("at_hash")
    return claims


# --------------------------------------------------------------------------- metadata


def _split_https(identifier: str, *, allow_query: bool) -> tuple[str, str, str]:
    parts = urlsplit(identifier)
    if parts.scheme != "https" or not parts.netloc or parts.fragment:
        raise ValueError(f"not an https identifier without fragment: {identifier!r}")
    if parts.query and not allow_query:
        raise ValueError(f"issuer identifiers have no query: {identifier!r}")
    return parts.netloc, parts.path, parts.query


def rfc8414_metadata_url(issuer: str, suffix: str = "oauth-authorization-server") -> str:
    """RFC 8414 Section 3.1: insert `/.well-known/<suffix>` between host and path."""
    netloc, path, _ = _split_https(issuer, allow_query=False)
    return f"https://{netloc}/.well-known/{suffix}{path.rstrip('/')}"


def oidc_discovery_url(issuer: str) -> str:
    """OIDC Discovery 1.0 Section 4.1: append `/.well-known/openid-configuration`."""
    _split_https(issuer, allow_query=False)
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


def rfc9728_metadata_url(resource: str, suffix: str = "oauth-protected-resource") -> str:
    """RFC 9728 Section 3.1: insert between host and path and/or query; drop a bare `/`."""
    netloc, path, query = _split_https(resource, allow_query=True)
    path = "" if path == "/" else path
    return f"https://{netloc}/.well-known/{suffix}{path}" + (f"?{query}" if query else "")


@dataclass(frozen=True, slots=True)
class AuthorizationServerMetadata:
    issuer: str
    jwks_uri: str
    response_types_supported: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProtectedResourceMetadata:
    resource: str
    authorization_servers: tuple[str, ...]


def _string_list(doc: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = doc.get(name, [])
    items = cast("list[object]", value) if isinstance(value, list) else None
    if items is None or not all(isinstance(v, str) for v in items):
        raise MetadataRejected(f"{name} must be a JSON array of strings")
    return tuple(str(v) for v in items)


def validate_authorization_server_metadata(
    doc: Mapping[str, object], expected_issuer: str
) -> AuthorizationServerMetadata:
    """RFC 8414 Section 3.3: `issuer` identical to the one the URL was built from."""
    issuer = doc.get("issuer")
    if not isinstance(issuer, str) or issuer != expected_issuer:
        raise MetadataRejected(f"issuer {issuer!r} != {expected_issuer!r}")
    jwks_uri = doc.get("jwks_uri")
    if not isinstance(jwks_uri, str) or urlsplit(jwks_uri).scheme != "https":
        raise MetadataRejected("jwks_uri must be an https URL")
    response_types = _string_list(doc, "response_types_supported")
    if not response_types:
        raise MetadataRejected("response_types_supported is REQUIRED")
    return AuthorizationServerMetadata(issuer, jwks_uri, response_types)


def validate_protected_resource_metadata(
    doc: Mapping[str, object], expected_resource: str
) -> ProtectedResourceMetadata:
    """RFC 9728 Section 3.3: `resource` identical to the identifier the URL came from."""
    resource = doc.get("resource")
    if not isinstance(resource, str) or resource != expected_resource:
        raise MetadataRejected(f"resource {resource!r} != {expected_resource!r}")
    return ProtectedResourceMetadata(resource, _string_list(doc, "authorization_servers"))


def choose_authorization_server(
    metadata: ProtectedResourceMetadata, trusted_issuers: frozenset[str]
) -> str:
    """RFC 9728 Sections 7.6 and 7.7: never follow an untrusted issuer the RS names."""
    for issuer in metadata.authorization_servers:
        if issuer in trusted_issuers:
            return issuer
    raise MetadataRejected("no advertised authorization server is trusted")


def bearer_challenge(
    *,
    resource_metadata: str,
    error: str | None = None,
    description: str | None = None,
    scope: str | None = None,
) -> str:
    """`WWW-Authenticate: Bearer` (RFC 6750 Section 3) + `resource_metadata` (RFC 9728 5.1).

    With no credentials presented, RFC 6750 Section 3.1 says to send no error code.
    """
    params: list[str] = []
    if error is not None:
        params.append(f'error="{error}"')
        if description is not None:
            params.append(f'error_description="{description}"')
    if scope is not None:
        params.append(f'scope="{scope}"')
    params.append(f'resource_metadata="{resource_metadata}"')
    return "Bearer " + ", ".join(params)


# --------------------------------------------------------------------------- resource server

Validator = Callable[[str], Principal]


def build_resource_server(
    *,
    resource: str,
    authorization_servers: Sequence[str],
    validator: Validator,
    required_scope: str | None,
) -> Starlette:
    """An API at `resource` that publishes RFC 9728 metadata and guards `GET` on its path."""
    metadata_url = rfc9728_metadata_url(resource)
    metadata_path = urlsplit(metadata_url).path
    api_path = urlsplit(resource).path or "/"

    async def metadata(_: Request) -> JSONResponse:
        doc: dict[str, object] = {
            "resource": resource,
            "authorization_servers": list(authorization_servers),
            "bearer_methods_supported": ["header"],
        }
        if required_scope is not None:  # RFC 9728 Section 3.2: omit empty arrays
            doc["scopes_supported"] = [required_scope]
        return JSONResponse(doc, headers={"Cache-Control": "max-age=3600"})

    async def api(request: Request) -> JSONResponse:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            challenge = bearer_challenge(resource_metadata=metadata_url)
            return JSONResponse({}, 401, headers={"WWW-Authenticate": challenge})
        try:
            principal = validator(token)
        except TokenRejected as exc:
            challenge = bearer_challenge(
                resource_metadata=metadata_url, error="invalid_token", description=exc.reason
            )
            body = {"error": "invalid_token", "reason": exc.reason}
            return JSONResponse(body, 401, headers={"WWW-Authenticate": challenge})
        if required_scope is not None and required_scope not in principal.scopes:
            challenge = bearer_challenge(
                resource_metadata=metadata_url, error="insufficient_scope", scope=required_scope
            )
            body = {"error": "insufficient_scope"}
            return JSONResponse(body, 403, headers={"WWW-Authenticate": challenge})
        return JSONResponse({"sub": principal.sub, "client_id": principal.client_id})

    return Starlette(routes=[Route(metadata_path, metadata), Route(api_path, api)])
