"""Protobuf binary wire format by hand: tags, base-128 varints, ZigZag, LEN, packed fields.

The codec has two layers.
The record layer (`parse_records`, `encode_record`) knows nothing about schemas: it splits bytes
into (field number, wire type, payload) records, which is all a proxy or an old parser can do.
The schema layer (`FieldSpec`, `encode_message`, `decode_message`) maps scalar kinds onto records
and follows the decoder rules in the Protocol Buffers encoding guide: last one wins, packed and
expanded repeated forms both accepted, wire-type mismatches kept as unknown fields.
Messages as field values (submessages) are deliberately out of scope: a submessage is a LEN
record whose payload is another message, and the lab tests it at the record level.
"""

from __future__ import annotations

import enum
import struct
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

type Scalar = int | float | bool | str | bytes
type FieldValue = Scalar | Sequence[Scalar]

MAX_VARINT_BYTES: Final = 10
MAX_FIELD_NUMBER: Final = (1 << 29) - 1
DEFAULT_MAX_DEPTH: Final = 100
_MASK32: Final = (1 << 32) - 1
_MASK64: Final = (1 << 64) - 1
_TAG_TYPE_BITS: Final = 3
_TAG_TYPE_MASK: Final = (1 << _TAG_TYPE_BITS) - 1


class WireError(ValueError):
    """The bytes are not a well-formed message (the reference parsers raise DecodeError)."""


class WireType(enum.IntEnum):
    """The six wire types; 6 and 7 are not assigned and make a message malformed."""

    VARINT = 0
    I64 = 1
    LEN = 2
    SGROUP = 3
    EGROUP = 4
    I32 = 5


class Kind(enum.Enum):
    """Scalar field types of the schema language, with the wire type each one uses."""

    INT32 = "int32"
    INT64 = "int64"
    UINT32 = "uint32"
    UINT64 = "uint64"
    SINT32 = "sint32"
    SINT64 = "sint64"
    BOOL = "bool"
    FIXED32 = "fixed32"
    SFIXED32 = "sfixed32"
    FLOAT = "float"
    FIXED64 = "fixed64"
    SFIXED64 = "sfixed64"
    DOUBLE = "double"
    STRING = "string"
    BYTES = "bytes"


_WIRE_TYPE_OF: Final[Mapping[Kind, WireType]] = {
    Kind.INT32: WireType.VARINT,
    Kind.INT64: WireType.VARINT,
    Kind.UINT32: WireType.VARINT,
    Kind.UINT64: WireType.VARINT,
    Kind.SINT32: WireType.VARINT,
    Kind.SINT64: WireType.VARINT,
    Kind.BOOL: WireType.VARINT,
    Kind.FIXED32: WireType.I32,
    Kind.SFIXED32: WireType.I32,
    Kind.FLOAT: WireType.I32,
    Kind.FIXED64: WireType.I64,
    Kind.SFIXED64: WireType.I64,
    Kind.DOUBLE: WireType.I64,
    Kind.STRING: WireType.LEN,
    Kind.BYTES: WireType.LEN,
}


def wire_type_of(kind: Kind) -> WireType:
    return _WIRE_TYPE_OF[kind]


def is_packable(kind: Kind) -> bool:
    """Only scalar numeric kinds (VARINT, I32, I64) can be packed; strings and bytes cannot."""
    return _WIRE_TYPE_OF[kind] is not WireType.LEN


# ----------------------------------------------------------------------------- varints


def encode_varint(value: int) -> bytes:
    """Base-128 varint of a uint64: 7 payload bits per byte, least significant group first."""
    if not 0 <= value <= _MASK64:
        raise ValueError(f"varint value out of uint64 range: {value}")
    out = bytearray()
    while value > 0x7F:
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    out.append(value)
    return bytes(out)


def decode_varint(buf: bytes, pos: int = 0) -> tuple[int, int]:
    """Return (value, next_pos).

    Like the reference parsers this accepts non-canonical (overlong) encodings, reads at most
    ten bytes, and discards payload bits above bit 63 of the tenth byte.
    """
    result = 0
    for index in range(MAX_VARINT_BYTES):
        if pos + index >= len(buf):
            raise WireError("truncated varint")
        byte = buf[pos + index]
        result |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            return result & _MASK64, pos + index + 1
    raise WireError("varint longer than 10 bytes")


def varint_size(value: int) -> int:
    return len(encode_varint(value))


# ----------------------------------------------------------------------------- signed integers


def to_twos_complement(value: int, bits: int) -> int:
    """Reinterpret a signed integer as the unsigned integer with the same low `bits` bits."""
    if not -(1 << (bits - 1)) <= value < (1 << (bits - 1)):
        raise ValueError(f"{value} does not fit in a signed {bits}-bit integer")
    return value & ((1 << bits) - 1)


def from_twos_complement(raw: int, bits: int) -> int:
    """Inverse of `to_twos_complement`; only the low `bits` bits of `raw` are used."""
    raw &= (1 << bits) - 1
    return raw - (1 << bits) if raw >> (bits - 1) else raw


def zigzag_encode(value: int, bits: int) -> int:
    """(n << 1) ^ (n >> (bits - 1)) with an arithmetic shift: 0, -1, 1, -2 -> 0, 1, 2, 3."""
    if not -(1 << (bits - 1)) <= value < (1 << (bits - 1)):
        raise ValueError(f"{value} does not fit in a signed {bits}-bit integer")
    return ((value << 1) ^ (value >> (bits - 1))) & ((1 << bits) - 1)


def zigzag_decode(raw: int) -> int:
    """(n >> 1) ^ -(n & 1): even numbers are non-negative, odd numbers are negative."""
    return (raw >> 1) ^ -(raw & 1)


# ----------------------------------------------------------------------------- tags


def make_tag(field_number: int, wire_type: WireType) -> int:
    """(field_number << 3) | wire_type, which is then written as a varint."""
    if not 1 <= field_number <= MAX_FIELD_NUMBER:
        raise ValueError(f"field number out of range 1..{MAX_FIELD_NUMBER}: {field_number}")
    return (field_number << _TAG_TYPE_BITS) | int(wire_type)


def split_tag(tag: int) -> tuple[int, WireType]:
    if tag > _MASK32:
        raise WireError("tag does not fit in 32 bits")
    field_number = tag >> _TAG_TYPE_BITS
    raw_type = tag & _TAG_TYPE_MASK
    if field_number == 0:
        raise WireError("field number 0 is illegal")
    try:
        return field_number, WireType(raw_type)
    except ValueError:
        raise WireError(f"unassigned wire type {raw_type}") from None


# ----------------------------------------------------------------------------- record layer


@dataclass(frozen=True, slots=True)
class Record:
    """One tag-value pair.

    `value` is the unsigned integer for VARINT, I32 and I64 records (fixed-width values are the
    little-endian integer), the payload bytes for LEN, and the nested records for SGROUP.
    """

    field_number: int
    wire_type: WireType
    value: int | bytes | tuple[Record, ...]


def parse_records(data: bytes, *, max_depth: int = DEFAULT_MAX_DEPTH) -> list[Record]:
    """Split a message into records without a schema; groups nest at most `max_depth` deep."""
    records, _ = _parse(data, 0, None, 0, max_depth)
    return records


def _parse(
    buf: bytes, pos: int, group: int | None, depth: int, max_depth: int
) -> tuple[list[Record], int]:
    records: list[Record] = []
    while pos < len(buf):
        tag, pos = decode_varint(buf, pos)
        field_number, wire_type = split_tag(tag)
        match wire_type:
            case WireType.VARINT:
                number, pos = decode_varint(buf, pos)
                records.append(Record(field_number, wire_type, number))
            case WireType.I64 | WireType.I32:
                width = 8 if wire_type is WireType.I64 else 4
                if pos + width > len(buf):
                    raise WireError("truncated fixed-width value")
                chunk = int.from_bytes(buf[pos : pos + width], "little")
                records.append(Record(field_number, wire_type, chunk))
                pos += width
            case WireType.LEN:
                length, pos = decode_varint(buf, pos)
                if length > len(buf) - pos:
                    raise WireError("length prefix runs past the end of the buffer")
                records.append(Record(field_number, wire_type, buf[pos : pos + length]))
                pos += length
            case WireType.SGROUP:
                if depth + 1 > max_depth:
                    raise WireError("groups nested too deeply")
                inner, pos = _parse(buf, pos, field_number, depth + 1, max_depth)
                records.append(Record(field_number, wire_type, tuple(inner)))
            case WireType.EGROUP:
                if group != field_number:
                    raise WireError("end-group tag does not match an open group")
                return records, pos
    if group is not None:
        raise WireError("group was never closed")
    return records, pos


def encode_record(record: Record) -> bytes:
    """Inverse of `parse_records` for one record (varints are written in canonical form)."""
    head = encode_varint(make_tag(record.field_number, record.wire_type))
    value = record.value
    match record.wire_type:
        case WireType.VARINT if isinstance(value, int):
            return head + encode_varint(value)
        case WireType.I32 if isinstance(value, int):
            return head + value.to_bytes(4, "little")
        case WireType.I64 if isinstance(value, int):
            return head + value.to_bytes(8, "little")
        case WireType.LEN if isinstance(value, bytes):
            return head + encode_varint(len(value)) + value
        case WireType.SGROUP if isinstance(value, tuple):
            end = encode_varint(make_tag(record.field_number, WireType.EGROUP))
            return head + b"".join(encode_record(inner) for inner in value) + end
        case _:
            pass
    raise ValueError(f"record value does not match wire type {record.wire_type.name}")


# ----------------------------------------------------------------------------- scalars


def _as_int(value: Scalar) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"expected int, got {type(value).__name__}")
    return value


def _as_float(value: Scalar) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"expected float, got {type(value).__name__}")
    return float(value)


def encode_scalar(kind: Kind, value: Scalar) -> bytes:
    """Bytes that follow the tag: a varint, 4 or 8 little-endian bytes, or length plus payload."""
    match kind:
        case Kind.INT32:
            return encode_varint(_sign_extended(_as_int(value), 32))
        case Kind.INT64:
            return encode_varint(_sign_extended(_as_int(value), 64))
        case Kind.UINT32:
            return encode_varint(_unsigned(_as_int(value), 32))
        case Kind.UINT64:
            return encode_varint(_unsigned(_as_int(value), 64))
        case Kind.SINT32:
            return encode_varint(zigzag_encode(_as_int(value), 32))
        case Kind.SINT64:
            return encode_varint(zigzag_encode(_as_int(value), 64))
        case Kind.BOOL:
            if not isinstance(value, bool):
                raise TypeError(f"expected bool, got {type(value).__name__}")
            return b"\x01" if value else b"\x00"
        case Kind.FIXED32:
            return _unsigned(_as_int(value), 32).to_bytes(4, "little")
        case Kind.SFIXED32:
            return to_twos_complement(_as_int(value), 32).to_bytes(4, "little")
        case Kind.FLOAT:
            return struct.pack("<f", _as_float(value))
        case Kind.FIXED64:
            return _unsigned(_as_int(value), 64).to_bytes(8, "little")
        case Kind.SFIXED64:
            return to_twos_complement(_as_int(value), 64).to_bytes(8, "little")
        case Kind.DOUBLE:
            return struct.pack("<d", _as_float(value))
        case Kind.STRING:
            if not isinstance(value, str):
                raise TypeError(f"expected str, got {type(value).__name__}")
            payload = value.encode("utf-8")
            return encode_varint(len(payload)) + payload
        case Kind.BYTES:
            if not isinstance(value, bytes):
                raise TypeError(f"expected bytes, got {type(value).__name__}")
            return encode_varint(len(value)) + value


def _sign_extended(value: int, bits: int) -> int:
    """int32 and int64 negatives become 64-bit two's complement, so they always take 10 bytes."""
    if not -(1 << (bits - 1)) <= value < (1 << (bits - 1)):
        raise ValueError(f"{value} does not fit in a signed {bits}-bit integer")
    return value & _MASK64


def _unsigned(value: int, bits: int) -> int:
    if not 0 <= value < (1 << bits):
        raise ValueError(f"{value} does not fit in an unsigned {bits}-bit integer")
    return value


def decode_scalar(kind: Kind, raw: int | bytes) -> Scalar:
    """Interpret a record value (integer for VARINT/I32/I64, bytes for LEN) as `kind`."""
    if isinstance(raw, bytes):
        if kind is Kind.BYTES:
            return raw
        if kind is Kind.STRING:
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WireError("string field is not valid UTF-8") from exc
        raise WireError(f"{kind.value} cannot be decoded from a LEN payload")
    match kind:
        case Kind.INT32 | Kind.SFIXED32:
            return from_twos_complement(raw, 32)
        case Kind.INT64 | Kind.SFIXED64:
            return from_twos_complement(raw, 64)
        case Kind.UINT32 | Kind.FIXED32:
            return raw & _MASK32
        case Kind.UINT64 | Kind.FIXED64:
            return raw
        case Kind.SINT32:
            return zigzag_decode(raw & _MASK32)
        case Kind.SINT64:
            return zigzag_decode(raw)
        case Kind.BOOL:
            return raw != 0
        case Kind.FLOAT:
            return float(struct.unpack("<f", (raw & _MASK32).to_bytes(4, "little"))[0])
        case Kind.DOUBLE:
            return float(struct.unpack("<d", raw.to_bytes(8, "little"))[0])
        case Kind.STRING | Kind.BYTES:
            raise WireError(f"{kind.value} must be decoded from a LEN payload")


# ----------------------------------------------------------------------------- schema layer


@dataclass(frozen=True, slots=True)
class FieldSpec:
    number: int
    kind: Kind
    repeated: bool = False
    packed: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.number <= MAX_FIELD_NUMBER:
            raise ValueError(f"field number out of range: {self.number}")
        if self.packed and not (self.repeated and is_packable(self.kind)):
            raise ValueError("only repeated numeric fields can be packed")


@dataclass(frozen=True, slots=True)
class DecodedMessage:
    values: dict[int, Scalar | list[Scalar]] = field(default_factory=lambda: {})
    unknown: tuple[Record, ...] = ()


def encode_message(specs: Sequence[FieldSpec], values: Mapping[int, FieldValue]) -> bytes:
    """Serialize present fields by ascending field number; a present 0 is written (explicit)."""
    out = bytearray()
    for spec in sorted(specs, key=lambda s: s.number):
        if spec.number not in values:
            continue
        wire_type = wire_type_of(spec.kind)
        value = values[spec.number]
        if not spec.repeated:
            out += encode_varint(make_tag(spec.number, wire_type))
            out += encode_scalar(spec.kind, _single(spec, value))
            continue
        items = _many(spec, value)
        if spec.packed:
            if items:
                body = b"".join(encode_scalar(spec.kind, item) for item in items)
                out += encode_varint(make_tag(spec.number, WireType.LEN))
                out += encode_varint(len(body)) + body
        else:
            for item in items:
                out += encode_varint(make_tag(spec.number, wire_type))
                out += encode_scalar(spec.kind, item)
    return bytes(out)


def _single(spec: FieldSpec, value: FieldValue) -> Scalar:
    if isinstance(value, (int, float, str, bytes)):
        return value
    raise TypeError(f"field {spec.number} is singular, got {type(value).__name__}")


def _many(spec: FieldSpec, value: FieldValue) -> Sequence[Scalar]:
    if isinstance(value, (list, tuple)):
        return value
    raise TypeError(f"field {spec.number} is repeated, got {type(value).__name__}")


def decode_message(
    specs: Sequence[FieldSpec], data: bytes, *, max_depth: int = DEFAULT_MAX_DEPTH
) -> DecodedMessage:
    """Decode with the reference rules: last one wins, packed or expanded accepted for any
    repeated numeric field, and a record whose wire type does not fit the schema is kept as an
    unknown field instead of being an error.
    """
    by_number = {spec.number: spec for spec in specs}
    values: dict[int, Scalar | list[Scalar]] = {}
    unknown: list[Record] = []
    for record in parse_records(data, max_depth=max_depth):
        spec = by_number.get(record.field_number)
        if spec is None:
            unknown.append(record)
        elif record.wire_type is wire_type_of(spec.kind) and not isinstance(record.value, tuple):
            _store(values, spec, [decode_scalar(spec.kind, record.value)])
        elif (
            spec.repeated
            and is_packable(spec.kind)
            and record.wire_type is WireType.LEN
            and isinstance(record.value, bytes)
        ):
            _store(values, spec, _decode_packed(spec.kind, record.value))
        else:
            unknown.append(record)
    return DecodedMessage(values, tuple(unknown))


def _store(values: dict[int, Scalar | list[Scalar]], spec: FieldSpec, items: list[Scalar]) -> None:
    if not spec.repeated:
        if items:
            values[spec.number] = items[-1]
        return
    existing = values.get(spec.number)
    if isinstance(existing, list):
        existing.extend(items)
    else:
        values[spec.number] = items


def _decode_packed(kind: Kind, payload: bytes) -> list[Scalar]:
    """Elements are decoded back to back until the payload is exhausted."""
    out: list[Scalar] = []
    wire_type = wire_type_of(kind)
    if wire_type is WireType.VARINT:
        pos = 0
        while pos < len(payload):
            raw, pos = decode_varint(payload, pos)
            out.append(decode_scalar(kind, raw))
        return out
    width = 4 if wire_type is WireType.I32 else 8
    if len(payload) % width:
        raise WireError("packed payload is not a whole number of fixed-width elements")
    for start in range(0, len(payload), width):
        out.append(decode_scalar(kind, int.from_bytes(payload[start : start + width], "little")))
    return out
