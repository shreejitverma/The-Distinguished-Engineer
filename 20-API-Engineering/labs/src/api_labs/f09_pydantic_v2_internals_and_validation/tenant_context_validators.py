"""Context-dependent cross-field invariants with @model_validator and ValidationInfo.context."""

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Literal, Self, cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    ValidationError,
    ValidationInfo,
    model_validator,
)
from pydantic_core import PydanticCustomError

MANUAL_CAPTURE = "payments:manual_capture"
REFUND_WINDOWS_FLAG = "refund_windows"


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Per-request runtime state passed as `context=`; never part of the payload."""

    tenant_id: str
    permissions: frozenset[str]
    flags: frozenset[str]
    max_amount_cents: int
    events: list[str] = field(default_factory=list[str])


def _tenant_context(info: ValidationInfo) -> TenantContext:
    """Return the TenantContext or fail as a ValidationError (not a crash) when it is absent."""
    ctx = info.context
    if not isinstance(ctx, TenantContext):
        raise PydanticCustomError(
            "missing_context",
            "validation requires a TenantContext, got {got}",
            {"got": type(ctx).__name__},
        )
    return ctx


class PaymentIntent(BaseModel):
    """Payment request whose invariants depend on who is asking, not only on the payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    amount_cents: int = Field(gt=0)
    currency: Literal["USD", "EUR", "JPY"]
    capture: Literal["automatic", "manual"] = "automatic"
    refund_window_days: int = Field(default=0, ge=0, le=180)

    @model_validator(mode="before")
    @classmethod
    def _stamp_tenant(cls, data: Any, info: ValidationInfo) -> Any:
        """Default `tenant_id` from the authenticated context, on a copy of the input."""
        ctx = _tenant_context(info)
        ctx.events.append("before")
        if not isinstance(data, Mapping):
            return data  # the model validator rejects non-mappings with `model_type`
        stamped = dict(cast("Mapping[str, object]", data))
        stamped.setdefault("tenant_id", ctx.tenant_id)
        return stamped

    @model_validator(mode="after")
    def _enforce_tenant_policy(self, info: ValidationInfo) -> Self:
        """Cross-field and context-dependent invariants over fully typed fields."""
        ctx = _tenant_context(info)
        ctx.events.append(f"after:{info.mode}")
        if self.tenant_id != ctx.tenant_id:
            raise PydanticCustomError(
                "tenant_mismatch",
                "payload tenant {claimed!r} does not match authenticated tenant {actual!r}",
                {"claimed": self.tenant_id, "actual": ctx.tenant_id},
            )
        if self.capture == "manual" and self.refund_window_days > 0:
            raise PydanticCustomError(
                "capture_refund_conflict", "manual capture cannot carry a refund window"
            )
        if self.capture == "manual" and MANUAL_CAPTURE not in ctx.permissions:
            raise PydanticCustomError(
                "permission_denied",
                "tenant {tenant} lacks {permission}",
                {"tenant": ctx.tenant_id, "permission": MANUAL_CAPTURE},
            )
        if self.refund_window_days > 0 and REFUND_WINDOWS_FLAG not in ctx.flags:
            raise PydanticCustomError(
                "feature_disabled",
                "feature {flag} is off for tenant {tenant}",
                {"flag": REFUND_WINDOWS_FLAG, "tenant": ctx.tenant_id},
            )
        if self.amount_cents > ctx.max_amount_cents:
            raise PydanticCustomError(
                "amount_over_limit",
                "amount {amount} exceeds tenant limit {limit}",
                {"amount": self.amount_cents, "limit": ctx.max_amount_cents},
            )
        return self

    @model_validator(mode="wrap")
    @classmethod
    def _audit_outcome(
        cls, data: Any, handler: ModelWrapValidatorHandler[Self], info: ValidationInfo
    ) -> Self:
        """Outermost layer: observe every outcome, including errors from inner validators."""
        ctx = _tenant_context(info)
        ctx.events.append("wrap:enter")
        try:
            result = handler(data)
        except ValidationError as exc:
            kinds = sorted({error["type"] for error in exc.errors()})
            ctx.events.append("wrap:rejected:" + ",".join(kinds))
            raise
        ctx.events.append("wrap:accepted")
        return result


def validator_chain(model_cls: type[BaseModel]) -> list[str]:
    """Walk the compiled CoreSchema from the root and name each wrapping layer."""
    chain: list[str] = []
    node: Mapping[str, Any] | None = cast("dict[str, Any]", model_cls.__pydantic_core_schema__)
    while node is not None:
        kind = str(node["type"])
        function = cast("Mapping[str, Any] | None", node.get("function"))
        chain.append(f"{kind}:{function['function'].__name__}" if function else kind)
        node = cast("Mapping[str, Any] | None", node.get("schema"))
    return chain


def revise_payment(current: PaymentIntent, ctx: TenantContext, **changes: object) -> PaymentIntent:
    """Atomic update: re-run the whole chain on a merged dict; `current` is never mutated."""
    return PaymentIntent.model_validate({**current.model_dump(), **changes}, context=ctx)


class MutableBudget(BaseModel):
    """validate_assignment plus an after validator: a rejected assignment still lands."""

    model_config = ConfigDict(validate_assignment=True)

    spent_cents: int
    limit_cents: int

    @model_validator(mode="after")
    def _within_limit(self) -> Self:
        if self.spent_cents > self.limit_cents:
            raise ValueError("spent exceeds limit")
        return self


class NaiveRefund(BaseModel):
    """A before validator that indexes raw input: KeyError escapes as a crash."""

    amount_cents: int
    refunded_cents: int

    @model_validator(mode="before")
    @classmethod
    def _refund_not_above_amount(cls, data: dict[str, int]) -> dict[str, int]:
        if data["refunded_cents"] > data["amount_cents"]:
            raise ValueError("refund exceeds amount")
        return data


class ForgetfulPolicy(BaseModel):
    """An after validator that forgets `return self`; `-> Any` hides it from type checkers."""

    amount_cents: int

    @model_validator(mode="after")
    def _positive(self) -> Any:
        if self.amount_cents <= 0:
            raise ValueError("amount must be positive")


_init_context: ContextVar[TenantContext | None] = ContextVar("_init_context", default=None)


@contextmanager
def init_context(ctx: TenantContext) -> Generator[None]:
    """Make `ctx` visible to `InitContextPayment(...)` for the duration of the block."""
    token = _init_context.set(ctx)
    try:
        yield
    finally:
        _init_context.reset(token)


def _tenant_name(info: ValidationInfo) -> str:
    ctx = info.context
    return ctx.tenant_id if isinstance(ctx, TenantContext) else "none"


class InitContextPayment(BaseModel):
    """The documented ContextVar `__init__` recipe and its model_validate side effect."""

    amount_cents: int
    observed: ClassVar[list[str]] = []

    def __init__(self, /, **data: Any) -> None:
        self.__pydantic_validator__.validate_python(
            data, self_instance=self, context=_init_context.get()
        )

    @model_validator(mode="before")
    @classmethod
    def _see_before(cls, data: Any, info: ValidationInfo) -> Any:
        cls.observed.append(f"before:{_tenant_name(info)}")
        return data

    @model_validator(mode="after")
    def _see_after(self, info: ValidationInfo) -> Self:
        type(self).observed.append(f"after:{_tenant_name(info)}")
        return self


class PaymentReceipt(BaseModel):
    """Context-free response model: safe for FastAPI to re-validate on the way out."""

    tenant_id: str
    amount_cents: int
    capture: str
    validator_events: list[str]


def build_payments_app(registry: Mapping[str, TenantContext]) -> FastAPI:
    """Resolve the tenant from the path, then validate the raw body with that context."""
    app = FastAPI()

    async def validated_intent(tenant_id: str, request: Request) -> tuple[PaymentIntent, list[str]]:
        template = registry.get(tenant_id)
        if template is None:
            raise HTTPException(status_code=404, detail="unknown tenant")
        ctx = replace(template, events=[])
        try:
            intent = PaymentIntent.model_validate_json(await request.body(), context=ctx)
        except ValidationError as exc:
            errors = exc.errors(include_url=False, include_input=False)
            raise RequestValidationError(
                [{**error, "loc": ("body", *error["loc"])} for error in errors]
            ) from exc
        return intent, ctx.events

    @app.post("/v1/tenants/{tenant_id}/payments", status_code=201)
    async def create_payment(tenant_id: str, request: Request) -> PaymentReceipt:
        intent, events = await validated_intent(tenant_id, request)
        return PaymentReceipt(
            tenant_id=intent.tenant_id,
            amount_cents=intent.amount_cents,
            capture=intent.capture,
            validator_events=events,
        )

    @app.post("/v1/tenants/{tenant_id}/payments/echo", status_code=201)
    async def echo_payment(tenant_id: str, request: Request) -> PaymentIntent:
        intent, _ = await validated_intent(tenant_id, request)
        return intent  # FastAPI re-validates this with context=None on the way out

    return app
