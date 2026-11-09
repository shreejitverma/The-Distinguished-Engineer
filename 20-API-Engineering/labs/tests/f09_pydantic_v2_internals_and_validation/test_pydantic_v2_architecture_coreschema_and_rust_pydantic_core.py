"""Unit tests for Pydantic v2 CoreSchema compilation and Rust SchemaValidator execution."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel, PydanticUserError, ValidationError

from api_labs.f09_pydantic_v2_internals_and_validation.core_schema_compiler import (
    MetricSample,
    MultiRefBatch,
    SingleRefBatch,
    build_telemetry_app,
    inspect_model_compilation,
    rebuild_without_prebuilt,
    verify_deferred_lifecycle,
)


@pytest.mark.unit
def test_leaf_and_single_ref_schema_inlining() -> None:
    leaf_report = inspect_model_compilation(MetricSample)
    assert leaf_report.is_complete is True
    assert leaf_report.root_schema_type == "model"
    assert leaf_report.inner_schema_type == "model-fields"
    assert leaf_report.field_schema_types == {
        "metric": "str",
        "value": "float",
        "unit": "default",
    }
    assert leaf_report.definitions_count == 0
    assert leaf_report.uses_prebuilt_validator is False
    assert leaf_report.adapter_shares_validator is True
    assert leaf_report.validator_title == "MetricSample"

    single_report = inspect_model_compilation(SingleRefBatch)
    assert single_report.is_complete is True
    assert single_report.root_schema_type == "model"
    assert single_report.definitions_count == 0
    assert single_report.field_schema_types == {
        "batch_id": "int",
        "primary": "model",
    }
    assert single_report.uses_prebuilt_validator is True
    assert single_report.adapter_shares_validator is True


@pytest.mark.unit
def test_multi_ref_schema_preserves_definitions_and_rebuilds_without_prebuilt() -> None:
    multi_report = inspect_model_compilation(MultiRefBatch)
    assert multi_report.root_schema_type == "definitions"
    assert multi_report.definitions_count == 1
    assert multi_report.field_schema_types == {
        "batch_id": "int",
        "primary": "definition-ref",
        "baseline": "definition-ref",
    }
    assert multi_report.uses_prebuilt_validator is True

    class LocalSample(BaseModel):
        code: str

    class DualBatch(BaseModel):
        batch_id: int
        first: LocalSample
        second: LocalSample

    initial = inspect_model_compilation(DualBatch)
    assert initial.uses_prebuilt_validator is True

    rebuilt = rebuild_without_prebuilt(DualBatch)
    assert rebuilt.root_schema_type == "definitions"
    assert rebuilt.definitions_count == 1
    assert rebuilt.uses_prebuilt_validator is False


@pytest.mark.unit
def test_defer_build_and_unresolved_forward_ref_mocks() -> None:
    complete_before, mocks_before, complete_after, compiled_after = verify_deferred_lifecycle()
    assert complete_before is False
    assert mocks_before is True
    assert complete_after is True
    assert compiled_after is True

    def make_incomplete_pair() -> tuple[type[BaseModel], type[BaseModel]]:
        class PendingEnvelope(BaseModel):
            payload: LaterPayload

        class LaterPayload(BaseModel):
            seq: int

        return PendingEnvelope, LaterPayload

    envelope_cls, payload_cls = make_incomplete_pair()
    assert envelope_cls.__pydantic_complete__ is False
    with pytest.raises(PydanticUserError) as exc_info:
        envelope_cls.model_validate({"payload": {"seq": 7}})
    assert exc_info.value.code == "class-not-fully-defined"

    assert envelope_cls.model_rebuild(_types_namespace={"LaterPayload": payload_cls}) is True
    assert envelope_cls.__pydantic_complete__ is True
    validated: Any = envelope_cls.model_validate({"payload": {"seq": 7}})
    assert validated.payload.seq == 7


@pytest.mark.unit
def test_rust_model_validator_slots_vs_model_construct() -> None:
    assert BaseModel.__slots__ == (
        "__dict__",
        "__pydantic_fields_set__",
        "__pydantic_extra__",
        "__pydantic_private__",
    )
    sample = MetricSample.model_validate_json(b'{"metric":"p99","value":"18.75"}')
    assert sample.metric == "p99"
    assert sample.value == 18.75
    assert sample.unit == "ms"
    assert sample.model_fields_set == {"metric", "value"}
    assert sample.model_extra is None

    with pytest.raises(ValidationError) as err_info:
        MetricSample.model_validate_json(b'{"metric":"","value":1.0}')
    errors = err_info.value.errors(include_url=False)
    assert errors[0]["type"] == "string_too_short"
    assert errors[0]["loc"] == ("metric",)

    unvalidated = MetricSample.model_construct(metric="", value=-1.0)
    assert unvalidated.metric == ""
    assert unvalidated.unit == "ms"
    assert unvalidated.model_fields_set == {"metric", "value"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_route_reuses_prebuilt_validator_and_formats_422() -> None:
    app = build_telemetry_app()
    routes = [r for r in app.routes if isinstance(r, APIRoute) and r.path == "/v1/batches"]
    assert len(routes) == 1
    body_field = routes[0].body_field
    assert body_field is not None
    assert body_field.field_info.annotation is MultiRefBatch
    route_adapter: Any = vars(body_field)["_type_adapter"]
    assert route_adapter.validator is not MultiRefBatch.__pydantic_validator__
    assert repr(route_adapter.validator).startswith(
        'SchemaValidator(title="MultiRefBatch", validator=Prebuilt('
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ok_resp = await client.post(
            "/v1/batches",
            json={
                "batch_id": 9,
                "primary": {"metric": "db_read", "value": 14.5},
                "baseline": {"metric": "db_read", "value": 10.25, "unit": "ms"},
            },
        )
        assert ok_resp.status_code == 200
        assert ok_resp.json() == {
            "batch_id": 9,
            "delta": 4.25,
            "primary_fields_set": ["metric", "value"],
            "validator_title": "MultiRefBatch",
        }

        bad_resp = await client.post(
            "/v1/batches",
            json={
                "batch_id": 9,
                "primary": {"metric": "", "value": 14.5},
                "baseline": {"metric": "db_read", "value": 10.25},
            },
        )
        assert bad_resp.status_code == 422
        detail = bad_resp.json()["detail"]
        assert detail[0]["type"] == "string_too_short"
        assert detail[0]["loc"] == ["body", "primary", "metric"]
