"""FastAPI 0.162.0 response pipeline: which branch runs, what it costs, and what it leaks."""

import json
import threading

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import ResponseValidationError
from fastapi.responses import JSONResponse

from api_labs.f11_fastapi_internals_and_patterns.response_pipeline import (
    BENCH_PATHS,
    PROBE,
    SECRET,
    build_counting_app,
    build_exposure_app,
    build_wire_app,
    run_asgi_benchmark,
    run_serializer_microbench,
)


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # (field validations, after-model validations, serializations)
        ("/dict", (1, 1, 1)),
        ("/model", (1, 2, 1)),
        ("/revalidate", (2, 2, 1)),
        ("/no-model", (1, 1, 1)),
        ("/prebuilt", (1, 1, 1)),
    ],
)
async def test_validation_and_serialization_counts_per_branch(
    path: str, expected: tuple[int, int, int]
) -> None:
    app = build_counting_app()
    async with client_for(app) as client:
        PROBE.reset()
        response = await client.get(path)
    assert response.status_code == 200
    assert response.json() == {"id": 7, "name": "widget", "price": 9.5}
    assert PROBE.counts() == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_endpoint_validates_in_threadpool_but_serializes_on_loop() -> None:
    app = build_counting_app()
    loop_thread = threading.get_ident()
    async with client_for(app) as client:
        PROBE.reset()
        sync_response = await client.get("/sync-dict")
        sync_validator, sync_serializer = PROBE.validator_threads[:], PROBE.serializer_threads[:]
        PROBE.reset()
        async_response = await client.get("/dict")
    assert sync_response.status_code == async_response.status_code == 200
    assert len(sync_validator) == len(sync_serializer) == 1
    assert sync_validator[0] != loop_thread
    assert sync_serializer[0] == loop_thread
    assert PROBE.validator_threads == [loop_thread]
    assert PROBE.serializer_threads == [loop_thread]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fast_path_writes_nan_as_null_and_skips_json_dumps() -> None:
    async with client_for(build_wire_app()) as client:
        response = await client.get("/reading")
    assert response.status_code == 200
    assert response.content == b'{"sensor":"t1","value":null}'
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-length"] == str(len(response.content))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_response_class_falls_back_to_json_dumps_and_rejects_nan() -> None:
    async with client_for(build_wire_app()) as client:
        with pytest.raises(ValueError, match="Out of range float values are not JSON compliant"):
            await client.get("/reading-explicit-json")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_level_default_response_class_disables_fast_path() -> None:
    async with client_for(build_wire_app(default_response_class=JSONResponse)) as client:
        with pytest.raises(ValueError, match="Out of range float values are not JSON compliant"):
            await client.get("/reading")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_annotation_choice_changes_decimal_and_datetime_wire_format() -> None:
    async with client_for(build_wire_app()) as client:
        model = await client.get("/money-model")
        any_ann = await client.get("/money-any")
        none_ann = await client.get("/money-no-model")
    assert model.content == b'{"amount":"12345678901234567.89","at":"2026-11-01T12:00:00Z"}'
    assert any_ann.content == model.content
    assert none_ann.content == b'{"amount":1.2345678901234568e+16,"at":"2026-11-01T12:00:00+00:00"}'
    # decimal_encoder() turned the Decimal into a binary64 float: the cents are gone.
    assert json.loads(none_ann.content)["amount"] == 12345678901234568.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_response_model_filters_subclass_and_reads_orm_attributes() -> None:
    async with client_for(build_exposure_app()) as client:
        subclass = await client.get("/subclass")
        orm = await client.get("/orm")
    assert subclass.json() == {"id": 1, "email": "ada@example.test"}
    assert orm.json() == {"id": 1, "email": "ada@example.test"}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/orm-no-model", "/bypass", "/polymorphic"])
async def test_paths_that_leak_fields_response_model_was_meant_to_hide(path: str) -> None:
    async with client_for(build_exposure_app()) as client:
        response = await client.get(path)
    body = response.json()
    assert response.status_code == 200
    assert body["hashed_password"] == SECRET
    assert "_sa_instance_state" not in body


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_return_value_raises_response_validation_error_with_input() -> None:
    async with client_for(build_exposure_app()) as client:
        with pytest.raises(ResponseValidationError) as excinfo:
            await client.get("/broken")
    errors = excinfo.value.errors()
    assert [(e["type"], e["loc"]) for e in errors] == [("missing", ("response", "email"))]
    assert SECRET in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_return_value_is_an_opaque_500_to_the_client() -> None:
    transport = httpx.ASGITransport(app=build_exposure_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/broken")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert SECRET not in response.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_benchmark_branches_emit_identical_bytes() -> None:
    results = await run_asgi_benchmark(item_count=20, requests_per_round=2, rounds=1)
    assert [r.path for r in results] == list(BENCH_PATHS)
    assert results[0].ratio_to_prebuilt == 1.0
    assert all(r.median_us > 0 and r.body_bytes == results[0].body_bytes for r in results)


@pytest.mark.unit
def test_serializer_microbench_candidates_emit_identical_bytes() -> None:
    micro = run_serializer_microbench(item_count=20, loops=2, rounds=1)
    assert set(micro) == {
        "dump_json(models)",
        "validate_python + dump_json",
        "validate_python + dump_python(json) + json.dumps",
        "jsonable_encoder + json.dumps",
    }
    assert all(us > 0 for us in micro.values())
