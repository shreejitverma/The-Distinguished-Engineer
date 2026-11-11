"""Wire, source, and semantic compatibility probes (AIP-180) against a typed Python client.

Each probe executes a real change instead of asserting a table:
JSON wire probes decode new-server bytes with old pydantic client models,
source probes bind old call sites against a new SDK signature,
semantic probes run old client logic against a new FastAPI server,
and protobuf probes parse one schema's bytes with another schema's descriptor.
"""

from __future__ import annotations

import inspect
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import FastAPI, Query
from google.protobuf import (  # type: ignore[import-untyped]
    descriptor_pb2,
    descriptor_pool,
    message_factory,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

# --- JSON wire compatibility of a typed client ------------------------------------------


class BookFormat(StrEnum):
    """Closed enum shipped in SDK v1: any value the server adds later fails validation."""

    HARDBACK = "HARDBACK"
    PAPERBACK = "PAPERBACK"


class TolerantBookFormat(StrEnum):
    """Open enum: pydantic-core falls back to calling the class, so `_missing_` absorbs news."""

    FORMAT_UNSPECIFIED = "FORMAT_UNSPECIFIED"
    HARDBACK = "HARDBACK"
    PAPERBACK = "PAPERBACK"

    @classmethod
    def _missing_(cls, value: object) -> TolerantBookFormat:
        return cls.FORMAT_UNSPECIFIED


class BookV1(BaseModel):
    """SDK v1 resource model; pydantic's default `extra="ignore"` drops unknown keys."""

    name: str
    title: str
    format: BookFormat


class StrictBookV1(BookV1):
    """SDK v1 model with `extra="forbid"`: every additive response field is a wire break."""

    model_config = ConfigDict(extra="forbid")


class TolerantBookV1(BaseModel):
    """Tolerant reader: keeps unknown keys (`extra="allow"`) and maps unknown enum values."""

    model_config = ConfigDict(extra="allow")

    name: str
    title: str
    format: TolerantBookFormat


class BookV2Sdk(BaseModel):
    """SDK v2 renames the attribute `title` -> `display_name` but keeps the JSON key `title`."""

    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)

    name: str
    display_name: str = Field(alias="title")
    format: BookFormat


def wire_error(model: type[BaseModel], payload: bytes) -> str | None:
    """Decode `payload` with `model`; return the first pydantic error type, or None on success."""
    try:
        model.model_validate_json(payload)
    except ValidationError as exc:
        return exc.errors()[0]["type"]
    return None


# --- Source compatibility of SDK call sites ----------------------------------------------


class _Arg:
    """Unique sentinel passed for one parameter so a rebinding can be traced back to it."""

    def __init__(self, name: str) -> None:
        self.name = name


def _call_shapes(old: inspect.Signature) -> dict[str, tuple[list[_Arg], dict[str, _Arg]]]:
    """Two call shapes the old signature accepts: most by position, and most by keyword.

    Positional-only parameters stay positional and keyword-only ones stay keyword in both
    shapes; `*args` and `**kwargs` parameters are not modeled.
    """
    positional: list[_Arg] = []
    positional_kwargs: dict[str, _Arg] = {}
    keyword_args: list[_Arg] = []
    keyword: dict[str, _Arg] = {}
    for param in old.parameters.values():
        arg = _Arg(param.name)
        if param.kind is inspect.Parameter.POSITIONAL_ONLY:
            positional.append(arg)
            keyword_args.append(arg)
        elif param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD:
            positional.append(arg)
            keyword[param.name] = arg
        elif param.kind is inspect.Parameter.KEYWORD_ONLY:
            positional_kwargs[param.name] = arg
            keyword[param.name] = arg
    return {"positional": (positional, positional_kwargs), "keyword": (keyword_args, keyword)}


def source_breaks(old: Callable[..., object], new: Callable[..., object]) -> list[str]:
    """List the old-legal call shapes that the new signature rejects or silently rebinds."""
    new_sig = inspect.signature(new)
    findings: list[str] = []
    for shape, (args, kwargs) in _call_shapes(inspect.signature(old)).items():
        try:
            bound = new_sig.bind(*args, **kwargs)
        except TypeError as exc:
            findings.append(f"{shape}: TypeError: {exc}")
            continue
        for param_name, value in bound.arguments.items():
            # A pure rename leaves the old name absent from the new signature and is harmless
            # positionally; a value landing on another slot while its own name still exists is
            # the silent rebinding that an inserted parameter causes.
            if not isinstance(value, _Arg) or value.name == param_name:
                continue
            if value.name in new_sig.parameters:
                findings.append(f"{shape}: {value.name} -> {param_name}")
    return findings


class BooksClientV1:
    """SDK v1 surface; the method body is irrelevant, only the signature is the contract."""

    def create_book(self, parent: str, book: BookV1, validate_only: bool = False) -> str:
        return f"{parent}:{book.name}:{validate_only}"


class BooksClientV2Inserted:
    """SDK v2 inserts `request_id` before `validate_only`: positional callers rebind."""

    def create_book(
        self,
        parent: str,
        book: BookV1,
        request_id: str | None = None,
        validate_only: bool = False,
    ) -> str:
        return f"{parent}:{book.name}:{request_id}:{validate_only}"


class BooksClientV2Renamed:
    """SDK v2 renames `parent` -> `shelf`: keyword callers raise `TypeError`."""

    def create_book(self, shelf: str, book: BookV1, validate_only: bool = False) -> str:
        return f"{shelf}:{book.name}:{validate_only}"


class BooksClientKeywordOnlyV1:
    """SDK v1 that made optional flags keyword-only from day one."""

    def create_book(self, parent: str, book: BookV1, *, validate_only: bool = False) -> str:
        return f"{parent}:{book.name}:{validate_only}"


class BooksClientKeywordOnlyV2:
    """SDK v2 adds `request_id` among keyword-only parameters: no old call shape changes."""

    def create_book(
        self,
        parent: str,
        book: BookV1,
        *,
        request_id: str | None = None,
        validate_only: bool = False,
    ) -> str:
        return f"{parent}:{book.name}:{request_id}:{validate_only}"


class BooksClientPositionalOnlyV1:
    """SDK v1 that made required parameters positional-only (`/`) from day one."""

    def create_book(self, parent: str, book: BookV1, /, *, validate_only: bool = False) -> str:
        return f"{parent}:{book.name}:{validate_only}"


class BooksClientPositionalOnlyV2Renamed:
    """SDK v2 renames positional-only `parent` -> `shelf`: no old call shape can name it."""

    def create_book(self, shelf: str, book: BookV1, /, *, validate_only: bool = False) -> str:
        return f"{shelf}:{book.name}:{validate_only}"


# --- Semantic compatibility against a real ASGI server ----------------------------------

CATALOG: tuple[str, ...] = tuple(f"books/{i}" for i in range(1, 6))
V2_DEFAULT_PAGE_SIZE = 2


class ListBooksResponseV1(BaseModel):
    """v1 response: no pagination, so old client code treats `books` as the full set."""

    books: list[str]


class CreateBookRequestV1(BaseModel):
    """v1 request body with pydantic's default `extra="ignore"`."""

    title: str


class StrictCreateBookRequestV1(CreateBookRequestV1):
    """v1 request body hardened with `extra="forbid"`: unknown request fields are `422`."""

    model_config = ConfigDict(extra="forbid")


class CreateBookRequestV2(CreateBookRequestV1):
    """v2 adds `validate_only` (AIP-163); its default reproduces the v1 behavior."""

    validate_only: bool = False


def build_server_v1(*, strict_requests: bool = False) -> tuple[FastAPI, list[str]]:
    """v1 Books API: unpaginated list, create without `validate_only`; returns its store."""
    app = FastAPI()
    created: list[str] = []

    @app.get("/v1/books")
    async def list_books() -> dict[str, list[str]]:
        return {"books": list(CATALOG)}

    if strict_requests:

        @app.post("/v1/books")
        async def create_book_strict(body: StrictCreateBookRequestV1) -> dict[str, str]:
            created.append(body.title)
            return {"name": f"books/{len(CATALOG) + len(created)}"}

    else:

        @app.post("/v1/books")
        async def create_book(body: CreateBookRequestV1) -> dict[str, str]:
            created.append(body.title)
            return {"name": f"books/{len(CATALOG) + len(created)}"}

    return app, created


def build_server_v2() -> tuple[FastAPI, list[str]]:
    """v2 Books API: list gains a default `page_size`, create gains `validate_only`."""
    app = FastAPI()
    created: list[str] = []

    @app.get("/v1/books")
    async def list_books(
        page_size: Annotated[int, Query(ge=1)] = V2_DEFAULT_PAGE_SIZE,
        page_token: str = "",
    ) -> dict[str, Any]:
        start = int(page_token or "0")
        end = start + page_size
        token = str(end) if end < len(CATALOG) else ""
        return {"books": list(CATALOG[start:end]), "next_page_token": token}

    @app.post("/v1/books")
    async def create_book(body: CreateBookRequestV2) -> dict[str, str]:
        if not body.validate_only:
            created.append(body.title)
        return {"name": f"books/{len(CATALOG) + len(created)}"}

    return app, created


# --- Protobuf: the same rename is wire-safe in binary and wire-breaking in ProtoJSON ------

_FIELD = descriptor_pb2.FieldDescriptorProto


def build_book_message(
    package: str, title_field: str, copies_type: int, *, with_isbn: bool = False
) -> Any:
    """Build a proto3 `Book` class at runtime: `name=1`, `<title_field>=2`, `copies=3`, `isbn=4`."""
    file_proto = descriptor_pb2.FileDescriptorProto(
        name=f"{package}/book.proto", package=package, syntax="proto3"
    )
    message = file_proto.message_type.add(name="Book")
    fields: list[tuple[str, int, int]] = [("name", 1, _FIELD.TYPE_STRING)]
    fields.append((title_field, 2, _FIELD.TYPE_STRING))
    fields.append(("copies", 3, copies_type))
    if with_isbn:
        fields.append(("isbn", 4, _FIELD.TYPE_STRING))
    for field_name, number, field_type in fields:
        message.field.add(
            name=field_name, number=number, type=field_type, label=_FIELD.LABEL_OPTIONAL
        )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName(f"{package}.Book"))


INT32 = cast("int", _FIELD.TYPE_INT32)
INT64 = cast("int", _FIELD.TYPE_INT64)
