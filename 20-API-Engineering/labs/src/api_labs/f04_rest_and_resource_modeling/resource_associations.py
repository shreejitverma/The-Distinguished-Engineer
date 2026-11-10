"""Many-to-many associations, canonical names, and polymorphic resources (AIP-124, AIP-144).

One in-memory catalog shows three modeling choices side by side:

* ``Book.authors`` is a bounded repeated field of ``authors/{author}`` resource names
  (AIP-124, AIP-144), changed either by whole-list replacement guarded by ``etag``
  (AIP-154) or by the ``:addAuthor`` and ``:removeAuthor`` custom methods.
* ``contributors`` is an association sub-resource under the book, its one canonical
  parent, because the link carries metadata (``role``).
* A book is polymorphic: a pydantic tagged union dispatched on ``kind``, the REST/JSON
  analogue of an AIP-146 ``oneof`` and of an OpenAPI ``discriminator``.
"""

import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Final, Literal

from fastapi import Body, FastAPI, Path, Query
from fastapi.responses import JSONResponse, Response
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter

MAX_AUTHORS: Final = 100
"""AIP-144 rule of thumb: bound a repeated field at about 100 elements."""

RESOURCE_ID: Final = r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
"""AIP-122 user-specified ID: RFC 1034 label, lower-case, at most 63 characters."""
AUTHOR_NAME_PATTERN: Final = rf"^authors/{RESOURCE_ID}$"
WILDCARD: Final = "-"
_HAS_AUTHOR_FILTER: Final = re.compile(rf'^authors:"(authors/{RESOURCE_ID})"$')

LinkSemantics = Literal["aip144", "jsonapi"]


def _reject_duplicates(names: list[str]) -> list[str]:
    if len(set(names)) != len(names):
        raise ValueError("authors must not repeat a resource name")
    return names


AuthorName = Annotated[str, Field(pattern=AUTHOR_NAME_PATTERN)]
AuthorList = Annotated[
    list[AuthorName], Field(max_length=MAX_AUTHORS), AfterValidator(_reject_duplicates)
]
PublisherId = Annotated[str, Path(pattern=rf"^(-|{RESOURCE_ID})$")]
BookId = Annotated[str, Path(pattern=rf"^{RESOURCE_ID}$")]
AuthorId = Annotated[str, Path(pattern=rf"^{RESOURCE_ID}$")]
NewId = Annotated[str, Query(pattern=rf"^{RESOURCE_ID}$")]


class _BookFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    authors: AuthorList = Field(default_factory=list[str])


class PrintBook(_BookFields):
    kind: Literal["print"]
    page_count: int = Field(ge=1)


class EBook(_BookFields):
    kind: Literal["ebook"]
    file_bytes: int = Field(ge=1)


class AudioBook(_BookFields):
    kind: Literal["audio"]
    duration_seconds: int = Field(ge=1)


AnyBook = PrintBook | EBook | AudioBook
TAGGED_BOOK: Final = TypeAdapter[AnyBook](Annotated[AnyBook, Field(discriminator="kind")])
UNTAGGED_BOOK: Final = TypeAdapter[AnyBook](AnyBook)


class LegacyPrint(BaseModel):
    """Pre-discriminator model: no ``kind``, extra keys ignored, every field defaulted."""

    title: str
    page_count: int = 0


class LegacyEBook(BaseModel):
    title: str
    file_bytes: int = 0


LEGACY_UNION: Final = TypeAdapter[LegacyPrint | LegacyEBook](LegacyPrint | LegacyEBook)


class AuthorsUpdate(BaseModel):
    """Whole-list replacement of ``authors``; ``kind`` and every other field are not updatable."""

    model_config = ConfigDict(extra="forbid")

    authors: AuthorList
    etag: str | None = None


class AuthorLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    author: AuthorName


class ContributorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    author: AuthorName
    role: Literal["translator", "illustrator", "editor"]


@dataclass(slots=True)
class StoredBook:
    name: str
    body: AnyBook
    revision: int = 1

    @property
    def etag(self) -> str:
        return f'"{self.revision}"'

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "etag": self.etag, **self.body.model_dump(mode="json")}


@dataclass(frozen=True, slots=True)
class Contributor:
    name: str
    author: str
    role: str

    def to_json(self) -> dict[str, str]:
        return {"name": self.name, "author": self.author, "role": self.role}


@dataclass(slots=True)
class Catalog:
    authors: set[str] = field(default_factory=set[str])
    books: dict[str, StoredBook] = field(default_factory=dict[str, StoredBook])
    contributors: dict[str, Contributor] = field(default_factory=dict[str, Contributor])

    def find_book(self, publisher: str, book: str) -> StoredBook | None:
        if publisher == WILDCARD:
            # Book IDs are unique across publishers (enforced on create), so this is unambiguous.
            matches = [b for b in self.books.values() if b.name.rsplit("/", 1)[1] == book]
            return matches[0] if matches else None
        return self.books.get(f"publishers/{publisher}/books/{book}")

    def unknown_authors(self, names: list[str]) -> list[str]:
        return [n for n in names if n not in self.authors]


def _error(code: int, status: str, message: str) -> JSONResponse:
    """A google.rpc.Status-shaped error body (AIP-193) with the HTTP mapping from code.proto."""
    return JSONResponse(
        status_code=code, content={"error": {"code": code, "status": status, "message": message}}
    )


def create_app(catalog: Catalog, *, link_semantics: LinkSemantics = "aip144") -> FastAPI:
    """Build the catalog API; ``link_semantics`` picks AIP-144 or JSON:API add/remove rules."""
    app = FastAPI()

    @app.post("/v1/publishers/{publisher}/books")
    async def create_book(
        publisher: PublisherId,
        book_id: NewId,
        body: Annotated[AnyBook, Body(discriminator="kind")],
    ) -> Response:
        if publisher == WILDCARD:
            return _error(400, "INVALID_ARGUMENT", "the - wildcard is valid only for reads")
        if catalog.find_book(WILDCARD, book_id) is not None:
            return _error(409, "ALREADY_EXISTS", f"book id {book_id} is taken")
        if unknown := catalog.unknown_authors(body.authors):
            return _error(400, "INVALID_ARGUMENT", f"unknown author references: {unknown}")
        stored = StoredBook(name=f"publishers/{publisher}/books/{book_id}", body=body)
        catalog.books[stored.name] = stored
        return JSONResponse(
            status_code=201, content=stored.to_json(), headers={"Location": f"/v1/{stored.name}"}
        )

    @app.get("/v1/publishers/{publisher}/books/{book}")
    async def get_book(publisher: PublisherId, book: BookId) -> Response:
        stored = catalog.find_book(publisher, book)
        if stored is None:
            return _error(404, "NOT_FOUND", f"book {book} not found")
        return JSONResponse(stored.to_json())

    @app.get("/v1/publishers/{publisher}/books")
    async def list_books(
        publisher: PublisherId,
        filter_: Annotated[str | None, Query(alias="filter")] = None,
    ) -> Response:
        author: str | None = None
        if filter_ is not None:
            match = _HAS_AUTHOR_FILTER.fullmatch(filter_)
            if match is None:
                return _error(400, "INVALID_ARGUMENT", 'only authors:"authors/{id}" is supported')
            author = match.group(1)
        prefix = "" if publisher == WILDCARD else f"publishers/{publisher}/books/"
        books = [
            stored.to_json()
            for name, stored in sorted(catalog.books.items())
            if name.startswith(prefix) and (author is None or author in stored.body.authors)
        ]
        return JSONResponse({"books": books})

    @app.patch("/v1/publishers/{publisher}/books/{book}")
    async def update_authors(publisher: PublisherId, book: BookId, body: AuthorsUpdate) -> Response:
        stored = catalog.find_book(publisher, book)
        if stored is None:
            return _error(404, "NOT_FOUND", f"book {book} not found")
        if body.etag is not None and body.etag != stored.etag:
            return _error(409, "ABORTED", f"etag {body.etag} is stale; current is {stored.etag}")
        if unknown := catalog.unknown_authors(body.authors):
            return _error(400, "INVALID_ARGUMENT", f"unknown author references: {unknown}")
        stored.body = stored.body.model_copy(update={"authors": list(body.authors)})
        stored.revision += 1
        return JSONResponse(stored.to_json())

    @app.post("/v1/publishers/{publisher}/books/{book}:addAuthor")
    async def add_author(publisher: PublisherId, book: BookId, body: AuthorLink) -> Response:
        stored = catalog.find_book(publisher, book)
        if stored is None:
            return _error(404, "NOT_FOUND", f"book {book} not found")
        if body.author in stored.body.authors:
            if link_semantics == "jsonapi":
                return JSONResponse(stored.to_json())
            return _error(409, "ALREADY_EXISTS", f"{body.author} is already an author")
        if catalog.unknown_authors([body.author]):
            return _error(400, "INVALID_ARGUMENT", f"unknown author reference: {body.author}")
        if len(stored.body.authors) >= MAX_AUTHORS:
            return _error(400, "FAILED_PRECONDITION", f"a book has at most {MAX_AUTHORS} authors")
        stored.body.authors.append(body.author)
        stored.revision += 1
        return JSONResponse(stored.to_json())

    @app.post("/v1/publishers/{publisher}/books/{book}:removeAuthor")
    async def remove_author(publisher: PublisherId, book: BookId, body: AuthorLink) -> Response:
        stored = catalog.find_book(publisher, book)
        if stored is None:
            return _error(404, "NOT_FOUND", f"book {book} not found")
        if body.author not in stored.body.authors:
            if link_semantics == "jsonapi":
                return JSONResponse(stored.to_json())
            return _error(404, "NOT_FOUND", f"{body.author} is not an author")
        stored.body.authors.remove(body.author)
        stored.revision += 1
        return JSONResponse(stored.to_json())

    @app.post("/v1/publishers/{publisher}/books/{book}/contributors")
    async def create_contributor(
        publisher: PublisherId, book: BookId, contributor_id: NewId, body: ContributorCreate
    ) -> Response:
        if publisher == WILDCARD:
            return _error(400, "INVALID_ARGUMENT", "the - wildcard is valid only for reads")
        stored = catalog.find_book(publisher, book)
        if stored is None:
            return _error(404, "NOT_FOUND", f"book {book} not found")
        if catalog.unknown_authors([body.author]):
            return _error(400, "INVALID_ARGUMENT", f"unknown author reference: {body.author}")
        name = f"{stored.name}/contributors/{contributor_id}"
        siblings = [
            c for c in catalog.contributors.values() if c.name.startswith(stored.name + "/")
        ]
        if name in catalog.contributors or any(
            (c.author, c.role) == (body.author, body.role) for c in siblings
        ):
            return _error(409, "ALREADY_EXISTS", f"{body.author} is already a {body.role}")
        catalog.contributors[name] = Contributor(name=name, author=body.author, role=body.role)
        return JSONResponse(
            status_code=201,
            content=catalog.contributors[name].to_json(),
            headers={"Location": f"/v1/{name}"},
        )

    @app.get("/v1/publishers/{publisher}/books/{book}/contributors")
    async def list_contributors(publisher: PublisherId, book: BookId) -> Response:
        stored = catalog.find_book(publisher, book)
        if stored is None:
            return _error(404, "NOT_FOUND", f"book {book} not found")
        prefix = stored.name + "/contributors/"
        rows = [
            c.to_json() for n, c in sorted(catalog.contributors.items()) if n.startswith(prefix)
        ]
        return JSONResponse({"contributors": rows})

    @app.delete("/v1/authors/{author}")
    async def delete_author(author: AuthorId) -> Response:
        name = f"authors/{author}"
        if name not in catalog.authors:
            return _error(404, "NOT_FOUND", f"{name} not found")
        catalog.authors.remove(name)  # references by name are weak: books keep the string
        return Response(status_code=204)

    return app
