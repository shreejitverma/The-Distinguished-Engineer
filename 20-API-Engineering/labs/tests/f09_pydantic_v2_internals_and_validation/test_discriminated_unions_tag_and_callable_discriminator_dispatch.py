"""Unit tests for tagged-union dispatch: str discriminators, Tag(), and callable Discriminator()."""

from __future__ import annotations

import functools
import warnings
from typing import Annotated, Any, Literal, cast

import httpx
import pytest
from pydantic import (
    BaseModel,
    Discriminator,
    Field,
    PydanticUserError,
    Tag,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from pydantic_core import SchemaError

from api_labs.f09_pydantic_v2_internals_and_validation.discriminated_union_dispatch import (
    EVENT_ADAPTER,
    FRAGILE_SHAPE_ADAPTER,
    SHAPE_ADAPTER,
    UNTAGGED_EVENT_ADAPTER,
    CardCharged,
    CardRefunded,
    Circle,
    PayoutSent,
    Polygon,
    Rect,
    benchmark_dispatch,
    benchmark_json_list,
    build_shape_app,
    build_variant_family,
    inspect_union_schema,
    shape_tag,
)


@pytest.mark.unit
def test_string_discriminator_compiles_to_tagged_union_with_every_literal_as_a_tag() -> None:
    report = inspect_union_schema(EVENT_ADAPTER)
    assert report.schema_type == "tagged-union"
    assert report.discriminator == "kind"
    assert report.tags == ("card.charged", "card.refunded", "payout.sent", "payout.sent.v2")
    assert inspect_union_schema(UNTAGGED_EVENT_ADAPTER).schema_type == "union"
    payout = EVENT_ADAPTER.validate_python({"kind": "payout.sent.v2", "payout_id": "p1"})
    assert isinstance(payout, PayoutSent)


@pytest.mark.unit
@pytest.mark.parametrize("size", [2, 8, 32])
def test_tagged_union_runs_one_variant_while_smart_union_runs_all_k(size: int) -> None:
    family = build_variant_family(size)
    first, last = family.payload(0), family.payload(size - 1)
    assert family.count_attempts(family.tagged, first) == 1
    assert family.count_attempts(family.tagged, last) == 1
    assert family.count_attempts(family.smart, first) == size
    assert family.count_attempts(family.smart, last) == size
    assert family.count_attempts(family.left_to_right, first) == 1
    assert family.count_attempts(family.left_to_right, last) == size


@pytest.mark.unit
def test_tagged_union_reports_one_error_under_the_tag_instead_of_one_per_variant() -> None:
    bad = {"kind": "card.refunded", "amount_cents": "ten", "reason": "dup"}
    with pytest.raises(ValidationError) as tagged:
        EVENT_ADAPTER.validate_python(bad)
    assert [(e["type"], e["loc"]) for e in tagged.value.errors()] == [
        ("int_parsing", ("card.refunded", "amount_cents"))
    ]
    with pytest.raises(ValidationError) as untagged:
        UNTAGGED_EVENT_ADAPTER.validate_python(bad)
    assert [(e["type"], e["loc"]) for e in untagged.value.errors()] == [
        ("literal_error", ("CardCharged", "kind")),
        ("int_parsing", ("CardCharged", "amount_cents")),
        ("missing", ("CardCharged", "currency")),
        ("int_parsing", ("CardRefunded", "amount_cents")),
        ("literal_error", ("PayoutSent", "kind")),
        ("missing", ("PayoutSent", "payout_id")),
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload", "error_type", "message"),
    [
        (
            {"kind": "card.disputed"},
            "union_tag_invalid",
            "Input tag 'card.disputed' found using 'kind' does not match any of the expected "
            "tags: 'card.charged', 'card.refunded', 'payout.sent', 'payout.sent.v2'",
        ),
        (
            {"amount_cents": 1},
            "union_tag_not_found",
            "Unable to extract tag using discriminator 'kind'",
        ),
        (
            ["kind", "card.charged"],
            "model_attributes_type",
            "Input should be a valid dictionary or object to extract fields from",
        ),
    ],
)
def test_tag_lookup_failures_have_distinct_error_types(
    payload: object, error_type: str, message: str
) -> None:
    with pytest.raises(ValidationError) as exc:
        EVENT_ADAPTER.validate_python(payload)
    [error] = exc.value.errors()
    assert (error["type"], error["loc"], error["msg"]) == (error_type, (), message)


@pytest.mark.unit
def test_json_duplicate_discriminator_key_is_last_wins_for_tag_and_fields() -> None:
    doc = '{"kind": "card.charged", "amount_cents": 5, "currency": "usd", "kind": "payout.sent",'
    doc += ' "payout_id": "p9"}'
    assert EVENT_ADAPTER.validate_json(doc) == PayoutSent(kind="payout.sent", payout_id="p9")


@pytest.mark.unit
def test_alias_discriminator_reads_attribute_name_then_alias() -> None:
    class Up(BaseModel):
        state: Literal["up"] = Field(alias="type")

    class Down(BaseModel):
        state: Literal["down"] = Field(alias="type")

    adapter: TypeAdapter[Up | Down] = TypeAdapter(
        Annotated[Up | Down, Field(discriminator="state")]
    )
    assert inspect_union_schema(adapter).discriminator == [["state"], ["type"]]
    assert isinstance(adapter.validate_python({"type": "down"}), Down)
    with pytest.raises(ValidationError) as exc:
        adapter.validate_python({"state": "down"})
    assert [(e["type"], e["loc"]) for e in exc.value.errors()] == [("missing", ("down", "type"))]


@pytest.mark.unit
def test_int_tags_match_by_python_hash_equality_so_true_and_float_route() -> None:
    class One(BaseModel):
        v: Literal[1]

    class Two(BaseModel):
        v: Literal[2]

    adapter: TypeAdapter[One | Two] = TypeAdapter(Annotated[One | Two, Field(discriminator="v")])
    assert isinstance(adapter.validate_python({"v": True}), One)
    assert isinstance(adapter.validate_json('{"v": 2.0}'), Two)
    with pytest.raises(ValidationError) as exc:
        adapter.validate_python({"v": "1"})
    assert exc.value.errors()[0]["type"] == "union_tag_invalid"


@pytest.mark.unit
def test_callable_discriminator_routes_by_structural_shape_for_python_and_json() -> None:
    assert inspect_union_schema(SHAPE_ADAPTER).tags == ("circle", "rect", "polygon")
    assert SHAPE_ADAPTER.validate_python({"radius": 2}) == Circle(radius=2.0)
    assert SHAPE_ADAPTER.validate_json('{"width": 3, "height": 4}') == Rect(width=3.0, height=4.0)
    polygon = SHAPE_ADAPTER.validate_json('{"points": [[0, 0], [1, 0], [0, 1]]}')
    assert isinstance(polygon, Polygon)
    with pytest.raises(ValidationError) as exc:
        SHAPE_ADAPTER.validate_python({"points": [[0, 0]]})
    assert [(e["type"], e["loc"]) for e in exc.value.errors()] == [
        ("too_short", ("polygon", "points"))
    ]
    with pytest.raises(ValidationError) as missing:
        SHAPE_ADAPTER.validate_python({"side": 1})
    assert missing.value.errors()[0]["msg"] == (
        "Unable to extract tag using discriminator shape_tag()"
    )


@pytest.mark.unit
def test_callable_sees_python_objects_even_for_json_input() -> None:
    seen: list[object] = []

    def recording_tag(value: Any) -> str | None:
        seen.append(value)
        return shape_tag(value)

    adapter: TypeAdapter[Circle | Rect] = TypeAdapter(
        Annotated[
            Annotated[Circle, Tag("circle")] | Annotated[Rect, Tag("rect")],
            Discriminator(recording_tag),
        ]
    )
    adapter.validate_json('{"radius": 1.5, "meta": {"nested": [1, 2]}}')
    assert seen == [{"radius": 1.5, "meta": {"nested": [1, 2]}}]
    seen.clear()
    adapter.dump_python(Circle(radius=1.0))
    assert len(seen) == 1
    assert isinstance(seen[0], Circle)


@pytest.mark.unit
def test_dict_only_callable_falls_back_to_left_to_right_serialization_with_warning() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        dumped = FRAGILE_SHAPE_ADAPTER.dump_python(Rect(width=1.0, height=2.0))
    assert dumped == {"width": 1.0, "height": 2.0}
    assert len(caught) == 1
    assert "Defaulting to left to right union serialization" in str(caught[0].message)
    with warnings.catch_warnings(record=True) as clean:
        warnings.simplefilter("always")
        SHAPE_ADAPTER.dump_python(Rect(width=1.0, height=2.0))
    assert clean == []


@pytest.mark.unit
def test_raising_callable_escapes_as_raw_exception_not_validation_error() -> None:
    assert FRAGILE_SHAPE_ADAPTER.validate_python({"type": "circle", "radius": 1}) == Circle(
        radius=1.0
    )
    with pytest.raises(KeyError, match="type"):
        FRAGILE_SHAPE_ADAPTER.validate_python({"radius": 1})
    with pytest.raises(TypeError, match="not subscriptable"):
        FRAGILE_SHAPE_ADAPTER.validate_python(7)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_maps_hardened_callable_to_422_and_raising_callable_to_500() -> None:
    transport = httpx.ASGITransport(app=build_shape_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.post("/shapes", json={"radius": 2})
        unknown = await client.post("/shapes", json={"side": 1})
        crash = await client.post("/fragile-shapes", json={"radius": 1})
    assert (ok.status_code, ok.json()) == (200, {"accepted": "Circle"})
    assert unknown.status_code == 422
    assert unknown.json()["detail"][0]["type"] == "union_tag_not_found"
    assert unknown.json()["detail"][0]["ctx"] == {"discriminator": "shape_tag()"}
    assert crash.status_code == 500


def _event_tag(value: Any) -> Any:
    if isinstance(value, dict):
        return cast("dict[str, Any]", value).get("kind")
    return getattr(value, "kind", None)


@pytest.mark.unit
def test_custom_error_is_honored_for_callable_but_dropped_for_str_discriminator() -> None:
    by_callable: TypeAdapter[CardCharged | CardRefunded] = TypeAdapter(
        Annotated[
            Annotated[CardCharged, Tag("card.charged")]
            | Annotated[CardRefunded, Tag("card.refunded")],
            Discriminator(
                _event_tag,
                custom_error_type="unknown_event",
                custom_error_message="Unsupported event type",
                custom_error_context={"docs": "/events"},
            ),
        ]
    )
    by_str: TypeAdapter[CardCharged | CardRefunded] = TypeAdapter(
        Annotated[
            CardCharged | CardRefunded,
            Discriminator(
                "kind",
                custom_error_type="unknown_event",
                custom_error_message="Unsupported event type",
                custom_error_context={"docs": "/events"},
            ),
        ]
    )
    with pytest.raises(ValidationError) as callable_exc:
        by_callable.validate_python({"kind": "card.disputed"})
    [error] = callable_exc.value.errors()
    assert (error["type"], error["msg"], error.get("ctx")) == (
        "unknown_event",
        "Unsupported event type",
        {"docs": "/events"},
    )
    with pytest.raises(ValidationError) as str_exc:
        by_str.validate_python({"kind": "card.disputed"})
    assert str_exc.value.errors()[0]["type"] == "union_tag_invalid"


@pytest.mark.unit
def test_schema_build_rejects_ambiguous_or_untaggable_unions() -> None:
    class DupCharged(BaseModel):
        kind: Literal["card.charged"]

    class Loose(BaseModel):
        kind: str

    class Normalized(BaseModel):
        kind: Literal["n"]

        @field_validator("kind", mode="before")
        @classmethod
        def lower(cls, v: Any) -> Any:
            return v

    with pytest.raises(TypeError, match="mapped to multiple choices"):
        TypeAdapter(Annotated[CardCharged | DupCharged, Field(discriminator="kind")])
    with pytest.raises(PydanticUserError) as literal:
        TypeAdapter(Annotated[CardCharged | Loose, Field(discriminator="kind")])
    assert literal.value.code == "discriminator-needs-literal"
    with pytest.raises(PydanticUserError) as validator:
        TypeAdapter(Annotated[CardCharged | Normalized, Field(discriminator="kind")])
    assert validator.value.code == "discriminator-validator"
    with pytest.raises(PydanticUserError) as no_tag:
        TypeAdapter(Annotated[Annotated[Circle, Tag("circle")] | Rect, Discriminator(shape_tag)])
    assert no_tag.value.code == "callable-discriminator-no-tag"
    with pytest.raises(TypeError, match="'list' is not a valid discriminated union variant"):
        TypeAdapter(Annotated[list[CardCharged | CardRefunded], Field(discriminator="kind")])


@pytest.mark.unit
def test_callable_without_dunder_name_fails_at_schema_build() -> None:
    def by_key(key: str, value: Any) -> Any:
        if isinstance(value, dict):
            return cast("dict[str, Any]", value).get(key)
        return getattr(value, key, None)

    with pytest.raises(SchemaError, match="has no attribute '__name__'"):
        TypeAdapter(
            Annotated[
                Annotated[CardCharged, Tag("card.charged")]
                | Annotated[CardRefunded, Tag("card.refunded")],
                Discriminator(functools.partial(by_key, "kind")),
            ]
        )
    named = functools.update_wrapper(functools.partial(by_key, "kind"), by_key)
    adapter: TypeAdapter[CardCharged | CardRefunded] = TypeAdapter(
        Annotated[
            Annotated[CardCharged, Tag("card.charged")]
            | Annotated[CardRefunded, Tag("card.refunded")],
            Discriminator(named),
        ]
    )
    refund = {"kind": "card.refunded", "amount_cents": 1, "reason": "x"}
    assert isinstance(adapter.validate_python(refund), CardRefunded)


@pytest.mark.unit
def test_nested_discriminators_nest_or_coalesce_by_field_name() -> None:
    class BlackCat(BaseModel):
        pet: Literal["cat"]
        color: Literal["black"]

    class WhiteCat(BaseModel):
        pet: Literal["cat"]
        color: Literal["white"]

    class Dog(BaseModel):
        pet: Literal["dog"]

    class Lion(BaseModel):
        pet: Literal["lion"]

    nested: TypeAdapter[BlackCat | WhiteCat | Dog] = TypeAdapter(
        Annotated[
            Annotated[BlackCat | WhiteCat, Field(discriminator="color")] | Dog,
            Field(discriminator="pet"),
        ]
    )
    with pytest.raises(ValidationError) as exc:
        nested.validate_python({"pet": "cat", "color": "red"})
    assert [(e["type"], e["loc"]) for e in exc.value.errors()] == [("union_tag_invalid", ("cat",))]

    merged: TypeAdapter[Dog | Lion | BlackCat] = TypeAdapter(
        Annotated[
            Annotated[Dog | Lion, Field(discriminator="pet")] | BlackCat,
            Field(discriminator="pet"),
        ]
    )
    assert inspect_union_schema(merged).tags == ("dog", "lion", "cat")


@pytest.mark.unit
def test_json_schema_emits_openapi_discriminator_only_for_string_discriminators() -> None:
    tagged = EVENT_ADAPTER.json_schema()
    assert "oneOf" in tagged
    assert tagged["discriminator"] == {
        "propertyName": "kind",
        "mapping": {
            "card.charged": "#/$defs/CardCharged",
            "card.refunded": "#/$defs/CardRefunded",
            "payout.sent": "#/$defs/PayoutSent",
            "payout.sent.v2": "#/$defs/PayoutSent",
        },
    }
    shaped = SHAPE_ADAPTER.json_schema()
    assert "oneOf" in shaped
    assert "discriminator" not in shaped
    assert "anyOf" in UNTAGGED_EVENT_ADAPTER.json_schema()


@pytest.mark.unit
def test_benchmark_harness_reports_error_amplification_of_2k_minus_1() -> None:
    rows = benchmark_dispatch(sizes=(2, 8), number=1, repeat=1)
    assert [(r.size, r.smart_error_count, r.tagged_error_count) for r in rows] == [
        (2, 3, 1),
        (8, 15, 1),
    ]
    assert all(r.smart_ns > 0 and r.tagged_ns > 0 and r.callable_ns > 0 for r in rows)
    assert sorted(benchmark_json_list(items=8, number=1, repeat=1)) == [
        "callable_json_us",
        "callable_python_us",
        "str_json_us",
        "str_python_us",
    ]
