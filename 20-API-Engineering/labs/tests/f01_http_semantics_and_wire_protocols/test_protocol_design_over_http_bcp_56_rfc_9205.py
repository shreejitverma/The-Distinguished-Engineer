"""BCP 56 (RFC 9205) anti-pattern audit and the client behaviors it protects."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

import httpx
import pytest

from api_labs.f01_http_semantics_and_wire_protocols.bcp56_audit import (
    REGISTERED_STATUS_CODES,
    WIDGET_NOT_FOUND,
    Exchange,
    PrefixGateway,
    audit_exchange,
    audit_vary,
    build_app,
    classify_error,
    exchange_from_httpx,
    fallback_status,
    h2_response_fields,
    h11_response_head,
)

if TYPE_CHECKING:
    from starlette.applications import Starlette

UNDERSTOOD = frozenset({200, 201, 204, 301, 302, 303, 307, 308, 400, 401, 404, 500, 503})
KNOWN_PROBLEMS = frozenset({WIDGET_NOT_FOUND})

CLEAN = Exchange(
    method="GET",
    target="/widgets/1",
    request_fields=(("host", "api.test"), ("accept", "application/json")),
    request_content=b"",
    status=200,
    response_fields=(("content-type", "application/json"), ("cache-control", "max-age=60")),
    response_content=b'{"id":"1"}',
)


def rules(exchange: Exchange) -> list[str]:
    return [finding.rule for finding in audit_exchange(exchange)]


def client(app: Starlette | PrefixGateway) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test")


@pytest.mark.unit
def test_clean_exchange_has_no_findings() -> None:
    assert audit_exchange(CLEAN) == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "flagged"),
    [("GET", False), ("PATCH", False), ("QUERY", False), ("FETCHWIDGET", True), ("get", True)],
    ids=["get", "patch", "query", "app-specific", "lowercase-is-a-different-method"],
)
def test_only_registered_methods_pass(method: str, flagged: bool) -> None:
    exchange = dataclasses.replace(CLEAN, method=method)
    assert ("unregistered-method" in rules(exchange)) is flagged


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "flagged"),
    [(422, False), (429, False), (299, True), (418, True), (499, True), (600, True)],
    ids=["422", "429", "unassigned-299", "reserved-418", "unassigned-499", "invalid-600"],
)
def test_only_registered_status_codes_pass(status: int, flagged: bool) -> None:
    exchange = dataclasses.replace(CLEAN, status=status)
    assert ("unregistered-status" in rules(exchange)) is flagged


@pytest.mark.unit
def test_x_prefix_flagged_unless_registered() -> None:
    exchange = dataclasses.replace(
        CLEAN,
        response_fields=(
            *CLEAN.response_fields,
            ("x-widget-count", "3"),
            ("x-content-type-options", "nosniff"),
            ("widgets-count", "3"),
        ),
    )
    found = [f.detail for f in audit_exchange(exchange) if f.rule == "x-prefixed-field"]
    assert found == ["x-widget-count uses X-"]


@pytest.mark.unit
def test_underscore_field_name_flagged() -> None:
    exchange = dataclasses.replace(
        CLEAN, request_fields=(*CLEAN.request_fields, ("widget_tenant", "a"))
    )
    assert rules(exchange) == ["underscore-field-name"]


@pytest.mark.unit
def test_get_with_content_flagged() -> None:
    exchange = dataclasses.replace(CLEAN, request_content=b'{"filter":"red"}')
    findings = audit_exchange(exchange)
    assert [(f.rule, f.detail) for f in findings] == [
        ("get-with-content", "GET carries 16 bytes of content")
    ]


@pytest.mark.unit
def test_problem_details_status_must_match_http_status() -> None:
    problem = dataclasses.replace(
        CLEAN,
        status=404,
        response_fields=(
            ("content-type", "application/problem+json"),
            ("cache-control", "no-store"),
        ),
        response_content=b'{"type":"about:blank","status":409}',
    )
    assert rules(problem) == ["problem-status-mismatch"]
    assert rules(dataclasses.replace(problem, response_content=b'{"status":404}')) == []
    assert rules(dataclasses.replace(problem, status=200)) == ["problem-with-success-status"]


@pytest.mark.unit
def test_problem_details_must_be_a_json_object() -> None:
    problem = dataclasses.replace(
        CLEAN,
        status=404,
        response_fields=(
            ("content-type", "application/problem+json"),
            ("cache-control", "no-store"),
        ),
        response_content=b'["not", "an", "object"]',
    )
    assert rules(problem) == ["problem-not-json-object"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "cache_control", "flagged"),
    [
        (200, None, True),
        (404, None, True),
        (201, None, False),
        (200, "max-age=0", False),
        (200, "no-store", False),
        (200, "private", True),
    ],
    ids=["200-bare", "404-bare", "201-not-heuristic", "max-age", "no-store", "private-only"],
)
def test_heuristically_cacheable_response_needs_explicit_freshness(
    status: int, cache_control: str | None, flagged: bool
) -> None:
    fields: tuple[tuple[str, str], ...] = (("content-type", "application/json"),)
    if cache_control is not None:
        fields = (*fields, ("cache-control", cache_control))
    exchange = dataclasses.replace(CLEAN, status=status, response_fields=fields)
    assert ("implicit-freshness" in rules(exchange)) is flagged


@pytest.mark.unit
def test_expires_is_explicit_freshness_and_only_get_and_head_are_checked() -> None:
    bare: tuple[tuple[str, str], ...] = (("content-type", "application/json"),)
    expires = (*bare, ("expires", "Thu, 01 Jan 2026 00:00:00 GMT"))
    assert rules(dataclasses.replace(CLEAN, response_fields=expires)) == []
    assert rules(dataclasses.replace(CLEAN, method="HEAD", response_fields=bare)) == [
        "implicit-freshness"
    ]
    assert rules(dataclasses.replace(CLEAN, method="POST", response_fields=bare)) == []


@pytest.mark.unit
def test_missing_vary_detected_from_two_exchanges() -> None:
    english = dataclasses.replace(
        CLEAN, request_fields=(("host", "api.test"), ("accept-language", "en"))
    )
    german = dataclasses.replace(
        english,
        request_fields=(("host", "api.test"), ("accept-language", "de")),
        response_content=b'{"id":"1","name":"Zahnrad"}',
    )
    assert [f.detail for f in audit_vary(english, german)] == ["response varies on accept-language"]
    declared = (*english.response_fields, ("vary", "Accept-Language"))
    assert (
        audit_vary(
            dataclasses.replace(english, response_fields=declared),
            dataclasses.replace(german, response_fields=declared),
        )
        == ()
    )


@pytest.mark.unit
def test_vary_audit_exemptions_and_preconditions() -> None:
    english = dataclasses.replace(
        CLEAN, request_fields=(("host", "api.test"), ("accept-language", "en"))
    )
    german = dataclasses.replace(
        english,
        request_fields=(("host", "api.test"), ("accept-language", "de")),
        response_content=b'{"id":"1","name":"Zahnrad"}',
    )
    no_store = (("content-type", "application/json"), ("cache-control", "no-store"))
    assert (
        audit_vary(
            dataclasses.replace(english, response_fields=no_store),
            dataclasses.replace(german, response_fields=no_store),
        )
        == ()
    )
    star = (*english.response_fields, ("vary", "*"))
    assert (
        audit_vary(
            dataclasses.replace(english, response_fields=star),
            dataclasses.replace(german, response_fields=star),
        )
        == ()
    )
    with pytest.raises(ValueError, match="share method and target"):
        audit_vary(english, dataclasses.replace(german, target="/widgets/2"))


@pytest.mark.unit
def test_identical_content_proves_no_request_field_selected_the_response() -> None:
    english = dataclasses.replace(
        CLEAN, request_fields=(("host", "api.test"), ("accept-language", "en"))
    )
    german = dataclasses.replace(
        english, request_fields=(("host", "api.test"), ("accept-language", "de"))
    )
    assert english.response_content == german.response_content
    assert audit_vary(english, german) == ()


@pytest.mark.unit
def test_field_name_seen_on_request_and_response_is_reported_once() -> None:
    both = ("x-widget-count", "3")
    exchange = dataclasses.replace(
        CLEAN,
        request_fields=(*CLEAN.request_fields, both),
        response_fields=(*CLEAN.response_fields, both),
    )
    assert [f.detail for f in audit_exchange(exchange)] == ["x-widget-count uses X-"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exchange_from_httpx_records_the_request_as_sent() -> None:
    async with client(build_app()) as http:
        response = await http.request(
            "GET",
            "/widgets/1?lang=en",
            content=b"filter=red",
            headers={"accept-language": "en"},
        )
    recorded = exchange_from_httpx(response)
    assert (recorded.method, recorded.target) == ("GET", "/widgets/1?lang=en")
    assert recorded.request_content == b"filter=red"
    assert recorded.request_field("accept-language") == "en"
    assert recorded.request_field("host") == "api.test"
    assert [f.rule for f in audit_exchange(recorded)] == ["get-with-content"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "expected"),
    [(404, 404), (471, 400), (499, 400), (299, 200), (308, 308), (600, 500), (99, 500)],
)
def test_unknown_status_falls_back_to_class_x00(status: int, expected: int) -> None:
    assert fallback_status(status, UNDERSTOOD) == expected


@pytest.mark.unit
def test_reason_phrase_exists_only_on_http_1_1_wire() -> None:
    fields = [(b"content-length", b"0")]
    head = h11_response_head(404, b"Widget Gone", fields)
    assert head.startswith(b"HTTP/1.1 404 Widget Gone\r\n")
    decoded = h2_response_fields(404, fields)
    assert decoded == ((b":status", b"404"), (b"content-length", b"0"))
    assert all(b"Widget Gone" not in name + value for name, value in decoded)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpx_synthesizes_reason_phrase_the_server_never_sent() -> None:
    async with client(build_app()) as http:
        response = await http.get("/widgets/9")
    assert "reason_phrase" not in response.extensions
    assert response.reason_phrase == "Not Found"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "code", "seen_method", "seen_content"),
    [
        ("POST", 301, "GET", ""),
        ("POST", 308, "POST", "w=1"),
        ("POST", 307, "POST", "w=1"),
        ("PUT", 301, "PUT", "w=1"),
        ("PUT", 302, "GET", ""),
        ("POST", 303, "GET", ""),
    ],
    ids=["post-301", "post-308", "post-307", "put-301", "put-302", "post-303"],
)
async def test_httpx_redirect_method_rewriting(
    method: str, code: int, seen_method: str, seen_content: str
) -> None:
    async with client(build_app()) as http:
        response = await http.request(
            method, f"/moved/{code}", content=b"w=1", follow_redirects=True
        )
    assert response.json()["method"] == seen_method
    assert response.json()["content"] == seen_content
    assert [r.status_code for r in response.history] == [code]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpx_strips_authorization_on_cross_origin_redirect() -> None:
    headers = {"authorization": "Bearer t0ken"}
    async with client(build_app()) as http:
        same = await http.get("/moved/307", headers=headers, follow_redirects=True)
        cross = await http.get(
            "/moved/307", params={"host": "other.test"}, headers=headers, follow_redirects=True
        )
    assert same.json()["authorization"] == "Bearer t0ken"
    assert cross.url.host == "other.test"
    assert cross.json()["authorization"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_intermediary_404_is_not_misattributed_to_the_application() -> None:
    app = build_app()
    async with client(app) as direct, client(PrefixGateway(app, prefix="/legacy")) as edge:
        from_app = await direct.get("/widgets/9")
        from_gateway = await edge.get("/widgets/1")
        existing = await direct.get("/widgets/1")
    assert (from_app.status_code, from_gateway.status_code, existing.status_code) == (404, 404, 200)
    app_class = classify_error(
        from_app.status_code,
        from_app.headers.get("content-type"),
        from_app.content,
        KNOWN_PROBLEMS,
        UNDERSTOOD,
    )
    gateway_class = classify_error(
        from_gateway.status_code,
        from_gateway.headers.get("content-type"),
        from_gateway.content,
        KNOWN_PROBLEMS,
        UNDERSTOOD,
    )
    assert (app_class.origin, app_class.kind) == ("application", WIDGET_NOT_FOUND)
    assert (gateway_class.origin, gateway_class.kind) == ("generic", "http-404")


@pytest.mark.unit
def test_unknown_problem_type_falls_back_to_status_class() -> None:
    verdict = classify_error(
        499, "application/problem+json", b'{"type":"urn:other"}', KNOWN_PROBLEMS, UNDERSTOOD
    )
    assert (verdict.origin, verdict.effective_status) == ("generic", 400)


@pytest.mark.unit
def test_problem_type_is_trusted_only_in_the_problem_media_type() -> None:
    content = json.dumps({"type": WIDGET_NOT_FOUND, "status": 404}).encode()
    plain = classify_error(404, "application/json", content, KNOWN_PROBLEMS, UNDERSTOOD)
    problem = classify_error(
        404, "application/problem+json; charset=utf-8", content, KNOWN_PROBLEMS, UNDERSTOOD
    )
    assert (plain.origin, plain.kind) == ("generic", "http-404")
    assert (problem.origin, problem.kind) == ("application", WIDGET_NOT_FOUND)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recorded_api_routes_audit_as_expected() -> None:
    async with client(build_app()) as http:
        good = exchange_from_httpx(await http.get("/widgets/1"))
        missing = exchange_from_httpx(await http.get("/widgets/9"))
        legacy = exchange_from_httpx(await http.get("/legacy/widgets/9"))
    assert audit_exchange(good) == ()
    assert rules(missing) == ["implicit-freshness"]
    assert rules(legacy) == [
        "x-prefixed-field",
        "problem-with-success-status",
        "implicit-freshness",
    ]


@pytest.mark.unit
def test_status_registry_snapshot_excludes_reserved_codes() -> None:
    assert {306, 418}.isdisjoint(REGISTERED_STATUS_CODES)
    assert {104, 421, 425, 451, 511} <= REGISTERED_STATUS_CODES
    assert all(100 <= code <= 599 for code in REGISTERED_STATUS_CODES)
