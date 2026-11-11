"""Streaming multipart uploads: byte, part-count, digest, cleanup, and peak-memory bounds."""

import hashlib
import os
import re
import threading
import tracemalloc
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Annotated, BinaryIO

import anyio
import httpx
import pytest
from fastapi import FastAPI, File, UploadFile
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Scope

from api_labs.f06_bulk_long_running_and_error_contracts import bounded_multipart
from api_labs.f06_bulk_long_running_and_error_contracts.bounded_multipart import (
    UploadLimits,
    build_upload_app,
    close_delimiter,
    file_part_head,
    text_part,
)

BOUNDARY = b"lab-boundary-7f3a"
CONTENT_TYPE = {"content-type": "multipart/form-data; boundary=lab-boundary-7f3a"}
KIB = 1024
MIB = 1024 * KIB
CHUNK = b"\xab" * (64 * KIB)
BIG_CHUNKS = 128  # 128 x 64 KiB = 8 MiB file


def sha256_hex(*pieces: bytes) -> str:
    hasher = hashlib.sha256()
    for piece in pieces:
        hasher.update(piece)
    return hasher.hexdigest()


BIG_SHA256 = sha256_hex(*([CHUNK] * BIG_CHUNKS))


class CountingBody:
    """Request body as an async iterable that records what the server actually pulled."""

    def __init__(self, pieces: Sequence[bytes], watch_dir: Path | None = None) -> None:
        self.pieces = pieces
        self.watch_dir = watch_dir
        self.pulled = 0
        self.partials_seen: set[str] = set()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for piece in self.pieces:
            if self.watch_dir is not None:
                self.partials_seen |= partial_names(self.watch_dir)
            self.pulled += len(piece)
            yield piece


def file_upload(name: str, filename: str, data: Sequence[bytes], digest: str | None) -> list[bytes]:
    pieces: list[bytes] = []
    if digest is not None:
        pieces.append(text_part(BOUNDARY, name + ".sha256", digest.encode()))
    pieces.append(file_part_head(BOUNDARY, name, filename))
    pieces.extend(data)
    pieces.append(b"\r\n")
    return pieces


async def post(
    app: ASGIApp,
    path: str,
    content: CountingBody | bytes,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(path, content=content, headers=headers or CONTENT_TYPE)


def leftovers(upload_dir: Path) -> list[str]:
    return sorted(p.name for p in upload_dir.iterdir())


def partial_names(upload_dir: Path) -> set[str]:
    return {p.name for p in upload_dir.glob("*.part")}


# --- The bounded receiver: success paths ---------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streams_8_mib_file_to_disk_with_verified_sha256(tmp_path: Path) -> None:
    app = build_upload_app(tmp_path, UploadLimits(max_body_bytes=16 * MIB))
    body = CountingBody([*file_upload("file", "big.bin", [CHUNK] * BIG_CHUNKS, BIG_SHA256)])
    body.pieces = [*body.pieces, close_delimiter(BOUNDARY)]

    response = await post(app, "/uploads", body)

    assert response.status_code == 201
    stored = response.json()["files"]
    assert [(f["field"], f["filename"], f["size"], f["sha256"]) for f in stored] == [
        ("file", "big.bin", 8 * MIB, BIG_SHA256)
    ]
    assert stored[0]["content_type"] == "application/octet-stream"
    on_disk = tmp_path / stored[0]["id"]
    assert on_disk.stat().st_size == 8 * MIB
    assert sha256_hex(on_disk.read_bytes()) == BIG_SHA256
    assert leftovers(tmp_path) == [stored[0]["id"]]  # the .part file was renamed, not copied


@pytest.mark.unit
@pytest.mark.asyncio
async def test_digest_field_may_follow_the_file_and_fields_keep_wire_order(
    tmp_path: Path,
) -> None:
    app = build_upload_app(tmp_path)
    data = b"hello multipart"
    body = (
        text_part(BOUNDARY, "title", b"first")
        + file_part_head(BOUNDARY, "doc", "a.txt")
        + data
        + b"\r\n"
        + text_part(BOUNDARY, "doc.sha256", sha256_hex(data).encode())
        + text_part(BOUNDARY, "title", b"second")
        + close_delimiter(BOUNDARY)
    )

    response = await post(app, "/uploads", body)

    assert response.status_code == 201
    assert response.json()["fields"] == [
        ["title", "first"],
        ["doc.sha256", sha256_hex(data)],
        ["title", "second"],
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_file_of_exactly_max_file_bytes_is_accepted(tmp_path: Path) -> None:
    app = build_upload_app(tmp_path, UploadLimits(max_file_bytes=64 * KIB))
    pieces = [*file_upload("file", "edge.bin", [CHUNK], sha256_hex(CHUNK))]
    body = b"".join(pieces) + close_delimiter(BOUNDARY)

    response = await post(app, "/uploads", body)

    assert response.status_code == 201
    assert response.json()["files"][0]["size"] == 64 * KIB


@pytest.mark.unit
@pytest.mark.asyncio
async def test_text_field_of_exactly_max_field_bytes_is_accepted(tmp_path: Path) -> None:
    app = build_upload_app(tmp_path, UploadLimits(max_field_bytes=16))
    body = text_part(BOUNDARY, "note", b"x" * 16) + close_delimiter(BOUNDARY)

    response = await post(app, "/uploads", body)

    assert response.status_code == 201
    assert response.json()["fields"] == [["note", "x" * 16]]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("declare_length", [True, False])
async def test_body_of_exactly_max_body_bytes_is_accepted(
    tmp_path: Path, declare_length: bool
) -> None:
    pieces = [*file_upload("file", "edge.bin", [CHUNK], sha256_hex(CHUNK))]
    pieces.append(close_delimiter(BOUNDARY))
    whole = b"".join(pieces)
    app = build_upload_app(tmp_path, UploadLimits(max_body_bytes=len(whole)))
    # bytes -> httpx declares Content-Length; an async iterable -> no Content-Length
    content: CountingBody | bytes = whole if declare_length else CountingBody(pieces)

    response = await post(app, "/uploads", content)

    assert response.status_code == 201
    assert response.json()["body_bytes"] == len(whole)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zero_byte_file_is_stored_with_empty_digest(tmp_path: Path) -> None:
    app = build_upload_app(tmp_path)
    body = b"".join(file_upload("file", "empty.bin", [], sha256_hex())) + close_delimiter(BOUNDARY)

    response = await post(app, "/uploads", body)

    assert response.status_code == 201
    stored = response.json()["files"][0]
    assert (stored["size"], stored["sha256"]) == (0, sha256_hex())
    assert (tmp_path / stored["id"]).read_bytes() == b""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exactly_max_files_and_max_fields_are_accepted(tmp_path: Path) -> None:
    app = build_upload_app(tmp_path, UploadLimits(max_files=2, max_fields=3, require_sha256=False))
    pieces = [text_part(BOUNDARY, f"k{i}", b"v") for i in range(3)]
    for index in range(2):
        pieces += file_upload(f"f{index}", f"{index}.bin", [b"data"], None)

    response = await post(app, "/uploads", b"".join(pieces) + close_delimiter(BOUNDARY))

    assert response.status_code == 201
    assert response.json()["fields"] == [["k0", "v"], ["k1", "v"], ["k2", "v"]]
    assert [f["field"] for f in response.json()["files"]] == ["f0", "f1"]
    assert leftovers(tmp_path) == sorted(f["id"] for f in response.json()["files"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type_header", "expected"),
    [
        (b"Content-Type: image/png\r\n", "image/png"),
        (b"", "text/plain"),  # RFC 7578 Section 4.4: a part's Content-Type defaults to text/plain
    ],
)
async def test_file_part_content_type_is_kept_and_defaults_to_text_plain(
    tmp_path: Path, content_type_header: bytes, expected: str
) -> None:
    app = build_upload_app(tmp_path, UploadLimits(require_sha256=False))
    body = (
        b"--"
        + BOUNDARY
        + b'\r\nContent-Disposition: form-data; name="file"; filename="a.bin"\r\n'
        + content_type_header
        + b"\r\ndata\r\n"
        + close_delimiter(BOUNDARY)
    )

    response = await post(app, "/uploads", body)

    assert response.status_code == 201
    assert response.json()["files"][0]["content_type"] == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_part_headers_are_ignored_not_rejected(tmp_path: Path) -> None:
    app = build_upload_app(tmp_path)
    body = (
        b"--"
        + BOUNDARY
        + b"\r\nX-Trace: 1\r\nX-Trace: 2\r\nContent-Transfer-Encoding: binary\r\n"
        + b'Content-Disposition: form-data; name="a"\r\n\r\nvalue\r\n'
        + close_delimiter(BOUNDARY)
    )

    response = await post(app, "/uploads", body)

    assert response.status_code == 201  # RFC 7578 Section 4.8: other part headers MUST be ignored
    assert response.json()["fields"] == [["a", "value"]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_filename_is_metadata_and_never_a_path(tmp_path: Path) -> None:
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    app = build_upload_app(upload_dir)
    data = b"payload"
    pieces = file_upload("file", "../../evil.sh", [data], sha256_hex(data))
    body = CountingBody([*pieces, close_delimiter(BOUNDARY)], watch_dir=upload_dir)

    response = await post(app, "/uploads", body)

    assert response.status_code == 201
    stored = response.json()["files"][0]
    assert stored["filename"] == "../../evil.sh"
    assert re.fullmatch(r"[0-9a-f]{32}", stored["id"])
    assert body.partials_seen == {stored["id"] + ".part"}  # spilled inside upload_dir
    assert leftovers(upload_dir) == [stored["id"]]
    assert leftovers(tmp_path) == ["uploads"]


class ThreadRecordingFile:
    """Wraps the real spill file and records which thread performs each write."""

    def __init__(self, inner: BinaryIO, write_threads: list[int]) -> None:
        self.inner = inner
        self.write_threads = write_threads

    def write(self, data: memoryview) -> int:
        self.write_threads.append(threading.get_ident())
        return self.inner.write(data)

    def close(self) -> None:
        self.inner.close()


class EventRecordingFile:
    """Wraps the real spill file and records each write size and the close, in order."""

    def __init__(self, inner: BinaryIO, events: list[tuple[str, int]]) -> None:
        self.inner = inner
        self.events = events

    def write(self, data: memoryview) -> int:
        self.events.append(("write", len(data)))
        return self.inner.write(data)

    def close(self) -> None:
        self.events.append(("close", 0))
        self.inner.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_file_writes_run_in_worker_threads_not_on_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_threads: list[int] = []

    def recording_open(path: Path) -> ThreadRecordingFile:
        return ThreadRecordingFile(path.open("xb"), write_threads)

    monkeypatch.setattr(bounded_multipart, "_open_exclusive", recording_open)
    app = build_upload_app(tmp_path)
    data = [CHUNK] * 4
    body = b"".join(file_upload("file", "a.bin", data, sha256_hex(*data)))

    response = await post(app, "/uploads", body + close_delimiter(BOUNDARY))

    assert response.status_code == 201
    assert len(write_threads) >= 1
    assert threading.get_ident() not in write_threads


@pytest.mark.unit
@pytest.mark.asyncio
async def test_spill_file_is_closed_before_it_is_renamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, int]] = []
    real_replace = os.replace

    def recording_open(path: Path) -> EventRecordingFile:
        return EventRecordingFile(path.open("xb"), events)

    def recording_replace(src: Path, dst: Path) -> None:
        events.append(("replace", 0))
        real_replace(src, dst)

    monkeypatch.setattr(bounded_multipart, "_open_exclusive", recording_open)
    monkeypatch.setattr(os, "replace", recording_replace)
    app = build_upload_app(tmp_path)
    data = b"small enough to sit in the write buffer until close"
    body = b"".join(file_upload("file", "a.txt", [data], sha256_hex(data)))

    response = await post(app, "/uploads", body + close_delimiter(BOUNDARY))

    assert response.status_code == 201
    assert events == [("write", len(data)), ("close", 0), ("replace", 0)]
    assert (tmp_path / response.json()["files"][0]["id"]).read_bytes() == data


# --- The bounded receiver: byte budgets ----------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_declared_content_length_over_budget_is_rejected_before_reading(
    tmp_path: Path,
) -> None:
    app = build_upload_app(tmp_path, UploadLimits(max_body_bytes=1 * MIB))
    body = CountingBody(file_upload("file", "big.bin", [CHUNK] * 32, None))

    response = await post(
        app, "/uploads", body, headers={**CONTENT_TYPE, "content-length": str(2 * MIB)}
    )

    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["status"] == 413
    assert response.headers["connection"] == "close"
    assert body.pulled == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chunked_body_over_budget_stops_reading_mid_stream_and_cleans_up(
    tmp_path: Path,
) -> None:
    limit = 256 * KIB
    app = build_upload_app(tmp_path, UploadLimits(max_body_bytes=limit))
    body = CountingBody(file_upload("file", "big.bin", [CHUNK] * 16, None), watch_dir=tmp_path)

    response = await post(app, "/uploads", body)  # async iterable -> no Content-Length

    assert response.status_code == 413
    assert response.headers["connection"] == "close"
    assert body.pulled <= limit + len(CHUNK)
    assert body.pulled < 1 * MIB
    assert body.partials_seen  # a partial file really existed on disk ...
    assert leftovers(tmp_path) == []  # ... and the rejection deleted it


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chunk_that_breaks_the_body_budget_is_never_parsed_or_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, int]] = []
    parsed: list[int] = []

    def recording_open(path: Path) -> EventRecordingFile:
        return EventRecordingFile(path.open("xb"), events)

    class RecordingParser(MultipartParser):
        def write(self, data: bytes) -> int:
            parsed.append(len(data))
            return super().write(data)

    monkeypatch.setattr(bounded_multipart, "_open_exclusive", recording_open)
    monkeypatch.setattr(bounded_multipart, "MultipartParser", RecordingParser)
    limit = 256 * KIB
    app = build_upload_app(tmp_path, UploadLimits(max_body_bytes=limit))
    body = CountingBody(file_upload("file", "big.bin", [CHUNK] * 16, None))

    response = await post(app, "/uploads", body)

    assert response.status_code == 413
    # The part head plus four 64 KiB chunks is the first total over 256 KiB, so exactly
    # three chunks were parsed and spilled; the fourth was counted and dropped.
    assert parsed == [len(file_part_head(BOUNDARY, "file", "big.bin")), *[len(CHUNK)] * 3]
    assert events == [("write", len(CHUNK))] * 3 + [("close", 0)]
    assert leftovers(tmp_path) == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("declared", ["-1", "+5", "1x", "0x10"])
async def test_invalid_content_length_is_400(tmp_path: Path, declared: str) -> None:
    app = build_upload_app(tmp_path)

    response = await post(
        app, "/uploads", CountingBody([b""]), headers={**CONTENT_TYPE, "content-length": declared}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid Content-Length"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_file_over_per_file_limit_is_413_and_partial_file_deleted(tmp_path: Path) -> None:
    app = build_upload_app(tmp_path, UploadLimits(max_file_bytes=200 * KIB))
    body = CountingBody(
        [*file_upload("file", "big.bin", [CHUNK] * 4, None), close_delimiter(BOUNDARY)],
        watch_dir=tmp_path,
    )

    response = await post(app, "/uploads", body)

    assert response.status_code == 413
    assert response.json()["detail"] == f"file exceeds {200 * KIB} bytes"
    assert body.partials_seen
    assert leftovers(tmp_path) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_text_field_over_max_field_bytes_is_413(tmp_path: Path) -> None:
    app = build_upload_app(tmp_path, UploadLimits(max_field_bytes=16))
    body = text_part(BOUNDARY, "note", b"x" * 17) + close_delimiter(BOUNDARY)

    response = await post(app, "/uploads", body)

    assert response.status_code == 413
    assert response.json()["detail"] == "field exceeds 16 bytes"


# --- The bounded receiver: part counts and structure ---------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_more_files_than_max_files_is_413_and_earlier_files_deleted(
    tmp_path: Path,
) -> None:
    app = build_upload_app(tmp_path, UploadLimits(max_files=2, require_sha256=False))
    pieces: list[bytes] = []
    for index in range(3):
        pieces += file_upload(f"f{index}", f"{index}.bin", [b"data"], None)
    body = CountingBody([*pieces, close_delimiter(BOUNDARY)], watch_dir=tmp_path)

    response = await post(app, "/uploads", body)

    assert response.status_code == 413
    assert response.json()["detail"] == "more than 2 files"
    assert len(body.partials_seen) == 2
    assert leftovers(tmp_path) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_more_fields_than_max_fields_is_413(tmp_path: Path) -> None:
    app = build_upload_app(tmp_path, UploadLimits(max_fields=3))
    body = b"".join(text_part(BOUNDARY, f"k{i}", b"v") for i in range(4))

    response = await post(app, "/uploads", body + close_delimiter(BOUNDARY))

    assert response.status_code == 413
    assert response.json()["detail"] == "more than 3 fields"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_closing_delimiter_is_400_and_partial_file_deleted(
    tmp_path: Path,
) -> None:
    app = build_upload_app(tmp_path, UploadLimits(require_sha256=False))
    body = CountingBody(
        [file_part_head(BOUNDARY, "file", "cut.bin"), CHUNK, CHUNK], watch_dir=tmp_path
    )

    response = await post(app, "/uploads", body)

    assert response.status_code == 400
    assert response.json()["detail"] == "closing boundary delimiter missing: body truncated"
    assert "connection" not in response.headers  # the body was read to its end
    assert body.partials_seen
    assert leftovers(tmp_path) == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "status", "detail"),
    [
        ({"content-type": "application/json"}, 415, "expected multipart/form-data"),
        (
            {"content-type": "multipart/form-data"},
            400,
            "multipart/form-data requires a boundary parameter",
        ),
        (
            {"content-type": "multipart/form-data; boundary=" + "b" * 257},
            400,
            "malformed multipart body",
        ),
    ],
)
async def test_content_type_and_boundary_are_validated_before_parsing(
    tmp_path: Path, headers: dict[str, str], status: int, detail: str
) -> None:
    app = build_upload_app(tmp_path)

    response = await post(app, "/uploads", b"--x--\r\n", headers=headers)

    assert response.status_code == status
    assert response.json()["detail"] == detail


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("part_headers", "detail"),
    [
        (
            b'Content-Disposition: form-data; name="a"\r\n'
            b'Content-Disposition: form-data; name="b"\r\n',
            "duplicate part header content-disposition",
        ),
        (
            b'Content-Disposition: attachment; name="a"\r\n',
            'each part needs Content-Disposition: form-data; name="..."',
        ),
        (
            b"Content-Disposition: form-data\r\n",
            'each part needs Content-Disposition: form-data; name="..."',
        ),
        (
            b"Content-Type: text/plain\r\n",
            'each part needs Content-Disposition: form-data; name="..."',
        ),
        (b"".join(b"X-H%d: v\r\n" % i for i in range(9)), "malformed multipart body"),
        (b'Content-Disposition: form-data; name="\xff"\r\n', "part name is not UTF-8"),
    ],
)
async def test_part_headers_are_strict(tmp_path: Path, part_headers: bytes, detail: str) -> None:
    app = build_upload_app(tmp_path)
    body = b"--" + BOUNDARY + b"\r\n" + part_headers + b"\r\nvalue\r\n" + close_delimiter(BOUNDARY)

    response = await post(app, "/uploads", body)

    assert response.status_code == 400
    assert response.json()["detail"] == detail


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_utf8_text_field_is_400(tmp_path: Path) -> None:
    app = build_upload_app(tmp_path)
    body = text_part(BOUNDARY, "note", b"caf\xe9") + close_delimiter(BOUNDARY)

    response = await post(app, "/uploads", body)

    assert response.status_code == 400
    assert response.json()["detail"] == "field note is not UTF-8"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_file_field_name_is_400(tmp_path: Path) -> None:
    app = build_upload_app(tmp_path, UploadLimits(require_sha256=False))
    pieces = file_upload("file", "a.bin", [b"a"], None) + file_upload("file", "b.bin", [b"b"], None)

    response = await post(app, "/uploads", b"".join(pieces) + close_delimiter(BOUNDARY))

    assert response.status_code == 400
    assert response.json()["detail"] == "duplicate file field file"
    assert leftovers(tmp_path) == []


# --- The bounded receiver: integrity and disconnects ---------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("digest", "detail"),
    [
        (None, "missing file.sha256 field"),
        ("0" * 64, "file failed SHA-256 verification"),
    ],
)
async def test_digest_missing_or_wrong_is_422_and_file_deleted(
    tmp_path: Path, digest: str | None, detail: str
) -> None:
    app = build_upload_app(tmp_path)
    body = CountingBody(
        [*file_upload("file", "a.bin", [CHUNK, CHUNK], digest), close_delimiter(BOUNDARY)],
        watch_dir=tmp_path,
    )

    response = await post(app, "/uploads", body)

    assert response.status_code == 422
    assert response.json() == {
        "type": "about:blank",
        "title": "Unprocessable Content",
        "status": 422,
        "detail": detail,
    }
    assert "connection" not in response.headers
    assert body.partials_seen
    assert leftovers(tmp_path) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_disconnect_mid_upload_deletes_partial_file(tmp_path: Path) -> None:
    app = build_upload_app(tmp_path, UploadLimits(require_sha256=False))
    incoming: list[Message] = [
        {
            "type": "http.request",
            "body": file_part_head(BOUNDARY, "file", "a.bin"),
            "more_body": True,
        },
        {"type": "http.request", "body": CHUNK, "more_body": True},
        {"type": "http.request", "body": CHUNK, "more_body": True},
    ]
    partials: list[str] = []
    sent: list[Message] = []

    async def receive() -> Message:
        if incoming:
            return incoming.pop(0)
        partials.extend(partial_names(tmp_path))
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(http_scope(), receive, send)

    assert len(partials) == 1
    assert leftovers(tmp_path) == []
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 400


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("digest_fields", "detail"),
    [
        (["not-hex"], "file.sha256 must be 64 lowercase hex digits"),
        ([sha256_hex(CHUNK).upper()], "file.sha256 must be 64 lowercase hex digits"),
        ([sha256_hex(CHUNK) + "0"], "file.sha256 must be 64 lowercase hex digits"),
        (["0" * 64, sha256_hex(CHUNK)], "duplicate file.sha256 field"),
    ],
)
async def test_malformed_or_duplicate_digest_field_is_400_and_file_deleted(
    tmp_path: Path, digest_fields: list[str], detail: str
) -> None:
    app = build_upload_app(tmp_path)
    pieces = [text_part(BOUNDARY, "file.sha256", d.encode()) for d in digest_fields]
    pieces += file_upload("file", "a.bin", [CHUNK], None)
    body = CountingBody([*pieces, close_delimiter(BOUNDARY)], watch_dir=tmp_path)

    response = await post(app, "/uploads", body)

    assert response.status_code == 400
    assert response.json()["detail"] == detail
    assert body.partials_seen
    assert leftovers(tmp_path) == []


def http_scope() -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/uploads",
        "raw_path": b"/uploads",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", CONTENT_TYPE["content-type"].encode())],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_request_still_deletes_partial_file(tmp_path: Path) -> None:
    app = build_upload_app(tmp_path, UploadLimits(require_sha256=False))
    incoming: list[Message] = [
        {
            "type": "http.request",
            "body": file_part_head(BOUNDARY, "file", "a.bin"),
            "more_body": True,
        },
        {"type": "http.request", "body": CHUNK, "more_body": True},
    ]
    stalled = anyio.Event()
    partials: list[str] = []

    async def receive() -> Message:
        if incoming:
            return incoming.pop(0)
        partials.extend(partial_names(tmp_path))
        stalled.set()
        await anyio.sleep_forever()  # a client that stops sending; a timeout cancels us
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        raise AssertionError(f"no response expected, got {message['type']}")

    async with anyio.create_task_group() as tg:
        tg.start_soon(app, http_scope(), receive, send)
        await stalled.wait()
        tg.cancel_scope.cancel()

    assert len(partials) == 1
    assert leftovers(tmp_path) == []


class FailingDiskFile:
    """A spill file whose write and close both fail, as on a full disk."""

    def __init__(self, inner: BinaryIO) -> None:
        self.inner = inner

    def write(self, data: memoryview) -> int:
        raise OSError(28, "No space left on device")

    def close(self) -> None:
        self.inner.close()
        raise OSError(28, "No space left on device")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failing_close_does_not_skip_cleanup_or_mask_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Path] = []

    def failing_open(path: Path) -> FailingDiskFile:
        opened.append(path)
        return FailingDiskFile(path.open("xb"))

    monkeypatch.setattr(bounded_multipart, "_open_exclusive", failing_open)
    app = build_upload_app(tmp_path, UploadLimits(require_sha256=False))
    body = CountingBody(
        [*file_upload("file", "a.bin", [CHUNK], None), close_delimiter(BOUNDARY)],
        watch_dir=tmp_path,
    )

    with pytest.raises(OSError, match="No space left on device") as excinfo:
        await post(app, "/uploads", body)

    assert excinfo.value.errno == 28
    assert [p.name.endswith(".part") for p in opened] == [True]  # the spill file existed
    assert leftovers(tmp_path) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failure_midway_through_commit_deletes_already_renamed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace
    calls: list[str] = []

    def replace_then_fail(src: Path, dst: Path) -> None:
        calls.append(Path(dst).name)
        if len(calls) == 2:
            raise OSError(5, "Input/output error")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace_then_fail)
    app = build_upload_app(tmp_path, UploadLimits(require_sha256=False))
    pieces = file_upload("a", "a.bin", [b"aaaa"], None) + file_upload("b", "b.bin", [b"bb"], None)

    with pytest.raises(OSError, match="Input/output error"):
        await post(app, "/uploads", b"".join(pieces) + close_delimiter(BOUNDARY))

    assert len(calls) == 2  # the first file really was renamed to its final name
    assert leftovers(tmp_path) == []


# --- Peak memory: bounded receiver vs buffering --------------------------------------


def big_upload() -> CountingBody:
    pieces = file_upload("file", "big.bin", [CHUNK] * BIG_CHUNKS, BIG_SHA256)
    return CountingBody([*pieces, close_delimiter(BOUNDARY)])


async def traced_peak(app: ASGIApp, path: str, make_body: Callable[[], CountingBody]) -> int:
    """Peak Python heap growth while serving one 8 MiB upload, after a warm-up request."""
    tracemalloc.start()
    try:
        warm = await post(app, path, make_body())
        assert warm.status_code < 300
        baseline = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        response = await post(app, path, make_body())
        peak = tracemalloc.get_traced_memory()[1] - baseline
    finally:
        tracemalloc.stop()
    assert response.status_code < 300
    return peak


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bounded_receiver_peak_heap_is_far_below_payload(tmp_path: Path) -> None:
    app = build_upload_app(tmp_path, UploadLimits(max_body_bytes=16 * MIB))

    peak = await traced_peak(app, "/uploads", big_upload)

    assert peak < 1 * MIB  # payload is 8 MiB; the receiver holds about one chunk at a time


@pytest.mark.unit
@pytest.mark.asyncio
async def test_control_request_body_buffers_the_whole_payload() -> None:
    async def endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"bytes": len(await request.body())})

    app = Starlette(routes=[Route("/raw", endpoint, methods=["POST"])])

    peak = await traced_peak(app, "/raw", big_upload)

    assert peak >= 8 * MIB


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_form_spools_one_mib_in_memory_then_rolls_to_disk() -> None:
    async def endpoint(request: Request) -> JSONResponse:
        async with request.form() as form:
            return JSONResponse({"keys": list(form.keys())})

    app = Starlette(routes=[Route("/form", endpoint, methods=["POST"])])

    peak = await traced_peak(app, "/form", big_upload)

    assert 1 * MIB <= peak < 4 * MIB


# --- Library defaults this receiver exists to replace --------------------------------


def default_fastapi_app() -> FastAPI:
    app = FastAPI()

    @app.post("/files")
    async def files(uploads: Annotated[list[UploadFile], File()]) -> dict[str, list[int | None]]:
        return {"sizes": [upload.size for upload in uploads]}

    return app


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_uploadfile_default_has_no_file_size_cap() -> None:
    body = file_part_head(BOUNDARY, "uploads", "a.bin") + b"\x01" * (3 * MIB) + b"\r\n"

    response = await post(default_fastapi_app(), "/files", body + close_delimiter(BOUNDARY))

    assert response.status_code == 200
    assert response.json() == {"sizes": [3 * MIB]}  # 3x the 1 MiB max_part_size default


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_default_max_part_size_applies_only_to_text_parts() -> None:
    body = text_part(BOUNDARY, "uploads", b"x" * (MIB + 1)) + close_delimiter(BOUNDARY)

    response = await post(default_fastapi_app(), "/files", body)

    assert response.status_code == 400
    assert response.json() == {"detail": "Part exceeded maximum size of 1024KB."}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_default_silently_drops_a_truncated_last_file() -> None:
    body = (
        file_part_head(BOUNDARY, "uploads", "a.bin")
        + b"aaa\r\n"
        + file_part_head(BOUNDARY, "uploads", "b.bin")
        + b"bbbb"  # no CRLF--boundary--: the client stopped mid-file
    )

    response = await post(default_fastapi_app(), "/files", body)

    assert response.status_code == 200
    assert response.json() == {"sizes": [3]}


@pytest.mark.unit
def test_python_multipart_max_size_truncates_instead_of_raising() -> None:
    parser = MultipartParser(BOUNDARY, {}, max_size=10)
    data = text_part(BOUNDARY, "a", b"y" * 100) + close_delimiter(BOUNDARY)

    written = parser.write(data)

    assert written == 10
    assert len(data) > written


@pytest.mark.unit
def test_python_multipart_rejects_boundary_longer_than_256_bytes() -> None:
    MultipartParser(b"b" * 256, {})
    with pytest.raises(FormParserError, match="exceeds maximum of 256"):
        MultipartParser(b"b" * 257, {})
