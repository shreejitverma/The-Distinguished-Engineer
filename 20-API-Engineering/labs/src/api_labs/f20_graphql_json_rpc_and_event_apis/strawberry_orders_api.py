"""Strawberry 0.340.0 `GraphQLRouter` in FastAPI: typed context, union payloads, subscriptions.

One schema, four mechanisms worth proving:

* `@strawberry.type` / `@strawberry.input` classes become SDL types and input objects.
* `create_order` returns a union, so an expected domain failure is data
  (`InsufficientInventoryError`) and not an entry in the top-level `errors` array.
* `AppContext` subclasses `strawberry.fastapi.BaseContext`; the router overwrites `request`,
  `response` and `background_tasks` on whatever object the context getter returns, so the getter
  must build a fresh object per request (`LeakyContext` shows the cost of not doing so).
* `Subscription.order_events` is an async generator fed by `OrderBroker`; its `finally` block runs
  when the client sends `complete` or disconnects.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator  # noqa: TC003 - Strawberry resolves it at runtime
from typing import TYPE_CHECKING, Annotated, Any, Final, cast

import strawberry
from fastapi import FastAPI, Header
from strawberry.exceptions import ConnectionRejectionError
from strawberry.extensions import MaskErrors
from strawberry.fastapi import BaseContext, GraphQLRouter
from strawberry.types.unset import UNSET, UnsetType

if TYPE_CHECKING:
    from collections.abc import Sequence

WS_TOKEN: Final = "lab-ws-token-NOT-A-REAL-SECRET"  # noqa: S105 - lab placeholder, not a credential


class OrderBroker:
    """Fan-out of order events to bounded per-subscriber queues; the slowest reader loses events."""

    def __init__(self, maxsize: int = 16) -> None:
        self._maxsize = maxsize
        self._queues: set[asyncio.Queue[OrderCreated]] = set()
        self.dropped = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)

    def subscribe(self) -> asyncio.Queue[OrderCreated]:
        queue: asyncio.Queue[OrderCreated] = asyncio.Queue(maxsize=self._maxsize)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[OrderCreated]) -> None:
        self._queues.discard(queue)

    def publish(self, event: OrderCreated) -> None:
        for queue in self._queues:
            if queue.full():
                queue.get_nowait()  # drop the oldest event instead of blocking the publisher
                self.dropped += 1
            queue.put_nowait(event)


class Store:
    """In-memory inventory per tenant and the orders created so far."""

    def __init__(self) -> None:
        self.stock: dict[str, dict[str, int]] = {
            "acme": {"WIDGET": 5, "GADGET": 0},
            "globex": {"WIDGET": 1},
        }
        self.orders: list[Order] = []
        self.broker = OrderBroker()


class AppContext(BaseContext):
    """Per-request context; `request`, `response` and `background_tasks` come from `BaseContext`."""

    def __init__(self, store: Store, tenant: str) -> None:
        super().__init__()
        self.store = store
        self.tenant = tenant


@strawberry.type
class Order:
    id: strawberry.ID
    tenant: str
    sku: str
    quantity: int


@strawberry.input
class CreateOrderInput:
    sku: str
    quantity: int


@strawberry.type
class OrderCreated:
    order: Order


@strawberry.type
class InsufficientInventoryError:
    sku: str
    requested: int
    available: int


@strawberry.type
class QuantityInvalid:
    message: str


CreateOrderResult = Annotated[
    OrderCreated | InsufficientInventoryError | QuantityInvalid,
    strawberry.union("CreateOrderResult"),
]


def create_order(store: Store, tenant: str, data: CreateOrderInput) -> CreateOrderResult:
    """Domain logic with no GraphQL in it: validate, reserve stock, record, publish."""
    if data.quantity < 1:
        return QuantityInvalid(message="quantity must be >= 1")
    stock = store.stock.setdefault(tenant, {})
    available = stock.get(data.sku, 0)
    if available < data.quantity:
        return InsufficientInventoryError(
            sku=data.sku, requested=data.quantity, available=available
        )
    stock[data.sku] = available - data.quantity
    order = Order(
        id=strawberry.ID(f"ord-{len(store.orders) + 1}"),
        tenant=tenant,
        sku=data.sku,
        quantity=data.quantity,
    )
    store.orders.append(order)
    created = OrderCreated(order=order)
    store.broker.publish(created)
    return created


@strawberry.type
class Query:
    @strawberry.field
    def inventory(self, info: strawberry.Info[AppContext, None], sku: str) -> int:
        return info.context.store.stock.get(info.context.tenant, {}).get(sku, 0)

    @strawberry.field
    def orders(self, info: strawberry.Info[AppContext, None]) -> list[Order]:
        tenant = info.context.tenant
        return [o for o in info.context.store.orders if o.tenant == tenant]

    @strawberry.field
    def whoami(self, info: strawberry.Info[AppContext, None]) -> str:
        return info.context.tenant

    @strawberry.field
    def subscriber_count(self, info: strawberry.Info[AppContext, None]) -> int:
        return info.context.store.broker.subscriber_count

    @strawberry.field
    def request_path(self, info: strawberry.Info[AppContext, None]) -> str:
        request = info.context.request
        return request.url.path if request is not None else ""


@strawberry.type
class Mutation:
    @strawberry.mutation
    def create_order(
        self, info: strawberry.Info[AppContext, None], input: CreateOrderInput
    ) -> CreateOrderResult:
        ctx = info.context
        result = create_order(ctx.store, ctx.tenant, input)
        if isinstance(result, OrderCreated) and ctx.response is not None:
            ctx.response.headers["x-order-id"] = str(result.order.id)
        return result

    @strawberry.mutation
    def explode(self) -> str:
        raise RuntimeError("db password=hunter2 refused connection")


@strawberry.type
class Subscription:
    @strawberry.subscription
    async def order_events(
        self, info: strawberry.Info[AppContext, None], sku: str | None = None
    ) -> AsyncGenerator[OrderCreated, None]:
        broker = info.context.store.broker
        tenant = info.context.tenant
        queue = broker.subscribe()
        try:
            while True:
                event = await queue.get()
                if event.order.tenant == tenant and (sku is None or event.order.sku == sku):
                    yield event
        finally:
            broker.unsubscribe(queue)

    @strawberry.subscription
    async def countdown(self, start: int = 3) -> AsyncGenerator[int, None]:
        for value in range(start, 0, -1):
            yield value


class OrdersRouter(GraphQLRouter[AppContext]):
    """Adds token auth to `connection_init`: browsers cannot set headers on a WebSocket."""

    async def on_ws_connect(self, context: AppContext) -> UnsetType | dict[str, object] | None:
        params = context.connection_params
        token = cast("dict[str, object]", params).get("token") if isinstance(params, dict) else None
        if token != WS_TOKEN:
            raise ConnectionRejectionError({"reason": "bad token"})
        ack: UnsetType = UNSET
        return ack


def build_schema(*, mask_errors: bool = False) -> strawberry.Schema:
    extensions: list[Any] = [MaskErrors] if mask_errors else []
    return strawberry.Schema(
        query=Query, mutation=Mutation, subscription=Subscription, extensions=extensions
    )


def build_app(
    store: Store | None = None,
    *,
    mask_errors: bool = False,
    graphql_ide: str | None = "graphiql",
    allowed_origins: Sequence[str] = (),
    max_subscriptions_per_connection: int | None = 100,
) -> tuple[FastAPI, Store]:
    """Mount the router at `/graphql`; the context getter is a FastAPI dependency."""
    shared = store or Store()

    async def get_context(x_tenant: Annotated[str, Header()] = "acme") -> AppContext:
        return AppContext(shared, x_tenant)  # a fresh object per request

    router = OrdersRouter(
        build_schema(mask_errors=mask_errors),
        context_getter=get_context,
        graphql_ide=graphql_ide,  # type: ignore[arg-type]
        allowed_origins=allowed_origins,
        max_subscriptions_per_connection=max_subscriptions_per_connection,
    )
    app = FastAPI()
    app.include_router(router, prefix="/graphql")
    return app, shared


class LeakyContext(BaseContext):
    """Anti-pattern: one shared instance, so the router's per-request writes race."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()


@strawberry.type
class LeakyQuery:
    @strawberry.field
    async def tenant_after_gate(self, info: strawberry.Info[LeakyContext, None]) -> str:
        await info.context.gate.wait()
        request = info.context.request
        return request.headers.get("x-tenant", "?") if request is not None else "?"


def build_leaky_app(*, per_request: bool) -> tuple[FastAPI, LeakyContext]:
    """`per_request=False` returns one shared `LeakyContext` from the getter (the bug)."""
    shared = LeakyContext()

    def get_context() -> LeakyContext:
        if per_request:
            fresh = LeakyContext()
            fresh.gate = shared.gate  # same gate so the test can release both requests together
            return fresh
        return shared

    router = GraphQLRouter[LeakyContext](
        strawberry.Schema(query=LeakyQuery), context_getter=get_context
    )
    app = FastAPI()
    app.include_router(router, prefix="/graphql")
    return app, shared
