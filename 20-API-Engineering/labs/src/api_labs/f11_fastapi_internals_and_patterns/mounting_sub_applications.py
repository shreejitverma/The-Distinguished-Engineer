"""Incremental monolith migration: mounting legacy WSGI apps, sub-apps, and static assets.

Demonstrates:
1. WSGIMiddleware mounting of legacy Flask applications (SCRIPT_NAME, PATH_INFO, url_for).
2. Strangler Fig pattern with root-level fallback routing.
3. Versioned sub-application mounting with independent OpenAPI schemas.
4. Composite lifespan execution across mounted ASGI sub-applications.
5. Shared ASGI authentication middleware bridging identity into WSGI environ.
6. StaticFiles mounting with RFC 9110 conditional caching (ETag / 304) and path safety.
"""

from __future__ import annotations

import warnings
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from flask import Flask, jsonify, request, url_for
from starlette.exceptions import StarletteDeprecationWarning
from starlette.staticfiles import StaticFiles

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from starlette.types import ASGIApp, Receive, Scope, Send

# Suppress Starlette's deprecation warning for WSGIMiddleware during migration
warnings.filterwarnings(
    "ignore",
    category=StarletteDeprecationWarning,
    message=r".*starlette\.middleware\.wsgi is deprecated.*",
)
from starlette.middleware.wsgi import WSGIMiddleware  # noqa: E402

# Internal identity header the perimeter middleware owns; ASGI header names are lowercase bytes.
IDENTITY_HEADER = b"x-authenticated-user"


@dataclass
class LifecycleTracker:
    """Tracks lifespan startup and shutdown states across parent and mounted sub-apps."""

    events: list[str] = field(default_factory=list[str])

    def record(self, event: str) -> None:
        self.events.append(event)


def create_legacy_flask_app() -> Flask:
    """Constructs a legacy Flask WSGI application representing the unmigrated monolith."""
    flask_app = Flask("legacy_monolith")

    @flask_app.route("/items", methods=["GET"])
    def get_items() -> Any:
        return jsonify(
            {
                "service": "legacy_flask",
                "items": [{"id": 1, "name": "Monolith Widget"}],
                "detail_url": url_for("get_item_detail", item_id=1),
            }
        )

    @flask_app.route("/items/<int:item_id>", methods=["GET"])
    def get_item_detail(item_id: int) -> Any:
        return jsonify(
            {
                "service": "legacy_flask",
                "item_id": item_id,
                "script_name": request.environ.get("SCRIPT_NAME", ""),
                "path_info": request.environ.get("PATH_INFO", ""),
                "full_path": request.full_path,
                "generated_self_url": url_for("get_item_detail", item_id=item_id, _external=False),
            }
        )

    @flask_app.route("/authenticated-resource", methods=["GET"])
    def get_authenticated_resource() -> Any:
        user_header = request.headers.get("X-Authenticated-User")
        environ_user = request.environ.get("HTTP_X_AUTHENTICATED_USER")
        if not user_header and not environ_user:
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify(
            {
                "service": "legacy_flask",
                "authenticated_user": user_header or environ_user,
            }
        )

    @flask_app.route("/legacy-fallback", methods=["GET"])
    def get_legacy_fallback() -> Any:
        return jsonify(
            {
                "service": "legacy_flask",
                "status": "handled_by_fallback",
                "path": request.path,
            }
        )

    return flask_app


def create_v2_sub_app(tracker: LifecycleTracker | None = None) -> FastAPI:
    """Constructs a modern FastAPI v2 sub-application with its own lifespan and OpenAPI doc."""

    @asynccontextmanager
    async def v2_lifespan(app: FastAPI) -> AsyncGenerator[dict[str, Any], None]:
        if tracker:
            tracker.record("v2:startup")
        try:
            yield {"v2_initialized": True}
        finally:
            if tracker:
                tracker.record("v2:shutdown")

    v2_app = FastAPI(
        title="Modern Service v2",
        version="2.0.0",
        lifespan=v2_lifespan,
    )

    @v2_app.get("/items")
    async def list_items_v2() -> dict[str, Any]:
        return {
            "service": "fastapi_v2",
            "items": [{"id": 1, "name": "Async Widget v2", "version": 2}],
        }

    @v2_app.get("/health")
    async def v2_health() -> dict[str, str]:
        return {"status": "healthy", "service": "v2"}

    return v2_app


class SharedAuthMiddleware:
    """Pure ASGI middleware enforcing authentication across FastAPI and mounted WSGI apps.

    Bridges authenticated identity into downstream WSGI HTTP headers.
    """

    def __init__(self, app: ASGIApp, protected_prefixes: tuple[str, ...]) -> None:
        self.app = app
        self.protected_prefixes = protected_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Strip any client-supplied identity header on every request, so a client
        # cannot spoof HTTP_X_AUTHENTICATED_USER into the WSGI environ.
        scope["headers"] = [
            (name, value) for name, value in scope.get("headers", []) if name != IDENTITY_HEADER
        ]

        path: str = scope.get("path", "")
        is_protected = any(path.startswith(prefix) for prefix in self.protected_prefixes)

        if is_protected:
            headers = dict(scope["headers"])
            auth_val = headers.get(b"authorization", b"").decode("latin1")
            if not auth_val.startswith("Bearer valid-token-"):
                response = JSONResponse(
                    {"detail": "Missing or invalid bearer token"},
                    status_code=401,
                )
                await response(scope, receive, send)
                return

            user_id = auth_val.removeprefix("Bearer valid-token-")
            # Mutate headers in scope so downstream WSGI environ receives HTTP_X_AUTHENTICATED_USER
            new_headers = list(scope["headers"])
            new_headers.append((IDENTITY_HEADER, user_id.encode("latin1")))
            scope["headers"] = new_headers
            if "state" not in scope:
                scope["state"] = {}
            scope["state"]["user_id"] = user_id

        await self.app(scope, receive, send)


def make_composite_lifespan(
    tracker: LifecycleTracker,
    child_apps: list[FastAPI],
) -> Callable[[FastAPI], AbstractAsyncContextManager[dict[str, Any]]]:
    """Builds a composite lifespan context manager executing parent and child lifespans."""

    @asynccontextmanager
    async def composite_lifespan(parent_app: FastAPI) -> AsyncGenerator[dict[str, Any], None]:
        tracker.record("parent:startup")
        async with AsyncExitStack() as stack:
            for child in child_apps:
                # Enter each child app's lifespan context
                ctx = child.router.lifespan_context(child)
                await stack.enter_async_context(ctx)
            yield {"system_ready": True}
        tracker.record("parent:shutdown")

    return composite_lifespan


def aggregate_openapi_schemas(
    root_app: FastAPI,
    mounted_apps: dict[str, FastAPI],
) -> dict[str, Any]:
    """Aggregates OpenAPI schemas from the root application and all mounted sub-applications."""
    root_schema = dict(root_app.openapi())
    aggregated_paths: dict[str, Any] = dict(root_schema.get("paths", {}))

    for mount_path, child_app in mounted_apps.items():
        child_schema = child_app.openapi()
        child_paths = child_schema.get("paths", {})
        clean_mount = mount_path.rstrip("/")
        for sub_path, path_item in child_paths.items():
            full_path = f"{clean_mount}{sub_path}"
            aggregated_paths[full_path] = path_item

    root_schema["paths"] = aggregated_paths
    return root_schema


def build_strangler_system(
    static_dir: Path,
    tracker: LifecycleTracker | None = None,
    enable_root_fallback: bool = True,
) -> tuple[FastAPI, Flask, FastAPI]:
    """Assembles the complete Strangler Fig architecture."""
    tracker_instance = tracker or LifecycleTracker()
    flask_app = create_legacy_flask_app()
    v2_app = create_v2_sub_app(tracker_instance)

    lifespan_handler = make_composite_lifespan(tracker_instance, [v2_app])

    root_app = FastAPI(
        title="Strangler Gateway Monolith",
        version="1.0.0",
        lifespan=lifespan_handler,
    )

    # Native FastAPI routes taking precedence in the routing table
    @root_app.get("/api/v1/status")
    async def root_status() -> dict[str, str]:
        return {"status": "operational", "engine": "fastapi"}

    @root_app.get("/items/strangled")
    async def strangled_item() -> dict[str, Any]:
        return {
            "service": "fastapi_strangled",
            "message": "This endpoint was intercepted from legacy monolith",
        }

    # Mount static files with directory checks
    root_app.mount(
        "/static",
        StaticFiles(directory=str(static_dir), check_dir=False),
        name="static",
    )

    # Mount versioned sub-application
    root_app.mount("/v2", v2_app, name="v2_subapp")

    # Mount explicit legacy WSGI application at /legacy
    root_app.mount("/legacy", WSGIMiddleware(flask_app), name="legacy_wsgi")

    # If fallback enabled, mount legacy WSGI application at root "/"
    if enable_root_fallback:
        root_app.mount("/", WSGIMiddleware(flask_app), name="root_fallback_wsgi")

    # Wrap root application with shared ASGI authentication middleware
    protected = (
        "/legacy/authenticated-resource",
        "/authenticated-resource",
        "/v2/items",
    )
    root_app.add_middleware(SharedAuthMiddleware, protected_prefixes=protected)

    return root_app, flask_app, v2_app
