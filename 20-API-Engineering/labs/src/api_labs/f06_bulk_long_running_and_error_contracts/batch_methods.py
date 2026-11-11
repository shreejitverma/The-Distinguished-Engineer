"""Google AIP-231 to AIP-235 batch methods: atomic, size-bounded, and order-preserving.

One FastAPI app exposes BatchGet, BatchCreate, BatchUpdate, and BatchDelete for a
`publishers/{publisher}/books/{book}` collection stored in one in-memory SQLite table.
Every mutation runs in one transaction and every error uses the AIP-193 HTTP/JSON shape.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Final, Literal
from urllib.parse import urlencode

import h11
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Sequence

MAX_BATCH_SIZE: Final = 1000
WILDCARD: Final = "-"
MUTABLE_FIELDS: Final = ("title", "author")

_ID: Final = r"[a-z][a-z0-9-]{0,62}"
_ID_RE: Final = re.compile(_ID)
_PARENT_RE: Final = re.compile(rf"publishers/(?P<publisher>{_ID})")
_NAME_RE: Final = re.compile(rf"publishers/(?P<publisher>{_ID})/books/(?P<book>{_ID})")

Code = Literal["INVALID_ARGUMENT", "NOT_FOUND", "ALREADY_EXISTS"]
_HTTP_STATUS: Final[dict[Code, int]] = {
    "INVALID_ARGUMENT": 400,
    "NOT_FOUND": 404,
    "ALREADY_EXISTS": 409,
}


class ApiError(Exception):
    """A google.rpc.Status rendered in the AIP-193 HTTP/1.1+JSON representation."""

    def __init__(self, code: Code, message: str, field_path: str | None = None) -> None:
        super().__init__(message)
        self.code: Code = code
        self.message = message
        self.field_path = field_path

    @property
    def http_status(self) -> int:
        return _HTTP_STATUS[self.code]

    def body(self) -> dict[str, object]:
        error: dict[str, object] = {
            "code": self.http_status,
            "message": self.message,
            "status": self.code,
        }
        if self.field_path is not None:
            error["details"] = [
                {
                    "@type": "type.googleapis.com/google.rpc.BadRequest",
                    "fieldViolations": [{"field": self.field_path, "description": self.message}],
                }
            ]
        return {"error": error}


def _invalid(field_path: str, message: str) -> ApiError:
    return ApiError("INVALID_ARGUMENT", f"{field_path}: {message}", field_path)


class _Message(BaseModel):
    """Proto3 JSON style: lowerCamelCase keys on the wire, both spellings accepted."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        extra="forbid",
        frozen=True,
    )


class Book(_Message):
    name: str = ""
    title: str = ""
    author: str = ""


class CreateBookRequest(_Message):
    parent: str = ""
    book_id: str
    book: Book


class UpdateBookRequest(_Message):
    book: Book
    update_mask: str = ""


class BatchCreateBooksRequest(_Message):
    requests: list[CreateBookRequest]


class BatchUpdateBooksRequest(_Message):
    requests: list[UpdateBookRequest]
    update_mask: str = ""


class BatchDeleteBooksRequest(_Message):
    names: list[str]


class BatchBooksResponse(_Message):
    books: list[Book]


class Empty(_Message):
    pass


def check_batch_size(count: int, field_path: str) -> None:
    """Reject an empty batch and a batch above MAX_BATCH_SIZE before touching storage."""
    if count == 0:
        raise _invalid(field_path, "must not be empty")
    if count > MAX_BATCH_SIZE:
        raise _invalid(field_path, f"has {count} entries; the maximum is {MAX_BATCH_SIZE}")


def check_url_parent(publisher: str) -> None:
    if publisher != WILDCARD and _ID_RE.fullmatch(publisher) is None:
        raise _invalid("parent", f"publishers/{publisher} is not a publisher name")


def parse_book_name(name: str, field_path: str, url_publisher: str) -> str:
    """Validate one resource name and its parent against the URL parent (or `-`)."""
    match = _NAME_RE.fullmatch(name)
    if match is None:
        raise _invalid(field_path, f"{name!r} is not a book name")
    if url_publisher not in (WILDCARD, match["publisher"]):
        raise _invalid(field_path, f"{name} is not under publishers/{url_publisher}")
    return name


def reject_duplicates(names: Sequence[str], field_path: str) -> None:
    first_index: dict[str, int] = {}
    for index, name in enumerate(names):
        if name in first_index:
            raise _invalid(
                field_path.format(index),
                f"{name} duplicates {field_path.format(first_index[name])}",
            )
        first_index[name] = index


def align_to_request(names: Sequence[str], rows: Iterable[Book]) -> list[Book]:
    """Return one Book per requested name, in request order; fail if any name is absent."""
    by_name = {row.name: row for row in rows}
    for index, name in enumerate(names):
        if name not in by_name:
            raise ApiError("NOT_FOUND", f"names[{index}]: {name} not found")
    return [by_name[name] for name in names]


@dataclass
class BookStore:
    """One SQLite connection in autocommit mode; transactions are explicit BEGIN/COMMIT."""

    conn: sqlite3.Connection
    statements: list[str] = field(default_factory=list[str])

    @classmethod
    def in_memory(cls) -> BookStore:
        conn = sqlite3.connect(":memory:", autocommit=True)
        conn.execute(
            "CREATE TABLE books (name TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL)"
        )
        store = cls(conn)
        conn.set_trace_callback(store.statements.append)
        return store

    @contextmanager
    def transaction(self) -> Generator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def select(self, names: Iterable[str]) -> list[Book]:
        unique = list(dict.fromkeys(names))
        marks = ", ".join("?" for _ in unique)
        # Only "?" placeholders are interpolated; every value is a bound parameter.
        sql = f"SELECT name, title, author FROM books WHERE name IN ({marks})"  # noqa: S608
        cursor = self.conn.execute(sql, unique)
        return [Book(name=n, title=t, author=a) for n, t, a in cursor.fetchall()]

    def insert_returning(self, books: Sequence[Book]) -> list[Book]:
        """One multi-row INSERT; RETURNING row order is arbitrary per the SQLite docs."""
        values = ", ".join("(?, ?, ?)" for _ in books)
        params = [value for book in books for value in (book.name, book.title, book.author)]
        sql = (
            f"INSERT INTO books (name, title, author) VALUES {values}"  # noqa: S608
            " RETURNING name, title, author"
        )
        cursor = self.conn.execute(sql, params)
        return [Book(name=n, title=t, author=a) for n, t, a in cursor.fetchall()]

    def update_returning(self, name: str, changes: dict[str, str]) -> Book | None:
        # Column names come only from MUTABLE_FIELDS; values are bound parameters.
        assignments = ", ".join(f"{column} = ?" for column in changes)
        sql = f"UPDATE books SET {assignments} WHERE name = ? RETURNING name, title, author"  # noqa: S608
        row = self.conn.execute(sql, [*changes.values(), name]).fetchone()
        return None if row is None else Book(name=row[0], title=row[1], author=row[2])

    def delete_returning(self, names: Sequence[str]) -> set[str]:
        marks = ", ".join("?" for _ in names)
        sql = f"DELETE FROM books WHERE name IN ({marks}) RETURNING name"  # noqa: S608
        return {row[0] for row in self.conn.execute(sql, list(names)).fetchall()}


def _field_path(loc: Sequence[int | str]) -> str:
    """("body", "requests", 2, "bookId") -> "requests[2].bookId" (google.rpc.BadRequest form)."""
    path = ""
    for part in loc[1:]:
        path += f"[{part}]" if isinstance(part, int) else (f".{part}" if path else part)
    return path or str(loc[0])


def build_app(store: BookStore) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        error = _invalid(_field_path(first["loc"]), str(first["msg"]))
        return JSONResponse(error.body(), status_code=error.http_status)

    @app.get("/v1/publishers/{publisher}/books:batchGet")
    async def batch_get_books(
        publisher: str, names: Annotated[list[str] | None, Query()] = None
    ) -> BatchBooksResponse:
        check_url_parent(publisher)
        requested = names or []
        check_batch_size(len(requested), "names")
        for index, name in enumerate(requested):
            parse_book_name(name, f"names[{index}]", publisher)
        # One SELECT is one consistent snapshot; without ORDER BY its row order is unspecified.
        return BatchBooksResponse(books=align_to_request(requested, store.select(requested)))

    @app.post("/v1/publishers/{publisher}/books:batchCreate")
    async def batch_create_books(
        publisher: str, body: BatchCreateBooksRequest
    ) -> BatchBooksResponse:
        check_url_parent(publisher)
        check_batch_size(len(body.requests), "requests")
        books: list[Book] = []
        for index, child in enumerate(body.requests):
            prefix = f"requests[{index}]"
            child_parent = child.parent or f"publishers/{publisher}"
            parent = _PARENT_RE.fullmatch(child_parent)
            if parent is None:
                raise _invalid(f"{prefix}.parent", "required when the URL parent is '-'")
            if publisher not in (WILDCARD, parent["publisher"]):
                raise _invalid(f"{prefix}.parent", f"must match publishers/{publisher}")
            if _ID_RE.fullmatch(child.book_id) is None:
                raise _invalid(f"{prefix}.bookId", f"{child.book_id!r} is not a valid id")
            name = f"{child_parent}/books/{child.book_id}"
            books.append(Book(name=name, title=child.book.title, author=child.book.author))
        names = [book.name for book in books]
        reject_duplicates(names, "requests[{}].bookId")
        try:
            with store.transaction():
                created = store.insert_returning(books)
        except sqlite3.IntegrityError:
            taken = {book.name for book in store.select(names)}
            conflicts = [index for index, name in enumerate(names) if name in taken]
            if not conflicts:
                raise
            index = conflicts[0]
            raise ApiError(
                "ALREADY_EXISTS", f"requests[{index}]: {names[index]} already exists"
            ) from None
        return BatchBooksResponse(books=align_to_request(names, created))

    @app.post("/v1/publishers/{publisher}/books:batchUpdate")
    async def batch_update_books(
        publisher: str, body: BatchUpdateBooksRequest
    ) -> BatchBooksResponse:
        check_url_parent(publisher)
        check_batch_size(len(body.requests), "requests")
        plan: list[tuple[str, dict[str, str]]] = []
        for index, child in enumerate(body.requests):
            prefix = f"requests[{index}]"
            name = parse_book_name(child.book.name, f"{prefix}.book.name", publisher)
            if body.update_mask and child.update_mask and child.update_mask != body.update_mask:
                raise _invalid(f"{prefix}.updateMask", "must match the hoisted updateMask")
            mask = child.update_mask or body.update_mask
            populated = [path for path in MUTABLE_FIELDS if getattr(child.book, path)]
            paths = mask.split(",") if mask else populated
            unknown = [path for path in paths if path not in MUTABLE_FIELDS]
            if unknown or not paths:
                raise _invalid(f"{prefix}.updateMask", f"selects no mutable field: {unknown}")
            plan.append((name, {path: str(getattr(child.book, path)) for path in paths}))
        reject_duplicates([name for name, _ in plan], "requests[{}].book.name")
        updated: list[Book] = []
        with store.transaction():
            for index, (name, changes) in enumerate(plan):
                row = store.update_returning(name, changes)
                if row is None:
                    raise ApiError("NOT_FOUND", f"requests[{index}].book.name: {name} not found")
                updated.append(row)
        return BatchBooksResponse(books=updated)

    @app.post("/v1/publishers/{publisher}/books:batchDelete")
    async def batch_delete_books(publisher: str, body: BatchDeleteBooksRequest) -> Empty:
        check_url_parent(publisher)
        check_batch_size(len(body.names), "names")
        for index, name in enumerate(body.names):
            parse_book_name(name, f"names[{index}]", publisher)
        reject_duplicates(body.names, "names[{}]")
        with store.transaction():
            deleted = store.delete_returning(body.names)
            for index, name in enumerate(body.names):
                if name not in deleted:
                    raise ApiError("NOT_FOUND", f"names[{index}]: {name} not found")
        return Empty()

    return app


def batch_get_target(publisher: str, names: Sequence[str]) -> bytes:
    """The HTTP/1.1 request-target a client sends for BatchGet (AIP-127 repeated query field)."""
    query = urlencode([("names", name) for name in names])
    return f"/v1/publishers/{publisher}/books:batchGet?{query}".encode("ascii")


def h11_server_parse(request: bytes, chunk_size: int, max_incomplete_event_size: int) -> int:
    """Feed a request to an h11 server in chunks; return 0 on a parsed Request, else the hint."""
    conn = h11.Connection(h11.SERVER, max_incomplete_event_size=max_incomplete_event_size)
    for start in range(0, len(request), chunk_size):
        conn.receive_data(request[start : start + chunk_size])
        try:
            event = conn.next_event()
        except h11.RemoteProtocolError as exc:
            return exc.error_status_hint
        if isinstance(event, h11.Request):
            return 0
    raise AssertionError("request head never completed")
