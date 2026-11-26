"""A JSON-RPC 2.0 dispatcher: envelope validation, notifications, batches, and error codes.

The core is `Dispatcher.handle`, a transport-free coroutine from request bytes to response text
(or `None` when the specification says to send nothing).
`build_app` wraps it in a Starlette POST endpoint, and `correlate` is the client half of a batch.
Every rule cites the section of https://www.jsonrpc.org/specification it implements.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from starlette.requests import Request

logger = logging.getLogger(__name__)

PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603

STANDARD_MESSAGES: Final[dict[int, str]] = {
    PARSE_ERROR: "Parse error",
    INVALID_REQUEST: "Invalid Request",
    METHOD_NOT_FOUND: "Method not found",
    INVALID_PARAMS: "Invalid params",
    INTERNAL_ERROR: "Internal error",
}

RESERVED_MIN: Final = -32768
SERVER_ERROR_MIN: Final = -32099
SERVER_ERROR_MAX: Final = -32000

type RequestId = int | str
type Handler = Callable[..., Any]


class _Missing:
    """Marks an absent `data` member, so `data=None` can still mean `"data": null`."""

    __slots__ = ()


MISSING: Final = _Missing()


class RpcError(Exception):
    """An error a handler raises on purpose; it becomes the response `error` object."""

    def __init__(self, code: int, message: str, data: Any = MISSING) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class _DuplicateMemberError(ValueError):
    """A JSON object repeated a member name (RFC 8259 Section 4 calls the result unpredictable)."""


@dataclass(frozen=True, slots=True)
class Limits:
    """Resource bounds the specification leaves to the implementation."""

    max_request_bytes: int = 1_048_576
    max_batch_size: int = 100
    max_depth: int = 64
    max_concurrency: int = 16
    reject_duplicate_members: bool = True


def _is_valid_id(value: object) -> bool:
    """Section 4: `id` is a String, Number, or Null; a Python `bool` is not a Number."""
    if value is None or isinstance(value, str):
        return True
    return isinstance(value, int | float) and not isinstance(value, bool)


def _detect_id(request: dict[str, Any]) -> Any:
    """Section 5: the response `id` echoes the request `id`, or is Null when undetectable."""
    value = request.get("id")
    return value if _is_valid_id(value) else None


def _is_valid_request(request: dict[str, Any]) -> bool:
    """Section 4: exact `jsonrpc`, string `method`, Structured `params` if present, typed `id`."""
    if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return False
    if "params" in request and not isinstance(request["params"], list | dict):
        return False
    return "id" not in request or _is_valid_id(request["id"])


def _render(payload: dict[str, Any]) -> str:
    """Compact, strict JSON: `allow_nan=False` makes NaN and Infinity an error, not bad JSON."""
    return json.dumps(payload, allow_nan=False, separators=(",", ":"))


def error_text(error: RpcError, request_id: Any) -> str:
    """Render an error response; a `data` value that cannot be serialized degrades to -32603."""
    body: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.data is not MISSING:
        body["data"] = error.data
    try:
        return _render({"jsonrpc": "2.0", "error": body, "id": request_id})
    except (TypeError, ValueError, RecursionError):
        fallback = {"code": INTERNAL_ERROR, "message": STANDARD_MESSAGES[INTERNAL_ERROR]}
        return _render({"jsonrpc": "2.0", "error": fallback, "id": request_id})


def standard_error(code: int, request_id: Any = None, data: Any = MISSING) -> str:
    return error_text(RpcError(code, STANDARD_MESSAGES[code], data), request_id)


def _as_batch(payload: object) -> list[Any] | None:
    return cast("list[Any] | None", payload) if isinstance(payload, list) else None


# The string branch always succeeds (an unterminated string runs to the end), so scanning is linear.
_STRING_OR_BRACKET = re.compile(r'"(?:[^"\\]|\\.?)*"?|[\[\]{}]', re.DOTALL)


def _nesting_depth(text: str) -> int:
    """Deepest bracket nesting outside string literals, counted without recursion."""
    depth = deepest = 0
    for token in _STRING_OR_BRACKET.findall(text):
        if token in ("[", "{"):
            depth += 1
            deepest = max(deepest, depth)
        elif token in ("]", "}"):
            depth -= 1
    return deepest


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON value (RFC 8259 Section 6)")


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} overflows a double")
    return value


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateMemberError(key)
        result[key] = value
    return result


class Dispatcher:
    """Registry of methods plus the JSON-RPC 2.0 processing model."""

    def __init__(self, limits: Limits | None = None) -> None:
        self.limits = limits or Limits()
        self._methods: dict[str, tuple[Handler, inspect.Signature]] = {}

    def register(self, name: str, handler: Handler) -> None:
        """Section 4: names starting with `rpc.` are reserved for the specification's extensions."""
        if name.startswith("rpc."):
            raise ValueError(f"{name!r} uses the reserved 'rpc.' prefix (Section 4)")
        self._methods[name] = (handler, inspect.signature(handler))

    def method(self, name: str) -> Callable[[Handler], Handler]:
        def decorate(handler: Handler) -> Handler:
            self.register(name, handler)
            return handler

        return decorate

    async def handle(self, raw: bytes | str) -> str | None:
        """Process one transport message; `None` means the specification says send nothing."""
        data = raw if isinstance(raw, bytes) else raw.encode("utf-8", errors="surrogatepass")
        if len(data) > self.limits.max_request_bytes:
            size = {"max_request_bytes": self.limits.max_request_bytes}
            return standard_error(INVALID_REQUEST, data=size)
        hook = _reject_duplicates if self.limits.reject_duplicate_members else None
        try:
            text = data.decode("utf-8")
            if _nesting_depth(text) > self.limits.max_depth:
                depth = {"max_depth": self.limits.max_depth}
                return standard_error(INVALID_REQUEST, data=depth)
            payload = json.loads(
                text,
                object_pairs_hook=hook,
                parse_constant=_reject_constant,
                parse_float=_parse_float,
            )
        except _DuplicateMemberError:
            return standard_error(INVALID_REQUEST)
        except (ValueError, RecursionError):
            return standard_error(PARSE_ERROR)
        gate = asyncio.Semaphore(self.limits.max_concurrency)
        batch = _as_batch(payload)
        if batch is None:
            return await self._dispatch_one(payload, gate)
        if not batch:
            return standard_error(INVALID_REQUEST)
        if len(batch) > self.limits.max_batch_size:
            limit = {"max_batch_size": self.limits.max_batch_size}
            return standard_error(INVALID_REQUEST, data=limit)
        parts = await asyncio.gather(*(self._dispatch_one(item, gate) for item in batch))
        replies = [part for part in parts if part is not None]
        return "[" + ",".join(replies) + "]" if replies else None

    async def _dispatch_one(self, item: Any, gate: asyncio.Semaphore) -> str | None:
        if not isinstance(item, dict):
            return standard_error(INVALID_REQUEST)
        request = cast("dict[str, Any]", item)
        request_id = _detect_id(request)
        if not _is_valid_request(request):
            return standard_error(INVALID_REQUEST, request_id)
        notification = "id" not in request
        error: RpcError
        try:
            result = await self._invoke(request, gate)
            if notification:
                return None
            return _render({"jsonrpc": "2.0", "result": result, "id": request_id})
        except RpcError as exc:
            error = self._checked(exc)
        except Exception:
            logger.exception("method %r failed", request["method"])
            error = RpcError(INTERNAL_ERROR, STANDARD_MESSAGES[INTERNAL_ERROR])
        return None if notification else error_text(error, request_id)

    async def _invoke(self, request: dict[str, Any], gate: asyncio.Semaphore) -> Any:
        entry = self._methods.get(request["method"])
        if entry is None:
            raise RpcError(METHOD_NOT_FOUND, STANDARD_MESSAGES[METHOD_NOT_FOUND])
        handler, signature = entry
        params = request.get("params")
        try:
            if isinstance(params, list):
                bound = signature.bind(*params)
            elif isinstance(params, dict):
                bound = signature.bind(**params)
            else:
                bound = signature.bind()
        except TypeError as exc:
            raise RpcError(INVALID_PARAMS, STANDARD_MESSAGES[INVALID_PARAMS]) from exc
        async with gate:
            value = handler(*bound.args, **bound.kwargs)
            if inspect.isawaitable(value):
                value = await value
        return value

    @staticmethod
    def _checked(error: RpcError) -> RpcError:
        """Section 5.1: handlers may not mint codes the specification reserves for itself."""
        code: Any = error.code
        message: Any = error.message
        internal = RpcError(INTERNAL_ERROR, STANDARD_MESSAGES[INTERNAL_ERROR])
        if not isinstance(code, int) or isinstance(code, bool) or not isinstance(message, str):
            return internal
        reserved = RESERVED_MIN <= code <= SERVER_ERROR_MAX
        allowed = code in STANDARD_MESSAGES or SERVER_ERROR_MIN <= code <= SERVER_ERROR_MAX
        return error if allowed or not reserved else internal


class BatchRejectedError(Exception):
    """The server answered a batch with one Response object instead of an Array (Section 6)."""

    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__(response)
        self.response = response


@dataclass(frozen=True, slots=True)
class Correlation:
    by_id: dict[RequestId, dict[str, Any]]
    missing: tuple[RequestId, ...]
    unmatched: tuple[dict[str, Any], ...]


def correlate(sent_ids: Sequence[RequestId], reply: str | None) -> Correlation:
    """Match a batch reply to the ids sent: order is irrelevant, `id` is the only join key."""
    if reply is None:
        return Correlation({}, tuple(sent_ids), ())
    decoded: Any = json.loads(reply)
    if isinstance(decoded, dict):
        raise BatchRejectedError(cast("dict[str, Any]", decoded))
    by_id: dict[RequestId, dict[str, Any]] = {}
    unmatched: list[dict[str, Any]] = []
    for response in decoded:
        response_id = response.get("id")
        if response_id in sent_ids and response_id not in by_id:
            by_id[response_id] = response
        else:
            unmatched.append(response)
    missing = tuple(sent_id for sent_id in sent_ids if sent_id not in by_id)
    return Correlation(by_id, missing, tuple(unmatched))


def build_app(dispatcher: Dispatcher) -> Starlette:
    """POST `/rpc`: JSON-RPC outcomes are `200`, silence is `204`, transport faults are `4xx`."""

    async def endpoint(request: Request) -> Response:
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return Response(status_code=415)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > dispatcher.limits.max_request_bytes:
                return Response(status_code=413)
        reply = await dispatcher.handle(bytes(body))
        if reply is None:
            return Response(status_code=204)
        return Response(reply, media_type="application/json")

    return Starlette(routes=[Route("/rpc", endpoint, methods=["POST"])])
