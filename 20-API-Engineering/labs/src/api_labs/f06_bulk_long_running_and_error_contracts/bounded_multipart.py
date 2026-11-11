"""Bounded streaming multipart/form-data receiver with byte, part-count, and memory limits.

Feeds `request.stream()` chunks straight into python-multipart's `MultipartParser`,
hashes and spills each file part to disk as it arrives, and rejects the request the
moment any budget is exceeded, deleting every partial file it created.
"""

import contextlib
import os
import re
import secrets
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, Protocol

import anyio.to_thread
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect

DIGEST_SUFFIX = ".sha256"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_DIGITS = re.compile(r"[0-9]+")


@dataclass(frozen=True, slots=True)
class UploadLimits:
    """Per-request budgets; every one is checked before the bytes that would break it are kept."""

    max_body_bytes: int = 64 * 1024 * 1024
    max_file_bytes: int = 32 * 1024 * 1024
    max_files: int = 4
    max_fields: int = 16
    max_field_bytes: int = 4 * 1024
    require_sha256: bool = True


class UploadRejected(Exception):
    """A request the receiver refuses; carries the HTTP status and a problem detail."""

    def __init__(self, status: int, title: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail


def _too_large(detail: str) -> UploadRejected:
    return UploadRejected(413, "Content Too Large", detail)


def _bad_request(detail: str) -> UploadRejected:
    return UploadRejected(400, "Bad Request", detail)


def _unprocessable(detail: str) -> UploadRejected:
    return UploadRejected(422, "Unprocessable Content", detail)


class _Hasher(Protocol):
    def update(self, data: memoryview, /) -> None: ...
    def hexdigest(self) -> str: ...


@dataclass(slots=True)
class _FilePart:
    field_name: str
    filename: str
    content_type: str
    temp_path: Path
    final_path: Path
    hasher: _Hasher = field(default_factory=sha256)
    handle: BinaryIO | None = None
    size: int = 0


@dataclass(frozen=True, slots=True)
class StoredFile:
    field_name: str
    filename: str
    content_type: str
    size: int
    sha256: str
    path: Path


@dataclass(frozen=True, slots=True)
class UploadResult:
    fields: list[tuple[str, str]]
    files: list[StoredFile]
    body_bytes: int


def _open_exclusive(path: Path) -> BinaryIO:
    # "xb" fails if the name exists: a server-chosen random name is never shared.
    return path.open("xb")


def _unlink_if_present(path: Path) -> None:
    path.unlink(missing_ok=True)


class BoundedMultipartReceiver:
    """One instance per request: parser callbacks mutate its state, `receive` drives it."""

    def __init__(self, upload_dir: Path, limits: UploadLimits) -> None:
        self.upload_dir = upload_dir
        self.limits = limits
        self.body_bytes = 0
        self.stream_complete = False
        self._ended = False
        self._fields: list[tuple[str, str]] = []
        self._files: list[_FilePart] = []
        self._pending: list[tuple[_FilePart, memoryview]] = []
        self._headers: dict[bytes, bytes] = {}
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._current_file: _FilePart | None = None
        self._field_name = ""
        self._field_value = bytearray()

    async def receive(self, request: Request) -> UploadResult:
        media_type, params = parse_options_header(request.headers.get("content-type"))
        if media_type != b"multipart/form-data":
            raise UploadRejected(415, "Unsupported Media Type", "expected multipart/form-data")
        boundary = params.get(b"boundary")
        if not boundary:
            raise _bad_request("multipart/form-data requires a boundary parameter")
        declared = request.headers.get("content-length")
        if declared is not None:
            if _DIGITS.fullmatch(declared) is None:
                raise _bad_request("invalid Content-Length")
            if int(declared) > self.limits.max_body_bytes:
                raise _too_large(f"body exceeds {self.limits.max_body_bytes} bytes")
        try:
            parser = MultipartParser(
                boundary,
                {
                    "on_part_begin": self._on_part_begin,
                    "on_header_field": self._on_header_field,
                    "on_header_value": self._on_header_value,
                    "on_header_end": self._on_header_end,
                    "on_headers_finished": self._on_headers_finished,
                    "on_part_data": self._on_part_data,
                    "on_part_end": self._on_part_end,
                    "on_end": self._on_end,
                },
            )
            async for chunk in request.stream():
                self.body_bytes += len(chunk)
                if self.body_bytes > self.limits.max_body_bytes:
                    raise _too_large(f"body exceeds {self.limits.max_body_bytes} bytes")
                parser.write(chunk)
                await self._flush()
            self.stream_complete = True
            parser.finalize()  # a no-op in python-multipart 0.0.33: it never checks the end state
            if not self._ended:
                raise _bad_request("closing boundary delimiter missing: body truncated")
            files = await self._commit()
        except FormParserError as exc:
            await self._abort()
            raise _bad_request("malformed multipart body") from exc
        except BaseException:
            await self._abort()
            raise
        return UploadResult(fields=list(self._fields), files=files, body_bytes=self.body_bytes)

    # python-multipart callbacks: synchronous, so they only validate, hash, and queue.

    def _on_part_begin(self) -> None:
        self._headers = {}
        self._current_file = None
        self._field_name = ""
        self._field_value = bytearray()

    def _on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def _on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def _on_header_end(self) -> None:
        name = bytes(self._header_name).lower()
        if name in self._headers:
            raise _bad_request(f"duplicate part header {name.decode('latin-1')}")
        # RFC 7578 Section 4.8: other part headers MUST be ignored, so keep only these two.
        if name in (b"content-disposition", b"content-type"):
            self._headers[name] = bytes(self._header_value)
        self._header_name = bytearray()
        self._header_value = bytearray()

    def _on_headers_finished(self) -> None:
        disposition, options = parse_options_header(self._headers.get(b"content-disposition"))
        if disposition != b"form-data" or b"name" not in options:
            raise _bad_request('each part needs Content-Disposition: form-data; name="..."')
        try:
            name = options[b"name"].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _bad_request("part name is not UTF-8") from exc
        if b"filename" not in options:
            if len(self._fields) >= self.limits.max_fields:
                raise _too_large(f"more than {self.limits.max_fields} fields")
            self._field_name = name
            return
        if len(self._files) >= self.limits.max_files:
            raise _too_large(f"more than {self.limits.max_files} files")
        if any(part.field_name == name for part in self._files):
            raise _bad_request(f"duplicate file field {name}")
        token = secrets.token_hex(16)
        part = _FilePart(
            field_name=name,
            filename=options[b"filename"].decode("utf-8", errors="replace"),
            content_type=self._headers.get(b"content-type", b"text/plain").decode("latin-1"),
            # The client filename is metadata only; it never reaches the filesystem path.
            temp_path=self.upload_dir / f"{token}.part",
            final_path=self.upload_dir / token,
        )
        self._files.append(part)
        self._current_file = part

    def _on_part_data(self, data: bytes, start: int, end: int) -> None:
        view = memoryview(data)[start:end]
        part = self._current_file
        if part is None:
            if len(self._field_value) + len(view) > self.limits.max_field_bytes:
                raise _too_large(f"field exceeds {self.limits.max_field_bytes} bytes")
            self._field_value += view
            return
        if part.size + len(view) > self.limits.max_file_bytes:
            raise _too_large(f"file exceeds {self.limits.max_file_bytes} bytes")
        part.size += len(view)
        part.hasher.update(view)
        self._pending.append((part, view))

    def _on_part_end(self) -> None:
        if self._current_file is None:
            try:
                value = self._field_value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _bad_request(f"field {self._field_name} is not UTF-8") from exc
            self._fields.append((self._field_name, value))

    def _on_end(self) -> None:
        self._ended = True

    # Disk I/O runs in the AnyIO worker pool, never on the event loop thread.

    async def _flush(self) -> None:
        pending, self._pending = self._pending, []
        for part, view in pending:
            if part.handle is None:
                part.handle = await anyio.to_thread.run_sync(_open_exclusive, part.temp_path)
            await anyio.to_thread.run_sync(part.handle.write, view)

    async def _commit(self) -> list[StoredFile]:
        digests: dict[str, str] = {}
        for name, value in self._fields:
            if name.endswith(DIGEST_SUFFIX):
                if name in digests:
                    raise _bad_request(f"duplicate {name} field")
                if _SHA256_HEX.fullmatch(value) is None:
                    raise _bad_request(f"{name} must be 64 lowercase hex digits")
                digests[name] = value
        stored: list[StoredFile] = []
        for part in self._files:
            expected = digests.get(part.field_name + DIGEST_SUFFIX)
            actual = part.hasher.hexdigest()
            if expected is None:
                if self.limits.require_sha256:
                    raise _unprocessable(f"missing {part.field_name}{DIGEST_SUFFIX} field")
            elif expected != actual:
                raise _unprocessable(f"{part.field_name} failed SHA-256 verification")
            stored.append(
                StoredFile(
                    field_name=part.field_name,
                    filename=part.filename,
                    content_type=part.content_type,
                    size=part.size,
                    sha256=actual,
                    path=part.final_path,
                )
            )
        for part in self._files:
            if part.handle is None:  # zero-byte file: no data callback ever opened it
                part.handle = await anyio.to_thread.run_sync(_open_exclusive, part.temp_path)
            await anyio.to_thread.run_sync(part.handle.close)
            await anyio.to_thread.run_sync(os.replace, part.temp_path, part.final_path)
        return stored

    async def _abort(self) -> None:
        # Shielded so cleanup still runs when the request task is cancelled, and
        # best effort per step so one failing close (say ENOSPC on flush) cannot skip
        # the remaining unlinks; the caller re-raises the original exception.
        self._pending.clear()
        with anyio.CancelScope(shield=True):
            for part in self._files:
                if part.handle is not None:
                    with contextlib.suppress(OSError):
                        await anyio.to_thread.run_sync(part.handle.close)
                for path in (part.temp_path, part.final_path):
                    with contextlib.suppress(OSError):
                        await anyio.to_thread.run_sync(_unlink_if_present, path)


def problem_response(exc: UploadRejected, *, body_fully_read: bool) -> JSONResponse:
    """RFC 9457 problem details; close an HTTP/1.1 connection whose body was left unread."""
    headers = {} if body_fully_read else {"connection": "close"}
    return JSONResponse(
        {"type": "about:blank", "title": exc.title, "status": exc.status, "detail": exc.detail},
        status_code=exc.status,
        headers=headers,
        media_type="application/problem+json",
    )


def build_upload_app(upload_dir: Path, limits: UploadLimits | None = None) -> FastAPI:
    """FastAPI app whose endpoint takes `Request`, so FastAPI never calls `request.form()`."""
    app = FastAPI()
    budget = limits if limits is not None else UploadLimits()

    @app.post("/uploads")
    async def upload(request: Request) -> Response:
        receiver = BoundedMultipartReceiver(upload_dir, budget)
        try:
            result = await receiver.receive(request)
        except UploadRejected as exc:
            return problem_response(exc, body_fully_read=receiver.stream_complete)
        except ClientDisconnect:
            # Nobody is listening; the receiver already deleted its partial files.
            return Response(status_code=400, headers={"connection": "close"})
        return JSONResponse(
            {
                "fields": [[name, value] for name, value in result.fields],
                "files": [
                    {
                        "field": f.field_name,
                        "filename": f.filename,
                        "content_type": f.content_type,
                        "size": f.size,
                        "sha256": f.sha256,
                        "id": f.path.name,
                    }
                    for f in result.files
                ],
                "body_bytes": result.body_bytes,
            },
            status_code=201,
        )

    return app


def text_part(boundary: bytes, name: str, value: bytes) -> bytes:
    """One complete text part, including the CRLF that begins the next delimiter."""
    disposition = f'Content-Disposition: form-data; name="{name}"'.encode()
    return b"--" + boundary + b"\r\n" + disposition + b"\r\n\r\n" + value + b"\r\n"


def file_part_head(boundary: bytes, name: str, filename: str) -> bytes:
    """Delimiter and headers of a file part; the caller streams the data, then `b"\\r\\n"`."""
    disposition = f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'
    return (
        b"--"
        + boundary
        + b"\r\n"
        + disposition.encode()
        + b"\r\nContent-Type: application/octet-stream\r\n\r\n"
    )


def close_delimiter(boundary: bytes) -> bytes:
    return b"--" + boundary + b"--\r\n"
