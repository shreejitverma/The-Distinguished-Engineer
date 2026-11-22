"""Protobuf wire format: the hand-written codec versus protobuf 7.36.2 byte for byte.

Reference messages are built at runtime from `FileDescriptorProto`s, so no `protoc` is needed.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import textwrap
from typing import Any

import pytest

from api_labs.f19_grpc_and_protocol_buffers.protobuf_wire_format import (
    FieldSpec,
    Kind,
    Record,
    Scalar,
    WireError,
    WireType,
    decode_message,
    decode_varint,
    encode_message,
    encode_record,
    encode_scalar,
    encode_varint,
    from_twos_complement,
    make_tag,
    parse_records,
    split_tag,
    to_twos_complement,
    zigzag_decode,
    zigzag_encode,
)

_TYPE_NAME = {
    Kind.INT32: "TYPE_INT32",
    Kind.INT64: "TYPE_INT64",
    Kind.UINT32: "TYPE_UINT32",
    Kind.UINT64: "TYPE_UINT64",
    Kind.SINT32: "TYPE_SINT32",
    Kind.SINT64: "TYPE_SINT64",
    Kind.BOOL: "TYPE_BOOL",
    Kind.FIXED32: "TYPE_FIXED32",
    Kind.SFIXED32: "TYPE_SFIXED32",
    Kind.FLOAT: "TYPE_FLOAT",
    Kind.FIXED64: "TYPE_FIXED64",
    Kind.SFIXED64: "TYPE_SFIXED64",
    Kind.DOUBLE: "TYPE_DOUBLE",
    Kind.STRING: "TYPE_STRING",
    Kind.BYTES: "TYPE_BYTES",
}


def _pb(name: str) -> Any:
    return importlib.import_module(name)


def _reference_class(specs: list[FieldSpec], syntax: str = "proto2") -> Any:
    """Message class with one field `f<number>` per spec; proto2 gives explicit presence."""
    descriptor_pb2 = _pb("google.protobuf.descriptor_pb2")
    field = descriptor_pb2.FieldDescriptorProto
    fdp = descriptor_pb2.FileDescriptorProto(name="wire.proto", package="wire", syntax=syntax)
    message = fdp.message_type.add(name="M")
    for spec in specs:
        entry = message.field.add(
            name=f"f{spec.number}",
            number=spec.number,
            type=getattr(field, _TYPE_NAME[spec.kind]),
            label=field.LABEL_REPEATED if spec.repeated else field.LABEL_OPTIONAL,
        )
        # proto2 defaults to expanded, so always state the choice; proto3 defaults to packed.
        numeric = spec.kind not in {Kind.STRING, Kind.BYTES}
        if spec.repeated and numeric and (syntax == "proto2" or spec.packed):
            entry.options.packed = spec.packed
    pool = _pb("google.protobuf.descriptor_pool").DescriptorPool()
    pool.Add(fdp)
    return _pb("google.protobuf.message_factory").GetMessageClass(
        pool.FindMessageTypeByName("wire.M")
    )


def _reference_bytes(
    specs: list[FieldSpec], values: dict[int, Any], syntax: str = "proto2"
) -> bytes:
    msg = _reference_class(specs, syntax)()
    for spec in specs:
        if spec.number not in values:
            continue
        if spec.repeated:
            getattr(msg, f"f{spec.number}").extend(values[spec.number])
        else:
            setattr(msg, f"f{spec.number}", values[spec.number])
    return bytes(msg.SerializeToString(deterministic=True))


def _reference_parse(specs: list[FieldSpec], data: bytes, syntax: str = "proto2") -> Any:
    msg = _reference_class(specs, syntax)()
    msg.ParseFromString(data)
    return msg


DECODE_ERROR = _pb("google.protobuf.message").DecodeError


def _nested_len(depth: int, field_number: int = 1) -> bytes:
    """`depth` nested LEN records, innermost empty: root.n.n....n."""
    payload = b""
    for _ in range(depth):
        payload = encode_record(Record(field_number, WireType.LEN, payload))
    return payload


def _nested_groups(depth: int) -> bytes:
    records: tuple[Record, ...] = ()
    for _ in range(depth):
        records = (Record(1, WireType.SGROUP, records),)
    return b"".join(encode_record(record) for record in records)


# --------------------------------------------------------------------------- tags and varints


@pytest.mark.unit
def test_tag_is_field_number_shifted_left_three_or_wire_type() -> None:
    assert make_tag(1, WireType.VARINT) == 0x08
    assert make_tag(2, WireType.LEN) == 0x12
    assert make_tag(3, WireType.LEN) == 0x1A
    assert split_tag(0x12) == (2, WireType.LEN)
    expected_tag_bytes = {1: 1, 2: 1, 15: 1, 16: 2, 2047: 2, 2048: 3, 2**29 - 1: 5}
    for number, size in expected_tag_bytes.items():
        spec = FieldSpec(number, Kind.INT32)
        reference = _reference_bytes([spec], {number: 1})
        tag = encode_varint(make_tag(number, WireType.VARINT))
        assert reference == tag + b"\x01"
        assert len(tag) == size


@pytest.mark.unit
def test_spec_example_150_encodes_as_9601_and_message_as_08_96_01() -> None:
    assert encode_varint(150) == bytes.fromhex("9601")
    assert decode_varint(bytes.fromhex("9601")) == (150, 2)
    spec = FieldSpec(1, Kind.INT32)
    assert _reference_bytes([spec], {1: 150}) == bytes.fromhex("089601")
    assert encode_message([spec], {1: 150}) == bytes.fromhex("089601")
    assert decode_message([spec], bytes.fromhex("089601")).values == {1: 150}


@pytest.mark.unit
def test_varint_length_steps_up_every_seven_bits_and_matches_protobuf() -> None:
    boundaries = [0, 1, 2**64 - 1]
    for groups in range(1, 10):
        boundaries += [2 ** (7 * groups) - 1, 2 ** (7 * groups)]
    spec = FieldSpec(1, Kind.UINT64)
    for value in boundaries:
        encoded = encode_varint(value)
        assert len(encoded) == max(1, -(-value.bit_length() // 7))
        assert _reference_bytes([spec], {1: value}) == b"\x08" + encoded
        assert decode_varint(encoded) == (value, len(encoded))
    assert len(encode_varint(2**63)) == 10


@pytest.mark.unit
def test_negative_int32_takes_ten_byte_varint_but_sint32_takes_one() -> None:
    int32 = FieldSpec(1, Kind.INT32)
    sint32 = FieldSpec(1, Kind.SINT32)
    int64 = FieldSpec(1, Kind.INT64)
    negative_int32 = _reference_bytes([int32], {1: -1})
    assert negative_int32 == b"\x08" + b"\xff" * 9 + b"\x01"
    assert encode_message([int32], {1: -1}) == negative_int32
    assert len(negative_int32) == 11
    assert encode_message([int64], {1: -1}) == negative_int32
    zigzag = _reference_bytes([sint32], {1: -1})
    assert zigzag == b"\x08\x01"
    assert encode_message([sint32], {1: -1}) == zigzag
    smallest = bytes.fromhex("08 80 80 80 80 f8 ff ff ff ff 01")
    assert _reference_bytes([int32], {1: -(2**31)}) == smallest
    assert encode_message([int32], {1: -(2**31)}) == smallest


@pytest.mark.unit
def test_zigzag_interleaves_signs_and_matches_protobuf_at_the_edges() -> None:
    table32 = {0: 0, -1: 1, 1: 2, -2: 3, -500: 999, 2**31 - 1: 0xFFFFFFFE, -(2**31): 0xFFFFFFFF}
    sint32 = FieldSpec(1, Kind.SINT32)
    for value, encoded in table32.items():
        assert zigzag_encode(value, 32) == encoded
        assert zigzag_decode(encoded) == value
        assert _reference_bytes([sint32], {1: value}) == b"\x08" + encode_varint(encoded)
    table64 = {2**63 - 1: 2**64 - 2, -(2**63): 2**64 - 1}
    sint64 = FieldSpec(1, Kind.SINT64)
    for value, encoded in table64.items():
        assert zigzag_encode(value, 64) == encoded
        assert _reference_bytes([sint64], {1: value}) == b"\x08" + encode_varint(encoded)
    with pytest.raises(ValueError, match="does not fit"):
        zigzag_encode(2**31, 32)


@pytest.mark.unit
def test_zigzag_is_a_bijection_and_small_magnitudes_stay_one_byte() -> None:
    values = range(-1024, 1025)
    encoded = [zigzag_encode(v, 32) for v in values]
    assert len(set(encoded)) == len(encoded)
    assert [zigzag_decode(e) for e in encoded] == list(values)
    assert all(len(encode_varint(zigzag_encode(v, 64))) == 1 for v in range(-64, 64))
    assert len(encode_varint(zigzag_encode(64, 64))) == 2
    assert len(encode_varint(zigzag_encode(-65, 64))) == 2
    assert from_twos_complement(to_twos_complement(-5, 32), 32) == -5


# --------------------------------------------------------------------------- scalars and packing


@pytest.mark.unit
def test_every_scalar_kind_matches_protobuf_bytes_and_decodes_back() -> None:
    specs = [FieldSpec(number, kind) for number, kind in enumerate(Kind, start=1)]
    by_kind: dict[Kind, Scalar] = {
        Kind.INT32: -7,
        Kind.INT64: -(2**40),
        Kind.UINT32: 2**32 - 1,
        Kind.UINT64: 2**64 - 1,
        Kind.SINT32: -(2**31),
        Kind.SINT64: -(2**63),
        Kind.BOOL: True,
        Kind.FIXED32: 200,
        Kind.SFIXED32: -5,
        Kind.FLOAT: 1.5,
        Kind.FIXED64: 2**63,
        Kind.SFIXED64: -2,
        Kind.DOUBLE: 25.4,
        Kind.STRING: "h\u00e9llo",
        Kind.BYTES: b"\x00\xff",
    }
    values = {spec.number: by_kind[spec.kind] for spec in specs}
    reference = _reference_bytes(specs, values)
    assert encode_message(specs, values) == reference
    decoded = decode_message(specs, reference)
    assert decoded.values == values
    assert decoded.unknown == ()
    assert decoded.values[7] is True


@pytest.mark.unit
def test_packed_repeated_is_one_len_record_and_expanded_is_one_record_per_element() -> None:
    expanded = FieldSpec(1, Kind.INT32, repeated=True, packed=False)
    packed = FieldSpec(2, Kind.INT32, repeated=True, packed=True)
    for count, payload_prefix in ((100, b"\x64"), (300, b"\xac\x02")):
        items = [1] * count
        packed_ref = _reference_bytes([packed], {2: items})
        expanded_ref = _reference_bytes([expanded], {1: items})
        assert packed_ref == b"\x12" + payload_prefix + b"\x01" * count
        assert expanded_ref == b"\x08\x01" * count
        assert encode_message([packed], {2: items}) == packed_ref
        assert encode_message([expanded], {1: items}) == expanded_ref
    assert len(_reference_bytes([packed], {2: [1] * 100})) == 102
    assert len(_reference_bytes([expanded], {1: [1] * 100})) == 200
    assert encode_message([packed], {2: []}) == b""
    assert _reference_bytes([packed], {2: []}) == b""
    doubles = FieldSpec(3, Kind.DOUBLE, repeated=True, packed=True)
    assert encode_message([doubles], {3: [1.5, -2.0]}) == _reference_bytes(
        [doubles], {3: [1.5, -2.0]}
    )


@pytest.mark.unit
def test_strings_are_never_packed_and_packed_on_strings_is_rejected() -> None:
    strings = FieldSpec(1, Kind.STRING, repeated=True)
    assert encode_message([strings], {1: ["a", "bc"]}) == bytes.fromhex("0a0161 0a026263")
    assert _reference_bytes([strings], {1: ["a", "bc"]}) == bytes.fromhex("0a0161 0a026263")
    with pytest.raises(ValueError, match="only repeated numeric"):
        FieldSpec(1, Kind.STRING, repeated=True, packed=True)
    with pytest.raises(ValueError, match="only repeated numeric"):
        FieldSpec(1, Kind.INT32, packed=True)


@pytest.mark.unit
def test_decoders_accept_packed_and_expanded_forms_for_the_same_field() -> None:
    expanded_bytes = bytes.fromhex("28 01 28 02 28 03")
    packed_bytes = bytes.fromhex("2a 03 01 02 03")
    split_bytes = bytes.fromhex("2a 02 01 02 28 09 2a 01 03")
    for declared_packed in (True, False):
        spec = FieldSpec(5, Kind.INT32, repeated=True, packed=declared_packed)
        assert decode_message([spec], expanded_bytes).values == {5: [1, 2, 3]}
        assert decode_message([spec], packed_bytes).values == {5: [1, 2, 3]}
        assert decode_message([spec], split_bytes).values == {5: [1, 2, 9, 3]}
    proto3 = FieldSpec(5, Kind.INT32, repeated=True)
    parsed = _reference_parse([proto3], expanded_bytes, syntax="proto3")
    assert list(parsed.f5) == [1, 2, 3]
    assert parsed.SerializeToString() == packed_bytes
    with pytest.raises(WireError):
        decode_message([proto3], bytes.fromhex("2a 03 01 02 80"))
    with pytest.raises(DECODE_ERROR):
        _reference_parse([proto3], bytes.fromhex("2a 03 01 02 80"), syntax="proto3")


@pytest.mark.unit
def test_proto3_and_editions_style_repeated_numerics_default_to_packed_proto2_to_expanded() -> None:
    descriptor_pb2 = _pb("google.protobuf.descriptor_pb2")
    field = descriptor_pb2.FieldDescriptorProto

    def serialized(syntax: str) -> bytes:
        fdp = descriptor_pb2.FileDescriptorProto(name="d.proto", package="d", syntax=syntax)
        fdp.message_type.add(name="M").field.add(
            name="e", number=5, type=field.TYPE_INT32, label=field.LABEL_REPEATED
        )
        pool = _pb("google.protobuf.descriptor_pool").DescriptorPool()
        pool.Add(fdp)
        msg = _pb("google.protobuf.message_factory").GetMessageClass(
            pool.FindMessageTypeByName("d.M")
        )()
        msg.e.extend([1, 2, 3])
        return bytes(msg.SerializeToString())

    assert serialized("proto3") == bytes.fromhex("2a 03 01 02 03")
    assert serialized("proto2") == bytes.fromhex("28 01 28 02 28 03")


# --------------------------------------------------------------------------- decoder rules


@pytest.mark.unit
def test_last_one_wins_for_singular_fields_and_repeated_records_accumulate() -> None:
    singular = FieldSpec(1, Kind.INT32)
    assert decode_message([singular], bytes.fromhex("08 01 08 02")).values == {1: 2}
    assert _reference_parse([singular], bytes.fromhex("08 01 08 02")).f1 == 2
    specs = [FieldSpec(4, Kind.STRING), FieldSpec(5, Kind.INT32, repeated=True)]
    interleaved = bytes.fromhex("28 01 28 02 22 05 68656c6c6f 28 03")
    assert decode_message(specs, interleaved).values == {5: [1, 2, 3], 4: "hello"}
    reference = _reference_parse(specs, interleaved)
    assert (reference.f4, list(reference.f5)) == ("hello", [1, 2, 3])


@pytest.mark.unit
def test_concatenated_messages_parse_as_merge_with_submessages_merged_field_by_field() -> None:
    descriptor_pb2 = _pb("google.protobuf.descriptor_pb2")
    field = descriptor_pb2.FieldDescriptorProto
    fdp = descriptor_pb2.FileDescriptorProto(name="m.proto", package="m", syntax="proto2")
    inner = fdp.message_type.add(name="Inner")
    inner.field.add(name="a", number=1, type=field.TYPE_INT32, label=field.LABEL_OPTIONAL)
    inner.field.add(name="b", number=2, type=field.TYPE_INT32, label=field.LABEL_OPTIONAL)
    inner.field.add(name="r", number=3, type=field.TYPE_INT32, label=field.LABEL_REPEATED)
    outer = fdp.message_type.add(name="Outer")
    outer.field.add(
        name="c",
        number=3,
        type=field.TYPE_MESSAGE,
        label=field.LABEL_OPTIONAL,
        type_name=".m.Inner",
    )
    pool = _pb("google.protobuf.descriptor_pool").DescriptorPool()
    pool.Add(fdp)
    outer_cls = _pb("google.protobuf.message_factory").GetMessageClass(
        pool.FindMessageTypeByName("m.Outer")
    )
    inner_specs = [
        FieldSpec(1, Kind.INT32),
        FieldSpec(2, Kind.INT32),
        FieldSpec(3, Kind.INT32, repeated=True),
    ]
    first = encode_record(Record(3, WireType.LEN, encode_message(inner_specs, {1: 1, 3: [7]})))
    second = encode_record(Record(3, WireType.LEN, encode_message(inner_specs, {2: 5, 3: [8]})))
    whole = outer_cls()
    whole.ParseFromString(first + second)
    assert (whole.c.a, whole.c.b, list(whole.c.r)) == (1, 5, [7, 8])
    separate = outer_cls()
    separate.ParseFromString(first)
    other = outer_cls()
    other.ParseFromString(second)
    separate.MergeFrom(other)
    assert whole.SerializeToString(deterministic=True) == separate.SerializeToString(
        deterministic=True
    )
    assert len(parse_records(whole.SerializeToString())) == 1
    assert len(parse_records(first + second)) == 2


@pytest.mark.unit
def test_overlong_varints_are_accepted_but_eleven_bytes_are_not() -> None:
    spec = FieldSpec(1, Kind.INT32)
    overlong_zero = bytes.fromhex("08 80 00")
    assert decode_message([spec], overlong_zero).values == {1: 0}
    reference = _reference_parse([spec], overlong_zero)
    assert reference.HasField("f1")
    assert reference.SerializeToString() == b"\x08\x00"
    high_bits = b"\x08" + b"\xff" * 9 + b"\x7f"
    assert decode_message([spec], high_bits).values == {1: -1}
    assert _reference_parse([spec], high_bits).f1 == -1
    assert decode_varint(b"\xff" * 9 + b"\x7f")[0] == 2**64 - 1
    too_long = b"\x08" + b"\x80" * 10 + b"\x00"
    with pytest.raises(WireError, match="longer than 10"):
        decode_message([spec], too_long)
    with pytest.raises(DECODE_ERROR):
        _reference_parse([spec], too_long)


@pytest.mark.unit
def test_int32_keeps_only_the_low_32_bits_of_a_wider_varint() -> None:
    spec = FieldSpec(1, Kind.INT32)
    for raw in ("08 ff ff ff ff 0f", "08 ff ff ff ff 1f"):
        assert decode_message([spec], bytes.fromhex(raw)).values == {1: -1}
        assert _reference_parse([spec], bytes.fromhex(raw)).f1 == -1
    assert decode_message([spec], bytes.fromhex("08 80 80 80 80 10")).values == {1: 0}
    assert _reference_parse([spec], bytes.fromhex("08 80 80 80 80 10")).f1 == 0


@pytest.mark.unit
def test_wire_type_mismatch_is_kept_as_an_unknown_field_and_reserialized() -> None:
    spec = FieldSpec(1, Kind.INT32)
    data = bytes.fromhex("09 0000000000000000 08 07")
    decoded = decode_message([spec], data)
    assert decoded.values == {1: 7}
    assert decoded.unknown == (Record(1, WireType.I64, 0),)
    reference = _reference_parse([spec], data)
    assert reference.f1 == 7
    unknown = [
        (u.field_number, u.wire_type, u.data)
        for u in _pb("google.protobuf.unknown_fields").UnknownFieldSet(reference)
    ]
    assert unknown == [(1, 1, 0)]
    rebuilt = encode_message([spec], decoded.values) + b"".join(
        encode_record(record) for record in decoded.unknown
    )
    assert rebuilt == reference.SerializeToString() == bytes.fromhex("08 07 09 0000000000000000")
    as_len = decode_message([spec], bytes.fromhex("0a 02 01 02"))
    assert as_len.values == {}
    assert as_len.unknown == (Record(1, WireType.LEN, b"\x01\x02"),)


@pytest.mark.unit
def test_unknown_groups_round_trip_through_the_record_layer() -> None:
    data = bytes.fromhex("0b 08 05 0c")
    records = parse_records(data)
    assert records == [Record(1, WireType.SGROUP, (Record(1, WireType.VARINT, 5),))]
    assert b"".join(encode_record(record) for record in records) == data
    reference = _reference_parse([FieldSpec(1, Kind.INT32)], data)
    assert reference.SerializeToString() == data


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("0001", id="field-number-0"),
        pytest.param("0e01", id="wire-type-6"),
        pytest.param("0f01", id="wire-type-7"),
        pytest.param("0896", id="truncated-varint"),
        pytest.param("1a0501", id="len-past-end"),
        pytest.param("1100", id="truncated-fixed64"),
        pytest.param("0c", id="stray-end-group"),
        pytest.param("0b", id="unclosed-group"),
        pytest.param("f8ffffffff0101", id="tag-wider-than-32-bits"),
        pytest.param("08" + "80" * 10 + "00", id="eleven-byte-varint"),
    ],
)
def test_malformed_wire_bytes_are_rejected_by_the_lab_and_by_protobuf(raw: str) -> None:
    data = bytes.fromhex(raw)
    with pytest.raises(WireError):
        parse_records(data)
    with pytest.raises(DECODE_ERROR):
        _reference_parse([FieldSpec(1, Kind.INT32)], data)


@pytest.mark.unit
def test_invalid_utf8_in_a_proto3_string_is_rejected() -> None:
    spec = FieldSpec(2, Kind.STRING)
    data = bytes.fromhex("12 02 ff fe")
    with pytest.raises(WireError, match="UTF-8"):
        decode_message([spec], data)
    with pytest.raises(DECODE_ERROR):
        _reference_parse([spec], data, syntax="proto3")


@pytest.mark.unit
def test_nesting_depth_limit_is_one_hundred_submessages() -> None:
    descriptor_pb2 = _pb("google.protobuf.descriptor_pb2")
    field = descriptor_pb2.FieldDescriptorProto
    fdp = descriptor_pb2.FileDescriptorProto(name="r.proto", package="r", syntax="proto3")
    fdp.message_type.add(name="R").field.add(
        name="n", number=1, type=field.TYPE_MESSAGE, label=field.LABEL_OPTIONAL, type_name=".r.R"
    )
    pool = _pb("google.protobuf.descriptor_pool").DescriptorPool()
    pool.Add(fdp)
    message_cls = _pb("google.protobuf.message_factory").GetMessageClass(
        pool.FindMessageTypeByName("r.R")
    )
    message_cls().ParseFromString(_nested_len(100))
    with pytest.raises(DECODE_ERROR, match="MaxDepth"):
        message_cls().ParseFromString(_nested_len(101))
    assert len(parse_records(_nested_groups(100))) == 1
    with pytest.raises(WireError, match="nested too deeply"):
        parse_records(_nested_groups(101))
    assert len(parse_records(_nested_groups(101), max_depth=101)) == 1


@pytest.mark.unit
def test_encoder_rejects_out_of_range_values_and_wrong_types() -> None:
    with pytest.raises(ValueError, match="does not fit"):
        encode_scalar(Kind.INT32, 2**31)
    with pytest.raises(ValueError, match="does not fit"):
        encode_scalar(Kind.UINT32, -1)
    with pytest.raises(TypeError, match="expected bool"):
        encode_scalar(Kind.BOOL, 1)
    with pytest.raises(TypeError, match="expected bytes"):
        encode_scalar(Kind.BYTES, "text")
    with pytest.raises(ValueError, match="out of uint64"):
        encode_varint(-1)
    with pytest.raises(ValueError, match="out of range"):
        make_tag(0, WireType.VARINT)
    with pytest.raises(ValueError, match="out of range"):
        make_tag(2**29, WireType.VARINT)
    with pytest.raises(TypeError, match="is repeated"):
        encode_message([FieldSpec(1, Kind.INT32, repeated=True)], {1: 5})
    with pytest.raises(TypeError, match="is singular"):
        encode_message([FieldSpec(1, Kind.INT32)], {1: [5]})


# --------------------------------------------------------------------------- backends

_BACKEND_SCRIPT = textwrap.dedent(
    """
    import json
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
    from google.protobuf.internal import api_implementation
    from google.protobuf.message import DecodeError

    f = descriptor_pb2.FieldDescriptorProto
    fdp = descriptor_pb2.FileDescriptorProto(name="b.proto", package="b", syntax="proto3")
    m = fdp.message_type.add(name="M")
    m.field.add(name="a", number=1, type=f.TYPE_INT32, label=f.LABEL_OPTIONAL)
    m.field.add(name="t", number=2, type=f.TYPE_STRING, label=f.LABEL_OPTIONAL)
    m.field.add(name="s", number=3, type=f.TYPE_SINT32, label=f.LABEL_OPTIONAL)
    m.field.add(name="e", number=5, type=f.TYPE_INT32, label=f.LABEL_REPEATED)
    pool = descriptor_pool.DescriptorPool()
    pool.Add(fdp)
    cls = message_factory.GetMessageClass(pool.FindMessageTypeByName("b.M"))
    msg = cls(a=-1, s=-1, e=[1, 2, 3])
    try:
        cls().ParseFromString(bytes.fromhex("12 02 ff fe"))
        error = "none"
    except DecodeError:
        error = "DecodeError"
    except Exception as exc:
        error = type(exc).__name__
    print(json.dumps({
        "impl": api_implementation.Type(),
        "hex": msg.SerializeToString(deterministic=True).hex(),
        "bad_utf8": error,
    }))
    """
)


def _run_backend(implementation: str) -> dict[str, str]:
    env = {**os.environ, "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": implementation}
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _BACKEND_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    result: dict[str, str] = json.loads(completed.stdout)
    return result


@pytest.mark.unit
def test_upb_and_pure_python_backends_emit_same_bytes_but_raise_different_utf8_errors() -> None:
    upb, pure = _run_backend("upb"), _run_backend("python")
    assert (upb["impl"], pure["impl"]) == ("upb", "python")
    expected = (b"\x08" + b"\xff" * 9 + b"\x01" + b"\x18\x01" + b"\x2a\x03\x01\x02\x03").hex()
    assert upb["hex"] == pure["hex"] == expected
    assert upb["bad_utf8"] == "DecodeError"
    assert pure["bad_utf8"] == "UnicodeDecodeError"
