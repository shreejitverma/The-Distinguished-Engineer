"""JSON Schema 2020-12 core vocabulary lab: identification, references, and assertions."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import jsonschema  # type: ignore[import-untyped]
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Iterable

    from referencing.jsonschema import Schema, SchemaRegistry
    from starlette.requests import Request

DIALECT = "https://json-schema.org/draft/2020-12/schema"
DRAFT_07 = "http://json-schema.org/draft-07/schema#"
_JSONSCHEMA: Any = jsonschema  # jsonschema 4.26.0 ships no py.typed marker

# Core Appendix A, with a const in every schema so a lookup result names what it found.
APPENDIX_A: dict[str, Any] = {
    "$id": "https://example.com/root.json",
    "$defs": {
        "A": {"$anchor": "foo", "const": "A"},
        "B": {
            "$id": "other.json",
            "const": "B",
            "$defs": {
                "X": {"$anchor": "bar", "const": "X"},
                "Y": {"$id": "t/inner.json", "$anchor": "bar", "const": "Y"},
            },
        },
        "C": {"$id": "urn:uuid:ee564b8a-7a87-4125-8c96-e9f123d6766f", "const": "C"},
    },
}

MONEY: dict[str, Any] = {
    "$schema": DIALECT,
    "$id": "https://schemas.example/common/money.json",
    "type": "object",
    "properties": {
        "amount": {"$ref": "#/$defs/minor-units"},
        "currency": {"$ref": "#currency"},
    },
    "required": ["amount", "currency"],
    "$defs": {
        "minor-units": {"type": "integer", "minimum": 0},
        "currency": {"$anchor": "currency", "type": "string", "pattern": "^[A-Z]{3}$"},
    },
}

ORDER: dict[str, Any] = {
    "$schema": DIALECT,
    "$id": "https://schemas.example/orders/order.json",
    "type": "object",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "total": {"$ref": "../common/money.json"},
        "lines": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/line"}},
        "gift": {"$ref": "gift.json"},
    },
    "required": ["id", "total", "lines"],
    "dependentRequired": {"coupon": ["coupon_issuer"]},
    "$defs": {
        "line": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "minLength": 1, "maxLength": 12},
                "qty": {"type": "integer", "minimum": 1, "maximum": 999},
                "bundle": {"type": "array", "items": {"$ref": "#/$defs/line"}},
            },
            "required": ["sku", "qty"],
        },
        "gift": {
            "$id": "gift.json",
            "type": "object",
            "properties": {"note": {"$ref": "#note"}, "wrap": {"$ref": "#/$defs/wrap"}},
            "$defs": {
                "note": {"$anchor": "note", "type": "string", "maxLength": 80},
                "wrap": {"enum": ["none", "paper", "box"]},
            },
        },
    },
}

# Core Appendix C: one recursive schema and its stricter extension.
TREE: dict[str, Any] = {
    "$schema": DIALECT,
    "$id": "https://example.com/tree",
    "$dynamicAnchor": "node",
    "type": "object",
    "properties": {
        "data": True,
        "children": {"type": "array", "items": {"$dynamicRef": "#node"}},
    },
}

STRICT_TREE: dict[str, Any] = {
    "$schema": DIALECT,
    "$id": "https://example.com/strict-tree",
    "$dynamicAnchor": "node",
    "$ref": "tree",
    "unevaluatedProperties": False,
}

# A generic container: "#item" is the type parameter, and the $defs entry is its default.
PAGE: dict[str, Any] = {
    "$schema": DIALECT,
    "$id": "https://schemas.example/generic/page.json",
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {"$dynamicRef": "#item"}},
        "next": {"type": ["string", "null"]},
    },
    "required": ["items"],
    "$defs": {"any-item": {"$dynamicAnchor": "item"}},
}

PAGE_OF_MONEY: dict[str, Any] = {
    "$schema": DIALECT,
    "$id": "https://schemas.example/generic/page-of-money.json",
    "$ref": "page.json",
    "$defs": {"item": {"$dynamicAnchor": "item", "$ref": "../common/money.json"}},
}

QUOTE: dict[str, Any] = {
    "$schema": DIALECT,
    "$id": "https://schemas.example/quotes/quote.json",
    "type": "object",
    "properties": {"price": {"type": "number", "minimum": 0, "maximum": 1000000}},
    "required": ["price"],
}


@dataclass(frozen=True, slots=True)
class Violation:
    """One validation error: where in the instance, which keyword, and which schema location."""

    pointer: str
    keyword: str
    schema_path: tuple[str | int, ...]


def resource_uris(schema: Schema) -> list[str]:
    """Return the canonical URI of every schema resource in one document, embedded ones too."""
    found: list[str] = []
    pending = [("", DRAFT202012.create_resource(schema))]
    while pending:
        base, resource = pending.pop()
        identifier = resource.id()
        if identifier is not None:
            base = urljoin(base, identifier)
            found.append(base)
        pending.extend((base, each) for each in resource.subresources())
    return sorted(found)


def closed_registry(*schemas: Schema) -> SchemaRegistry:
    """Register documents under their $id with no retrieval hook; refuse a URI claimed twice."""
    registry: SchemaRegistry = Registry()
    claimed: set[str] = set()
    for schema in schemas:
        resource = DRAFT202012.create_resource(schema)
        identifier = resource.id()
        if identifier is None:
            raise ValueError("a registered schema document needs an absolute $id")
        uris = resource_uris(schema)
        clash = sorted(uri for uri in set(uris) if uris.count(uri) > 1 or uri in claimed)
        if clash:
            raise ValueError(f"schema URI claimed twice: {clash}")
        claimed.update(uris)
        registry = registry.with_resource(identifier, resource)
    return registry.crawl()


def compile_validator(
    schema: Schema,
    registry: SchemaRegistry | None = None,
    *,
    assert_format: bool = False,
) -> Any:
    """Pick the class from $schema, check the schema against its meta-schema, close the registry."""
    validators = _JSONSCHEMA.validators
    cls = validators.validator_for(schema, default=validators.Draft202012Validator)
    cls.check_schema(schema)
    closed: SchemaRegistry = Registry() if registry is None else registry
    checker = cls.FORMAT_CHECKER if assert_format else None
    return cls(schema, registry=closed, format_checker=checker)


def _pointer(parts: Iterable[str | int]) -> str:
    """Encode instance path segments as an RFC 6901 JSON Pointer."""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def violations(validator: Any, instance: Any) -> list[Violation]:
    """Collect every error the validator reports for the instance, in a stable order."""
    found = [
        Violation(
            _pointer(error.absolute_path),
            str(error.validator),
            tuple(error.absolute_schema_path),
        )
        for error in validator.iter_errors(instance)
    ]
    return sorted(found, key=lambda item: (item.pointer, item.keyword, str(item.schema_path)))


def resolve(registry: SchemaRegistry, uri: str) -> Schema:
    """Dereference an absolute URI, with an optional fragment, against the registry."""
    return registry.resolver().lookup(uri).contents


def dangling_references(schema: Schema, registry: SchemaRegistry) -> list[str]:
    """Eagerly resolve every $ref and $dynamicRef in one document; return those that fail."""
    root = DRAFT202012.create_resource(schema)
    pending = [(root, registry.resolver_with_root(root))]
    dangling: list[str] = []
    while pending:
        resource, resolver = pending.pop()
        contents = resource.contents
        if isinstance(contents, bool):
            continue
        for keyword in ("$ref", "$dynamicRef"):
            reference = contents.get(keyword)
            if isinstance(reference, str):
                try:
                    resolver.lookup(reference)
                except Unresolvable:
                    dangling.append(reference)
        pending.extend((each, resolver.in_subresource(each)) for each in resource.subresources())
    return sorted(dangling)


def nested_tree(depth: int) -> dict[str, Any]:
    """Build a TREE instance whose single branch is depth children deep."""
    root: dict[str, Any] = {}
    node = root
    for _ in range(depth):
        child: dict[str, Any] = {}
        node["children"] = [child]
        node = child
    return root


def max_nesting_depth(validator: Any, upper: int = 2000) -> int:
    """Binary-search the deepest nested_tree the validator finishes before RecursionError."""
    low, high = 0, upper
    while low < high:
        middle = (low + high + 1) // 2
        try:
            validator.is_valid(nested_tree(middle))
        except RecursionError:
            high = middle - 1
        else:
            low = middle
    return low


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite JSON number token: {token}")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"JSON number overflows binary64: {text}")
    return value


def _unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    members = dict(pairs)
    if len(members) != len(pairs):
        raise ValueError("duplicate object member name")
    return members


def strict_json_loads(text: str | bytes) -> Any:
    """Parse RFC 8259 JSON only: no NaN or Infinity, no overflow to inf, no duplicate names."""
    return json.loads(
        text,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
        object_pairs_hook=_unique_members,
    )


def build_app() -> Starlette:
    """Two routes over QUOTE: the lenient one trusts Request.json, the strict one does not."""
    validator = compile_validator(QUOTE)

    def respond(instance: Any) -> JSONResponse:
        found = violations(validator, instance)
        if found:
            detail = [f"{item.pointer}: {item.keyword}" for item in found]
            return JSONResponse({"violations": detail}, status_code=422)
        return JSONResponse({"accepted": True})

    async def lenient(request: Request) -> JSONResponse:
        return respond(await request.json())

    async def strict(request: Request) -> JSONResponse:
        try:
            instance = strict_json_loads(await request.body())
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        return respond(instance)

    return Starlette(
        routes=[
            Route("/lenient/quotes", lenient, methods=["POST"]),
            Route("/strict/quotes", strict, methods=["POST"]),
        ]
    )
