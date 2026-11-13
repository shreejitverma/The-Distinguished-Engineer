"""Lab tests for "Where Google, Microsoft, Zalando, and Stripe Disagree"."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from itertools import product
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel, to_snake

from api_labs.f07_versioning_evolution_and_api_guidelines.style_guide_synthesis import (
    AZURE,
    ERROR_CODES,
    ERROR_INFO_TYPE,
    GOOGLE,
    GUIDES,
    LINK_STYLES,
    MEDIA_TYPE,
    PAGE_PARAMS,
    STRIPE,
    ZALANDO,
    AbsentVersion,
    Case,
    ErrorStyle,
    PageStyle,
    ProblemKind,
    Report,
    StyleProfile,
    VersionCarrier,
    WireNames,
    create,
    create_app,
    lint_exchange,
    list_all,
    normalize_error,
    parse_report,
    profile_conflicts,
    render_report,
    version_parts,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
CREATED = datetime(2026, 2, 1, 12, 30, tzinfo=UTC)
REPORTS = [
    Report(f"rpt_{n:03d}", f"Report {n}", f"{n:064x}", T0 + timedelta(days=n - 1))
    for n in range(1, 6)
]
HASH_1 = "0" * 63 + "1"
JSON = {"content-type": "application/json"}
FORM = {"content-type": "application/x-www-form-urlencoded"}
BY_NAME = {profile.name: profile for profile in GUIDES}
ALL_GUIDES = pytest.mark.parametrize("profile", GUIDES, ids=list(BY_NAME))

# A deployment that takes Azure's query versioning and keeps Zalando's link rule unchanged.
HYBRID = replace(
    ZALANDO,
    name="hybrid",
    version_carrier=VersionCarrier.QUERY,
    version_name="api-version",
    version="2026-06-01",
    absent_version=AbsentVersion.REJECT,
)


def client_for(profile: StyleProfile) -> httpx.AsyncClient:
    app = create_app(profile, REPORTS, now=lambda: CREATED)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def first_page(client: httpx.AsyncClient, profile: StyleProfile) -> httpx.Response:
    query, headers = version_parts(profile)
    size_name, _ = PAGE_PARAMS[profile.page_style]
    params = {size_name: "2", **query}
    return await client.get(profile.collection_path, params=params, headers=headers)


def wire_members(profile: StyleProfile, **canonical: object) -> dict[str, object]:
    return {profile.names.by_canonical[name]: value for name, value in canonical.items()}


WIRE_SHAPES: dict[str, dict[str, object]] = {
    "google-aip": {
        "name": "reports/rpt_001",
        "displayName": "Report 1",
        "sha256Hash": HASH_1,
        "createTime": "2026-01-01T00:00:00Z",
    },
    "azure": {
        "id": "rpt_001",
        "displayName": "Report 1",
        "sha256Hash": HASH_1,
        "createdAt": "2026-01-01T00:00:00Z",
    },
    "zalando": {
        "id": "rpt_001",
        "display_name": "Report 1",
        "sha256_hash": HASH_1,
        "created_at": "2026-01-01T00:00:00Z",
    },
    "stripe-v1": {
        "id": "rpt_001",
        "display_name": "Report 1",
        "sha256_hash": HASH_1,
        "created": 1767225600,
    },
}


@pytest.mark.unit
@ALL_GUIDES
def test_one_record_has_four_wire_shapes_and_each_parses_back(profile: StyleProfile) -> None:
    wire = render_report(profile, REPORTS[0])
    assert wire == WIRE_SHAPES[profile.name]
    # A tolerant reader ignores members it does not know and recovers the same record.
    assert parse_report(profile, {**wire, "livemode": False}) == REPORTS[0]


@pytest.mark.unit
@ALL_GUIDES
def test_an_offset_time_is_rendered_as_the_same_instant_in_utc(profile: StyleProfile) -> None:
    india = timezone(timedelta(hours=5, minutes=30))
    local = replace(REPORTS[0], create_time=datetime(2026, 1, 1, 5, 30, tzinfo=india))
    # 05:30 at +05:30 is midnight UTC, so the wire form is the one the UTC record has.
    assert render_report(profile, local) == WIRE_SHAPES[profile.name]
    assert parse_report(profile, render_report(profile, local)) == REPORTS[0]


@pytest.mark.unit
def test_same_case_does_not_mean_same_names() -> None:
    wire = {profile.name: render_report(profile, REPORTS[0]) for profile in GUIDES}
    assert set(wire["google-aip"]) ^ set(wire["azure"]) == {
        "name",
        "id",
        "createTime",
        "createdAt",
    }
    assert set(wire["zalando"]) ^ set(wire["stripe-v1"]) == {"created_at", "created"}
    # Both are snake_case, yet one sends an RFC 3339 string and the other Unix seconds.
    assert wire["zalando"]["created_at"] == "2026-01-01T00:00:00Z"
    assert wire["stripe-v1"]["created"] == 1767225600


PAGE_SHAPES: dict[str, tuple[list[str], list[str]]] = {
    "google-aip": (["reports", "nextPageToken"], ["reports"]),
    "azure": (["value", "nextLink"], ["value"]),
    "zalando": (["self", "items", "next"], ["self", "items"]),
    "stripe-v1": (["object", "url", "has_more", "data"], ["object", "url", "has_more", "data"]),
}


@pytest.mark.unit
@pytest.mark.asyncio
@ALL_GUIDES
async def test_page_envelope_members_and_end_of_collection_signal(profile: StyleProfile) -> None:
    query, headers = version_parts(profile)
    size_name, _ = PAGE_PARAMS[profile.page_style]
    async with client_for(profile) as client:
        first = (await first_page(client, profile)).json()
        whole = await client.get(
            profile.collection_path, params={size_name: "1000", **query}, headers=headers
        )
    expected_first, expected_last = PAGE_SHAPES[profile.name]
    assert list(first) == expected_first
    assert list(whole.json()) == expected_last
    if profile is STRIPE:
        assert (first["has_more"], whole.json()["has_more"]) == (True, False)
        assert first["url"] == "/v1/reports"
    if profile is AZURE:
        assert first["nextLink"] == (
            "http://test/reports?maxpagesize=2&continuationToken=cnB0XzAwMg&api-version=2026-06-01"
        )
    if profile is ZALANDO:
        assert first["self"] == "http://test/reports?limit=2"
        assert first["next"] == "http://test/reports?limit=2&cursor=cnB0XzAwMg"


@pytest.mark.unit
@pytest.mark.asyncio
@ALL_GUIDES
async def test_one_client_recovers_the_same_records_from_every_dialect(
    profile: StyleProfile,
) -> None:
    async with client_for(profile) as client:
        reports, requests = await list_all(client, profile, page_size=2)
        _, exact_fit_requests = await list_all(client, profile, page_size=5)
    # Stripe lists newest first; the other three dialects keep the store's order.
    assert reports == (REPORTS[::-1] if profile is STRIPE else REPORTS)
    assert requests == 3
    # A page that ends exactly at the end of the collection carries no continuation.
    assert exact_fit_requests == 1


@pytest.mark.unit
@pytest.mark.asyncio
@ALL_GUIDES
async def test_page_size_zero_defaults_oversize_is_capped_and_bad_positions_are_400(
    profile: StyleProfile,
) -> None:
    query, headers = version_parts(profile)
    size_name, position_name = PAGE_PARAMS[profile.page_style]
    items_key = "data" if profile is STRIPE else list(PAGE_SHAPES[profile.name][1])[-1]
    async with client_for(profile) as client:

        async def page(**params: str) -> httpx.Response:
            return await client.get(
                profile.collection_path, params={**params, **query}, headers=headers
            )

        assert len((await page()).json()[items_key]) == 3
        assert len((await page(**{size_name: "0"})).json()[items_key]) == 3
        assert len((await page(**{size_name: "1000"})).json()[items_key]) == 5
        unknown = "rpt_999" if profile is STRIPE else "cnB0Xzk5OQ"
        # "cnB0XzAwMg" is the token for rpt_002; a lenient decoder drops the "!!!!" and accepts it.
        repairable = "rpt_!!!!002" if profile is STRIPE else "cnB0!!!!XzAwMg"
        # Stripe has no documented code for a bad position, so its kind is not recoverable.
        expected_kind = ERROR_CODES[profile.error_style][ProblemKind.INVALID_PAGE] and (
            ProblemKind.INVALID_PAGE
        )
        for bad in (
            {size_name: "-1"},
            {size_name: "two"},
            {position_name: "%%%"},
            {position_name: unknown},
            {position_name: repairable},
        ):
            response = await page(**bad)
            assert response.status_code == 400
            assert normalize_error(profile, response).kind is expected_kind
            assert lint_exchange(profile, response) == []
    many = [Report(f"rpt_{n:03d}", "bulk", "00", T0 + timedelta(hours=n)) for n in range(1, 102)]
    app = create_app(profile, many, now=lambda: CREATED)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        capped = await client.get(
            profile.collection_path, params={size_name: "1000", **query}, headers=headers
        )
        everything, requests = await list_all(client, profile, page_size=1000)
    assert len(capped.json()[items_key]) == 100
    assert (len(everything), requests) == (101, 2)


@pytest.mark.unit
@pytest.mark.asyncio
@ALL_GUIDES
async def test_page_positions_survive_ids_that_need_the_url_safe_alphabet(
    profile: StyleProfile,
) -> None:
    # "ab~" and "ab?" encode to "YWJ-" and "YWI_": the two characters base64url substitutes.
    odd = [Report(report_id, "odd", "00", T0) for report_id in ("ab~", "ab?", "abc")]
    app = create_app(profile, odd, now=lambda: CREATED)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        reports, requests = await list_all(client, profile, page_size=1)
        if profile is GOOGLE:
            first = await client.get("/v1/reports", params={"pageSize": "1"})
            assert first.json()["nextPageToken"] == "YWJ-"
    assert reports == (odd[::-1] if profile is STRIPE else odd)
    assert requests == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_google_version_is_a_path_segment_and_nothing_else() -> None:
    async with client_for(GOOGLE) as client:
        assert (await client.get("/v1/reports")).status_code == 200
        for path in ("/v2/reports", "/reports"):
            missing = await client.get(path)
            assert missing.status_code == 404
            assert missing.json()["error"]["status"] == "NOT_FOUND"
        assert "vary" not in (await client.get("/v1/reports")).headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_azure_requires_exactly_one_supported_api_version_query_parameter() -> None:
    async with client_for(AZURE) as client:
        absent = await client.get("/reports")
        unknown = await client.get("/reports", params={"api-version": "2019-01-01"})
        repeated = await client.get("/reports?api-version=2026-06-01&api-version=2026-06-01")
        good = await client.get("/reports", params={"api-version": "2026-06-01"})
        unversioned_write = await client.post("/reports", json={"displayName": "Q3"})
    assert (absent.status_code, absent.headers["x-ms-error-code"]) == (
        400,
        "MissingApiVersionParameter",
    )
    assert absent.json()["error"]["code"] == "MissingApiVersionParameter"
    assert unknown.headers["x-ms-error-code"] == "UnsupportedApiVersionValue"
    assert repeated.headers["x-ms-error-code"] == "UnsupportedApiVersionValue"
    assert unversioned_write.headers["x-ms-error-code"] == "MissingApiVersionParameter"
    assert good.status_code == 200
    assert "vary" not in good.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zalando_negotiates_the_version_by_media_type_and_defaults_when_absent() -> None:
    versioned = f"{MEDIA_TYPE};version=2"
    async with client_for(ZALANDO) as client:
        default = await client.get("/reports")
        explicit = await client.get("/reports", headers={"accept": f"text/html, {versioned};q=0.9"})
        generic = await client.get("/reports", headers={"accept": "application/json"})
        old = await client.get("/reports", headers={"accept": f"{MEDIA_TYPE}; Version=1"})
        url_version = await client.get("/v1/reports")
        two_lines = await client.get(
            "/reports",
            headers=[("accept", f"{MEDIA_TYPE};version=1"), ("accept", f"{MEDIA_TYPE};version=2")],
        )
        statuses = {
            accept: (await client.get("/reports", headers={"accept": accept})).status_code
            for accept in (
                f"{MEDIA_TYPE};version=1, {MEDIA_TYPE};version=2",
                f"{MEDIA_TYPE};version=1, */*;q=0.1",
                f"{MEDIA_TYPE};version=1;q=0, {MEDIA_TYPE};version=2",
                f"{MEDIA_TYPE};version=1, {MEDIA_TYPE}",
                f"{MEDIA_TYPE};version=1, application/*",
                f'{MEDIA_TYPE};version="2"',
                f"{MEDIA_TYPE};version=1, {MEDIA_TYPE};version=2;q=0",
                f"{MEDIA_TYPE};version=1, {MEDIA_TYPE};version=2;q=0.000",
                f"{MEDIA_TYPE};version=1, */*;q=0",
                f"{MEDIA_TYPE.upper()};version=1",
                f"{MEDIA_TYPE};version=1, Application/*",
                f"{MEDIA_TYPE};version=1, {MEDIA_TYPE};version=2;q=0.0000",
            )
        }
    for response in (default, explicit, generic):
        assert response.status_code == 200
        assert response.headers["content-type"] == versioned
        assert response.headers["vary"] == "Accept"
    # Any acceptable range that names version 2 (as a token or a quoted string), the bare media
    # type, or a wildcard is enough; a range with a weight of 0 is "not acceptable" and does not
    # count, and the media type name matches in any case. Lab policy for the last one: four
    # digits are not a qvalue (RFC 9110 Section 12.4.2), so that range is not read as refused.
    assert list(statuses.values()) == [200, 200, 200, 200, 200, 200, 406, 406, 406, 406, 200, 200]
    # Two Accept field lines are one list (RFC 9110 Section 5.3), so the second line counts.
    assert two_lines.status_code == 200
    assert old.status_code == 406
    assert old.headers["content-type"] == "application/problem+json"
    assert old.json()["type"] == "/problems/unsupported-version"
    assert url_version.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stripe_header_selects_the_version_and_absence_means_the_account_default() -> None:
    async with client_for(STRIPE) as client:
        default = await client.get("/v1/reports")
        pinned = await client.get("/v1/reports", headers={"Stripe-Version": "2026-10-28.endive"})
        unknown = await client.get("/v1/reports", headers={"stripe-version": "2011-01-01"})
    assert default.status_code == pinned.status_code == 200
    assert default.json() == pinned.json()
    assert pinned.headers["vary"] == "Stripe-Version"
    assert unknown.status_code == 400
    assert unknown.json()["error"]["type"] == "invalid_request_error"
    assert "code" not in unknown.json()["error"]


ERROR_SHAPES: dict[str, dict[str, Any]] = {
    "google-aip": {
        "error": {
            "code": 400,
            "message": "'sha256Hash' is required.",
            "status": "INVALID_ARGUMENT",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "FIELD_REQUIRED",
                    "domain": "reports.lab.example",
                    "metadata": {"field": "sha256Hash"},
                },
                {
                    "@type": "type.googleapis.com/google.rpc.BadRequest",
                    "fieldViolations": [
                        {"field": "sha256Hash", "description": "'sha256Hash' is required."}
                    ],
                },
            ],
        }
    },
    "azure": {
        "error": {
            "code": "MissingRequiredField",
            "message": "'sha256Hash' is required.",
            "target": "sha256Hash",
        }
    },
    "zalando": {
        "type": "/problems/missing-field",
        "title": "Missing required field",
        "status": 400,
        "detail": "'sha256_hash' is required.",
        "errors": [{"detail": "'sha256_hash' is required.", "pointer": "#/sha256_hash"}],
    },
    "stripe-v1": {
        "error": {
            "type": "invalid_request_error",
            "message": "'sha256_hash' is required.",
            "code": "parameter_missing",
            "param": "sha256_hash",
        }
    },
}


@pytest.mark.unit
@pytest.mark.asyncio
@ALL_GUIDES
async def test_one_failure_has_four_envelopes_and_one_normalized_meaning(
    profile: StyleProfile,
) -> None:
    async with client_for(profile) as client:
        response = await create(client, profile, wire_members(profile, display_name="Q3"))
    assert response.status_code == 400
    assert response.json() == ERROR_SHAPES[profile.name]
    expected_type = "application/problem+json" if profile is ZALANDO else "application/json"
    assert response.headers["content-type"] == expected_type
    assert response.headers.get("x-ms-error-code") == (
        "MissingRequiredField" if profile is AZURE else None
    )
    error = normalize_error(profile, response)
    assert (error.status, error.kind, error.field) == (
        400,
        ProblemKind.MISSING_FIELD,
        "sha256_hash",
    )
    assert error.pointer == profile.names.by_canonical["sha256_hash"]


@pytest.mark.unit
@pytest.mark.asyncio
@ALL_GUIDES
async def test_create_accepts_wire_names_in_the_profile_request_encoding(
    profile: StyleProfile,
) -> None:
    members = wire_members(profile, display_name="Q3", sha256_hash="ab" * 32)
    async with client_for(profile) as client:
        response = await create(client, profile, members)
        reports, _ = await list_all(client, profile, page_size=50)
    expected_encoding = (
        "application/x-www-form-urlencoded" if profile is STRIPE else "application/json"
    )
    assert response.request.headers["content-type"] == expected_encoding
    assert response.status_code == 201
    assert lint_exchange(profile, response) == []
    created = Report("rpt_006", "Q3", "ab" * 32, CREATED)
    assert parse_report(profile, response.json()) == created
    assert created in reports
    assert len(reports) == 6


@pytest.mark.unit
def test_case_conversion_is_not_a_bijection_but_the_registry_is() -> None:
    # pydantic 2.14.3: forward conversion is fine, the reverse splits before the digit run.
    assert to_camel("sha256_hash") == "sha256Hash"
    assert to_snake("sha256Hash") == "sha_256_hash"
    lossy = [
        name
        for name in ("sha256_hash", "ipv4_address", "oauth2_token", "display_name", "create_time")
        if to_snake(to_camel(name)) != name
    ]
    assert lossy == ["sha256_hash", "ipv4_address", "oauth2_token"]
    # camelCase with an acronym does not survive the other direction either.
    assert to_camel(to_snake("userID")) == "userId"
    for profile in (GOOGLE, AZURE):
        # The registry agrees with the generator going out and disagrees with it coming back.
        wire = profile.names.by_canonical["sha256_hash"]
        assert wire == to_camel("sha256_hash")
        assert profile.names.by_wire[wire] == "sha256_hash" != to_snake(wire)
    for profile in GUIDES:
        names = profile.names
        assert [names.by_wire[names.by_canonical[field]] for field in names.by_canonical] == list(
            names.by_canonical
        )


@pytest.mark.unit
def test_pydantic_alias_models_accept_both_spellings_only_when_validate_by_name_is_on() -> None:
    class Lenient(BaseModel):
        model_config = ConfigDict(
            alias_generator=to_camel, validate_by_name=True, validate_by_alias=True
        )
        is_admin: bool = False

    class Strict(BaseModel):
        model_config = ConfigDict(alias_generator=to_camel, extra="forbid")
        is_admin: bool = False

    assert Lenient.model_validate({"is_admin": True}).is_admin is True
    # With both spellings present the alias wins, whatever the member order.
    assert Lenient.model_validate({"isAdmin": False, "is_admin": True}).is_admin is False
    assert Lenient.model_validate({"is_admin": True, "isAdmin": False}).is_admin is False
    assert Strict.model_validate({"isAdmin": True}).is_admin is True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Strict.model_validate({"is_admin": True})


@pytest.mark.unit
def test_wire_names_refuse_collisions_wrong_case_and_missing_fields() -> None:
    good = dict(AZURE.names.by_canonical)
    with pytest.raises(ValueError, match="share one wire name"):
        WireNames(Case.CAMEL, {**good, "display_name": "id"})
    with pytest.raises(ValueError, match="is not camelCase"):
        WireNames(Case.CAMEL, {**good, "sha256_hash": "sha256_hash"})
    with pytest.raises(ValueError, match="is not snake_case"):
        WireNames(Case.SNAKE, good)
    with pytest.raises(ValueError, match="cover exactly"):
        WireNames(Case.CAMEL, {"report_id": "id"})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_alternate_spellings_and_duplicate_members_are_rejected() -> None:
    both = {"displayName": "Q3", "sha256Hash": "ab" * 32, "sha256_hash": "cd" * 32}
    duplicate = b'{"displayName": "Q3", "sha256Hash": "aa", "sha256Hash": "bb"}'
    query, _ = version_parts(AZURE)
    async with client_for(AZURE) as client:
        alternate = await create(client, AZURE, both)
        output_only = await create(client, AZURE, {"id": "rpt_001", "displayName": "Q3"})
        repeated = await client.post("/reports", params=query, content=duplicate, headers=JSON)
        reports, _ = await list_all(client, AZURE, page_size=50)
    error = normalize_error(AZURE, alternate)
    # The canonical spelling is not a wire name in a camelCase API, so it is an unknown field.
    assert (error.kind, error.pointer, error.field) == (
        ProblemKind.UNKNOWN_FIELD,
        "sha256_hash",
        None,
    )
    assert normalize_error(AZURE, output_only).pointer == "id"
    assert normalize_error(AZURE, repeated).kind is ProblemKind.INVALID_BODY
    assert "duplicate member 'sha256Hash'" in repeated.json()["error"]["message"]
    assert reports == REPORTS
    async with client_for(STRIPE) as client:
        form = await client.post(
            "/v1/reports", content=b"display_name=a&display_name=b&sha256_hash=c", headers=FORM
        )
        unknown = await create(client, STRIPE, {"displayName": "Q3"})
        stored, _ = await list_all(client, STRIPE, page_size=50)
    assert form.status_code == 400
    assert form.json()["error"] == {
        "type": "invalid_request_error",
        "message": "duplicate member 'display_name'",
    }
    assert len(stored) == 5
    assert unknown.json()["error"]["code"] == "parameter_unknown"
    assert unknown.json()["error"]["param"] == "displayName"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "pointer"),
    [
        (b"{not json", None),
        (b"[1, 2]", None),
        (b'{"displayName": 7}', "displayName"),
        (b'{"displayName": null}', "displayName"),
        (b"[" * 100_000, None),
        (b"\xff\xfe", None),
    ],
    ids=[
        "malformed",
        "not-an-object",
        "wrong-type",
        "null-is-not-a-string",
        "nested-too-deeply",
        "not-utf-8",
    ],
)
async def test_invalid_bodies_are_400_in_the_profile_envelope(
    body: bytes, pointer: str | None
) -> None:
    async with client_for(GOOGLE) as client:
        response = await client.post("/v1/reports", content=body, headers=JSON)
    error = normalize_error(GOOGLE, response)
    assert (error.status, error.kind, error.pointer) == (400, ProblemKind.INVALID_BODY, pointer)
    assert lint_exchange(GOOGLE, response) == []


@pytest.mark.unit
@pytest.mark.asyncio
@ALL_GUIDES
async def test_request_encoding_is_enforced_with_415_in_the_profile_envelope(
    profile: StyleProfile,
) -> None:
    wrong = JSON if profile is STRIPE else FORM
    body = b"{}" if profile is STRIPE else b"displayName=Q3"
    query, headers = version_parts(profile)
    async with client_for(profile) as client:
        response = await client.post(
            profile.collection_path, params=query, headers={**headers, **wrong}, content=body
        )
        bare = await client.post(profile.collection_path, params=query, headers=headers)
    assert response.status_code == bare.status_code == 415
    expected_kind = None if profile is STRIPE else ProblemKind.UNSUPPORTED_MEDIA_TYPE
    assert normalize_error(profile, response).kind is expected_kind
    # The response is in the house envelope; only the request broke the guide.
    assert lint_exchange(profile, response) == ["request-encoding"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_content_type_matches_as_a_media_type_and_blank_form_values_are_members() -> None:
    spelled = {"content-type": "Application/JSON; charset=utf-8"}
    body = b'{"displayName": "Q3", "sha256Hash": "ab"}'
    query, _ = version_parts(AZURE)
    async with client_for(AZURE) as client:
        created = await client.post("/reports", params=query, content=body, headers=spelled)
    # Type and subtype are case-insensitive and parameters may follow (RFC 9110 Section 8.3.1).
    assert created.status_code == 201
    assert lint_exchange(AZURE, created) == []
    async with client_for(STRIPE) as client:
        blank = await client.post(
            "/v1/reports", content=b"display_name=&sha256_hash=ab", headers=FORM
        )
    # A blank form value is a present member holding an empty string, not an absent member.
    assert blank.status_code == 201
    assert blank.json()["display_name"] == ""


@pytest.mark.unit
@pytest.mark.asyncio
@ALL_GUIDES
async def test_wrong_method_is_405_with_allow_in_the_profile_envelope(
    profile: StyleProfile,
) -> None:
    query, headers = version_parts(profile)
    async with client_for(profile) as client:
        response = await client.delete(profile.collection_path, params=query, headers=headers)
    assert response.status_code == 405
    assert {method.strip() for method in response.headers["allow"].split(",")} >= {"GET", "POST"}
    assert lint_exchange(profile, response) == []
    expected_kind = None if profile is STRIPE else ProblemKind.METHOD_NOT_ALLOWED
    assert normalize_error(profile, response).kind is expected_kind


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile", "vary"),
    [(GOOGLE, None), (AZURE, None), (ZALANDO, "Accept"), (STRIPE, "Stripe-Version")],
    ids=list(BY_NAME),
)
async def test_error_responses_carry_the_same_vary_as_successes(
    profile: StyleProfile, vary: str | None
) -> None:
    query, headers = version_parts(profile)
    async with client_for(profile) as client:
        responses = [
            await client.get("/nowhere", headers=headers),
            await client.delete(profile.collection_path, params=query, headers=headers),
            await create(client, profile, {}),
        ]
    # 404 and 405 are heuristically cacheable (RFC 9110 Section 15.1), so an error selected by a
    # request header needs the same Vary as the success it replaces.
    assert [response.status_code for response in responses] == [404, 405, 400]
    assert [response.headers.get("vary") for response in responses] == [vary, vary, vary]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "content", "content_type"),
    [
        (502, b"<html>Bad Gateway</html>", "text/html"),
        (404, b"Not Found", "text/plain"),
        (500, b'"boom"', "application/json"),
        (500, b'{"error": "boom"}', "application/json"),
        (503, b"", "text/plain"),
        (400, b'{"error": {"code": ["x"], "param": ["y"]}}', "application/json"),
    ],
    ids=[
        "proxy-html",
        "framework-text",
        "json-string",
        "error-is-a-string",
        "empty",
        "members-of-the-wrong-type",
    ],
)
def test_errors_from_other_layers_are_findings_and_normalize_without_a_kind(
    status: int, content: bytes, content_type: str
) -> None:
    for profile in GUIDES:
        query, headers = version_parts(profile)
        url = httpx.URL(f"http://test{profile.collection_path}", params=query)
        response = httpx.Response(
            status,
            content=content,
            headers={"content-type": content_type},
            request=httpx.Request("GET", url, headers=headers),
        )
        error = normalize_error(profile, response)
        assert (error.status, error.kind, error.field) == (status, None, None)
        assert lint_exchange(profile, response) == ["error-envelope"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recombined_profiles_still_answer_in_an_envelope() -> None:
    google_errors = replace(ZALANDO, name="mixed", error_style=ErrorStyle.GOOGLE_STATUS)
    async with client_for(google_errors) as client:
        old = await client.get("/reports", headers={"accept": f"{MEDIA_TYPE};version=1"})
    # google.rpc.Code has no value for 406, so the lab reports UNKNOWN rather than crash.
    assert old.status_code == 406
    assert (old.json()["error"]["code"], old.json()["error"]["status"]) == (406, "UNKNOWN")
    assert normalize_error(google_errors, old).kind is ProblemKind.UNSUPPORTED_VERSION


@pytest.mark.unit
@pytest.mark.asyncio
async def test_problem_json_pointer_is_an_escaped_rfc_6901_fragment() -> None:
    member = "a/b~c d~1e"
    async with client_for(ZALANDO) as client:
        response = await create(client, ZALANDO, {member: "x"})
    # "~1" in the name itself becomes "~01"; unescaping "~0" before "~1" would read it as "/".
    assert response.json()["errors"][0]["pointer"] == "#/a~1b~0c%20d~01e"
    error = normalize_error(ZALANDO, response)
    assert (error.kind, error.pointer, error.field) == (ProblemKind.UNKNOWN_FIELD, member, None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_store_ids_stay_unique_and_times_are_whole_aware_seconds() -> None:
    duplicate = [REPORTS[0], REPORTS[0]]
    with pytest.raises(ValueError, match="ids must be unique"):
        create_app(GOOGLE, duplicate, now=lambda: CREATED)
    with pytest.raises(ValueError, match="timezone-aware"):
        Report("rpt_001", "naive", "00", datetime(2026, 1, 1))
    seeded = [Report("rpt_002", "seed", "00", T0)]
    fractional = CREATED.replace(microsecond=500_000)
    for profile in (GOOGLE, STRIPE):
        app = create_app(profile, seeded, now=lambda: fractional)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            members = wire_members(profile, display_name="Q3", sha256_hash="ab")
            first = await create(client, profile, members)
            second = await create(client, profile, members)
            reports, _ = await list_all(client, profile, page_size=1)
        # rpt_002 is taken, so the first new id skips it; sub-second precision is dropped.
        assert parse_report(profile, first.json()) == Report("rpt_003", "Q3", "ab", CREATED)
        assert parse_report(profile, second.json()).report_id == "rpt_004"
        assert sorted(report.report_id for report in reports) == ["rpt_002", "rpt_003", "rpt_004"]


LINT_MATRIX = {
    "google-aip": {
        "google-aip": [],
        "azure": ["error-envelope", "page-envelope", "version-carrier"],
        "zalando": ["error-envelope", "field-case", "page-envelope", "vary", "version-carrier"],
        "stripe-v1": [
            "error-envelope",
            "field-case",
            "page-envelope",
            "request-encoding",
            "vary",
        ],
    },
    "azure": {
        "google-aip": ["error-envelope", "page-envelope", "version-carrier"],
        "azure": [],
        "zalando": ["error-envelope", "field-case", "page-envelope", "vary", "version-carrier"],
        "stripe-v1": [
            "error-envelope",
            "field-case",
            "page-envelope",
            "request-encoding",
            "vary",
            "version-carrier",
        ],
    },
    "zalando": {
        "google-aip": ["error-envelope", "field-case", "page-envelope", "version-carrier"],
        "azure": [
            "error-envelope",
            "field-case",
            "link-carries-version",
            "page-envelope",
            "version-carrier",
        ],
        "zalando": [],
        "stripe-v1": [
            "error-envelope",
            "page-envelope",
            "request-encoding",
            "vary",
            "version-carrier",
        ],
    },
    "stripe-v1": {
        "google-aip": ["error-envelope", "field-case", "page-envelope", "request-encoding"],
        "azure": [
            "error-envelope",
            "field-case",
            "page-envelope",
            "request-encoding",
            "version-carrier",
        ],
        "zalando": [
            "error-envelope",
            "page-envelope",
            "request-encoding",
            "vary",
            "version-carrier",
        ],
        "stripe-v1": [],
    },
}


@pytest.mark.unit
@pytest.mark.asyncio
@ALL_GUIDES
async def test_each_dialect_is_clean_under_its_own_profile_and_fails_the_other_three(
    profile: StyleProfile,
) -> None:
    async with client_for(profile) as client:
        page = await first_page(client, profile)
        failure = await create(client, profile, {})
        missing = await client.get("/nowhere")
    row = {
        judge.name: sorted({*lint_exchange(judge, page), *lint_exchange(judge, failure)})
        for judge in GUIDES
    }
    assert row == LINT_MATRIX[profile.name]
    # The framework's own 404 goes through the same envelope, so it is clean too.
    assert missing.status_code == 404
    assert "error-envelope" not in lint_exchange(profile, missing)


def exchange(
    url: str,
    body: dict[str, Any],
    *,
    status: int = 200,
    request_headers: dict[str, str] | None = None,
    response_headers: dict[str, str] | None = None,
) -> httpx.Response:
    request = httpx.Request("GET", url, headers=request_headers)
    return httpx.Response(status, json=body, headers=response_headers, request=request)


AZURE_URL = "http://test/reports?api-version=2026-06-01"
AZURE_ITEM = WIRE_SHAPES["azure"]
STRIPE_PAGE: dict[str, Any] = {
    "object": "list",
    "url": "/v1/reports",
    "has_more": False,
    "data": [],
}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("profile", "response", "finding"),
    [
        (AZURE, exchange(AZURE_URL, {"value": [AZURE_ITEM]}), None),
        (AZURE, exchange(AZURE_URL, {"value": [{"display_name": "x"}]}), "field-case"),
        (AZURE, exchange(AZURE_URL, {"value": [], "count": 5}), "page-envelope"),
        (AZURE, exchange(AZURE_URL, {"items": []}), "page-envelope"),
        (AZURE, exchange(AZURE_URL, {"nextLink": AZURE_URL}), "page-envelope"),
        (
            AZURE,
            exchange(AZURE_URL, {"value": [], "nextLink": "http://test/reports?maxpagesize=2"}),
            "link-carries-version",
        ),
        (
            AZURE,
            exchange("http://test/v1/reports?api-version=2026-06-01", {"value": []}),
            "version-carrier",
        ),
        (AZURE, exchange("http://test/reports", {"value": []}), "version-carrier"),
        (
            AZURE,
            exchange(
                AZURE_URL,
                {"error": {"code": "NotFound", "message": "gone"}},
                status=404,
                response_headers={"x-ms-error-code": "ResourceNotFound"},
            ),
            "error-envelope",
        ),
        (STRIPE, exchange("http://test/v1/reports", STRIPE_PAGE), "vary"),
        (
            STRIPE,
            exchange(
                "http://test/v1/reports",
                STRIPE_PAGE,
                response_headers={"vary": "Accept-Encoding, stripe-version"},
            ),
            None,
        ),
        (
            STRIPE,
            exchange("http://test/v1/reports", {"error": {"type": "oops"}}, status=400),
            "error-envelope",
        ),
        (GOOGLE, exchange("http://test/v1beta/reports", {"reports": []}), "version-carrier"),
        (
            GOOGLE,
            exchange(
                "http://test/v1/reports",
                {"error": {"code": 404, "message": "m", "status": "NOT_FOUND", "details": []}},
                status=404,
            ),
            "error-envelope",
        ),
        (
            ZALANDO,
            exchange(
                "http://test/reports",
                {"type": "/problems/x", "status": 400},
                status=409,
                response_headers={"content-type": "application/problem+json"},
            ),
            "error-envelope",
        ),
        (
            ZALANDO,
            exchange("http://test/reports", {"type": "/problems/x", "status": 400}, status=400),
            "error-envelope",
        ),
        (
            GOOGLE,
            exchange(
                "http://test/v1/reports",
                {
                    "error": {
                        "code": 404,
                        "message": "m",
                        "details": [{"@type": ERROR_INFO_TYPE, "reason": "GONE", "domain": "d"}],
                    }
                },
                status=404,
            ),
            "error-envelope",
        ),
        (AZURE, exchange(AZURE_URL, {"error": {"message": "m"}}, status=400), "error-envelope"),
        (
            STRIPE,
            exchange(
                "http://test/v1/reports",
                {"error": {"type": "invalid_request_error", "message": "m"}},
                status=400,
                response_headers={"content-type": "application/problem+json"},
            ),
            "error-envelope",
        ),
        (
            GOOGLE,
            exchange(
                "http://test/v1/reports",
                {
                    "error": {
                        "code": 5,
                        "message": "m",
                        "status": "NOT_FOUND",
                        "details": [{"@type": ERROR_INFO_TYPE, "reason": "GONE", "domain": "d"}],
                    }
                },
                status=404,
            ),
            "error-envelope",
        ),
        (
            AZURE,
            exchange("http://test/v8-engines?api-version=2026-06-01", {"value": []}),
            None,
        ),
        (
            AZURE,
            exchange(
                AZURE_URL,
                {"error": {"code": "NotFound", "message": "gone"}},
                status=404,
                response_headers={
                    "x-ms-error-code": "NotFound",
                    "content-type": "Application/JSON; charset=utf-8",
                },
            ),
            None,
        ),
        (
            ZALANDO,
            exchange(
                "http://test/reports",
                {"type": "/problems/x", "status": 400},
                status=400,
                response_headers={"content-type": "application/problem+json ;charset=utf-8"},
            ),
            None,
        ),
    ],
    ids=[
        "azure-clean",
        "snake-key-in-camel-api",
        "extra-envelope-member",
        "foreign-items-member",
        "required-envelope-member-missing",
        "next-link-drops-api-version",
        "path-version-beside-query-version",
        "query-version-missing",
        "error-header-and-body-code-differ",
        "header-version-without-vary",
        "vary-names-are-case-insensitive",
        "undocumented-stripe-error-type",
        "path-version-is-not-the-profile-version",
        "google-error-without-error-info",
        "problem-status-differs-from-http-status",
        "problem-body-served-as-plain-json",
        "google-error-without-status-name",
        "azure-error-without-code-or-header",
        "stripe-error-served-as-problem-json",
        "google-code-is-the-grpc-number-not-the-http-status",
        "collection-name-that-starts-with-v-and-a-digit",
        "error-media-type-in-another-case-with-a-parameter",
        "problem-media-type-with-a-parameter",
    ],
)
def test_linter_reports_exactly_the_axis_that_deviates(
    profile: StyleProfile, response: httpx.Response, finding: str | None
) -> None:
    assert lint_exchange(profile, response) == ([] if finding is None else [finding])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("base", [STRIPE, ZALANDO], ids=["header", "media-type"])
async def test_a_required_header_or_media_type_version_is_resent_on_every_page(
    base: StyleProfile,
) -> None:
    # Stripe /v2 requires its header on every request; a link cannot carry it, so the client must.
    strict = replace(base, name="strict", absent_version=AbsentVersion.REJECT)
    async with client_for(strict) as client:
        bare = await client.get(strict.collection_path)
        reports, requests = await list_all(client, strict, page_size=2)
    assert bare.status_code == 400
    expected_kind = None if base is STRIPE else ProblemKind.MISSING_VERSION
    assert normalize_error(strict, bare).kind is expected_kind
    assert lint_exchange(strict, bare) == []
    assert (len(reports), requests) == (5, 3)


@pytest.mark.unit
def test_azure_error_code_is_read_from_the_header_when_the_body_disagrees() -> None:
    response = exchange(
        AZURE_URL,
        {"error": {"code": "SomethingElse", "message": "gone"}},
        status=404,
        response_headers={"x-ms-error-code": "NotFound"},
    )
    # The header is the code of record for the client; the mismatch itself is a lint finding.
    assert normalize_error(AZURE, response).kind is ProblemKind.NOT_FOUND
    assert lint_exchange(AZURE, response) == ["error-envelope"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_query_versioning_with_links_that_omit_the_version_breaks_on_page_two() -> None:
    async with client_for(HYBRID) as client:
        page = await client.get("/reports", params={"limit": "2", "api-version": "2026-06-01"})
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await list_all(client, HYBRID, page_size=2)
    # Page one is fine and every single axis is a copy of a published rule...
    assert page.status_code == 200
    assert page.json()["next"] == "http://test/reports?limit=2&cursor=cnB0XzAwMg"
    # ...but the link rule and the version rule do not compose, and only the linter says so.
    assert lint_exchange(HYBRID, page) == ["link-carries-version"]
    failed = caught.value.response
    assert failed.request.url == "http://test/reports?limit=2&cursor=cnB0XzAwMg"
    assert normalize_error(HYBRID, failed).kind is ProblemKind.MISSING_VERSION
    assert failed.headers["content-type"] == "application/problem+json"

    coherent = replace(HYBRID, links_carry_version=True)
    async with client_for(coherent) as client:
        page = await client.get("/reports", params={"limit": "2", "api-version": "2026-06-01"})
        reports, requests = await list_all(client, coherent, page_size=2)
    assert lint_exchange(coherent, page) == []
    assert (reports, requests) == (REPORTS, 3)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_default_version_turns_the_same_defect_into_a_silent_unpin() -> None:
    # The hybrid again, but an absent version now means "the default" instead of 400.
    unpinned = replace(HYBRID, name="unpinned", absent_version=AbsentVersion.DEFAULT)
    async with client_for(unpinned) as client:
        page = await client.get("/reports", params={"limit": "2", "api-version": "2026-06-01"})
        second = await client.get(page.json()["next"])
        reports, requests = await list_all(client, unpinned, page_size=2)
    # Nothing fails: page two is served, without the version the client pinned on page one.
    assert (second.status_code, reports, requests) == (200, REPORTS, 3)
    assert "api-version" in page.request.url.params
    assert "api-version" not in second.request.url.params
    # Both checks still name the defect, the static one before any request is made.
    assert profile_conflicts(unpinned) == ["links-drop-query-version"]
    assert lint_exchange(unpinned, page) == ["link-carries-version"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_links_carry_version_is_inert_without_a_query_carrier() -> None:
    inert = replace(ZALANDO, name="inert", links_carry_version=True)
    async with client_for(inert) as client:
        page = await first_page(client, inert)
    # A media type version cannot ride in a URL, so the flag changes nothing on the wire.
    assert page.json()["next"] == "http://test/reports?limit=2&cursor=cnB0XzAwMg"
    assert profile_conflicts(inert) == ["links-carry-version-is-inert"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("profile", "conflicts"),
    [
        (GOOGLE, []),
        (AZURE, []),
        (ZALANDO, []),
        (STRIPE, []),
        (HYBRID, ["links-drop-query-version"]),
        (replace(HYBRID, links_carry_version=True), []),
        (replace(AZURE, links_carry_version=False), ["links-drop-query-version"]),
        (replace(AZURE, page_style=PageStyle.PAGE_TOKEN), ["links-carry-version-is-inert"]),
        (replace(STRIPE, links_carry_version=True), ["links-carry-version-is-inert"]),
        (replace(GOOGLE, path_prefix="/v2"), ["version-carrier-incomplete"]),
        (replace(GOOGLE, version_carrier=VersionCarrier.QUERY), ["version-carrier-incomplete"]),
        (replace(ZALANDO, page_style=PageStyle.NEXT_LINK), ["envelope-case"]),
        (replace(ZALANDO, page_style=PageStyle.PAGE_TOKEN), ["envelope-case"]),
        (replace(ZALANDO, error_style=ErrorStyle.GOOGLE_STATUS), ["envelope-case"]),
        (replace(ZALANDO, error_style=ErrorStyle.AZURE_ENVELOPE), []),
        (replace(AZURE, error_style=ErrorStyle.PROBLEM_JSON), []),
        (replace(AZURE, error_style=ErrorStyle.STRIPE_ERROR), ["envelope-case"]),
        (
            replace(GOOGLE, page_style=PageStyle.OBJECT_ID, links_carry_version=True),
            ["envelope-case", "links-carry-version-is-inert"],
        ),
    ],
    ids=[
        "google-aip",
        "azure",
        "zalando",
        "stripe-v1",
        "query-version-with-zalando-links",
        "the-hybrid-repaired",
        "azure-with-the-link-rule-switched-off",
        "flag-set-on-a-page-style-without-links",
        "flag-set-on-a-header-carrier",
        "path-carrier-whose-prefix-is-another-version",
        "query-carrier-without-a-parameter-name",
        "snake-case-with-next-link",
        "snake-case-with-next-page-token",
        "snake-case-with-field-violations",
        "snake-case-with-the-azure-error",
        "camel-case-with-problem-details",
        "camel-case-with-decline-code",
        "two-conflicts-at-once",
    ],
)
def test_profile_conflicts_names_each_cross_axis_rule_a_profile_breaks(
    profile: StyleProfile, conflicts: list[str]
) -> None:
    assert profile_conflicts(profile) == conflicts


VERSION_AXES = ("path_prefix", "version_carrier", "version_name", "version", "absent_version")


def recombine(
    names: StyleProfile, version: StyleProfile, page: StyleProfile, errors: StyleProfile
) -> StyleProfile:
    """One axis group from each guide; links carry the version exactly when they can."""
    carrier = version.version_carrier
    version_axes: dict[str, Any] = {axis: getattr(version, axis) for axis in VERSION_AXES}
    return replace(
        names,
        name=f"{names.name}+{version.name}+{page.name}+{errors.name}",
        page_style=page.page_style,
        links_carry_version=carrier is VersionCarrier.QUERY and page.page_style in LINK_STYLES,
        error_style=errors.error_style,
        **version_axes,
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "page"),
    list(product(GUIDES, GUIDES)),
    ids=[f"{version.name}-version+{page.name}-pages" for version, page in product(GUIDES, GUIDES)],
)
async def test_every_recombination_of_the_four_guides_pages_and_reports_the_same_error(
    version: StyleProfile, page: StyleProfile
) -> None:
    for names, errors in product(GUIDES, GUIDES):
        profile = recombine(names, version, page, errors)
        async with client_for(profile) as client:
            first = await first_page(client, profile)
            failure = await create(client, profile, wire_members(profile, display_name="Q3"))
            reports, requests = await list_all(client, profile, page_size=2)
        assert (reports, requests) == (REPORTS[::-1] if names is STRIPE else REPORTS, 3), profile
        error = normalize_error(profile, failure)
        assert (error.status, error.kind, error.field) == (
            400,
            ProblemKind.MISSING_FIELD,
            "sha256_hash",
        ), profile
        # With the link rule satisfied, the only thing a recombination can still break is its own
        # case rule, by serving an envelope whose member names are in the other case.
        conflicts = profile_conflicts(profile)
        findings = {*lint_exchange(profile, first), *lint_exchange(profile, failure)}
        assert set(conflicts) <= {"envelope-case"}, profile
        assert findings <= {"field-case"}, profile
        assert conflicts or not findings, profile
