"""Additive field rollouts: tolerant readers, optional defaults, and fail-closed writers."""

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import ResponseValidationError
from pydantic import ValidationError

from api_labs.f07_versioning_evolution_and_api_guidelines.tolerant_reader import (
    DuplicateKeyError,
    EagerOrderViewV2,
    OrderCreateV2,
    OrderStore,
    PreservingOrderViewV1,
    StrictOrderViewV1,
    TolerantOrderViewV1,
    TolerantOrderViewV2,
    build_v1_app,
    build_v2_app,
    loads_rejecting_duplicate_keys,
    writable_state,
)


def _client(app: FastAPI, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _error_types(exc: ValidationError) -> list[tuple[str, tuple[int | str, ...]]]:
    return [(e["type"], e["loc"]) for e in exc.errors(include_url=False)]


def _rejection(response: httpx.Response) -> tuple[int, list[tuple[str, list[str]]]]:
    return response.status_code, [(d["type"], d["loc"]) for d in response.json()["detail"]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_additive_response_field_breaks_strict_reader_not_tolerant_reader() -> None:
    store = OrderStore()
    async with _client(build_v2_app(store)) as v2:
        created = await v2.post("/orders", json={"sku": "A-1", "quantity": 2, "gift_note": "hi"})
    assert created.status_code == 201
    body = created.content
    assert json.loads(body) == {
        "id": "ord_1",
        "sku": "A-1",
        "quantity": 2,
        "status": "pending",
        "gift_note": "hi",
    }

    with pytest.raises(ValidationError) as strict_err:
        StrictOrderViewV1.model_validate_json(body)
    assert _error_types(strict_err.value) == [("extra_forbidden", ("gift_note",))]

    tolerant = TolerantOrderViewV1.model_validate_json(body)
    assert tolerant.model_dump() == {
        "id": "ord_1",
        "sku": "A-1",
        "quantity": 2,
        "status": "pending",
    }
    assert tolerant.model_extra is None


@pytest.mark.unit
def test_unknown_enum_string_maps_to_fallback_but_wrong_type_still_fails() -> None:
    raw = {"id": "ord_9", "sku": "A-1", "quantity": 1, "status": "on_hold"}
    with pytest.raises(ValidationError) as strict_err:
        StrictOrderViewV1.model_validate(raw)
    assert _error_types(strict_err.value) == [("literal_error", ("status",))]

    assert TolerantOrderViewV1.model_validate(raw).status == "unknown"

    with pytest.raises(ValidationError) as type_err:
        TolerantOrderViewV1.model_validate({**raw, "status": 7})
    assert _error_types(type_err.value) == [("literal_error", ("status",))]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_reader_needs_a_default_because_old_servers_omit_the_field() -> None:
    store = OrderStore()
    async with _client(build_v1_app(store, request_extra="forbid")) as v1:
        created = await v1.post("/orders", json={"sku": "A-1", "quantity": 1})
    assert created.status_code == 201
    assert "gift_note" not in created.json()

    reader = TolerantOrderViewV2.model_validate_json(created.content)
    assert reader.gift_note is None
    assert "gift_note" not in reader.model_fields_set

    with pytest.raises(ValidationError) as eager_err:
        EagerOrderViewV2.model_validate_json(created.content)
    assert _error_types(eager_err.value) == [("missing", ("gift_note",))]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_server_reads_and_replaces_a_record_written_by_the_old_server() -> None:
    store = OrderStore()
    async with _client(build_v1_app(store, request_extra="forbid")) as v1:
        await v1.post("/orders", json={"sku": "A-1", "quantity": 1})
    assert store.orders == {
        "ord_1": {"id": "ord_1", "sku": "A-1", "quantity": 1, "status": "pending"}
    }

    async with _client(build_v2_app(store)) as v2:
        read = await v2.get("/orders/ord_1")
        replaced = await v2.put(
            "/orders/ord_1", json={"sku": "A-1", "quantity": 2, "gift_note": "g"}
        )
    assert read.status_code == 200
    assert read.json() == {
        "id": "ord_1",
        "sku": "A-1",
        "quantity": 1,
        "status": "pending",
        "gift_note": None,
    }
    assert replaced.status_code == 200
    assert replaced.json() == {
        "id": "ord_1",
        "sku": "A-1",
        "quantity": 2,
        "status": "pending",
        "gift_note": "g",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_old_server_in_ignore_mode_silently_drops_dry_run_and_commits() -> None:
    payload = {"sku": "A-1", "quantity": 3, "dry_run": True}

    ignoring_store = OrderStore()
    async with _client(build_v1_app(ignoring_store, request_extra="ignore")) as v1_ignore:
        silent = await v1_ignore.post("/orders", json=payload)
    assert silent.status_code == 201
    assert list(ignoring_store.orders) == ["ord_1"]

    forbidding_store = OrderStore()
    async with _client(build_v1_app(forbidding_store, request_extra="forbid")) as v1_forbid:
        loud = await v1_forbid.post("/orders", json=payload)
    assert loud.status_code == 422
    assert [(d["type"], d["loc"]) for d in loud.json()["detail"]] == [
        ("extra_forbidden", ["body", "dry_run"])
    ]
    assert forbidding_store.orders == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_request_fields_default_to_v1_behavior() -> None:
    assert OrderCreateV2.model_fields["dry_run"].default is False
    assert OrderCreateV2.model_fields["gift_note"].default is None

    store = OrderStore()
    async with _client(build_v2_app(store)) as v2:
        v1_shaped = await v2.post("/orders", json={"sku": "A-1", "quantity": 1})
        preview = await v2.post("/orders", json={"sku": "A-1", "quantity": 1, "dry_run": True})
    assert v1_shaped.status_code == 201
    assert v1_shaped.json()["gift_note"] is None
    assert preview.status_code == 200
    assert preview.json()["id"] == "preview"
    assert store.orders == {
        "ord_1": {
            "id": "ord_1",
            "sku": "A-1",
            "quantity": 1,
            "gift_note": None,
            "status": "pending",
        }
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ignoring_reader_erases_new_field_on_put_preserving_reader_keeps_it() -> None:
    store = OrderStore()
    async with _client(build_v2_app(store)) as v2:
        await v2.post("/orders", json={"sku": "A-1", "quantity": 1, "gift_note": "wrap it"})
        await v2.post("/orders", json={"sku": "B-2", "quantity": 1, "gift_note": "wrap it"})

        lossy = TolerantOrderViewV1.model_validate_json((await v2.get("/orders/ord_1")).content)
        lossy.quantity = 5
        put_lossy = await v2.put("/orders/ord_1", json=writable_state(lossy))

        keeper = PreservingOrderViewV1.model_validate_json((await v2.get("/orders/ord_2")).content)
        keeper.quantity = 5
        assert keeper.model_extra == {"gift_note": "wrap it"}
        put_keeper = await v2.put("/orders/ord_2", json=writable_state(keeper))

    assert put_lossy.status_code == 200
    assert put_keeper.status_code == 200
    assert store.orders["ord_1"]["gift_note"] is None
    assert store.orders["ord_2"]["gift_note"] == "wrap it"
    assert store.orders["ord_2"]["quantity"] == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_old_server_returns_500_for_record_with_new_enum_value_and_filters_new_field() -> (
    None
):
    store = OrderStore()
    async with _client(build_v2_app(store)) as v2:
        await v2.post("/orders", json={"sku": "A-1", "quantity": 1, "gift_note": "n"})
        await v2.post("/orders", json={"sku": "B-2", "quantity": 1})
    store.orders["ord_2"]["status"] = "on_hold"

    async with _client(
        build_v1_app(store, request_extra="forbid"), raise_app_exceptions=False
    ) as v1:
        filtered = await v1.get("/orders/ord_1")
        broken = await v1.get("/orders/ord_2")
    assert filtered.status_code == 200
    assert filtered.json() == {"id": "ord_1", "sku": "A-1", "quantity": 1, "status": "pending"}
    assert broken.status_code == 500

    async with _client(build_v1_app(store, request_extra="forbid")) as v1_raising:
        with pytest.raises(ResponseValidationError) as exc_info:
            await v1_raising.get("/orders/ord_2")
    assert [(e["type"], e["loc"]) for e in exc_info.value.errors()] == [
        ("literal_error", ("response", "status"))
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_allow_mode_on_server_request_model_enables_mass_assignment() -> None:
    attack = {"sku": "A-1", "quantity": 1, "status": "shipped", "dry_run": True}
    outcomes: dict[str, tuple[int, Any]] = {}
    rejections: dict[str, Any] = {}
    for mode in ("allow", "ignore", "forbid"):
        store = OrderStore()
        async with _client(build_v1_app(store, request_extra=mode)) as v1:
            response = await v1.post("/orders", json=attack)
        outcomes[mode] = (response.status_code, store.orders.get("ord_1"))
        rejections[mode] = response.json().get("detail")
    assert outcomes == {
        "allow": (
            201,
            {"id": "ord_1", "sku": "A-1", "quantity": 1, "status": "shipped", "dry_run": True},
        ),
        "ignore": (201, {"id": "ord_1", "sku": "A-1", "quantity": 1, "status": "pending"}),
        "forbid": (422, None),
    }
    assert rejections["allow"] is None
    assert rejections["ignore"] is None
    assert [(d["type"], d["loc"]) for d in rejections["forbid"]] == [
        ("extra_forbidden", ["body", "status"]),
        ("extra_forbidden", ["body", "dry_run"]),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forbid_rejection_adds_one_detail_entry_per_unknown_member() -> None:
    one = {"sku": "A-1", "quantity": 1, "dry_run": True}
    many = {"sku": "A-1", "quantity": 1} | {f"unknown_{i:04d}": "x" * 8 for i in range(1000)}
    store = OrderStore()
    async with _client(build_v1_app(store, request_extra="forbid")) as v1:
        small = await v1.post("/orders", json=one)
        large = await v1.post("/orders", json=many)
    assert (small.status_code, large.status_code) == (422, 422)
    assert (len(small.request.content), len(small.content)) == (41, 116)
    assert (len(large.request.content), len(large.content)) == (26026, 115012)
    detail = large.json()["detail"]
    assert len(detail) == 1000
    assert detail[0] == {
        "type": "extra_forbidden",
        "loc": ["body", "unknown_0000"],
        "msg": "Extra inputs are not permitted",
        "input": "xxxxxxxx",
    }
    assert store.orders == {}


@pytest.mark.unit
def test_forbid_request_model_publishes_closed_schema_response_model_stays_open() -> None:
    schemas = build_v2_app(OrderStore()).openapi()["components"]["schemas"]
    assert schemas["OrderCreateV2"]["additionalProperties"] is False
    assert schemas["OrderReplaceV2"]["additionalProperties"] is False
    assert "additionalProperties" not in schemas["OrderOutV2"]
    assert "gift_note" not in schemas["OrderCreateV2"]["required"]


@pytest.mark.unit
def test_extra_mode_is_published_as_additional_properties_in_json_schema() -> None:
    assert StrictOrderViewV1.model_json_schema()["additionalProperties"] is False
    assert PreservingOrderViewV1.model_json_schema()["additionalProperties"] is True
    assert "additionalProperties" not in TolerantOrderViewV1.model_json_schema()


@pytest.mark.unit
def test_per_call_extra_overrides_validation_but_only_config_allow_dumps_extras() -> None:
    raw = {"id": "ord_1", "sku": "A-1", "quantity": 1, "status": "pending", "gift_note": "n"}
    known = {"id": "ord_1", "sku": "A-1", "quantity": 1, "status": "pending"}

    with pytest.raises(ValidationError) as forbid_err:
        TolerantOrderViewV1.model_validate(raw, extra="forbid")
    assert _error_types(forbid_err.value) == [("extra_forbidden", ("gift_note",))]

    relaxed = StrictOrderViewV1.model_validate(raw, extra="ignore")
    assert relaxed.model_extra is None
    assert relaxed.model_dump() == known

    per_call = StrictOrderViewV1.model_validate(raw, extra="allow")
    assert per_call.model_extra == {"gift_note": "n"}
    assert per_call.model_dump() == known

    configured = PreservingOrderViewV1.model_validate(raw)
    assert "gift_note" in configured.model_fields_set
    assert configured.model_dump() == raw
    assert json.loads(configured.model_dump_json()) == raw


@pytest.mark.unit
def test_duplicate_member_names_take_last_value_unless_rejected() -> None:
    raw = b'{"id":"ord_1","sku":"A-1","quantity":1,"quantity":1000,"status":"pending"}'
    assert TolerantOrderViewV1.model_validate_json(raw).quantity == 1000
    assert json.loads(raw)["quantity"] == 1000
    with pytest.raises(DuplicateKeyError, match="'quantity'"):
        loads_rejecting_duplicate_keys(raw)
    assert loads_rejecting_duplicate_keys(b'{"a":1,"b":{"a":2}}') == {"a": 1, "b": {"a": 2}}
    assert loads_rejecting_duplicate_keys(b'{"a":{"a":2}}') == {"a": {"a": 2}}
    with pytest.raises(DuplicateKeyError, match="'a'"):
        loads_rejecting_duplicate_keys(b'{"b":{"a":1,"a":2}}')


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_order_id_is_404_on_every_build() -> None:
    store = OrderStore()
    async with _client(build_v1_app(store, request_extra="forbid")) as v1:
        v1_get = await v1.get("/orders/ord_404")
    async with _client(build_v2_app(store)) as v2:
        v2_get = await v2.get("/orders/ord_404")
        v2_put = await v2.put("/orders/ord_404", json={"sku": "A-1", "quantity": 1})
    assert (v1_get.status_code, v2_get.status_code, v2_put.status_code) == (404, 404, 404)
    assert store.orders == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_field_bounds_are_enforced_on_every_build() -> None:
    edge = {"sku": "S" * 64, "quantity": 1000}
    out_of_range: list[tuple[dict[str, Any], tuple[str, list[str]]]] = [
        ({"sku": "", "quantity": 1}, ("string_too_short", ["body", "sku"])),
        ({"sku": "S" * 65, "quantity": 1}, ("string_too_long", ["body", "sku"])),
        ({"sku": "A-1", "quantity": 0}, ("greater_than_equal", ["body", "quantity"])),
        ({"sku": "A-1", "quantity": 1001}, ("less_than_equal", ["body", "quantity"])),
    ]
    long_note: tuple[dict[str, Any], tuple[str, list[str]]] = (
        {"sku": "A-1", "quantity": 1, "gift_note": "n" * 201},
        ("string_too_long", ["body", "gift_note"]),
    )

    v1_store = OrderStore()
    async with _client(build_v1_app(v1_store, request_extra="forbid")) as v1:
        assert (await v1.post("/orders", json=edge)).status_code == 201
        for body, error in out_of_range:
            assert _rejection(await v1.post("/orders", json=body)) == (422, [error])
    assert list(v1_store.orders) == ["ord_1"]

    v2_store = OrderStore()
    async with _client(build_v2_app(v2_store)) as v2:
        seeded = await v2.post("/orders", json={**edge, "gift_note": "n" * 200})
        assert seeded.status_code == 201
        for body, error in [*out_of_range, long_note]:
            assert _rejection(await v2.post("/orders", json=body)) == (422, [error])
            assert _rejection(await v2.put("/orders/ord_1", json=body)) == (422, [error])
    assert v2_store.orders == {
        "ord_1": {"id": "ord_1", **edge, "gift_note": "n" * 200, "status": "pending"}
    }
