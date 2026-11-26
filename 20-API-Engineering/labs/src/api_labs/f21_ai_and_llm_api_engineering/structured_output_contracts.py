"""Compile Pydantic v2 models into strict JSON Schemas for constrained decoding.

A constrained decoder compiles a JSON Schema into a token mask.
That only works on a closed, finite subset of JSON Schema, so this module:

* subclasses ``GenerateJsonSchema`` so that silently dropped union branches and
  non-serializable defaults fail loudly instead of vanishing,
* rewrites the result into the strict shape (closed objects, every property required),
* applies a ``SchemaProfile`` that moves, rewrites, or rejects keywords the decoder cannot
  enforce, and records everything the client must still enforce itself,
* classifies a completion (refusal, truncation, schema violation) before trusting it.

The profiles are lab models of the documented provider rules, not provider clients.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, TypeGuard

from pydantic import BaseModel, ValidationError
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaWarningKind

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

JsonObject = dict[str, Any]
Treatment = Literal["keep", "describe", "reject"]
FinishReason = Literal["stop", "length", "refusal"]

_SCHEMA_VALUED: Final = (
    "items",
    "additionalProperties",
    "not",
    "if",
    "then",
    "else",
    "contains",
    "propertyNames",
)
_SCHEMA_LISTS: Final = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_MAPS: Final = ("properties", "patternProperties", "$defs", "dependentSchemas")
_REF_PREFIX: Final = "#/$defs/"


class SchemaContractError(ValueError):
    """The schema cannot be expressed in the target profile."""

    def __init__(self, violations: Sequence[Violation]) -> None:
        self.violations: tuple[Violation, ...] = tuple(violations)
        lines = [f"{v.path or '/'}: {v.code}: {v.message}" for v in self.violations]
        super().__init__("; ".join(lines))


class StructuredOutputError(Exception):
    """A completion that must not be treated as a valid structured result."""


class RefusalError(StructuredOutputError):
    """The model refused; the text need not match the schema."""


class TruncatedError(StructuredOutputError):
    """Generation stopped at the token limit; the JSON is probably incomplete."""


class ContractViolationError(StructuredOutputError):
    """The text parsed as a completion but failed validation against the Pydantic model."""

    def __init__(self, error_types: Sequence[str]) -> None:
        self.error_types: tuple[str, ...] = tuple(error_types)
        super().__init__(", ".join(self.error_types))


@dataclass(frozen=True, slots=True)
class Violation:
    """One reason a schema does not fit a profile; ``path`` is an RFC 6901 JSON Pointer."""

    path: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class SchemaProfile:
    """A lab model of what one constrained-decoding backend accepts."""

    name: str
    allowed_keywords: frozenset[str]
    constraints: Mapping[str, Treatment]
    allowed_formats: frozenset[str] | None
    allow_recursion: bool
    convert_one_of: bool
    root_must_be_object: bool
    max_properties: int | None = None
    max_depth: int | None = None
    max_enum_values: int | None = None
    drop_titles: bool = False


@dataclass(frozen=True, slots=True)
class CompiledSchema:
    """A schema ready to send, plus what the compiler changed and what the client must enforce."""

    schema: JsonObject
    rewrites: tuple[str, ...]
    client_enforced: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class Completion:
    """A normalized provider response: the text and why generation stopped."""

    text: str
    finish: FinishReason


_STRUCTURAL: Final = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "anyOf",
        "enum",
        "const",
        "$ref",
        "$defs",
        "description",
        "title",
    }
)
_CONSTRAINT_KEYWORDS: Final = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minItems",
    "maxItems",
    "uniqueItems",
)
_COMMON_FORMATS: Final = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "ipv4", "ipv6", "uuid"}
)


def _treat(default: Treatment, **overrides: Treatment) -> Mapping[str, Treatment]:
    """Map every constraint keyword to ``default`` except the named overrides."""
    return {key: overrides.get(key, default) for key in _CONSTRAINT_KEYWORDS}


# Mirrors the SDK transform described in the Anthropic structured outputs docs: unsupported
# constraints are removed from the wire schema and appended to the field description.
ANTHROPIC_STYLE: Final = SchemaProfile(
    name="anthropic-style",
    allowed_keywords=_STRUCTURAL,
    constraints=_treat("describe", format="keep"),
    allowed_formats=_COMMON_FORMATS | {"uri"},
    allow_recursion=False,
    convert_one_of=True,
    root_must_be_object=False,
    drop_titles=True,
)

# Numeric limits match the OpenAI structured outputs guide (as of 2026-11); re-check before use.
OPENAI_STYLE: Final = SchemaProfile(
    name="openai-style",
    allowed_keywords=_STRUCTURAL,
    constraints=_treat("keep", uniqueItems="describe"),
    allowed_formats=_COMMON_FORMATS,
    allow_recursion=True,
    convert_one_of=True,
    root_must_be_object=True,
    max_properties=5000,
    max_depth=10,
    max_enum_values=1000,
)

# A deliberately small profile used where the target backend is unknown.
CONSERVATIVE: Final = SchemaProfile(
    name="conservative",
    allowed_keywords=_STRUCTURAL,
    constraints=_treat("reject"),
    allowed_formats=None,
    allow_recursion=False,
    convert_one_of=True,
    root_must_be_object=True,
)


class StrictSchemaGenerator(GenerateJsonSchema):
    """Pydantic generator that turns silent degradation into an error.

    ``GenerateJsonSchema`` ignores the ``skipped-choice`` warning by default, so a union
    member that has no JSON Schema form (for example ``Callable``) is dropped from the
    ``anyOf`` without a sound.
    A constrained decoder would then be told the field can only be the remaining branches.
    """

    ignored_warning_kinds: set[JsonSchemaWarningKind] = set()  # noqa: RUF012

    def emit_warning(self, kind: JsonSchemaWarningKind, detail: str) -> None:
        raise SchemaContractError([Violation("", kind, detail)])


def _pointer(segments: Sequence[str]) -> str:
    return "".join("/" + s.replace("~", "~0").replace("/", "~1") for s in segments)


def _is_obj(value: object) -> TypeGuard[JsonObject]:
    return isinstance(value, dict)


def _is_list(value: object) -> TypeGuard[list[Any]]:
    return isinstance(value, list)


def iter_subschemas(node: JsonObject) -> Iterator[tuple[tuple[str, ...], JsonObject]]:
    """Yield ``(path segments, child)`` for every direct subschema of ``node``.

    Only schema-valued keywords are visited, so ``enum``, ``const``, ``default``, and the
    *names* inside ``properties`` are never mistaken for keywords.
    """
    for key in _SCHEMA_VALUED:
        child = node.get(key)
        if _is_obj(child):
            yield (key,), child
    for key in _SCHEMA_LISTS:
        items = node.get(key)
        if _is_list(items):
            for i, item in enumerate(items):
                if _is_obj(item):
                    yield (key, str(i)), item
    for key in _SCHEMA_MAPS:
        mapping = node.get(key)
        if _is_obj(mapping):
            for name, sub in mapping.items():
                if _is_obj(sub):
                    yield (key, name), sub


def _map_subschemas(
    node: JsonObject, fn: Callable[[tuple[str, ...], JsonObject], JsonObject]
) -> JsonObject:
    """Copy ``node`` with every direct subschema replaced by ``fn(segments, child)``."""
    out: JsonObject = dict(node)
    for key in _SCHEMA_VALUED:
        child = node.get(key)
        if _is_obj(child):
            out[key] = fn((key,), child)
    for key in _SCHEMA_LISTS:
        items = node.get(key)
        if _is_list(items):
            out[key] = [
                fn((key, str(i)), item) if _is_obj(item) else item for i, item in enumerate(items)
            ]
    for key in _SCHEMA_MAPS:
        mapping = node.get(key)
        if _is_obj(mapping):
            out[key] = {
                name: fn((key, name), sub) if _is_obj(sub) else sub for name, sub in mapping.items()
            }
    return out


def _ref_name(ref: str) -> str | None:
    if ref == "#":
        return "#"
    if ref.startswith(_REF_PREFIX):
        return ref[len(_REF_PREFIX) :]
    return None


def _defs(schema: JsonObject) -> JsonObject:
    defs = schema.get("$defs")
    return defs if _is_obj(defs) else {}


def _target(schema: JsonObject, ref: str) -> JsonObject | None:
    """The definition a ``#/$defs/Name`` reference points at, or ``None``."""
    name = _ref_name(ref)
    if name is None or name == "#":
        return None
    target = _defs(schema).get(name)
    return target if _is_obj(target) else None


def _refs_in(node: JsonObject) -> set[str]:
    """Every ``$ref`` value inside one definition body (not following the references)."""
    found: set[str] = set()
    ref = node.get("$ref")
    if isinstance(ref, str):
        found.add(ref)
    for _segments, child in iter_subschemas(node):
        found |= _refs_in(child)
    return found


def find_ref_cycle(schema: JsonObject) -> list[str] | None:
    """Return one reference cycle such as ``['Node', 'Node']``, or ``None`` if acyclic."""
    graph: dict[str, set[str]] = {"#": set()}
    defs = _defs(schema)
    root_body = {k: v for k, v in schema.items() if k != "$defs"}
    graph["#"] = {n for r in _refs_in(root_body) if (n := _ref_name(r)) is not None}
    for name, body in defs.items():
        if _is_obj(body):
            graph[name] = {n for r in _refs_in(body) if (n := _ref_name(r)) is not None}
    state: dict[str, int] = {}

    def visit(name: str, stack: list[str]) -> list[str] | None:
        if state.get(name) == 1:
            return [*stack[stack.index(name) :], name]
        if state.get(name) == 2 or name not in graph:
            return None
        state[name] = 1
        for nxt in sorted(graph[name]):
            cycle = visit(nxt, [*stack, name])
            if cycle is not None:
                return cycle
        state[name] = 2
        return None

    return visit("#", [])


_REGEX_ESCAPE_BACKREF: Final = re.compile(r"[1-9]")


def unsupported_regex_features(pattern: str) -> list[str]:
    """Report regex constructs that documented structured-output subsets reject.

    Backreferences are not regular; lookaround and word boundaries are regular in theory,
    but the Anthropic structured outputs docs list all three as unsupported.
    The scan is escape-aware and skips character classes.
    """
    found: list[str] = []
    i, in_class = 0, False
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            if not in_class and _REGEX_ESCAPE_BACKREF.fullmatch(nxt):
                found.append("backreference")
            elif not in_class and nxt in "bB":
                found.append("word-boundary")
            i += 2
            continue
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif pattern.startswith(("(?=", "(?!"), i):
            found.append("lookahead")
        elif pattern.startswith(("(?<=", "(?<!"), i):
            found.append("lookbehind")
        i += 1
    return sorted(set(found))


class _Compiler:
    def __init__(self, profile: SchemaProfile) -> None:
        self.profile = profile
        self.violations: list[Violation] = []
        self.rewrites: list[str] = []
        self.client_enforced: list[str] = []
        self.enum_values = 0

    def flag(self, path: Sequence[str], code: str, message: str) -> None:
        self.violations.append(Violation(_pointer(path), code, message))

    def visit(self, node: JsonObject, path: tuple[str, ...]) -> JsonObject:
        node = _map_subschemas(node, lambda seg, child: self.visit(child, (*path, *seg)))
        out: JsonObject = {}
        moved: list[str] = []
        for key, value in node.items():
            if key == "discriminator":
                self.rewrites.append(f"{_pointer(path)}: dropped OpenAPI-only 'discriminator'")
            elif key == "default":
                self.rewrites.append(f"{_pointer(path)}: dropped 'default' (field is required)")
            elif key == "title" and self.profile.drop_titles:
                continue
            elif key == "oneOf" and self.profile.convert_one_of and "anyOf" not in node:
                out["anyOf"] = value
                self.rewrites.append(f"{_pointer(path)}: oneOf -> anyOf")
            elif (treatment := self.profile.constraints.get(key)) is not None:
                self.constraint(path, key, value, treatment, out, moved)
            elif key not in self.profile.allowed_keywords:
                self.flag(path, "unsupported-keyword", f"keyword {key!r} is not supported")
                out[key] = value
            else:
                out[key] = value
        if moved:
            text = ", ".join(moved)
            existing = out.get("description")
            note = f"(constraints: {text})"
            out["description"] = f"{existing} {note}" if isinstance(existing, str) else note
        self.shape(out, path)
        return out

    def constraint(
        self,
        path: tuple[str, ...],
        key: str,
        value: Any,
        treatment: Treatment,
        out: JsonObject,
        moved: list[str],
    ) -> None:
        if treatment == "describe":
            moved.append(f"{key}={json.dumps(value)}")
            self.client_enforced.append(f"{_pointer(path)}: {key}")
            return
        if treatment == "reject":
            self.flag(path, "constraint-rejected", f"constraint {key!r} cannot be enforced")
            out[key] = value
            return
        if key == "pattern" and isinstance(value, str):
            for feature in unsupported_regex_features(value):
                self.flag(path, "unsupported-regex", f"pattern uses {feature}")
        allowed = self.profile.allowed_formats
        if key == "format" and allowed is not None and value not in allowed:
            self.flag(path, "unsupported-format", f"format {value!r} is not in the allowed list")
        out[key] = value

    def shape(self, node: JsonObject, path: tuple[str, ...]) -> None:
        """Close objects, require every property, and flag open maps."""
        extra = node.get("additionalProperties", None)
        if node.get("type") == "object" and "properties" not in node:
            self.flag(path, "open-object", "object without 'properties' is an open map")
        if "properties" in node:
            if "additionalProperties" not in node:
                node["additionalProperties"] = False
                self.rewrites.append(f"{_pointer(path)}: added additionalProperties: false")
            elif extra is not False:
                self.flag(path, "open-object", "additionalProperties must be false")
            names = list(node["properties"])
            if node.get("required") != names:
                node["required"] = names
                self.rewrites.append(f"{_pointer(path)}: required = all properties")
        enum = node.get("enum")
        if _is_list(enum):
            self.enum_values += len(enum)


def _resolve_root(schema: JsonObject) -> JsonObject:
    seen: set[str] = set()
    node = schema
    while isinstance(ref := node.get("$ref"), str) and ref not in seen:
        seen.add(ref)
        target = _target(schema, ref)
        if target is None:
            break
        node = target
    return node


def _depth(schema: JsonObject, node: JsonObject, active: frozenset[str]) -> int:
    """Object nesting levels below ``node``, following references once per path."""
    best = 0
    ref = node.get("$ref")
    if isinstance(ref, str):
        name = _ref_name(ref)
        target = _target(schema, ref)
        if name is not None and name not in active and target is not None:
            best = _depth(schema, target, active | {name})
    deepest = max([best, *(_depth(schema, c, active) for _s, c in iter_subschemas(node))])
    return deepest + 1 if node.get("type") == "object" else deepest


def _count_properties(node: JsonObject) -> int:
    props = node.get("properties")
    own = len(props) if _is_obj(props) else 0
    return own + sum(_count_properties(c) for _s, c in iter_subschemas(node))


def structural_fingerprint(schema: JsonObject) -> str:
    """SHA-256 of the canonical schema with ``title`` and ``description`` removed.

    Two schemas that differ only in annotations hash alike, which is the key a compile cache
    wants when the backend caches by structure.
    """

    def strip(node: JsonObject) -> JsonObject:
        copy = _map_subschemas(node, lambda _s, child: strip(child))
        copy.pop("title", None)
        copy.pop("description", None)
        return copy

    canonical = json.dumps(strip(schema), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def apply_profile(schema: JsonObject, profile: SchemaProfile) -> CompiledSchema:
    """Rewrite ``schema`` into ``profile`` or raise ``SchemaContractError`` with every problem."""
    compiler = _Compiler(profile)
    out = compiler.visit(schema, ())
    problems = compiler.violations

    cycle = find_ref_cycle(out)
    if cycle is not None and not profile.allow_recursion:
        problems.append(Violation("", "recursive-ref", " -> ".join(cycle)))
    for pointer in sorted(_refs_in(out)):
        if _ref_name(pointer) not in ("#", *_defs(out)):
            problems.append(Violation("", "unresolved-ref", pointer))

    if profile.root_must_be_object:
        root = _resolve_root(out)
        if "anyOf" in root:
            problems.append(Violation("", "root-any-of", "root must be an object, not anyOf"))
        elif root.get("type") != "object":
            problems.append(Violation("", "root-not-object", "root must be an object"))

    if profile.max_properties is not None:
        count = _count_properties(out)
        if count > profile.max_properties:
            problems.append(Violation("", "too-many-properties", f"{count}"))
    if profile.max_depth is not None and cycle is None:
        depth = _depth(out, out, frozenset())
        if depth > profile.max_depth:
            problems.append(Violation("", "too-deep", f"{depth}"))
    if profile.max_enum_values is not None and compiler.enum_values > profile.max_enum_values:
        problems.append(Violation("", "too-many-enum-values", f"{compiler.enum_values}"))

    if problems:
        raise SchemaContractError(problems)
    return CompiledSchema(
        schema=out,
        rewrites=tuple(compiler.rewrites),
        client_enforced=tuple(compiler.client_enforced),
        fingerprint=structural_fingerprint(out),
    )


def compile_strict_schema(model: type[BaseModel], profile: SchemaProfile) -> CompiledSchema:
    """Generate the validation-mode schema for ``model`` and rewrite it into ``profile``."""
    raw = model.model_json_schema(schema_generator=StrictSchemaGenerator, mode="validation")
    return apply_profile(raw, profile)


def allowed_next_keys(object_schema: JsonObject, emitted: Sequence[str]) -> frozenset[str] | None:
    """Model of the key mask for a decoder that emits properties in schema order.

    ``None`` means the next key is an arbitrary string, so no finite mask exists.
    An empty set means the object must be closed.
    """
    if object_schema.get("additionalProperties") is not False:
        return None
    names = list(object_schema.get("properties", {}))
    if list(emitted) != names[: len(emitted)]:
        raise ValueError(f"keys {list(emitted)} are not a prefix of {names}")
    return frozenset(names[len(emitted) : len(emitted) + 1])


def parse_completion[M: BaseModel](model: type[M], completion: Completion) -> M:
    """Trust a completion only after the stop reason and the full Pydantic model agree."""
    if completion.finish == "refusal":
        raise RefusalError("model refused; text is not schema-bound")
    if completion.finish == "length":
        raise TruncatedError("generation hit the token limit")
    try:
        return model.model_validate_json(completion.text)
    except ValidationError as exc:
        raise ContractViolationError([e["type"] for e in exc.errors()]) from exc
