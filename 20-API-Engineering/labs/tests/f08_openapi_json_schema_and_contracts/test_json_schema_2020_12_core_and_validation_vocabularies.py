"""Unit tests for JSON Schema 2020-12 identification, references, and validation keywords."""

from __future__ import annotations

import copy
import json
import math
import re
import sys
import urllib.request
from decimal import Decimal
from typing import Any

import httpx
import jsonschema  # type: ignore[import-untyped]
import pytest
from referencing import Registry
from referencing.exceptions import NoSuchAnchor, Unresolvable
from referencing.jsonschema import DRAFT202012, SchemaRegistry

from api_labs.f08_openapi_json_schema_and_contracts.core_vocabulary import (
    APPENDIX_A,
    DIALECT,
    DRAFT_07,
    MONEY,
    ORDER,
    PAGE,
    PAGE_OF_MONEY,
    STRICT_TREE,
    TREE,
    Violation,
    build_app,
    closed_registry,
    compile_validator,
    dangling_references,
    max_nesting_depth,
    nested_tree,
    resolve,
    resource_uris,
    strict_json_loads,
    violations,
)

_JSONSCHEMA: Any = jsonschema  # jsonschema 4.26.0 ships no py.typed marker
MISSPELLED = {"children": [{"daat": 1}]}


def violations_of(schema: Any, registry: Any, instance: Any) -> list[Violation]:
    """Compile the schema against the registry and list its violations for the instance."""
    return violations(compile_validator(schema, registry), instance)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("uri", "const"),
    [
        ("https://example.com/root.json", "root"),
        ("https://example.com/root.json#", "root"),
        ("https://example.com/root.json#foo", "A"),
        ("https://example.com/root.json#/$defs/A", "A"),
        ("https://example.com/other.json", "B"),
        ("https://example.com/other.json#", "B"),
        ("https://example.com/root.json#/$defs/B", "B"),
        ("https://example.com/other.json#bar", "X"),
        ("https://example.com/other.json#/$defs/X", "X"),
        ("https://example.com/root.json#/$defs/B/$defs/X", "X"),
        ("https://example.com/t/inner.json", "Y"),
        ("https://example.com/t/inner.json#bar", "Y"),
        ("https://example.com/t/inner.json#", "Y"),
        ("https://example.com/other.json#/$defs/Y", "Y"),
        ("https://example.com/root.json#/$defs/B/$defs/Y", "Y"),
        ("urn:uuid:ee564b8a-7a87-4125-8c96-e9f123d6766f", "C"),
        ("urn:uuid:ee564b8a-7a87-4125-8c96-e9f123d6766f#", "C"),
        ("https://example.com/root.json#/$defs/C", "C"),
    ],
)
def test_every_appendix_a_uri_identifies_its_schema(uri: str, const: str) -> None:
    found = resolve(closed_registry(APPENDIX_A), uri)
    assert not isinstance(found, bool)
    assert found.get("const", "root") == const


@pytest.mark.unit
def test_embedded_ids_create_resources_and_scope_their_anchors() -> None:
    assert resource_uris(APPENDIX_A) == [
        "https://example.com/other.json",
        "https://example.com/root.json",
        "https://example.com/t/inner.json",
        "urn:uuid:ee564b8a-7a87-4125-8c96-e9f123d6766f",
    ]
    registry = closed_registry(APPENDIX_A)
    with pytest.raises(NoSuchAnchor):
        resolve(registry, "https://example.com/root.json#bar")
    with pytest.raises(Unresolvable):
        resolve(registry, "https://example.com/inner.json")


@pytest.mark.unit
def test_registry_compares_uris_as_exact_strings() -> None:
    registry = closed_registry({"$id": "https://example.com/a/~b.json", "const": 1})
    assert resolve(registry, "https://example.com/a/~b.json#") == {
        "$id": "https://example.com/a/~b.json",
        "const": 1,
    }
    for equivalent in (
        "HTTPS://EXAMPLE.COM/a/~b.json",
        "https://example.com/a/%7Eb.json",
        "https://example.com:443/a/~b.json",
        "https://example.com/x/../a/~b.json",
    ):
        with pytest.raises(Unresolvable):
            resolve(registry, equivalent)


@pytest.mark.unit
def test_references_resolve_against_the_nearest_enclosing_id() -> None:
    registry = closed_registry(MONEY, ORDER)
    order = {
        "id": "0b4f6c2e-58a1-4f0e-9d53-6d1f3a2b9c11",
        "total": {"amount": 1999},
        "lines": [{"sku": "A-1", "qty": 2, "bundle": [{"sku": "B-1", "qty": 0}]}],
        "gift": {"note": "hi", "wrap": "foil"},
    }
    assert violations_of(ORDER, registry, order) == [
        Violation("/gift/wrap", "enum", ("properties", "gift", "properties", "wrap", "enum")),
        Violation(
            "/lines/0/bundle/0/qty",
            "minimum",
            (
                *("properties", "lines", "items", "properties", "bundle", "items"),
                *("properties", "qty", "minimum"),
            ),
        ),
        Violation("/total", "required", ("properties", "total", "required")),
    ]

    flattened = copy.deepcopy(ORDER)
    del flattened["$defs"]["gift"]["$id"]
    flattened["properties"]["gift"] = {"$ref": "#/$defs/gift"}
    assert dangling_references(ORDER, registry) == []
    assert dangling_references(flattened, registry) == ["#/$defs/wrap"]
    wrapped = {
        "id": "0b4f6c2e-58a1-4f0e-9d53-6d1f3a2b9c11",
        "total": {"amount": 1999, "currency": "EUR"},
        "lines": [{"sku": "A-1", "qty": 2}],
        "gift": {"wrap": "box"},
    }
    assert compile_validator(ORDER, registry).is_valid(wrapped) is True
    with pytest.raises(Unresolvable):
        compile_validator(flattened, registry).is_valid(wrapped)


@pytest.mark.unit
def test_violation_pointers_escape_member_names_per_rfc_6901() -> None:
    schema = {"properties": {"a/b": {"type": "integer"}, "m~n": {"type": "integer"}}}
    found = violations(compile_validator(schema), {"a/b": "x", "m~n": "y"})
    assert [each.pointer for each in found] == ["/a~1b", "/m~0n"]


@pytest.mark.unit
def test_schema_keyword_selects_the_dialect_and_the_meaning_of_ref_siblings() -> None:
    body = {"$defs": {"s": {"type": "string"}}, "definitions": {"s": {"type": "string"}}}
    modern = compile_validator({"$schema": DIALECT, "$ref": "#/$defs/s", "maxLength": 2, **body})
    legacy = compile_validator(
        {"$schema": DRAFT_07, "$ref": "#/definitions/s", "maxLength": 2, **body}
    )
    assert type(modern).__name__ == "Draft202012Validator"
    assert type(legacy).__name__ == "Draft7Validator"
    assert type(compile_validator({"maxLength": 2})).__name__ == "Draft202012Validator"

    assert [each.message for each in modern.iter_errors("abc")] == ["'abc' is too long"]
    assert list(legacy.iter_errors("abc")) == []
    assert [each.validator for each in legacy.iter_errors(5)] == ["type"]


@pytest.mark.unit
def test_dynamic_ref_rebinds_to_the_outermost_dynamic_anchor() -> None:
    registry = closed_registry(MONEY, TREE, STRICT_TREE, PAGE, PAGE_OF_MONEY)
    assert violations_of(TREE, registry, MISSPELLED) == []
    assert violations_of(STRICT_TREE, registry, MISSPELLED) == [
        Violation(
            "/children/0",
            "unevaluatedProperties",
            ("properties", "children", "items", "$dynamicRef", "unevaluatedProperties"),
        )
    ]

    page = {"items": [{"amount": 5, "currency": "EUR"}, {"amount": "5"}], "next": None}
    assert violations_of(PAGE, registry, page) == []
    assert [
        (each.pointer, each.keyword) for each in violations_of(PAGE_OF_MONEY, registry, page)
    ] == [("/items/1", "required"), ("/items/1/amount", "type")]


@pytest.mark.unit
def test_dynamic_ref_to_a_plain_anchor_stays_static() -> None:
    static_tree = {**TREE, "$id": "https://example.com/static-tree", "$anchor": "node"}
    del static_tree["$dynamicAnchor"]
    extension = {
        **STRICT_TREE,
        "$id": "https://example.com/strict-static-tree",
        "$ref": "static-tree",
    }
    registry = closed_registry(static_tree, extension)
    assert violations_of(extension, registry, MISSPELLED) == []


@pytest.mark.unit
def test_referencing_0_37_0_departs_from_core_8_2_3_2_in_two_cases() -> None:
    # Case 1: a $ref to a fragment made by $dynamicAnchor is rebound dynamically here,
    # although Core 8.2.3.2 gives dynamic behavior to $dynamicRef only (spec result: []).
    ref_tree = copy.deepcopy(TREE)
    ref_tree["$id"] = "https://example.com/ref-tree"
    ref_tree["properties"]["children"]["items"] = {"$ref": "#node"}
    extension = {**STRICT_TREE, "$id": "https://example.com/strict-ref-tree", "$ref": "ref-tree"}
    registry = closed_registry(TREE, ref_tree, extension)
    assert [each.pointer for each in violations_of(extension, registry, MISSPELLED)] == [
        "/children/0"
    ]

    # Case 2: a root schema without $id has an empty base URI, never enters the dynamic
    # scope, and its $dynamicAnchor is not found (spec result: one violation).
    anonymous = {
        "$schema": DIALECT,
        "$dynamicAnchor": "node",
        "$ref": "https://example.com/tree",
        "unevaluatedProperties": False,
    }
    assert violations_of(anonymous, registry, MISSPELLED) == []
    assert violations_of(STRICT_TREE, closed_registry(TREE, STRICT_TREE), MISSPELLED) != []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("schema", "instance", "valid"),
    [
        pytest.param({"type": "integer"}, 1.0, True, id="integer-accepts-1.0"),
        pytest.param({"type": "integer"}, 1.5, False, id="integer-rejects-1.5"),
        pytest.param({"type": "integer"}, True, False, id="integer-rejects-true"),
        pytest.param({"type": "number"}, False, False, id="number-rejects-false"),
        pytest.param({"const": 1}, 1.0, True, id="const-1-equals-1.0"),
        pytest.param({"const": 1}, True, False, id="const-1-differs-from-true"),
        pytest.param({"enum": [0]}, False, False, id="enum-0-differs-from-false"),
        pytest.param(
            {"const": {"a": 1, "b": [2]}}, {"b": [2.0], "a": 1}, True, id="const-ignores-order"
        ),
        pytest.param({"uniqueItems": True}, [1, True], True, id="unique-1-and-true-differ"),
        pytest.param({"uniqueItems": True}, [1, 1.0], False, id="unique-1-equals-1.0"),
        pytest.param(
            {"uniqueItems": True},
            [{"a": 1, "b": 2}, {"b": 2, "a": 1}],
            False,
            id="unique-objects-ignore-order",
        ),
        pytest.param({"maxLength": 1}, "\U0001f600", True, id="length-counts-code-points"),
        pytest.param({"maxLength": 1}, "e\u0301", False, id="length-does-not-normalize"),
        pytest.param({"pattern": "es"}, "expression", True, id="pattern-is-unanchored"),
        pytest.param({"maxLength": 1}, 12345, True, id="maxLength-ignores-numbers"),
        pytest.param({"minimum": 10}, "9", True, id="minimum-ignores-strings"),
        pytest.param({"required": ["a"]}, ["b"], True, id="required-ignores-arrays"),
        pytest.param({"required": ["a"]}, {"a": None}, True, id="required-accepts-null-member"),
        pytest.param(
            {"type": ["string", "null"], "maxLength": 3}, None, True, id="type-list-admits-null"
        ),
        pytest.param(
            {"contains": {"type": "string"}, "minContains": 0}, [1], True, id="minContains-0"
        ),
        pytest.param({"contains": {"type": "string"}}, [], False, id="contains-needs-one-match"),
        pytest.param({"maxContains": 0}, [1, 2], True, id="maxContains-alone-is-inert"),
        pytest.param(
            {"contains": {"type": "integer"}, "maxContains": 1}, [1, 2], False, id="maxContains"
        ),
        pytest.param(
            {"dependentRequired": {"card": ["billing"]}}, {"card": 1}, False, id="dependent-fires"
        ),
        pytest.param(
            {"dependentRequired": {"card": ["billing"]}}, {"cash": 1}, True, id="dependent-idle"
        ),
        pytest.param({"exclusiveMinimum": 0}, 0, False, id="exclusiveMinimum-excludes-bound"),
        pytest.param({"maximum": 10}, 10, True, id="maximum-includes-bound"),
        pytest.param({"maximum": 2**53}, 2**53 + 1, False, id="integers-past-2**53-are-exact"),
        pytest.param(False, None, False, id="false-schema"),
        pytest.param(True, None, True, id="true-schema"),
    ],
)
def test_assertions_follow_the_json_data_model(schema: Any, instance: Any, valid: bool) -> None:
    assert compile_validator(schema).is_valid(instance) is valid


@pytest.mark.unit
def test_python_re_is_not_the_ecma_262_dialect() -> None:
    code = compile_validator({"type": "string", "pattern": "^[a-z]+$"})
    assert code.is_valid("abc") is True
    assert code.is_valid("abc\n") is True
    assert compile_validator({"pattern": "^\\d+$"}).is_valid("\u0661\u0662\u0663") is True

    portable = compile_validator(
        {"type": "string", "pattern": "^[0-9a-z]+$", "not": {"pattern": "[\n\r]"}}
    )
    assert portable.is_valid("abc1") is True
    assert portable.is_valid("abc\n") is False
    assert portable.is_valid("\u0661\u0662\u0663") is False

    ecma_only = {"pattern": "^\\p{L}+$"}
    with pytest.raises(_JSONSCHEMA.SchemaError, match="is not a 'regex'"):
        compile_validator(ecma_only)
    with pytest.raises(re.error, match="bad escape"):
        _JSONSCHEMA.Draft202012Validator(ecma_only).is_valid("abc")


@pytest.mark.unit
def test_multiple_of_divides_binary_floats_unless_given_exact_numbers() -> None:
    assert compile_validator({"multipleOf": 0.01}).is_valid(19.99) is False
    assert compile_validator({"multipleOf": 0.1}).is_valid(0.3) is False
    assert compile_validator({"multipleOf": 0.0001}).is_valid(0.0075) is True

    assert compile_validator({"type": "integer", "multipleOf": 1}).is_valid(1999) is True
    exact = compile_validator({"multipleOf": Decimal("0.01")})
    assert exact.is_valid(json.loads("19.99", parse_float=Decimal)) is True
    assert exact.is_valid(json.loads("19.995", parse_float=Decimal)) is False

    assert math.isinf(1e308 / 0.5)
    assert compile_validator({"multipleOf": 0.5}).is_valid(1e308) is True
    assert compile_validator({"multipleOf": 0.123456789}).is_valid(1e308) is False


@pytest.mark.unit
def test_format_asserts_only_with_a_checker_and_only_for_installed_formats() -> None:
    schema = {"type": "string", "format": "date-time"}
    assert compile_validator(schema).is_valid("not a timestamp") is True

    asserting = compile_validator(schema, assert_format=True)
    assert asserting.is_valid("not a timestamp") is False
    assert asserting.is_valid("2026-11-11T00:00:00Z") is True
    assert asserting.is_valid("2026-11-11T00:00:00") is False
    assert sorted(asserting.FORMAT_CHECKER.checkers) == [
        "date",
        "date-time",
        "email",
        "idn-email",
        "idn-hostname",
        "ipv4",
        "ipv6",
        "regex",
        "time",
        "uuid",
    ]
    for name, bad in [
        ("hostname", "-bad-.example"),
        ("uri", "not a uri"),
        ("duration", "P1Q"),
        ("no-such-format", "x"),
    ]:
        assert compile_validator({"format": name}, assert_format=True).is_valid(bad) is True
    assert compile_validator({"format": "ipv4"}, assert_format=True).is_valid("256.1.1.1") is False
    assert compile_validator({"format": "ipv4"}, assert_format=True).is_valid(256) is True


@pytest.mark.unit
def test_vocabulary_declarations_are_not_consulted() -> None:
    meta = {
        "$schema": DIALECT,
        "$id": "https://schemas.example/meta/asserting",
        "$vocabulary": {
            "https://json-schema.org/draft/2020-12/vocab/core": True,
            "https://json-schema.org/draft/2020-12/vocab/applicator": True,
            "https://json-schema.org/draft/2020-12/vocab/validation": True,
            "https://json-schema.org/draft/2020-12/vocab/format-assertion": True,
        },
        "$dynamicAnchor": "meta",
        "allOf": [{"$ref": DIALECT}],
    }
    schema = {"$schema": meta["$id"], "format": "ipv4"}
    with pytest.warns(DeprecationWarning, match="metaschema specified by \\$schema was not found"):
        cls = _JSONSCHEMA.validators.validator_for(schema)
    assert cls.__name__ == "Draft202012Validator"
    assert cls(schema, registry=closed_registry(meta)).is_valid("not an address") is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "schema",
    [
        pytest.param({"type": "strng"}, id="unknown-type-name"),
        pytest.param({"minLength": -1}, id="negative-length"),
        pytest.param({"multipleOf": 0}, id="zero-divisor"),
        pytest.param({"required": ["a", "a"]}, id="duplicate-required-name"),
        pytest.param({"$id": "https://x.example/a#frag"}, id="id-with-fragment"),
        pytest.param({"$anchor": "1bad"}, id="anchor-starts-with-digit"),
        pytest.param({"$defs": []}, id="defs-not-an-object"),
        pytest.param({"maxLength": 1.5}, id="fractional-length"),
        pytest.param({"$ref": 5}, id="ref-not-a-string"),
    ],
)
def test_check_schema_rejects_malformed_keyword_values(schema: dict[str, Any]) -> None:
    with pytest.raises(_JSONSCHEMA.SchemaError):
        compile_validator(schema)


@pytest.mark.unit
def test_dangling_reference_passes_check_schema_and_fails_only_when_reached() -> None:
    schema = {
        "$id": "https://schemas.example/dangling.json",
        "properties": {
            "a": {"$ref": "#/$defs/missing"},
            "b": {"$dynamicRef": "other.json#x"},
            "c": True,
        },
    }
    validator = compile_validator(schema)
    assert validator.is_valid({}) is True
    assert validator.is_valid({"c": 1}) is True
    with pytest.raises(Unresolvable):
        validator.is_valid({"a": 1})
    assert dangling_references(schema, closed_registry()) == ["#/$defs/missing", "other.json#x"]


@pytest.mark.unit
def test_unknown_keywords_are_ignored_and_hide_no_resources() -> None:
    assert compile_validator({"type": "string", "minLenght": 5}).is_valid("") is True

    schema = {
        "$id": "https://schemas.example/e.json",
        "$defs": {"real": {"$id": "real.json"}},
        "x-schemas": {"hidden": {"$id": "hidden.json"}},
        "properties": {"p": {"enum": [{"$id": "fake.json"}]}},
    }
    assert resource_uris(schema) == [
        "https://schemas.example/e.json",
        "https://schemas.example/real.json",
    ]
    registry = closed_registry(schema)
    for name in ("hidden.json", "fake.json"):
        with pytest.raises(Unresolvable):
            resolve(registry, "https://schemas.example/" + name)


@pytest.mark.unit
def test_json_pointer_fragments_decode_percent_then_tilde_escapes() -> None:
    schema = {
        "$id": "https://schemas.example/p.json",
        "$defs": {
            "a/b": {"const": 1},
            "m~n": {"const": 2},
            "~1": {"const": 3},
            "c%d": {"const": 4},
        },
        "prefixItems": [{"const": 5}, {"const": 6}],
    }
    registry = closed_registry(schema)
    base = "https://schemas.example/p.json"
    expected = {
        "#/$defs/a~1b": 1,
        "#/$defs/m~0n": 2,
        "#/$defs/~01": 3,
        "#/$defs/c%25d": 4,
        "#/prefixItems/0": 5,
    }
    for fragment, const in expected.items():
        assert resolve(registry, base + fragment) == {"const": const}
    for fragment in ("#/$defs/a%2Fb", "#/prefixItems/2"):
        with pytest.raises(Unresolvable):
            resolve(registry, base + fragment)

    # referencing 0.37.0 parses array tokens with int(); RFC 6901 Section 4 allows none of these.
    assert resolve(registry, base + "#/prefixItems/-1") == {"const": 6}
    assert resolve(registry, base + "#/prefixItems/01") == {"const": 6}
    with pytest.raises(ValueError, match="invalid literal for int"):
        resolve(registry, base + "#/prefixItems/-")


@pytest.mark.unit
def test_default_registry_fetches_unknown_references_and_a_closed_one_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    def refuse(request: urllib.request.Request, *args: Any, **kwargs: Any) -> Any:
        attempts.append(request.full_url)
        raise OSError("network disabled in the lab")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    targets = [
        "https://attacker.example/schema.json",
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data/",
    ]
    for target in targets:
        with pytest.raises(Unresolvable):
            _JSONSCHEMA.Draft202012Validator({"$ref": target}).is_valid(1)
    assert attempts == targets

    attempts.clear()
    for target in targets:
        with pytest.raises(Unresolvable):
            compile_validator({"$ref": target}).is_valid(1)
    assert attempts == []


@pytest.mark.unit
def test_nan_passes_numeric_bounds_and_the_strict_loader_rejects_it() -> None:
    bounded = compile_validator({"type": "number", "minimum": 0, "maximum": 10})
    nan = json.loads("NaN")
    assert math.isnan(nan)
    assert bounded.is_valid(nan) is True
    assert bounded.is_valid(json.loads("1e400")) is False
    assert compile_validator({"minimum": 0}).is_valid(json.loads("Infinity")) is True
    with pytest.raises(ValueError, match="cannot convert float NaN to integer"):
        compile_validator({"multipleOf": 0.5}).is_valid(nan)

    assert json.loads('{"role": "user", "role": "admin"}') == {"role": "admin"}
    for text, message in [
        ('{"price": NaN}', "non-finite JSON number token: NaN"),
        ('{"price": -Infinity}', "non-finite JSON number token: -Infinity"),
        ('{"price": 1e400}', "JSON number overflows binary64: 1e400"),
        ('{"role": "user", "role": "admin"}', "duplicate object member name"),
    ]:
        with pytest.raises(ValueError, match=message):
            strict_json_loads(text)
    assert strict_json_loads('{"price": 12.5, "n": 9007199254740993}') == {
        "price": 12.5,
        "n": 9007199254740993,
    }


@pytest.mark.unit
def test_reference_cycles_and_deep_instances_end_in_recursion_error() -> None:
    with pytest.raises(RecursionError):
        compile_validator({"$ref": "#"}).is_valid(1)

    tree = compile_validator(TREE, closed_registry(TREE))
    assert tree.is_valid(nested_tree(40)) is True
    with pytest.raises(RecursionError):
        tree.is_valid(nested_tree(sys.getrecursionlimit()))
    assert 40 <= max_nesting_depth(tree) < sys.getrecursionlimit()


@pytest.mark.unit
def test_registry_replaces_a_duplicate_id_silently_unless_guarded() -> None:
    uri = "https://schemas.example/role.json"
    original: dict[str, Any] = {"$id": uri, "enum": ["user"]}
    hijack: dict[str, Any] = {"$id": uri, "enum": ["user", "admin"]}
    unguarded: SchemaRegistry = Registry()
    for each in (original, hijack):
        unguarded = unguarded.with_resource(uri, DRAFT202012.create_resource(each))
    assert unguarded.contents(uri) is hijack

    with pytest.raises(ValueError, match="claimed twice"):
        closed_registry(original, hijack)
    bundle = {"$id": "https://schemas.example/bundle.json", "$defs": {"role": hijack}}
    with pytest.raises(ValueError, match="claimed twice"):
        closed_registry(original, bundle)
    with pytest.raises(ValueError, match="needs an absolute \\$id"):
        closed_registry({"type": "string"})


@pytest.mark.unit
def test_embedded_resource_is_validated_under_its_own_dialect() -> None:
    outer = {
        "$schema": DIALECT,
        "$id": "https://schemas.example/outer.json",
        "properties": {"legacy": {"$ref": "legacy.json"}},
        "$defs": {
            "legacy": {
                "$schema": DRAFT_07,
                "$id": "legacy.json",
                "items": [{"type": "integer"}],
                "dependencies": {"a": ["b"]},
            }
        },
    }
    # Core 9.3.3: one meta-schema cannot check a compound document with mixed dialects.
    with pytest.raises(_JSONSCHEMA.SchemaError, match="is not of type 'object', 'boolean'"):
        compile_validator(outer)

    validator = _JSONSCHEMA.Draft202012Validator(outer, registry=closed_registry(outer))
    assert [each.validator for each in validator.iter_errors({"legacy": ["x"]})] == ["type"]
    assert [each.validator for each in validator.iter_errors({"legacy": {"a": 1}})] == [
        "dependencies"
    ]
    assert validator.is_valid({"legacy": [1, "x"]}) is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_json_admits_nan_past_bounds_and_strict_parsing_returns_400() -> None:
    transport = httpx.ASGITransport(app=build_app())
    headers = {"content-type": "application/json"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        over = await client.post("/lenient/quotes", content=b'{"price": 1000001}', headers=headers)
        assert over.status_code == 422
        assert over.json() == {"violations": ["/price: maximum"]}

        lenient = await client.post("/lenient/quotes", content=b'{"price": NaN}', headers=headers)
        assert lenient.status_code == 200
        assert lenient.json() == {"accepted": True}

        strict = await client.post("/strict/quotes", content=b'{"price": NaN}', headers=headers)
        assert strict.status_code == 400
        assert strict.json() == {"error": "non-finite JSON number token: NaN"}

        accepted = await client.post("/strict/quotes", content=b'{"price": 12.5}', headers=headers)
        assert accepted.status_code == 200
        missing = await client.post("/strict/quotes", content=b"{}", headers=headers)
        assert missing.status_code == 422
        assert missing.json() == {"violations": [": required"]}
