"""Single-pass model_validate_json vs two-pass model_validate(json.loads(...))."""

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from api_labs.f09_pydantic_v2_internals_and_validation.single_pass_json import (
    SEEN_BEFORE_INPUT_TYPES,
    AuditedOrder,
    Label,
    Nested,
    Order,
    Price,
    StrictOrder,
    UncachedLabel,
    build_ingest_app,
    error_types,
    make_payload,
    nested_arrays,
    traced_intermediate_tree_bytes,
    traced_peak_bytes,
    validate_single_pass,
    validate_two_pass,
)

STRICT_BODY = (
    b'{"order_id":"6f1c1d3e-8a4b-4c7e-9f00-0123456789ab","placed_on":"2026-11-12",'
    b'"lines":[{"sku":"SKU-1","qty":2,"unit_price":1.5}]}'
)


@pytest.mark.unit
def test_both_paths_build_equal_models_in_lax_mode() -> None:
    raw = make_payload(50)
    assert validate_single_pass(raw) == validate_two_pass(raw)
    assert isinstance(validate_single_pass(raw), Order)


@pytest.mark.unit
def test_single_pass_skips_the_python_dict_tree() -> None:
    raw = make_payload(1_000)
    two_pass = traced_peak_bytes(validate_two_pass, raw)
    single_pass = traced_peak_bytes(validate_single_pass, raw)
    tree = traced_intermediate_tree_bytes(raw)
    assert single_pass < 0.85 * two_pass
    # The gap is the throwaway json.loads tree, within a factor of two.
    assert 0.5 * tree < two_pass - single_pass < 2.0 * tree


@pytest.mark.unit
def test_strict_mode_accepts_json_strings_only_from_json() -> None:
    order = StrictOrder.model_validate_json(STRICT_BODY)
    assert order.placed_on.isoformat() == "2026-11-12"
    with pytest.raises(ValidationError) as excinfo:
        StrictOrder.model_validate(json.loads(STRICT_BODY))
    assert sorted(str(e["type"]) for e in excinfo.value.errors()) == [
        "date_type",
        "is_instance_of",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_body_param_is_two_pass_and_breaks_strict_models() -> None:
    app = build_ingest_app()
    headers = {"content-type": "application/json"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        framework = await client.post("/orders/framework", content=STRICT_BODY, headers=headers)
        raw = await client.post("/orders/raw", content=STRICT_BODY, headers=headers)
        broken = await client.post("/orders/raw", content=b'{"order_id": ', headers=headers)
    assert framework.status_code == 422
    assert {e["type"] for e in framework.json()["detail"]} == {"date_type", "is_instance_of"}
    assert raw.status_code == 200
    assert raw.json() == {"placed_on": "2026-11-12"}
    assert broken.status_code == 422
    assert [e["type"] for e in broken.json()["detail"]] == ["json_invalid"]


@pytest.mark.unit
def test_parse_errors_and_non_json_input_are_validation_errors() -> None:
    assert error_types('{"text": "x"} trailing', Label) == ["json_invalid"]
    assert error_types('{"text": ', Label) == ["json_invalid"]
    with pytest.raises(ValidationError) as excinfo:
        Label.model_validate_json({"text": "x"})  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    assert excinfo.value.errors()[0]["type"] == "json_type"
    with pytest.raises(json.JSONDecodeError):
        json.loads('{"text": ')


@pytest.mark.unit
def test_jiter_depth_limit_rejects_where_cpython_recurses() -> None:
    assert error_types(nested_arrays(200), Nested) == []
    assert error_types(nested_arrays(201), Nested) == ["json_invalid"]
    with pytest.raises(ValidationError, match="recursion limit exceeded"):
        Nested.model_validate_json(nested_arrays(201))
    deep = nested_arrays(201)
    assert json.dumps(json.loads(deep), separators=(",", ":")) == deep  # CPython accepts 201
    with pytest.raises(RecursionError):
        json.loads("[" * 200_000 + "]" * 200_000)


@pytest.mark.unit
def test_integer_digit_limit_matches_cpython_4300() -> None:
    ok = '{"text": "x", "n": ' + "9" * 4300 + "}"
    too_long = '{"text": "x", "n": ' + "9" * 4301 + "}"
    assert error_types(ok, Label) == []
    assert error_types(too_long, Label) == ["json_invalid"]
    with pytest.raises(ValueError, match="4300"):
        json.loads(too_long)


@pytest.mark.unit
def test_unicode_boundaries_differ_between_parsers() -> None:
    lone_surrogate = '{"text": "\\ud800"}'
    assert error_types(lone_surrogate, Label) == ["json_invalid"]
    assert json.loads(lone_surrogate)["text"] == "\ud800"
    utf16 = '{"text": "x"}'.encode("utf-16")
    assert error_types(utf16, Label) == ["json_invalid"]
    assert json.loads(utf16) == {"text": "x"}
    invalid_utf8 = b'{"text": "\xff"}'
    assert error_types(invalid_utf8, Label) == ["json_invalid"]
    with pytest.raises(UnicodeDecodeError):
        json.loads(invalid_utf8)


@pytest.mark.unit
def test_duplicate_keys_resolve_to_last_value_in_both_paths() -> None:
    raw = '{"text": "first", "text": "last"}'
    assert Label.model_validate_json(raw).text == "last"
    assert Label.model_validate(json.loads(raw)).text == "last"


@pytest.mark.unit
def test_decimal_from_json_number_passes_through_binary64() -> None:
    raw = '{"amount": 0.1000000000000000055511151231257827}'
    assert Price.model_validate_json(raw).amount == Decimal("0.1")
    assert Price.model_validate(json.loads(raw)).amount == Decimal("0.1")
    exact = Price.model_validate(json.loads(raw, parse_float=Decimal)).amount
    assert exact == Decimal("0.1000000000000000055511151231257827")
    as_string = '{"amount": "0.1000000000000000055511151231257827"}'
    assert Price.model_validate_json(as_string).amount == exact


@pytest.mark.unit
def test_string_cache_shares_python_str_objects_across_calls() -> None:
    raw = '{"text": "hello world"}'
    assert Label.model_validate_json(raw).text is Label.model_validate_json(raw).text
    assert UncachedLabel.model_validate_json(raw).text is not (
        UncachedLabel.model_validate_json(raw).text
    )
    assert json.loads(raw)["text"] is not json.loads(raw)["text"]


@pytest.mark.unit
def test_before_model_validator_materializes_a_python_dict() -> None:
    SEEN_BEFORE_INPUT_TYPES.clear()
    raw = b'{"order_id":"6f1c1d3e-8a4b-4c7e-9f00-0123456789ab","lines":[]}'
    AuditedOrder.model_validate_json(raw)
    AuditedOrder.model_validate(json.loads(raw))
    assert SEEN_BEFORE_INPUT_TYPES == ["dict", "dict"]
