"""Unit tests for YAML 1.1 vs 1.2 resolution, alias expansion, and strict OpenAPI YAML loading."""

from __future__ import annotations

import io
import json
import os
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import ruamel.yaml
import yaml as _pyyaml  # type: ignore[import-untyped]
from datamodel_code_generator.util import get_safe_loader
from jsonschema_path.loaders import JsonschemaSafeLoader
from openapi_spec_validator import validate
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError
from schemathesis.core.deserialization import deserialize_yaml

from api_labs.f08_openapi_json_schema_and_contracts.yaml_hazards import (
    Divergence,
    ExpansionReport,
    Limits,
    YamlHazardError,
    classify_yaml_media_type,
    create_app,
    find_mistyped_string_literals,
    load_openapi_yaml,
    load_yaml_1_1,
    load_yaml_1_2,
    measure_expansion,
    resolution_divergences,
    resolve_core_schema,
    resolve_yaml_1_1,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

yaml: Any = _pyyaml
_ruamel: Any = ruamel.yaml

NORWAY = """\
openapi: 3.1.0
info:
  title: Shipping API
  version: 1.10
paths:
  /rates:
    get:
      parameters:
        - name: country
          in: query
          schema:
            type: string
            enum: [SE, DK, NO, FI]
            default: NO
        - name: window
          in: query
          schema:
            type: string
            example: 12:30
      responses:
        200:
          description: Rates
components:
  schemas:
    Toggle:
      type: object
      properties:
        on:
          type: boolean
        zip:
          type: string
          example: 010
"""

CLEAN = """\
openapi: 3.1.0
info:
  title: Shipping API
  version: "1.10"
paths:
  /rates:
    get:
      parameters:
        - name: country
          in: query
          schema:
            type: string
            enum: [SE, DK, "NO", FI]
            default: "NO"
      responses:
        "200":
          description: Rates
components:
  schemas:
    Toggle:
      type: object
      properties:
        "on":
          type: boolean
        ratio:
          type: number
          maximum: 1.5
          nullable: true
"""

TOOL_PROBE = """\
country: NO
flag: on
version: 3.10
rate: 1e3
zip: 010
window: 12:30
day: 2026-11-11
responses:
  200: ok
  on: x
"""


def _alias_bomb(levels: int, fan: int) -> str:
    lines = ["a0: &a0 [" + ",".join(['"lol"'] * fan) + "]"]
    lines += [
        f"a{level}: &a{level} [" + ",".join([f"*a{level - 1}"] * fan) + "]"
        for level in range(1, levels)
    ]
    return "\n".join(lines) + "\n"


def _merge_bomb(levels: int) -> str:
    lines = ["m0: &m0 {k: v}"]
    lines += [
        f"m{level}: &m{level}\n  <<: [*m{level - 1}, *m{level - 1}]" for level in range(1, levels)
    ]
    return "\n".join(lines) + "\n"


def _hazard(text: str, limits: Limits | None = None) -> tuple[str, str]:
    with pytest.raises(YamlHazardError) as caught:
        load_openapi_yaml(text, limits)
    return caught.value.code, caught.value.pointer


@pytest.mark.unit
def test_same_openapi_text_is_two_documents_under_yaml_1_1_and_1_2() -> None:
    assert resolution_divergences(NORWAY) == (
        Divergence("key", "/components/schemas/Toggle/properties", "True", "'on'"),
        Divergence("value", "/components/schemas/Toggle/properties/zip/example", "8", "10"),
        Divergence("value", "/paths/~1rates/get/parameters/0/schema/default", "False", "'NO'"),
        Divergence("value", "/paths/~1rates/get/parameters/0/schema/enum/2", "False", "'NO'"),
        Divergence("value", "/paths/~1rates/get/parameters/1/schema/example", "750", "'12:30'"),
    )
    assert resolution_divergences(CLEAN) == ()
    assert resolution_divergences("a: .nan\nb: [.inf, 1.0]\n") == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scalar", "yaml_1_1", "yaml_1_2"),
    [
        ("NO", "False", "'NO'"),
        ("on", "True", "'on'"),
        ("y", "'y'", "'y'"),
        ("3.10", "3.1", "3.1"),
        ("1.0", "1.0", "1.0"),
        ("3.1.0", "'3.1.0'", "'3.1.0'"),
        ("1e3", "'1e3'", "1000.0"),
        ("010", "8", "10"),
        ("08", "'08'", "8"),
        ("0o17", "'0o17'", "15"),
        ("12:30", "750", "'12:30'"),
        ("12:30:00", "45000", "'12:30:00'"),
        ("-.5", "'-.5'", "-0.5"),
        ("2026-11-11", "datetime.date(2026, 11, 11)", "datetime.date(2026, 11, 11)"),
    ],
)
def test_plain_scalar_resolution_in_pyyaml_and_ruamel(
    scalar: str, yaml_1_1: str, yaml_1_2: str
) -> None:
    old: Any = load_yaml_1_1(f"v: {scalar}\n")
    new: Any = load_yaml_1_2(f"v: {scalar}\n")
    assert (repr(old["v"]), repr(new["v"])) == (yaml_1_1, yaml_1_2)


@pytest.mark.unit
def test_core_schema_resolver_reproduces_yaml_1_2_2_example_10_9() -> None:
    resolved = [
        resolve_core_schema(text)
        for text in ("null", "", "true", "True", "false", "FALSE", "0", "0o7", "0x3A", "-19")
    ]
    assert resolved == [None, None, True, True, False, False, 0, 7, 58, -19]
    assert [type(value) for value in resolved[2:]] == [bool] * 4 + [int] * 4
    floats = [resolve_core_schema(text) for text in ("0.", "-0.0", ".5", "+12e03", "-2E+05")]
    assert floats == [0.0, -0.0, 0.5, 12000.0, -200000.0]
    assert all(type(value) is float for value in floats)
    assert repr([resolve_core_schema(text) for text in (".inf", "-.Inf", "+.INF", ".NAN")]) == (
        "[inf, -inf, inf, nan]"
    )
    yaml_1_1_only = ["NO", "yes", "on", "off", "0b101", "1_000", "12:30", "<<", "="]
    assert [resolve_core_schema(text) for text in yaml_1_1_only] == yaml_1_1_only


@pytest.mark.unit
def test_ruamel_yaml_1_2_agrees_with_the_core_schema_except_for_its_yaml_1_1_leftovers() -> None:
    corpus = ["NO", "on", "true", "~", "7", "010", "0o17", "0x1F", "1e3", "3.10", ".5", "12:30"]
    corpus += ["-.5", "+1", "1.5e+3", "null", "x", "3.1.0"]
    for text in corpus:
        loaded: Any = load_yaml_1_2(f"v: {text}\n")
        assert repr(loaded["v"]) == repr(resolve_core_schema(text)), text
    leftovers = {"0b101": 5, "1_000": 1000, "-0x1F": -31}
    for text, extension in leftovers.items():
        loaded = load_yaml_1_2(f"v: {text}\n")
        assert loaded["v"] == extension
        assert resolve_core_schema(text) == text


@pytest.mark.unit
def test_resolve_yaml_1_1_matches_pyyaml_safe_load_scalar_by_scalar() -> None:
    corpus = ["NO", "Yes", "off", "y", "n", "010", "0755", "08", "0x1F", "0b101", "1_000", "+1"]
    corpus += ["12:30", "190:20:30.15", "1e3", "1.5e+3", ".5", "-.5", "3.10", "~", "null", "x"]
    corpus += ["2026-11-11", "2026-11-11T10:00:00Z", ".inf", "0o17", "true"]
    for text in corpus:
        loaded: Any = load_yaml_1_1(f"v: {text}\n")
        assert repr(resolve_yaml_1_1(text)) == repr(loaded["v"]), text
    for unconstructible in ("=", "<<"):
        with pytest.raises(yaml.constructor.ConstructorError, match="could not determine"):
            resolve_yaml_1_1(unconstructible)
        with pytest.raises(yaml.constructor.ConstructorError, match="could not determine"):
            load_yaml_1_1(f"v: {unconstructible}\n")


@pytest.mark.unit
def test_six_loaders_read_one_document_six_ways() -> None:
    schemathesis_view: Any = deserialize_yaml(TOOL_PROBE)
    path_view: Any = yaml.load(TOOL_PROBE, Loader=JsonschemaSafeLoader)
    codegen_view: Any = yaml.load(TOOL_PROBE, Loader=get_safe_loader())
    pyyaml_view: Any = load_yaml_1_1(TOOL_PROBE)
    ruamel_view: Any = load_yaml_1_2(TOOL_PROBE)
    day = pyyaml_view["day"]
    assert repr(day) == "datetime.date(2026, 11, 11)"
    common = {"version": 3.1}
    assert pyyaml_view == common | {
        "country": False,
        "flag": True,
        "rate": "1e3",
        "zip": 8,
        "window": 750,
        "day": day,
        "responses": {200: "ok", True: "x"},
    }
    assert ruamel_view == common | {
        "country": "NO",
        "flag": "on",
        "rate": 1000.0,
        "zip": 10,
        "window": "12:30",
        "day": day,
        "responses": {200: "ok", "on": "x"},
    }
    assert schemathesis_view == common | {
        "country": "NO",
        "flag": "on",
        "rate": 1000.0,
        "zip": 8,
        "window": 750,
        "day": "2026-11-11",
        "responses": {"200": "ok", "on": "x"},
    }
    assert path_view == common | {
        "country": False,
        "flag": True,
        "rate": 1000.0,
        "zip": 8,
        "window": 750,
        "day": "2026-11-11",
        "responses": {200: "ok", True: "x"},
    }
    assert codegen_view == common | {
        "country": "NO",
        "flag": "on",
        "rate": 1000.0,
        "zip": 8,
        "window": 750,
        "day": "2026-11-11",
        "responses": {200: "ok", "on": "x"},
    }
    assert [type(key) for key in pyyaml_view["responses"]] == [int, bool]
    assert [type(key) for key in schemathesis_view["responses"]] == [str, str]
    assert _hazard(TOOL_PROBE) == ("version-dependent-scalar", "/country")


@pytest.mark.unit
def test_integer_response_keys_collide_in_json_and_crash_the_validator() -> None:
    text = CLEAN.replace('"200":\n          description: Rates', "200: {description: a}")
    text = text.replace("      responses:\n", '      responses:\n        "200": {description: b}\n')
    document: Any = load_yaml_1_1(text)
    responses = document["paths"]["/rates"]["get"]["responses"]
    assert list(responses) == ["200", 200]
    serialized = json.dumps(responses)
    assert serialized == '{"200": {"description": "b"}, "200": {"description": "a"}}'
    assert json.loads(serialized) == {"200": {"description": "a"}}
    with pytest.raises(TypeError, match="not supported between instances"):
        json.dumps(responses, sort_keys=True)
    del responses["200"]
    with pytest.raises(ValueError, match="Dict key must be str"):
        validate(document)
    assert _hazard(text) == ("non-string-key", "/paths/~1rates/get/responses")


@pytest.mark.unit
def test_unquoted_info_version_fails_schema_validation_under_both_yaml_versions() -> None:
    text = CLEAN.replace('version: "1.10"', "version: 1.10")
    for loader in (load_yaml_1_1, load_yaml_1_2):
        document: Any = loader(text)
        assert document["info"]["version"] == 1.1
        with pytest.raises(OpenAPIValidationError, match=r'1\.1 is not of type "string"'):
            validate(document)
    clean: Any = load_openapi_yaml(CLEAN)
    assert clean["info"]["version"] == "1.10"
    validate(clean)


@pytest.mark.unit
def test_mistyped_string_literals_are_found_after_either_loader() -> None:
    assert find_mistyped_string_literals(load_yaml_1_1(NORWAY)) == (
        "/components/schemas/Toggle/properties/zip/example",
        "/paths/~1rates/get/parameters/0/schema/default",
        "/paths/~1rates/get/parameters/0/schema/enum/2",
        "/paths/~1rates/get/parameters/1/schema/example",
    )
    assert find_mistyped_string_literals(load_yaml_1_2(NORWAY)) == (
        "/components/schemas/Toggle/properties/zip/example",
    )
    versions = "type: string\nenum: [3.9, 3.10, 3.11]\nconst: 3.10\n"
    for loader in (load_yaml_1_1, load_yaml_1_2):
        assert find_mistyped_string_literals(loader(versions)) == (
            "/const",
            "/enum/0",
            "/enum/1",
            "/enum/2",
        )
    assert find_mistyped_string_literals(load_openapi_yaml(CLEAN)) == ()
    nullable = "type: [string, 'null']\nenum: [NO, 1, null, ok]\nexamples: [NO, ok]\n"
    assert find_mistyped_string_literals(load_yaml_1_1(nullable)) == (
        "/enum/0",
        "/enum/1",
        "/examples/0",
    )
    assert find_mistyped_string_literals(load_yaml_1_2(nullable)) == ("/enum/1",)
    assert find_mistyped_string_literals(load_yaml_1_1("type: string\nenum: [null]\n")) == (
        "/enum/0",
    )
    assert (
        find_mistyped_string_literals(load_yaml_1_1("type: [string, integer]\nenum: [1]\n")) == ()
    )


@pytest.mark.unit
def test_duplicate_path_key_drops_an_operation_in_pyyaml_and_raises_in_ruamel() -> None:
    text = "paths:\n  /pets:\n    get: {operationId: list}\n"
    text += "  /pets:\n    post: {operationId: add}\n"
    assert load_yaml_1_1(text) == {"paths": {"/pets": {"post": {"operationId": "add"}}}}
    with pytest.raises(_ruamel.constructor.DuplicateKeyError, match='found duplicate key "/pets"'):
        load_yaml_1_2(text)
    assert _hazard(text) == ("duplicate-key", "/paths")


@pytest.mark.unit
def test_alias_bomb_is_a_28_node_graph_until_something_walks_it_as_a_tree() -> None:
    text = _alias_bomb(9, 9)
    tree = 10
    total = 1 + 9 + tree
    for _ in range(8):
        tree = 1 + 9 * tree
        total += tree
    assert measure_expansion(text) == ExpansionReport(
        stream_bytes=432, graph_nodes=28, tree_nodes=total, cyclic=False
    )
    assert total == 490_329_064
    for loader in (load_yaml_1_1, load_yaml_1_2):
        loaded: Any = loader(text)
        assert loaded["a8"][0] is loaded["a7"]
        assert loaded["a8"][8] is loaded["a8"][0]
    small: Any = load_yaml_1_1(_alias_bomb(5, 9))
    expanded = json.dumps(small["a4"])
    assert expanded.count('"lol"') == 9**5
    assert (len(_alias_bomb(5, 9)), len(expanded)) == (248, 428_103)


@pytest.mark.unit
def test_strict_loader_charges_every_alias_its_expanded_size() -> None:
    assert _hazard(_alias_bomb(9, 9)) == ("node-budget", "/a5/0")
    modest = _alias_bomb(3, 3)
    assert measure_expansion(modest).tree_nodes == 4 + 4 + 13 + 40
    assert load_openapi_yaml(modest, Limits(max_nodes=61)) == load_yaml_1_1(modest)
    assert _hazard(modest, Limits(max_nodes=60)) == ("node-budget", "/a2/2")


@pytest.mark.unit
def test_cyclic_alias_loads_as_a_self_referential_object_that_json_cannot_serialize() -> None:
    text = "x: &x\n  y: *x\n"
    assert measure_expansion(text) == ExpansionReport(
        stream_bytes=14, graph_nodes=4, tree_nodes=None, cyclic=True
    )
    for loader in (load_yaml_1_1, load_yaml_1_2):
        loaded: Any = loader(text)
        assert loaded["x"]["y"] is loaded["x"]
        with pytest.raises(ValueError, match="Circular reference detected"):
            json.dumps(loaded)
    assert _hazard(text) == ("cyclic-alias", "/x/y")
    assert _hazard("a: *missing\n") == ("undefined-alias", "/a")
    assert _hazard("a: &n 1\nb: &n 2\n") == ("duplicate-anchor", "/b")


@pytest.mark.unit
def test_merge_keys_copy_entries_exponentially_inside_safe_load() -> None:
    levels = 12
    text = _merge_bomb(levels)
    loader = yaml.SafeLoader(text)
    try:
        root = loader.get_single_node()
        document = loader.construct_document(root)
    finally:
        loader.dispose()
    last_mapping = root.value[-1][1]
    assert len(text) == 296
    assert len(last_mapping.value) == 2 ** (levels - 1)
    assert document[f"m{levels - 1}"] == {"k": "v"}
    assert _hazard(text) == ("merge-key", "/m1")
    merged: Any = load_yaml_1_1("base: &b {type: object}\nPet:\n  <<: *b\n  type: array\n")
    assert merged["Pet"] == {"type": "array"}


@pytest.mark.unit
def test_deep_nesting_overflows_recursive_composers_but_not_the_event_loop() -> None:
    depth = 1100
    text = "[" * depth + "]" * depth
    for loader in (load_yaml_1_1, load_yaml_1_2):
        with pytest.raises(RecursionError):
            loader(text)
    assert _hazard(text)[0] == "too-deep"
    node = load_openapi_yaml(text, Limits(max_depth=depth))
    levels = 0
    while isinstance(node, list):
        levels += 1
        node = node[0] if node else None
    assert levels == depth
    assert _hazard(text, Limits(max_depth=depth - 1))[0] == "too-deep"


@pytest.mark.unit
def test_tags_outside_the_json_schema_are_rejected_and_full_loader_resolves_python_names() -> None:
    payload = "x: !!python/name:os.system\n"
    with pytest.raises(TypeError, match="missing 1 required positional argument: 'Loader'"):
        yaml.load(payload)
    with pytest.raises(yaml.constructor.ConstructorError, match="could not determine"):
        load_yaml_1_1(payload)
    resolved = yaml.full_load(payload)["x"]
    assert (resolved.__module__, resolved.__name__) == (os.name, "system")
    assert callable(resolved)
    assert load_yaml_1_1("x: !!binary aGk=\n") == {"x": b"hi"}
    assert load_yaml_1_1("x: !!set {a}\n") == {"x": {"a"}}
    rejected = (payload, "x: !!binary aGk=\n", "x: !!set {a}\n", "x: !mine 1\n", "x: !!int a\n")
    for text in (*rejected, "x: !!bool yes\n", "x: !!float 1\n", "x: ! [1]\n", "x: !!seq {a: 1}\n"):
        assert _hazard(text) == ("tag", "/x")
    assert load_openapi_yaml("a: !!str 123\nb: !!int 7\nc: !!null ~\nd: !!map {e: !!seq []}\n") == {
        "a": "123",
        "b": 7,
        "c": None,
        "d": {"e": []},
    }


@pytest.mark.unit
def test_explicit_and_non_specific_tags_do_not_hide_version_dependent_scalars() -> None:
    for text in (
        "a: !!int 010\n",
        "a: !<tag:yaml.org,2002:int> 010\n",
        "%TAG !e! tag:yaml.org,2002:\n---\na: !e!int 010\n",
    ):
        assert (load_yaml_1_1(text), load_yaml_1_2(text)) == ({"a": 8}, {"a": 10})
        assert _hazard(text) == ("version-dependent-scalar", "/a")
    assert (load_yaml_1_1("a: ! NO\n"), load_yaml_1_2("a: ! NO\n")) == ({"a": False}, {"a": "NO"})
    assert (load_yaml_1_1("a: ! 12\n"), load_yaml_1_2("a: ! 12\n")) == ({"a": 12}, {"a": 12})
    assert _hazard("a: ! NO\n") == ("tag", "/a")
    assert _hazard("a: ! 12\n") == ("tag", "/a")
    merge = "b: &b {type: object}\nP:\n  ! <<: *b\n  x: 1\n"
    for loader in (load_yaml_1_1, load_yaml_1_2):
        merged: Any = loader(merge)
        assert merged["P"] == {"type": "object", "x": 1}
    assert _hazard(merge) == ("tag", "/P")
    literal = "b: &b {t: o}\nP:\n  !!str <<: *b\n"
    assert load_openapi_yaml(literal) == load_yaml_1_1(literal) == load_yaml_1_2(literal)
    assert load_openapi_yaml(literal) == {"b": {"t": "o"}, "P": {"<<": {"t": "o"}}}


@pytest.mark.unit
def test_yaml_directive_switches_ruamel_to_yaml_1_1_and_is_rejected_unless_1_2() -> None:
    legacy = "%YAML 1.1\n---\na: y\n"
    assert (load_yaml_1_1(legacy), load_yaml_1_2(legacy)) == ({"a": "y"}, {"a": True})
    assert _hazard(legacy) == ("yaml-version", "")
    future = "%YAML 1.3\n---\na: y\n"
    assert load_yaml_1_1(future) == {"a": "y"}
    with pytest.raises(AssertionError, match="version minor part can only be 2 or 1"):
        load_yaml_1_2(future)
    assert _hazard(future) == ("yaml-version", "")
    current = "%YAML 1.2\n---\na: y\nb: NO\n"
    assert (load_yaml_1_1(current), load_yaml_1_2(current)) == (
        {"a": "y", "b": False},
        {"a": "y", "b": "NO"},
    )
    assert _hazard(current) == ("version-dependent-scalar", "/b")
    assert load_openapi_yaml("%YAML 1.2\n---\na: y\n") == {"a": "y"}


@pytest.mark.unit
def test_oversized_integers_escape_safe_load_as_value_error_not_yaml_error() -> None:
    for text in ("a: " + "1" * 5000 + "\n", "a: 0x" + "f" * 4000 + "\n"):
        for loader in (load_yaml_1_1, load_yaml_1_2):
            with pytest.raises(ValueError, match="Exceeds the limit") as caught:
                repr(loader(text))
            assert not isinstance(caught.value, yaml.YAMLError)
        assert _hazard(text) == ("unloadable-number", "/a")
    with pytest.raises(ValueError, match="invalid literal for int") as octal:
        load_yaml_1_1("a: !!int 08\n")
    assert not isinstance(octal.value, yaml.YAMLError)
    assert _hazard("a: \udc80\n") == ("syntax", "")


@pytest.mark.unit
def test_alias_depth_counts_against_the_nesting_limit() -> None:
    text = "a0: &a0 [[[1]]]\nb: [[*a0]]\n"
    expected = {"a0": [[[1]]], "b": [[[[[1]]]]]}
    assert load_yaml_1_1(text) == expected
    assert load_openapi_yaml(text, Limits(max_depth=6)) == expected
    assert _hazard(text, Limits(max_depth=5)) == ("too-deep", "/b/0/0")
    assert _hazard(text, Limits(max_depth=3)) == ("too-deep", "/a0/0/0")
    assert load_openapi_yaml("a: &a 1\nb: [[*a]]\n", Limits(max_depth=3)) == {"a": 1, "b": [[1]]}


@pytest.mark.unit
def test_stream_shape_and_size_limits() -> None:
    assert _hazard("a: 1\n---\nb: 2\n") == ("multi-document", "")
    assert list(yaml.safe_load_all("a: 1\n---\nb: 2\n")) == [{"a": 1}, {"b": 2}]
    assert _hazard("") == ("empty", "")
    assert measure_expansion("") == ExpansionReport(0, 0, 0, cyclic=False)
    assert _hazard("# comment only\n") == ("empty", "")
    assert load_openapi_yaml("---\n") is None
    assert _hazard("a: [1, 2\n") == ("syntax", "/a/2")
    assert _hazard("? [a, b]\n: 1\n") == ("non-string-key", "")
    assert _hazard("null: 1\n") == ("non-string-key", "")
    assert load_openapi_yaml("k: &k name\n*k : 1\n") == {"k": "name", "name": 1}
    text = "a: " + "x" * 61 + "\n"
    assert len(text.encode()) == 65
    assert _hazard(text, Limits(max_bytes=64)) == ("too-large", "")
    assert load_openapi_yaml(text, Limits(max_bytes=65)) == {"a": "x" * 61}


@pytest.mark.unit
@pytest.mark.parametrize(
    "scalar",
    [
        *("NO", "yes", "on", "010", "08", "0o17", "0b101", "1_000", "12:30", "1e3", "-.5", "="),
        *("2026-11-11", "2026-11-11T10:00:00Z"),
    ],
)
def test_version_dependent_plain_scalars_are_rejected_until_quoted(scalar: str) -> None:
    assert _hazard(f"a:\n  - {scalar}\n") == ("version-dependent-scalar", "/a/0")
    assert load_openapi_yaml(f'a:\n  - "{scalar}"\n') == {"a": [scalar]}


@pytest.mark.unit
def test_portable_scalars_load_and_non_finite_floats_do_not() -> None:
    text = "a: [true, False, null, ~, 7, -3, 0x1F, 1.5, 3.10, 1.5e+3, y, n, plain text, 3.1.0]\n"
    expected = [True, False, None, None, 7, -3, 31, 1.5, 3.1, 1500.0, "y", "n"]
    expected += ["plain text", "3.1.0"]
    assert load_openapi_yaml(text) == {"a": expected}
    assert load_yaml_1_1(text) == {"a": expected}
    assert load_yaml_1_2(text) == {"a": expected}
    for scalar in (".inf", "-.INF", ".nan"):
        assert _hazard(f"a: {scalar}\n") == ("non-json-float", "/a")
        with pytest.raises(ValueError, match="Out of range float values"):
            json.dumps(load_yaml_1_1(f"a: {scalar}\n"), allow_nan=False)
    assert json.dumps(load_yaml_1_1("a: .inf\n")) == '{"a": Infinity}'


@pytest.mark.unit
def test_clean_document_is_identical_under_every_loader_and_validates() -> None:
    strict = load_openapi_yaml(CLEAN)
    assert strict == load_yaml_1_1(CLEAN) == load_yaml_1_2(CLEAN)
    assert json.loads(json.dumps(strict, allow_nan=False)) == strict
    assert isinstance(strict, dict)
    validate(strict)


@pytest.mark.unit
def test_each_emitter_writes_plain_scalars_the_other_yaml_version_misreads() -> None:
    assert yaml.safe_dump({"v": "NO"}) == "v: 'NO'\n"
    for text, misread in (("1e3", 1000.0), ("0o17", 15), ("08", 8), ("-.5", -0.5)):
        emitted = yaml.safe_dump({"v": text})
        assert emitted == f"v: {text}\n"
        assert load_yaml_1_1(emitted) == {"v": text}
        assert load_yaml_1_2(emitted) == {"v": misread}
    emitter = _ruamel.YAML(typ="safe", pure=True)
    for text, misread in (("NO", False), ("on", True), ("12:30", 750)):
        buffer = io.StringIO()
        emitter.dump({"v": text}, buffer)
        assert buffer.getvalue() == f"{{v: {text}}}\n"
        assert load_yaml_1_2(buffer.getvalue()) == {"v": text}
        assert load_yaml_1_1(buffer.getvalue()) == {"v": misread}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("application/yaml", "registered"),
        ("Application/YAML; charset=utf-8", "registered"),
        ("application/openapi+yaml;version=3.1", "suffix"),
        ("application/vnd.oai.workflows+yaml", "suffix"),
        ("application/x-yaml", "deprecated-alias"),
        ("text/yaml", "deprecated-alias"),
        ("text/x-yaml", "deprecated-alias"),
        ("application/vnd.oai.openapi", None),
        ("application/json", None),
        ("application/yaml+json", None),
        ("application/+yaml", None),
        ("/x+yaml", None),
        ("foo/bar/baz+yaml", None),
        ("", None),
    ],
)
def test_media_type_classification_follows_rfc_9512_section_2(
    content_type: str, expected: str | None
) -> None:
    assert classify_yaml_media_type(content_type) == expected


def _client(limits: Limits | None = None) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(limits))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_endpoint_normalizes_clean_yaml_to_json_and_reports_the_media_type_class() -> None:
    async with _client() as client:
        for content_type, kind in (
            ("application/yaml", "registered"),
            ("application/openapi+yaml;version=3.1", "suffix"),
            ("text/yaml; charset=utf-8", "deprecated-alias"),
        ):
            response = await client.post(
                "/openapi/normalize", content=CLEAN, headers={"Content-Type": content_type}
            )
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/json"
            assert response.json() == {"mediaType": kind, "document": load_openapi_yaml(CLEAN)}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_endpoint_rejects_non_yaml_media_types_with_415() -> None:
    async with _client() as client:
        for content_type in ("application/json", "application/vnd.oai.openapi"):
            response = await client.post(
                "/openapi/normalize", content=CLEAN, headers={"Content-Type": content_type}
            )
            assert response.status_code == 415
            assert response.headers["content-type"] == "application/problem+json"
            assert response.json() == {
                "type": "about:blank",
                "title": "Unsupported Media Type",
                "status": 415,
            }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_endpoint_reports_hazards_as_422_problem_details_with_a_pointer() -> None:
    headers = {"Content-Type": "application/yaml"}
    async with _client() as client:
        response = await client.post("/openapi/normalize", content=NORWAY, headers=headers)
        assert response.status_code == 422
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json() == {
            "type": "about:blank",
            "title": "Unprocessable Content",
            "status": 422,
            "code": "version-dependent-scalar",
            "pointer": "/paths/~1rates/get/parameters/0/schema/enum/2",
            "detail": "plain scalar 'NO' is False in YAML 1.1 and 'NO' in YAML 1.2",
        }
        bomb = await client.post("/openapi/normalize", content=_alias_bomb(9, 9), headers=headers)
        assert (bomb.status_code, bomb.json()["code"]) == (422, "node-budget")
        huge = await client.post(
            "/openapi/normalize", content="a: " + "1" * 5000 + "\n", headers=headers
        )
        assert (huge.status_code, huge.json()["code"]) == (422, "unloadable-number")
        broken = await client.post("/openapi/normalize", content="a: [1, 2\n", headers=headers)
        assert (broken.status_code, broken.json()["title"], broken.json()["code"]) == (
            400,
            "Bad Request",
            "syntax",
        )
        latin = await client.post("/openapi/normalize", content=b"a: \xe9\n", headers=headers)
        assert (latin.status_code, latin.json()["detail"]) == (400, "body is not UTF-8")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_endpoint_stops_reading_the_body_at_the_byte_cap() -> None:
    sent = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal sent
        for _ in range(10):
            sent += 1
            yield b"# " + b"x" * 29 + b"\n"

    headers = {"Content-Type": "application/yaml"}
    async with _client(Limits(max_bytes=64)) as client:
        response = await client.post("/openapi/normalize", content=body(), headers=headers)
        assert response.status_code == 413
        assert response.json()["title"] == "Content Too Large"
        assert sent == 3
        exact = await client.post(
            "/openapi/normalize", content=b"a: " + b"x" * 60 + b"\n", headers=headers
        )
        assert (exact.status_code, len(exact.request.content)) == (200, 64)
