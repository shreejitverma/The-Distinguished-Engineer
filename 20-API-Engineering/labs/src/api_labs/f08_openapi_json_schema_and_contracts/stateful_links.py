"""Stateful API fuzzing: an item lifecycle API, its OpenAPI links, and a Schemathesis state machine.

The app is a Starlette router over an in-memory store with three switchable defects.
`LIFECYCLE_SPEC` declares OpenAPI Link objects that chain `POST /items` (201) into
`GET`, `PATCH`, and `DELETE /items/{item_id}`, and chain `PATCH` (200) and `DELETE` (204)
back into `GET`, so Schemathesis can build a Hypothesis `RuleBasedStateMachine` from them.
`evaluate_expression` and `resolve_pointer` are a strict reference reading of the
runtime-expression ABNF (OpenAPI 3.1.2 Section 4.8.20.3) and of RFC 6901, used to compare
against what Schemathesis does.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

import schemathesis
from schemathesis.config import SchemathesisConfig
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Mapping

    from schemathesis.core.transport import Response as StResponse
    from schemathesis.generation.case import Case
    from schemathesis.generation.stateful.state_machine import APIStateMachine
    from schemathesis.specs.openapi.schemas import OpenApiSchema
    from starlette.requests import Request

MAX_NAME: Final = 40
_ITEM_PATH: Final = "/items/{item_id}"


@dataclass(frozen=True, slots=True)
class Defects:
    """Switchable server bugs; each one breaks a different lifecycle invariant."""

    keep_deleted: bool = False  # DELETE answers 204 but keeps the row: use after free
    drop_creates: bool = False  # POST answers 201 but never stores the row: lost create
    stale_patch: bool = False  # PATCH answers 200 with the new name but persists nothing


@dataclass(slots=True)
class ItemStore:
    """In-memory table; `reset` gives every scenario the same starting state.

    `first_id` above the range a fuzzer favours (0, 1, 2, ...) makes ids unguessable,
    so only a working link can deliver a live id.
    """

    first_id: int = 1
    rows: dict[int, str] = field(default_factory=dict[int, str])
    next_id: int = field(init=False)

    def __post_init__(self) -> None:
        self.next_id = self.first_id

    def reset(self) -> None:
        self.rows.clear()
        self.next_id = self.first_id


def _problem(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def _parse_id(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 18 else None


async def _parse_name(request: Request) -> str | None:
    try:
        body: object = await request.json()
    except ValueError:
        return None
    fields = _object(body)
    if set(fields) != {"name"}:
        return None
    name = fields["name"]
    if not isinstance(name, str) or not 1 <= len(name) <= MAX_NAME:
        return None
    return name


def make_app(store: ItemStore, defects: Defects | None = None) -> Starlette:
    """Build the ASGI app; `/openapi.json` serves `LIFECYCLE_SPEC`."""
    bugs = defects or Defects()

    async def collection(request: Request) -> Response:
        name = await _parse_name(request)
        if name is None:
            return _problem(422, "body must be {name: string of 1..40 characters}")
        item_id = store.next_id
        store.next_id += 1
        if not bugs.drop_creates:
            store.rows[item_id] = name
        return JSONResponse(
            {"id": item_id, "name": name},
            status_code=201,
            headers={"Location": f"/items/{item_id}"},
        )

    async def item(request: Request) -> Response:
        item_id = _parse_id(request.path_params["item_id"])
        if item_id is None:
            return _problem(422, "item_id must be a non-negative integer")
        if item_id not in store.rows:
            return _problem(404, "no such item")
        if request.method == "GET":
            return JSONResponse({"id": item_id, "name": store.rows[item_id]})
        if request.method == "PATCH":
            name = await _parse_name(request)
            if name is None:
                return _problem(422, "body must be {name: string of 1..40 characters}")
            if not bugs.stale_patch:
                store.rows[item_id] = name
            return JSONResponse({"id": item_id, "name": name})
        if not bugs.keep_deleted:
            del store.rows[item_id]
        return Response(status_code=204)

    async def openapi(request: Request) -> Response:
        return JSONResponse(LIFECYCLE_SPEC)

    return Starlette(
        routes=[
            Route("/openapi.json", openapi, methods=["GET"]),
            Route("/items", collection, methods=["POST"]),
            Route(_ITEM_PATH, item, methods=["GET", "PATCH", "DELETE"]),
        ]
    )


_ITEM_ID: Final = {
    "name": "item_id",
    "in": "path",
    "required": True,
    "schema": {"type": "integer", "minimum": 0, "maximum": 999_999_999},
}
_NEW_ITEM: Final = {
    "type": "object",
    "properties": {"name": {"type": "string", "minLength": 1, "maxLength": MAX_NAME}},
    "required": ["name"],
    "additionalProperties": False,
}
_ITEM: Final = {
    "type": "object",
    "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
    "required": ["id", "name"],
    "additionalProperties": False,
}
_PROBLEM: Final = {
    "type": "object",
    "properties": {"detail": {"type": "string"}},
    "required": ["detail"],
}


def _json(schema: Mapping[str, object], description: str) -> dict[str, object]:
    return {"description": description, "content": {"application/json": {"schema": schema}}}


def _link(operation_id: str, item_id: str) -> dict[str, object]:
    return {"operationId": operation_id, "parameters": {"path.item_id": item_id}}


_FROM_BODY: Final = "$response.body#/id"
_FROM_PATH: Final = "$request.path.item_id"

LIFECYCLE_SPEC: Final[dict[str, Any]] = {
    "openapi": "3.1.2",
    "info": {"title": "Item lifecycle", "version": "1.0.0"},
    "paths": {
        "/items": {
            "post": {
                "operationId": "createItem",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": _NEW_ITEM}},
                },
                "responses": {
                    "201": {
                        **_json(_ITEM, "Created"),
                        "headers": {"Location": {"required": True, "schema": {"type": "string"}}},
                        "links": {
                            "GetItem": _link("getItem", _FROM_BODY),
                            "UpdateItem": _link("updateItem", _FROM_BODY),
                            "DeleteItem": _link("deleteItem", _FROM_BODY),
                        },
                    },
                    "422": _json(_PROBLEM, "Invalid body"),
                },
            }
        },
        _ITEM_PATH: {
            "parameters": [_ITEM_ID],
            "get": {
                "operationId": "getItem",
                "responses": {
                    "200": _json(_ITEM, "The item"),
                    "404": _json(_PROBLEM, "No such item"),
                    "422": _json(_PROBLEM, "Invalid item_id"),
                },
            },
            "patch": {
                "operationId": "updateItem",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": _NEW_ITEM}},
                },
                "responses": {
                    "200": {
                        **_json(_ITEM, "Updated"),
                        "links": {"GetAfterUpdate": _link("getItem", _FROM_PATH)},
                    },
                    "404": _json(_PROBLEM, "No such item"),
                    "422": _json(_PROBLEM, "Invalid input"),
                },
            },
            "delete": {
                "operationId": "deleteItem",
                "responses": {
                    "204": {
                        "description": "Deleted",
                        "links": {"GetAfterDelete": _link("getItem", _FROM_PATH)},
                    },
                    "404": _json(_PROBLEM, "No such item"),
                    "422": _json(_PROBLEM, "Invalid item_id"),
                },
            },
        },
    },
}


def spec_without_links() -> dict[str, Any]:
    """`LIFECYCLE_SPEC` with every `links` map removed."""
    spec = copy.deepcopy(LIFECYCLE_SPEC)
    for _, _, response in iter_responses(spec):
        response.pop("links", None)
    return spec


def iter_responses(spec: Mapping[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """Every (operationId, status code, Response Object) in the spec, in document order."""
    found: list[tuple[str, str, dict[str, Any]]] = []
    for path_item in cast("dict[str, dict[str, Any]]", spec["paths"]).values():
        for operation in path_item.values():
            if isinstance(operation, dict):
                op = cast("dict[str, Any]", operation)
                responses = cast("dict[str, dict[str, Any]]", op["responses"])
                found.extend((op["operationId"], code, resp) for code, resp in responses.items())
    return found


def stateful_config(
    *, inference: bool = False, negative: bool = False, constants: bool = True
) -> SchemathesisConfig:
    """Positive-only generation and no link inference unless asked, so the declared links drive.

    `constants` toggles `analysis.constants.enabled`: Schemathesis parses the app's own source
    modules and feeds the literals it finds back into generated parameters.
    """
    mode = "all" if negative else "positive"
    algorithms = ["dependency-analysis", "location-headers"] if inference else []
    raw: dict[str, object] = {
        "generation": {"mode": mode},
        "phases": {"stateful": {"inference": {"algorithms": algorithms}}},
        "analysis": {"constants": {"enabled": constants}},
    }
    # schemathesis 4.33.1 annotates `data: dict` without type arguments
    return SchemathesisConfig.from_dict(raw)  # pyright: ignore[reportUnknownMemberType]


def load_schema(
    app: Starlette, spec: Mapping[str, Any] | None = None, *, config: SchemathesisConfig
) -> OpenApiSchema:
    """Load `spec` (default: the app's own `/openapi.json`) bound to `app` for in-process calls."""
    if spec is None:
        return schemathesis.openapi.from_asgi("/openapi.json", app, config=config)
    schema = schemathesis.openapi.from_dict(dict(spec), config=config)
    schema.app = app
    return schema


def _object(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


@dataclass(slots=True)
class LifecycleModel:
    """Reference model of the store; `observe` returns the first invariant a response breaks."""

    names: dict[int, str] = field(default_factory=dict[int, str])
    deleted: set[int] = field(default_factory=set[int])

    def observe(
        self, method: str, item_id: int | None, sent: object, status: int, body: object
    ) -> str | None:
        fields = _object(body)
        got = fields.get("name")
        if method == "POST" and status == 201 and "id" in fields:
            self.names[int(cast("int", fields["id"]))] = str(got)
        elif item_id is None or item_id not in self.names.keys() | self.deleted:
            return None  # an id this scenario never created: nothing to compare against
        elif method == "GET" and status == 200 and item_id in self.deleted:
            return f"GET /items/{item_id} -> 200 after a successful DELETE"
        elif method == "GET" and status == 200 and got != self.names[item_id]:
            return f"GET /items/{item_id} -> {got!r}, model expects {self.names[item_id]!r}"
        elif method == "GET" and status == 404 and item_id in self.names:
            return f"GET /items/{item_id} -> 404 for an item that was never deleted"
        elif method == "PATCH" and status == 200 and "name" in _object(sent):
            self.names[item_id] = str(_object(sent)["name"])
        elif method == "DELETE" and status == 204:
            self.names.pop(item_id, None)
            self.deleted.add(item_id)
        return None


def lifecycle_machine(
    schema: OpenApiSchema, store: ItemStore, *, model: bool = False
) -> type[APIStateMachine]:
    """The Schemathesis state machine, reset per scenario, optionally checked against the model."""
    base = schema.as_state_machine()

    class Lifecycle(base):  # type: ignore[misc, valid-type]
        reference: LifecycleModel

        def setup(self) -> None:
            store.reset()
            self.reference = LifecycleModel()

        def after_call(self, response: StResponse, case: Case[Any]) -> None:
            # schemathesis 4.33.1 annotates `case: Case` without its type argument
            super().after_call(response, case)  # pyright: ignore[reportUnknownMemberType]
            if not model:
                return
            raw = (case.path_parameters or {}).get("item_id")
            item_id = raw if isinstance(raw, int) else None
            body: object = response.json() if response.content else None
            problem = self.reference.observe(
                case.method.upper(), item_id, case.body, response.status_code, body
            )
            if problem is not None:
                raise AssertionError(f"lifecycle invariant broken: {problem}")

    return Lifecycle


# ---- strict reference reading of runtime expressions and JSON Pointer ----------------------

_ARRAY_INDEX: Final = re.compile(r"0|[1-9][0-9]*")
_TCHAR: Final = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


class Unresolved(Exception):
    """The expression or pointer does not resolve; per the spec no value is passed."""


def resolve_pointer(document: object, pointer: str) -> object:
    """RFC 6901 Section 4: `~1` before `~0`, and array indexes without sign or leading zero."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise Unresolved(f"pointer {pointer!r} does not start with '/'")
    current = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            members = cast("dict[str, object]", current)
            if token not in members:
                raise Unresolved(f"no member {token!r}")
            current = members[token]
        elif isinstance(current, list):
            items = cast("list[object]", current)
            if not _ARRAY_INDEX.fullmatch(token) or int(token) >= len(items):
                raise Unresolved(f"bad array index {token!r}")
            current = items[int(token)]
        else:
            raise Unresolved(f"cannot descend into {type(current).__name__}")
    return current


@dataclass(frozen=True, slots=True)
class Exchange:
    """One finished request and response, the context a runtime expression reads."""

    method: str
    url: str
    status: int
    request_path: Mapping[str, object] = field(default_factory=dict[str, object])
    request_query: Mapping[str, object] = field(default_factory=dict[str, object])
    request_headers: Mapping[str, str] = field(default_factory=dict[str, str])
    request_body: object = None
    response_headers: Mapping[str, str] = field(default_factory=dict[str, str])
    response_body: object = None


def _header(headers: Mapping[str, str], token: str) -> str:
    if not _TCHAR.fullmatch(token):
        raise Unresolved(f"{token!r} is not an RFC 9110 token")
    for name, value in headers.items():
        if name.lower() == token.lower():  # token is case-insensitive
            return value
    raise Unresolved(f"no header {token!r}")


def _source(where: str, rest: str, exchange: Exchange) -> object:
    request = where == "$request"
    if rest == "body" or rest.startswith("body#"):
        body = exchange.request_body if request else exchange.response_body
        return resolve_pointer(body, rest[5:]) if "#" in rest else body
    kind, dot, name = rest.partition(".")
    if not dot:
        raise Unresolved(f"bad source {rest!r}")
    if kind == "header":
        return _header(exchange.request_headers if request else exchange.response_headers, name)
    container = {"query": exchange.request_query, "path": exchange.request_path}.get(kind)
    if not request or container is None:
        raise Unresolved(f"{where}.{kind} is not a source")
    if name not in container:  # name is case-sensitive
        raise Unresolved(f"no {kind} parameter {name!r}")
    return container[name]


def evaluate_expression(expression: str, exchange: Exchange) -> object:
    """Evaluate one bare runtime expression, preserving the referenced value's type."""
    if expression == "$url":
        return exchange.url
    if expression == "$method":
        return exchange.method
    if expression == "$statusCode":
        return exchange.status
    where, dot, rest = expression.partition(".")
    if dot and where in ("$request", "$response"):
        return _source(where, rest, exchange)
    raise Unresolved(f"{expression!r} is not a runtime expression")


def evaluate_template(template: str, exchange: Exchange) -> str:
    """Evaluate `{expression}` segments embedded in a string; the result is always a string."""
    return re.sub(r"\{([^{}]*)\}", lambda m: str(evaluate_expression(m[1], exchange)), template)
