"""OpenAPI drift gates for code-first and design-first workflows.

Code-first: export `app.openapi()`, canonicalize it, and compare it with a committed snapshot;
any difference fails with the RFC 6901 JSON Pointers that were added, removed, or changed.
Design-first: the committed document is the source of truth, and the operations the router
really serves must match it in both directions (nothing missing, nothing undocumented).
"""

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import BaseModel, Field

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]

# Arrays that JSON Schema defines as sets, so their element order carries no meaning.
SET_VALUED_KEYWORDS = frozenset({"required"})
# Path Item Object operation fields in OpenAPI 3.1; OpenAPI 3.2 adds `query`.
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace", "query")
_TEMPLATE_NAME = re.compile(r"\{[^{}]*\}")


class SpecDriftError(AssertionError):
    """The exported document differs from the committed snapshot."""

    def __init__(self, snapshot_path: Path, changes: list[str]) -> None:
        self.snapshot_path = snapshot_path
        self.changes = changes
        super().__init__(f"OpenAPI drift against {snapshot_path.name}:\n" + "\n".join(changes))


class SnapshotMissingError(AssertionError):
    """No snapshot is committed, so there is nothing to compare against."""


class UpdateRefusedError(RuntimeError):
    """An update was requested where only a human-reviewed commit may change the snapshot."""


@dataclass(frozen=True, slots=True)
class SurfaceDrift:
    """Operations that exist on only one side of a design-first contract."""

    missing: frozenset[str]
    undocumented: frozenset[str]

    @property
    def clean(self) -> bool:
        return not self.missing and not self.undocumented


def canonicalize(node: JsonValue, parent_key: str | None = None) -> JsonValue:
    """Sort object members recursively and sort only the arrays that are sets by definition."""
    if isinstance(node, dict):
        return {key: canonicalize(node[key], key) for key in sorted(node)}
    if isinstance(node, list):
        items = [canonicalize(item) for item in node]
        if parent_key in SET_VALUED_KEYWORDS:
            names = [item for item in items if isinstance(item, str)]
            if len(names) == len(items):
                return [*sorted(names)]
        return items
    return node


def render(document: JsonValue) -> str:
    """Canonical text: sorted members, two-space indent, ASCII escapes, one trailing newline."""
    return json.dumps(canonicalize(document), indent=2, ensure_ascii=True, allow_nan=False) + "\n"


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def pointer_diff(old: JsonValue, new: JsonValue, pointer: str = "") -> list[str]:
    """List the JSON Pointers that were removed (-), added (+), or changed (~)."""
    if isinstance(old, dict) and isinstance(new, dict):
        changes: list[str] = []
        for key in sorted(old.keys() | new.keys()):
            child = f"{pointer}/{_escape(key)}"
            if key not in new:
                changes.append(f"- {child}")
            elif key not in old:
                changes.append(f"+ {child}")
            else:
                changes.extend(pointer_diff(old[key], new[key], child))
        return changes
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        return [
            change
            for index, (before, after) in enumerate(zip(old, new, strict=True))
            for change in pointer_diff(before, after, f"{pointer}/{index}")
        ]
    return [] if old == new and type(old) is type(new) else [f"~ {pointer}"]


def check_snapshot(
    document: JsonValue, snapshot_path: Path, *, update: bool = False, ci: bool = False
) -> Literal["unchanged", "written"]:
    """Compare the document with the committed snapshot; rewrite it only on request, never in CI."""
    if update and ci:
        raise UpdateRefusedError("snapshot updates are disabled in CI; regenerate and commit")
    rendered = render(document)
    if update:
        snapshot_path.write_text(rendered, encoding="utf-8", newline="\n")
        return "written"
    if not snapshot_path.exists():
        raise SnapshotMissingError(f"{snapshot_path} is not committed; run the gate in update mode")
    committed = snapshot_path.read_text(encoding="utf-8")
    if committed == rendered:
        return "unchanged"
    old: JsonValue = json.loads(committed)
    changes = pointer_diff(canonicalize(old), canonicalize(document))
    raise SpecDriftError(snapshot_path, changes or ["~ (snapshot is not in canonical form)"])


def duplicate_operation_ids(document: JsonObject) -> list[str]:
    """Return every `operationId` used by more than one operation, sorted."""
    counts: Counter[str] = Counter()
    paths = document.get("paths")
    if isinstance(paths, dict):
        for path_item in paths.values():
            if not isinstance(path_item, dict):
                continue
            for method in HTTP_METHODS:
                operation = path_item.get(method)
                if isinstance(operation, dict):
                    operation_id = operation.get("operationId")
                    if isinstance(operation_id, str):
                        counts[operation_id] += 1
    return sorted(operation_id for operation_id, count in counts.items() if count > 1)


def _operation_key(method: str, path: str) -> str:
    # Templated paths that differ only in the template name are the same path.
    return f"{method.upper()} {_TEMPLATE_NAME.sub('{}', path)}"


def documented_surface(document: JsonObject) -> frozenset[str]:
    """Operations a document declares, as `METHOD /path/{}` keys."""
    surface: set[str] = set()
    paths = document.get("paths")
    if isinstance(paths, dict):
        for path, path_item in paths.items():
            if isinstance(path_item, dict):
                surface.update(_operation_key(m, path) for m in HTTP_METHODS if m in path_item)
    return frozenset(surface)


def implemented_surface(app: FastAPI) -> frozenset[str]:
    """Operations the router serves, including routes hidden with `include_in_schema=False`."""
    surface: set[str] = set()
    for context in iter_route_contexts(app.routes):
        path, methods = context.path_format, context.methods
        if isinstance(context.original_route, APIRoute) and path is not None and methods:
            surface.update(_operation_key(method, path) for method in methods)
    return frozenset(surface)


def surface_drift(design: JsonObject, app: FastAPI) -> SurfaceDrift:
    """Compare a design-first document with the operations the application really serves."""
    documented, implemented = documented_surface(design), implemented_surface(app)
    return SurfaceDrift(missing=documented - implemented, undocumented=implemented - documented)


class LineItem(BaseModel):
    sku: str
    quantity: int = Field(ge=1)


class OrderCreate(BaseModel):
    customer_id: str
    items: list[LineItem]


class Order(BaseModel):
    id: str
    customer_id: str
    items: list[LineItem]
    status: Literal["open", "shipped"]


@dataclass(slots=True)
class OrderStore:
    """In-memory orders; every build of the app below shares these two handlers' logic."""

    orders: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])

    def create(self, body: BaseModel) -> dict[str, object]:
        order_id = f"ord_{len(self.orders) + 1}"
        self.orders[order_id] = {"id": order_id, "status": "open", **body.model_dump()}
        return self.orders[order_id]

    def get(self, order_id: str) -> dict[str, object]:
        if order_id not in self.orders:
            raise HTTPException(status_code=404, detail="order not found")
        return self.orders[order_id]


def _assemble(
    store: OrderStore,
    create_order: Callable[..., dict[str, object]],
    order_model: type[BaseModel],
    *,
    reverse_registration: bool = False,
    hidden_route: bool = False,
) -> FastAPI:
    router = APIRouter(prefix="/orders", tags=["orders"])

    def add_create() -> None:
        router.add_api_route(
            "",
            create_order,
            methods=["POST"],
            status_code=201,
            response_model=order_model,
            operation_id="createOrder",
        )

    def add_get() -> None:
        router.add_api_route(
            "/{order_id}",
            store.get,
            methods=["GET"],
            response_model=order_model,
            operation_id="getOrder",
            responses={404: {"description": "Order not found"}},
        )

    for register in (add_get, add_create) if reverse_registration else (add_create, add_get):
        register()
    app = FastAPI(title="Orders API", version="1.0.0")
    app.include_router(router)
    if hidden_route:

        def reindex() -> dict[str, int]:
            return {"reindexed": len(store.orders)}

        app.add_api_route("/internal/reindex", reindex, methods=["POST"], include_in_schema=False)
    return app


def build_orders_app(*, hidden_route: bool = False) -> FastAPI:
    """The code-first application whose document is committed as the snapshot."""
    store = OrderStore()

    def create_order(body: OrderCreate) -> dict[str, object]:
        return store.create(body)

    return _assemble(store, create_order, Order, hidden_route=hidden_route)


def build_reordered_orders_app() -> FastAPI:
    """Same contract, different source order: fields reordered and routes registered in reverse."""
    store = OrderStore()

    class Order(BaseModel):
        status: Literal["open", "shipped"]
        items: list[LineItem]
        customer_id: str
        id: str

    def create_order(body: OrderCreate) -> dict[str, object]:
        return store.create(body)

    return _assemble(store, create_order, Order, reverse_registration=True)


def build_gift_note_orders_app() -> FastAPI:
    """One additive response field: `Order.gift_note`."""
    store = OrderStore()

    class Order(BaseModel):
        id: str
        customer_id: str
        items: list[LineItem]
        status: Literal["open", "shipped"]
        gift_note: str | None = None

    def create_order(body: OrderCreate) -> dict[str, object]:
        return store.create(body)

    return _assemble(store, create_order, Order)


def build_priced_orders_app() -> FastAPI:
    """One added `Decimal` field on the model shared by the request and the response."""
    store = OrderStore()

    class LineItem(BaseModel):
        sku: str
        quantity: int = Field(ge=1)
        unit_price: Decimal

    class OrderCreate(BaseModel):
        customer_id: str
        items: list[LineItem]

    class Order(BaseModel):
        id: str
        customer_id: str
        items: list[LineItem]
        status: Literal["open", "shipped"]

    def create_order(body: OrderCreate) -> dict[str, object]:
        return store.create(body)

    return _assemble(store, create_order, Order)
