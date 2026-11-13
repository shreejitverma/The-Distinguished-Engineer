"""Azure REST API Guidelines data-plane contracts as an in-process Starlette app.

What the lab enforces, each rule anchored in microsoft/api-guidelines vNext azure/Guidelines.md:

- ``api-version`` is a required query parameter on every operation; a missing value is
  ``400 MissingApiVersionParameter`` and an unknown value is ``400 UnsupportedApiVersionValue``
  whose message lists every stable version and only the latest preview.
- Every response, including Starlette's own unhandled-exception ``500``, carries a unique,
  service-generated ``x-ms-request-id``; ``x-ms-client-request-id`` and
  ``x-ms-correlation-request-id`` are validated as undecorated GUIDs and logged.
  The same outermost wrapper drops the content of every ``HEAD`` response.
- Every error body is ``{"error": {"code", "message", ...}}`` and its ``code`` is byte-equal to
  the ``x-ms-error-code`` response header; no response field is ever ``null``.
- ``POST /widgets/{name}:rebuild`` is an LRO action: ``202 Accepted``, ``operation-id``,
  an absolute ``operation-location`` carrying the caller's ``api-version``, a status monitor
  body, ``retry-after`` while non-terminal, and retention for 24 hours after completion;
  ``POST /operations/{id}:cancel`` answers ``200 OK`` with the monitor and never rewrites a
  terminal one.
- ``GET /widgets`` is a server-paged list: ``{"value": [...]}`` plus an absolute ``nextLink``
  that already carries ``api-version``, absent (never ``null``) on the last page.

Error ``code`` strings not named by the guidelines (for example ``OperationIdConflict``), the
``continuationToken`` parameter name, and the page-size constants are this lab's own choices.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    import httpx
    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

STABLE_VERSIONS: Final = ("2024-10-01", "2025-06-01")
PREVIEW_VERSIONS: Final = ("2025-09-01-preview", "2026-03-01-preview")
SUPPORTED_VERSIONS: Final = frozenset(STABLE_VERSIONS + PREVIEW_VERSIONS)
SIZE_FIELD_SINCE: Final = "2025-06-01"

MISSING_API_VERSION_MESSAGE: Final = (
    "The api-version query parameter (?api-version=) is required for all requests"
)
POLL_INTERVAL_SECONDS: Final = 2
QUEUE_SECONDS: Final = 1.0
REBUILD_SECONDS: Final = {"full": 6.0, "incremental": 2.0}
STATUS_MONITOR_RETENTION_SECONDS: Final = 24 * 60 * 60
TERMINAL_STATUSES: Final = frozenset({"Succeeded", "Failed", "Canceled"})
DEFAULT_PAGE_SIZE: Final = 2
MAX_PAGE_SIZE: Final = 3

_RESOURCE_ID = re.compile(r"[0-9A-Za-z\-._~]{1,63}")
_GUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_DELAY_SECONDS = re.compile(r"[0-9]+")
_PAGE_SIZE = re.compile(r"[0-9]{1,9}")
_LIST_PARAMETERS: Final = frozenset({"api-version", "maxpagesize", "continuationToken"})
_API_VERSION_SCOPE_KEY: Final = "azure.api_version"


def unsupported_api_version_message(value: str) -> str:
    """Return the guideline message: all stable versions plus only the latest preview."""
    advertised = [*STABLE_VERSIONS, max(PREVIEW_VERSIONS)]
    return (
        f"Unsupported api-version '{value}'. "
        f"The supported api-versions are '{', '.join(advertised)}'."
    )


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    target: str | None = None,
    details: Sequence[Mapping[str, str]] = (),
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build the Azure error envelope; header and body ``code`` share one source value."""
    detail: dict[str, Any] = {"code": code, "message": message}
    if target is not None:
        detail["target"] = target
    if details:
        detail["details"] = [dict(item) for item in details]
    merged = {**(headers or {}), "x-ms-error-code": code}
    return JSONResponse({"error": detail}, status_code=status_code, headers=merged)


class AzureError(Exception):
    """An error that maps one-to-one onto the Azure error envelope."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        target: str | None = None,
        details: Sequence[Mapping[str, str]] = (),
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.target = target
        self.details = details


@dataclass(frozen=True)
class RequestLogRecord:
    """One access-log line: the identifiers a support engineer correlates on."""

    request_id: str
    client_request_id: str | None
    correlation_request_id: str | None
    method: str
    path: str
    status: int


class AzureEdge:
    """Outermost ASGI wrapper: request ids, header validation, and the api-version gate.

    It must wrap the whole Starlette app, not sit in ``Starlette(middleware=...)``: Starlette
    always places ``ServerErrorMiddleware`` outside user middleware, so only an outer wrapper
    sees the ``500`` that ``ServerErrorMiddleware`` sends.
    """

    def __init__(
        self, app: ASGIApp, *, log: list[RequestLogRecord], new_id: Callable[[], str]
    ) -> None:
        self.app = app
        self._log = log
        self._new_id = new_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = self._new_id()
        headers = Headers(scope=scope)
        client_id = headers.get("x-ms-client-request-id")
        correlation_id = headers.get("x-ms-correlation-request-id")
        echo_client_id = (
            client_id is not None
            and _GUID.fullmatch(client_id) is not None
            and headers.get("x-ms-return-client-request-id", "").lower() == "true"
        )
        status = 0

        async def send_with_ids(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                raw: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                raw.append((b"x-ms-request-id", request_id.encode("ascii")))
                if echo_client_id and client_id is not None:
                    raw.append((b"x-ms-client-request-id", client_id.encode("ascii")))
                message = {**message, "headers": raw}
            elif message["type"] == "http.response.body" and scope["method"] == "HEAD":
                # HEAD is GET without content (RFC 9110 Section 9.3.2); not every server strips it.
                message = {**message, "body": b""}
            await send(message)

        try:
            rejection = _check_edge(scope, headers)
            if rejection is not None:
                await rejection(scope, receive, send_with_ids)
            else:
                await self.app(scope, receive, send_with_ids)
        finally:
            self._log.append(
                RequestLogRecord(
                    request_id=request_id,
                    client_request_id=client_id,
                    correlation_request_id=correlation_id,
                    method=scope["method"],
                    path=scope["path"],
                    status=status,
                )
            )


def _check_edge(scope: Scope, headers: Headers) -> Response | None:
    for name in ("x-ms-client-request-id", "x-ms-correlation-request-id"):
        value = headers.get(name)
        if value is not None and _GUID.fullmatch(value) is None:
            return error_response(
                400, "InvalidHeaderValue", f"Header '{name}' must be a GUID.", target=name
            )
    query: bytes = scope["query_string"]
    versions = [
        value
        for key, value in parse_qsl(query.decode("latin-1"), keep_blank_values=True)
        if key == "api-version"
    ]
    if not versions:
        return error_response(400, "MissingApiVersionParameter", MISSING_API_VERSION_MESSAGE)
    if len(versions) > 1:
        return error_response(
            400,
            "InvalidQueryParameterValue",
            "The api-version query parameter must appear exactly once.",
            target="api-version",
        )
    if versions[0] not in SUPPORTED_VERSIONS:
        return error_response(
            400, "UnsupportedApiVersionValue", unsupported_api_version_message(versions[0])
        )
    scope[_API_VERSION_SCOPE_KEY] = versions[0]
    return None


class WidgetV1(BaseModel):
    """Request schema for api-versions before ``SIZE_FIELD_SINCE``."""

    model_config = ConfigDict(extra="forbid", strict=True)
    color: Literal["red", "green", "blue"]


class WidgetV2(WidgetV1):
    """Request schema from ``SIZE_FIELD_SINCE`` on: adds the optional ``size`` field."""

    size: Literal["S", "M", "L"] = "M"


class RebuildRequest(BaseModel):
    """Body of the ``:rebuild`` action."""

    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["full", "incremental"]


@dataclass
class Operation:
    """A status monitor whose state is a pure function of the clock and deletions."""

    id: str
    widget: str
    mode: str
    fingerprint: str
    created_at: float
    canceled_at: float | None = None

    @property
    def completes_at(self) -> float:
        return self.created_at + REBUILD_SECONDS[self.mode]

    @property
    def ended_at(self) -> float:
        """When the monitor became terminal: the cancel time, else the scheduled completion."""
        return self.completes_at if self.canceled_at is None else self.canceled_at

    def monitor(self, now: float, deletions: Sequence[float]) -> dict[str, Any]:
        body: dict[str, Any] = {"id": self.id, "kind": "WidgetRebuild"}
        if self.canceled_at is not None:
            body["status"] = "Canceled"
        elif now < self.created_at + QUEUE_SECONDS:
            body["status"] = "NotStarted"
        elif now < self.completes_at:
            body["status"] = "Running"
        elif any(self.created_at <= t < self.completes_at for t in deletions):
            body["status"] = "Failed"
            body["error"] = {
                "code": "WidgetDeleted",
                "message": f"Widget '{self.widget}' was deleted before the rebuild finished.",
            }
        else:
            body["status"] = "Succeeded"
            body["result"] = {"widgetName": self.widget, "mode": self.mode}
        return body


@dataclass
class ManualClock:
    """Deterministic time source; tests advance it instead of sleeping."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Store:
    widgets: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    deletions: dict[str, list[float]] = field(default_factory=dict[str, list[float]])
    operations: dict[str, Operation] = field(default_factory=dict[str, Operation])


@dataclass
class AzureLab:
    app: ASGIApp
    clock: ManualClock
    store: Store
    log: list[RequestLogRecord]


def _api_version(request: Request) -> str:
    version: str = request.scope[_API_VERSION_SCOPE_KEY]
    return version


def _resource_id(raw: str, target: str) -> str:
    if _RESOURCE_ID.fullmatch(raw) is None:
        raise AzureError(400, "InvalidResourceId", f"'{raw}' is not a valid {target}.", target)
    return raw


async def _json_body(request: Request) -> object:
    try:
        return await request.json()
    except ValueError as exc:
        raise AzureError(400, "InvalidRequestContent", "Body is not valid JSON.") from exc


def _validation_error(exc: ValidationError) -> AzureError:
    """Map pydantic errors onto one frozen top-level code; several errors also fill ``details``."""
    details: list[dict[str, str]] = []
    for error in exc.errors():
        item = {"code": "InvalidRequestContent", "message": error["msg"]}
        target = ".".join(str(part) for part in error["loc"])
        if target:
            item["target"] = target
        details.append(item)
    first = details[0]
    return AzureError(
        400,
        "InvalidRequestContent",
        first["message"],
        first.get("target"),
        details if len(details) > 1 else (),
    )


def _list_options(request: Request) -> tuple[int, str]:
    """Return ``(page size, exclusive start name)`` after validating the list query.

    Parameter names are case-sensitive, unsupported names are errors, and no name may repeat.
    """
    options: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in _LIST_PARAMETERS:
            problem = f"Query parameter '{key}' is not supported."
            raise AzureError(400, "InvalidQueryParameterValue", problem, key)
        if key in options:
            problem = f"Query parameter '{key}' must appear at most once."
            raise AzureError(400, "InvalidQueryParameterValue", problem, key)
        options[key] = value
    raw_size = options.get("maxpagesize", str(DEFAULT_PAGE_SIZE))
    if _PAGE_SIZE.fullmatch(raw_size) is None or int(raw_size) < 1:
        problem = "Query parameter 'maxpagesize' must be an integer of at least 1."
        raise AzureError(400, "InvalidQueryParameterValue", problem, "maxpagesize")
    token = options.get("continuationToken")
    if token is not None and _RESOURCE_ID.fullmatch(token) is None:
        problem = "Query parameter 'continuationToken' is not a value this service issued."
        raise AzureError(400, "InvalidQueryParameterValue", problem, "continuationToken")
    return min(int(raw_size), MAX_PAGE_SIZE), token or ""


def create_app(
    clock: ManualClock | None = None,
    *,
    extra_routes: Sequence[BaseRoute] = (),
    new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> AzureLab:
    """Build the lab service; ``extra_routes`` lets tests mount a failing endpoint."""
    lab_clock = clock or ManualClock()
    store = Store()
    log: list[RequestLogRecord] = []

    def visible(widget: dict[str, str], version: str) -> dict[str, str]:
        if version[:10] >= SIZE_FIELD_SINCE:
            return dict(widget)
        return {"name": widget["name"], "color": widget["color"]}

    async def list_widgets(request: Request) -> Response:
        version = _api_version(request)
        page_size, after = _list_options(request)
        names = sorted(name for name in store.widgets if name > after)
        page = names[:page_size]
        body: dict[str, Any] = {"value": [visible(store.widgets[name], version) for name in page]}
        if len(names) > page_size:
            # nextLink is absolute and carries every parameter the next GET needs.
            query = {
                "api-version": version,
                "maxpagesize": str(page_size),
                "continuationToken": page[-1],
            }
            body["nextLink"] = str(request.url.replace(query=urlencode(query)))
        return JSONResponse(body)

    async def put_widget(request: Request) -> Response:
        name = _resource_id(request.path_params["name"], "widget name")
        version = _api_version(request)
        model = WidgetV2 if version[:10] >= SIZE_FIELD_SINCE else WidgetV1
        try:
            parsed = model.model_validate(await _json_body(request))
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        # A PUT from an older api-version resets newer fields to their defaults.
        widget = {"name": name, **WidgetV2.model_validate(parsed.model_dump()).model_dump()}
        created = name not in store.widgets
        store.widgets[name] = widget
        return JSONResponse(visible(widget, version), status_code=201 if created else 200)

    async def get_widget(request: Request) -> Response:
        name = _resource_id(request.path_params["name"], "widget name")
        widget = store.widgets.get(name)
        if widget is None:
            raise AzureError(404, "WidgetNotFound", f"Widget '{name}' does not exist.")
        return JSONResponse(visible(widget, _api_version(request)))

    async def delete_widget(request: Request) -> Response:
        name = _resource_id(request.path_params["name"], "widget name")
        if store.widgets.pop(name, None) is not None:
            store.deletions.setdefault(name, []).append(lab_clock())
        return Response(status_code=204)

    async def rebuild_widget(request: Request) -> Response:
        name = _resource_id(request.path_params["name"], "widget name")
        version = _api_version(request)
        raw_body = await _json_body(request)
        try:
            body = RebuildRequest.model_validate(raw_body)
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        if name not in store.widgets:
            raise AzureError(404, "WidgetNotFound", f"Widget '{name}' does not exist.")
        requested_id = request.headers.get("operation-id")
        op_id = (
            _resource_id(requested_id, "Operation-Id")
            if requested_id is not None
            else str(uuid.uuid4())
        )
        fingerprint = json.dumps(["rebuild", name, raw_body], sort_keys=True)
        operation = store.operations.get(op_id)
        if operation is None:
            operation = Operation(op_id, name, body.mode, fingerprint, lab_clock())
            store.operations[op_id] = operation
        elif operation.fingerprint != fingerprint:
            raise AzureError(
                409, "OperationIdConflict", f"Operation-Id '{op_id}' is already in use."
            )
        monitor = operation.monitor(lab_clock(), store.deletions.get(name, []))
        location = request.url.replace(
            path=f"/operations/{op_id}", query=urlencode({"api-version": version})
        )
        headers = {"operation-id": op_id, "operation-location": str(location)}
        if monitor["status"] not in TERMINAL_STATUSES:
            headers["retry-after"] = str(POLL_INTERVAL_SECONDS)
        return JSONResponse(monitor, status_code=202, headers=headers)

    def retained_operation(request: Request) -> tuple[Operation, float]:
        op_id = _resource_id(request.path_params["operation_id"], "operation id")
        operation = store.operations.get(op_id)
        now = lab_clock()
        if operation is None or now > operation.ended_at + STATUS_MONITOR_RETENTION_SECONDS:
            raise AzureError(404, "OperationNotFound", f"Operation '{op_id}' does not exist.")
        return operation, now

    async def get_operation(request: Request) -> Response:
        operation, now = retained_operation(request)
        monitor = operation.monitor(now, store.deletions.get(operation.widget, []))
        headers: dict[str, str] = {}
        if monitor["status"] not in TERMINAL_STATUSES:
            headers["retry-after"] = str(POLL_INTERVAL_SECONDS)
        return JSONResponse(monitor, headers=headers)

    async def cancel_operation(request: Request) -> Response:
        operation, now = retained_operation(request)
        # Cancel is idempotent and never rewrites a monitor that is already terminal.
        if operation.canceled_at is None and now < operation.completes_at:
            operation.canceled_at = now
        return JSONResponse(operation.monitor(now, store.deletions.get(operation.widget, [])))

    widget_methods = {
        "GET": get_widget,
        "HEAD": get_widget,
        "PUT": put_widget,
        "DELETE": delete_widget,
    }

    async def widget(request: Request) -> Response:
        return await widget_methods[request.method](request)

    async def on_azure_error(request: Request, exc: Exception) -> Response:
        error = cast("AzureError", exc)
        return error_response(
            error.status_code,
            error.code,
            error.message,
            target=error.target,
            details=error.details,
        )

    async def on_http_exception(request: Request, exc: Exception) -> Response:
        http_exc = cast("HTTPException", exc)
        code = {404: "NotFound", 405: "MethodNotAllowed"}.get(http_exc.status_code, "BadRequest")
        return error_response(http_exc.status_code, code, http_exc.detail, headers=http_exc.headers)

    async def on_unhandled(request: Request, exc: Exception) -> Response:
        return error_response(500, "InternalServerError", "The service failed unexpectedly.")

    routes: list[BaseRoute] = [
        Route("/widgets/{name}:rebuild", rebuild_widget, methods=["POST"]),
        # One route per URL, so a 405 lists every method of the target resource in ``allow``.
        Route("/widgets/{name}", widget, methods=list(widget_methods)),
        Route("/widgets", list_widgets, methods=["GET"]),
        Route("/operations/{operation_id}:cancel", cancel_operation, methods=["POST"]),
        Route("/operations/{operation_id}", get_operation, methods=["GET"]),
        *extra_routes,
    ]
    starlette_app = Starlette(
        routes=routes,
        exception_handlers={
            AzureError: on_azure_error,
            HTTPException: on_http_exception,
            Exception: on_unhandled,
        },
    )
    return AzureLab(AzureEdge(starlette_app, log=log, new_id=new_id), lab_clock, store, log)


def with_api_version(url: str, api_version: str) -> str:
    """Replace (or add) ``api-version`` in a service-returned URL, keeping other parameters."""
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, v) for k, v in pairs if k != "api-version"]
    query.append(("api-version", api_version))
    return urlunsplit(parts._replace(query=urlencode(query)))


def retry_after_seconds(value: str | None, default: int) -> int:
    """Parse ``delay-seconds = 1*DIGIT`` (RFC 9110 Section 10.2.3); fall back to ``default``."""
    if value is None or _DELAY_SECONDS.fullmatch(value) is None:
        return default
    return int(value)


async def poll_lro(
    client: httpx.AsyncClient,
    initial: httpx.Response,
    *,
    api_version: str,
    sleep: Callable[[float], Awaitable[None]],
    default_interval: int = POLL_INTERVAL_SECONDS,
    max_polls: int = 50,
) -> tuple[dict[str, Any], list[int]]:
    """Poll ``operation-location`` with the caller's api-version until a terminal status.

    Waits ``retry-after`` before every GET, including the first, and compares status
    case-insensitively. Returns the terminal status monitor and the waits it performed.
    """
    url = with_api_version(initial.headers["operation-location"], api_version)
    waits: list[int] = []
    response = initial
    for _ in range(max_polls):
        delay = retry_after_seconds(response.headers.get("retry-after"), default_interval)
        waits.append(delay)
        await sleep(delay)
        response = await client.get(url)
        response.raise_for_status()
        monitor: dict[str, Any] = response.json()
        if str(monitor["status"]).lower() in {s.lower() for s in TERMINAL_STATUSES}:
            return monitor, waits
    raise TimeoutError(f"operation still running after {max_polls} polls")


async def list_all(
    client: httpx.AsyncClient, url: str, *, max_pages: int = 50
) -> list[dict[str, Any]]:
    """Collect every item of a paged list by following ``nextLink`` verbatim.

    ``nextLink`` is an opaque absolute URL that already carries ``api-version``; the only
    edit a client may make to it is replacing that one value (see ``with_api_version``).
    """
    items: list[dict[str, Any]] = []
    next_url = url
    for _ in range(max_pages):
        response = await client.get(next_url)
        response.raise_for_status()
        page: dict[str, Any] = response.json()
        items.extend(page["value"])
        if "nextLink" not in page:
            return items
        next_url = page["nextLink"]
    raise TimeoutError(f"collection still paging after {max_pages} pages")
