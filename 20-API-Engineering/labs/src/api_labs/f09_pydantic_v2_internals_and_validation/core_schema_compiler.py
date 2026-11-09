"""Inspect Pydantic v2 CoreSchema compilation and Rust SchemaValidator internals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic._internal._mock_val_ser import MockCoreSchema, MockValSer
from pydantic_core import SchemaSerializer, SchemaValidator


class MetricSample(BaseModel):
    """Leaf model compiled into a reusable Rust SchemaValidator."""

    metric: str = Field(min_length=1)
    value: float
    unit: str = "ms"


class SingleRefBatch(BaseModel):
    """Parent model referencing MetricSample once (inlined in CoreSchema)."""

    batch_id: int
    primary: MetricSample


class MultiRefBatch(BaseModel):
    """Parent model referencing MetricSample twice (shared via definitions)."""

    batch_id: int
    primary: MetricSample
    baseline: MetricSample


@dataclass(frozen=True, slots=True)
class SchemaCompilationReport:
    """Snapshot of a BaseModel class's compiled CoreSchema and Rust validator graph."""

    model_name: str
    is_complete: bool
    root_schema_type: str
    inner_schema_type: str
    field_schema_types: dict[str, str]
    definitions_count: int
    uses_prebuilt_validator: bool
    adapter_shares_validator: bool
    validator_title: str


def inspect_model_compilation(model_cls: type[BaseModel]) -> SchemaCompilationReport:
    """Extract CoreSchema structure and SchemaValidator metadata from a completed BaseModel."""
    raw_schema = cast("dict[str, Any]", model_cls.__pydantic_core_schema__)
    root_type = str(raw_schema["type"])
    if root_type == "definitions":
        definitions = cast("list[dict[str, Any]]", raw_schema["definitions"])
        definitions_count = len(definitions)
        model_schema = cast("dict[str, Any]", raw_schema["schema"])
    else:
        definitions_count = 0
        model_schema = raw_schema

    fields_schema = cast("dict[str, Any]", model_schema["schema"])
    raw_fields = cast("dict[str, dict[str, Any]]", fields_schema["fields"])
    field_schema_types = {
        name: str(cast("dict[str, Any]", field_def["schema"])["type"])
        for name, field_def in raw_fields.items()
    }

    validator = model_cls.__pydantic_validator__
    validator_repr = repr(validator)
    adapter = TypeAdapter(model_cls)
    return SchemaCompilationReport(
        model_name=model_cls.__name__,
        is_complete=model_cls.__pydantic_complete__,
        root_schema_type=root_type,
        inner_schema_type=str(fields_schema["type"]),
        field_schema_types=field_schema_types,
        definitions_count=definitions_count,
        uses_prebuilt_validator="PrebuiltValidator" in validator_repr,
        adapter_shares_validator=adapter.validator is validator,
        validator_title=validator.title,
    )


def rebuild_without_prebuilt(model_cls: type[BaseModel]) -> SchemaCompilationReport:
    """Force-rebuild a model with _use_prebuilt=False and return its updated report."""
    model_cls.model_rebuild(force=True)
    return inspect_model_compilation(model_cls)


def verify_deferred_lifecycle() -> tuple[bool, bool, bool, bool]:
    """Return (complete_before, mocks_before, complete_after, compiled_after) for defer_build."""

    class DeferredEnvelope(BaseModel):
        model_config = ConfigDict(defer_build=True)
        envelope_id: str
        sample: MetricSample

    complete_before = DeferredEnvelope.__pydantic_complete__
    mocks_before = (
        isinstance(DeferredEnvelope.__dict__["__pydantic_core_schema__"], MockCoreSchema)
        and isinstance(DeferredEnvelope.__dict__["__pydantic_validator__"], MockValSer)
        and isinstance(DeferredEnvelope.__dict__["__pydantic_serializer__"], MockValSer)
    )

    instance = DeferredEnvelope(
        envelope_id="env-1",
        sample=MetricSample(metric="latency", value=12.5),
    )
    complete_after = DeferredEnvelope.__pydantic_complete__ and instance.sample.value == 12.5
    compiled_after = isinstance(
        DeferredEnvelope.__dict__["__pydantic_validator__"], SchemaValidator
    ) and isinstance(DeferredEnvelope.__dict__["__pydantic_serializer__"], SchemaSerializer)
    return complete_before, mocks_before, complete_after, compiled_after


def build_telemetry_app() -> FastAPI:
    """Create an ASGI app whose route ModelField reuses MultiRefBatch's SchemaValidator."""
    app = FastAPI()

    @app.post("/v1/batches")
    async def ingest_batch(batch: MultiRefBatch) -> dict[str, Any]:
        return {
            "batch_id": batch.batch_id,
            "delta": round(batch.primary.value - batch.baseline.value, 4),
            "primary_fields_set": sorted(batch.primary.model_fields_set),
            "validator_title": MultiRefBatch.__pydantic_validator__.title,
        }

    return app
