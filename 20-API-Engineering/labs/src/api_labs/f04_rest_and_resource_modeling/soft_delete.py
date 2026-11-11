"""AIP-164 soft delete, undelete, expunge, purge windows, and tombstones over SQLite."""

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

RESOURCE_ID = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")  # AIP-122, RFC 1034
DEFAULT_RETENTION = timedelta(days=30)
Auth = Annotated[str | None, Header()]

# Partial unique index: the business key is unique among live rows only.
SCHEMA = """
CREATE TABLE books (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    isbn TEXT NOT NULL,
    create_us INTEGER NOT NULL,
    delete_us INTEGER,
    purge_us INTEGER
);
CREATE UNIQUE INDEX books_isbn_live ON books (isbn) WHERE delete_us IS NULL;
CREATE TABLE tombstones (id TEXT PRIMARY KEY, purged_us INTEGER NOT NULL);
"""

# The trap: NULLs are distinct in a UNIQUE index, so live rows never collide.
NAIVE_SCHEMA = """
CREATE TABLE books (
    id TEXT PRIMARY KEY,
    isbn TEXT NOT NULL,
    delete_us INTEGER,
    UNIQUE (isbn, delete_us)
);
"""


def to_us(moment: datetime) -> int:
    """Encode an aware datetime as integer microseconds since the Unix epoch."""
    delta = moment - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def from_us(micros: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=micros)


class ApiError(Exception):
    """A google.rpc.Status error rendered in the AIP-193 HTTP/JSON shape."""

    def __init__(self, code: int, status: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


class BookIn(BaseModel):
    title: str
    isbn: str


class Book(BaseModel):
    name: str
    title: str
    isbn: str
    state: Literal["ACTIVE", "DELETED"]
    create_time: datetime
    delete_time: datetime | None = None
    purge_time: datetime | None = None


class ListBooksResponse(BaseModel):
    books: list[Book]


@dataclass
class SoftDeleteStore:
    """Book storage where Delete marks, Expunge and the reaper remove, and IDs stay reserved."""

    clock: Callable[[], datetime]
    retention: timedelta = DEFAULT_RETENTION
    conn: sqlite3.Connection = field(init=False)

    def __post_init__(self) -> None:
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.executescript(SCHEMA)

    def _row(self, book_id: str) -> Book | None:
        row: tuple[str, str, str, int, int | None, int | None] | None = self.conn.execute(
            "SELECT id, title, isbn, create_us, delete_us, purge_us FROM books WHERE id = ?",
            (book_id,),
        ).fetchone()
        if row is None:
            return None
        rid, title, isbn, create_us, delete_us, purge_us = row
        return Book(
            name=f"books/{rid}",
            title=title,
            isbn=isbn,
            state="ACTIVE" if delete_us is None else "DELETED",
            create_time=from_us(create_us),
            delete_time=None if delete_us is None else from_us(delete_us),
            purge_time=None if purge_us is None else from_us(purge_us),
        )

    def _require(self, book_id: str) -> Book:
        book = self._row(book_id)
        if book is None:
            raise ApiError(404, "NOT_FOUND", f"books/{book_id} not found")
        return book

    def create(self, book_id: str, body: BookIn) -> Book:
        reserved = self.conn.execute(
            "SELECT 1 FROM books WHERE id = ? UNION ALL SELECT 1 FROM tombstones WHERE id = ?",
            (book_id, book_id),
        ).fetchone()
        if reserved is not None:
            raise ApiError(409, "ALREADY_EXISTS", f"books/{book_id} is taken or tombstoned")
        try:
            self.conn.execute(
                "INSERT INTO books (id, title, isbn, create_us) VALUES (?, ?, ?, ?)",
                (book_id, body.title, body.isbn, to_us(self.clock())),
            )
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "ALREADY_EXISTS", f"isbn {body.isbn} is live") from exc
        return self._require(book_id)

    def get(self, book_id: str) -> Book:
        return self._require(book_id)

    def list_books(self, *, show_deleted: bool) -> list[Book]:
        sql = "SELECT id FROM books"
        if not show_deleted:
            sql += " WHERE delete_us IS NULL"
        ids: list[tuple[str]] = self.conn.execute(sql + " ORDER BY id").fetchall()
        return [self._require(rid) for (rid,) in ids]

    def delete(self, book_id: str, *, allow_missing: bool) -> Book | None:
        now = self.clock()
        # Compare-and-set: only a live row is stamped, so a retry cannot extend retention.
        stamped = self.conn.execute(
            "UPDATE books SET delete_us = ?, purge_us = ? WHERE id = ? AND delete_us IS NULL",
            (to_us(now), to_us(now + self.retention), book_id),
        ).rowcount
        if stamped == 1:
            return self._require(book_id)
        current = self._row(book_id)
        if allow_missing:
            return current
        raise ApiError(404, "NOT_FOUND", f"books/{book_id} not found or already deleted")

    def undelete(self, book_id: str) -> Book:
        try:
            restored = self.conn.execute(
                "UPDATE books SET delete_us = NULL, purge_us = NULL "
                "WHERE id = ? AND delete_us IS NOT NULL",
                (book_id,),
            ).rowcount
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "ALREADY_EXISTS", "a live book now holds this isbn") from exc
        if restored == 0:
            book = self._require(book_id)
            raise ApiError(409, "ALREADY_EXISTS", f"{book.name} is not deleted")
        return self._require(book_id)

    def _remove(self, book_id: str, *, due_us: int | None = None) -> bool:
        """Remove the row and tombstone it atomically; False if nothing was removed.

        With `due_us`, only a row still soft-deleted and due is removed (the reaper); without it
        any row goes (Expunge). A concurrent Undelete or Expunge therefore makes this a no-op.
        """
        sql = "DELETE FROM books WHERE id = ?"
        args: tuple[str | int, ...] = (book_id,)
        if due_us is not None:
            sql += " AND purge_us IS NOT NULL AND purge_us <= ?"
            args = (book_id, due_us)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            removed = self.conn.execute(sql, args).rowcount
            if removed == 1:
                self.conn.execute(
                    "INSERT INTO tombstones (id, purged_us) VALUES (?, ?)",
                    (book_id, to_us(self.clock())),
                )
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")
        return removed == 1

    def expunge(self, book_id: str) -> None:
        self._require(book_id)
        if not self._remove(book_id):
            raise ApiError(404, "NOT_FOUND", f"books/{book_id} not found")

    def purge_expired(self) -> list[str]:
        """Reaper: remove rows whose purge_time has been reached, never earlier."""
        now = to_us(self.clock())
        due: list[tuple[str]] = self.conn.execute(
            "SELECT id FROM books WHERE purge_us IS NOT NULL AND purge_us <= ? ORDER BY id",
            (now,),
        ).fetchall()
        return [rid for (rid,) in due if self._remove(rid, due_us=now)]


def build_app(store: SoftDeleteStore, grants: dict[str, frozenset[str]]) -> FastAPI:
    """Build the AIP-164 Book API; `grants` maps a bearer principal to its permissions."""
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def render_error(_request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, ApiError):
            raise exc
        body = {"error": {"code": exc.code, "message": exc.message, "status": exc.status}}
        return JSONResponse(body, status_code=exc.code)

    def authorize(authorization: str | None, permission: str, book_id: str | None) -> None:
        # Syntax first, then permission, then existence (AIP-164 Errors, AIP-193).
        if book_id is not None and not RESOURCE_ID.fullmatch(book_id):
            raise ApiError(400, "INVALID_ARGUMENT", "book id must follow AIP-122 (RFC 1034)")
        principal = (authorization or "").removeprefix("Bearer ")
        if permission not in grants.get(principal, frozenset()):
            raise ApiError(403, "PERMISSION_DENIED", f"missing {permission}")

    @app.post("/v1/books", response_model_exclude_none=True)
    async def create_book(
        book_id: Annotated[str, Query()], body: BookIn, authorization: Auth = None
    ) -> Book:
        authorize(authorization, "books.create", book_id)
        return store.create(book_id, body)

    @app.get("/v1/books", response_model_exclude_none=True)
    async def list_books(
        show_deleted: bool = False, authorization: Auth = None
    ) -> ListBooksResponse:
        authorize(authorization, "books.list", None)
        return ListBooksResponse(books=store.list_books(show_deleted=show_deleted))

    @app.get("/v1/books/{book_id}", response_model_exclude_none=True)
    async def get_book(book_id: str, authorization: Auth = None) -> Book:
        authorize(authorization, "books.get", book_id)
        return store.get(book_id)

    @app.delete("/v1/books/{book_id}", response_model=None)
    async def delete_book(
        book_id: str, allow_missing: bool = False, authorization: Auth = None
    ) -> Response:
        authorize(authorization, "books.delete", book_id)
        book = store.delete(book_id, allow_missing=allow_missing)
        if book is None:
            return Response(status_code=204)
        return Response(book.model_dump_json(exclude_none=True), media_type="application/json")

    @app.post("/v1/books/{book_id}:undelete", response_model_exclude_none=True)
    async def undelete_book(book_id: str, authorization: Auth = None) -> Book:
        authorize(authorization, "books.undelete", book_id)
        return store.undelete(book_id)

    @app.post("/v1/books/{book_id}:expunge")
    async def expunge_book(book_id: str, authorization: Auth = None) -> dict[str, str]:
        authorize(authorization, "books.expunge", book_id)
        store.expunge(book_id)
        return {}

    return app
