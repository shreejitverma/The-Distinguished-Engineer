"""Resumable Uploads for HTTP (draft-ietf-httpbis-resumable-upload-15, interop version 10).

Three layers:
- a Structured Fields subset (RFC 9651 Section 4.2) for Upload-Offset, Upload-Complete,
  Upload-Length, and the Upload-Limit dictionary;
- a Starlette upload server: creation (Section 6), offset retrieval (Section 7), append
  (Section 8), cancellation (Section 9), and preemption of stale writers (Section 10);
- h11 wire helpers for the 104 (Upload Resumption Supported) interim response, which ASGI
  cannot express, and a client-side tracker that applies Appendix B.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import enum
import hashlib
import json
import secrets
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import h11
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from starlette.types import Receive, Scope, Send

INTEROP_VERSION = 10
UPLOAD_RESUMPTION_SUPPORTED = 104
PARTIAL_UPLOAD = "application/partial-upload"
PROBLEM_JSON = "application/problem+json"
MISMATCHING_OFFSET = "https://iana.org/assignments/http-problem-types#mismatching-upload-offset"
INCONSISTENT_LENGTH = "https://iana.org/assignments/http-problem-types#inconsistent-upload-length"
SF_INTEGER_MAX = 999_999_999_999_999

# --- Structured Fields subset (RFC 9651) -------------------------------------------------------


class SFParseError(ValueError):
    """The field value is not a valid Structured Field; the whole field must be ignored."""


@dataclass(frozen=True, slots=True)
class SFDate:
    """An sf-date (RFC 9651 Section 3.3.7): seconds since the epoch."""

    seconds: int


type BareItem = int | float | str | bytes | bool | SFDate
type Params = dict[str, BareItem]
type InnerList = list[tuple[BareItem, Params]]

_DIGITS = frozenset("0123456789")
_LCALPHA = frozenset("abcdefghijklmnopqrstuvwxyz")
_ALPHA = _LCALPHA | frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_KEY_CHARS = _LCALPHA | _DIGITS | frozenset("_-.*")
_TOKEN_CHARS = _ALPHA | _DIGITS | frozenset("!#$%&'*+-.^_`|~:/")


class _SFParser:
    """RFC 9651 Section 4.2 parser for Items and Dictionaries; Display Strings are rejected."""

    def __init__(self, text: str) -> None:
        if not text.isascii():
            raise SFParseError("field value is not ASCII")
        self.text = text.lstrip(" ")
        self.pos = 0

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def take(self) -> str:
        char = self.peek()
        self.pos += 1
        return char

    def skip(self, chars: str) -> None:
        while self.peek() and self.peek() in chars:
            self.pos += 1

    def finish(self) -> None:
        self.skip(" ")
        if self.pos != len(self.text):
            raise SFParseError(f"trailing characters: {self.text[self.pos :]!r}")

    def key(self) -> str:
        if not (self.peek() in _LCALPHA or self.peek() == "*"):
            raise SFParseError("key must start with lcalpha or *")
        start = self.pos
        while self.peek() and self.peek() in _KEY_CHARS:
            self.pos += 1
        return self.text[start : self.pos]

    def params(self) -> Params:
        params: Params = {}
        while self.peek() == ";":
            self.pos += 1
            self.skip(" ")
            name = self.key()
            value: BareItem = True
            if self.peek() == "=":
                self.pos += 1
                value = self.bare_item()
            params[name] = value
        return params

    def bare_item(self) -> BareItem:
        char = self.peek()
        if char == "-" or char in _DIGITS:
            return self.number()
        if char == '"':
            return self.string()
        if char in _ALPHA or char == "*":
            return self.token()
        if char == ":":
            return self.byte_sequence()
        if char == "?":
            return self.boolean()
        if char == "@":
            self.pos += 1
            seconds = self.number()
            if type(seconds) is not int:
                raise SFParseError("date must be an integer")
            return SFDate(seconds)
        raise SFParseError(f"unrecognized or unsupported item type at {char!r}")

    def number(self) -> int | float:
        sign = -1 if self.peek() == "-" else 1
        if sign == -1:
            self.pos += 1
        if not self.peek() or self.peek() not in _DIGITS:
            raise SFParseError("number must start with a digit")
        start, is_decimal = self.pos, False
        while self.peek() and (self.peek() in _DIGITS or self.peek() == "."):
            if self.peek() == ".":
                if is_decimal or self.pos - start > 12:
                    raise SFParseError("malformed decimal")
                is_decimal = True
            self.pos += 1
            if not is_decimal and self.pos - start > 15:
                raise SFParseError("integer has more than 15 digits")
            if is_decimal and self.pos - start > 16:
                raise SFParseError("decimal has more than 16 characters")
        digits = self.text[start : self.pos]
        if not is_decimal:
            return sign * int(digits)
        if digits.endswith(".") or len(digits.split(".")[1]) > 3:
            raise SFParseError("decimal needs 1 to 3 fractional digits")
        return sign * float(digits)

    def string(self) -> str:
        self.pos += 1
        out: list[str] = []
        while self.pos < len(self.text):
            char = self.take()
            if char == "\\":
                escaped = self.take()
                if escaped not in {'"', "\\"}:
                    raise SFParseError("invalid escape in string")
                out.append(escaped)
            elif char == '"':
                return "".join(out)
            elif not (0x20 <= ord(char) <= 0x7E):
                raise SFParseError("control character in string")
            else:
                out.append(char)
        raise SFParseError("unterminated string")

    def token(self) -> str:
        start = self.pos
        self.pos += 1
        while self.peek() and self.peek() in _TOKEN_CHARS:
            self.pos += 1
        return self.text[start : self.pos]

    def byte_sequence(self) -> bytes:
        self.pos += 1
        end = self.text.find(":", self.pos)
        if end < 0:
            raise SFParseError("unterminated byte sequence")
        encoded = self.text[self.pos : end]
        self.pos = end + 1
        # Section 4.2.7: padding may be absent; any other decoding failure fails parsing.
        # Strict mode also rejects characters outside the base64 alphabet, and unlike the
        # lenient mode it gives the same answer on every CPython from 3.12 to 3.15.
        try:
            return base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
        except binascii.Error as exc:
            raise SFParseError("byte sequence is not valid base64") from exc

    def boolean(self) -> bool:
        self.pos += 1
        char = self.take()
        if char == "1":
            return True
        if char == "0":
            return False
        raise SFParseError("boolean must be ?0 or ?1")

    def item_or_inner_list(self) -> tuple[BareItem | InnerList, Params]:
        if self.peek() != "(":
            return self.bare_item(), self.params()
        self.pos += 1
        members: InnerList = []
        while self.pos < len(self.text):
            self.skip(" ")
            if self.peek() == ")":
                self.pos += 1
                return members, self.params()
            members.append((self.bare_item(), self.params()))
            if self.peek() not in {" ", ")"}:
                raise SFParseError("inner list members must be separated by SP")
        raise SFParseError("unterminated inner list")


def parse_item(value: str) -> tuple[BareItem, Params]:
    """Parse an Item Structured Field (RFC 9651 Section 4.2.3)."""
    parser = _SFParser(value)
    item, params = parser.bare_item(), parser.params()
    parser.finish()
    return item, params


def parse_dictionary(value: str) -> dict[str, tuple[BareItem | InnerList, Params]]:
    """Parse a Dictionary Structured Field (RFC 9651 Section 4.2.2); duplicate keys: last wins."""
    parser = _SFParser(value)
    members: dict[str, tuple[BareItem | InnerList, Params]] = {}
    while parser.pos < len(parser.text):
        name = parser.key()
        if parser.peek() == "=":
            parser.pos += 1
            members[name] = parser.item_or_inner_list()
        else:
            members[name] = (True, parser.params())
        parser.skip(" \t")
        if parser.pos == len(parser.text):
            return members
        if parser.take() != ",":
            raise SFParseError("dictionary members must be separated by ','")
        parser.skip(" \t")
        if parser.pos == len(parser.text):
            raise SFParseError("trailing comma")
    return members


def sf_non_negative_integer(value: str | None) -> int | None:
    """Upload-Offset and Upload-Length: an Integer Item >= 0, else the field is ignored."""
    if value is None:
        return None
    try:
        item, _params = parse_item(value)
    except SFParseError:
        return None
    # bool is a subclass of int in Python, so `?1` must not pass as the integer 1.
    return item if type(item) is int and item >= 0 else None


def sf_boolean(value: str | None) -> bool | None:
    """Upload-Complete: a Boolean Item, else the field is ignored."""
    if value is None:
        return None
    try:
        item, _params = parse_item(value)
    except SFParseError:
        return None
    return item if type(item) is bool else None


@dataclass(frozen=True, slots=True)
class UploadLimits:
    """The Upload-Limit dictionary (Section 5.1); None means the limit is not announced."""

    max_size: int | None = None
    min_size: int | None = None
    max_append_size: int | None = None
    min_append_size: int | None = None
    max_age: int | None = None

    def valid(self) -> bool:
        """Member ranges and combined ranges from Sections 5.1.1 to 5.1.5."""
        positive = (self.max_size, self.max_append_size)
        non_negative = (self.min_size, self.min_append_size, self.max_age)
        if any(v is not None and v <= 0 for v in positive):
            return False
        if any(v is not None and v < 0 for v in non_negative):
            return False
        pairs = (
            (self.min_size, self.max_size),
            (self.min_append_size, self.max_append_size),
            (self.min_append_size, self.max_size),
        )
        return all(low is None or high is None or low <= high for low, high in pairs)

    def serialize(self) -> str:
        """Dictionary form; with no limits it is `min-size=0`, never empty (Section 5.2)."""
        members = [
            ("max-size", self.max_size),
            ("min-size", self.min_size),
            ("max-append-size", self.max_append_size),
            ("min-append-size", self.min_append_size),
            ("max-age", self.max_age),
        ]
        text = ", ".join(f"{name}={value}" for name, value in members if value is not None)
        return text or "min-size=0"


_LIMIT_KEYS = {
    "max-size": "max_size",
    "min-size": "min_size",
    "max-append-size": "max_append_size",
    "min-append-size": "min_append_size",
    "max-age": "max_age",
}


def parse_upload_limit(value: str | None) -> UploadLimits | None:
    """Parse Upload-Limit; any invalid known member voids the whole field (Section 5.1)."""
    if value is None:
        return None
    try:
        members = parse_dictionary(value)
    except SFParseError:
        return None
    known: dict[str, int] = {}
    for name, (member, _params) in members.items():
        if name not in _LIMIT_KEYS:
            continue
        if type(member) is not int:
            return None
        known[_LIMIT_KEYS[name]] = member
    limits = UploadLimits(**known)
    return limits if limits.valid() else None


def combined_field(headers: Iterable[tuple[str, str]], name: str) -> str | None:
    """Join every field line of `name` with ", " as RFC 9651 Section 4.2 requires."""
    values = [value for key, value in headers if key.lower() == name]
    return ", ".join(values) if values else None


# --- Upload server (Starlette) -----------------------------------------------------------------


@dataclass(eq=False)
class Upload:
    """Server-side state of one upload resource (Section 4)."""

    upload_id: str
    expires_at: float
    length: int | None = None
    data: bytearray = field(default_factory=bytearray)
    complete: bool = False
    active: bool = True
    final: tuple[int, dict[str, str], bytes] | None = None
    writer: asyncio.Task[object] | None = None
    writer_done: asyncio.Event = field(default_factory=asyncio.Event)


class _Absorb(enum.Enum):
    OK = "ok"
    DISCONNECTED = "disconnected"
    TOO_LARGE = "too-large"
    PAST_LENGTH = "past-length"


class _NoResponse(Response):
    """The peer is gone: there is nobody to send a response to."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive, send


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    return int(raw) if raw is not None and raw.isdigit() else None


class ResumableUploadService:
    """In-memory upload resources behind `POST /files` and `/uploads/{upload_id}`."""

    def __init__(
        self,
        limits: UploadLimits,
        *,
        lifetime: int = 3600,
        clock: Callable[[], float] = time.monotonic,
        new_id: Callable[[], str] = lambda: secrets.token_urlsafe(16),
        may_complete: Callable[[str], bool] = lambda _upload_id: True,
        buffer_body: bool = False,
    ) -> None:
        self.limits = limits
        self.lifetime = lifetime
        self.clock = clock
        self.new_id = new_id
        self.may_complete = may_complete
        self.buffer_body = buffer_body
        self.uploads: dict[str, Upload] = {}
        self.app = Starlette(
            routes=[
                Route("/files", self.create, methods=["POST", "OPTIONS"]),
                Route("/uploads/{upload_id}", self.offset, methods=["HEAD"]),
                Route("/uploads/{upload_id}", self.append, methods=["PATCH"]),
                Route("/uploads/{upload_id}", self.cancel, methods=["DELETE"]),
            ]
        )

    # Responses -------------------------------------------------------------------------------

    def _limits_for(self, upload: Upload | None) -> str:
        if upload is None:
            return self.limits.serialize()
        remaining = max(0, int(upload.expires_at - self.clock()))
        return UploadLimits(
            self.limits.max_size,
            self.limits.min_size,
            self.limits.max_append_size,
            self.limits.min_append_size,
            remaining,
        ).serialize()

    def _target_response(self, upload_id: str, data: bytes) -> tuple[int, dict[str, str], bytes]:
        """What the target resource `/files` produces after processing the representation."""
        summary = {
            "attachmentId": upload_id,
            "length": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        body = json.dumps(summary, separators=(",", ":")).encode()
        return 201, {"Location": f"/files/{upload_id}", "Content-Type": "application/json"}, body

    def _final(self, upload: Upload) -> Response:
        if upload.final is None:
            raise RuntimeError("upload has no final response yet")
        status, headers, body = upload.final
        return Response(body, status_code=status, headers={**headers, "Upload-Complete": "?1"})

    def _inconsistent_length(self) -> Response:
        problem = {"type": INCONSISTENT_LENGTH, "title": "inconsistent length values for upload"}
        headers = {"Upload-Complete": "?0"}
        return JSONResponse(problem, status_code=400, headers=headers, media_type=PROBLEM_JSON)

    def _too_large(self, upload: Upload | None) -> Response:
        headers = {"Upload-Limit": self._limits_for(upload), "Upload-Complete": "?0"}
        if upload is not None and upload.active:
            headers["Upload-Offset"] = str(len(upload.data))
        return Response(status_code=413, headers=headers)

    def _mismatch(self, upload: Upload, provided: int) -> Response:
        expected = len(upload.data)
        problem = {
            "type": MISMATCHING_OFFSET,
            "title": "offset from request does not match offset of resource",
            "expected-offset": expected,
            "provided-offset": provided,
        }
        headers = {"Upload-Offset": str(expected), "Upload-Complete": "?0"}
        return JSONResponse(problem, status_code=409, headers=headers, media_type=PROBLEM_JSON)

    # Shared mechanics --------------------------------------------------------------------------

    def _live(self, request: Request) -> Upload | None:
        upload = self.uploads.get(request.path_params["upload_id"])
        if upload is None or not upload.active:
            return None
        if self.clock() >= upload.expires_at:
            self._deactivate(upload)
            return None
        return upload

    def _deactivate(self, upload: Upload) -> None:
        upload.active = False
        upload.data.clear()
        self.uploads.pop(upload.upload_id, None)

    async def _preempt(self, upload: Upload) -> bool:
        """Terminate in-flight writers before serving a newer request (Section 10).

        Several requests can wait for the same stale writer, and the first one to resume may
        become a writer or delete the upload, so look again after every wait.
        Returns False when the upload was deactivated while this request waited.
        """
        while (writer := upload.writer) is not None and writer is not asyncio.current_task():
            writer.cancel()
            await upload.writer_done.wait()
        return upload.active

    def _declared_length(
        self, request: Request, offset: int, complete: bool, recorded: int | None
    ) -> tuple[int | None, bool]:
        """Return (length, consistent) from Upload-Length and Upload-Complete (Section 4.3)."""
        declared = sf_non_negative_integer(combined_field(request.headers.items(), "upload-length"))
        content_length = _content_length(request)
        inferred = offset + content_length if complete and content_length is not None else None
        known = [v for v in (declared, inferred, recorded) if v is not None]
        if len(set(known)) > 1:
            return None, False
        return (known[0] if known else None), True

    def _accept(self, upload: Upload, chunk: bytes, received: int) -> _Absorb:
        new_offset = len(upload.data) + len(chunk)
        if upload.length is not None and new_offset > upload.length:
            self._deactivate(upload)  # Section 8.2: reject, deactivate, refuse further interaction
            return _Absorb.PAST_LENGTH
        if self.limits.max_size is not None and new_offset > self.limits.max_size:
            self._deactivate(upload)
            return _Absorb.TOO_LARGE
        max_append = self.limits.max_append_size
        if max_append is not None and received > max_append:
            return _Absorb.TOO_LARGE
        upload.data += chunk
        return _Absorb.OK

    async def _absorb(self, request: Request, upload: Upload) -> _Absorb:
        """Append request content as it arrives, so an interruption keeps the received prefix."""
        upload.writer = asyncio.current_task()
        upload.writer_done = asyncio.Event()
        received = 0
        try:
            if self.buffer_body:  # the anti-pattern: nothing is stored until the body is whole
                body = await request.body()
                return self._accept(upload, body, len(body))
            async with aclosing(request.stream()) as stream:
                async for chunk in stream:
                    received += len(chunk)
                    verdict = self._accept(upload, chunk, received)
                    if verdict is not _Absorb.OK:
                        return verdict
        except ClientDisconnect:
            return _Absorb.DISCONNECTED
        finally:
            upload.writer = None
            upload.writer_done.set()
        return _Absorb.OK

    async def _ingest(self, request: Request, upload: Upload, complete: bool) -> Response | None:
        """Absorb content; return an error response, or None when the client may continue."""
        verdict = await self._absorb(request, upload)
        if verdict is _Absorb.DISCONNECTED:
            return _NoResponse()
        if verdict is _Absorb.TOO_LARGE:
            return self._too_large(upload)
        if verdict is _Absorb.PAST_LENGTH:
            return self._inconsistent_length()
        if not complete:
            return None
        if upload.length is not None and len(upload.data) != upload.length:
            return self._inconsistent_length()
        upload.length = len(upload.data)
        upload.complete = True
        if not self.may_complete(upload.upload_id):  # Section 20.4: re-check before finalizing
            upload.final = (403, {}, b"")
        else:
            upload.final = self._target_response(upload.upload_id, bytes(upload.data))
        return self._final(upload)

    # Endpoints ---------------------------------------------------------------------------------

    async def create(self, request: Request) -> Response:
        """`OPTIONS` discovers limits (Section 5.2); `POST` creates an upload (Section 6)."""
        if request.method == "OPTIONS":
            return Response(status_code=204, headers={"Upload-Limit": self.limits.serialize()})
        complete = sf_boolean(combined_field(request.headers.items(), "upload-complete"))
        if complete is None:  # a conventional upload: the target resource ignores the protocol
            status, headers, body = self._target_response(self.new_id(), await request.body())
            return Response(body, status_code=status, headers=headers)
        length, consistent = self._declared_length(request, 0, complete, None)
        if not consistent:
            return self._inconsistent_length()
        max_size = self.limits.max_size
        if length is not None and max_size is not None and length > max_size:
            return self._too_large(None)
        upload = Upload(self.new_id(), expires_at=self.clock() + self.lifetime, length=length)
        self.uploads[upload.upload_id] = upload
        problem = await self._ingest(request, upload, complete)
        if problem is not None:
            return problem
        headers = {
            "Location": f"/uploads/{upload.upload_id}",
            "Upload-Complete": "?0",
            "Upload-Offset": str(len(upload.data)),
            "Upload-Limit": self._limits_for(upload),
        }
        return Response(status_code=201, headers=headers)

    async def offset(self, request: Request) -> Response:
        """`HEAD` offset retrieval (Section 7.2)."""
        upload = self._live(request)
        if upload is None or not await self._preempt(upload):
            return Response(status_code=404)
        headers = {
            "Upload-Offset": str(len(upload.data)),
            "Upload-Complete": "?1" if upload.complete else "?0",
            "Upload-Limit": self._limits_for(upload),
            "Cache-Control": "no-store",
        }
        if upload.length is not None:
            headers["Upload-Length"] = str(upload.length)
        return Response(status_code=204, headers=headers)

    async def append(self, request: Request) -> Response:
        """`PATCH` with `application/partial-upload` appends at Upload-Offset (Section 8.2)."""
        upload = self._live(request)
        if upload is None or not await self._preempt(upload):
            return Response(status_code=404)
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type != PARTIAL_UPLOAD:  # RFC 5789 Section 2.2: unsupported patch document
            return Response(status_code=415, headers={"Accept-Patch": PARTIAL_UPLOAD})
        if "content-encoding" in request.headers:
            return Response(status_code=415, headers={"Upload-Complete": "?0"})
        offset = sf_non_negative_integer(combined_field(request.headers.items(), "upload-offset"))
        complete = sf_boolean(combined_field(request.headers.items(), "upload-complete"))
        if offset is None or complete is None:
            return Response(status_code=400)
        if offset != len(upload.data):
            return self._mismatch(upload, offset)
        if upload.complete:  # the final response was lost; replay it (Section 8.2)
            if complete and _content_length(request) == 0:
                return self._final(upload)
            return Response(status_code=400, headers={"Upload-Complete": "?0"})
        length, consistent = self._declared_length(request, offset, complete, upload.length)
        if not consistent:
            return self._inconsistent_length()
        upload.length = length
        problem = await self._ingest(request, upload, complete)
        if problem is not None:
            return problem
        headers = {"Upload-Complete": "?0", "Upload-Offset": str(len(upload.data))}
        return Response(status_code=204, headers=headers)

    async def cancel(self, request: Request) -> Response:
        """`DELETE` cancellation (Section 9.2)."""
        upload = self._live(request)
        if upload is None or not await self._preempt(upload):
            return Response(status_code=404)
        self._deactivate(upload)
        return Response(status_code=204)


# --- Client-side resume planning ----------------------------------------------------------------


class UploadAborted(RuntimeError):
    """The client must stop and cancel the upload."""


@dataclass(frozen=True, slots=True)
class AppendPlan:
    """The next `PATCH` after an offset retrieval (Sections 7.1 and 8.1)."""

    offset: int
    headers: dict[str, str]
    content: bytes


def plan_resume(head_headers: Mapping[str, str], data: bytes) -> AppendPlan:
    """Turn a `HEAD` response into the `PATCH` that completes the upload."""
    if sf_boolean(head_headers.get("upload-complete")) is True:
        raise UploadAborted("upload already complete; do not resume (Section 4.2.1)")
    offset = sf_non_negative_integer(head_headers.get("upload-offset"))
    if offset is None:
        raise UploadAborted("no valid Upload-Offset; retry the offset retrieval (Section 7.1)")
    if offset > len(data):
        raise UploadAborted("server offset beyond local data; cancel the upload (Section 7.1)")
    headers = {
        "Upload-Offset": str(offset),
        "Upload-Complete": "?1",
        "Content-Type": PARTIAL_UPLOAD,
        "Upload-Draft-Interop-Version": str(INTEROP_VERSION),
    }
    return AppendPlan(offset, headers, data[offset:])


# --- 104 (Upload Resumption Supported) on the HTTP/1.1 wire (h11) -------------------------------


def _h11_field(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> str | None:
    values = [v.decode("latin-1") for k, v in headers if k.lower() == name]
    return ", ".join(values) if values else None


def interim_fields(
    interop_version: str | None,
    *,
    location: str | None = None,
    offset: int | None = None,
    limits: UploadLimits | None = None,
) -> list[tuple[str, str]] | None:
    """Fields of a 104 on any HTTP version, or None when Appendix B forbids sending one.

    `interop_version` is the request's combined Upload-Draft-Interop-Version field value.
    """
    if sf_non_negative_integer(interop_version) != INTEROP_VERSION:
        return None
    fields = [("Upload-Draft-Interop-Version", str(INTEROP_VERSION))]
    if location is not None:
        fields.append(("Location", location))
    if limits is not None:
        fields.append(("Upload-Limit", limits.serialize()))
    if offset is not None:
        fields.append(("Upload-Offset", str(offset)))
    return fields


def interim_104(
    request: h11.Request,
    *,
    location: str | None = None,
    offset: int | None = None,
    limits: UploadLimits | None = None,
) -> h11.InformationalResponse | None:
    """Build a 104 only for an HTTP/1.1 client that sent the matching interop version.

    RFC 9110 Section 15.2 forbids 1xx to HTTP/1.0; Appendix B forbids 104 when
    Upload-Draft-Interop-Version is missing or different.
    """
    if request.http_version != b"1.1":
        return None
    version = _h11_field(request.headers, b"upload-draft-interop-version")
    fields = interim_fields(version, location=location, offset=offset, limits=limits)
    if fields is None:
        return None
    return h11.InformationalResponse(
        status_code=UPLOAD_RESUMPTION_SUPPORTED,
        headers=fields,
        reason=b"Upload Resumption Supported",
    )


def _send(conn: h11.Connection, event: h11.Event) -> bytes:
    data = conn.send(event)
    return data if data is not None else b""


def h11_creation_exchange(
    segments: Sequence[bytes], *, upload_id: str, limits: UploadLimits
) -> bytes:
    """Serve one upload creation on HTTP/1.1: 104 with Location, 104 progress, then the final 201.

    The request arrives in `segments`, as TCP would deliver it; after each segment that leaves
    the body incomplete, the server reports its offset in another 104.
    """
    conn = h11.Connection(h11.SERVER)
    out = bytearray()
    request: h11.Request | None = None
    body = bytearray()
    for segment in segments:
        conn.receive_data(segment)
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA or event is h11.PAUSED:
                break
            if isinstance(event, h11.Request):
                request = event
                interim = interim_104(event, location=f"/uploads/{upload_id}", limits=limits)
                if interim is not None:
                    out += _send(conn, interim)
            elif isinstance(event, h11.Data):
                body += event.data
            elif isinstance(event, h11.EndOfMessage):
                if request is None:
                    raise RuntimeError("h11 produced a body before a request head")
                complete = sf_boolean(_h11_field(request.headers, b"upload-complete")) is True
                content = json.dumps({"attachmentId": upload_id, "length": len(body)}).encode()
                headers = [
                    ("Upload-Complete", "?1" if complete else "?0"),
                    ("Location", f"/files/{upload_id}" if complete else f"/uploads/{upload_id}"),
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(content))),
                ]
                final = h11.Response(status_code=201, headers=headers, reason=b"Created")
                out += _send(conn, final)
                out += _send(conn, h11.Data(data=content))
                out += _send(conn, h11.EndOfMessage())
                return bytes(out)
        if request is not None:
            progress = interim_104(request, offset=len(body))
            if progress is not None:
                out += _send(conn, progress)
    raise ValueError("request ended before its content was complete")


@dataclass
class InterimTracker:
    """Client view of the 104 responses to one creation or append request (Appendix B)."""

    location: str | None = None
    offset: int = 0
    limits: UploadLimits | None = None
    ignored: int = 0

    def observe(self, status: int, headers: Sequence[tuple[bytes, bytes]]) -> None:
        if status != UPLOAD_RESUMPTION_SUPPORTED:
            return
        version = sf_non_negative_integer(_h11_field(headers, b"upload-draft-interop-version"))
        if version != INTEROP_VERSION:
            self.ignored += 1  # Appendix B: a client MUST ignore a mismatching 104
            return
        location = _h11_field(headers, b"location")
        if location is not None:
            if self.location is not None and location != self.location:
                raise UploadAborted("upload resource URI changed (Section 6.2)")
            self.location = location
        offset = sf_non_negative_integer(_h11_field(headers, b"upload-offset"))
        if offset is not None:
            if offset < self.offset:
                raise UploadAborted("offset decreased (Section 4.1)")
            self.offset = offset
        limits = parse_upload_limit(_h11_field(headers, b"upload-limit"))
        if limits is not None:
            self.limits = limits


def h11_client_receive(
    request: h11.Request, content: bytes, wire: bytes
) -> tuple[InterimTracker, int]:
    """Send `request` with `content` on an h11 client, feed it `wire`, return (tracker, status)."""
    conn = h11.Connection(h11.CLIENT)
    _send(conn, request)
    _send(conn, h11.Data(data=content))
    _send(conn, h11.EndOfMessage())
    conn.receive_data(wire)
    tracker = InterimTracker()
    while True:
        event = conn.next_event()
        if isinstance(event, h11.InformationalResponse):
            tracker.observe(event.status_code, list(event.headers))
        elif isinstance(event, h11.Response):
            return tracker, event.status_code
        else:
            raise RuntimeError(f"incomplete response, h11 returned {event!r}")
