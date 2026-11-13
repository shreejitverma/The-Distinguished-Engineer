"""Closed-object composition: additionalProperties vs unevaluatedProperties (2020-12)."""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import FastAPI
from jsonschema import Draft202012Validator, validators  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

type Schema = dict[str, Any] | bool

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
DRAFT_07 = "http://json-schema.org/draft-07/schema#"
SAMPLE: dict[str, Any] = {"street": "1 Main St", "city": "Leeds", "kind": "business", "extra": 1}
_JSONSCHEMA: Any = validators  # jsonschema 4.26.0 ships no py.typed marker


def address_schema(*, closed: bool) -> dict[str, Any]:
    """Base object schema; closed=True adds the keyword that only sees its own schema object."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"street": {"type": "string"}, "city": {"type": "string"}},
        "required": ["street", "city"],
    }
    if closed:
        schema["additionalProperties"] = False
    return schema


def kind_schema(*, closed: bool) -> dict[str, Any]:
    """Extension object schema that adds the kind property."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"kind": {"enum": ["residential", "business"]}},
        "required": ["kind"],
    }
    if closed:
        schema["additionalProperties"] = False
    return schema


def trap_schema() -> dict[str, Any]:
    """Two individually closed schemas under allOf: no object can satisfy both."""
    return {
        "$schema": DRAFT_2020_12,
        "allOf": [address_schema(closed=True), kind_schema(closed=True)],
    }


def fixed_schema(dialect: str = DRAFT_2020_12) -> dict[str, Any]:
    """Open building blocks, closed once at the composition root by unevaluatedProperties."""
    return {
        "$schema": dialect,
        "$defs": {"address": address_schema(closed=False)},
        "allOf": [{"$ref": "#/$defs/address"}],
        **kind_schema(closed=False),
        "unevaluatedProperties": False,
    }


def redeclared_schema() -> dict[str, Any]:
    """Draft-07 compatible workaround: redeclare inherited names beside additionalProperties."""
    return {
        "$schema": DRAFT_07,
        "allOf": [address_schema(closed=False)],
        "type": "object",
        "properties": {"street": True, "city": True, "kind": {"enum": ["residential", "business"]}},
        "required": ["kind"],
        "additionalProperties": False,
    }


def closure_chain(depth: int) -> dict[str, Any]:
    """Wrap one leaf schema in depth layers of allOf, each layer closing itself again."""
    schema: dict[str, Any] = {"properties": {"a": {"type": "integer"}}}
    for _ in range(depth):
        schema = {"allOf": [schema], "unevaluatedProperties": False}
    return schema


@dataclass(frozen=True, slots=True)
class Violation:
    """One jsonschema error reduced to the fields a contract test asserts on."""

    keyword: str
    schema_path: tuple[str | int, ...]
    message: str


def violations(schema: Schema, instance: Any, validator_cls: Any = None) -> list[Violation]:
    """Validate with jsonschema; the class defaults to the one that $schema selects."""
    cls = _JSONSCHEMA.validator_for(schema) if validator_cls is None else validator_cls
    found = [
        Violation(str(error.validator), tuple(error.absolute_schema_path), str(error.message))
        for error in cls(schema).iter_errors(instance)
    ]
    return sorted(found, key=lambda item: (item.keyword, [str(part) for part in item.schema_path]))


def key_subsets(sample: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield every object that keeps a subset of the sample's properties (2**n objects)."""
    names = list(sample)
    for size in range(len(names) + 1):
        for chosen in itertools.combinations(names, size):
            yield {name: sample[name] for name in chosen}


def accepted_key_sets(schema: Schema, sample: Mapping[str, Any]) -> set[frozenset[str]]:
    """Exhaustively list the property-name sets that the schema accepts."""
    return {frozenset(obj) for obj in key_subsets(sample) if not violations(schema, obj)}


@dataclass(frozen=True, slots=True)
class Outcome:
    """Assertion result plus the property names annotated as evaluated at this location."""

    valid: bool
    evaluated: frozenset[str] = frozenset()


def _same(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def _has_type(instance: object, name: str) -> bool:
    if name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    kinds: dict[str, type] = {"object": dict, "string": str, "boolean": bool, "array": list}
    return isinstance(instance, kinds[name])


def evaluate(schema: Schema, instance: object, root: Schema | None = None) -> Outcome:
    """Reference evaluator for the closed-object keyword subset, following annotation rules."""
    root = schema if root is None else root
    if isinstance(schema, bool):
        return Outcome(schema)  # boolean schemas never produce annotations
    seen: set[str] = set()

    def in_place(subschema: Schema) -> bool:
        result = evaluate(subschema, instance, root)
        seen.update(result.evaluated)  # a failed subschema returns no annotations
        return result.valid

    checks: list[bool] = []
    if "$ref" in schema:
        target = cast("dict[str, Any]", root)["$defs"][schema["$ref"].removeprefix("#/$defs/")]
        checks.append(in_place(target))
    if "type" in schema:
        checks.append(_has_type(instance, schema["type"]))
    if "const" in schema:
        checks.append(_same(instance, schema["const"]))
    if "enum" in schema:
        checks.append(any(_same(instance, option) for option in schema["enum"]))
    if "allOf" in schema:
        checks.append(all([in_place(sub) for sub in schema["allOf"]]))
    if "anyOf" in schema:
        checks.append(any([in_place(sub) for sub in schema["anyOf"]]))  # examine every branch
    if "oneOf" in schema:
        passing = [r for sub in schema["oneOf"] if (r := evaluate(sub, instance, root)).valid]
        checks.append(len(passing) == 1)
        seen.update(passing[0].evaluated if len(passing) == 1 else ())
    if "not" in schema:
        checks.append(not evaluate(schema["not"], instance, root).valid)  # annotations discarded
    if "if" in schema:
        branch = schema.get("then" if in_place(schema["if"]) else "else", True)
        checks.append(in_place(branch))
    if isinstance(instance, dict):
        obj = cast("dict[str, Any]", instance)
        checks.append(all(name in obj for name in schema.get("required", ())))
        for name, sub in schema.get("dependentSchemas", {}).items():
            if name in obj:
                checks.append(in_place(sub))
        local: set[str] = set()  # annotations of properties and patternProperties in THIS object
        for name, sub in schema.get("properties", {}).items():
            if name in obj:
                local.add(name)
                checks.append(evaluate(sub, obj[name], root).valid)
        for pattern, sub in schema.get("patternProperties", {}).items():
            for name in obj:
                if re.search(pattern, name):
                    local.add(name)
                    checks.append(evaluate(sub, obj[name], root).valid)
        seen |= local
        if "additionalProperties" in schema:
            for name in obj.keys() - local:  # blind to allOf, $ref, and every other subschema
                checks.append(evaluate(schema["additionalProperties"], obj[name], root).valid)
                seen.add(name)
        if "unevaluatedProperties" in schema:
            for name in obj.keys() - seen:  # sees every successful in-place subschema above
                checks.append(evaluate(schema["unevaluatedProperties"], obj[name], root).valid)
                seen.add(name)
    return Outcome(True, frozenset(seen)) if all(checks) else Outcome(False)


def count_keyword_calls(schema: Schema, instance: Any, keyword: str = "properties") -> int:
    """Count how often jsonschema runs one keyword function while validating the instance."""
    original: Any = Draft202012Validator.VALIDATORS[keyword]
    calls = 0

    def counting(validator: Any, value: Any, target: Any, parent: Any) -> Iterator[Any]:
        nonlocal calls
        calls += 1
        yield from original(validator, value, target, parent)

    counting_cls = _JSONSCHEMA.extend(Draft202012Validator, {keyword: counting})
    list(counting_cls(schema).iter_errors(instance))
    return calls


class Address(BaseModel):
    """Closed write model: extra='forbid' compiles to additionalProperties: false."""

    model_config = ConfigDict(extra="forbid")

    street: str
    city: str


class BusinessAddress(Address):
    """Python inheritance flattens into one schema object, so no allOf is emitted."""

    kind: Literal["residential", "business"]


def build_address_app() -> FastAPI:
    """ASGI app whose OpenAPI document publishes the flattened closed schema."""
    app = FastAPI()

    @app.post("/addresses")
    async def create_address(address: BusinessAddress) -> BusinessAddress:
        return address

    return app
