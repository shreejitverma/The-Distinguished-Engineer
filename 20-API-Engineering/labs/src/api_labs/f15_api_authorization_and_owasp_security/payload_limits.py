"""API4:2023 payload guards: body byte cap, JSON shape scan, and ReDoS-safe patterns.

Three layers, cheapest first:

1. ``BodyLimitMiddleware`` (pure ASGI) rejects a declared ``Content-Length`` above the
   cap before the application reads a byte, and counts streamed bytes so a chunked
   upload (no ``Content-Length``) is cut at the first chunk that crosses the cap.
2. ``scan_json_shape`` walks the raw bytes once, without building objects, and rejects
   excessive nesting depth, container width, total value count, string length, and
   scalar (number) length before any parser allocates.
3. ``SKU_PATTERN`` is a regex whose possessive group cannot backtrack, and the
   ``OrderIn`` model checks ``max_length`` before its pattern (pydantic-core order).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

MIB: Final = 1024 * 1024
PROBLEM_JSON: Final = b"application/problem+json"


# --- Layer 1: request body byte cap ------------------------------------------------


class BodyTooLargeError(Exception):
    """Raised inside ``receive`` when the streamed body crosses the cap."""


def _problem(status: int, title: str, detail: str) -> bytes:
    payload = {"type": "about:blank", "title": title, "status": status, "detail": detail}
    return json.dumps(payload, separators=(",", ":")).encode()


async def _send_problem(send: Send, status: int, title: str, detail: str) -> None:
    body = _problem(status, title, detail)
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", PROBLEM_JSON),
                (b"content-length", str(len(body)).encode()),
                # Unread body bytes are still on the wire; closing stops the server
                # from draining (and keep-alive from reusing) a poisoned stream.
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


def declared_length(scope: Scope) -> int | None:
    """Return the single valid ``Content-Length`` value, ``None`` if absent.

    Raises ``ValueError`` for a non-digit value or conflicting duplicates
    (RFC 9112 Section 6.3, rule 5), which the middleware answers with ``400``.
    """
    values: list[bytes] = [v for k, v in scope["headers"] if k == b"content-length"]
    if not values:
        return None
    parts = {p.strip() for v in values for p in v.split(b",")}
    if len(parts) != 1:
        raise ValueError("conflicting Content-Length values")
    (only,) = parts
    if not only.isdigit():
        raise ValueError("Content-Length is not 1*DIGIT")
    return int(only)


class BodyLimitMiddleware:
    """Cap the request body at ``max_bytes`` whether or not it is length-delimited."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be >= 0")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            declared = declared_length(scope)
        except ValueError as exc:
            await _send_problem(send, 400, "Bad Request", str(exc))
            return
        if declared is not None and declared > self.max_bytes:
            # Decided from the header section alone: the app never runs.
            detail = f"declared {declared} bytes exceeds limit {self.max_bytes}"
            await _send_problem(send, 413, "Content Too Large", detail)
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLargeError:
            if response_started:
                raise  # Too late for a 413: let the server abort the connection.
            detail = f"streamed body exceeds limit {self.max_bytes}"
            await _send_problem(send, 413, "Content Too Large", detail)


# --- Layer 2: JSON shape scan before parsing -----------------------------------------


@dataclass(frozen=True, slots=True)
class JsonShapeLimits:
    max_depth: int = 32
    max_container_items: int = 1_000
    max_total_values: int = 10_000
    max_string_bytes: int = 64 * 1024
    max_scalar_bytes: int = 64


@dataclass(frozen=True, slots=True)
class JsonShape:
    max_depth: int
    max_container_items: int
    total_values: int


class JsonShapeError(ValueError):
    def __init__(self, limit: str, offset: int) -> None:
        super().__init__(f"{limit} exceeded at byte {offset}")
        self.limit = limit
        self.offset = offset


_STRUCTURAL: Final = re.compile(rb'[\[\]{}",:]')
_IN_STRING: Final = re.compile(rb'["\\]')
_WHITESPACE: Final = b" \t\r\n"


def scan_json_shape(data: bytes, limits: JsonShapeLimits) -> JsonShape:
    """Measure nesting, width, value count, and token sizes in one forward pass.

    The scan does not validate JSON grammar (the parser that runs next does); it only
    guarantees that parsing cannot exceed the limits.  It stops at the first violation,
    so rejected input costs at most the bytes up to that offset.
    """
    counts: list[int] = []  # items seen in each open container
    opened = False  # True right after "[" or "{" until the first token
    deepest = widest = total = 0
    pos = 0
    end = len(data)
    while True:
        match = _STRUCTURAL.search(data, pos)
        stop = match.start() if match else end
        scalar = data[pos:stop].strip(_WHITESPACE)
        if scalar:
            if len(scalar) > limits.max_scalar_bytes:
                raise JsonShapeError("max_scalar_bytes", pos)
            if opened:
                counts[-1] = 1
                opened = False
                widest = max(widest, 1)
            total += 1
            if total > limits.max_total_values:
                raise JsonShapeError("max_total_values", pos)
        if match is None:
            break
        byte = data[stop : stop + 1]
        if opened and byte not in (b"]", b"}"):
            counts[-1] = 1
            opened = False
            widest = max(widest, 1)
        if byte in (b"[", b"{"):
            total += 1
            if total > limits.max_total_values:
                raise JsonShapeError("max_total_values", stop)
            counts.append(0)
            opened = True
            deepest = max(deepest, len(counts))
            if len(counts) > limits.max_depth:
                raise JsonShapeError("max_depth", stop)
        elif byte in (b"]", b"}"):
            if counts:
                counts.pop()
            opened = False
        elif byte == b",":
            if counts:
                counts[-1] += 1
                widest = max(widest, counts[-1])
                if counts[-1] > limits.max_container_items:
                    raise JsonShapeError("max_container_items", stop)
        elif byte == b'"':
            total += 1  # Keys count too: each one is an allocated str.
            if total > limits.max_total_values:
                raise JsonShapeError("max_total_values", stop)
            pos = _skip_string(data, stop + 1, limits.max_string_bytes)
            continue
        pos = stop + 1
    return JsonShape(max_depth=deepest, max_container_items=widest, total_values=total)


def _skip_string(data: bytes, start: int, max_bytes: int) -> int:
    """Return the offset after the closing quote; enforce the raw string length."""
    pos = start
    while True:
        match = _IN_STRING.search(data, pos)
        if match is None:
            return len(data)  # Unterminated: the parser will reject it.
        if match.group() == b"\\":
            pos = match.start() + 2  # Skip the escaped byte, including \".
            continue
        if match.start() - start > max_bytes:
            raise JsonShapeError("max_string_bytes", start)
        return match.end()


def parse_json_guarded(data: bytes, limits: JsonShapeLimits) -> Any:
    """Decode strictly as UTF-8, scan, then parse the same text with the stdlib.

    ``json.loads(bytes)`` would sniff UTF-16 and UTF-32, where a code unit such as
    U+5C41 (bytes ``41 5C``) contains an ASCII backslash and blinds a byte scanner.
    Parsing the decoded ``str`` turns sniffing off, and UTF-8 never reuses ASCII bytes
    inside multibyte sequences (RFC 8259 Section 8.1 requires UTF-8), so the scan sees
    exactly the structure the parser sees.
    """
    text = data.decode("utf-8")  # UnicodeDecodeError is a ValueError -> 400
    scan_json_shape(data, limits)
    return json.loads(text)


# --- Layer 3: ReDoS-safe patterns and capped models ------------------------------------

# Catastrophic: nested quantifiers give 2**n ways to split a run of word characters.
SKU_PATTERN_VULNERABLE: Final = r"^(\w+\s?)*$"
# Same language: the possessive group commits to its first (greedy) parse, which is
# already the longest prefix it can match, so a failing suffix cannot trigger retries.
SKU_PATTERN: Final = r"^(?:\w+\s?)*+$"


class OrderLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: Annotated[str, Field(max_length=64, pattern=r"^[A-Z0-9][A-Z0-9-]{0,63}$")]
    quantity: Annotated[int, Field(ge=1, le=1_000)]


class OrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: Annotated[str, Field(max_length=256, pattern=SKU_PATTERN_VULNERABLE)] = ""
    lines: Annotated[list[OrderLine], Field(min_length=1, max_length=100)]


ORDER_LIMITS: Final = JsonShapeLimits(
    max_depth=4,
    max_container_items=100,
    max_total_values=1_000,
    max_string_bytes=1_024,
    max_scalar_bytes=16,
)


def build_app(max_body_bytes: int = MIB) -> FastAPI:
    """Guarded ``POST /orders`` next to an unguarded ``POST /naive`` for comparison."""
    app = FastAPI()

    @app.post("/naive")
    async def naive(payload: dict[str, Any]) -> dict[str, int]:
        return {"keys": len(payload)}

    @app.post("/orders", status_code=201)
    async def create_order(request: Request) -> JSONResponse:
        raw = await request.body()  # Bounded by BodyLimitMiddleware.
        try:
            document = parse_json_guarded(raw, ORDER_LIMITS)
        except JsonShapeError as exc:
            problem = _problem(422, "Unprocessable Content", str(exc))
            return JSONResponse(json.loads(problem), status_code=422)
        except ValueError as exc:  # JSONDecodeError and the int digit limit
            problem = _problem(400, "Bad Request", f"invalid JSON: {type(exc).__name__}")
            return JSONResponse(json.loads(problem), status_code=400)
        try:
            order = OrderIn.model_validate(document)
        except ValidationError as exc:
            problem = _problem(422, "Unprocessable Content", f"{exc.error_count()} errors")
            return JSONResponse(json.loads(problem), status_code=422)
        return JSONResponse({"lines": len(order.lines)}, status_code=201)

    app.add_middleware(BodyLimitMiddleware, max_bytes=max_body_bytes)
    return app
