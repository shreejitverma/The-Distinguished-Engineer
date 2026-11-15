"""FastAPI parameter sources: Path, Query, Header, Cookie, Body, embed, and parameter models."""

from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import Body, Cookie, Depends, FastAPI, Header, Path, Query
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field


class Item(BaseModel):
    name: str
    price: float = Field(gt=0)


class User(BaseModel):
    username: str


class FilterParams(BaseModel):
    """Query parameter model: every field is read from the query string."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=10, gt=0, le=100)
    order_by: Literal["created_at", "updated_at"] = "created_at"
    tags: list[str] = []


class TraceHeaders(BaseModel):
    """Header parameter model: field `x_trace_id` is read from header `x-trace-id`."""

    x_trace_id: str
    x_hop: list[str] = []


class SessionCookies(BaseModel):
    """Cookie parameter model with an allowlist of cookie names."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    theme: str = "light"


def build_param_app(dependency_calls: list[str] | None = None) -> FastAPI:
    """Build one endpoint per parameter-source mechanism the note explains."""
    app = FastAPI()
    calls = dependency_calls if dependency_calls is not None else []

    async def audit(x_tenant: Annotated[str, Header()]) -> str:
        calls.append(x_tenant)
        return x_tenant

    @app.get("/audited")
    async def audited(
        tenant: Annotated[str, Depends(audit)],
        n: int,
    ) -> dict[str, object]:
        return {"tenant": tenant, "n": n}

    @app.get("/items/{item_id}")
    async def read_item(
        item_id: Annotated[int, Path(ge=1)],
        tags: Annotated[list[str] | None, Query()] = None,
        verbose: bool = False,
    ) -> dict[str, object]:
        return {"item_id": item_id, "tags": tags, "verbose": verbose}

    @app.get("/headers")
    async def read_headers(
        x_request_id: Annotated[str, Header()],
        x_hop: Annotated[list[str], Header()],
        legacy_key: Annotated[str | None, Header(convert_underscores=False)] = None,
    ) -> dict[str, object]:
        return {"x_request_id": x_request_id, "x_hop": x_hop, "legacy_key": legacy_key}

    @app.get("/search")
    async def search(filters: Annotated[FilterParams, Query()]) -> FilterParams:
        return filters

    @app.get("/trace")
    async def trace(
        headers: Annotated[TraceHeaders | None, Header()] = None,
    ) -> dict[str, object]:
        return {"headers": None if headers is None else headers.model_dump()}

    @app.get("/session")
    async def session(cookies: Annotated[SessionCookies, Cookie()]) -> SessionCookies:
        return cookies

    @app.get("/strict")
    async def strict(n: Annotated[int, Query(strict=True)]) -> dict[str, int]:
        return {"n": n}

    @app.post("/single")
    async def single(item: Item) -> Item:
        return item

    @app.post("/embedded")
    async def embedded(item: Annotated[Item, Body(embed=True)]) -> Item:
        return item

    @app.post("/multi")
    async def multi(
        item: Item,
        user: User,
        importance: Annotated[int, Body(gt=0)],
    ) -> dict[str, object]:
        return {"item": item.name, "user": user.username, "importance": importance}

    @app.post("/scalar")
    async def scalar(count: Annotated[int, Body()]) -> dict[str, int]:
        return {"count": count}

    return app


def param_sources(endpoint: Callable[..., Any], path: str) -> dict[str, str]:
    """Map each parameter name to the source FastAPI infers for it at route compile time."""
    app = FastAPI()
    app.add_api_route(path, endpoint, methods=["POST"])
    route = app.router.routes[-1]
    if not isinstance(route, APIRoute):
        raise TypeError(f"expected APIRoute, got {type(route).__name__}")
    dependant = route.dependant
    sources: dict[str, str] = {}
    for source, fields in (
        ("path", dependant.path_params),
        ("query", dependant.query_params),
        ("header", dependant.header_params),
        ("cookie", dependant.cookie_params),
        ("body", dependant.body_params),
    ):
        for field in fields:
            sources[field.name] = source
    return sources
