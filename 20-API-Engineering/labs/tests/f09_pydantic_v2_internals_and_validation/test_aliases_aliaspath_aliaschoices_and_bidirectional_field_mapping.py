"""Unit tests for validation_alias, serialization_alias, AliasPath, AliasChoices, AliasGenerator."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import AliasPath, BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel, to_snake

from api_labs.f09_pydantic_v2_internals_and_validation.field_alias_mapping import (
    OrderView,
    build_order_import_app,
    one_way_fields,
    output_key_collisions,
    parse_legacy_order,
)

LEGACY: dict[str, Any] = {
    "order": {
        "header": {"id": 4711},
        "customer": {"email": "ops@example.com"},
        "lines": [{"sku": "A-1"}, {"sku": "B-2"}, {"sku": "C-3"}],
        "totals": {"grand_cents": 12999},
    }
}
CAMEL = {
    "orderId": 4711,
    "customerEmail": "ops@example.com",
    "firstSku": "A-1",
    "lastSku": "C-3",
    "totalCents": 12999,
}


def _errors(exc: ValidationError) -> list[tuple[str, tuple[int | str, ...]]]:
    return [(e["type"], e["loc"]) for e in exc.errors(include_url=False)]


@pytest.mark.unit
def test_aliaspath_and_aliaschoices_flatten_legacy_json() -> None:
    from_json = parse_legacy_order(json.dumps(LEGACY).encode())
    from_python = OrderView.model_validate(LEGACY)
    assert from_json == from_python
    assert from_json.order_id == 4711
    assert from_json.first_sku == "A-1"
    assert from_json.last_sku == "C-3"  # AliasPath(..., -1, ...) indexes from the end

    both = {**LEGACY, "cust_email": "flat@example.com"}
    # Choice order, not input order, decides: "cust_email" is listed before the nested path.
    assert parse_legacy_order(json.dumps(both).encode()).customer_email == "flat@example.com"
    assert OrderView.model_validate(both).customer_email == "flat@example.com"


@pytest.mark.unit
def test_serialize_by_alias_emits_camel_case_and_round_trips() -> None:
    view = OrderView.model_validate(LEGACY)
    assert view.model_dump() == CAMEL
    assert view.model_dump(by_alias=False)["order_id"] == 4711
    assert OrderView.model_validate_json(view.model_dump_json()) == view
    assert one_way_fields(OrderView) == []
    assert output_key_collisions(OrderView) == {}


@pytest.mark.unit
def test_error_loc_is_matched_path_or_first_alias() -> None:
    bad_type: dict[str, Any] = json.loads(json.dumps(LEGACY))
    bad_type["order"]["header"]["id"] = "x"
    bad_type["order"]["totals"]["grand_cents"] = -1
    with pytest.raises(ValidationError) as info:
        parse_legacy_order(json.dumps(bad_type).encode())
    assert _errors(info.value) == [
        ("int_parsing", ("order", "header", "id")),
        ("greater_than_equal", ("order", "totals", "grand_cents")),
    ]

    missing = {k: v for k, v in CAMEL.items() if k != "orderId"}
    with pytest.raises(ValidationError) as info:
        OrderView.model_validate(missing)
    assert _errors(info.value) == [("missing", ("orderId",))]


@pytest.mark.unit
def test_duplicate_json_keys_last_wins_and_replace_nested_parent() -> None:
    dup = b'{"orderId": 1, "orderId": 2, "customerEmail": "a@example.com",'
    dup += b' "firstSku": "A", "lastSku": "Z", "totalCents": 5}'
    assert parse_legacy_order(dup).order_id == 2

    replaced = json.dumps(LEGACY)[:-1] + ', "order": 7}'
    with pytest.raises(ValidationError) as info:
        parse_legacy_order(replaced.encode())
    assert _errors(info.value) == [
        ("missing", ("orderId",)),
        ("missing", ("customerEmail",)),
        ("missing", ("firstSku",)),
        ("missing", ("lastSku",)),
        ("missing", ("totalCents",)),
        ("extra_forbidden", ("order",)),
    ]


@pytest.mark.unit
def test_alias_generator_respects_alias_priority() -> None:
    class Generated(BaseModel):
        model_config = ConfigDict(alias_generator=to_camel)
        order_id: int
        note: str = Field(alias="comment")
        legacy: int = Field(validation_alias="LEGACY")
        low: int = Field(alias="lowPri", alias_priority=1)

    def summary(model: type[BaseModel]) -> dict[str, tuple[object, ...]]:
        return {
            n: (f.alias, f.validation_alias, f.serialization_alias, f.alias_priority)
            for n, f in model.model_fields.items()
        }

    assert summary(Generated) == {
        "order_id": ("orderId", "orderId", "orderId", 1),
        "note": ("comment", "comment", "comment", 2),
        "legacy": ("legacy", "LEGACY", "legacy", 2),
        "low": ("low", "low", "low", 1),
    }
    order_id = OrderView.model_fields["order_id"]
    assert (order_id.alias, order_id.serialization_alias, order_id.alias_priority) == (
        None,
        "orderId",
        2,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field_name", "camel", "back"),
    [
        ("http_response_code", "httpResponseCode", "http_response_code"),
        ("sha256_hash", "sha256Hash", "sha_256_hash"),
        ("a_1b", "a1B", "a_1_b"),
        ("userId", "userId", "user_id"),
    ],
    ids=["round-trips", "digit-split", "digit-letter", "camel-passthrough"],
)
def test_to_camel_is_not_invertible(field_name: str, camel: str, back: str) -> None:
    assert to_camel(field_name) == camel
    assert to_snake(camel) == back


@pytest.mark.unit
def test_audit_finds_one_way_and_colliding_fields() -> None:
    class Lossy(BaseModel):
        model_config = ConfigDict(serialize_by_alias=True)
        a: int = Field(serialization_alias="b")
        b: int
        nested_id: int = Field(validation_alias=AliasPath("meta", "id"))

    assert one_way_fields(Lossy) == ["a", "nested_id"]
    assert output_key_collisions(Lossy) == {"b": ["a", "b"]}
    m = Lossy.model_validate({"a": 1, "b": 2, "meta": {"id": 3}})
    assert m.model_dump() == {"b": 2, "nested_id": 3}  # field a silently lost
    assert m.model_dump_json() == '{"b":1,"b":2,"nested_id":3}'  # duplicate JSON names
    assert list(Lossy.model_json_schema(mode="validation")["properties"]) == [
        "a",
        "b",
        "nested_id",  # documents the attribute name, but the wire needs meta.id
    ]


@pytest.mark.unit
def test_name_and_alias_conflict_and_aliaspath_root_consumption() -> None:
    class Both(BaseModel):
        model_config = ConfigDict(validate_by_name=True, extra="forbid")
        a: int = Field(alias="b")

    with pytest.raises(ValidationError) as info:
        Both.model_validate({"a": 1, "b": 2})
    assert _errors(info.value) == [("extra_forbidden", ("a",))]

    smuggled: dict[str, Any] = json.loads(json.dumps(LEGACY))
    smuggled["order"]["internal_discount_cents"] = 12999
    # extra="forbid" only polices top-level keys; "order" is consumed by AliasPath roots.
    assert parse_legacy_order(json.dumps(smuggled).encode()).total_cents == 12999


@pytest.mark.unit
def test_json_schema_names_follow_mode_and_first_single_key_choice() -> None:
    for mode in ("validation", "serialization"):
        schema = OrderView.model_json_schema(mode=mode)
        assert list(schema["properties"]) == list(CAMEL)
        assert schema["required"] == list(CAMEL)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_import_endpoint_answers_in_camel_case() -> None:
    transport = httpx.ASGITransport(app=build_order_import_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.post("/v2/orders:import", json=LEGACY)
        assert ok.status_code == 200
        assert ok.json() == CAMEL

        bad: dict[str, Any] = json.loads(json.dumps(LEGACY))
        bad["order"]["header"]["id"] = "x"
        resp = await client.post("/v2/orders:import", json=bad)
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert [(d["type"], d["loc"]) for d in detail] == [
            ("int_parsing", ["body", "order", "header", "id"]),
        ]
