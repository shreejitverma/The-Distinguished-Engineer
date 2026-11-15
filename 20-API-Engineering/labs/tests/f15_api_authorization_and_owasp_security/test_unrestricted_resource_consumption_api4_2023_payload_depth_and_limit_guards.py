"""Lab for API4:2023 payload guards: byte caps, JSON shape limits, ReDoS-safe regexes."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import TYPE_CHECKING, Annotated, Any

import httpx
import orjson
import pydantic_core
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from api_labs.f15_api_authorization_and_owasp_security.payload_limits import (
    MIB,
    SKU_PATTERN,
    SKU_PATTERN_VULNERABLE,
    BodyLimitMiddleware,
    BodyTooLargeError,
    JsonShapeError,
    JsonShapeLimits,
    OrderIn,
    build_app,
    parse_json_guarded,
    scan_json_shape,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.requests import Request
    from starlette.types import Message, Receive, Scope, Send

CHUNK = 64 * 1024
LIMITS = JsonShapeLimits()


class BodyCounter:
    """ASGI app that drains the body and records how many receive() calls it made."""

    def __init__(self) -> None:
        self.receives = 0
        self.bytes = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        more = True
        while more:
            message = await receive()
            self.receives += 1
            self.bytes += len(message.get("body", b""))
            more = message.get("more_body", False)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(self.bytes).encode()})


def client(app: Any, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class ChunkSource:
    """Chunked request body; counts how many chunks the server actually pulled."""

    def __init__(self, chunks: int) -> None:
        self.total = chunks
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self.total):
            self.pulled += 1
            yield b"x" * CHUNK


# --- Layer 1: body byte cap -----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_declared_content_length_over_limit_is_rejected_before_any_receive() -> None:
    inner = BodyCounter()
    async with client(BodyLimitMiddleware(inner, max_bytes=MIB)) as c:
        response = await c.post("/", content=b"x" * (MIB + 1))
    assert response.status_code == 413
    assert response.headers["connection"] == "close"
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == f"declared {MIB + 1} bytes exceeds limit {MIB}"
    assert inner.receives == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chunked_upload_without_content_length_is_cut_at_the_crossing_chunk() -> None:
    inner = BodyCounter()
    source = ChunkSource(chunks=64)  # 4 MiB offered, no Content-Length
    async with client(BodyLimitMiddleware(inner, max_bytes=MIB)) as c:
        response = await c.post("/", content=source)
    assert response.request.headers["transfer-encoding"] == "chunked"
    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert response.json()["detail"] == f"streamed body exceeds limit {MIB}"
    # 16 chunks are exactly 1 MiB; the 17th crosses the cap and nothing more is read.
    assert source.pulled == 17
    assert inner.receives == 16


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("size", "status"), [(MIB, 200), (MIB + 1, 413)])
async def test_limit_is_inclusive_for_length_and_chunked_bodies(size: int, status: int) -> None:
    async def one_stream() -> AsyncIterator[bytes]:
        yield b"x" * size

    async with client(BodyLimitMiddleware(BodyCounter(), max_bytes=MIB)) as c:
        fixed = await c.post("/", content=b"x" * size)
        chunked = await c.post("/", content=one_stream())
    assert (fixed.status_code, chunked.status_code) == (status, status)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "detail"),
    [
        ([(b"content-length", b"10"), (b"content-length", b"11")], "conflicting"),
        ([(b"content-length", b"10, 11")], "conflicting"),
        ([(b"content-length", b"-1")], "not 1*DIGIT"),
        ([(b"content-length", b"0x10")], "not 1*DIGIT"),
    ],
)
async def test_invalid_content_length_is_a_400_not_a_bypass(
    headers: list[tuple[bytes, bytes]], detail: str
) -> None:
    inner = BodyCounter()
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    await BodyLimitMiddleware(inner, max_bytes=MIB)(scope, receive, send)
    assert sent[0]["status"] == 400
    assert detail in json.loads(sent[1]["body"])["detail"]
    assert inner.receives == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repeated_identical_content_length_is_accepted() -> None:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"abc", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    scope["headers"] = [(b"content-length", b"3, 3")]
    await BodyLimitMiddleware(BodyCounter(), max_bytes=MIB)(scope, receive, send)
    assert sent[0]["status"] == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_overflow_after_response_started_propagates_instead_of_a_second_start() -> None:
    async def streams_then_reads(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while (await receive()).get("more_body", False):
            pass

    async with client(BodyLimitMiddleware(streams_then_reads, max_bytes=MIB)) as c:
        with pytest.raises(BodyTooLargeError):
            await c.post("/", content=ChunkSource(chunks=32))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_builtin_body_limit_also_cuts_a_chunked_stream() -> None:
    async def echo_size(request: Request) -> PlainTextResponse:
        return PlainTextResponse(str(len(await request.body())))

    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=MIB)
    source = ChunkSource(chunks=64)
    async with client(app) as c:
        response = await c.post("/", content=source)
    assert response.status_code == 413
    assert source.pulled == 17
    # Starlette 1.10.0 does not ask the server to close the connection.
    assert "connection" not in response.headers


# --- Layer 2: JSON shape before parsing -----------------------------------------------


def nested(depth: int) -> bytes:
    return b"[" * depth + b"]" * depth


@pytest.mark.unit
def test_depth_limit_is_inclusive_and_reports_the_offending_offset() -> None:
    assert scan_json_shape(nested(32), LIMITS).max_depth == 32
    with pytest.raises(JsonShapeError) as info:
        scan_json_shape(nested(500_000), LIMITS)
    assert (info.value.limit, info.value.offset) == ("max_depth", 32)


@pytest.mark.unit
def test_brackets_and_escaped_quotes_inside_strings_do_not_count() -> None:
    doc = rb'{"a": "[[[[\\\"{{{{", "b": ["]]]", "\\\\"], "c": {"d": []}}'
    assert json.loads(doc)["a"] == '[[[[\\"{{{{'
    shape = scan_json_shape(doc, JsonShapeLimits(max_depth=3))
    assert shape.max_depth == 3
    assert shape.max_container_items == 3
    assert shape.total_values == 11


@pytest.mark.unit
@pytest.mark.parametrize(
    ("doc", "limit"),
    [
        (b"[" + b"0," * 1_000 + b"0]", "max_container_items"),
        (b"[" + b",".join([b"[" + b"0," * 998 + b"0]"] * 11) + b"]", "max_total_values"),
        (b'["' + b"a" * (64 * 1024 + 1) + b'"]', "max_string_bytes"),
        (b'{"' + b"k" * (64 * 1024 + 1) + b'": 1}', "max_string_bytes"),
        (b'{"n": ' + b"9" * 65 + b"}", "max_scalar_bytes"),
    ],
)
def test_width_count_string_and_scalar_limits(doc: bytes, limit: str) -> None:
    with pytest.raises(JsonShapeError) as info:
        scan_json_shape(doc, LIMITS)
    assert info.value.limit == limit


@pytest.mark.unit
def test_values_at_each_limit_are_accepted() -> None:
    ok = [
        b"[" + b"0," * 999 + b"0]",
        b'["' + b"a" * (64 * 1024) + b'"]',
        b'{"n": ' + b"9" * 64 + b"}",
    ]
    for doc in ok:
        scan_json_shape(doc, LIMITS)
        json.loads(doc)


@pytest.mark.unit
def test_stdlib_failures_on_hostile_json_are_not_json_decode_errors() -> None:
    with pytest.raises(RecursionError):
        json.loads(nested(400_000))
    assert not issubclass(RecursionError, ValueError)
    with pytest.raises(ValueError, match="Exceeds the limit") as info:
        json.loads(b"1" * 4_301)
    assert not isinstance(info.value, json.JSONDecodeError)
    with pytest.raises(JsonShapeError):
        parse_json_guarded(nested(400_000), LIMITS)


@pytest.mark.unit
def test_utf16_body_blinds_a_byte_scanner_so_the_guard_requires_utf8() -> None:
    # U+5C41 encodes as 41 5C in UTF-16-LE: the 5C looks like a backslash that
    # escapes the closing quote, so the scanner never leaves the string.
    doc = ('["\u5c41", ' + "[" * 400_000 + "]" * 400_000 + "]").encode("utf-16-le")
    assert scan_json_shape(doc, LIMITS).max_depth == 1
    with pytest.raises(RecursionError):
        json.loads(doc)  # bytes input: the stdlib sniffs UTF-16 and parses it
    # Every byte here is ASCII, so it decodes as UTF-8; parsing the decoded str
    # disables sniffing, and the NUL bytes are then plain invalid JSON.
    with pytest.raises(json.JSONDecodeError, match="Expecting value: line 1 column 2"):
        parse_json_guarded(doc, LIMITS)


@pytest.mark.unit
def test_rust_and_c_parsers_have_fixed_depth_limits() -> None:
    pydantic_core.from_json(nested(201))
    with pytest.raises(ValueError, match="recursion limit exceeded"):
        pydantic_core.from_json(nested(202))
    orjson.loads(nested(1_024))
    with pytest.raises(orjson.JSONDecodeError, match="depth limit exceeded"):
        orjson.loads(nested(1_025))


@pytest.mark.unit
def test_pydantic_list_max_length_runs_after_the_whole_array_is_parsed() -> None:
    class Capped(BaseModel):
        items: Annotated[list[int], Field(max_length=3)]

    with pytest.raises(ValidationError) as info:
        Capped.model_validate_json(b'{"items": [' + b"1," * 49_999 + b"1]}")
    error = info.value.errors()[0]
    assert error["type"] == "too_long"
    assert error.get("ctx") == {"field_type": "List", "max_length": 3, "actual_length": 50_000}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naive_fastapi_body_pays_the_full_parse_then_reports_400_or_echoes_422() -> None:
    as_json = {"content-type": "application/json"}
    wide = b"[" + b"0," * 400_000 + b"0]"  # valid JSON, wrong type for dict[str, Any]
    async with client(build_app(), raise_app_exceptions=False) as c:
        deep = await c.post("/naive", content=b'{"a": ' + nested(400_000) + b"}", headers=as_json)
        big_int = await c.post("/naive", content=b'{"a": ' + b"1" * 5_000 + b"}", headers=as_json)
        echoed = await c.post("/naive", content=wide, headers=as_json)
    # FastAPI 0.162.0 wraps any non-JSONDecodeError parse failure in a generic 400.
    for response in (deep, big_int):
        assert response.status_code == 400
        assert response.json() == {"detail": "There was an error parsing the body"}
    # The default 422 handler serializes the parsed input back to the caller.
    assert echoed.status_code == 422
    assert echoed.json()["detail"][0]["type"] == "dict_type"
    assert len(echoed.content) > len(wide)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_guarded_route_maps_every_hostile_body_to_a_4xx() -> None:
    line = b'{"sku": "AB-1", "quantity": 2}'
    async with client(build_app(max_body_bytes=MIB)) as c:
        ok = await c.post("/orders", content=b'{"lines": [' + line + b"]}")
        deep = await c.post("/orders", content=b'{"lines": ' + nested(400_000) + b"}")
        big_int = await c.post("/orders", content=b'{"lines": [' + b"1" * 5_000 + b"]}")
        wide = await c.post("/orders", content=b'{"lines": [' + b",".join([line] * 101) + b"]}")
        schema = await c.post("/orders", content=b'{"lines": [], "admin": true}')
        broken = await c.post("/orders", content=b'{"lines": ')
        huge = await c.post("/orders", content=b" " * (MIB + 1))
    assert ok.status_code == 201
    assert deep.json()["detail"] == "max_depth exceeded at byte 13"
    assert big_int.json()["detail"] == "max_scalar_bytes exceeded at byte 11"
    assert wide.json()["detail"].startswith("max_container_items exceeded")
    assert [r.status_code for r in (deep, big_int, wide, schema)] == [422] * 4
    assert schema.json()["detail"] == "2 errors"
    assert broken.status_code == 400
    assert huge.status_code == 413


# --- Layer 3: ReDoS -------------------------------------------------------------------


@pytest.mark.unit
@given(st.text(alphabet="ab _!\t", max_size=12))
def test_possessive_rewrite_accepts_exactly_the_same_strings(text: str) -> None:
    vulnerable = re.fullmatch(SKU_PATTERN_VULNERABLE, text) is not None
    safe = re.fullmatch(SKU_PATTERN, text) is not None
    assert vulnerable == safe


def run_match(pattern: str, subject_expr: str, timeout: float) -> bool:
    """Return True if the match finished within ``timeout`` seconds in a child process."""
    code = f"import re; re.match({pattern!r}, {subject_expr})"
    try:
        subprocess.run([sys.executable, "-I", "-c", code], check=True, timeout=timeout)  # noqa: S603
    except subprocess.TimeoutExpired:
        return False
    return True


@pytest.mark.unit
def test_nested_quantifier_backtracks_exponentially_but_possessive_does_not() -> None:
    attack = "'a' * 40 + '!'"  # 2**40 splits to try before giving up
    assert not run_match(SKU_PATTERN_VULNERABLE, attack, timeout=2.0)
    assert run_match(SKU_PATTERN, "'a' * 1_000_000 + '!'", timeout=10.0)


@pytest.mark.unit
def test_pydantic_rust_regex_is_linear_even_for_the_vulnerable_pattern() -> None:
    with pytest.raises(ValidationError) as info:
        OrderIn.model_validate({"note": "a" * 250 + "!", "lines": [{"sku": "A", "quantity": 1}]})
    assert info.value.errors()[0]["type"] == "string_pattern_mismatch"
    with pytest.raises(ValidationError) as too_long:
        OrderIn.model_validate({"note": "a" * 257, "lines": [{"sku": "A", "quantity": 1}]})
    # pydantic-core checks max_length before the pattern.
    assert too_long.value.errors()[0]["type"] == "string_too_long"


@pytest.mark.unit
def test_compiled_pattern_silently_switches_pydantic_to_backtracking_re() -> None:
    code = (
        "import re\n"
        "from pydantic import BaseModel, Field\n"
        "class M(BaseModel):\n"
        f"    v: str = Field(pattern=re.compile({SKU_PATTERN_VULNERABLE!r}))\n"
        "try:\n"
        "    M(v='a' * 40 + '!')\n"
        "except Exception:\n"
        "    pass\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run([sys.executable, "-I", "-c", code], check=True, timeout=3.0)  # noqa: S603
