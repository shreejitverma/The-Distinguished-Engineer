"""Security response headers: HSTS, nosniff, Cache-Control, CSP, and security.txt."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from api_labs.f15_api_authorization_and_owasp_security.security_headers import (
    ONE_YEAR,
    SECURITY_TXT_PATH,
    SecurityHeadersMiddleware,
    StsPolicy,
    blocked,
    build_app,
    csp_hash,
    determine_nosniff,
    framing_allowed,
    get_decode_split,
    inline_scripts,
    parse_csp,
    parse_rfc3339,
    parse_sts,
    preload_eligible,
    received_sts,
    render_security_txt,
    secure,
    validate_security_txt,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import ASGIApp

NOW = datetime(2026, 11, 15, tzinfo=UTC)
CANONICAL = "https://api.example.com/.well-known/security.txt"
HTTPS = "https://api.example.com"
HTTP = "http://api.example.com"


def _security_txt() -> str:
    return render_security_txt(
        contacts=["mailto:security@example.com", "https://example.com/report"],
        expires=NOW + timedelta(days=180),
        canonical=[CANONICAL],
        policy="https://example.com/disclosure-policy",
        preferred_languages=["en", "de"],
    )


async def _get(
    app: ASGIApp,
    url: str,
    *,
    base: str = HTTPS,
    headers: dict[str, str] | None = None,
    root_path: str = "",
    client: tuple[str, int] = ("127.0.0.1", 123),
) -> httpx.Response:
    transport = httpx.ASGITransport(
        app=app, raise_app_exceptions=False, root_path=root_path, client=client
    )
    async with httpx.AsyncClient(transport=transport, base_url=base) as http:
        return await http.get(url, headers=headers)


async def _ok(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def _route_app(**headers: str) -> Starlette:
    async def endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True}, headers=headers)

    return Starlette(routes=[Route("/r", endpoint)])


# --- HSTS: what a user agent accepts (RFC 6797) -------------------------------------------


@pytest.mark.unit
def test_sts_accepts_the_rfc_6797_section_6_2_examples() -> None:
    assert parse_sts("max-age=31536000") == StsPolicy(31536000, False, False)
    assert parse_sts("max-age=15768000 ; includeSubDomains") == StsPolicy(15768000, True, False)
    assert parse_sts('max-age="31536000"') == StsPolicy(31536000, False, False)
    # max-age=0 is valid and tells the UA to delete the policy, includeSubDomains or not.
    assert parse_sts("max-age=0; includeSubDomains") == StsPolicy(0, True, False)


@pytest.mark.unit
def test_sts_directive_names_are_case_insensitive_and_empty_directives_allowed() -> None:
    assert parse_sts("MAX-AGE=10;;INCLUDESUBDOMAINS;") == StsPolicy(10, True, False)


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    # chr(0x0661) is ARABIC-INDIC DIGIT ONE: str.isdigit() is True for it.
    [
        "includeSubDomains",
        "max-age",
        "max-age=",
        "max-age=-1",
        "max-age=1.5",
        "max-age=" + chr(0x0661),
    ],
)
def test_sts_without_a_valid_max_age_is_ignored(value: str) -> None:
    assert parse_sts(value) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    ["max-age=1; max-age=2", "max-age=1; includeSubDomains; INCLUDESUBDOMAINS"],
)
def test_a_repeated_directive_invalidates_the_whole_field(value: str) -> None:
    assert parse_sts(value) is None


@pytest.mark.unit
def test_unknown_directives_are_ignored_and_preload_is_one_of_them() -> None:
    assert parse_sts('max-age=60; preload; report-uri="x;y"') == StsPolicy(60, False, True)


@pytest.mark.unit
@pytest.mark.parametrize(
    "value", ["max-age=1; includeSubDomains=yes", "max-age=1, includeSubDomains", "max-age=1 2"]
)
def test_nonconforming_syntax_invalidates_the_whole_field(value: str) -> None:
    assert parse_sts(value) is None


@pytest.mark.unit
def test_huge_max_age_is_clamped_by_length_before_int() -> None:
    hostile = "max-age=" + "9" * 5000  # int() would raise past 4300 digits
    assert parse_sts(hostile) == StsPolicy(10**18, False, False)
    clamped = parse_sts(hostile, max_age_cap=ONE_YEAR)
    assert clamped is not None
    assert clamped.max_age == 31536000


@pytest.mark.unit
@pytest.mark.parametrize("host", ["192.0.2.1", "[2001:db8::1]"])
def test_ua_never_notes_an_ip_literal_host(host: str) -> None:
    assert received_sts(["max-age=60"], secure=True, host=host) is None
    assert received_sts(["max-age=60"], secure=True, host="api.example.com") is not None


@pytest.mark.unit
def test_ua_ignores_sts_received_over_plain_http() -> None:
    assert received_sts(["max-age=60"], secure=False, host="api.example.com") is None


@pytest.mark.unit
def test_ua_processes_only_the_first_sts_field() -> None:
    policy = received_sts(["max-age=0", "max-age=31536000"], secure=True, host="a.example")
    assert policy == StsPolicy(0, False, False)


@pytest.mark.unit
def test_preload_needs_a_year_include_subdomains_and_preload() -> None:
    assert preload_eligible(parse_sts("max-age=31536000; includeSubDomains; preload"))
    assert not preload_eligible(parse_sts("max-age=31535999; includeSubDomains; preload"))
    assert not preload_eligible(parse_sts("max-age=63072000; preload"))
    assert not preload_eligible(parse_sts("max-age=63072000; includeSubDomains"))


# --- nosniff: what Fetch blocks ---------------------------------------------------------


@pytest.mark.unit
def test_get_decode_split_matches_the_fetch_examples() -> None:
    assert get_decode_split(["nosniff,"]) == ["nosniff", ""]
    assert get_decode_split([""]) == [""]
    assert get_decode_split([]) is None
    assert get_decode_split(['text/html;", x/x']) == ['text/html;", x/x']


@pytest.mark.unit
def test_only_the_first_x_content_type_options_member_counts() -> None:
    assert determine_nosniff(["NoSniff"])
    assert determine_nosniff(["nosniff", "something-else"])
    assert not determine_nosniff(["sniff, nosniff"])
    assert not determine_nosniff(["foo", "nosniff"])
    assert not determine_nosniff([])


@pytest.mark.unit
def test_json_loaded_as_a_script_runs_without_nosniff_and_is_blocked_with_it() -> None:
    assert not blocked("script", "application/json", [])
    assert blocked("script", "application/json", ["nosniff"])
    assert not blocked("script", "text/javascript; charset=utf-8", ["nosniff"])
    assert blocked("script", None, ["nosniff"])  # missing Content-Type is failure
    assert blocked("style", "application/json", ["nosniff"])
    assert not blocked("style", "text/css", ["nosniff"])
    assert not blocked("image", "application/json", ["nosniff"])  # images are not checked


@pytest.mark.unit
def test_script_destinations_refuse_image_and_csv_types_even_without_nosniff() -> None:
    assert blocked("worker", "image/png", [])
    assert blocked("script", "text/csv", [])
    assert not blocked("script", "text/plain", [])


# --- CSP --------------------------------------------------------------------------------


@pytest.mark.unit
def test_csp_folds_directive_names_and_keeps_the_first_duplicate() -> None:
    policy = parse_csp("Script-Src 'none'; script-src *; ;default-src 'self'")
    assert policy == {"script-src": ["'none'"], "default-src": ["'self'"]}


@pytest.mark.unit
def test_frame_ancestors_does_not_fall_back_to_default_src() -> None:
    assert framing_allowed(["default-src 'none'"])
    assert not framing_allowed(["default-src 'none'; frame-ancestors 'none'"])
    assert not framing_allowed(["script-src 'self', frame-ancestors 'none'"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_docs_csp_allows_exactly_the_inline_script_that_was_served() -> None:
    response = await _get(secure(build_app(security_txt=_security_txt)), "/docs")
    scripts = inline_scripts(response.text)
    assert len(scripts) == 1
    policy = parse_csp(response.headers["content-security-policy"])
    assert csp_hash(scripts[0]) in policy["script-src"]
    assert "'unsafe-inline'" not in policy["script-src"]
    assert csp_hash(scripts[0] + " ") not in policy["script-src"]  # one byte changes it
    assert policy["frame-ancestors"] == ["'none'"]
    assert policy["connect-src"] == ["'self'"]  # Swagger UI fetches /openapi.json


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_docs_hash_depends_on_root_path() -> None:
    app = secure(build_app(security_txt=_security_txt))
    direct = await _get(app, "/docs")
    mounted = await _get(app, "/docs", root_path="/api")
    assert "/api/openapi.json" in mounted.text
    direct_hash = parse_csp(direct.headers["content-security-policy"])["script-src"][-1]
    mounted_hash = parse_csp(mounted.headers["content-security-policy"])["script-src"][-1]
    assert direct_hash != mounted_hash


@pytest.mark.unit
@pytest.mark.asyncio
async def test_docs_without_their_own_policy_get_the_api_default_that_blocks_swagger() -> None:
    app = secure(build_app(security_txt=_security_txt, docs_csp=False))
    response = await _get(app, "/docs")
    policy = parse_csp(response.headers["content-security-policy"])
    assert policy == {"default-src": ["'none'"], "frame-ancestors": ["'none'"]}


# --- the middleware ---------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_json_response_over_https_gets_the_full_api_header_set() -> None:
    response = await _get(secure(build_app(security_txt=_security_txt)), "/v1/me")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    csp = response.headers["content-security-policy"]
    assert csp == "default-src 'none'; frame-ancestors 'none'"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["cache-control"] == "no-store"  # the route set none: fail closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hsts_is_never_sent_over_plain_http_even_if_a_route_sets_it() -> None:
    app = secure(_route_app(**{"Strict-Transport-Security": "max-age=600"}))
    plain = await _get(app, "/r", base=HTTP)
    assert "strict-transport-security" not in plain.headers
    assert plain.headers["x-content-type-options"] == "nosniff"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exactly_one_hsts_field_even_when_the_route_also_sets_one() -> None:
    app = secure(_route_app(**{"Strict-Transport-Security": "max-age=0"}))
    response = await _get(app, "/r")
    assert response.headers.get_list("strict-transport-security") == [
        "max-age=31536000; includeSubDomains"
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_credentialed_request_never_receives_a_shareable_response() -> None:
    app = secure(build_app(security_txt=_security_txt))
    anonymous = await _get(app, "/v1/catalog")
    bearer = await _get(app, "/v1/catalog", headers={"Authorization": "Bearer EXAMPLE-TOKEN"})
    cookie = await _get(app, "/v1/catalog", headers={"Cookie": "session=EXAMPLE"})
    assert anonymous.headers["cache-control"] == "public, max-age=60"
    assert bearer.headers["cache-control"] == "no-store"
    assert cookie.headers["cache-control"] == "no-store"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_private_survives_for_a_credentialed_request() -> None:
    app = secure(_route_app(**{"Cache-Control": "private, max-age=30"}))
    response = await _get(app, "/r", headers={"Authorization": "Bearer EXAMPLE-TOKEN"})
    assert response.headers["cache-control"] == "private, max-age=30"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_middleware_misses_the_500_from_server_error_middleware() -> None:
    app = build_app(security_txt=_security_txt)
    app.add_middleware(SecurityHeadersMiddleware)
    response = await _get(app, "/v1/boom")
    assert response.status_code == 500
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert "x-content-type-options" not in response.headers
    assert "strict-transport-security" not in response.headers
    handled = await _get(app, "/missing")  # 404 comes from ExceptionMiddleware, inside
    assert handled.headers["x-content-type-options"] == "nosniff"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wrapping_the_whole_app_covers_the_500_too() -> None:
    response = await _get(secure(build_app(security_txt=_security_txt)), "/v1/boom")
    assert response.status_code == 500
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["strict-transport-security"].startswith("max-age=31536000")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hsts_follows_the_scheme_only_a_trusted_proxy_may_assert() -> None:
    proxy = ProxyHeadersMiddleware(cast("Any", secure(_route_app())), trusted_hosts="10.0.0.1")
    app = cast("ASGIApp", proxy)
    claim = {"X-Forwarded-Proto": "https"}
    via_proxy = await _get(app, "/r", base=HTTP, headers=claim, client=("10.0.0.1", 5000))
    spoofed = await _get(app, "/r", base=HTTP, headers=claim, client=("203.0.113.9", 5000))
    assert "strict-transport-security" in via_proxy.headers
    assert "strict-transport-security" not in spoofed.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_https_redirect_is_a_307_after_the_request_was_sent() -> None:
    inner = Starlette(
        routes=[Route("/r", _ok)],
        middleware=[Middleware(HTTPSRedirectMiddleware)],
    )
    response = await _get(secure(inner), "/r?x=1", base=HTTP)
    assert response.status_code == 307
    assert response.headers["location"] == "https://api.example.com/r?x=1"
    assert "strict-transport-security" not in response.headers


# --- security.txt (RFC 9116) ------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_security_txt_is_served_as_utf8_plain_text_at_the_well_known_path() -> None:
    app = secure(build_app(security_txt=_security_txt))
    response = await _get(app, SECURITY_TXT_PATH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert validate_security_txt(response.text, now=NOW, retrieved_from=CANONICAL) == []
    legacy = await _get(app, "/security.txt")
    assert legacy.status_code == 301
    assert legacy.headers["location"] == SECURITY_TXT_PATH


@pytest.mark.unit
def test_the_rfc_9116_section_2_6_example_is_now_expired() -> None:
    example = (
        "# Our security address\n"
        "Contact: mailto:security@example.com\n"
        "# Our OpenPGP key\n"
        "Encryption: https://example.com/pgp-key.txt\n"
        "# Our security policy\n"
        "Policy: https://example.com/security-policy.html\n"
        "# Our security acknowledgments page\n"
        "Acknowledgments: https://example.com/hall-of-fame.html\n"
        "Expires: 2021-12-31T18:37:07z\n"
    )
    assert validate_security_txt(example, now=NOW, retrieved_from=CANONICAL) == ["expires-past"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("expires_lines", "code"),
    [
        ([], "expires-missing"),
        (["2027-01-01T00:00:00Z", "2027-02-01T00:00:00Z"], "expires-repeated"),
        (["2027-01-01T00:00:00"], "expires-invalid"),
        (["2027-01-01 00:00:00Z"], "expires-invalid"),
        (["2028-01-01T00:00:00Z"], "expires-over-a-year"),
    ],
)
def test_expires_is_required_once_with_an_offset(expires_lines: list[str], code: str) -> None:
    text = "Contact: mailto:security@example.com\n" + "".join(
        f"Expires: {value}\n" for value in expires_lines
    )
    assert validate_security_txt(text, now=NOW, retrieved_from=CANONICAL) == [code]


@pytest.mark.unit
def test_contact_is_required_and_web_uris_must_be_https() -> None:
    expires = "Expires: 2027-01-01T00:00:00Z\n"
    assert validate_security_txt(expires, now=NOW, retrieved_from=CANONICAL) == ["contact-missing"]
    text = "Contact: http://example.com/report\nPolicy: http://example.com/p\n" + expires
    assert validate_security_txt(text, now=NOW, retrieved_from=CANONICAL) == [
        "insecure-uri-contact",
        "insecure-uri-policy",
    ]
    fine = "Contact: tel:+1-201-555-0123\nEncryption: openpgp4fpr:5f2de5521c63\n" + expires
    assert validate_security_txt(fine, now=NOW, retrieved_from=CANONICAL) == []


@pytest.mark.unit
def test_a_canonical_list_without_the_retrieval_uri_is_untrusted() -> None:
    text = _security_txt()
    assert validate_security_txt(
        text, now=NOW, retrieved_from="https://other.example.com/.well-known/security.txt"
    ) == ["canonical-mismatch"]


@pytest.mark.unit
def test_field_syntax_line_endings_and_singletons() -> None:
    base = "Contact: mailto:security@example.com\nExpires: 2027-01-01T00:00:00Z\n"
    assert validate_security_txt(
        base + "Policy:https://example.com/p\n", now=NOW, retrieved_from=CANONICAL
    ) == ["malformed-line-3"]
    assert validate_security_txt(base.rstrip("\n"), now=NOW, retrieved_from=CANONICAL) == [
        "no-final-newline"
    ]
    assert validate_security_txt(
        base + "Preferred-Languages: en\nPreferred-Languages: fr\n",
        now=NOW,
        retrieved_from=CANONICAL,
    ) == ["preferred-languages-repeated"]
    assert (
        validate_security_txt(
            base.replace("\n", "\r\n") + "CONTACT: mailto:alt@example.com\r\n",
            now=NOW,
            retrieved_from=CANONICAL,
        )
        == []
    )


@pytest.mark.unit
def test_consumer_side_size_limits_from_section_5_4() -> None:
    base = "Contact: mailto:security@example.com\nExpires: 2027-01-01T00:00:00Z\n"
    assert validate_security_txt(base + "#" * (32 * 1024), now=NOW, retrieved_from=CANONICAL) == [
        "too-large"
    ]
    assert validate_security_txt(base + "\n" * 999, now=NOW, retrieved_from=CANONICAL) == [
        "too-many-lines"
    ]
    long_field = base + "Hiring: https://example.com/" + "a" * 2048 + "\n"
    assert validate_security_txt(long_field, now=NOW, retrieved_from=CANONICAL) == [
        "malformed-line-3"
    ]


@pytest.mark.unit
def test_stdlib_fromisoformat_is_not_an_rfc_3339_parser() -> None:
    # RFC 9116's own example uses a lowercase "z", which RFC 3339 Section 5.6 permits.
    with pytest.raises(ValueError, match="Invalid isoformat string"):
        datetime.fromisoformat("2021-12-31T18:37:07z")
    assert datetime.fromisoformat("2021-12-31T18:37:07").tzinfo is None  # no offset accepted
    assert datetime.fromisoformat("20211231T183707Z").year == 2021  # basic format accepted
    assert parse_rfc3339("2021-12-31T18:37:07z") == datetime(2021, 12, 31, 18, 37, 7, tzinfo=UTC)
    assert parse_rfc3339("2021-12-31T18:37:07") is None
    assert parse_rfc3339("20211231T183707Z") is None
