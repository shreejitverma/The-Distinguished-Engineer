"""Modular application factory composing versioned routers, lifespans, and settings."""

from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseModel):
    """Database connection and pool settings (a plain model: it never reads the environment)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = "sqlite+aiosqlite:///:memory:"
    pool_size: int = 5
    max_overflow: int = 10


class AuthSettings(BaseModel):
    """Authentication and token verification settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    secret_key: str = "test-secret-key-32-chars-minimum-len"  # noqa: S105
    token_ttl_seconds: int = 3600


class AppSettings(BaseSettings):
    """Root application configuration composed of domain-level settings."""

    model_config = SettingsConfigDict(frozen=True, extra="forbid")

    environment: str = "test"
    service_name: str = "enterprise-api"
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)


@dataclass
class ExecutionLog:
    """Deterministic event recorder for router dependencies and lifespans."""

    events: list[str] = field(default_factory=list[str])

    def record(self, event: str) -> None:
        self.events.append(event)


@dataclass
class MockDatabasePool:
    """Mock database pool lifecycle target."""

    url: str
    is_closed: bool = False

    async def close(self) -> None:
        self.is_closed = True


@dataclass
class MockDomainCache:
    """Mock domain-level cache lifecycle target."""

    domain: str
    is_cleared: bool = False

    async def clear(self) -> None:
        self.is_cleared = True


class UserRead(BaseModel):
    """User representation schema."""

    user_id: str
    username: str
    source: str


class OrderRead(BaseModel):
    """Order representation schema."""

    order_id: str
    user_id: str
    amount_cents: int


def get_db(request: Request) -> MockDatabasePool:
    """Extract the database pool from request state or application state."""
    db: MockDatabasePool | None = getattr(request.state, "db_pool", None) or getattr(
        request.app.state, "db_pool", None
    )
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database pool not initialized in request state",
        )
    return db


def get_domain_cache(request: Request) -> MockDomainCache:
    """Extract the domain cache from request state or application state."""
    cache: MockDomainCache | None = getattr(request.state, "domain_cache", None) or getattr(
        request.app.state, "domain_cache", None
    )
    if cache is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Domain cache not initialized in request state",
        )
    return cache


def create_user_router(log: ExecutionLog) -> APIRouter:
    """Create the users domain router with dependencies and route ordering."""

    async def user_domain_guard() -> None:
        log.record("guard:user_domain")

    router = APIRouter(
        prefix="/users",
        tags=["users"],
        dependencies=[Depends(user_domain_guard)],
    )

    # Static path must precede dynamic parameterized path to avoid route shadowing.
    @router.get("/me")
    async def get_current_user(
        db: Annotated[MockDatabasePool, Depends(get_db)],
        cache: Annotated[MockDomainCache, Depends(get_domain_cache)],
    ) -> UserRead:
        log.record("endpoint:users_me")
        return UserRead(
            user_id="usr_current",
            username="alice",
            source=f"db={db.url},cache={cache.domain}",
        )

    @router.get("/{user_id}")
    async def get_user_by_id(
        user_id: str,
        db: Annotated[MockDatabasePool, Depends(get_db)],
    ) -> UserRead:
        log.record(f"endpoint:users_by_id:{user_id}")
        return UserRead(
            user_id=user_id,
            username=f"user_{user_id}",
            source=f"db={db.url}",
        )

    return router


def create_order_router(log: ExecutionLog) -> APIRouter:
    """Create the orders domain router with dependencies."""

    async def order_domain_guard() -> None:
        log.record("guard:order_domain")

    router = APIRouter(
        prefix="/orders",
        tags=["orders"],
        dependencies=[Depends(order_domain_guard)],
    )

    @router.get("/{order_id}")
    async def get_order_by_id(
        order_id: str,
        db: Annotated[MockDatabasePool, Depends(get_db)],
    ) -> OrderRead:
        log.record(f"endpoint:orders_by_id:{order_id}")
        return OrderRead(
            order_id=order_id,
            user_id="usr_current",
            amount_cents=4999,
        )

    return router


def create_api_v1_router(log: ExecutionLog) -> APIRouter:
    """Create top-level versioned API router composing domain routers."""

    async def api_v1_guard() -> None:
        log.record("guard:api_v1")

    v1_router = APIRouter(
        prefix="/v1",
        dependencies=[Depends(api_v1_guard)],
    )

    v1_router.include_router(create_user_router(log))
    v1_router.include_router(create_order_router(log))
    return v1_router


def create_lifespan_graph(
    settings: AppSettings,
    log: ExecutionLog,
) -> Any:
    """Compose multi-resource lifespan context using AsyncExitStack."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[dict[str, Any]]:
        # Root lifespan orchestrator managing multi-tier dependencies in LIFO order
        log.record("lifespan:enter_root")
        async with AsyncExitStack() as stack:
            # 1. Initialize infra: Database Pool
            db_pool = MockDatabasePool(url=settings.database.url)
            log.record(f"lifespan:startup_db:{settings.database.url}")
            stack.push_async_callback(db_pool.close)
            stack.callback(log.record, "lifespan:shutdown_db")

            # 2. Initialize domain cache (depends on infra)
            domain_cache = MockDomainCache(domain=settings.service_name)
            log.record(f"lifespan:startup_cache:{settings.service_name}")
            stack.push_async_callback(domain_cache.clear)
            stack.callback(log.record, "lifespan:shutdown_cache")

            # Expose initialized resources via ASGI state dict and app.state
            app.state.db_pool = db_pool
            app.state.domain_cache = domain_cache
            app.state.app_settings = settings

            yield {
                "db_pool": db_pool,
                "domain_cache": domain_cache,
                "app_settings": settings,
            }

        log.record("lifespan:exit_root")

    return lifespan


def create_app(
    settings: AppSettings | None = None,
    log: ExecutionLog | None = None,
) -> FastAPI:
    """Application factory for multi-team FastAPI service architecture."""
    active_settings = settings or AppSettings()
    active_log = log or ExecutionLog()

    app = FastAPI(
        title=active_settings.service_name,
        lifespan=create_lifespan_graph(active_settings, active_log),
    )

    # Top-level API router composition
    api_router = APIRouter(prefix="/api")
    api_router.include_router(create_api_v1_router(active_log))
    app.include_router(api_router)

    return app
