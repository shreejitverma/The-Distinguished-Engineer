"""JSON Merge Patch (RFC 7396) and JSON Patch (RFC 6902) applied atomically behind HTTP PATCH."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

type JSON = bool | int | float | str | list[JSON] | dict[str, JSON] | None
type Container = list[JSON] | dict[str, JSON]
type ErrorKind = Literal["malformed", "conflict", "limit"]

MERGE_PATCH = "application/merge-patch+json"
JSON_PATCH = "application/json-patch+json"
ACCEPT_PATCH = f"{MERGE_PATCH}, {JSON_PATCH}"
MAX_OPERATIONS = 100
MAX_DEPTH = 32
OPERATIONS = frozenset({"add", "remove", "replace", "move", "copy", "test"})
STATUS_FOR_KIND: dict[ErrorKind, int] = {"malformed": 400, "conflict": 409, "limit": 422}
# RFC 6901 Section 4: "0", or digits without a leading "0"; ASCII only, unlike str.isdigit().
ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")
BAD_ESCAPE = re.compile(r"~(?![01])")


class PatchError(Exception):
    """A patch that must not be applied; `kind` selects the HTTP status code."""

    def __init__(self, kind: ErrorKind, index: int | None, message: str) -> None:
        where = "patch document" if index is None else f"operation {index}"
        super().__init__(f"{where}: {message}")
        self.kind: ErrorKind = kind
        self.index = index


class BodyError(ValueError):
    """A request body that is not interoperable JSON (RFC 8259 Sections 4 and 6)."""


# --- RFC 7396 JSON Merge Patch ---------------------------------------------------------------


def merge_patch(target: JSON, patch: JSON) -> JSON:
    """Return MergePatch(target, patch) from RFC 7396 Section 2 without mutating either input."""
    return _merge_in_place(copy.deepcopy(target), patch)


def _merge_in_place(target: JSON, patch: JSON) -> JSON:
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result: dict[str, JSON] = target if isinstance(target, dict) else {}
    for name, value in patch.items():
        if value is None:
            result.pop(name, None)
        else:
            result[name] = _merge_in_place(result.get(name), value)
    return result


# --- RFC 6901 JSON Pointer ---------------------------------------------------------------------


def parse_pointer(pointer: str) -> list[str]:
    """Split a JSON Pointer into unescaped reference tokens (RFC 6901 Sections 3 and 4)."""
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"pointer {pointer!r} must be empty or start with '/'")
    tokens = pointer[1:].split("/")
    for token in tokens:
        if BAD_ESCAPE.search(token):
            raise ValueError(f"pointer {pointer!r} has '~' not followed by '0' or '1'")
    # Decode '~1' before '~0' so that '~01' becomes '~1', never '/'.
    return [token.replace("~1", "/").replace("~0", "~") for token in tokens]


def _array_index(segment: str, length: int, *, allow_end: bool, index: int) -> int:
    if allow_end and segment == "-":
        return length
    if not ARRAY_INDEX.fullmatch(segment):
        raise PatchError("conflict", index, f"{segment!r} is not an array index")
    position = int(segment)
    limit = length if allow_end else length - 1
    if position > limit:
        raise PatchError("conflict", index, f"array index {position} out of range")
    return position


def _child(node: JSON, token: str, index: int) -> JSON:
    if isinstance(node, dict):
        if token not in node:
            raise PatchError("conflict", index, f"member {token!r} does not exist")
        return node[token]
    if isinstance(node, list):
        return node[_array_index(token, len(node), allow_end=False, index=index)]
    raise PatchError("conflict", index, f"cannot descend into a scalar with {token!r}")


def _get(document: JSON, tokens: list[str], index: int) -> JSON:
    node = document
    for token in tokens:
        node = _child(node, token, index)
    return node


def _parent(document: JSON, tokens: list[str], index: int) -> Container:
    node = _get(document, tokens[:-1], index)
    if not isinstance(node, dict | list):
        raise PatchError("conflict", index, "parent of the target location is not a container")
    return node


# --- RFC 6902 JSON Patch -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Operation:
    """One validated JSON Patch operation object (RFC 6902 Section 4)."""

    op: str
    path: list[str]
    value: JSON = None
    source: list[str] | None = None


def json_equal(left: JSON, right: JSON) -> bool:
    """Equality from RFC 6902 Section 4.6: same JSON type, numbers compared numerically."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, int | float) and isinstance(right, int | float):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(json_equal, left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(json_equal(left[k], right[k]) for k in left)
    return False


def _pointer_member(raw: dict[str, JSON], name: str, index: int) -> list[str]:
    pointer = raw.get(name)
    if not isinstance(pointer, str):
        raise PatchError("malformed", index, f"{name!r} must be a JSON Pointer string")
    try:
        return parse_pointer(pointer)
    except ValueError as exc:
        raise PatchError("malformed", index, str(exc)) from exc


def parse_operation(raw: JSON, index: int) -> Operation:
    """Validate one operation object; members not defined for the op are ignored."""
    if not isinstance(raw, dict):
        raise PatchError("malformed", index, "an operation must be a JSON object")
    op = raw.get("op")
    if not isinstance(op, str) or op not in OPERATIONS:
        raise PatchError("malformed", index, f"unknown op {op!r}")
    path = _pointer_member(raw, "path", index)
    if op in {"add", "replace", "test"} and "value" not in raw:
        raise PatchError("malformed", index, f"{op!r} requires a 'value' member")
    source = _pointer_member(raw, "from", index) if op in {"move", "copy"} else None
    if op == "move" and source is not None and source != path and path[: len(source)] == source:
        raise PatchError("malformed", index, "'from' is a proper prefix of 'path'")
    return Operation(op=op, path=path, value=raw.get("value"), source=source)


def _add(document: JSON, tokens: list[str], value: JSON, index: int) -> JSON:
    if not tokens:
        return value
    parent = _parent(document, tokens, index)
    if isinstance(parent, dict):
        parent[tokens[-1]] = value
    else:
        parent.insert(_array_index(tokens[-1], len(parent), allow_end=True, index=index), value)
    return document


def _remove(document: JSON, tokens: list[str], index: int) -> JSON:
    if not tokens:
        raise PatchError("conflict", index, "cannot remove the document root")
    parent = _parent(document, tokens, index)
    if isinstance(parent, dict):
        if tokens[-1] not in parent:
            raise PatchError("conflict", index, f"member {tokens[-1]!r} does not exist")
        return parent.pop(tokens[-1])
    return parent.pop(_array_index(tokens[-1], len(parent), allow_end=False, index=index))


def _apply_one(document: JSON, operation: Operation, index: int) -> JSON:
    match operation.op:
        case "test":
            if not json_equal(_get(document, operation.path, index), operation.value):
                raise PatchError("conflict", index, "test failed")
            return document
        case "remove":
            _remove(document, operation.path, index)
            return document
        case "replace":
            _get(document, operation.path, index)  # the target location MUST exist
            if operation.path:
                _remove(document, operation.path, index)
            return _add(document, operation.path, copy.deepcopy(operation.value), index)
        case "move" | "copy":
            source = operation.source if operation.source is not None else []
            value = _get(document, source, index)
            if operation.op == "move":
                if source:
                    _remove(document, source, index)
            else:
                value = copy.deepcopy(value)
            return _add(document, operation.path, value, index)
        case _:
            return _add(document, operation.path, copy.deepcopy(operation.value), index)


def apply_json_patch(document: JSON, patch: JSON, *, max_operations: int = MAX_OPERATIONS) -> JSON:
    """Apply an RFC 6902 patch to a private copy: all operations succeed, or nothing changes."""
    if not isinstance(patch, list):
        raise PatchError("malformed", None, "a JSON Patch document is a JSON array")
    if len(patch) > max_operations:
        raise PatchError("limit", None, f"{len(patch)} operations exceed {max_operations}")
    working = copy.deepcopy(document)
    for index, raw in enumerate(patch):
        working = _apply_one(working, parse_operation(raw, index), index)
    return working


# --- Strict request-body parsing ---------------------------------------------------------------


def _reject_duplicates(pairs: list[tuple[str, JSON]]) -> dict[str, JSON]:
    result: dict[str, JSON] = {}
    for name, value in pairs:
        if name in result:
            raise BodyError(f"duplicate member name {name!r}")
        result[name] = value
    return result


def _reject_constant(name: str) -> JSON:
    raise BodyError(f"{name} is not a JSON number")


def json_depth(value: JSON) -> int:
    """Nesting depth of containers, computed iteratively so hostile input cannot overflow."""
    deepest = 0
    stack: list[tuple[JSON, int]] = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict | list):
            depth += 1
            deepest = max(deepest, depth)
            children = node.values() if isinstance(node, dict) else node
            stack.extend((child, depth) for child in children)
    return deepest


def parse_json_strict(body: bytes, *, max_depth: int = MAX_DEPTH) -> JSON:
    """Parse JSON, rejecting duplicate names, NaN and Infinity, and deep nesting."""
    try:
        value: JSON = json.loads(
            body, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant
        )
    except (ValueError, RecursionError) as exc:
        raise BodyError(f"invalid JSON: {exc}") from exc
    if json_depth(value) > max_depth:
        raise BodyError(f"nesting deeper than {max_depth}")
    return value


# --- The HTTP resource -------------------------------------------------------------------------


class Widget(BaseModel):
    """Schema every patched candidate must satisfy before it replaces the stored document."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    name: str
    tags: list[str]
    labels: dict[str, str]
    note: str | None = None


@dataclass
class WidgetStore:
    """In-memory resource state; `writes` counts committed PATCH requests."""

    documents: dict[str, dict[str, JSON]] = field(default_factory=dict[str, dict[str, JSON]])
    writes: int = 0


def etag_of(document: JSON) -> str:
    """Strong entity tag over the canonical JSON serialization."""
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return f'"{hashlib.sha256(canonical).hexdigest()[:32]}"'


def _problem(status: int, detail: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        {"status": status, "detail": detail},
        status_code=status,
        headers=headers,
        media_type="application/problem+json",
    )


def _representation(document: dict[str, JSON]) -> JSONResponse:
    return JSONResponse(document, headers={"ETag": etag_of(document), "Accept-Patch": ACCEPT_PATCH})


def _if_match_passes(header: str | None, current_etag: str) -> bool:
    if header is None or header.strip() == "*":
        return True
    return current_etag in (tag.strip() for tag in header.split(","))


def build_app(store: WidgetStore) -> FastAPI:
    """PATCH /widgets/{id} dispatching on Content-Type to RFC 7396 or RFC 6902."""
    app = FastAPI()

    @app.get("/widgets/{widget_id}")
    async def get_widget(widget_id: str) -> Response:
        document = store.documents.get(widget_id)
        if document is None:
            return _problem(404, f"widget {widget_id!r} not found")
        return _representation(document)

    @app.patch("/widgets/{widget_id}")
    async def patch_widget(widget_id: str, request: Request) -> Response:
        current = store.documents.get(widget_id)
        if current is None:
            return _problem(404, f"widget {widget_id!r} not found")
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type not in {MERGE_PATCH, JSON_PATCH}:
            return _problem(
                415, f"unsupported patch format {media_type!r}", {"Accept-Patch": ACCEPT_PATCH}
            )
        if not _if_match_passes(request.headers.get("if-match"), etag_of(current)):
            return _problem(412, "If-Match does not match the current ETag")
        try:
            patch = parse_json_strict(await request.body())
        except BodyError as exc:
            return _problem(400, str(exc))
        try:
            if media_type == MERGE_PATCH:
                candidate = merge_patch(current, patch)
            else:
                candidate = apply_json_patch(current, patch)
        except PatchError as exc:
            return _problem(STATUS_FOR_KIND[exc.kind], str(exc))
        if not isinstance(candidate, dict):
            return _problem(422, "the patched document is not a JSON object")
        if candidate.get("id") != widget_id:
            return _problem(422, "'id' is immutable")
        try:
            Widget.model_validate(candidate)
        except ValidationError as exc:
            return _problem(422, f"patched document is invalid: {exc.error_count()} error(s)")
        store.documents[widget_id] = candidate
        store.writes += 1
        return _representation(candidate)

    return app
