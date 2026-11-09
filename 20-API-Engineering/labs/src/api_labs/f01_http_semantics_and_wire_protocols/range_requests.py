"""Byte-range requests, 206, 416, and If-Range (RFC 9110 Sections 13.1.5, 14, and 15.3.7)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import httpx
    from starlette.types import Receive, Scope, Send

DEFAULT_MAX_RANGES: Final = 8
_OWS: Final = " \t"
_DIGITS: Final = re.compile(r"[0-9]+")
_BOUNDARY: Final = re.compile(r"[0-9A-Za-z'()+_,./:=?-]{1,70}")
_CONTENT_RANGE: Final = re.compile(r"(?i:bytes) (?:([0-9]+)-([0-9]+)/([0-9]+|\*)|\*/([0-9]+))")


@dataclass(frozen=True, slots=True)
class ByteRange:
    """An inclusive byte range ``first..last`` (RFC 9110 Section 14.1.2)."""

    first: int
    last: int

    def __post_init__(self) -> None:
        if not 0 <= self.first <= self.last:
            raise ValueError(f"invalid byte range {self.first}-{self.last}")

    def content_range(self, complete_length: int) -> str:
        return f"bytes {self.first}-{self.last}/{complete_length}"


@dataclass(frozen=True, slots=True)
class IgnoreRange:
    """Serve the full representation with 200: the Range field is ignored."""

    reason: str


@dataclass(frozen=True, slots=True)
class Satisfiable:
    """Serve 206 with these coalesced, ascending ranges."""

    ranges: tuple[ByteRange, ...]


@dataclass(frozen=True, slots=True)
class Unsatisfiable:
    """Serve 416 with ``Content-Range: bytes */complete_length``."""

    complete_length: int


type RangeDecision = IgnoreRange | Satisfiable | Unsatisfiable


def _canonical(digits: str) -> str:
    return digits.lstrip("0") or "0"


def _decimal_lt(a: str, b: str) -> bool:
    """Compare two non-negative decimals exactly without int() on attacker-sized input."""
    ca, cb = _canonical(a), _canonical(b)
    return (len(ca), ca) < (len(cb), cb)


def _clamped_int(digits: str, ceiling: int) -> int:
    """Return ``min(int(digits), ceiling)``, converting at most as many digits as *ceiling*."""
    canonical = _canonical(digits)
    if len(canonical) > len(str(ceiling)):
        return ceiling
    return min(int(canonical), ceiling)


def coalesce(ranges: Sequence[ByteRange]) -> tuple[ByteRange, ...]:
    """Merge overlapping or adjacent ranges in O(n log n) (RFC 9110 Section 15.3.7.2 MAY)."""
    merged: list[ByteRange] = []
    for r in sorted(ranges, key=lambda item: item.first):
        if merged and r.first <= merged[-1].last + 1:
            merged[-1] = ByteRange(merged[-1].first, max(merged[-1].last, r.last))
        else:
            merged.append(r)
    return tuple(merged)


def evaluate_range(
    range_value: str, complete_length: int, *, max_ranges: int = DEFAULT_MAX_RANGES
) -> RangeDecision:
    """Decide 200, 206, or 416 for one ``Range`` field value on a GET (RFC 9110 Section 14.2)."""
    if complete_length < 0:
        raise ValueError("complete_length must be non-negative")
    unit, sep, range_set = range_value.partition("=")
    if not sep:
        return IgnoreRange("invalid ranges-specifier")
    if unit.lower() != "bytes":
        return IgnoreRange("unsupported range unit")
    if complete_length == 0:
        return IgnoreRange("zero-length representation")
    specs = [spec for spec in (part.strip(_OWS) for part in range_set.split(",")) if spec]
    if not specs:
        return IgnoreRange("invalid ranges-specifier")
    if len(specs) > max_ranges:
        return IgnoreRange("too many ranges")
    satisfiable: list[ByteRange] = []
    for spec in specs:
        first_s, dash, last_s = spec.partition("-")
        if not dash:
            return IgnoreRange("invalid ranges-specifier")
        if first_s:
            if not _DIGITS.fullmatch(first_s) or (last_s and not _DIGITS.fullmatch(last_s)):
                return IgnoreRange("invalid ranges-specifier")
            if last_s and _decimal_lt(last_s, first_s):
                return IgnoreRange("invalid ranges-specifier")
            first = _clamped_int(first_s, complete_length)
            if first >= complete_length:
                continue
            last = _clamped_int(last_s, complete_length - 1) if last_s else complete_length - 1
        else:
            if not _DIGITS.fullmatch(last_s):
                return IgnoreRange("invalid ranges-specifier")
            suffix = _clamped_int(last_s, complete_length)
            if suffix == 0:
                continue
            first, last = complete_length - suffix, complete_length - 1
        satisfiable.append(ByteRange(first, last))
    if not satisfiable:
        return Unsatisfiable(complete_length)
    return Satisfiable(coalesce(satisfiable))


def if_range_holds(
    if_range: str, *, etag: str | None, last_modified: str | None, date: str | None
) -> bool:
    """Evaluate ``If-Range`` (RFC 9110 Section 13.1.5): exact, strong, never weak."""
    value = if_range.strip(_OWS)
    if value.startswith(("W/", '"')):
        return etag is not None and not etag.startswith("W/") and value == etag
    if last_modified is None or date is None or value != last_modified:
        return False
    try:
        age = parsedate_to_datetime(date) - parsedate_to_datetime(last_modified)
    except (TypeError, ValueError):
        return False
    return age.total_seconds() >= 1


@dataclass(frozen=True, slots=True)
class ContentRange:
    """A parsed ``Content-Range``; ``first`` is None for ``bytes */N``."""

    first: int | None
    last: int | None
    complete_length: int | None


def parse_content_range(value: str) -> ContentRange:
    """Parse and validate ``Content-Range`` (RFC 9110 Section 14.4); raise on invalid."""
    match = _CONTENT_RANGE.fullmatch(value)
    if match is None:
        raise ValueError(f"malformed Content-Range: {value!r}")
    first_s, last_s, complete_s, unsatisfied_s = match.groups()
    if unsatisfied_s is not None:
        return ContentRange(None, None, int(unsatisfied_s))
    first, last = int(first_s), int(last_s)
    complete = None if complete_s == "*" else int(complete_s)
    if last < first or (complete is not None and complete <= last):
        raise ValueError(f"invalid Content-Range: {value!r}")
    return ContentRange(first, last, complete)


def render_multipart_byteranges(
    data: bytes, ranges: Sequence[ByteRange], *, content_type: str, boundary: str
) -> bytes:
    """Render ``multipart/byteranges`` content (RFC 9110 Section 14.6, RFC 2046 Section 5.1)."""
    if not _BOUNDARY.fullmatch(boundary):
        raise ValueError("boundary must be 1-70 RFC 2046 bchars")
    delimiter = b"--" + boundary.encode("ascii")
    parts: list[bytes] = []
    for r in ranges:
        chunk = data[r.first : r.last + 1]
        if delimiter in chunk:
            raise ValueError("boundary occurs inside the enclosed content")
        head = f"Content-Type: {content_type}\r\nContent-Range: {r.content_range(len(data))}\r\n"
        parts.append(delimiter + b"\r\n" + head.encode("ascii") + b"\r\n" + chunk + b"\r\n")
    parts.append(delimiter + b"--\r\n")
    return b"".join(parts)


@dataclass(frozen=True, slots=True)
class Representation:
    """A selected representation with its strong validators and a fixed ``Date``."""

    data: bytes
    content_type: str
    etag: str
    last_modified: str
    date: str


@dataclass(frozen=True, slots=True)
class RangeResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


def respond(
    method: str,
    request_headers: Mapping[str, str],
    rep: Representation,
    *,
    boundary: str,
    max_ranges: int = DEFAULT_MAX_RANGES,
) -> RangeResponse:
    """Apply RFC 9110 Section 13.2.2 step 5 then Section 14.2 to one GET."""
    common = (
        ("accept-ranges", "bytes"),
        ("date", rep.date),
        ("etag", rep.etag),
        ("last-modified", rep.last_modified),
    )
    size = len(rep.data)
    full = RangeResponse(
        200,
        (*common, ("content-type", rep.content_type), ("content-length", str(size))),
        rep.data,
    )
    range_value = request_headers.get("range")
    if method != "GET" or range_value is None:
        return full
    if_range = request_headers.get("if-range")
    if if_range is not None and not if_range_holds(
        if_range, etag=rep.etag, last_modified=rep.last_modified, date=rep.date
    ):
        return full
    match evaluate_range(range_value, size, max_ranges=max_ranges):
        case IgnoreRange():
            return full
        case Unsatisfiable(complete_length=n):
            return RangeResponse(
                416, (*common, ("content-range", f"bytes */{n}"), ("content-length", "0")), b""
            )
        case Satisfiable(ranges=(only,)):
            body = rep.data[only.first : only.last + 1]
            return RangeResponse(
                206,
                (
                    *common,
                    ("content-type", rep.content_type),
                    ("content-range", only.content_range(size)),
                    ("content-length", str(len(body))),
                ),
                body,
            )
        case Satisfiable(ranges=ranges):
            body = render_multipart_byteranges(
                rep.data, ranges, content_type=rep.content_type, boundary=boundary
            )
            return RangeResponse(
                206,
                (
                    *common,
                    ("content-type", f"multipart/byteranges; boundary={boundary}"),
                    ("content-length", str(len(body))),
                ),
                body,
            )


class RangeApp:
    """Raw ASGI app serving mutable in-memory representations with range support."""

    def __init__(
        self,
        store: dict[str, Representation],
        *,
        boundary: str,
        max_ranges: int = DEFAULT_MAX_RANGES,
    ) -> None:
        self.store = store
        self.boundary = boundary
        self.max_ranges = max_ranges

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        headers: dict[str, str] = {}
        for raw_name, raw_value in scope["headers"]:
            name, value = raw_name.decode("latin-1"), raw_value.decode("latin-1")
            headers[name] = f"{headers[name]}, {value}" if name in headers else value
        rep = self.store.get(scope["path"])
        if rep is None:
            response = RangeResponse(404, (("content-length", "0"),), b"")
        else:
            response = respond(
                scope["method"],
                headers,
                rep,
                boundary=self.boundary,
                max_ranges=self.max_ranges,
            )
        raw = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in response.headers]
        await send({"type": "http.response.start", "status": response.status, "headers": raw})
        await send({"type": "http.response.body", "body": response.body})


@dataclass(frozen=True, slots=True)
class ResumeResult:
    body: bytes
    etag: str
    resumed: bool


async def resume_download(
    client: httpx.AsyncClient, url: str, *, partial: bytes, etag: str, use_if_range: bool = True
) -> ResumeResult:
    """Resume a download; with If-Range the result is never a splice of two versions."""
    request_headers = {"range": f"bytes={len(partial)}-"}
    if use_if_range:
        request_headers["if-range"] = etag
    resp = await client.get(url, headers=request_headers)
    if resp.status_code == 200:
        return ResumeResult(resp.content, resp.headers["etag"], resumed=False)
    if resp.status_code != 206:
        raise ValueError(f"cannot resume: status {resp.status_code}")
    cr = parse_content_range(resp.headers["content-range"])
    if cr.first != len(partial) or cr.complete_length is None or cr.last != cr.complete_length - 1:
        raise ValueError(f"unexpected Content-Range: {resp.headers['content-range']}")
    new_etag = resp.headers["etag"]
    if use_if_range and new_etag != etag:
        raise ValueError("206 carries a different strong validator; refusing to combine")
    return ResumeResult(partial + resp.content, new_etag, resumed=True)
