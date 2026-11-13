"""Tagged-union dispatch in Pydantic v2: str discriminators, Tag(), and callable Discriminator()."""

from __future__ import annotations

import functools
import json
import platform
import statistics
import sys
import timeit
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union, cast

from fastapi import FastAPI
from pydantic import (
    BaseModel,
    Discriminator,
    Field,
    Tag,
    TypeAdapter,
    ValidationError,
    create_model,
    model_validator,
)

# --- String discriminator: one shared Literal field, inferred tag -> variant map ---------------


class CardCharged(BaseModel):
    kind: Literal["card.charged"]
    amount_cents: int
    currency: str


class CardRefunded(BaseModel):
    kind: Literal["card.refunded"]
    amount_cents: int
    reason: str


class PayoutSent(BaseModel):
    kind: Literal["payout.sent", "payout.sent.v2"]
    payout_id: str


Event = Annotated[CardCharged | CardRefunded | PayoutSent, Field(discriminator="kind")]
UntaggedEvent = CardCharged | CardRefunded | PayoutSent

EVENT_ADAPTER: TypeAdapter[CardCharged | CardRefunded | PayoutSent] = TypeAdapter(Event)
UNTAGGED_EVENT_ADAPTER: TypeAdapter[CardCharged | CardRefunded | PayoutSent] = TypeAdapter(
    UntaggedEvent
)

# --- Callable discriminator: route by structural shape, no shared field --------------------


class Circle(BaseModel):
    radius: float


class Rect(BaseModel):
    width: float
    height: float


class Polygon(BaseModel):
    points: list[tuple[float, float]] = Field(min_length=3)


def shape_tag(value: Any) -> str | None:
    """Return the tag for a dict (validation) or a model instance (serialization); never raise."""
    if isinstance(value, dict):
        keys = cast("dict[str, Any]", value).keys()
        if "radius" in keys:
            return "circle"
        if "width" in keys and "height" in keys:
            return "rect"
        if "points" in keys:
            return "polygon"
        return None
    if isinstance(value, Circle):
        return "circle"
    if isinstance(value, Rect):
        return "rect"
    if isinstance(value, Polygon):
        return "polygon"
    return None


Shape = Annotated[
    Annotated[Circle, Tag("circle")]
    | Annotated[Rect, Tag("rect")]
    | Annotated[Polygon, Tag("polygon")],
    Discriminator(shape_tag),
]
SHAPE_ADAPTER: TypeAdapter[Circle | Rect | Polygon] = TypeAdapter(Shape)


def fragile_shape_tag(value: Any) -> str:
    """Anti-pattern: assumes a dict with a 'type' key, so it raises on anything else."""
    return cast("str", value["type"])


FragileShape = Annotated[
    Annotated[Circle, Tag("circle")] | Annotated[Rect, Tag("rect")],
    Discriminator(fragile_shape_tag),
]
FRAGILE_SHAPE_ADAPTER: TypeAdapter[Circle | Rect] = TypeAdapter(FragileShape)

# --- Dispatch cost: count how many variants each union strategy actually attempts ------------


@dataclass(frozen=True, slots=True)
class VariantFamily:
    """K generated variants behind three union strategies sharing one attempt log."""

    size: int
    smart: TypeAdapter[Any]
    left_to_right: TypeAdapter[Any]
    tagged: TypeAdapter[Any]
    attempts: list[str]

    def payload(self, index: int) -> dict[str, object]:
        return {"kind": f"v{index}", "a": index}

    def count_attempts(self, adapter: TypeAdapter[Any], payload: dict[str, object]) -> int:
        """Validate once and return how many variant validators ran."""
        self.attempts.clear()
        adapter.validate_python(payload)
        return len(self.attempts)


def build_variant_family(size: int) -> VariantFamily:
    """Build `size` models differing only in their `kind` literal; each logs its attempts."""
    attempts: list[str] = []

    class CountingVariant(BaseModel):
        a: int

        @model_validator(mode="before")
        @classmethod
        def _log_attempt(cls, data: Any) -> Any:
            attempts.append(cls.__name__)
            return data

    # Runtime-built type forms: typed as Any because static checkers cannot follow them.
    literal: Any = Literal
    union_form: Any = Union
    annotated: Any = Annotated
    kind_types: list[Any] = [literal[f"v{i}"] for i in range(size)]
    variants: list[type[CountingVariant]] = [
        create_model(f"Variant{i}", __base__=CountingVariant, kind=(kind_type, ...))
        for i, kind_type in enumerate(kind_types)
    ]
    union: Any = union_form[tuple(variants)]
    return VariantFamily(
        size=size,
        smart=TypeAdapter(union),
        left_to_right=TypeAdapter(annotated[union, Field(union_mode="left_to_right")]),
        tagged=TypeAdapter(annotated[union, Field(discriminator="kind")]),
        attempts=attempts,
    )


# --- CoreSchema inspection -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnionSchemaReport:
    schema_type: str
    discriminator: object
    tags: tuple[object, ...]


def inspect_union_schema(adapter: TypeAdapter[Any]) -> UnionSchemaReport:
    """Unwrap a `definitions` wrapper and report the union node that pydantic-core will build."""
    schema = cast("dict[str, Any]", adapter.core_schema)
    if schema["type"] == "definitions":
        schema = cast("dict[str, Any]", schema["schema"])
    if schema["type"] == "tagged-union":
        choices = cast("dict[object, Any]", schema["choices"])
        return UnionSchemaReport("tagged-union", schema["discriminator"], tuple(choices))
    return UnionSchemaReport(str(schema["type"]), None, ())


# --- FastAPI boundary: hardened versus fragile callable discriminator -------------------------


def build_shape_app() -> FastAPI:
    app = FastAPI()

    @app.post("/shapes")
    async def create_shape(  # pyright: ignore[reportUnusedFunction]
        shape: Shape,
    ) -> dict[str, str]:
        return {"accepted": type(shape).__name__}

    @app.post("/fragile-shapes")
    async def create_fragile_shape(  # pyright: ignore[reportUnusedFunction]
        shape: FragileShape,
    ) -> dict[str, str]:
        return {"accepted": type(shape).__name__}

    return app


# --- Wall-clock benchmark (run as a module; tests assert only the deterministic columns) ------


@dataclass(frozen=True, slots=True)
class DispatchTiming:
    size: int
    smart_ns: float
    left_to_right_last_ns: float
    tagged_ns: float
    callable_ns: float
    smart_error_count: int
    tagged_error_count: int


def _plain_variants(size: int) -> list[type[BaseModel]]:
    literal: Any = Literal
    kind_types: list[Any] = [literal[f"v{i}"] for i in range(size)]
    return [
        create_model(f"Plain{i}", kind=(kind_type, ...), a=(int, ...), b=(str, ...), c=(float, ...))
        for i, kind_type in enumerate(kind_types)
    ]


def _attribute_or_key(value: Any) -> Any:
    if isinstance(value, dict):
        return cast("dict[str, Any]", value).get("kind")
    return getattr(value, "kind", None)


def _median_ns(adapter: TypeAdapter[Any], payload: object, number: int, repeat: int) -> float:
    runs = timeit.repeat(lambda: adapter.validate_python(payload), number=number, repeat=repeat)
    return statistics.median(runs) / number * 1e9


def _error_count(adapter: TypeAdapter[Any], payload: object) -> int:
    try:
        adapter.validate_python(payload)
    except ValidationError as exc:
        return exc.error_count()
    return 0


def benchmark_dispatch(
    sizes: tuple[int, ...] = (2, 4, 8, 16, 32), number: int = 2000, repeat: int = 9
) -> list[DispatchTiming]:
    """Median ns per validate_python call for a payload that matches the LAST of K variants."""
    union_form: Any = Union
    annotated: Any = Annotated
    rows: list[DispatchTiming] = []
    for size in sizes:
        variants = _plain_variants(size)
        union: Any = union_form[tuple(variants)]
        tagged_choices: Any = union_form[
            tuple(annotated[v, Tag(f"v{i}")] for i, v in enumerate(variants))
        ]
        smart: TypeAdapter[Any] = TypeAdapter(union)
        left_to_right: TypeAdapter[Any] = TypeAdapter(
            annotated[union, Field(union_mode="left_to_right")]
        )
        tagged: TypeAdapter[Any] = TypeAdapter(annotated[union, Field(discriminator="kind")])
        by_callable: TypeAdapter[Any] = TypeAdapter(
            annotated[tagged_choices, Discriminator(_attribute_or_key)]
        )
        good = {"kind": f"v{size - 1}", "a": 1, "b": "x", "c": 1.5}
        bad = {**good, "a": "not-an-int"}
        rows.append(
            DispatchTiming(
                size=size,
                smart_ns=_median_ns(smart, good, number, repeat),
                left_to_right_last_ns=_median_ns(left_to_right, good, number, repeat),
                tagged_ns=_median_ns(tagged, good, number, repeat),
                callable_ns=_median_ns(by_callable, good, number, repeat),
                smart_error_count=_error_count(smart, bad),
                tagged_error_count=_error_count(tagged, bad),
            )
        )
    return rows


def benchmark_json_list(items: int = 1000, number: int = 50, repeat: int = 9) -> dict[str, float]:
    """Median us to validate a JSON list of `items` events: str vs callable discriminator."""
    union_form: Any = Union
    annotated: Any = Annotated
    list_form: Any = list
    variants = _plain_variants(4)
    union: Any = union_form[tuple(variants)]
    tagged_choices: Any = union_form[
        tuple(annotated[v, Tag(f"v{i}")] for i, v in enumerate(variants))
    ]
    by_str: TypeAdapter[list[Any]] = TypeAdapter(
        list_form[annotated[union, Field(discriminator="kind")]]
    )
    by_callable: TypeAdapter[list[Any]] = TypeAdapter(
        list_form[annotated[tagged_choices, Discriminator(_attribute_or_key)]]
    )
    doc = json.dumps(
        [{"kind": f"v{i % 4}", "a": i, "b": "x" * 20, "c": 1.5} for i in range(items)]
    ).encode()
    data = json.loads(doc)
    result: dict[str, float] = {}
    for label, adapter in (("str", by_str), ("callable", by_callable)):
        json_runs = timeit.repeat(
            functools.partial(adapter.validate_json, doc), number=number, repeat=repeat
        )
        result[f"{label}_json_us"] = statistics.median(json_runs) / number * 1e6
        result[f"{label}_python_us"] = _median_ns(adapter, data, number, repeat) / 1e3
    return result


if __name__ == "__main__":
    print(f"python {sys.version.split()[0]} {platform.platform()}")
    print("K smart_ns l2r_last_ns tagged_ns callable_ns smart_errors tagged_errors")
    for row in benchmark_dispatch():
        print(
            row.size,
            round(row.smart_ns),
            round(row.left_to_right_last_ns),
            round(row.tagged_ns),
            round(row.callable_ns),
            row.smart_error_count,
            row.tagged_error_count,
        )
    print({k: round(v) for k, v in benchmark_json_list().items()})
