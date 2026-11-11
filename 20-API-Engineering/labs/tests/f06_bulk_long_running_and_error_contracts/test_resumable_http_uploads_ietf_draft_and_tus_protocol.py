"""Lab: draft-ietf-httpbis-resumable-upload-15 creation, offset recovery, append, and 104."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import io
import itertools
import json
import re
import socket
from typing import TYPE_CHECKING, cast

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions
import h11
import httpcore
import httpx
import pytest
from starlette.requests import ClientDisconnect, Request

from api_labs.f06_bulk_long_running_and_error_contracts.resumable_upload import (
    INCONSISTENT_LENGTH,
    INTEROP_VERSION,
    MISMATCHING_OFFSET,
    PARTIAL_UPLOAD,
    InterimTracker,
    ResumableUploadService,
    SFDate,
    SFParseError,
    UploadAborted,
    UploadLimits,
    combined_field,
    h11_client_receive,
    h11_creation_exchange,
    interim_104,
    interim_fields,
    parse_dictionary,
    parse_item,
    parse_upload_limit,
    plan_resume,
    sf_boolean,
    sf_non_negative_integer,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from aioquic.h3.connection import H3Connection
    from aioquic.h3.events import H3Event
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.connection import QuicConnection
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

LIMITS = UploadLimits(max_size=1000, max_append_size=100)
DRAFT = {"Upload-Draft-Interop-Version": str(INTEROP_VERSION)}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def service(clock: FakeClock) -> ResumableUploadService:
    counter = itertools.count(1)
    return ResumableUploadService(LIMITS, clock=clock, new_id=lambda: f"u{next(counter)}")


def client_for(service: ResumableUploadService) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app), base_url="http://test")


async def create_empty(client: httpx.AsyncClient, **extra: str) -> str:
    response = await client.post("/files", headers={"Upload-Complete": "?0", **DRAFT, **extra})
    assert response.status_code == 201
    return response.headers["location"]


def append_headers(offset: int, *, complete: bool) -> dict[str, str]:
    return {
        "Upload-Offset": str(offset),
        "Upload-Complete": "?1" if complete else "?0",
        "Content-Type": PARTIAL_UPLOAD,
    }


# --- Raw ASGI driver for interruption and concurrency --------------------------------------------


class ScriptedReceive:
    """ASGI receive() fed by the test; `waiting` is set whenever the app blocks for more."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Message] = asyncio.Queue()
        self.waiting = asyncio.Event()

    async def __call__(self) -> Message:
        if self.queue.empty():
            self.waiting.set()
        message = await self.queue.get()
        self.waiting.clear()
        return message

    def chunk(self, body: bytes, *, more: bool = True) -> None:
        self.queue.put_nowait({"type": "http.request", "body": body, "more_body": more})

    def disconnect(self) -> None:
        self.queue.put_nowait({"type": "http.disconnect"})


def http_scope(method: str, path: str, headers: dict[str, str]) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }


async def run_asgi(
    service: ResumableUploadService,
    method: str,
    path: str,
    headers: dict[str, str],
    receive: ScriptedReceive,
) -> list[Message]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await service.app(http_scope(method, path, headers), receive, send)
    return sent


# --- Structured Fields ----------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("25000000", 25000000),
        ("0", 0),
        ("25000000;unknown=1", 25000000),
        ("  42", 42),
        ("999999999999999", 999999999999999),
        ("1000000000000000", None),
        ("?1", None),
        ("-1", None),
        ("1.5", None),
        ("0x10", None),
        ("abc", None),
        ("", None),
        ("5, 7", None),
        (":a:", None),
        (None, None),
    ],
    ids=[
        "integer",
        "zero",
        "unknown-param-ignored",
        "leading-sp",
        "15-digits",
        "16-digits",
        "boolean-not-int",
        "negative",
        "decimal",
        "hex",
        "token",
        "empty",
        "two-field-lines",
        "undecodable-byte-sequence",
        "absent",
    ],
)
def test_upload_offset_is_a_non_negative_sf_integer_or_ignored(
    raw: str | None, expected: int | None
) -> None:
    assert sf_non_negative_integer(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("?1", True), ("?0", False), ("?1;x=2", True), ("1", None), ("true", None), ("?2", None)],
)
def test_upload_complete_accepts_only_sf_boolean(raw: str, expected: bool | None) -> None:
    assert sf_boolean(raw) is expected


@pytest.mark.unit
def test_duplicate_field_lines_are_combined_before_parsing() -> None:
    joined = combined_field([("Upload-Offset", "5"), ("upload-offset", "7")], "upload-offset")
    assert joined == "5, 7"
    assert sf_non_negative_integer(joined) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("max-size=1000, max-age=60", UploadLimits(max_size=1000, max_age=60)),
        ("max-size=1000, x-vendor=(1 2);a=?0, y=:aGk=:", UploadLimits(max_size=1000)),
        ('min-size=0, note="hi", when=@1700000000', UploadLimits(min_size=0)),
        ("max-size=0", None),
        ("min-size=10, max-size=5", None),
        ("min-append-size=50, max-append-size=10", None),
        ("min-append-size=50, max-size=10", None),
        ("max-append-size=0", None),
        ("min-size=-1", None),
        ("min-append-size=-1", None),
        ("max-size=?1", None),
        ("max-age=-1", None),
        ("max-size=1.0", None),
        ("max-size=10,", None),
        ("max-size=10, max-size=20", UploadLimits(max_size=20)),
        ("Max-Size=10", None),
    ],
    ids=[
        "known",
        "unknown-members-ignored",
        "string-and-date-members",
        "zero-max-size",
        "min-above-max",
        "append-range",
        "min-append-above-max-size",
        "zero-max-append-size",
        "negative-min-size",
        "negative-min-append-size",
        "boolean",
        "negative-age",
        "decimal",
        "trailing-comma",
        "duplicate-last-wins",
        "uppercase-key",
    ],
)
def test_upload_limit_invalid_member_voids_whole_field(
    raw: str, expected: UploadLimits | None
) -> None:
    assert parse_upload_limit(raw) == expected


@pytest.mark.unit
def test_upload_limit_without_limits_serializes_min_size_zero() -> None:
    assert UploadLimits().serialize() == "min-size=0"
    assert parse_upload_limit(LIMITS.serialize()) == LIMITS


@pytest.mark.unit
def test_sf_parser_returns_typed_bare_items() -> None:
    assert parse_item('"a\\"b";q=1.5') == ('a"b', {"q": 1.5})
    assert parse_item("@1700000000") == (SFDate(1700000000), {})
    assert parse_item(":aGk=:") == (b"hi", {})
    # RFC 9651 Section 4.2.7: missing padding and non-zero pad bits do not fail parsing.
    assert parse_item(":aGk:") == (b"hi", {})
    assert parse_item(":aGl=:") == (b"hi", {})
    assert parse_item("-12.345") == (-12.345, {})
    assert parse_dictionary("a, b=?0;x, c=(1 tok)") == {
        "a": (True, {}),
        "b": (False, {"x": True}),
        "c": ([(1, {}), ("tok", {})], {}),
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        "\u00e9",
        "@1.5",
        "-",
        "1.2.3",
        "1234567890123.5",
        "12345678901.1234",
        "1.",
        '"a\\x"',
        '"tab\there"',
        '"open',
        ":aGk=",
        ":a$b:",
        ":a:",
        ":aG=k:",
        ":aGk==:",
        ":aGk=aGk=:",
        '%"display"',
        "1 2",
    ],
    ids=[
        "non-ascii",
        "decimal-date",
        "bare-minus",
        "two-dots",
        "13-digit-integer-part",
        "4-fraction-digits",
        "trailing-dot",
        "bad-escape",
        "control-char",
        "unterminated-string",
        "unterminated-bytes",
        "bad-base64",
        "undecodable-base64",
        "padding-inside-base64",
        "excess-base64-padding",
        "data-after-base64-padding",
        "display-string-unsupported",
        "trailing-garbage",
    ],
)
def test_sf_item_parser_rejects_malformed_values(raw: str) -> None:
    with pytest.raises(SFParseError):
        parse_item(raw)


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    ["\u00e9", '"caf\u00e9"', "tok\u00e9n", "1\u0663", "?1;\u00e9"],
    ids=["bare", "in-string", "in-token", "arabic-digit", "in-parameter"],
)
def test_sf_parser_rejects_non_ascii_before_looking_at_the_item_type(raw: str) -> None:
    with pytest.raises(SFParseError, match="not ASCII"):
        parse_item(raw)
    assert sf_non_negative_integer(raw) is None
    assert sf_boolean(raw) is None


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["a=(1 2", "a=(1,2)", "a=1;b=2 c=3", "A=1", "a=1,,b=2"])
def test_sf_dictionary_parser_rejects_malformed_values(raw: str) -> None:
    with pytest.raises(SFParseError):
        parse_dictionary(raw)


# --- Upload creation, offset retrieval, append, cancellation ------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_options_announces_limits(service: ResumableUploadService) -> None:
    async with client_for(service) as client:
        response = await client.options("/files")
    assert response.status_code == 204
    assert parse_upload_limit(response.headers["upload-limit"]) == LIMITS


@pytest.mark.unit
@pytest.mark.asyncio
async def test_careful_creation_returns_upload_resource(service: ResumableUploadService) -> None:
    async with client_for(service) as client:
        response = await client.post("/files", headers={"Upload-Complete": "?0", **DRAFT})
    assert response.status_code == 201
    assert response.headers["location"] == "/uploads/u1"
    assert response.headers["upload-complete"] == "?0"
    assert response.headers["upload-offset"] == "0"
    assert parse_upload_limit(response.headers["upload-limit"]) == UploadLimits(
        max_size=1000, max_append_size=100, max_age=3600
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_optimistic_creation_completes_in_one_request(
    service: ResumableUploadService,
) -> None:
    data = b"x" * 80
    async with client_for(service) as client:
        response = await client.post(
            "/files", content=data, headers={"Upload-Complete": "?1", **DRAFT}
        )
    assert response.status_code == 201
    assert response.headers["upload-complete"] == "?1"
    assert response.headers["location"] == "/files/u1"
    assert response.json()["sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_creation_with_content_and_upload_complete_false_keeps_the_content(
    service: ResumableUploadService,
) -> None:
    data = b"0123456789" * 6
    async with client_for(service) as client:
        created = await client.post(
            "/files", content=data[:30], headers={"Upload-Complete": "?0", **DRAFT}
        )
        head = await client.head(created.headers["location"])
        done = await client.patch(
            created.headers["location"],
            content=data[30:],
            headers=append_headers(30, complete=True),
        )
    assert created.status_code == 201
    assert created.headers["location"] == "/uploads/u1"
    assert (created.headers["upload-complete"], created.headers["upload-offset"]) == ("?0", "30")
    assert (head.headers["upload-complete"], head.headers["upload-offset"]) == ("?0", "30")
    assert "upload-length" not in head.headers  # ?0 plus Content-Length declares no length
    assert done.status_code == 201
    assert done.json()["sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_interrupted_creation_keeps_received_prefix_and_is_not_complete(
    service: ResumableUploadService,
) -> None:
    data = b"resumable-" * 9
    async with client_for(service) as client:
        receive = ScriptedReceive()
        receive.chunk(data[:37])
        receive.disconnect()
        headers = {"Upload-Complete": "?1", **DRAFT}
        sent = await run_asgi(service, "POST", "/files", headers, receive)
        head = await client.head("/uploads/u1")  # the URI a 104 would have announced
        plan = plan_resume(head.headers, data)
        done = await client.patch("/uploads/u1", content=plan.content, headers=plan.headers)
    assert sent == []
    assert (head.headers["upload-complete"], head.headers["upload-offset"]) == ("?0", "37")
    assert done.status_code == 201
    assert done.headers["upload-complete"] == "?1"
    assert done.json()["sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_without_upload_complete_falls_back_to_conventional(
    service: ResumableUploadService,
) -> None:
    async with client_for(service) as client:
        response = await client.post("/files", content=b"plain")
    assert response.status_code == 201
    assert "upload-complete" not in response.headers
    assert service.uploads == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_head_reports_offset_length_limits_and_no_store(
    service: ResumableUploadService, clock: FakeClock
) -> None:
    async with client_for(service) as client:
        url = await create_empty(client, **{"Upload-Length": "300"})
        await client.patch(url, content=b"a" * 40, headers=append_headers(0, complete=False))
        clock.now += 600
        head = await client.head(url)
    assert head.status_code == 204
    assert head.headers["upload-offset"] == "40"
    assert head.headers["upload-length"] == "300"
    assert head.headers["upload-complete"] == "?0"
    assert head.headers["cache-control"] == "no-store"
    assert parse_upload_limit(head.headers["upload-limit"]) == UploadLimits(
        max_size=1000, max_append_size=100, max_age=3000
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expired_upload_resource_is_gone(
    service: ResumableUploadService, clock: FakeClock
) -> None:
    async with client_for(service) as client:
        url = await create_empty(client)
        clock.now += 3600
        head = await client.head(url)
    assert head.status_code == 404
    assert service.uploads == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mismatched_offset_is_409_with_problem_details(
    service: ResumableUploadService,
) -> None:
    async with client_for(service) as client:
        url = await create_empty(client)
        await client.patch(url, content=b"a" * 10, headers=append_headers(0, complete=False))
        response = await client.patch(url, content=b"b", headers=append_headers(25, complete=False))
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["upload-offset"] == "10"
    assert response.headers["upload-complete"] == "?0"
    assert response.json() == {
        "type": MISMATCHING_OFFSET,
        "title": "offset from request does not match offset of resource",
        "expected-offset": 10,
        "provided-offset": 25,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_append_requires_partial_upload_media_type(
    service: ResumableUploadService,
) -> None:
    headers = append_headers(0, complete=False) | {"Content-Type": "application/octet-stream"}
    async with client_for(service) as client:
        url = await create_empty(client)
        response = await client.patch(url, content=b"a", headers=headers)
        head = await client.head(url)
    assert response.status_code == 415
    assert response.headers["accept-patch"] == PARTIAL_UPLOAD
    assert head.headers["upload-offset"] == "0"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_append_with_content_encoding_is_415_and_incomplete(
    service: ResumableUploadService,
) -> None:
    headers = append_headers(0, complete=False) | {"Content-Encoding": "gzip"}
    async with client_for(service) as client:
        url = await create_empty(client)
        response = await client.patch(url, content=b"a", headers=headers)
    assert response.status_code == 415
    assert response.headers["upload-complete"] == "?0"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"Upload-Offset": "?1"},
        {"Upload-Offset": "-1"},
        {"Upload-Offset": ":a:"},
        {"Upload-Complete": "1"},
        {"Upload-Complete": "true"},
    ],
    ids=[
        "offset-boolean",
        "offset-negative",
        "offset-undecodable-bytes",
        "complete-integer",
        "complete-token",
    ],
)
async def test_append_without_valid_offset_or_complete_is_400(
    service: ResumableUploadService, override: dict[str, str]
) -> None:
    async with client_for(service) as client:
        url = await create_empty(client)
        response = await client.patch(
            url, content=b"a", headers=append_headers(0, complete=False) | override
        )
    assert response.status_code == 400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inconsistent_length_indicators_are_400_problem(
    service: ResumableUploadService,
) -> None:
    async with client_for(service) as client:
        created = await client.post(
            "/files", content=b"abc", headers={"Upload-Complete": "?1", "Upload-Length": "5"}
        )
        url = await create_empty(client, **{"Upload-Length": "50"})
        changed = await client.patch(
            url,
            content=b"a",
            headers=append_headers(0, complete=False) | {"Upload-Length": "60"},
        )
    for response in (created, changed):
        assert response.status_code == 400
        assert response.json()["type"] == INCONSISTENT_LENGTH
        assert response.headers["upload-complete"] == "?0"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_completing_short_of_declared_length_is_inconsistent(
    service: ResumableUploadService,
) -> None:
    async def body() -> AsyncIterator[bytes]:
        yield b"0123"

    async with client_for(service) as client:
        url = await create_empty(client, **{"Upload-Length": "10"})
        sized = await client.patch(url, content=b"0123", headers=append_headers(0, complete=True))
        untouched = await client.head(url)
        chunked = await client.patch(url, content=body(), headers=append_headers(0, complete=True))
        kept = await client.head(url)
    assert "content-length" not in chunked.request.headers
    for response in (sized, chunked):
        assert response.status_code == 400
        assert response.json()["type"] == INCONSISTENT_LENGTH
        assert response.headers["upload-complete"] == "?0"
    assert untouched.headers["upload-offset"] == "0"  # Content-Length: rejected before any byte
    assert (kept.headers["upload-offset"], kept.headers["upload-complete"]) == ("4", "?0")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_length_first_declared_on_an_append_is_recorded(
    service: ResumableUploadService,
) -> None:
    async with client_for(service) as client:
        url = await create_empty(client)
        before = await client.head(url)
        declared = await client.patch(
            url,
            content=b"a" * 10,
            headers=append_headers(0, complete=False) | {"Upload-Length": "20"},
        )
        head = await client.head(url)
        changed = await client.patch(
            url,
            content=b"a",
            headers=append_headers(10, complete=False) | {"Upload-Length": "25"},
        )
    assert "upload-length" not in before.headers
    assert declared.status_code == 204
    assert head.headers["upload-length"] == "20"
    assert changed.status_code == 400
    assert changed.json()["type"] == INCONSISTENT_LENGTH


@pytest.mark.unit
@pytest.mark.asyncio
async def test_append_past_known_length_deactivates_upload(
    service: ResumableUploadService,
) -> None:
    async def body() -> AsyncIterator[bytes]:
        yield b"0123456789"
        yield b"X"

    async with client_for(service) as client:
        url = await create_empty(client, **{"Upload-Length": "10"})
        response = await client.patch(
            url, content=body(), headers=append_headers(0, complete=False)
        )
        after = await client.head(url)
    assert "content-length" not in response.request.headers
    assert response.status_code == 400
    assert response.json()["type"] == INCONSISTENT_LENGTH
    assert after.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_append_over_max_append_size_is_413_with_limits(
    service: ResumableUploadService,
) -> None:
    async with client_for(service) as client:
        url = await create_empty(client)
        response = await client.patch(
            url, content=b"a" * 101, headers=append_headers(0, complete=False)
        )
        retry = await client.patch(
            url, content=b"a" * 100, headers=append_headers(0, complete=False)
        )
    assert response.status_code == 413
    assert parse_upload_limit(response.headers["upload-limit"]) is not None
    assert response.headers["upload-offset"] == "0"
    assert retry.status_code == 204
    assert retry.headers["upload-offset"] == "100"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_creation_declaring_length_over_max_size_is_413(
    service: ResumableUploadService,
) -> None:
    async with client_for(service) as client:
        response = await client.post(
            "/files", headers={"Upload-Complete": "?0", "Upload-Length": "1001"}
        )
    assert response.status_code == 413
    assert service.uploads == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_appends_cannot_accumulate_past_max_size(clock: FakeClock) -> None:
    limits = UploadLimits(max_size=50, max_append_size=30)
    small = ResumableUploadService(limits, clock=clock, new_id=lambda: "u1")
    async with client_for(small) as client:
        url = await create_empty(client)
        first = await client.patch(
            url, content=b"a" * 30, headers=append_headers(0, complete=False)
        )
        second = await client.patch(
            url, content=b"b" * 30, headers=append_headers(30, complete=False)
        )
        after = await client.head(url)
    assert first.status_code == 204
    assert second.status_code == 413  # each append fits max-append-size; the total does not
    assert parse_upload_limit(second.headers["upload-limit"]) == UploadLimits(
        max_size=50, max_append_size=30, max_age=3600
    )
    assert after.status_code == 404
    assert small.uploads == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_series_of_appends_then_complete_returns_target_response(
    service: ResumableUploadService,
) -> None:
    data = bytes(range(250))
    async with client_for(service) as client:
        url = await create_empty(client)
        offset = 0
        for start in range(0, 200, 100):
            part = await client.patch(
                url,
                content=data[start : start + 100],
                headers=append_headers(offset, complete=False),
            )
            assert part.status_code == 204
            assert part.headers["upload-complete"] == "?0"
            offset = int(part.headers["upload-offset"])
        last = await client.patch(
            url, content=data[200:], headers=append_headers(200, complete=True)
        )
    assert last.status_code == 201
    assert last.headers["upload-complete"] == "?1"
    assert last.json() == {
        "attachmentId": "u1",
        "length": 250,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_interrupted_append_keeps_received_prefix_and_resume_completes(
    service: ResumableUploadService,
) -> None:
    data = b"resumable-" * 9
    async with client_for(service) as client:
        url = await create_empty(client)
        receive = ScriptedReceive()
        receive.chunk(data[:37])
        receive.disconnect()
        sent = await run_asgi(service, "PATCH", url, append_headers(0, complete=True), receive)
        head = await client.head(url)
        plan = plan_resume(head.headers, data)
        done = await client.patch(url, content=plan.content, headers=plan.headers)
    assert sent == []
    assert head.headers["upload-offset"] == "37"
    assert plan.offset == 37
    assert done.status_code == 201
    assert done.json()["sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_buffering_the_whole_body_loses_the_prefix_on_disconnect(clock: FakeClock) -> None:
    buffered = ResumableUploadService(LIMITS, clock=clock, new_id=lambda: "u1", buffer_body=True)
    async with client_for(buffered) as client:
        url = await create_empty(client)
        receive = ScriptedReceive()
        receive.chunk(b"a" * 37)
        receive.disconnect()
        await run_asgi(buffered, "PATCH", url, append_headers(0, complete=True), receive)
        head = await client.head(url)
    assert head.headers["upload-offset"] == "0"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_request_preempts_stale_append_and_sees_current_offset(
    service: ResumableUploadService,
) -> None:
    async with client_for(service) as client:
        url = await create_empty(client)
        receive = ScriptedReceive()
        stale = asyncio.create_task(
            run_asgi(service, "PATCH", url, append_headers(0, complete=False), receive)
        )
        receive.chunk(b"a" * 30)
        await asyncio.wait_for(receive.waiting.wait(), timeout=2)
        head = await asyncio.wait_for(client.head(url), timeout=2)  # a lock alone would hang here
        receive.chunk(b"b" * 30)  # late bytes from the attempt the client gave up on
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stale, timeout=2)
        again = await client.head(url)
        resumed = await client.patch(
            url, content=b"c" * 10, headers=append_headers(30, complete=False)
        )
    assert head.headers["upload-offset"] == "30"
    assert again.headers["upload-offset"] == "30"
    assert resumed.status_code == 204
    assert resumed.headers["upload-offset"] == "40"
    assert bytes(service.uploads["u1"].data) == b"a" * 30 + b"c" * 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_append_preempts_stale_append_without_an_offset_retrieval(
    service: ResumableUploadService,
) -> None:
    async with client_for(service) as client:
        url = await create_empty(client)
        receive = ScriptedReceive()
        stale = asyncio.create_task(
            run_asgi(service, "PATCH", url, append_headers(0, complete=False), receive)
        )
        receive.chunk(b"a" * 30)
        await asyncio.wait_for(receive.waiting.wait(), timeout=2)
        resumed = await asyncio.wait_for(
            client.patch(url, content=b"c" * 10, headers=append_headers(30, complete=False)),
            timeout=2,
        )
        receive.chunk(b"b" * 30)  # late bytes must not land behind the newer append
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stale, timeout=2)
        head = await client.head(url)
    assert resumed.status_code == 204
    assert resumed.headers["upload-offset"] == "40"
    assert head.headers["upload-offset"] == "40"
    assert bytes(service.uploads["u1"].data) == b"a" * 30 + b"c" * 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preempting_append_stays_registered_and_is_preempted_in_turn(
    service: ResumableUploadService,
) -> None:
    async with client_for(service) as client:
        url = await create_empty(client)
        first, second = ScriptedReceive(), ScriptedReceive()
        stale = asyncio.create_task(
            run_asgi(service, "PATCH", url, append_headers(0, complete=False), first)
        )
        first.chunk(b"a" * 30)
        await asyncio.wait_for(first.waiting.wait(), timeout=2)
        newer = asyncio.create_task(
            run_asgi(service, "PATCH", url, append_headers(30, complete=False), second)
        )
        second.chunk(b"b" * 10)
        await asyncio.wait_for(second.waiting.wait(), timeout=2)
        # The first writer's cleanup must not erase the second writer's registration.
        head = await asyncio.wait_for(client.head(url), timeout=2)
        second.chunk(b"c" * 10)  # late bytes from the second attempt
        for task in (stale, newer):
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        again = await client.head(url)
    assert head.headers["upload-offset"] == "40"
    assert again.headers["upload-offset"] == "40"
    assert bytes(service.uploads["u1"].data) == b"a" * 30 + b"b" * 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_appends_waiting_behind_one_stale_writer_do_not_both_write(
    service: ResumableUploadService,
) -> None:
    async with client_for(service) as client:
        url = await create_empty(client)
        first, second, third = ScriptedReceive(), ScriptedReceive(), ScriptedReceive()
        stale = asyncio.create_task(
            run_asgi(service, "PATCH", url, append_headers(0, complete=False), first)
        )
        first.chunk(b"a" * 30)
        await asyncio.wait_for(first.waiting.wait(), timeout=2)
        # Both start before the stale writer has finished unwinding, so both wait for it.
        middle = asyncio.create_task(
            run_asgi(service, "PATCH", url, append_headers(30, complete=False), second)
        )
        third.chunk(b"c" * 10, more=False)
        newest = asyncio.create_task(
            run_asgi(service, "PATCH", url, append_headers(30, complete=False), third)
        )
        sent = await asyncio.wait_for(newest, timeout=2)
        second.chunk(b"b" * 10)  # late bytes from the request that lost its turn
        for task in (stale, middle):
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        head = await client.head(url)
    assert sent[0]["status"] == 204
    assert (b"upload-offset", b"40") in sent[0]["headers"]
    assert head.headers["upload-offset"] == "40"
    assert bytes(service.uploads["u1"].data) == b"a" * 30 + b"c" * 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_append_that_waited_behind_a_delete_finds_the_upload_gone(
    service: ResumableUploadService,
) -> None:
    async with client_for(service) as client:
        url = await create_empty(client)
        first, unused, late = ScriptedReceive(), ScriptedReceive(), ScriptedReceive()
        stale = asyncio.create_task(
            run_asgi(service, "PATCH", url, append_headers(0, complete=False), first)
        )
        first.chunk(b"a" * 30)
        await asyncio.wait_for(first.waiting.wait(), timeout=2)
        # Both wait for the stale writer; the DELETE is first in line and removes the upload.
        deleting = asyncio.create_task(run_asgi(service, "DELETE", url, {}, unused))
        late.chunk(b"b" * 10, more=False)
        appending = asyncio.create_task(
            run_asgi(service, "PATCH", url, append_headers(30, complete=False), late)
        )
        deleted = await asyncio.wait_for(deleting, timeout=2)
        appended = await asyncio.wait_for(appending, timeout=2)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stale, timeout=2)
    assert deleted[0]["status"] == 204
    assert appended[0]["status"] == 404  # not a 409 against the emptied buffer, not a 204
    assert service.uploads == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_cancels_in_flight_append_and_releases_resource(
    service: ResumableUploadService,
) -> None:
    async with client_for(service) as client:
        url = await create_empty(client)
        receive = ScriptedReceive()
        writer = asyncio.create_task(
            run_asgi(service, "PATCH", url, append_headers(0, complete=False), receive)
        )
        receive.chunk(b"a" * 5)
        await asyncio.wait_for(receive.waiting.wait(), timeout=2)
        deleted = await asyncio.wait_for(client.delete(url), timeout=2)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(writer, timeout=2)
        after = await client.head(url)
        patch = await client.patch(url, content=b"a", headers=append_headers(5, complete=False))
        repeated = await client.delete(url)
    assert deleted.status_code == 204
    assert after.status_code == 404
    assert patch.status_code == 404
    assert repeated.status_code == 404
    assert service.uploads == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lost_final_response_is_replayed(service: ResumableUploadService) -> None:
    async with client_for(service) as client:
        url = await create_empty(client)
        first = await client.patch(url, content=b"abc", headers=append_headers(0, complete=True))
        head = await client.head(url)
        replay = await client.patch(url, content=b"", headers=append_headers(3, complete=True))
        extra = await client.patch(url, content=b"d", headers=append_headers(3, complete=False))
        again = await client.patch(url, content=b"", headers=append_headers(3, complete=True))
    assert head.headers["upload-complete"] == "?1"
    assert (replay.status_code, replay.content) == (first.status_code, first.content)
    assert replay.headers["upload-complete"] == "?1"
    assert extra.status_code == 400
    assert extra.headers["upload-complete"] == "?0"
    assert (again.status_code, again.content) == (first.status_code, first.content)  # still there
    with pytest.raises(UploadAborted, match="already complete"):
        plan_resume(head.headers, b"abc")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authorization_is_rechecked_before_completion(clock: FakeClock) -> None:
    allowed = {"u1": True}
    guarded = ResumableUploadService(
        LIMITS, clock=clock, new_id=lambda: "u1", may_complete=lambda uid: allowed[uid]
    )
    async with client_for(guarded) as client:
        url = await create_empty(client)
        allowed["u1"] = False  # access revoked while the upload was in progress
        response = await client.patch(url, content=b"abc", headers=append_headers(0, complete=True))
    assert response.status_code == 403
    assert response.headers["upload-complete"] == "?1"


@pytest.mark.unit
def test_plan_resume_backtracks_and_refuses_offsets_beyond_local_data() -> None:
    data = b"0123456789"
    plan = plan_resume({"upload-offset": "4", "upload-complete": "?0"}, data)
    assert (plan.offset, plan.content) == (4, b"456789")
    assert plan.headers["Upload-Offset"] == "4"
    with pytest.raises(UploadAborted, match="beyond local data"):
        plan_resume({"upload-offset": "11"}, data)
    with pytest.raises(UploadAborted, match="no valid Upload-Offset"):
        plan_resume({"upload-offset": "?1"}, data)


# --- 104 on the HTTP/1.1 wire ---------------------------------------------------------------------

REQUEST_HEAD = (
    b"POST /files HTTP/1.1\r\n"
    b"Host: example.test\r\n"
    b"Content-Length: 10\r\n"
    b"Upload-Complete: ?1\r\n"
    b"Upload-Draft-Interop-Version: 10\r\n"
    b"\r\n"
)


@pytest.mark.unit
def test_creation_on_the_wire_sends_104_location_then_progress_then_final() -> None:
    wire = h11_creation_exchange(
        [REQUEST_HEAD + b"01234", b"56789"], upload_id="abc", limits=UploadLimits(max_size=1000)
    )
    interim, _, final = wire.partition(b"HTTP/1.1 201 Created\r\n")
    assert interim == (
        b"HTTP/1.1 104 Upload Resumption Supported\r\n"
        b"Upload-Draft-Interop-Version: 10\r\n"
        b"Location: /uploads/abc\r\n"
        b"Upload-Limit: max-size=1000\r\n"
        b"\r\n"
        b"HTTP/1.1 104 Upload Resumption Supported\r\n"
        b"Upload-Draft-Interop-Version: 10\r\n"
        b"Upload-Offset: 5\r\n"
        b"\r\n"
    )
    assert final.startswith(b"Upload-Complete: ?1\r\nLocation: /files/abc\r\n")


@pytest.mark.unit
def test_wire_exchange_without_interop_version_sends_only_the_final_response() -> None:
    head = REQUEST_HEAD.replace(b"Upload-Draft-Interop-Version: 10\r\n", b"")
    wire = h11_creation_exchange([head + b"01234", b"56789"], upload_id="abc", limits=LIMITS)
    assert head != REQUEST_HEAD
    assert wire.startswith(b"HTTP/1.1 201 Created\r\nUpload-Complete: ?1\r\n")
    assert b" 104 " not in wire


@pytest.mark.unit
def test_wire_creation_with_upload_complete_false_points_at_the_upload_resource() -> None:
    head = REQUEST_HEAD.replace(b"Upload-Complete: ?1", b"Upload-Complete: ?0")
    wire = h11_creation_exchange([head + b"0123456789"], upload_id="abc", limits=LIMITS)
    _, _, final = wire.partition(b"HTTP/1.1 201 Created\r\n")
    assert head != REQUEST_HEAD
    assert final.startswith(b"Upload-Complete: ?0\r\nLocation: /uploads/abc\r\n")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("version_header", "http_version"),
    [(None, b"1.1"), (b"9", b"1.1"), (b"?1", b"1.1"), (b"10", b"1.0")],
    ids=["missing", "older-draft", "boolean", "http-1.0-client"],
)
def test_no_104_without_matching_interop_version_or_on_http_1_0(
    version_header: bytes | None, http_version: bytes
) -> None:
    headers = [(b"host", b"example.test"), (b"upload-complete", b"?1")]
    if version_header is not None:
        headers.append((b"upload-draft-interop-version", version_header))
    request = h11.Request(
        method="POST", target="/files", headers=headers, http_version=http_version
    )
    assert interim_104(request, location="/uploads/abc") is None
    matching = h11.Request(
        method="POST",
        target="/files",
        headers=[*headers[:2], (b"upload-draft-interop-version", b"10")],
    )
    assert interim_104(matching, location="/uploads/abc") is not None


def _client_request() -> h11.Request:
    headers = [
        ("Host", "example.test"),
        ("Content-Length", "10"),
        ("Upload-Complete", "?1"),
        ("Upload-Draft-Interop-Version", "10"),
    ]
    return h11.Request(method="POST", target="/files", headers=headers)


@pytest.mark.unit
def test_client_learns_location_and_offset_from_104() -> None:
    wire = h11_creation_exchange(
        [REQUEST_HEAD + b"01234", b"56789"], upload_id="abc", limits=UploadLimits(max_size=1000)
    )
    tracker, status = h11_client_receive(_client_request(), b"0123456789", wire)
    assert (tracker.location, tracker.offset, status) == ("/uploads/abc", 5, 201)
    assert tracker.limits == UploadLimits(max_size=1000)


@pytest.mark.unit
def test_client_ignores_mismatched_104_and_aborts_on_location_change() -> None:
    head = b"HTTP/1.1 104 Upload Resumption Supported\r\nUpload-Draft-Interop-Version: "
    old_draft = head + b"6\r\nLocation: /evil\r\n\r\n"
    good = head + b"10\r\nLocation: /uploads/a\r\n\r\n"
    moved = head + b"10\r\nLocation: /uploads/b\r\n\r\n"
    final = b"HTTP/1.1 201 Created\r\nContent-Length: 0\r\n\r\n"
    tracker, status = h11_client_receive(_client_request(), b"0123456789", old_draft + good + final)
    assert (tracker.ignored, tracker.location, status) == (1, "/uploads/a", 201)
    with pytest.raises(UploadAborted, match="URI changed"):
        h11_client_receive(_client_request(), b"0123456789", good + moved + final)


@pytest.mark.unit
def test_h11_cannot_send_104_as_a_final_response() -> None:
    with pytest.raises(h11.LocalProtocolError, match=r"range \[200, 1000\)"):
        h11.Response(status_code=104, headers=[])
    assert h11.InformationalResponse(status_code=104, headers=[]).status_code == 104
    assert h11.Response(status_code=201, headers=[]).status_code == 201


class _ReplaySocket:
    def __init__(self, wire: bytes) -> None:
        self.wire = wire

    def makefile(self, mode: str) -> io.BufferedReader:
        del mode
        return io.BufferedReader(io.BytesIO(self.wire))


@pytest.mark.unit
def test_stdlib_http_client_returns_104_as_the_final_response() -> None:
    wire = h11_creation_exchange([REQUEST_HEAD + b"0123456789"], upload_id="abc", limits=LIMITS)
    response = http.client.HTTPResponse(cast("socket.socket", _ReplaySocket(wire)), method="POST")
    response.begin()
    assert response.status == 104
    assert response.getheader("Location") == "/uploads/abc"
    assert response.read() == b""


@pytest.mark.unit
def test_httpcore_discards_104_and_its_location() -> None:
    wire = h11_creation_exchange([REQUEST_HEAD + b"0123456789"], upload_id="abc", limits=LIMITS)
    with httpcore.ConnectionPool(network_backend=httpcore.MockBackend([wire])) as pool:
        response = pool.request(
            "POST",
            "http://example.test/files",
            headers={"Upload-Complete": "?1", "Upload-Draft-Interop-Version": "10"},
            content=b"0123456789",
        )
    assert wire.startswith(b"HTTP/1.1 104 ")  # the 104 and its Location were on the wire
    assert b"Location: /uploads/abc\r\n" in wire
    assert response.status == 201
    assert dict(response.headers)[b"Location"] == b"/files/abc"
    assert b"/uploads/abc" not in b"".join(value for _name, value in response.headers)
    assert json.loads(response.content) == {"attachmentId": "abc", "length": 10}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_body_raises_client_disconnect() -> None:
    receive = ScriptedReceive()
    receive.chunk(b"partial")
    receive.disconnect()
    request = Request(http_scope("POST", "/", {}), receive)
    with pytest.raises(ClientDisconnect):
        await request.body()


@pytest.mark.unit
def test_tracker_aborts_when_offset_decreases_and_skips_other_1xx() -> None:
    tracker = InterimTracker()
    tracker.observe(103, [(b"link", b"</a.css>; rel=preload")])
    version = (b"upload-draft-interop-version", b"10")
    tracker.observe(104, [version, (b"upload-offset", b"50")])
    with pytest.raises(UploadAborted, match="offset decreased"):
        tracker.observe(104, [version, (b"upload-offset", b"40")])
    assert (tracker.ignored, tracker.offset) == (0, 50)  # the 103 is not a mismatched 104


@pytest.mark.unit
def test_wire_exchange_rejects_truncated_request() -> None:
    with pytest.raises(ValueError, match="before its content was complete"):
        h11_creation_exchange([REQUEST_HEAD + b"012"], upload_id="abc", limits=LIMITS)


# --- 104 on HTTP/2 (h2) and HTTP/3 (aioquic) ------------------------------------------------------

H2_HEADERS, H2_DATA, H2_RST_STREAM = 0x1, 0x0, 0x3
H2_END_STREAM, H2_END_HEADERS = 0x1, 0x4
CREATION_FIELDS = [
    (":method", "POST"),
    (":scheme", "https"),
    (":authority", "example.test"),
    (":path", "/files"),
    ("upload-complete", "?1"),
    ("upload-draft-interop-version", "10"),
]


def _h2_frames(wire: bytes) -> list[tuple[int, int, int]]:
    """Split HTTP/2 bytes into (type, flags, stream id) per frame header (RFC 9113 Section 4.1)."""
    frames: list[tuple[int, int, int]] = []
    position = 0
    while position < len(wire):
        length = int.from_bytes(wire[position : position + 3], "big")
        stream_id = int.from_bytes(wire[position + 5 : position + 9], "big") & 0x7FFFFFFF
        frames.append((wire[position + 3], wire[position + 4], stream_id))
        position += 9 + length
    return frames


def _h2_creation_in_progress() -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    """A connected pair: stream 1 carries the creation request head and its first 5 bytes."""
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    client.send_headers(1, CREATION_FIELDS)
    client.send_data(1, b"01234")
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    server.receive_data(client.data_to_send())  # the client's SETTINGS acknowledgement
    return client, server


def _interim(
    *, location: str | None = None, offset: int | None = None, limits: UploadLimits | None = None
) -> list[tuple[str, str]]:
    """A 104 field section for HTTP/2 and HTTP/3: `:status` first, field names lowercase."""
    fields = interim_fields("10", location=location, offset=offset, limits=limits)
    assert fields is not None
    return [(":status", "104"), *((name.lower(), value) for name, value in fields)]


def _observe(tracker: InterimTracker, headers: list[tuple[bytes, bytes]]) -> int:
    """Feed one response field section to the tracker; return its status code."""
    status = int(dict(headers)[b":status"])
    tracker.observe(status, [(name, value) for name, value in headers if name[:1] != b":"])
    return status


@pytest.mark.unit
def test_h2_carries_each_104_as_a_headers_frame_without_end_stream() -> None:
    client, server = _h2_creation_in_progress()
    limits = UploadLimits(max_size=1000)
    server.send_headers(1, _interim(location="/uploads/abc", limits=limits))
    server.send_headers(1, _interim(offset=5))
    client.send_data(1, b"56789", end_stream=True)
    server.receive_data(client.data_to_send())
    server.send_headers(1, [(":status", "201"), ("upload-complete", "?1")])
    server.send_data(1, b"{}", end_stream=True)
    wire = server.data_to_send()
    events = client.receive_data(wire)
    tracker = InterimTracker()
    statuses = [
        _observe(tracker, [(bytes(name), bytes(value)) for name, value in event.headers])
        for event in events
        if isinstance(event, h2.events.InformationalResponseReceived | h2.events.ResponseReceived)
    ]
    assert _h2_frames(wire) == [
        (H2_HEADERS, H2_END_HEADERS, 1),
        (H2_HEADERS, H2_END_HEADERS, 1),
        (H2_HEADERS, H2_END_HEADERS, 1),
        (H2_DATA, H2_END_STREAM, 1),
    ]
    assert [type(event).__name__ for event in events] == [
        "InformationalResponseReceived",
        "InformationalResponseReceived",
        "ResponseReceived",
        "DataReceived",
        "StreamEnded",
    ]
    assert statuses == [104, 104, 201]
    assert (tracker.location, tracker.offset, tracker.limits) == ("/uploads/abc", 5, limits)


@pytest.mark.unit
def test_h2_refuses_a_104_that_ends_the_stream_or_follows_the_final_response() -> None:
    _, server = _h2_creation_in_progress()
    with pytest.raises(h2.exceptions.ProtocolError, match="END_STREAM"):
        server.send_headers(1, _interim(location="/uploads/abc"), end_stream=True)
    server.send_headers(1, [(":status", "201")])
    with pytest.raises(h2.exceptions.ProtocolError, match="after final response"):
        server.send_headers(1, _interim(offset=5))


@pytest.mark.unit
def test_h2_cancellation_is_rst_stream_with_cancel_and_closes_the_stream() -> None:
    client, server = _h2_creation_in_progress()
    client.reset_stream(1, error_code=h2.errors.ErrorCodes.CANCEL)
    wire = client.data_to_send()
    events = server.receive_data(wire)
    assert _h2_frames(wire) == [(H2_RST_STREAM, 0, 1)]
    assert wire[9:] == (0x8).to_bytes(4, "big")  # CANCEL (RFC 9113 Section 7)
    assert len(events) == 1
    reset = events[0]
    assert isinstance(reset, h2.events.StreamReset)
    assert (reset.stream_id, reset.error_code, reset.remote_reset) == (1, 0x8, True)
    with pytest.raises(h2.exceptions.ProtocolError, match="Invalid input"):
        server.send_headers(1, _interim(offset=5))  # no more 104 on a reset stream


class _StreamOnlyQuic:
    """The QuicConnection surface H3Connection uses: ordered stream bytes, no TLS, no packets."""

    def __init__(self, configuration: QuicConfiguration) -> None:
        self.configuration = configuration
        self._quic_logger = None
        self._remote_max_datagram_frame_size = None
        first = 0 if configuration.is_client else 1
        self._next_stream = {False: first, True: first + 2}
        self.sent: list[tuple[int, bytes, bool]] = []

    def get_next_available_stream_id(self, is_unidirectional: bool = False) -> int:
        stream_id = self._next_stream[is_unidirectional]
        self._next_stream[is_unidirectional] = stream_id + 4
        return stream_id

    def send_stream_data(self, stream_id: int, data: bytes, end_stream: bool = False) -> None:
        self.sent.append((stream_id, data, end_stream))


def _h3_deliver(
    source: _StreamOnlyQuic, target: H3Connection
) -> tuple[list[H3Event], list[tuple[int, bytes, bool]]]:
    """Hand every stream write of `source` to the peer in order; return (events, writes)."""
    from aioquic.quic.events import StreamDataReceived

    writes, source.sent = source.sent, []
    events: list[H3Event] = []
    for stream_id, data, end_stream in writes:
        received = StreamDataReceived(data=data, end_stream=end_stream, stream_id=stream_id)
        events.extend(target.handle_event(received))
    return events, writes


@pytest.mark.unit
def test_h3_carries_each_104_as_a_headers_frame_before_the_final_response() -> None:
    # aioquic needs pylsqpack, which has no free-threaded wheel; skip there, never fail.
    pytest.importorskip("aioquic")
    from aioquic.h3.connection import H3Connection
    from aioquic.h3.events import DataReceived, HeadersReceived
    from aioquic.quic.configuration import QuicConfiguration

    client_quic = _StreamOnlyQuic(QuicConfiguration(is_client=True))
    server_quic = _StreamOnlyQuic(QuicConfiguration(is_client=False))
    client = H3Connection(cast("QuicConnection", client_quic))
    server = H3Connection(cast("QuicConnection", server_quic))
    stream_id = client_quic.get_next_available_stream_id()
    client.send_headers(stream_id, [(k.encode(), v.encode()) for k, v in CREATION_FIELDS])
    client.send_data(stream_id, b"01234", end_stream=False)
    _h3_deliver(client_quic, server)  # control and QPACK streams, then the request stream
    _h3_deliver(server_quic, client)

    def send(fields: list[tuple[str, str]]) -> None:
        server.send_headers(stream_id, [(k.encode(), v.encode()) for k, v in fields])

    limits = UploadLimits(max_size=1000)
    send(_interim(location="/uploads/abc", limits=limits))
    send(_interim(offset=5))
    client.send_data(stream_id, b"56789", end_stream=True)
    _h3_deliver(client_quic, server)
    send([(":status", "201"), ("upload-complete", "?1")])
    server.send_data(stream_id, b"{}", end_stream=True)
    events, writes = _h3_deliver(server_quic, client)
    tracker = InterimTracker()
    statuses = [_observe(tracker, e.headers) for e in events if isinstance(e, HeadersReceived)]
    on_request_stream = [(data[0], fin) for sid, data, fin in writes if sid == stream_id]
    assert on_request_stream == [(0x1, False), (0x1, False), (0x1, False), (0x0, True)]
    assert statuses == [104, 104, 201]
    assert (tracker.location, tracker.offset, tracker.limits) == ("/uploads/abc", 5, limits)
    last = events[-1]
    assert isinstance(last, DataReceived)
    assert (last.data, last.stream_ended) == (b"{}", True)


# --- An ASGI app cannot send a 104 and then a final response (real servers on loopback) --------

CREATION_REQUEST = (
    REQUEST_HEAD.replace(b"\r\n\r\n", b"\r\nConnection: close\r\n\r\n") + b"0123456789"
)


def _app_sending_104_then_201(log: list[str]) -> ASGIApp:
    """The only way ASGI offers to try a 104: `http.response.start` twice."""
    messages: list[Message] = [
        {"type": "http.response.start", "status": 104, "headers": [(b"location", b"/uploads/abc")]},
        {"type": "http.response.start", "status": 201, "headers": [(b"content-length", b"0")]},
        {"type": "http.response.body", "body": b""},
    ]

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del receive
        if scope["type"] != "http":
            return
        try:
            for message in messages:
                await send(message)
                log.append(f"sent {message.get('status', 'body')}")
        except Exception as exc:
            log.append(type(exc).__name__)
            raise

    return app


def _listening_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)  # connects queue in the backlog before the server starts accepting
    return sock


async def _creation_roundtrip(port: int) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(CREATION_REQUEST)
        await writer.drain()
        return await reader.read()  # every server closes the connection after its answer
    finally:
        writer.close()  # an open client connection would keep a server from shutting down


async def _stopped(server: asyncio.Task[object]) -> None:
    """Wait for a server task to exit; cancel it and fail rather than hang if it does not."""
    async with asyncio.timeout(5):
        await server


async def _via_uvicorn(app: ASGIApp, http: str) -> bytes:
    pytest.importorskip("uvicorn")
    pytest.importorskip(http)
    import uvicorn

    sock = _listening_socket()
    config = uvicorn.Config(
        app, http=http, lifespan="off", log_level="critical", timeout_graceful_shutdown=1
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            return await _creation_roundtrip(sock.getsockname()[1])
    finally:
        server.should_exit = True
        try:
            await _stopped(task)
        finally:
            sock.close()


async def _via_hypercorn(app: ASGIApp) -> bytes:
    pytest.importorskip("hypercorn")
    from hypercorn.asyncio import serve  # pyright: ignore[reportUnknownVariableType]
    from hypercorn.config import Config

    sock = _listening_socket()
    port = sock.getsockname()[1]
    config = Config()
    config.bind = [f"fd://{sock.detach()}"]  # Hypercorn now owns and closes the descriptor
    config.accesslog = None
    config.errorlog = None
    config.graceful_timeout = 1.0
    stop = asyncio.Event()
    server = asyncio.create_task(serve(app, config, shutdown_trigger=stop.wait))  # type: ignore[arg-type]
    try:
        async with asyncio.timeout(10):
            return await _creation_roundtrip(port)
    finally:
        stop.set()
        await _stopped(server)


async def _via_granian(app: ASGIApp) -> bytes:
    pytest.importorskip("granian")
    from granian.constants import HTTPModes, Interfaces
    from granian.server.embed import Server as GranianServer

    # Granian binds in Rust, so pick a free loopback port and retry until it listens.
    probe = _listening_socket()
    port = probe.getsockname()[1]
    probe.close()
    server = GranianServer(
        app,
        address="127.0.0.1",
        port=port,
        interface=Interfaces.ASGINL,
        http=HTTPModes.http1,
        websockets=False,
        log_enabled=False,
    )
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while True:
                try:
                    return await _creation_roundtrip(port)
                except ConnectionRefusedError:
                    if task.done():
                        task.result()  # Granian failed to start: surface why, do not retry
                    await asyncio.sleep(0.01)
    finally:
        server.stop()  # type: ignore[no-untyped-call]
        await _stopped(task)


ASGI_SERVERS: dict[str, Callable[[ASGIApp], Awaitable[bytes]]] = {
    "uvicorn-h11": lambda app: _via_uvicorn(app, "h11"),
    "uvicorn-httptools": lambda app: _via_uvicorn(app, "httptools"),
    "hypercorn": _via_hypercorn,
    "granian": _via_granian,
}


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server", "app_log", "status_lines"),
    [
        ("uvicorn-h11", ["LocalProtocolError"], []),
        ("uvicorn-httptools", ["sent 104", "RuntimeError"], [104]),
        ("hypercorn", ["sent 104", "UnexpectedMessageError"], [104]),
        ("granian", ["sent 104", "RuntimeError"], [500]),
    ],
    ids=["uvicorn-h11", "uvicorn-httptools", "hypercorn", "granian"],
)
async def test_no_asgi_server_lets_an_app_send_104_and_then_a_final_response(
    server: str, app_log: list[str], status_lines: list[int]
) -> None:
    log: list[str] = []
    wire = await ASGI_SERVERS[server](_app_sending_104_then_201(log))
    assert log == app_log
    assert [int(code) for code in re.findall(rb"^HTTP/1\.1 (\d{3})", wire, re.MULTILINE)] == (
        status_lines
    )
    assert b" 201" not in wire  # the final response never reaches the client
