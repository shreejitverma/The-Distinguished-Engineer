"""What FastAPI adds over Starlette and Pydantic: the glue layer, made inspectable."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.routing import Router

if TYPE_CHECKING:
    from starlette.types import ASGIApp


class Item(BaseModel):
    id: int
    name: str
    price: float = 1.0


@dataclass(frozen=True)
class RouteAnatomy:
    """What APIRoute compiled from one endpoint signature at registration time."""

    methods: frozenset[str]
    handler_qualname: str
    path_params: tuple[str, ...]
    query_params: tuple[str, ...]
    body_param: str | None
    response_adapter_type: str | None
    unique_id: str


def build_glue_app() -> tuple[FastAPI, APIRouter]:
    """Build an app whose routes each exercise one glue mechanism."""
    app = FastAPI(title="glue", version="1")
    router = APIRouter(prefix="/v1")

    @app.get("/items/{item_id}", response_model=Item)
    async def read_item(item_id: int, verbose: bool = False) -> dict[str, Any]:
        # `secret` is not in Item; `inf` is not valid JSON.
        return {"id": item_id, "name": "widget", "price": float("inf"), "secret": "s3"}

    @app.get("/unvalidated", response_model=None)
    async def unvalidated() -> dict[str, float]:
        # response_model=None disables the TypeAdapter: jsonable_encoder + JSONResponse.
        return {"price": float("inf")}

    @app.post("/items")
    async def create_item(item: Item) -> Item:
        return item

    # Included before it has any routes: inclusion stores the router, not copies.
    app.include_router(router)

    @router.get("/late")
    async def late() -> dict[str, bool]:
        return {"late": True}

    return app, router


def find_api_route(app: FastAPI, path: str, method: str) -> APIRoute:
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == path and method in (route.methods or ()):
            return route
    raise LookupError(f"{method} {path}")


def describe_route(route: APIRoute) -> RouteAnatomy:
    response_field = route.response_field
    adapter = None if response_field is None else vars(response_field)["_type_adapter"]
    return RouteAnatomy(
        methods=frozenset(route.methods or ()),
        handler_qualname=getattr(route.app, "__qualname__", type(route.app).__name__),
        path_params=tuple(p.name for p in route.dependant.path_params),
        query_params=tuple(p.name for p in route.dependant.query_params),
        body_param=None if route.body_field is None else route.body_field.name,
        response_adapter_type=None if adapter is None else type(adapter).__name__,
        unique_id=route.unique_id,
    )


def middleware_chain(app: Starlette) -> list[str]:
    """Walk the `.app` links of the built middleware stack, outermost first."""
    names: list[str] = []
    node: ASGIApp | None = app.build_middleware_stack()
    while node is not None:
        names.append(type(node).__name__)
        nxt: ASGIApp | None = getattr(node, "app", None)
        # Router.app is a bound method: the router is the end of the chain.
        node = None if isinstance(node, Router) else nxt
    return names
