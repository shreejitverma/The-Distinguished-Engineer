"""Unit tests for APIRoute signature compilation, parameter classification, and body merging."""

from typing import Annotated, Any

import fastapi.dependencies.utils as dependency_utils
import httpx
import pytest
from fastapi import Depends, FastAPI, Path, Query
from fastapi.dependencies.utils import get_dependant, get_flat_params
from fastapi.routing import APIRoute, iter_route_contexts

from api_labs.f11_fastapi_internals_and_patterns.apiroute_compilation import (
    BodyPlan,
    Page,
    ParamLayout,
    body_plan,
    build_body_app,
    build_forward_ref_trap_app,
    build_prefixed_router,
    catalog_endpoint,
    compile_layout,
    describe_dependant,
    route_by_path,
    type_checking_only_dependency,
)

type PositiveLimit = Annotated[int, Query(ge=1, le=100)]


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
def test_signature_partitions_into_location_buckets() -> None:
    layout = compile_layout("/items/{item_id}", catalog_endpoint)

    assert layout == ParamLayout(
        call="catalog_endpoint",
        path=("item_id",),
        query=("sizes", "page", "verbose"),
        header=(),
        cookie=("session",),
        body=("item", "tags"),
        injected=(
            ("request", "request"),
            ("response", "response"),
            ("background_tasks", "background"),
        ),
        dependencies=(
            ParamLayout(
                call="common_filters",
                path=(),
                query=("q",),
                header=(("x_trace_id", "x-trace-id"),),
                cookie=(),
                body=("actor",),
                injected=(),
                dependencies=(),
            ),
        ),
    )


@pytest.mark.unit
def test_unannotated_list_is_body_but_query_marker_makes_it_query() -> None:
    dependant = get_dependant(path="/items/{item_id}", call=catalog_endpoint)
    fields = {f.name: f for f in dependant.body_params + dependant.query_params}

    assert type(fields["tags"].field_info).__name__ == "Body"
    assert type(fields["sizes"].field_info).__name__ == "Query"
    assert type(fields["verbose"].field_info).__name__ == "Query"


@pytest.mark.unit
def test_query_parameter_model_is_flattened_only_by_get_flat_params() -> None:
    dependant = get_dependant(path="/items/{item_id}", call=catalog_endpoint)
    flat_names = [f.name for f in get_flat_params(dependant)]

    assert [f.name for f in dependant.query_params] == ["sizes", "page", "verbose"]
    assert flat_names == [
        "item_id",
        "sizes",
        *Page.model_fields,
        "verbose",
        "q",
        "x_trace_id",
        "session",
    ]


@pytest.mark.unit
def test_get_dependant_needs_path_format_not_the_convertor_template() -> None:
    raw = compile_layout("/single/{item_id:int}", catalog_endpoint)
    route = route_by_path(build_body_app(), "/single/{item_id:int}")

    assert "item_id" not in raw.path
    assert raw.query[0] == "item_id"
    assert route.path_format == "/single/{item_id}"
    assert describe_dependant(route.dependant).path == ("item_id",)


@pytest.mark.unit
def test_single_body_param_is_the_whole_body() -> None:
    plan = body_plan(route_by_path(build_body_app(), "/single/{item_id:int}"))

    assert plan == BodyPlan(
        embed=False,
        field_name="item",
        field_alias="item",
        model="Item",
        model_fields=("name", "price"),
        field_info_class="Body",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "model", "model_fields"),
    [
        ("/multi", "Body_multi_multi_post", ("item", "user")),
        ("/embedded", "Body_embedded_embedded_post", ("item",)),
        ("/via-dependency", "Body_via_dependency_via_dependency_post", ("item", "actor")),
    ],
)
def test_embedding_merges_body_params_into_synthetic_model(
    path: str, model: str, model_fields: tuple[str, ...]
) -> None:
    plan = body_plan(route_by_path(build_body_app(), path))

    assert plan == BodyPlan(
        embed=True,
        field_name="body",
        field_alias="body",
        model=model,
        model_fields=model_fields,
        field_info_class="Body",
    )


@pytest.mark.unit
def test_same_body_name_in_endpoint_and_dependency_counts_once() -> None:
    route = route_by_path(build_body_app(), "/same-name")
    layout = describe_dependant(route.dependant)

    assert layout.body == ("actor",)
    assert layout.dependencies[0].body == ("actor",)
    assert body_plan(route) == BodyPlan(
        embed=False,
        field_name="actor",
        field_alias="actor",
        model="User",
        model_fields=("username",),
        field_info_class="Body",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wire_shape_follows_the_embed_decision() -> None:
    item = {"name": "pen", "price": 1.5}
    async with _client(build_body_app()) as client:
        single = await client.post("/single/7", json=item)
        single_wrapped = await client.post("/single/7", json={"item": item})
        multi = await client.post("/multi", json={"item": item, "user": {"username": "ada"}})
        multi_flat = await client.post("/multi", json=item)
        embedded = await client.post("/embedded", json={"item": item})
        same = await client.post("/same-name", json={"username": "ada"})

    assert (single.status_code, single.json()) == (200, {"item_id": 7, "name": "pen"})
    assert single_wrapped.status_code == 422
    assert (multi.status_code, multi.json()) == (200, {"name": "pen", "user": "ada"})
    assert multi_flat.status_code == 422
    assert [e["loc"] for e in multi_flat.json()["detail"]] == [["body", "item"], ["body", "user"]]
    assert (embedded.status_code, embedded.json()) == (200, {"name": "pen"})
    assert same.json() == {"direct": "ada", "from_dependency": "ada"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_include_prefix_recompiles_query_param_into_path_param() -> None:
    router, app = build_prefixed_router()
    original = router.routes[0]
    assert isinstance(original, APIRoute)
    effective = [ctx for ctx in iter_route_contexts(app.router.routes) if ctx.path_format]
    orders = next(ctx for ctx in effective if ctx.path_format == "/users/{user_id}/orders")

    assert describe_dependant(original.dependant).query == ("user_id",)
    assert describe_dependant(orders.dependant).path == ("user_id",)
    assert orders.dependant is not original.dependant
    async with _client(app) as client:
        response = await client.get("/users/42/orders")
    assert (response.status_code, response.json()) == (200, {"user_id": 42})


@pytest.mark.unit
def test_invalid_signatures_fail_at_decoration_time() -> None:
    app = FastAPI()

    with pytest.raises(AssertionError, match="Path parameters cannot have default values"):

        @app.get("/a/{item_id}")
        def path_default(item_id: Annotated[int, Path()] = 3) -> None: ...

    with pytest.raises(AssertionError, match="default value cannot be set in `Annotated`"):

        @app.get("/b")
        def annotated_default(q: Annotated[int, Query(default=1)]) -> None: ...

    def dep() -> int:
        return 1

    with pytest.raises(AssertionError, match="Cannot specify `Depends` in `Annotated`"):

        @app.get("/c")
        def double_depends(
            v: Annotated[int, Depends(dep)] = Depends(dep),  # noqa: FAST002
        ) -> None: ...

    assert [r.path for r in app.routes if isinstance(r, APIRoute)] == []


@pytest.mark.unit
def test_pep695_type_alias_is_unwrapped_before_classification() -> None:
    def endpoint(limit: PositiveLimit = 10) -> None: ...

    dependant = get_dependant(path="/", call=endpoint)
    (field,) = dependant.query_params

    assert field.name == "limit"
    assert type(field.field_info).__name__ == "Query"
    assert field.field_info.default == 10


@pytest.mark.unit
def test_type_checking_only_annotation_falls_back_instead_of_raising() -> None:
    layout = compile_layout("/", type_checking_only_dependency)

    assert layout.query == ("q",)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unresolvable_forward_ref_silently_becomes_a_query_param() -> None:
    app = build_forward_ref_trap_app()
    route = route_by_path(app, "/trap")
    layout = describe_dependant(route.dependant)

    assert (layout.query, layout.body) == (("item",), ())
    assert route.body_field is None
    async with _client(app) as client:
        response = await client.post("/trap", json={"name": "pen"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "item"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_any_dependency_override_recompiles_every_sub_dependant_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real_get_dependant = dependency_utils.get_dependant

    def counting_get_dependant(**kwargs: Any) -> Any:
        calls.append(getattr(kwargs["call"], "__name__", "?"))
        return real_get_dependant(**kwargs)

    def first() -> int:
        return 1

    def second() -> int:
        return 2

    def unrelated() -> int:
        return 0

    app = FastAPI()

    @app.get("/sum")
    def total(
        a: Annotated[int, Depends(first)], b: Annotated[int, Depends(second)]
    ) -> dict[str, int]:
        return {"sum": a + b}

    monkeypatch.setattr(dependency_utils, "get_dependant", counting_get_dependant)
    async with _client(app) as client:
        await client.get("/sum")
        assert calls == []
        app.dependency_overrides[unrelated] = lambda: 0
        await client.get("/sum")
        await client.get("/sum")

    assert calls == ["first", "second", "first", "second"]
