"""Mode-aware JSON Schema generation and GenerateJsonSchema subclasses (pydantic 2.14.3)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal  # noqa: TC003 - pydantic resolves field annotations at runtime
from typing import TYPE_CHECKING, Annotated, Any, Protocol, cast, override

from fastapi import FastAPI
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, computed_field
from pydantic.json_schema import (
    GenerateJsonSchema,
    JsonSchemaMode,
    JsonSchemaValue,
    JsonSchemaWarningKind,
    models_json_schema,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_core import CoreSchema, core_schema

COMPONENTS_REF_TEMPLATE = "#/components/schemas/{model}"


class Fill(BaseModel):
    """Identical in both modes, so it keeps one unsuffixed definition."""

    venue: str
    qty: int


class Order(BaseModel):
    """Diverges between modes: aliases, a serializer-rewritten default, a computed field."""

    order_id: int = Field(validation_alias="orderId", serialization_alias="order_ref")
    price: Decimal = Field(max_digits=12, decimal_places=4)
    qty: int = 1
    placed_ms: Annotated[int, PlainSerializer(str, return_type=str)] = 0
    fill: Fill | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def notional(self) -> Decimal:
        """Server-derived, so it appears only in the serialization schema."""
        return self.price * self.qty


class PinnedOrder(Order):
    """Forces one schema for both modes, whatever mode the caller asks for."""

    model_config = ConfigDict(json_schema_mode_override="validation")


class OutputDefaultsRequired(BaseModel):
    """Marks defaulted fields required in serialization mode (they are always dumped)."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    qty: int = 1


class SchemaContractError(RuntimeError):
    """Raised when a contract generator refuses to emit a lossy schema."""


class ContractGenerateJsonSchema(GenerateJsonSchema):
    """Declares the dialect, fails closed on dropped defaults, and pins decimal syntax."""

    @override
    def generate(self, schema: CoreSchema, mode: JsonSchemaMode = "validation") -> JsonSchemaValue:
        json_schema = super().generate(schema, mode=mode)
        json_schema["$schema"] = self.schema_dialect
        return json_schema

    @override
    def emit_warning(self, kind: JsonSchemaWarningKind, detail: str) -> None:
        if kind == "non-serializable-default":
            raise SchemaContractError(f"{detail} [{kind}]")
        super().emit_warning(kind, detail)

    @override
    def get_decimal_pattern(self, schema: core_schema.DecimalSchema) -> str | None:
        return self.build_decimal_pattern(schema)


class OpenAPI30GenerateJsonSchema(GenerateJsonSchema):
    """Emits OpenAPI 3.0 `nullable` instead of a JSON Schema `{"type": "null"}` branch."""

    @override
    def nullable_schema(self, schema: core_schema.NullableSchema) -> JsonSchemaValue:
        inner = self.generate_inner(schema["schema"])
        if "$ref" in inner:
            # OpenAPI 3.0 ignores siblings of a Reference Object, so wrap the $ref.
            return {"allOf": [inner], "nullable": True}
        return {**inner, "nullable": True}


@dataclass(frozen=True, slots=True)
class SchemaPair:
    """The two JSON Schemas pydantic derives from one model."""

    validation: JsonSchemaValue
    serialization: JsonSchemaValue

    def property_names(self, mode: JsonSchemaMode) -> tuple[str, ...]:
        schema = self.validation if mode == "validation" else self.serialization
        return tuple(schema["properties"])

    def required(self, mode: JsonSchemaMode) -> tuple[str, ...]:
        schema = self.validation if mode == "validation" else self.serialization
        return tuple(schema.get("required", ()))


def schema_pair(
    model: type[BaseModel], generator: type[GenerateJsonSchema] = GenerateJsonSchema
) -> SchemaPair:
    """Generate the validation and serialization schemas with fresh generator instances."""
    return SchemaPair(
        validation=model.model_json_schema(mode="validation", schema_generator=generator),
        serialization=model.model_json_schema(mode="serialization", schema_generator=generator),
    )


class _Validator(Protocol):
    def iter_errors(self, instance: Any) -> Any: ...


def contract_errors(schema: JsonSchemaValue, instance: Any) -> list[str]:
    """Validate `instance` against a draft 2020-12 schema; return sorted error messages."""
    validator = cast("_Validator", Draft202012Validator(schema))
    return sorted(str(error.message) for error in validator.iter_errors(instance))


def component_schemas(
    models: Sequence[type[BaseModel]],
    generator: type[GenerateJsonSchema] = GenerateJsonSchema,
) -> tuple[dict[str, str], dict[str, JsonSchemaValue]]:
    """Build OpenAPI-style components for each model in both modes from one generator.

    Returns `{"<Model>:<mode>": "$ref"}` and the shared definitions, keyed by component name.
    """
    inputs: list[tuple[type[BaseModel], JsonSchemaMode]] = [
        (model, mode) for model in models for mode in ("validation", "serialization")
    ]
    refs, top = models_json_schema(
        inputs, ref_template=COMPONENTS_REF_TEMPLATE, schema_generator=generator
    )
    ref_map = {
        f"{model.__name__}:{mode}": str(schema["$ref"]) for (model, mode), schema in refs.items()
    }
    return ref_map, dict(top.get("$defs", {}))


def dangling_refs(schema: JsonSchemaValue) -> set[str]:
    """Return local `$ref` targets that do not resolve inside `schema` itself."""
    refs: set[str] = set()
    stack: list[Any] = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():  # pyright: ignore[reportUnknownVariableType]
                if key == "$ref" and isinstance(value, str):
                    refs.add(value)
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)  # pyright: ignore[reportUnknownArgumentType]
    unresolved: set[str] = set()
    for ref in refs:
        target: object = schema
        for part in ref.removeprefix("#/").split("/"):
            target = (
                cast("dict[str, object]", target).get(part) if isinstance(target, dict) else None
            )
        if target is None:
            unresolved.add(ref)
    return unresolved


def build_order_app() -> FastAPI:
    """One route whose request and response share `Order`, so OpenAPI needs both modes."""
    app = FastAPI()

    @app.post("/v1/orders")
    async def create_order(order: Order) -> Order:  # pyright: ignore[reportUnusedFunction]
        return order

    return app
