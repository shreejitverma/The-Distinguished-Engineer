"""Architectural performance limits and comparison across FastAPI, Litestar, and Starlette."""

import time
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from litestar import Litestar, get
from litestar.di import NamedDependency, Provide
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


@dataclass(frozen=True)
class FrameworkColdStartMetrics:
    """Startup initialization duration for N-route applications."""

    starlette_init_ms: float
    fastapi_init_ms: float
    litestar_init_ms: float
    route_count: int


def build_starlette_app(route_count: int = 500) -> Starlette:
    """Build a raw Starlette application with N linearly matched routes."""
    routes: list[Route] = []
    for i in range(route_count):

        async def handler(request: Request, idx: int = i) -> Response:
            return JSONResponse({"route": idx, "framework": "starlette"})

        routes.append(Route(f"/route_{i}", handler, methods=["GET"]))
    return Starlette(routes=routes)


def build_fastapi_app(route_count: int = 500) -> FastAPI:
    """Build a FastAPI application with N routes and Dependant structures."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    for i in range(route_count):

        def _register(idx: int) -> None:
            @app.get(f"/route_{idx}")
            async def endpoint() -> dict[str, Any]:
                return {"route": idx, "framework": "fastapi"}

        _register(i)
    return app


def build_litestar_app(route_count: int = 500) -> Litestar:
    """Build a Litestar application with N routes compiled into a routing trie."""
    route_handlers: list[Any] = []
    for i in range(route_count):

        def _make_handler(idx: int) -> Any:
            @get(f"/route_{idx}")
            async def handler() -> dict[str, Any]:
                return {"route": idx, "framework": "litestar"}

            return handler

        route_handlers.append(_make_handler(i))
    return Litestar(route_handlers=route_handlers, openapi_config=None)


def measure_cold_start(route_count: int = 100) -> FrameworkColdStartMetrics:
    """Measure synchronous app instantiation and route compilation time."""
    t0 = time.perf_counter()
    _ = build_starlette_app(route_count)
    t_starlette = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    _ = build_fastapi_app(route_count)
    t_fastapi = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    _ = build_litestar_app(route_count)
    t_litestar = (time.perf_counter() - t0) * 1000.0

    return FrameworkColdStartMetrics(
        starlette_init_ms=t_starlette,
        fastapi_init_ms=t_fastapi,
        litestar_init_ms=t_litestar,
        route_count=route_count,
    )


# Dependency injection topologies for FastAPI
async def async_dep_1() -> int:
    return 1


async def async_dep_2(d1: Annotated[int, Depends(async_dep_1)]) -> int:
    return d1 + 1


async def async_dep_3(d2: Annotated[int, Depends(async_dep_2)]) -> int:
    return d2 + 1


async def async_dep_4(d3: Annotated[int, Depends(async_dep_3)]) -> int:
    return d3 + 1


async def async_dep_5(d4: Annotated[int, Depends(async_dep_4)]) -> int:
    return d4 + 1


def sync_dep_1() -> int:
    return 1


def sync_dep_2(s1: Annotated[int, Depends(sync_dep_1)]) -> int:
    return s1 + 1


def sync_dep_3(s2: Annotated[int, Depends(sync_dep_2)]) -> int:
    return s2 + 1


def sync_dep_4(s3: Annotated[int, Depends(sync_dep_3)]) -> int:
    return s3 + 1


def sync_dep_5(s4: Annotated[int, Depends(sync_dep_4)]) -> int:
    return s4 + 1


def build_fastapi_di_app() -> FastAPI:
    """Build a FastAPI app testing 0, 1, 5 async deps and 1, 5 sync (threadpool) deps."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/dep-0")
    async def get_dep_0() -> dict[str, int]:
        return {"result": 0}

    @app.get("/dep-1")
    async def get_dep_1(d: Annotated[int, Depends(async_dep_1)]) -> dict[str, int]:
        return {"result": d}

    @app.get("/dep-5")
    async def get_dep_5(d: Annotated[int, Depends(async_dep_5)]) -> dict[str, int]:
        return {"result": d}

    @app.get("/dep-sync-1")
    async def get_dep_sync_1(s: Annotated[int, Depends(sync_dep_1)]) -> dict[str, int]:
        return {"result": s}

    @app.get("/dep-sync-5")
    async def get_dep_sync_5(s: Annotated[int, Depends(sync_dep_5)]) -> dict[str, int]:
        return {"result": s}

    return app


# Litestar dependencies
async def litestar_dep_1() -> int:
    return 1


async def litestar_dep_2(d1: NamedDependency[int]) -> int:
    return d1 + 1


async def litestar_dep_3(d2: NamedDependency[int]) -> int:
    return d2 + 1


async def litestar_dep_4(d3: NamedDependency[int]) -> int:
    return d3 + 1


async def litestar_dep_5(d4: NamedDependency[int]) -> int:
    return d4 + 1


def build_litestar_di_app() -> Litestar:
    """Build a Litestar app testing 0, 1, and 5 compiled dependencies."""

    @get("/dep-0")
    async def get_dep_0() -> dict[str, int]:
        return {"result": 0}

    @get(
        "/dep-1",
        dependencies={"d": Provide(litestar_dep_1)},
    )
    async def get_dep_1(d: NamedDependency[int]) -> dict[str, int]:
        return {"result": d}

    @get(
        "/dep-5",
        dependencies={
            "d1": Provide(litestar_dep_1),
            "d2": Provide(litestar_dep_2),
            "d3": Provide(litestar_dep_3),
            "d4": Provide(litestar_dep_4),
            "d5": Provide(litestar_dep_5),
        },
    )
    async def get_dep_5(d5: NamedDependency[int]) -> dict[str, int]:
        return {"result": d5}

    return Litestar(route_handlers=[get_dep_0, get_dep_1, get_dep_5], openapi_config=None)
