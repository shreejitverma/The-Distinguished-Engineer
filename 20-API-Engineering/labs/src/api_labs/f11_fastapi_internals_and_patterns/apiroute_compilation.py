"""Inspect how FastAPI compiles an endpoint signature into a Dependant tree and a body field."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Cookie,
    Depends,
    FastAPI,
    Header,
    Query,
    Request,
    Response,
)
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel

if TYPE_CHECKING:
    from fastapi.dependencies.models import Dependant


class Item(BaseModel):
    name: str
    price: float


class User(BaseModel):
    username: str


class Page(BaseModel):
    """Query parameter model: one annotation that FastAPI flattens into several query keys."""

    limit: int = 10
    offset: int = 0


@dataclass(frozen=True)
class ParamLayout:
    """The per-location buckets that get_dependant() fills for one callable."""

    call: str
    path: tuple[str, ...]
    query: tuple[str, ...]
    header: tuple[tuple[str, str], ...]
    cookie: tuple[str, ...]
    body: tuple[str, ...]
    injected: tuple[tuple[str, str], ...]
    dependencies: tuple[ParamLayout, ...]


def describe_dependant(dependant: Dependant) -> ParamLayout:
    """Flatten a Dependant into plain tuples, recursing into sub-dependencies."""
    injected = (
        ("request", dependant.request_param_name),
        ("websocket", dependant.websocket_param_name),
        ("http_connection", dependant.http_connection_param_name),
        ("response", dependant.response_param_name),
        ("background_tasks", dependant.background_tasks_param_name),
        ("security_scopes", dependant.security_scopes_param_name),
    )
    return ParamLayout(
        call=getattr(dependant.call, "__name__", repr(dependant.call)),
        path=tuple(f.name for f in dependant.path_params),
        query=tuple(f.name for f in dependant.query_params),
        header=tuple((f.name, f.alias) for f in dependant.header_params),
        cookie=tuple(f.name for f in dependant.cookie_params),
        body=tuple(f.name for f in dependant.body_params),
        injected=tuple((kind, name) for kind, name in injected if name is not None),
        dependencies=tuple(describe_dependant(sub) for sub in dependant.dependencies),
    )


def common_filters(
    q: str | None = None,
    x_trace_id: Annotated[str | None, Header()] = None,
    actor: User | None = None,
) -> str | None:
    """A dependency that contributes a query param, a header, and a body param."""
    return q


def catalog_endpoint(
    item_id: int,
    item: Item,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    tags: list[int],
    sizes: Annotated[list[int] | None, Query()] = None,
    page: Annotated[Page | None, Query()] = None,
    session: Annotated[str | None, Cookie()] = None,
    verbose: bool = False,
    filters: Annotated[str | None, Depends(common_filters)] = None,
) -> dict[str, Any]:
    """An endpoint whose parameters land in every bucket get_dependant() knows."""
    return {"item_id": item_id}


def compile_layout(path: str, call: Any) -> ParamLayout:
    """Run get_dependant() exactly as a caller outside APIRoute would."""
    return describe_dependant(get_dependant(path=path, call=call))


@dataclass(frozen=True)
class BodyPlan:
    """What APIRoute decided about the request body at registration time."""

    embed: bool
    field_name: str
    field_alias: str
    model: str
    model_fields: tuple[str, ...]
    field_info_class: str


def body_plan(route: APIRoute) -> BodyPlan | None:
    """Read the merged body field that APIRoute built from all flattened body params."""
    field = route.body_field
    if field is None:
        return None
    annotation: object = field.field_info.annotation
    model_name = repr(annotation)
    model_fields: tuple[str, ...] = ()
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        model_name = annotation.__name__
        model_fields = tuple(annotation.model_fields)
    return BodyPlan(
        embed=route._embed_body_fields,  # pyright: ignore[reportPrivateUsage]
        field_name=field.name,
        field_alias=field.alias,
        model=model_name,
        model_fields=model_fields,
        field_info_class=type(field.field_info).__name__,
    )


def actor_from_body(actor: User) -> str:
    """A dependency whose body parameter is merged into the endpoint's request body."""
    return actor.username


def build_body_app() -> FastAPI:
    """Routes that exercise each body-merging rule in _should_embed_body_fields()."""
    app = FastAPI()

    @app.post("/single/{item_id:int}")
    def single(item_id: int, item: Item) -> dict[str, Any]:
        return {"item_id": item_id, "name": item.name}

    @app.post("/multi")
    def multi(item: Item, user: User) -> dict[str, Any]:
        return {"name": item.name, "user": user.username}

    @app.post("/embedded")
    def embedded(item: Annotated[Item, Body(embed=True)]) -> dict[str, Any]:
        return {"name": item.name}

    @app.post("/via-dependency")
    def via_dependency(
        item: Item, actor: Annotated[str, Depends(actor_from_body)]
    ) -> dict[str, Any]:
        return {"name": item.name, "actor": actor}

    @app.post("/same-name")
    def same_name(
        actor: User, username: Annotated[str, Depends(actor_from_body)]
    ) -> dict[str, Any]:
        return {"direct": actor.username, "from_dependency": username}

    return app


def route_by_path(app: FastAPI, path: str) -> APIRoute:
    """Find a directly registered APIRoute by its declared path template."""
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == path:
            return route
    raise LookupError(path)


def build_prefixed_router() -> tuple[APIRouter, FastAPI]:
    """A router whose endpoint parameter only becomes a path parameter after inclusion."""
    router = APIRouter()

    @router.get("/orders")
    def list_orders(user_id: int) -> dict[str, int]:
        return {"user_id": user_id}

    app = FastAPI()
    app.include_router(router, prefix="/users/{user_id}")
    return router, app


def build_forward_ref_trap_app() -> FastAPI:
    """A function-local model plus PEP 563 string annotations: the name cannot be resolved."""

    class LocalItem(BaseModel):
        name: str

    app = FastAPI()

    @app.post("/trap")
    def trap(item: LocalItem) -> dict[str, str]:
        return {"name": item.name}

    return app


def type_checking_only_dependency(q: str) -> Dependant:
    """Return annotation names a TYPE_CHECKING-only import, so eval_str=True raises NameError."""
    raise NotImplementedError


def time_compilation(repeats: int = 500) -> dict[str, float]:
    """Best-of-five mean microseconds per compile; for the note's table, not asserted."""

    def plain() -> None:
        return None

    cases: dict[str, Any] = {
        "get_dependant(no params)": lambda: get_dependant(path="/", call=plain),
        "get_dependant(catalog_endpoint)": lambda: get_dependant(
            path="/items/{item_id}", call=catalog_endpoint
        ),
        "APIRoute(no params)": lambda: APIRoute("/", plain),
        "APIRoute(catalog_endpoint)": lambda: APIRoute(
            "/items/{item_id}", catalog_endpoint, methods=["POST"]
        ),
    }
    results: dict[str, float] = {}
    for label, build in cases.items():
        for _ in range(20):
            build()
        samples: list[float] = []
        for _ in range(5):
            start = time.perf_counter()
            for _ in range(repeats):
                build()
            samples.append((time.perf_counter() - start) / repeats * 1e6)
        results[label] = min(samples)
    return results
