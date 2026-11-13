"""Polymorphism as an OpenAPI oneOf with a discriminator and as a Pydantic v2 tagged union."""

from __future__ import annotations

import timeit
from typing import Annotated, Any, Literal, cast

from fastapi import FastAPI
from openapi_schema_validator import OAS30Validator, OAS31Validator
from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    TypeAdapter,
    ValidationInfo,
    model_validator,
)
from referencing import Registry, Specification
from referencing.jsonschema import DRAFT4, DRAFT202012

DOCUMENT_URI = "urn:lab:openapi"
COMPONENT_PREFIX = "#/components/schemas/"


class TracedModel(BaseModel):
    """Append the class name to context["attempts"] each time a union enters this branch."""

    @model_validator(mode="before")
    @classmethod
    def record_attempt(cls, data: Any, info: ValidationInfo) -> Any:
        if info.context is not None:
            cast("dict[str, list[str]]", info.context)["attempts"].append(cls.__name__)
        return data


class CardPayment(TracedModel):
    kind: Literal["card"]
    amount: int
    last4: str


class BankPayment(TracedModel):
    kind: Literal["bank_transfer", "sepa"]
    amount: int
    iban: str


class WalletPayment(TracedModel):
    kind: Literal["wallet"]
    amount: int
    wallet_id: str


class VoucherPayment(TracedModel):
    kind: Literal["voucher"]
    amount: int
    code: str


class Refund(BaseModel):
    amount: int
    reason: str | None = None


class Charge(BaseModel):
    amount: int
    capture: bool = True


class OtherPayment(BaseModel):
    """Fallback branch: the Pydantic counterpart of an OpenAPI 3.2 defaultMapping schema."""

    model_config = ConfigDict(extra="allow")

    amount: int


PaymentUnion = CardPayment | BankPayment | WalletPayment | VoucherPayment
TaggedPayment = Annotated[PaymentUnion, Field(discriminator="kind")]

TAGGED_PAYMENT: TypeAdapter[PaymentUnion] = TypeAdapter(TaggedPayment)
SMART_PAYMENT: TypeAdapter[PaymentUnion] = TypeAdapter(PaymentUnion)
LEFT_TO_RIGHT_PAYMENT: TypeAdapter[PaymentUnion] = TypeAdapter(
    Annotated[PaymentUnion, Field(union_mode="left_to_right")]
)
SMART_ADJUSTMENT: TypeAdapter[Refund | Charge] = TypeAdapter(Refund | Charge)
LEFT_TO_RIGHT_ADJUSTMENT: TypeAdapter[Refund | Charge] = TypeAdapter(
    Annotated[Refund | Charge, Field(union_mode="left_to_right")]
)

_LENIENT_TAGS = {"card": "card", "bank_transfer": "bank", "sepa": "bank"}


def lenient_tag(value: Any) -> str:
    """Map a payload to a branch tag, sending a missing or unknown kind to the fallback."""
    if isinstance(value, dict):
        kind: object = cast("dict[str, object]", value).get("kind")
    else:
        kind = getattr(value, "kind", None)
    return _LENIENT_TAGS.get(kind, "other") if isinstance(kind, str) else "other"


LenientPayment = Annotated[
    Annotated[CardPayment, Tag("card")]
    | Annotated[BankPayment, Tag("bank")]
    | Annotated[OtherPayment, Tag("other")],
    Discriminator(lenient_tag),
]
LENIENT_PAYMENT: TypeAdapter[CardPayment | BankPayment | OtherPayment] = TypeAdapter(LenientPayment)


def validate_traced(adapter: TypeAdapter[Any], payload: dict[str, Any]) -> tuple[str, list[str]]:
    """Validate a payload and return the chosen branch plus every branch the union entered."""
    attempts: list[str] = []
    instance = adapter.validate_python(payload, context={"attempts": attempts})
    return type(instance).__name__, attempts


def _object(required: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "required": required, "properties": properties}


def _ref(name: str) -> dict[str, str]:
    return {"$ref": COMPONENT_PREFIX + name}


_INTEGER = {"type": "integer"}
_STRING = {"type": "string"}

# A hand-written component graph that uses only keywords shared by OAS 3.0, 3.1, and 3.2.
PAYMENT_DOCUMENT: dict[str, Any] = {
    "components": {
        "schemas": {
            "Payment": {
                "oneOf": [_ref("CardPayment"), _ref("BankPayment")],
                "discriminator": {
                    "propertyName": "kind",
                    "mapping": {
                        "card": COMPONENT_PREFIX + "CardPayment",
                        "bank_transfer": "BankPayment",
                        "sepa": "BankPayment",
                    },
                },
            },
            "LenientPayment": {
                "oneOf": [_ref("CardPayment"), _ref("BankPayment"), _ref("OtherPayment")],
                "discriminator": {
                    "propertyName": "kind",
                    "mapping": {
                        "card": "CardPayment",
                        "bank_transfer": "BankPayment",
                        "sepa": "BankPayment",
                    },
                    "defaultMapping": "OtherPayment",
                },
            },
            "LoosePayment": {
                "oneOf": [_ref("LooseCard"), _ref("LooseBank")],
                "discriminator": {"propertyName": "kind"},
            },
            "Adjustment": {"anyOf": [_ref("Refund"), _ref("Charge")]},
            "CardPayment": _object(
                ["kind", "amount", "last4"],
                {
                    "kind": {"type": "string", "enum": ["card"]},
                    "amount": _INTEGER,
                    "last4": _STRING,
                },
            ),
            "BankPayment": _object(
                ["kind", "amount", "iban"],
                {
                    "kind": {"type": "string", "enum": ["bank_transfer", "sepa"]},
                    "amount": _INTEGER,
                    "iban": _STRING,
                },
            ),
            "OtherPayment": _object(
                ["amount"],
                {"amount": _INTEGER, "kind": {"not": {"enum": ["card", "bank_transfer", "sepa"]}}},
            ),
            "LooseCard": _object(["kind", "amount"], {"kind": _STRING, "amount": _INTEGER}),
            "LooseBank": _object(["kind", "amount"], {"kind": _STRING, "amount": _INTEGER}),
            "Refund": _object(["amount"], {"amount": _INTEGER, "reason": _STRING}),
            "Charge": _object(["amount"], {"amount": _INTEGER, "capture": {"type": "boolean"}}),
            "LedgerAdjustment": _object(
                ["amount", "approved_by"], {"amount": _INTEGER, "approved_by": _STRING}
            ),
        }
    }
}


class DiscriminatorError(ValueError):
    """The discriminator cannot select a schema; reason is a stable machine-readable code."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


def is_valid(
    document: dict[str, Any], ref: str, instance: object, dialect: Literal["3.0", "3.1"] = "3.1"
) -> bool:
    """Validate an instance against one component with the OAS 3.0 or OAS 3.1 dialect."""
    validator_class = OAS30Validator if dialect == "3.0" else OAS31Validator
    specification: Specification[Any] = DRAFT4 if dialect == "3.0" else DRAFT202012
    registry: Registry[Any] = Registry[Any]().with_resource(
        DOCUMENT_URI, specification.create_resource(document)
    )
    validator = validator_class({"$ref": DOCUMENT_URI + ref}, registry=registry)
    return bool(validator.is_valid(instance))


def branch_refs(document: dict[str, Any], union_ref: str) -> list[str]:
    """Return the $ref targets listed in the oneOf or anyOf of a union component."""
    schema = document["components"]["schemas"][union_ref.removeprefix(COMPONENT_PREFIX)]
    branches = cast("list[dict[str, str]]", schema.get("oneOf") or schema.get("anyOf") or [])
    return [branch["$ref"] for branch in branches]


def matching_branches(document: dict[str, Any], union_ref: str, instance: object) -> list[str]:
    """Return every listed branch the instance validates against, in declaration order."""
    return [ref for ref in branch_refs(document, union_ref) if is_valid(document, ref, instance)]


def resolve_discriminator(document: dict[str, Any], union_ref: str, instance: object) -> str:
    """Select the branch a Discriminator Object names, refusing targets outside the union.

    Explicit mapping wins, then the implicit component name, then defaultMapping (OAS 3.2).
    A mapping value that names a component is read as a schema name before a URI reference.
    """
    schemas = cast("dict[str, dict[str, Any]]", document["components"]["schemas"])
    discriminator = schemas[union_ref.removeprefix(COMPONENT_PREFIX)]["discriminator"]
    listed = branch_refs(document, union_ref)

    def as_ref(target: str) -> str:
        return COMPONENT_PREFIX + target if target in schemas else target

    def fallback(reason: str, detail: str) -> str:
        if "defaultMapping" in discriminator:
            return as_ref(cast("str", discriminator["defaultMapping"]))
        raise DiscriminatorError(reason, detail)

    payload = cast("dict[str, object]", instance) if isinstance(instance, dict) else {}
    tag = payload.get(discriminator["propertyName"])
    if not isinstance(tag, str):
        return fallback("tag-missing", f"no string {discriminator['propertyName']!r} property")
    mapping = cast("dict[str, str]", discriminator.get("mapping", {}))
    target = as_ref(mapping.get(tag, tag))
    if target not in listed:
        return fallback("target-not-listed", f"{tag!r} selects {target!r}, not a listed branch")
    return target


def classify_json(raw: bytes) -> str:
    """Return the branch the tagged union selects for raw JSON bytes."""
    return type(TAGGED_PAYMENT.validate_json(raw)).__name__


def build_payment_app() -> FastAPI:
    """Create an ASGI app with one tagged-union body and one untagged-union body."""
    app = FastAPI(title="Payments", version="1.0.0")

    @app.post("/payments", status_code=201)
    async def create_payment(payment: TaggedPayment) -> TaggedPayment:
        return payment

    @app.post("/adjustments")
    async def create_adjustment(adjustment: Refund | Charge) -> dict[str, str]:
        return {"branch": type(adjustment).__name__}

    return app


def main() -> None:  # pragma: no cover - timing harness, not a deterministic test
    """Print microseconds per call for the last-declared variant under each union mode."""
    payload = {"kind": "voucher", "amount": 1, "code": "X"}
    loose = {"kind": "LooseCard", "amount": 1}
    cases = {
        "pydantic tagged-union": lambda: TAGGED_PAYMENT.validate_python(payload),
        "pydantic smart union": lambda: SMART_PAYMENT.validate_python(payload),
        "pydantic left_to_right union": lambda: LEFT_TO_RIGHT_PAYMENT.validate_python(payload),
        "jsonschema oneOf (2 branches)": lambda: is_valid(
            PAYMENT_DOCUMENT, COMPONENT_PREFIX + "LoosePayment", loose
        ),
        "jsonschema hinted branch only": lambda: is_valid(
            PAYMENT_DOCUMENT, resolve_discriminator(PAYMENT_DOCUMENT, "LoosePayment", loose), loose
        ),
    }
    for label, call in cases.items():
        number = 200_000 if label.startswith("pydantic") else 2_000
        best = min(timeit.repeat(call, number=number, repeat=5)) / number
        print(f"{label:32s} {best * 1e6:9.3f} us/call")


if __name__ == "__main__":  # pragma: no cover
    main()
