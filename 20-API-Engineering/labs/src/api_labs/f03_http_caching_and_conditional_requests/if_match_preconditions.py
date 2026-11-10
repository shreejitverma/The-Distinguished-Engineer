"""Lost-update prevention with If-Match, 412 Precondition Failed, and 428 Precondition Required."""

import json
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Final, Literal

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

PROBLEM_JSON: Final = "application/problem+json"
MAX_IF_MATCH_TAGS: Final = 16
# Canonical decimal only, so "v01" never equals "v1"; 18 digits stay below 2**63 and far
# below the int() digit limit that guards against CVE-2020-10735.
_VERSION_TAG: Final = re.compile(r"v([1-9][0-9]{0,17})")
# One #entity-tag list element (RFC 9110 Sections 5.6.1.2 and 8.8.3): optional OWS, an
# optional entity-tag (empty elements are legal), optional OWS, then a comma or the end.
_LIST_ELEMENT: Final = re.compile(
    r'[ \t]*(?:(?P<weak>W/)?"(?P<opaque>[\x21\x23-\x7e\x80-\xff]*)")?[ \t]*(?:,|\Z)'
)


@dataclass(frozen=True, slots=True)
class EntityTag:
    """An entity-tag: an opaque-tag plus the case-sensitive "W/" weakness indicator."""

    opaque: str
    weak: bool = False

    def __str__(self) -> str:
        return f'{"W/" if self.weak else ""}"{self.opaque}"'


class Wildcard(Enum):
    """The `If-Match: *` form: true when any current representation exists."""

    ANY = "*"


type IfMatch = Wildcard | tuple[EntityTag, ...]


class MalformedPreconditionError(ValueError):
    """The If-Match field value does not match `"*" / #entity-tag`."""


def parse_if_match(field_lines: list[str]) -> IfMatch:
    """Parse every If-Match field line of one request (RFC 9110 Section 13.1.1).

    Field lines are joined with ", " first (RFC 9110 Section 5.3), empty list elements are
    skipped (RFC 9110 Section 5.6.1.2), and the value is tokenized rather than split on ","
    because an opaque-tag may itself contain a comma.
    """
    value = ", ".join(field_lines)
    if value.strip(" \t") == "*":
        return Wildcard.ANY
    tags: list[EntityTag] = []
    pos = 0
    while pos < len(value):
        element = _LIST_ELEMENT.match(value, pos)
        if element is None:
            raise MalformedPreconditionError(f"invalid If-Match at offset {pos}: {value!r}")
        if element["opaque"] is not None:
            tags.append(EntityTag(element["opaque"], weak=element["weak"] is not None))
        pos = element.end()
    if len(tags) > MAX_IF_MATCH_TAGS:
        raise MalformedPreconditionError(f"If-Match lists {len(tags)} > {MAX_IF_MATCH_TAGS} tags")
    return tuple(tags)


def version_of(tag: EntityTag) -> int | None:
    """Map a tag to the row version it names, or None if it can never match.

    This server only emits strong tags of the form "v<N>", so the strong comparison of
    RFC 9110 Section 8.8.3.2 reduces to: a weak tag never matches, and the opaque-tag must
    equal the canonical "v<N>" spelling octet for octet.
    """
    if tag.weak:
        return None
    match = _VERSION_TAG.fullmatch(tag.opaque)
    return int(match[1]) if match else None


def expected_versions(condition: IfMatch) -> frozenset[int] | None:
    """Versions that satisfy the condition; None means any current version (`*`)."""
    if condition is Wildcard.ANY:
        return None
    return frozenset(v for v in map(version_of, condition) if v is not None)


def etag_for(version: int) -> EntityTag:
    return EntityTag(f"v{version}")


@dataclass(frozen=True, slots=True)
class Document:
    doc_id: int
    version: int
    content: bytes


class WriteFailure(Enum):
    STALE = "stale"
    MISSING = "missing"


type Pause = Callable[[], Awaitable[None]]
type Strategy = Literal["conditional_update", "check_then_write"]


async def _no_pause() -> None:
    return None


class DocumentStore:
    """SQLite rows with a version column; the ETag is derived from the version.

    `pause` runs between deciding to write and writing; tests pass a barrier to force two
    requests to interleave exactly where a real database round trip would let them.
    """

    def __init__(self, pause: Pause = _no_pause) -> None:
        self._db = sqlite3.connect(":memory:", isolation_level=None)
        self._db.execute(
            "CREATE TABLE documents ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "version INTEGER NOT NULL, "
            "content BLOB NOT NULL)"
        )
        self._pause = pause

    def close(self) -> None:
        self._db.close()

    def create(self, content: bytes) -> Document:
        row = self._db.execute(
            "INSERT INTO documents (version, content) VALUES (1, ?) RETURNING id", (content,)
        ).fetchone()
        return Document(doc_id=int(row[0]), version=1, content=content)

    def get(self, doc_id: int) -> Document | None:
        row = self._db.execute(
            "SELECT version, content FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            return None
        return Document(doc_id=doc_id, version=int(row[0]), content=bytes(row[1]))

    async def conditional_update(
        self, doc_id: int, versions: frozenset[int] | None, content: bytes
    ) -> int | WriteFailure:
        """Compare and write in one statement: the version predicate is the lock."""
        await self._pause()
        if versions is None:
            row = self._db.execute(
                "UPDATE documents SET content = ?, version = version + 1 "
                "WHERE id = ? RETURNING version",
                (content, doc_id),
            ).fetchone()
        else:
            row = self._db.execute(
                "UPDATE documents SET content = ?, version = version + 1 "
                "WHERE id = ? AND version IN (SELECT value FROM json_each(?)) "
                "RETURNING version",
                (content, doc_id, json.dumps(sorted(versions))),
            ).fetchone()
        if row is not None:
            return int(row[0])
        # Zero rows updated is ambiguous: tell "gone" apart from "changed".
        return WriteFailure.MISSING if self.get(doc_id) is None else WriteFailure.STALE

    async def check_then_write(
        self, doc_id: int, versions: frozenset[int] | None, content: bytes
    ) -> int | WriteFailure:
        """Anti-pattern: read, compare in Python, yield, then write unconditionally."""
        current = self.get(doc_id)
        if current is None:
            return WriteFailure.MISSING
        if versions is not None and current.version not in versions:
            return WriteFailure.STALE
        await self._pause()
        row = self._db.execute(
            "UPDATE documents SET content = ?, version = version + 1 "
            "WHERE id = ? RETURNING version",
            (content, doc_id),
        ).fetchone()
        return int(row[0])


class DocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(max_length=10_000)


class ProblemError(Exception):
    """Rendered as an RFC 9457 problem details object."""

    def __init__(self, status: int, title: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail


def build_app(
    store: DocumentStore,
    *,
    require_if_match: bool,
    strategy: Strategy = "conditional_update",
) -> FastAPI:
    """GET returns the stored bytes with a strong ETag; PUT replaces them conditionally."""
    app = FastAPI()
    write = store.conditional_update if strategy == "conditional_update" else store.check_then_write

    @app.exception_handler(ProblemError)
    async def render_problem(_request: Request, exc: ProblemError) -> JSONResponse:
        problem = {"type": "about:blank", "title": exc.title, "status": exc.status}
        return JSONResponse(
            {**problem, "detail": exc.detail}, status_code=exc.status, media_type=PROBLEM_JSON
        )

    async def precondition(request: Request, doc_id: int) -> frozenset[int] | None:
        """Evaluate If-Match as a dependency, which FastAPI runs before body validation.

        `async def` keeps it on the event loop thread; a plain `def` dependency runs in a
        worker thread, where this single-threaded SQLite connection refuses to be used.
        """
        current = store.get(doc_id)
        if current is None:
            # RFC 9110 Section 13.2.1: a 404 detectable up front outranks any precondition.
            raise ProblemError(404, "Not Found", f"document {doc_id} does not exist")
        field_lines = request.headers.getlist("if-match")
        if not field_lines:
            if require_if_match:
                raise ProblemError(
                    428,
                    "Precondition Required",
                    "This resource only accepts conditional writes: GET it, then resend the "
                    "PUT with If-Match set to the ETag you received.",
                )
            return None
        try:
            condition = parse_if_match(field_lines)
        except MalformedPreconditionError as exc:
            raise ProblemError(400, "Bad Request", str(exc)) from exc
        versions = expected_versions(condition)
        if versions is not None and current.version not in versions:
            raise ProblemError(412, "Precondition Failed", "GET the document, reapply, retry.")
        return versions

    @app.get("/documents/{doc_id}")
    async def read_document(doc_id: int) -> Response:
        current = store.get(doc_id)
        if current is None:
            raise ProblemError(404, "Not Found", f"document {doc_id} does not exist")
        return Response(
            current.content,
            media_type="application/json",
            headers={"ETag": str(etag_for(current.version))},
        )

    @app.put("/documents/{doc_id}", status_code=204)
    async def replace_document(
        doc_id: int,
        request: Request,
        document: DocumentIn,
        versions: Annotated[frozenset[int] | None, Depends(precondition)],
    ) -> Response:
        # Store the validated request bytes verbatim, so the new representation is the PUT
        # content and RFC 9110 Section 9.3.4 allows an ETag on the success response.
        outcome = await write(doc_id, versions, await request.body())
        if outcome is WriteFailure.MISSING:
            raise ProblemError(404, "Not Found", f"document {doc_id} does not exist")
        if outcome is WriteFailure.STALE:
            raise ProblemError(412, "Precondition Failed", "GET the document, reapply, retry.")
        return Response(status_code=204, headers={"ETag": str(etag_for(outcome))})

    return app
