"""JWS compact structure, JWK thumbprints, and RFC 9068 JWT access tokens.

The module mints access tokens with joserfc 1.8.0 and validates them with an explicit,
ordered pipeline that follows RFC 9068 Section 4: strict base64url and JSON parsing of the
header, explicit typing, an algorithm allow-list bound to the key, signature verification,
and only then the claims.
joserfc does the signature math; everything around it is spelled out here because neither
joserfc's ``jwt.decode`` nor PyJWT's ``decode`` checks ``typ``, and joserfc's ``decode``
validates no claims at all.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from joserfc import jws
from joserfc.errors import JoseError
from joserfc.jwk import ECKey, OKPKey, RSAKey
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from starlette.requests import Request

type JoseKey = ECKey | OKPKey | RSAKey

_B64URL_ALPHABET: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
# RFC 7638 Section 3.2 and RFC 8037 Section 2: the members hashed into a JWK Thumbprint.
_THUMBPRINT_MEMBERS: Final[Mapping[str, tuple[str, ...]]] = {
    "EC": ("crv", "kty", "x", "y"),
    "RSA": ("e", "kty", "n"),
    "oct": ("k", "kty"),
    "OKP": ("crv", "kty", "x"),
}
_PRIVATE_MEMBERS: Final = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})
# RFC 9068 Section 2.2: every JWT access token carries all seven.
REQUIRED_CLAIMS: Final = ("iss", "exp", "aud", "sub", "client_id", "iat", "jti")
AT_JWT_MEDIA_TYPE: Final = "application/at+jwt"


class TokenRejected(Exception):
    """The token failed validation; ``reason`` is safe to put in ``error_description``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- RFC 7515 Section 2 and Appendix C: base64url without padding --------------------------


def b64url_encode(data: bytes) -> str:
    """BASE64URL(data): RFC 4648 Section 5 alphabet, trailing '=' removed."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(segment: str) -> bytes:
    """Decode one compact-serialization segment, rejecting every alternate spelling.

    Padding, characters outside the alphabet, a length of 1 mod 4, and non-zero trailing
    bits all decode to the same bytes in a lenient decoder, which gives one token several
    serializations; only the canonical one is accepted here.
    """
    if any(ch not in _B64URL_ALPHABET for ch in segment):
        raise TokenRejected("segment is not unpadded base64url")
    if len(segment) % 4 == 1:
        raise TokenRejected("segment length is 1 mod 4")
    decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    if b64url_encode(decoded) != segment:
        raise TokenRejected("segment has non-zero trailing bits")
    return decoded


# --- RFC 7515 Sections 5.2 and 10.12: strict JSON ----------------------------------------


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for name, value in pairs:
        if name in obj:
            raise TokenRejected(f"duplicate member name {name!r}")
        obj[name] = value
    return obj


def _reject_constant(name: str) -> Any:
    raise TokenRejected(f"{name} is not JSON")


def parse_json_object(data: bytes, what: str) -> dict[str, Any]:
    """Parse UTF-8 bytes as one RFC 8259 JSON object: no duplicates, no NaN, no trailing data."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise TokenRejected(f"{what} is not UTF-8") from None
    try:
        value = json.loads(
            text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant
        )
    except ValueError:
        raise TokenRejected(f"{what} is not valid JSON") from None
    if not isinstance(value, dict):
        raise TokenRejected(f"{what} is not a JSON object")
    return cast("dict[str, Any]", value)


# --- RFC 7638: JWK Thumbprint --------------------------------------------------------------


def jwk_thumbprint(jwk: Mapping[str, Any]) -> str:
    """SHA-256 JWK Thumbprint: required members only, sorted, no whitespace (RFC 7638 Section 3)."""
    kty = jwk.get("kty")
    if not isinstance(kty, str) or kty not in _THUMBPRINT_MEMBERS:
        raise ValueError(f"no thumbprint member list for kty {kty!r}")
    subset = {name: jwk[name] for name in _THUMBPRINT_MEMBERS[kty]}
    if not all(isinstance(value, str) for value in subset.values()):
        raise ValueError("thumbprint members must be strings")
    canonical = json.dumps(subset, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    return b64url_encode(hashlib.sha256(canonical.encode("utf-8")).digest())


# --- Signing keys and the JWK Set ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SigningKey:
    """A private key pinned to one JWS algorithm; ``kid`` is its RFC 7638 thumbprint."""

    alg: str
    kid: str
    private: JoseKey

    def public_jwk(self) -> dict[str, Any]:
        public = self.private.as_dict(private=False)
        return {**public, "kid": self.kid, "alg": self.alg, "use": "sig"}


def _signing_key(alg: str, private: JoseKey) -> SigningKey:
    return SigningKey(alg, jwk_thumbprint(private.as_dict(private=False)), private)


def ed25519_signing_key(seed: bytes) -> SigningKey:
    """Deterministic Ed25519 key from a 32-byte seed, for the fully specified ``Ed25519`` alg."""
    raw = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    return _signing_key("Ed25519", OKPKey.import_key(raw))


def es256_signing_key(scalar: int) -> SigningKey:
    """Deterministic P-256 key from a private scalar (signatures still use a random nonce)."""
    raw = ec.derive_private_key(scalar, ec.SECP256R1())
    return _signing_key("ES256", ECKey.import_key(raw))


def rs256_signing_key(bits: int = 2048) -> SigningKey:
    """Fresh RSA key; RFC 7518 Section 3.3 requires 2048 bits or more for RS256."""
    raw = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    return _signing_key("RS256", RSAKey.import_key(raw))


def jwk_set(*keys: SigningKey) -> dict[str, Any]:
    """The public JWK Set an authorization server serves at its ``jwks_uri``."""
    return {"keys": [key.public_jwk() for key in keys]}


# --- Minting (authorization server side) ---------------------------------------------------


def mint_access_token(
    key: SigningKey,
    *,
    issuer: str,
    subject: str,
    audience: str | Sequence[str],
    client_id: str,
    issued_at: int,
    lifetime_s: int,
    jti: str,
    scope: Sequence[str] = (),
    typ: str = "at+jwt",
    extra_claims: Mapping[str, Any] | None = None,
) -> str:
    """Sign an RFC 9068 access token: header ``typ``/``alg``/``kid``, then the seven claims."""
    header = {"typ": typ, "alg": key.alg, "kid": key.kid}
    claims: dict[str, Any] = {
        "iss": issuer,
        "exp": issued_at + lifetime_s,
        "aud": audience if isinstance(audience, str) else list(audience),
        "sub": subject,
        "client_id": client_id,
        "iat": issued_at,
        "jti": jti,
    }
    if scope:
        claims["scope"] = " ".join(scope)
    if extra_claims:
        claims.update(extra_claims)
    payload = json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return jws.serialize_compact(header, payload, key.private, algorithms=[key.alg])


# --- Validation (resource server side) -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccessTokenPolicy:
    """What one resource server accepts; every field is a deliberate choice, not a default."""

    issuer: str
    audience: str
    algorithms: frozenset[str] = frozenset({"RS256", "ES256", "Ed25519"})
    leeway_s: int = 60
    max_token_bytes: int = 8192
    understood_crit: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class AccessToken:
    header: Mapping[str, Any]
    claims: Mapping[str, Any]
    subject: str
    client_id: str
    jti: str
    scopes: frozenset[str]
    expires_at: int | float


def is_access_token_typ(typ: object) -> bool:
    """RFC 9068 Section 4 with RFC 7515 Section 4.1.9 media-type rules.

    A ``typ`` without '/' means ``application/<typ>``, and media type names are
    case-insensitive, so ``at+jwt``, ``AT+JWT``, and ``application/at+jwt`` all match.
    """
    if not isinstance(typ, str):
        return False
    media_type = typ if "/" in typ else f"application/{typ}"
    return media_type.lower() == AT_JWT_MEDIA_TYPE


def select_key(jwks: Mapping[str, Any], kid: object, alg: str) -> dict[str, Any]:
    """Pick exactly one public verification key and bind it to the header ``alg``."""
    keys: object = jwks.get("keys")
    if not isinstance(keys, list):
        raise TokenRejected("JWK Set has no keys array")
    candidates: list[dict[str, Any]] = [
        cast("dict[str, Any]", k) for k in cast("list[object]", keys) if isinstance(k, dict)
    ]
    if kid is None:
        if len(candidates) != 1:
            raise TokenRejected("no kid and the JWK Set holds more than one key")
        matches = candidates
    elif isinstance(kid, str):
        matches = [k for k in candidates if k.get("kid") == kid]
    else:
        raise TokenRejected("kid is not a string")
    if len(matches) != 1:
        raise TokenRejected("no unique key for kid")
    key = matches[0]
    if _PRIVATE_MEMBERS & key.keys():
        raise TokenRejected("JWK Set entry holds private or symmetric key material")
    if key.get("use", "sig") != "sig":
        raise TokenRejected("key use is not sig")
    if key.get("alg") != alg:
        raise TokenRejected("header alg does not match the key's alg")
    return key


def _verify_signature(token: str, key_jwk: Mapping[str, Any], alg: str, limit: int) -> None:
    registry = jws.JWSRegistry(algorithms=[alg], strict_check_header=False)
    registry.max_header_length = limit
    registry.max_payload_length = limit
    try:
        jws.deserialize_compact(token, _import_public(key_jwk), registry=registry)
    except JoseError:
        raise TokenRejected("signature verification failed") from None


def _import_public(key_jwk: Mapping[str, Any]) -> JoseKey:
    data = cast("dict[str, str | list[str]]", dict(key_jwk))
    match data.get("kty"):
        case "OKP":
            return OKPKey.import_key(data)
        case "EC":
            return ECKey.import_key(data)
        case "RSA":
            return RSAKey.import_key(data)
        case other:
            raise TokenRejected(f"unsupported kty {other!r}")


def _numeric_date(claims: Mapping[str, Any], name: str) -> int | float:
    value = claims[name]
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise TokenRejected(f"{name} is not a NumericDate")
    return value


def _non_empty_string(claims: Mapping[str, Any], name: str) -> str:
    value = claims[name]
    if not isinstance(value, str) or not value:
        raise TokenRejected(f"{name} is not a non-empty string")
    return value


def validate_access_token(
    token: str, jwks: Mapping[str, Any], policy: AccessTokenPolicy, *, now: int
) -> AccessToken:
    """Validate a JWT access token per RFC 9068 Section 4; raise ``TokenRejected`` on failure."""
    if len(token.encode("utf-8")) > policy.max_token_bytes:
        raise TokenRejected("token exceeds the size limit")
    segments = token.split(".")
    if len(segments) == 5:
        raise TokenRejected("encrypted JWT (JWE) was not negotiated")
    if len(segments) != 3:
        raise TokenRejected("not a JWS Compact Serialization")
    header_b64, payload_b64, signature_b64 = segments

    header = parse_json_object(b64url_decode(header_b64), "header")
    if not is_access_token_typ(header.get("typ")):
        raise TokenRejected("typ is not at+jwt")
    alg = header.get("alg")
    if not isinstance(alg, str) or alg == "none":
        raise TokenRejected("alg is missing or none")
    if alg not in policy.algorithms:
        raise TokenRejected("alg is not allowed")
    crit = header.get("crit")
    if crit is not None:
        if not isinstance(crit, list) or not crit:
            raise TokenRejected("crit is not a non-empty array")
        for name in cast("list[object]", crit):
            if name not in policy.understood_crit or name not in header:
                raise TokenRejected("crit names an extension this server does not understand")

    key_jwk = select_key(jwks, header.get("kid"), alg)
    payload = b64url_decode(payload_b64)
    if not b64url_decode(signature_b64):
        raise TokenRejected("empty signature")
    _verify_signature(token, key_jwk, alg, policy.max_token_bytes)

    claims = parse_json_object(payload, "claims")
    missing = [name for name in REQUIRED_CLAIMS if name not in claims]
    if missing:
        raise TokenRejected(f"missing required claims {missing}")
    if claims["iss"] != policy.issuer:
        raise TokenRejected("iss does not match the expected issuer")
    aud: object = claims["aud"]
    audiences: list[object] = (
        [aud]
        if isinstance(aud, str)
        else cast("list[object]", aud)
        if isinstance(aud, list)
        else []
    )
    if not audiences or not all(isinstance(a, str) for a in audiences):
        raise TokenRejected("aud is not a string or array of strings")
    if policy.audience not in audiences:
        raise TokenRejected("aud does not contain this resource server")
    exp = _numeric_date(claims, "exp")
    iat = _numeric_date(claims, "iat")
    if now >= exp + policy.leeway_s:
        raise TokenRejected("token expired")
    if iat > now + policy.leeway_s:
        raise TokenRejected("iat is in the future")
    if "nbf" in claims and _numeric_date(claims, "nbf") > now + policy.leeway_s:
        raise TokenRejected("token not yet valid")
    subject = _non_empty_string(claims, "sub")
    client_id = _non_empty_string(claims, "client_id")
    jti = _non_empty_string(claims, "jti")
    scope = claims.get("scope", "")
    if not isinstance(scope, str):
        raise TokenRejected("scope is not a string")
    scopes = frozenset(scope.split(" ")) if scope else frozenset[str]()
    if "" in scopes:
        raise TokenRejected("scope has an empty scope-token")
    return AccessToken(header, claims, subject, client_id, jti, scopes, exp)


# --- RFC 6750 Section 3: the resource server's HTTP contract -------------------------------


def bearer_challenge(
    realm: str, error: str | None = None, description: str | None = None, scope: str | None = None
) -> str:
    """Build a ``WWW-Authenticate: Bearer`` value; quotes and backslashes are not allowed."""
    params = [f'realm="{realm}"']
    if error is not None:
        params.append(f'error="{error}"')
    if description is not None:
        safe = "".join(ch for ch in description if ch.isascii() and ch not in '"\\')
        params.append(f'error_description="{safe}"')
    if scope is not None:
        params.append(f'scope="{scope}"')
    return "Bearer " + ", ".join(params)


def build_resource_server(
    jwks: Mapping[str, Any],
    policy: AccessTokenPolicy,
    *,
    clock: Callable[[], int],
    required_scope: str,
    realm: str = "orders",
) -> Starlette:
    """A one-route ASGI resource server that enforces the profile and RFC 6750 errors."""

    async def orders(request: Request) -> Response:
        authorization = request.headers.get("authorization")
        if authorization is None:
            return Response(status_code=401, headers={"WWW-Authenticate": bearer_challenge(realm)})
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() != "bearer" or not credentials or " " in credentials:
            challenge = bearer_challenge(realm, "invalid_request", "malformed Authorization")
            return Response(status_code=400, headers={"WWW-Authenticate": challenge})
        try:
            token = validate_access_token(credentials, jwks, policy, now=clock())
        except TokenRejected as exc:
            challenge = bearer_challenge(realm, "invalid_token", exc.reason)
            return Response(status_code=401, headers={"WWW-Authenticate": challenge})
        if required_scope not in token.scopes:
            challenge = bearer_challenge(realm, "insufficient_scope", scope=required_scope)
            return Response(status_code=403, headers={"WWW-Authenticate": challenge})
        return JSONResponse({"sub": token.subject, "client_id": token.client_id})

    return Starlette(routes=[Route("/orders", orders)])
