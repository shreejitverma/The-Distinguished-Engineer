"""FastAPI OpenAPI generation internals, schema splitting, operation IDs, and webhooks."""

import json
import statistics
import time
from decimal import Decimal
from enum import Enum
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, computed_field


class ItemWithDefault(BaseModel):
    """Model with default values.

    In Pydantic v2, standard defaults yield identical validation and serialization
    JSON schemas, maintaining a single unified schema in OpenAPI components.
    """

    name: str
    count: int = 1


class ProductWithDecimal(BaseModel):
    """Model with Decimal field.

    Validation schema accepts string or number (JSON union).
    Serialization schema produces string (to preserve IEEE-754 precision).
    Triggers separate -Input and -Output schemas when separate_input_output_schemas=True.
    """

    name: str
    price: Decimal


class UserWithComputed(BaseModel):
    """Model with excluded fields and computed fields.

    Validation accepts raw_password but ignores display_name.
    Serialization outputs display_name but excludes raw_password.
    Triggers separate -Input and -Output schemas.
    """

    name: str
    raw_password: str = Field(exclude=True)

    @computed_field
    def display_name(self) -> str:
        return f"User: {self.name}"


class SubscriptionEvent(BaseModel):
    """Webhook payload model describing outbound event notification."""

    event_id: str
    customer_id: str
    amount: Decimal


def create_schema_app(*, separate_input_output_schemas: bool = True) -> FastAPI:
    """Build FastAPI application configured for schema separation inspection."""
    app = FastAPI(
        title="Schema Splitting Inspection App",
        version="1.0.0",
        separate_input_output_schemas=separate_input_output_schemas,
    )

    @app.post("/items")
    def create_item(item: ItemWithDefault) -> ItemWithDefault:
        return item

    @app.post("/products")
    def create_product(product: ProductWithDecimal) -> ProductWithDecimal:
        return product

    @app.post("/users")
    def create_user(user: UserWithComputed) -> UserWithComputed:
        return user

    return app


def generate_clean_operation_id(route: APIRoute) -> str:
    """Generate clean, deterministic operation IDs for SDK generator client classes.

    Default FastAPI operation IDs format as `{name}{path}_{method}` which produces
    cluttered SDK client methods like `get_item_items__item_id__get`.
    This custom generator formats IDs as `{tag}_{name}` or `{name}`.
    """
    if route.tags:
        raw_tag = route.tags[0]
        tag_val: object = raw_tag.value if isinstance(raw_tag, Enum) else raw_tag
        clean_tag = str(tag_val).lower().replace(" ", "_").replace("-", "_")
        return f"{clean_tag}_{route.name}"
    return route.name


def create_operation_id_app(*, use_custom_ids: bool = False) -> FastAPI:
    """Build FastAPI app with either default or custom operation ID generator."""
    if use_custom_ids:
        app = FastAPI(
            title="Operation ID Inspection App",
            version="1.0.0",
            generate_unique_id_function=generate_clean_operation_id,
        )
    else:
        app = FastAPI(
            title="Operation ID Inspection App",
            version="1.0.0",
        )

    items_router = APIRouter(prefix="/items", tags=["items"])

    @items_router.get("/{item_id}")
    def get_item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    @items_router.post("/")
    def create_item(item_name: str) -> dict[str, str]:
        return {"item_name": item_name}

    app.include_router(items_router)
    return app


def create_webhook_app() -> FastAPI:
    """Build FastAPI application with registered OpenAPI 3.1 webhooks."""
    app = FastAPI(
        title="Webhook Docs App",
        version="1.0.0",
    )

    @app.post("/subscribe")
    def subscribe(target_url: str) -> dict[str, str]:
        return {"status": "subscribed", "target_url": target_url}

    @app.webhooks.post("subscription-created")
    def on_subscription_created(event: SubscriptionEvent) -> None:
        """Invoked by our billing system when a new subscription is activated."""

    return app


def install_cached_custom_openapi(
    app: FastAPI,
    *,
    internal_prefix: str = "/internal/",
    api_title: str = "Enterprise Gateway",
) -> None:
    """Install custom schema generator on app.openapi with defensive caching.

    Filters internal endpoints from public specification and injects enterprise
    custom extensions and security headers.
    """

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema

        schema = get_openapi(
            title=api_title,
            version=app.version,
            openapi_version=app.openapi_version,
            summary=app.summary,
            description=app.description,
            routes=app.routes,
            webhooks=app.webhooks.routes,
            tags=app.openapi_tags,
            servers=app.servers,
            terms_of_service=app.terms_of_service,
            contact=app.contact,
            license_info=app.license_info,
            separate_input_output_schemas=app.separate_input_output_schemas,
        )

        filtered_paths = {
            path: path_item
            for path, path_item in schema.get("paths", {}).items()
            if not path.startswith(internal_prefix)
        }
        schema["paths"] = filtered_paths

        schema["info"]["x-api-governance"] = {
            "tier": "tier-1-mission-critical",
            "managed-by": "api-platform-team",
        }

        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


class BenchLine(BaseModel):
    """Nested item used by the generation benchmark payload."""

    sku: str
    qty: int


class BenchPayload(BaseModel):
    """Benchmark body and response model with a nested list."""

    name: str
    lines: list[BenchLine]


def build_benchmark_app(route_count: int) -> FastAPI:
    """Build an app with `route_count` POST routes sharing one model."""
    app = FastAPI(title="OpenAPI Benchmark", version="1.0.0")
    for index in range(route_count):

        def endpoint(payload: BenchPayload) -> BenchPayload:
            return payload

        app.post(f"/r{index}", name=f"ep{index}")(endpoint)
    return app


def measure_openapi_generation(route_count: int, repeats: int = 30) -> tuple[float, float, int]:
    """Return (cold median ms, cached median us, schema JSON bytes) for `route_count` routes.

    Each repeat builds a fresh app, times the first `app.openapi()` call (cold),
    then times a second call that is served from `app.openapi_schema` (cached).
    """
    cold_ns: list[int] = []
    cached_ns: list[int] = []
    schema: dict[str, Any] = {}
    for _ in range(repeats):
        app = build_benchmark_app(route_count)
        start = time.perf_counter_ns()
        schema = app.openapi()
        cold_ns.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        app.openapi()
        cached_ns.append(time.perf_counter_ns() - start)
    size = len(json.dumps(schema).encode())
    return statistics.median(cold_ns) / 1e6, statistics.median(cached_ns) / 1e3, size
