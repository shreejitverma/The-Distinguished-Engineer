"""Tests for exact money, UTC instants, civil dates, durations, and IANA zones on a JSON wire."""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx
import msgspec
import pytest
from pydantic import TypeAdapter, ValidationError

from api_labs.f07_versioning_evolution_and_api_guidelines.money_time_contracts import (
    DECIMAL_STRING_PATTERN,
    LOCAL_DATE_TIME_PATTERN,
    MAX_SAFE_INTEGER,
    SECONDS_DURATION_PATTERN,
    CivilDate,
    Interval,
    LocalEvent,
    Money,
    PaymentRequest,
    add_months,
    allocate_minor_units,
    build_payments_app,
    format_rfc3339_utc,
    format_seconds_duration,
    naive_minor_units,
    parse_as_binary64,
    parse_decimal_string,
    parse_full_date,
    parse_local_date_time,
    parse_rfc3339_utc,
    parse_seconds_duration,
    quantize_to_currency,
    resolve_civil_time,
    validate_zone_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable

NEW_YORK = "America/New_York"


def usd(amount: str) -> Money:
    return Money.model_validate_json(json.dumps({"currency_code": "USD", "amount": amount}))


@pytest.mark.unit
def test_binary64_truncation_loses_a_cent_and_decimal_string_does_not() -> None:
    assert 19.99 * 100 == 1998.9999999999998
    assert naive_minor_units(b'{"amount": 19.99}') == 1998
    assert json.dumps(0.1 + 0.2) == "0.30000000000000004"
    assert usd("19.99").to_minor_units() == 1999
    assert (usd("0.10").amount + usd("0.20").amount) == Decimal("0.30")


@pytest.mark.unit
def test_integers_above_2_53_collapse_in_binary64_and_minor_units_refuse_them() -> None:
    assert parse_as_binary64(b'{"id": 9007199254740993}') == {"id": 9007199254740992.0}
    assert float("90071992547409.93") == 90071992547409.94
    assert Money.from_minor_units(MAX_SAFE_INTEGER, "USD").to_minor_units() == 2**53 - 1
    with pytest.raises(ValueError, match="interoperable range"):
        Money.from_minor_units(2**53, "USD")
    with pytest.raises(ValueError, match="interoperable range"):
        Money.from_minor_units(True, "USD")
    with pytest.raises(ValidationError, match=r"exceeds 2\*\*53 - 1 minor units"):
        usd("90071992547409.92")
    assert usd("-90071992547409.91").to_minor_units() == -(2**53 - 1)
    with pytest.raises(ValidationError, match=r"exceeds 2\*\*53 - 1 minor units"):
        usd("-90071992547409.92")
    with pytest.raises(ValueError, match="interoperable range"):
        Money.from_minor_units(-(2**53), "USD")


@pytest.mark.unit
def test_json_number_amount_is_rejected_because_pydantic_decimal_routes_it_through_f64() -> None:
    plain = TypeAdapter(Decimal)
    assert plain.validate_json("1.000000000000000000001") == Decimal("1")
    assert plain.validate_json("0.1", strict=True) == Decimal("0.1")
    assert plain.validate_json('"1.000000000000000000001"') == Decimal("1.000000000000000000001")
    with pytest.raises(ValidationError, match="not a JSON number"):
        Money.model_validate_json('{"currency_code": "USD", "amount": 19.99}')
    with pytest.raises(ValidationError, match="not a JSON number"):
        Money.model_validate_json('{"currency_code": "USD", "amount": 1999}')


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "1e3",
        "1E+1000000000",
        "1,234.56",
        "1.234,56",
        " 1.5",
        "1.5\n",
        "+1.5",
        "1_000",
        ".5",
        "5.",
        "01.50",
        "\u0661\u0662.\u0663\u0664",
        "NaN",
        "Infinity",
        "",
        "1.23456",
        "1234567890123456789",
        "12,34",
        "1\u00a0234,56",
        "1\u202f234,56",
        "1'234.56",
        "\u0661\u0662\u066b\u0663\u0664",
        "\uff11\uff12.\uff13\uff14",
        "$19.99",
        "19.99 USD",
        "(19.99)",
        "19.99-",
        "\u221219.99",
    ],
    ids=[
        "exponent",
        "huge-exponent",
        "en-US-grouping",
        "de-DE-grouping",
        "leading-space",
        "trailing-newline",
        "plus-sign",
        "underscore",
        "no-integer-part",
        "no-fraction-digits",
        "leading-zero",
        "arabic-indic-digits",
        "nan",
        "infinity",
        "empty",
        "five-fraction-digits",
        "nineteen-integer-digits",
        "decimal-comma",
        "fr-FR-no-break-space-grouping",
        "fr-FR-narrow-no-break-space-grouping",
        "de-CH-apostrophe-grouping",
        "arabic-decimal-separator",
        "fullwidth-digits",
        "currency-symbol",
        "currency-code-suffix",
        "accounting-parentheses",
        "trailing-minus",
        "unicode-minus-sign",
    ],
)
def test_amount_grammar_rejects_what_decimal_and_pydantic_would_accept(text: str) -> None:
    with pytest.raises(ValueError, match="does not match"):
        parse_decimal_string(text)


@pytest.mark.unit
def test_stdlib_decimal_and_pydantic_accept_the_inputs_the_grammar_refuses() -> None:
    plain = TypeAdapter(Decimal)
    assert Decimal("\u0661\u0662.\u0663\u0664") == Decimal("12.34")
    assert plain.validate_json('"\u0661\u0662.\u0663\u0664"') == Decimal("12.34")
    assert plain.validate_json('"1_000"') == Decimal("1000")
    assert plain.validate_json('" 1.5 "') == Decimal("1.5")
    huge = plain.validate_json('"1E+1000000000"')
    assert huge.as_tuple().exponent == 1_000_000_000
    assert parse_decimal_string("12.34") == Decimal("12.34")
    assert str(parse_decimal_string("-0.00")) == "0.00"
    negative_zero = Money(currency_code="USD", amount=Decimal("-0.00"))
    assert negative_zero.model_dump_json() == '{"currency_code":"USD","amount":"0.00"}'


@pytest.mark.unit
def test_scale_is_bounded_by_the_iso_4217_minor_unit_of_the_currency() -> None:
    def money(code: str, amount: str) -> Money:
        return Money.model_validate({"currency_code": code, "amount": amount})

    assert money("JPY", "100").to_minor_units() == 100
    assert money("USD", "1.10").to_minor_units() == 110
    assert money("KWD", "1.005").to_minor_units() == 1005
    assert money("CLF", "1.0001").to_minor_units() == 10001
    with pytest.raises(ValidationError, match="JPY allows at most 0 fractional digits"):
        money("JPY", "100.5")
    with pytest.raises(ValidationError, match="USD allows at most 2 fractional digits"):
        money("USD", "1.005")
    with pytest.raises(ValidationError, match="USD allows at most 2 fractional digits"):
        Money(currency_code="USD", amount=Decimal("1E+3"))


@pytest.mark.unit
def test_money_serializes_as_fixed_scale_string_and_round_trips_bytes() -> None:
    assert usd("1.1").model_dump_json() == '{"currency_code":"USD","amount":"1.10"}'
    assert usd("-5").model_dump(mode="json") == {"currency_code": "USD", "amount": "-5.00"}
    yen = Money.from_minor_units(500, "JPY")
    assert yen.model_dump_json() == '{"currency_code":"JPY","amount":"500"}'
    dinar = Money.from_minor_units(1005, "KWD")
    wire = dinar.model_dump_json()
    assert wire == '{"currency_code":"KWD","amount":"1.005"}'
    assert Money.model_validate_json(wire).model_dump_json() == wire


@pytest.mark.unit
def test_money_json_schema_declares_a_string_in_both_modes() -> None:
    expected = {"type": "string", "pattern": DECIMAL_STRING_PATTERN, "title": "Amount"}
    assert Money.model_json_schema(mode="validation")["properties"]["amount"] == expected
    assert Money.model_json_schema(mode="serialization")["properties"]["amount"] == expected
    assert TypeAdapter(Decimal).json_schema() == {"anyOf": [{"type": "number"}, {"type": "string"}]}


@pytest.mark.unit
@pytest.mark.parametrize("code", ["usd", "US", "USDT", "XXX", "ZZZ", ""])
def test_currency_code_must_be_an_exact_supported_iso_4217_alpha_3(code: str) -> None:
    with pytest.raises(ValidationError, match="unsupported ISO 4217 currency_code"):
        Money.model_validate({"currency_code": code, "amount": "1"})


@pytest.mark.unit
def test_units_and_nanos_share_a_sign_as_google_type_money_requires() -> None:
    assert usd("-1.75").to_units_nanos() == (-1, -750_000_000)
    assert usd("-0.05").to_units_nanos() == (0, -50_000_000)
    assert usd("0.05").to_units_nanos() == (0, 50_000_000)
    assert usd("12").to_units_nanos() == (12, 0)


@pytest.mark.unit
def test_instant_normalizes_every_offset_to_utc_with_0_3_or_6_fraction_digits() -> None:
    parsed = parse_rfc3339_utc("2026-11-11T15:30:00+05:30")
    assert parsed == datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
    assert parsed.utcoffset() == timedelta(0)
    assert format_rfc3339_utc(parsed) == "2026-11-11T10:00:00Z"
    assert format_rfc3339_utc(parse_rfc3339_utc("1996-12-19T16:39:57-08:00")) == (
        "1996-12-20T00:39:57Z"
    )
    assert format_rfc3339_utc(parse_rfc3339_utc("2026-11-11T10:00:00.5Z")) == (
        "2026-11-11T10:00:00.500Z"
    )
    assert format_rfc3339_utc(parse_rfc3339_utc("2026-11-11T10:00:00.000001Z")) == (
        "2026-11-11T10:00:00.000001Z"
    )
    kolkata = datetime(2026, 11, 11, 15, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert format_rfc3339_utc(kolkata) == "2026-11-11T10:00:00Z"
    assert format_rfc3339_utc(datetime(1, 1, 1, tzinfo=UTC)) == "0001-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="naive"):
        format_rfc3339_utc(datetime(2026, 11, 11, 10, 0))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("2026-11-11T10:00:00", "must match RFC 3339"),
        (1762855200, "not a number"),
        (1762855200.5, "not a number"),
        ("1762855200", "must match RFC 3339"),
        ("2026-11-11t10:00:00z", "must match RFC 3339"),
        ("2026-11-11t10:00:00Z", "must match RFC 3339"),
        ("2026-11-11T10:00:00z", "must match RFC 3339"),
        ("2026-11-11 10:00:00Z", "must match RFC 3339"),
        ("20261111T100000Z", "must match RFC 3339"),
        ("2026-W46-3T10:00:00Z", "must match RFC 3339"),
        ("2026-11-11", "must match RFC 3339"),
        ("2026-11-11T10:00Z", "must match RFC 3339"),
        ("2026-11-11T24:00:00Z", "must match RFC 3339"),
        ("2026-11-11T10:00:00,5Z", "must match RFC 3339"),
        ("2026-11-11T10:00:00+0530", "must match RFC 3339"),
        ("2026-11-11T10:00:00Z\n", "must match RFC 3339"),
        ("2026-11-11T10:00:00Z[Europe/Paris]", "must match RFC 3339"),
        ("\u0662\u0660\u0662\u0666-11-11T10:00:00Z", "must match RFC 3339"),
        ("2026-11-11T10:00:00.1234567Z", "refusing to truncate"),
        ("1990-12-31T23:59:60Z", "leap second"),
        ("2026-02-30T10:00:00Z", "day"),
        ("0001-01-01T00:00:00+01:00", "outside years 0001 to 9999"),
        ("9999-12-31T23:59:59-01:00", "outside years 0001 to 9999"),
        (datetime(2026, 11, 11, 10, 0), "must carry a UTC offset"),
    ],
    ids=[
        "naive",
        "unix-seconds-int",
        "unix-seconds-float",
        "unix-seconds-string",
        "lowercase-t-z",
        "lowercase-t-only",
        "lowercase-z-only",
        "space-separator",
        "basic-format",
        "week-date",
        "date-only",
        "no-seconds",
        "hour-24",
        "comma-fraction",
        "offset-without-colon",
        "trailing-newline",
        "ixdtf-suffix",
        "arabic-indic-year",
        "seven-fraction-digits",
        "leap-second",
        "february-30",
        "before-year-1-in-utc",
        "after-year-9999-in-utc",
        "naive-datetime-instance",
    ],
)
def test_instant_rejects_naive_numeric_and_loose_iso_8601_forms(value: object, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_rfc3339_utc(value)


@pytest.mark.unit
def test_pydantic_lax_datetime_guesses_units_at_2e10_and_truncates_fractions() -> None:
    lax = TypeAdapter(datetime)
    assert lax.validate_json("20000000000") == datetime(2603, 10, 11, 11, 33, 20, tzinfo=UTC)
    assert lax.validate_json("20000000001") == datetime(1970, 8, 20, 11, 33, 20, 1000, tzinfo=UTC)
    assert lax.validate_json('"2026-11-11T10:00:00"').tzinfo is None
    assert lax.validate_json('"2026-11-11T10:00:00.123456789Z"').microsecond == 123456
    assert lax.dump_json(datetime(2026, 11, 11, 10, 0)) == b'"2026-11-11T10:00:00"'
    with pytest.raises(ValueError, match="not a number"):
        parse_rfc3339_utc(20000000001)


@pytest.mark.unit
def test_stdlib_fromisoformat_hour_24_depends_on_the_cpython_version() -> None:
    text = "2026-11-11T24:00:00+00:00"
    if sys.version_info >= (3, 14):
        assert datetime.fromisoformat(text) == datetime(2026, 11, 12, 0, 0, tzinfo=UTC)
    else:
        with pytest.raises(ValueError, match=r"hour must be in 0\.\.23"):
            datetime.fromisoformat(text)
    with pytest.raises(ValueError, match="must match RFC 3339"):
        parse_rfc3339_utc("2026-11-11T24:00:00Z")


@pytest.mark.unit
def test_serializers_disagree_on_utc_suffix_and_decimal_support() -> None:
    instant = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
    assert instant.isoformat() == "2026-11-11T10:00:00+00:00"
    assert msgspec.json.encode(instant) == b'"2026-11-11T10:00:00Z"'
    assert TypeAdapter(datetime).dump_json(instant) == b'"2026-11-11T10:00:00Z"'
    assert format_rfc3339_utc(instant) == "2026-11-11T10:00:00Z"
    price = Decimal("1.10")
    with pytest.raises(TypeError, match="not JSON serializable"):
        json.dumps(price)
    assert msgspec.json.encode(price) == b'"1.10"'
    assert msgspec.json.decode(b"1.000000000000000000001", type=Decimal) == Decimal(
        "1.000000000000000000001"
    )
    assert TypeAdapter(Decimal).dump_json(price) == b'"1.10"'


@pytest.mark.unit
def test_orjson_prints_a_numeric_utc_offset_unless_told_otherwise_and_refuses_decimal() -> None:
    orjson = pytest.importorskip("orjson", reason="orjson 3.13.0 has no free-threaded build")
    instant = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
    assert orjson.dumps(instant) == b'"2026-11-11T10:00:00+00:00"'
    assert orjson.dumps(instant, option=orjson.OPT_UTC_Z) == b'"2026-11-11T10:00:00Z"'
    with pytest.raises(TypeError, match="not JSON serializable"):
        orjson.dumps(Decimal("1.10"))


@pytest.mark.unit
def test_seconds_duration_round_trips_exactly_without_binary64() -> None:
    assert int(float("0.000249") * 1_000_000) == 248
    assert parse_seconds_duration("0.000249s") == timedelta(microseconds=249)
    assert format_seconds_duration(parse_seconds_duration("0.000249s")) == "0.000249s"
    assert format_seconds_duration(parse_seconds_duration("8.2s")) == "8.200s"
    assert format_seconds_duration(parse_seconds_duration("3s")) == "3s"
    assert format_seconds_duration(parse_seconds_duration("0.000001s")) == "0.000001s"
    assert format_seconds_duration(parse_seconds_duration("-1.5s")) == "-1.500s"
    assert format_seconds_duration(timedelta(hours=36, minutes=5)) == "129900s"
    assert parse_seconds_duration("315576000000s") == timedelta(seconds=315_576_000_000)
    with pytest.raises(ValueError, match="outside"):
        parse_seconds_duration("315576000000.000001s")
    with pytest.raises(ValueError, match="outside"):
        parse_seconds_duration("315576000001s")
    with pytest.raises(ValueError, match="outside"):
        parse_seconds_duration(timedelta(seconds=315_576_000_001))
    assert parse_seconds_duration("-315576000000s") == timedelta(seconds=-315_576_000_000)
    with pytest.raises(ValueError, match="outside"):
        parse_seconds_duration("-315576000001s")
    with pytest.raises(ValueError, match="outside"):
        parse_seconds_duration(timedelta(seconds=-315_576_000_001))
    almost_maximum = timedelta(seconds=315_575_999_999, microseconds=999_999)
    assert almost_maximum.total_seconds() == 315_576_000_000.0
    assert format_seconds_duration(almost_maximum) == "315575999999.999999s"
    assert parse_seconds_duration("315575999999.999999s") == almost_maximum


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "P1M",
        "P1Y",
        "PT3.5S",
        "P1D",
        "3.5",
        3.5,
        3,
        "1e3s",
        "3.5S",
        "3.5 s",
        "03s",
        "1.0000001s",
        "3s\n",
        "3.5s ",
    ],
    ids=[
        "iso-month",
        "iso-year",
        "iso-seconds",
        "iso-day",
        "bare-string",
        "json-float",
        "json-int",
        "exponent",
        "uppercase-suffix",
        "space-before-suffix",
        "leading-zero",
        "seven-fraction-digits",
        "trailing-newline",
        "trailing-space",
    ],
)
def test_duration_refuses_calendar_units_numbers_and_loose_forms(value: object) -> None:
    with pytest.raises(ValueError, match="duration must be"):
        parse_seconds_duration(value)


@pytest.mark.unit
def test_pydantic_timedelta_scales_calendar_units_to_fixed_days() -> None:
    lax = TypeAdapter(timedelta)
    assert lax.validate_json('"P1M"') == timedelta(days=30)
    assert lax.validate_json('"P1Y"') == timedelta(days=365)
    assert lax.validate_json('"PT1M"') == timedelta(seconds=60)
    assert lax.dump_json(timedelta(seconds=3.5)) == b'"PT3.5S"'
    february = datetime(2024, 2, 1, tzinfo=UTC)
    assert february + lax.validate_json('"P1M"') == datetime(2024, 3, 2, tzinfo=UTC)
    with pytest.raises(ValueError, match="calendar units are refused"):
        parse_seconds_duration("P1M")


@pytest.mark.unit
@pytest.mark.parametrize(
    "zone_id",
    ["america/new_york", "../../etc/passwd", "/etc/passwd", "", "Mars/Olympus", "posixrules", 5],
    ids=["wrong-case", "traversal", "absolute", "empty", "unknown", "posixrules", "not-a-string"],
)
def test_zone_id_must_be_an_exact_key_of_the_iana_database(zone_id: object) -> None:
    with pytest.raises(ValueError, match="unknown IANA time zone"):
        validate_zone_id(zone_id)
    with pytest.raises(ValidationError, match="unknown IANA time zone"):
        LocalEvent.model_validate({"local_time": "2024-07-04T09:00:00", "time_zone": zone_id})
    if isinstance(zone_id, str):
        with pytest.raises(ValueError, match="unknown IANA time zone"):
            resolve_civil_time(datetime(2024, 7, 4, 9, 0), zone_id)


@pytest.mark.unit
def test_known_zone_ids_validate_and_same_wall_time_moves_in_utc_across_dst() -> None:
    assert validate_zone_id(NEW_YORK) == NEW_YORK
    assert validate_zone_id("Asia/Kolkata") == "Asia/Kolkata"
    before = resolve_civil_time(datetime(2024, 3, 9, 9, 0), NEW_YORK)
    after = resolve_civil_time(datetime(2024, 3, 11, 9, 0), NEW_YORK)
    assert before == datetime(2024, 3, 9, 14, 0, tzinfo=UTC)
    assert after == datetime(2024, 3, 11, 13, 0, tzinfo=UTC)
    assert after - before == timedelta(hours=47)
    with pytest.raises(ValueError, match="must be naive"):
        resolve_civil_time(datetime(2024, 3, 9, 9, 0, tzinfo=UTC), NEW_YORK)


@pytest.mark.unit
def test_nonexistent_wall_time_in_the_spring_gap_is_rejected_not_shifted() -> None:
    gap = datetime(2024, 3, 10, 2, 30)
    silently_shifted = gap.replace(tzinfo=ZoneInfo(NEW_YORK)).astimezone(UTC)
    assert silently_shifted == datetime(2024, 3, 10, 7, 30, tzinfo=UTC)
    assert silently_shifted.astimezone(ZoneInfo(NEW_YORK)).hour == 3
    with pytest.raises(ValueError, match="does not exist in America/New_York"):
        resolve_civil_time(gap, NEW_YORK)
    with pytest.raises(ValueError, match="does not exist in America/New_York"):
        resolve_civil_time(gap, NEW_YORK, fold=1)


@pytest.mark.unit
def test_ambiguous_wall_time_in_the_autumn_fold_needs_an_explicit_choice() -> None:
    event = LocalEvent.model_validate_json(
        '{"local_time": "2024-11-03T01:30:00", "time_zone": "America/New_York"}'
    )
    with pytest.raises(ValueError, match="occurs twice in America/New_York"):
        event.instant()
    assert event.instant(fold=0) == datetime(2024, 11, 3, 5, 30, tzinfo=UTC)
    assert event.instant(fold=1) == datetime(2024, 11, 3, 6, 30, tzinfo=UTC)
    with pytest.raises(ValidationError, match="no offset"):
        LocalEvent.model_validate_json(
            '{"local_time": "2024-11-03T01:30:00-04:00", "time_zone": "America/New_York"}'
        )
    with pytest.raises(ValidationError, match="no offset"):
        LocalEvent(local_time=datetime(2024, 11, 3, 1, 30, tzinfo=UTC), time_zone=NEW_YORK)
    with pytest.raises(ValueError, match="outside years 0001 to 9999"):
        resolve_civil_time(datetime(1, 1, 1), "Asia/Kolkata")


@pytest.mark.unit
def test_civil_time_refuses_fractions_and_loose_forms_and_accepts_whole_seconds() -> None:
    event = LocalEvent(local_time=datetime(2024, 7, 4, 9, 0), time_zone=NEW_YORK)
    assert event.model_dump_json() == (
        '{"local_time":"2024-07-04T09:00:00","time_zone":"America/New_York"}'
    )
    assert LocalEvent.model_validate_json(event.model_dump_json()) == event
    with pytest.raises(ValidationError, match="no offset"):
        LocalEvent(local_time=datetime(2024, 7, 4, 9, 0, 0, 500), time_zone=NEW_YORK)
    for text in ("2024-07-04T09:00:00.5", "2024-07-04 09:00:00", "2024-07-04T09:00", "2024-07-04"):
        with pytest.raises(ValidationError, match="no offset"):
            LocalEvent.model_validate({"local_time": text, "time_zone": NEW_YORK})


@pytest.mark.unit
def test_models_are_closed_and_frozen() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Money.model_validate_json(
            '{"currency_code": "USD", "amount": "1.00", "discounted_amount": "0.50"}'
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        LocalEvent.model_validate_json(
            '{"local_time": "2024-07-04T09:00:00", "time_zone": "America/New_York",'
            ' "utc_offset": "-04:00"}'
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PaymentRequest.model_validate_json(
            '{"amount": {"currency_code": "USD", "amount": "19.99"},'
            ' "capture_time": "2026-11-11T10:00:00Z", "hold_duration": "3s", "tip": "1.00"}'
        )
    with pytest.raises(ValidationError, match="Input should be a valid string"):
        Money.model_validate({"currency_code": b"USD", "amount": "1.00"})
    money = usd("1.00")
    event = LocalEvent(local_time=datetime(2024, 7, 4, 9, 0), time_zone=NEW_YORK)
    for model, field in ((money, "amount"), (money, "currency_code"), (event, "time_zone")):
        with pytest.raises(ValidationError, match="Instance is frozen"):
            setattr(model, field, "JPY")
    assert money.model_dump_json() == '{"currency_code":"USD","amount":"1.00"}'


@pytest.mark.unit
def test_payment_schema_publishes_a_seconds_pattern_not_format_duration() -> None:
    assert TypeAdapter(timedelta).json_schema() == {"type": "string", "format": "duration"}
    for mode in ("validation", "serialization"):
        properties = PaymentRequest.model_json_schema(mode=mode)["properties"]
        assert properties["hold_duration"] == {
            "type": "string",
            "pattern": r"^-?(0|[1-9][0-9]{0,11})(\.[0-9]{1,6})?s$",
            "title": "Hold Duration",
        }
        assert LocalEvent.model_json_schema(mode=mode)["properties"]["local_time"] == {
            "type": "string",
            "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$",
            "title": "Local Time",
        }
        assert properties["capture_time"] == {
            "type": "string",
            "format": "date-time",
            "title": "Capture Time",
        }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_payment_endpoint_emits_canonical_wire_forms() -> None:
    transport = httpx.ASGITransport(app=build_payments_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/payments",
            content=(
                b'{"amount": {"currency_code": "USD", "amount": "19.99"},'
                b' "capture_time": "2026-11-11T15:30:00+05:30", "hold_duration": "3.5s"}'
            ),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 200
    assert response.content == (
        b'{"amount":{"currency_code":"USD","amount":"19.99"},"amount_minor_units":1999,'
        b'"capture_time":"2026-11-11T10:00:00Z","expire_time":"2026-11-11T10:00:03.500Z",'
        b'"hold_duration":"3.500s"}'
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_payment_endpoint_answers_422_for_number_amount_epoch_time_and_iso_month() -> None:
    transport = httpx.ASGITransport(app=build_payments_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/payments",
            content=(
                b'{"amount": {"currency_code": "USD", "amount": 19.99},'
                b' "capture_time": 1762855200, "hold_duration": "P1M"}'
            ),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 422
    errors = {tuple(item["loc"]): item["msg"] for item in response.json()["detail"]}
    assert set(errors) == {
        ("body", "amount", "amount"),
        ("body", "capture_time"),
        ("body", "hold_duration"),
    }
    assert "not a JSON number" in errors["body", "amount", "amount"]
    assert "not a number" in errors["body", "capture_time"]
    assert "calendar units are refused" in errors["body", "hold_duration"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_payment_endpoint_answers_422_when_expire_time_leaves_the_calendar() -> None:
    transport = httpx.ASGITransport(app=build_payments_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/payments",
            content=(
                b'{"amount": {"currency_code": "JPY", "amount": "500"},'
                b' "capture_time": "2026-01-01T00:00:00Z", "hold_duration": "315576000000s"}'
            ),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 422
    assert response.json() == {
        "detail": "capture_time + hold_duration is outside years 0001 to 9999"
    }


@pytest.mark.unit
def test_python_json_emits_non_json_tokens_for_non_finite_floats_unless_forbidden() -> None:
    assert json.dumps({"rate": float("inf")}) == '{"rate": Infinity}'
    assert json.dumps(float("nan")) == "NaN"
    assert json.dumps(1e400) == "Infinity"
    with pytest.raises(ValueError, match="Out of range float values are not JSON compliant"):
        json.dumps(float("nan"), allow_nan=False)
    assert TypeAdapter(float).dump_json(float("nan")) == b"null"
    for token in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError, match="does not match"):
            parse_decimal_string(token)


@pytest.mark.unit
def test_rounding_mode_is_a_named_argument_because_the_defaults_disagree() -> None:
    assert round(2.675, 2) == 2.67
    assert round(Decimal("2.675"), 2) == Decimal("2.68")
    assert round(Decimal("0.125"), 2) == Decimal("0.12")
    assert (round(0.5), round(1.5), round(2.5)) == (0, 2, 2)
    exact = Decimal("0.125")
    assert quantize_to_currency(exact, "USD", rounding=ROUND_HALF_EVEN) == usd("0.12")
    assert quantize_to_currency(exact, "USD", rounding=ROUND_HALF_UP) == usd("0.13")
    assert quantize_to_currency(Decimal("-0.125"), "USD", rounding=ROUND_HALF_UP) == usd("-0.13")
    assert quantize_to_currency(Decimal("0.129"), "USD", rounding=ROUND_DOWN) == usd("0.12")
    yen = quantize_to_currency(Decimal("100.5"), "JPY", rounding=ROUND_HALF_EVEN)
    assert yen.model_dump_json() == '{"currency_code":"JPY","amount":"100"}'
    dinar = quantize_to_currency(Decimal("1.0005"), "KWD", rounding=ROUND_HALF_UP)
    assert dinar.model_dump_json() == '{"currency_code":"KWD","amount":"1.001"}'
    tax = usd("19.99").amount * Decimal("0.0825")
    assert tax == Decimal("1.649175")
    assert quantize_to_currency(tax, "USD", rounding=ROUND_HALF_EVEN).to_minor_units() == 165
    with pytest.raises(ValueError, match="unsupported ISO 4217 currency_code"):
        quantize_to_currency(exact, "ZZZ", rounding=ROUND_HALF_EVEN)
    for unbounded in ("1E+40", "NaN", "Infinity"):
        with pytest.raises(ValueError, match="cannot be rounded to a finite minor-unit value"):
            quantize_to_currency(Decimal(unbounded), "USD", rounding=ROUND_HALF_EVEN)
    with pytest.raises(ValidationError, match=r"exceeds 2\*\*53 - 1 minor units"):
        quantize_to_currency(Decimal("90071992547409.915"), "USD", rounding=ROUND_HALF_UP)


@pytest.mark.unit
def test_allocation_by_largest_remainder_never_creates_or_destroys_a_minor_unit() -> None:
    third = (Decimal("100.00") / 3).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    assert third * 3 == Decimal("99.99")
    assert allocate_minor_units(10000, [1, 1, 1]) == [3334, 3333, 3333]
    assert allocate_minor_units(100, [70, 30]) == [70, 30]
    assert allocate_minor_units(5, [3, 7]) == [2, 3]
    assert allocate_minor_units(1, [1, 2, 2]) == [0, 1, 0]
    assert allocate_minor_units(7, [0, 1]) == [0, 7]
    assert allocate_minor_units(0, [1, 1]) == [0, 0]
    assert allocate_minor_units(2, [1, 1, 1]) == [1, 1, 0]
    for total, weights in ((10001, [1, 1, 1]), (999, [5, 3, 2, 1]), (MAX_SAFE_INTEGER, [1, 3])):
        assert sum(allocate_minor_units(total, weights)) == total
    refused: list[tuple[int, list[int]]] = [
        (-1, [1]),
        (True, [1]),
        (1, []),
        (1, [0, 0]),
        (1, [2, -1]),
        (1, [True]),
    ]
    for total, weights in refused:
        with pytest.raises(ValueError, match="must be"):
            allocate_minor_units(total, weights)


@pytest.mark.unit
def test_money_addition_is_exact_bounded_and_refuses_a_second_currency() -> None:
    assert (usd("0.10") + usd("0.20")).model_dump_json() == (
        '{"currency_code":"USD","amount":"0.30"}'
    )
    assert (usd("-5.00") + usd("5.00")).model_dump_json() == (
        '{"currency_code":"USD","amount":"0.00"}'
    )
    euro = Money.model_validate({"currency_code": "EUR", "amount": "1.00"})
    with pytest.raises(ValueError, match="cannot add EUR to USD"):
        _ = usd("1.00") + euro
    with pytest.raises(ValidationError, match=r"exceeds 2\*\*53 - 1 minor units"):
        _ = Money.from_minor_units(MAX_SAFE_INTEGER, "USD") + usd("0.01")


@pytest.mark.unit
def test_instant_strings_sort_chronologically_only_at_a_fixed_fraction_width() -> None:
    earlier = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
    later = earlier + timedelta(milliseconds=500)
    shortest = [format_rfc3339_utc(earlier), format_rfc3339_utc(later)]
    assert shortest == ["2026-11-11T10:00:00Z", "2026-11-11T10:00:00.500Z"]
    assert sorted(shortest) == [shortest[1], shortest[0]]
    fixed = [format_rfc3339_utc(value, fraction_digits=6) for value in (earlier, later)]
    assert fixed == ["2026-11-11T10:00:00.000000Z", "2026-11-11T10:00:00.500000Z"]
    assert sorted(fixed) == fixed
    assert format_rfc3339_utc(later, fraction_digits=3) == "2026-11-11T10:00:00.500Z"
    assert format_rfc3339_utc(earlier, fraction_digits=3) == "2026-11-11T10:00:00.000Z"
    assert format_rfc3339_utc(earlier, fraction_digits=0) == "2026-11-11T10:00:00Z"
    with pytest.raises(ValueError, match="needs more than 0 fractional digits"):
        format_rfc3339_utc(later, fraction_digits=0)
    with pytest.raises(ValueError, match="needs more than 3 fractional digits"):
        format_rfc3339_utc(earlier + timedelta(microseconds=1), fraction_digits=3)
    mixed_offsets = ["2026-11-11T09:59:00Z", "2026-11-11T05:00:00-05:00"]
    assert sorted(mixed_offsets) == [mixed_offsets[1], mixed_offsets[0]]
    assert parse_rfc3339_utc(mixed_offsets[0]) < parse_rfc3339_utc(mixed_offsets[1])


@pytest.mark.unit
def test_pydantic_lax_date_accepts_date_times_and_epoch_numbers_and_drops_the_offset() -> None:
    lax = TypeAdapter(date)
    assert lax.validate_json('"2026-11-11T00:00:00"') == date(2026, 11, 11)
    assert lax.validate_json('"2026-11-11T00:00:00+05:30"') == date(2026, 11, 11)
    assert parse_rfc3339_utc("2026-11-11T00:00:00+05:30").date() == date(2026, 11, 10)
    assert lax.validate_json("1762819200") == date(2025, 11, 11)
    assert lax.validate_json('"1762819200"') == date(2025, 11, 11)
    assert lax.json_schema() == {"type": "string", "format": "date"}
    strict = TypeAdapter(CivilDate)
    assert strict.validate_json('"2026-11-11"') == date(2026, 11, 11)
    assert strict.dump_json(date(2026, 11, 11)) == b'"2026-11-11"'
    assert strict.json_schema() == {"type": "string", "format": "date"}
    assert strict.validate_python(date(2026, 11, 11)) == date(2026, 11, 11)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("2026-11-11T00:00:00", "no time and no offset"),
        ("2026-11-11T00:00:00Z", "no time and no offset"),
        (1762819200, "no time and no offset"),
        ("1762819200", "no time and no offset"),
        ("20261111", "no time and no offset"),
        ("2026-W46-3", "no time and no offset"),
        ("2026-315", "no time and no offset"),
        ("11/12/2026", "no time and no offset"),
        ("12.11.2026", "no time and no offset"),
        ("2026-11-11\n", "no time and no offset"),
        ("\u0662\u0660\u0662\u0666-11-11", "no time and no offset"),
        (datetime(2026, 11, 11), "no time and no offset"),
        ("2026-02-30", "day"),
        ("2026-13-01", "month"),
        ("0000-01-01", "year"),
    ],
    ids=[
        "date-time-at-midnight",
        "utc-midnight",
        "epoch-number",
        "epoch-string",
        "basic-format",
        "week-date",
        "ordinal-date",
        "us-or-uk-slashes",
        "de-DE-dots",
        "trailing-newline",
        "arabic-indic-year",
        "datetime-instance",
        "february-30",
        "month-13",
        "year-zero",
    ],
)
def test_civil_date_accepts_only_rfc_3339_full_date(value: object, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_full_date(value)
    with pytest.raises(ValidationError):
        TypeAdapter(CivilDate).validate_python(value)


@pytest.mark.unit
def test_month_arithmetic_clamps_to_month_end_and_must_restart_from_the_anchor() -> None:
    anchor = date(2024, 1, 31)
    assert add_months(anchor, 1) == date(2024, 2, 29)
    assert add_months(date(2023, 1, 31), 1) == date(2023, 2, 28)
    assert add_months(anchor, 2) == date(2024, 3, 31)
    assert add_months(add_months(anchor, 1), 1) == date(2024, 3, 29)
    assert add_months(anchor, 11) == date(2024, 12, 31)
    assert add_months(anchor, 12) == date(2025, 1, 31)
    assert add_months(date(2024, 3, 31), -1) == date(2024, 2, 29)
    assert add_months(date(2024, 1, 15), -1) == date(2023, 12, 15)
    assert add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)
    assert add_months(date(2024, 2, 1), 1) - date(2024, 2, 1) == timedelta(days=29)
    assert TypeAdapter(timedelta).validate_json('"P1M"') == timedelta(days=30)
    for start, months in ((date(9999, 12, 1), 1), (date(1, 1, 1), -1)):
        with pytest.raises(ValueError, match="outside years 0001 to 9999"):
            add_months(start, months)


@pytest.mark.unit
def test_interval_is_half_open_so_adjacent_intervals_share_no_instant() -> None:
    noon = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
    morning = Interval.model_validate_json(
        '{"start_time": "2026-11-11T00:00:00Z", "end_time": "2026-11-11T12:00:00Z"}'
    )
    afternoon = Interval(start_time=noon, end_time=noon + timedelta(hours=12))
    assert morning.contains(morning.start_time)
    assert morning.contains(noon - timedelta(microseconds=1))
    assert not morning.contains(noon)
    assert afternoon.contains(noon)
    assert [span.contains(noon) for span in (morning, afternoon)].count(True) == 1
    empty = Interval(start_time=noon, end_time=noon)
    assert not empty.contains(noon)
    assert morning.model_dump_json() == (
        '{"start_time":"2026-11-11T00:00:00Z","end_time":"2026-11-11T12:00:00Z"}'
    )
    with pytest.raises(ValidationError, match="start_time must not be after end_time"):
        Interval(start_time=noon, end_time=noon - timedelta(microseconds=1))
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Interval.model_validate(
            {"start_time": "2026-11-11T00:00:00Z", "end_time": "2026-11-11T12:00:00Z", "tz": "x"}
        )
    for field in ("start_time", "end_time"):
        with pytest.raises(ValidationError, match="Instance is frozen"):
            setattr(morning, field, noon + timedelta(hours=1))
    with pytest.raises(TypeError, match="offset-naive and offset-aware"):
        morning.contains(datetime(2026, 11, 11, 6, 0))


@pytest.mark.unit
def test_zone_database_keys_include_inverted_signs_second_offsets_and_renamed_links() -> None:
    etc = ZoneInfo(validate_zone_id("Etc/GMT+5"))
    assert etc.utcoffset(datetime(2026, 1, 1)) == timedelta(hours=-5)
    assert datetime(2026, 1, 1, 12, tzinfo=etc).isoformat() == "2026-01-01T12:00:00-05:00"
    local_mean_time = datetime(1880, 1, 1, 12, tzinfo=ZoneInfo(NEW_YORK))
    assert local_mean_time.utcoffset() == -timedelta(hours=4, minutes=56, seconds=2)
    assert local_mean_time.isoformat() == "1880-01-01T12:00:00-04:56:02"
    with pytest.raises(ValueError, match="must match RFC 3339"):
        parse_rfc3339_utc(local_mean_time.isoformat())
    assert format_rfc3339_utc(local_mean_time) == "1880-01-01T16:56:02Z"
    summer = datetime(2026, 7, 1, 12, 0)
    old_name = resolve_civil_time(summer, validate_zone_id("Europe/Kiev"))
    new_name = resolve_civil_time(summer, validate_zone_id("Europe/Kyiv"))
    assert old_name == new_name == datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    for not_a_zone in ("EDT", "PST", "UTC+5", "-05:00", "GMT+5", "Etc/Unknown"):
        with pytest.raises(ValueError, match="unknown IANA time zone"):
            validate_zone_id(not_a_zone)


@pytest.mark.unit
def test_published_schema_patterns_accept_exactly_what_the_parsers_accept() -> None:
    def accepted(parser: Callable[[object], object], text: str) -> bool:
        try:
            parser(text)
        except ValueError:
            return False
        return True

    corpora: list[tuple[str, Callable[[object], object], list[str]]] = [
        (
            DECIMAL_STRING_PATTERN,
            parse_decimal_string,
            [
                "0",
                "-0",
                "19.99",
                "-5",
                "0.0001",
                "123456789012345678",
                "1234567890123456789",
                "01",
                "1.",
                ".5",
                "1.23456",
                "+1",
                "1e3",
                "1,5",
                " 1",
                "1 ",
                "",
                "-",
                "1.5s",
            ],
        ),
        (
            SECONDS_DURATION_PATTERN,
            parse_seconds_duration,
            [
                "0s",
                "3s",
                "-1.5s",
                "0.000001s",
                "315576000000s",
                "999999999999s",
                "1.0000001s",
                "03s",
                "3S",
                "3",
                "s",
                "-s",
                "3.s",
                ".5s",
                "PT3S",
                "3 s",
                "1e3s",
                "1234567890123s",
            ],
        ),
        (
            LOCAL_DATE_TIME_PATTERN,
            parse_local_date_time,
            [
                "2024-11-03T01:30:00",
                "2024-12-31T23:59:59",
                "0001-01-01T00:00:00",
                "2024-11-03T24:00:00",
                "2024-11-03T23:60:00",
                "2024-11-03T23:59:60",
                "2024-11-03T01:30",
                "2024-11-03 01:30:00",
                "2024-11-03T01:30:00Z",
                "2024-11-03",
                "2024-11-03T01:30:00.5",
                "2024-11-03T::::::::",
                "2024-11-03T99:99:99",
            ],
        ),
    ]
    for published, parser, corpus in corpora:
        assert published.startswith("^")
        assert published.endswith("$")
        grammar = re.compile(published[1:-1])
        verdicts = {text: grammar.fullmatch(text) is not None for text in corpus}
        assert True in verdicts.values()
        assert False in verdicts.values()
        for text, matches in verdicts.items():
            if text != "999999999999s":
                assert accepted(parser, text) is matches, text
    assert not accepted(parse_seconds_duration, "999999999999s")
