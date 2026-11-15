"""Lab: the OWASP API Security Top 10 (2023) reproduced and closed in one FastAPI reference API.

Every risk has one switchable control; the audit proves that each control closes its own risk,
only its own risk, and never breaks the benign request that exercises the same endpoint.
"""

import ipaddress
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

import httpx
import jwt
import pytest

from api_labs.f15_api_authorization_and_owasp_security.owasp_top10_threat_model import (
    ALL_CONTROLS,
    AUDIENCE,
    FAKE_IMDS_KEY,
    ISSUER,
    MAX_BODY_BYTES,
    MAX_PAGE,
    MAX_UPSTREAM_BYTES,
    NO_CONTROLS,
    THREAT_MODEL,
    FakeClock,
    Finding,
    Risk,
    Store,
    UnsafeTargetError,
    audit,
    bearer,
    build_app,
    forge_unsigned_token,
    guard_outbound_url,
    inventory_drift,
    is_public_address,
    mint_token,
    read_token_unverified,
    route_inventory,
    seed_store,
    verify_token,
)

OWASP_2023_TITLES = {
    Risk.API1: "Broken Object Level Authorization",
    Risk.API2: "Broken Authentication",
    Risk.API3: "Broken Object Property Level Authorization",
    Risk.API4: "Unrestricted Resource Consumption",
    Risk.API5: "Broken Function Level Authorization",
    Risk.API6: "Unrestricted Access to Sensitive Business Flows",
    Risk.API7: "Server Side Request Forgery",
    Risk.API8: "Security Misconfiguration",
    Risk.API9: "Improper Inventory Management",
    Risk.API10: "Unsafe Consumption of APIs",
}
ALICE = bearer(mint_token("u-alice"))
BOB = bearer(mint_token("u-bob"))
ROOT = bearer(mint_token("u-root"))


@asynccontextmanager
async def serve(
    store: Store, controls: frozenset[Risk] = ALL_CONTROLS
) -> AsyncGenerator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_app(store, controls), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------- the threat model as data


@pytest.mark.unit
def test_threat_model_has_exactly_the_ten_2023_risks_in_order() -> None:
    assert [row.risk for row in THREAT_MODEL] == list(Risk)
    assert {row.risk: row.title for row in THREAT_MODEL} == OWASP_2023_TITLES
    assert all(row.control and row.layer for row in THREAT_MODEL)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_with_no_controls_the_audit_exploits_all_ten() -> None:
    assert await audit(NO_CONTROLS) == dict.fromkeys(Risk, Finding.EXPLOITED)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_with_all_controls_every_attack_is_blocked_and_no_benign_call_breaks() -> None:
    assert await audit(ALL_CONTROLS) == dict.fromkeys(Risk, Finding.BLOCKED)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("missing", list(Risk), ids=[r.name for r in Risk])
async def test_each_control_closes_its_own_risk_and_no_other(missing: Risk) -> None:
    findings = await audit(ALL_CONTROLS - {missing})
    exploited = {risk for risk, finding in findings.items() if finding is Finding.EXPLOITED}
    assert exploited == {missing}
    assert Finding.BROKEN not in findings.values()


# ---------------------------------------------------------------- API1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api1_foreign_and_absent_ids_get_byte_identical_404s() -> None:
    async with serve(seed_store()) as client:
        foreign = await client.get("/v1/orders/1002", headers=ALICE)
        absent = await client.get("/v1/orders/9999", headers=ALICE)
        own = await client.get("/v1/orders/1002", headers=BOB)
    assert (foreign.status_code, absent.status_code, own.status_code) == (404, 404, 200)
    assert foreign.content == absent.content


# ---------------------------------------------------------------- API2


@pytest.mark.unit
def test_api2_pyjwt_verify_signature_false_also_skips_expiry_and_audience() -> None:
    stale = mint_token("u-alice", now=1_000_000_000, audience="some-other-api")
    assert read_token_unverified(stale) is not None
    assert verify_token(stale) is None
    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(stale, options={"verify_signature": False, "verify_exp": True})


@pytest.mark.unit
@pytest.mark.parametrize(
    "token",
    [
        forge_unsigned_token({"sub": "u-root", "aud": AUDIENCE, "iss": ISSUER, "exp": 4e9}),
        mint_token("u-root", key=b"attacker-chosen-key-that-is-32-bytes-long"),
        mint_token("u-root", audience="billing-api"),
        mint_token("u-root", now=1_000_000_000),
        jwt.encode({"sub": "u-root", "aud": AUDIENCE, "iss": ISSUER}, b"x" * 44, "HS256"),
    ],
    ids=["alg-none", "wrong-key", "wrong-aud", "expired", "no-exp-claim"],
)
def test_api2_verifier_rejects_each_classic_jwt_attack(token: str) -> None:
    assert verify_token(token) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api2_rejection_is_401_with_an_rfc_6750_challenge() -> None:
    async with serve(seed_store()) as client:
        missing = await client.get("/v1/users/me")
        forged = await client.get(
            "/v1/users/me", headers=bearer(forge_unsigned_token({"sub": "u-root"}))
        )
    assert missing.status_code == forged.status_code == 401
    assert missing.headers["www-authenticate"] == 'Bearer realm="orders"'
    assert forged.headers["www-authenticate"] == 'Bearer realm="orders", error="invalid_token"'


# ---------------------------------------------------------------- API3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api3_unknown_property_is_a_422_extra_forbidden_and_nothing_changes() -> None:
    store = seed_store()
    async with serve(store) as client:
        response = await client.patch(
            "/v1/users/me", headers=BOB, json={"display_name": "B", "role": "admin"}
        )
    assert response.status_code == 422
    assert [(e["type"], e["loc"]) for e in response.json()["detail"]] == [
        ("extra_forbidden", ["body", "role"])
    ]
    assert (store.users["u-bob"].role, store.users["u-bob"].display_name) == ("user", "Bob")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api3_response_model_filters_a_dict_that_carries_secrets() -> None:
    async with serve(seed_store()) as client:
        hardened = (await client.get("/v1/users/me", headers=ALICE)).json()
    async with serve(seed_store(), ALL_CONTROLS - {Risk.API3}) as client:
        raw = (await client.get("/v1/users/me", headers=ALICE)).json()
    assert set(hardened) == {"id", "email", "display_name"}
    assert {"role", "password_hash"} <= set(raw)


# ---------------------------------------------------------------- API4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api4_page_size_cap_is_inclusive_at_max_page() -> None:
    async with serve(seed_store()) as client:
        at_cap = await client.get(f"/v1/orders?limit={MAX_PAGE}", headers=ALICE)
        over = await client.get(f"/v1/orders?limit={MAX_PAGE + 1}", headers=ALICE)
    assert (at_cap.status_code, over.status_code) == (200, 422)
    assert over.json()["detail"][0]["type"] == "less_than_equal"


def body_of(size: int) -> bytes:
    """A JSON object of exactly `size` bytes."""
    shell = b'{"display_name": "x", "pad": ""}'
    return shell[:-2] + b"y" * (size - len(shell)) + b'"}'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api4_body_cap_rejects_one_byte_over_with_content_length() -> None:
    headers = {**ALICE, "Content-Type": "application/json"}
    async with serve(seed_store(), ALL_CONTROLS - {Risk.API3}) as client:
        at_cap = await client.patch(
            "/v1/users/me", headers=headers, content=body_of(MAX_BODY_BYTES)
        )
        over = await client.patch(
            "/v1/users/me", headers=headers, content=body_of(MAX_BODY_BYTES + 1)
        )
    assert (at_cap.status_code, over.status_code) == (200, 413)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api4_body_cap_also_counts_chunked_bodies_without_content_length() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"display_name": "'
        for _ in range(64):
            yield b"z" * 1024
        yield b'"}'

    async with serve(seed_store()) as client:
        request = client.build_request(
            "PATCH",
            "/v1/users/me",
            headers={**ALICE, "Content-Type": "application/json"},
            content=chunks(),
        )
        assert "content-length" not in request.headers
        response = await client.send(request)
    assert response.status_code == 413


# ---------------------------------------------------------------- API5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api5_every_admin_route_denies_a_user_and_admits_an_admin() -> None:
    store = seed_store()
    app = build_app(store)
    admin_routes = sorted(r for r in route_inventory(app) if "/admin/" in r[1])
    assert admin_routes == [("DELETE", "/v1/admin/users/{user_id}"), ("GET", "/v1/admin/users")]
    async with serve(store) as client:
        for method, path in admin_routes:
            url = path.replace("{user_id}", "u-alice")
            assert (await client.request(method, url, headers=BOB)).status_code == 403
        assert "u-alice" in store.users
        assert (await client.delete("/v1/admin/users/u-alice", headers=ROOT)).status_code == 204
    assert "u-alice" not in store.users


# ---------------------------------------------------------------- API6


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api6_dwell_gate_then_per_account_cap() -> None:
    clock = FakeClock()
    store = seed_store(clock)
    async with serve(store) as client:
        unseen = await client.post("/v1/drops/console-x/purchase", headers=BOB)
        await client.get("/v1/drops/console-x", headers=BOB)
        clock.now += 0.4
        too_fast = await client.post("/v1/drops/console-x/purchase", headers=BOB)
        clock.now += 0.6
        first = await client.post("/v1/drops/console-x/purchase", headers=BOB)
        second = await client.post("/v1/drops/console-x/purchase", headers=BOB)
    assert [r.status_code for r in (unseen, too_fast, first, second)] == [429, 429, 200, 409]
    assert too_fast.headers["retry-after"] == "1"
    assert store.stock["console-x"] == 9


# ---------------------------------------------------------------- API7


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "https://169.254.169.254/latest/meta-data/",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://[::ffff:169.254.169.254]/",
        "https://[64:ff9b::a9fe:a9fe]/",
        "https://10.0.0.5/",
        "https://hooks.partner.example:8443/",
        "https://hooks.partner.example@169.254.169.254/",
        "https://metadata.attacker.example/",
        "https://mixed.attacker.example/",
        "https://nat64.attacker.example/",
        "https://unknown.example/",
    ],
)
def test_api7_guard_rejects_internal_targets(url: str) -> None:
    with pytest.raises(UnsafeTargetError):
        guard_outbound_url(url, seed_store().resolve)


@pytest.mark.unit
def test_api7_stdlib_is_global_alone_misses_nat64_but_the_guard_does_not() -> None:
    nat64_imds = ipaddress.ip_address("64:ff9b::a9fe:a9fe")
    assert nat64_imds.is_global  # CPython 3.14: the NAT64 prefix itself counts as global
    assert not is_public_address(nat64_imds)
    assert is_public_address(ipaddress.ip_address("93.184.215.14"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api7_allowed_webhook_is_pinned_to_the_checked_ip_with_host_and_sni() -> None:
    store = seed_store()
    async with serve(store) as client:
        response = await client.post(
            "/v1/webhooks/test", headers=ALICE, json={"url": "https://hooks.partner.example/e"}
        )
    assert response.json() == {"delivered": True, "status": 200}
    [sent] = store.outbound
    assert str(sent.url) == "https://93.184.215.14/e"
    assert sent.headers["host"] == "hooks.partner.example"
    assert sent.extensions["sni_hostname"] == "hooks.partner.example"


# ---------------------------------------------------------------- API8


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api8_debug_mode_returns_the_traceback_and_hardened_mode_does_not() -> None:
    plain = {**ALICE, "Accept": "text/plain"}
    async with serve(seed_store(), ALL_CONTROLS - {Risk.API8}) as client:
        debug = await client.get("/v1/orders/1001/invoice", headers=plain)
    async with serve(seed_store()) as client:
        quiet = await client.get("/v1/orders/1001/invoice", headers=plain)
    assert debug.status_code == quiet.status_code == 500
    assert debug.text.startswith("Traceback (most recent call last):")
    assert "db.internal" in debug.text
    assert quiet.text == "Internal Server Error"
    # ServerErrorMiddleware sits outside every user middleware, so the 500 misses their headers.
    assert "x-content-type-options" not in quiet.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api8_starlette_star_plus_credentials_reflects_any_origin() -> None:
    evil = {**ALICE, "Origin": "https://evil.example"}
    good = {**ALICE, "Origin": "https://app.example.com"}
    async with serve(seed_store(), ALL_CONTROLS - {Risk.API8}) as client:
        star = await client.get("/v1/users/me", headers=evil)
    async with serve(seed_store()) as client:
        denied = await client.get("/v1/users/me", headers=evil)
        allowed = await client.get("/v1/users/me", headers=good)
    assert star.headers["access-control-allow-origin"] == "https://evil.example"
    assert star.headers["access-control-allow-credentials"] == "true"
    assert "access-control-allow-origin" not in denied.headers
    assert allowed.headers["access-control-allow-origin"] == "https://app.example.com"
    assert allowed.headers["cache-control"] == "no-store"
    assert allowed.headers["x-content-type-options"] == "nosniff"


# ---------------------------------------------------------------- API9


@pytest.mark.unit
def test_api9_inventory_diff_names_the_shadow_routes() -> None:
    assert inventory_drift(build_app(seed_store())) == frozenset()
    drift = inventory_drift(build_app(seed_store(), ALL_CONTROLS - {Risk.API9}))
    assert drift == {
        ("GET", "/v0/users/{user_id}"),
        ("GET", "/openapi.json"),
        ("GET", "/docs"),
        ("GET", "/docs/oauth2-redirect"),
        ("GET", "/redoc"),
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api9_legacy_version_bypasses_every_new_control() -> None:
    store = seed_store()
    async with serve(store, ALL_CONTROLS - {Risk.API9}) as client:
        legacy = await client.get("/v0/users/u-root")
    assert legacy.status_code == 200
    assert legacy.json()["password_hash"] == store.users["u-root"].password_hash


# ---------------------------------------------------------------- API10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api10_308_from_partner_resends_the_post_body_unless_redirects_are_off() -> None:
    for controls, expected_status, leaks in (
        (ALL_CONTROLS - {Risk.API10}, 200, True),
        (ALL_CONTROLS, 502, False),
    ):
        store = seed_store()
        store.partner_mode = "redirect"
        async with serve(store, controls) as client:
            response = await client.get("/v1/orders/1001/shipping-quote", headers=ALICE)
        stolen = [r for r in store.outbound if r.url.host == "attacker.example"]
        assert response.status_code == expected_status
        assert bool(stolen) is leaks
        if stolen:
            assert stolen[0].method == "POST"
            assert b"1 Alice Way" in stolen[0].content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api10_read_cap_stops_pulling_a_huge_partner_body() -> None:
    store = seed_store()
    store.partner_mode = "huge"
    async with serve(store) as client:
        response = await client.get("/v1/orders/1001/shipping-quote", headers=ALICE)
    assert response.status_code == 502
    assert store.upstream_bytes_sent <= MAX_UPSTREAM_BYTES + 2 * 1024
    unbounded = seed_store()
    unbounded.partner_mode = "huge"
    async with serve(unbounded, ALL_CONTROLS - {Risk.API10}) as client:
        echoed = await client.get("/v1/orders/1001/shipping-quote", headers=ALICE)
    assert echoed.status_code == 200
    assert unbounded.upstream_bytes_sent > 1024 * 1024


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api10_out_of_range_partner_value_is_a_502_not_a_negative_price() -> None:
    store = seed_store()
    store.partner_mode = "negative"
    async with serve(store) as client:
        response = await client.get("/v1/orders/1001/shipping-quote", headers=ALICE)
    assert response.status_code == 502
    assert response.json() == {"detail": "shipping partner returned an unusable response"}


@pytest.mark.unit
def test_imds_fixture_key_is_the_aws_documentation_example() -> None:
    assert FAKE_IMDS_KEY == "AKIAIOSFODNN7EXAMPLE"
