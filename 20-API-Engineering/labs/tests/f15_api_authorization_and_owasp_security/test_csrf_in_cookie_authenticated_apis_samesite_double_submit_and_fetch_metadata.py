"""Lab: CSRF in cookie-authenticated APIs (SameSite, Fetch Metadata, Origin, signed tokens)."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from api_labs.f15_api_authorization_and_owasp_security.csrf_guard import (
    CSRF_HEADER,
    SESSION_COOKIE,
    WEBSOCKET_POLICY_VIOLATION,
    Bank,
    BrowserRequest,
    CrossOriginGuardMiddleware,
    CrossOriginPolicy,
    FetchSite,
    Origin,
    SameSite,
    Verdict,
    build_bank_app,
    cookie_is_sent,
    issue_csrf_token,
    naive_double_submit_ok,
    registrable_domain,
    same_site,
    sec_fetch_site,
    verify_csrf_token,
)

APP = "https://bank.example"
EVIL = "https://evil.example"
SIBLING = "https://blog.bank.example"
SECRET = b"lab-secret-EXAMPLE-NOT-A-REAL-KEY"
TRANSFER = json.dumps({"to": "mallory", "amount": 100})
# HMAC-SHA256 over len|sid|len|nonce with a zero nonce; fixed so a format change fails loudly.
GOLDEN_TOKEN = "AAAAAAAAAAAAAAAAAAAAAA.sM0YZvtwHVCHA9sA_AXtbh-ohazhnNfauV67WrSwEGo"  # noqa: S105


# --- Browser models -------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("api.bank.example", "bank.example"),
        ("a.b.c.bank.co.uk", "bank.co.uk"),
        ("alice.github.io", "alice.github.io"),
        ("github.io", "github.io"),
        ("127.0.0.1", "127.0.0.1"),
    ],
)
def test_registrable_domain_uses_longest_public_suffix(host: str, expected: str) -> None:
    assert registrable_domain(host) == expected


@pytest.mark.unit
def test_same_site_is_wider_than_same_origin_and_schemeful() -> None:
    api, blog = Origin.parse("https://api.bank.example"), Origin.parse(SIBLING)
    assert api != blog
    assert same_site(api, blog)
    assert not same_site(
        Origin.parse("https://alice.github.io"), Origin.parse("https://bob.github.io")
    )
    plain = Origin.parse("http://api.bank.example")
    assert not same_site(api, plain)
    assert same_site(api, plain, schemeful=False)


@pytest.mark.unit
def test_origin_serialization_drops_default_port_only() -> None:
    assert Origin.parse("https://Bank.Example:443/x?y").serialize() == APP
    assert Origin.parse("http://localhost:8000").serialize() == "http://localhost:8000"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("initiator", "urls", "expected"),
    [
        (APP, [f"{APP}/cookie/transfer"], FetchSite.SAME_ORIGIN),
        (SIBLING, [f"{APP}/cookie/transfer"], FetchSite.SAME_SITE),
        (EVIL, [f"{APP}/cookie/transfer"], FetchSite.CROSS_SITE),
        (APP, ["http://bank.example/cookie/transfer"], FetchSite.CROSS_SITE),
        # A redirect through a cross-site hop taints the chain even when it ends home.
        (APP, [f"{EVIL}/bounce", f"{APP}/cookie/transfer"], FetchSite.CROSS_SITE),
        (APP, [f"{SIBLING}/r", f"{APP}/cookie/transfer"], FetchSite.SAME_SITE),
        (None, [f"{APP}/"], FetchSite.NONE),
    ],
)
def test_sec_fetch_site_follows_the_whole_redirect_chain(
    initiator: str | None, urls: list[str], expected: FetchSite
) -> None:
    origin = None if initiator is None else Origin.parse(initiator)
    assert sec_fetch_site(origin, urls) is expected


CROSS_SITE_POST_NAV = BrowserRequest("POST", False, top_level_navigation=True)
CROSS_SITE_GET_NAV = BrowserRequest("GET", False, top_level_navigation=True)
CROSS_SITE_FETCH = BrowserRequest("POST", False, top_level_navigation=False)
SIBLING_FETCH = BrowserRequest("POST", True, top_level_navigation=False)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("policy", "request_", "age_s", "expected"),
    [
        (SameSite.STRICT, CROSS_SITE_GET_NAV, 3600.0, False),
        (SameSite.LAX, CROSS_SITE_GET_NAV, 3600.0, True),
        (SameSite.LAX, CROSS_SITE_POST_NAV, 30.0, False),
        (SameSite.LAX, CROSS_SITE_FETCH, 3600.0, False),
        (SameSite.DEFAULT, CROSS_SITE_POST_NAV, 120.0, True),
        (SameSite.DEFAULT, CROSS_SITE_POST_NAV, 121.0, False),
        (SameSite.DEFAULT, CROSS_SITE_FETCH, 30.0, False),
        (SameSite.NONE, CROSS_SITE_FETCH, 3600.0, True),
        # The limit SameSite cannot fix: a compromised sibling subdomain is same-site.
        (SameSite.STRICT, SIBLING_FETCH, 3600.0, True),
    ],
)
def test_samesite_cookie_retrieval_matrix(
    policy: SameSite, request_: BrowserRequest, age_s: float, expected: bool
) -> None:
    assert cookie_is_sent(policy, request_, cookie_age_s=age_s) is expected


@pytest.mark.unit
def test_legacy_browser_sends_attributeless_cookie_on_cross_site_fetch() -> None:
    assert not cookie_is_sent(SameSite.DEFAULT, CROSS_SITE_FETCH)
    assert cookie_is_sent(SameSite.DEFAULT, CROSS_SITE_FETCH, lax_by_default=False)


# --- The Fetch Metadata / Origin policy -------------------------------------------------


def _h(**headers: str) -> dict[str, str]:
    return {"host": "bank.example"} | {k.replace("_", "-"): v for k, v in headers.items()}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "headers", "expected"),
    [
        ("GET", _h(sec_fetch_site="cross-site"), Verdict.ALLOW_SAFE_METHOD),
        ("TRACE", _h(sec_fetch_site="cross-site"), Verdict.DENY_SEC_FETCH_SITE),
        ("POST", _h(sec_fetch_site="same-origin"), Verdict.ALLOW_FETCH_METADATA),
        ("POST", _h(sec_fetch_site="none"), Verdict.ALLOW_FETCH_METADATA),
        ("POST", _h(sec_fetch_site="same-site", origin=SIBLING), Verdict.DENY_SEC_FETCH_SITE),
        ("DELETE", _h(sec_fetch_site="cross-site", origin=EVIL), Verdict.DENY_SEC_FETCH_SITE),
        # Sec-Fetch-Site wins over a matching Origin: it carries the redirect taint.
        ("POST", _h(sec_fetch_site="cross-site", origin=APP), Verdict.DENY_SEC_FETCH_SITE),
        ("POST", _h(sec_fetch_site="Same-Origin"), Verdict.DENY_SEC_FETCH_SITE),
        ("POST", _h(origin=APP), Verdict.ALLOW_ORIGIN_MATCHES_HOST),
        ("POST", _h(origin=EVIL), Verdict.DENY_ORIGIN_MISMATCH),
        ("POST", _h(origin="null"), Verdict.DENY_ORIGIN_MISMATCH),
        ("POST", _h(origin="https://bank.example.evil.example"), Verdict.DENY_ORIGIN_MISMATCH),
        ("POST", _h(), Verdict.ALLOW_NON_BROWSER),
    ],
)
def test_default_policy_decisions(method: str, headers: dict[str, str], expected: Verdict) -> None:
    assert CrossOriginPolicy().decide(method, "/cookie/transfer", headers) is expected


@pytest.mark.unit
def test_policy_knobs_same_site_trusted_origin_and_strict_mode() -> None:
    sibling = _h(sec_fetch_site="same-site", origin=SIBLING)
    assert CrossOriginPolicy(allow_same_site=True).decide("POST", "/", sibling).allowed
    partner = _h(sec_fetch_site="cross-site", origin="https://partner.example")
    trusted = CrossOriginPolicy(trusted_origins=frozenset({"https://partner.example"}))
    assert trusted.decide("POST", "/", partner) is Verdict.ALLOW_TRUSTED_ORIGIN
    evil = _h(sec_fetch_site="cross-site", origin=EVIL)
    assert trusted.decide("POST", "/", evil) is Verdict.DENY_SEC_FETCH_SITE
    strict = CrossOriginPolicy(require_browser_headers=True)
    assert strict.decide("POST", "/", _h()) is Verdict.DENY_NO_BROWSER_HEADERS


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "allowed"),
    [("/webhooks/stripe", True), ("/webhooks/stripe/", False), ("/webhooks/stripe2", False)],
)
def test_bypass_paths_match_exactly(path: str, allowed: bool) -> None:
    policy = CrossOriginPolicy(bypass_paths=frozenset({"/webhooks/stripe"}))
    assert policy.decide("POST", path, _h(sec_fetch_site="cross-site")).allowed is allowed


# --- End to end through ASGI: the attack and the defenses ------------------------------


def _bank() -> Bank:
    return Bank(
        sessions={"sid-alice": "alice"},
        bearer_tokens={"tok-alice": "alice"},
        balances={"alice": 1000, "mallory": 0},
    )


def _client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=APP)


# What a browser sends for fetch(url, {method: "POST", mode: "no-cors", credentials:
# "include", body: jsonString}) from https://evil.example when the session cookie is
# SameSite=None: a string body gets text/plain;charset=UTF-8 (Fetch Section 5.2), a
# CORS-safelisted Content-Type, so there is no preflight.
FORGED = {
    "cookie": f"{SESSION_COOKIE}=sid-alice",
    "origin": EVIL,
    "sec-fetch-site": "cross-site",
    "sec-fetch-mode": "no-cors",
    "content-type": "text/plain;charset=UTF-8",
}
JSON_CT = {"content-type": "application/json"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unguarded_lenient_json_api_executes_forged_request() -> None:
    bank = _bank()
    async with _client(build_bank_app(bank, lenient_json=True)) as client:
        response = await client.post("/cookie/transfer", content=TRANSFER, headers=FORGED)
    assert response.status_code == 200
    assert bank.balances == {"alice": 900, "mallory": 100}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_guard_rejects_forged_request_and_state_is_unchanged() -> None:
    bank = _bank()
    app = build_bank_app(bank, policy=CrossOriginPolicy(), lenient_json=True)
    async with _client(app) as client:
        response = await client.post("/cookie/transfer", content=TRANSFER, headers=FORGED)
    assert response.status_code == 403
    assert response.json() == {
        "detail": "cross-origin request rejected",
        "reason": "deny:sec-fetch-site",
    }
    assert bank.balances == {"alice": 1000, "mallory": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_guard_admits_same_origin_spa_request() -> None:
    bank = _bank()
    same_origin = {"origin": APP, "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors"}
    headers = FORGED | same_origin | JSON_CT
    async with _client(build_bank_app(bank, policy=CrossOriginPolicy())) as client:
        response = await client.post("/cookie/transfer", content=TRANSFER, headers=headers)
    assert response.status_code == 200
    assert bank.balances["mallory"] == 100


@pytest.mark.unit
@pytest.mark.asyncio
async def test_old_browser_fallback_compares_origin_with_host() -> None:
    bank = _bank()
    old_browser = {k: v for k, v in FORGED.items() if not k.startswith("sec-fetch")}
    app = build_bank_app(bank, policy=CrossOriginPolicy(), lenient_json=True)
    async with _client(app) as client:
        forged = await client.post("/cookie/transfer", content=TRANSFER, headers=old_browser)
        legit = await client.post(
            "/cookie/transfer", content=TRANSFER, headers=old_browser | {"origin": APP}
        )
    assert (forged.status_code, legit.status_code) == (403, 200)
    assert forged.json()["reason"] == "deny:origin-mismatch"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_text_plain_simple_request_is_refused_by_media_type_check() -> None:
    bank = _bank()
    async with _client(build_bank_app(bank)) as client:
        response = await client.post("/cookie/transfer", content=TRANSFER, headers=FORGED)
    assert response.status_code == 415
    assert bank.balances["alice"] == 1000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bearer_api_ignores_ambient_cookie() -> None:
    bank = _bank()
    async with _client(build_bank_app(bank)) as client:
        forged = await client.post("/bearer/transfer", content=TRANSFER, headers=FORGED)
        legit = await client.post(
            "/bearer/transfer",
            content=TRANSFER,
            headers={"authorization": "Bearer tok-alice", "content-type": "application/json"},
        )
    assert forged.status_code == 401
    assert forged.headers["www-authenticate"] == "Bearer"
    assert legit.status_code == 200


# --- Signed double-submit tokens -------------------------------------------------------


@pytest.mark.unit
def test_signed_token_is_bound_to_session_and_tamper_evident() -> None:
    token = issue_csrf_token(SECRET, "sid-alice", nonce=bytes(16))
    assert token == GOLDEN_TOKEN
    assert verify_csrf_token(SECRET, "sid-alice", token)
    assert not verify_csrf_token(SECRET, "sid-mallory", token)
    assert not verify_csrf_token(b"other-secret", "sid-alice", token)
    nonce, _, mac = token.partition(".")
    assert not verify_csrf_token(SECRET, "sid-alice", f"B{nonce[1:]}.{mac}")
    assert not verify_csrf_token(SECRET, "sid-alice", nonce)
    assert not verify_csrf_token(SECRET, "sid-alice", "!!.??")
    assert not verify_csrf_token(SECRET, "sid-alice", f"AAAA.{mac}")


@pytest.mark.unit
def test_cookie_tossing_beats_naive_double_submit_but_not_signed_token() -> None:
    # A sibling subdomain sets csrf=attacker for the parent domain and sends the same value
    # in the header; a naive cookie == header check cannot tell.
    assert naive_double_submit_ok("attacker", "attacker")
    assert not verify_csrf_token(SECRET, "sid-alice", "attacker")
    mallory_token = issue_csrf_token(SECRET, "sid-mallory")
    assert not verify_csrf_token(SECRET, "sid-alice", mallory_token)
    assert not naive_double_submit_ok(None, "x")
    assert not naive_double_submit_ok("x", "y")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_token_mode_rejects_missing_and_foreign_tokens() -> None:
    bank = _bank()
    good = issue_csrf_token(SECRET, "sid-alice")
    foreign = issue_csrf_token(SECRET, "sid-mallory")
    async with _client(build_bank_app(bank, csrf_secret=SECRET, lenient_json=True)) as client:
        codes = [
            (
                await client.post("/cookie/transfer", content=TRANSFER, headers=FORGED | extra)
            ).status_code
            for extra in ({}, {CSRF_HEADER: foreign}, {CSRF_HEADER: good})
        ]
    assert codes == [403, 403, 200]


# --- Library defaults that decide exposure ---------------------------------------------


class Item(BaseModel):
    name: str


def _fastapi_app(*, strict: bool) -> FastAPI:
    app = FastAPI(strict_content_type=strict)

    @app.post("/items")
    async def create(item: Item) -> Item:  # pyright: ignore[reportUnusedFunction]
        return item

    return app


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strict", "content_type", "expected"),
    [
        (True, "text/plain", 422),
        (True, None, 422),
        (False, None, 200),
        (False, "text/plain", 422),
        (True, "application/json", 200),
        (True, "application/merge-patch+json", 200),
    ],
)
async def test_fastapi_parses_json_only_for_json_media_types(
    strict: bool, content_type: str | None, expected: int
) -> None:
    headers = {} if content_type is None else {"content-type": content_type}
    transport = httpx.ASGITransport(app=_fastapi_app(strict=strict))
    async with httpx.AsyncClient(transport=transport, base_url=APP) as client:
        response = await client.post("/items", content=b'{"name": "x"}', headers=headers)
    assert response.request.headers.get("content-type") == content_type
    assert response.status_code == expected


@pytest.mark.unit
def test_starlette_set_cookie_defaults_to_samesite_lax() -> None:
    response = Response()
    response.set_cookie(SESSION_COOKIE, "sid-alice", secure=True, httponly=True)
    assert response.headers["set-cookie"] == (
        "__Host-sid=sid-alice; HttpOnly; Path=/; SameSite=lax; Secure"
    )


# --- WebSocket handshakes (cross-site WebSocket hijacking) -----------------------------


async def _echo(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_text("ok")
    await websocket.close()


def _ws_app() -> Starlette:
    return Starlette(
        routes=[WebSocketRoute("/ws", _echo)],
        middleware=[Middleware(CrossOriginGuardMiddleware, policy=CrossOriginPolicy())],
    )


@pytest.mark.unit
def test_cross_site_websocket_handshake_is_closed_with_1008() -> None:
    with TestClient(_ws_app(), base_url="http://testserver") as client:
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/ws", headers={"origin": EVIL}),
        ):
            pass
        assert excinfo.value.code == WEBSOCKET_POLICY_VIOLATION
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            assert ws.receive_text() == "ok"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_guard_passes_lifespan_and_unrelated_routes() -> None:
    async def hello(_: object) -> PlainTextResponse:
        return PlainTextResponse("hi")

    app = Starlette(
        routes=[Route("/hello", hello, methods=["GET"])],
        middleware=[Middleware(CrossOriginGuardMiddleware)],
    )
    async with _client(app) as client:
        response = await client.get("/hello", headers={"sec-fetch-site": "cross-site"})
    assert response.text == "hi"
