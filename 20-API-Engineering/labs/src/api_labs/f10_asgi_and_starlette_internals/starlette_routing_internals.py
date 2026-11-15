"""Starlette 1.10.0 routing internals: compile_path, convertors, Mount, Host, and 405 vs 404."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Final

from starlette.applications import Starlette
from starlette.convertors import Convertor, register_url_convertor
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Host, Mount, Route, Router, compile_path
from starlette.websockets import WebSocketClose

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    type Endpoint = Callable[[Request], Awaitable[Response]]

MAX_ID_DIGITS: Final = 18  # 10**18 - 1 < 2**63 - 1, and far below CPython's 4300-digit int() limit


def describe_pattern(path: str) -> tuple[str, str, dict[str, str]]:
    """Return what `compile_path` builds: regex source, URL format, and convertor class per name."""
    regex, path_format, convertors = compile_path(path)
    return regex.pattern, path_format, {name: type(c).__name__ for name, c in convertors.items()}


class BoundedIntConvertor(Convertor[int]):
    """An `int` convertor whose regex bounds the digit count before `int()` ever runs."""

    regex = f"[0-9]{{1,{MAX_ID_DIGITS}}}"

    def convert(self, value: str) -> int:
        return int(value)

    def to_string(self, value: int) -> str:
        if value < 0 or len(str(value)) > MAX_ID_DIGITS:
            raise ValueError(f"id out of range: {value}")
        return str(value)


# register_url_convertor mutates the process-global CONVERTOR_TYPES table; it must run
# before any compile_path call that names the key, so it runs at import time here.
register_url_convertor("id64", BoundedIntConvertor())


class MethodTableRoute(Route):
    """One `Route` per path that dispatches by method, so a 405 advertises every method."""

    def __init__(self, path: str, handlers: Mapping[str, Endpoint], *, name: str) -> None:
        table = {method.upper(): handler for method, handler in handlers.items()}
        if "GET" in table and "HEAD" not in table:
            table["HEAD"] = table["GET"]
        self.table: dict[str, Endpoint] = table

        async def dispatch(request: Request) -> Response:
            return await table[request.method](request)

        super().__init__(path, dispatch, methods=list(table), name=name)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["method"] in self.table:
            await super().handle(scope, receive, send)
            return
        headers = {"Allow": ", ".join(sorted(self.table))}  # sorted: stable across hash seeds
        if "app" in scope:
            raise HTTPException(status_code=405, headers=headers)
        await PlainTextResponse("Method Not Allowed", 405, headers=headers)(scope, receive, send)


class RejectControlCharactersMiddleware:
    """Reject a decoded path holding C0 controls or DEL before any `$`-anchored regex sees it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and any(
            ord(char) < 0x20 or ord(char) == 0x7F for char in scope["path"]
        ):
            if scope["type"] == "http":
                await PlainTextResponse("Bad Request", 400)(scope, receive, send)
            else:
                await WebSocketClose(code=1008)(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def echo(request: Request) -> Response:
    """Report what routing wrote into the scope for this request."""
    return JSONResponse(
        {
            "path_params": {key: str(value) for key, value in request.path_params.items()},
            "param_types": {k: type(v).__name__ for k, v in request.path_params.items()},
            "route": request.scope["route"].path,
            "root_path": request.scope.get("root_path", ""),
            "path": request.scope["path"],
        }
    )


async def list_items(request: Request) -> Response:
    return PlainTextResponse("list")


async def create_item(request: Request) -> Response:
    return PlainTextResponse("create", status_code=201)


async def upload(request: Request) -> Response:
    return PlainTextResponse("uploaded", status_code=201)


def build_demo_app(*, redirect_slashes: bool = True) -> Starlette:
    """Every routing shape the note discusses, in one deliberately ordered route table."""
    org_router = Router(routes=[Route("/users/{uid:int}", echo, name="user")])
    app = Starlette(
        routes=[
            # Two Route objects share one path: the classic split-handler layout.
            Route("/items", list_items, methods=["GET"]),
            Route("/items", create_item, methods=["POST"]),
            # The fix: one Route that owns every method of the resource.
            MethodTableRoute("/things", {"GET": list_items, "POST": create_item}, name="things"),
            Route("/health", list_items),
            Route("/n/{value:int}", echo),
            Route("/b/{value:id64}", echo),
            Route("/f/{value:float}", echo),
            Route("/u/{value:uuid}", echo),
            Route("/s/{value}", echo),
            Route("/p/{value:path}", echo),
            # Declaration order is precedence: the parameterised route shadows /users/me.
            Route("/users/{name}", echo),
            Route("/users/me", echo, name="me"),
            # A POST-only route that a later Mount silently converts from 405 into 404.
            Route("/api/upload", upload, methods=["POST"]),
            Mount("/api", name="api", routes=[Route("/users/{uid:int}", echo, name="user")]),
            Mount("/org/{org}", app=org_router, name="org"),
            Host("{sub}.example.org", app=Router(routes=[Route("/", echo)]), name="tenant"),
        ]
    )
    app.router.redirect_slashes = redirect_slashes
    return app


class _NoopASGI:
    """A class-based endpoint: `Route` treats it as raw ASGI and applies no method filter."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


async def _receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _discard(message: Message) -> None:
    return None


async def measure_dispatch_ns(n_routes: int, target_index: int, iterations: int) -> float:
    """Mean nanoseconds for `Router` to scan to route `target_index` of `n_routes` and dispatch."""
    endpoint = _NoopASGI()
    router = Router(routes=[Route(f"/r{i}/{{item_id:int}}", endpoint) for i in range(n_routes)])
    base: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": f"/r{target_index}/42",
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }
    start = time.perf_counter_ns()
    for _ in range(iterations):
        await router(dict(base), _receive, _discard)
    return (time.perf_counter_ns() - start) / iterations
