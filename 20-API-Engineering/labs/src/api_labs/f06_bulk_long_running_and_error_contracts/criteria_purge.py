"""Criteria-based purge (AIP-165): filter-driven deletion, force=False dry runs, LRO handoff.

`POST /v1/publishers/{publisher}/books:purge` takes `{"filter", "force", "requestId"}`.
With `force` false or absent it returns a completed operation whose response carries
`purgeCount` and at most `sample_max` names, and deletes nothing.
With `force` true it returns `202 Accepted` and a pending operation; `PurgeService.step`
deletes one chunk of at most `chunk_size` books per call, so tests drive progress.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

SAMPLE_MAX = 100
CHUNK_SIZE = 1000
MAX_FILTER_LENGTH = 5000
MAX_CONDITIONS = 6
TYPE_PREFIX = "type.googleapis.com/library.example.v1."

Availability = Literal["IN_STOCK", "BACKORDER", "OUT_OF_STOCK"]
AVAILABILITIES: frozenset[str] = frozenset({"IN_STOCK", "BACKORDER", "OUT_OF_STOCK"})
FIELD_TYPES: dict[str, str] = {
    "availability": "enum",
    "create_time": "timestamp",
    "page_count": "int",
}
PARENT_RE = re.compile(r"publishers/(-|[a-z0-9][a-z0-9-]{0,62})")
TOKEN_RE = re.compile(r'\s*(?:(AND)\b|([a-z_]+)\s*(<=|>=|!=|=|<|>)\s*("[^"]*"|-?\d+))')


class ApiError(Exception):
    """An AIP-193 style error: HTTP status, `google.rpc.Code` name, and message."""

    def __init__(self, http_status: int, status: str, message: str) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.status = status
        self.message = message


def invalid(message: str) -> ApiError:
    return ApiError(400, "INVALID_ARGUMENT", message)


@dataclass(slots=True)
class Book:
    name: str
    availability: Availability
    create_time: datetime
    page_count: int
    create_seq: int


@dataclass(frozen=True, slots=True)
class Condition:
    field: str
    op: str
    value: str | int | datetime

    def matches(self, book: Book) -> bool:
        actual: str | int | datetime = getattr(book, self.field)
        if self.op == "=":
            return actual == self.value
        if self.op == "!=":
            return actual != self.value
        if isinstance(actual, int) and isinstance(self.value, int):
            return _ordered(self.op, actual, self.value)
        if isinstance(actual, datetime) and isinstance(self.value, datetime):
            return _ordered(self.op, actual, self.value)
        raise invalid(f"operator {self.op} does not apply to field {self.field!r}")


def _ordered[T: (int, datetime)](op: str, actual: T, value: T) -> bool:
    if op == "<":
        return actual < value
    if op == "<=":
        return actual <= value
    if op == ">":
        return actual > value
    return actual >= value


@dataclass(frozen=True, slots=True)
class Filter:
    """A conjunction of conditions; `match_all` is the AIP-165 `"*"` wildcard."""

    conditions: tuple[Condition, ...]
    match_all: bool = False

    def matches(self, book: Book) -> bool:
        return self.match_all or all(c.matches(book) for c in self.conditions)


def _typed_value(field: str, op: str, raw: str) -> str | int | datetime:
    kind = FIELD_TYPES[field]
    quoted = raw.startswith('"')
    text = raw[1:-1] if quoted else raw
    if kind == "int":
        if quoted:
            raise invalid(f"field {field!r} is int32; got string {raw}")
        return int(text)
    if not quoted:
        raise invalid(f"field {field!r} needs a double-quoted value; got {raw}")
    if kind == "enum":
        if op not in {"=", "!="}:
            raise invalid(f"enum field {field!r} supports only = and !=")
        if text not in AVAILABILITIES:
            raise invalid(f"{text!r} is not a valid {field} value")
        return text
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        raise invalid(f"{text!r} is not an RFC 3339 timestamp") from None
    if stamp.utcoffset() is None:
        raise invalid(f"timestamp {text!r} must carry an offset, for example Z")
    return stamp


def parse_filter(text: str) -> Filter:
    """Parse the AIP-160 subset `field op value (AND field op value)*`, or the `"*"` wildcard.

    Unknown fields, mistyped values, and trailing garbage raise `INVALID_ARGUMENT`;
    nothing is ever silently ignored, because an ignored condition widens the match set.
    """
    if len(text) > MAX_FILTER_LENGTH:
        raise invalid(f"filter exceeds {MAX_FILTER_LENGTH} characters")
    stripped = text.strip()
    if not stripped:
        raise invalid('filter is required; use "*" to purge everything under parent')
    if stripped == "*":
        return Filter(conditions=(), match_all=True)
    conditions: list[Condition] = []
    pos = 0
    expect_condition = True
    while pos < len(stripped):
        token = TOKEN_RE.match(stripped, pos)
        if token is None:
            raise invalid(f"cannot parse filter at offset {pos}: {stripped[pos:]!r}")
        conjunction, field, op, raw = token.groups()
        if conjunction is not None:
            if expect_condition:
                raise invalid(f"unexpected AND at offset {pos}")
            expect_condition = True
        else:
            if not expect_condition:
                raise invalid(f"missing AND before offset {pos}")
            if field not in FIELD_TYPES:
                raise invalid(f"unknown field {field!r}; filterable: {sorted(FIELD_TYPES)}")
            conditions.append(Condition(field, op, _typed_value(field, op, raw)))
            expect_condition = False
        pos = token.end()
    if expect_condition:
        raise invalid("filter ends with AND")
    if len(conditions) > MAX_CONDITIONS:
        raise invalid(f"filter has {len(conditions)} conditions; maximum is {MAX_CONDITIONS}")
    return Filter(conditions=tuple(conditions))


class BookStore:
    """In-memory collection; `seq` is a monotonic counter bumped by every create."""

    def __init__(self) -> None:
        self._books: dict[str, Book] = {}
        self.seq = 0

    def create(
        self, name: str, availability: Availability, create_time: str, page_count: int
    ) -> Book:
        self.seq += 1
        stamp = datetime.fromisoformat(create_time).astimezone(UTC)
        book = Book(name, availability, stamp, page_count, self.seq)
        self._books[name] = book
        return book

    def set_availability(self, name: str, availability: Availability) -> None:
        self._books[name].availability = availability

    def books(self) -> list[Book]:
        """Every book in name order: the keyset order purge chunks walk."""
        return [self._books[name] for name in sorted(self._books)]

    def delete(self, name: str) -> None:
        del self._books[name]


@dataclass(slots=True)
class PurgeJob:
    name: str
    prefix: str
    predicate: Filter
    high_water: int
    cursor: str = ""
    deleted: int = 0
    done: bool = False


class PurgeBooksRequest(BaseModel):
    """The JSON body (`body: "*"`); `parent` comes from the URI path."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    filter: str
    force: StrictBool = False
    request_id: Annotated[str | None, Field(alias="requestId", max_length=64)] = None


def _prefix(parent: str) -> str:
    if PARENT_RE.fullmatch(parent) is None:
        raise invalid(f"parent {parent!r} must be publishers/{{publisher}} or publishers/-")
    return "publishers/" if parent == "publishers/-" else f"{parent}/books/"


class PurgeService:
    def __init__(
        self, store: BookStore, *, sample_max: int = SAMPLE_MAX, chunk_size: int = CHUNK_SIZE
    ) -> None:
        self.store = store
        self.sample_max = sample_max
        self.chunk_size = chunk_size
        self.jobs: dict[str, PurgeJob] = {}
        self.by_request_id: dict[str, str] = {}
        self._next_id = 0

    def _eligible(self, book: Book, prefix: str, predicate: Filter, high_water: int) -> bool:
        return (
            book.name.startswith(prefix)
            and book.create_seq <= high_water
            and predicate.matches(book)
        )

    def purge(self, parent: str, request: PurgeBooksRequest) -> dict[str, Any]:
        prefix = _prefix(parent)
        predicate = parse_filter(request.filter)
        if not request.force:
            matched = [
                book.name
                for book in self.store.books()
                if self._eligible(book, prefix, predicate, self.store.seq)
            ]
            response = {
                "@type": TYPE_PREFIX + "PurgeBooksResponse",
                "purgeCount": len(matched),
                "purgeSample": matched[: self.sample_max],
            }
            return {"done": True, "response": response}
        if request.request_id is not None and request.request_id in self.by_request_id:
            return self.operation(self.by_request_id[request.request_id])
        self._next_id += 1
        job = PurgeJob(f"operations/purge-{self._next_id}", prefix, predicate, self.store.seq)
        self.jobs[job.name] = job
        if request.request_id is not None:
            self.by_request_id[request.request_id] = job.name
        return self.operation(job.name)

    def step(self, operation_name: str) -> int:
        """Delete up to `chunk_size` eligible books after the keyset cursor; return the count."""
        job = self.jobs[operation_name]
        deleted = 0
        for book in self.store.books():
            if book.name <= job.cursor:
                continue
            if deleted == self.chunk_size:
                break
            job.cursor = book.name
            if self._eligible(book, job.prefix, job.predicate, job.high_water):
                self.store.delete(book.name)
                deleted += 1
        else:
            job.done = True
        job.deleted += deleted
        return deleted

    def operation(self, operation_name: str) -> dict[str, Any]:
        job = self.jobs.get(operation_name)
        if job is None:
            raise ApiError(404, "NOT_FOUND", f"{operation_name!r} does not exist")
        body: dict[str, Any] = {
            "name": job.name,
            "done": job.done,
            "metadata": {
                "@type": TYPE_PREFIX + "PurgeBooksMetadata",
                "deletedCount": job.deleted,
            },
        }
        if job.done:
            body["response"] = {
                "@type": TYPE_PREFIX + "PurgeBooksResponse",
                "purgeCount": job.deleted,
            }
        return body


def _error_body(error: ApiError) -> dict[str, Any]:
    return {"error": {"code": error.http_status, "status": error.status, "message": error.message}}


def create_app(service: PurgeService) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(_error_body(exc), status_code=exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def bad_body(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = sorted({".".join(str(p) for p in e["loc"][1:]) for e in exc.errors()})
        error = invalid(f"invalid request fields: {fields}")
        return JSONResponse(_error_body(error), status_code=400)

    @app.post("/v1/publishers/{publisher}/books:purge")
    async def purge_books(publisher: str, body: PurgeBooksRequest) -> JSONResponse:
        operation = service.purge(f"publishers/{publisher}", body)
        if operation["done"]:
            return JSONResponse(operation, status_code=200)
        location = f"/v1/{operation['name']}"
        return JSONResponse(operation, status_code=202, headers={"Location": location})

    @app.get("/v1/operations/{operation_id}")
    async def get_operation(operation_id: str) -> dict[str, Any]:
        return service.operation(f"operations/{operation_id}")

    return app
