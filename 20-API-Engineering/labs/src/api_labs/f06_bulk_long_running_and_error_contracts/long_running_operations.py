"""AIP-151 long-running operations: 202 Accepted, an Operation resource, polling, and cancel.

The server half is a FastAPI app over an in-memory operation store whose worker is advanced
one item at a time by `LroService.step`, so every intermediate state is observable and the
tests are deterministic. The client half is `wait_for_operation`, a poller that honors
`Retry-After` within bounds and treats an absent `done` field as `false`, as ProtoJSON omits it.
"""

from __future__ import annotations

import asyncio
import email.utils
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import httpx

# google.rpc.Code values (googleapis google/rpc/code.proto).
CANCELLED = 1
ALREADY_EXISTS = 6
ABORTED = 10

METADATA_TYPE = "type.googleapis.com/lab.library.v1.ImportBooksMetadata"
RESPONSE_TYPE = "type.googleapis.com/lab.library.v1.ImportBooksResponse"
NO_STORE = {"Cache-Control": "no-store"}


class Status(BaseModel):
    """ProtoJSON view of google.rpc.Status."""

    model_config = ConfigDict(extra="forbid")

    code: int
    message: str
    details: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])


class Operation(BaseModel):
    """ProtoJSON view of google.longrunning.Operation; an absent `done` means `false`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    metadata: dict[str, Any] | None = None
    done: bool = False
    error: Status | None = None
    response: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _result_matches_done(self) -> Operation:
        results = int(self.error is not None) + int(self.response is not None)
        if results > 1:
            raise ValueError("error and response are a oneof: at most one may be set")
        if not self.done and results:
            raise ValueError("an operation with done=false must not carry error or response")
        return self

    def to_proto_json(self) -> dict[str, Any]:
        """Serialize like ProtoJSON: default-valued fields, including `done=false`, are omitted."""
        return self.model_dump(mode="json", exclude_defaults=True)


class ImportBooksRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    titles: list[str] = Field(min_length=1, max_length=100)


class ParallelOperationError(Exception):
    """AIP-151: a resource that refuses parallel operations must answer ABORTED."""


@dataclass
class OperationRecord:
    """Server-side state behind one Operation resource."""

    op_id: str
    parent: str
    titles: tuple[str, ...]
    create_time: str
    processed: int = 0
    cancel_requested: bool = False
    done: bool = False
    error: Status | None = None
    response: dict[str, Any] | None = None

    def to_operation(self) -> Operation:
        total = len(self.titles)
        metadata: dict[str, Any] = {
            "@type": METADATA_TYPE,
            "createTime": self.create_time,
            "processedCount": self.processed,
            "totalCount": total,
            "progressPercent": self.processed * 100 // total,
            "cancelRequested": self.cancel_requested,
        }
        return Operation(
            name=f"operations/{self.op_id}",
            metadata=metadata,
            done=self.done,
            error=self.error,
            response=self.response,
        )


@dataclass
class LroService:
    """In-memory operation store plus a worker that imports one title per `step`."""

    retry_after_seconds: int = 1
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    operations: dict[str, OperationRecord] = field(default_factory=dict[str, OperationRecord])
    library: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    _next_id: int = 0

    def start_import(self, parent: str, titles: list[str]) -> OperationRecord:
        for record in self.operations.values():
            if record.parent == parent and not record.done:
                raise ParallelOperationError(
                    f"operations/{record.op_id} is still importing into {parent}"
                )
        self._next_id += 1
        record = OperationRecord(
            op_id=f"op-{self._next_id:06d}",
            parent=parent,
            titles=tuple(titles),
            create_time=self.clock().isoformat().replace("+00:00", "Z"),
        )
        self.operations[record.op_id] = record
        return record

    def step(self, op_id: str) -> None:
        """Advance the worker by one unit of work; cancellation is honored between items."""
        record = self.operations[op_id]
        if record.done:
            return
        if record.cancel_requested:
            record.error = Status(code=CANCELLED, message="operation was cancelled")
            record.done = True
            return
        shelf = self.library.setdefault(record.parent, [])
        title = record.titles[record.processed]
        if title in shelf:
            record.error = Status(code=ALREADY_EXISTS, message=f"book {title!r} already exists")
            record.done = True
            return
        shelf.append(title)
        record.processed += 1
        if record.processed == len(record.titles):
            record.response = {"@type": RESPONSE_TYPE, "importedCount": record.processed}
            record.done = True

    def run_to_completion(self, op_id: str) -> None:
        while not self.operations[op_id].done:
            self.step(op_id)

    def cancel(self, op_id: str) -> None:
        """Best effort, as google.longrunning.Operations.CancelOperation: a done operation keeps
        its result, and work already applied is not rolled back."""
        record = self.operations[op_id]
        if not record.done:
            record.cancel_requested = True


def _operation_response(record: OperationRecord, status: int, retry_after: int) -> JSONResponse:
    headers = dict(NO_STORE)
    if not record.done:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(record.to_operation().to_proto_json(), status, headers)


def build_app(service: LroService, *, run_in_background_tasks: bool = False) -> FastAPI:
    """`run_in_background_tasks=True` is the anti-pattern: work runs in the request's task."""
    app = FastAPI()

    def lookup(operation_id: str) -> OperationRecord:
        record = service.operations.get(operation_id)
        if record is None:
            raise HTTPException(404, f"operations/{operation_id} not found")
        return record

    @app.post("/v1/publishers/{publisher}/books:import", status_code=202)
    async def import_books(
        publisher: str, body: ImportBooksRequest, background_tasks: BackgroundTasks
    ) -> JSONResponse:
        try:
            record = service.start_import(f"publishers/{publisher}", body.titles)
        except ParallelOperationError as exc:
            error = {"error": {"code": 409, "message": str(exc), "status": "ABORTED"}}
            return JSONResponse(error, 409)
        if run_in_background_tasks:
            background_tasks.add_task(service.run_to_completion, record.op_id)
        response = _operation_response(record, 202, service.retry_after_seconds)
        response.headers["Location"] = f"/v1/operations/{record.op_id}"
        return response

    @app.get("/v1/operations/{operation_id}")
    async def get_operation(operation_id: str) -> JSONResponse:
        return _operation_response(lookup(operation_id), 200, service.retry_after_seconds)

    @app.post("/v1/operations/{operation_id}:cancel")
    async def cancel_operation(operation_id: str) -> JSONResponse:
        service.cancel(lookup(operation_id).op_id)
        return JSONResponse({}, 200, NO_STORE)

    return app


def operation_url(response: httpx.Response) -> str:
    """Absolute status-monitor URL from a 202, resolving a relative `Location`."""
    if response.status_code != 202 or "Location" not in response.headers:
        raise ValueError(f"expected 202 with Location, got {response.status_code}")
    return str(response.url.join(response.headers["Location"]))


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """Parse `Retry-After` as delay-seconds or an HTTP-date (RFC 9110 Section 10.2.3)."""
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        return float(int(text))
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


@dataclass(frozen=True)
class PollPolicy:
    default_delay: float = 1.0
    min_delay: float = 0.5
    max_delay: float = 30.0
    timeout: float = 300.0


class OperationTimeoutError(Exception):
    """The client stopped waiting; the server-side operation keeps running."""

    def __init__(self, last: Operation) -> None:
        super().__init__(f"{last.name} not done before the client deadline")
        self.last = last


@dataclass
class PollResult:
    operation: Operation
    delays: list[float]


async def wait_for_operation(
    client: httpx.AsyncClient,
    url: str,
    *,
    policy: PollPolicy | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PollResult:
    """Poll `GET url` until `done`, sleeping for the server's clamped `Retry-After` hint."""
    policy = policy or PollPolicy()
    deadline = monotonic() + policy.timeout
    delays: list[float] = []
    while True:
        reply = await client.get(url)
        reply.raise_for_status()
        operation = Operation.model_validate(reply.json())
        if operation.done:
            return PollResult(operation, delays)
        hint = parse_retry_after(reply.headers.get("Retry-After"), now())
        delay = policy.default_delay if hint is None else hint
        delay = min(max(delay, policy.min_delay), policy.max_delay)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise OperationTimeoutError(operation)
        delay = min(delay, remaining)
        delays.append(delay)
        await sleep(delay)
