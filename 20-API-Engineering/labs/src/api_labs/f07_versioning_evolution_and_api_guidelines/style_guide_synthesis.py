"""Four published API house styles over one store, and the profile that makes a choice checkable.

Lab for "Where Google, Microsoft, Zalando, and Stripe Disagree". A ``StyleProfile`` is plain data:
one decision per wire-visible axis (field names, version carrier, pagination envelope, error
envelope, timestamp format, request encoding, default order). ``create_app`` interprets a profile
faithfully, ``list_all`` and ``normalize_error`` are the client that has to speak all of them, and
``lint_exchange`` checks one request/response pair against a profile. The linter covers member
case, version carrier, request encoding, both envelopes, link versions, and ``Vary``; member names,
time format, and default order are pinned by the tests' expected values instead.

The four constants model the published guides as this lab reads them, not the vendors' servers.
Machine-readable error codes are lab-defined except Azure's two ``api-version`` codes and Stripe's
``parameter_missing`` and ``parameter_unknown``. Page tokens are bare base64url of the last id,
which AIP-158 calls insufficient obfuscation; opacity is another note's subject. Timestamps are
whole seconds on the wire, because Unix seconds are the coarsest of the four forms.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qsl, quote, unquote, urlencode

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    from starlette.exceptions import HTTPException
    from starlette.requests import Request

CANONICAL_FIELDS = ("report_id", "display_name", "sha256_hash", "create_time")
WRITABLE_FIELDS = ("display_name", "sha256_hash")
DEFAULT_PAGE_SIZE = 3
MAX_PAGE_SIZE = 100
MEDIA_TYPE = "application/x.lab.report+json"
ERROR_DOMAIN = "reports.lab.example"
ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"
BAD_REQUEST_TYPE = "type.googleapis.com/google.rpc.BadRequest"
STRIPE_ERROR_TYPES = frozenset(
    {"api_error", "card_error", "idempotency_error", "invalid_request_error"}
)


class Case(Enum):
    SNAKE = "snake_case"
    CAMEL = "camelCase"


# Zalando rule 118 gives the snake_case pattern; AIP-122 gives this camelCase pattern for
# collection identifiers, reused here for JSON keys.
CASE_PATTERNS = {
    Case.SNAKE: re.compile(r"[a-z_][a-z_0-9]*"),
    Case.CAMEL: re.compile(r"[a-z][a-zA-Z0-9]*"),
}


class VersionCarrier(Enum):
    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    MEDIA_TYPE = "media-type"


class AbsentVersion(Enum):
    REJECT = "reject"
    DEFAULT = "default"


class PageStyle(Enum):
    PAGE_TOKEN = "page-token"  # noqa: S105 - a pagination style, not a credential
    NEXT_LINK = "next-link"
    CURSOR_LINKS = "cursor-links"
    OBJECT_ID = "object-id"


class ErrorStyle(Enum):
    GOOGLE_STATUS = "google.rpc.Status"
    AZURE_ENVELOPE = "azure-error-envelope"
    PROBLEM_JSON = "application/problem+json"
    STRIPE_ERROR = "stripe-error"


class TimeFormat(Enum):
    RFC3339 = "rfc3339"
    EPOCH_SECONDS = "epoch-seconds"


class Encoding(Enum):
    JSON = "application/json"
    FORM = "application/x-www-form-urlencoded"


class ProblemKind(Enum):
    MISSING_FIELD = "Missing required field"
    UNKNOWN_FIELD = "Unknown field"
    INVALID_BODY = "Invalid request body"
    INVALID_PAGE = "Invalid page position"
    MISSING_VERSION = "Missing API version"
    UNSUPPORTED_VERSION = "Unsupported API version"
    UNSUPPORTED_MEDIA_TYPE = "Unsupported media type"
    NOT_FOUND = "Not found"
    METHOD_NOT_ALLOWED = "Method not allowed"


@dataclass(frozen=True, slots=True)
class Report:
    """The canonical record; every dialect is a projection of it."""

    report_id: str
    display_name: str
    sha256_hash: str
    create_time: datetime

    def __post_init__(self) -> None:
        if self.create_time.tzinfo is None:
            raise ValueError("create_time must be timezone-aware")


@dataclass(frozen=True)
class WireNames:
    """One guide's field names: an explicit bijection, never a runtime case conversion."""

    case: Case
    by_canonical: Mapping[str, str]
    by_wire: Mapping[str, str] = field(init=False)

    def __post_init__(self) -> None:
        if tuple(self.by_canonical) != CANONICAL_FIELDS:
            raise ValueError(f"wire names must cover exactly {CANONICAL_FIELDS}")
        pattern = CASE_PATTERNS[self.case]
        wrong = [wire for wire in self.by_canonical.values() if not pattern.fullmatch(wire)]
        if wrong:
            raise ValueError(f"{wrong} is not {self.case.value}")
        by_wire = {wire: canonical for canonical, wire in self.by_canonical.items()}
        if len(by_wire) != len(self.by_canonical):
            raise ValueError("two canonical fields share one wire name")
        object.__setattr__(self, "by_wire", by_wire)


@dataclass(frozen=True)
class StyleProfile:
    """One decision per wire-visible axis; the app, the client, and the linter all read it."""

    name: str
    names: WireNames
    id_prefix: str
    path_prefix: str
    version_carrier: VersionCarrier
    version_name: str
    version: str
    absent_version: AbsentVersion
    page_style: PageStyle
    links_carry_version: bool
    error_style: ErrorStyle
    time_format: TimeFormat
    request_encoding: Encoding
    newest_first: bool

    @property
    def collection_path(self) -> str:
        return f"{self.path_prefix}/reports"


GOOGLE = StyleProfile(
    name="google-aip",
    names=WireNames(
        Case.CAMEL,
        {
            "report_id": "name",
            "display_name": "displayName",
            "sha256_hash": "sha256Hash",
            "create_time": "createTime",
        },
    ),
    id_prefix="reports/",
    path_prefix="/v1",
    version_carrier=VersionCarrier.PATH,
    version_name="",
    version="v1",
    absent_version=AbsentVersion.REJECT,
    page_style=PageStyle.PAGE_TOKEN,
    links_carry_version=False,
    error_style=ErrorStyle.GOOGLE_STATUS,
    time_format=TimeFormat.RFC3339,
    request_encoding=Encoding.JSON,
    newest_first=False,
)

AZURE = StyleProfile(
    name="azure",
    names=WireNames(
        Case.CAMEL,
        {
            "report_id": "id",
            "display_name": "displayName",
            "sha256_hash": "sha256Hash",
            "create_time": "createdAt",
        },
    ),
    id_prefix="",
    path_prefix="",
    version_carrier=VersionCarrier.QUERY,
    version_name="api-version",
    version="2026-06-01",
    absent_version=AbsentVersion.REJECT,
    page_style=PageStyle.NEXT_LINK,
    links_carry_version=True,
    error_style=ErrorStyle.AZURE_ENVELOPE,
    time_format=TimeFormat.RFC3339,
    request_encoding=Encoding.JSON,
    newest_first=False,
)

ZALANDO = StyleProfile(
    name="zalando",
    names=WireNames(
        Case.SNAKE,
        {
            "report_id": "id",
            "display_name": "display_name",
            "sha256_hash": "sha256_hash",
            "create_time": "created_at",
        },
    ),
    id_prefix="",
    path_prefix="",
    version_carrier=VersionCarrier.MEDIA_TYPE,
    version_name="version",
    version="2",
    absent_version=AbsentVersion.DEFAULT,
    page_style=PageStyle.CURSOR_LINKS,
    links_carry_version=False,
    error_style=ErrorStyle.PROBLEM_JSON,
    time_format=TimeFormat.RFC3339,
    request_encoding=Encoding.JSON,
    newest_first=False,
)

STRIPE = StyleProfile(
    name="stripe-v1",
    names=WireNames(
        Case.SNAKE,
        {
            "report_id": "id",
            "display_name": "display_name",
            "sha256_hash": "sha256_hash",
            "create_time": "created",
        },
    ),
    id_prefix="",
    path_prefix="/v1",
    version_carrier=VersionCarrier.HEADER,
    version_name="Stripe-Version",
    version="2026-10-28.endive",
    absent_version=AbsentVersion.DEFAULT,
    page_style=PageStyle.OBJECT_ID,
    links_carry_version=False,
    error_style=ErrorStyle.STRIPE_ERROR,
    time_format=TimeFormat.EPOCH_SECONDS,
    request_encoding=Encoding.FORM,
    newest_first=True,
)

GUIDES = (GOOGLE, AZURE, ZALANDO, STRIPE)

# Request parameter names (page size, position) and response member names (items, continuation).
PAGE_PARAMS = {
    PageStyle.PAGE_TOKEN: ("pageSize", "pageToken"),
    PageStyle.NEXT_LINK: ("maxpagesize", "continuationToken"),
    PageStyle.CURSOR_LINKS: ("limit", "cursor"),
    PageStyle.OBJECT_ID: ("limit", "starting_after"),
}
PAGE_MEMBERS = {
    PageStyle.PAGE_TOKEN: ("reports", "nextPageToken"),
    PageStyle.NEXT_LINK: ("value", "nextLink"),
    PageStyle.CURSOR_LINKS: ("items", "next"),
    PageStyle.OBJECT_ID: ("data", "has_more"),
}
_PAGE_REQUIRED = {
    PageStyle.PAGE_TOKEN: {"reports"},
    PageStyle.NEXT_LINK: {"value"},
    PageStyle.CURSOR_LINKS: {"items"},
    PageStyle.OBJECT_ID: {"object", "url", "has_more", "data"},
}
_PAGE_OPTIONAL = {
    PageStyle.PAGE_TOKEN: {"nextPageToken"},
    PageStyle.NEXT_LINK: {"nextLink"},
    PageStyle.CURSOR_LINKS: {"self", "first", "prev", "next", "last", "query"},
    PageStyle.OBJECT_ID: set[str](),
}

# The machine-readable identifier each guide puts on the wire; None means the member is omitted.
ERROR_CODES: dict[ErrorStyle, dict[ProblemKind, str | None]] = {
    ErrorStyle.GOOGLE_STATUS: {
        ProblemKind.MISSING_FIELD: "FIELD_REQUIRED",
        ProblemKind.UNKNOWN_FIELD: "FIELD_UNKNOWN",
        ProblemKind.INVALID_BODY: "BODY_INVALID",
        ProblemKind.INVALID_PAGE: "PAGE_TOKEN_INVALID",
        ProblemKind.MISSING_VERSION: "API_VERSION_MISSING",
        ProblemKind.UNSUPPORTED_VERSION: "API_VERSION_UNSUPPORTED",
        ProblemKind.UNSUPPORTED_MEDIA_TYPE: "MEDIA_TYPE_UNSUPPORTED",
        ProblemKind.NOT_FOUND: "ROUTE_NOT_FOUND",
        ProblemKind.METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
    },
    ErrorStyle.AZURE_ENVELOPE: {
        ProblemKind.MISSING_FIELD: "MissingRequiredField",
        ProblemKind.UNKNOWN_FIELD: "UnknownField",
        ProblemKind.INVALID_BODY: "InvalidRequestContent",
        ProblemKind.INVALID_PAGE: "InvalidContinuationToken",
        ProblemKind.MISSING_VERSION: "MissingApiVersionParameter",
        ProblemKind.UNSUPPORTED_VERSION: "UnsupportedApiVersionValue",
        ProblemKind.UNSUPPORTED_MEDIA_TYPE: "UnsupportedMediaType",
        ProblemKind.NOT_FOUND: "NotFound",
        ProblemKind.METHOD_NOT_ALLOWED: "MethodNotAllowed",
    },
    ErrorStyle.PROBLEM_JSON: {
        ProblemKind.MISSING_FIELD: "/problems/missing-field",
        ProblemKind.UNKNOWN_FIELD: "/problems/unknown-field",
        ProblemKind.INVALID_BODY: "/problems/invalid-body",
        ProblemKind.INVALID_PAGE: "/problems/invalid-cursor",
        ProblemKind.MISSING_VERSION: "/problems/missing-version",
        ProblemKind.UNSUPPORTED_VERSION: "/problems/unsupported-version",
        ProblemKind.UNSUPPORTED_MEDIA_TYPE: "/problems/unsupported-media-type",
        ProblemKind.NOT_FOUND: "/problems/not-found",
        ProblemKind.METHOD_NOT_ALLOWED: "/problems/method-not-allowed",
    },
    ErrorStyle.STRIPE_ERROR: {
        ProblemKind.MISSING_FIELD: "parameter_missing",
        ProblemKind.UNKNOWN_FIELD: "parameter_unknown",
        ProblemKind.INVALID_BODY: None,
        ProblemKind.INVALID_PAGE: None,
        ProblemKind.MISSING_VERSION: None,
        ProblemKind.UNSUPPORTED_VERSION: None,
        ProblemKind.UNSUPPORTED_MEDIA_TYPE: None,
        ProblemKind.NOT_FOUND: None,
        ProblemKind.METHOD_NOT_ALLOWED: None,
    },
}
# google/rpc/code.proto maps INVALID_ARGUMENT to 400 and NOT_FOUND to 404; it has no code for
# 405, 406, or 415, so the lab reports those as UNKNOWN rather than invent a mapping.
_GOOGLE_STATUS_NAMES = {400: "INVALID_ARGUMENT", 404: "NOT_FOUND"}
_ZERO_WEIGHT = re.compile(r"0(\.0{0,3})?")


@dataclass(frozen=True, slots=True)
class Problem:
    """A dialect-neutral failure; ``pointer`` is a wire field name as the client sent it."""

    kind: ProblemKind
    status: int
    message: str
    pointer: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedError:
    """What one client-side adapter recovers from any of the four error envelopes."""

    status: int
    kind: ProblemKind | None
    field: str | None
    pointer: str | None


# --- server: one interpreter for every profile -------------------------------------------------


def render_report(profile: StyleProfile, report: Report) -> dict[str, object]:
    """Project the canonical record into a guide's field names, id form, and time format."""
    moment = report.create_time.astimezone(UTC)
    created: object = (
        int(moment.timestamp())
        if profile.time_format is TimeFormat.EPOCH_SECONDS
        else moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    values: dict[str, object] = {
        "report_id": profile.id_prefix + report.report_id,
        "display_name": report.display_name,
        "sha256_hash": report.sha256_hash,
        "create_time": created,
    }
    return {profile.names.by_canonical[name]: value for name, value in values.items()}


def _vary(profile: StyleProfile) -> dict[str, str]:
    if profile.version_carrier is VersionCarrier.HEADER:
        return {"vary": profile.version_name}
    if profile.version_carrier is VersionCarrier.MEDIA_TYPE:
        return {"vary": "Accept"}
    return {}


def _respond(profile: StyleProfile, body: Mapping[str, object], status: int = 200) -> Response:
    media_type = "application/json"
    if profile.version_carrier is VersionCarrier.MEDIA_TYPE:
        media_type = f"{MEDIA_TYPE};version={profile.version}"
    return JSONResponse(body, status, headers=_vary(profile), media_type=media_type)


def _to_pointer(member: str) -> str:
    """A member name as an RFC 6901 JSON Pointer in URI fragment form."""
    return "#/" + quote(member.replace("~", "~0").replace("/", "~1"), safe="")


def _from_pointer(pointer: str) -> str:
    return unquote(pointer.removeprefix("#/")).replace("~1", "/").replace("~0", "~")


def error_response(
    profile: StyleProfile, problem: Problem, extra_headers: Mapping[str, str] | None = None
) -> Response:
    """Render one failure in the profile's error envelope."""
    code = ERROR_CODES[profile.error_style][problem.kind]
    headers = {**(extra_headers or {}), **_vary(profile)}
    media_type = "application/json"
    body: dict[str, object]
    if profile.error_style is ErrorStyle.GOOGLE_STATUS:
        info: dict[str, object] = {"@type": ERROR_INFO_TYPE, "reason": code, "domain": ERROR_DOMAIN}
        details = [info]
        if problem.pointer is not None:
            info["metadata"] = {"field": problem.pointer}
            violation = {"field": problem.pointer, "description": problem.message}
            details.append({"@type": BAD_REQUEST_TYPE, "fieldViolations": [violation]})
        status_name = _GOOGLE_STATUS_NAMES.get(problem.status, "UNKNOWN")
        body = {
            "error": {
                "code": problem.status,
                "message": problem.message,
                "status": status_name,
                "details": details,
            }
        }
    elif profile.error_style is ErrorStyle.AZURE_ENVELOPE:
        detail: dict[str, object] = {"code": code, "message": problem.message}
        if problem.pointer is not None:
            detail["target"] = problem.pointer
        body = {"error": detail}
        headers["x-ms-error-code"] = str(code)
    elif profile.error_style is ErrorStyle.PROBLEM_JSON:
        media_type = "application/problem+json"
        body = {
            "type": code,
            "title": problem.kind.value,
            "status": problem.status,
            "detail": problem.message,
        }
        if problem.pointer is not None:
            pointer = _to_pointer(problem.pointer)
            body["errors"] = [{"detail": problem.message, "pointer": pointer}]
    else:
        error: dict[str, object] = {"type": "invalid_request_error", "message": problem.message}
        if code is not None:
            error["code"] = code
        if problem.pointer is not None:
            error["param"] = problem.pointer
        body = {"error": error}
    return JSONResponse(body, problem.status, headers=headers, media_type=media_type)


def _requested_media_versions(accept_lines: Iterable[str]) -> list[str]:
    """Versions that acceptable media ranges pin; empty when the default representation will do."""
    versions: list[str] = []
    for line in accept_lines:
        for media_range in line.split(","):
            name, *pairs = (part.strip() for part in media_range.split(";"))
            parameters = {
                key.strip().lower(): value.strip().strip('"')
                for key, _, value in (pair.partition("=") for pair in pairs)
            }
            if _ZERO_WEIGHT.fullmatch(parameters.get("q", "1")):
                continue  # a weight of 0 means "not acceptable"
            version = parameters.get("version")
            if name.lower() in ("*/*", "application/*"):
                return []
            if name.lower() == MEDIA_TYPE:
                if version is None:
                    return []
                versions.append(version)
    return versions


def version_problem(profile: StyleProfile, request: Request) -> Problem | None:
    """Read the version from the profile's carrier; absent and unknown versions are policy."""
    if profile.version_carrier is VersionCarrier.PATH:
        return None  # the router only knows the versioned path
    if profile.version_carrier is VersionCarrier.QUERY:
        stated = request.query_params.getlist(profile.version_name)
    elif profile.version_carrier is VersionCarrier.HEADER:
        stated = request.headers.getlist(profile.version_name)
    else:
        stated = _requested_media_versions(request.headers.getlist("accept"))
    if not stated:
        if profile.absent_version is AbsentVersion.DEFAULT:
            return None
        message = f"{profile.version_name} is required on every request."
        return Problem(ProblemKind.MISSING_VERSION, 400, message)
    negotiated = profile.version_carrier is VersionCarrier.MEDIA_TYPE
    supported = profile.version in stated if negotiated else stated == [profile.version]
    if not supported:
        status = 406 if negotiated else 400
        message = f"Unsupported version {stated}; supported: {profile.version}."
        return Problem(ProblemKind.UNSUPPORTED_VERSION, status, message)
    return None


def _unique(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"duplicate member {name!r}")
        result[name] = value
    return result


def decode_body(profile: StyleProfile, raw: bytes) -> dict[str, object]:
    """Decode a request body in the profile's encoding; duplicate names are an error."""
    if profile.request_encoding is Encoding.FORM:
        return _unique(parse_qsl(raw.decode("utf-8"), keep_blank_values=True))
    try:
        parsed: object = json.loads(raw, object_pairs_hook=_unique)
    except RecursionError as exc:
        raise ValueError("the body is nested too deeply") from exc
    if not isinstance(parsed, dict):
        raise ValueError("the body must be a JSON object")
    return cast("dict[str, object]", parsed)


def _encode_position(report_id: str) -> str:
    return base64.urlsafe_b64encode(report_id.encode("ascii")).decode("ascii").rstrip("=")


def _decode_position(profile: StyleProfile, raw: str | None) -> str | None:
    if raw is None:
        return None
    if profile.page_style is PageStyle.OBJECT_ID:
        return raw.removeprefix(profile.id_prefix)  # the position is the id as the wire shows it
    padded = raw + "=" * (-len(raw) % 4)
    return base64.b64decode(padded, altchars=b"-_", validate=True).decode("ascii")


def _link(profile: StyleProfile, request: Request, size: int, token: str) -> str:
    size_name, position_name = PAGE_PARAMS[profile.page_style]
    query = {size_name: str(size), position_name: token}
    if profile.version_carrier is VersionCarrier.QUERY and profile.links_carry_version:
        query[profile.version_name] = profile.version
    return str(request.url.replace(query=urlencode(query)))


def create_app(
    profile: StyleProfile, reports: Sequence[Report], *, now: Callable[[], datetime]
) -> Starlette:
    """Serve ``reports`` (oldest first) in the wire dialect that ``profile`` describes."""
    store = list(reports)
    if len({report.report_id for report in store}) != len(store):
        raise ValueError("report ids must be unique")

    def page_body(
        request: Request, page: Sequence[Report], size: int, more: bool
    ) -> dict[str, Any]:
        items = [render_report(profile, report) for report in page]
        items_key, continuation_key = PAGE_MEMBERS[profile.page_style]
        body: dict[str, Any] = {items_key: items}
        if profile.page_style is PageStyle.OBJECT_ID:
            return {"object": "list", "url": request.url.path, continuation_key: more, **body}
        if profile.page_style is PageStyle.CURSOR_LINKS:
            body = {"self": str(request.url), **body}
        if more:
            token = _encode_position(page[-1].report_id)
            is_token = profile.page_style is PageStyle.PAGE_TOKEN
            body[continuation_key] = token if is_token else _link(profile, request, size, token)
        return body

    async def list_reports(request: Request) -> Response:
        rejection = version_problem(profile, request)
        if rejection is not None:
            return error_response(profile, rejection)
        size_name, position_name = PAGE_PARAMS[profile.page_style]
        ordered = store[::-1] if profile.newest_first else store
        ids = [report.report_id for report in ordered]
        try:
            size = int(request.query_params.get(size_name, "0")) or DEFAULT_PAGE_SIZE
            after = _decode_position(profile, request.query_params.get(position_name))
            if size < 0 or (after is not None and after not in ids):
                raise ValueError("page position is out of range")
        except ValueError:
            message = "The page size or page position is not valid."
            return error_response(profile, Problem(ProblemKind.INVALID_PAGE, 400, message))
        size = min(size, MAX_PAGE_SIZE)
        start = 0 if after is None else ids.index(after) + 1
        page = ordered[start : start + size]
        more = start + size < len(ordered)
        return _respond(profile, page_body(request, page, size, more))

    async def create_report(request: Request) -> Response:
        rejection = version_problem(profile, request)
        if rejection is not None:
            return error_response(profile, rejection)
        sent = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if sent != profile.request_encoding.value:
            message = f"Send {profile.request_encoding.value}, not '{sent}'."
            return error_response(
                profile, Problem(ProblemKind.UNSUPPORTED_MEDIA_TYPE, 415, message)
            )
        try:
            members = decode_body(profile, await request.body())
        except ValueError as exc:
            return error_response(profile, Problem(ProblemKind.INVALID_BODY, 400, str(exc)))
        values: dict[str, str] = {}
        for wire, value in members.items():
            canonical = profile.names.by_wire.get(wire)
            if canonical not in WRITABLE_FIELDS:
                message = f"'{wire}' is not a writable field of a report."
                return error_response(
                    profile, Problem(ProblemKind.UNKNOWN_FIELD, 400, message, wire)
                )
            if not isinstance(value, str):
                message = f"'{wire}' must be a string."
                return error_response(
                    profile, Problem(ProblemKind.INVALID_BODY, 400, message, wire)
                )
            values[canonical] = value
        for canonical in WRITABLE_FIELDS:
            if canonical not in values:
                wire = profile.names.by_canonical[canonical]
                message = f"'{wire}' is required."
                return error_response(
                    profile, Problem(ProblemKind.MISSING_FIELD, 400, message, wire)
                )
        taken = {report.report_id for report in store}
        number = len(store) + 1
        while f"rpt_{number:03d}" in taken:
            number += 1
        report = Report(f"rpt_{number:03d}", values["display_name"], values["sha256_hash"], now())
        store.append(report)
        return _respond(profile, render_report(profile, report), 201)

    async def collection(request: Request) -> Response:
        handler = create_report if request.method == "POST" else list_reports
        return await handler(request)

    async def framework_error(request: Request, exc: Exception) -> Response:
        raised = cast("HTTPException", exc)
        not_allowed = raised.status_code == 405
        kind = ProblemKind.METHOD_NOT_ALLOWED if not_allowed else ProblemKind.NOT_FOUND
        message = f"{request.method} {request.url.path} is not served here."
        return error_response(profile, Problem(kind, raised.status_code, message), raised.headers)

    return Starlette(
        routes=[Route(profile.collection_path, collection, methods=["GET", "POST"])],
        exception_handlers={404: framework_error, 405: framework_error},
    )


# --- client: the adapter every consumer of more than one guide has to write ---------------------


def version_parts(profile: StyleProfile) -> tuple[dict[str, str], dict[str, str]]:
    """Query parameters and headers that state the profile's version on a request."""
    if profile.version_carrier is VersionCarrier.QUERY:
        return {profile.version_name: profile.version}, {}
    if profile.version_carrier is VersionCarrier.HEADER:
        return {}, {profile.version_name: profile.version}
    if profile.version_carrier is VersionCarrier.MEDIA_TYPE:
        return {}, {"accept": f"{MEDIA_TYPE};version={profile.version}"}
    return {}, {}


def parse_report(profile: StyleProfile, wire: Mapping[str, Any]) -> Report:
    """Recover the canonical record from one guide's representation, ignoring unknown members."""
    values = {canonical: wire[name] for name, canonical in profile.names.by_wire.items()}
    created = values["create_time"]
    moment = (
        datetime.fromtimestamp(created, UTC)
        if profile.time_format is TimeFormat.EPOCH_SECONDS
        else datetime.fromisoformat(created)
    )
    return Report(
        str(values["report_id"]).removeprefix(profile.id_prefix),
        values["display_name"],
        values["sha256_hash"],
        moment,
    )


async def list_all(
    client: httpx.AsyncClient, profile: StyleProfile, *, page_size: int
) -> tuple[list[Report], int]:
    """Walk a collection in the profile's pagination dialect; returns records and request count."""
    size_name, position_name = PAGE_PARAMS[profile.page_style]
    items_key, continuation_key = PAGE_MEMBERS[profile.page_style]
    version_query, headers = version_parts(profile)
    first = {size_name: str(page_size), **version_query}
    url = profile.collection_path
    params: dict[str, str] | None = first
    reports: list[Report] = []
    requests = 0
    while True:
        response = await client.get(url, params=params, headers=headers)
        requests += 1
        response.raise_for_status()
        body = response.json()
        items = body[items_key]
        reports.extend(parse_report(profile, item) for item in items)
        continuation = body.get(continuation_key)
        if not continuation:
            return reports, requests
        if profile.page_style is PageStyle.PAGE_TOKEN:
            params = {**first, position_name: continuation}
        elif profile.page_style is PageStyle.OBJECT_ID:
            last_id = items[-1][profile.names.by_canonical["report_id"]]
            params = {**first, position_name: last_id}
        else:
            url, params = continuation, first  # follow the link verbatim


async def create(
    client: httpx.AsyncClient, profile: StyleProfile, members: Mapping[str, object]
) -> httpx.Response:
    """POST ``members`` (already in wire names) using the profile's request encoding."""
    query, headers = version_parts(profile)
    headers["content-type"] = profile.request_encoding.value
    if profile.request_encoding is Encoding.FORM:
        content = urlencode(members)
    else:
        content = json.dumps(members)
    return await client.post(
        profile.collection_path, params=query, headers=headers, content=content
    )


def _read_envelope(profile: StyleProfile, response: httpx.Response) -> tuple[Any, Any]:
    """The machine-readable code and the field pointer, as the profile's envelope carries them."""
    body = response.json()
    pointer: Any = None
    if profile.error_style is ErrorStyle.GOOGLE_STATUS:
        details: list[dict[str, Any]] = body["error"]["details"]
        code = next((d["reason"] for d in details if d["@type"] == ERROR_INFO_TYPE), None)
        for detail in details:
            if detail["@type"] == BAD_REQUEST_TYPE:
                pointer = detail["fieldViolations"][0]["field"]
    elif profile.error_style is ErrorStyle.AZURE_ENVELOPE:
        code = response.headers["x-ms-error-code"]
        pointer = body["error"].get("target")
    elif profile.error_style is ErrorStyle.PROBLEM_JSON:
        code = body["type"]
        if "errors" in body:
            pointer = _from_pointer(body["errors"][0]["pointer"])
    else:
        code = body["error"].get("code")
        pointer = body["error"].get("param")
    return code, pointer


def normalize_error(profile: StyleProfile, response: httpx.Response) -> NormalizedError:
    """Recover (status, kind, canonical field); a body that is not the profile's has no kind."""
    try:
        code, pointer = _read_envelope(profile, response)
    except (LookupError, TypeError, ValueError, AttributeError):
        return NormalizedError(response.status_code, None, None, None)
    kinds = {wire: kind for kind, wire in ERROR_CODES[profile.error_style].items() if wire}
    canonical = profile.names.by_wire.get(pointer) if isinstance(pointer, str) else None
    kind = kinds.get(code) if isinstance(code, str) else None
    return NormalizedError(response.status_code, kind, canonical, pointer)


# --- linter: a profile is only a style guide once an exchange can fail it ------------------------


def _member_names(value: object) -> Iterator[str]:
    """Every object member name in a JSON value, skipping ``@type`` style annotations."""
    if isinstance(value, dict):
        for name, member in cast("dict[str, object]", value).items():
            if not name.startswith("@"):
                yield name
            yield from _member_names(member)
    elif isinstance(value, list):
        for element in cast("list[object]", value):
            yield from _member_names(element)


def _error_envelope_ok(profile: StyleProfile, response: httpx.Response, body: Any) -> bool:
    media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if profile.error_style is ErrorStyle.PROBLEM_JSON:
        if media_type != "application/problem+json" or not isinstance(body, dict):
            return False
        problem = cast("dict[str, Any]", body)
        return "type" in problem and problem.get("status") == response.status_code
    error = cast("dict[str, Any]", body).get("error") if isinstance(body, dict) else None
    if media_type != "application/json" or not isinstance(error, dict):
        return False
    envelope = cast("dict[str, Any]", error)
    if profile.error_style is ErrorStyle.GOOGLE_STATUS:
        details: object = envelope.get("details")
        has_info = isinstance(details, list) and any(
            isinstance(d, dict) and cast("dict[str, Any]", d).get("@type") == ERROR_INFO_TYPE
            for d in cast("list[object]", details)
        )
        return envelope.get("code") == response.status_code and "status" in envelope and has_info
    if profile.error_style is ErrorStyle.AZURE_ENVELOPE:
        code = envelope.get("code")
        return isinstance(code, str) and response.headers.get("x-ms-error-code") == code
    return envelope.get("type") in STRIPE_ERROR_TYPES


def lint_exchange(profile: StyleProfile, response: httpx.Response) -> list[str]:
    """Rule ids that one request/response pair violates under ``profile`` (empty means clean)."""
    request = response.request
    path = request.url.path
    try:
        body: Any = json.loads(response.content) if response.content else None
    except ValueError:
        body = None  # an intermediary's HTML or plain text is a finding, not a crash
    findings: set[str] = set()

    pattern = CASE_PATTERNS[profile.names.case]
    if not all(pattern.fullmatch(name) for name in _member_names(body)):
        findings.add("field-case")

    has_path_version = re.match(r"/v\d+[a-z0-9]*/", path) is not None
    expects_path_version = profile.path_prefix != ""
    query_versions = request.url.params.get_list(profile.version_name)
    carrier = profile.version_carrier
    if (
        has_path_version != expects_path_version
        or (expects_path_version and not path.startswith(f"{profile.path_prefix}/"))
        or (carrier is VersionCarrier.QUERY and query_versions != [profile.version])
        or (carrier is not VersionCarrier.QUERY and "api-version" in request.url.params)
    ):
        findings.add("version-carrier")

    sent = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if request.method == "POST" and sent != profile.request_encoding.value:
        findings.add("request-encoding")

    if response.status_code >= 400:
        if not _error_envelope_ok(profile, response, body):
            findings.add("error-envelope")
        return sorted(findings)

    if request.method == "GET":
        members = set(cast("dict[str, Any]", body)) if isinstance(body, dict) else set[str]()
        required = _PAGE_REQUIRED[profile.page_style]
        if not required <= members <= required | _PAGE_OPTIONAL[profile.page_style]:
            findings.add("page-envelope")
        if carrier is VersionCarrier.QUERY and isinstance(body, dict):
            links = [
                httpx.URL(value)
                for value in cast("dict[str, object]", body).values()
                if isinstance(value, str) and value.startswith(("http://", "https://"))
            ]
            expected = [profile.version]
            if any(link.params.get_list(profile.version_name) != expected for link in links):
                findings.add("link-carries-version")
        vary = {name.strip().lower() for name in response.headers.get("vary", "").split(",")}
        selecting = {
            VersionCarrier.HEADER: profile.version_name,
            VersionCarrier.MEDIA_TYPE: "accept",
        }
        if carrier in selecting and selecting[carrier].lower() not in vary:
            findings.add("vary")
    return sorted(findings)


# --- design-time check: cross-axis rules a profile can break before it serves a request ----------

LINK_STYLES = frozenset({PageStyle.NEXT_LINK, PageStyle.CURSOR_LINKS})
# Member names each published error envelope defines (AIP-193 and error_details.proto, the Azure
# guidelines, RFC 9457 Section 3.1, the Stripe API reference); the lab emits a subset of them.
ERROR_MEMBERS = {
    ErrorStyle.GOOGLE_STATUS: ("error", "code", "message", "status", "details", "fieldViolations"),
    ErrorStyle.AZURE_ENVELOPE: ("error", "code", "message", "target", "details", "innererror"),
    ErrorStyle.PROBLEM_JSON: ("type", "title", "status", "detail", "instance"),
    ErrorStyle.STRIPE_ERROR: ("error", "type", "code", "message", "param", "decline_code"),
}


def profile_conflicts(profile: StyleProfile) -> list[str]:
    """Cross-axis rule ids that ``profile`` breaks as data, before any exchange exists."""
    conflicts: set[str] = set()
    carrier = profile.version_carrier
    links_can_carry = carrier is VersionCarrier.QUERY and profile.page_style in LINK_STYLES
    if links_can_carry and not profile.links_carry_version:
        conflicts.add("links-drop-query-version")
    if profile.links_carry_version and not links_can_carry:
        conflicts.add("links-carry-version-is-inert")
    if carrier is VersionCarrier.PATH:
        if profile.path_prefix != f"/{profile.version}":
            conflicts.add("version-carrier-incomplete")
    elif not profile.version_name:
        conflicts.add("version-carrier-incomplete")
    # An envelope served verbatim brings its own member names into the house name table.
    page = _PAGE_REQUIRED[profile.page_style] | _PAGE_OPTIONAL[profile.page_style]
    pattern = CASE_PATTERNS[profile.names.case]
    envelope = (*sorted(page), *ERROR_MEMBERS[profile.error_style])
    if not all(pattern.fullmatch(name) for name in envelope):
        conflicts.add("envelope-case")
    return sorted(conflicts)
