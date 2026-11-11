"""Lab: AIP-203 field behaviors (REQUIRED, OUTPUT_ONLY, INPUT_ONLY, IMMUTABLE, IDENTIFIER)."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from api_labs.f04_rest_and_resource_modeling.field_behavior import (
    DISK_BEHAVIORS,
    EPOCH,
    B,
    Disk,
    DiskStore,
    FieldBehaviorSchemaError,
    behavior_table,
    behaviors,
    create_app,
    fixed_clock,
    is_truthy,
    plan_create,
    plan_update,
    render,
    sequential_uuid4,
)

BASE: dict[str, Any] = {"display_name": "data", "disk_type": "pd-ssd", "size_gb": 10}


def new_store() -> DiskStore:
    return DiskStore(fixed_clock(EPOCH, timedelta(seconds=1)), sequential_uuid4())


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def create(client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    return await client.post("/v1/disks", params={"disk_id": "d1"}, json=body)


def error_of(response: httpx.Response) -> tuple[int, str, str, str]:
    error = response.json()["error"]
    info = error["details"][0]
    return error["code"], error["status"], info["reason"], info["metadata"]["field"]


# --- OUTPUT_ONLY -----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_silently_drops_output_only_and_identifier_values() -> None:
    async with client_for(create_app(new_store())) as client:
        forged = {
            **BASE,
            "name": "disks/someone-else",
            "uid": "attacker-chosen",
            "state": "DELETED",
            "create_time": "1999-01-01T00:00:00Z",
        }
        response = await create(client, forged)
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "disks/d1"
    assert body["uid"] == "00000000-0000-4000-8000-000000000001"
    assert body["state"] == "READY"
    assert body["create_time"] == "2026-11-10T12:00:00Z"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_modify_patch_round_trip_succeeds_without_a_mask() -> None:
    async with client_for(create_app(new_store())) as client:
        await create(client, BASE)
        fetched = (await client.get("/v1/disks/d1")).json()
        fetched["display_name"] = "renamed"
        response = await client.patch("/v1/disks/d1", json=fetched)
    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "renamed"
    assert body["uid"] == fetched["uid"]
    assert body["create_time"] == fetched["create_time"]
    assert body["update_time"] == "2026-11-10T12:00:01Z"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_output_only_path_in_update_mask_is_ignored_not_rejected() -> None:
    async with client_for(create_app(new_store())) as client:
        await create(client, BASE)
        response = await client.patch(
            "/v1/disks/d1",
            params={"update_mask": "state,display_name"},
            json={"state": "DELETED", "display_name": "x"},
        )
    assert response.status_code == 200
    assert response.json()["state"] == "READY"
    assert response.json()["display_name"] == "x"


# --- IMMUTABLE and IDENTIFIER ----------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_immutable_field_with_unchanged_value_is_accepted_as_a_no_op() -> None:
    async with client_for(create_app(new_store())) as client:
        created = (await create(client, BASE)).json()
        response = await client.patch(
            "/v1/disks/d1", params={"update_mask": "disk_type"}, json={"disk_type": "pd-ssd"}
        )
    assert response.status_code == 200
    assert response.json()["update_time"] == created["update_time"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_immutable_field_change_is_invalid_argument_and_store_unchanged() -> None:
    async with client_for(create_app(new_store())) as client:
        await create(client, BASE)
        response = await client.patch(
            "/v1/disks/d1",
            params={"update_mask": "disk_type,display_name"},
            json={"disk_type": "pd-standard", "display_name": "x"},
        )
        after = (await client.get("/v1/disks/d1")).json()
    assert response.status_code == 400
    assert error_of(response) == (400, "INVALID_ARGUMENT", "FIELD_IMMUTABLE", "disk_type")
    assert after["disk_type"] == "pd-ssd"
    assert after["display_name"] == "data"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_identifier_cannot_be_renamed_through_update() -> None:
    async with client_for(create_app(new_store())) as client:
        await create(client, BASE)
        same = await client.patch("/v1/disks/d1", json={"name": "disks/d1", "size_gb": 20})
        renamed = await client.patch("/v1/disks/d1", json={"name": "disks/d2"})
    assert same.status_code == 200
    assert same.json()["size_gb"] == 20
    assert renamed.status_code == 400
    assert error_of(renamed)[2:] == ("FIELD_IMMUTABLE", "name")


# --- REQUIRED ----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"disk_type": "pd-ssd"}, "display_name"),
        ({"display_name": "", "disk_type": "pd-ssd"}, "display_name"),
        ({"display_name": "data", "disk_type": ""}, "disk_type"),
    ],
    ids=["absent", "empty-string", "empty-immutable"],
)
async def test_required_field_must_be_truthy_on_create(body: dict[str, Any], field: str) -> None:
    async with client_for(create_app(new_store())) as client:
        response = await create(client, body)
    assert response.status_code == 400
    assert error_of(response) == (400, "INVALID_ARGUMENT", "FIELD_REQUIRED", field)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_required_field_may_be_omitted_when_absent_from_the_mask() -> None:
    async with client_for(create_app(new_store())) as client:
        await create(client, BASE)
        response = await client.patch(
            "/v1/disks/d1", params={"update_mask": "size_gb"}, json={"size_gb": 50}
        )
    assert response.status_code == 200
    assert response.json()["display_name"] == "data"
    assert response.json()["size_gb"] == 50


@pytest.mark.unit
@pytest.mark.asyncio
async def test_required_field_in_mask_cannot_be_cleared() -> None:
    async with client_for(create_app(new_store())) as client:
        await create(client, BASE)
        response = await client.patch(
            "/v1/disks/d1", params={"update_mask": "display_name"}, json={}
        )
    assert response.status_code == 400
    assert error_of(response)[2:] == ("FIELD_REQUIRED", "display_name")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, False),
        (0.0, False),
        ("", False),
        (b"", False),
        (False, False),
        ([], False),
        ({}, False),
        (None, False),
        (1, True),
        ("x", True),
        (True, True),
        ([0], True),
    ],
)
def test_truthiness_matches_aip_203_definition(value: object, expected: bool) -> None:
    assert is_truthy(value) is expected


# --- INPUT_ONLY --------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_input_only_ttl_is_consumed_and_never_rendered() -> None:
    async with client_for(create_app(new_store())) as client:
        created = (await create(client, {**BASE, "ttl_seconds": 3600})).json()
        fetched = (await client.get("/v1/disks/d1")).json()
    assert "ttl_seconds" not in created
    assert "ttl_seconds" not in fetched
    assert created["expire_time"] == "2026-11-10T13:00:00Z"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_input_only_ttl_on_update_recomputes_expire_time() -> None:
    async with client_for(create_app(new_store())) as client:
        await create(client, {**BASE, "ttl_seconds": 3600})
        response = await client.patch(
            "/v1/disks/d1", params={"update_mask": "ttl_seconds"}, json={"ttl_seconds": 60}
        )
    assert response.status_code == 200
    assert "ttl_seconds" not in response.json()
    assert response.json()["expire_time"] == "2026-11-10T12:01:01Z"


@pytest.mark.unit
def test_render_hides_input_only_even_when_a_value_is_present() -> None:
    disk = Disk(display_name="d", disk_type="pd-ssd", ttl_seconds=5)
    assert disk.model_dump()["ttl_seconds"] == 5
    assert "ttl_seconds" not in render(disk, DISK_BEHAVIORS)


# --- Masks, timestamps, and the plan ------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_mask_path_is_invalid_argument() -> None:
    async with client_for(create_app(new_store())) as client:
        await create(client, BASE)
        response = await client.patch("/v1/disks/d1", params={"update_mask": "colour"}, json={})
    assert response.status_code == 400
    assert error_of(response)[2:] == ("UNKNOWN_FIELD", "colour")


@pytest.mark.unit
def test_plan_create_strips_output_only_and_identifier_before_storage() -> None:
    body = Disk.model_validate({**BASE, "name": "disks/x", "uid": "u", "state": "DELETED"})
    plan = plan_create(body, DISK_BEHAVIORS)
    assert plan.ignored == ("name", "state", "uid")
    assert plan.changes == BASE


@pytest.mark.unit
def test_plan_reports_ignored_paths_in_mask_order() -> None:
    stored = new_store().create("d1", Disk.model_validate(BASE))
    body = Disk(uid="u", disk_type="pd-ssd", size_gb=99)
    plan = plan_update(stored, body, ["uid", "disk_type", "size_gb"], DISK_BEHAVIORS)
    assert plan.changes == {"size_gb": 99}
    assert plan.ignored == ("uid", "disk_type")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_maskless_patch_applies_every_sent_key_even_an_empty_value() -> None:
    # Lab choice: no mask means "the keys the client sent" (`model_fields_set`), not
    # AIP-134's "all fields that are populated (have a non-empty value)".
    async with client_for(create_app(new_store())) as client:
        await create(client, BASE)
        zeroed = await client.patch("/v1/disks/d1", json={"size_gb": 0})
        cleared = await client.patch("/v1/disks/d1", json={"display_name": ""})
    assert zeroed.status_code == 200
    assert zeroed.json()["size_gb"] == 0
    assert cleared.status_code == 400
    assert error_of(cleared)[2:] == ("FIELD_REQUIRED", "display_name")


# --- OpenAPI -------------------------------------------------------------------


@pytest.mark.unit
def test_openapi_marks_output_only_read_only_and_input_only_write_only() -> None:
    schemas = create_app(new_store()).openapi()["components"]["schemas"]
    disk_schemas = {k: v for k, v in schemas.items() if k.startswith("Disk")}
    assert disk_schemas
    for schema in disk_schemas.values():
        props = schema["properties"]
        assert all(props[f].get("readOnly") is True for f in ("uid", "state", "create_time"))
        assert "readOnly" not in props["name"]
        assert "readOnly" not in props["display_name"]
        assert props["ttl_seconds"].get("writeOnly") is True


# --- Annotation linting ----------------------------------------------------------


class _Unannotated(BaseModel):
    title: str = ""


class _OutputRequired(BaseModel):
    uid: Annotated[str, behaviors(B.OUTPUT_ONLY, B.REQUIRED)] = ""


class _IdentifierElsewhere(BaseModel):
    parent: Annotated[str, behaviors(B.IDENTIFIER)] = ""


class _InputOnlyAlone(BaseModel):
    ttl: Annotated[int, behaviors(B.INPUT_ONLY)] = 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("model", "fragment"),
    [
        (_Unannotated, "no field_behavior"),
        (_OutputRequired, "contradicts"),
        (_IdentifierElsewhere, "only on `name`"),
        (_InputOnlyAlone, "needs REQUIRED"),
    ],
    ids=["missing", "output-and-required", "identifier-misplaced", "input-only-alone"],
)
def test_behavior_table_rejects_models_that_break_aip_203(
    model: type[BaseModel], fragment: str
) -> None:
    with pytest.raises(FieldBehaviorSchemaError, match=fragment):
        behavior_table(model)


# --- Library traps (Pydantic 2.14.3 and FastAPI 0.162.0 characterization) ---------


class _FrozenDisk(BaseModel):
    disk_type: str = Field(frozen=True)


@pytest.mark.unit
def test_pydantic_frozen_field_guards_assignment_only() -> None:
    disk = _FrozenDisk(disk_type="pd-ssd")
    with pytest.raises(ValidationError) as caught:
        disk.disk_type = "pd-standard"  # type: ignore[misc]  # the plugin knows it is frozen
    assert caught.value.errors()[0]["type"] == "frozen_field"
    assert disk.model_copy(update={"disk_type": "pd-standard"}).disk_type == "pd-standard"
    merged = _FrozenDisk.model_validate({**disk.model_dump(), "disk_type": "pd-standard"})
    assert merged.disk_type == "pd-standard"


@pytest.mark.unit
def test_naive_model_copy_update_mass_assigns_output_only_fields() -> None:
    stored = new_store().create("d1", Disk.model_validate(BASE))
    body = Disk.model_validate({"state": "DELETED", "uid": "forged"})
    naive = stored.model_copy(update=body.model_dump(exclude_unset=True))
    assert (naive.state, naive.uid) == ("DELETED", "forged")
    plan = plan_update(stored, body, None, DISK_BEHAVIORS)
    assert plan.changes == {}


class _StrictCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    disk_type: str


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extra_forbid_create_model_rejects_a_round_tripped_resource() -> None:
    app = FastAPI()

    @app.post("/v1/disks")
    async def create_strict(body: _StrictCreate) -> dict[str, str]:
        return body.model_dump()

    async with client_for(app) as client:
        response = await client.post("/v1/disks", json={**BASE, "uid": "u"})
    assert response.status_code == 422
    types = {e["type"] for e in response.json()["detail"]}
    assert types == {"extra_forbidden"}


class _Account(BaseModel):
    password: SecretStr


@pytest.mark.unit
def test_secret_str_masks_the_value_but_still_emits_the_field() -> None:
    dumped = _Account(password=SecretStr("hunter2")).model_dump_json()
    assert dumped == '{"password":"**********"}'


class _PlainRequired(BaseModel):
    display_name: str


@pytest.mark.unit
def test_pydantic_required_means_present_not_truthy() -> None:
    assert _PlainRequired.model_validate({"display_name": ""}).display_name == ""
    with pytest.raises(ValidationError):
        _PlainRequired.model_validate({})
