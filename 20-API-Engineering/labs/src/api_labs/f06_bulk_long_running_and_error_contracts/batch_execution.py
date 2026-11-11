"""Atomic all-or-nothing vs partial-success batch create over SQLite transactions and savepoints."""

import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_BATCH_ITEMS = 100
PROBLEM_JSON = "application/problem+json"
PROBLEM_BASE = "https://api.example.test/problems/"

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE shelves (
    shelf TEXT PRIMARY KEY,
    capacity INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    CHECK (used <= capacity)
);
CREATE TABLE books (
    id INTEGER PRIMARY KEY,
    isbn TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    shelf TEXT NOT NULL REFERENCES shelves (shelf),
    copies INTEGER NOT NULL
);
"""

INSERT_BOOK = "INSERT INTO books (isbn, title, shelf, copies) VALUES (?, ?, ?, ?)"
RESERVE_SHELF = "UPDATE shelves SET used = used + ? WHERE shelf = ?"


class BookCreate(BaseModel):
    """One item of a batch: the body a singular Create method would accept."""

    model_config = ConfigDict(extra="forbid", strict=True)

    isbn: str = Field(pattern=r"^[0-9]{13}$")
    title: str = Field(min_length=1, max_length=200)
    shelf: str = Field(min_length=1, max_length=32)
    copies: int = Field(ge=1, le=1000)


class BatchCreateRequest(BaseModel):
    """Envelope: items stay untyped so one bad item cannot reject the whole envelope."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["atomic", "partial"] = "atomic"
    requests: list[object] = Field(min_length=1, max_length=MAX_BATCH_ITEMS)


class TypedBatchCreateRequest(BaseModel):
    """Anti-pattern envelope: FastAPI validates every item before the endpoint runs."""

    requests: list[BookCreate] = Field(min_length=1, max_length=MAX_BATCH_ITEMS)


@dataclass(frozen=True, slots=True)
class Problem:
    """RFC 9457 problem details for one item (or for the whole atomic batch)."""

    status: int
    slug: str
    title: str
    detail: str
    errors: tuple[dict[str, str], ...] = ()

    def to_json(self, **extensions: object) -> dict[str, object]:
        body: dict[str, object] = {
            "type": PROBLEM_BASE + self.slug,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
        }
        if self.errors:
            body["errors"] = list(self.errors)
        body.update(extensions)
        return body


@dataclass(frozen=True, slots=True)
class ItemResult:
    """Outcome of one batch item, reported at the index the client sent it."""

    index: int
    status: int
    book: BookCreate | None = None
    problem: Problem | None = None

    def to_json(self) -> dict[str, object]:
        body: dict[str, object] = {"index": self.index, "status": self.status}
        if self.book is not None:
            body["book"] = self.book.model_dump()
        if self.problem is not None:
            body["problem"] = self.problem.to_json()
        return body


class AtomicBatchRejectedError(Exception):
    """An atomic batch failed; nothing it attempted is visible in the database."""

    def __init__(self, problem: Problem, failed_indexes: tuple[int, ...]) -> None:
        super().__init__(problem.detail)
        self.problem = problem
        self.failed_indexes = failed_indexes


def validation_problem(index: int, exc: ValidationError) -> Problem:
    """Map a pydantic ValidationError for item `index` to RFC 6901 pointers into the body."""
    errors = tuple(
        {
            "pointer": "#/requests/" + "/".join(str(part) for part in (index, *err["loc"])),
            "detail": err["msg"],
        }
        for err in exc.errors()
    )
    return Problem(
        422, "invalid-item", "Item is not valid.", f"requests[{index}] is invalid", errors
    )


def constraint_problem(item: BookCreate, exc: sqlite3.IntegrityError) -> Problem | None:
    """Map constraints this API owns to item problems; any other failure is not an item error."""
    match exc.sqlite_errorname:
        case "SQLITE_CONSTRAINT_UNIQUE":
            return Problem(409, "already-exists", "Book already exists.", f"isbn {item.isbn}")
        case "SQLITE_CONSTRAINT_FOREIGNKEY":
            return Problem(422, "unknown-shelf", "Shelf does not exist.", f"shelf {item.shelf}")
        case "SQLITE_CONSTRAINT_CHECK":
            return Problem(409, "shelf-full", "Shelf capacity exceeded.", f"shelf {item.shelf}")
        case _:
            return None


def open_catalog(shelves: dict[str, int]) -> sqlite3.Connection:
    """Open an in-memory catalog in SQLite autocommit mode so every transaction is explicit SQL."""
    conn = sqlite3.connect(":memory:", autocommit=True, check_same_thread=False)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO shelves (shelf, capacity) VALUES (?, ?)", shelves.items())
    return conn


@dataclass
class BatchExecutor:
    """Runs a batch as one transaction; partial mode isolates each item in a SAVEPOINT."""

    conn: sqlite3.Connection
    statements: list[str] = field(default_factory=list[str])
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.conn.set_trace_callback(self.statements.append)

    def _apply(self, item: BookCreate) -> None:
        # Two statements per item: a failure in the second must also undo the first.
        self.conn.execute(INSERT_BOOK, (item.isbn, item.title, item.shelf, item.copies))
        self.conn.execute(RESERVE_SHELF, (item.copies, item.shelf))

    def create_atomic(self, raw_items: list[object]) -> list[BookCreate]:
        items: list[BookCreate] = []
        invalid: list[tuple[int, Problem]] = []
        for index, raw in enumerate(raw_items):
            try:
                items.append(BookCreate.model_validate(raw))
            except ValidationError as exc:
                invalid.append((index, validation_problem(index, exc)))
        if invalid:
            # Report every invalid item at once, before any SQL runs.
            errors = tuple(err for _, problem in invalid for err in problem.errors)
            detail = f"{len(invalid)} of {len(raw_items)} items are invalid"
            problem = Problem(422, "invalid-batch", "Batch is not valid.", detail, errors)
            raise AtomicBatchRejectedError(problem, tuple(index for index, _ in invalid))
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for index, item in enumerate(items):
                    try:
                        self._apply(item)
                    except sqlite3.IntegrityError as exc:
                        conflict = constraint_problem(item, exc)
                        if conflict is None:
                            raise
                        raise AtomicBatchRejectedError(conflict, (index,)) from exc
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")
        return items

    def create_partial(
        self, raw_items: list[object], *, per_item_savepoint: bool = True
    ) -> list[ItemResult]:
        results: list[ItemResult] = []
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for index, raw in enumerate(raw_items):
                    results.append(self._partial_item(index, raw, per_item_savepoint))
            except BaseException:
                # Not an item error (I/O, auth, unknown constraint): the batch as a whole fails.
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")
        return results

    def _partial_item(self, index: int, raw: object, per_item_savepoint: bool) -> ItemResult:
        try:
            item = BookCreate.model_validate(raw)
        except ValidationError as exc:
            problem = validation_problem(index, exc)
            return ItemResult(index, problem.status, problem=problem)
        savepoint = f"item_{index}"
        if per_item_savepoint:
            self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            self._apply(item)
        except sqlite3.IntegrityError as exc:
            conflict = constraint_problem(item, exc)
            if conflict is None:
                raise
            if per_item_savepoint:
                self.conn.execute(f"ROLLBACK TO {savepoint}")
                self.conn.execute(f"RELEASE {savepoint}")
            return ItemResult(index, conflict.status, problem=conflict)
        if per_item_savepoint:
            self.conn.execute(f"RELEASE {savepoint}")
        return ItemResult(index, 201, book=item)


def build_app(executor: BatchExecutor) -> FastAPI:
    """POST /v1/books:batchCreate in atomic (200 or one problem) or partial (207) mode."""
    app = FastAPI()

    @app.post("/v1/books:batchCreate")
    def batch_create(body: BatchCreateRequest) -> JSONResponse:
        if body.mode == "partial":
            results = executor.create_partial(body.requests)
            failed = sum(1 for result in results if result.problem is not None)
            content: dict[str, object] = {
                "results": [result.to_json() for result in results],
                "summary": {"succeeded": len(results) - failed, "failed": failed},
            }
            return JSONResponse(content, status_code=207)
        try:
            books = executor.create_atomic(body.requests)
        except AtomicBatchRejectedError as exc:
            content = exc.problem.to_json(failed_indexes=list(exc.failed_indexes))
            return JSONResponse(content, status_code=exc.problem.status, media_type=PROBLEM_JSON)
        return JSONResponse({"books": [book.model_dump() for book in books]}, status_code=200)

    @app.post("/v1/books:batchCreateTyped")
    def batch_create_typed(body: TypedBatchCreateRequest) -> JSONResponse:
        raw: list[object] = [book.model_dump() for book in body.requests]
        results = executor.create_partial(raw)
        return JSONResponse({"results": [result.to_json() for result in results]}, 207)

    return app
