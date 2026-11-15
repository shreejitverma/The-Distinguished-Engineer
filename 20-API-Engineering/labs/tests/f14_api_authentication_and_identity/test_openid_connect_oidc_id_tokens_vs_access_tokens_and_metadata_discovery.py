"""Lab: an ID Token is not an access token, and metadata must name itself exactly.

Note: OpenID-Connect-OIDC-ID-Tokens-vs-Access-Tokens-and-Metadata-Discovery.md
"""

from __future__ import annotations

import functools
import time
from typing import Any

import httpx
import jwt as pyjwt
import pytest
from authlib.oauth2.rfc8414 import get_well_known_url  # type: ignore[import-untyped]
from authlib.oauth2.rfc9068.claims import (  # type: ignore[import-untyped]
    JWTAccessTokenClaims,
)
from joserfc.errors import InvalidClaimError
from pydantic import AnyHttpUrl

from api_labs.f14_api_authentication_and_identity.oidc_token_confusion import (
    MetadataRejected,
    OpenIDProvider,
    TokenRejected,
    build_resource_server,
    choose_authorization_server,
    half_hash,
    naive_validate,
    oidc_discovery_url,
    rfc8414_metadata_url,
    rfc9728_metadata_url,
    validate_access_token,
    validate_authorization_server_metadata,
    validate_id_token,
    validate_protected_resource_metadata,
)

ISSUER = "https://id.example.test"
API = "https://api.example.test/orders"
OTHER_API = "https://billing.example.test/"
NOW = 1_800_000_000
ALICE_AT = "alice-token"  # opaque access token strings for the at_hash test
MALLORY_AT = "mallory-token"


@pytest.fixture(scope="module")
def op() -> OpenIDProvider:
    return OpenIDProvider.generate(ISSUER)


def strict(op: OpenIDProvider, token: str, *, resource: str = API, now: int = NOW + 1) -> Any:
    return validate_access_token(token, jwks=op.jwks(), issuer=ISSUER, resource=resource, now=now)


def victim_id_token_for_attacker_app(op: OpenIDProvider) -> str:
    """Alice signs in to quiz-app (attacker-run RP); its backend now holds her ID Token."""
    return op.issue_id_token(client_id="quiz-app", sub="alice", nonce="n-1", now=NOW)


def api_client(op: OpenIDProvider, *, naive: bool) -> httpx.AsyncClient:
    if naive:
        validator = functools.partial(naive_validate, jwks=op.jwks(), issuer=ISSUER, now=NOW + 1)
        app = build_resource_server(
            resource=API, authorization_servers=[ISSUER], validator=validator, required_scope=None
        )
    else:
        validator = functools.partial(
            validate_access_token, jwks=op.jwks(), issuer=ISSUER, resource=API, now=NOW + 1
        )
        app = build_resource_server(
            resource=API,
            authorization_servers=[ISSUER],
            validator=validator,
            required_scope="orders:read",
        )
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="https://api.example.test")


# --------------------------------------------------------------------------- the attack


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naive_api_lets_attacker_app_replay_victims_id_token(op: OpenIDProvider) -> None:
    token = victim_id_token_for_attacker_app(op)
    async with api_client(op, naive=True) as client:
        response = await client.get("/orders", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"sub": "alice", "client_id": "quiz-app"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strict_api_rejects_id_token_with_invalid_token_challenge(
    op: OpenIDProvider,
) -> None:
    token = victim_id_token_for_attacker_app(op)
    async with api_client(op, naive=False) as client:
        response = await client.get("/orders", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_token", "reason": "typ"}
    assert response.headers["www-authenticate"] == (
        'Bearer error="invalid_token", error_description="typ", '
        'resource_metadata="https://api.example.test/.well-known/oauth-protected-resource/orders"'
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strict_api_serves_a_real_access_token(op: OpenIDProvider) -> None:
    token = op.issue_access_token(
        client_id="spa", sub="alice", resource=API, scope="orders:read", jti="j-1", now=NOW
    )
    async with api_client(op, naive=False) as client:
        response = await client.get("/orders", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"sub": "alice", "client_id": "spa"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_credentials_gets_401_without_error_code(op: OpenIDProvider) -> None:
    async with api_client(op, naive=False) as client:
        response = await client.get("/orders")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        'Bearer resource_metadata="https://api.example.test/.well-known/oauth-protected-resource/orders"'
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wrong_scope_gets_403_insufficient_scope(op: OpenIDProvider) -> None:
    token = op.issue_access_token(
        client_id="spa", sub="alice", resource=API, scope="profile", jti="j-2", now=NOW
    )
    async with api_client(op, naive=False) as client:
        response = await client.get("/orders", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert 'error="insufficient_scope"' in response.headers["www-authenticate"]
    assert 'scope="orders:read"' in response.headers["www-authenticate"]


# --------------------------------------------------------------------------- RFC 9068 rules


@pytest.mark.unit
def test_typ_alone_rejects_a_jwt_that_carries_every_access_token_claim(
    op: OpenIDProvider,
) -> None:
    # Same issuer, key, aud, and claims; only the explicit type differs.
    token = op.issue_access_token(
        client_id="spa", sub="alice", resource=API, scope="orders:read", jti="j", now=NOW, typ="JWT"
    )
    with pytest.raises(TokenRejected) as info:
        strict(op, token)
    assert info.value.reason == "typ"


@pytest.mark.unit
def test_missing_typ_is_rejected_not_waved_through(op: OpenIDProvider) -> None:
    token = op.issue_access_token(
        client_id="spa", sub="alice", resource=API, scope="orders:read", jti="j", now=NOW, typ=None
    )
    with pytest.raises(TokenRejected) as info:
        strict(op, token)
    assert info.value.reason == "typ"


@pytest.mark.unit
@pytest.mark.parametrize("typ", ["at+jwt", "at+JWT", "application/at+jwt", "Application/AT+JWT"])
def test_typ_is_compared_as_a_case_insensitive_media_type(op: OpenIDProvider, typ: str) -> None:
    token = op.issue_access_token(
        client_id="spa", sub="alice", resource=API, scope="orders:read", jti="j", now=NOW, typ=typ
    )
    assert strict(op, token).sub == "alice"


@pytest.mark.unit
def test_access_token_for_another_resource_is_rejected_by_aud(op: OpenIDProvider) -> None:
    token = op.issue_access_token(
        client_id="spa", sub="alice", resource=OTHER_API, scope="orders:read", jti="j", now=NOW
    )
    assert strict(op, token, resource=OTHER_API).sub == "alice"
    with pytest.raises(TokenRejected) as info:
        strict(op, token, resource=API)
    assert info.value.reason == "aud"


@pytest.mark.unit
def test_issuer_is_compared_code_point_for_code_point(op: OpenIDProvider) -> None:
    token = op.issue_access_token(
        client_id="spa", sub="alice", resource=API, scope="orders:read", jti="j", now=NOW
    )
    with pytest.raises(TokenRejected) as info:
        validate_access_token(token, jwks=op.jwks(), issuer=ISSUER + "/", resource=API, now=NOW)
    assert info.value.reason == "iss"


@pytest.mark.unit
def test_exp_boundary_with_60_second_leeway(op: OpenIDProvider) -> None:
    token = op.issue_access_token(
        client_id="spa", sub="alice", resource=API, scope="orders:read", jti="j", now=NOW
    )
    exp = NOW + 300
    assert strict(op, token, now=exp + 59).sub == "alice"
    with pytest.raises(TokenRejected) as info:
        strict(op, token, now=exp + 60)
    assert info.value.reason == "exp"


@pytest.mark.unit
def test_token_from_another_issuer_key_fails_signature(op: OpenIDProvider) -> None:
    rogue = OpenIDProvider.generate(ISSUER)  # same issuer string, different key
    token = rogue.issue_access_token(
        client_id="spa", sub="alice", resource=API, scope="orders:read", jti="j", now=NOW
    )
    with pytest.raises(TokenRejected) as info:
        strict(op, token)
    assert info.value.reason == "signature"


@pytest.mark.unit
def test_es256_signature_segment_is_86_base64url_characters(op: OpenIDProvider) -> None:
    token = victim_id_token_for_attacker_app(op)
    assert len(token.split(".")[2]) == 86  # 64-byte r||s, unpadded base64url


# --------------------------------------------------------------------------- OIDC Core RP rules


@pytest.mark.unit
def test_rp_rejects_an_access_token_presented_as_login(op: OpenIDProvider) -> None:
    token = op.issue_access_token(
        client_id="spa", sub="alice", resource="spa", scope="openid", jti="j", now=NOW
    )
    with pytest.raises(TokenRejected) as info:
        validate_id_token(
            token, jwks=op.jwks(), issuer=ISSUER, client_id="spa", nonce="n-1", now=NOW + 1
        )
    assert info.value.reason == "typ"


@pytest.mark.unit
def test_rp_rejects_id_token_with_untrusted_extra_audience(op: OpenIDProvider) -> None:
    token = op.issue_id_token(
        client_id="spa", sub="alice", nonce="n-1", now=NOW, extra_audiences=["partner"]
    )
    kwargs: dict[str, Any] = {"jwks": op.jwks(), "issuer": ISSUER, "client_id": "spa"}
    with pytest.raises(TokenRejected) as info:
        validate_id_token(token, nonce="n-1", now=NOW + 1, **kwargs)
    assert info.value.reason == "aud:untrusted"
    trusted = frozenset({"partner"})
    claims = validate_id_token(token, nonce="n-1", now=NOW + 1, trusted_audiences=trusted, **kwargs)
    assert claims["sub"] == "alice"


@pytest.mark.unit
def test_rp_rejects_id_token_for_another_client(op: OpenIDProvider) -> None:
    token = victim_id_token_for_attacker_app(op)
    with pytest.raises(TokenRejected) as info:
        validate_id_token(
            token, jwks=op.jwks(), issuer=ISSUER, client_id="spa", nonce="n-1", now=NOW + 1
        )
    assert info.value.reason == "aud"


@pytest.mark.unit
def test_rp_checks_azp_when_present(op: OpenIDProvider) -> None:
    token = op.issue_id_token(client_id="spa", sub="alice", nonce="n-1", now=NOW, azp="other")
    with pytest.raises(TokenRejected) as info:
        validate_id_token(
            token, jwks=op.jwks(), issuer=ISSUER, client_id="spa", nonce="n-1", now=NOW + 1
        )
    assert info.value.reason == "azp"


@pytest.mark.unit
def test_rp_rejects_replayed_id_token_with_other_nonce(op: OpenIDProvider) -> None:
    token = op.issue_id_token(client_id="spa", sub="alice", nonce="n-1", now=NOW)
    with pytest.raises(TokenRejected) as info:
        validate_id_token(
            token, jwks=op.jwks(), issuer=ISSUER, client_id="spa", nonce="n-2", now=NOW + 1
        )
    assert info.value.reason == "nonce"


@pytest.mark.unit
def test_at_hash_detects_access_token_substitution(op: OpenIDProvider) -> None:
    token = op.issue_id_token(
        client_id="spa", sub="alice", nonce="n-1", now=NOW, access_token=ALICE_AT
    )
    kwargs: dict[str, Any] = {"jwks": op.jwks(), "issuer": ISSUER, "client_id": "spa"}
    claims = validate_id_token(token, nonce="n-1", now=NOW + 1, access_token=ALICE_AT, **kwargs)
    assert claims["at_hash"] == half_hash(ALICE_AT, "ES256")
    with pytest.raises(TokenRejected) as info:
        validate_id_token(token, nonce="n-1", now=NOW + 1, access_token=MALLORY_AT, **kwargs)
    assert info.value.reason == "at_hash"


@pytest.mark.unit
def test_half_hash_matches_oidc_core_appendix_a_vectors() -> None:
    # OIDC Core Appendix A.3 (at_hash) and A.4 (c_hash), both RS256.
    assert half_hash("jHkWEdUXMU1BwAsC4vtUsZwnNvTIxEl0z9K3vx5KF0Y", "RS256") == (
        "77QmUPtjPfzWtF2AnpK9RQ"
    )
    code = "Qcb0Orv1zh30vL1MPRsbm-diHiMwcLyZvn1arpZv-Jxf_11jnpEX3Tgfvk"
    assert half_hash(code, "RS256") == "LDktKdoQak3Pk0cnXxCltA"
    assert len(half_hash(code, "ES512")) == 43  # 32 bytes -> 43 unpadded characters


@pytest.mark.unit
def test_half_hash_refuses_algorithms_core_does_not_define() -> None:
    with pytest.raises(ValueError, match="EdDSA"):
        half_hash("x", "EdDSA")


# --------------------------------------------------------------------------- library behavior


@pytest.mark.unit
def test_pyjwt_audience_list_is_any_match_and_ignores_typ(op: OpenIDProvider) -> None:
    # PyJWT 2.15.1 jwt/api_jwt.py PyJWT._validate_aud: any listed audience matches.
    token = victim_id_token_for_attacker_app(op)
    pem = op.signing_key.as_pem(private=False)
    claims = pyjwt.decode(
        token,
        pem,
        algorithms=["ES256"],
        audience=[API, "quiz-app"],  # "our API, or our login client": the classic merge
        issuer=ISSUER,
        options={"verify_exp": False, "verify_iat": False},  # tokens are pinned to NOW
    )
    assert claims["sub"] == "alice"
    with pytest.raises(pyjwt.InvalidAudienceError):
        pyjwt.decode(
            token,
            pem,
            algorithms=["ES256"],
            audience=API,
            issuer=ISSUER,
            options={"verify_exp": False, "verify_iat": False},
        )


@pytest.mark.unit
def test_authlib_rfc9068_claims_waves_through_a_missing_typ() -> None:
    # authlib 1.9.5 authlib/oauth2/rfc9068/claims.py JWTAccessTokenClaims.validate
    claims = {"iss": ISSUER, "aud": API, "sub": "alice", "exp": NOW + 300}
    options: dict[str, Any] = {"aud": {"essential": True, "value": API}}
    untyped = JWTAccessTokenClaims(claims, {"alg": "ES256"}, dict(options))
    untyped.validate(now=NOW)  # pyright: ignore[reportUnknownMemberType]
    typed_jwt = JWTAccessTokenClaims(claims, {"alg": "ES256", "typ": "JWT"}, dict(options))
    with pytest.raises(InvalidClaimError):
        typed_jwt.validate(now=NOW)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.unit
def test_authlib_rfc8414_url_keeps_a_terminating_slash_ours_strips_it() -> None:
    issuer = "https://id.example.test/tenant1/"
    authlib_url = get_well_known_url(issuer, external=True)
    assert authlib_url == "https://id.example.test/.well-known/oauth-authorization-server/tenant1/"
    assert rfc8414_metadata_url(issuer) == (
        "https://id.example.test/.well-known/oauth-authorization-server/tenant1"
    )


@pytest.mark.unit
def test_pydantic_url_type_rewrites_issuer_so_exact_match_needs_str() -> None:
    assert str(AnyHttpUrl("https://id.example.test")) == "https://id.example.test/"
    assert str(AnyHttpUrl("https://ID.Example.test:443/t")) == "https://id.example.test/t"
    doc = {
        "issuer": "https://ID.Example.test",
        "jwks_uri": "https://k",
        "response_types_supported": ["code"],
    }
    with pytest.raises(MetadataRejected):
        validate_authorization_server_metadata(doc, "https://id.example.test")


# --------------------------------------------------------------------------- metadata


@pytest.mark.unit
@pytest.mark.parametrize(
    ("issuer", "rfc8414", "oidc"),
    [
        (
            "https://id.example.test",
            "https://id.example.test/.well-known/oauth-authorization-server",
            "https://id.example.test/.well-known/openid-configuration",
        ),
        (
            "https://id.example.test/tenant1",
            "https://id.example.test/.well-known/oauth-authorization-server/tenant1",
            "https://id.example.test/tenant1/.well-known/openid-configuration",
        ),
        (
            "https://id.example.test:8443/a/b/",
            "https://id.example.test:8443/.well-known/oauth-authorization-server/a/b",
            "https://id.example.test:8443/a/b/.well-known/openid-configuration",
        ),
    ],
    ids=["root", "path", "port-and-trailing-slash"],
)
def test_issuer_to_metadata_url_insert_vs_append(issuer: str, rfc8414: str, oidc: str) -> None:
    assert rfc8414_metadata_url(issuer) == rfc8414
    assert oidc_discovery_url(issuer) == oidc


@pytest.mark.unit
@pytest.mark.parametrize(
    ("resource", "url"),
    [
        (
            "https://api.example.test",
            "https://api.example.test/.well-known/oauth-protected-resource",
        ),
        (
            "https://api.example.test/",
            "https://api.example.test/.well-known/oauth-protected-resource",
        ),
        (
            "https://api.example.test/orders",
            "https://api.example.test/.well-known/oauth-protected-resource/orders",
        ),
        (
            "https://api.example.test/v1?tenant=a",
            "https://api.example.test/.well-known/oauth-protected-resource/v1?tenant=a",
        ),
    ],
    ids=["bare-host", "host-slash", "path", "path-and-query"],
)
def test_resource_to_metadata_url(resource: str, url: str) -> None:
    assert rfc9728_metadata_url(resource) == url


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    ["http://id.example.test", "https://id.example.test/#f", "https://id.example.test/?q=1"],
    ids=["http", "fragment", "query"],
)
def test_issuer_identifier_shape_is_enforced(bad: str) -> None:
    with pytest.raises(ValueError, match="identifier"):
        rfc8414_metadata_url(bad)


@pytest.mark.unit
def test_impersonating_as_metadata_is_not_used() -> None:
    good = {
        "issuer": ISSUER,
        "jwks_uri": ISSUER + "/jwks.json",
        "response_types_supported": ["code"],
    }
    assert validate_authorization_server_metadata(good, ISSUER).jwks_uri.endswith("jwks.json")
    with pytest.raises(MetadataRejected, match="issuer"):
        validate_authorization_server_metadata({**good, "issuer": "https://evil.test"}, ISSUER)
    with pytest.raises(MetadataRejected, match="jwks_uri"):
        validate_authorization_server_metadata({**good, "jwks_uri": "http://k"}, ISSUER)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_discovers_as_through_401_and_protected_resource_metadata(
    op: OpenIDProvider,
) -> None:
    async with api_client(op, naive=False) as client:
        challenge = (await client.get("/orders")).headers["www-authenticate"]
        metadata_url = challenge.split('resource_metadata="', 1)[1].rstrip('"')
        response = await client.get(metadata_url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "max-age=3600"
    prm = validate_protected_resource_metadata(response.json(), API)
    assert choose_authorization_server(prm, frozenset({ISSUER})) == ISSUER
    assert response.json()["scopes_supported"] == ["orders:read"]
    with pytest.raises(MetadataRejected, match="trusted"):
        choose_authorization_server(prm, frozenset({"https://other.example.test"}))
    with pytest.raises(MetadataRejected, match="resource"):
        validate_protected_resource_metadata(response.json(), "https://api.example.test/")


@pytest.mark.unit
def test_lab_uses_injected_clock_not_wall_clock(op: OpenIDProvider) -> None:
    token = op.issue_access_token(
        client_id="spa", sub="alice", resource=API, scope="orders:read", jti="j", now=NOW
    )
    with pytest.raises(TokenRejected):
        strict(op, token, now=int(time.time()) + 10**9)
