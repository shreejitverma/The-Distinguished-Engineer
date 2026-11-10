"""Strong and weak HTTP validators: entity-tag grammar, comparison, and date strength (RFC 9110)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime, parsedate_to_datetime
from typing import TYPE_CHECKING, Final, Literal, cast

from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request

type Comparison = Literal["strong", "weak"]
type EntityTagList = tuple[EntityTag, ...] | Literal["*"]

# RFC 9110 Section 13.1.1, 13.1.2, and 13.1.5: which comparison each precondition uses.
COMPARISON_FOR_FIELD: Final[dict[str, Comparison]] = {
    "if-match": "strong",
    "if-none-match": "weak",
    "if-range": "strong",
}
ONE_SECOND: Final = timedelta(seconds=1)
_OWS: Final = " \t"


def _is_etagc(char: str) -> bool:
    """`etagc = %x21 / %x23-7E / obs-text` (RFC 9110 Section 8.8.3)."""
    code = ord(char)
    return code == 0x21 or 0x23 <= code <= 0x7E or 0x80 <= code <= 0xFF


@dataclass(frozen=True, slots=True)
class EntityTag:
    """An `entity-tag`: an opaque quoted string, optionally prefixed by the weakness indicator."""

    opaque: str
    weak: bool = False

    def __post_init__(self) -> None:
        if not all(_is_etagc(char) for char in self.opaque):
            raise ValueError(f"opaque-tag contains a non-etagc character: {self.opaque!r}")

    def __str__(self) -> str:
        return f'W/"{self.opaque}"' if self.weak else f'"{self.opaque}"'


def _scan_entity_tag(value: str, start: int) -> tuple[EntityTag, int]:
    """Parse one `entity-tag` at `value[start:]`; return it and the index just past it."""
    weak = value.startswith("W/", start)
    index = start + 2 if weak else start
    if index >= len(value) or value[index] != '"':
        raise ValueError(f"entity-tag must start with W/ or DQUOTE: {value[start:]!r}")
    close = value.find('"', index + 1)
    if close == -1:
        raise ValueError(f"unterminated opaque-tag: {value[start:]!r}")
    return EntityTag(value[index + 1 : close], weak=weak), close + 1


def parse_entity_tag(value: str) -> EntityTag:
    """Parse an `ETag` field value strictly (RFC 9110 Section 8.8.3); `w/` is not `W/`."""
    stripped = value.strip(_OWS)
    tag, end = _scan_entity_tag(stripped, 0)
    if end != len(stripped):
        raise ValueError(f"trailing data after entity-tag: {stripped[end:]!r}")
    return tag


def parse_entity_tag_list(value: str) -> EntityTagList:
    """Parse `"*" / #entity-tag` (RFC 9110 Section 13.1.1) with a quote-aware scanner.

    A comma is a legal `etagc`, so splitting the field value on `,` is wrong.
    Empty list elements are ignored (RFC 9110 Section 5.6.1).
    """
    stripped = value.strip(_OWS)
    if stripped == "*":
        return "*"
    tags: list[EntityTag] = []
    index = 0
    while index < len(stripped):
        char = stripped[index]
        if char in _OWS or char == ",":
            index += 1
            continue
        if char == "*":
            raise ValueError("'*' cannot be combined with other list members")
        tag, index = _scan_entity_tag(stripped, index)
        tags.append(tag)
        while index < len(stripped) and stripped[index] in _OWS:
            index += 1
        if index < len(stripped) and stripped[index] != ",":
            raise ValueError(f"expected ',' after entity-tag: {stripped[index:]!r}")
    if not tags:
        raise ValueError("empty entity-tag list")
    return tuple(tags)


def strong_compare(left: EntityTag, right: EntityTag) -> bool:
    """Both tags are not weak and their opaque-tags match octet by octet (Section 8.8.3.2)."""
    return not left.weak and not right.weak and left.opaque == right.opaque


def weak_compare(left: EntityTag, right: EntityTag) -> bool:
    """Opaque-tags match octet by octet, whatever either weakness flag is (Section 8.8.3.2)."""
    return left.opaque == right.opaque


def _any_match(field_value: str, current: EntityTag, comparison: Comparison) -> bool:
    members = parse_entity_tag_list(field_value)
    if isinstance(members, str):
        return True
    compare = strong_compare if comparison == "strong" else weak_compare
    return any(compare(member, current) for member in members)


def if_match_holds(field_value: str, current: EntityTag | None) -> bool:
    """Evaluate `If-Match` with the strong comparison (RFC 9110 Section 13.1.1)."""
    if current is None:
        return False
    return _any_match(field_value, current, COMPARISON_FOR_FIELD["if-match"])


def if_none_match_holds(field_value: str, current: EntityTag | None) -> bool:
    """Evaluate `If-None-Match` with the weak comparison (RFC 9110 Section 13.1.2)."""
    if current is None:
        return True
    return not _any_match(field_value, current, COMPARISON_FOR_FIELD["if-none-match"])


def strong_etag(representation_data: bytes) -> EntityTag:
    """A strong tag: a collision-resistant hash of the exact bytes a `200 OK` would carry."""
    return EntityTag(hashlib.sha256(representation_data).hexdigest()[:32])


def weak_json_etag(document: object) -> EntityTag:
    """A weak tag over the canonical JSON form: equal for semantically equal documents."""
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return EntityTag(hashlib.sha256(canonical.encode("ascii")).hexdigest()[:32], weak=True)


def format_http_date(moment: datetime) -> str:
    """IMF-fixdate (RFC 9110 Section 5.6.7); sub-second precision is discarded."""
    return format_datetime(moment.astimezone(UTC), usegmt=True)


def parse_http_date(value: str) -> datetime | None:
    """Accept all three `HTTP-date` forms in UTC; return `None` for anything invalid.

    `email.utils.parsedate_to_datetime` also accepts numeric zones such as `+0500`,
    which `HTTP-date` forbids, so a non-UTC offset is rejected here.
    """
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    if parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(UTC)


def last_modified_for_response(modified_at: datetime, date: datetime) -> datetime:
    """Clamp a future modification time to the message origination `Date` (Section 8.8.2.1)."""
    return min(modified_at, date)


def last_modified_is_strong(last_modified: datetime, date: datetime) -> bool:
    """Cache-side strength rule: `Date` is at least one second after `Last-Modified`.

    Both values are compared at the one-second resolution they have on the wire
    (RFC 9110 Section 8.8.2.2).
    """
    on_wire_lm = last_modified.replace(microsecond=0)
    on_wire_date = date.replace(microsecond=0)
    return on_wire_date - on_wire_lm >= ONE_SECOND


def if_range_holds(
    field_value: str,
    current_etag: EntityTag | None,
    last_modified: datetime | None,
    *,
    date_is_strong: bool,
) -> bool:
    """Evaluate `If-Range` (RFC 9110 Section 13.1.5); `False` means ignore `Range` and send 200.

    An entity-tag is recognized by a DQUOTE in the first three characters.
    A weak tag can never pass the strong comparison, and an `HTTP-date` passes only
    when it is a strong validator and exactly equals `Last-Modified`.
    """
    stripped = field_value.strip(_OWS)
    if '"' in stripped[:3]:
        if current_etag is None:
            return False
        try:
            presented = parse_entity_tag(stripped)
        except ValueError:
            return False
        return strong_compare(presented, current_etag)
    presented_date = parse_http_date(stripped)
    if presented_date is None or last_modified is None or not date_is_strong:
        return False
    return presented_date == last_modified.replace(microsecond=0)


@dataclass(slots=True)
class Document:
    """An in-memory JSON resource with a sub-second modification clock."""

    body: dict[str, object]
    modified_at: datetime
    changes_in_modified_second: int = 1

    def replace(self, body: dict[str, object], now: datetime) -> None:
        same_second = now.replace(microsecond=0) == self.modified_at.replace(microsecond=0)
        self.changes_in_modified_second = self.changes_in_modified_second + 1 if same_second else 1
        self.body = body
        self.modified_at = now


def serialize(body: dict[str, object]) -> bytes:
    """Compact JSON in insertion order: key order is part of the representation data."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _single_range(field_value: str, length: int) -> tuple[int, int] | None:
    """Parse `bytes=first-last` or `bytes=first-`; ignore anything else (RFC 9110 Section 14.2)."""
    unit, _, spec = field_value.partition("=")
    first, dash, last = spec.partition("-")
    if unit.strip().lower() != "bytes" or not dash or not first.isdigit():
        return None
    if last and not last.isdigit():
        return None
    start = int(first)
    end = min(int(last), length - 1) if last else length - 1
    if start > end:
        return None
    return start, end


def build_document_app(
    document: Document, *, weak_tags: bool, clock: Callable[[], datetime]
) -> Starlette:
    """`GET` and `PUT` on `/doc`, applying each precondition with its RFC 9110 comparison."""

    def current_tag() -> EntityTag:
        return weak_json_etag(document.body) if weak_tags else strong_etag(serialize(document.body))

    async def get_document(request: Request) -> Response:
        now = clock()
        data = serialize(document.body)
        tag = current_tag()
        last_modified = last_modified_for_response(document.modified_at, now)
        headers = {
            "etag": str(tag),
            "last-modified": format_http_date(last_modified),
            "date": format_http_date(now),
            "accept-ranges": "bytes",
        }
        if_none_match = request.headers.get("if-none-match")
        if if_none_match is not None and not if_none_match_holds(if_none_match, tag):
            return Response(status_code=304, headers=headers)
        range_value = request.headers.get("range")
        span = _single_range(range_value, len(data)) if range_value is not None else None
        if span is not None:
            if_range = request.headers.get("if-range")
            # Origin-side strength: the representation changed only once in its LM second.
            date_is_strong = document.changes_in_modified_second == 1
            if if_range is None or if_range_holds(
                if_range, tag, last_modified, date_is_strong=date_is_strong
            ):
                start, end = span
                headers["content-range"] = f"bytes {start}-{end}/{len(data)}"
                return Response(
                    data[start : end + 1], 206, headers=headers, media_type="application/json"
                )
        return Response(data, 200, headers=headers, media_type="application/json")

    async def put_document(request: Request) -> Response:
        if_match = request.headers.get("if-match")
        if if_match is not None and not if_match_holds(if_match, current_tag()):
            return Response(status_code=412, headers={"etag": str(current_tag())})
        payload: object = await request.json()
        if not isinstance(payload, dict):
            return Response(status_code=422)
        document.replace(cast("dict[str, object]", payload), clock())
        return Response(status_code=204, headers={"etag": str(current_tag())})

    return Starlette(
        routes=[
            Route("/doc", get_document, methods=["GET"]),
            Route("/doc", put_document, methods=["PUT"]),
        ]
    )
