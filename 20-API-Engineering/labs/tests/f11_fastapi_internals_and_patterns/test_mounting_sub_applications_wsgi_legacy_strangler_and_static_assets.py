"""Executable verification for mounting WSGI apps, sub-apps, and static assets."""

from __future__ import annotations

import tempfile
import warnings
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest
from asgi_lifespan import LifespanManager
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import StarletteDeprecationWarning
from starlette.staticfiles import StaticFiles

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator
    from pathlib import Path

warnings.filterwarnings(
    "ignore",
    category=StarletteDeprecationWarning,
    message=r".*starlette\.middleware\.wsgi is deprecated.*",
)
from starlette.middleware.wsgi import WSGIMiddleware  # noqa: E402

from api_labs.f11_fastapi_internals_and_patterns.mounting_sub_applications import (  # noqa: E402
    LifecycleTracker,
    aggregate_openapi_schemas,
    build_strangler_system,
    create_legacy_flask_app,
)


@pytest.fixture
def static_workspace() -> Iterator[Path]:
    """Provides a hermetic temporary directory with sample static files."""
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        static_dir = tmp_path / "static_assets"
        static_dir.mkdir(parents=True, exist_ok=True)

        # Create a sample CSS file
        css_file = static_dir / "style.css"
        css_file.write_text("body { background: #fafafa; }", encoding="utf-8")

        # Create a file outside the static directory to test path traversal
        secret_file = tmp_path / "secret.env"
        secret_file.write_text("SUPER_SECRET_KEY=12345", encoding="utf-8")

        yield static_dir


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wsgi_mount_script_name_and_path_info_propagation(
    static_workspace: Path,
) -> None:
    """Proves WSGIMiddleware maps ASGI scope['root_path'] to SCRIPT_NAME and PATH_INFO."""
    root_app, _, _ = build_strangler_system(static_workspace, enable_root_fallback=False)

    transport = ASGITransport(app=root_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Request mounted legacy route
        response = await client.get("/legacy/items/42?query=active")
        assert response.status_code == 200
        data = response.json()

        # In WSGI, SCRIPT_NAME is the mount prefix, PATH_INFO is the remaining path
        assert data["service"] == "legacy_flask"
        assert data["item_id"] == 42
        assert data["script_name"] == "/legacy"
        assert data["path_info"] == "/items/42"
        assert data["full_path"] == "/items/42?query=active"

        # Flask's url_for must automatically preserve SCRIPT_NAME in generated URLs
        assert data["generated_self_url"] == "/legacy/items/42"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strangler_fig_root_fallback_and_route_precedence(
    static_workspace: Path,
) -> None:
    """Proves route precedence: native routes intercept before falling back to WSGI."""
    root_app, _, _ = build_strangler_system(static_workspace, enable_root_fallback=True)

    transport = ASGITransport(app=root_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Native FastAPI route matches first
        r_native = await client.get("/items/strangled")
        assert r_native.status_code == 200
        assert r_native.json()["service"] == "fastapi_strangled"

        # 2. Unmigrated route falls through to root WSGI fallback
        r_fallback = await client.get("/legacy-fallback")
        assert r_fallback.status_code == 200
        assert r_fallback.json()["service"] == "legacy_flask"
        assert r_fallback.json()["status"] == "handled_by_fallback"

        # 3. Completely unknown route returns 404 from the fallback legacy app
        r_not_found = await client.get("/completely-unknown-route")
        assert r_not_found.status_code == 404


@pytest.mark.unit
def test_independent_openapi_documentation_isolation(
    static_workspace: Path,
) -> None:
    """Proves mounted sub-apps keep OpenAPI schemas isolated unless aggregated."""
    root_app, _, v2_app = build_strangler_system(static_workspace)

    root_schema = root_app.openapi()
    v2_schema = v2_app.openapi()

    # Root OpenAPI documentation does NOT contain mounted sub-app routes
    assert "/api/v1/status" in root_schema["paths"]
    assert "/items/strangled" in root_schema["paths"]
    assert "/v2/items" not in root_schema["paths"]
    assert "/legacy/items" not in root_schema["paths"]

    # v2 OpenAPI schema has its own paths and metadata
    assert v2_schema["info"]["title"] == "Modern Service v2"
    assert "/items" in v2_schema["paths"]
    assert "/health" in v2_schema["paths"]

    # Aggregated schema merges child paths prefixed with their mount point
    aggregated = aggregate_openapi_schemas(root_app, {"/v2": v2_app})
    assert "/v2/items" in aggregated["paths"]
    assert "/v2/health" in aggregated["paths"]
    assert "/api/v1/status" in aggregated["paths"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_subapp_lifespan_execution_via_composite_lifespan(
    static_workspace: Path,
) -> None:
    """Proves Starlette Mount needs a composite lifespan to run child lifespans."""
    tracker = LifecycleTracker()
    root_app, _, _ = build_strangler_system(static_workspace, tracker=tracker)

    # Boot the application using LifespanManager
    async with LifespanManager(root_app):
        # Startup must have executed both parent and v2 child lifespans
        assert tracker.events == ["parent:startup", "v2:startup"]

    # Shutdown occurs in reverse LIFO order
    assert tracker.events == [
        "parent:startup",
        "v2:startup",
        "v2:shutdown",
        "parent:shutdown",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_mount_omits_subapp_lifespan() -> None:
    """Proves that default Starlette Mount never triggers sub-app lifespans."""
    tracker = LifecycleTracker()

    @asynccontextmanager
    async def sub_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        tracker.record("sub:startup")
        yield
        tracker.record("sub:shutdown")

    sub = FastAPI(lifespan=sub_lifespan)
    parent = FastAPI()
    parent.mount("/sub", sub)

    async with LifespanManager(parent):
        assert "sub:startup" not in tracker.events

    assert tracker.events == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shared_asgi_authentication_middleware_across_boundaries(
    static_workspace: Path,
) -> None:
    """Proves ASGI middleware intercepts requests before Mount and bridges auth into WSGI."""
    root_app, _, _ = build_strangler_system(static_workspace)

    transport = ASGITransport(app=root_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Unauthenticated request to protected WSGI route is rejected by ASGI middleware
        r_unauth_wsgi = await client.get("/legacy/authenticated-resource")
        assert r_unauth_wsgi.status_code == 401
        assert r_unauth_wsgi.json()["detail"] == "Missing or invalid bearer token"

        # 2. Unauthenticated request to protected v2 sub-app route is rejected
        r_unauth_v2 = await client.get("/v2/items")
        assert r_unauth_v2.status_code == 401

        # 3. Authenticated request to v2 sub-app succeeds
        headers = {"Authorization": "Bearer valid-token-alice"}
        r_auth_v2 = await client.get("/v2/items", headers=headers)
        assert r_auth_v2.status_code == 200
        assert r_auth_v2.json()["service"] == "fastapi_v2"

        # 4. Authenticated request to legacy WSGI succeeds and receives bridged identity
        r_auth_wsgi = await client.get("/legacy/authenticated-resource", headers=headers)
        assert r_auth_wsgi.status_code == 200
        assert r_auth_wsgi.json()["authenticated_user"] == "alice"

        # 5. A client-supplied identity header is stripped, not comma-joined with the real one
        spoofed = {**headers, "X-Authenticated-User": "mallory"}
        r_spoof = await client.get("/legacy/authenticated-resource", headers=spoofed)
        assert r_spoof.status_code == 200
        assert r_spoof.json()["authenticated_user"] == "alice"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_staticfiles_caching_etag_304_and_directory_traversal_protection(
    static_workspace: Path,
) -> None:
    """Proves StaticFiles generates ETags, honors If-None-Match, and rejects traversal."""
    root_app, _, _ = build_strangler_system(static_workspace)

    transport = ASGITransport(app=root_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Fetch static asset
        r1 = await client.get("/static/style.css")
        assert r1.status_code == 200
        assert "body { background: #fafafa; }" in r1.text
        etag = r1.headers.get("etag")
        assert etag is not None
        assert "last-modified" in r1.headers

        # 2. Conditional request with matching ETag yields 304 Not Modified
        r2 = await client.get("/static/style.css", headers={"if-none-match": etag})
        assert r2.status_code == 304
        assert r2.text == ""

        # 3. httpx removes dot segments client-side, so this request reaches /secret.env
        # (the root WSGI fallback), not StaticFiles; it proves only that nothing leaks.
        r_traversal = await client.get("/static/../secret.env")
        assert r_traversal.status_code == 404

    # 4. CVE-2023-29159: a sibling directory sharing the served directory's name prefix.
    # os.path.commonprefix accepted it before Starlette 0.27.0; commonpath must reject it.
    sibling = static_workspace.parent / (static_workspace.name + "_secret")
    sibling.mkdir()
    (sibling / "keys.env").write_text("LEAKED=1", encoding="utf-8")
    static = StaticFiles(directory=str(static_workspace))
    assert static.lookup_path("../" + sibling.name + "/keys.env") == ("", None)
    assert static.lookup_path("style.css")[1] is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_dependencies_bypass_mounted_apps() -> None:
    """Proves that root FastAPI(dependencies=[...]) is completely bypassed by mounted sub-apps."""

    def require_admin() -> None:
        raise HTTPException(status_code=403, detail="Forbidden by root dependency")

    flask_app = create_legacy_flask_app()

    # Root app with global dependency guard
    guarded_app = FastAPI(dependencies=[Depends(require_admin)])

    @guarded_app.get("/native-secret")
    def native_secret() -> dict[str, str]:
        return {"secret": "revealed"}

    guarded_app.mount("/legacy", WSGIMiddleware(flask_app))

    transport = ASGITransport(app=guarded_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Native route is blocked by Depends
        r_native = await client.get("/native-secret")
        assert r_native.status_code == 403

        # Mounted WSGI route BYPASSES the dependency and executes
        r_legacy = await client.get("/legacy/items")
        assert r_legacy.status_code == 200
        assert r_legacy.json()["service"] == "legacy_flask"
