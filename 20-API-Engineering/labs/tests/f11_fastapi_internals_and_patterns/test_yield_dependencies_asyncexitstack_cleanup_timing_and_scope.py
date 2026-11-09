"""Unit tests for FastAPI yield dependency AsyncExitStack teardown timing and scopes."""

from collections.abc import AsyncGenerator, AsyncIterator, Generator
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.exceptions import DependencyScopeError, FastAPIError

from api_labs.f11_fastapi_internals_and_patterns.yield_dependencies_scope import (
    LifecycleRecorder,
    build_lifecycle_app,
    build_post_yield_error_app,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_yield_cleanup_ordering_across_function_and_request_scopes() -> None:
    recorder = LifecycleRecorder()
    app = build_lifecycle_app(recorder)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/stream-order")

    assert response.status_code == 200
    assert response.text == "chunk-1\nchunk-2\n"
    assert recorder.stack_callback_counts == {
        "fastapi_middleware_astack": 0,
        "fastapi_inner_astack": 2,
        "fastapi_function_astack": 1,
    }
    assert recorder.active_resources == set()
    assert recorder.events == [
        "req_root:enter",
        "req_child(req_root):enter",
        "func_guard(req_root):enter",
        "endpoint:return(child_of_req_root,func_guard)",
        "func_guard:exit",
        "stream:chunk_1(req_alive=True,func_alive=False)",
        "stream:chunk_2",
        "background:run(req_alive=True,func_alive=False)",
        "req_child:exit(root_alive=True)",
        "req_root:exit",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_yield_dependency_catches_endpoint_exception_and_translates_to_409() -> None:
    recorder = LifecycleRecorder()
    app = build_lifecycle_app(recorder)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/conflict")

    assert response.status_code == 409
    assert response.json() == {"detail": "duplicate ledger key"}
    assert recorder.active_resources == set()
    assert recorder.events == [
        "req_root:enter",
        "func_guard(req_root):enter",
        "endpoint:raise_conflict",
        "func_guard:caught_conflict",
        "func_guard:exit",
        "req_root:exit",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_yield_http_exception_in_function_scope_returns_503() -> None:
    app = build_post_yield_error_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/post-yield-function-error")

    assert response.status_code == 503
    assert response.json() == {"detail": "function scope commit failed"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_yield_http_exception_in_request_scope_fails_after_response_start() -> None:
    app = build_post_yield_error_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        with pytest.raises(
            RuntimeError,
            match=r"Caught handled exception, but response already started\.",
        ):
            await client.get("/post-yield-request-error")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_swallowing_exception_in_yield_dependency_raises_fastapi_error() -> None:
    swallowed_log: list[str] = []
    app = build_post_yield_error_app(swallowed_log=swallowed_log)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        with pytest.raises(FastAPIError, match=r"Response not awaited\."):
            await client.get("/swallowed-error")

    assert swallowed_log == ["swallowed:unhandled failure"]


@pytest.mark.unit
def test_request_scoped_dependency_rejects_function_scoped_sub_dependency() -> None:
    app = FastAPI()

    async def narrow_dep() -> AsyncIterator[str]:
        yield "narrow"

    async def wide_dep(
        sub: Annotated[str, Depends(narrow_dep, scope="function")],
    ) -> AsyncIterator[str]:
        yield sub

    with pytest.raises(
        DependencyScopeError,
        match=r'The dependency "wide_dep" has a scope of "request", '
        r'it cannot depend on dependencies with scope "function"\.',
    ):

        @app.get("/invalid-scope")
        async def invalid_endpoint(
            val: Annotated[str, Depends(wide_dep, scope="request")],
        ) -> dict[str, str]:
            return {"val": val}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_computed_scope_is_part_of_dependency_cache_key() -> None:
    app = FastAPI()
    events: list[str] = []
    counter = 0

    async def shared_gen() -> AsyncIterator[int]:
        nonlocal counter
        counter += 1
        instance_id = counter
        events.append(f"enter:{instance_id}")
        try:
            yield instance_id
        finally:
            events.append(f"exit:{instance_id}")

    @app.get("/dual-scope")
    async def dual_scope(
        func_scoped: Annotated[int, Depends(shared_gen, scope="function")],
        req_scoped: Annotated[int, Depends(shared_gen, scope="request")],
    ) -> dict[str, int]:
        events.append(f"endpoint:{func_scoped}:{req_scoped}")
        return {"func": func_scoped, "req": req_scoped}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/dual-scope")

    assert response.status_code == 200
    assert response.json() == {"func": 1, "req": 2}
    assert events == [
        "enter:1",
        "enter:2",
        "endpoint:1:2",
        "exit:1",
        "exit:2",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_contextmanager_decorated_dependency_crashes_at_request_time() -> None:
    app = FastAPI()
    entered: list[str] = []

    @contextmanager
    def decorated_sync() -> Generator[str]:
        entered.append("sync")
        yield "never-injected"

    @asynccontextmanager
    async def decorated_async() -> AsyncGenerator[str]:
        entered.append("async")
        yield "never-injected"

    @app.get("/decorated-sync")
    async def sync_endpoint(
        val: Annotated[str, Depends(decorated_sync)],
    ) -> dict[str, str]:
        return {"val": val}

    @app.get("/decorated-async")
    async def async_endpoint(
        val: Annotated[str, Depends(decorated_async)],
    ) -> dict[str, str]:
        return {"val": val}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        # inspect.unwrap() follows __wrapped__, so FastAPI classifies the helper as
        # a generator and wraps it a second time; the request then crashes.
        with pytest.raises(
            AttributeError,
            match=r"'_GeneratorContextManager' object has no attribute 'throw'",
        ) as sync_info:
            await client.get("/decorated-sync")
        with pytest.raises(
            TypeError,
            match=r"'_AsyncGeneratorContextManager' object is not an async iterator",
        ):
            await client.get("/decorated-async")

    assert isinstance(sync_info.value.__context__, TypeError)
    assert "is not an iterator" in str(sync_info.value.__context__)
    assert entered == []
