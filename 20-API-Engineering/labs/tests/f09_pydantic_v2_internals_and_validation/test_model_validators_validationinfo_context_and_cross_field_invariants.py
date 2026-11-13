"""Unit tests for @model_validator modes, ValidationInfo.context, and cross-field invariants."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from fastapi.exceptions import ResponseValidationError
from pydantic import ValidationError

from api_labs.f09_pydantic_v2_internals_and_validation.tenant_context_validators import (
    MANUAL_CAPTURE,
    REFUND_WINDOWS_FLAG,
    ForgetfulPolicy,
    InitContextPayment,
    MutableBudget,
    NaiveRefund,
    PaymentIntent,
    TenantContext,
    build_payments_app,
    init_context,
    revise_payment,
    validator_chain,
)

ACME = TenantContext(
    tenant_id="acme",
    permissions=frozenset({MANUAL_CAPTURE}),
    flags=frozenset({REFUND_WINDOWS_FLAG}),
    max_amount_cents=50_000,
)
GLOBEX = TenantContext(
    tenant_id="globex", permissions=frozenset(), flags=frozenset(), max_amount_cents=1_000
)


def fresh(template: TenantContext) -> TenantContext:
    return replace(template, events=[])


def error_types(exc: ValidationError) -> list[str]:
    return [error["type"] for error in exc.errors()]


@pytest.mark.unit
def test_before_wraps_model_fields_and_after_wrap_wrap_the_model() -> None:
    assert validator_chain(PaymentIntent) == [
        "function-wrap:_audit_outcome",
        "function-after:_enforce_tenant_policy",
        "model",
        "function-before:_stamp_tenant",
        "model-fields",
    ]
    ctx = fresh(ACME)
    PaymentIntent.model_validate({"amount_cents": 100, "currency": "USD"}, context=ctx)
    assert ctx.events == ["wrap:enter", "before", "after:python", "wrap:accepted"]


@pytest.mark.unit
def test_context_stamps_tenant_on_a_copy_and_reports_json_mode() -> None:
    payload: dict[str, object] = {"amount_cents": 100, "currency": "EUR"}
    ctx = fresh(ACME)
    intent = PaymentIntent.model_validate(payload, context=ctx)
    assert intent.tenant_id == "acme"
    assert payload == {"amount_cents": 100, "currency": "EUR"}

    json_ctx = fresh(ACME)
    PaymentIntent.model_validate_json(b'{"amount_cents": 100, "currency": "EUR"}', context=json_ctx)
    assert "after:json" in json_ctx.events


@pytest.mark.unit
def test_missing_context_is_a_validation_error_with_empty_loc() -> None:
    with pytest.raises(ValidationError) as excinfo:
        PaymentIntent.model_validate({"amount_cents": 100, "currency": "USD"})
    (error,) = excinfo.value.errors(include_url=False)
    assert error["type"] == "missing_context"
    assert error["loc"] == ()
    assert error.get("ctx") == {"got": "NoneType"}


@pytest.mark.unit
def test_same_payload_passes_or_fails_by_permission_flag_and_limit() -> None:
    manual = {"amount_cents": 900, "currency": "USD", "capture": "manual"}
    assert PaymentIntent.model_validate(manual, context=fresh(ACME)).capture == "manual"
    with pytest.raises(ValidationError) as denied:
        PaymentIntent.model_validate(manual, context=fresh(GLOBEX))
    assert error_types(denied.value) == ["permission_denied"]

    refund = {"amount_cents": 900, "currency": "USD", "refund_window_days": 30}
    assert PaymentIntent.model_validate(refund, context=fresh(ACME)).refund_window_days == 30
    with pytest.raises(ValidationError) as disabled:
        PaymentIntent.model_validate(refund, context=fresh(GLOBEX))
    assert error_types(disabled.value) == ["feature_disabled"]

    big = {"amount_cents": 5_000, "currency": "USD"}
    assert PaymentIntent.model_validate(big, context=fresh(ACME)).amount_cents == 5_000
    with pytest.raises(ValidationError) as over:
        PaymentIntent.model_validate(big, context=fresh(GLOBEX))
    assert error_types(over.value) == ["amount_over_limit"]


@pytest.mark.unit
def test_cross_tenant_payload_and_context_free_conflict_are_rejected() -> None:
    with pytest.raises(ValidationError) as mismatch:
        PaymentIntent.model_validate(
            {"tenant_id": "globex", "amount_cents": 1, "currency": "USD"}, context=fresh(ACME)
        )
    assert error_types(mismatch.value) == ["tenant_mismatch"]

    conflict = {"amount_cents": 1, "currency": "USD", "capture": "manual", "refund_window_days": 7}
    with pytest.raises(ValidationError) as excinfo:
        PaymentIntent.model_validate(conflict, context=fresh(ACME))
    assert error_types(excinfo.value) == ["capture_refund_conflict"]


@pytest.mark.unit
def test_field_errors_stop_before_after_validator_and_wrap_sees_them() -> None:
    ctx = fresh(ACME)
    with pytest.raises(ValidationError) as excinfo:
        PaymentIntent.model_validate({"amount_cents": 0, "currency": "GBP"}, context=ctx)
    assert sorted(error_types(excinfo.value)) == ["greater_than", "literal_error"]
    assert ctx.events == ["wrap:enter", "before", "wrap:rejected:greater_than,literal_error"]


@pytest.mark.unit
def test_instance_input_skips_before_but_reruns_after_under_new_context() -> None:
    acme_intent = PaymentIntent.model_validate(
        {"amount_cents": 100, "currency": "USD"}, context=fresh(ACME)
    )
    ctx = fresh(GLOBEX)
    with pytest.raises(ValidationError) as excinfo:
        PaymentIntent.model_validate(acme_intent, context=ctx)
    assert error_types(excinfo.value) == ["tenant_mismatch"]
    assert ctx.events == ["wrap:enter", "after:python", "wrap:rejected:tenant_mismatch"]


@pytest.mark.unit
def test_validate_assignment_failure_leaves_the_mutation_but_revise_is_atomic() -> None:
    budget = MutableBudget(spent_cents=10, limit_cents=100)
    with pytest.raises(ValidationError):
        budget.spent_cents = 500
    assert budget.spent_cents == 500  # pydantic#7105: the rejected value was already written

    intent = PaymentIntent.model_validate(
        {"amount_cents": 100, "currency": "USD"}, context=fresh(ACME)
    )
    with pytest.raises(ValidationError) as excinfo:
        revise_payment(intent, fresh(GLOBEX), amount_cents=200)
    assert error_types(excinfo.value) == ["tenant_mismatch"]
    revised = revise_payment(intent, fresh(ACME), amount_cents=200)
    assert (intent.amount_cents, revised.amount_cents) == (100, 200)


@pytest.mark.unit
def test_only_value_and_assertion_errors_become_validation_errors() -> None:
    with pytest.raises(KeyError):
        NaiveRefund.model_validate({"amount_cents": 100})
    with pytest.raises(ValidationError) as excinfo:
        NaiveRefund.model_validate({"amount_cents": 100, "refunded_cents": 200})
    (error,) = excinfo.value.errors(include_url=False)
    assert (error["type"], error["loc"]) == ("value_error", ())


@pytest.mark.unit
def test_after_validator_without_return_self_yields_none_from_model_validate() -> None:
    assert ForgetfulPolicy.model_validate({"amount_cents": 5}) is None


@pytest.mark.unit
def test_custom_init_contextvar_recipe_drops_model_validate_context() -> None:
    InitContextPayment.observed.clear()
    with init_context(fresh(ACME)):
        InitContextPayment(amount_cents=1)
    assert InitContextPayment.observed == ["before:acme", "after:acme"]

    InitContextPayment.observed.clear()
    InitContextPayment.model_validate({"amount_cents": 1}, context=fresh(ACME))
    assert InitContextPayment.observed == ["before:none", "after:none", "after:acme"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_route_resolves_context_from_path_tenant() -> None:
    app = build_payments_app({"acme": ACME, "globex": GLOBEX})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = {"amount_cents": 900, "currency": "USD", "capture": "manual"}
        accepted = await client.post("/v1/tenants/acme/payments", json=body)
        denied = await client.post("/v1/tenants/globex/payments", json=body)
        crossed = await client.post(
            "/v1/tenants/globex/payments", json={**body, "tenant_id": "acme"}
        )
        unknown = await client.post("/v1/tenants/initech/payments", json=body)

    assert accepted.status_code == 201
    assert accepted.json() == {
        "tenant_id": "acme",
        "amount_cents": 900,
        "capture": "manual",
        "validator_events": ["wrap:enter", "before", "after:json", "wrap:accepted"],
    }
    assert denied.status_code == 422
    assert denied.json()["detail"] == [
        {
            "type": "permission_denied",
            "loc": ["body"],
            "msg": "tenant globex lacks payments:manual_capture",
            "ctx": {"tenant": "globex", "permission": MANUAL_CAPTURE},
        }
    ]
    assert crossed.json()["detail"][0]["type"] == "tenant_mismatch"
    assert unknown.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_response_model_revalidation_runs_validators_without_context() -> None:
    app = build_payments_app({"acme": ACME})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(ResponseValidationError) as excinfo:
            await client.post(
                "/v1/tenants/acme/payments/echo", json={"amount_cents": 1, "currency": "USD"}
            )
    (error,) = excinfo.value.errors()
    assert (error["type"], error["loc"]) == ("missing_context", ("response",))
