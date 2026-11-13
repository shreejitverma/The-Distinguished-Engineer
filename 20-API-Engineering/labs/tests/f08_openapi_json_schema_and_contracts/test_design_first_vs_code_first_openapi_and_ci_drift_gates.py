"""Snapshot and surface drift gates for code-first and design-first OpenAPI workflows."""

import json
import os
import subprocess
import sys
import warnings
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from openapi_spec_validator import validate
from pydantic import create_model

from api_labs.f08_openapi_json_schema_and_contracts.openapi_drift_gate import (
    JsonObject,
    SnapshotMissingError,
    SpecDriftError,
    UpdateRefusedError,
    build_gift_note_orders_app,
    build_orders_app,
    build_priced_orders_app,
    build_reordered_orders_app,
    canonicalize,
    check_snapshot,
    duplicate_operation_ids,
    pointer_diff,
    render,
    surface_drift,
)

EXPORT_ORDERS = (
    "from api_labs.f08_openapi_json_schema_and_contracts.openapi_drift_gate import "
    "build_orders_app, render\n"
    "print(render(build_orders_app().openapi()), end='')\n"
)
EXPORT_MULTI_METHOD = (
    "import warnings\n"
    "from fastapi import FastAPI\n"
    "warnings.simplefilter('ignore')\n"
    "app = FastAPI(title='t', version='1')\n"
    "@app.api_route('/multi', methods=['GET', 'POST', 'PUT', 'DELETE'])\n"
    "def multi() -> int:\n"
    "    return 1\n"
    "print(app.openapi()['paths']['/multi']['get']['operationId'], end='')\n"
)


def run_with_hash_seed(code: str, seed: int) -> str:
    env = {**os.environ, "PYTHONHASHSEED": str(seed)}
    result = subprocess.run(  # noqa: S603 - fixed interpreter and fixed code
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    )
    return result.stdout


@pytest.mark.unit
def test_render_ignores_registration_order_and_field_order() -> None:
    base, reordered = build_orders_app().openapi(), build_reordered_orders_app().openapi()
    assert json.dumps(base) != json.dumps(reordered)
    assert list(base["paths"]) == ["/orders", "/orders/{order_id}"]
    assert list(reordered["paths"]) == ["/orders/{order_id}", "/orders"]
    assert render(base) == render(reordered)


@pytest.mark.unit
def test_only_set_valued_arrays_are_sorted() -> None:
    schema: JsonObject = {"required": ["b", "a"], "enum": ["b", "a"], "allOf": [{"z": 1}, {"a": 2}]}
    assert canonicalize(schema) == {
        "allOf": [{"z": 1}, {"a": 2}],
        "enum": ["b", "a"],
        "required": ["a", "b"],
    }


@pytest.mark.unit
def test_snapshot_round_trip_is_unchanged(tmp_path: Path) -> None:
    snapshot = tmp_path / "openapi.json"
    assert check_snapshot(build_orders_app().openapi(), snapshot, update=True) == "written"
    assert check_snapshot(build_reordered_orders_app().openapi(), snapshot) == "unchanged"
    assert snapshot.read_bytes().endswith(b"}\n")


@pytest.mark.unit
def test_additive_response_field_fails_with_one_pointer(tmp_path: Path) -> None:
    snapshot = tmp_path / "openapi.json"
    check_snapshot(build_orders_app().openapi(), snapshot, update=True)
    with pytest.raises(SpecDriftError) as caught:
        check_snapshot(build_gift_note_orders_app().openapi(), snapshot)
    assert caught.value.changes == ["+ /components/schemas/Order/properties/gift_note"]


@pytest.mark.unit
def test_decimal_field_splits_the_shared_component(tmp_path: Path) -> None:
    snapshot = tmp_path / "openapi.json"
    check_snapshot(build_orders_app().openapi(), snapshot, update=True)
    with pytest.raises(SpecDriftError) as caught:
        check_snapshot(build_priced_orders_app().openapi(), snapshot)
    assert caught.value.changes == [
        "- /components/schemas/LineItem",
        "+ /components/schemas/LineItem-Input",
        "+ /components/schemas/LineItem-Output",
        "~ /components/schemas/Order/properties/items/items/$ref",
        "~ /components/schemas/OrderCreate/properties/items/items/$ref",
    ]


@pytest.mark.unit
def test_missing_snapshot_fails_instead_of_creating_one(tmp_path: Path) -> None:
    snapshot = tmp_path / "openapi.json"
    with pytest.raises(SnapshotMissingError):
        check_snapshot(build_orders_app().openapi(), snapshot)
    assert not snapshot.exists()


@pytest.mark.unit
def test_update_is_refused_in_ci(tmp_path: Path) -> None:
    snapshot = tmp_path / "openapi.json"
    check_snapshot(build_orders_app().openapi(), snapshot, update=True)
    before = snapshot.read_bytes()
    with pytest.raises(UpdateRefusedError):
        check_snapshot(build_gift_note_orders_app().openapi(), snapshot, update=True, ci=True)
    assert snapshot.read_bytes() == before


@pytest.mark.unit
def test_non_canonical_snapshot_with_equal_content_fails(tmp_path: Path) -> None:
    document = build_orders_app().openapi()
    snapshot = tmp_path / "openapi.json"
    snapshot.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SpecDriftError) as caught:
        check_snapshot(document, snapshot)
    assert caught.value.changes == ["~ (snapshot is not in canonical form)"]


@pytest.mark.unit
def test_pointer_tokens_are_escaped_per_rfc_6901() -> None:
    assert pointer_diff({}, {"a/b~c": 1}) == ["+ /a~1b~0c"]


@pytest.mark.unit
def test_pointer_diff_separates_true_from_one() -> None:
    assert {"minimum": 1} == {"minimum": True}  # the Python equality the diff must not trust
    assert pointer_diff({"minimum": 1}, {"minimum": True}) == ["~ /minimum"]


@pytest.mark.unit
def test_duplicate_operation_id_only_warns_in_fastapi() -> None:
    app = FastAPI(title="t", version="1")

    @app.get("/x", operation_id="dup")
    def first() -> int:  # pyright: ignore[reportUnusedFunction]
        return 1

    @app.get("/y", operation_id="dup")
    def second() -> int:  # pyright: ignore[reportUnusedFunction]
        return 2

    with pytest.warns(UserWarning, match="Duplicate Operation ID dup"):
        document = app.openapi()
    assert duplicate_operation_ids(document) == ["dup"]
    assert duplicate_operation_ids(build_orders_app().openapi()) == []


@pytest.mark.unit
def test_export_validates_as_openapi_3_1() -> None:
    document = build_orders_app().openapi()
    assert document["openapi"] == "3.1.0"
    validate(document)


@pytest.mark.unit
def test_multi_method_operation_id_depends_on_hash_seed() -> None:
    ids = {run_with_hash_seed(EXPORT_MULTI_METHOD, seed) for seed in range(8)}
    assert len(ids) > 1
    assert ids <= {f"multi_multi_{m}" for m in ("get", "post", "put", "delete")}


@pytest.mark.unit
def test_orders_export_is_byte_stable_across_hash_seeds() -> None:
    exports = {run_with_hash_seed(EXPORT_ORDERS, seed) for seed in range(4)}
    assert len(exports) == 1
    assert exports == {render(build_orders_app().openapi())}


@pytest.mark.unit
def test_design_first_gate_finds_undocumented_and_missing_operations() -> None:
    design = build_orders_app().openapi()
    assert surface_drift(design, build_orders_app()).clean
    hidden = surface_drift(design, build_orders_app(hidden_route=True))
    assert hidden.undocumented == {"POST /internal/reindex"}
    assert hidden.missing == frozenset()
    design["paths"]["/orders/{id}/cancel"] = {"post": {"responses": {"200": {"description": "ok"}}}}
    assert surface_drift(design, build_orders_app()).missing == {"POST /orders/{}/cancel"}


@pytest.mark.unit
def test_template_names_do_not_count_as_drift() -> None:
    design = build_orders_app().openapi()
    design["paths"]["/orders/{id}"] = design["paths"].pop("/orders/{order_id}")
    assert surface_drift(design, build_orders_app()).clean


@pytest.mark.unit
def test_openapi_cache_rebuilds_on_new_route_but_keeps_mutations() -> None:
    app = build_orders_app()
    first = app.openapi()
    first["info"]["x-patched"] = True
    assert app.openapi() is first

    @app.get("/late")
    def late() -> int:  # pyright: ignore[reportUnusedFunction]
        return 1

    second = app.openapi()
    assert second is not first
    assert "/late" in second["paths"]
    assert "x-patched" not in second["info"]


@pytest.mark.unit
def test_colliding_model_names_rename_the_existing_component() -> None:
    thing_a = create_model("Thing", __module__="pkg_a.models", x=(int, ...))
    thing_b = create_model("Thing", __module__="pkg_b.models", y=(str, ...))
    app = FastAPI(title="t", version="1")
    app.add_api_route("/a", lambda: {"x": 1}, response_model=thing_a)
    assert "Thing" in app.openapi()["components"]["schemas"]
    app.add_api_route("/b", lambda: {"y": "1"}, response_model=thing_b)
    names = set(app.openapi()["components"]["schemas"])
    assert {"pkg_a__models__Thing", "pkg_b__models__Thing"} <= names
    assert "Thing" not in names


@pytest.mark.unit
@pytest.mark.asyncio
async def test_served_document_matches_the_exported_snapshot() -> None:
    app = build_orders_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/openapi.json")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert render(response.json()) == render(build_orders_app().openapi())


@pytest.mark.unit
def test_orders_app_export_emits_no_fastapi_warnings() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        build_orders_app().openapi()
    assert [str(w.message) for w in caught] == []
