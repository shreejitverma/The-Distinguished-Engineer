"""Resource-oriented hierarchy lab: AIP-121, AIP-122, AIP-123, AIP-156, and AIP-159 on Starlette.

The module has two halves.
`ResourcePattern` validates and matches resource name patterns such as
`publishers/{publisher}/books/{book}` against the AIP-122 and AIP-123 grammar.
`build_app` serves a publisher -> book hierarchy with a `config` singleton per publisher,
cascading delete, the `-` wildcard, an imperative `:move` custom method, and a guard
against percent-encoded `/` in path segments.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

    Endpoint = Callable[[Request], Awaitable[Response]]

# AIP-122 "Collection identifiers": /[a-z][a-zA-Z0-9]*/.
COLLECTION_ID = re.compile(r"[a-z][a-zA-Z0-9]*")
# AIP-123 pattern variables: [a-z][_a-z0-9]*[a-z0-9].
PATTERN_VARIABLE = re.compile(r"[a-z][_a-z0-9]*[a-z0-9]")
# AIP-122 user-specified resource IDs: RFC 1034 labels, lower case, at most 63 characters.
RESOURCE_ID = re.compile(r"[a-z]([a-z0-9-]{0,61}[a-z0-9])?")
# AIP-159 wildcard parent collection ID.
WILDCARD = "-"
# Lab policy, not an AIP rule: one root collection plus Zalando rule #147's "<= 3" nesting levels.
MAX_COLLECTIONS = 4


class PatternError(ValueError):
    """A resource name pattern violates the AIP-122 or AIP-123 grammar or the depth policy."""


@dataclass(frozen=True, slots=True)
class ResourcePattern:
    """A parsed resource name pattern: alternating collection IDs and `{variable}` segments."""

    pattern: str
    segments: tuple[str, ...]

    @classmethod
    def parse(cls, pattern: str, *, max_collections: int = MAX_COLLECTIONS) -> ResourcePattern:
        if pattern.startswith("/") or not pattern:
            raise PatternError(f"{pattern!r}: resource names have no leading slash")
        segments = tuple(pattern.split("/"))
        collections: set[str] = set()
        variables: set[str] = set()
        for index, segment in enumerate(segments):
            is_variable = segment.startswith("{") and segment.endswith("}")
            if index % 2 == 0:
                if is_variable or not COLLECTION_ID.fullmatch(segment):
                    raise PatternError(f"{pattern!r}: segment {index} must be a collection ID")
                if segment in collections:
                    raise PatternError(f"{pattern!r}: duplicate collection ID {segment!r}")
                collections.add(segment)
            else:
                variable = segment[1:-1]
                if not is_variable or not PATTERN_VARIABLE.fullmatch(variable):
                    raise PatternError(f"{pattern!r}: segment {index} must be a {{variable}}")
                if variable.endswith("_id"):
                    raise PatternError(f"{pattern!r}: variable {variable!r} has an _id suffix")
                if variable in variables:
                    raise PatternError(f"{pattern!r}: duplicate variable {variable!r}")
                variables.add(variable)
        if len(variables) > max_collections:
            raise PatternError(f"{pattern!r}: {len(variables)} collections > {max_collections}")
        return cls(pattern=pattern, segments=segments)

    @property
    def is_singleton(self) -> bool:
        """An odd segment count ends in a static segment: a singleton (AIP-156)."""
        return len(self.segments) % 2 == 1

    @property
    def variables(self) -> tuple[str, ...]:
        return tuple(segment[1:-1] for segment in self.segments[1::2])

    def match(self, name: str, *, allow_wildcard: bool = False) -> dict[str, str] | None:
        """Return the variable bindings for `name`, or None if it is not a valid instance."""
        parts = name.split("/")
        if len(parts) != len(self.segments):
            return None
        bindings: dict[str, str] = {}
        for segment, part in zip(self.segments, parts, strict=True):
            if not segment.startswith("{"):
                if part != segment:
                    return None
            elif RESOURCE_ID.fullmatch(part) or (allow_wildcard and part == WILDCARD):
                bindings[segment[1:-1]] = part
            else:
                return None
        return bindings

    def render(self, **ids: str) -> str:
        if set(ids) != set(self.variables):
            raise PatternError(f"{self.pattern!r}: expected {self.variables}, got {sorted(ids)}")
        rendered = [
            ids[segment[1:-1]] if segment.startswith("{") else segment for segment in self.segments
        ]
        return "/".join(rendered)

    def max_name_length(self) -> int:
        """Longest valid resource name: every ID at the 63-character RFC 1034 maximum."""
        return sum(63 if s.startswith("{") else len(s) for s in self.segments) + (
            len(self.segments) - 1
        )


PUBLISHER = ResourcePattern.parse("publishers/{publisher}")
BOOK = ResourcePattern.parse("publishers/{publisher}/books/{book}")
CONFIG = ResourcePattern.parse("publishers/{publisher}/config")


@dataclass
class Store:
    """In-memory resources keyed by canonical resource name, in insertion order."""

    resources: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])

    def children(self, parent: str) -> list[str]:
        prefix = parent + "/"
        return [name for name in self.resources if name.startswith(prefix)]


def error(code: int, status: str, message: str) -> JSONResponse:
    """A `google.rpc.Status`-shaped error body (HTTP mappings from google/rpc/code.proto)."""
    return JSONResponse(
        {"error": {"code": code, "status": status, "message": message}}, status_code=code
    )


def created(resource: dict[str, Any]) -> JSONResponse:
    """`201 Created` with `Location` naming the new resource (RFC 9110 Section 15.3.2)."""
    return JSONResponse(resource, status_code=201, headers={"Location": f"/v1/{resource['name']}"})


def _valid_id(value: str) -> bool:
    return RESOURCE_ID.fullmatch(value) is not None


async def _json_object(request: Request) -> dict[str, Any] | None:
    try:
        body: object = await request.json()
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    return dict(body)  # pyright: ignore[reportUnknownArgumentType]


class RejectEncodedSlash:
    """Pure ASGI guard: ASGI `path` is percent-decoded, so `%2F` would add segments."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            raw_path: bytes | None = scope.get("raw_path")
            if raw_path is not None and b"%2f" in raw_path.lower():
                response = error(400, "INVALID_ARGUMENT", "encoded '/' in a resource ID")
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def method_route(path: str, handlers: dict[str, Endpoint]) -> Route:
    """One `Route` per path, so a `405` lists every method in `Allow` (RFC 9110 Section 15.5.6).

    `Router.app` hands a method mismatch to the first PARTIAL match only, so splitting one path
    across several `Route` objects makes `Allow` name just the first route's methods.
    """

    async def dispatch(request: Request) -> Response:
        return await handlers["GET" if request.method == "HEAD" else request.method](request)

    return Route(path, dispatch, methods=list(handlers))


def build_app(*, guard_encoded_slash: bool = True) -> Starlette:
    """Serve publishers, their books, and one `config` singleton per publisher."""
    store = Store()

    async def create_publisher(request: Request) -> Response:
        publisher_id = request.query_params.get("publisher_id", "")
        if not _valid_id(publisher_id):
            return error(400, "INVALID_ARGUMENT", f"invalid publisher_id {publisher_id!r}")
        name = PUBLISHER.render(publisher=publisher_id)
        if name in store.resources:
            return error(409, "ALREADY_EXISTS", name)
        store.resources[name] = {"name": name}
        # AIP-156: the singleton exists by virtue of its parent; it has no Create method.
        config = CONFIG.render(publisher=publisher_id)
        store.resources[config] = {"name": config, "language": "en"}
        return created(store.resources[name])

    async def get_publisher(request: Request) -> Response:
        name = PUBLISHER.render(publisher=request.path_params["publisher"])
        if name not in store.resources:
            return error(404, "NOT_FOUND", name)
        return JSONResponse(store.resources[name])

    async def delete_publisher(request: Request) -> Response:
        name = PUBLISHER.render(publisher=request.path_params["publisher"])
        if name not in store.resources:
            return error(404, "NOT_FOUND", name)
        force = request.query_params.get("force") == "true"
        children = store.children(name)
        blocking = [child for child in children if CONFIG.match(child) is None]
        if blocking and not force:
            # AIP-135: FAILED_PRECONDITION, which code.proto maps to HTTP 400.
            return error(400, "FAILED_PRECONDITION", f"{name} has {len(blocking)} child resources")
        for child in children:
            del store.resources[child]
        del store.resources[name]
        return Response(status_code=204)

    async def list_books(request: Request) -> Response:
        publisher = request.path_params["publisher"]
        if publisher != WILDCARD and PUBLISHER.render(publisher=publisher) not in store.resources:
            return error(404, "NOT_FOUND", PUBLISHER.render(publisher=publisher))
        books = [
            resource
            for name, resource in store.resources.items()
            if (bindings := BOOK.match(name)) is not None
            and (publisher in (WILDCARD, bindings["publisher"]))
        ]
        # AIP-159: responses carry canonical names with the real parent, never `-`.
        return JSONResponse({"books": books})

    async def create_book(request: Request) -> Response:
        publisher = request.path_params["publisher"]
        book_id = request.query_params.get("book_id", "")
        if not _valid_id(publisher) or not _valid_id(book_id):
            return error(400, "INVALID_ARGUMENT", "invalid parent or book_id")
        parent = PUBLISHER.render(publisher=publisher)
        if parent not in store.resources:
            return error(404, "NOT_FOUND", parent)
        body = await _json_object(request)
        if body is None:
            return error(400, "INVALID_ARGUMENT", "body must be a JSON object")
        name = BOOK.render(publisher=publisher, book=book_id)
        if name in store.resources:
            return error(409, "ALREADY_EXISTS", name)
        store.resources[name] = {"name": name, "title": str(body.get("title", ""))}
        return created(store.resources[name])

    def _book_name(request: Request) -> str | Response:
        publisher = request.path_params["publisher"]
        book = request.path_params["book"]
        if publisher == WILDCARD:
            # AIP-159: book IDs are unique only per publisher, so Get with `-` is refused.
            return error(400, "INVALID_ARGUMENT", "'-' is not supported: book IDs are per-parent")
        if not _valid_id(publisher) or not _valid_id(book):
            return error(400, "INVALID_ARGUMENT", f"invalid resource ID in {publisher}/{book}")
        name = BOOK.render(publisher=publisher, book=book)
        if name not in store.resources:
            return error(404, "NOT_FOUND", name)
        return name

    async def get_book(request: Request) -> Response:
        name = _book_name(request)
        if isinstance(name, Response):
            return name
        return JSONResponse(store.resources[name])

    async def update_book(request: Request) -> Response:
        name = _book_name(request)
        if isinstance(name, Response):
            return name
        body = await _json_object(request)
        if body is None:
            return error(400, "INVALID_ARGUMENT", "body must be a JSON object")
        if body.get("name", name) != name:
            # AIP-203 IDENTIFIER: the name is immutable on Update; AIP-180: names never change.
            return error(400, "INVALID_ARGUMENT", "name is immutable; use :move")
        if "title" in body:
            store.resources[name]["title"] = str(body["title"])
        return JSONResponse(store.resources[name])

    async def delete_book(request: Request) -> Response:
        name = _book_name(request)
        if isinstance(name, Response):
            return name
        del store.resources[name]
        return Response(status_code=204)

    async def move_book(request: Request) -> Response:
        name = _book_name(request)
        if isinstance(name, Response):
            return name
        body = await _json_object(request)
        destination = None if body is None else body.get("destination_parent")
        bindings = None if not isinstance(destination, str) else PUBLISHER.match(destination)
        if bindings is None:
            return error(400, "INVALID_ARGUMENT", "destination_parent must be publishers/{id}")
        if PUBLISHER.render(**bindings) not in store.resources:
            return error(404, "NOT_FOUND", str(destination))
        new_name = BOOK.render(publisher=bindings["publisher"], book=request.path_params["book"])
        if new_name in store.resources:
            return error(409, "ALREADY_EXISTS", new_name)
        # The moved book is a different resource name; the old name now answers NOT_FOUND.
        moved = store.resources.pop(name)
        moved["name"] = new_name
        store.resources[new_name] = moved
        return JSONResponse(moved)

    def _config_name(request: Request) -> str | Response:
        name = CONFIG.render(publisher=request.path_params["publisher"])
        if name not in store.resources:
            return error(404, "NOT_FOUND", name)
        return name

    async def get_config(request: Request) -> Response:
        name = _config_name(request)
        if isinstance(name, Response):
            return name
        return JSONResponse(store.resources[name])

    async def update_config(request: Request) -> Response:
        name = _config_name(request)
        if isinstance(name, Response):
            return name
        body = await _json_object(request)
        if body is None or body.get("name", name) != name:
            return error(400, "INVALID_ARGUMENT", "body must be a JSON object with the same name")
        if "language" in body:
            store.resources[name]["language"] = str(body["language"])
        return JSONResponse(store.resources[name])

    routes = [
        method_route("/v1/publishers", {"POST": create_publisher}),
        method_route(
            "/v1/publishers/{publisher}", {"GET": get_publisher, "DELETE": delete_publisher}
        ),
        method_route("/v1/publishers/{publisher}/books", {"GET": list_books, "POST": create_book}),
        method_route(
            "/v1/publishers/{publisher}/books/{book}",
            {"GET": get_book, "PATCH": update_book, "DELETE": delete_book},
        ),
        method_route("/v1/publishers/{publisher}/books/{book}:move", {"POST": move_book}),
        # AIP-156: a singleton has Get and Update, never Create or Delete.
        method_route(
            "/v1/publishers/{publisher}/config", {"GET": get_config, "PATCH": update_config}
        ),
    ]
    app = Starlette(routes=routes)
    if guard_encoded_slash:
        app.add_middleware(RejectEncodedSlash)
    return app
