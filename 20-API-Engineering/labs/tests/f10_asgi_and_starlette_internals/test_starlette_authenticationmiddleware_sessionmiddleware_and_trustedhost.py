"""Lab: Starlette Authentication, signed-cookie Session, HTTPSRedirect, and TrustedHost middlewares.

Pinned: starlette 1.10.0, itsdangerous 2.2.0, uvicorn 0.54.0, httpx 0.28.1.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from starlette.exceptions import HTTPException
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from api_labs.f10_asgi_and_starlette_internals.starlette_security_middlewares import (
    DEFAULT_MAX_AGE,
    Clock,
    ClockedSessionMiddleware,
    SessionOrBearerBackend,
    build_app,
    build_http_exception_backend_app,
    build_misordered_app,
    decode_session_cookie,
    drive_asgi,
    json_401_on_error,
    ok_app,
    sign_session_cookie,
    signed_cookie_value_length,
    trusted_host_status,
    websocket_scope,
    websocket_session_writer,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp

T0 = 1_700_000_000
KEY = "lab-secret-EXAMPLE-NOT-REAL"
ALICE = {"user": "alice", "scopes": ["authenticated"]}


def client_for(app: ASGIApp, base_url: str = "http://test") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


def session_cookie(value: str) -> dict[str, str]:
    """An explicit `Cookie` header; per-request `cookies=` is deprecated in httpx 0.28.1."""
    return {"Cookie": f"session={value}"}


# --- SessionMiddleware: wire format and integrity -------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cookie_is_byte_identical_to_an_independent_hmac_sha1_rebuild() -> None:
    app = build_app(secret_keys=[KEY], clock=Clock(T0))
    async with client_for(app) as client:
        response = await client.post("/login")
    value = response.cookies["session"]
    assert value == sign_session_cookie(ALICE, KEY, T0)
    assert response.headers["set-cookie"] == (
        f"session={value}; path=/; Max-Age={DEFAULT_MAX_AGE}; httponly; samesite=lax"
    )
    assert response.headers["vary"] == "Cookie"


@pytest.mark.unit
def test_payload_is_readable_without_the_key() -> None:
    decoded = decode_session_cookie(sign_session_cookie(ALICE, KEY, T0))
    assert decoded.session == ALICE
    assert decoded.timestamp == T0
    assert len(decoded.signature) == 20  # HMAC-SHA1 digest size


@pytest.mark.unit
@pytest.mark.parametrize("pad", [0, 1, 2, 3, 91, 2994])
def test_cookie_value_length_is_4_ceil_n_over_3_plus_35(pad: int) -> None:
    session = {"d": "x" * pad}
    n = len(json.dumps(session))  # 9 + pad bytes; pad 2994 -> 3003, the 4096-byte ceiling
    value = sign_session_cookie(session, KEY, T0)
    assert len(value) == signed_cookie_value_length(n, T0) == 4 * -(-n // 3) + 35


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tampered_payload_silently_becomes_an_empty_session() -> None:
    app = build_app(secret_keys=[KEY], clock=Clock(T0))
    _, stamp, signature = sign_session_cookie(ALICE, KEY, T0).split(".")
    evil = base64.b64encode(json.dumps({"user": "mallory", "scopes": ["authenticated"]}).encode())
    forged = f"{evil.decode()}.{stamp}.{signature}"
    async with client_for(app) as client:
        response = await client.get("/me", headers=session_cookie(forged))
    assert response.status_code == 403
    assert "set-cookie" not in response.headers  # no error, no cookie rewrite: just anonymous


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anyone_with_the_secret_key_mints_any_identity() -> None:
    app = build_app(secret_keys=[KEY], clock=Clock(T0))
    minted = sign_session_cookie({"user": "mallory", "scopes": ["authenticated"]}, KEY, T0)
    async with client_for(app) as client:
        response = await client.get("/me", headers=session_cookie(minted))
    assert (response.status_code, response.text) == (200, "mallory")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("age", "status"),
    [(0, 200), (DEFAULT_MAX_AGE, 200), (DEFAULT_MAX_AGE + 1, 403), (-1, 403)],
)
async def test_server_side_max_age_rejects_old_and_future_timestamps(age: int, status: int) -> None:
    clock = Clock(T0 + age)
    app = build_app(secret_keys=[KEY], clock=clock)
    async with client_for(app) as client:
        response = await client.get(
            "/me", headers=session_cookie(sign_session_cookie(ALICE, KEY, T0))
        )
    assert response.status_code == status


# --- SessionMiddleware: lifecycle semantics ------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_logout_cookie_does_not_revoke_a_captured_cookie() -> None:
    app = build_app(secret_keys=[KEY], clock=Clock(T0))
    async with client_for(app) as client:
        captured = (await client.post("/login")).cookies["session"]
        logout = await client.post("/logout")
        assert logout.headers["set-cookie"] == (
            "session=null; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT; httponly; samesite=lax"
        )
        client.cookies.clear()
        replay = await client.get("/me", headers=session_cookie(captured))
    assert (replay.status_code, replay.text) == (200, "alice")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reads_never_refresh_so_expiry_is_absolute_from_the_last_write() -> None:
    clock = Clock(T0)
    app = build_app(secret_keys=[KEY], clock=clock)
    async with client_for(app) as client:
        await client.post("/login")
        clock.now = T0 + DEFAULT_MAX_AGE - 10
        read = await client.get("/me")
        assert read.status_code == 200
        assert "set-cookie" not in read.headers  # active user, but no re-signing
        clock.now = T0 + DEFAULT_MAX_AGE + 1
        expired = await client.get("/me")
    assert expired.status_code == 403


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nested_mutation_is_lost_and_reassignment_persists() -> None:
    app = build_app(secret_keys=[KEY], clock=Clock(T0))
    async with client_for(app) as client:
        await client.post("/cart/init")
        in_place = await client.post("/cart/append-in-place")
        assert in_place.json() == ["book"]
        assert "set-cookie" not in in_place.headers
        assert decode_session_cookie(client.cookies["session"]).session["cart"] == []
        reassigned = await client.post("/cart/reassign")
        assert "set-cookie" in reassigned.headers
    assert decode_session_cookie(reassigned.cookies["session"]).session["cart"] == ["book"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_key_rotation_verifies_old_keys_and_signs_with_the_newest() -> None:
    old_cookie = sign_session_cookie(ALICE, "old-key-EXAMPLE", T0)
    rotating = build_app(secret_keys=["old-key-EXAMPLE", KEY], clock=Clock(T0))
    new_only = build_app(secret_keys=[KEY], clock=Clock(T0))
    async with client_for(rotating) as client:
        assert (await client.get("/me", headers=session_cookie(old_cookie))).status_code == 200
        client.cookies.clear()
        fresh = (await client.post("/login")).cookies["session"]
    assert fresh == sign_session_cookie(ALICE, KEY, T0)
    async with client_for(new_only) as client:
        assert (await client.get("/me", headers=session_cookie(old_cookie))).status_code == 403


@pytest.mark.unit
@pytest.mark.asyncio
async def test_vary_cookie_follows_session_access_not_session_presence() -> None:
    no_auth = build_app(secret_keys=[KEY], clock=Clock(T0), authenticate=False)
    with_auth = build_app(secret_keys=[KEY], clock=Clock(T0))
    async with client_for(no_auth) as client:
        assert "vary" not in (await client.get("/public")).headers
        peek = await client.get("/peek")
        assert (peek.headers["vary"], "set-cookie" in peek.headers) == ("Cookie", False)
    async with client_for(with_auth) as client:
        # The session-reading backend touches the session on every request.
        assert (await client.get("/public")).headers["vary"] == "Cookie"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_https_only_adds_secure_and_nothing_else_does() -> None:
    plain = build_app(secret_keys=[KEY], clock=Clock(T0))
    secure = build_app(secret_keys=[KEY], clock=Clock(T0), https_only=True)
    async with client_for(plain) as client:
        assert "secure" not in (await client.post("/login")).headers["set-cookie"]
    async with client_for(secure) as client:
        assert (
            (await client.post("/login"))
            .headers["set-cookie"]
            .endswith("; httponly; samesite=lax; secure")
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_websocket_session_writes_are_never_persisted() -> None:
    guard = ClockedSessionMiddleware(websocket_session_writer, secret_keys=[KEY], clock=Clock(T0))
    scope = websocket_scope("/ws", [(b"host", b"example.com")])
    sent = await drive_asgi(guard, scope, [{"type": "websocket.connect"}])
    assert scope["session"].modified is True
    assert sent == [
        {"type": "websocket.accept", "headers": []},
        {"type": "websocket.close", "code": 1000},
    ]


# --- AuthenticationMiddleware --------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_on_error_is_400_with_the_exception_text() -> None:
    app = build_app(secret_keys=[KEY], clock=Clock(T0))
    async with client_for(app) as client:
        response = await client.get("/me", headers={"Authorization": "Bearer nope"})
    assert (response.status_code, response.text) == (400, "invalid bearer token")
    assert "www-authenticate" not in response.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_custom_on_error_returns_401_with_a_challenge() -> None:
    app = build_app(secret_keys=[KEY], clock=Clock(T0), on_error=json_401_on_error)
    async with client_for(app) as client:
        response = await client.get("/me", headers={"Authorization": "Bearer nope"})
        bearer = await client.get(
            "/me", headers={"Authorization": "Bearer demo-token-EXAMPLE-NOT-REAL"}
        )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "invalid credentials"}
    assert (bearer.status_code, bearer.text) == (200, "carol")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_requires_is_403_or_303_with_a_next_parameter() -> None:
    app = build_app(secret_keys=[KEY], clock=Clock(T0))
    async with client_for(app) as client:
        forbidden = await client.get("/me")
        redirect = await client.get("/dashboard")
    assert forbidden.status_code == 403
    assert redirect.status_code == 303
    assert (
        redirect.headers["location"] == "http://test/login-page?next=http%3A%2F%2Ftest%2Fdashboard"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_websocket_auth_error_closes_with_1000_not_1008() -> None:
    guard = AuthenticationMiddleware(ok_app, backend=SessionOrBearerBackend())
    scope = websocket_scope("/ws", [(b"host", b"example.com"), (b"authorization", b"Basic x")])
    sent = await drive_asgi(guard, scope, [{"type": "websocket.connect"}])
    assert sent == [{"type": "websocket.close", "code": 1000}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_auth_outside_session_fails_the_session_assertion() -> None:
    app = build_misordered_app(secret_keys=[KEY], clock=Clock(T0))
    async with client_for(app) as client:
        with pytest.raises(AssertionError, match="SessionMiddleware must be installed"):
            await client.get("/public")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_exception_in_a_backend_is_a_500_not_a_401() -> None:
    app = build_http_exception_backend_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/public")
    assert (response.status_code, response.text) == (500, "Internal Server Error")
    async with client_for(app) as client:
        with pytest.raises(HTTPException):
            await client.get("/public")


# --- TrustedHostMiddleware -----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "status"),
    [
        ("example.com", 200),
        ("EXAMPLE.com", 200),  # lowercased since starlette 1.9.0
        ("api.example.com", 200),
        ("example.com:8443", 200),  # the port is never compared
        ("evil.com", 400),
        ("evilexample.com", 400),
        ("example.com.evil.com", 400),
        ("example.com/abc?x=", 400),  # GHSA-86qp-5c8j-p5mr shape
        ("example.com.", 400),  # trailing-dot FQDN is not normalized
        ("a.example.com.", 400),
        ("[::1]", 400),
        (None, 400),
    ],
)
async def test_trusted_host_matrix(host: str | None, status: int) -> None:
    assert await trusted_host_status(["example.com", "*.example.com"], host) == status


@pytest.mark.unit
@pytest.mark.asyncio
async def test_host_header_poisons_url_for_unless_trusted_host_runs() -> None:
    open_app = build_app(secret_keys=[KEY], clock=Clock(T0), authenticate=False)
    guarded = build_app(
        secret_keys=[KEY], clock=Clock(T0), authenticate=False, allowed_hosts=["example.com"]
    )
    poisoned = {"Host": "evil.example"}
    async with client_for(open_app) as client:
        assert (await client.get("/reset-link", headers=poisoned)).text == (
            "http://evil.example/reset/c-EXAMPLE"
        )
    async with client_for(guarded) as client:
        response = await client.get("/reset-link", headers=poisoned)
    assert (response.status_code, response.text) == (400, "Invalid host header")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_www_redirect_is_307_and_keeps_port_path_and_query() -> None:
    app = build_app(
        secret_keys=[KEY], clock=Clock(T0), authenticate=False, allowed_hosts=["www.example.com"]
    )
    async with client_for(app, "http://example.com:8000") as client:
        response = await client.get("/public?q=1")
    assert response.status_code == 307
    assert response.headers["location"] == "http://www.example.com:8000/public?q=1"


@pytest.mark.unit
def test_malformed_wildcard_is_an_assert_so_python_O_turns_it_into_a_bypass() -> None:
    with pytest.raises(AssertionError, match=r"must be like '\*\.example\.com'"):
        TrustedHostMiddleware(ok_app, allowed_hosts=["*example.com"])
    probe = (
        "import asyncio\n"
        "from api_labs.f10_asgi_and_starlette_internals.starlette_security_middlewares"
        " import trusted_host_status\n"
        "print(asyncio.run(trusted_host_status(['*example.com'], 'evilexample.com')))\n"
    )
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-O", "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "200"


# --- HTTPSRedirectMiddleware ---------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "location"),
    [
        ("http://test", "https://test/login?a=1"),
        ("http://test:80", "https://test/login?a=1"),
        ("http://test:8080", "https://test:8080/login?a=1"),  # port kept, scheme swapped
    ],
)
async def test_https_redirect_307_strips_only_80_and_443(base_url: str, location: str) -> None:
    app = build_app(secret_keys=[KEY], clock=Clock(T0), authenticate=False, https_redirect=True)
    async with client_for(app, base_url) as client:
        response = await client.post("/login?a=1")
    assert (response.status_code, response.headers["location"]) == (307, location)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_behind_a_tls_proxy_only_trusted_x_forwarded_proto_stops_the_loop() -> None:
    app = build_app(secret_keys=[KEY], clock=Clock(T0), authenticate=False, https_redirect=True)
    # uvicorn types its ASGI callables with asgiref-style aliases that mypy and pyright
    # do not treat as Starlette's; the runtime contract is the same.
    proxied = cast("ASGIApp", ProxyHeadersMiddleware(cast("Any", app), trusted_hosts="127.0.0.1"))
    xfp = {"X-Forwarded-Proto": "https"}
    async with client_for(app) as client:
        assert (await client.get("/public", headers=xfp)).status_code == 307
    async with client_for(proxied) as client:  # httpx default client address is 127.0.0.1
        assert (await client.get("/public", headers=xfp)).status_code == 200
    spoofer = httpx.ASGITransport(app=proxied, client=("203.0.113.9", 4000))
    async with httpx.AsyncClient(transport=spoofer, base_url="http://test") as client:
        assert (await client.get("/public", headers=xfp)).status_code == 307


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ws_redirect_uses_the_websocket_denial_response_extension() -> None:
    guard = HTTPSRedirectMiddleware(ok_app)
    scope = websocket_scope("/ws", [(b"host", b"example.com")])
    sent = await drive_asgi(guard, scope, [{"type": "websocket.connect"}])
    assert sent[0]["type"] == "websocket.http.response.start"
    assert sent[0]["status"] == 307
    assert (b"location", b"wss://example.com/ws") in sent[0]["headers"]
