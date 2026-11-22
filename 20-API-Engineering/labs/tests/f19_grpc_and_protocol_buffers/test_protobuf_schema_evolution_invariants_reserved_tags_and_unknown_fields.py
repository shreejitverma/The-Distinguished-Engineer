"""Lab for the note on wire-safe Protobuf schema evolution."""

from __future__ import annotations

import itertools

import pytest
from google.protobuf import json_format
from google.protobuf.message import DecodeError

from api_labs.f19_grpc_and_protocol_buffers.protobuf_schema_evolution import (
    SCALAR_TYPES,
    WIRE_LEN,
    WIRE_VARINT,
    Compat,
    EnumSpec,
    FieldSpec,
    MessageSpec,
    build_classes,
    check_reserved,
    classify_type_change,
    diff_schema,
    observe_type_change,
    scan_wire,
    unknown_records,
)

V1 = MessageSpec(
    "Account", (FieldSpec("name", 1, "string"), FieldSpec("balance_cents", 2, "int32"))
)
V2 = MessageSpec(
    "Account",
    (
        *V1.fields,
        FieldSpec("email", 3, "string"),
        FieldSpec("score", 4, "int64"),
    ),
)

# What an old release wrote to disk: three scalar fields the next release will delete.
STORED = MessageSpec(
    "Account",
    (
        FieldSpec("name", 1, "string"),
        FieldSpec("tax_id", 2, "string"),
        FieldSpec("balance_cents", 3, "int32"),
        FieldSpec("note", 4, "string"),
    ),
)


def _stored_bytes() -> bytes:
    cls = build_classes("stored", (STORED,))["Account"]
    return bytes(
        cls(name="ada", tax_id="123-45-6789", balance_cents=12345, note="hello").SerializeToString()
    )


@pytest.mark.unit
def test_unknown_fields_survive_a_round_trip_through_an_old_service() -> None:
    new = build_classes("v2", (V2,))["Account"]
    old = build_classes("v1", (V1,))["Account"]
    original = new(name="ada", balance_cents=5, email="ada@example.com", score=1 << 40)
    wire = original.SerializeToString()

    through_old = old.FromString(wire)
    assert through_old.name == "ada"
    assert not hasattr(through_old, "email")
    assert [r.number for r in unknown_records(old, wire)] == [3, 4]

    reserialized = through_old.SerializeToString()
    assert reserialized == wire
    assert new.FromString(reserialized) == original


@pytest.mark.unit
def test_unknown_fields_are_serialized_after_known_fields() -> None:
    old = build_classes("v1", (V1,))["Account"]
    # field 4 (unknown) is written before field 1 (known) on the wire
    wire = bytes([0x20, 0x09, 0x0A, 0x01, ord("x")])
    out = old.FromString(wire).SerializeToString()
    assert [r.number for r in scan_wire(out)] == [1, 4]


@pytest.mark.unit
def test_actions_that_drop_unknown_fields() -> None:
    new = build_classes("v2", (V2,))["Account"]
    old = build_classes("v1", (V1,))["Account"]
    wire = new(name="ada", balance_cents=5, email="e", score=9).SerializeToString()
    parsed = old.FromString(wire)

    copied = old()
    copied.CopyFrom(parsed)
    assert copied.SerializeToString() == wire

    merged = old()
    merged.MergeFrom(parsed)
    assert merged.SerializeToString() == wire

    fieldwise = old()
    fieldwise.name = parsed.name
    fieldwise.balance_cents = parsed.balance_cents
    assert fieldwise.SerializeToString() != wire
    assert unknown_records(old, fieldwise.SerializeToString()) == []

    discarded = old.FromString(wire)
    discarded.DiscardUnknownFields()
    assert discarded.SerializeToString() == fieldwise.SerializeToString()

    via_json = old()
    json_format.Parse(json_format.MessageToJson(parsed), via_json)
    assert via_json.SerializeToString() == fieldwise.SerializeToString()


@pytest.mark.unit
def test_parse_from_string_clears_unknown_but_merge_accumulates() -> None:
    old = build_classes("v1", (V1,))["Account"]
    msg = old.FromString(bytes([0x28, 0x09]))
    msg.MergeFromString(bytes([0x30, 0x07]))
    assert msg.SerializeToString() == bytes([0x28, 0x09, 0x30, 0x07])
    msg.ParseFromString(bytes([0x10, 0x03]))  # field 2 (balance_cents) = 3, nothing unknown
    assert msg.balance_cents == 3
    assert msg.SerializeToString() == bytes([0x10, 0x03])


@pytest.mark.unit
def test_json_rejects_unknown_fields_unless_told_to_ignore() -> None:
    new = build_classes("v2", (V2,))["Account"]
    old = build_classes("v1", (V1,))["Account"]
    text = json_format.MessageToJson(new(name="ada", email="e"))
    with pytest.raises(json_format.ParseError, match="no field named"):
        json_format.Parse(text, old())
    lenient = json_format.Parse(text, old(), ignore_unknown_fields=True)
    assert lenient.name == "ada"


@pytest.mark.unit
def test_runtime_pool_does_not_enforce_reserved() -> None:
    bad = MessageSpec(
        "Account",
        (FieldSpec("loyalty_points", 2, "int32"), FieldSpec("internal", 19500, "int32")),
        reserved_ranges=((2, 2),),
    )
    classes = build_classes("bad", (bad,))
    assert classes["Account"](loyalty_points=1, internal=2).loyalty_points == 1
    assert check_reserved(bad) == [
        'Field "loyalty_points" uses reserved number 2.',
        "Field numbers 19000 through 19999 are reserved for the protocol buffer library "
        "implementation.",
    ]


@pytest.mark.unit
def test_check_reserved_matches_protoc_messages() -> None:
    spec = MessageSpec(
        "A",
        (FieldSpec("loyalty_points", 2, "int32"), FieldSpec("balance", 3, "string")),
        reserved_ranges=((2, 2), (9, 11)),
        reserved_names=("balance",),
    )
    assert check_reserved(spec) == [
        'Field "loyalty_points" uses reserved number 2.',
        'Field name "balance" is reserved.',
    ]
    overlap = MessageSpec(
        "A",
        (FieldSpec("y", 536870912, "int32"), FieldSpec("z", 0, "int32")),
        reserved_ranges=((2, 5), (4, 6)),
        reserved_names=("x", "x"),
    )
    assert check_reserved(overlap) == [
        "Reserved range 4 to 6 overlaps with already-defined range 2 to 5.",
        'Field name "x" is reserved multiple times.',
        "Field numbers cannot be greater than 536870911.",
        "Field numbers must be positive integers.",
    ]
    assert check_reserved(MessageSpec("A", (FieldSpec("y", 536870911, "int32"),))) == []


@pytest.mark.unit
def test_reusing_a_string_number_leaks_the_old_value() -> None:
    wire = _stored_bytes()
    reused = MessageSpec(
        "Account",
        (FieldSpec("name", 1, "string"), FieldSpec("nickname", 2, "string")),
    )
    new = build_classes("reused", (reused,))["Account"]
    assert new.FromString(wire).nickname == "123-45-6789"


@pytest.mark.unit
def test_reusing_an_int_number_silently_changes_meaning() -> None:
    wire = _stored_bytes()
    reused = MessageSpec("Account", (FieldSpec("loyalty_points", 3, "int32"),))
    new = build_classes("reused", (reused,))["Account"]
    assert new.FromString(wire).loyalty_points == 12345


@pytest.mark.unit
def test_reusing_a_number_with_another_wire_type_hides_data_but_keeps_bytes() -> None:
    wire = _stored_bytes()
    reused = MessageSpec("Account", (FieldSpec("flags", 3, "string"),))
    new = build_classes("reused", (reused,))["Account"]
    parsed = new.FromString(wire)
    assert parsed.flags == ""
    assert [r.number for r in unknown_records(new, wire) if r.wire_type == WIRE_VARINT] == [3]
    assert parsed.SerializeToString() == wire


@pytest.mark.unit
def test_reusing_a_string_number_for_a_message_fails_to_parse() -> None:
    wire = _stored_bytes()
    reused = (
        MessageSpec("Address", (FieldSpec("street", 1, "string"),)),
        MessageSpec("Account", (FieldSpec("address", 4, "message", "Address"),)),
    )
    new = build_classes("reused", reused)["Account"]
    with pytest.raises(DecodeError):
        new.FromString(wire)
    well_formed = build_classes("stored", (STORED,))["Account"](note="\x0a\x03abc")
    assert new.FromString(well_formed.SerializeToString()).address.street == "abc"


@pytest.mark.unit
def test_type_change_classification_matches_the_real_parser() -> None:
    # The guide calls sint32/sint64 -> bool incompatible, but zigzag keeps zero as zero, so the
    # real parser still yields the right truthiness. The documented rule is deliberately stricter.
    stricter = {("sint32", "bool"), ("sint64", "bool")}
    wrong = [
        (old, new, classify_type_change(old, new), observe_type_change(old, new))
        for old, new in itertools.product(SCALAR_TYPES, repeat=2)
        if classify_type_change(old, new) is not observe_type_change(old, new)
        and (old, new) not in stricter
    ]
    assert wrong == []
    for pair in stricter:
        assert classify_type_change(*pair) is Compat.REINTERPRETED
        assert observe_type_change(*pair) is Compat.COMPATIBLE
    assert classify_type_change("int32", "sint32") is Compat.REINTERPRETED
    assert classify_type_change("fixed32", "float") is Compat.REINTERPRETED
    assert classify_type_change("int32", "string") is Compat.WIRE_MISMATCH
    assert classify_type_change("string", "bytes") is Compat.COMPATIBLE


@pytest.mark.unit
def test_compatible_int_widening_truncates_out_of_range_values() -> None:
    i64 = build_classes("a", (MessageSpec("M", (FieldSpec("v", 1, "int64"),)),))["M"]
    i32 = build_classes("b", (MessageSpec("M", (FieldSpec("v", 1, "int32"),)),))["M"]
    s64 = build_classes("c", (MessageSpec("M", (FieldSpec("v", 1, "sint64"),)),))["M"]
    s32 = build_classes("d", (MessageSpec("M", (FieldSpec("v", 1, "sint32"),)),))["M"]
    assert i32.FromString(i64(v=(1 << 40) + 5).SerializeToString()).v == 5
    assert i32.FromString(i64(v=1 << 31).SerializeToString()).v == -(1 << 31)
    assert s32.FromString(s64(v=1 << 31).SerializeToString()).v == 0
    negative = i32(v=-5).SerializeToString()
    assert len(negative) == 11  # 1 tag byte + 10 varint bytes
    assert i64.FromString(negative).v == -5


@pytest.mark.unit
def test_invalid_utf8_makes_bytes_to_string_fail() -> None:
    b = build_classes("a", (MessageSpec("M", (FieldSpec("v", 1, "bytes"),)),))["M"]
    s = build_classes("b", (MessageSpec("M", (FieldSpec("v", 1, "string"),)),))["M"]
    assert s.FromString(b(v=b"ok").SerializeToString()).v == "ok"
    # upb raises DecodeError; the pure-Python backend raises UnicodeDecodeError instead
    with pytest.raises((DecodeError, UnicodeDecodeError)):
        s.FromString(b(v=b"\xff\xfe").SerializeToString())


@pytest.mark.unit
def test_repeated_to_singular_keeps_last_string_and_strands_packed_numbers() -> None:
    rep_s = build_classes("a", (MessageSpec("M", (FieldSpec("v", 1, "string", repeated=True),)),))[
        "M"
    ]
    one_s = build_classes("b", (MessageSpec("M", (FieldSpec("v", 1, "string"),)),))["M"]
    assert one_s.FromString(rep_s(v=["a", "b", "c"]).SerializeToString()).v == "c"

    rep_i = build_classes("c", (MessageSpec("M", (FieldSpec("v", 1, "int32", repeated=True),)),))[
        "M"
    ]
    one_i = build_classes("d", (MessageSpec("M", (FieldSpec("v", 1, "int32"),)),))["M"]
    wire = rep_i(v=[1, 2, 3]).SerializeToString()
    assert wire == bytes([0x0A, 0x03, 1, 2, 3])
    parsed = one_i.FromString(wire)
    assert parsed.v == 0
    assert parsed.SerializeToString() == wire
    assert [r.wire_type for r in unknown_records(one_i, wire)] == [WIRE_LEN]


@pytest.mark.unit
def test_closed_enum_moves_unknown_values_and_reorders_a_repeated_field() -> None:
    enum = EnumSpec("E", (("A", 0), ("B", 1)))
    field = FieldSpec("r", 1, "enum", "E", repeated=True)
    wire = bytes([0x08, 0, 0x08, 2, 0x08, 1, 0x08, 2])  # unpacked [0, 2, 1, 2]
    closed = build_classes("ce", (MessageSpec("M", (field,)),), enums=(enum,), syntax="proto2")["M"]
    parsed = closed.FromString(wire)
    assert list(parsed.r) == [0, 1]
    assert parsed.SerializeToString() == bytes([0x08, 0, 0x08, 1, 0x08, 2, 0x08, 2])
    opened = build_classes("oe", (MessageSpec("M", (field,)),), enums=(enum,))["M"]
    assert list(opened.FromString(wire).r) == [0, 2, 1, 2]


@pytest.mark.unit
def test_bytes_field_accepts_an_embedded_message_encoding() -> None:
    inner = MessageSpec("Inner", (FieldSpec("street", 1, "string"),))
    typed = MessageSpec("Outer", (FieldSpec("addr", 1, "message", "Inner"),))
    opaque = MessageSpec("Outer", (FieldSpec("addr", 1, "bytes"),))
    classes = build_classes("typed", (inner, typed))
    raw = classes["Outer"](addr={"street": "abc"}).SerializeToString()
    as_bytes = build_classes("opaque", (opaque,))["Outer"].FromString(raw)
    assert as_bytes.addr == classes["Inner"](street="abc").SerializeToString()


@pytest.mark.unit
def test_scan_wire_rejects_malformed_input() -> None:
    assert [r.number for r in scan_wire(bytes([0x08, 0x01, 0x2B, 0x08, 0x01, 0x2C]))] == [1, 5]
    for bad in (bytes([0x00, 0x01]), bytes([0x2A, 0x05, 0x01]), bytes([0x0C]), bytes([0x2B, 0x34])):
        with pytest.raises(ValueError, match=r"."):
            scan_wire(bad)


@pytest.mark.unit
def test_diff_schema_flags_unreserved_removal_and_reuse() -> None:
    removed = MessageSpec("Account", (FieldSpec("name", 1, "string"),))
    codes = {f.code for f in diff_schema(V1, removed)}
    assert codes == {"REMOVED_NUMBER_NOT_RESERVED", "REMOVED_NAME_NOT_RESERVED"}

    reserved = MessageSpec(
        "Account",
        (FieldSpec("name", 1, "string"),),
        reserved_ranges=((2, 2),),
        reserved_names=("balance_cents",),
    )
    assert diff_schema(V1, reserved) == []

    reuse = MessageSpec(
        "Account",
        (FieldSpec("name", 1, "string"), FieldSpec("tag", 2, "string")),
        reserved_ranges=((2, 2),),
    )
    assert {f.code for f in diff_schema(reserved, reuse)} == {
        "RESERVED_NUMBER_REUSED",
        "RESERVED_NAME_RELEASED",
    }

    released = MessageSpec("Account", (FieldSpec("name", 1, "string"),))
    assert "RESERVED_NUMBER_RELEASED" in {f.code for f in diff_schema(reserved, released)}


@pytest.mark.unit
def test_diff_schema_grades_type_changes() -> None:
    def change(old_type: str, new_type: str, *, rep: bool = False) -> set[tuple[str, str]]:
        old = MessageSpec("M", (FieldSpec("v", 1, old_type),))
        new = MessageSpec("M", (FieldSpec("v", 1, new_type, repeated=rep),))
        return {(f.code, f.severity) for f in diff_schema(old, new)}

    assert change("int32", "int64") == {("TYPE_CHANGE_COMPATIBLE", "warning")}
    assert change("int32", "sint32") == {("NUMBER_REUSED_INCOMPATIBLE_TYPE", "error")}
    assert change("int32", "string") == {("NUMBER_REUSED_INCOMPATIBLE_TYPE", "error")}
    assert change("int32", "int32", rep=True) == {("CARDINALITY_CHANGE", "error")}
    assert change("string", "string", rep=True) == {("CARDINALITY_CHANGE", "warning")}
    renamed = diff_schema(
        MessageSpec("M", (FieldSpec("a", 1, "int32"),)),
        MessageSpec("M", (FieldSpec("b", 1, "int32"),)),
    )
    assert [(f.code, f.severity) for f in renamed] == [("RENAMED_OR_REUSED", "warning")]
