"""Lab for the RFC 10017 BFF note: tokens stay server-side, the browser holds one cookie."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from api_labs.f14_api_authentication_and_identity.bff_token_mediation import (
    SESSION_COOKIE,
    TXN_COOKIE,
    BffSettings,
    BrowserCookie,
    EncryptedSessionStore,
    Lab,
    RequestContext,
    SimulatedBrowser,
    TokenSet,
    basic_client_auth,
    build_lab,
    cached_token_reusable,
    new_code_verifier,
    parse_set_cookie,
    prefix_rejection,
    s256_challenge,
    samesite_allows,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from starlette.requests import Request

CSRF = {"x-bff-csrf": "1"}
CLIENT_AUTH = basic_client_auth("spa-bff", "bff-secret-EXAMPLE-NOT-REAL")
CROSS_GET = RequestContext.CROSS_SITE_TOP_LEVEL_GET


@asynccontextmanager
async def lab_browser(
    settings: BffSettings | None = None,
) -> AsyncGenerator[tuple[Lab, SimulatedBrowser]]:
    lab = build_lab(settings)
    transport = httpx.ASGITransport(app=lab.bff.app)
    async with httpx.AsyncClient(transport=transport, base_url="https://bff.example") as client:
        yield lab, SimulatedBrowser(client)
    await lab.bff.upstream.aclose()


async def login(lab: Lab, browser: SimulatedBrowser, *, sub: str = "alice") -> httpx.Response:
    start = await browser.request("GET", "/bff/login?return_to=/orders")
    assert start.status_code == 302
    callback = lab.auth.authorize(start.headers["location"], sub=sub)
    return await browser.request("GET", callback, context=CROSS_GET)


def session_id(browser: SimulatedBrowser) -> str:
    return browser.jar[SESSION_COOKIE].value


def set_cookie_named(response: httpx.Response, name: str) -> str:
    return next(h for h in response.headers.get_list("set-cookie") if h.startswith(f"{name}="))


# --- PKCE and cookie rules ---


@pytest.mark.unit
def test_s256_challenge_matches_rfc_7636_appendix_b_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert s256_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


@pytest.mark.unit
def test_code_verifier_is_43_unreserved_characters() -> None:
    verifiers = {new_code_verifier() for _ in range(64)}
    assert len(verifiers) == 64
    assert all(re.fullmatch(r"[A-Za-z0-9._~-]{43}", v) for v in verifiers)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header", "accepted"),
    [
        # draft-ietf-httpbis-layered-cookies-02 Section 4.1.3.2 examples, verbatim
        ("__Host-SID=12345", False),
        ("__Host-SID=12345; Secure", False),
        ("__Host-SID=12345; Domain=site.example", False),
        ("__Host-SID=12345; Domain=site.example; Path=/", False),
        ("__Host-SID=12345; Secure; Domain=site.example; Path=/", False),
        ("__Host-SID=12345; Secure; Path=/", True),
        # Section 4.1.3.4: __Host-Http- additionally needs HttpOnly
        ("__Host-Http-SID=12345; Secure; Path=/", False),
        ("__Host-Http-SID=12345; Secure; Path=/; HttpOnly", True),
        ("__host-http-SID=12345; Secure; Path=/", False),  # prefix match is case-insensitive
        ("__Http-SID=12345; Secure", False),
        ("__Http-SID=12345; Secure; HttpOnly", True),
        ("__Secure-SID=12345; Domain=site.example", False),
    ],
)
def test_cookie_name_prefix_rules(header: str, accepted: bool) -> None:
    assert (prefix_rejection(header) is None) is accepted


@pytest.mark.unit
def test_samesite_strict_is_withheld_from_the_cross_site_callback_but_lax_is_sent() -> None:
    assert samesite_allows("lax", CROSS_GET)
    assert not samesite_allows("strict", CROSS_GET)
    assert not samesite_allows("lax", RequestContext.CROSS_SITE_TOP_LEVEL_POST)  # form_post
    assert not samesite_allows("lax", RequestContext.CROSS_SITE_SUBRESOURCE)
    assert samesite_allows("strict", RequestContext.SAME_SITE)
    assert samesite_allows("none", RequestContext.CROSS_SITE_SUBRESOURCE)


# --- Login ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_login_redirect_carries_s256_challenge_and_a_lax_host_http_txn_cookie() -> None:
    async with lab_browser() as (lab, browser):
        response = await browser.request("GET", "/bff/login")
        assert response.status_code == 302
        query = {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}
        assert query["code_challenge_method"] == "S256"
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", query["code_challenge"])
        assert query["redirect_uri"] == lab.bff.settings.redirect_uri
        _, _, attrs = parse_set_cookie(set_cookie_named(response, TXN_COOKIE))
        assert attrs["samesite"] == "lax"
        assert {"secure", "httponly", "path", "max-age"} <= attrs.keys()
        assert "domain" not in attrs
        assert browser.rejected == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_login_sets_a_strict_host_http_session_cookie_and_never_exposes_tokens() -> None:
    async with lab_browser() as (lab, browser):
        done = await login(lab, browser)
        assert done.status_code == 303
        assert done.headers["location"] == "/orders"
        _, _, attrs = parse_set_cookie(set_cookie_named(done, SESSION_COOKIE))
        assert attrs["samesite"] == "strict"
        assert attrs["path"] == "/"
        assert attrs["max-age"] == "28800"  # session lifetime == refresh token lifetime
        assert {"secure", "httponly"} <= attrs.keys()
        assert "domain" not in attrs
        assert TXN_COOKIE not in browser.jar  # transaction cookie deleted
        tokens = lab.bff.store.get(session_id(browser))
        assert tokens is not None
        check = await browser.request("GET", "/bff/session", headers=CSRF)
        assert check.json() == {"authenticated": True, "sub": "alice", "scope": tokens.scope}
        assert tokens.access_token not in check.text
        assert tokens.refresh_token not in check.text
        assert check.headers["cache-control"] == "no-store"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_strict_txn_cookie_breaks_the_cross_site_callback() -> None:
    async with lab_browser(BffSettings(txn_samesite="strict")) as (lab, browser):
        done = await login(lab, browser)
        assert done.status_code == 400
        assert done.json() == {"error": "no login transaction"}
        assert lab.upstreams.to("as.example") == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_state_mismatch_aborts_before_any_token_request() -> None:
    async with lab_browser() as (lab, browser):
        start = await browser.request("GET", "/bff/login")
        callback = lab.auth.authorize(start.headers["location"], sub="alice")
        forged = re.sub(r"state=[^&]+", "state=attacker-state", callback)
        response = await browser.request("GET", forged, context=CROSS_GET)
        assert response.status_code == 400
        assert response.json() == {"error": "state mismatch"}
        assert lab.upstreams.to("as.example") == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_iss_mismatch_rejects_a_mixed_up_authorization_response() -> None:
    async with lab_browser() as (lab, browser):
        start = await browser.request("GET", "/bff/login")
        location = start.headers["location"]
        callback = lab.auth.authorize(location, sub="alice", iss="https://evil.example")
        response = await browser.request("GET", callback, context=CROSS_GET)
        assert response.status_code == 400
        assert response.json() == {"error": "issuer mismatch"}
        assert lab.upstreams.to("as.example") == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/", "/\\evil.example"])
async def test_login_refuses_open_redirect_return_targets(target: str) -> None:
    async with lab_browser() as (_, browser):
        response = await browser.request("GET", f"/bff/login?return_to={quote(target, safe='')}")
        assert response.status_code == 400
        assert TXN_COOKIE not in browser.jar


@pytest.mark.unit
@pytest.mark.asyncio
async def test_login_mints_a_fresh_session_id_instead_of_adopting_a_planted_one() -> None:
    async with lab_browser() as (lab, browser):
        planted = "attacker-chosen-session-id"
        browser.jar[SESSION_COOKIE] = BrowserCookie(planted, "strict")
        await login(lab, browser)
        assert session_id(browser) != planted
        assert lab.bff.store.get(planted) is None
        assert lab.bff.store.get(session_id(browser)) is not None


# --- Proxying ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_proxy_swaps_the_cookie_for_a_bearer_token_and_drops_upstream_set_cookie() -> None:
    async with lab_browser() as (lab, browser):
        await login(lab, browser)
        response = await browser.request("GET", "/bff/api/orders/42?expand=lines", headers=CSRF)
        assert response.status_code == 200
        assert response.json() == {
            "sub": "alice",
            "method": "GET",
            "path": "/orders/42",
            "saw_cookie": False,
        }
        sent = lab.upstreams.to("api.example")[-1]
        tokens = lab.bff.store.get(session_id(browser))
        assert tokens is not None
        assert sent.headers["authorization"] == f"Bearer {tokens.access_token}"
        assert "cookie" not in sent.headers
        assert str(sent.url) == "https://api.example/orders/42?expand=lines"
        assert "set-cookie" not in response.headers
        assert "rs-tracker" not in browser.jar


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_csrf_header_is_rejected_before_any_upstream_call() -> None:
    async with lab_browser() as (lab, browser):
        await login(lab, browser)
        before = len(lab.upstreams.sent)
        response = await browser.request("POST", "/bff/api/orders", content=b"{}")
        assert response.status_code == 403
        assert len(lab.upstreams.sent) == before


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "admin",
        "orders/1@evil.example",
        "orders%2F..%2Fadmin",
        "orders/1%2F..%2F..%2Fadmin",
        "admin?next=orders",  # CVE-2025-54576 shape: the query must not satisfy the pattern
        "orders/1234567890123",  # 13 digits, over the {1,12} bound
    ],
)
async def test_unlisted_paths_are_never_forwarded(path: str) -> None:
    async with lab_browser() as (lab, browser):
        await login(lab, browser)
        before = len(lab.upstreams.sent)
        response = await browser.request("GET", f"/bff/api/{path}", headers=CSRF)
        assert response.status_code == 404
        assert len(lab.upstreams.sent) == before


@pytest.mark.unit
@pytest.mark.asyncio
async def test_method_outside_the_route_allowlist_is_405() -> None:
    async with lab_browser() as (lab, browser):
        await login(lab, browser)
        before = len(lab.upstreams.sent)
        response = await browser.request("DELETE", "/bff/api/orders", headers=CSRF)
        assert response.status_code == 405
        assert len(lab.upstreams.sent) == before


# --- Refresh ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expired_access_token_is_refreshed_inline_with_client_authentication() -> None:
    async with lab_browser() as (lab, browser):
        await login(lab, browser)
        old = lab.bff.store.get(session_id(browser))
        lab.clock.advance(600)
        response = await browser.request("GET", "/bff/api/orders", headers=CSRF)
        assert response.status_code == 200
        new = lab.bff.store.get(session_id(browser))
        assert old is not None
        assert new is not None
        assert new.access_token != old.access_token
        assert new.refresh_token != old.refresh_token  # rotated
        assert lab.auth.grants == ["authorization_code", "refresh_token"]
        refresh = next(r for r in lab.upstreams.to("as.example") if b"refresh_token" in r.content)
        assert refresh.headers["authorization"] == CLIENT_AUTH


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_calls_refresh_once_and_never_trip_reuse_detection() -> None:
    async with lab_browser() as (lab, browser):
        await login(lab, browser)
        lab.clock.advance(600)
        responses = await asyncio.gather(
            *(browser.request("GET", "/bff/api/orders", headers=CSRF) for _ in range(5))
        )
        assert [r.status_code for r in responses] == [200] * 5
        assert lab.auth.grants.count("refresh_token") == 1
        assert lab.auth.reuse_detected == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rotation_never_extends_the_absolute_refresh_lifetime() -> None:
    async with lab_browser() as (lab, browser):
        await login(lab, browser)
        first = lab.bff.store.get(session_id(browser))
        assert first is not None
        deadline = lab.auth.refresh_expires_at(first.refresh_token)
        lab.clock.advance(600)  # RFC 10017 Section 6.3.2.3 example: refresh after 10 minutes
        await browser.request("GET", "/bff/api/orders", headers=CSRF)
        second = lab.bff.store.get(session_id(browser))
        assert second is not None
        assert lab.auth.refresh_expires_at(second.refresh_token) == deadline
        assert deadline - lab.clock() == 7 * 3600 + 50 * 60  # 7 hours 50 minutes left


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_dead_refresh_token_ends_the_bff_session() -> None:
    settings = BffSettings(session_lifetime=10 * 3600)  # deliberately longer than the RT
    async with lab_browser(settings) as (lab, browser):
        await login(lab, browser)
        sid = session_id(browser)
        lab.clock.advance(8 * 3600)
        response = await browser.request("GET", "/bff/api/orders", headers=CSRF)
        assert response.status_code == 401
        assert lab.bff.store.get(sid) is None
        assert SESSION_COOKIE not in browser.jar  # the deletion passed the prefix check


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replaying_a_rotated_refresh_token_revokes_the_whole_family() -> None:
    async with lab_browser() as (lab, browser):
        await login(lab, browser)
        stolen = lab.bff.store.get(session_id(browser))
        assert stolen is not None
        lab.clock.advance(600)
        await browser.request("GET", "/bff/api/orders", headers=CSRF)  # legitimate rotation
        replay = await lab.bff.upstream.post(
            "https://as.example/token",
            content=urlencode(
                {"grant_type": "refresh_token", "refresh_token": stolen.refresh_token}
            ),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "authorization": CLIENT_AUTH,
            },
        )
        assert replay.json() == {"error": "invalid_grant"}
        assert lab.auth.reuse_detected == 1
        after = await browser.request("GET", "/bff/api/orders", headers=CSRF)
        assert after.status_code == 401  # the current access token died with its family


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authorization_server_requires_client_authentication_and_single_use_codes() -> None:
    async with lab_browser() as (lab, browser):
        start = await browser.request("GET", "/bff/login")
        query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
        callback = lab.auth.authorize(start.headers["location"], sub="alice")
        code = parse_qs(urlsplit(callback).query)["code"][0]
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": query["redirect_uri"],
            "code_verifier": new_code_verifier(),  # not the BFF's verifier
        }
        headers = {"content-type": "application/x-www-form-urlencoded"}
        no_auth = await lab.bff.upstream.post(
            "https://as.example/token", content=urlencode(form), headers=headers
        )
        assert no_auth.status_code == 401
        assert no_auth.json() == {"error": "invalid_client"}
        wrong_verifier = await lab.bff.upstream.post(
            "https://as.example/token",
            content=urlencode(form),
            headers={**headers, "authorization": CLIENT_AUTH},
        )
        assert wrong_verifier.json() == {"error": "invalid_grant"}
        done = await browser.request("GET", callback, context=CROSS_GET)
        assert done.status_code == 303
        replay = await browser.request("GET", callback, context=CROSS_GET)
        assert replay.status_code == 400  # txn consumed; the AS would also refuse the used code


# --- Storage and logout ---


@pytest.mark.unit
def test_store_rows_hold_no_plaintext_and_reject_rows_swapped_between_sessions() -> None:
    store = EncryptedSessionStore({"k1": AESGCM.generate_key(bit_length=256)}, "k1")
    alice = TokenSet("alice", "orders:read", "at-alice-PLAINTEXT", "rt-alice", 1.0, 2.0)
    bob = TokenSet("bob", "orders:read", "at-bob-PLAINTEXT", "rt-bob", 1.0, 2.0)
    store.put("sid-alice", alice)
    store.put("sid-bob", bob)
    dump = b"".join(store.rows.values()) + "".join(store.rows).encode()
    assert b"PLAINTEXT" not in dump
    assert b"sid-alice" not in dump
    assert store.get("sid-alice") == alice
    a, b = store.lookup_key("sid-alice"), store.lookup_key("sid-bob")
    store.rows[a], store.rows[b] = store.rows[b], store.rows[a]
    assert store.get("sid-alice") is None
    assert store.get("sid-bob") is None


@pytest.mark.unit
def test_store_key_rotation_reads_old_rows_and_writes_with_the_active_key() -> None:
    k1, k2 = AESGCM.generate_key(bit_length=256), AESGCM.generate_key(bit_length=256)
    old = EncryptedSessionStore({"k1": k1}, "k1")
    tokens = TokenSet("alice", "orders:read", "at", "rt", 1.0, 2.0)
    old.put("sid", tokens)
    rotated = EncryptedSessionStore({"k1": k1, "k2": k2}, "k2", rows=old.rows)
    assert rotated.get("sid") == tokens
    rotated.put("sid", tokens)
    assert rotated.kid_of("sid") == "k2"
    assert EncryptedSessionStore({"k2": k2}, "k2", rows=rotated.rows).get("sid") == tokens


@pytest.mark.unit
@pytest.mark.asyncio
async def test_logout_revokes_the_grant_and_deletes_the_cookie() -> None:
    async with lab_browser() as (lab, browser):
        await login(lab, browser)
        tokens = lab.bff.store.get(session_id(browser))
        assert tokens is not None
        response = await browser.request("POST", "/bff/logout", headers=CSRF)
        assert response.status_code == 204
        assert SESSION_COOKIE not in browser.jar
        assert browser.rejected == []
        assert lab.auth.access_valid(tokens.access_token) is None
        direct = await lab.bff.upstream.get(
            "https://api.example/orders", headers={"authorization": f"Bearer {tokens.access_token}"}
        )
        assert direct.status_code == 401


@pytest.mark.unit
def test_cached_token_is_reused_only_for_an_equal_scope_set() -> None:
    assert cached_token_reusable("orders:read orders:write", "orders:write orders:read")
    assert not cached_token_reusable("orders:read orders:write", "orders:read")  # superset
    assert not cached_token_reusable("orders:read", "orders:read orders:write")  # subset


# --- What the stock Starlette pieces do ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_session_middleware_signs_but_does_not_encrypt() -> None:
    async def put_token(request: Request) -> Response:
        request.session["access_token"] = "at-PLAINTEXT-123"  # noqa: S105 - lab value
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/", put_token)],
        middleware=[Middleware(SessionMiddleware, secret_key="lab-signing-key-EXAMPLE")],  # noqa: S106
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")
    header = response.headers["set-cookie"]
    name, value, attrs = parse_set_cookie(header)
    assert name == "session"
    payload = json.loads(base64.b64decode(value.split(".")[0]))
    assert payload == {"access_token": "at-PLAINTEXT-123"}  # readable by anyone holding it
    assert "secure" not in attrs  # https_only defaults to False
    assert attrs["samesite"] == "lax"
    assert attrs["max-age"] == "1209600"  # 14 days


@pytest.mark.unit
def test_starlette_delete_cookie_defaults_fail_the_host_http_prefix() -> None:
    response = Response()
    response.delete_cookie(SESSION_COOKIE)
    header = response.headers["set-cookie"]
    assert prefix_rejection(header) is not None  # the browser keeps the old cookie
    fixed = Response()
    fixed.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    assert prefix_rejection(fixed.headers["set-cookie"]) is None


@pytest.mark.unit
def test_starlette_partitioned_cookie_needs_python_3_14() -> None:
    response = Response()
    if sys.version_info < (3, 14):
        with pytest.raises(ValueError, match="Partitioned cookies"):
            response.set_cookie("c", "v", secure=True, partitioned=True)
    else:
        response.set_cookie("c", "v", secure=True, partitioned=True)
        assert "Partitioned" in response.headers["set-cookie"]
