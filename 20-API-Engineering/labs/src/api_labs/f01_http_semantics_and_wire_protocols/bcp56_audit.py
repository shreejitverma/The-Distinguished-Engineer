"""Executable BCP 56 (RFC 9205) audit rules for recorded HTTP exchanges.

Each rule maps one anti-pattern from RFC 9205 to a mechanical check over a
recorded request/response pair, so an API's conformance can be gated in CI.
The registry snapshots below were taken from the IANA HTTP registries (as of 2026-11).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

import h2.config
import h2.connection
import h2.events
import h11
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Sequence

    import httpx
    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

Fields = tuple[tuple[str, str], ...]

# IANA "HTTP Method Registry" (as of 2026-11); "*" is reserved there, not a method.
REGISTERED_METHODS: frozenset[str] = frozenset(
    {
        "ACL", "BASELINE-CONTROL", "BIND", "CHECKIN", "CHECKOUT", "CONNECT", "COPY",
        "DELETE", "GET", "HEAD", "LABEL", "LINK", "LOCK", "MERGE", "MKACTIVITY",
        "MKCALENDAR", "MKCOL", "MKREDIRECTREF", "MKWORKSPACE", "MOVE", "OPTIONS",
        "ORDERPATCH", "PATCH", "POST", "PRI", "PROPFIND", "PROPPATCH", "PUT", "QUERY",
        "REBIND", "REPORT", "SEARCH", "TRACE", "UNBIND", "UNCHECKOUT", "UNLINK",
        "UNLOCK", "UPDATE", "UPDATEREDIRECTREF", "VERSION-CONTROL",
    }
)  # fmt: skip

# IANA "HTTP Status Code Registry" (as of 2026-11), minus 306 and 418, which are
# registered as "(Unused)" and reserved (RFC 9110 Sections 15.4.7 and 15.5.19).
REGISTERED_STATUS_CODES: frozenset[int] = frozenset(
    {
        100, 101, 102, 103, 104,
        200, 201, 202, 203, 204, 205, 206, 207, 208, 226,
        300, 301, 302, 303, 304, 305, 307, 308,
        400, 401, 402, 403, 404, 405, 406, 407, 408, 409, 410, 411, 412, 413, 414,
        415, 416, 417, 421, 422, 423, 424, 425, 426, 428, 429, 431, 451,
        500, 501, 502, 503, 504, 505, 506, 507, 508, 510, 511,
    }
)  # fmt: skip

# The only "X-" names in the IANA "Hypertext Transfer Protocol (HTTP) Field Name
# Registry" (as of 2026-11).
REGISTERED_X_FIELDS: frozenset[str] = frozenset({"x-content-type-options", "x-frame-options"})

# RFC 9110 Section 15.1: status codes defined there as heuristically cacheable.
HEURISTICALLY_CACHEABLE: frozenset[int] = frozenset(
    {200, 203, 204, 206, 300, 301, 308, 404, 405, 410, 414, 501}
)

_EXPLICIT_CACHE_DIRECTIVES = frozenset({"max-age", "s-maxage", "no-store", "no-cache"})
PROBLEM_JSON = "application/problem+json"


@dataclass(frozen=True, slots=True)
class Exchange:
    """One recorded request/response pair; field names are stored lowercased."""

    method: str
    target: str
    request_fields: Fields
    request_content: bytes
    status: int
    response_fields: Fields
    response_content: bytes

    def request_field(self, name: str) -> str | None:
        return _field(self.request_fields, name)

    def response_field(self, name: str) -> str | None:
        return _field(self.response_fields, name)


@dataclass(frozen=True, slots=True)
class Finding:
    """A violated rule and the RFC section that states it."""

    rule: str
    section: str
    detail: str


@dataclass(frozen=True, slots=True)
class ErrorClass:
    """Who most plausibly produced an error response, and how a client should act on it."""

    origin: Literal["application", "generic"]
    kind: str
    effective_status: int


def _field(fields: Fields, name: str) -> str | None:
    wanted = name.lower()
    values = [value for key, value in fields if key == wanted]
    return ", ".join(values) if values else None


def _tokens(value: str | None) -> frozenset[str]:
    if value is None:
        return frozenset()
    return frozenset(
        part.split("=", 1)[0].strip().lower() for part in value.split(",") if part.strip()
    )


def _media_type(value: str | None) -> str:
    return "" if value is None else value.split(";", 1)[0].strip().lower()


def _json_object(content: bytes) -> dict[str, object] | None:
    try:
        document: object = json.loads(content)
    except ValueError:
        return None
    return cast("dict[str, object]", document) if isinstance(document, dict) else None


def _normalise(fields: Sequence[tuple[str, str]]) -> Fields:
    return tuple((name.lower(), value) for name, value in fields)


def exchange_from_httpx(response: httpx.Response) -> Exchange:
    """Record a completed httpx exchange (request as sent, response as received)."""
    request = response.request
    return Exchange(
        method=request.method,
        target=request.url.raw_path.decode("ascii"),
        request_fields=_normalise(list(request.headers.items())),
        request_content=request.content,
        status=response.status_code,
        response_fields=_normalise(list(response.headers.items())),
        response_content=response.content,
    )


def _field_name_findings(names: Sequence[str]) -> list[Finding]:
    findings: list[Finding] = []
    for name in names:
        if name.startswith("x-") and name not in REGISTERED_X_FIELDS:
            findings.append(
                Finding("x-prefixed-field", "RFC 9110 Section 16.3.2.1", f"{name} uses X-")
            )
        if "_" in name:
            findings.append(
                Finding("underscore-field-name", "RFC 9110 Section 17.10", f"{name} has '_'")
            )
    return findings


def _problem_findings(exchange: Exchange) -> list[Finding]:
    if _media_type(exchange.response_field("content-type")) != PROBLEM_JSON:
        return []
    if exchange.status < 400:
        detail = f"problem details sent with status {exchange.status}"
        return [Finding("problem-with-success-status", "RFC 9205 Section 4.6", detail)]
    document = _json_object(exchange.response_content)
    if document is None:
        detail = "content is not a JSON object"
        return [Finding("problem-not-json-object", "RFC 9457 Section 3", detail)]
    advisory = document.get("status")
    if advisory is not None and advisory != exchange.status:
        detail = f"status member {advisory!r} != HTTP status {exchange.status}"
        return [Finding("problem-status-mismatch", "RFC 9457 Section 3.1.2", detail)]
    return []


def _freshness_findings(exchange: Exchange) -> list[Finding]:
    if exchange.method not in {"GET", "HEAD"} or exchange.status not in HEURISTICALLY_CACHEABLE:
        return []
    directives = _tokens(exchange.response_field("cache-control"))
    if directives & _EXPLICIT_CACHE_DIRECTIVES or exchange.response_field("expires") is not None:
        return []
    detail = f"{exchange.status} has no explicit freshness; caches may apply a heuristic"
    return [Finding("implicit-freshness", "RFC 9205 Section 4.9.1", detail)]


def audit_exchange(exchange: Exchange) -> tuple[Finding, ...]:
    """Return every BCP 56 rule that *exchange* violates, in a stable order."""
    findings: list[Finding] = []
    if exchange.method not in REGISTERED_METHODS:
        detail = f"{exchange.method} is not in the HTTP Method Registry"
        findings.append(Finding("unregistered-method", "RFC 9205 Section 4.5", detail))
    if exchange.status not in REGISTERED_STATUS_CODES:
        detail = f"{exchange.status} is not a registered, usable status code"
        findings.append(Finding("unregistered-status", "RFC 9205 Section 4.6", detail))
    if exchange.method == "GET" and exchange.request_content:
        detail = f"GET carries {len(exchange.request_content)} bytes of content"
        findings.append(Finding("get-with-content", "RFC 9205 Section 4.5.1", detail))
    names = [name for name, _ in exchange.request_fields + exchange.response_fields]
    findings.extend(_field_name_findings(list(dict.fromkeys(names))))
    findings.extend(_problem_findings(exchange))
    findings.extend(_freshness_findings(exchange))
    return tuple(findings)


def audit_vary(first: Exchange, second: Exchange) -> tuple[Finding, ...]:
    """Flag request fields that changed the response but are missing from `Vary`.

    Two exchanges for the same method and target whose content differs prove
    that some differing request field selected the response (RFC 9205 Section 4.9.4).
    """
    if (first.method, first.target) != (second.method, second.target):
        raise ValueError("exchanges must share method and target")
    if first.response_content == second.response_content:
        return ()
    if "no-store" in _tokens(first.response_field("cache-control")):
        return ()
    varied = _tokens(first.response_field("vary")) | _tokens(second.response_field("vary"))
    if "*" in varied:
        return ()
    names = dict.fromkeys(name for name, _ in first.request_fields + second.request_fields)
    differing = [n for n in names if first.request_field(n) != second.request_field(n)]
    return tuple(
        Finding("missing-vary", "RFC 9205 Section 4.9.4", f"response varies on {name}")
        for name in differing
        if name not in varied
    )


def fallback_status(status: int, understood: frozenset[int]) -> int:
    """Map *status* to the semantics a generic client must apply (RFC 9110 Section 15)."""
    if not 100 <= status <= 599:
        return 500
    if status in understood:
        return status
    return status // 100 * 100


def classify_error(
    status: int,
    content_type: str | None,
    content: bytes,
    known_problem_types: frozenset[str],
    understood: frozenset[int],
) -> ErrorClass:
    """Attribute an error to the application only when its problem `type` proves it."""
    if _media_type(content_type) == PROBLEM_JSON:
        document = _json_object(content) or {}
        problem_type = document.get("type", "about:blank")
        if isinstance(problem_type, str) and problem_type in known_problem_types:
            return ErrorClass("application", problem_type, status)
    effective = fallback_status(status, understood)
    return ErrorClass("generic", f"http-{effective}", effective)


def h11_response_head(status: int, reason: bytes, fields: Sequence[tuple[bytes, bytes]]) -> bytes:
    """Serialize a response head with h11, as an HTTP/1.1 origin would put it on the wire."""
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
    if not isinstance(conn.next_event(), h11.Request):
        raise RuntimeError("h11 did not parse the request")
    return conn.send(h11.Response(status_code=status, headers=list(fields), reason=reason))


def h2_response_fields(
    status: int, fields: Sequence[tuple[bytes, bytes]]
) -> tuple[tuple[bytes, bytes], ...]:
    """Send a response over an in-memory h2 connection and return what the client decodes."""
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    server.receive_data(client.data_to_send())
    request = [(":method", "GET"), (":path", "/"), (":scheme", "https"), (":authority", "a.test")]
    client.send_headers(1, request, end_stream=True)
    server.receive_data(client.data_to_send())
    server.send_headers(1, [(b":status", str(status).encode("ascii")), *fields], end_stream=True)
    for event in client.receive_data(server.data_to_send()):
        if isinstance(event, h2.events.ResponseReceived):
            return tuple((bytes(name), bytes(value)) for name, value in event.headers)
    raise RuntimeError("client saw no response headers")


WIDGET_NOT_FOUND = "https://widgets.example/problems/not-found"
_WIDGETS = {"1": {"id": "1", "name": "sprocket"}}


async def _widget(request: Request) -> Response:
    widget = _WIDGETS.get(request.path_params["wid"])
    if widget is None:
        problem = {"type": WIDGET_NOT_FOUND, "title": "Widget not found", "status": 404}
        return JSONResponse(problem, status_code=404, media_type=PROBLEM_JSON)
    return JSONResponse(widget, headers={"cache-control": "max-age=60"})


async def _legacy_widget(request: Request) -> Response:
    error = {"type": WIDGET_NOT_FOUND, "title": "Widget not found", "status": 404}
    headers = {"x-widget-count": str(len(_WIDGETS))}
    return JSONResponse(error, status_code=200, headers=headers, media_type=PROBLEM_JSON)


async def _moved(request: Request) -> Response:
    code = int(request.path_params["code"])
    host = request.query_params.get("host")
    location = f"http://{host}/echo" if host else "/echo"
    return Response(status_code=code, headers={"location": location})


async def _echo(request: Request) -> Response:
    content = await request.body()
    seen = {
        "method": request.method,
        "authorization": request.headers.get("authorization"),
        "content": content.decode("utf-8"),
    }
    return JSONResponse(seen, headers={"cache-control": "no-store"})


_ANY = ["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"]


def build_app() -> Starlette:
    """A widget API with one BCP 56 conformant route, one legacy route, and redirects."""
    return Starlette(
        routes=[
            Route("/widgets/{wid}", _widget, methods=["GET"]),
            Route("/legacy/widgets/{wid}", _legacy_widget, methods=["GET"]),
            Route("/moved/{code:int}", _moved, methods=_ANY),
            Route("/echo", _echo, methods=_ANY),
        ]
    )


class PrefixGateway:
    """A reverse proxy that only routes *prefix* and answers everything else itself."""

    def __init__(self, app: ASGIApp, prefix: str) -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not str(scope["path"]).startswith(self.prefix):
            await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
            return
        await self.app(scope, receive, send)
