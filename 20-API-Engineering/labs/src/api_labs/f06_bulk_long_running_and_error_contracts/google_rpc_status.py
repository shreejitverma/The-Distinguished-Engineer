"""The AIP-193 error model: google.rpc.Status with ErrorInfo, RetryInfo, QuotaFailure, BadRequest.

One `Status` value is rendered three ways that must carry identical information:
the proto3 wire bytes of `google.rpc.Status`, the AIP-193 HTTP/1.1+JSON body, and the
gRPC trailers (`grpc-status`, `grpc-message`, `grpc-status-details-bin`).
The proto3 encoder is hand-rolled so the lab needs no googleapis-common-protos package;
field numbers follow googleapis google/rpc/status.proto and google/rpc/error_details.proto.
"""

from __future__ import annotations

import base64
import enum
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Final

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from starlette.requests import Request
    from starlette.responses import Response

type JsonValue = str | int | bool | list[JsonValue] | dict[str, JsonValue]

TYPE_URL_PREFIX: Final = "type.googleapis.com/"
DETAILS_TRAILER: Final = "grpc-status-details-bin"
_REASON_RE: Final = re.compile(r"[A-Z][A-Z0-9_]+[A-Z0-9]")
_METADATA_KEY_RE: Final = re.compile(r"[a-z][a-zA-Z0-9-_]+")
_REASON_MAX: Final = 63
_METADATA_KEY_MAX: Final = 64
_NANOS_PER_SECOND: Final = 1_000_000_000


class Code(enum.IntEnum):
    """google.rpc.Code, the canonical error space shared by gRPC and AIP-193."""

    OK = 0
    CANCELLED = 1
    UNKNOWN = 2
    INVALID_ARGUMENT = 3
    DEADLINE_EXCEEDED = 4
    NOT_FOUND = 5
    ALREADY_EXISTS = 6
    PERMISSION_DENIED = 7
    RESOURCE_EXHAUSTED = 8
    FAILED_PRECONDITION = 9
    ABORTED = 10
    OUT_OF_RANGE = 11
    UNIMPLEMENTED = 12
    INTERNAL = 13
    UNAVAILABLE = 14
    DATA_LOSS = 15
    UNAUTHENTICATED = 16


# The "HTTP Mapping" comment on each value of google.rpc.Code in google/rpc/code.proto.
HTTP_STATUS: Final[Mapping[Code, int]] = {
    Code.OK: 200,
    Code.CANCELLED: 499,
    Code.UNKNOWN: 500,
    Code.INVALID_ARGUMENT: 400,
    Code.DEADLINE_EXCEEDED: 504,
    Code.NOT_FOUND: 404,
    Code.ALREADY_EXISTS: 409,
    Code.PERMISSION_DENIED: 403,
    Code.RESOURCE_EXHAUSTED: 429,
    Code.FAILED_PRECONDITION: 400,
    Code.ABORTED: 409,
    Code.OUT_OF_RANGE: 400,
    Code.UNIMPLEMENTED: 501,
    Code.INTERNAL: 500,
    Code.UNAVAILABLE: 503,
    Code.DATA_LOSS: 500,
    Code.UNAUTHENTICATED: 401,
}


class ErrorModelViolation(ValueError):
    """A Status that breaks an AIP-193 rule or a google.rpc field constraint."""


# --- proto3 wire encoding (wire type 0 = varint, 2 = length-delimited) ---


def _varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64  # proto int32/int64 negatives are 10-byte two's complement
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _len_field(number: int, payload: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _int_field(number: int, value: int) -> bytes:
    """Implicit-presence scalar: proto3 omits the default value 0."""
    return b"" if value == 0 else _varint(number << 3) + _varint(value)


def _str_field(number: int, value: str) -> bytes:
    """Implicit-presence string: proto3 omits the empty string."""
    return b"" if not value else _len_field(number, value.encode("utf-8"))


def _map_field(number: int, mapping: Mapping[str, str]) -> bytes:
    """map<string, string> is a repeated entry message {key = 1; value = 2}, keys sorted."""
    return b"".join(
        _len_field(number, _str_field(1, key) + _str_field(2, value))
        for key, value in sorted(mapping.items())
    )


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _str_map(mapping: Mapping[str, str]) -> dict[str, JsonValue]:
    return {key: mapping[key] for key in sorted(mapping)}


# --- the standard detail payloads ---


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """google.rpc.ErrorInfo: the machine-readable (reason, domain) identity of an error."""

    TYPE_NAME: ClassVar[str] = "google.rpc.ErrorInfo"
    reason: str
    domain: str
    metadata: Mapping[str, str] = field(default_factory=dict[str, str])

    def validate(self) -> None:
        if len(self.reason) > _REASON_MAX or not _REASON_RE.fullmatch(self.reason):
            raise ErrorModelViolation(f"ErrorInfo.reason {self.reason!r} is not UPPER_SNAKE_CASE")
        if not self.domain:
            raise ErrorModelViolation("ErrorInfo.domain must be set")
        for key in self.metadata:
            if len(key) > _METADATA_KEY_MAX or not _METADATA_KEY_RE.fullmatch(key):
                raise ErrorModelViolation(f"ErrorInfo.metadata key {key!r} is invalid")

    def to_proto(self) -> bytes:
        return (
            _str_field(1, self.reason) + _str_field(2, self.domain) + _map_field(3, self.metadata)
        )

    def to_json(self) -> dict[str, JsonValue]:
        out: dict[str, JsonValue] = {"reason": self.reason, "domain": self.domain}
        if self.metadata:
            out["metadata"] = _str_map(self.metadata)
        return out


@dataclass(frozen=True, slots=True)
class RetryInfo:
    """google.rpc.RetryInfo: retry_delay is a google.protobuf.Duration {seconds, nanos}."""

    TYPE_NAME: ClassVar[str] = "google.rpc.RetryInfo"
    seconds: int
    nanos: int = 0

    def validate(self) -> None:
        if self.seconds < 0 or not 0 <= self.nanos < _NANOS_PER_SECOND:
            raise ErrorModelViolation("RetryInfo.retry_delay must be a non-negative Duration")

    def to_proto(self) -> bytes:
        duration = _int_field(1, self.seconds) + _int_field(2, self.nanos)
        return _len_field(1, duration)  # message field: present even when zero

    def to_json(self) -> dict[str, JsonValue]:
        return {"retryDelay": duration_json(self.seconds, self.nanos)}


@dataclass(frozen=True, slots=True)
class QuotaViolation:
    """google.rpc.QuotaFailure.Violation."""

    subject: str
    description: str
    api_service: str = ""
    quota_metric: str = ""
    quota_id: str = ""
    quota_dimensions: Mapping[str, str] = field(default_factory=dict[str, str])
    quota_value: int = 0
    future_quota_value: int | None = None  # proto3 `optional`: explicit presence

    def to_proto(self) -> bytes:
        future = b""
        if self.future_quota_value is not None:
            future = _varint(8 << 3) + _varint(self.future_quota_value)
        return (
            _str_field(1, self.subject)
            + _str_field(2, self.description)
            + _str_field(3, self.api_service)
            + _str_field(4, self.quota_metric)
            + _str_field(5, self.quota_id)
            + _map_field(6, self.quota_dimensions)
            + _int_field(7, self.quota_value)
            + future
        )

    def to_json(self) -> dict[str, JsonValue]:
        out: dict[str, JsonValue] = {}
        for name in ("subject", "description", "api_service", "quota_metric", "quota_id"):
            value: str = getattr(self, name)
            if value:
                out[_camel(name)] = value
        if self.quota_dimensions:
            out["quotaDimensions"] = _str_map(self.quota_dimensions)
        if self.quota_value:
            out["quotaValue"] = str(self.quota_value)  # ProtoJSON writes int64 as a string
        if self.future_quota_value is not None:
            out["futureQuotaValue"] = str(self.future_quota_value)
        return out


@dataclass(frozen=True, slots=True)
class QuotaFailure:
    """google.rpc.QuotaFailure."""

    TYPE_NAME: ClassVar[str] = "google.rpc.QuotaFailure"
    violations: tuple[QuotaViolation, ...]

    def validate(self) -> None:
        if not self.violations:
            raise ErrorModelViolation("QuotaFailure needs at least one violation")

    def to_proto(self) -> bytes:
        return b"".join(_len_field(1, v.to_proto()) for v in self.violations)

    def to_json(self) -> dict[str, JsonValue]:
        return {"violations": [v.to_json() for v in self.violations]}


@dataclass(frozen=True, slots=True)
class LocalizedMessage:
    """google.rpc.LocalizedMessage: a BCP 47 locale and a user-safe message."""

    TYPE_NAME: ClassVar[str] = "google.rpc.LocalizedMessage"
    locale: str
    message: str

    def validate(self) -> None:
        if not self.locale or not self.message:
            raise ErrorModelViolation("LocalizedMessage needs both locale and message")

    def to_proto(self) -> bytes:
        return _str_field(1, self.locale) + _str_field(2, self.message)

    def to_json(self) -> dict[str, JsonValue]:
        return {"locale": self.locale, "message": self.message}


@dataclass(frozen=True, slots=True)
class FieldViolation:
    """google.rpc.BadRequest.FieldViolation; `field` is a path such as `emailAddresses[0].email`."""

    field: str
    description: str
    reason: str = ""
    localized_message: LocalizedMessage | None = None

    def to_proto(self) -> bytes:
        localized = b""
        if self.localized_message is not None:
            localized = _len_field(4, self.localized_message.to_proto())
        return (
            _str_field(1, self.field)
            + _str_field(2, self.description)
            + _str_field(3, self.reason)
            + localized
        )

    def to_json(self) -> dict[str, JsonValue]:
        out: dict[str, JsonValue] = {"field": self.field, "description": self.description}
        if self.reason:
            out["reason"] = self.reason
        if self.localized_message is not None:
            out["localizedMessage"] = self.localized_message.to_json()
        return out


@dataclass(frozen=True, slots=True)
class BadRequest:
    """google.rpc.BadRequest: every syntactic field violation of one request."""

    TYPE_NAME: ClassVar[str] = "google.rpc.BadRequest"
    field_violations: tuple[FieldViolation, ...]

    def validate(self) -> None:
        if not self.field_violations:
            raise ErrorModelViolation("BadRequest needs at least one field violation")
        for violation in self.field_violations:
            if violation.reason and not _REASON_RE.fullmatch(violation.reason):
                raise ErrorModelViolation(f"FieldViolation.reason {violation.reason!r} invalid")

    def to_proto(self) -> bytes:
        return b"".join(_len_field(1, v.to_proto()) for v in self.field_violations)

    def to_json(self) -> dict[str, JsonValue]:
        return {"fieldViolations": [v.to_json() for v in self.field_violations]}


type Detail = ErrorInfo | RetryInfo | QuotaFailure | BadRequest | LocalizedMessage


def duration_json(seconds: int, nanos: int) -> str:
    """ProtoJSON Duration: 0, 3, 6, or 9 fractional digits, then the suffix `s`."""
    if nanos == 0:
        return f"{seconds}s"
    if nanos % 1_000_000 == 0:
        return f"{seconds}.{nanos // 1_000_000:03d}s"
    if nanos % 1_000 == 0:
        return f"{seconds}.{nanos // 1_000:06d}s"
    return f"{seconds}.{nanos:09d}s"


# --- google.rpc.Status ---


@dataclass(frozen=True, slots=True)
class Status:
    """google.rpc.Status {int32 code = 1; string message = 2; repeated Any details = 3}."""

    code: Code
    message: str
    details: tuple[Detail, ...] = ()

    def validate(self) -> None:
        """Enforce the AIP-193 rules that a schema alone cannot express."""
        if self.code is Code.OK:
            raise ErrorModelViolation("an error Status must not carry Code.OK")
        names = [detail.TYPE_NAME for detail in self.details]
        if ErrorInfo.TYPE_NAME not in names:
            raise ErrorModelViolation("AIP-193: every error response must include an ErrorInfo")
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ErrorModelViolation(f"AIP-193: detail types repeated: {duplicates}")
        for detail in self.details:
            detail.validate()

    def error_info(self) -> ErrorInfo:
        return next(d for d in self.details if isinstance(d, ErrorInfo))


def encode_status(status: Status) -> bytes:
    """Serialize google.rpc.Status, packing each detail into google.protobuf.Any."""
    status.validate()
    details = b"".join(
        _len_field(
            3,
            _str_field(1, TYPE_URL_PREFIX + detail.TYPE_NAME) + _len_field(2, detail.to_proto()),
        )
        for detail in status.details
    )
    return _int_field(1, status.code) + _str_field(2, status.message) + details


def to_http_json(status: Status) -> dict[str, JsonValue]:
    """The AIP-193 HTTP/1.1+JSON body: `code` is the HTTP status, `status` the Code name."""
    status.validate()
    details: list[JsonValue] = [
        {"@type": TYPE_URL_PREFIX + detail.TYPE_NAME, **detail.to_json()}
        for detail in status.details
    ]
    return {
        "error": {
            "code": HTTP_STATUS[status.code],
            "message": status.message,
            "status": status.code.name,
            "details": details,
        }
    }


# --- gRPC trailers (grpc/doc/PROTOCOL-HTTP2.md, Responses) ---

# Percent-Byte-Unencoded = %x20-%x24 / %x26-%x7E: space and VCHAR except "%".
_UNENCODED: Final = frozenset(range(0x20, 0x7F)) - {0x25}
_HEXDIGITS: Final = frozenset("0123456789ABCDEFabcdef")
# PROTOCOL-HTTP2 suggests 8 KiB for Trailers; grpc core's soft limit default is also 8 KB.
TRAILERS_BUDGET: Final = 8192


def percent_encode(message: str) -> str:
    """grpc-message value: UTF-8, then percent-encode every byte outside _UNENCODED."""
    return "".join(
        chr(byte) if byte in _UNENCODED else f"%{byte:02X}" for byte in message.encode("utf-8")
    )


def percent_decode(value: str) -> str:
    raw = bytearray()
    i = 0
    while i < len(value):
        hex_pair = value[i + 1 : i + 3]
        if value[i] == "%" and len(hex_pair) == 2 and all(c in _HEXDIGITS for c in hex_pair):
            raw.append(int(hex_pair, 16))
            i += 3
        else:  # MUST NOT error on a broken escape: keep it as-is
            raw.extend(value[i].encode("utf-8"))
            i += 1
    return raw.decode("utf-8", errors="replace")


def trailers_size(trailers: Sequence[tuple[str, str]]) -> int:
    """HTTP/2 header-list size: name + value + 32 per field, binary values post-base64."""
    return sum(len(name) + len(value) + 32 for name, value in trailers)


def to_grpc_trailers(status: Status, *, budget: int = TRAILERS_BUDGET) -> list[tuple[str, str]]:
    """The Trailers a gRPC server sends for this Status, as HTTP/2 field values."""
    details = base64.b64encode(encode_status(status)).decode("ascii").rstrip("=")
    trailers = [
        ("grpc-status", str(int(status.code))),
        ("grpc-message", percent_encode(status.message)),
        (DETAILS_TRAILER, details),  # SHOULD emit un-padded base64
    ]
    size = trailers_size(trailers)
    if size > budget:
        # Past a peer's metadata limit the client sees a library-made RESOURCE_EXHAUSTED.
        raise ErrorModelViolation(f"trailers are {size} bytes, over the {budget}-byte budget")
    return trailers


@dataclass(frozen=True, slots=True)
class DecodedStatus:
    """What a client recovers from trailers: code, message, and Any (type_url, value) pairs."""

    code: int
    message: str
    details: tuple[tuple[str, bytes], ...]

    def detail(self, type_name: str) -> bytes | None:
        url = TYPE_URL_PREFIX + type_name
        return next((value for type_url, value in self.details if type_url == url), None)


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise ErrorModelViolation("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift >= 70:
            raise ErrorModelViolation("varint longer than 10 bytes")


def _fields(buf: bytes) -> list[tuple[int, int | bytes]]:
    """Split one message into (field number, varint or length-delimited payload)."""
    out: list[tuple[int, int | bytes]] = []
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        number, wire_type = key >> 3, key & 7
        if wire_type == 0:
            value, pos = _read_varint(buf, pos)
            out.append((number, value))
        elif wire_type == 2:
            length, pos = _read_varint(buf, pos)
            if pos + length > len(buf):
                raise ErrorModelViolation("length-delimited field overruns the buffer")
            out.append((number, buf[pos : pos + length]))
            pos += length
        elif wire_type in (1, 5):  # fixed64 / fixed32: skip unknown fields
            pos += 8 if wire_type == 1 else 4
            if pos > len(buf):
                raise ErrorModelViolation("fixed-width field overruns the buffer")
        else:
            raise ErrorModelViolation(f"unsupported wire type {wire_type}")
    return out


def _as_bytes(value: int | bytes) -> bytes:
    if isinstance(value, int):
        raise ErrorModelViolation("expected a length-delimited field")
    return value


def _as_int(value: int | bytes) -> int:
    if isinstance(value, bytes):
        raise ErrorModelViolation("expected a varint field")
    return value - (1 << 64) if value >= 1 << 63 else value


def decode_status(buf: bytes) -> DecodedStatus:
    code, message, details = 0, "", list[tuple[str, bytes]]()
    for number, value in _fields(buf):
        if number == 1:
            code = _as_int(value)
        elif number == 2:
            message = _as_bytes(value).decode("utf-8")
        elif number == 3:
            parts = dict(_fields(_as_bytes(value)))
            details.append(
                (_as_bytes(parts.get(1, b"")).decode("utf-8"), _as_bytes(parts.get(2, b"")))
            )
    return DecodedStatus(code, message, tuple(details))


def decode_error_info(buf: bytes) -> ErrorInfo:
    reason, domain, metadata = "", "", dict[str, str]()
    for number, value in _fields(buf):
        if number == 1:
            reason = _as_bytes(value).decode("utf-8")
        elif number == 2:
            domain = _as_bytes(value).decode("utf-8")
        elif number == 3:
            entry = dict(_fields(_as_bytes(value)))
            key = _as_bytes(entry.get(1, b"")).decode("utf-8")
            metadata[key] = _as_bytes(entry.get(2, b"")).decode("utf-8")
    return ErrorInfo(reason, domain, metadata)


def decode_retry_info(buf: bytes) -> RetryInfo:
    seconds = nanos = 0
    for number, value in _fields(buf):
        if number == 1:
            duration = dict(_fields(_as_bytes(value)))
            seconds = _as_int(duration.get(1, 0))
            nanos = _as_int(duration.get(2, 0))
    return RetryInfo(seconds, nanos)


def parse_grpc_trailers(trailers: Sequence[tuple[str, str]]) -> DecodedStatus:
    """Client side: rebuild the Status and enforce that details never contradict grpc-status."""
    fields = {name.lower(): value for name, value in trailers}
    code = int(fields["grpc-status"])
    message = percent_decode(fields.get("grpc-message", ""))
    encoded = fields.get(DETAILS_TRAILER)
    if encoded is None:
        return DecodedStatus(code, message, ())
    raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4))  # MUST accept both paddings
    decoded = decode_status(raw)
    if decoded.code != code:
        raise ErrorModelViolation(
            f"{DETAILS_TRAILER} code {decoded.code} contradicts grpc-status {code}"
        )
    return decoded


# --- HTTP: a Starlette exception handler that emits the AIP-193 JSON body ---


class ApiError(Exception):
    """Raise from a handler; `api_error_handler` renders it as AIP-193 JSON."""

    def __init__(self, status: Status) -> None:
        status.validate()
        super().__init__(status.message)
        self.status = status


async def api_error_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, ApiError):
        raise exc
    return JSONResponse(to_http_json(exc.status), status_code=HTTP_STATUS[exc.status.code])
