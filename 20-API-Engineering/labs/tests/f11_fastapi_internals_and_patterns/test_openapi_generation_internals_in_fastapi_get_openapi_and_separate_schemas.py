"""Tests for OpenAPI generation internals, schema splitting, operation IDs, and webhooks."""

import warnings

import pytest

from api_labs.f11_fastapi_internals_and_patterns.openapi_generation_internals import (
    create_operation_id_app,
    create_schema_app,
    create_webhook_app,
    install_cached_custom_openapi,
    measure_openapi_generation,
)


@pytest.mark.unit
def test_separate_schemas_enabled_splits_divergent_models_only() -> None:
    app = create_schema_app(separate_input_output_schemas=True)
    schema = app.openapi()
    schemas = schema["components"]["schemas"]

    assert "ProductWithDecimal-Input" in schemas
    assert "ProductWithDecimal-Output" in schemas
    assert "UserWithComputed-Input" in schemas
    assert "UserWithComputed-Output" in schemas

    # Models whose validation and serialization schemas are identical remain unified
    assert "ItemWithDefault" in schemas
    assert "ItemWithDefault-Input" not in schemas
    assert "ItemWithDefault-Output" not in schemas

    product_path = schema["paths"]["/products"]["post"]
    req_ref = product_path["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    res_ref = product_path["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]

    assert req_ref == "#/components/schemas/ProductWithDecimal-Input"
    assert res_ref == "#/components/schemas/ProductWithDecimal-Output"


@pytest.mark.unit
def test_separate_schemas_disabled_collapses_models_without_computed_fields() -> None:
    app = create_schema_app(separate_input_output_schemas=False)
    schema = app.openapi()
    schemas = schema["components"]["schemas"]

    # Decimal model collapses into single unified schema using validation definition
    assert "ProductWithDecimal" in schemas
    assert "ProductWithDecimal-Input" not in schemas
    assert "ProductWithDecimal-Output" not in schemas

    # Computed fields explicitly bypass separate_input_output_schemas=False in FastAPI._compat
    assert "UserWithComputed-Input" in schemas
    assert "UserWithComputed-Output" in schemas


@pytest.mark.unit
def test_operation_id_default_vs_custom_naming_convention() -> None:
    default_app = create_operation_id_app(use_custom_ids=False)
    default_schema = default_app.openapi()
    default_op_id = default_schema["paths"]["/items/{item_id}"]["get"]["operationId"]
    assert default_op_id == "get_item_items__item_id__get"

    custom_app = create_operation_id_app(use_custom_ids=True)
    custom_schema = custom_app.openapi()
    custom_op_id = custom_schema["paths"]["/items/{item_id}"]["get"]["operationId"]
    assert custom_op_id == "items_get_item"


@pytest.mark.unit
def test_duplicate_operation_id_detection_warning() -> None:
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/first", operation_id="conflict_id")
    def first_endpoint() -> dict[str, str]:
        return {"msg": "first"}

    @app.get("/second", operation_id="conflict_id")
    def second_endpoint() -> dict[str, str]:
        return {"msg": "second"}

    with warnings.catch_warnings(record=True) as captured_warnings:
        warnings.simplefilter("always")
        schema = app.openapi()

    assert schema["paths"]["/first"]["get"]["operationId"] == "conflict_id"
    assert schema["paths"]["/second"]["get"]["operationId"] == "conflict_id"

    duplicate_warnings = [
        w for w in captured_warnings if "Duplicate Operation ID conflict_id" in str(w.message)
    ]
    assert len(duplicate_warnings) == 1


@pytest.mark.unit
def test_openapi_31_webhooks_mapping() -> None:
    app = create_webhook_app()
    schema = app.openapi()

    assert schema["openapi"] == "3.1.0"
    assert "webhooks" in schema
    assert "subscription-created" in schema["webhooks"]

    webhook_post = schema["webhooks"]["subscription-created"]["post"]
    assert webhook_post["summary"] == "On Subscription Created"
    req_body_ref = webhook_post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert "SubscriptionEvent" in req_body_ref


@pytest.mark.unit
def test_openapi_caching_and_customization_interception() -> None:
    from fastapi import FastAPI

    app = FastAPI(version="2.5.0")

    @app.get("/public/users")
    def list_users() -> list[str]:
        return ["alice", "bob"]

    @app.get("/internal/health")
    def health_check() -> dict[str, str]:
        return {"status": "ok"}

    install_cached_custom_openapi(app, internal_prefix="/internal/", api_title="Enterprise API")

    schema_1 = app.openapi()
    assert schema_1["info"]["title"] == "Enterprise API"
    assert schema_1["info"]["x-api-governance"]["tier"] == "tier-1-mission-critical"

    # Public route preserved, internal route stripped
    assert "/public/users" in schema_1["paths"]
    assert "/internal/health" not in schema_1["paths"]

    # Verify memoization caching
    schema_2 = app.openapi()
    assert schema_1 is schema_2


@pytest.mark.unit
def test_webhook_added_after_cache_is_invisible_until_routes_change() -> None:
    app = create_webhook_app()
    first = app.openapi()

    @app.webhooks.post("subscription-cancelled")
    def on_cancelled() -> None:
        """Registered after the schema was cached."""

    # app.webhooks is a separate APIRouter; it does not bump app.router's version.
    assert app.openapi() is first
    assert "subscription-cancelled" not in app.openapi()["webhooks"]

    @app.get("/late")
    def late() -> dict[str, str]:
        return {"ok": "yes"}

    # A main-router change invalidates the cache and the late webhook appears.
    assert "subscription-cancelled" in app.openapi()["webhooks"]


@pytest.mark.unit
def test_measure_openapi_generation_reports_size_growth() -> None:
    _, _, small = measure_openapi_generation(2, repeats=1)
    _, _, large = measure_openapi_generation(8, repeats=1)
    assert large > small > 0


@pytest.mark.unit
def test_multi_method_route_reuses_one_operation_id_for_every_method() -> None:
    from fastapi import FastAPI

    app = FastAPI()

    @app.api_route("/multi", methods=["GET", "POST"])
    def multi() -> dict[str, str]:
        return {"ok": "yes"}

    with warnings.catch_warnings(record=True) as captured_warnings:
        warnings.simplefilter("always")
        schema = app.openapi()

    op_ids = {op["operationId"] for op in schema["paths"]["/multi"].values()}
    # unique_id is computed once per route from list(route.methods)[0], a set whose
    # order depends on PYTHONHASHSEED, so both methods share one id.
    assert len(op_ids) == 1
    assert op_ids <= {"multi_multi_get", "multi_multi_post"}
    assert any("Duplicate Operation ID" in str(w.message) for w in captured_warnings)


@pytest.mark.unit
def test_nested_computed_field_splits_child_even_when_separation_disabled() -> None:
    from fastapi import FastAPI
    from pydantic import BaseModel, computed_field

    class Child(BaseModel):
        x: int

        @computed_field
        def doubled(self) -> int:
            return self.x * 2

    class Parent(BaseModel):
        child: Child

    app = FastAPI(separate_input_output_schemas=False)

    @app.post("/parents")
    def create_parent(parent: Parent) -> Parent:
        return parent

    schemas = app.openapi()["components"]["schemas"]
    # _has_computed_fields inspects only the top-level field, so Parent stays unified,
    # while the flattened Child model field keeps serialization mode and splits.
    assert "Parent" in schemas
    assert "Child-Input" in schemas
    assert "Child-Output" in schemas
