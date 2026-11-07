"""FastAPI yield dependency AsyncExitStack teardown timing, exception handling, and scopes."""

from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse


class DomainConflictError(Exception):
    """Domain error translated into an HTTP 409 response by a yield dependency."""


@dataclass
class LifecycleRecorder:
    """Deterministic event log and resource state tracker for yield dependency tests."""

    events: list[str] = field(default_factory=list[str])
    active_resources: set[str] = field(default_factory=set[str])
    stack_callback_counts: dict[str, int] = field(default_factory=dict[str, int])

    def record(self, event: str) -> None:
        self.events.append(event)


def build_lifecycle_app(recorder: LifecycleRecorder) -> FastAPI:
    """Build a FastAPI app proving scope='function' vs scope='request' teardown order."""
    app = FastAPI()

    async def request_root_dep() -> AsyncIterator[str]:
        recorder.record("req_root:enter")
        recorder.active_resources.add("req_root")
        try:
            yield "req_root"
        finally:
            recorder.active_resources.discard("req_root")
            recorder.record("req_root:exit")

    async def request_child_dep(
        root: Annotated[str, Depends(request_root_dep, scope="request")],
    ) -> AsyncIterator[str]:
        recorder.record(f"req_child({root}):enter")
        recorder.active_resources.add("req_child")
        try:
            yield f"child_of_{root}"
        finally:
            # Parent resource must still be active during child LIFO teardown.
            parent_alive = "req_root" in recorder.active_resources
            recorder.active_resources.discard("req_child")
            recorder.record(f"req_child:exit(root_alive={parent_alive})")

    def function_guard_dep(
        root: Annotated[str, Depends(request_root_dep, scope="request")],
    ) -> Iterator[str]:
        recorder.record(f"func_guard({root}):enter")
        recorder.active_resources.add("func_guard")
        try:
            yield "func_guard"
        except DomainConflictError as exc:
            recorder.record("func_guard:caught_conflict")
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            recorder.active_resources.discard("func_guard")
            recorder.record("func_guard:exit")

    @app.get("/stream-order")
    async def stream_order(
        request: Request,
        background_tasks: BackgroundTasks,
        req_val: Annotated[str, Depends(request_child_dep, scope="request")],
        func_val: Annotated[str, Depends(function_guard_dep, scope="function")],
    ) -> StreamingResponse:
        for key in (
            "fastapi_middleware_astack",
            "fastapi_inner_astack",
            "fastapi_function_astack",
        ):
            stack: object = request.scope.get(key)
            callbacks: tuple[object, ...] = getattr(stack, "_exit_callbacks", ())
            if isinstance(stack, AsyncExitStack):
                recorder.stack_callback_counts[key] = len(callbacks)

        recorder.record(f"endpoint:return({req_val},{func_val})")
        background_tasks.add_task(
            lambda: recorder.record(
                f"background:run(req_alive={'req_child' in recorder.active_resources},"
                f"func_alive={'func_guard' in recorder.active_resources})"
            )
        )

        async def chunk_stream() -> AsyncIterator[bytes]:
            recorder.record(
                f"stream:chunk_1(req_alive={'req_child' in recorder.active_resources},"
                f"func_alive={'func_guard' in recorder.active_resources})"
            )
            yield b"chunk-1\n"
            recorder.record("stream:chunk_2")
            yield b"chunk-2\n"

        return StreamingResponse(chunk_stream(), media_type="text/plain")

    @app.get("/conflict")
    async def conflict_endpoint(
        _guard: Annotated[str, Depends(function_guard_dep, scope="function")],
    ) -> dict[str, str]:
        recorder.record("endpoint:raise_conflict")
        raise DomainConflictError("duplicate ledger key")

    return app


def build_post_yield_error_app(swallowed_log: list[str] | None = None) -> FastAPI:
    """Build an app contrasting post-yield HTTPException in function vs request scope."""
    app = FastAPI()

    async def function_commit_dep() -> AsyncIterator[str]:
        yield "tx_func"
        raise HTTPException(status_code=503, detail="function scope commit failed")

    async def request_commit_dep() -> AsyncIterator[str]:
        yield "tx_req"
        raise HTTPException(status_code=503, detail="request scope commit failed")

    async def swallowing_dep() -> AsyncIterator[str]:
        try:
            yield "swallow"
        except ValueError as exc:
            # Anti-pattern: catching and logging without re-raising suppresses the error.
            if swallowed_log is not None:
                swallowed_log.append(f"swallowed:{exc}")

    @app.get("/post-yield-function-error")
    async def post_yield_func(
        tx: Annotated[str, Depends(function_commit_dep, scope="function")],
    ) -> dict[str, str]:
        return {"tx": tx}

    @app.get("/post-yield-request-error")
    async def post_yield_req(
        tx: Annotated[str, Depends(request_commit_dep, scope="request")],
    ) -> dict[str, str]:
        return {"tx": tx}

    @app.get("/swallowed-error")
    async def swallowed_error(
        _s: Annotated[str, Depends(swallowing_dep, scope="request")],
    ) -> dict[str, str]:
        raise ValueError("unhandled failure")

    return app
