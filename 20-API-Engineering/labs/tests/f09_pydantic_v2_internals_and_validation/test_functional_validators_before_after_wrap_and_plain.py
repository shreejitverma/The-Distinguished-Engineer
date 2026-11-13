"""Execution order and failure semantics of Before, After, Wrap, and Plain validators."""

from typing import Any

import pytest
from pydantic import ValidationError

from api_labs.f09_pydantic_v2_internals_and_validation.functional_validator_order import (
    BEFORE_PORT_TYPED,
    DOCS_ORDER,
    FALLBACK,
    GT_AFTER_FUNC,
    GT_BEFORE_FUNC,
    INTERLEAVED,
    NON_NEGATIVE_ITEMS,
    PLAIN_IN_MIDDLE,
    PLAIN_PORT,
    SHORT_CIRCUIT,
    SKU,
    TICKET,
    ChildOrder,
    DecoratedOrder,
    DefaultOnNone,
    DefaultsAndAssignment,
    InfoProbe,
    OmittedField,
    OptionalPlacement,
    ParentOrder,
    Relocated,
    StrictEvent,
    handler_error_view,
    reject_with_assert,
    reject_with_custom_error,
    reject_with_type_error,
    reject_with_value_error,
    run_after,
    schema_chain,
)


def _errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [dict(err) for err in exc.errors(include_url=False)]


@pytest.mark.unit
def test_last_annotation_is_outermost_node() -> None:
    assert schema_chain(DOCS_ORDER) == [
        "function-wrap:wrap_w4",
        "function-before:before_b3",
        "function-after:after_a2",
        "function-after:after_a1",
        "str",
    ]
    assert DOCS_ORDER.validate_python("in") == "in>w4.pre>b3>a1>a2>w4.post"


@pytest.mark.unit
def test_wrap_post_step_runs_before_outer_after() -> None:
    assert INTERLEAVED.validate_python("in") == "in>b4>w3.pre>b1>a2>w3.post>a5"


@pytest.mark.unit
def test_plain_discards_type_and_everything_to_its_left() -> None:
    assert schema_chain(PLAIN_IN_MIDDLE) == [
        "function-after:after_a5",
        "function-before:before_b4",
        "function-plain:plain_p3",
    ]
    # An int is accepted for a `str` field: the str schema never runs, nor do b1 and a2.
    assert PLAIN_IN_MIDDLE.validate_python(7) == "7>b4>p3>a5"


@pytest.mark.unit
def test_wrap_without_handler_call_skips_inner_schema() -> None:
    assert SHORT_CIRCUIT.validate_python("skip") == {"short_circuited": True}
    assert SHORT_CIRCUIT.validate_python("12") == "12>a1"


@pytest.mark.unit
def test_wrap_handler_raises_validation_error_that_can_be_recovered() -> None:
    assert FALLBACK.validate_python("nope") == -1
    assert FALLBACK.validate_python("42") == 42
    assert handler_error_view("nope") == ("ValidatorCallable", [()])
    with pytest.raises(ValidationError) as exc_info:
        Relocated(port="eighty")  # pyright: ignore[reportArgumentType]
    assert [err["loc"] for err in _errors(exc_info.value)] == [("port", "inner")]


@pytest.mark.unit
def test_decorators_wrap_annotated_metadata() -> None:
    assert DecoratedOrder(value="in").value == "in>dec_b>ann_b>ann_a>dec_a"


@pytest.mark.unit
def test_subclass_override_keeps_parent_position() -> None:
    assert ParentOrder(value="in").value == "in>parent.one>parent.two"
    assert ChildOrder(value="in").value == "in>child.one>parent.two"


@pytest.mark.unit
def test_exception_conversion_rules() -> None:
    cases = [
        (reject_with_value_error, "value_error", "Value error, 1 is odd"),
        (reject_with_assert, "assertion_error", "Assertion failed, too small"),
        (reject_with_custom_error, "port_reserved", "port 1 is reserved"),
    ]
    for func, err_type, msg in cases:
        with pytest.raises(ValidationError) as exc_info:
            run_after(func, 1)
        err = _errors(exc_info.value)[0]
        assert (err["type"], err["msg"]) == (err_type, msg)
    with pytest.raises(TypeError, match="signature bug"):
        run_after(reject_with_type_error, 1)


@pytest.mark.unit
def test_use_default_and_omit() -> None:
    assert DefaultOnNone(region=None).region == "eu-west-1"  # pyright: ignore[reportArgumentType]
    assert DefaultOnNone(region="us-east-2").region == "us-east-2"
    assert NON_NEGATIVE_ITEMS.validate_python([1, -2, 3]) == [1, 3]
    dropped = OmittedField(quota=-5)
    assert dropped.__dict__ == {"owner": "nobody"}
    assert dropped.model_fields_set == set()
    assert not hasattr(dropped, "quota")


@pytest.mark.unit
def test_constraint_position_changes_semantics_and_engine() -> None:
    assert schema_chain(GT_BEFORE_FUNC) == ["function-after:_minus_ten", "int[gt]"]
    assert GT_BEFORE_FUNC.validate_python(5) == -5
    assert schema_chain(GT_AFTER_FUNC) == [
        "function-after:partial",
        "function-after:_minus_ten",
        "int",
    ]
    with pytest.raises(ValidationError) as exc_info:
        GT_AFTER_FUNC.validate_python(5)
    err = _errors(exc_info.value)[0]
    # The rejected value was -5, but `input` reports what entered the failing node.
    assert (err["type"], err["input"]) == ("greater_than", 5)


@pytest.mark.unit
def test_defaults_skip_validators_and_assignment_reruns_them() -> None:
    model = DefaultsAndAssignment()
    assert (model.unvalidated, model.validated) == (1, 10)
    model.unvalidated = 2
    assert model.unvalidated == 20


@pytest.mark.unit
def test_optional_placement_decides_whether_none_reaches_validator() -> None:
    nulls = OptionalPlacement(outer=None, inner=None)
    assert (nulls.outer, nulls.inner) == (-1, None)
    ints = OptionalPlacement(outer=3, inner=3)
    assert (ints.outer, ints.inner) == (3, 3)


@pytest.mark.unit
def test_before_and_wrap_lose_strict_json_string_allowance() -> None:
    ts = '"2026-11-13T00:00:00Z"'
    body = f'{{"bare_ts":{ts},"after_ts":{ts},"before_ts":{ts},"wrap_ts":{ts}}}'
    with pytest.raises(ValidationError) as exc_info:
        StrictEvent.model_validate_json(body)
    failed = {(err["loc"], err["type"]) for err in _errors(exc_info.value)}
    assert failed == {(("before_ts",), "datetime_type"), (("wrap_ts",), "datetime_type")}


@pytest.mark.unit
def test_pep695_alias_nests_inside_outer_annotations() -> None:
    assert TICKET.validate_python("in") == "in>outer_b>alias_b>alias_a>outer_a"


@pytest.mark.unit
def test_validation_info_carries_field_data_mode_and_context() -> None:
    from_json = InfoProbe.model_validate_json(
        '{"first": 1, "second": "s"}', context={"tenant": "t1"}
    )
    assert from_json.second == "s|second|{'first': 1}|json|{'tenant': 't1'}"
    from_python = InfoProbe.model_validate({"first": 1, "second": "s"})
    assert from_python.second == "s|second|{'first': 1}|python|None"


@pytest.mark.unit
def test_json_schema_input_types() -> None:
    assert PLAIN_PORT.json_schema() == {}
    assert PLAIN_PORT.json_schema(mode="serialization") == {"type": "integer"}
    assert BEFORE_PORT_TYPED.json_schema() == {"anyOf": [{"type": "string"}, {"type": "integer"}]}


@pytest.mark.unit
def test_sku_pipeline_normalizes_and_emits_stable_error_codes() -> None:
    assert SKU.validate_python(" sku-000019 ") == "SKU-000019"
    assert SKU.validate_python(19) == "SKU-000019"
    reasons: list[tuple[Any, Any]] = []
    for bad in (" sku-1 ", "SKU-000018", 3.5):
        with pytest.raises(ValidationError) as exc_info:
            SKU.validate_python(bad)
        err = _errors(exc_info.value)[0]
        assert err["type"] == "sku_invalid"
        reasons.append((err["input"], err["ctx"]["reason"]))
    assert reasons == [
        ("SKU-1", "string_pattern_mismatch"),
        ("SKU-000018", "value_error"),
        (3.5, "string_type"),
    ]
