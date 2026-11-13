"""Lax versus strict validation: the coercion matrix, precedence, scope, and the HTTP edge."""

from __future__ import annotations

from collections import UserDict
from datetime import date
from typing import Annotated, Any

import httpx
import pytest
from pydantic import Strict, TypeAdapter, ValidationError

from api_labs.f09_pydantic_v2_internals_and_validation.coercion_matrix import (
    ORDER_ID,
    FieldStrictOrder,
    MatrixRow,
    ParsedQty,
    Side,
    StrictTicket,
    Venue,
    build_matrix,
    build_trade_app,
    model_outcome,
)

D = "date:datetime.date(2026, 11, 13)"
U = f"UUID:UUID('{ORDER_ID}')"
DEC = "Decimal:Decimal('1.10')"

EXPECTED_MATRIX: dict[str, MatrixRow] = {
    "int <- '42'": MatrixRow("int:42", "!int_type", "int:42", "!int_type"),
    "int <- 42.0": MatrixRow("int:42", "!int_type", "int:42", "!int_type"),
    "int <- 42.5": MatrixRow("!int_from_float", "!int_type", "!int_from_float", "!int_type"),
    "int <- True": MatrixRow("int:1", "!int_type", "int:1", "!int_type"),
    "float <- 1": MatrixRow("float:1.0", "float:1.0", "float:1.0", "float:1.0"),
    "float <- '1.5'": MatrixRow("float:1.5", "!float_type", "float:1.5", "!float_type"),
    "str <- 42": MatrixRow("!string_type", "!string_type", "!string_type", "!string_type"),
    "bool <- 'yes'": MatrixRow("bool:True", "!bool_type", "bool:True", "!bool_type"),
    "bool <- 2": MatrixRow("!bool_parsing", "!bool_type", "!bool_parsing", "!bool_type"),
    "date <- '2026-11-13'": MatrixRow(D, "!date_type", D, D),
    "UUID <- str": MatrixRow(U, "!is_instance_of", U, U),
    "Decimal <- '1.10'": MatrixRow(DEC, "!is_instance_of", DEC, DEC),
    "bytes <- 'abc'": MatrixRow("bytes:b'abc'", "!bytes_type", "bytes:b'abc'", "bytes:b'abc'"),
    "tuple <- list": MatrixRow("tuple:(1, 2)", "!tuple_type", "tuple:(1, 2)", "tuple:(1, 2)"),
}


@pytest.mark.unit
def test_coercion_matrix_python_and_json_in_lax_and_strict() -> None:
    matrix = build_matrix()
    assert matrix == EXPECTED_MATRIX
    json_only_strict = sorted(
        name
        for name, row in matrix.items()
        if row.json_strict == row.json_lax
        and row.python_strict.startswith("!")
        and not row.json_strict.startswith("!")
    )
    assert json_only_strict == [
        "Decimal <- '1.10'",
        "UUID <- str",
        "bytes <- 'abc'",
        "date <- '2026-11-13'",
        "tuple <- list",
    ]


@pytest.mark.unit
def test_strict_scalars_accept_subclasses_and_reject_bool() -> None:
    int_adapter: TypeAdapter[int] = TypeAdapter(int)
    str_adapter: TypeAdapter[str] = TypeAdapter(str)
    venue = int_adapter.validate_python(Venue.NASDAQ, strict=True)
    side = str_adapter.validate_python(Side.BUY, strict=True)
    assert (type(venue), venue) == (int, 1)
    assert (type(side), side) == (str, "buy")
    with pytest.raises(ValidationError) as err:
        int_adapter.validate_python(True, strict=True)
    assert err.value.errors()[0]["type"] == "int_type"


@pytest.mark.unit
def test_lax_numeric_parsing_edges_and_digit_limits() -> None:
    ints: TypeAdapter[int] = TypeAdapter(int)
    assert [ints.validate_python(s) for s in (" 42 ", "4_2", "042", "42.000")] == [42] * 4

    def first_error(value: Any) -> str:
        with pytest.raises(ValidationError) as err:
            ints.validate_python(value)
        return str(err.value.errors()[0]["type"])

    def first_json_error(text: str) -> str:
        with pytest.raises(ValidationError) as err:
            ints.validate_json(text)
        return str(err.value.errors()[0]["type"])

    assert first_error("0x2a") == "int_parsing"
    assert first_error("1e3") == "int_parsing"
    assert first_error(1e20) == "int_parsing_size"
    assert first_error(float("inf")) == "finite_number"
    assert ints.validate_json("1e3") == 1000
    assert len(str(ints.validate_python("9" * 4300))) == 4300
    assert first_error("9" * 4301) == "int_parsing_size"
    assert first_json_error("9" * 4301) == "json_invalid"


@pytest.mark.unit
def test_runtime_strict_argument_overrides_field_strictness_both_ways() -> None:
    data = {"qty": "5", "symbol": b"AAPL", "limit_ticks": "3"}
    assert model_outcome(FieldStrictOrder, data, strict=None) == [
        (("qty",), "int_type"),
        (("symbol",), "string_type"),
        (("limit_ticks",), "int_type"),
    ]
    assert model_outcome(FieldStrictOrder, data, strict=False) == {
        "qty": 5,
        "symbol": "AAPL",
        "limit_ticks": 3,
    }
    strings = FieldStrictOrder.model_validate_strings(
        {"qty": "5", "symbol": "AAPL", "limit_ticks": "3"}, strict=True
    )
    assert (strings.qty, strings.limit_ticks) == (5, 3)


@pytest.mark.unit
def test_config_strict_scope_versus_runtime_strict() -> None:
    data = {
        "td_leg": {"qty": "1"},
        "model_leg": {"qty": "1"},
        "dc_leg": {"qty": "1"},
        "override": "5",
    }
    assert model_outcome(StrictTicket, data, strict=None) == [(("td_leg", "qty"), "int_type")]
    assert model_outcome(StrictTicket, data, strict=True) == [
        (("td_leg", "qty"), "int_type"),
        (("model_leg", "qty"), "int_type"),
        (("dc_leg",), "dataclass_exact_type"),
        (("override",), "int_type"),
    ]
    clean = {"td_leg": {"qty": 1}, "model_leg": {"qty": 1}, "dc_leg": {"qty": 1}}
    assert model_outcome(StrictTicket, clean, strict=None) == {
        "td_leg": {"qty": 1},
        "model_leg": {"qty": 1},
        "dc_leg": {"qty": 1},
        "override": 0,
    }
    assert model_outcome(StrictTicket, UserDict(clean), strict=None) == [((), "model_type")]


@pytest.mark.unit
def test_smart_union_json_strings_tie_and_member_order_decides() -> None:
    date_first: TypeAdapter[date | str] = TypeAdapter(date | str)
    str_first: TypeAdapter[str | date] = TypeAdapter(str | date)
    assert date_first.validate_json('"2026-01-01"') == date(2026, 1, 1)
    assert str_first.validate_json('"2026-01-01"') == "2026-01-01"
    assert date_first.validate_python("2026-01-01") == "2026-01-01"


@pytest.mark.unit
def test_before_validator_runs_ahead_of_strict_inner_schema() -> None:
    parsed: TypeAdapter[int] = TypeAdapter(ParsedQty)
    plain: TypeAdapter[int] = TypeAdapter(Annotated[int, Strict()])
    assert parsed.validate_python("7") == 7
    schema: Any = parsed.core_schema
    assert schema["type"] == "function-before"
    assert schema["schema"] == {"type": "int", "strict": True}
    with pytest.raises(ValidationError) as err:
        plain.validate_python("7")
    assert err.value.errors()[0]["type"] == "int_type"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_body_is_python_input_so_strict_date_rejects_iso_string() -> None:
    app = build_trade_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = {"trade_date": "2026-11-13", "qty": 3}
        parsed = await client.post("/v1/trades/parsed", json=body)
        assert parsed.status_code == 422
        detail = parsed.json()["detail"][0]
        assert (detail["type"], detail["loc"]) == ("date_type", ["body", "trade_date"])

        raw = await client.post("/v1/trades/raw", json=body)
        assert (raw.status_code, raw.json()) == (200, {"trade_date": "2026-11-13"})

        bad = await client.post("/v1/trades/raw", json={"trade_date": "2026-11-13", "qty": "3"})
        assert bad.status_code == 422
        assert [(d["type"], d["loc"]) for d in bad.json()["detail"]] == [("int_type", ["qty"])]
