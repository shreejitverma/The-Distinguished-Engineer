"""Edge termination vs zero-trust propagation: spoofing, replay, and the checks that stop them."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Annotated, Any

import httpx
import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from api_labs.f14_api_authentication_and_identity.edge_vs_zero_trust import (
    BearerBackend,
    EdgeGateway,
    InternalIssuer,
    PropagationMode,
    SvidError,
    TokenRejected,
    TokenVerifier,
    Upstream,
    build_header_trust_service,
    build_zero_trust_service,
    decode_unverified_claims,
    issue_svid,
    lab_key,
    mint_access_token,
    propagate,
    spiffe_id_from_pem,
    with_peer_certificate,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from starlette.requests import Request
    from starlette.types import ASGIApp

IDP = "https://idp.example.com"
EDGE_ISSUER = "https://edge.internal.example"
API_AUD = "https://api.example.com"
ORDERS_AUD = "https://orders.internal.example"
PAYMENTS_AUD = "https://payments.internal.example"
GATEWAY_ID = "spiffe://prod.example/ns/edge/sa/gateway"
ORDERS_ID = "spiffe://prod.example/ns/shop/sa/orders"
REPORTS_ID = "spiffe://prod.example/ns/shop/sa/reports"

IDP_KEY = lab_key(1001)
EDGE_KEY = lab_key(2002)
ATTACKER_KEY = lab_key(3003)
GATEWAY_PEM, _ = issue_svid([GATEWAY_ID], 11)
ORDERS_PEM, _ = issue_svid([ORDERS_ID], 12)
REPORTS_PEM, _ = issue_svid([REPORTS_ID], 13)

ORDERS = {"o-1": {"id": "o-1", "owner": "alice"}, "o-2": {"id": "o-2", "owner": "bob"}}
PAYMENTS = {"p-1": {"id": "p-1", "owner": "alice"}}

EXTERNAL = TokenVerifier(IDP, API_AUD, {"idp-1": IDP_KEY.public_key()})
ISSUER = InternalIssuer(EDGE_ISSUER, "edge-1", EDGE_KEY, GATEWAY_ID, GATEWAY_PEM)


def internal_verifier(audience: str) -> TokenVerifier:
    return TokenVerifier(EDGE_ISSUER, audience, {"edge-1": EDGE_KEY.public_key()})


def user_token(subject: str = "alice", scopes: Sequence[str] = ("orders:read",)) -> str:
    return mint_access_token(
        IDP_KEY, kid="idp-1", issuer=IDP, subject=subject, audience=API_AUD, scopes=scopes
    )


def orders_service(**kwargs: Any) -> Starlette:
    return build_zero_trust_service(
        "orders",
        ORDERS,
        internal_verifier(ORDERS_AUD),
        allowed_callers=frozenset({GATEWAY_ID}),
        **kwargs,
    )


def payments_service(**kwargs: Any) -> Starlette:
    callers = frozenset({GATEWAY_ID, ORDERS_ID})
    return build_zero_trust_service(
        "payments", PAYMENTS, internal_verifier(PAYMENTS_AUD), allowed_callers=callers, **kwargs
    )


async def call(
    app: ASGIApp,
    path: str,
    headers: Sequence[tuple[str, str]] = (),
    peer_pem: str | None = None,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=with_peer_certificate(app, peer_pem))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=list(headers))


def bearer(token: str) -> tuple[str, str]:
    return ("Authorization", f"Bearer {token}")


# --- edge-only: identity in a plain header ------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_header_trust_service_believes_any_caller_that_reaches_it() -> None:
    # No credential at all: a lateral caller inside the network just types the header.
    r = await call(
        build_header_trust_service("orders", ORDERS), "/orders/o-2", [("X-User-ID", "bob")]
    )
    assert (r.status_code, r.json()["served_to"]) == (200, "bob")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_header_append_lets_the_client_copy_win() -> None:
    gateway = EdgeGateway(
        [Upstream("/orders", ORDERS_AUD, build_header_trust_service("orders", ORDERS))],
        EXTERNAL,
        PropagationMode.HEADER_APPEND,
    )
    # Alice authenticates honestly, then adds Bob's identity header; Starlette reads the first.
    r = await call(gateway, "/orders/o-2", [bearer(user_token("alice")), ("X-User-ID", "bob")])
    assert (r.status_code, r.json()["served_to"]) == (200, "bob")


@pytest.mark.unit
def test_header_append_emits_two_identity_lines_and_set_emits_one() -> None:
    principal = EXTERNAL.verify(user_token("alice"))
    incoming = [(b"x-user-id", b"bob")]
    appended = propagate(
        incoming, principal, PropagationMode.HEADER_APPEND, audience="", issuer=None
    )
    assert [v for n, v in appended if n == b"x-user-id"] == [b"bob", b"alice"]
    replaced = propagate(incoming, principal, PropagationMode.HEADER_SET, audience="", issuer=None)
    assert [v for n, v in replaced if n == b"x-user-id"] == [b"alice"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_header_set_strips_client_copies_in_any_case() -> None:
    gateway = EdgeGateway(
        [Upstream("/orders", ORDERS_AUD, build_header_trust_service("orders", ORDERS))],
        EXTERNAL,
        PropagationMode.HEADER_SET,
    )
    spoof = [bearer(user_token("alice")), ("X-USER-ID", "bob"), ("x-user-scopes", "admin")]
    assert (await call(gateway, "/orders/o-2", spoof)).status_code == 404
    r = await call(gateway, "/orders/o-1", spoof)
    assert (r.status_code, r.json()["served_to"]) == (200, "alice")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gateway_challenges_missing_and_rejects_internal_tokens() -> None:
    gateway = EdgeGateway(
        [Upstream("/orders", ORDERS_AUD, build_header_trust_service("orders", ORDERS))],
        EXTERNAL,
        PropagationMode.HEADER_SET,
    )
    missing = await call(gateway, "/orders/o-1")
    assert (missing.status_code, missing.headers["www-authenticate"]) == (401, 'Bearer realm="api"')
    internal = ISSUER.mint(EXTERNAL.verify(user_token()), ORDERS_AUD)
    r = await call(gateway, "/orders/o-1", [bearer(internal)])
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers["www-authenticate"]


# --- zero trust: peer identity + audience-restricted token + object check ------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_token_exchange_end_to_end_with_object_level_check() -> None:
    gateway = EdgeGateway(
        [Upstream("/orders", ORDERS_AUD, orders_service())],
        EXTERNAL,
        PropagationMode.TOKEN_EXCHANGE,
        ISSUER,
    )
    token = user_token("alice")
    r = await call(gateway, "/orders/o-1", [bearer(token), ("X-User-ID", "bob")])
    assert r.status_code == 200
    assert (r.json()["served_to"], r.json()["via"]) == ("alice", GATEWAY_ID)
    # Same valid token and scope, someone else's object: API1:2023, answered as 404.
    assert (await call(gateway, "/orders/o-2", [bearer(token)])).status_code == 404


@pytest.mark.unit
def test_exchanged_token_is_audience_restricted_short_lived_and_names_the_gateway() -> None:
    principal = EXTERNAL.verify(user_token("alice"))
    headers = propagate(
        [(b"authorization", b"Bearer external"), (b"x-user-id", b"bob")],
        principal,
        PropagationMode.TOKEN_EXCHANGE,
        audience=ORDERS_AUD,
        issuer=ISSUER,
    )
    auth = [v for n, v in headers if n == b"authorization"]
    assert len(auth) == 1
    assert not [n for n, _ in headers if n == b"x-user-id"]
    claims = decode_unverified_claims(auth[0].decode().removeprefix("Bearer "))
    assert (claims["aud"], claims["iss"], claims["sub"]) == (ORDERS_AUD, EDGE_ISSUER, "alice")
    assert claims["act"] == {"sub": GATEWAY_ID}
    assert claims["exp"] - claims["iat"] == 60


@pytest.mark.unit
@pytest.mark.asyncio
async def test_direct_call_without_peer_certificate_is_refused() -> None:
    token = ISSUER.mint(EXTERNAL.verify(user_token()), ORDERS_AUD)
    r = await call(orders_service(), "/orders/o-1", [bearer(token)])
    assert (r.status_code, r.json()["error"]) == (403, "caller_not_allowed")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lateral_workload_not_in_allowlist_is_refused_even_with_valid_token() -> None:
    token = ISSUER.mint(EXTERNAL.verify(user_token()), ORDERS_AUD)
    r = await call(orders_service(), "/orders/o-1", [bearer(token)], peer_pem=REPORTS_PEM)
    assert (r.status_code, r.json()["peer"]) == (403, REPORTS_ID)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_token_for_orders_replayed_at_payments_is_invalid_token() -> None:
    token = ISSUER.mint(EXTERNAL.verify(user_token(scopes=["payments:read"])), ORDERS_AUD)
    r = await call(payments_service(), "/payments/p-1", [bearer(token)], peer_pem=GATEWAY_PEM)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Bearer realm="payments", error="invalid_token"'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stolen_delegated_token_fails_when_actor_is_not_the_peer() -> None:
    # A compromised orders workload replays a token the gateway minted for payments.
    token = ISSUER.mint(EXTERNAL.verify(user_token(scopes=["payments:read"])), PAYMENTS_AUD)
    r = await call(payments_service(), "/payments/p-1", [bearer(token)], peer_pem=ORDERS_PEM)
    assert (r.status_code, r.json()["actor"]) == (403, GATEWAY_ID)
    unbound = payments_service(bind_actor_to_peer=False)
    assert (await call(unbound, "/payments/p-1", [bearer(token)], ORDERS_PEM)).status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_passthrough_external_token_fails_audience_unless_audience_is_shared() -> None:
    def upstreams(verifier: TokenVerifier) -> list[Upstream]:
        callers = frozenset({GATEWAY_ID})
        orders = build_zero_trust_service(
            "orders", ORDERS, verifier, allowed_callers=callers, bind_actor_to_peer=False
        )
        return [Upstream("/orders", ORDERS_AUD, with_peer_certificate(orders, GATEWAY_PEM))]

    token = user_token()
    strict = EdgeGateway(
        upstreams(TokenVerifier(IDP, ORDERS_AUD, EXTERNAL.keys)),
        EXTERNAL,
        PropagationMode.PASSTHROUGH,
    )
    assert (await call(strict, "/orders/o-1", [bearer(token)])).status_code == 401
    # Making passthrough work means every service accepts the edge audience: a token any one
    # of them receives can be replayed at all of them (RFC 8725 Section 2.7).
    shared = EdgeGateway(upstreams(EXTERNAL), EXTERNAL, PropagationMode.PASSTHROUGH)
    assert (await call(shared, "/orders/o-1", [bearer(token)])).status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_token_gets_a_bare_bearer_challenge() -> None:
    r = await call(orders_service(), "/orders/o-1", peer_pem=GATEWAY_PEM)
    assert (r.status_code, r.headers["www-authenticate"]) == (401, 'Bearer realm="orders"')


@pytest.mark.unit
@pytest.mark.asyncio
async def test_insufficient_scope_is_403_with_required_scope() -> None:
    token = ISSUER.mint(EXTERNAL.verify(user_token(scopes=["payments:read"])), ORDERS_AUD)
    r = await call(orders_service(), "/orders/o-1", [bearer(token)], peer_pem=GATEWAY_PEM)
    assert r.status_code == 403
    assert r.headers["www-authenticate"] == (
        'Bearer realm="orders", error="insufficient_scope", scope="orders:read"'
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_authorization_fields_are_invalid_request() -> None:
    token = ISSUER.mint(EXTERNAL.verify(user_token()), ORDERS_AUD)
    headers = [bearer(token), bearer(token)]
    r = await call(orders_service(), "/orders/o-1", headers, peer_pem=GATEWAY_PEM)
    assert (r.status_code, r.json()["error"]) == (400, "invalid_request")


# --- the RFC 9068 verifier ----------------------------------------------------------------


def _mint(**overrides: Any) -> str:
    args: dict[str, Any] = {
        "kid": "idp-1",
        "issuer": IDP,
        "subject": "alice",
        "audience": API_AUD,
        "scopes": ["orders:read"],
    }
    key = overrides.pop("key", IDP_KEY)
    args.update(overrides)
    return mint_access_token(key, **args)


REJECTIONS: list[tuple[str, Callable[[], str], str]] = [
    ("id-token-typ", lambda: _mint(typ="JWT"), "typ 'JWT'"),
    ("wrong-issuer", lambda: _mint(issuer="https://evil.example"), "InvalidIssuerError"),
    ("wrong-audience", lambda: _mint(audience=ORDERS_AUD), "InvalidAudienceError"),
    ("no-audience", lambda: _mint(audience=None), "MissingRequiredClaimError"),
    ("unknown-kid", lambda: _mint(kid="idp-9"), "unknown kid"),
    ("forged-signature", lambda: _mint(key=ATTACKER_KEY), "InvalidSignatureError"),
    ("expired", lambda: _mint(now=time.time() - 390), "ExpiredSignatureError"),
    (
        "hs256",
        lambda: jwt.encode(
            {"sub": "alice"},
            "k" * 32,
            algorithm="HS256",
            headers={"kid": "idp-1", "typ": "at+jwt"},
        ),
        "alg 'HS256'",
    ),
    ("garbage", lambda: "not.a.jwt", "malformed"),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("make", "reason"), [r[1:] for r in REJECTIONS], ids=[r[0] for r in REJECTIONS]
)
def test_verifier_rejects(make: Callable[[], str], reason: str) -> None:
    with pytest.raises(TokenRejected, match=reason) as info:
        EXTERNAL.verify(make())
    assert info.value.error == "invalid_token"


@pytest.mark.unit
@pytest.mark.parametrize("typ", ["at+jwt", "at+JWT", "application/at+jwt", "APPLICATION/AT+JWT"])
def test_verifier_accepts_every_spelling_of_the_access_token_type(typ: str) -> None:
    assert EXTERNAL.verify(_mint(typ=typ)).subject == "alice"


@pytest.mark.unit
def test_expiry_leeway_boundary() -> None:
    # Lifetime 300 s, leeway 60 s: expired 30 s ago passes, expired 90 s ago does not.
    assert EXTERNAL.verify(_mint(now=time.time() - 330)).subject == "alice"
    with pytest.raises(TokenRejected, match="ExpiredSignatureError"):
        EXTERNAL.verify(_mint(now=time.time() - 390))


@pytest.mark.unit
def test_pyjwt_defaults_skip_audience_and_issuer_when_not_configured() -> None:
    key = IDP_KEY.public_key()
    # audience=None accepts a token with no aud at all, and issuer=None accepts any iss.
    claims = jwt.decode(_mint(audience=None, issuer="https://evil.example"), key, ["ES256"])
    assert claims["iss"] == "https://evil.example"
    # ...but a token that does carry aud fails when the verifier names no audience.
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(_mint(), key, ["ES256"])


@pytest.mark.unit
def test_nested_actors_are_ignored_only_current_actor_counts() -> None:
    claims = {
        "iss": EDGE_ISSUER,
        "sub": "alice",
        "client_id": "web-app",
        "aud": ORDERS_AUD,
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
        "jti": "j-1",
        "scope": "orders:read",
        "act": {"sub": GATEWAY_ID, "act": {"sub": REPORTS_ID}},
    }
    token = jwt.encode(claims, EDGE_KEY, "ES256", headers={"kid": "edge-1", "typ": "at+jwt"})
    assert internal_verifier(ORDERS_AUD).verify(token).actor == GATEWAY_ID


# --- workload identity --------------------------------------------------------------------


@pytest.mark.unit
def test_leaf_svid_yields_its_spiffe_id() -> None:
    assert spiffe_id_from_pem(ORDERS_PEM) == ORDERS_ID


@pytest.mark.unit
@pytest.mark.parametrize(
    ("uris", "ca", "reason"),
    [
        ([ORDERS_ID, REPORTS_ID], False, "exactly one URI SAN, got 2"),
        ([], False, "exactly one URI SAN, got 0"),
        (["spiffe://prod.example"], False, "not a leaf SPIFFE ID"),
        (["https://prod.example/ns/shop/sa/orders"], False, "not a leaf SPIFFE ID"),
        ([ORDERS_ID], True, "CA certificates must not authenticate"),
    ],
    ids=["two-uris", "no-uri", "root-id", "https-scheme", "ca-cert"],
)
def test_unusable_svids_are_rejected(uris: list[str], ca: bool, reason: str) -> None:
    pem, _ = issue_svid(uris, 99, ca=ca)
    with pytest.raises(SvidError, match=reason):
        spiffe_id_from_pem(pem)


# --- framework defaults the architecture has to override ----------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_default_on_error_is_400_without_a_challenge() -> None:
    async def home(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/", home)],
        middleware=[Middleware(AuthenticationMiddleware, backend=BearerBackend(EXTERNAL))],
    )
    r = await call(app, "/", [bearer(_mint(audience=ORDERS_AUD))])
    assert r.status_code == 400
    assert "www-authenticate" not in r.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_http_bearer_missing_credentials_is_401_bearer() -> None:
    app = FastAPI()

    @app.get("/")
    async def home(
        creds: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())],
    ) -> dict[str, str]:
        return {"scheme": creds.scheme}

    r = await call(app, "/")
    assert (r.status_code, r.headers["www-authenticate"]) == (401, "Bearer")
    # HTTPBearer only parses; an unverifiable token still reaches the endpoint.
    assert (await call(app, "/", [bearer("not.a.jwt")])).json() == {"scheme": "Bearer"}
