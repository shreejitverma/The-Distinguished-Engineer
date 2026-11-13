"""Trace the onion execution order of Pydantic v2 functional field validators.

Every marker validator appends its label to the value it handles, so the validated
string itself is the execution trace: no global state, no side effects.
"""

from datetime import datetime
from typing import Annotated, Any, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainValidator,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    ValidatorFunctionWrapHandler,
    WrapValidator,
    field_validator,
)
from pydantic_core import PydanticCustomError, PydanticOmit, PydanticUseDefault


def before(label: str) -> BeforeValidator:
    """Append `>label` to the raw input before the inner schema runs."""

    def run(value: Any) -> Any:
        return f"{value}>{label}"

    run.__name__ = f"before_{label}"
    return BeforeValidator(run)


def after(label: str) -> AfterValidator:
    """Append `>label` to the value the inner schema produced."""

    def run(value: Any) -> Any:
        return f"{value}>{label}"

    run.__name__ = f"after_{label}"
    return AfterValidator(run)


def wrap(label: str) -> WrapValidator:
    """Mark the input, call the inner schema through `handler`, then mark the result."""

    def run(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        result = handler(f"{value}>{label}.pre")
        return f"{result}>{label}.post"

    run.__name__ = f"wrap_{label}"
    return WrapValidator(run)


def plain(label: str) -> PlainValidator:
    """Replace the inner schema entirely; nothing to the left of it runs."""

    def run(value: Any) -> Any:
        return f"{value}>{label}"

    run.__name__ = f"plain_{label}"
    return PlainValidator(run)


def schema_chain(adapter: TypeAdapter[Any]) -> list[str]:
    """Walk the compiled CoreSchema from the outermost node inwards."""
    node = cast("dict[str, Any]", adapter.core_schema)
    chain: list[str] = []
    while True:
        kind = str(node["type"])
        if not kind.startswith("function-"):
            constraints = sorted(key for key in node if key in {"gt", "ge", "lt", "le"})
            chain.append(kind + "".join(f"[{key}]" for key in constraints))
            return chain
        func = cast("dict[str, Any]", node["function"])["function"]
        chain.append(f"{kind}:{getattr(func, '__name__', type(func).__name__)}")
        if kind == "function-plain":
            return chain
        node = cast("dict[str, Any]", node["schema"])


# The ordering example from the pydantic docs ("Ordering of validators").
DOCS_ORDER: TypeAdapter[str] = TypeAdapter(
    Annotated[str, after("a1"), after("a2"), before("b3"), wrap("w4")]
)

# A wrap in the middle: its post-step runs before the outer after validator.
INTERLEAVED: TypeAdapter[str] = TypeAdapter(
    Annotated[str, before("b1"), after("a2"), wrap("w3"), before("b4"), after("a5")]
)

# A plain validator discards the str schema and every annotation to its left.
PLAIN_IN_MIDDLE: TypeAdapter[str] = TypeAdapter(
    Annotated[str, before("b1"), after("a2"), plain("p3"), before("b4"), after("a5")]
)


def _skip_on_sentinel(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    if value == "skip":
        return {"short_circuited": True}
    return handler(value)


# A wrap that returns without calling handler skips the int schema and a1.
SHORT_CIRCUIT: TypeAdapter[Any] = TypeAdapter(
    Annotated[int, after("a1"), WrapValidator(_skip_on_sentinel)]
)


def _fallback_to_minus_one(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    try:
        return handler(value)
    except ValidationError:
        return -1


FALLBACK: TypeAdapter[int] = TypeAdapter(Annotated[int, WrapValidator(_fallback_to_minus_one)])


def handler_error_view(value: object) -> tuple[str, list[tuple[Any, ...]]]:
    """Return the title and locs of the ValidationError a wrap handler raises."""
    seen: list[tuple[str, list[tuple[Any, ...]]]] = []

    def capture(raw: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        try:
            return handler(raw)
        except ValidationError as exc:
            seen.append((exc.title, [err["loc"] for err in exc.errors()]))
            return 0

    TypeAdapter(Annotated[int, WrapValidator(capture)]).validate_python(value)
    return seen[0]


def _relocate(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    return handler(value, "inner")


class Relocated(BaseModel):
    """`handler(value, outer_location)` prefixes inner error locations."""

    port: Annotated[int, WrapValidator(_relocate)]


class DecoratedOrder(BaseModel):
    """Decorators are appended after Annotated metadata, so they are outermost."""

    value: Annotated[str, before("ann_b"), after("ann_a")]

    @field_validator("value", mode="before")
    @classmethod
    def dec_before(cls, value: Any) -> Any:
        return f"{value}>dec_b"

    @field_validator("value", mode="after")
    @classmethod
    def dec_after(cls, value: str) -> str:
        return f"{value}>dec_a"


class ParentOrder(BaseModel):
    """Two after decorators run in definition order: one, then two."""

    value: str

    @field_validator("value")
    @classmethod
    def one(cls, value: str) -> str:
        return f"{value}>parent.one"

    @field_validator("value")
    @classmethod
    def two(cls, value: str) -> str:
        return f"{value}>parent.two"


class ChildOrder(ParentOrder):
    """Overriding `one` keeps the parent's slot in the decorator dict."""

    @field_validator("value")
    @classmethod
    def one(cls, value: str) -> str:
        return f"{value}>child.one"


def run_after(func: Any, value: object) -> Any:
    """Validate `value` as int, then run `func` as an AfterValidator."""
    return TypeAdapter(Annotated[int, AfterValidator(func)]).validate_python(value)


def reject_with_value_error(value: int) -> int:
    raise ValueError(f"{value} is odd")


def reject_with_assert(value: int) -> int:
    assert value > 5, "too small"  # noqa: S101 - demonstrates AssertionError conversion
    return value


def reject_with_custom_error(value: int) -> int:
    raise PydanticCustomError("port_reserved", "port {port} is reserved", {"port": value})


def reject_with_type_error(value: int) -> int:
    raise TypeError("signature bug, not a validation failure")


def _none_means_default(value: Any) -> Any:
    if value is None:
        raise PydanticUseDefault
    return value


class DefaultOnNone(BaseModel):
    """`PydanticUseDefault` from a before validator falls back to the field default."""

    region: Annotated[str, BeforeValidator(_none_means_default)] = "eu-west-1"


def _omit_negative(value: int) -> int:
    if value < 0:
        raise PydanticOmit
    return value


NON_NEGATIVE_ITEMS: TypeAdapter[list[int]] = TypeAdapter(
    list[Annotated[int, AfterValidator(_omit_negative)]]
)


class OmittedField(BaseModel):
    """`PydanticOmit` on a model field silently drops a *required* field."""

    quota: Annotated[int, AfterValidator(_omit_negative)]
    owner: str = "nobody"


def _minus_ten(value: int) -> int:
    return value - 10


# Constraint before the function: `gt` is a key on the Rust int schema.
GT_BEFORE_FUNC: TypeAdapter[int] = TypeAdapter(
    Annotated[int, Field(gt=0), AfterValidator(_minus_ten)]
)
# Constraint after the function: a Python `greater_than_validator` wraps the result.
GT_AFTER_FUNC: TypeAdapter[int] = TypeAdapter(
    Annotated[int, AfterValidator(_minus_ten), Field(gt=0)]
)


def _times_ten(value: int) -> int:
    return value * 10


class DefaultsAndAssignment(BaseModel):
    """Defaults skip validators unless `validate_default=True`; assignment reruns them."""

    model_config = ConfigDict(validate_assignment=True)

    unvalidated: Annotated[int, AfterValidator(_times_ten)] = 1
    validated: Annotated[int, AfterValidator(_times_ten)] = Field(default=1, validate_default=True)


def _tag_none(value: int | None) -> int:
    return -1 if value is None else value


class OptionalPlacement(BaseModel):
    """Where the validator sits relative to `| None` decides whether it sees None."""

    # The validator wraps the nullable schema, so it sees None.
    outer: Annotated[int | None, AfterValidator(_tag_none)] = 0
    # The validator wraps only the int branch, so None never reaches it.
    inner: Annotated[int, AfterValidator(_tag_none)] | None = 0


def _passthrough(value: Any) -> Any:
    return value


def _passthrough_wrap(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    return handler(value)


class StrictEvent(BaseModel):
    """Strict JSON accepts ISO strings for datetime, until a before or wrap hop intervenes."""

    model_config = ConfigDict(strict=True)

    bare_ts: datetime
    after_ts: Annotated[datetime, AfterValidator(_passthrough)]
    before_ts: Annotated[datetime, BeforeValidator(_passthrough)]
    wrap_ts: Annotated[datetime, WrapValidator(_passthrough_wrap)]


type TicketCode = Annotated[str, before("alias_b"), after("alias_a")]
"""A PEP 695 alias carrying its own validators."""

TICKET: TypeAdapter[str] = TypeAdapter(Annotated[TicketCode, before("outer_b"), after("outer_a")])


def _describe_info(value: str, info: ValidationInfo) -> str:
    return f"{value}|{info.field_name}|{info.data}|{info.mode}|{info.context}"


class InfoProbe(BaseModel):
    """Two positional parameters select the with-info variant of a validator."""

    first: int
    second: Annotated[str, AfterValidator(_describe_info)]


def _parse_port(value: Any) -> int:
    return int(value)


PLAIN_PORT: TypeAdapter[int] = TypeAdapter(Annotated[int, PlainValidator(_parse_port)])
BEFORE_PORT_TYPED: TypeAdapter[int] = TypeAdapter(
    Annotated[int, BeforeValidator(_parse_port, json_schema_input_type=str | int)]
)


def _normalize_sku(value: Any) -> Any:
    if isinstance(value, int):
        value = f"SKU-{value:06d}"
    return value.strip().upper() if isinstance(value, str) else value


def _stable_sku_error(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    try:
        return handler(value)
    except ValidationError as exc:
        raise PydanticCustomError(
            "sku_invalid", "invalid SKU: {reason}", {"reason": exc.errors()[0]["type"]}
        ) from exc


def _require_checksum(value: str) -> str:
    digits = value.removeprefix("SKU-")
    if sum(int(d) for d in digits) % 10 != 0:
        raise ValueError("checksum digit mismatch")
    return value


# Production shape: normalize (before) -> stable error code (wrap) -> pattern (Rust) -> checksum.
type Sku = Annotated[
    str,
    Field(pattern=r"^SKU-\d{6}$"),
    AfterValidator(_require_checksum),
    WrapValidator(_stable_sku_error),
    BeforeValidator(_normalize_sku),
]

SKU: TypeAdapter[str] = TypeAdapter(Sku)
