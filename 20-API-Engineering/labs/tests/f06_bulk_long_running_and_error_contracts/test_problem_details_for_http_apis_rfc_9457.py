"""RFC 9457 problem details: generator, registry, consumer, and framework behavior."""

import http
import http.client
import json
import sys
from typing import Any, cast

import httpx
import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from litestar import Litestar, get
from litestar.exceptions import NotFoundException
from litestar.plugins.problem_details import (
    ProblemDetailsConfig,
    ProblemDetailsException,
    ProblemDetailsPlugin,
)

from api_labs.f06_bulk_long_running_and_error_contracts.problem_details import (
    ABOUT_BLANK,
    OUT_OF_CREDIT,
    PROBLEM_JSON,
    VALIDATION_ERROR,
    Problem,
    ProblemParseError,
    ProblemRegistry,
    ProblemType,
    about_blank,
    build_app,
    is_json_media_type,
    json_pointer_fragment,
    parse_problem,
)

# RFC 9457 Appendix A (non-normative JSON Schema), with the descriptions removed.
APPENDIX_A_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "type": {"type": "string", "format": "uri-reference"},
        "title": {"type": "string"},
        "status": {"type": "integer", "minimum": 100, "maximum": 599},
        "detail": {"type": "string"},
        "instance": {"type": "string", "format": "uri-reference"},
    },
}

RFC_OUT_OF_CREDIT = {
    "type": "https://example.com/probs/out-of-credit",
    "title": "You do not have enough credit.",
    "detail": "Your current balance is 30, but that costs 50.",
    "instance": "/account/12345/msgs/abc",
    "balance": 30,
    "accounts": ["/account/12345", "/account/67890"],
}


def lab_client(*, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
    registry = ProblemRegistry()
    registry.register(OUT_OF_CREDIT)
    registry.register(VALIDATION_ERROR)
    transport = httpx.ASGITransport(
        app=build_app(registry), raise_app_exceptions=raise_app_exceptions
    )
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_out_of_credit_matches_rfc_9457_section_3_example() -> None:
    async with lab_client() as client:
        response = await client.post(
            "/purchase",
            json={"item": 123456, "quantity": 2},
            headers={"Accept": "application/json"},
        )

    assert response.status_code == 403
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.headers["content-language"] == "en"
    body = response.json()
    assert body == {**RFC_OUT_OF_CREDIT, "status": 403}
    assert body["status"] == response.status_code
    assert list(body)[:3] == ["type", "title", "status"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validation_errors_extension_uses_json_pointer_fragments() -> None:
    async with lab_client() as client:
        response = await client.post("/details", json={"age": 42.3, "profile": {"color": "yellow"}})

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json() == {
        "type": "https://example.net/validation-error",
        "title": "Your request is not valid.",
        "status": 422,
        "errors": [
            {"detail": "must be a positive integer", "pointer": "#/age"},
            {"detail": "must be 'green', 'red' or 'blue'", "pointer": "#/profile/color"},
        ],
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valid_details_payload_is_not_a_problem() -> None:
    async with lab_client() as client:
        response = await client.post("/details", json={"age": 42, "profile": {"color": "red"}})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "pointers"),
    [
        ([42], ["#/age", "#/profile/color"]),
        ({"age": True, "profile": {"color": "red"}}, ["#/age"]),
        ({"age": 7, "profile": "red"}, ["#/profile/color"]),
    ],
)
async def test_details_reports_one_pointer_per_failing_field(
    payload: object, pointers: list[str]
) -> None:
    async with lab_client() as client:
        response = await client.post("/details", json=payload)

    assert response.status_code == 422
    assert [error["pointer"] for error in response.json()["errors"]] == pointers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_exception_becomes_about_blank_with_occurrence_detail() -> None:
    async with lab_client() as client:
        response = await client.get("/items/7")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": ABOUT_BLANK,
        "title": "Not Found",
        "status": 404,
        "detail": "Item 7 does not exist",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_miss_drops_starlettes_autofilled_detail() -> None:
    async with lab_client() as client:
        response = await client.get("/no-such-route")

    assert response.status_code == 404
    assert response.json() == {"type": ABOUT_BLANK, "title": "Not Found", "status": 404}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_method_not_allowed_problem_keeps_allow_header() -> None:
    async with lab_client() as client:
        response = await client.delete("/items/7")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.json() == {"type": ABOUT_BLANK, "title": "Method Not Allowed", "status": 405}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unhandled_exception_body_leaks_nothing() -> None:
    async with lab_client(raise_app_exceptions=False) as client:
        response = await client.get("/boom")

    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json() == {"type": ABOUT_BLANK, "title": "Internal Server Error", "status": 500}
    assert b"hunter2" not in response.content
    assert b"postgres" not in response.content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_reraises_after_sending_the_500_problem() -> None:
    async with lab_client() as client:
        with pytest.raises(RuntimeError, match="hunter2"):
            await client.get("/boom")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_default_validation_error_is_not_problem_json() -> None:
    async with lab_client() as client:
        response = await client.get("/items/not-an-int")

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert set(body) == {"detail"}
    assert body["detail"][0]["loc"] == ["path", "item_id"]
    assert body["detail"][0]["input"] == "not-an-int"


@pytest.mark.unit
def test_about_blank_title_is_pinned_while_httpstatus_phrase_moved_in_3_13() -> None:
    assert about_blank(422).title == "Unprocessable Content"
    assert about_blank(413).title == "Content Too Large"
    assert about_blank(429).title == "Too Many Requests"
    stdlib_phrase = http.HTTPStatus(422).phrase
    if sys.version_info >= (3, 13):
        assert stdlib_phrase == "Unprocessable Content"
    else:
        assert stdlib_phrase == "Unprocessable Entity"


@pytest.mark.unit
def test_about_blank_title_does_not_read_the_stdlib_phrase_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(http.client.responses, 422, "Unprocessable Entity")
    assert about_blank(422).title == "Unprocessable Content"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("token", "fragment"),
    [
        ("foo", "#/foo"),
        ("", "#/"),
        ("a/b", "#/a~1b"),
        ("c%d", "#/c%25d"),
        ("e^f", "#/e%5Ef"),
        ("g|h", "#/g%7Ch"),
        ("i\\j", "#/i%5Cj"),
        ('k"l', "#/k%22l"),
        (" ", "#/%20"),
        ("m~n", "#/m~0n"),
        ("~1", "#/~01"),
    ],
)
def test_json_pointer_fragment_matches_rfc_6901_section_6(token: str, fragment: str) -> None:
    assert json_pointer_fragment([token]) == fragment


@pytest.mark.unit
def test_json_pointer_fragment_indexes_arrays_and_roots() -> None:
    assert json_pointer_fragment([]) == "#"
    assert json_pointer_fragment(["foo", 0]) == "#/foo/0"


@pytest.mark.unit
@pytest.mark.parametrize("name", ["status", "invalid-params", "ab", "1st", "type"])
def test_problem_type_rejects_extension_names_outside_section_4(name: str) -> None:
    with pytest.raises(ValueError, match="extension"):
        ProblemType("https://example.com/probs/x", "X", 400, frozenset({name}))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("uri", "status"),
    [("example-problem", 400), ("/types/123", 400), ("https://example.com/p", 600)],
)
def test_problem_type_requires_absolute_uri_and_valid_status(uri: str, status: int) -> None:
    with pytest.raises(ValueError, match=r"absolute|status"):
        ProblemType(uri, "X", status)


@pytest.mark.unit
def test_problem_type_requires_a_title() -> None:
    with pytest.raises(ValueError, match="title"):
        ProblemType("https://example.com/probs/x", "", 400)


@pytest.mark.unit
def test_registry_rejects_a_second_owner_of_a_type_uri() -> None:
    registry = ProblemRegistry()
    registry.register(OUT_OF_CREDIT)
    fork = ProblemType(OUT_OF_CREDIT.uri, "Out of credit", 402)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(fork)
    assert registry.get(OUT_OF_CREDIT.uri) is OUT_OF_CREDIT


@pytest.mark.unit
def test_problem_rejects_undeclared_extension_that_would_shadow_status() -> None:
    with pytest.raises(ValueError, match="undeclared"):
        Problem(OUT_OF_CREDIT, extensions={"status": 200})
    with pytest.raises(ValueError, match="undeclared"):
        Problem(OUT_OF_CREDIT, extensions={"credit_limit": 100})


@pytest.mark.unit
def test_consumer_ignores_mistyped_members_instead_of_failing() -> None:
    body = json.dumps({"type": 123, "title": 5, "status": True, "detail": ["x"], "balance": 30})
    parsed = parse_problem(body.encode(), base_uri="https://api.example.org/a", http_status=403)

    assert parsed.type == ABOUT_BLANK
    assert parsed.title is None
    assert parsed.status is None
    assert parsed.detail is None
    assert parsed.extensions == {"balance": 30}
    assert parsed.ignored == ("type", "status", "title", "detail")
    assert isinstance(True, int)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("404", 404), ("404.0", 404), ("404.5", None), ("99", None), ("600", None), ('"404"', None)],
)
def test_consumer_status_member_accepts_only_status_code_numbers(
    raw: str, expected: int | None
) -> None:
    body = ('{"status": ' + raw + "}").encode()
    parsed = parse_problem(body, base_uri="https://x.test/", http_status=404)
    assert parsed.status == expected
    assert ("status" in parsed.ignored) is (expected is None)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("base", "resolved"),
    [
        ("https://api.example.org/foo/bar/123", "https://api.example.org/foo/bar/example-problem"),
        ("https://api.example.org/widget/456", "https://api.example.org/widget/example-problem"),
    ],
)
def test_consumer_resolves_relative_type_per_section_3_1_1(base: str, resolved: str) -> None:
    parsed = parse_problem(b'{"type": "example-problem"}', base_uri=base, http_status=400)
    assert parsed.type == resolved


@pytest.mark.unit
@pytest.mark.parametrize(
    "uri", [ABOUT_BLANK, "tag:example@example.org,2021-09-17:OutOfLuck", "/types/123"]
)
def test_consumer_keeps_absolute_and_full_path_type_uris_stable_across_bases(uri: str) -> None:
    body = json.dumps({"type": uri}).encode()
    first = parse_problem(body, base_uri="https://api.example.org/foo/bar/123", http_status=400)
    second = parse_problem(body, base_uri="https://api.example.org/widget/456", http_status=400)
    assert first.type == second.type


@pytest.mark.unit
def test_absent_type_means_about_blank() -> None:
    parsed = parse_problem(b'{"title": "Not Found"}', base_uri="https://x.test/", http_status=404)
    assert parsed.type == ABOUT_BLANK
    assert parsed.ignored == ()


@pytest.mark.unit
def test_consumer_rejects_duplicate_member_names_that_stdlib_json_accepts() -> None:
    body = b'{"type": "https://example.com/probs/out-of-credit", "type": "about:blank"}'
    assert json.loads(body) == {"type": "about:blank"}
    with pytest.raises(ProblemParseError, match="duplicate"):
        parse_problem(body, base_uri="https://x.test/", http_status=403)


@pytest.mark.unit
@pytest.mark.parametrize("body", [b"[]", b'"error"', b"null"])
def test_consumer_rejects_non_object_documents(body: bytes) -> None:
    with pytest.raises(ProblemParseError, match="object"):
        parse_problem(body, base_uri="https://x.test/", http_status=500)


@pytest.mark.unit
def test_status_mismatch_flags_an_intermediary_rewrite() -> None:
    body = json.dumps({**RFC_OUT_OF_CREDIT, "status": 403}).encode()
    rewritten = parse_problem(body, base_uri="https://example.com/", http_status=502)
    original = parse_problem(body, base_uri="https://example.com/", http_status=403)
    absent = parse_problem(b"{}", base_uri="https://example.com/", http_status=502)

    assert rewritten.status_mismatch is True
    assert original.status_mismatch is False
    assert absent.status_mismatch is False


@pytest.mark.unit
def test_rfc_7807_validation_example_still_parses_under_9457_rules() -> None:
    body = json.dumps(
        {
            "type": "https://example.net/validation-error",
            "title": "Your request parameters didn't validate.",
            "invalid-params": [{"name": "age", "reason": "must be a positive integer"}],
        }
    ).encode()
    parsed = parse_problem(body, base_uri="https://example.net/", http_status=400)
    assert parsed.type == "https://example.net/validation-error"
    assert parsed.extensions == {
        "invalid-params": [{"name": "age", "reason": "must be a positive integer"}]
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("application/problem+json", True),
        ("Application/Problem+JSON; charset=utf-8", True),
        ("application/json", True),
        ("application/problem+xml", False),
        ("text/json", False),
        ("application/jsonx", False),
    ],
)
def test_json_media_type_detection_covers_the_suffix(content_type: str, expected: bool) -> None:
    assert is_json_media_type(content_type) is expected


@pytest.mark.unit
def test_appendix_a_schema_rejects_documents_the_consumer_must_accept() -> None:
    validator = Draft202012Validator(APPENDIX_A_SCHEMA)
    valid = {**RFC_OUT_OF_CREDIT, "status": 403}
    assert validator.is_valid(valid) is True  # pyright: ignore[reportUnknownMemberType]
    for broken in ({"status": 600}, {"status": True}, {"type": 123}):
        assert validator.is_valid({**valid, **broken}) is False  # pyright: ignore[reportUnknownMemberType]
        parsed = parse_problem(
            json.dumps({**valid, **broken}).encode(),
            base_uri="https://example.com/",
            http_status=403,
        )
        assert parsed.title == "You do not have enough credit."
        assert parsed.ignored == (next(iter(broken)),)


@get("/missing")
async def litestar_missing() -> None:
    raise NotFoundException(detail="Item 7 does not exist")


@get("/credit")
async def litestar_credit() -> None:
    raise ProblemDetailsException(
        status_code=403,
        type_=OUT_OF_CREDIT.uri,
        title=OUT_OF_CREDIT.title,
        extra={"status": 200},
    )


def litestar_client() -> httpx.AsyncClient:
    config = ProblemDetailsConfig(enable_for_all_http_exceptions=True)
    app = Litestar([litestar_missing, litestar_credit], plugins=[ProblemDetailsPlugin(config)])
    # Litestar types its scope as TypedDicts; httpx expects MutableMapping. Same ASGI protocol.
    transport = httpx.ASGITransport(app=cast("Any", app))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_litestar_plugin_puts_occurrence_detail_in_title() -> None:
    async with litestar_client() as client:
        response = await client.get("/missing")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json() == {
        "status": 404,
        "title": "Item 7 does not exist",
        "detail": "Not Found",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_litestar_extra_overwrites_status_member() -> None:
    async with litestar_client() as client:
        response = await client.get("/credit")

    assert response.status_code == 403
    assert response.json()["status"] == 200
    parsed = parse_problem(response.content, base_uri="http://test/", http_status=403)
    assert parsed.status_mismatch is True
