"""Partial responses (AIP-157): a read mask that is validated, then pushed down to SQL.

The request names fields with ``fields=title,author.display_name`` or a view enum.
The mask is parsed into a tree, checked against a per-caller schema, turned into
SQLAlchemy ``load_only(..., raiseload=True)`` options so unrequested columns are absent
from the ``SELECT``, and finally used to serialize exactly the selected fields.
SQLite helpers at the bottom prove the I/O claim: an unprojected large column lives in
overflow pages that the query never reads.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import JSON, ForeignKey, Text, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    QueryableAttribute,
    Session,
    joinedload,
    load_only,
    mapped_column,
    relationship,
)
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine
    from sqlalchemy.orm.interfaces import ORMOption
    from starlette.requests import Request

# ---------------------------------------------------------------- storage model


class Base(DeclarativeBase):
    pass


class Author(Base):
    __tablename__ = "authors"
    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str]
    bio: Mapped[str] = mapped_column(Text)


class Book(Base):
    __tablename__ = "books"
    # Small columns first, the large one last: SQLite stores a row as one record, and a
    # column placed after a value that spilled to overflow pages forces a walk of that chain.
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    rating: Mapped[int]
    labels: Mapped[list[str]] = mapped_column(JSON)
    author_id: Mapped[int] = mapped_column(ForeignKey("authors.id"))
    internal_notes: Mapped[str]
    body: Mapped[str] = mapped_column(Text)
    # lazy="raise": touching an unrequested relationship is a bug, never a hidden query.
    author: Mapped[Author] = relationship(lazy="raise")


# ---------------------------------------------------------------- public schema


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One field of the public resource; ``children`` set means a message field."""

    attr: QueryableAttribute[Any]
    children: Mapping[str, FieldSpec] | None = None
    repeated: bool = False
    roles: frozenset[str] = frozenset()  # empty: visible to every caller


AUTHOR_FIELDS: Final[Mapping[str, FieldSpec]] = {
    "id": FieldSpec(Author.id),
    "display_name": FieldSpec(Author.display_name),
    "bio": FieldSpec(Author.bio),
}
BOOK_FIELDS: Final[Mapping[str, FieldSpec]] = {
    "id": FieldSpec(Book.id),
    "title": FieldSpec(Book.title),
    "rating": FieldSpec(Book.rating),
    "labels": FieldSpec(Book.labels, repeated=True),
    "author": FieldSpec(Book.author, children=AUTHOR_FIELDS),
    "internal_notes": FieldSpec(Book.internal_notes, roles=frozenset({"admin"})),
    "body": FieldSpec(Book.body),
}
ALWAYS_RETURNED: Final = "id"  # identity, like JSON:API's id; not subject to the mask

# AIP-157 view enum: UNSPECIFIED is valid and means the method's documented default.
VIEW_MASKS: Final[Mapping[str, str]] = {
    "BOOK_VIEW_BASIC": "id,title,rating,labels,author.display_name",
    "BOOK_VIEW_FULL": "*",
}
VIEW_UNSPECIFIED: Final = "BOOK_VIEW_UNSPECIFIED"

LIST_LIMIT: Final = 50  # no pagination here; see the keyset notes in this folder
MAX_MASK_LENGTH: Final = 512
MAX_MASK_PATHS: Final = 32
SEGMENT: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")

# A selection is the resolved mask: every key is a field to return, None marks a
# scalar or repeated leaf, and a mapping lists the selected children of a message.
type Selection = Mapping[str, Selection | None]
type _Tree = dict[str, _Tree]  # protobuf FieldMaskTree shape: empty dict is a leaf


class MaskError(ValueError):
    """The mask is malformed or names a field this caller cannot see (HTTP 400)."""


# ---------------------------------------------------------------- parsing


def _visible(spec: FieldSpec, roles: frozenset[str]) -> bool:
    return not spec.roles or bool(spec.roles & roles)


def _add_path(
    tree: _Tree, path: str, schema: Mapping[str, FieldSpec], roles: frozenset[str]
) -> None:
    segments = path.split(".")
    if not all(SEGMENT.fullmatch(s) for s in segments):
        raise MaskError(f"invalid field path {path!r}")
    level: Mapping[str, FieldSpec] | None = schema
    for i, name in enumerate(segments):
        spec = level.get(name) if level is not None else None
        if spec is None or not _visible(spec, roles):
            # Hidden fields get the same answer as missing ones, so the error leaks nothing.
            raise MaskError(f"unknown field path {path!r}")
        if i < len(segments) - 1 and spec.children is None:
            what = "repeated" if spec.repeated else "not a message"
            raise MaskError(f"field {name!r} in {path!r} is {what}; it must be the last segment")
        level = spec.children
    # Same algorithm as protobuf's _FieldMaskTree.AddPath: a covered path adds nothing,
    # and a path that covers existing sub-paths replaces them.
    node = tree
    for name in segments:
        if name not in node:
            node[name] = {}
        elif not node[name]:
            return
        node = node[name]
    node.clear()


def _expand(schema: Mapping[str, FieldSpec], roles: frozenset[str]) -> Selection:
    return {
        name: None if spec.children is None else _expand(spec.children, roles)
        for name, spec in schema.items()
        if _visible(spec, roles)
    }


def _resolve(tree: _Tree, schema: Mapping[str, FieldSpec], roles: frozenset[str]) -> Selection:
    out: dict[str, Selection | None] = {}
    for name, spec in schema.items():  # schema order, so output order is deterministic
        if name not in tree:
            continue
        sub = tree[name]
        if spec.children is None:
            out[name] = None
        elif not sub:
            out[name] = _expand(spec.children, roles)
        else:
            out[name] = _resolve(sub, spec.children, roles)
    return out


def parse_tree(raw: str, roles: frozenset[str] = frozenset()) -> _Tree:
    """Validate a comma-separated mask and return its canonical tree."""
    if len(raw) > MAX_MASK_LENGTH:
        raise MaskError(f"mask is longer than {MAX_MASK_LENGTH} characters")
    paths = raw.split(",")
    if len(paths) > MAX_MASK_PATHS:
        raise MaskError(f"mask has more than {MAX_MASK_PATHS} paths")
    tree: _Tree = {}
    for path in paths:
        _add_path(tree, path, BOOK_FIELDS, roles)
    return tree


def canonical_paths(tree: _Tree, prefix: str = "") -> list[str]:
    """Sorted leaf paths, the form protobuf's CanonicalFormFromMask produces."""
    out: list[str] = []
    for name, sub in tree.items():
        path = f"{prefix}{name}"
        out.extend(canonical_paths(sub, f"{path}.") if sub else [path])
    return sorted(out)


def parse_mask(raw: str, roles: frozenset[str] = frozenset()) -> Selection:
    """``*`` selects every field the caller may see; ``id`` is always returned."""
    if raw == "*":
        return _expand(BOOK_FIELDS, roles)
    tree = parse_tree(raw, roles)
    tree.setdefault(ALWAYS_RETURNED, {})
    return _resolve(tree, BOOK_FIELDS, roles)


def selection_for(
    fields: str | None, view: str | None, *, default_view: str, roles: frozenset[str]
) -> Selection:
    """Map the request's ``fields`` and ``view`` parameters to one selection."""
    if fields is not None and view is not None:
        raise MaskError("specify at most one of fields and view")
    if view is not None and view != VIEW_UNSPECIFIED:
        if view not in VIEW_MASKS:
            raise MaskError(f"unknown view {view!r}")
        return parse_mask(VIEW_MASKS[view], roles)
    # AIP-157: an omitted or empty mask means the documented default view.
    if not fields:
        return parse_mask(VIEW_MASKS[default_view], roles)
    return parse_mask(fields, roles)


# ---------------------------------------------------------------- push-down and output


def _columns(sel: Selection, schema: Mapping[str, FieldSpec]) -> list[QueryableAttribute[Any]]:
    return [schema[name].attr for name in sel if schema[name].children is None]


def loader_options(sel: Selection) -> list[ORMOption]:
    """``load_only`` per entity; ``raiseload`` turns any unrequested access into an error.

    Supports message fields one level deep, which is all ``BOOK_FIELDS`` declares.
    """
    options: list[ORMOption] = [load_only(*_columns(sel, BOOK_FIELDS), raiseload=True)]
    for name, sub in sel.items():
        spec = BOOK_FIELDS[name]
        if spec.children is not None and sub is not None:
            child_columns = _columns(sub, spec.children)
            options.append(joinedload(spec.attr).load_only(*child_columns, raiseload=True))
    return options


def fetch_books(session: Session, sel: Selection, *, book_id: int | None = None) -> Sequence[Book]:
    stmt = select(Book).options(*loader_options(sel)).order_by(Book.id).limit(LIST_LIMIT)
    if book_id is not None:
        stmt = stmt.where(Book.id == book_id)
    return session.scalars(stmt).all()


def serialize(
    obj: object, sel: Selection, schema: Mapping[str, FieldSpec] = BOOK_FIELDS
) -> dict[str, Any]:
    """Read only the selected attributes; anything else would raise via raiseload."""
    out: dict[str, Any] = {}
    for name, sub in sel.items():
        spec = schema[name]
        value: object = getattr(obj, spec.attr.key)
        if sub is not None and spec.children is not None:
            out[name] = None if value is None else serialize(value, sub, spec.children)
        else:
            out[name] = value
    return out


# ---------------------------------------------------------------- HTTP surface


def _error(code: int, status: str, message: str) -> JSONResponse:
    # AIP-193 HTTP/JSON shape: {"error": {"code", "message", "status"}}.
    return JSONResponse({"error": {"code": code, "message": message, "status": status}}, code)


def create_app(engine: Engine, *, roles: frozenset[str] = frozenset()) -> Starlette:
    """``roles`` stands in for the authenticated principal of every request."""

    def _selection(request: Request, default_view: str) -> Selection:
        q = request.query_params
        return selection_for(q.get("fields"), q.get("view"), default_view=default_view, roles=roles)

    def list_books(request: Request) -> JSONResponse:
        try:
            sel = _selection(request, "BOOK_VIEW_BASIC")
        except MaskError as exc:
            return _error(400, "INVALID_ARGUMENT", str(exc))
        with Session(engine) as session:
            books = [serialize(b, sel) for b in fetch_books(session, sel)]
        return JSONResponse({"books": books})

    def get_book(request: Request) -> JSONResponse:
        try:
            sel = _selection(request, "BOOK_VIEW_FULL")
        except MaskError as exc:
            return _error(400, "INVALID_ARGUMENT", str(exc))
        book_id = int(request.path_params["book_id"])
        with Session(engine) as session:
            found = fetch_books(session, sel, book_id=book_id)
            if not found:
                return _error(404, "NOT_FOUND", f"book {book_id} not found")
            return JSONResponse(serialize(found[0], sel))

    # Sync endpoints: Starlette runs them in its threadpool, so blocking SQLite I/O is fine.
    return Starlette(
        routes=[
            Route("/books", list_books, methods=["GET"]),
            Route("/books/{book_id:int}", get_book, methods=["GET"]),
        ]
    )


def create_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        # Fixed before the first table exists, so overflow math is the same on every build.
        conn.exec_driver_sql("PRAGMA page_size = 4096")
        conn.exec_driver_sql("PRAGMA auto_vacuum = NONE")
    Base.metadata.create_all(engine)


# ---------------------------------------------------------------- SQLite storage proof


def overflow_pages(db_path: Path, table: str) -> list[int]:
    """Page numbers of every overflow page holding ``table`` payload (dbstat vtab)."""
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT pageno FROM dbstat WHERE name = ? AND pagetype = 'overflow' ORDER BY pageno",
            (table,),
        ).fetchall()
    conn.close()
    return [int(r[0]) for r in rows]


def break_overflow_chains(db_path: Path, pages: Sequence[int], page_size: int = 4096) -> None:
    """Zero each overflow page's 4-byte next-page pointer, ending every chain early.

    Any statement that reads past the first overflow page of a value now fails with
    ``database disk image is malformed``; statements that never touch the chain still work.
    """
    with db_path.open("r+b") as f:
        for page in pages:
            f.seek((page - 1) * page_size)
            f.write(b"\x00\x00\x00\x00")


def table_leaf_local_bytes(payload: int, usable: int = 4096) -> int:
    """Bytes of a table b-tree leaf cell kept on the leaf page (fileformat2 Section 1.6)."""
    x = usable - 35
    if payload <= x:
        return payload
    m = ((usable - 12) * 32 // 255) - 23
    k = m + ((payload - m) % (usable - 4))
    return k if k <= x else m


def predicted_overflow_pages(payload: int, usable: int = 4096) -> int:
    spill = payload - table_leaf_local_bytes(payload, usable)
    return -(-spill // (usable - 4))  # each overflow page carries usable - 4 bytes


def _varint_len(value: int) -> int:
    n = 1
    while value >= 1 << (7 * n) and n < 9:
        n += 1
    return n


def single_blob_record_size(blob_len: int) -> int:
    """Payload of a one-column record holding a BLOB (fileformat2 Section 2.1)."""
    serial_type = blob_len * 2 + 12
    header = 1 + _varint_len(serial_type)  # header-size varint fits in 1 byte here
    return header + blob_len
