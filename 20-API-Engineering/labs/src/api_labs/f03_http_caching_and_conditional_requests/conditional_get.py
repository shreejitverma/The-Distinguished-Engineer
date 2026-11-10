"""Conditional GET evaluation (RFC 9110 Section 13.2.2) and 304 header merging (RFC 9111)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Final

from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    import httpx
    from starlette.requests import Request

type HeaderList = list[tuple[str, str]]

# RFC 9110 Section 8.8.3: entity-tag = [ weak ] opaque-tag; etagc = %x21 / %x23-7E / obs-text.
_ENTITY_TAG: Final = re.compile(r'(W/)?("[\x21\x23-\x7e\x80-\xff]*")')
_LIST_SEPARATOR: Final = re.compile(r"[ \t]*,[ \t]*")
# Lab policy: RFC 9110 Section 5.6.1.2 only bounds empty elements ("a reasonable number");
# this cap counts empty and non-empty elements alike.
MAX_LIST_ELEMENTS: Final = 64

_DAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_LONG_DAYS: Final = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_MONTHS: Final = (
    *("Jan", "Feb", "Mar", "Apr", "May", "Jun"),
    *("Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
)
_TIME: Final = r"(\d{2}):(\d{2}):(\d{2})"
_MON: Final = "(" + "|".join(_MONTHS) + ")"
_IMF_FIXDATE: Final = re.compile(
    "(?:" + "|".join(_DAYS) + r"), (\d{2}) " + _MON + r" (\d{4}) " + _TIME + " GMT"
)
_RFC850_DATE: Final = re.compile(
    "(?:" + "|".join(_LONG_DAYS) + r"), (\d{2})-" + _MON + r"-(\d{2}) " + _TIME + " GMT"
)
_ASCTIME_DATE: Final = re.compile(
    "(?:" + "|".join(_DAYS) + ") " + _MON + r" ( \d|\d{2}) " + _TIME + r" (\d{4})"
)

# Fields a 304 must carry if a 200 would have (RFC 9110 Section 15.4.5).
NOT_MODIFIED_FIELDS: Final = frozenset(
    {"cache-control", "content-location", "date", "etag", "expires", "vary"}
)
# Fields a cache must not copy from a 304 into the stored response (RFC 9111 Sections 3.1, 3.2).
_HOP_BY_HOP: Final = frozenset(
    {"connection", "proxy-connection", "keep-alive", "te", "transfer-encoding", "upgrade"}
)
_PROXY_SPECIFIC: Final = frozenset(
    {"proxy-authenticate", "proxy-authentication-info", "proxy-authorization"}
)
_NEVER_UPDATED: Final = frozenset({"content-length", "content-range"})


@dataclass(frozen=True, slots=True)
class EntityTag:
    """An entity tag; `opaque` keeps its double quotes, as on the wire."""

    opaque: str
    weak: bool = False

    def __str__(self) -> str:
        return ("W/" if self.weak else "") + self.opaque

    @classmethod
    def parse(cls, value: str) -> EntityTag | None:
        match = _ENTITY_TAG.fullmatch(value.strip(" \t"))
        if match is None:
            return None
        return cls(opaque=match.group(2), weak=match.group(1) is not None)


def weak_match(a: EntityTag, b: EntityTag) -> bool:
    """Weak comparison (RFC 9110 Section 8.8.3.2): opaque-tags match, weakness ignored."""
    return a.opaque == b.opaque


def strong_match(a: EntityTag, b: EntityTag) -> bool:
    """Strong comparison (RFC 9110 Section 8.8.3.2): both strong and opaque-tags match."""
    return not a.weak and not b.weak and a.opaque == b.opaque


class AnyRepresentation(Enum):
    """The `If-None-Match: *` member."""

    STAR = "*"


def parse_if_none_match(value: str) -> AnyRepresentation | list[EntityTag] | None:
    """Parse `"*" / #entity-tag`; None means syntactically invalid.

    Splitting on "," is wrong because "," is a legal etagc octet, so this scans tag by tag.
    """
    text = value.strip(" \t")
    if text == "*":
        return AnyRepresentation.STAR
    tags: list[EntityTag] = []
    position, elements = 0, 0
    while position < len(text):
        elements += 1
        if elements > MAX_LIST_ELEMENTS:
            return None
        separator = _LIST_SEPARATOR.match(text, position)
        if separator is not None:  # an empty list element (RFC 9110 Section 5.6.1.2)
            position = separator.end()
            continue
        match = _ENTITY_TAG.match(text, position)
        if match is None:
            return None
        tags.append(EntityTag(opaque=match.group(2), weak=match.group(1) is not None))
        position = match.end()
        if position < len(text):
            separator = _LIST_SEPARATOR.match(text, position)
            if separator is None:
                return None
            position = separator.end()
    return tags


def _resolve_two_digit_year(
    short_year: int, month_day_time: tuple[str, str, str, str, str], now: datetime
) -> int:
    """Pick the year of an RFC 850 date (RFC 9110 Section 5.6.7).

    A timestamp that "appears to be more than 50 years in the future" is read as the most recent
    past year with the same last two digits, so the comparison is on the full timestamp: the year
    exactly 50 years ahead is still accepted until the month, day, and time pass `now`.
    """
    limit_year = now.year + 50
    year = limit_year - (limit_year - short_year) % 100  # latest year <= limit_year
    month, day, hh, mm, ss = month_day_time
    stamp = (_MONTHS.index(month) + 1, int(day), int(hh), int(mm), int(ss))
    limit = (now.month, now.day, now.hour, now.minute, now.second)
    return year - 100 if year == limit_year and stamp > limit else year


def parse_http_date(value: str, *, now: datetime) -> datetime | None:
    """Parse the three HTTP-date formats (RFC 9110 Section 5.6.7); None if invalid."""
    text = value.strip(" \t")
    if match := _IMF_FIXDATE.fullmatch(text):
        day, month, year, hh, mm, ss = match.groups()
    elif match := _RFC850_DATE.fullmatch(text):
        day, month, short_year, hh, mm, ss = match.groups()
        year = str(_resolve_two_digit_year(int(short_year), (month, day, hh, mm, ss), now))
    elif match := _ASCTIME_DATE.fullmatch(text):
        month, day, hh, mm, ss, year = match.groups()
    else:
        return None
    try:
        return datetime(
            int(year),
            _MONTHS.index(month) + 1,
            int(day),
            int(hh),
            int(mm),
            min(int(ss), 59),  # 23:59:60 is a legal leap second; datetime has no slot for it.
            tzinfo=UTC,
        )
    except ValueError:
        return None


def format_http_date(moment: datetime) -> str:
    """Generate IMF-fixdate, the only format a sender may generate."""
    utc = moment.astimezone(UTC)
    return (
        f"{_DAYS[utc.weekday()]}, {utc.day:02d} {_MONTHS[utc.month - 1]} {utc.year:04d} "
        f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d} GMT"
    )


def field_lines(headers: Iterable[tuple[str, str]], name: str) -> list[str]:
    wanted = name.lower()
    return [value for key, value in headers if key.lower() == wanted]


@dataclass(frozen=True, slots=True)
class Representation:
    """Validators of the selected representation at the origin."""

    etag: EntityTag | None
    last_modified: datetime | None


class Outcome(Enum):
    PERFORM = 200
    NOT_MODIFIED = 304
    PRECONDITION_FAILED = 412


@dataclass(frozen=True, slots=True)
class Evaluation:
    outcome: Outcome
    decided_by: str  # "if-none-match", "if-modified-since", or "none"


def evaluate_cache_validators(
    method: str,
    headers: Sequence[tuple[str, str]],
    current: Representation | None,
    *,
    now: datetime,
) -> Evaluation:
    """Steps 3 and 4 of RFC 9110 Section 13.2.2.

    Steps 1 and 2 (`If-Match`, `If-Unmodified-Since`) belong to the optimistic concurrency lab.
    `current is None` means the target resource has no current representation.
    """
    inm_lines = field_lines(headers, "if-none-match")
    if inm_lines:
        # Presence alone disables If-Modified-Since (RFC 9110 Section 13.1.3), even if invalid.
        parsed = parse_if_none_match(", ".join(inm_lines))
        if parsed is AnyRepresentation.STAR:
            condition = current is None
        elif isinstance(parsed, list):
            condition = not (
                current is not None
                and current.etag is not None
                and any(weak_match(tag, current.etag) for tag in parsed)
            )
        else:
            condition = True  # nothing listed can match
        if condition:
            return Evaluation(Outcome.PERFORM, "if-none-match")
        failed = Outcome.NOT_MODIFIED if method in {"GET", "HEAD"} else Outcome.PRECONDITION_FAILED
        return Evaluation(failed, "if-none-match")

    ims_lines = field_lines(headers, "if-modified-since")
    if (
        method in {"GET", "HEAD"}
        and len(ims_lines) == 1
        and current is not None
        and current.last_modified is not None
    ):
        since = parse_http_date(ims_lines[0], now=now)
        if since is not None:
            if current.last_modified <= since:
                return Evaluation(Outcome.NOT_MODIFIED, "if-modified-since")
            return Evaluation(Outcome.PERFORM, "if-modified-since")
    return Evaluation(Outcome.PERFORM, "none")


def not_modified_headers(full_response_headers: Sequence[tuple[str, str]]) -> HeaderList:
    """Project the 200 header list onto what a 304 sends (RFC 9110 Section 15.4.5).

    `Last-Modified` is kept only when there is no `ETag`, as the cache-update exception allows.
    """
    has_etag = bool(field_lines(full_response_headers, "etag"))
    kept: HeaderList = []
    for name, value in full_response_headers:
        lowered = name.lower()
        if lowered in NOT_MODIFIED_FIELDS or (lowered == "last-modified" and not has_etag):
            kept.append((name, value))
    return kept


# --- Cache side: RFC 9111 Sections 3.2, 4.3.1, and 4.3.4 -----------------------------------


@dataclass(slots=True)
class StoredResponse:
    status: int
    headers: HeaderList
    body: bytes
    response_time: float
    update_count: int = 0

    def etag(self) -> EntityTag | None:
        lines = field_lines(self.headers, "etag")
        return EntityTag.parse(lines[-1]) if lines else None

    def last_modified(self) -> str | None:
        lines = field_lines(self.headers, "last-modified")
        return lines[-1] if lines else None


def build_validation_headers(stored: Sequence[StoredResponse]) -> HeaderList:
    """Validators for a forwarded conditional GET (RFC 9111 Section 4.3.1).

    Entity tags are a MUST when stored; `If-Modified-Since` is a SHOULD for a single stored
    response, sent alongside the tags for intermediaries that ignore `If-None-Match`.
    """
    headers: HeaderList = []
    tags = [str(tag) for response in stored if (tag := response.etag()) is not None]
    if tags:
        headers.append(("if-none-match", ", ".join(dict.fromkeys(tags))))
    if len(stored) == 1 and (last_modified := stored[0].last_modified()) is not None:
        headers.append(("if-modified-since", last_modified))
    return headers


def select_for_update(
    initial_set: Sequence[StoredResponse], not_modified: Sequence[tuple[str, str]]
) -> list[StoredResponse]:
    """Choose which stored responses a 304 freshens (RFC 9111 Section 4.3.4, first match).

    Simplification: only a strong `ETag` counts as a strong validator; `Last-Modified` is treated
    as implicitly weak (RFC 9110 Section 8.8.2.2) because proving it strong needs `Date` history.
    """
    etag_lines = field_lines(not_modified, "etag")
    new_tag = EntityTag.parse(etag_lines[-1]) if etag_lines else None
    lm_lines = field_lines(not_modified, "last-modified")
    new_lm = lm_lines[-1] if lm_lines else None

    if new_tag is not None and not new_tag.weak:
        return [
            response
            for response in initial_set
            if (tag := response.etag()) is not None and strong_match(tag, new_tag)
        ]
    if new_tag is not None or new_lm is not None:
        matching = [
            response
            for response in initial_set
            if (
                new_tag is not None
                and (tag := response.etag()) is not None
                and weak_match(tag, new_tag)
            )
            or (new_lm is not None and response.last_modified() == new_lm)
        ]
        return [max(matching, key=lambda response: response.response_time)] if matching else []
    if (
        len(initial_set) == 1
        and initial_set[0].etag() is None
        and initial_set[0].last_modified() is None
    ):
        return [initial_set[0]]
    return []


def excluded_from_update(new_headers: Sequence[tuple[str, str]]) -> frozenset[str]:
    """Field names a cache must not copy from a 304 (RFC 9111 Sections 3.1 and 3.2)."""
    connection_options = {
        option.strip().lower()
        for value in field_lines(new_headers, "connection")
        for option in value.split(",")
        if option.strip()
    }
    return _HOP_BY_HOP | _PROXY_SPECIFIC | _NEVER_UPDATED | frozenset(connection_options)


def merge_header_fields(
    stored_headers: Sequence[tuple[str, str]],
    new_headers: Sequence[tuple[str, str]],
    *,
    protected: frozenset[str] = frozenset(),
) -> HeaderList:
    """Add each new field, replacing values already present (RFC 9111 Section 3.2).

    `protected` names fields a cache that stores processed content keeps (for example a browser
    that decoded `Content-Encoding` while storing), which Section 3.2 allows on an exceptional
    basis.
    """
    skip = excluded_from_update(new_headers) | protected
    replacements: dict[str, HeaderList] = {}
    for name, value in new_headers:
        if name.lower() not in skip:
            replacements.setdefault(name.lower(), []).append((name, value))

    merged: HeaderList = []
    emitted: set[str] = set()
    for name, value in stored_headers:
        lowered = name.lower()
        if lowered not in replacements:
            merged.append((name, value))
        elif lowered not in emitted:
            merged.extend(replacements[lowered])  # replace in place, at the first occurrence
            emitted.add(lowered)
    for lowered, lines in replacements.items():
        if lowered not in emitted:
            merged.extend(lines)
    return merged


def freshen(
    initial_set: Sequence[StoredResponse],
    not_modified: Sequence[tuple[str, str]],
    *,
    response_time: float,
) -> list[StoredResponse]:
    """Apply a 304 to the stored responses it identifies; returns the ones updated."""
    selected = select_for_update(initial_set, not_modified)
    for response in selected:
        response.headers = merge_header_fields(response.headers, not_modified)
        response.response_time = response_time
        response.update_count += 1
    return selected


# --- An origin and a revalidating client cache, wired over ASGI --------------------------


@dataclass(slots=True)
class OriginResource:
    """One mutable resource; `full_responses` counts 200 bodies the origin generated."""

    body: bytes
    version: int
    last_modified: datetime
    cache_control: str = "max-age=60"
    clock: datetime = field(default_factory=lambda: datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC))
    full_responses: int = 0
    not_modified_responses: int = 0

    def etag(self) -> EntityTag:
        return EntityTag(f'"v{self.version}"')

    def representation(self) -> Representation:
        return Representation(etag=self.etag(), last_modified=self.last_modified)

    def full_headers(self) -> HeaderList:
        return [
            ("content-type", "application/json"),
            ("cache-control", self.cache_control),
            ("date", format_http_date(self.clock)),
            ("etag", str(self.etag())),
            ("last-modified", format_http_date(self.last_modified)),
            ("vary", "accept-encoding"),
            ("x-request-cost", "expensive"),
        ]


def build_origin_app(resource: OriginResource) -> Starlette:
    async def read(request: Request) -> Response:
        headers = [(key, value) for key, value in request.headers.items()]
        evaluation = evaluate_cache_validators(
            request.method, headers, resource.representation(), now=resource.clock
        )
        full = resource.full_headers()
        if evaluation.outcome is Outcome.NOT_MODIFIED:
            resource.not_modified_responses += 1
            return Response(status_code=304, headers=dict(not_modified_headers(full)))
        resource.full_responses += 1
        return Response(content=resource.body, status_code=200, headers=dict(full))

    return Starlette(routes=[Route("/resource", read, methods=["GET"])])


@dataclass(frozen=True, slots=True)
class CacheResult:
    status: int
    headers: HeaderList
    body: bytes
    source: str  # "network", "revalidated", or "replaced"
    sent_validators: HeaderList


@dataclass(slots=True)
class RevalidatingCache:
    """A one-entry-per-URL private cache that always revalidates (freshness is out of scope)."""

    entries: dict[str, StoredResponse] = field(default_factory=dict[str, StoredResponse])

    async def get(self, client: httpx.AsyncClient, url: str, *, now: float) -> CacheResult:
        stored = self.entries.get(url)
        validators = build_validation_headers([stored]) if stored is not None else []
        response = await client.get(url, headers=validators)
        received = [(key, value) for key, value in response.headers.multi_items()]
        if stored is not None and response.status_code == 304:
            if freshen([stored], received, response_time=now):
                return CacheResult(200, stored.headers, stored.body, "revalidated", validators)
            # RFC 9111 Section 4.3.4: an unmatched 304 updates nothing; refetch unconditionally.
            response = await client.get(url)
            received = [(key, value) for key, value in response.headers.multi_items()]
        source = "network" if stored is None else "replaced"
        entry = StoredResponse(response.status_code, received, response.content, now)
        self.entries[url] = entry
        return CacheResult(entry.status, entry.headers, entry.body, source, validators)
