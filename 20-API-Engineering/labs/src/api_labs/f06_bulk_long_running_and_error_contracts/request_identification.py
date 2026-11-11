"""Three identifiers, three jobs: AIP-155 request_id, server X-Request-ID, W3C traceparent.

- `request_id` (body, client-chosen, AIP-155): names one logical operation; retries reuse it.
- `X-Request-ID` (response header, server-assigned): names one attempt; retries get a new one.
- `traceparent` (W3C Trace Context): joins this attempt to the caller's distributed trace.
"""

import asyncio
import contextvars
import hashlib
import json
import re
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Protocol

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

PROBLEM_JSON = "application/problem+json"
REQUEST_ID_HEADER = b"x-request-id"
TRACEPARENT_HEADER = b"traceparent"
V4_ONLY = frozenset({4})  # AIP-155: (google.api.field_info).format = UUID4
ANY_RFC9562_VERSION = frozenset(range(1, 9))

# RFC 9562 Section 4 ABNF: 8-4-4-4-12 hex digits, any letter case; exactly 36 ASCII characters.
_UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# W3C Trace Context 3.2.2: lowercase hex; later versions may append "-" and more fields.
_TRACEPARENT = re.compile(r"([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})(-.*)?")

Log = list[dict[str, object]]


class RequestIdError(ValueError):
    """A request_id that is not an acceptable UUID; the message never echoes the input."""


def parse_request_id(raw: object, versions: frozenset[int] = V4_ONLY) -> uuid.UUID:
    """Grammar first, then value: stdlib and pydantic parsers accept far more than 36 chars."""
    if not isinstance(raw, str) or _UUID_TEXT.fullmatch(raw) is None:
        raise RequestIdError("request_id must be a 36-character hex-and-dash UUID")
    value = uuid.UUID(raw)
    if value.int == 0:
        raise RequestIdError("request_id must not be the nil UUID")
    if value.version not in versions:  # UUID.version is None unless the variant is RFC 9562
        raise RequestIdError(f"request_id must be a UUID of version {sorted(versions)}")
    return value


def canonical_request_id(raw: object) -> str:
    """AIP-202: compare UUIDs as values, so normalize to lowercase before using as a key."""
    return str(parse_request_id(raw))


RequestId = Annotated[str, BeforeValidator(canonical_request_id)]
Principal = Annotated[str, Header(pattern=r"^[a-z0-9-]{1,32}$")]


@dataclass(frozen=True, slots=True)
class TraceParent:
    trace_id: str
    parent_id: str
    sampled: bool


def parse_traceparent(value: str) -> TraceParent | None:
    """Return None (restart the trace) for anything W3C Trace Context says to ignore."""
    match = _TRACEPARENT.fullmatch(value.strip(" \t"))
    if match is None:
        return None
    version, trace_id, parent_id, flags, rest = match.groups()
    if version == "ff" or (version == "00" and rest is not None):
        return None
    if trace_id == "0" * 32 or parent_id == "0" * 16:
        return None
    return TraceParent(trace_id, parent_id, sampled=bool(int(flags, 16) & 0x01))


class IdSource(Protocol):
    def request_id(self) -> str: ...
    def trace_id(self) -> str: ...
    def span_id(self) -> str: ...


class RandomIds:
    """Production source: UUIDv4 attempt IDs, CSPRNG trace and span IDs (never all zero)."""

    def request_id(self) -> str:
        return str(uuid.uuid4())

    def trace_id(self) -> str:
        return _nonzero_hex(16)

    def span_id(self) -> str:
        return _nonzero_hex(8)


def _nonzero_hex(nbytes: int) -> str:
    while (value := secrets.token_hex(nbytes)) == "0" * (2 * nbytes):
        pass
    return value


@dataclass(frozen=True, slots=True)
class Correlation:
    server_request_id: str  # this attempt; echoed as X-Request-ID
    trace_id: str
    span_id: str  # this server's span, a child of parent_id
    parent_id: str | None
    sampled: bool


_current: contextvars.ContextVar[Correlation | None] = contextvars.ContextVar(
    "correlation", default=None
)


def current_correlation() -> Correlation:
    correlation = _current.get()
    if correlation is None:
        raise RuntimeError("no correlation context: is CorrelationMiddleware installed?")
    return correlation


def log_event(log: Log, event: str, **fields: object) -> None:
    """Every record carries both IDs under names that cannot be confused."""
    correlation = current_correlation()
    log.append(
        {
            "event": event,
            "server_request_id": correlation.server_request_id,
            "trace_id": correlation.trace_id,
            "span_id": correlation.span_id,
            **fields,
        }
    )


def problem(status: int, title: str, detail: str, **extensions: object) -> JSONResponse:
    """RFC 9457 body whose `instance` names this occurrence by the server's attempt ID."""
    correlation = current_correlation()
    body: dict[str, object] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": f"urn:uuid:{correlation.server_request_id}",
        "trace_id": correlation.trace_id,
        **extensions,
    }
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


class CorrelationMiddleware:
    """Pure ASGI: assign the attempt ID, continue the trace, stamp every response, catch 500s."""

    def __init__(self, app: ASGIApp, *, ids: IdSource, log: Log, trust_inbound: bool) -> None:
        self.app = app
        self.ids = ids
        self.log = log
        self.trust_inbound = trust_inbound

    def _inbound_request_id(self, values: list[str]) -> str | None:
        if not self.trust_inbound or len(values) != 1:
            return None
        try:
            return str(parse_request_id(values[0], ANY_RFC9562_VERSION))
        except RequestIdError:
            return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # ASGI: request header names "should be lowercased, but it is not required".
        headers = [(k.lower(), v.decode("latin-1")) for k, v in scope["headers"]]
        inbound = [v for k, v in headers if k == REQUEST_ID_HEADER]
        parents = [v for k, v in headers if k == TRACEPARENT_HEADER]
        adopted = self._inbound_request_id(inbound)
        parent = parse_traceparent(parents[0]) if len(parents) == 1 else None
        correlation = Correlation(
            server_request_id=adopted or self.ids.request_id(),
            trace_id=parent.trace_id if parent else self.ids.trace_id(),
            span_id=self.ids.span_id(),
            parent_id=parent.parent_id if parent else None,
            sampled=parent.sampled if parent else False,
        )
        stamp = correlation.server_request_id.encode("ascii")
        status: list[int] = []

        async def send_stamped(message: Message) -> None:
            if message["type"] == "http.response.start":
                status.append(message["status"])
                kept = [(k, v) for k, v in message.get("headers", []) if k != REQUEST_ID_HEADER]
                message = {**message, "headers": [*kept, (REQUEST_ID_HEADER, stamp)]}
            await send(message)

        token = _current.set(correlation)
        try:
            await self.app(scope, receive, send_stamped)
        except Exception as exc:
            log_event(self.log, "unhandled_exception", exception=type(exc).__name__)
            if status:  # headers already sent: nothing safe to add, let the server abort
                raise
            response = problem(500, "Internal Server Error", "Quote the instance to support.")
            await response(scope, receive, send_stamped)
        finally:
            log_event(
                self.log,
                "access",
                status=status[0] if status else None,
                parent_id=correlation.parent_id,
                inbound_request_id_ignored=bool(inbound) and adopted is None,
            )
            _current.reset(token)


class CreateRequest(BaseModel):
    """Body of CreateBook and CreateShelf: a title and the optional AIP-155 request_id."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    request_id: RequestId | None = None

    def fingerprint(self) -> str:
        """Hash of everything except request_id, to detect reuse with a different payload."""
        canonical = json.dumps(self.model_dump(exclude={"request_id"}), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Replay:
    fingerprint: str
    body: dict[str, object]


@dataclass(slots=True)
class BookStore:
    books: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    shelves: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    # Key: (principal, method, canonical request_id). Never the raw text, never global.
    replays: dict[tuple[str, str, str], Replay] = field(
        default_factory=dict[tuple[str, str, str], Replay]
    )


@dataclass(frozen=True, slots=True)
class Lab:
    app: FastAPI
    store: BookStore
    log: Log


def build_lab(*, ids: IdSource | None = None, trust_inbound: bool = False) -> Lab:
    store = BookStore()
    log: Log = []
    app = FastAPI()
    app.add_middleware(
        CorrelationMiddleware, ids=ids or RandomIds(), log=log, trust_inbound=trust_inbound
    )

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]}
            for e in exc.errors()
        ]  # no "input": a rejected value is attacker-controlled text
        return problem(400, "Bad Request", "The request body is invalid.", errors=errors)

    def create(
        method: str,
        collection: str,
        rows: list[dict[str, object]],
        body: CreateRequest,
        principal: str,
    ) -> JSONResponse:
        key = None if body.request_id is None else (principal, method, body.request_id)
        prior = None if key is None else store.replays.get(key)
        if prior is not None:
            if prior.fingerprint != body.fingerprint():
                log_event(log, "request_id_reused", client_request_id=body.request_id)
                return problem(422, "Unprocessable Content", "request_id reused with new payload.")
            log_event(log, "replayed", client_request_id=body.request_id, name=prior.body["name"])
            return JSONResponse(prior.body, status_code=201)
        row: dict[str, object] = {"name": f"{collection}/{len(rows) + 1}", "title": body.title}
        rows.append(row)
        if key is not None:
            store.replays[key] = Replay(body.fingerprint(), row)
        log_event(log, "created", client_request_id=body.request_id, name=row["name"])
        return JSONResponse(row, status_code=201)

    # x-principal stands in for the authenticated caller in this lab.
    @app.post("/v1/books", status_code=201)
    async def create_book(
        body: CreateRequest, x_principal: Principal = "anonymous"
    ) -> JSONResponse:
        return create("CreateBook", "books", store.books, body, x_principal)

    @app.post("/v1/shelves", status_code=201)
    async def create_shelf(
        body: CreateRequest, x_principal: Principal = "anonymous"
    ) -> JSONResponse:
        return create("CreateShelf", "shelves", store.shelves, body, x_principal)

    @app.get("/v1/whoami")
    async def whoami() -> dict[str, str | None]:
        await asyncio.sleep(0)  # yield to other requests, as a real database call would
        correlation = current_correlation()
        return {
            "server_request_id": correlation.server_request_id,
            "trace_id": correlation.trace_id,
            "parent_id": correlation.parent_id,
        }

    @app.get("/v1/boom")
    async def boom() -> None:
        raise RuntimeError("database password=hunter2 rejected")  # must never reach the client

    return Lab(app=app, store=store, log=log)
