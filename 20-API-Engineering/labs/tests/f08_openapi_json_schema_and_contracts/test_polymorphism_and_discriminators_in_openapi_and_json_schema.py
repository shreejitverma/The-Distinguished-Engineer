"""Unit tests for OpenAPI discriminator mappings against Pydantic v2 discriminated unions."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from openapi_spec_validator import validate
from pydantic import TypeAdapter, ValidationError

from api_labs.f08_openapi_json_schema_and_contracts.discriminated_polymorphism import (
    COMPONENT_PREFIX,
    LEFT_TO_RIGHT_ADJUSTMENT,
    LEFT_TO_RIGHT_PAYMENT,
    LENIENT_PAYMENT,
    PAYMENT_DOCUMENT,
    SMART_ADJUSTMENT,
    SMART_PAYMENT,
    TAGGED_PAYMENT,
    Charge,
    DiscriminatorError,
    OtherPayment,
    Refund,
    build_payment_app,
    classify_json,
    is_valid,
    matching_branches,
    resolve_discriminator,
    validate_traced,
)

ALL_BRANCHES = ["CardPayment", "BankPayment", "WalletPayment", "VoucherPayment"]
CARD: dict[str, Any] = {"kind": "card", "amount": 1, "last4": "4242"}
SEPA: dict[str, Any] = {"kind": "sepa", "amount": 1, "iban": "DE00"}
VOUCHER: dict[str, Any] = {"kind": "voucher", "amount": 1, "code": "X"}
FORGED: dict[str, Any] = {"kind": "LedgerAdjustment", "amount": -500, "approved_by": "mallory"}
LOOSE: dict[str, Any] = {"kind": "LooseCard", "amount": 1}


def ref(name: str) -> str:
    return COMPONENT_PREFIX + name


@pytest.mark.unit
@pytest.mark.parametrize(
    ("adapter", "payload", "expected_attempts"),
    [
        (TAGGED_PAYMENT, CARD, ["CardPayment"]),
        (TAGGED_PAYMENT, VOUCHER, ["VoucherPayment"]),
        (SMART_PAYMENT, CARD, ALL_BRANCHES),
        (SMART_PAYMENT, VOUCHER, ALL_BRANCHES),
        (LEFT_TO_RIGHT_PAYMENT, CARD, ["CardPayment"]),
        (LEFT_TO_RIGHT_PAYMENT, VOUCHER, ALL_BRANCHES),
    ],
    ids=["tagged-first", "tagged-last", "smart-first", "smart-last", "ltr-first", "ltr-last"],
)
def test_union_mode_decides_how_many_branches_run(
    adapter: TypeAdapter[Any], payload: dict[str, Any], expected_attempts: list[str]
) -> None:
    chosen, attempts = validate_traced(adapter, payload)
    assert chosen == expected_attempts[-1] if adapter is not SMART_PAYMENT else chosen
    assert attempts == expected_attempts


@pytest.mark.unit
def test_unknown_tag_is_one_error_when_tagged_and_one_per_branch_field_when_untagged() -> None:
    bad = {"kind": "wire", "amount": "x"}
    with pytest.raises(ValidationError) as tagged:
        TAGGED_PAYMENT.validate_python(bad)
    with pytest.raises(ValidationError) as smart:
        SMART_PAYMENT.validate_python(bad)

    assert [(e["type"], e["loc"]) for e in tagged.value.errors()] == [("union_tag_invalid", ())]
    assert smart.value.error_count() == 12
    assert {e["loc"][0] for e in smart.value.errors()} == set(ALL_BRANCHES)
    assert len(smart.value.json(include_url=False)) > 4 * len(tagged.value.json(include_url=False))


@pytest.mark.unit
def test_smart_mode_breaks_structural_overlap_by_fields_set_then_declaration_order() -> None:
    tie = SMART_ADJUSTMENT.validate_python({"amount": 5})
    more_fields = SMART_ADJUSTMENT.validate_python({"amount": 5, "capture": False})
    first_match = LEFT_TO_RIGHT_ADJUSTMENT.validate_python({"amount": 5, "capture": False})

    assert type(tie) is Refund
    assert more_fields == Charge(amount=5, capture=False)
    assert type(first_match) is Refund
    assert first_match.model_fields_set == {"amount"}


@pytest.mark.unit
def test_overlapping_branches_pass_anyof_and_fail_oneof() -> None:
    both = [ref("Refund"), ref("Charge")]
    assert matching_branches(PAYMENT_DOCUMENT, ref("Adjustment"), {"amount": 5}) == both
    assert is_valid(PAYMENT_DOCUMENT, ref("Adjustment"), {"amount": 5}) is True

    loose_matches = matching_branches(PAYMENT_DOCUMENT, ref("LoosePayment"), LOOSE)
    assert loose_matches == [ref("LooseCard"), ref("LooseBank")]
    assert is_valid(PAYMENT_DOCUMENT, ref("LoosePayment"), LOOSE) is False

    assert matching_branches(PAYMENT_DOCUMENT, ref("Payment"), CARD) == [ref("CardPayment")]
    assert is_valid(PAYMENT_DOCUMENT, ref("Payment"), CARD) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("union", "instance", "expected"),
    [
        ("Payment", CARD, "CardPayment"),
        ("Payment", SEPA, "BankPayment"),
        ("LoosePayment", LOOSE, "LooseCard"),
        ("LenientPayment", SEPA, "BankPayment"),
        ("LenientPayment", {"kind": "wire", "amount": 1}, "OtherPayment"),
        ("LenientPayment", {"amount": 1}, "OtherPayment"),
        ("LenientPayment", FORGED, "OtherPayment"),
    ],
    ids=["uri-map", "name-map", "implicit", "default-unused", "unmapped", "missing", "outside"],
)
def test_resolver_applies_mapping_then_component_name_then_default_mapping(
    union: str, instance: dict[str, Any], expected: str
) -> None:
    assert resolve_discriminator(PAYMENT_DOCUMENT, ref(union), instance) == ref(expected)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("instance", "reason"),
    [
        (FORGED, "target-not-listed"),
        ({"kind": "wire", "amount": 1}, "target-not-listed"),
        ({"amount": 1}, "tag-missing"),
        ({"kind": 7, "amount": 1}, "tag-missing"),
    ],
    ids=["other-component", "unknown", "absent", "non-string"],
)
def test_resolver_refuses_targets_outside_the_union(instance: dict[str, Any], reason: str) -> None:
    with pytest.raises(DiscriminatorError) as raised:
        resolve_discriminator(PAYMENT_DOCUMENT, ref("Payment"), instance)
    assert raised.value.reason == reason


@pytest.mark.unit
def test_oas30_validator_dispatch_changes_the_validation_outcome() -> None:
    # A tag naming a component outside the oneOf is accepted by the 3.0 dispatch only.
    assert is_valid(PAYMENT_DOCUMENT, ref("Payment"), FORGED, "3.0") is True
    assert is_valid(PAYMENT_DOCUMENT, ref("Payment"), FORGED, "3.1") is False
    # Two matching branches fail a real oneOf, yet the 3.0 dispatch checks one branch.
    assert is_valid(PAYMENT_DOCUMENT, ref("LoosePayment"), LOOSE, "3.0") is True
    assert is_valid(PAYMENT_DOCUMENT, ref("LoosePayment"), LOOSE, "3.1") is False
    # A bare schema-name mapping value is not resolved by the 3.0 dispatch.
    assert is_valid(PAYMENT_DOCUMENT, ref("Payment"), SEPA, "3.0") is False
    assert is_valid(PAYMENT_DOCUMENT, ref("Payment"), SEPA, "3.1") is True
    assert is_valid(PAYMENT_DOCUMENT, ref("Payment"), CARD, "3.0") is True


@pytest.mark.unit
def test_default_mapping_branch_excludes_mapped_tags_so_oneof_stays_exclusive() -> None:
    lenient = ref("LenientPayment")
    unknown = {"kind": "crypto", "amount": 3, "chain": "btc"}

    assert matching_branches(PAYMENT_DOCUMENT, lenient, CARD) == [ref("CardPayment")]
    assert matching_branches(PAYMENT_DOCUMENT, lenient, unknown) == [ref("OtherPayment")]
    assert matching_branches(PAYMENT_DOCUMENT, lenient, {"amount": 3}) == [ref("OtherPayment")]

    fallback = LENIENT_PAYMENT.validate_python(unknown)
    assert type(fallback) is OtherPayment
    assert fallback.model_extra == {"kind": "crypto", "chain": "btc"}
    assert type(LENIENT_PAYMENT.validate_python({"amount": 3})) is OtherPayment


@pytest.mark.unit
def test_callable_discriminator_emits_no_discriminator_and_an_ambiguous_oneof() -> None:
    emitted = LENIENT_PAYMENT.json_schema()

    assert "discriminator" in TAGGED_PAYMENT.json_schema()
    assert "discriminator" not in emitted
    assert [branch["$ref"] for branch in emitted["oneOf"]] == [
        "#/$defs/CardPayment",
        "#/$defs/BankPayment",
        "#/$defs/OtherPayment",
    ]
    # The runtime accepts a card payload that its own published oneOf rejects.
    assert type(LENIENT_PAYMENT.validate_python(CARD)).__name__ == "CardPayment"
    assert is_valid(emitted, "", CARD) is False
    assert is_valid(emitted, "#/$defs/CardPayment", CARD) is True
    assert is_valid(emitted, "#/$defs/OtherPayment", CARD) is True


@pytest.mark.unit
def test_duplicate_tag_key_last_value_selects_the_branch() -> None:
    body = b'{"kind":"%s","kind":"%s","amount":1,"iban":"DE00","last4":"4242"}'

    assert classify_json(body % (b"card", b"sepa")) == "BankPayment"
    assert classify_json(body % (b"sepa", b"card")) == "CardPayment"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_emits_oneof_with_mapping_for_tagged_and_anyof_for_untagged() -> None:
    transport = httpx.ASGITransport(app=build_payment_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        document = (await client.get("/openapi.json")).json()

    validate(document)
    assert document["openapi"] == "3.1.0"
    tagged = document["paths"]["/payments"]["post"]["requestBody"]["content"]["application/json"]
    assert [branch["$ref"] for branch in tagged["schema"]["oneOf"]] == [
        ref(name) for name in ALL_BRANCHES
    ]
    assert tagged["schema"]["discriminator"] == {
        "propertyName": "kind",
        "mapping": {
            "card": ref("CardPayment"),
            "bank_transfer": ref("BankPayment"),
            "sepa": ref("BankPayment"),
            "wallet": ref("WalletPayment"),
            "voucher": ref("VoucherPayment"),
        },
    }
    schemas = document["components"]["schemas"]
    assert schemas["CardPayment"]["properties"]["kind"]["const"] == "card"
    assert schemas["BankPayment"]["properties"]["kind"]["enum"] == ["bank_transfer", "sepa"]
    assert "kind" in schemas["CardPayment"]["required"]

    untagged = document["paths"]["/adjustments"]["post"]["requestBody"]["content"]
    assert untagged["application/json"]["schema"]["anyOf"] == [
        {"$ref": ref("Refund")},
        {"$ref": ref("Charge")},
    ]
    assert "discriminator" not in untagged["application/json"]["schema"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_422_names_the_tag_when_tagged_and_every_branch_when_untagged() -> None:
    transport = httpx.ASGITransport(app=build_payment_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/payments", json=SEPA)
        unknown = await client.post("/payments", json={"kind": "wire", "amount": 1})
        missing = await client.post("/payments", json={"amount": 1})
        untagged = await client.post("/adjustments", json={"amount": "x"})
        overlap = await client.post("/adjustments", json={"amount": 5, "capture": False})

    assert (created.status_code, created.json()) == (201, SEPA)
    assert unknown.status_code == 422
    assert [(e["type"], e["loc"]) for e in unknown.json()["detail"]] == [
        ("union_tag_invalid", ["body"])
    ]
    assert unknown.json()["detail"][0]["ctx"]["expected_tags"] == (
        "'card', 'bank_transfer', 'sepa', 'wallet', 'voucher'"
    )
    assert [e["type"] for e in missing.json()["detail"]] == ["union_tag_not_found"]
    assert [e["loc"] for e in untagged.json()["detail"]] == [
        ["body", "Refund", "amount"],
        ["body", "Charge", "amount"],
    ]
    assert overlap.json() == {"branch": "Charge"}
