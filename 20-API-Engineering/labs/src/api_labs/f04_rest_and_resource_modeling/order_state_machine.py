"""AIP-136 custom methods driving an AIP-216 state machine for an order resource.

Routes:
    POST /v1/orders?order_id=ID       create; `state` is output only (AIP-203)
    GET  /v1/orders/{order_id}        read
    POST /v1/orders/{order_id}:pay      PENDING_PAYMENT -> PAID
    POST /v1/orders/{order_id}:ship     PAID -> SHIPPED
    POST /v1/orders/{order_id}:deliver  SHIPPED -> DELIVERED
    POST /v1/orders/{order_id}:cancel   PENDING_PAYMENT | PAID -> CANCELLED

Errors use the AIP-193 HTTP/1.1+JSON shape: {"error": {"code", "status", "message", "details"}}.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from fastapi import Body, FastAPI, Path, Query
from fastapi.responses import JSONResponse
from pydantic import UUID4, BaseModel, ConfigDict


class State(StrEnum):
    """Order lifecycle states; output only, changed only by custom methods (AIP-216)."""

    PENDING_PAYMENT = "PENDING_PAYMENT"
    PAID = "PAID"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


Verb = Literal["pay", "ship", "deliver", "cancel"]
VERBS: Final[tuple[Verb, ...]] = ("pay", "ship", "deliver", "cancel")

# verb -> (states the verb may be called from, state after the verb)
TRANSITIONS: Final[dict[Verb, tuple[frozenset[State], State]]] = {
    "pay": (frozenset({State.PENDING_PAYMENT}), State.PAID),
    "ship": (frozenset({State.PAID}), State.SHIPPED),
    "deliver": (frozenset({State.SHIPPED}), State.DELIVERED),
    "cancel": (frozenset({State.PENDING_PAYMENT, State.PAID}), State.CANCELLED),
}

# 4-63 chars of [a-z0-9-], starting with a letter: no ':' so the verb suffix is unambiguous.
ORDER_ID_PATTERN: Final = r"^[a-z][a-z0-9-]{3,62}$"
DOMAIN: Final = "orders.example.com"


class InvalidTransitionError(Exception):
    """Raised when a verb is called from a state that does not allow it."""

    def __init__(self, verb: Verb, state: State) -> None:
        super().__init__(f"cannot {verb} an order in state {state}")
        self.verb = verb
        self.state = state


def next_state(state: State, verb: Verb) -> State:
    """Pure transition function: the whole lifecycle is the TRANSITIONS table."""
    allowed_from, target = TRANSITIONS[verb]
    if state not in allowed_from:
        raise InvalidTransitionError(verb, state)
    return target


class ApiError(Exception):
    """An error rendered as an AIP-193 google.rpc.Status JSON body."""

    def __init__(
        self,
        code: int,
        status: str,
        message: str,
        *,
        reason: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.reason = reason
        self.metadata = metadata or {}

    def response(self) -> JSONResponse:
        error: dict[str, Any] = {"code": self.code, "status": self.status, "message": self.message}
        if self.reason is not None:
            error["details"] = [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": self.reason,
                    "domain": DOMAIN,
                    "metadata": self.metadata,
                }
            ]
        return JSONResponse({"error": error}, status_code=self.code)


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    state: State
    revision: int

    @property
    def etag(self) -> str:
        return f'"{self.revision}"'

    def to_json(self) -> dict[str, str]:
        return {"name": f"orders/{self.order_id}", "state": self.state, "etag": self.etag}


@dataclass(frozen=True, slots=True)
class _Replay:
    fingerprint: tuple[Verb, str]
    body: dict[str, str]


@dataclass
class OrderStore:
    """In-memory store; `apply` has no await point, so it is atomic on one event loop."""

    orders: dict[str, Order] = field(default_factory=dict[str, Order])
    replays: dict[str, _Replay] = field(default_factory=dict[str, _Replay])
    transitions_applied: int = 0

    def create(self, order_id: str) -> Order:
        if order_id in self.orders:
            raise ApiError(409, "ALREADY_EXISTS", f"orders/{order_id} already exists")
        order = Order(order_id, State.PENDING_PAYMENT, 1)
        self.orders[order_id] = order
        return order

    def get(self, order_id: str) -> Order:
        order = self.orders.get(order_id)
        if order is None:
            raise ApiError(404, "NOT_FOUND", f"orders/{order_id} not found")
        return order

    def apply(
        self, order_id: str, verb: Verb, *, request_id: str | None, etag: str | None
    ) -> dict[str, str]:
        # 1. Replay first: a retried success must not be re-checked against the new state.
        if request_id is not None:
            prior = self.replays.get(request_id)
            if prior is not None:
                if prior.fingerprint != (verb, order_id):
                    raise ApiError(
                        400,
                        "INVALID_ARGUMENT",
                        "request_id was already used for a different request",
                        reason="REQUEST_ID_REUSED",
                        metadata={"requestId": request_id},
                    )
                return prior.body
        order = self.get(order_id)
        # 2. Optimistic concurrency (AIP-154): a stale etag is ABORTED, retry at a higher level.
        if etag is not None and etag != order.etag:
            raise ApiError(
                409,
                "ABORTED",
                f"etag {etag} does not match current etag {order.etag}",
                reason="ETAG_MISMATCH",
                metadata={"currentEtag": order.etag},
            )
        # 3. State machine (AIP-216): a disallowed transition is FAILED_PRECONDITION.
        try:
            target = next_state(order.state, verb)
        except InvalidTransitionError as exc:
            allowed_from = ",".join(sorted(TRANSITIONS[verb][0]))
            raise ApiError(
                400,
                "FAILED_PRECONDITION",
                str(exc),
                reason="INVALID_STATE_TRANSITION",
                metadata={"state": order.state, "verb": verb, "allowedFrom": allowed_from},
            ) from exc
        updated = Order(order_id, target, order.revision + 1)
        self.orders[order_id] = updated
        self.transitions_applied += 1
        body = updated.to_json()
        if request_id is not None:
            self.replays[request_id] = _Replay((verb, order_id), body)
        return body


class TransitionRequest(BaseModel):
    """Body of every state transition custom method (`body: "*"`)."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID4 | None = None
    etag: str | None = None


class OrderInput(BaseModel):
    """Create body; `state` is OUTPUT_ONLY, so it is accepted and ignored (AIP-203)."""

    state: str | None = None


OrderId = Annotated[str, Path(pattern=ORDER_ID_PATTERN)]


def _transition_endpoint(store: OrderStore, verb: Verb) -> Any:
    async def endpoint(
        order_id: OrderId,
        body: Annotated[TransitionRequest | None, Body()] = None,
    ) -> JSONResponse:
        request = body or TransitionRequest()
        request_id = None if request.request_id is None else str(request.request_id)
        try:
            result = store.apply(order_id, verb, request_id=request_id, etag=request.etag)
        except ApiError as exc:
            return exc.response()
        return JSONResponse(result)

    endpoint.__name__ = f"{verb}_order"
    return endpoint


def build_app(store: OrderStore) -> FastAPI:
    """Register custom methods first: a later same-method `/{order_id}` route cannot shadow them."""
    app = FastAPI()
    for verb in VERBS:
        app.add_api_route(
            f"/v1/orders/{{order_id}}:{verb}",
            _transition_endpoint(store, verb),
            methods=["POST"],
            name=f"orders:{verb}",
            response_model=None,
        )

    @app.post("/v1/orders", response_model=None)
    async def create_order(
        order_id: Annotated[str, Query(pattern=ORDER_ID_PATTERN)],
        body: Annotated[OrderInput | None, Body()] = None,
    ) -> JSONResponse:
        try:
            order = store.create(order_id)
        except ApiError as exc:
            return exc.response()
        return JSONResponse(
            order.to_json(), status_code=201, headers={"Location": f"/v1/orders/{order_id}"}
        )

    @app.get("/v1/orders/{order_id}", response_model=None)
    async def get_order(order_id: OrderId) -> JSONResponse:
        try:
            return JSONResponse(store.get(order_id).to_json())
        except ApiError as exc:
            return exc.response()

    return app
