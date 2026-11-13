"""Generate a typed httpx client package from a FastAPI OpenAPI document.

The provider is a small FastAPI inventory service.  Its OpenAPI 3.1 document is fed to
openapi-python-client 0.29.1, which renders an importable package of attrs models and
one module per operation.  Nothing here opens a socket: generation reads a local file and
the tests drive the generated client through ``httpx.ASGITransport``.
"""

import hashlib
import importlib
import json
import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from openapi_python_client import generate
from openapi_python_client.config import Config, ConfigFile, MetaType
from openapi_python_client.parser.errors import ErrorLevel
from pydantic import BaseModel, Field

FIXED_CREATED_AT = datetime(2026, 11, 11, 9, 30, tzinfo=UTC)
LAB_BEARER = "lab-bearer-EXAMPLE-NOT-A-REAL-KEY"
THROTTLED_ITEM_ID = 429


class ItemStatus(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


class ItemCreate(BaseModel):
    sku: str
    quantity: int = Field(ge=0)
    note: str | None = None


class ItemPatch(BaseModel):
    quantity: int | None = Field(default=None, ge=0)
    note: str | None = None


class Item(BaseModel):
    id: int
    sku: str
    quantity: int
    status: ItemStatus
    note: str | None = None
    created_at: datetime


class Problem(BaseModel):
    title: str
    status: int
    detail: str


class SkuEcho(BaseModel):
    sku: str


def operation_id_from_route_name(route: APIRoute) -> str:
    """Use the handler name as the operationId, so the SDK function is ``get_item``."""
    return route.name


def _not_found(item_id: int) -> JSONResponse:
    problem = Problem(title="Not Found", status=404, detail=f"item {item_id} does not exist")
    return JSONResponse(status_code=404, content=problem.model_dump())


def create_inventory_app(*, readable_operation_ids: bool = True) -> FastAPI:
    """Build the provider.  ``readable_operation_ids=False`` keeps FastAPI's default ids."""
    if readable_operation_ids:
        app = FastAPI(
            title="Inventory",
            version="1.4.0",
            generate_unique_id_function=operation_id_from_route_name,
        )
    else:
        app = FastAPI(title="Inventory", version="1.4.0")

    bearer = HTTPBearer()
    items: dict[int, Item] = {
        1: Item(
            id=1,
            sku="widget-1",
            quantity=3,
            status=ItemStatus.ACTIVE,
            note="seed",
            created_at=FIXED_CREATED_AT,
        )
    }

    @app.post("/items", status_code=201, tags=["items"])
    async def create_item(body: ItemCreate) -> Item:
        item = Item(
            id=max(items) + 1,
            sku=body.sku,
            quantity=body.quantity,
            status=ItemStatus.ACTIVE,
            note=body.note,
            created_at=FIXED_CREATED_AT,
        )
        items[item.id] = item
        return item

    @app.get("/items", tags=["items"])
    async def list_items(
        status: Annotated[ItemStatus | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[Item]:
        matching = [item for item in items.values() if status is None or item.status == status]
        return matching[:limit]

    @app.get(
        "/items/{item_id}",
        tags=["items"],
        response_model=Item,
        responses={404: {"model": Problem}},
    )
    async def get_item(item_id: int) -> Item | JSONResponse:
        if item_id == THROTTLED_ITEM_ID:
            # A gateway-style status that the OpenAPI document does not describe.
            return JSONResponse(
                status_code=429,
                content={"title": "Too Many Requests"},
                headers={"Retry-After": "7"},
            )
        item = items.get(item_id)
        return item if item is not None else _not_found(item_id)

    @app.patch(
        "/items/{item_id}",
        tags=["items"],
        response_model=Item,
        responses={404: {"model": Problem}},
    )
    async def patch_item(item_id: int, body: ItemPatch) -> Item | JSONResponse:
        item = items.get(item_id)
        if item is None:
            return _not_found(item_id)
        # Only keys present in the JSON body change the item: absent is not null.
        updated = item.model_copy(update=body.model_dump(exclude_unset=True))
        items[item_id] = updated
        return updated

    @app.delete("/items/{item_id}", status_code=204, tags=["items"])
    async def delete_item(
        item_id: int,
        credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    ) -> None:
        if credentials.credentials != LAB_BEARER:
            raise HTTPException(status_code=403, detail="wrong bearer")
        items.pop(item_id, None)

    @app.get("/skus/{sku}", tags=["skus"])
    async def get_sku(sku: str) -> SkuEcho:
        return SkuEcho(sku=sku)

    return app


@dataclass(frozen=True, slots=True)
class GeneratedPackage:
    """Where a generated client package lives and what the generator reported."""

    root: Path
    name: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def package_dir(self) -> Path:
        return self.root / self.name


def generate_client_package(document: Mapping[str, Any], root: Path, name: str) -> GeneratedPackage:
    """Render ``document`` into the importable package ``root / name``.

    ``MetaType.NONE`` writes only the package (no ``pyproject.toml``, no ``py.typed``), which
    is the layout for vendoring a client inside an existing distribution.  ``post_hooks=[]``
    skips the default ``ruff`` subprocesses so that generation stays in-process.
    """
    source = root / f"{name}.openapi.json"
    source.write_text(json.dumps(document, indent=2), encoding="utf-8")
    config = Config.from_sources(
        ConfigFile(package_name_override=name, post_hooks=[]),
        MetaType.NONE,
        document_source=source,
        file_encoding="utf-8",
        overwrite=True,
        output_path=root / name,
    )
    errors: list[str] = []
    warnings: list[str] = []
    for problem in generate(config=config):
        message = f"{problem.header} {problem.detail or ''}".strip()
        (errors if problem.level is ErrorLevel.ERROR else warnings).append(message)
    return GeneratedPackage(root=root, name=name, errors=tuple(errors), warnings=tuple(warnings))


def tree_digest(directory: Path) -> str:
    """SHA-256 over every file path and file body under ``directory``, in sorted order."""
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@contextmanager
def imported_package(generated: GeneratedPackage) -> Generator[ModuleType]:
    """Import a generated package, then remove it from ``sys.path`` and ``sys.modules``."""
    root = str(generated.root)
    sys.path.insert(0, root)
    importlib.invalidate_caches()
    try:
        yield importlib.import_module(generated.name)
    finally:
        sys.path.remove(root)
        prefix = f"{generated.name}."
        for module_name in [m for m in sys.modules if m == generated.name or m.startswith(prefix)]:
            del sys.modules[module_name]
