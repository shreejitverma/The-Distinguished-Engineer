"""Exact money, UTC instants, civil dates, fixed durations, and IANA zones on a JSON wire."""

from __future__ import annotations

import calendar
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import cache
from typing import TYPE_CHECKING, Annotated, Literal, Self
from zoneinfo import ZoneInfo, available_timezones

from fastapi import FastAPI, HTTPException
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    PlainSerializer,
    WithJsonSchema,
    field_serializer,
    model_validator,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# Largest integer every IEEE 754 binary64 parser agrees on (RFC 7493 Section 2.2).
MAX_SAFE_INTEGER = 2**53 - 1
# Bound of google.protobuf.Duration.seconds (duration.proto), about 10,000 years.
MAX_DURATION_SECONDS = 315_576_000_000
# Minor unit exponents copied from ISO 4217 List One as published by SIX on 2026-09-17.
MINOR_UNIT_EXPONENTS: dict[str, int] = {
    "BHD": 3,
    "CHF": 2,
    "CLF": 4,
    "EUR": 2,
    "GBP": 2,
    "INR": 2,
    "ISK": 0,
    "JPY": 0,
    "KRW": 0,
    "KWD": 3,
    "USD": 2,
    "UYW": 4,
}

# Patterns published in the JSON Schema; each mirrors the compiled grammar enforced below.
DECIMAL_STRING_PATTERN = r"^-?(0|[1-9][0-9]{0,17})(\.[0-9]{1,4})?$"
SECONDS_DURATION_PATTERN = r"^-?(0|[1-9][0-9]{0,11})(\.[0-9]{1,6})?s$"
LOCAL_DATE_TIME_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$"
_DECIMAL_STRING = re.compile(r"-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,4})?")
_RFC3339_DATE_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:(?P<second>[0-5][0-9]|60)"
    r"(?P<fraction>\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
_FULL_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_LOCAL_DATE_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
)
_SECONDS_DURATION = re.compile(
    r"(?P<sign>-?)(?P<whole>0|[1-9][0-9]{0,11})(?:\.(?P<fraction>[0-9]{1,6}))?s"
)


def naive_minor_units(wire: bytes) -> int:
    """Convert a JSON number amount to cents the way binary64 code does: truncate value * 100."""
    return int(json.loads(wire)["amount"] * 100)


def parse_as_binary64(wire: bytes) -> object:
    """Decode JSON as a parser whose only number type is IEEE 754 binary64 would."""
    return json.loads(wire, parse_int=float)


def parse_decimal_string(value: object) -> Decimal:
    """Parse the wire form of an exact decimal: an ASCII digit string, never a JSON number."""
    if isinstance(value, Decimal):
        return value.copy_abs() if value.is_zero() else value
    if not isinstance(value, str):
        raise ValueError('amount must be a JSON string such as "19.99", not a JSON number')
    if _DECIMAL_STRING.fullmatch(value) is None:
        raise ValueError(f"amount does not match {DECIMAL_STRING_PATTERN}")
    amount = Decimal(value)
    return amount.copy_abs() if amount.is_zero() else amount


def _to_utc(value: datetime) -> datetime:
    try:
        return value.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError("instant is outside years 0001 to 9999 in UTC") from exc


def parse_rfc3339_utc(value: object) -> datetime:
    """Parse one RFC 3339 date-time (uppercase T, mandatory offset) into an aware UTC datetime."""
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("timestamp must carry a UTC offset")
        return _to_utc(value)
    if not isinstance(value, str):
        raise ValueError("timestamp must be an RFC 3339 string, not a number")
    match = _RFC3339_DATE_TIME.fullmatch(value)
    if match is None:
        raise ValueError("timestamp must match RFC 3339 date-time with T and Z or a numeric offset")
    if match["second"] == "60":
        raise ValueError("leap second 60 is valid RFC 3339 but not representable here")
    fraction = match["fraction"]
    if fraction is not None and len(fraction) > 7:
        raise ValueError("timestamp has more than 6 fractional digits; refusing to truncate")
    return _to_utc(datetime.fromisoformat(value))


def format_rfc3339_utc(value: datetime, *, fraction_digits: Literal[0, 3, 6] | None = None) -> str:
    """Serialize an aware datetime as Z-normalized RFC 3339 with 0, 3, or 6 fractional digits.

    The default prints the shortest of the three widths, which is canonical but does not sort
    as a string; pass fraction_digits for a fixed width that does (RFC 3339 Section 5.1).
    """
    if value.utcoffset() is None:
        raise ValueError("cannot serialize a naive datetime as an instant")
    utc = _to_utc(value)
    micro = utc.microsecond
    if fraction_digits is None:
        fraction_digits = 0 if micro == 0 else 3 if micro % 1000 == 0 else 6
    elif micro % 10 ** (6 - fraction_digits):
        raise ValueError(f"instant needs more than {fraction_digits} fractional digits")
    fraction = (
        f".{micro // 10 ** (6 - fraction_digits):0{fraction_digits}d}" if fraction_digits else ""
    )
    day = f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}"
    return f"{day}T{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}{fraction}Z"


def parse_seconds_duration(value: object) -> timedelta:
    """Parse the protobuf JSON duration form: decimal seconds with a mandatory "s" suffix."""
    if isinstance(value, timedelta):
        if abs(value) > timedelta(seconds=MAX_DURATION_SECONDS):
            raise ValueError("duration is outside +-315,576,000,000 seconds")
        return value
    if not isinstance(value, str):
        raise ValueError('duration must be a string such as "3.5s", not a number')
    match = _SECONDS_DURATION.fullmatch(value)
    if match is None:
        raise ValueError("duration must be decimal seconds ending in s; calendar units are refused")
    whole = int(match["whole"])
    micros = int((match["fraction"] or "").ljust(6, "0"))
    if whole > MAX_DURATION_SECONDS or (whole == MAX_DURATION_SECONDS and micros):
        raise ValueError("duration is outside +-315,576,000,000 seconds")
    magnitude = timedelta(seconds=whole, microseconds=micros)
    return -magnitude if match["sign"] else magnitude


def format_seconds_duration(value: timedelta) -> str:
    """Serialize a timedelta as exact decimal seconds with 0, 3, or 6 fractional digits."""
    total_micros = (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds
    sign = "-" if total_micros < 0 else ""
    whole, micro = divmod(abs(total_micros), 1_000_000)
    if micro == 0:
        fraction = ""
    elif micro % 1000 == 0:
        fraction = f".{micro // 1000:03d}"
    else:
        fraction = f".{micro:06d}"
    return f"{sign}{whole}{fraction}s"


@cache
def _iana_zone_keys() -> frozenset[str]:
    return frozenset(available_timezones())


def validate_zone_id(value: object) -> str:
    """Accept only an exact, case-sensitive key of the installed IANA time zone database."""
    if not isinstance(value, str) or value not in _iana_zone_keys():
        raise ValueError(f"unknown IANA time zone: {value!r}")
    return value


def resolve_civil_time(
    local: datetime, zone_id: str, *, fold: Literal[0, 1] | None = None
) -> datetime:
    """Map a wall-clock time in an IANA zone to a UTC instant, refusing gaps and silent folds."""
    if local.tzinfo is not None:
        raise ValueError("civil time must be naive; the zone travels in its own field")
    zone = ZoneInfo(validate_zone_id(zone_id))
    earlier = local.replace(tzinfo=zone, fold=0)
    later = local.replace(tzinfo=zone, fold=1)
    if _to_utc(earlier).astimezone(zone).replace(tzinfo=None) != local:
        raise ValueError(f"{local.isoformat()} does not exist in {zone_id} (DST gap)")
    if earlier.utcoffset() != later.utcoffset() and fold is None:
        raise ValueError(f"{local.isoformat()} occurs twice in {zone_id}; pass fold=0 or fold=1")
    return _to_utc(later if fold == 1 else earlier)


def parse_local_date_time(value: object) -> datetime:
    """Parse an offset-free civil date-time (the Zalando date-time-local shape)."""
    if isinstance(value, datetime) and value.tzinfo is None and value.microsecond == 0:
        return value
    if not isinstance(value, str) or _LOCAL_DATE_TIME.fullmatch(value) is None:
        raise ValueError("civil time must look like 2024-11-03T01:30:00 with no offset")
    return datetime.fromisoformat(value)


def parse_full_date(value: object) -> date:
    """Parse one RFC 3339 full-date; a date-time, even one at midnight, is not a civil date."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or _FULL_DATE.fullmatch(value) is None:
        raise ValueError("civil date must look like 2026-11-11 with no time and no offset")
    return date.fromisoformat(value)


def add_months(anchor: date, months: int) -> date:
    """Add calendar months to an anchor date, clamping the day to the end of a shorter month."""
    year, month_index = divmod(anchor.year * 12 + anchor.month - 1 + months, 12)
    if not 1 <= year <= 9999:
        raise ValueError("date is outside years 0001 to 9999")
    last_day = calendar.monthrange(year, month_index + 1)[1]
    return date(year, month_index + 1, min(anchor.day, last_day))


def allocate_minor_units(total: int, weights: Sequence[int]) -> list[int]:
    """Split a minor-unit amount by integer weights; the parts always sum to the total."""
    if isinstance(total, bool) or total < 0:
        raise ValueError("total must be a non-negative integer count of minor units")
    weight_sum = sum(weights)
    if weight_sum <= 0 or any(isinstance(weight, bool) or weight < 0 for weight in weights):
        raise ValueError("weights must be non-negative integers with a positive sum")
    shares = [total * weight // weight_sum for weight in weights]
    remainders = [total * weight % weight_sum for weight in weights]
    by_largest_remainder = sorted(range(len(shares)), key=lambda index: (-remainders[index], index))
    for index in by_largest_remainder[: total - sum(shares)]:
        shares[index] += 1
    return shares


DecimalString = Annotated[
    Decimal,
    BeforeValidator(parse_decimal_string),
    WithJsonSchema({"type": "string", "pattern": DECIMAL_STRING_PATTERN}),
]
UtcInstant = Annotated[
    datetime,
    BeforeValidator(parse_rfc3339_utc),
    PlainSerializer(format_rfc3339_utc, return_type=str),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]
SecondsDuration = Annotated[
    timedelta,
    BeforeValidator(parse_seconds_duration),
    PlainSerializer(format_seconds_duration, return_type=str),
    WithJsonSchema({"type": "string", "pattern": SECONDS_DURATION_PATTERN}),
]
CivilDateTime = Annotated[
    datetime,
    BeforeValidator(parse_local_date_time),
    WithJsonSchema({"type": "string", "pattern": LOCAL_DATE_TIME_PATTERN}),
]
CivilDate = Annotated[date, BeforeValidator(parse_full_date)]


class Money(BaseModel):
    """An exact amount in one ISO 4217 currency: decimal string plus alpha-3 code."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    currency_code: str
    amount: DecimalString

    @model_validator(mode="after")
    def _check_currency_and_scale(self) -> Self:
        exponent = MINOR_UNIT_EXPONENTS.get(self.currency_code)
        if exponent is None:
            raise ValueError(f"unsupported ISO 4217 currency_code: {self.currency_code!r}")
        scale = self.amount.as_tuple().exponent
        if not isinstance(scale, int) or scale > 0 or -scale > exponent:
            raise ValueError(f"{self.currency_code} allows at most {exponent} fractional digits")
        if abs(self.amount.scaleb(exponent)) > MAX_SAFE_INTEGER:
            raise ValueError("amount exceeds 2**53 - 1 minor units")
        return self

    @field_serializer("amount")
    def _serialize_amount(self, amount: Decimal) -> str:
        return f"{amount:.{MINOR_UNIT_EXPONENTS[self.currency_code]}f}"

    def to_minor_units(self) -> int:
        """Return the amount as an integer count of the currency's minor unit."""
        return int(self.amount.scaleb(MINOR_UNIT_EXPONENTS[self.currency_code]))

    @classmethod
    def from_minor_units(cls, minor_units: int, currency_code: str) -> Self:
        """Build Money from an integer minor-unit amount (the Stripe wire shape)."""
        if isinstance(minor_units, bool) or abs(minor_units) > MAX_SAFE_INTEGER:
            raise ValueError("minor_units is outside the interoperable range +-(2**53 - 1)")
        exponent = MINOR_UNIT_EXPONENTS.get(currency_code, 0)
        return cls(currency_code=currency_code, amount=Decimal(minor_units).scaleb(-exponent))

    def to_units_nanos(self) -> tuple[int, int]:
        """Split into google.type.Money units and nanos, which always share a sign."""
        units = int(self.amount)
        nanos = int((self.amount - units).scaleb(9))
        return units, nanos

    def __add__(self, other: Money) -> Money:
        """Add two amounts of one currency; a second currency needs an explicit conversion."""
        if other.currency_code != self.currency_code:
            raise ValueError(f"cannot add {other.currency_code} to {self.currency_code}")
        return Money(currency_code=self.currency_code, amount=self.amount + other.amount)


def quantize_to_currency(amount: Decimal, currency_code: str, *, rounding: str) -> Money:
    """Round an exact intermediate result to a currency's minor unit with a named rounding mode."""
    exponent = MINOR_UNIT_EXPONENTS.get(currency_code)
    if exponent is None:
        raise ValueError(f"unsupported ISO 4217 currency_code: {currency_code!r}")
    if not amount.is_finite():
        raise ValueError("amount cannot be rounded to a finite minor-unit value")
    try:
        rounded = amount.quantize(Decimal(1).scaleb(-exponent), rounding=rounding)
    except InvalidOperation as exc:
        raise ValueError("amount cannot be rounded to a finite minor-unit value") from exc
    return Money(currency_code=currency_code, amount=rounded)


class LocalEvent(BaseModel):
    """A future civil event: wall-clock time plus the IANA zone whose rules give it meaning."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    local_time: CivilDateTime
    time_zone: Annotated[str, BeforeValidator(validate_zone_id)]

    def instant(self, *, fold: Literal[0, 1] | None = None) -> datetime:
        """Resolve to UTC under the zone rules installed on this host."""
        return resolve_civil_time(self.local_time, self.time_zone, fold=fold)


class Interval(BaseModel):
    """A half-open span of instants, [start_time, end_time), the google.type.Interval shape."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_time: UtcInstant
    end_time: UtcInstant

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.start_time > self.end_time:
            raise ValueError("start_time must not be after end_time")
        return self

    def contains(self, instant: datetime) -> bool:
        """Report whether an aware instant is at or after the start and before the end."""
        return self.start_time <= instant < self.end_time


class PaymentRequest(BaseModel):
    """Request body of POST /payments."""

    model_config = ConfigDict(extra="forbid")

    amount: Money
    capture_time: UtcInstant
    hold_duration: SecondsDuration


class PaymentResponse(BaseModel):
    """Response body of POST /payments, echoing every quantity in canonical wire form."""

    amount: Money
    amount_minor_units: int
    capture_time: UtcInstant
    expire_time: UtcInstant
    hold_duration: SecondsDuration


def build_payments_app() -> FastAPI:
    """Build an in-process FastAPI app that enforces the money and time wire contract."""
    app = FastAPI()

    @app.post("/payments")
    async def create_payment(body: PaymentRequest) -> PaymentResponse:
        try:
            expire_time = body.capture_time + body.hold_duration
        except OverflowError as exc:
            detail = "capture_time + hold_duration is outside years 0001 to 9999"
            raise HTTPException(status_code=422, detail=detail) from exc
        return PaymentResponse(
            amount=body.amount,
            amount_minor_units=body.amount.to_minor_units(),
            capture_time=body.capture_time,
            expire_time=expire_time,
            hold_duration=body.hold_duration,
        )

    return app
