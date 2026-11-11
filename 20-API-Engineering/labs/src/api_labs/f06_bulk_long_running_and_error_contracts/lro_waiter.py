"""LRO engineering details: bounded :wait long-polling, completion-relative TTL, cancel, replay."""

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import FastAPI, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

CODE_CANCELLED = 1  # google.rpc.Code.CANCELLED
CODE_INTERNAL = 13  # google.rpc.Code.INTERNAL
OPERATION_ID = r"^[0-9a-f]{32}$"
UUID4 = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
DURATION = r"^[0-9]{1,6}(\.[0-9]{1,9})?s$"  # protobuf JSON mapping of google.protobuf.Duration
PROBLEM_JSON = "application/problem+json"

JobResult = dict[str, object]


class ExportRequest(BaseModel):
    """Start an export; `request_id` makes the create idempotent (AIP-155)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(pattern=UUID4)
    table: str = Field(pattern=r"^[a-z_]{1,64}$")

    def fingerprint(self) -> str:
        """Hash of everything except the request_id, to detect key reuse with a new payload."""
        payload = self.model_dump(exclude={"request_id"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


Job = Callable[[ExportRequest], Awaitable[JobResult]]
Disconnected = Callable[[], Coroutine[None, None, None]]


class WaitRequest(BaseModel):
    """Body of `:wait`; `timeout` uses the protobuf JSON Duration form, for example `"5s"`."""

    model_config = ConfigDict(extra="forbid", strict=True)

    timeout: str | None = Field(default=None, pattern=DURATION)

    def seconds(self) -> float | None:
        return None if self.timeout is None else float(Decimal(self.timeout[:-1]))


class ManualClock:
    """Wall clock for TTL arithmetic that tests advance explicitly."""

    def __init__(self, start: float = 1_767_225_600.0) -> None:  # 2026-01-01T00:00:00Z
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _rfc3339(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class Operation:
    """One google.longrunning.Operation-shaped record plus server-side bookkeeping."""

    name: str
    request_id: str
    fingerprint: str
    create_time: float
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    done: bool = False
    response: JobResult | None = None
    error: tuple[int, str] | None = None
    end_time: float | None = None
    expire_time: float | None = None

    def to_json(self) -> dict[str, object]:
        metadata: dict[str, object] = {"create_time": _rfc3339(self.create_time)}
        if self.end_time is not None and self.expire_time is not None:
            metadata["end_time"] = _rfc3339(self.end_time)
            metadata["expire_time"] = _rfc3339(self.expire_time)
        body: dict[str, object] = {"name": self.name, "done": self.done, "metadata": metadata}
        if self.error is not None:  # oneof result: set only when done
            body["error"] = {"code": self.error[0], "message": self.error[1]}
        elif self.done:
            body["response"] = self.response
        return body


class RequestIdReusedError(Exception):
    """The same request_id arrived with a different payload."""


class OperationStore:
    """In-memory operations collection: start, replay, wait, cancel, expire, sweep."""

    def __init__(self, job: Job, clock: ManualClock, *, ttl: float, max_wait: float) -> None:
        self._job = job
        self._clock = clock
        self._ttl = ttl
        self.max_wait = max_wait
        self._ops: dict[str, Operation] = {}
        self._by_request_id: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}  # strong refs: the loop keeps weak ones
        self.active_waiters = 0
        self.job_starts = 0

    @property
    def operation_count(self) -> int:
        return len(self._ops)

    @property
    def request_id_count(self) -> int:
        return len(self._by_request_id)

    def get(self, name: str) -> Operation | None:
        """Lazy expiry: an expired record is invisible even before the sweeper deletes it."""
        op = self._ops.get(name)
        if op is None or (op.expire_time is not None and self._clock.now() >= op.expire_time):
            return None
        return op

    def start(self, request: ExportRequest) -> Operation:
        """Create or replay; no await between lookup and insert, so replays cannot race."""
        fingerprint = request.fingerprint()
        previous = self._by_request_id.get(request.request_id)
        if previous is not None and (op := self.get(previous)) is not None:
            if op.fingerprint != fingerprint:
                raise RequestIdReusedError(request.request_id)
            return op
        op = Operation(
            name=f"operations/{uuid.uuid4().hex}",
            request_id=request.request_id,
            fingerprint=fingerprint,
            create_time=self._clock.now(),
        )
        self._ops[op.name] = op
        self._by_request_id[request.request_id] = op.name
        self.job_starts += 1
        self._tasks[op.name] = asyncio.create_task(self._run(op, request))
        return op

    async def _run(self, op: Operation, request: ExportRequest) -> None:
        try:
            result = await self._job(request)
        except asyncio.CancelledError:
            self._finish(op, error=(CODE_CANCELLED, "Operation cancelled by CancelOperation."))
            raise
        except Exception:
            self._finish(op, error=(CODE_INTERNAL, "Export failed."))
        else:
            self._finish(op, response=result)
        finally:
            self._tasks.pop(op.name, None)

    def _finish(
        self,
        op: Operation,
        *,
        response: JobResult | None = None,
        error: tuple[int, str] | None = None,
    ) -> None:
        if op.done:
            return
        now = self._clock.now()
        op.done, op.response, op.error = True, response, error
        op.end_time, op.expire_time = now, now + self._ttl  # TTL counts from completion
        op.done_event.set()  # level-triggered: later waiters return at once

    def cancel(self, name: str) -> Operation | None:
        """Best effort: request cancellation and return; the worker records CANCELLED."""
        op = self.get(name)
        if op is None:
            return None
        task = self._tasks.get(name)
        if task is not None and not op.done:
            task.cancel()
        return op

    async def wait(self, op: Operation, wanted: float | None, disconnected: Disconnected) -> None:
        """Block until done, the clamped wait budget, or client disconnect, whichever is first."""
        budget = self.max_wait if wanted is None else min(wanted, self.max_wait)
        self.active_waiters += 1
        done = asyncio.create_task(op.done_event.wait())
        gone = asyncio.create_task(disconnected())
        try:
            await asyncio.wait({done, gone}, timeout=budget, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.active_waiters -= 1
            done.cancel()
            gone.cancel()
            await asyncio.gather(done, gone, return_exceptions=True)

    def sweep(self) -> int:
        """Delete expired operations and their request_id index entries together."""
        expired = [name for name in self._ops if self.get(name) is None]
        for name in expired:
            op = self._ops.pop(name)
            if self._by_request_id.get(op.request_id) == name:
                del self._by_request_id[op.request_id]
        return len(expired)


def _problem(status: int, title: str, detail: str) -> JSONResponse:
    body = {"type": "about:blank", "title": title, "status": status, "detail": detail}
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


def _not_found(operation_id: str) -> JSONResponse:
    return _problem(404, "Not Found", f"operations/{operation_id} does not exist or has expired.")


OperationId = Annotated[str, Path(pattern=OPERATION_ID)]


def create_app(store: OperationStore) -> FastAPI:
    """Expose the store with AIP-style custom methods `:wait` and `:cancel`."""
    app = FastAPI()

    @app.post("/v1/exports")
    async def create_export(body: ExportRequest) -> JSONResponse:
        try:
            op = store.start(body)
        except RequestIdReusedError:
            return _problem(422, "Unprocessable Content", "request_id reused with a new payload.")
        return JSONResponse(op.to_json(), status_code=202)

    @app.get("/v1/operations/{operation_id}")
    async def get_operation(operation_id: OperationId) -> JSONResponse:
        op = store.get(f"operations/{operation_id}")
        return _not_found(operation_id) if op is None else JSONResponse(op.to_json())

    @app.post("/v1/operations/{operation_id}:wait")
    async def wait_operation(
        operation_id: OperationId, body: WaitRequest, request: Request
    ) -> JSONResponse:
        name = f"operations/{operation_id}"
        op = store.get(name)
        if op is None:
            return _not_found(operation_id)

        async def disconnected() -> None:
            while (await request.receive())["type"] != "http.disconnect":
                pass

        await store.wait(op, body.seconds(), disconnected)
        latest = store.get(name)  # the record may have expired while we waited
        if latest is None:
            return _not_found(operation_id)
        return JSONResponse(latest.to_json(), headers={"Cache-Control": "no-store"})

    @app.post("/v1/operations/{operation_id}:cancel")
    async def cancel_operation(operation_id: OperationId) -> JSONResponse:
        op = store.cancel(f"operations/{operation_id}")
        return _not_found(operation_id) if op is None else JSONResponse({})

    return app
