"""AIP-160 filter subset: lexer, bounded recursive-descent parser, AST, and SQLAlchemy compiler."""

from __future__ import annotations

import datetime as dt
import enum
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from sqlalchemy import and_, not_, or_, select

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy import ColumnElement, Engine, Table

COMPARATORS = ("<=", ">=", "!=", "<", ">", "=", ":")
_SINGLE_CHAR_TOKENS = "()-,"
_TEXT_STOP = frozenset(" \t\r\n()<>=!:,\"'")
_INT_RE = re.compile(r"-?[0-9]{1,19}")
_FLOAT_RE = re.compile(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?")
_RFC3339_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})"
)
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


class FilterError(ValueError):
    """A filter rejected with google.rpc.Code INVALID_ARGUMENT (HTTP 400)."""

    def __init__(self, reason: str, position: int) -> None:
        super().__init__(f"{reason} (at offset {position})")
        self.reason = reason
        self.position = position


@dataclass(frozen=True, slots=True)
class Limits:
    """Budgets enforced while parsing, before any SQL is built."""

    max_length: int = 1024
    max_depth: int = 24
    max_restrictions: int = 32


@dataclass(frozen=True, slots=True)
class Token:
    kind: str  # TEXT, STRING, OP, AND, OR, NOT, a char from _SINGLE_CHAR_TOKENS, or EOF
    text: str
    pos: int


def tokenize(source: str) -> list[Token]:
    """Split a filter into tokens; whitespace separates tokens and is otherwise dropped."""
    tokens: list[Token] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if ch in " \t\r\n":
            i += 1
        elif ch in _SINGLE_CHAR_TOKENS:
            tokens.append(Token(ch, ch, i))
            i += 1
        elif ch in "<>!=:":
            two = source[i : i + 2]
            op = two if two in COMPARATORS else ch
            if op not in COMPARATORS:
                raise FilterError(f"unknown operator {op!r}", i)
            tokens.append(Token("OP", op, i))
            i += len(op)
        elif ch in "\"'":
            j, chars = i + 1, list[str]()
            while j < n and source[j] != ch:
                if source[j] == "\\" and j + 1 < n:
                    j += 1
                chars.append(source[j])
                j += 1
            if j >= n:
                raise FilterError("unterminated string", i)
            tokens.append(Token("STRING", "".join(chars), i))
            i = j + 1
        else:
            j = i
            while j < n and source[j] not in _TEXT_STOP:
                j += 1
            text = source[i:j]
            kind = text if text in ("AND", "OR", "NOT") else "TEXT"
            tokens.append(Token(kind, text, i))
            i = j
    tokens.append(Token("EOF", "", n))
    return tokens


@dataclass(frozen=True, slots=True)
class Restriction:
    path: str
    op: str
    raw: str
    quoted: bool
    pos: int


@dataclass(frozen=True, slots=True)
class And:
    children: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class Or:
    children: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class Not:
    child: Node


type Node = Restriction | And | Or | Not


class _Parser:
    """Recursive descent over the AIP-160 EBNF: expression > sequence > factor > term."""

    def __init__(self, source: str, limits: Limits) -> None:
        self.tokens = tokenize(source)
        self.i = 0
        self.limits = limits
        self.depth = 0
        self.restrictions = 0

    def peek(self) -> Token:
        return self.tokens[self.i]

    def take(self, kind: str) -> Token:
        tok = self.tokens[self.i]
        if tok.kind != kind:
            raise FilterError(f"expected {kind}, found {tok.text or 'end of filter'!r}", tok.pos)
        self.i += 1
        return tok

    def enter(self, pos: int) -> None:
        self.depth += 1
        if self.depth > self.limits.max_depth:
            raise FilterError(f"nesting depth exceeds {self.limits.max_depth}", pos)

    def expression(self) -> Node:  # sequence {AND sequence}: loosest binding
        parts = [self.sequence()]
        while self.peek().kind == "AND":
            self.i += 1
            parts.append(self.sequence())
        return _flatten(And, parts)

    def sequence(self) -> Node:  # factor {WS factor}: implicit AND under exact matching
        parts = [self.factor()]
        while self.peek().kind in ("TEXT", "STRING", "NOT", "-", "("):
            parts.append(self.factor())
        return _flatten(And, parts)

    def factor(self) -> Node:  # term {OR term}: OR binds tighter than AND (AIP-160)
        parts = [self.term()]
        while self.peek().kind == "OR":
            self.i += 1
            parts.append(self.term())
        return _flatten(Or, parts)

    def term(self) -> Node:  # [NOT | -] simple
        tok = self.peek()
        if tok.kind in ("NOT", "-"):
            self.i += 1
            self.enter(tok.pos)
            child = self.term()
            self.depth -= 1
            return Not(child)
        if tok.kind == "(":
            self.i += 1
            self.enter(tok.pos)
            inner = self.expression()
            self.take(")")
            self.depth -= 1
            return inner
        return self.restriction()

    def restriction(self) -> Restriction:
        lhs = self.take("TEXT")
        nxt = self.peek()
        if nxt.kind == "(":
            raise FilterError(f"functions are not supported: {lhs.text!r}", lhs.pos)
        if nxt.kind != "OP":
            raise FilterError(f"global restriction {lhs.text!r} needs a comparator", lhs.pos)
        self.i += 1
        self.restrictions += 1
        if self.restrictions > self.limits.max_restrictions:
            raise FilterError(f"more than {self.limits.max_restrictions} restrictions", lhs.pos)
        sign = ""
        if self.peek().kind == "-":
            self.i += 1
            sign = "-"
        arg = self.peek()
        if arg.kind not in ("TEXT", "STRING") or (sign and arg.kind == "STRING"):
            raise FilterError("right-hand side must be a literal", arg.pos)
        self.i += 1
        return Restriction(lhs.text, nxt.text, sign + arg.text, arg.kind == "STRING", lhs.pos)


def _flatten(cls: type[And] | type[Or], parts: list[Node]) -> Node:
    if len(parts) == 1:
        return parts[0]
    flat: list[Node] = []
    for part in parts:
        flat.extend(part.children if isinstance(part, cls) else (part,))
    return cls(tuple(flat))


def parse_filter(source: str, limits: Limits | None = None) -> Node | None:
    """Parse an AIP-160 filter; an empty filter returns None (match everything)."""
    limits = limits or Limits()
    if len(source) > limits.max_length:
        raise FilterError(f"filter longer than {limits.max_length} characters", limits.max_length)
    if not source.strip():
        return None
    parser = _Parser(source, limits)
    node = parser.expression()
    parser.take("EOF")
    return node


def canonical(node: Node) -> str:
    """Render a fully parenthesised filter: stable text for cursor binding and logs."""
    match node:
        case Restriction(path=path, op=op, raw=raw, quoted=quoted):
            value = '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"' if quoted else raw
            return f"{path}:{value}" if op == ":" else f"{path} {op} {value}"
        case Not(child=child):
            inner = canonical(child)
            return f"NOT {inner}" if isinstance(child, Restriction) else f"NOT ({inner})"
        case And(children=kids) | Or(children=kids):
            joiner = " AND " if isinstance(node, And) else " OR "
            return joiner.join(
                canonical(k) if isinstance(k, Restriction | Not) else f"({canonical(k)})"
                for k in kids
            )


class FieldType(enum.Enum):
    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    TIMESTAMP = "timestamp"
    ENUM = "enum"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One allowlisted filter path, bound to an application-owned column."""

    column: ColumnElement[Any]
    type: FieldType
    enum_values: frozenset[str] = frozenset()


type Schema = Mapping[str, FieldSpec]
type Scalar = str | int | float | bool | dt.datetime


def _coerce(r: Restriction, spec: FieldSpec) -> Scalar:
    bad = FilterError(f"{r.raw!r} is not a valid {spec.type.value} for {r.path!r}", r.pos)
    match spec.type:
        case FieldType.STRING:
            return r.raw
        case FieldType.INT:
            if r.quoted or not _INT_RE.fullmatch(r.raw):
                raise bad
            value = int(r.raw)
            if not _INT64_MIN <= value <= _INT64_MAX:
                raise bad
            return value
        case FieldType.FLOAT:
            if r.quoted or not _FLOAT_RE.fullmatch(r.raw):
                raise bad
            number = float(r.raw)
            if not math.isfinite(number):
                raise bad
            return number
        case FieldType.BOOL:
            if r.quoted or r.raw not in ("true", "false"):
                raise bad
            return r.raw == "true"
        case FieldType.TIMESTAMP:
            if not _RFC3339_RE.fullmatch(r.raw):
                raise bad
            # Normalise to naive UTC: the column stores naive UTC, so an offset must not leak.
            return dt.datetime.fromisoformat(r.raw).astimezone(dt.UTC).replace(tzinfo=None)
        case FieldType.ENUM:
            if r.raw not in spec.enum_values:
                raise bad
            return r.raw


def _compile_restriction(r: Restriction, schema: Schema) -> ColumnElement[bool]:
    spec = schema.get(r.path)
    if spec is None:
        raise FilterError(f"unknown field {r.path!r}", r.pos)
    col = spec.column
    if r.op == ":":
        if r.raw != "*" or r.quoted:
            raise FilterError("only presence (field:*) is supported for ':'", r.pos)
        return col.is_not(None)
    if spec.type in (FieldType.BOOL, FieldType.ENUM) and r.op not in ("=", "!="):
        raise FilterError(f"{r.op!r} is not defined for {spec.type.value} fields", r.pos)
    value = _coerce(r, spec)
    if (
        spec.type is FieldType.STRING
        and r.quoted
        and r.op in ("=", "!=")
        and "*" in (r.raw[:1] + r.raw[-1:])
    ):
        lead, trail = r.raw.startswith("*"), r.raw.endswith("*") and len(r.raw) > 1
        core = r.raw[int(lead) : len(r.raw) - int(trail)]
        if lead and trail:
            like = col.contains(core, autoescape=True)
        elif lead:
            like = col.endswith(core, autoescape=True)
        else:
            like = col.startswith(core, autoescape=True)
        return like if r.op == "=" else not_(like)
    match r.op:
        case "=":
            return col == value
        case "!=":
            return col != value
        case "<":
            return col < value
        case "<=":
            return col <= value
        case ">":
            return col > value
        case _:
            return col >= value


def compile_filter(node: Node | None, schema: Schema) -> ColumnElement[bool] | None:
    """Type-check the AST against the allowlist and build a bound SQLAlchemy predicate."""
    return None if node is None else _compile_node(node, schema)


def _compile_node(node: Node, schema: Schema) -> ColumnElement[bool]:
    match node:
        case Restriction():
            return _compile_restriction(node, schema)
        case Not(child=child):
            return not_(_compile_node(child, schema))
        case And(children=kids):
            return and_(*(_compile_node(k, schema) for k in kids))
        case Or(children=kids):
            return or_(*(_compile_node(k, schema) for k in kids))


def build_books_app(engine: Engine, books: Table, schema: Schema) -> FastAPI:
    """A List endpoint whose `filter` string is parsed, checked, and compiled per request."""
    app = FastAPI()

    @app.get("/books")
    def list_books(filter_: Annotated[str, Query(alias="filter")] = "") -> JSONResponse:
        try:
            node = parse_filter(filter_)
            predicate = compile_filter(node, schema)
        except FilterError as exc:
            body = {"error": {"code": 400, "message": str(exc), "status": "INVALID_ARGUMENT"}}
            return JSONResponse(body, status_code=400)
        stmt = select(books.c.id).order_by(books.c.id)
        if predicate is not None:
            stmt = stmt.where(predicate)
        with engine.connect() as conn:
            ids = [row.id for row in conn.execute(stmt)]
        # Echo the canonical filter so clients see how precedence and decoding were applied.
        return JSONResponse({"filter": canonical(node) if node else "", "ids": ids})

    return app
