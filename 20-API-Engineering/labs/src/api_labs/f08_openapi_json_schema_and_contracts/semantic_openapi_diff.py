"""Semantic OpenAPI differ: classify every change by what it does to the accepted-value set.

A change either widens the set of payloads a schema admits, narrows it, makes the old and
new sets incomparable, or leaves it alone. Whether that breaks a client depends only on the
side of the wire the schema sits on: a request schema is the server's input, so narrowing it
rejects requests that old clients send (contravariant position); a response schema is the
client's input, so widening it hands old clients payloads they were never promised
(covariant position). ``derive_level`` is that law; every rule below states an effect and a
direction and never hard-codes a severity. Rule ids follow oasdiff v1.37.0 so the two can be
compared change for change.

Scope (deliberate): local ``#/`` references only (external references are refused, the
lesson of GHSA-fhj3-7267-7vv5), ``application/json`` bodies, ``properties``/``required``/
``items``/``type``/``nullable``/``enum``/``maxLength``, operations, non-path parameters, and
response status codes. ``allOf``/``oneOf``/``anyOf`` are not merged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    type Node = Mapping[str, object]

METHODS: Final = ("get", "put", "post", "delete", "options", "head", "patch", "trace", "query")
JSON_MEDIA_TYPE: Final = "application/json"


class Direction(Enum):
    """Which side of the wire a rule judges."""

    REQUEST = "request"
    RESPONSE = "response"
    NONE = "none"


class Effect(Enum):
    """What a change does to the set of payloads (or operations) the contract admits."""

    NONE = "none"
    WIDENS = "widens"
    NARROWS = "narrows"
    INCOMPARABLE = "incomparable"
    UNKNOWN = "unknown"
    VIOLATION = "violation"


class Level(IntEnum):
    """Severity; ``ERR`` and ``WARN`` count as breaking, as in oasdiff."""

    INFO = 1
    WARN = 2
    ERR = 3


def derive_level(effect: Effect, direction: Direction) -> Level:
    """The variance law: narrowing breaks request consumers, widening breaks response consumers."""
    if effect in (Effect.VIOLATION, Effect.INCOMPARABLE):
        return Level.ERR
    if effect is Effect.UNKNOWN:
        return Level.WARN
    if effect is Effect.NONE:
        return Level.INFO
    breaks = (effect is Effect.NARROWS) != (direction is Direction.RESPONSE)
    return Level.ERR if breaks else Level.INFO


class ExternalRefError(ValueError):
    """A ``$ref`` points outside the document; resolving it would fetch a URL or read a file."""


class RefCycleError(ValueError):
    """A chain of ``$ref`` objects never reaches a schema."""


@dataclass(frozen=True, slots=True, order=True)
class Change:
    """One classified difference between the base and the revision."""

    path: str
    method: str
    id: str
    args: tuple[str, ...]
    direction: Direction = field(compare=False)
    effect: Effect = field(compare=False)

    @property
    def level(self) -> Level:
        return derive_level(self.effect, self.direction)

    @property
    def fingerprint(self) -> str:
        """Stable id built like oasdiff's: SHA-256 of id, method, path, and args, 12 hex chars."""
        key = f"{self.id}:{self.method.upper()}:{self.path}:{';'.join(self.args)}"
        return hashlib.sha256(key.encode()).hexdigest()[:12]

    def text(self) -> str:
        return f"{self.level.name:<4} [{self.id}] {self.method.upper()} {self.path}: " + " ".join(
            self.args
        )


def _as_node(value: object) -> Node:
    if isinstance(value, dict):
        return cast("Node", value)
    if value is True or value is None:
        return {}
    raise TypeError(f"expected an object, got {type(value).__name__}")


class _Document:
    """Resolves local JSON Pointer references inside one OpenAPI document."""

    def __init__(self, root: Node) -> None:
        self.root = root

    def resolve(self, node: object) -> Node:
        current = _as_node(node)
        hops: set[str] = set()
        while "$ref" in current:
            ref = str(current["$ref"])
            if not ref.startswith("#/"):
                raise ExternalRefError(ref)
            if ref in hops:
                raise RefCycleError(ref)
            hops.add(ref)
            target: object = self.root
            for token in ref[2:].split("/"):
                target = _as_node(target)[token.replace("~1", "/").replace("~0", "~")]
            current = _as_node(target)
        return current


def _template_key(path: str) -> str:
    """``/orders/{orderId}`` and ``/orders/{id}`` are the same endpoint (OpenAPI Paths Object)."""
    out: list[str] = []
    depth = 0
    for char in path:
        if char == "{":
            depth += 1
            if depth == 1:
                out.append("{}")
        elif char == "}":
            depth -= 1
        elif depth == 0:
            out.append(char)
    return "".join(out)


def _operations(doc: _Document) -> dict[tuple[str, str], tuple[str, Node, Node]]:
    ops: dict[tuple[str, str], tuple[str, Node, Node]] = {}
    for raw_path, raw_item in _as_node(doc.root.get("paths")).items():
        item = doc.resolve(raw_item)
        for method in METHODS:
            if method in item:
                key = (method, _template_key(raw_path))
                if key in ops:
                    raise ValueError(f"duplicate endpoint {method.upper()} {raw_path}")
                ops[key] = (raw_path, item, doc.resolve(item[method]))
    return ops


def _types(schema: Node) -> frozenset[str] | None:
    """The instance types a schema admits, without ``null``; ``None`` means unconstrained."""
    raw = schema.get("type")
    if raw is None:
        return None
    names = [raw] if isinstance(raw, str) else [str(t) for t in cast("list[object]", raw)]
    return frozenset(name for name in names if name != "null")


def _nullable(schema: Node) -> bool:
    raw = schema.get("type")
    in_type = raw == "null" or (isinstance(raw, list) and "null" in cast("list[object]", raw))
    return in_type or schema.get("nullable") is True


def _subtypes(small: frozenset[str] | None, big: frozenset[str] | None) -> bool:
    """Every instance of ``small`` is an instance of ``big`` (``integer`` is a ``number``)."""
    if big is None:
        return True
    if small is None:
        return False
    return all(t in big or (t == "integer" and "number" in big) for t in small)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _effect_of(widened: bool, narrowed: bool) -> Effect:
    if widened and narrowed:
        return Effect.INCOMPARABLE
    if widened:
        return Effect.WIDENS
    return Effect.NARROWS if narrowed else Effect.NONE


@dataclass(slots=True)
class _Walk:
    """State for one request or response payload of one operation."""

    base: _Document
    revision: _Document
    direction: Direction
    prefix: str
    method: str
    path: str
    status: str
    out: list[Change]
    seen: set[tuple[int, int]] = field(default_factory=lambda: set[tuple[int, int]]())

    def emit(self, suffix: str, effect: Effect, *args: str) -> None:
        """Record rule ``<prefix>-<suffix>``, e.g. ``response-property`` + ``enum-value-added``."""
        self.emit_as(f"{self.prefix}-{suffix}", effect, *args)

    def emit_as(self, rule: str, effect: Effect, *args: str) -> None:
        tail = (f"status={self.status}",) if self.status else ()
        self.out.append(
            Change(self.path, self.method, rule, (*args, *tail), self.direction, effect)
        )

    def invisible(self, prop: Node) -> bool:
        """``readOnly`` never travels in requests, ``writeOnly`` never in responses."""
        if self.direction is Direction.REQUEST:
            return prop.get("readOnly") is True
        return prop.get("writeOnly") is True


def _diff_keywords(walk: _Walk, base: Node, rev: Node, where: str) -> None:
    if _nullable(base) != _nullable(rev):
        widened = _nullable(rev)
        suffix = "became-nullable" if widened else "became-not-nullable"
        walk.emit(suffix, _effect_of(widened, not widened), where)

    old_types, new_types = _types(base), _types(rev)
    grew, shrank = _subtypes(old_types, new_types), _subtypes(new_types, old_types)
    if not (grew and shrank):  # both ways means the same instance set, e.g. number|integer
        if grew:
            suffix, effect = "type-generalized", Effect.WIDENS
        elif shrank and walk.direction is Direction.RESPONSE:
            suffix, effect = "type-specialized", Effect.NARROWS
        else:  # a narrowed request type, or a change neither set contains
            suffix, effect = "type-changed", Effect.NARROWS if shrank else Effect.INCOMPARABLE
        walk.emit(suffix, effect, where, _render_types(old_types), _render_types(new_types))

    if "enum" in base and "enum" in rev:
        old_values = {_canonical(v): v for v in cast("list[object]", base["enum"])}
        new_values = {_canonical(v): v for v in cast("list[object]", rev["enum"])}
        for key in sorted(old_values.keys() - new_values.keys()):
            walk.emit("enum-value-removed", Effect.NARROWS, where, key)
        for key in sorted(new_values.keys() - old_values.keys()):
            walk.emit("enum-value-added", Effect.WIDENS, where, key)

    old_max, new_max = base.get("maxLength"), rev.get("maxLength")
    if old_max != new_max:
        if old_max is None:
            walk.emit("max-length-set", Effect.NARROWS, where, str(new_max))
        elif new_max is None:
            walk.emit("max-length-unset", Effect.WIDENS, where, str(old_max))
        elif cast("int", new_max) < cast("int", old_max):
            walk.emit("max-length-decreased", Effect.NARROWS, where, f"{old_max}->{new_max}")
        else:
            walk.emit("max-length-increased", Effect.WIDENS, where, f"{old_max}->{new_max}")


def _render_types(types: frozenset[str] | None) -> str:
    return "any" if types is None else "|".join(sorted(types))


def _required(schema: Node) -> frozenset[str]:
    return frozenset(str(name) for name in cast("list[object]", schema.get("required", [])))


def _diff_schema(walk: _Walk, raw_base: object, raw_rev: object, where: str) -> None:
    base, rev = walk.base.resolve(raw_base), walk.revision.resolve(raw_rev)
    key = (id(base), id(rev))
    if key in walk.seen:  # recursion, or a shared component already compared on this payload
        return
    walk.seen.add(key)
    _diff_keywords(walk, base, rev, where)

    old_props, new_props = _as_node(base.get("properties")), _as_node(rev.get("properties"))
    old_req, new_req = _required(base), _required(rev)
    request = walk.direction is Direction.REQUEST
    for name in sorted(old_props.keys() | new_props.keys()):
        child = f"{where}/{name}" if where else name
        if name not in new_props:
            if walk.invisible(walk.base.resolve(old_props[name])):
                continue
            if request:  # the server may still accept and ignore it: oasdiff calls this unknown
                walk.emit("removed", Effect.UNKNOWN, child)
            elif name in old_req:  # "always present" became "maybe absent": the set grew
                walk.emit_as("response-required-property-removed", Effect.WIDENS, child)
            else:
                walk.emit_as("response-optional-property-removed", Effect.NONE, child)
            continue
        new_prop = walk.revision.resolve(new_props[name])
        if walk.invisible(new_prop):
            continue
        if name not in old_props:
            kind = "required" if name in new_req else "optional"
            effect = Effect.NARROWS if name in new_req else Effect.NONE
            rule = f"new-{kind}-request-property" if request else f"response-{kind}-property-added"
            walk.emit_as(rule, effect, child)
            continue
        if (name in old_req) != (name in new_req):
            became_required = name in new_req
            suffix = "became-required" if became_required else "became-optional"
            walk.emit(suffix, _effect_of(not became_required, became_required), child)
        _diff_schema(walk, old_props[name], new_props[name], child)

    if "items" in base and "items" in rev:
        _diff_schema(walk, base["items"], rev["items"], f"{where}[]")


def _json_schema(doc: _Document, holder: object) -> Node | None:
    content = _as_node(doc.resolve(holder).get("content"))
    media = content.get(JSON_MEDIA_TYPE)
    if media is None:
        return None
    return _as_node(doc.resolve(media).get("schema"))


def _parameters(doc: _Document, item: Node, operation: Node) -> dict[tuple[str, str], Node]:
    """Path-level parameters overridden by operation-level ones, keyed by (in, name)."""
    merged: dict[tuple[str, str], Node] = {}
    for raw_list in (item.get("parameters", []), operation.get("parameters", [])):
        for raw in cast("list[object]", raw_list):
            param = doc.resolve(raw)
            location = str(param.get("in"))
            if location != "path":  # path parameters are matched positionally by the template
                merged[(location, str(param.get("name")))] = param
    return merged


def _diff_parameters(
    base: _Document, rev: _Document, items: tuple[Node, Node], ops: tuple[Node, Node], walk: _Walk
) -> None:
    old = _parameters(base, items[0], ops[0])
    new = _parameters(rev, items[1], ops[1])
    for key in sorted(old.keys() | new.keys()):
        label = f"{key[0]}:{key[1]}"
        if key not in new:
            walk.emit("removed", Effect.UNKNOWN, label)
            continue
        new_required = new[key].get("required") is True
        if key not in old:
            kind = "required" if new_required else "optional"
            effect = Effect.NARROWS if new_required else Effect.NONE
            walk.emit_as(f"new-{kind}-request-parameter", effect, label)
            continue
        old_required = old[key].get("required") is True
        if old_required != new_required:
            walk.emit(
                "became-required" if new_required else "became-optional",
                _effect_of(old_required, new_required),
                label,
            )
        _diff_schema(walk, old[key].get("schema"), new[key].get("schema"), label)


def _removal(path: str, method: str, operation: Node, today: date) -> Change:
    if operation.get("deprecated") is not True:
        rule, effect = "api-removed-without-deprecation", Effect.NARROWS
    elif "x-sunset" in operation and date.fromisoformat(str(operation["x-sunset"])) > today:
        rule, effect = "api-removed-before-sunset", Effect.VIOLATION
    else:
        rule, effect = "api-removed-with-deprecation", Effect.NONE
    return Change(path, method, rule, (), Direction.NONE, effect)


def _is_success(status: str) -> bool:
    return status.startswith("2") or status.upper() == "2XX"


def diff_specs(base_spec: Node, revision_spec: Node, *, today: date) -> list[Change]:
    """Every classified change between two OpenAPI 3.x documents, in a deterministic order."""
    base, rev = _Document(base_spec), _Document(revision_spec)
    old_ops, new_ops = _operations(base), _operations(rev)
    out: list[Change] = []
    for key in sorted(old_ops.keys() | new_ops.keys()):
        method = key[0]
        if key not in new_ops:
            path, _, operation = old_ops[key]
            out.append(_removal(path, method, operation, today))
            continue
        if key not in old_ops:
            continue  # a new endpoint widens the API surface: never breaking, not reported here
        (_, old_item, old_op), (path, new_item, new_op) = old_ops[key], new_ops[key]

        def walk(direction: Direction, prefix: str, status: str = "") -> _Walk:
            return _Walk(base, rev, direction, prefix, method, path, status, out)  # noqa: B023

        _diff_parameters(
            base,
            rev,
            (old_item, new_item),
            (old_op, new_op),
            walk(Direction.REQUEST, "request-parameter"),
        )
        old_body, new_body = old_op.get("requestBody"), new_op.get("requestBody")
        if old_body is not None and new_body is not None:
            old_schema, new_schema = _json_schema(base, old_body), _json_schema(rev, new_body)
            if old_schema is not None and new_schema is not None:
                _diff_schema(
                    walk(Direction.REQUEST, "request-property"), old_schema, new_schema, ""
                )

        old_resp, new_resp = _as_node(old_op.get("responses")), _as_node(new_op.get("responses"))
        for status in sorted(old_resp.keys() | new_resp.keys()):
            kind = "success" if _is_success(status) else "non-success"
            if status not in new_resp or status not in old_resp:
                removed = status not in new_resp
                # The client chooses which statuses it handles, so status availability is judged
                # like a request (oasdiff GuardNegotiated); non-success sets are never closed.
                effect = Effect.NARROWS if removed and kind == "success" else Effect.NONE
                verb = "removed" if removed else "added"
                out.append(
                    Change(
                        path,
                        method,
                        f"response-{kind}-status-{verb}",
                        (status,),
                        Direction.REQUEST,
                        effect,
                    )
                )
                continue
            old_schema, new_schema = (
                _json_schema(base, old_resp[status]),
                _json_schema(rev, new_resp[status]),
            )
            if old_schema is not None and new_schema is not None:
                _diff_schema(
                    walk(Direction.RESPONSE, "response-property", status),
                    old_schema,
                    new_schema,
                    "",
                )
    return sorted(out)


def breaking(changes: Iterable[Change]) -> list[Change]:
    return [change for change in changes if change.level >= Level.WARN]


def gate(
    changes: Iterable[Change], *, fail_on: Level = Level.ERR, ignore: Iterable[str] = ()
) -> int:
    """CI exit status: 1 if any change at or above ``fail_on`` is not in the ignore list."""
    waived = frozenset(ignore)
    return int(any(c.level >= fail_on and c.fingerprint not in waived for c in changes))
