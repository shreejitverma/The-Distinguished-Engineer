"""Catalog of breaking changes in HTTP/JSON APIs, classified by message direction.

A request schema is read by the new server from bytes an old client wrote, so a change is safe
only if it does not narrow the set of documents the server accepts. A response schema is read by
an old client from bytes the new server wrote, so a change is safe only if it does not widen the
set of documents the server may emit. `classify` diffs two schemas written in a small JSON Schema
subset and labels every delta as narrowing, widening, or both; `breaking` applies the direction
rule; `witnesses` checks the verdict empirically with a real JSON Schema validator.

The runtime half shows the same four families against FastAPI and pydantic: request tightening
(`422` where `201` used to be), response loosening (`null` and integers beyond 2**53), enum
additions (strict, lossy, and raw-preserving readers), and behavioral shifts (`201` -> `202`,
`400` -> `422`).
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Protocol

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Iterable

    import httpx

type Schema = dict[str, Any]
type Direction = Literal["request", "response", "both"]

_OBJECT_KEYWORDS: Final = frozenset({"type", "properties", "required", "additionalProperties"})
_PROPERTY_KEYWORDS: Final = frozenset({"type", "enum", "maxLength", "maximum"})


@dataclass(frozen=True, slots=True)
class Delta:
    """One schema difference and its effect on the set of valid documents."""

    path: str
    kind: str
    narrows: bool
    widens: bool


class _Validator(Protocol):
    def is_valid(self, instance: Any) -> bool: ...


def _validator(schema: Schema) -> _Validator:
    validator: _Validator = Draft202012Validator(schema)
    return validator


@functools.lru_cache(maxsize=1024)
def _well_formed(canonical: str) -> bool:
    """Whether a schema, as canonical JSON text, is valid against the 2020-12 metaschema."""
    # Metaschema validation costs about a millisecond, so it runs once per distinct schema.
    return _validator(Draft202012Validator.META_SCHEMA).is_valid(json.loads(canonical))


def _check_supported(schema: Schema) -> None:
    """Refuse schemas the classifier does not model, so it never reports a silent 'safe'."""
    try:
        canonical = json.dumps(schema, sort_keys=True)
    except TypeError as exc:
        raise ValueError(f"not a plain, valid JSON Schema: {exc}") from exc
    # The round trip refuses Python-only values (integer keys, tuples) that JSON cannot carry.
    if json.loads(canonical) != schema or not _well_formed(canonical):
        raise ValueError("not a plain, valid JSON Schema 2020-12 document")
    unknown = set(schema) - _OBJECT_KEYWORDS
    if schema.get("type") != "object":
        unknown.add("type (the root must be 'object')")
    if not isinstance(schema.get("additionalProperties", True), bool):
        unknown.add("additionalProperties (only true or false)")
    for name, prop in schema.get("properties", {}).items():
        if isinstance(prop, bool):
            unknown.add(f"{name} (boolean schema)")
        else:
            unknown |= {f"{name}.{key}" for key in set(prop) - _PROPERTY_KEYWORDS}
    if unknown:
        raise ValueError(f"unsupported schema keywords: {sorted(unknown)}")


def _is_open(schema: Schema) -> bool:
    return schema.get("additionalProperties", True) is not False


def _types(prop: Schema) -> frozenset[str] | None:
    raw = prop.get("type")
    if raw is None:
        return None
    return frozenset([raw] if isinstance(raw, str) else raw)


def _covered(json_type: str, allowed: frozenset[str]) -> bool:
    # JSON Schema: every "integer" is also a "number".
    return json_type in allowed or (json_type == "integer" and "number" in allowed)


def _type_delta(old: frozenset[str] | None, new: frozenset[str] | None) -> tuple[bool, bool]:
    narrows = new is not None and (old is None or any(not _covered(t, new) for t in old))
    widens = old is not None and (new is None or any(not _covered(t, old) for t in new))
    return narrows, widens


def _enum_values(prop: Schema) -> frozenset[str] | None:
    raw = prop.get("enum")
    return None if raw is None else frozenset(json.dumps(value) for value in raw)


def _enum_delta(old: frozenset[str] | None, new: frozenset[str] | None) -> tuple[bool, bool]:
    narrows = new is not None and (old is None or bool(old - new))
    widens = old is not None and (new is None or bool(new - old))
    return narrows, widens


def _bound_delta(old: int | None, new: int | None) -> tuple[bool, bool]:
    # An absent upper bound is infinite.
    narrows = new is not None and (old is None or new < old)
    widens = old is not None and (new is None or new > old)
    return narrows, widens


def _property_deltas(path: str, old: Schema, new: Schema) -> list[Delta]:
    checks = {
        "type": _type_delta(_types(old), _types(new)),
        "enum": _enum_delta(_enum_values(old), _enum_values(new)),
        "maxLength": _bound_delta(old.get("maxLength"), new.get("maxLength")),
        "maximum": _bound_delta(old.get("maximum"), new.get("maximum")),
    }
    return [
        Delta(path, keyword, narrows, widens)
        for keyword, (narrows, widens) in checks.items()
        if narrows or widens
    ]


def classify(old: Schema, new: Schema) -> list[Delta]:
    """Diff two object schemas and label each delta as narrowing and/or widening."""
    _check_supported(old)
    _check_supported(new)
    deltas: list[Delta] = []
    old_open, new_open = _is_open(old), _is_open(new)
    if old_open and not new_open:
        deltas.append(Delta("$", "additionalProperties closed", narrows=True, widens=False))
    if new_open and not old_open:
        deltas.append(Delta("$", "additionalProperties opened", narrows=False, widens=True))
    old_props: dict[str, Schema] = old.get("properties", {})
    new_props: dict[str, Schema] = new.get("properties", {})
    for name in sorted(old_props.keys() | new_props.keys()):
        path = f"$.{name}"
        if name not in old_props:
            # The key was free (open schema) or forbidden (closed schema) before.
            if old_open:
                deltas.append(Delta(path, "property added", bool(new_props[name]), False))
            else:
                deltas.append(Delta(path, "property added", narrows=False, widens=True))
        elif name not in new_props:
            if new_open:
                deltas.append(Delta(path, "property removed", False, bool(old_props[name])))
            else:
                deltas.append(Delta(path, "property removed", narrows=True, widens=False))
        else:
            deltas.extend(_property_deltas(path, old_props[name], new_props[name]))
    old_required = set(old.get("required", []))
    new_required = set(new.get("required", []))
    deltas += [
        Delta(f"$.{n}", "required added", True, False) for n in sorted(new_required - old_required)
    ]
    deltas += [
        Delta(f"$.{n}", "required removed", False, True)
        for n in sorted(old_required - new_required)
    ]
    return deltas


def breaking(deltas: Iterable[Delta], direction: Direction) -> list[Delta]:
    """Requests must not narrow, responses must not widen, shared schemas must do neither."""
    if direction == "request":
        return [d for d in deltas if d.narrows]
    if direction == "response":
        return [d for d in deltas if d.widens]
    return [d for d in deltas if d.narrows or d.widens]


def witnesses(
    old: Schema, new: Schema, direction: Literal["request", "response"], probes: Iterable[Any]
) -> list[Any]:
    """Documents that prove a break: old-valid but new-invalid (request), or the reverse."""
    old_validator, new_validator = _validator(old), _validator(new)
    found: list[Any] = []
    for probe in probes:
        old_ok = old_validator.is_valid(probe)
        new_ok = new_validator.is_valid(probe)
        if (direction == "request" and old_ok and not new_ok) or (
            direction == "response" and new_ok and not old_ok
        ):
            found.append(probe)
    return found


def object_schema(properties: dict[str, Schema], required: list[str], *, open_: bool) -> Schema:
    """Build an object schema; `open_=False` is `additionalProperties: false` (closed)."""
    schema: Schema = {"type": "object", "properties": properties, "required": required}
    if not open_:
        schema["additionalProperties"] = False
    return schema


_STR: Final[Schema] = {"type": "string"}
_TITLE: Final[Schema] = {"type": "string", "maxLength": 100}
_FORMAT: Final[Schema] = {"type": "string", "enum": ["HARDBACK", "PAPERBACK"]}
_STATUS: Final[Schema] = {"type": "string", "enum": ["ACTIVE", "SUSPENDED"]}
_COPIES: Final[Schema] = {"type": "integer"}

# Request bodies are closed (unknown fields rejected); response bodies are open (tolerant reader).
REQUEST_V1: Final = object_schema(
    {"name": _STR, "title": _TITLE, "format": _FORMAT}, ["name", "title"], open_=False
)
RESPONSE_V1: Final = object_schema(
    {"name": _STR, "title": _STR, "status": _STATUS, "copies": _COPIES},
    ["name", "title", "status"],
    open_=True,
)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """A named schema change applied to a message in one direction."""

    name: str
    direction: Literal["request", "response"]
    old: Schema
    new: Schema


def _request(props: dict[str, Schema], required: list[str], *, open_: bool = False) -> Schema:
    return object_schema(props, required, open_=open_)


def _response(props: dict[str, Schema], required: list[str], *, open_: bool = True) -> Schema:
    return object_schema(props, required, open_=open_)


_RQ: Final = {"name": _STR, "title": _TITLE, "format": _FORMAT}
_RS: Final = {"name": _STR, "title": _STR, "status": _STATUS, "copies": _COPIES}
_RQ_REQ: Final = ["name", "title"]
_RS_REQ: Final = ["name", "title", "status"]

CATALOG: Final[tuple[CatalogEntry, ...]] = (
    CatalogEntry(
        "rq_add_optional_field", "request", REQUEST_V1, _request({**_RQ, "isbn": _STR}, _RQ_REQ)
    ),
    CatalogEntry(
        "rq_add_required_field",
        "request",
        REQUEST_V1,
        _request({**_RQ, "isbn": _STR}, [*_RQ_REQ, "isbn"]),
    ),
    CatalogEntry("rq_required_to_optional", "request", REQUEST_V1, _request(_RQ, ["name"])),
    CatalogEntry(
        "rq_optional_to_required", "request", REQUEST_V1, _request(_RQ, [*_RQ_REQ, "format"])
    ),
    CatalogEntry(
        "rq_remove_field", "request", REQUEST_V1, _request({"name": _STR, "title": _TITLE}, _RQ_REQ)
    ),
    CatalogEntry(
        "rq_tighten_max_length",
        "request",
        REQUEST_V1,
        _request({**_RQ, "title": {"type": "string", "maxLength": 40}}, _RQ_REQ),
    ),
    CatalogEntry(
        "rq_loosen_max_length",
        "request",
        REQUEST_V1,
        _request({**_RQ, "title": {"type": "string", "maxLength": 200}}, _RQ_REQ),
    ),
    CatalogEntry(
        "rq_extend_enum",
        "request",
        REQUEST_V1,
        _request(
            {**_RQ, "format": {"type": "string", "enum": ["HARDBACK", "PAPERBACK", "EBOOK"]}},
            _RQ_REQ,
        ),
    ),
    CatalogEntry(
        "rq_reduce_enum",
        "request",
        REQUEST_V1,
        _request({**_RQ, "format": {"type": "string", "enum": ["HARDBACK"]}}, _RQ_REQ),
    ),
    CatalogEntry(
        "rq_open_schema_add_typed_field",
        "request",
        _request(_RQ, _RQ_REQ, open_=True),
        _request({**_RQ, "isbn": _STR}, _RQ_REQ, open_=True),
    ),
    CatalogEntry(
        "rs_add_field",
        "response",
        RESPONSE_V1,
        _response({**_RS, "isbn": _STR}, [*_RS_REQ, "isbn"]),
    ),
    CatalogEntry(
        "rs_optional_to_required", "response", RESPONSE_V1, _response(_RS, [*_RS_REQ, "copies"])
    ),
    CatalogEntry(
        "rs_required_to_optional", "response", RESPONSE_V1, _response(_RS, ["name", "title"])
    ),
    CatalogEntry(
        "rs_remove_field",
        "response",
        RESPONSE_V1,
        _response({"name": _STR, "title": _STR, "status": _STATUS}, _RS_REQ),
    ),
    CatalogEntry(
        "rs_extend_enum",
        "response",
        RESPONSE_V1,
        _response(
            {**_RS, "status": {"type": "string", "enum": ["ACTIVE", "SUSPENDED", "ARCHIVED"]}},
            _RS_REQ,
        ),
    ),
    CatalogEntry(
        "rs_reduce_enum",
        "response",
        RESPONSE_V1,
        _response({**_RS, "status": {"type": "string", "enum": ["ACTIVE"]}}, _RS_REQ),
    ),
    CatalogEntry(
        "rs_make_nullable",
        "response",
        RESPONSE_V1,
        _response({**_RS, "title": {"type": ["string", "null"]}}, _RS_REQ),
    ),
    CatalogEntry(
        "rs_integer_to_number",
        "response",
        RESPONSE_V1,
        _response({**_RS, "copies": {"type": "number"}}, _RS_REQ),
    ),
    CatalogEntry(
        "rs_closed_schema_add_field",
        "response",
        _response(_RS, _RS_REQ, open_=False),
        _response({**_RS, "isbn": _STR}, _RS_REQ, open_=False),
    ),
)


def _variants(
    base: dict[str, Any], edits: Iterable[dict[str, Any]], drops: Iterable[str]
) -> list[Any]:
    out: list[Any] = [base]
    out += [{**base, **edit} for edit in edits]
    out += [{k: v for k, v in base.items() if k != key} for key in drops]
    return out


# A shared probe pool: each catalog verdict must be reproducible by some document in it.
PROBES: Final[tuple[Any, ...]] = (
    *_variants(
        {"name": "books/1", "title": "SICP", "format": "HARDBACK"},
        [
            {"isbn": "978-0262510875"},
            {"isbn": 7},
            {"title": "x" * 50},
            {"title": "x" * 150},
            {"format": "PAPERBACK"},
            {"format": "EBOOK"},
        ],
        ["title", "format"],
    ),
    *_variants(
        {"name": "books/1", "title": "SICP", "status": "ACTIVE", "copies": 3},
        [
            {"isbn": "978-0262510875"},
            {"isbn": 7},
            {"status": "SUSPENDED"},
            {"status": "ARCHIVED"},
            {"title": None},
            {"copies": 1.5},
            {"copies": "3"},
        ],
        ["status", "copies"],
    ),
)


# Runtime half: one v1 server and a v2 "framework rewrite" that its team believed compatible.


class CreateBookV1(BaseModel):
    title: str


class CreateBookV2(BaseModel):
    title: Annotated[str, Field(max_length=40)]


def build_books_server_v1() -> FastAPI:
    """v1: synchronous create (`201` with the book) and a custom `400` for invalid input."""
    app = FastAPI()

    @app.exception_handler(RequestValidationError)
    async def as_bad_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": "INVALID_ARGUMENT"}, status_code=400)

    @app.post("/books", status_code=201)
    async def create_book(body: CreateBookV1) -> dict[str, str]:
        return {"id": "books/1", "title": body.title}

    return app


def build_books_server_v2() -> FastAPI:
    """v2: `max_length=40` on `title`, FastAPI's default `422`, and an asynchronous `202`."""
    app = FastAPI()

    @app.post("/books", status_code=202)
    async def create_book(body: CreateBookV2) -> dict[str, str | bool]:
        return {"id": "operations/1", "done": False}

    return app


def legacy_created_id(response: httpx.Response) -> str:
    """v1 SDK: any 2xx is success and the body's `id` is the new book's name."""
    response.raise_for_status()
    return str(response.json()["id"])


class UnexpectedStatusError(Exception):
    """Raised by a client that pins the exact success status it was written against."""


def strict_created_id(response: httpx.Response) -> str:
    """A client that treats a different success code as a contract change, not as success."""
    if response.status_code != 201:
        raise UnexpectedStatusError(f"expected 201 Created, got {response.status_code}")
    return str(response.json()["id"])


def legacy_should_retry(status_code: int) -> bool:
    """v1 SDK: `400` means fix the input; every other failure is assumed transient."""
    return not 200 <= status_code < 300 and status_code != 400


def class_based_should_retry(status_code: int) -> bool:
    """Decide by status class (RFC 9110 Section 15): only `429` and `5xx` are retried."""
    return status_code == 429 or status_code // 100 == 5


class Status(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class LossyStatus(StrEnum):
    """Fallback enum: unknown values collapse to `UNKNOWN`, losing the server's value."""

    UNKNOWN = "UNKNOWN"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"

    @classmethod
    def _missing_(cls, value: object) -> LossyStatus:
        return cls.UNKNOWN


class BookLiteralClient(BaseModel):
    name: str
    status: Literal["ACTIVE", "SUSPENDED"]


class BookEnumClient(BaseModel):
    name: str
    status: Status


class BookLossyClient(BaseModel):
    name: str
    status: LossyStatus


class BookOpenClient(BaseModel):
    """Raw-preserving reader: keeps the wire string and derives the known member on demand."""

    name: str
    status: str

    @property
    def known_status(self) -> Status | None:
        try:
            return Status(self.status)
        except ValueError:
            return None


class BookClosedClient(BaseModel):
    """Closed reader: `extra="forbid"` turns every additive response field into a wire break."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: str


class TitleV1Client(BaseModel):
    title: str


class TitleTolerantClient(BaseModel):
    title: str | None


class ShortTitle(BaseModel):
    title: Annotated[str, Field(max_length=3)]


def parse_like_javascript(raw: str | bytes) -> Any:
    """Decode JSON the way `JSON.parse` does: every number becomes an IEEE 754 binary64."""
    return json.loads(raw, parse_int=float)
