"""Publish a public OpenAPI description from an internal one.

The pipeline is: generate the internal description with FastAPI, apply an Overlay
(Overlay Specification 1.2.0) whose targets are RFC 9535 JSONPath queries, prune the
components that only removed operations referenced, then gate the result with a leak scan
and `openapi-spec-validator`.

The JSONPath engine implements the RFC 9535 grammar for the root and current-node
identifiers, child and descendant segments, name, wildcard, and index selectors, and filter
selectors with existence tests, comparisons, `!`, `&&`, `||`, and parentheses.  Array slices
and function extensions (RFC 9535 Sections 2.3.4 and 2.4) raise `JsonPathUnsupportedError`,
so this lab is deliberately not an Overlay-compliant tool (Overlay 1.2.0 Section 4.9).
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Annotated, Any, Final, TypeGuard

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

type Location = tuple[str | int, ...]

I_JSON_MAX: Final = 2**53 - 1  # RFC 9535 Section 2.1, via RFC 7493 Section 2.2


def is_object(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def is_array(value: object) -> TypeGuard[list[Any]]:
    return isinstance(value, list)


def is_number(value: object) -> bool:
    # bool is a subclass of int in Python; JSON true is not the number 1.
    return isinstance(value, int | float) and not isinstance(value, bool)


def json_equal(a: object, b: object) -> bool:
    """RFC 9535 Section 2.3.5.2.2 equality, without Python's `True == 1`."""
    if is_number(a) and is_number(b):
        return a == b
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if a is None or b is None:
        return a is b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if is_array(a) and is_array(b):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b, strict=True))
    if is_object(a) and is_object(b):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    return False


# --------------------------------------------------------------------------- JSONPath


class JsonPathSyntaxError(ValueError):
    """The query is not well-formed and valid (RFC 9535 Section 2.1)."""


class JsonPathUnsupportedError(JsonPathSyntaxError):
    """Valid RFC 9535 syntax that this lab does not implement (slices, functions)."""


@dataclass(frozen=True, slots=True)
class Name:
    name: str


@dataclass(frozen=True, slots=True)
class Wildcard:
    pass


@dataclass(frozen=True, slots=True)
class Index:
    index: int


@dataclass(frozen=True, slots=True)
class Filter:
    expr: Expr


type Selector = Name | Wildcard | Index | Filter


@dataclass(frozen=True, slots=True)
class Segment:
    selectors: tuple[Selector, ...]
    descendant: bool


@dataclass(frozen=True, slots=True)
class Query:
    relative: bool  # True for '@', False for '$'
    segments: tuple[Segment, ...]

    def is_singular(self) -> bool:
        return all(
            not s.descendant and len(s.selectors) == 1 and isinstance(s.selectors[0], Name | Index)
            for s in self.segments
        )


@dataclass(frozen=True, slots=True)
class Literal:
    value: str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class Exists:
    query: Query


@dataclass(frozen=True, slots=True)
class Compare:
    op: str
    left: Literal | Query
    right: Literal | Query


@dataclass(frozen=True, slots=True)
class Not:
    expr: Expr


@dataclass(frozen=True, slots=True)
class And:
    parts: tuple[Expr, ...]


@dataclass(frozen=True, slots=True)
class Or:
    parts: tuple[Expr, ...]


type Expr = Exists | Compare | Not | And | Or

_NAME_FIRST = re.compile("[A-Za-z_\u0080-\ud7ff\ue000-\U0010ffff]")
_NAME_CHAR = re.compile("[A-Za-z0-9_\u0080-\ud7ff\ue000-\U0010ffff]")
_INT = re.compile(r"0|-?[1-9][0-9]*")
_NUMBER = re.compile(r"(-?(?:0|[1-9][0-9]*))(\.[0-9]+)?([eE][-+]?[0-9]+)?")
_COMPARISON_OPS: Final = ("==", "!=", "<=", ">=", "<", ">")
_ESCAPES: Final = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "/": "/", "\\": "\\"}


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def error(self, message: str) -> JsonPathSyntaxError:
        return JsonPathSyntaxError(f"{message} at offset {self.pos} in {self.text!r}")

    def peek(self, token: str) -> bool:
        return self.text.startswith(token, self.pos)

    def eat(self, token: str) -> bool:
        if self.peek(token):
            self.pos += len(token)
            return True
        return False

    def expect(self, token: str) -> None:
        if not self.eat(token):
            raise self.error(f"expected {token!r}")

    def blank(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t\n\r":
            self.pos += 1

    def parse(self) -> Query:
        self.expect("$")
        query = Query(relative=False, segments=self.segments())
        if self.pos != len(self.text):
            raise self.error("unexpected trailing input")
        return query

    def segments(self) -> tuple[Segment, ...]:
        found: list[Segment] = []
        while True:
            start = self.pos
            self.blank()
            if self.eat(".."):
                if self.peek("["):
                    found.append(Segment(self.bracketed(), descendant=True))
                elif self.eat("*"):
                    found.append(Segment((Wildcard(),), descendant=True))
                else:
                    found.append(Segment((Name(self.shorthand()),), descendant=True))
            elif self.eat("."):
                if self.eat("*"):
                    found.append(Segment((Wildcard(),), descendant=False))
                else:
                    found.append(Segment((Name(self.shorthand()),), descendant=False))
            elif self.peek("["):
                found.append(Segment(self.bracketed(), descendant=False))
            else:
                self.pos = start
                return tuple(found)

    def shorthand(self) -> str:
        # member-name-shorthand = name-first *name-char (RFC 9535 Section 2.5.1.1)
        start = self.pos
        if self.pos >= len(self.text) or not _NAME_FIRST.match(self.text[self.pos]):
            raise self.error("member-name-shorthand must start with ALPHA, '_', or non-ASCII")
        self.pos += 1
        while self.pos < len(self.text) and _NAME_CHAR.match(self.text[self.pos]):
            self.pos += 1
        if self.pos < len(self.text) and self.text[self.pos] == "-":
            raise self.error("'-' is not allowed in member-name-shorthand; use ['name']")
        return self.text[start : self.pos]

    def bracketed(self) -> tuple[Selector, ...]:
        self.expect("[")
        self.blank()
        selectors = [self.selector()]
        while True:
            self.blank()
            if not self.eat(","):
                break
            self.blank()
            selectors.append(self.selector())
        self.expect("]")
        return tuple(selectors)

    def selector(self) -> Selector:
        if self.peek("'") or self.peek('"'):
            return Name(self.string())
        if self.eat("*"):
            return Wildcard()
        if self.eat("?"):
            self.blank()
            return Filter(self.logical_or())
        if self.peek(":"):
            raise JsonPathUnsupportedError("array slice selectors are not implemented")
        match = _INT.match(self.text, self.pos)
        if match is None:
            raise self.error("expected a selector")
        self.pos = match.end()
        start = self.pos
        self.blank()
        if self.peek(":"):
            raise JsonPathUnsupportedError("array slice selectors are not implemented")
        self.pos = start
        value = int(match.group())
        if abs(value) > I_JSON_MAX:
            raise self.error("index outside the I-JSON exact integer range")
        return Index(value)

    def string(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        out: list[str] = []
        while True:
            if self.pos >= len(self.text):
                raise self.error("unterminated string literal")
            char = self.text[self.pos]
            self.pos += 1
            if char == quote:
                return "".join(out)
            if char == "\\":
                out.append(self.escape(quote))
            elif ord(char) < 0x20:
                raise self.error("control character in string literal")
            else:
                out.append(char)

    def escape(self, quote: str) -> str:
        if self.pos >= len(self.text):
            raise self.error("dangling escape")
        char = self.text[self.pos]
        self.pos += 1
        if char == quote:
            return quote
        if char in _ESCAPES:
            return _ESCAPES[char]
        if char != "u":
            raise self.error(f"invalid escape \\{char}")
        code = self.hex4()
        if 0xDC00 <= code <= 0xDFFF:
            raise self.error("lone low surrogate")
        if 0xD800 <= code <= 0xDBFF:
            if not self.eat("\\u"):
                raise self.error("high surrogate without low surrogate")
            low = self.hex4()
            if not 0xDC00 <= low <= 0xDFFF:
                raise self.error("high surrogate without low surrogate")
            code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
        return chr(code)

    def hex4(self) -> int:
        digits = self.text[self.pos : self.pos + 4]
        if len(digits) != 4 or any(c not in "0123456789abcdefABCDEF" for c in digits):
            raise self.error("expected four hex digits")
        self.pos += 4
        return int(digits, 16)

    def logical_or(self) -> Expr:
        parts = [self.logical_and()]
        while True:
            start = self.pos
            self.blank()
            if not self.eat("||"):
                self.pos = start
                break
            self.blank()
            parts.append(self.logical_and())
        return parts[0] if len(parts) == 1 else Or(tuple(parts))

    def logical_and(self) -> Expr:
        parts = [self.basic()]
        while True:
            start = self.pos
            self.blank()
            if not self.eat("&&"):
                self.pos = start
                break
            self.blank()
            parts.append(self.basic())
        return parts[0] if len(parts) == 1 else And(tuple(parts))

    def basic(self) -> Expr:
        if self.eat("!"):
            self.blank()
            if self.peek("("):
                return Not(self.paren())
            query = self.filter_query()
            if self.comparison_op() is not None:
                raise self.error("'!' cannot negate a comparison without parentheses")
            return Not(Exists(query))
        if self.peek("("):
            return self.paren()
        left = self.comparable()
        op = self.comparison_op()
        if op is None:
            if isinstance(left, Query):
                return Exists(left)
            raise self.error("a literal must be compared")
        self.blank()
        right = self.comparable()
        for side in (left, right):
            if isinstance(side, Query) and not side.is_singular():
                raise self.error("only singular queries can be compared")
        return Compare(op, left, right)

    def paren(self) -> Expr:
        self.expect("(")
        self.blank()
        expr = self.logical_or()
        self.blank()
        self.expect(")")
        return expr

    def comparison_op(self) -> str | None:
        start = self.pos
        self.blank()
        for op in _COMPARISON_OPS:
            if self.eat(op):
                return op
        self.pos = start
        return None

    def filter_query(self) -> Query:
        if self.eat("@"):
            return Query(relative=True, segments=self.segments())
        if self.eat("$"):
            return Query(relative=False, segments=self.segments())
        raise self.error("expected '@' or '$'")

    def comparable(self) -> Literal | Query:
        if self.peek("@") or self.peek("$"):
            return self.filter_query()
        if self.peek("'") or self.peek('"'):
            return Literal(self.string())
        for word, value in (("true", True), ("false", False), ("null", None)):
            if self.eat(word):
                return Literal(value)
        number = _NUMBER.match(self.text, self.pos)
        if number is not None:
            self.pos = number.end()
            integer, frac, exp = number.groups()
            if frac is None and exp is None:
                return Literal(int(integer))
            return Literal(float(number.group()))
        if re.match(r"[a-z][a-z0-9_]*\s*\(", self.text[self.pos :]):
            raise JsonPathUnsupportedError("function extensions are not implemented")
        raise self.error("expected a literal or a query")


@dataclass(frozen=True, slots=True)
class Node:
    location: Location
    value: Any


_NOTHING: Final = object()


def _children(node: Node) -> list[Node]:
    if is_object(node.value):
        return [Node((*node.location, k), v) for k, v in node.value.items()]
    if is_array(node.value):
        return [Node((*node.location, i), v) for i, v in enumerate(node.value)]
    return []


def _descendants(node: Node) -> list[Node]:
    # Iterative pre-order walk: RFC 9535 Section 4.1 warns that a naive recursive
    # descendant segment can overflow the stack on attacker-shaped input.
    out: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        out.append(current)
        stack.extend(reversed(_children(current)))
    return out


class JsonPath:
    """A compiled RFC 9535 query (subset documented in the module docstring)."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.query = _Parser(text).parse()

    def find(self, document: object) -> list[Node]:
        return _evaluate(self.query, document, document)


def _evaluate(query: Query, root: object, current: object) -> list[Node]:
    nodes = [Node((), current if query.relative else root)]
    for segment in query.segments:
        selected: list[Node] = []
        for node in nodes:
            inputs = _descendants(node) if segment.descendant else [node]
            for item in inputs:
                for selector in segment.selectors:
                    selected.extend(_select(selector, item, root))
        nodes = selected
    return nodes


def _select(selector: Selector, node: Node, root: object) -> list[Node]:
    value = node.value
    match selector:
        case Name(name):
            if is_object(value) and name in value:
                return [Node((*node.location, name), value[name])]
            return []
        case Wildcard():
            return _children(node)
        case Index(index):
            if is_array(value):
                position = index + len(value) if index < 0 else index
                if 0 <= position < len(value):
                    return [Node((*node.location, position), value[position])]
            return []
        case Filter(expr):
            return [child for child in _children(node) if _test(expr, child.value, root)]


def _test(expr: Expr, current: object, root: object) -> bool:
    match expr:
        case Exists(query):
            return bool(_evaluate(query, root, current))
        case Not(inner):
            return not _test(inner, current, root)
        case And(parts):
            return all(_test(p, current, root) for p in parts)
        case Or(parts):
            return any(_test(p, current, root) for p in parts)
        case Compare(op, left, right):
            return _compare(op, _operand(left, current, root), _operand(right, current, root))


def _operand(side: Literal | Query, current: object, root: object) -> object:
    if isinstance(side, Literal):
        return side.value
    nodes = _evaluate(side, root, current)
    return nodes[0].value if len(nodes) == 1 else _NOTHING


def _less(a: object, b: object) -> bool:
    if a is _NOTHING or b is _NOTHING:
        return False
    if isinstance(a, int | float) and isinstance(b, int | float) and is_number(a) and is_number(b):
        return a < b
    if isinstance(a, str) and isinstance(b, str):
        return a < b  # Python compares code points, as RFC 9535 Section 2.3.5.2.2 requires
    return False


def _compare(op: str, a: object, b: object) -> bool:
    equal = (a is _NOTHING and b is _NOTHING) or (
        a is not _NOTHING and b is not _NOTHING and json_equal(a, b)
    )
    match op:
        case "==":
            return equal
        case "!=":
            return not equal
        case "<":
            return _less(a, b)
        case "<=":
            return _less(a, b) or equal
        case ">":
            return _less(b, a)
        case _:  # ">="
            return _less(b, a) or equal


def normalized_path(location: Location) -> str:
    """RFC 9535 Section 2.7 Normalized Path, for example `$['paths']['/a']['get']`."""
    parts = ["$"]
    for step in location:
        if isinstance(step, int):
            parts.append(f"[{step}]")
            continue
        escaped: list[str] = []
        for char in step:
            if char in "\\'":
                escaped.append("\\" + char)
            elif char in "\b\f\n\r\t":
                escaped.append("\\" + "bfnrt"["\b\f\n\r\t".index(char)])
            elif ord(char) < 0x20:
                escaped.append(f"\\u{ord(char):04x}")
            else:
                escaped.append(char)
        parts.append("['" + "".join(escaped) + "']")
    return "".join(parts)


# --------------------------------------------------------------------------- Overlay


class OverlayError(ValueError):
    """The Overlay document or one of its actions cannot be applied."""


@dataclass(frozen=True, slots=True)
class ActionReport:
    index: int
    kind: str  # "update", "copy", or "remove"
    target: str
    matched: tuple[str, ...]  # Normalized Paths of the selected nodes


@dataclass(frozen=True, slots=True)
class OverlayResult:
    document: dict[str, Any]
    actions: tuple[ActionReport, ...]

    def unmatched(self) -> tuple[ActionReport, ...]:
        return tuple(a for a in self.actions if not a.matched)


_OVERLAY_VERSION = re.compile(r"1\.(0|1|2)\.(0|[1-9][0-9]*)")


def detach(document: dict[str, Any]) -> dict[str, Any]:
    """Return an unshared JSON copy of *document*.

    `copy.deepcopy` preserves aliasing (its memo maps one shared object to one copy), so a
    document with shared nodes, which is what a YAML loader returns for anchors and aliases,
    would let an update at one location appear at another.  A JSON round trip breaks every
    alias, rejects NaN and values JSON cannot represent (such as `datetime.date`), and turns
    an integer key such as a YAML `200:` into the string `"200"`.
    """
    text = json.dumps(document, allow_nan=False, sort_keys=False)
    detached: dict[str, Any] = json.loads(text)
    return detached


def _kind(value: object) -> str:
    if is_object(value):
        return "object"
    if is_array(value):
        return "array"
    return "primitive"  # RFC 8259 Section 1: strings, numbers, booleans, and null


def merge_update(target: dict[str, Any], update: dict[str, Any], where: str = "$") -> None:
    """Overlay 1.2.0 Section 4.5.4 merge: insert, replace primitives, concatenate arrays."""
    for key, value in update.items():
        here = f"{where}[{key!r}]"
        if key not in target:
            target[key] = copy.deepcopy(value)
            continue
        current = target[key]
        if _kind(current) == "primitive" and _kind(value) == "primitive":
            target[key] = value
        elif is_array(current) and is_array(value):
            current.extend(copy.deepcopy(value))
        elif is_object(current) and is_object(value):
            merge_update(current, value, here)
        else:
            raise OverlayError(
                f"incompatible merge of {_kind(value)} into {_kind(current)} at {here}"
            )


def _unescape_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")  # RFC 6901 Section 4 order


def _resolve_action(raw: object, overlay: dict[str, Any], minor: int) -> dict[str, Any]:
    if not is_object(raw):
        raise OverlayError("each action must be an object")
    if "$ref" not in raw:
        return raw
    if minor < 2:
        raise OverlayError("Reusable Action Reference Objects need overlay 1.2")
    ref = raw["$ref"]
    prefix = "#/components/actions/"
    if not isinstance(ref, str) or not ref.startswith(prefix) or "/" in ref[len(prefix) :]:
        raise OverlayError(f"$ref must point into {prefix}: {ref!r}")
    key = _unescape_pointer_token(ref[len(prefix) :])
    reusable = overlay.get("components", {}).get("actions", {}).get(key)
    if not is_object(reusable) or not is_object(reusable.get("fields", {})):
        raise OverlayError(f"unresolvable reusable action {ref!r}")
    fields: dict[str, Any] = copy.deepcopy(reusable.get("fields", {}))
    if "target" in fields:
        raise OverlayError("a Reusable Action Object's fields MUST NOT include target")
    fields.update({k: v for k, v in raw.items() if k != "$ref"})  # reference fields win
    return fields


def _container(document: dict[str, Any], location: Location) -> Any:
    value: Any = document
    for step in location:
        value = value[step]
    return value


def _removal_order(location: Location) -> tuple[int, int]:
    # Deepest first, then higher array indices first, so earlier deletions never shift
    # the index of a node that is still waiting to be deleted.
    last = location[-1]
    return (-len(location), -last if isinstance(last, int) else 0)


def apply_overlay(
    document: dict[str, Any], overlay: dict[str, Any], *, strict: bool = False
) -> OverlayResult:
    """Apply *overlay* to a detached copy of *document*; actions run in order.

    With `strict`, an action whose target selects zero nodes raises instead of being the
    successful no-op that Overlay 1.1.0 and 1.2.0 specify.
    """
    version = overlay.get("overlay")
    match = _OVERLAY_VERSION.fullmatch(version) if isinstance(version, str) else None
    if match is None:
        raise OverlayError(f"unsupported overlay version {version!r}")
    minor = int(match.group(1))  # the patch version is ignored (Section 4.1)
    info = overlay.get("info")
    if not is_object(info) or not all(isinstance(info.get(k), str) for k in ("title", "version")):
        raise OverlayError("info.title and info.version are REQUIRED strings")
    actions = overlay.get("actions")
    if not is_array(actions) or not actions:
        raise OverlayError("actions MUST contain at least one value")

    result = detach(document)
    reports: list[ActionReport] = []
    for index, raw in enumerate(actions):
        action = _resolve_action(raw, overlay, minor)
        target = action.get("target")
        if not isinstance(target, str):
            raise OverlayError(f"action {index}: target is REQUIRED")
        remove = action.get("remove", False)
        if not isinstance(remove, bool):
            raise OverlayError(f"action {index}: remove must be a boolean")
        if remove:
            kind = "remove"
        elif "update" in action and "copy" in action:
            # Each field says it has no impact when the other is present; refuse to guess.
            raise OverlayError(f"action {index}: update and copy are mutually exclusive")
        elif "copy" in action:
            if minor < 1:
                raise OverlayError(f"action {index}: copy needs overlay 1.1")
            kind = "copy"
        elif "update" in action:
            kind = "update"
        else:
            raise OverlayError(f"action {index}: no update, copy, or remove")

        nodes: list[Node] = []
        seen: set[Location] = set()
        for node in JsonPath(target).find(result):
            if node.location not in seen:  # RFC 9535 Section 2.1.2 keeps duplicates
                seen.add(node.location)
                nodes.append(node)
        reports.append(
            ActionReport(index, kind, target, tuple(normalized_path(n.location) for n in nodes))
        )
        if not nodes:
            if strict:
                raise OverlayError(f"action {index}: target {target!r} selected nothing")
            continue

        if kind == "remove":
            if any(not n.location for n in nodes):
                raise OverlayError(f"action {index}: the root cannot be removed")
            for node in sorted(nodes, key=lambda n: _removal_order(n.location)):
                del _container(result, node.location[:-1])[node.location[-1]]
            continue

        if kind == "copy":
            sources = JsonPath(action["copy"]).find(result)
            if len(sources) != 1:
                raise OverlayError(f"action {index}: copy must select exactly one node")
            value: Any = sources[0].value
        else:
            value = action["update"]
        kinds = {_kind(n.value) for n in nodes}
        if len(kinds) > 1:
            raise OverlayError(f"action {index}: selected nodes mix {sorted(kinds)}")
        for node in nodes:
            payload = copy.deepcopy(value)  # never alias one update into several nodes
            if is_object(node.value):
                if not is_object(payload):
                    raise OverlayError(f"action {index}: object targets need an object")
                merge_update(node.value, payload, normalized_path(node.location))
            elif is_array(node.value):
                if is_array(payload):
                    node.value.extend(payload)
                else:
                    node.value.append(payload)
            else:
                if minor < 1:
                    raise OverlayError(f"action {index}: 1.0 targets must be objects or arrays")
                if _kind(payload) != "primitive" or not node.location:
                    raise OverlayError(f"action {index}: primitive targets need a primitive")
                _container(result, node.location[:-1])[node.location[-1]] = payload
    return OverlayResult(result, tuple(reports))


# --------------------------------------------------------------------------- pruning

COMPONENT_SECTIONS: Final = (
    "schemas",
    "responses",
    "parameters",
    "examples",
    "requestBodies",
    "headers",
    "securitySchemes",
    "links",
    "callbacks",
    "pathItems",
    "mediaTypes",
)
OPERATION_KEYS: Final = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace", "query"}
)


def _references(value: object, found: set[tuple[str, str]]) -> None:
    """Collect every local component reference reachable inside *value*."""
    stack = [value]
    while stack:
        item = stack.pop()
        if is_array(item):
            stack.extend(item)
            continue
        if not is_object(item):
            continue
        for key, child in item.items():
            if key == "$ref" and isinstance(child, str):
                found.add(_component_of(child))
            elif key == "discriminator" and is_object(child):
                # mapping and defaultMapping values are schema names or URI references,
                # never `$ref` keys (OpenAPI 3.2.1 Sections 4.25.1 and 4.25.3)
                targets = list(child.get("mapping", {}).values())
                targets.append(child.get("defaultMapping"))
                for target in targets:
                    if isinstance(target, str):
                        found.add(_mapping_target(target))
            elif key == "security" and is_array(child):
                for requirement in child:
                    if is_object(requirement):
                        found.update(("securitySchemes", name) for name in requirement)
            stack.append(child)


def _component_of(ref: str) -> tuple[str, str]:
    if not ref.startswith("#/components/"):
        raise ValueError(f"bundle external or non-component references first: {ref!r}")
    tokens = ref[len("#/components/") :].split("/")
    if len(tokens) < 2:
        raise ValueError(f"reference does not name a component: {ref!r}")
    return tokens[0], _unescape_pointer_token(tokens[1])


_COMPONENT_KEY: Final = re.compile(r"[a-zA-Z0-9.\-_]+")  # OpenAPI 3.1.2 Section 4.8.7


def _mapping_target(value: str) -> tuple[str, str]:
    # OpenAPI 3.2.1 Section 4.25.3: a value that is both a schema name and a relative URI
    # reference is RECOMMENDED to be read as a name; "./foo" forces a URI reference.
    if value.startswith("#"):
        return _component_of(value)
    if _COMPONENT_KEY.fullmatch(value):
        return ("schemas", value)
    raise ValueError(f"bundle external discriminator targets first: {value!r}")


def _operations(item: object) -> list[dict[str, Any]]:
    """The Operation Objects of one Path Item, including OpenAPI 3.2 additionalOperations."""
    if not is_object(item):
        return []
    found = [op for key, op in item.items() if key in OPERATION_KEYS and is_object(op)]
    extra = item.get("additionalOperations")
    if is_object(extra):
        found.extend(op for op in extra.values() if is_object(op))
    return found


def _discriminated_children(
    schemas: dict[str, Any], reachable: set[tuple[str, str]]
) -> set[tuple[str, str]]:
    """Schemas that inherit, through `allOf`, from a reachable schema with a discriminator.

    No JSON Schema keyword points from the parent to its children (OpenAPI 3.2.1 Section
    4.25.2), so a forward-only reachability walk deletes every implicit-mapping subtype.
    """
    parents = {
        f"#/components/schemas/{name.replace('~', '~0').replace('/', '~1')}"
        for section, name in reachable
        if section == "schemas" and is_object(schemas.get(name))
        if is_object(schemas[name].get("discriminator"))
    }
    children: set[tuple[str, str]] = set()
    for name, schema in schemas.items():
        if ("schemas", name) in reachable or not is_object(schema):
            continue
        branches = schema.get("allOf")
        if is_array(branches) and any(is_object(b) and b.get("$ref") in parents for b in branches):
            children.add(("schemas", name))
    return children


def _close_over_tag_parents(tags: list[Any], used: set[str]) -> set[str]:
    # A kept tag needs its whole ancestor chain: `parent` MUST name an existing tag
    # (OpenAPI 3.2.1 Section 4.22.1), and a cycle MUST NOT occur, so the walk terminates.
    parent_of = {t.get("name"): t.get("parent") for t in tags if is_object(t)}
    kept = set(used)
    frontier = set(used)
    while frontier:
        frontier = {p for n in frontier if isinstance(p := parent_of.get(n), str)} - kept
        kept |= frontier
    return kept


def prune_for_publication(document: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Drop empty path items, unused tags, and unreachable components.

    Returns the new document and the JSON Pointers of everything removed.
    """
    doc = detach(document)
    removed: list[str] = []

    paths: dict[str, Any] = doc.get("paths", {})
    for path in list(paths):
        if path.startswith("/") and not _operations(paths[path]):
            # An empty Path Item still reveals that the path exists
            # (OpenAPI 3.1.2 Section 4.10, Security Filtering).
            del paths[path]
            removed.append("/paths/" + path.replace("~", "~0").replace("/", "~1"))

    used_tags: set[str] = set()
    for item in [*paths.values(), *doc.get("webhooks", {}).values()]:
        for operation in _operations(item):
            used_tags.update(operation.get("tags", []))
    if is_array(doc.get("tags")):
        keep = _close_over_tag_parents(doc["tags"], used_tags)
        kept = [t for t in doc["tags"] if is_object(t) and t.get("name") in keep]
        removed.extend(f"/tags/{i}" for i, t in enumerate(doc["tags"]) if t not in kept)
        doc["tags"] = kept

    components: dict[str, Any] = doc.get("components", {})
    reachable: set[tuple[str, str]] = set()
    _references({k: v for k, v in doc.items() if k != "components"}, reachable)
    frontier = set(reachable)
    while frontier:
        found: set[tuple[str, str]] = set()
        for section, name in frontier:
            _references(components.get(section, {}).get(name), found)
        found |= _discriminated_children(components.get("schemas", {}), reachable | found)
        frontier = found - reachable
        reachable |= frontier
    for section in COMPONENT_SECTIONS:
        entries = components.get(section)
        if not is_object(entries):
            continue
        for name in sorted(entries):
            if (section, name) not in reachable:
                del entries[name]
                removed.append(f"/components/{section}/{name}")
        if not entries:
            del components[section]
    if "components" in doc and not components:
        del doc["components"]
    return doc, tuple(removed)


def dangling_required(document: object) -> list[str]:
    """Normalized Paths of `required` entries that name no sibling `properties` member.

    Removing an internal property with an Overlay leaves its name in `required`; JSON
    Schema still demands the member, so the public schema now requires an undocumented
    field.  Only schemas that declare `properties` are checked.
    """
    hits: list[str] = []
    for node in _descendants(Node((), document)):
        value = node.value
        if not is_object(value) or not is_object(value.get("properties")):
            continue
        required = value.get("required")
        if is_array(required):
            for index, name in enumerate(required):
                if isinstance(name, str) and name not in value["properties"]:
                    hits.append(normalized_path((*node.location, "required", index)))
    return hits


def find_leaks(document: object, needles: tuple[str, ...]) -> list[str]:
    """Normalized Paths whose member name or string value contains a needle (any case)."""
    lowered = tuple(n.lower() for n in needles)
    hits: list[str] = []
    for node in _descendants(Node((), document)):
        name = node.location[-1] if node.location else ""
        texts = [name] if isinstance(name, str) else []
        if isinstance(node.value, str):
            texts.append(node.value)
        if any(n in t.lower() for t in texts for n in lowered):
            hits.append(normalized_path(node.location))
    return hits


# Maps whose keys are names chosen by the API author, not OpenAPI fields, so a key such
# as `x-request-id` (a header name) or `x-foo` (a property name) is not an extension.
_NAME_MAPS: Final = frozenset(
    {"properties", "patternProperties", "$defs", "dependentSchemas", "headers", "content"}
    | {"encoding", "examples", "links", "callbacks", "variables", "scopes", "mapping"}
    | {"webhooks", "security"}
)


def extension_inventory(document: dict[str, Any]) -> dict[str, list[str]]:
    """Classify `x-`-like keys; a heuristic, not a full OpenAPI object model."""
    report: dict[str, list[str]] = {"extensions": [], "reserved": [], "case_mismatch": []}
    for node in _descendants(Node((), document)):
        if not is_object(node.value):
            continue
        parent = node.location[-1] if node.location else None
        grandparent = node.location[-2] if len(node.location) >= 2 else None
        if parent in _NAME_MAPS or (grandparent == "components" and parent in COMPONENT_SECTIONS):
            continue
        for key in node.value:
            pointer = normalized_path((*node.location, key))
            if key.startswith("x-"):
                report["extensions"].append(pointer)
                if key.startswith(("x-oai-", "x-oas-")):
                    report["reserved"].append(pointer)
            elif key[:2].lower() == "x-":
                report["case_mismatch"].append(pointer)  # the pattern `^x-` is case-sensitive
    return report


def canonical_json(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(document: object) -> str:
    return hashlib.sha256(canonical_json(document)).hexdigest()


# --------------------------------------------------------------------------- the API

LAB_OPERATOR_KEY: Final = "lab-operator-key-EXAMPLE-NOT-REAL"

public_key = APIKeyHeader(name="X-Api-Key", scheme_name="ApiKey", auto_error=False)
operator_key = APIKeyHeader(name="X-Admin-Token", scheme_name="AdminToken", auto_error=False)


class Money(BaseModel):
    currency: str = Field(pattern="^[A-Z]{3}$")
    amount_minor: int


class OrderCreate(BaseModel):
    sku: str
    quantity: int = Field(ge=1)


class Order(BaseModel):
    id: str
    sku: str
    quantity: int
    total: Money


class OperatorIdentity(BaseModel):
    operator_id: str
    ticket: str


class AdminAuditEntry(BaseModel):
    order_id: str
    refunded: Money
    operator: OperatorIdentity


def require_customer(key: Annotated[str | None, Security(public_key)]) -> str:
    if not key:
        raise HTTPException(status_code=401, detail="missing X-Api-Key")
    return key


def require_operator(key: Annotated[str | None, Security(operator_key)]) -> str:
    if key is None or not hmac.compare_digest(key, LAB_OPERATOR_KEY):
        raise HTTPException(status_code=401, detail="operator credential required")
    return key


def build_app() -> FastAPI:
    app = FastAPI(
        title="Orders API",
        version="2026.11.0",
        openapi_tags=[
            {"name": "orders", "description": "Customer order lifecycle."},
            {"name": "admin", "description": "Internal operator tooling: refunds and debug."},
        ],
    )

    @app.get(
        "/v1/orders/{order_id}",
        tags=["orders"],
        operation_id="getOrder",
        response_model=Order,
    )
    async def get_order(order_id: str, _: Annotated[str, Depends(require_customer)]) -> Order:
        return Order(
            id=order_id, sku="sku-1", quantity=1, total=Money(currency="EUR", amount_minor=1999)
        )

    @app.post(
        "/v1/orders",
        tags=["orders"],
        operation_id="createOrder",
        status_code=201,
        response_model=Order,
    )
    async def create_order(
        body: OrderCreate, _: Annotated[str, Depends(require_customer)]
    ) -> Order:
        total = Money(currency="EUR", amount_minor=1999 * body.quantity)
        return Order(id="ord-1", sku=body.sku, quantity=body.quantity, total=total)

    @app.post(
        "/admin/orders/{order_id}/refund-override",
        tags=["admin"],
        operation_id="adminRefundOverride",
        response_model=AdminAuditEntry,
        openapi_extra={"x-internal": True},
    )
    async def refund_override(
        order_id: str, operator: Annotated[str, Depends(require_operator)]
    ) -> AdminAuditEntry:
        identity = OperatorIdentity(operator_id=operator[:8], ticket="OPS-1")
        refunded = Money(currency="EUR", amount_minor=1999)
        return AdminAuditEntry(order_id=order_id, refunded=refunded, operator=identity)

    @app.get("/admin/debug/config", include_in_schema=False)
    async def debug_config(_: Annotated[str, Depends(require_operator)]) -> dict[str, list[str]]:
        return {"feature_flags": ["refund-override"]}

    return app


CODE_SAMPLE: Final = """\
import httpx

r = httpx.post("https://api.example.test/v1/orders",
               headers={"X-Api-Key": "..."}, json={"sku": "sku-1", "quantity": 2})
r.raise_for_status()
"""

PUBLIC_PORTAL_OVERLAY: Final[dict[str, Any]] = {
    "overlay": "1.2.0",
    "info": {"title": "Public developer portal", "version": "1.0.0"},
    "components": {
        "actions": {
            "rateLimitHeader": {
                "description": "Document the RateLimit-Policy response field.",
                "fields": {
                    "update": {
                        "headers": {
                            "RateLimit-Policy": {
                                "description": "Quota policy, draft-ietf-httpapi-ratelimit-headers",
                                "schema": {"type": "string"},
                                "example": '"default";q=100;w=60',
                            }
                        }
                    }
                },
            }
        }
    },
    "actions": [
        {
            "description": "Drop operator-only operations.",
            "target": "$.paths.*[?@['x-internal'] == true]",
            "remove": True,
        },
        {"target": "$.tags[?@.name == 'admin']", "remove": True},
        {
            "target": "$.paths.*[?@.operationId]",
            "update": {"x-acme-rate-limit": {"policy": "default", "quota": 100, "window": 60}},
        },
        {
            "$ref": "#/components/actions/rateLimitHeader",
            "target": "$.paths.*.*.responses['200','201']",
        },
        {
            "target": "$.paths['/v1/orders'].post",
            "update": {
                "x-codeSamples": [{"lang": "Python", "label": "httpx", "source": CODE_SAMPLE}]
            },
        },
    ],
}


def build_public_spec(app: FastAPI) -> tuple[dict[str, Any], OverlayResult, tuple[str, ...]]:
    """Internal spec -> overlay (strict) -> prune; the app's cached schema is not mutated."""
    overlaid = apply_overlay(app.openapi(), PUBLIC_PORTAL_OVERLAY, strict=True)
    public, removed = prune_for_publication(overlaid.document)
    return public, overlaid, removed
