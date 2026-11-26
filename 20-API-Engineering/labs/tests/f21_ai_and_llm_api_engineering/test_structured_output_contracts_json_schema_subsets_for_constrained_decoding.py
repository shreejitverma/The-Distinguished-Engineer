"""Lab for Structured-Output-Contracts-JSON-Schema-Subsets-for-Constrained-Decoding."""

from __future__ import annotations

import dataclasses
import json
import warnings
from collections.abc import Callable  # noqa: TC003 - Pydantic resolves it at runtime
from typing import Annotated, Any, Literal, TypeGuard

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from api_labs.f21_ai_and_llm_api_engineering.structured_output_contracts import (
    ANTHROPIC_STYLE,
    CONSERVATIVE,
    OPENAI_STYLE,
    Completion,
    ContractViolationError,
    JsonObject,
    RefusalError,
    SchemaContractError,
    TruncatedError,
    allowed_next_keys,
    apply_profile,
    compile_strict_schema,
    find_ref_cycle,
    parse_completion,
    structural_fingerprint,
    unsupported_regex_features,
)


class Dog(BaseModel):
    kind: Literal["dog"] = "dog"
    bark: int = Field(ge=0, le=10)


class Cat(BaseModel):
    kind: Literal["cat"]
    lives: int


class Ticket(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    priority: int = Field(default=3, ge=1, le=5)
    pet: Annotated[Dog | Cat, Field(discriminator="kind")]
    note: str | None = None


class StrictTicket(Ticket):
    model_config = ConfigDict(extra="forbid")


class Node(BaseModel):
    value: str
    children: list[Node] = []


class Ping(BaseModel):
    pong: Pong | None = None


class Pong(BaseModel):
    ping: Ping | None = None


def _is_obj(value: object) -> TypeGuard[JsonObject]:
    return isinstance(value, dict)


def _is_list(value: object) -> TypeGuard[list[Any]]:
    return isinstance(value, list)


def _children(schema: object) -> list[Any]:
    """Direct children of a JSON container."""
    if _is_obj(schema):
        return list(schema.values())
    if _is_list(schema):
        return list(schema)
    return []


def _objects(schema: object) -> list[JsonObject]:
    """Every dict in the schema tree that has a ``properties`` mapping."""
    found: list[JsonObject] = []
    if _is_obj(schema) and _is_obj(schema.get("properties")):
        found.append(schema)
    for child in _children(schema):
        found.extend(_objects(child))
    return found


def _keys_anywhere(schema: object) -> set[str]:
    keys: set[str] = set(schema) if _is_obj(schema) else set()
    for child in _children(schema):
        keys |= _keys_anywhere(child)
    return keys


def _valid(schema: JsonObject, instance: object) -> bool:
    validator: Any = Draft202012Validator(schema)
    return bool(validator.is_valid(instance))


def _nest(levels: int) -> JsonObject:
    node: JsonObject = {"type": "object", "properties": {"leaf": {"type": "string"}}}
    for _ in range(levels - 1):
        node = {"type": "object", "properties": {"child": node}}
    return node


def _codes(exc: pytest.ExceptionInfo[SchemaContractError]) -> list[str]:
    return [v.code for v in exc.value.violations]


@pytest.mark.unit
def test_every_object_is_closed_and_requires_every_property_in_field_order() -> None:
    compiled = compile_strict_schema(Ticket, OPENAI_STYLE)
    objects = _objects(compiled.schema)
    assert len(objects) == 3  # Ticket, Dog, Cat
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])
    assert list(compiled.schema["properties"]) == ["title", "priority", "pet", "note"]


@pytest.mark.unit
def test_defaults_are_dropped_and_default_bearing_fields_become_required() -> None:
    raw = Ticket.model_json_schema()
    assert raw["required"] == ["title", "pet"]  # Pydantic marks default fields optional
    assert "default" in _keys_anywhere(raw)
    compiled = compile_strict_schema(Ticket, OPENAI_STYLE)
    assert "default" not in _keys_anywhere(compiled.schema)
    assert {"priority", "note"} <= set(compiled.schema["required"])


@pytest.mark.unit
def test_discriminated_union_becomes_any_of_without_openapi_discriminator() -> None:
    raw = Ticket.model_json_schema()
    assert "oneOf" in raw["properties"]["pet"]
    assert "discriminator" in raw["properties"]["pet"]
    pet = compile_strict_schema(Ticket, OPENAI_STYLE).schema["properties"]["pet"]
    assert "oneOf" not in pet
    assert "discriminator" not in pet
    assert [branch["$ref"] for branch in pet["anyOf"]] == ["#/$defs/Dog", "#/$defs/Cat"]


@pytest.mark.unit
def test_fields_named_like_keywords_are_not_treated_as_keywords() -> None:
    class Tricky(BaseModel):
        title: str
        default: int
        minimum: int
        pattern: str

    compiled = compile_strict_schema(Tricky, ANTHROPIC_STYLE)
    assert list(compiled.schema["properties"]) == ["title", "default", "minimum", "pattern"]
    assert compiled.schema["required"] == ["title", "default", "minimum", "pattern"]
    assert "description" not in compiled.schema
    assert compiled.client_enforced == ()


@pytest.mark.unit
def test_describe_treatment_moves_constraints_and_the_client_still_enforces_them() -> None:
    compiled = compile_strict_schema(Ticket, ANTHROPIC_STYLE)
    priority = compiled.schema["properties"]["priority"]
    assert not {"minimum", "maximum"} & set(priority)
    assert priority["description"] == "(constraints: maximum=5, minimum=1)"
    assert "/properties/priority: maximum" in compiled.client_enforced
    # The decoder was never told priority <= 5, so only the Pydantic model can catch this.
    body = json.dumps(
        {"title": "x", "priority": 9, "pet": {"kind": "cat", "lives": 7}, "note": None}
    )
    with pytest.raises(ContractViolationError) as caught:
        parse_completion(Ticket, Completion(body, "stop"))
    assert caught.value.error_types == ("less_than_equal",)


@pytest.mark.unit
def test_keep_treatment_leaves_constraints_in_the_schema() -> None:
    compiled = compile_strict_schema(Ticket, OPENAI_STYLE)
    assert compiled.schema["properties"]["priority"]["minimum"] == 1
    assert compiled.client_enforced == ()


@pytest.mark.unit
def test_conservative_profile_reports_every_rejected_constraint_with_its_pointer() -> None:
    with pytest.raises(SchemaContractError) as caught:
        compile_strict_schema(Ticket, CONSERVATIVE)
    paths = sorted(v.path for v in caught.value.violations)
    assert set(_codes(caught)) == {"constraint-rejected"}
    assert paths == [
        "/$defs/Dog/properties/bark",
        "/$defs/Dog/properties/bark",
        "/properties/priority",
        "/properties/priority",
        "/properties/title",
        "/properties/title",
    ]


@pytest.mark.unit
def test_recursive_model_is_rejected_only_where_the_profile_forbids_recursion() -> None:
    with pytest.raises(SchemaContractError) as caught:
        compile_strict_schema(Node, ANTHROPIC_STYLE)
    assert _codes(caught) == ["recursive-ref"]
    assert caught.value.violations[0].message == "Node -> Node"
    compiled = compile_strict_schema(Node, OPENAI_STYLE)
    assert compiled.schema["$ref"] == "#/$defs/Node"


@pytest.mark.unit
def test_mutual_recursion_cycle_is_reported_with_its_path() -> None:
    cycle = find_ref_cycle(Ping.model_json_schema())
    assert cycle is not None
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {"Ping", "Pong"}
    assert find_ref_cycle(compile_strict_schema(Ticket, OPENAI_STYLE).schema) is None


@pytest.mark.unit
def test_open_maps_and_extra_allow_are_flagged_not_silently_closed() -> None:
    class WithMap(BaseModel):
        tags: dict[str, int]

    class Open(BaseModel):
        model_config = ConfigDict(extra="allow")
        name: str

    with pytest.raises(SchemaContractError) as map_error:
        compile_strict_schema(WithMap, OPENAI_STYLE)
    assert [(v.path, v.code) for v in map_error.value.violations] == [
        ("/properties/tags", "open-object")
    ]
    with pytest.raises(SchemaContractError) as open_error:
        compile_strict_schema(Open, OPENAI_STYLE)
    assert [(v.path, v.code) for v in open_error.value.violations] == [("", "open-object")]


@pytest.mark.unit
def test_root_must_be_an_object_and_not_any_of_when_the_profile_says_so() -> None:
    with pytest.raises(SchemaContractError) as array_root:
        apply_profile({"type": "array", "items": {"type": "string"}}, OPENAI_STYLE)
    assert _codes(array_root) == ["root-not-object"]
    union_root = {"anyOf": [Dog.model_json_schema(), Cat.model_json_schema()]}
    with pytest.raises(SchemaContractError) as any_of_root:
        apply_profile(union_root, OPENAI_STYLE)
    assert _codes(any_of_root) == ["root-any-of"]
    assert apply_profile(union_root, ANTHROPIC_STYLE).schema["anyOf"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        (r"^\d+$", []),
        (r"(?=a)b", ["lookahead"]),
        (r"(?!a)b", ["lookahead"]),
        (r"(?<=a)b", ["lookbehind"]),
        (r"(?<!a)b", ["lookbehind"]),
        (r"(a)\1", ["backreference"]),
        (r"\bfoo\b", ["word-boundary"]),
        (r"[\b]", []),
        (r"[(?=]", []),
        (r"\\1", []),
        (r"\(?=x", []),
        (r"(?=a)(a)\1", ["backreference", "lookahead"]),
    ],
)
def test_regex_scan_finds_non_regular_constructs_and_respects_escapes(
    pattern: str, expected: list[str]
) -> None:
    assert unsupported_regex_features(pattern) == expected


@pytest.mark.unit
def test_pattern_violation_is_reported_at_the_field_pointer() -> None:
    class Coded(BaseModel):
        # The default 'rust-regex' engine refuses lookaround at class creation, so opt in.
        model_config = ConfigDict(regex_engine="python-re")
        code: str = Field(pattern=r"(?=A)\w+")

    with pytest.raises(SchemaContractError) as caught:
        compile_strict_schema(Coded, OPENAI_STYLE)
    assert [(v.path, v.code) for v in caught.value.violations] == [
        ("/properties/code", "unsupported-regex")
    ]


@pytest.mark.unit
def test_format_outside_the_allow_list_is_rejected() -> None:
    class Blob(BaseModel):
        data: bytes
        when: str = Field(json_schema_extra={"format": "date-time"})

    with pytest.raises(SchemaContractError) as caught:
        compile_strict_schema(Blob, OPENAI_STYLE)
    assert [(v.path, v.code) for v in caught.value.violations] == [
        ("/properties/data", "unsupported-format")
    ]


@pytest.mark.unit
def test_pydantic_silently_drops_an_unrepresentable_union_branch_and_we_refuse() -> None:
    class Handler(BaseModel):
        action: int | Callable[[int], int]

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        plain = Handler.model_json_schema()  # no warning: 'skipped-choice' is ignored
    assert plain["properties"]["action"] == {"title": "Action", "type": "integer"}
    with pytest.raises(SchemaContractError) as caught:
        compile_strict_schema(Handler, OPENAI_STYLE)
    assert _codes(caught) == ["skipped-choice"]


@pytest.mark.unit
def test_non_serializable_default_is_an_error_not_a_warning() -> None:
    class Blob(BaseModel):
        data: bytes = b"\xff"

    with pytest.raises(SchemaContractError) as caught:
        compile_strict_schema(Blob, CONSERVATIVE)
    assert _codes(caught) == ["non-serializable-default"]


@pytest.mark.unit
def test_nesting_limit_counts_object_levels() -> None:
    assert apply_profile(_nest(10), OPENAI_STYLE).schema
    with pytest.raises(SchemaContractError) as caught:
        apply_profile(_nest(11), OPENAI_STYLE)
    assert _codes(caught) == ["too-deep"]
    assert caught.value.violations[0].message == "11"


@pytest.mark.unit
def test_property_and_enum_budgets_are_enforced() -> None:
    tight = dataclasses.replace(OPENAI_STYLE, max_properties=3, max_enum_values=2)
    ok: JsonObject = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "enum": ["x", "y"]},
            "b": {"type": "string"},
            "c": {"type": "string"},
        },
    }
    assert apply_profile(ok, tight).schema
    too_many = {"type": "object", "properties": {**ok["properties"], "d": {"type": "string"}}}
    with pytest.raises(SchemaContractError) as props:
        apply_profile(too_many, tight)
    assert _codes(props) == ["too-many-properties"]
    wide = {"type": "object", "properties": {"a": {"type": "string", "enum": ["x", "y", "z"]}}}
    with pytest.raises(SchemaContractError) as enums:
        apply_profile(wide, tight)
    assert _codes(enums) == ["too-many-enum-values"]


@pytest.mark.unit
def test_external_and_dangling_refs_are_unresolved() -> None:
    external = {"type": "object", "properties": {"x": {"$ref": "https://example.com/x.json"}}}
    dangling = {"type": "object", "properties": {"x": {"$ref": "#/$defs/Missing"}}}
    for schema in (external, dangling):
        with pytest.raises(SchemaContractError) as caught:
            apply_profile(schema, OPENAI_STYLE)
        assert _codes(caught) == ["unresolved-ref"]


@pytest.mark.unit
def test_unknown_keyword_is_reported_not_passed_through() -> None:
    schema = {"type": "object", "properties": {"x": {"type": "string", "not": {"const": "a"}}}}
    with pytest.raises(SchemaContractError) as caught:
        apply_profile(schema, OPENAI_STYLE)
    assert [(v.path, v.code) for v in caught.value.violations] == [
        ("/properties/x", "unsupported-keyword")
    ]
    assert "'not'" in caught.value.violations[0].message


@pytest.mark.unit
def test_compiled_schema_accepts_valid_output_and_rejects_open_or_partial_objects() -> None:
    compiled = compile_strict_schema(StrictTicket, OPENAI_STYLE)
    good = {"title": "t", "priority": 2, "pet": {"kind": "dog", "bark": 3}, "note": None}
    assert _valid(compiled.schema, good)
    assert not _valid(compiled.schema, {**good, "extra": 1})
    assert not _valid(compiled.schema, {k: v for k, v in good.items() if k != "priority"})
    # The raw Pydantic schema lets the model omit priority and add keys; the compiled one does not.
    raw = Ticket.model_json_schema()
    assert _valid(raw, {k: v for k, v in good.items() if k != "priority"})
    assert _valid(raw, {**good, "extra": 1})


@pytest.mark.unit
def test_next_key_mask_is_finite_only_for_closed_objects() -> None:
    obj = compile_strict_schema(Ticket, OPENAI_STYLE).schema
    assert allowed_next_keys(obj, []) == frozenset({"title"})
    assert allowed_next_keys(obj, ["title", "priority"]) == frozenset({"pet"})
    assert allowed_next_keys(obj, ["title", "priority", "pet", "note"]) == frozenset()
    assert allowed_next_keys(Ticket.model_json_schema(), []) is None
    with pytest.raises(ValueError, match="not a prefix"):
        allowed_next_keys(obj, ["priority"])


@pytest.mark.unit
def test_parse_completion_checks_the_stop_reason_before_the_text() -> None:
    body = json.dumps(
        {"title": "t", "priority": 2, "pet": {"kind": "dog", "bark": 3}, "note": None}
    )
    assert parse_completion(Ticket, Completion(body, "stop")).priority == 2
    with pytest.raises(RefusalError):
        parse_completion(Ticket, Completion(body, "refusal"))
    with pytest.raises(TruncatedError):  # valid JSON, but the stop reason still wins
        parse_completion(Ticket, Completion(body, "length"))
    with pytest.raises(ContractViolationError) as cut:
        parse_completion(Ticket, Completion(body[:-10], "stop"))
    assert cut.value.error_types == ("json_invalid",)


@pytest.mark.unit
def test_client_model_must_forbid_extra_keys_itself() -> None:
    body = json.dumps(
        {"title": "t", "priority": 2, "pet": {"kind": "dog", "bark": 3}, "note": None, "x": 1}
    )
    assert parse_completion(Ticket, Completion(body, "stop")).title == "t"  # silently ignored
    with pytest.raises(ContractViolationError) as caught:
        parse_completion(StrictTicket, Completion(body, "stop"))
    assert caught.value.error_types == ("extra_forbidden",)


@pytest.mark.unit
def test_fingerprint_ignores_annotations_but_not_property_names() -> None:
    class Plain(BaseModel):
        text: str

    class Described(BaseModel):
        """A documented model."""

        text: str = Field(description="the text", title="Text Field")

    class Renamed(BaseModel):
        description: str

    class Other(BaseModel):
        summary: str

    assert structural_fingerprint(Plain.model_json_schema()) == structural_fingerprint(
        Described.model_json_schema()
    )
    assert structural_fingerprint(Renamed.model_json_schema()) != structural_fingerprint(
        Other.model_json_schema()
    )


@pytest.mark.unit
def test_compilation_is_deterministic_and_does_not_mutate_the_input() -> None:
    first = compile_strict_schema(Ticket, ANTHROPIC_STYLE)
    second = compile_strict_schema(Ticket, ANTHROPIC_STYLE)
    assert json.dumps(first.schema) == json.dumps(second.schema)
    assert first.fingerprint == second.fingerprint
    raw = Ticket.model_json_schema()
    snapshot = json.dumps(raw)
    apply_profile(raw, ANTHROPIC_STYLE)
    assert json.dumps(raw) == snapshot
