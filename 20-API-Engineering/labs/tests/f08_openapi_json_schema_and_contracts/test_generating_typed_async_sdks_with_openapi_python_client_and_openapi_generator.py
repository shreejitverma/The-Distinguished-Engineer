"""Generate an httpx SDK from a FastAPI OpenAPI document and call the provider with it."""

import ast
import copy
import importlib
import inspect
import os
import sys
from collections.abc import Iterator
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mypy import api as mypy_api

from api_labs.f08_openapi_json_schema_and_contracts.sdk_generation import (
    FIXED_CREATED_AT,
    LAB_BEARER,
    THROTTLED_ITEM_ID,
    GeneratedPackage,
    create_inventory_app,
    generate_client_package,
    imported_package,
    tree_digest,
)

PACKAGE = "inventory_client"
EXPECTED_FILES = frozenset(
    {
        "__init__.py",
        "api/__init__.py",
        "api/items/__init__.py",
        "api/items/create_item.py",
        "api/items/delete_item.py",
        "api/items/get_item.py",
        "api/items/list_items.py",
        "api/items/patch_item.py",
        "api/skus/__init__.py",
        "api/skus/get_sku.py",
        "client.py",
        "errors.py",
        "models/__init__.py",
        "models/http_validation_error.py",
        "models/item.py",
        "models/item_create.py",
        "models/item_patch.py",
        "models/item_status.py",
        "models/problem.py",
        "models/sku_echo.py",
        "models/validation_error.py",
        "models/validation_error_context.py",
        "types.py",
    }
)
OPERATION_MODULES = (
    "api.items.create_item",
    "api.items.delete_item",
    "api.items.get_item",
    "api.items.list_items",
    "api.items.patch_item",
    "api.skus.get_sku",
)
HOSTILE = '"""\nimport os\nos.environ["SDK_LAB_PWNED"] = "1"\n"""'
HOSTILE_LITERAL = '"; import os; os.environ["SDK_LAB_PWNED"] = "1"; x = "'
HOSTILE_ENUM = 'retired"\n    import os; os.environ["SDK_LAB_PWNED"] = "1"\n    X = "'

GOOD_CONSUMER = """\
from inventory_client import AuthenticatedClient, Client
from inventory_client.api.items import delete_item, get_item
from inventory_client.models import Item, Problem


async def quantity(client: Client, item_id: int) -> int:
    result = await get_item.asyncio(item_id, client=client)
    if isinstance(result, Item):
        return result.quantity
    if isinstance(result, Problem):
        return -result.status
    return 0


async def remove(client: AuthenticatedClient, item_id: int) -> None:
    await delete_item.asyncio_detailed(item_id, client=client)
"""
BAD_CONSUMER = """\
from inventory_client import Client
from inventory_client.api.items import delete_item, get_item


async def quantity(client: Client) -> int:
    result = await get_item.asyncio("one", client=client)
    return result.quantity


async def remove(client: Client) -> None:
    await delete_item.asyncio_detailed(1, client=client)
"""


class Sdk:
    """The generated package, imported for the lifetime of this test module."""

    def __init__(self, generated: GeneratedPackage, package: ModuleType) -> None:
        self.generated = generated
        self.package = package

    def module(self, dotted: str) -> Any:
        return importlib.import_module(f"{self.generated.name}.{dotted}")

    def client(self, app: FastAPI, **options: Any) -> Any:
        """A generated ``Client`` whose httpx client talks to ``app`` in-process."""
        client = self.package.Client(base_url="http://test", **options)
        client.set_async_httpx_client(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        )
        return client


def relative_files(directory: Path) -> frozenset[str]:
    # Importing the package writes __pycache__ beside it; that is not generator output.
    return frozenset(
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )


@pytest.fixture(scope="module")
def sdk(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Sdk]:
    root = tmp_path_factory.mktemp("sdk")
    generated = generate_client_package(create_inventory_app().openapi(), root, PACKAGE)
    with imported_package(generated) as package:
        yield Sdk(generated, package)


@pytest.mark.unit
def test_package_layout_maps_tags_to_packages_and_operation_ids_to_modules(sdk: Sdk) -> None:
    assert sdk.generated.errors == ()
    assert sdk.generated.warnings == ()
    # Exact equality also proves that --meta none writes no py.typed and no pyproject.toml.
    assert relative_files(sdk.generated.package_dir) == EXPECTED_FILES


@pytest.mark.unit
@pytest.mark.parametrize("dotted", OPERATION_MODULES)
def test_each_operation_module_exposes_sync_and_asyncio_call_styles(sdk: Sdk, dotted: str) -> None:
    module = sdk.module(dotted)
    for name in ("sync", "sync_detailed"):
        assert inspect.isfunction(getattr(module, name))
        assert not inspect.iscoroutinefunction(getattr(module, name))
    for name in ("asyncio", "asyncio_detailed"):
        assert inspect.iscoroutinefunction(getattr(module, name))


@pytest.mark.unit
def test_default_fastapi_operation_ids_leak_paths_into_module_names(tmp_path: Path) -> None:
    document = create_inventory_app(readable_operation_ids=False).openapi()
    assert document["paths"]["/items/{item_id}"]["get"]["operationId"] == (
        "get_item_items__item_id__get"
    )
    generated = generate_client_package(document, tmp_path, "default_ids_client")
    items = generated.package_dir / "api" / "items"
    assert sorted(p.name for p in items.glob("*.py") if p.name != "__init__.py") == [
        "create_item_items_post.py",
        "delete_item_items_item_id_delete.py",
        "get_item_items_item_id_get.py",
        "list_items_items_get.py",
        "patch_item_items_item_id_patch.py",
    ]


@pytest.mark.unit
def test_generation_is_byte_deterministic_and_tracks_the_document(tmp_path: Path) -> None:
    document = create_inventory_app().openapi()
    roots = [tmp_path / name for name in ("first", "second", "changed")]
    for root in roots:
        root.mkdir()
    first = generate_client_package(document, roots[0], PACKAGE)
    second = generate_client_package(document, roots[1], PACKAGE)
    assert tree_digest(first.package_dir) == tree_digest(second.package_dir)

    changed = copy.deepcopy(document)
    changed["components"]["schemas"]["Item"]["properties"]["bin"] = {"type": "string"}
    third = generate_client_package(changed, roots[2], PACKAGE)
    assert tree_digest(third.package_dir) != tree_digest(first.package_dir)
    model = (third.package_dir / "models" / "item.py").read_text()
    # "bin" shadows a builtin, so the attribute is renamed while the JSON key is kept.
    assert "bin_: str | Unset = UNSET" in model
    assert 'field_dict["bin"] = bin_' in model


@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_round_trip_parses_json_into_typed_attrs_models(sdk: Sdk) -> None:
    models = sdk.module("models")
    client = sdk.client(create_inventory_app())
    async with client:
        item = await sdk.module("api.items.get_item").asyncio(1, client=client)
        created = await sdk.module("api.items.create_item").asyncio(
            client=client, body=models.ItemCreate(sku="bolt-7", quantity=12)
        )
        active = await sdk.module("api.items.list_items").asyncio(
            client=client, status=models.ItemStatus.ACTIVE
        )
        retired = await sdk.module("api.items.list_items").asyncio(
            client=client, status=models.ItemStatus.RETIRED
        )

    assert isinstance(item, models.Item)
    assert (item.id, item.sku, item.quantity, item.note) == (1, "widget-1", 3, "seed")
    assert item.status is models.ItemStatus.ACTIVE
    assert isinstance(item.created_at, datetime)
    assert item.created_at == FIXED_CREATED_AT
    assert (created.id, created.sku, created.quantity, created.note) == (2, "bolt-7", 12, None)
    assert [entry.id for entry in active] == [1, 2]
    assert retired == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detailed_call_keeps_status_headers_and_raw_bytes(sdk: Sdk) -> None:
    client = sdk.client(create_inventory_app())
    async with client:
        response = await sdk.module("api.items.get_item").asyncio_detailed(1, client=client)

    assert response.status_code is HTTPStatus.OK
    assert response.headers["content-type"] == "application/json"
    assert response.content == (
        b'{"id":1,"sku":"widget-1","quantity":3,"status":"active",'
        b'"note":"seed","created_at":"2026-11-11T09:30:00Z"}'
    )
    assert response.parsed.id == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_documented_error_status_is_returned_as_a_model_not_raised(sdk: Sdk) -> None:
    models = sdk.module("models")
    client = sdk.client(create_inventory_app())
    async with client:
        missing = await sdk.module("api.items.get_item").asyncio(77, client=client)
        patched = await sdk.module("api.items.patch_item").asyncio(
            77, client=client, body=models.ItemPatch(quantity=1)
        )
        invalid = await sdk.module("api.items.create_item").asyncio_detailed(
            client=client, body=models.ItemCreate(sku="bolt-7", quantity=-1)
        )

    assert missing == models.Problem(title="Not Found", status=404, detail="item 77 does not exist")
    assert isinstance(patched, models.Problem)
    # The client sent quantity=-1: the ge=0 constraint exists only on the server.
    assert invalid.status_code is HTTPStatus.UNPROCESSABLE_ENTITY
    assert isinstance(invalid.parsed, models.HTTPValidationError)
    assert invalid.parsed.detail[0].loc == ["body", "quantity"]
    assert invalid.parsed.detail[0].type_ == "greater_than_equal"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_undocumented_status_is_none_by_default_and_raises_when_opted_in(sdk: Sdk) -> None:
    get_item = sdk.module("api.items.get_item")
    errors = sdk.module("errors")
    app = create_inventory_app()

    lenient = sdk.client(app)
    async with lenient:
        parsed = await get_item.asyncio(THROTTLED_ITEM_ID, client=lenient)
        detailed = await get_item.asyncio_detailed(THROTTLED_ITEM_ID, client=lenient)
    assert parsed is None
    assert detailed.status_code is HTTPStatus.TOO_MANY_REQUESTS
    assert detailed.parsed is None
    assert detailed.headers["retry-after"] == "7"

    strict = sdk.client(app, raise_on_unexpected_status=True)
    async with strict:
        with pytest.raises(errors.UnexpectedStatus) as caught:
            await get_item.asyncio(THROTTLED_ITEM_ID, client=strict)
    assert caught.value.status_code == 429
    assert caught.value.content == b'{"title":"Too Many Requests"}'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unset_and_none_are_distinct_on_the_wire(sdk: Sdk) -> None:
    models = sdk.module("models")
    patch_item = sdk.module("api.items.patch_item")
    assert models.ItemPatch().to_dict() == {}
    assert models.ItemPatch(note=None).to_dict() == {"note": None}
    assert models.ItemCreate(sku="s", quantity=1).to_dict() == {"sku": "s", "quantity": 1}

    client = sdk.client(create_inventory_app())
    async with client:
        untouched = await patch_item.asyncio(1, client=client, body=models.ItemPatch(quantity=9))
        cleared = await patch_item.asyncio(1, client=client, body=models.ItemPatch(note=None))
    assert (untouched.quantity, untouched.note) == (9, "seed")
    assert (cleared.quantity, cleared.note) == (9, None)


@pytest.mark.unit
def test_schema_defaults_are_frozen_into_the_client_and_none_query_values_are_dropped(
    sdk: Sdk,
) -> None:
    list_items = sdk.module("api.items.list_items")
    models = sdk.module("models")
    unset = sdk.module("types").UNSET

    # limit=50 is the provider's default at generation time; the client now sends it explicitly.
    assert list_items._get_kwargs()["params"] == {"limit": 50}
    assert list_items._get_kwargs(status=None, limit=unset)["params"] == {}
    assert list_items._get_kwargs(status=models.ItemStatus.RETIRED, limit=5)["params"] == {
        "status": "retired",
        "limit": 5,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_path_parameters_are_percent_encoded_as_one_segment(sdk: Sdk) -> None:
    get_sku = sdk.module("api.skus.get_sku")
    assert get_sku._get_kwargs(sku="a/b c?d#e")["url"] == "/skus/a%2Fb%20c%3Fd%23e"

    client = sdk.client(create_inventory_app())
    async with client:
        echoed = await get_sku.asyncio("a b#c?d", client=client)
        slash = await get_sku.asyncio_detailed("a/b", client=client)
    # "?" and "#" reach the handler as data instead of starting a query or a fragment.
    assert echoed.sku == "a b#c?d"
    # %2F is decoded before Starlette routes, so the segment splits and no route matches.
    assert slash.status_code is HTTPStatus.NOT_FOUND
    assert slash.content == b'{"detail":"Not Found"}'


@pytest.mark.unit
def test_from_dict_checks_presence_and_enums_but_not_types_or_constraints(sdk: Sdk) -> None:
    models = sdk.module("models")
    payload = {
        "id": "not-an-int",
        "sku": 7,
        "quantity": -5,
        "status": "active",
        "created_at": "2026-11-11T09:30:00Z",
        "warehouse": "ams-2",
    }
    loose = models.Item.from_dict(payload)
    assert (loose.id, loose.sku, loose.quantity) == ("not-an-int", 7, -5)
    assert loose.note is sdk.module("types").UNSET
    assert loose.additional_properties == {"warehouse": "ams-2"}
    assert loose.to_dict()["warehouse"] == "ams-2"

    with pytest.raises(KeyError, match="id"):
        models.Item.from_dict({"sku": "s"})
    with pytest.raises(ValueError, match="'archived' is not a valid ItemStatus"):
        models.Item.from_dict({**payload, "status": "archived"})
    with pytest.raises(TypeError, match="fromisoformat: argument must be str"):
        models.Item.from_dict({**payload, "created_at": 1_794_216_600})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_secured_operation_sends_bearer_only_through_the_owned_httpx_client(
    sdk: Sdk,
) -> None:
    delete_item = sdk.module("api.items.delete_item")
    client_parameter = inspect.signature(delete_item.asyncio_detailed).parameters["client"]
    assert client_parameter.annotation is sdk.package.AuthenticatedClient

    app = create_inventory_app()
    seen: list[str | None] = []

    async def record_authorization(request: httpx.Request) -> None:
        seen.append(request.headers.get("authorization"))

    def authenticated(token: str) -> Any:
        return sdk.package.AuthenticatedClient(
            base_url="http://test",
            token=token,
            httpx_args={
                "transport": httpx.ASGITransport(app=app),
                "event_hooks": {"request": [record_authorization]},
            },
        )

    owned = authenticated(LAB_BEARER)
    async with owned:
        deleted = await delete_item.asyncio_detailed(1, client=owned)
    assert deleted.status_code is HTTPStatus.NO_CONTENT
    assert seen == [f"Bearer {LAB_BEARER}"]

    wrong = authenticated("lab-bearer-WRONG-NOT-A-REAL-KEY")
    async with wrong:
        forbidden = await delete_item.asyncio_detailed(1, client=wrong)
    assert forbidden.status_code is HTTPStatus.FORBIDDEN

    # A caller-supplied httpx client replaces the one that would carry the Authorization header.
    replaced = sdk.package.AuthenticatedClient(base_url="http://test", token=LAB_BEARER)
    replaced.set_async_httpx_client(
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    )
    async with replaced:
        anonymous = await delete_item.asyncio_detailed(1, client=replaced)
    assert anonymous.status_code is HTTPStatus.UNAUTHORIZED
    assert anonymous.content == b'{"detail":"Not authenticated"}'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_client_disables_httpx_timeouts_and_redirects(sdk: Sdk) -> None:
    default = sdk.package.Client(base_url="http://test").get_async_httpx_client()
    bounded = sdk.package.Client(
        base_url="http://test", timeout=httpx.Timeout(2.0)
    ).get_async_httpx_client()
    try:
        assert default.timeout == httpx.Timeout(None)
        assert default.follow_redirects is False
        assert bounded.timeout == httpx.Timeout(2.0)
    finally:
        await default.aclose()
        await bounded.aclose()


@pytest.mark.unit
def test_openapi_3_2_document_is_rejected_before_any_file_is_written(tmp_path: Path) -> None:
    document = create_inventory_app().openapi()
    assert document["openapi"] == "3.1.0"
    document["openapi"] = "3.2.0"

    generated = generate_client_package(document, tmp_path, "v32_client")

    assert len(generated.errors) == 1
    assert "Only OpenAPI versions 3.1.* are supported, got 3.2.0" in generated.errors[0]
    assert not generated.package_dir.exists()


@pytest.mark.unit
def test_unsupported_response_media_type_is_dropped_with_a_warning(tmp_path: Path) -> None:
    document = create_inventory_app().openapi()
    ok = document["paths"]["/skus/{sku}"]["get"]["responses"]["200"]
    ok["content"] = {"application/xml": ok["content"]["application/json"]}

    generated = generate_client_package(document, tmp_path, "xml_client")

    assert generated.errors == ()
    assert len(generated.warnings) == 1
    assert generated.warnings[0].startswith("WARNING parsing GET /skus/{sku} within skus.")
    assert "Unsupported content_type application/xml" in generated.warnings[0]
    source = (generated.package_dir / "api" / "skus" / "get_sku.py").read_text()
    # The operation is still generated, but its 200 response is now an undocumented status.
    assert "response.status_code == 200" not in source
    assert "response.status_code == 422" in source


@pytest.mark.unit
def test_hostile_document_strings_are_escaped_and_never_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = create_inventory_app().openapi()
    document["info"]["title"] = "Inventory" + HOSTILE
    operation = document["paths"]["/items/{item_id}"]["get"]
    operation["summary"] = HOSTILE
    operation["description"] = HOSTILE
    item = document["components"]["schemas"]["Item"]
    item["description"] = HOSTILE
    item["properties"]["sku"]["description"] = HOSTILE
    item["properties"]["sku"]["default"] = HOSTILE_LITERAL
    document["components"]["schemas"]["ItemStatus"]["enum"] = ["active", HOSTILE_ENUM]
    monkeypatch.delenv("SDK_LAB_PWNED", raising=False)

    generated = generate_client_package(document, tmp_path, "hostile_client")
    assert generated.errors == ()

    imports: set[str] = set()
    for path in generated.package_dir.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
    assert "os" not in imports

    with imported_package(generated):
        for dotted in ("api.items.get_item", "models.item", "models.item_status"):
            importlib.import_module(f"hostile_client.{dotted}")
        models = importlib.import_module("hostile_client.models")
        # The payloads survive only as data: a default value and an enum value.
        assert models.Item.__attrs_attrs__.sku.default == HOSTILE_LITERAL.replace('"', '\\"')
        assert models.ItemStatus("active").name == "ACTIVE"
        assert len(list(models.ItemStatus)) == 2
    assert "SDK_LAB_PWNED" not in os.environ


@pytest.mark.unit
def test_imported_package_is_removed_from_sys_path_and_sys_modules(tmp_path: Path) -> None:
    generated = generate_client_package(create_inventory_app().openapi(), tmp_path, "scoped_client")
    with imported_package(generated) as package:
        assert package.__name__ == "scoped_client"
        assert "scoped_client.client" in sys.modules
        assert str(tmp_path) in sys.path
    assert [name for name in sys.modules if name.startswith("scoped_client")] == []
    assert str(tmp_path) not in sys.path


@pytest.mark.unit
def test_type_checker_rejects_calls_that_violate_the_contract(
    sdk: Sdk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = tmp_path / "good_consumer.py"
    bad = tmp_path / "bad_consumer.py"
    good.write_text(GOOD_CONSUMER, encoding="utf-8")
    bad.write_text(BAD_CONSUMER, encoding="utf-8")
    monkeypatch.setenv("MYPYPATH", str(sdk.generated.root))

    stdout, stderr, exit_code = mypy_api.run(
        [
            "--config-file=",
            "--strict",
            "--no-incremental",
            f"--cache-dir={tmp_path / 'mypy-cache'}",
            "--follow-imports=silent",
            "--no-error-summary",
            "--hide-error-context",
            "--no-color-output",
            "--python-executable",
            sys.executable,
            str(good),
            str(bad),
        ]
    )

    assert stderr == ""
    assert exit_code == 1
    errors = [line.removeprefix(f"{tmp_path}/") for line in stdout.splitlines()]
    assert not [line for line in errors if line.startswith("good_consumer.py")]
    assert (
        'bad_consumer.py:6: error: Argument 1 to "asyncio" has incompatible type "str"; '
        'expected "int"  [arg-type]'
    ) in errors
    assert (
        'bad_consumer.py:7: error: Item "None" of "HTTPValidationError | Item | Problem | None" '
        'has no attribute "quantity"  [union-attr]'
    ) in errors
    assert (
        'bad_consumer.py:11: error: Argument "client" to "asyncio_detailed" has incompatible '
        'type "Client"; expected "AuthenticatedClient"  [arg-type]'
    ) in errors
