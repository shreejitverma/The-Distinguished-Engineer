"""API versioning topologies: URI path, header, Accept media type, query, and AIP-185 channels."""

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise
from typing import Final

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

VERSION_HEADER: Final = "API-Version"
VERSION_QUERY: Final = "api-version"
MAX_ACCEPT_LENGTH: Final = 1024
MAX_MEDIA_RANGES: Final = 32

_TOKEN_RE: Final = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_QVALUE_RE: Final = re.compile(r"0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?")
_VERSION_VALUE_RE: Final = re.compile(r"[1-9][0-9]{0,2}")
_SEGMENT_RE: Final = re.compile(
    r"v(?P<major>[1-9][0-9]*)(?:(?P<stability>alpha|beta)(?P<release>[1-9][0-9]*)?)?"
)
_VENDOR_SUBTYPE_RE: Final = re.compile(r"vnd\.acme\.v(?P<major>[1-9][0-9]{0,2})\+json")


class VersioningError(Exception):
    """A request whose version signals cannot be honored; carries the HTTP status to send."""

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------- Accept (RFC 9110 12.5.1)


@dataclass(frozen=True, slots=True)
class MediaRange:
    """One parsed Accept member; weight is the qvalue in integer thousandths (0..1000)."""

    type: str
    subtype: str
    params: tuple[tuple[str, str], ...]
    weight: int

    def precedence(self) -> tuple[int, int]:
        """More specific ranges win: */* < type/* < type/subtype < type/subtype;params."""
        if self.type == "*":
            return (0, 0)
        if self.subtype == "*":
            return (1, len(self.params))
        return (2, len(self.params))

    def matches(self, media_type: str, params: Mapping[str, str]) -> bool:
        main_type, _, subtype = media_type.partition("/")
        if self.type not in ("*", main_type):
            return False
        if self.subtype not in ("*", subtype):
            return False
        return all(params.get(name) == value for name, value in self.params)


def _split_unquoted(value: str, separator: str) -> list[str]:
    """Split on a separator that is not inside a quoted-string (RFC 9110 Section 5.6.4)."""
    parts: list[str] = []
    buffer: list[str] = []
    in_quotes = False
    escaped = False
    for char in value:
        if escaped:
            escaped = False
        elif in_quotes and char == "\\":
            escaped = True
        elif char == '"':
            in_quotes = not in_quotes
        elif char == separator and not in_quotes:
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    if in_quotes:
        raise VersioningError(400, "malformed_accept", "unterminated quoted-string")
    parts.append("".join(buffer))
    return parts


def qvalue_to_weight(raw: str) -> int:
    """Map an RFC 9110 Section 12.4.2 qvalue to integer thousandths, rejecting bad grammar."""
    if _QVALUE_RE.fullmatch(raw) is None:
        raise VersioningError(400, "malformed_accept", f"invalid qvalue {raw!r}")
    whole, _, fraction = raw.partition(".")
    return int(whole) * 1000 + int((fraction + "000")[:3])


def _parse_param(raw: str) -> tuple[str, str]:
    name, sep, value = raw.partition("=")
    if not sep or _TOKEN_RE.fullmatch(name) is None:
        raise VersioningError(400, "malformed_accept", f"invalid parameter {raw!r}")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = re.sub(r"\\(.)", r"\1", value[1:-1])
    elif _TOKEN_RE.fullmatch(value) is None:
        raise VersioningError(400, "malformed_accept", f"invalid parameter {raw!r}")
    return name.lower(), value


def parse_accept(value: str) -> tuple[MediaRange, ...]:
    """Parse an Accept field value with explicit size limits and strict qvalue grammar."""
    if len(value) > MAX_ACCEPT_LENGTH:
        raise VersioningError(400, "accept_too_long", f"Accept exceeds {MAX_ACCEPT_LENGTH} bytes")
    members = [member.strip() for member in _split_unquoted(value, ",")]
    members = [member for member in members if member]  # RFC 9110 5.6.1.2: ignore empties
    if len(members) > MAX_MEDIA_RANGES:
        raise VersioningError(400, "accept_too_many_ranges", f"> {MAX_MEDIA_RANGES} media ranges")
    ranges: list[MediaRange] = []
    for member in members:
        head, *raw_params = (piece.strip() for piece in _split_unquoted(member, ";"))
        main_type, slash, subtype = head.lower().partition("/")
        if (
            not slash
            or _TOKEN_RE.fullmatch(main_type) is None
            or _TOKEN_RE.fullmatch(subtype) is None
            or (main_type == "*" and subtype != "*")
        ):
            raise VersioningError(400, "malformed_accept", f"invalid media range {head!r}")
        weight = 1000
        params: list[tuple[str, str]] = []
        for raw_param in raw_params:
            name, param_value = _parse_param(raw_param)
            if name == "q":
                weight = qvalue_to_weight(param_value)
            else:
                params.append((name, param_value))
        ranges.append(MediaRange(main_type, subtype, tuple(params), weight))
    return tuple(ranges)


def _best_match(
    ranges: Iterable[MediaRange], media_type: str, params: Mapping[str, str]
) -> tuple[int, tuple[int, int]] | None:
    """(weight, precedence) of the most specific range matching a media type, if any."""
    matching = [r for r in ranges if r.matches(media_type, params)]
    if not matching:
        return None
    top = max(r.precedence() for r in matching)
    return max(r.weight for r in matching if r.precedence() == top), top


def quality_of(ranges: Iterable[MediaRange], candidate: str) -> int:
    """Weight of a candidate media type from the most specific matching range (0 if none)."""
    media_type, *raw_params = (piece.strip() for piece in candidate.split(";"))
    params = dict(_parse_param(raw) for raw in raw_params)
    match = _best_match(ranges, media_type.lower(), params)
    return 0 if match is None else match[0]


def vendor_media_type(version: int) -> str:
    return f"application/vnd.acme.v{version}+json"


# ------------------------------------------------------------- version resolution policy


class Strategy(StrEnum):
    PATH = "path"
    HEADER = "header"
    MEDIA_TYPE = "media_type"
    QUERY = "query"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class NegotiationPolicy:
    supported: tuple[int, ...] = (1, 2)
    default: int = 1
    use_header: bool = True
    use_query: bool = True
    emit_vary: bool = True


@dataclass(frozen=True, slots=True)
class VersionDecision:
    version: int
    media_type: str
    source: Strategy
    vary: tuple[str, ...]


def vary_fields(policy: NegotiationPolicy) -> tuple[str, ...]:
    """Request fields that select the representation; path and query are in the cache key."""
    fields = ["Accept"]
    if policy.use_header:
        fields.append(VERSION_HEADER)
    return tuple(fields)


def _single_version(values: list[str], where: str, policy: NegotiationPolicy) -> int | None:
    if not values:
        return None
    if len(values) > 1:
        raise VersioningError(400, "ambiguous_version", f"{where} sent {len(values)} times")
    raw = values[0].strip()
    if _VERSION_VALUE_RE.fullmatch(raw) is None:
        raise VersioningError(400, "invalid_version", f"{where} value {raw!r} is not a version")
    version = int(raw)
    if version not in policy.supported:
        raise VersioningError(400, "unsupported_version", f"{where}={version}")
    return version


def resolve_version(
    *,
    accept_values: list[str],
    header_values: list[str],
    query_values: list[str],
    policy: NegotiationPolicy,
) -> VersionDecision:
    """Pick one version from header, query, and Accept, refusing duplicates and conflicts."""
    pinned: dict[Strategy, int] = {}
    if policy.use_header and (v := _single_version(header_values, VERSION_HEADER, policy)):
        pinned[Strategy.HEADER] = v
    if policy.use_query and (v := _single_version(query_values, VERSION_QUERY, policy)):
        pinned[Strategy.QUERY] = v
    if len(set(pinned.values())) > 1:
        raise VersioningError(400, "conflicting_version_signals", repr(sorted(pinned.items())))

    # Candidate order is the tie-breaker: a wildcard Accept pins the default, never the latest.
    if pinned:
        version = next(iter(pinned.values()))
        candidates = [(vendor_media_type(version), version), ("application/json", version)]
    else:
        candidates = [
            (vendor_media_type(policy.default), policy.default),
            ("application/json", policy.default),
            *((vendor_media_type(v), v) for v in policy.supported if v != policy.default),
        ]
    if not accept_values:  # RFC 9110 12.4.1: absence means no preference
        media_type, version = candidates[0]
        source = next(iter(pinned)) if pinned else Strategy.DEFAULT
        return VersionDecision(version, media_type, source, vary_fields(policy))

    ranges = parse_accept(", ".join(accept_values))
    best: tuple[tuple[int, tuple[int, int], int], str, int] | None = None
    for index, (media_type, version) in enumerate(candidates):
        match = _best_match(ranges, media_type, {})
        if match is None or match[0] == 0:
            continue
        rank = (match[0], match[1], -index)
        if best is None or rank > best[0]:
            best = (rank, media_type, version)
    if best is None:
        available = [media_type for media_type, _ in candidates]
        raise VersioningError(406, "not_acceptable", ", ".join(available))

    (_, precedence, _), media_type, version = best
    if pinned:
        source = next(iter(pinned))
    elif precedence[0] == 2 and _VENDOR_SUBTYPE_RE.fullmatch(media_type.partition("/")[2]):
        source = Strategy.MEDIA_TYPE  # the client named a vendor type, not a wildcard
    else:
        source = Strategy.DEFAULT
    return VersionDecision(version, media_type, source, vary_fields(policy))


# ------------------------------------------------------- AIP-185 path segments and channels


@dataclass(frozen=True, slots=True)
class VersionSegment:
    major: int
    stability: str  # "stable", "beta", or "alpha"
    release: int | None  # set only for release-based versions such as v1beta2


def parse_version_segment(segment: str) -> VersionSegment | None:
    """AIP-185 grammar: major only (no v1.1), stability suffix only for alpha and beta."""
    match = _SEGMENT_RE.fullmatch(segment)
    if match is None:
        return None
    release = match.group("release")
    return VersionSegment(
        major=int(match.group("major")),
        stability=match.group("stability") or "stable",
        release=int(release) if release else None,
    )


@dataclass(frozen=True, slots=True)
class ChannelSurface:
    fields: frozenset[str]
    deprecated: frozenset[str] = frozenset()


Catalog = Mapping[str, ChannelSurface]


def channel_violations(catalog: Catalog) -> list[str]:
    """AIP-185: beta must be a superset of stable, and alpha a superset of beta."""
    problems: list[str] = []
    majors = {seg.major for name in catalog if (seg := parse_version_segment(name))}
    for major in sorted(majors):
        names = [f"v{major}", f"v{major}beta", f"v{major}alpha"]
        present = [name for name in names if name in catalog]
        for narrower, wider in pairwise(present):
            missing = catalog[narrower].fields - catalog[wider].fields
            if missing:
                problems.append(f"{wider} lacks {sorted(missing)} present in {narrower}")
    return problems


def promotion_violations(before: Catalog, after: Catalog) -> list[str]:
    """AIP-185: functionality must not arrive pre-deprecated in any channel."""
    problems = channel_violations(after)
    for name, surface in after.items():
        previous = before.get(name, ChannelSurface(frozenset()))
        arrived = surface.fields - previous.fields
        pre_deprecated = arrived & surface.deprecated
        if pre_deprecated:
            problems.append(f"{name} gains already-deprecated {sorted(pre_deprecated)}")
    return problems


# Version-agnostic storage (one record); every surface is a projection of it.
WIDGETS: Final[Mapping[int, Mapping[str, object]]] = {
    7: {"id": 7, "name": "sprocket", "labels": ["blue"], "color": "#0000ff"},
}

CATALOG: Final[Catalog] = {
    "v1": ChannelSurface(frozenset({"id", "name"})),
    "v1beta": ChannelSurface(frozenset({"id", "name", "labels"})),
    "v1alpha": ChannelSurface(frozenset({"id", "name", "labels", "color"})),
    "v2": ChannelSurface(frozenset({"id", "display_name"})),
}


def project(record: Mapping[str, object], fields: frozenset[str]) -> dict[str, object]:
    """Shape the stored record for one surface; v2 renames name -> display_name (breaking)."""
    source = {**record, "display_name": record["name"]}
    return {key: source[key] for key in sorted(fields)}


# ------------------------------------------------------------------------------ the app

_CACHEABLE: Final = "public, max-age=60"


def _error(exc: VersioningError, vary: tuple[str, ...]) -> JSONResponse:
    body: dict[str, object] = {"error": exc.code, "detail": exc.detail}
    if exc.status_code == 406:
        body["available"] = exc.detail.split(", ")  # RFC 9110 15.5.7: list what exists
    headers = {"Cache-Control": "no-store"}
    if vary:
        headers["Vary"] = ", ".join(vary)
    return JSONResponse(body, status_code=exc.status_code, headers=headers)


def build_app(policy: NegotiationPolicy | None = None) -> FastAPI:
    """One resource exposed through every topology: path, channel, header, Accept, query."""
    active = policy or NegotiationPolicy()
    app = FastAPI()

    @app.get("/widgets/{widget_id}")
    async def negotiated(widget_id: int, request: Request) -> JSONResponse:
        vary = vary_fields(active) if active.emit_vary else ()
        try:
            decision = resolve_version(
                accept_values=request.headers.getlist("accept"),
                header_values=request.headers.getlist(VERSION_HEADER),
                query_values=request.query_params.getlist(VERSION_QUERY),
                policy=active,
            )
        except VersioningError as exc:
            return _error(exc, vary)
        record = WIDGETS.get(widget_id)
        if record is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        headers = {"Cache-Control": _CACHEABLE, "X-Version-Source": decision.source.value}
        if vary:
            headers["Vary"] = ", ".join(vary)
        body = project(record, CATALOG[f"v{decision.version}"].fields)
        return JSONResponse(body, headers=headers, media_type=decision.media_type)

    @app.get("/{version_segment}/widgets/{widget_id}")
    async def path_versioned(version_segment: str, widget_id: int) -> JSONResponse:
        segment = parse_version_segment(version_segment)
        surface = CATALOG.get(version_segment)
        record = WIDGETS.get(widget_id)
        if segment is None or surface is None or record is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        # The version is part of the target URI, so caches key on it without any Vary.
        return JSONResponse(project(record, surface.fields), headers={"Cache-Control": _CACHEABLE})

    return app


# ------------------------------------------------- a shared cache per RFC 9111 Section 4.1


KeyNormalizer = Callable[[str, str | None], str | None]


@dataclass(frozen=True, slots=True)
class StoredResponse:
    selecting: tuple[tuple[str, str | None], ...]
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass
class SharedCache:
    """Secondary keys from Vary; only whitespace trimming and line combining (RFC 9111 4.1)."""

    normalizer: KeyNormalizer | None = None
    entries: dict[str, list[StoredResponse]] = field(
        default_factory=dict[str, list[StoredResponse]]
    )
    origin_hits: int = 0

    def _selecting_value(self, name: str, request_headers: list[tuple[str, str]]) -> str | None:
        values = [v.strip() for k, v in request_headers if k.lower() == name]
        combined = ", ".join(values) if values else None
        return self.normalizer(name, combined) if self.normalizer else combined

    def lookup(self, url: str, request_headers: list[tuple[str, str]]) -> StoredResponse | None:
        for stored in self.entries.get(url, []):
            nominated = [name for name, _ in stored.selecting]
            if "*" in nominated:
                continue
            current = tuple((n, self._selecting_value(n, request_headers)) for n in nominated)
            if current == stored.selecting:
                return stored
        return None

    def store(
        self, url: str, request_headers: list[tuple[str, str]], response: httpx.Response
    ) -> None:
        cache_control = response.headers.get("cache-control", "")
        if response.status_code != 200 or "max-age" not in cache_control:
            return
        vary_lines = response.headers.get_list("vary")
        vary = [name.strip().lower() for line in vary_lines for name in line.split(",")]
        selecting = tuple((n, self._selecting_value(n, request_headers)) for n in vary if n)
        stored = StoredResponse(
            selecting, response.status_code, tuple(response.headers.items()), response.content
        )
        self.entries.setdefault(url, []).append(stored)

    def entry_count(self) -> int:
        return sum(len(stored) for stored in self.entries.values())


async def fetch_through_cache(
    client: httpx.AsyncClient,
    cache: SharedCache,
    url: str,
    request_headers: list[tuple[str, str]],
) -> tuple[int, bytes, bool]:
    """Return (status, body, served_from_cache) as a shared cache in front of the origin."""
    hit = cache.lookup(url, request_headers)
    if hit is not None:
        return hit.status_code, hit.body, True
    cache.origin_hits += 1
    response = await client.get(url, headers=request_headers)
    cache.store(url, request_headers, response)
    return response.status_code, response.content, False


def edge_accept_normalizer(policy: NegotiationPolicy) -> KeyNormalizer:
    """Collapse Accept to the media type it negotiates; valid only when Accept alone selects."""

    def normalize(name: str, value: str | None) -> str | None:
        if name != "accept" or value is None:
            return value
        try:
            return resolve_version(
                accept_values=[value], header_values=[], query_values=[], policy=policy
            ).media_type
        except VersioningError as exc:
            return f"error:{exc.code}"

    return normalize
