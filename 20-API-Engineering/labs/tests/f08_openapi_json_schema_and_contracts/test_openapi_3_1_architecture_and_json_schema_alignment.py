"""Unit tests for OpenAPI 3.0 versus OpenAPI 3.1 Schema Object and document semantics."""

from __future__ import annotations

import gc
import json
import weakref
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from jsonschema_path import SchemaPath
from openapi_spec_validator import OpenAPIV31SpecValidator

from api_labs.f08_openapi_json_schema_and_contracts.oas31_alignment import (
    DRAFT4_DIALECT,
    audit,
    build_subscription_app,
    check_every_schema_object,
    external_references,
    migrate_schema_30_to_31,
    oas30_document,
    oas31_document,
    schema_object_locations,
    verdicts,
)

if TYPE_CHECKING:
    from pathlib import Path

WEBHOOK_BODY_SCHEMA = (
    "/webhooks/subscription.renewed/post/requestBody/content/application~1json/schema"
)


def _schemas(document: dict[str, Any]) -> dict[str, Any]:
    schemas: dict[str, Any] = document["components"]["schemas"]
    return schemas


def _properties(document: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = _schemas(document)["Subscription"]["properties"]
    return properties


@pytest.mark.unit
def test_oas30_document_validates_and_rejects_every_31_only_construct() -> None:
    baseline = audit(oas30_document())
    assert baseline.validator == "OpenAPIV30SpecValidator"
    assert baseline.errors == ()

    type_array = oas30_document()
    _properties(type_array)["coupon"] = {"type": ["string", "null"]}
    assert audit(type_array).valid is False

    const = oas30_document()
    _properties(const)["plan"] = {"const": "pro"}
    assert audit(const).valid is False

    numeric_bound = oas30_document()
    _properties(numeric_bound)["seats"] = {"type": "integer", "exclusiveMinimum": 0}
    assert any("0 is not of type 'boolean'" in error for error in audit(numeric_bound).errors)

    webhooks = oas30_document()
    webhooks["webhooks"] = {}
    assert any("'webhooks'" in error for error in audit(webhooks).errors)

    no_paths = oas30_document()
    del no_paths["paths"]
    assert any("paths" in error and "required" in error for error in audit(no_paths).errors)


@pytest.mark.unit
def test_oas31_document_needs_one_of_paths_components_or_webhooks() -> None:
    full = audit(oas31_document())
    assert full.validator == "OpenAPIV31SpecValidator"
    assert full.errors == ()

    header = {"openapi": "3.1.2", "info": {"title": "Events", "version": "1.0.0"}}
    assert audit(header).valid is False
    assert audit({**header, "webhooks": {"ping": {"post": {}}}}).valid is True
    # A webhook key is a name, not a path template, and an operation needs no `responses`.
    assert audit({**header, "webhooks": {"not/a path {x}": {"post": {}}}}).valid is True
    assert audit({**header, "paths": {"no-leading-slash": {}}}).valid is False
    assert audit({**header, "components": {}}).valid is True
    assert audit({**header, "paths": {}}).valid is True

    # The patch digit is free, the minor is not: 3.1.99 passes and a bare 3.1 does not.
    assert audit({**header, "openapi": "3.1.99", "paths": {}}).valid is True
    assert audit({**header, "openapi": "3.1", "paths": {}}).valid is False


@pytest.mark.unit
def test_nullable_is_inert_in_31_and_type_arrays_do_not_exist_in_30() -> None:
    nullable = {"type": "string", "nullable": True}
    type_array = {"type": ["string", "null"]}
    instances = [None, "abc", 7]

    assert verdicts("3.0", nullable, instances) == (True, True, False)
    assert verdicts("3.1", type_array, instances) == (True, True, False)
    assert verdicts("3.1", _properties(oas31_document())["coupon"], instances) == (
        True,
        True,
        False,
    )
    # `nullable` is an unknown keyword in the 3.1 dialect: the document still validates
    # and null is silently rejected.
    assert verdicts("3.1", nullable, instances) == (False, True, False)
    silent = oas31_document()
    _properties(silent)["coupon"] = nullable
    assert audit(silent).valid is True

    # Pinned behavior of openapi-schema-validator 0.9.0: the 3.0 `type` keyword looks the
    # value up in a dict, so a type array is a crash, not a validation result.
    with pytest.raises(TypeError, match="unhashable"):
        verdicts("3.0", type_array, instances)

    # A closed enum still excludes null in both dialects.
    assert verdicts("3.0", {**nullable, "enum": ["a"]}, [None, "a"]) == (False, True)
    assert verdicts("3.1", {**type_array, "enum": ["a"]}, [None, "a"]) == (False, True)
    assert verdicts("3.1", {**type_array, "enum": ["a", None]}, [None, "a"]) == (True, True)

    # A reference has no `type` to extend: use anyOf, and not oneOf when a branch admits null.
    defs = {"$defs": {"code": {"type": "string"}}}
    nullable_ref = {"anyOf": [{"$ref": "#/$defs/code"}, {"type": "null"}], **defs}
    assert verdicts("3.1", nullable_ref, [None, "abc", 7]) == (True, True, False)
    double_match = {"oneOf": [type_array, {"type": "null"}]}
    assert verdicts("3.1", double_match, [None, "abc"]) == (False, True)


@pytest.mark.unit
def test_migration_rewrites_30_keywords_and_preserves_verdicts() -> None:
    before = _schemas(oas30_document())["Subscription"]
    after = migrate_schema_30_to_31(before)
    assert after["properties"] == {
        "id": {"type": "string", "examples": ["sub_1"]},
        "plan": {"type": "string", "enum": ["pro"]},
        "coupon": {"type": ["string", "null"], "maxLength": 8},
        "seats": {"type": "integer", "exclusiveMinimum": 0},
        "window": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2},
    }
    assert before["properties"]["coupon"] == {"type": "string", "nullable": True, "maxLength": 8}

    instances: list[object] = [
        {"id": "sub_1", "plan": "pro", "coupon": None, "seats": 1},
        {"id": "sub_1", "plan": "pro", "coupon": "SAVE10", "seats": 1},
        {"id": "sub_1", "plan": "pro", "coupon": None, "seats": 0},
        {"id": "sub_1", "plan": "team", "coupon": None, "seats": 1},
        {"id": "sub_1", "plan": "pro", "coupon": "TOO-LONG-CODE", "seats": 1},
        {"id": None, "plan": "pro", "coupon": None, "seats": 1},
        {"id": "sub_1", "plan": "pro", "seats": 1},
        {"id": "sub_1", "plan": "pro", "coupon": None, "seats": 1, "window": ["a", "b"]},
        {"id": "sub_1", "plan": "pro", "coupon": None, "seats": 1, "window": ["a"]},
    ]
    expected = (True, True, False, False, False, False, False, True, False)
    assert verdicts("3.0", before, instances) == expected
    assert verdicts("3.1", after, instances) == expected
    assert migrate_schema_30_to_31(after) == after

    nested = {
        "oneOf": [{"type": "string", "nullable": True}, {"type": "integer"}],
        "not": {"type": "boolean", "example": True},
        "additionalProperties": False,
    }
    assert migrate_schema_30_to_31(nested) == {
        "oneOf": [{"type": ["string", "null"]}, {"type": "integer"}],
        "not": {"type": "boolean", "examples": [True]},
        "additionalProperties": False,
    }
    assert migrate_schema_30_to_31({"nullable": True}) == {}
    assert migrate_schema_30_to_31({"type": "number", "maximum": 5, "exclusiveMaximum": False}) == {
        "type": "number",
        "maximum": 5,
    }
    assert migrate_schema_30_to_31({"$ref": "#/components/schemas/A", "nullable": True}) == {
        "$ref": "#/components/schemas/A"
    }


@pytest.mark.unit
def test_binary_formats_move_to_the_content_vocabulary_and_raw_binary_loses_its_type() -> None:
    encoded = migrate_schema_30_to_31({"type": "string", "format": "byte", "nullable": True})
    assert encoded == {"type": ["string", "null"], "contentEncoding": "base64"}
    assert verdicts("3.1", encoded, ["aGk=", None, 5]) == (True, True, False)
    # The content vocabulary is annotation-only: the string is not decoded.
    assert verdicts("3.1", encoded, ["!!!not base64!!!"]) == (True,)

    legacy = {"type": "string", "format": "binary", "nullable": True}
    raw = migrate_schema_30_to_31(legacy)
    assert raw == {"contentMediaType": "application/octet-stream"}
    # This rewrite follows the specification table and is the one that is not
    # verdict-preserving for JSON instances: raw binary is outside `type`.
    assert verdicts("3.0", legacy, ["bytes", 5]) == (True, False)
    assert verdicts("3.1", raw, ["bytes", 5]) == (True, True)


@pytest.mark.unit
def test_boolean_exclusive_minimum_is_read_as_the_number_one_by_the_31_keyword_set() -> None:
    legacy = {"type": "integer", "minimum": 0, "exclusiveMinimum": True}
    migrated = migrate_schema_30_to_31(legacy)
    assert migrated == {"type": "integer", "exclusiveMinimum": 0}

    assert verdicts("3.0", legacy, [0, 1, 2]) == (False, True, True)
    assert verdicts("3.1", migrated, [0, 1, 2]) == (False, True, True)
    # Unmigrated, the 3.1 keyword takes a number and Python's True equals 1: 1 is now rejected.
    assert verdicts("3.1", legacy, [0, 1, 2]) == (False, False, True)
    # With a bound above 1 the same leftover makes an exclusive bound inclusive instead.
    above_one = {"minimum": 7, "exclusiveMinimum": True}
    assert verdicts("3.0", above_one, [7]) == (False,)
    assert verdicts("3.1", above_one, [7]) == (True,)

    unmigrated = oas31_document()
    _properties(unmigrated)["seats"] = legacy
    assert audit(unmigrated).errors == ("True is not of type 'number'",)


@pytest.mark.unit
def test_const_prefix_items_and_integer_semantics_differ_between_keyword_sets() -> None:
    properties = _properties(oas31_document())

    assert verdicts("3.1", properties["plan"], ["pro", "team"]) == (True, False)
    assert verdicts("3.0", properties["plan"], ["pro", "team"]) == (True, True)

    windows: list[object] = [["2026-11", 30], ["2026-11", "30"], ["2026-11", 30, "extra"]]
    assert verdicts("3.1", properties["window"], windows) == (True, False, False)
    open_tail = {"type": "array", "prefixItems": [{"type": "string"}]}
    assert verdicts("3.1", open_tail, [["a", 1, None], [1]]) == (True, False)
    assert verdicts("3.0", open_tail, [["a", 1, None], [1]]) == (True, True)

    # JSON Schema 2020-12 defines integer mathematically; the 3.0 validator checks the type.
    assert verdicts("3.1", {"type": "integer"}, [1, 1.0, 1.5, True]) == (True, True, False, False)
    assert verdicts("3.0", {"type": "integer"}, [1, 1.0, 1.5, True]) == (True, False, False, False)


@pytest.mark.unit
def test_ref_siblings_are_live_keywords_and_reference_objects_stay_closed() -> None:
    schema = {
        "$ref": "#/$defs/code",
        "maxLength": 3,
        "$defs": {"code": {"type": "string"}},
    }
    assert verdicts("3.1", schema, ["abc", "abcdef", 5]) == (True, False, False)
    # OpenAPI 3.0 says the siblings SHALL be ignored; this 3.0 validator applies them anyway.
    legacy = {"$ref": "#/definitions/code", "maxLength": 3, "definitions": schema["$defs"]}
    assert verdicts("3.0", legacy, ["abc", "abcdef"]) == (True, False)

    described = oas31_document()
    described["webhooks"]["subscription.renewed"]["description"] = "Sent on each renewal."
    assert audit(described).valid is True

    extended = oas31_document()
    extended["webhooks"]["subscription.renewed"]["x-internal"] = True
    assert any("'x-internal' was unexpected" in error for error in audit(extended).errors)

    both = oas31_document()
    both["info"]["license"]["url"] = "https://www.apache.org/licenses/LICENSE-2.0"
    assert audit(both).valid is False


@pytest.mark.unit
def test_spec_validator_skips_webhook_schemas_that_the_exhaustive_walk_checks() -> None:
    document = oas31_document()
    assert check_every_schema_object(document) == ()
    assert [location[:2] for location in schema_object_locations(document)] == [
        ("components", "schemas"),
        ("components", "pathItems"),
    ]

    in_components = oas31_document()
    _schemas(in_components)["Broken"] = {"type": "strnig"}
    assert audit(in_components).errors == ("'strnig' is not valid under any of the given schemas",)

    in_webhook = oas31_document()
    in_webhook["webhooks"]["subscription.renewed"] = {
        "post": {
            "requestBody": {"content": {"application/json": {"schema": {"type": "strnig"}}}},
            "responses": {
                "200": {
                    "description": "ok",
                    "headers": {"X-Trace": {"schema": {"type": "string", "default": 5}}},
                }
            },
        }
    }
    assert audit(in_webhook).errors == ()
    assert check_every_schema_object(in_webhook) == (
        f"{WEBHOOK_BODY_SCHEMA}: 'strnig' is not valid under any of the given schemas",
        "/webhooks/subscription.renewed/post/responses/200/headers/X-Trace/schema: "
        "5 is not of type 'string'",
    )


_BAD: dict[str, Any] = {"type": "strnig"}
_MEDIA: dict[str, Any] = {"application/json": {"schema": _BAD}}
_CALLBACK: dict[str, Any] = {"{$request.body#/url}": {"post": {"requestBody": {"content": _MEDIA}}}}
_MULTIPART: dict[str, Any] = {
    "multipart/form-data": {"encoding": {"file": {"headers": {"X-Sum": {"schema": _BAD}}}}}
}
_BODY_SCHEMA = "requestBody/content/application~1json/schema"
_LOCATIONS: list[tuple[dict[str, Any], str, bool]] = [
    ({"components": {"schemas": {"S": _BAD}}}, "/components/schemas/S", True),
    (
        {"paths": {"/x": {"get": {"parameters": [{"name": "q", "in": "query", "schema": _BAD}]}}}},
        "/paths/~1x/get/parameters/0/schema",
        True,
    ),
    (
        {"paths": {"/x": {"get": {"responses": {"200": {"description": "d", "content": _MEDIA}}}}}},
        "/paths/~1x/get/responses/200/content/application~1json/schema",
        True,
    ),
    (
        {"paths": {"/x": {"post": {"requestBody": {"content": _MEDIA}}}}},
        f"/paths/~1x/post/{_BODY_SCHEMA}",
        False,
    ),
    (
        {"paths": {"/x": {"parameters": [{"name": "q", "in": "query", "content": _MEDIA}]}}},
        "/paths/~1x/parameters/0/content/application~1json/schema",
        False,
    ),
    (
        {"paths": {"/x": {"post": {"callbacks": {"done": _CALLBACK}}}}},
        f"/paths/~1x/post/callbacks/done/{{$request.body#~1url}}/post/{_BODY_SCHEMA}",
        False,
    ),
    (
        {"webhooks": {"ping": {"post": {"requestBody": {"content": _MEDIA}}}}},
        f"/webhooks/ping/post/{_BODY_SCHEMA}",
        False,
    ),
    (
        {"components": {"pathItems": {"P": {"post": {"requestBody": {"content": _MEDIA}}}}}},
        f"/components/pathItems/P/post/{_BODY_SCHEMA}",
        False,
    ),
    (
        {"components": {"parameters": {"Q": {"name": "q", "in": "query", "schema": _BAD}}}},
        "/components/parameters/Q/schema",
        False,
    ),
    ({"components": {"headers": {"H": {"schema": _BAD}}}}, "/components/headers/H/schema", False),
    (
        {"components": {"requestBodies": {"B": {"content": _MEDIA}}}},
        "/components/requestBodies/B/content/application~1json/schema",
        False,
    ),
    (
        {"components": {"responses": {"R": {"description": "d", "content": _MEDIA}}}},
        "/components/responses/R/content/application~1json/schema",
        False,
    ),
    (
        {"components": {"callbacks": {"C": _CALLBACK}}},
        f"/components/callbacks/C/{{$request.body#~1url}}/post/{_BODY_SCHEMA}",
        False,
    ),
    (
        {"components": {"requestBodies": {"B": {"content": _MULTIPART}}}},
        "/components/requestBodies/B/content/multipart~1form-data/encoding/file/headers/X-Sum/schema",
        False,
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("fragment", "pointer", "seen_by_spec_validator"),
    _LOCATIONS,
    ids=[pointer for _, pointer, _ in _LOCATIONS],
)
def test_malformed_schema_is_found_at_every_location_by_the_walk_only(
    fragment: dict[str, Any], pointer: str, seen_by_spec_validator: bool
) -> None:
    document = {"openapi": "3.1.2", "info": {"title": "Probe", "version": "1.0.0"}, **fragment}
    message = "'strnig' is not valid under any of the given schemas"

    assert audit(document).errors == ((message,) if seen_by_spec_validator else ())
    assert check_every_schema_object(document) == (f"{pointer}: {message}",)


@pytest.mark.unit
def test_walk_ignores_reference_object_siblings_and_extension_keys() -> None:
    # Every `_BAD` below sits where the specification says the content is ignored or opaque:
    # beside a `$ref`, or under an `x-` extension key.
    operation = {"requestBody": {"content": _MEDIA}}
    document: dict[str, Any] = {
        "openapi": "3.1.2",
        "info": {"title": "Probe", "version": "1.0.0"},
        "paths": {
            "x-internal": {"post": operation},
            "/x": {
                "parameters": [{"$ref": "#/components/parameters/Q", "schema": _BAD}],
                "get": {
                    "parameters": [{"$ref": "#/components/parameters/Q", "content": _MEDIA}],
                    "requestBody": {"$ref": "#/components/requestBodies/Raw", "content": _MEDIA},
                    "responses": {
                        "200": {"$ref": "#/components/responses/R", "content": _MEDIA},
                        "x-meta": {"content": "opaque"},
                    },
                    "callbacks": {
                        "done": {"$ref": "#/components/callbacks/C", "{$url}": {"post": operation}},
                        "other": {"x-note": {"post": operation}},
                    },
                },
            },
        },
        "webhooks": {"ping": {"$ref": "#/components/pathItems/P", "post": operation}},
        "components": {
            "parameters": {"Q": {"name": "q", "in": "query", "schema": {"type": "string"}}},
            "requestBodies": {
                "B": {"$ref": "#/components/requestBodies/Raw", "content": _MEDIA},
                "Raw": {"content": {"application/octet-stream": {}}},
            },
            "responses": {
                "R": {
                    "description": "d",
                    "headers": {"X-A": {"$ref": "#/components/headers/H", "schema": _BAD}},
                }
            },
        },
    }
    assert schema_object_locations(document) == (("components", "parameters", "Q", "schema"),)

    # `webhooks` has no extension carve-out: a webhook named `x-ping` is a real webhook.
    named_like_an_extension = {"webhooks": {"x-ping": {"post": operation}}}
    assert schema_object_locations(named_like_an_extension) == (
        ("webhooks", "x-ping", "post", "requestBody", "content", "application/json", "schema"),
    )


@pytest.mark.unit
def test_walk_reports_a_shared_schema_object_at_each_location_and_rejects_other_versions() -> None:
    shared = {"post": {"requestBody": {"content": _MEDIA}}}
    document = {
        "openapi": "3.1.2",
        "info": {"title": "Probe", "version": "1.0.0"},
        "webhooks": {"created": shared, "deleted": shared},
    }
    message = "'strnig' is not valid under any of the given schemas"
    assert check_every_schema_object(document) == (
        f"/webhooks/created/post/{_BODY_SCHEMA}: {message}",
        f"/webhooks/deleted/post/{_BODY_SCHEMA}: {message}",
    )

    with pytest.raises(ValueError, match=r"OpenAPI 3\.1 object model only"):
        check_every_schema_object(oas30_document())
    with pytest.raises(ValueError, match=r"unsupported openapi version: '3\.2\.0'"):
        audit({**document, "openapi": "3.2.0"})
    with pytest.raises(ValueError, match="unsupported openapi version: None"):
        audit({"info": {"title": "Probe", "version": "1.0.0"}})


@pytest.mark.unit
def test_json_schema_dialect_and_schema_keyword_select_the_meta_schema() -> None:
    legacy = {"type": "integer", "minimum": 0, "exclusiveMinimum": True}

    default_dialect = oas31_document()
    _schemas(default_dialect)["Legacy"] = legacy
    assert audit(default_dialect).errors == ("True is not of type 'number'",)

    document_wide = oas31_document()
    _schemas(document_wide)["Legacy"] = legacy
    document_wide["jsonSchemaDialect"] = DRAFT4_DIALECT
    del _schemas(document_wide)["Subscription"]
    assert audit(document_wide).errors == ()

    per_schema = oas31_document()
    _schemas(per_schema)["Legacy"] = {"$schema": DRAFT4_DIALECT, **legacy}
    assert audit(per_schema).errors == ()

    unknown = oas31_document()
    unknown["jsonSchemaDialect"] = "https://example.test/dialect"
    assert audit(unknown).errors == ("Unknown JSON Schema dialect: 'https://example.test/dialect'",)

    # The dated iteration of the OAS dialect is published but not registered in this release.
    dated = oas31_document()
    dated["jsonSchemaDialect"] = "https://spec.openapis.org/oas/3.1/dialect/2024-11-10"
    assert audit(dated).errors == (
        "Unknown JSON Schema dialect: 'https://spec.openapis.org/oas/3.1/dialect/2024-11-10'",
    )


@pytest.mark.unit
def test_external_file_reference_is_read_by_default_and_flagged_by_the_prescan(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external.json"
    external.write_text(json.dumps({"type": "string", "default": 5}), encoding="utf-8")

    document = oas31_document()
    assert external_references(document) == ()
    _schemas(document)["Imported"] = {"$ref": external.as_uri()}

    # The error can only come from the file's contents: the validator opened it.
    assert audit(document).errors == ("5 is not of type 'string'",)
    assert external_references(document) == (external.as_uri(),)

    # An empty handler map is not a control: unknown schemes fall through to urlopen.
    unguarded = OpenAPIV31SpecValidator(SchemaPath.from_dict(document, handlers={}))
    assert [str(error.message) for error in unguarded.iter_errors()] == [
        "5 is not of type 'string'"
    ]

    external.write_text(json.dumps({"type": "string", "default": "ok"}), encoding="utf-8")
    assert audit(document).errors == ()


@pytest.mark.unit
def test_unknown_keywords_format_and_pattern_follow_annotation_and_python_re_defaults() -> None:
    # A misspelled keyword is an annotation in the 3.1 dialect, so nothing is closed.
    typo = {"type": "object", "additionalProperites": False}
    closed = {"type": "object", "additionalProperties": False}
    assert verdicts("3.1", typo, [{"x": 1}]) == (True,)
    assert verdicts("3.1", closed, [{"x": 1}]) == (False,)
    with_typo = oas31_document()
    _schemas(with_typo)["Typo"] = typo
    assert audit(with_typo).errors == ()

    # `format` is an annotation unless a format checker is passed to the validator.
    assert verdicts("3.1", {"type": "integer", "format": "int32"}, [2**40]) == (True,)
    assert verdicts("3.1", {"type": "string", "format": "date-time"}, ["not a date"]) == (True,)

    # Pinned environment: without the optional `regress` package `pattern` runs on Python `re`,
    # which is unanchored, matches non-ASCII digits with \\d, and accepts Python-only syntax.
    assert verdicts("3.1", {"pattern": "es"}, ["expression"]) == (True,)
    assert verdicts("3.1", {"pattern": r"^\d{3}$"}, ["\u0661\u0662\u0663", "12a"]) == (True, False)
    assert verdicts("3.1", {"pattern": "(?P<x>a)"}, ["a"]) == (True,)


@pytest.mark.unit
def test_spec_validator_instances_are_retained_by_the_method_cache() -> None:
    def validate_once() -> weakref.ref[OpenAPIV31SpecValidator]:
        validator = OpenAPIV31SpecValidator(oas31_document())
        assert list(validator.iter_errors()) == []
        return weakref.ref(validator)

    reference = validate_once()
    gc.collect()
    # `iter_errors` is wrapped in an unbounded lru_cache keyed on `self`.
    assert reference() is not None

    def construct_only() -> weakref.ref[OpenAPIV31SpecValidator]:
        return weakref.ref(OpenAPIV31SpecValidator(oas31_document()))

    unused = construct_only()
    gc.collect()
    assert unused() is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_emits_openapi_31_with_null_union_const_tuple_and_webhooks() -> None:
    app = build_subscription_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        document = (await client.get("/openapi.json")).json()
        resource = (await client.get("/subscriptions/sub_1")).json()

    assert document["openapi"] == "3.1.0"
    assert sorted(document) == ["components", "info", "openapi", "paths", "webhooks"]
    assert list(document["webhooks"]) == ["subscription.renewed"]
    properties = _properties(document)
    assert properties["coupon"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert properties["plan"]["const"] == "pro"
    assert properties["seats"]["exclusiveMinimum"] == 0
    assert properties["window"]["prefixItems"] == [{"type": "string"}, {"type": "integer"}]

    assert audit(document).errors == ()
    assert check_every_schema_object(document) == ()
    assert resource == {
        "id": "sub_1",
        "plan": "pro",
        "coupon": None,
        "seats": 3,
        "window": ["2026-11", 30],
    }
    schema = _schemas(document)["Subscription"]
    assert verdicts("3.1", schema, [resource, {**resource, "seats": 0}]) == (True, False)


@pytest.mark.unit
def test_relabelling_the_fastapi_document_as_30_does_not_make_it_30() -> None:
    app = build_subscription_app()
    app.openapi_version = "3.0.2"
    document = app.openapi()

    assert document["openapi"] == "3.0.2"
    result = audit(document)
    assert result.validator == "OpenAPIV30SpecValidator"
    assert any("'webhooks'" in error for error in result.errors)

    # Without the webhooks the schemas still carry 3.1 keywords such as a numeric bound.
    del document["webhooks"]
    assert any("is not of type 'boolean'" in error for error in audit(document).errors)
