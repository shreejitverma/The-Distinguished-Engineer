"""OAuth 2.0 -> OAuth 2.1 and RFC 9700: PKCE, exact redirect URIs, removed grants, code replay."""

from __future__ import annotations

import asyncio
import base64
import importlib
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio

from api_labs.f14_api_authentication_and_identity.oauth21_server import (
    CODE_LIFETIME_SECONDS,
    AuthorizationResponseError,
    AuthorizationServer,
    Client,
    ErrorPage,
    Redirect,
    RegistrationError,
    create_app,
    hash_client_secret,
    is_pkce_value,
    new_code_verifier,
    redirect_uri_matches,
    s256_challenge,
    validate_authorization_response,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

ISSUER = "https://as.example.test"
WEB_SECRET = "web-secret-EXAMPLE-NOT-A-REAL-SECRET"  # noqa: S105 - test placeholder
LEGACY_SECRET = "legacy-secret-EXAMPLE-NOT-A-REAL-SECRET"  # noqa: S105 - test placeholder
# RFC 7636 Appendix B.
RFC7636_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC7636_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
# Authlib ships no type information, so its PKCE module is loaded as Any.
AUTHLIB_PKCE: Any = importlib.import_module("authlib.oauth2.rfc7636")


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def make_clients() -> list[Client]:
    return [
        Client("spa", ("https://spa.example.test/cb",)),
        Client(
            "web",
            ("https://web.example.test/cb?tenant=a",),
            secret_sha256=hash_client_secret(WEB_SECRET),
        ),
        Client(
            "legacy",
            ("https://legacy.example.test/cb", "https://legacy.example.test/cb2"),
            secret_sha256=hash_client_secret(LEGACY_SECRET),
            legacy_oauth20=True,
        ),
        Client("native", ("http://127.0.0.1/callback",)),
    ]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def server(clock: Clock) -> AuthorizationServer:
    return AuthorizationServer(ISSUER, make_clients(), clock=clock)


@pytest_asyncio.fixture
async def http(server: AuthorizationServer) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(server))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def query_of(location: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def authorize_code(
    server: AuthorizationServer, client_id: str, verifier: str | None, **extra: str
) -> str:
    params = [("response_type", "code"), ("client_id", client_id), ("state", "s1")]
    if verifier is not None:
        params += [("code_challenge", s256_challenge(verifier)), ("code_challenge_method", "S256")]
    params += list(extra.items())
    outcome = server.authorize(params, subject="alice")
    assert isinstance(outcome, Redirect), outcome
    return query_of(outcome.location)["code"]


def authorize_error(server: AuthorizationServer, params: list[tuple[str, str]]) -> str:
    outcome = server.authorize(params, subject="alice")
    assert isinstance(outcome, Redirect), outcome
    reply = query_of(outcome.location)
    assert reply["iss"] == ISSUER  # draft Section 4.1.2.1: errors carry iss too
    return reply["error"]


# PKCE values (RFC 7636, draft-ietf-oauth-v2-1-17 Section 4.1.1)


@pytest.mark.unit
def test_s256_matches_rfc_7636_appendix_b_vector() -> None:
    assert s256_challenge(RFC7636_VERIFIER) == RFC7636_CHALLENGE


@pytest.mark.unit
def test_s256_matches_oauth_2_1_draft_17_example_pair() -> None:
    # The Section 4.1.1 challenge and the Section 4.1.3 verifier belong together.
    verifier = "3641a2d12d66101249cdf7a79c000c1f8c05d2aafcf14bf146497bed"
    assert s256_challenge(verifier) == "6fdkQaPm51l13DSukcAH3Mdx7_ntecHYd1vi3n0hMZY"


@pytest.mark.unit
def test_new_code_verifier_is_43_characters_carrying_32_random_octets() -> None:
    verifier = new_code_verifier()
    assert len(verifier) == 43
    assert is_pkce_value(verifier)
    assert len(base64.urlsafe_b64decode(verifier + "=")) == 32
    assert len(s256_challenge(verifier)) == 43


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("a" * 42, False),
        ("a" * 43, True),
        ("a" * 128, True),
        ("a" * 129, False),
        ("A-._~" + "z" * 38, True),
        ("+" + "a" * 42, False),
        ("a" * 42 + "=", False),
    ],
    ids=["42", "43", "128", "129", "unreserved", "plus", "padding"],
)
def test_pkce_value_follows_43_to_128_unreserved_abnf(value: str, valid: bool) -> None:
    assert is_pkce_value(value) is valid


@pytest.mark.unit
def test_authlib_1_9_5_defaults_still_accept_plain() -> None:
    # The pinned library computes S256 identically but keeps RFC 7636's plain default.
    assert AUTHLIB_PKCE.create_s256_code_challenge(RFC7636_VERIFIER) == RFC7636_CHALLENGE
    assert AUTHLIB_PKCE.CodeChallenge.DEFAULT_CODE_CHALLENGE_METHOD == "plain"
    assert AUTHLIB_PKCE.CodeChallenge.SUPPORTED_CODE_CHALLENGE_METHOD == ["plain", "S256"]


# Redirect URI matching (RFC 9700 Section 4.1.3, draft Sections 4.1.1 and 8.4.2)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("registered", "presented", "matches"),
    [
        ("https://c.example/cb", "https://c.example/cb", True),
        ("https://c.example/cb", "https://c.example/cb/", False),
        ("https://c.example/cb", "https://C.example/cb", False),
        ("https://c.example/cb", "https://c.example:443/cb", False),
        ("https://c.example/cb", "https://c.example/%63b", False),
        ("https://c.example/cb", "https://c.example/cb?x=1", False),
        ("https://c.example/cb", "https://c.example/cb/../evil", False),
        ("https://c.example:8443/cb", "https://c.example:9443/cb", False),
        ("http://127.0.0.1/callback", "http://127.0.0.1:51004/callback", True),
        ("http://127.0.0.1:1/callback", "http://127.0.0.1:65535/callback", True),
        ("http://[::1]/callback", "http://[::1]:61023/callback", True),
        ("http://127.0.0.1/callback", "http://127.0.0.1:0/callback", False),
        ("http://127.0.0.1/callback", "http://127.0.0.1:65536/callback", False),
        ("http://127.0.0.1/callback", "http://127.0.0.1:51004/other", False),
        ("http://localhost/callback", "http://localhost:51004/callback", False),
        ("http://127.0.0.1/callback", "https://127.0.0.1:51004/callback", False),
    ],
    ids=[
        "exact",
        "trailing-slash",
        "host-case",
        "default-port",
        "percent-encoding",
        "extra-query",
        "dot-segments",
        "https-port",
        "loopback-v4-port",
        "loopback-port-range",
        "loopback-v6-port",
        "loopback-port-0",
        "loopback-port-65536",
        "loopback-path",
        "localhost-name",
        "loopback-scheme",
    ],
)
def test_redirect_uri_is_simple_string_comparison_with_loopback_port_exception(
    registered: str, presented: str, matches: bool
) -> None:
    assert redirect_uri_matches(registered, presented) is matches


@pytest.mark.unit
@pytest.mark.parametrize(
    "uri",
    [
        "https://c.example/cb#frag",
        "http://c.example/cb",
        "http://localhost:8080/cb",
        "myapp:/cb",
        "/relative/cb",
    ],
    ids=["fragment", "http-remote", "http-localhost", "scheme-without-dot", "relative"],
)
def test_registration_rejects_uris_oauth_2_1_disallows(uri: str) -> None:
    with pytest.raises(RegistrationError):
        Client("bad", (uri,))


@pytest.mark.unit
def test_registration_accepts_https_loopback_and_reverse_domain_schemes() -> None:
    client = Client(
        "ok",
        ("https://c.example/cb", "http://[::1]/cb", "com.example.app:/oauth2redirect"),
    )
    assert client.is_public


# Authorization endpoint


@pytest.mark.unit
@pytest.mark.asyncio
async def test_full_code_flow_over_asgi(http: httpx.AsyncClient) -> None:
    verifier = new_code_verifier()
    response = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "spa",
            "redirect_uri": "https://spa.example.test/cb",
            "state": "xyz",
            "code_challenge": s256_challenge(verifier),
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("https://spa.example.test/cb?code=")
    code = validate_authorization_response(location, expected_state="xyz", expected_issuer=ISSUER)

    # OAuth 2.1 client: no redirect_uri in the token request (draft Section 10.2).
    token = await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "spa",
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200
    assert token.headers["cache-control"] == "no-store"
    body = token.json()
    assert body["token_type"] == "Bearer"  # noqa: S105 - a token type, not a secret
    assert body["expires_in"] == 3600
    assert "refresh_token" not in body

    resource = await http.get(
        "/resource", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert resource.status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unregistered_redirect_uri_gets_400_not_a_redirect(http: httpx.AsyncClient) -> None:
    response = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "spa",
            "redirect_uri": "https://attacker.example/.spa.example.test/cb",
            "code_challenge": RFC7636_CHALLENGE,
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 400
    assert "location" not in response.headers


@pytest.mark.unit
def test_unknown_client_and_repeated_parameter_are_not_redirected(
    server: AuthorizationServer,
) -> None:
    assert server.authorize([("client_id", "nobody")], "alice") == ErrorPage(400, "invalid_client")
    repeated = [("client_id", "spa"), ("client_id", "spa")]
    assert server.authorize(repeated, "alice") == ErrorPage(400, "invalid_request")


@pytest.mark.unit
def test_omitted_redirect_uri_requires_a_single_registration(server: AuthorizationServer) -> None:
    base = [
        ("response_type", "code"),
        ("code_challenge", RFC7636_CHALLENGE),
        ("code_challenge_method", "S256"),
    ]
    assert isinstance(server.authorize([*base, ("client_id", "spa")], "alice"), Redirect)
    outcome = server.authorize([*base, ("client_id", "legacy")], "alice")
    assert outcome == ErrorPage(400, "invalid_request")


@pytest.mark.unit
def test_redirect_keeps_registered_query_and_adds_iss(server: AuthorizationServer) -> None:
    outcome = server.authorize(
        [
            ("response_type", "code"),
            ("client_id", "web"),
            ("state", "s1"),
            ("code_challenge", RFC7636_CHALLENGE),
            ("code_challenge_method", "S256"),
        ],
        "alice",
    )
    assert isinstance(outcome, Redirect)
    assert outcome.location.startswith("https://web.example.test/cb?tenant=a&code=")
    reply = query_of(outcome.location)
    assert reply["tenant"] == "a"
    assert reply["state"] == "s1"
    assert reply["iss"] == ISSUER


@pytest.mark.unit
@pytest.mark.parametrize("client_id", ["spa", "web", "native"])
def test_missing_code_challenge_is_rejected_for_public_and_oauth_2_1_clients(
    server: AuthorizationServer, client_id: str
) -> None:
    params = [("response_type", "code"), ("client_id", client_id), ("state", "s")]
    assert authorize_error(server, params) == "invalid_request"


@pytest.mark.unit
@pytest.mark.parametrize(
    "pkce",
    [
        [("code_challenge", RFC7636_VERIFIER), ("code_challenge_method", "plain")],
        [("code_challenge", RFC7636_CHALLENGE)],
        [("code_challenge", RFC7636_CHALLENGE), ("code_challenge_method", "s256")],
        [("code_challenge", "short"), ("code_challenge_method", "S256")],
        [("code_challenge_method", "S256")],
    ],
    ids=["plain", "method-omitted-defaults-to-plain", "lowercase-s256", "short", "no-challenge"],
)
def test_only_s256_with_a_valid_challenge_is_accepted(
    server: AuthorizationServer, pkce: list[tuple[str, str]]
) -> None:
    params = [("response_type", "code"), ("client_id", "legacy"), *pkce]
    params.append(("redirect_uri", "https://legacy.example.test/cb"))
    assert authorize_error(server, params) == "invalid_request"


@pytest.mark.unit
@pytest.mark.parametrize("response_type", ["token", "code token", "password"])
def test_implicit_and_other_response_types_are_unsupported(
    server: AuthorizationServer, response_type: str
) -> None:
    params = [
        ("response_type", response_type),
        ("client_id", "spa"),
        ("code_challenge", RFC7636_CHALLENGE),
        ("code_challenge_method", "S256"),
    ]
    assert authorize_error(server, params) == "unsupported_response_type"


@pytest.mark.unit
def test_loopback_redirect_with_ephemeral_port_is_used_verbatim(
    server: AuthorizationServer,
) -> None:
    outcome = server.authorize(
        [
            ("response_type", "code"),
            ("client_id", "native"),
            ("redirect_uri", "http://127.0.0.1:51004/callback"),
            ("code_challenge", RFC7636_CHALLENGE),
            ("code_challenge_method", "S256"),
        ],
        "alice",
    )
    assert isinstance(outcome, Redirect)
    assert outcome.location.startswith("http://127.0.0.1:51004/callback?code=")


# Token endpoint


def code_form(code: str, client_id: str, verifier: str | None) -> list[tuple[str, str]]:
    form = [("grant_type", "authorization_code"), ("code", code), ("client_id", client_id)]
    if verifier is not None:
        form.append(("code_verifier", verifier))
    return form


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sent", "error"),
    [
        (None, "invalid_request"),
        ("", "invalid_request"),
        ("a" * 42, "invalid_request"),
        ("x", "invalid_request"),
        ("b" * 43, "invalid_grant"),
    ],
    ids=["missing", "empty-means-omitted", "42-chars", "one-char", "wrong-verifier"],
)
async def test_bad_code_verifier_is_rejected(
    server: AuthorizationServer, sent: str | None, error: str
) -> None:
    code = authorize_code(server, "spa", RFC7636_VERIFIER)
    result = await server.token(code_form(code, "spa", sent))
    assert (result.status, result.body) == (400, {"error": error})
    # A failed attempt does not consume the code: the right verifier still works.
    good = await server.token(code_form(code, "spa", RFC7636_VERIFIER))
    assert good.status == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pkce_downgrade_verifier_without_challenge_is_rejected(
    server: AuthorizationServer,
) -> None:
    # The legacy confidential client may skip PKCE, but a verifier then proves a downgrade.
    auth = ("client_secret", LEGACY_SECRET)
    uri = "https://legacy.example.test/cb"
    code = authorize_code(server, "legacy", None, redirect_uri=uri)
    attacked = await server.token(
        [*code_form(code, "legacy", RFC7636_VERIFIER), auth, ("redirect_uri", uri)]
    )
    assert (attacked.status, attacked.body["error"]) == (400, "invalid_request")
    honest = await server.token([*code_form(code, "legacy", None), auth, ("redirect_uri", uri)])
    assert honest.status == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_client_must_repeat_redirect_uri_but_oauth_2_1_client_need_not(
    server: AuthorizationServer,
) -> None:
    uri = "https://legacy.example.test/cb2"
    auth = ("client_secret", LEGACY_SECRET)
    code = authorize_code(server, "legacy", RFC7636_VERIFIER, redirect_uri=uri)
    missing = await server.token([*code_form(code, "legacy", RFC7636_VERIFIER), auth])
    assert missing.body == {"error": "invalid_request"}
    wrong = await server.token(
        [
            *code_form(code, "legacy", RFC7636_VERIFIER),
            auth,
            ("redirect_uri", "https://legacy.example.test/cb"),
        ]
    )
    assert wrong.body == {"error": "invalid_grant"}

    code21 = authorize_code(
        server, "spa", RFC7636_VERIFIER, redirect_uri="https://spa.example.test/cb"
    )
    assert (await server.token(code_form(code21, "spa", RFC7636_VERIFIER))).status == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_password_and_unknown_grants_are_unsupported(server: AuthorizationServer) -> None:
    form = [
        ("grant_type", "password"),
        ("username", "alice"),
        ("password", "pw-EXAMPLE"),
        ("client_id", "spa"),
    ]
    result = await server.token(form)
    assert (result.status, result.body) == (400, {"error": "unsupported_grant_type"})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valid_code_replay_is_refused_and_revokes_issued_tokens(
    server: AuthorizationServer,
) -> None:
    code = authorize_code(server, "spa", RFC7636_VERIFIER)
    first = await server.token(code_form(code, "spa", RFC7636_VERIFIER))
    token = first.body["access_token"]
    assert server.token_is_active(token)
    second = await server.token(code_form(code, "spa", RFC7636_VERIFIER))
    assert (second.status, second.body) == (400, {"error": "invalid_grant"})
    assert not server.token_is_active(token)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_replay_does_not_revoke_issued_tokens(server: AuthorizationServer) -> None:
    code = authorize_code(server, "spa", RFC7636_VERIFIER)
    first = await server.token(code_form(code, "spa", RFC7636_VERIFIER))
    replay = await server.token(code_form(code, "spa", "c" * 43))
    assert replay.body == {"error": "invalid_grant"}
    assert server.token_is_active(first.body["access_token"])  # no denial-of-service lever


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_redemptions_mint_exactly_one_token(server: AuthorizationServer) -> None:
    code = authorize_code(server, "spa", RFC7636_VERIFIER)
    results = await asyncio.gather(
        *(server.token(code_form(code, "spa", RFC7636_VERIFIER)) for _ in range(5))
    )
    assert sorted(r.status for r in results) == [200, 400, 400, 400, 400]
    assert len(server.tokens) == 1
    # The losers were valid replays, so the winner's token is revoked as well.
    assert not any(server.token_is_active(t) for t in server.tokens)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_expires_after_600_seconds(server: AuthorizationServer, clock: Clock) -> None:
    fresh = authorize_code(server, "spa", RFC7636_VERIFIER)
    stale = authorize_code(server, "spa", RFC7636_VERIFIER)
    clock.now += CODE_LIFETIME_SECONDS - 1
    assert (await server.token(code_form(fresh, "spa", RFC7636_VERIFIER))).status == 200
    clock.now += 1
    result = await server.token(code_form(stale, "spa", RFC7636_VERIFIER))
    assert result.body == {"error": "invalid_grant"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_is_bound_to_the_client_it_was_issued_to(server: AuthorizationServer) -> None:
    code = authorize_code(server, "spa", RFC7636_VERIFIER)
    result = await server.token(code_form(code, "native", RFC7636_VERIFIER))
    assert result.body == {"error": "invalid_grant"}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("secret", "status"),
    [(WEB_SECRET, 200), ("wrong", 401), ("", 401), (None, 401)],
    ids=["right", "wrong", "empty-means-omitted", "missing"],
)
async def test_confidential_client_authenticates_with_client_secret_post(
    server: AuthorizationServer, secret: str | None, status: int
) -> None:
    code = authorize_code(server, "web", RFC7636_VERIFIER)
    form = code_form(code, "web", RFC7636_VERIFIER)
    if secret is not None:
        form.append(("client_secret", secret))
    result = await server.token(form)
    assert result.status == status
    if status == 401:
        assert result.body == {"error": "invalid_client"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_secret_basic_and_one_method_per_request(server: AuthorizationServer) -> None:
    basic = "Basic " + base64.b64encode(f"web:{WEB_SECRET}".encode()).decode()
    code = authorize_code(server, "web", RFC7636_VERIFIER)
    form = [("grant_type", "authorization_code"), ("code", code)]
    both = await server.token(
        [*form, ("code_verifier", RFC7636_VERIFIER), ("client_secret", WEB_SECRET)], basic
    )
    assert both.body == {"error": "invalid_request"}
    bad = "Basic " + base64.b64encode(b"web:wrong").decode()
    failed = await server.token([*form, ("code_verifier", RFC7636_VERIFIER)], bad)
    assert failed.status == 401
    assert failed.headers["WWW-Authenticate"] == "Basic"
    ok = await server.token([*form, ("code_verifier", RFC7636_VERIFIER)], basic)
    assert ok.status == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repeated_token_parameter_is_invalid_request(server: AuthorizationServer) -> None:
    code = authorize_code(server, "spa", RFC7636_VERIFIER)
    form = [*code_form(code, "spa", RFC7636_VERIFIER), ("code_verifier", RFC7636_VERIFIER)]
    assert (await server.token(form)).body == {"error": "invalid_request"}


# Client-side response validation (RFC 9207, RFC 9700 Section 4.4.2.1)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("location", "message"),
    [
        ("https://c/cb?code=c&state=s&iss=https%3A%2F%2Fevil.example", "issuer mismatch"),
        ("https://c/cb?code=c&state=s", "issuer mismatch"),
        ("https://c/cb?code=c&state=s&iss=https%3A%2F%2Fas.example.test%2F", "issuer mismatch"),
        ("https://c/cb?code=c&state=t&iss=https%3A%2F%2Fas.example.test", "state mismatch"),
        ("https://c/cb?error=access_denied&state=s&iss=https%3A%2F%2Fas.example.test", "denied"),
    ],
    ids=["other-issuer", "missing-iss", "trailing-slash-iss", "wrong-state", "error"],
)
def test_client_rejects_mixed_up_or_forged_responses(location: str, message: str) -> None:
    with pytest.raises(AuthorizationResponseError, match=message):
        validate_authorization_response(location, expected_state="s", expected_issuer=ISSUER)


# Resource server and metadata


@pytest.mark.unit
@pytest.mark.asyncio
async def test_access_token_in_query_string_is_ignored(
    server: AuthorizationServer, http: httpx.AsyncClient
) -> None:
    code = authorize_code(server, "spa", RFC7636_VERIFIER)
    token = (await server.token(code_form(code, "spa", RFC7636_VERIFIER))).body["access_token"]
    response = await http.get("/resource", params={"access_token": token})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_metadata_advertises_s256_only_and_iss_support(http: httpx.AsyncClient) -> None:
    metadata = (await http.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["issuer"] == ISSUER
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["response_types_supported"] == ["code"]
    assert metadata["grant_types_supported"] == ["authorization_code"]
    assert metadata["authorization_response_iss_parameter_supported"] is True
