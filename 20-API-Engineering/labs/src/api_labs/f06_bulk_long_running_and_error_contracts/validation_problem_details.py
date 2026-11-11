"""Map FastAPI RequestValidationError lists to RFC 9457 problem details with RFC 6901 pointers."""

from collections.abc import Callable, Coroutine, Mapping, Sequence
from typing import Any, Final, NotRequired, TypedDict, cast
from urllib.parse import quote, unquote

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.requests import Request

PROBLEM_JSON: Final = "application/problem+json"
VALIDATION_TYPE: Final = "https://errors.example.com/problems/request-validation"
MALFORMED_JSON_TYPE: Final = "https://errors.example.com/problems/malformed-json"
PARAMETER_SOURCES: Final = frozenset({"path", "query", "header", "cookie"})
# RFC 3986 Section 3.5: fragment = *( pchar / "/" / "?" ); pchar adds sub-delims, ":" and "@"
# to the unreserved set that urllib.parse.quote never escapes.
FRAGMENT_SAFE: Final = "!$&'()*+,;=:@/?"


class ProblemError(TypedDict):
    """One validation failure inside the `errors` extension member."""

    detail: str
    code: str
    source: str
    pointer: NotRequired[str]
    parameter: NotRequired[str]


class ValidationProblem(TypedDict):
    """RFC 9457 problem details object for request validation failures."""

    type: str
    title: str
    status: int
    detail: str
    errors: list[ProblemError]
    error_count: int


class ProblemJSONResponse(JSONResponse):
    """JSONResponse with the RFC 9457 Section 3 media type."""

    media_type = PROBLEM_JSON


def encode_token(token: str | int) -> str:
    """Escape one reference token (RFC 6901 Section 3): '~' -> '~0' first, then '/' -> '~1'."""
    return str(token).replace("~", "~0").replace("/", "~1")


def to_fragment(tokens: Sequence[str | int]) -> str:
    """Render tokens as a URI fragment JSON Pointer (RFC 6901 Section 6), UTF-8 percent-encoded."""
    return "#" + "".join("/" + quote(encode_token(t), safe=FRAGMENT_SAFE) for t in tokens)


def evaluate_fragment(document: object, fragment: str) -> object:
    """Resolve a URI fragment JSON Pointer against a document (RFC 6901 Sections 4 and 6)."""
    if not fragment.startswith("#"):
        raise ValueError(f"not a URI fragment pointer: {fragment!r}")
    pointer = unquote(fragment[1:], encoding="utf-8", errors="strict")
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError(f"pointer must start with '/': {pointer!r}")
    node = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        found, node = _child(node, _reference_key(node, token))
        if not found:
            raise KeyError(token)
    return node


def _reference_key(node: object, token: str) -> str | int:
    """Object member name, or an RFC 6901 Section 4 array-index (ASCII digits, no leading 0)."""
    if not isinstance(node, list):
        return token
    if not (token.isascii() and token.isdigit()) or (len(token) > 1 and token[0] == "0"):
        raise KeyError(token)
    return int(token)


def _child(node: object, segment: str | int) -> tuple[bool, object]:
    """Step one level into a JSON object or array; report whether the member exists."""
    if isinstance(node, Mapping) and isinstance(segment, str):
        mapping = cast("Mapping[str, object]", node)
        if segment in mapping:
            return True, mapping[segment]
    elif isinstance(node, list) and type(segment) is int:
        items = cast("list[object]", node)
        if 0 <= segment < len(items):
            return True, items[segment]
    return False, None


def body_tokens(loc_tail: Sequence[str | int], body: object, error_type: str) -> list[str | int]:
    """Keep only loc segments that address the request document.

    pydantic-core inserts schema-only segments into `loc`: union member names (`int`, `bool`),
    discriminator tags (`cat`), and `[key]` for dict-key errors. Those never exist in the body,
    so the segment is kept only if it resolves; the last segment of a `missing` error is kept
    because it names the absent member the client must add.
    """
    tokens: list[str | int] = []
    node = body
    last = len(loc_tail) - 1
    for index, segment in enumerate(loc_tail):
        found, child = _child(node, segment)
        if found:
            tokens.append(segment)
            node = child
        elif index == last and error_type == "missing" and isinstance(segment, str):
            tokens.append(segment)
    return tokens


def map_error(error: Mapping[str, Any], body: object) -> ProblemError:
    """Translate one pydantic-core ErrorDetails dict; drop `input`, `ctx`, and `url`."""
    loc: tuple[str | int, ...] = tuple(error["loc"])
    source = str(loc[0]) if loc else "body"
    mapped: ProblemError = {
        "detail": str(error["msg"]),
        "code": str(error["type"]),
        "source": source,
    }
    if source in PARAMETER_SOURCES:
        if len(loc) > 1:
            mapped["parameter"] = str(loc[1])
    else:
        mapped["pointer"] = to_fragment(body_tokens(loc[1:], body, str(error["type"])))
    return mapped


def build_validation_problem(
    errors: Sequence[Mapping[str, Any]], body: object, *, max_errors: int = 50
) -> ValidationProblem:
    """Build the 422 problem; keep at most `max_errors` items and report the true count."""
    mapped = [map_error(error, body) for error in errors[:max_errors]]
    count = len(errors)
    noun = "error" if count == 1 else "errors"
    return {
        "type": VALIDATION_TYPE,
        "title": "Request validation failed",
        "status": 422,
        "detail": f"The request has {count} validation {noun}.",
        "errors": mapped,
        "error_count": count,
    }


def build_malformed_json_problem(error: Mapping[str, Any]) -> dict[str, object]:
    """Build the 400 problem for a JSON syntax error; the loc offset is not a pointer."""
    loc: tuple[str | int, ...] = tuple(error["loc"])
    offset = loc[1] if len(loc) > 1 and type(loc[1]) is int else None
    reason = str(error.get("ctx", {}).get("error", "invalid JSON"))
    problem: dict[str, object] = {
        "type": MALFORMED_JSON_TYPE,
        "title": "Malformed JSON request body",
        "status": 400,
        "detail": f"The request body is not valid JSON: {reason}.",
    }
    if offset is not None:
        problem["offset"] = offset
    return problem


# FastAPI types `exception_handlers` values as Callable[[Request, Any], Coroutine[..., Response]].
ValidationHandler = Callable[[Request, Any], Coroutine[Any, Any, Response]]


def make_validation_handler(max_errors: int = 50) -> ValidationHandler:
    """Return an exception handler for `FastAPI(exception_handlers={RequestValidationError: h})`."""

    async def handler(request: Request, exc: Any) -> Response:
        if not isinstance(exc, RequestValidationError):
            raise TypeError(f"expected RequestValidationError, got {type(exc).__name__}")
        errors = exc.errors()
        if len(errors) == 1 and errors[0].get("type") == "json_invalid":
            return ProblemJSONResponse(build_malformed_json_problem(errors[0]), status_code=400)
        problem = build_validation_problem(errors, exc.body, max_errors=max_errors)
        return ProblemJSONResponse(problem, status_code=problem["status"])

    return handler


_ERROR_ITEM_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "required": ["detail", "code", "source"],
    "properties": {
        "detail": {"type": "string"},
        "code": {"type": "string"},
        "source": {"type": "string", "enum": ["body", "path", "query", "header", "cookie"]},
        "pointer": {"type": "string"},
        "parameter": {"type": "string"},
    },
}

PROBLEM_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    400: {
        "description": "Malformed JSON request body",
        "content": {PROBLEM_JSON: {"schema": {"type": "object"}}},
    },
    422: {
        "description": "Request validation failed",
        "content": {
            PROBLEM_JSON: {
                "schema": {
                    "type": "object",
                    "required": ["type", "title", "status", "errors", "error_count"],
                    "properties": {
                        "type": {"type": "string", "format": "uri-reference"},
                        "title": {"type": "string"},
                        "status": {"type": "integer"},
                        "detail": {"type": "string"},
                        "errors": {"type": "array", "items": _ERROR_ITEM_SCHEMA},
                        "error_count": {"type": "integer"},
                    },
                }
            }
        },
    },
}
