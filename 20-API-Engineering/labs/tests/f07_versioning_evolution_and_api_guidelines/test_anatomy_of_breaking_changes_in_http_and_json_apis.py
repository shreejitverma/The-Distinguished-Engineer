"""Lab: Catalog of Breaking Changes in HTTP/JSON APIs (request tightening, response loosening,
enum additions, and behavioral shifts)."""

from __future__ import annotations

import itertools
import json
from typing import TYPE_CHECKING, Any, Literal

import httpx
import pytest
from pydantic import ValidationError

from api_labs.f07_versioning_evolution_and_api_guidelines.breaking_change_catalog import (
    CATALOG,
    PROBES,
    RESPONSE_V1,
    BookClosedClient,
    BookEnumClient,
    BookLiteralClient,
    BookLossyClient,
    BookOpenClient,
    CatalogEntry,
    ShortTitle,
    Status,
    TitleTolerantClient,
    TitleV1Client,
    UnexpectedStatusError,
    breaking,
    build_books_server_v1,
    build_books_server_v2,
    class_based_should_retry,
    classify,
    legacy_created_id,
    legacy_should_retry,
    object_schema,
    parse_like_javascript,
    strict_created_id,
    witnesses,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

# Expected verdicts: Zalando rule #107 for closed request and open response schemas, plus the two
# content-model cases (#109 for open requests, a strict `extra="forbid"`-style response reader).
EXPECTED_BREAKING: dict[str, bool] = {
    "rq_add_optional_field": False,
    "rq_add_required_field": True,
    "rq_required_to_optional": False,
    "rq_optional_to_required": True,
    "rq_remove_field": True,
    "rq_tighten_max_length": True,
    "rq_loosen_max_length": False,
    "rq_extend_enum": False,
    "rq_reduce_enum": True,
    "rq_open_schema_add_typed_field": True,
    "rs_add_field": False,
    "rs_optional_to_required": False,
    "rs_required_to_optional": True,
    "rs_remove_field": True,
    "rs_extend_enum": True,
    "rs_reduce_enum": False,
    "rs_make_nullable": True,
    "rs_integer_to_number": True,
    "rs_closed_schema_add_field": True,
}

ENTRY_IDS = [entry.name for entry in CATALOG]


def _value(prop: dict[str, Any], *, open_: bool = False) -> dict[str, Any]:
    return object_schema({"v": prop}, ["v"], open_=open_)


# Keyword edges the catalog does not reach: (old, new) pairs that only narrow the document set.
# Each also runs reversed, where the same pair only widens it.
NARROWING_EDGES: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "max_length_added": (_value({"type": "string"}), _value({"type": "string", "maxLength": 40})),
    "maximum_lowered": (
        _value({"type": "integer", "maximum": 10}),
        _value({"type": "integer", "maximum": 5}),
    ),
    "maximum_added": (_value({"type": "integer"}), _value({"type": "integer", "maximum": 5})),
    "number_to_integer": (_value({"type": "number"}), _value({"type": "integer"})),
    "null_removed": (_value({"type": ["string", "null"]}), _value({"type": "string"})),
    "enum_added": (_value({"type": "string"}), _value({"type": "string", "enum": ["A"]})),
    "type_added": (_value({}), _value({"type": "string"})),
    "schema_closed": (_value({"type": "string"}, open_=True), _value({"type": "string"})),
}
EDGE_CASES = [
    *[(old, new, True) for old, new in NARROWING_EDGES.values()],
    *[(new, old, False) for old, new in NARROWING_EDGES.values()],
]
EDGE_IDS = [*NARROWING_EDGES, *[f"{name}_reversed" for name in NARROWING_EDGES]]
EDGE_PROBES: tuple[Any, ...] = (
    *[{"v": value} for value in ("A", "B", "x" * 50, 3, 7, 1.5, None)],
    {"v": "A", "extra": 1},
)

# A small schema family, checked exhaustively: every ordered pair, in both directions.
FAMILY_PROPERTIES: tuple[dict[str, Any] | None, ...] = (
    None,
    {},
    {"type": "string"},
    {"type": "string", "maxLength": 3},
    {"type": "string", "maximum": 5},
    {"type": "string", "enum": ["A"]},
    {"type": "string", "enum": ["A", "B"]},
    {"type": ["string", "null"]},
    {"type": "integer"},
    {"type": "integer", "maximum": 5},
    {"type": "number"},
    {"enum": [1]},
    {"enum": ["1"]},
)
FAMILY_POOL: tuple[dict[str, Any], ...] = tuple(
    doc
    for base in ({}, *[{"v": value} for value in ("A", "B", "1", "xxxx", 1, 3, 7, 1.5, None)])
    for doc in (base, {**base, "w": 1})
)


def _family() -> list[dict[str, Any]]:
    schemas: list[dict[str, Any]] = []
    for prop, required, closed in itertools.product(
        FAMILY_PROPERTIES, (False, True), (False, True)
    ):
        schema: dict[str, Any] = {"type": "object"}
        if prop is not None:
            schema["properties"] = {"v": prop}
        if required:
            schema["required"] = ["v"]
        if closed:
            schema["additionalProperties"] = False
        schemas.append(schema)
    return schemas


def _accepted(schema: dict[str, Any]) -> frozenset[int]:
    """Indices of the family pool documents that `schema` accepts, per the real validator."""
    # `{"type": "object"}` accepts every pool document, so its request witnesses against
    # `schema` are exactly the documents `schema` rejects.
    rejected = witnesses({"type": "object"}, schema, "request", FAMILY_POOL)
    return frozenset(i for i, doc in enumerate(FAMILY_POOL) if doc not in rejected)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
def test_catalog_covers_every_expected_change() -> None:
    assert sorted(ENTRY_IDS) == sorted(EXPECTED_BREAKING)


@pytest.mark.unit
@pytest.mark.parametrize("entry", CATALOG, ids=ENTRY_IDS)
def test_direction_rule_matches_the_guideline_table(entry: CatalogEntry) -> None:
    found = breaking(classify(entry.old, entry.new), entry.direction)
    assert bool(found) is EXPECTED_BREAKING[entry.name], found


@pytest.mark.unit
@pytest.mark.parametrize("entry", CATALOG, ids=ENTRY_IDS)
def test_classifier_agrees_with_a_real_validator_in_both_directions(entry: CatalogEntry) -> None:
    deltas = classify(entry.old, entry.new)
    for direction in ("request", "response"):
        predicted = bool(breaking(deltas, direction))
        observed = witnesses(entry.old, entry.new, direction, PROBES)
        assert predicted is bool(observed), (direction, deltas, observed)


@pytest.mark.unit
@pytest.mark.parametrize(("old", "new", "narrows"), EDGE_CASES, ids=EDGE_IDS)
def test_keyword_edges_agree_with_a_real_validator(
    old: dict[str, Any], new: dict[str, Any], narrows: bool
) -> None:
    deltas = classify(old, new)
    assert bool(breaking(deltas, "request")) is narrows
    assert bool(breaking(deltas, "response")) is not narrows
    assert bool(breaking(deltas, "both")) is True
    assert bool(witnesses(old, new, "request", EDGE_PROBES)) is narrows
    assert bool(witnesses(old, new, "response", EDGE_PROBES)) is not narrows


@pytest.mark.unit
def test_classifier_never_reports_safe_when_a_witness_exists() -> None:
    family = _family()
    accepted = [_accepted(schema) for schema in family]
    assert (len(family), len(FAMILY_POOL)) == (52, 20)
    safe = proven = unproven = 0
    for (old, old_ok), (new, new_ok) in itertools.product(
        zip(family, accepted, strict=True), repeat=2
    ):
        deltas = classify(old, new)
        checks: tuple[tuple[Literal["request", "response"], frozenset[int]], ...] = (
            ("request", old_ok - new_ok),
            ("response", new_ok - old_ok),
        )
        for direction, witness in checks:
            if not breaking(deltas, direction):
                assert not witness, (direction, old, new, [FAMILY_POOL[i] for i in sorted(witness)])
                safe += 1
            elif witness:
                proven += 1
            else:
                unproven += 1
    # Sound (no "safe" verdict has a witness) but conservative: 234 "breaking" verdicts have none.
    assert (safe, proven, unproven) == (960, 4214, 234)


@pytest.mark.unit
def test_classifier_is_conservative_when_a_bound_cannot_apply_to_the_type() -> None:
    old = _value({"type": "string"})
    new = _value({"type": "string", "maximum": 5})  # `maximum` constrains numbers only
    assert [d.kind for d in breaking(classify(old, new), "request")] == ["maximum"]
    assert [d.kind for d in breaking(classify(new, old), "response")] == ["maximum"]
    assert witnesses(old, new, "request", EDGE_PROBES) == []
    assert witnesses(new, old, "response", EDGE_PROBES) == []


@pytest.mark.unit
def test_declaring_an_unconstrained_property_on_an_open_schema_is_not_a_change() -> None:
    bare = object_schema({}, [], open_=True)
    declared = object_schema({"v": {}}, [], open_=True)
    assert breaking(classify(bare, declared), "both") == []
    assert breaking(classify(declared, bare), "both") == []
    for direction in ("request", "response"):
        assert witnesses(bare, declared, direction, EDGE_PROBES) == []
        assert witnesses(declared, bare, direction, EDGE_PROBES) == []


@pytest.mark.unit
def test_same_enum_extension_is_safe_in_requests_and_breaking_in_responses() -> None:
    extended = object_schema(
        {
            **RESPONSE_V1["properties"],
            "status": {"type": "string", "enum": ["ACTIVE", "SUSPENDED", "ARCHIVED"]},
        },
        RESPONSE_V1["required"],
        open_=True,
    )
    deltas = classify(RESPONSE_V1, extended)
    assert breaking(deltas, "request") == []
    assert [d.kind for d in breaking(deltas, "response")] == ["enum"]
    assert [d.kind for d in breaking(deltas, "both")] == ["enum"]
    assert witnesses(RESPONSE_V1, extended, "response", PROBES) == [
        {"name": "books/1", "title": "SICP", "status": "ARCHIVED", "copies": 3}
    ]


_TITLE_ONLY: dict[str, Any] = object_schema({"title": {"type": "string"}}, ["title"], open_=False)
_MALFORMED = "not a plain, valid JSON Schema"
UNMODELLED: dict[str, tuple[dict[str, Any], str]] = {
    "property_keyword": (
        object_schema({"title": {"type": "string", "pattern": "^[A-Z]"}}, ["title"], open_=False),
        r"title\.pattern",
    ),
    "object_keyword": ({**_TITLE_ONLY, "minProperties": 1}, "minProperties"),
    "non_object_root": ({**_TITLE_ONLY, "type": "array"}, "type"),
    "untyped_root": ({"properties": {"title": {"type": "string"}}}, "type"),
    "schema_valued_additional_properties": (
        {**_TITLE_ONLY, "additionalProperties": {"type": "string"}},
        "additionalProperties",
    ),
    "boolean_property_schema": ({**_TITLE_ONLY, "properties": {"title": False}}, "boolean schema"),
    # Modelled keywords with malformed values: a typo must not read as "safe".
    "misspelled_type": ({**_TITLE_ONLY, "properties": {"title": {"type": "strng"}}}, _MALFORMED),
    "null_bound": (
        {**_TITLE_ONLY, "properties": {"title": {"type": "string", "maxLength": None}}},
        _MALFORMED,
    ),
    "non_schema_property": ({**_TITLE_ONLY, "properties": {"title": []}}, _MALFORMED),
    "non_json_enum_key": (
        {**_TITLE_ONLY, "properties": {"title": {"enum": [{1: "a"}]}}},
        _MALFORMED,
    ),
}


@pytest.mark.unit
@pytest.mark.parametrize(("unmodelled", "named"), UNMODELLED.values(), ids=UNMODELLED)
def test_unmodelled_keyword_is_refused_rather_than_reported_safe(
    unmodelled: dict[str, Any], named: str
) -> None:
    with pytest.raises(ValueError, match=named):
        classify(_TITLE_ONLY, unmodelled)
    with pytest.raises(ValueError, match=named):
        classify(unmodelled, _TITLE_ONLY)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_tightening_turns_201_into_422_for_an_unchanged_client() -> None:
    body = {"title": "x" * 50}
    async with _client(build_books_server_v1()) as client:
        v1 = await client.post("/books", json=body)
    async with _client(build_books_server_v2()) as client:
        v2 = await client.post("/books", json=body)
        at_limit = await client.post("/books", json={"title": "x" * 40})
        over_limit = await client.post("/books", json={"title": "x" * 41})
    assert v1.status_code == 201
    assert v2.status_code == 422
    assert [e["type"] for e in v2.json()["detail"]] == ["string_too_long"]
    assert (at_limit.status_code, over_limit.status_code) == (202, 422)


@pytest.mark.unit
def test_max_length_counts_code_points_not_utf16_units_or_bytes() -> None:
    three = "\U0001f600" * 3
    assert len(three) == 3  # Unicode code points, the unit pydantic-core counts
    assert ShortTitle(title=three).title == three
    assert len(three.encode("utf-16-le")) // 2 == 6  # JavaScript `.length`
    assert len(three.encode("utf-8")) == 12  # a byte-sized VARCHAR or header budget
    with pytest.raises(ValidationError) as excinfo:
        ShortTitle(title="\U0001f600" * 4)
    assert excinfo.value.errors()[0]["type"] == "string_too_long"


@pytest.mark.unit
def test_response_loosening_to_null_breaks_a_non_optional_reader() -> None:
    wire = b'{"title": null}'
    with pytest.raises(ValidationError) as excinfo:
        TitleV1Client.model_validate_json(wire)
    assert excinfo.value.errors()[0]["type"] == "string_type"
    assert TitleTolerantClient.model_validate_json(wire).title is None


@pytest.mark.unit
def test_closed_pydantic_reader_rejects_an_additive_response_field() -> None:
    wire = b'{"name": "books/1", "status": "ACTIVE", "isbn": "978-0262510875"}'
    with pytest.raises(ValidationError) as excinfo:
        BookClosedClient.model_validate_json(wire)
    assert [(e["type"], e["loc"]) for e in excinfo.value.errors()] == [
        ("extra_forbidden", ("isbn",))
    ]
    assert BookOpenClient.model_validate_json(wire).status == "ACTIVE"


@pytest.mark.unit
def test_integer_ids_beyond_2_pow_53_collide_in_binary64_readers() -> None:
    wire = json.dumps({"ids": [2**53, 2**53 + 1], "id_strs": [str(2**53), str(2**53 + 1)]})
    as_python = json.loads(wire)
    as_javascript = parse_like_javascript(wire)
    assert as_python["ids"][0] != as_python["ids"][1]
    assert as_javascript["ids"][0] == as_javascript["ids"][1] == 9007199254740992.0
    assert as_javascript["id_strs"][0] != as_javascript["id_strs"][1]
    snowflake = 10765432100123456789  # the example in the X IDs documentation
    assert int(parse_like_javascript(str(snowflake))) == 10765432100123457536


@pytest.mark.unit
def test_enum_addition_breaks_literal_and_enum_readers() -> None:
    wire = b'{"name": "books/1", "status": "ARCHIVED"}'
    with pytest.raises(ValidationError) as literal_exc:
        BookLiteralClient.model_validate_json(wire)
    with pytest.raises(ValidationError) as enum_exc:
        BookEnumClient.model_validate_json(wire)
    assert literal_exc.value.errors()[0]["type"] == "literal_error"
    assert enum_exc.value.errors()[0]["type"] == "enum"


@pytest.mark.unit
def test_lossy_enum_fallback_corrupts_read_modify_write_but_raw_reader_does_not() -> None:
    wire = b'{"name": "books/1", "status": "ARCHIVED"}'
    lossy = BookLossyClient.model_validate_json(wire)
    open_reader = BookOpenClient.model_validate_json(wire)
    assert json.loads(lossy.model_dump_json())["status"] == "UNKNOWN"
    assert json.loads(open_reader.model_dump_json())["status"] == "ARCHIVED"
    assert open_reader.known_status is None
    assert BookOpenClient(name="books/1", status="ACTIVE").known_status is Status.ACTIVE


@pytest.mark.unit
@pytest.mark.asyncio
async def test_201_to_202_is_silently_misread_by_a_2xx_client() -> None:
    body = {"title": "SICP"}
    async with _client(build_books_server_v1()) as client:
        v1 = await client.post("/books", json=body)
    async with _client(build_books_server_v2()) as client:
        v2 = await client.post("/books", json=body)
    assert (v1.status_code, v2.status_code) == (201, 202)
    assert legacy_created_id(v1) == "books/1"
    assert legacy_created_id(v2) == "operations/1"  # an operation id stored as a book id
    assert strict_created_id(v1) == "books/1"
    with pytest.raises(UnexpectedStatusError, match="got 202"):
        strict_created_id(v2)


@pytest.mark.unit
@pytest.mark.parametrize("status", [200, 202])
def test_strict_client_rejects_every_success_code_but_201(status: int) -> None:
    body = {"id": "books/1"}
    assert strict_created_id(httpx.Response(201, json=body)) == "books/1"
    with pytest.raises(UnexpectedStatusError, match=f"got {status}"):
        strict_created_id(httpx.Response(status, json=body))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_400_to_422_turns_a_permanent_error_into_a_retry_loop() -> None:
    body = {"title": 7}
    async with _client(build_books_server_v1()) as client:
        v1 = await client.post("/books", json=body)
    async with _client(build_books_server_v2()) as client:
        v2 = await client.post("/books", json=body)
    assert (v1.status_code, v2.status_code) == (400, 422)
    for failed in (v1, v2):  # the 2xx-only client does reject both; it differs on retrying
        with pytest.raises(httpx.HTTPStatusError):
            legacy_created_id(failed)
    assert legacy_should_retry(201) is False
    assert legacy_should_retry(v1.status_code) is False
    assert legacy_should_retry(v2.status_code) is True
    assert class_based_should_retry(v1.status_code) is False
    assert class_based_should_retry(v2.status_code) is False
    # RFC 9110 Section 15: an unrecognized code is handled as the x00 code of its class.
    assert class_based_should_retry(471) is False
    assert class_based_should_retry(202) is False
    assert class_based_should_retry(429) is True
    assert class_based_should_retry(500) is True
    assert class_based_should_retry(503) is True
