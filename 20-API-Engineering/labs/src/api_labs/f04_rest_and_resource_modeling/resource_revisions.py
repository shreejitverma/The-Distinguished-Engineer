"""AIP-162 resource revisions: immutable snapshots, aliases, keyset listing, and rollback.

Revision creation strategy (AIP-162 requires an API to document it): a new revision is
created when a book is created, on every update that changes the snapshot, and on every
rollback.  A rollback never reuses the target revision ID; it appends a new revision whose
snapshot equals the target snapshot, so each revision ID covers one contiguous time range.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request

# AIP-122 user-specified resource ID: RFC 1034 label, lower-case, at most 63 characters.
RESOURCE_ID_RE = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")
REVISION_ID_RE = re.compile(r"^[0-9a-f]{8}$")
LATEST = "latest"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 1000
PAGE_TOKEN_TTL_SECONDS = 3600
MAX_ID_ATTEMPTS = 8


class Book(BaseModel):
    """The mutable parent resource; instances are frozen so a stored snapshot cannot change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    author: str = Field(min_length=1, max_length=200)
    tags: tuple[str, ...] = ()


class BookPatch(BaseModel):
    """Partial update body; only the fields the client sent are applied."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    author: str | None = Field(default=None, min_length=1, max_length=200)
    tags: tuple[str, ...] | None = None


class AliasRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias_id: str


@dataclass(frozen=True, slots=True)
class Revision:
    revision_id: str
    seq: int
    snapshot: Book
    create_time: datetime


@dataclass(slots=True)
class BookHistory:
    current: Book
    revisions: list[Revision]  # append-only, ascending seq; the last element is `latest`
    aliases: dict[str, str] = field(default_factory=dict[str, str])  # alias_id -> revision_id


class ApiError(Exception):
    """An error rendered as the AIP-193 HTTP/1.1+JSON shape."""

    def __init__(self, http_status: int, status: str, message: str) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.status = status
        self.message = message


def _invalid(message: str) -> ApiError:
    return ApiError(400, "INVALID_ARGUMENT", message)


def _not_found(message: str) -> ApiError:
    return ApiError(404, "NOT_FOUND", message)


def default_revision_id() -> str:
    """Last eight hex characters of a UUIDv4 (the pre-2023 AIP-162 rule of thumb)."""
    return uuid.uuid4().hex[-8:]


def default_clock() -> datetime:
    return datetime.now(UTC)


def etag_for(revision_id: str) -> str:
    return f'"{revision_id}"'


def if_match_passes(header: str | None, current_etag: str) -> bool:
    """RFC 9110 Section 13.1.1: `*` or a strong match against any listed entity tag."""
    if header is None:
        return True
    if header.strip() == "*":
        return True
    for candidate in header.split(","):
        tag = candidate.strip()
        if not tag.startswith("W/") and tag == current_etag:
            return True
    return False


class RevisionStore:
    """In-memory store; one asyncio.Lock makes every read-check-write sequence atomic."""

    def __init__(
        self,
        *,
        token_key: bytes,
        new_revision_id: Callable[[], str] = default_revision_id,
        clock: Callable[[], datetime] = default_clock,
        storage_round_trip: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._books: dict[str, BookHistory] = {}
        self._seq = 0
        self._lock = asyncio.Lock()
        self._fernet = Fernet(token_key)
        self._new_revision_id = new_revision_id
        self._clock = clock
        self._round_trip = storage_round_trip or _yield_to_loop

    # -- helpers -----------------------------------------------------------------------------

    def _history(self, book_id: str) -> BookHistory:
        if not RESOURCE_ID_RE.match(book_id):
            hint = ""
            if "@" in book_id:
                hint = "; address a revision as books/{book}/revisions/{revision} (AIP-162)"
            raise _invalid(f"invalid book ID {book_id!r}{hint}")
        history = self._books.get(book_id)
        if history is None:
            raise _not_found(f"books/{book_id} not found")
        return history

    def _resolve(self, book_id: str, history: BookHistory, ref: str) -> Revision:
        """Resolve a revision ID or alias to a concrete revision of this book only."""
        if ref == LATEST:
            return history.revisions[-1]
        revision_id = history.aliases.get(ref, ref)
        if not REVISION_ID_RE.match(revision_id) and not RESOURCE_ID_RE.match(ref):
            raise _invalid(f"invalid revision ID {ref!r}")
        for revision in history.revisions:
            if revision.revision_id == revision_id:
                return revision
        raise _not_found(f"books/{book_id}/revisions/{ref} not found")

    def _fresh_revision_id(self, history: BookHistory) -> str:
        taken = {r.revision_id for r in history.revisions} | set(history.aliases) | {LATEST}
        for _ in range(MAX_ID_ATTEMPTS):
            candidate = self._new_revision_id()
            if candidate not in taken:
                return candidate
        raise ApiError(500, "INTERNAL", "could not allocate a unique revision ID")

    def _append(self, history: BookHistory, snapshot: Book) -> Revision:
        self._seq += 1
        revision = Revision(
            revision_id=self._fresh_revision_id(history),
            seq=self._seq,
            snapshot=snapshot,
            create_time=self._clock(),
        )
        history.revisions.append(revision)
        history.current = snapshot
        return revision

    def alternate_ids(self, book_id: str, revision: Revision) -> list[str]:
        history = self._books[book_id]
        ids = sorted(a for a, rid in history.aliases.items() if rid == revision.revision_id)
        if history.revisions[-1].revision_id == revision.revision_id:
            ids.insert(0, LATEST)
        return ids

    def current_etag(self, book_id: str) -> str:
        return etag_for(self._history(book_id).revisions[-1].revision_id)

    def _check_precondition(self, book_id: str, if_match: str | None) -> None:
        if not if_match_passes(if_match, self.current_etag(book_id)):
            raise ApiError(412, "FAILED_PRECONDITION", f"books/{book_id} has changed")

    # -- operations --------------------------------------------------------------------------

    async def create_book(self, book_id: str, book: Book) -> Revision:
        if not RESOURCE_ID_RE.match(book_id):
            raise _invalid(f"invalid book ID {book_id!r}")
        async with self._lock:
            if book_id in self._books:
                raise ApiError(409, "ALREADY_EXISTS", f"books/{book_id} already exists")
            history = BookHistory(current=book, revisions=[])
            self._books[book_id] = history
            return self._append(history, book)

    def get_book(self, book_id: str) -> Book:
        return self._history(book_id).current

    async def update_book(self, book_id: str, patch: BookPatch, if_match: str | None) -> Book:
        async with self._lock:
            history = self._history(book_id)
            self._check_precondition(book_id, if_match)
            # Revalidate the merged state: model_copy(update=...) skips validation, and an
            # invalid snapshot would be frozen into history forever.
            merged = {**history.current.model_dump(), **patch.model_dump(exclude_unset=True)}
            try:
                updated = Book.model_validate(merged)
            except ValidationError as exc:
                raise _invalid(exc.errors(include_url=False)[0]["msg"]) from None
            await self._round_trip()
            if updated != history.current:
                self._append(history, updated)
            return history.current

    def list_revisions(
        self, book_id: str, page_size: int, page_token: str
    ) -> tuple[list[Revision], str]:
        history = self._history(book_id)
        if page_size < 0:
            raise _invalid("page_size must not be negative")
        size = DEFAULT_PAGE_SIZE if page_size == 0 else min(page_size, MAX_PAGE_SIZE)
        before_seq = self._decode_token(book_id, page_token) if page_token else None
        newest_first = sorted(history.revisions, key=lambda r: r.seq, reverse=True)
        if before_seq is not None:
            newest_first = [r for r in newest_first if r.seq < before_seq]
        page = newest_first[:size]
        more = len(newest_first) > size
        return page, self._encode_token(book_id, page[-1].seq) if more else ""

    def _encode_token(self, book_id: str, seq: int) -> str:
        return self._fernet.encrypt(f"{book_id}\n{seq}".encode()).decode()

    def _decode_token(self, book_id: str, token: str) -> int:
        try:
            raw = self._fernet.decrypt(token, ttl=PAGE_TOKEN_TTL_SECONDS).decode()
        except InvalidToken:
            raise _invalid("invalid page_token") from None
        token_book, _, seq = raw.partition("\n")
        if token_book != book_id:
            raise _invalid("page_token was issued for a different collection")
        return int(seq)

    def get_revision(self, book_id: str, ref: str) -> Revision:
        history = self._history(book_id)
        return self._resolve(book_id, history, ref)

    async def rollback(self, book_id: str, ref: str, if_match: str | None) -> Revision:
        async with self._lock:
            history = self._history(book_id)
            target = self._resolve(book_id, history, ref)
            self._check_precondition(book_id, if_match)
            await self._round_trip()
            return self._append(history, target.snapshot)

    async def alias(self, book_id: str, ref: str, alias_id: str) -> Revision:
        async with self._lock:
            history = self._history(book_id)
            target = self._resolve(book_id, history, ref)
            if alias_id == LATEST:
                raise _invalid("'latest' is a read-only server-managed alias")
            if not RESOURCE_ID_RE.match(alias_id):
                raise _invalid(f"invalid alias ID {alias_id!r}")
            if any(r.revision_id == alias_id for r in history.revisions):
                raise ApiError(409, "ALREADY_EXISTS", f"{alias_id!r} is already a revision ID")
            history.aliases[alias_id] = target.revision_id
            return target

    async def delete_revision(self, book_id: str, ref: str) -> None:
        async with self._lock:
            history = self._history(book_id)
            if ref == LATEST:
                raise _invalid("'latest' is a read-only server-managed alias")
            if ref in history.aliases:
                del history.aliases[ref]
                return
            target = self._resolve(book_id, history, ref)
            if len(history.revisions) == 1:
                raise ApiError(400, "FAILED_PRECONDITION", "cannot delete the only revision")
            history.revisions.remove(target)
            history.aliases = {
                a: rid for a, rid in history.aliases.items() if rid != target.revision_id
            }


async def _yield_to_loop() -> None:
    """Stand-in for a storage round trip: the await point where another request can run."""
    await asyncio.sleep(0)


# -- HTTP surface ------------------------------------------------------------------------------


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def book_json(book_id: str, book: Book) -> dict[str, Any]:
    return {"name": f"books/{book_id}", **book.model_dump(mode="json")}


def revision_json(store: RevisionStore, book_id: str, revision: Revision) -> dict[str, Any]:
    return {
        "name": f"books/{book_id}/revisions/{revision.revision_id}",
        "snapshot": book_json(book_id, revision.snapshot),
        "create_time": _rfc3339(revision.create_time),
        "alternate_ids": store.alternate_ids(book_id, revision),
    }


async def _json_body(request: Request) -> Any:
    try:
        return await request.json()
    except ValueError:
        raise _invalid("request body is not valid JSON") from None


def _parse[M: BaseModel](model: type[M], data: Any) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise _invalid(exc.errors(include_url=False)[0]["msg"]) from None


def _query_int(request: Request, name: str) -> int:
    raw = request.query_params.get(name, "0")
    try:
        return int(raw)
    except ValueError:
        raise _invalid(f"{name} must be an integer") from None


def build_app(store: RevisionStore) -> Starlette:
    """Routes follow AIP-162: a `revisions` subcollection plus `:rollback` and `:alias`."""

    async def create_book(request: Request) -> Response:
        book_id = request.query_params.get("book_id", "")
        book = _parse(Book, await _json_body(request))
        revision = await store.create_book(book_id, book)
        headers = {"ETag": etag_for(revision.revision_id), "Location": f"/v1/books/{book_id}"}
        return JSONResponse(book_json(book_id, book), status_code=201, headers=headers)

    async def get_book(request: Request) -> Response:
        book_id = request.path_params["book"]
        book = store.get_book(book_id)
        return JSONResponse(book_json(book_id, book), headers={"ETag": store.current_etag(book_id)})

    async def update_book(request: Request) -> Response:
        book_id = request.path_params["book"]
        patch = _parse(BookPatch, await _json_body(request))
        book = await store.update_book(book_id, patch, request.headers.get("if-match"))
        return JSONResponse(book_json(book_id, book), headers={"ETag": store.current_etag(book_id)})

    async def list_revisions(request: Request) -> Response:
        book_id = request.path_params["book"]
        page, next_token = store.list_revisions(
            book_id, _query_int(request, "page_size"), request.query_params.get("page_token", "")
        )
        body: dict[str, Any] = {"revisions": [revision_json(store, book_id, r) for r in page]}
        if next_token:
            body["next_page_token"] = next_token
        return JSONResponse(body)

    async def get_revision(request: Request) -> Response:
        book_id, ref = request.path_params["book"], request.path_params["revision"]
        revision = store.get_revision(book_id, ref)
        return JSONResponse(revision_json(store, book_id, revision))

    async def delete_revision(request: Request) -> Response:
        await store.delete_revision(request.path_params["book"], request.path_params["revision"])
        return Response(status_code=204)

    async def rollback(request: Request) -> Response:
        book_id, ref = request.path_params["book"], request.path_params["revision"]
        revision = await store.rollback(book_id, ref, request.headers.get("if-match"))
        headers = {"ETag": etag_for(revision.revision_id)}
        return JSONResponse(revision_json(store, book_id, revision), headers=headers)

    async def alias(request: Request) -> Response:
        book_id, ref = request.path_params["book"], request.path_params["revision"]
        body = _parse(AliasRequest, await _json_body(request))
        revision = await store.alias(book_id, ref, body.alias_id)
        return JSONResponse(revision_json(store, book_id, revision))

    async def on_api_error(request: Request, exc: Exception) -> Response:
        if not isinstance(exc, ApiError):  # registered only for ApiError
            raise exc
        error = {"code": exc.http_status, "status": exc.status, "message": exc.message}
        return JSONResponse({"error": error}, status_code=exc.http_status)

    revision_path = "/v1/books/{book}/revisions/{revision}"
    routes = [
        Route("/v1/books", create_book, methods=["POST"]),
        Route("/v1/books/{book}", get_book, methods=["GET"]),
        Route("/v1/books/{book}", update_book, methods=["PATCH"]),
        Route("/v1/books/{book}/revisions", list_revisions, methods=["GET"]),
        Route(revision_path, get_revision, methods=["GET"]),
        Route(revision_path, delete_revision, methods=["DELETE"]),
        Route(revision_path + ":rollback", rollback, methods=["POST"]),
        Route(revision_path + ":alias", alias, methods=["POST"]),
    ]
    return Starlette(routes=routes, exception_handlers={ApiError: on_api_error})
