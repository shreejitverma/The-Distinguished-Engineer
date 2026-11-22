"""Wire-safe Protobuf schema evolution: reserved tags, type changes, unknown fields.

Everything here is in-process and deterministic.
Message classes are built at runtime from ``FileDescriptorProto`` objects, each in its own
``DescriptorPool``, so an "old" and a "new" schema can share one full name in one process.
``protoc`` is not needed.

The module has four parts:

* ``build_classes``: schema specs -> real ``protobuf`` message classes.
* ``scan_wire`` and ``unknown_records``: a strict tag-length-value scanner, because the upb
  Python backend (protobuf 7.36.2) raises ``NotImplementedError`` from ``UnknownFields()``.
* ``check_reserved``: the reserved-number and reserved-name rules that ``protoc`` enforces and
  the upb runtime descriptor pool used by Python does not.
* ``classify_type_change`` and ``diff_schema``: a small breaking-change checker driven by the
  compatibility groups in the proto3 language guide ("Updating A Message Type").
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import cache
from typing import Any

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

FDP = descriptor_pb2.FieldDescriptorProto

# Dynamic protobuf messages have no static attributes, so they are typed as Any.
MessageClass = Callable[..., Any]

WIRE_VARINT = 0
WIRE_I64 = 1
WIRE_LEN = 2
WIRE_SGROUP = 3
WIRE_EGROUP = 4
WIRE_I32 = 5

MAX_FIELD_NUMBER = 2**29 - 1
FIRST_IMPLEMENTATION_NUMBER = 19000
LAST_IMPLEMENTATION_NUMBER = 19999
MAX_GROUP_DEPTH = 100

# name -> (FieldDescriptorProto.Type, wire type)
SCALARS: dict[str, tuple[int, int]] = {
    "int32": (FDP.TYPE_INT32, WIRE_VARINT),
    "uint32": (FDP.TYPE_UINT32, WIRE_VARINT),
    "int64": (FDP.TYPE_INT64, WIRE_VARINT),
    "uint64": (FDP.TYPE_UINT64, WIRE_VARINT),
    "bool": (FDP.TYPE_BOOL, WIRE_VARINT),
    "sint32": (FDP.TYPE_SINT32, WIRE_VARINT),
    "sint64": (FDP.TYPE_SINT64, WIRE_VARINT),
    "fixed32": (FDP.TYPE_FIXED32, WIRE_I32),
    "sfixed32": (FDP.TYPE_SFIXED32, WIRE_I32),
    "float": (FDP.TYPE_FLOAT, WIRE_I32),
    "fixed64": (FDP.TYPE_FIXED64, WIRE_I64),
    "sfixed64": (FDP.TYPE_SFIXED64, WIRE_I64),
    "double": (FDP.TYPE_DOUBLE, WIRE_I64),
    "string": (FDP.TYPE_STRING, WIRE_LEN),
    "bytes": (FDP.TYPE_BYTES, WIRE_LEN),
}
SCALAR_TYPES: tuple[str, ...] = tuple(SCALARS)

# Wire-compatible groups from the proto3 guide, "Binary Wire-compatible Changes".
COMPATIBILITY_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"int32", "uint32", "int64", "uint64", "bool"}),
    frozenset({"sint32", "sint64"}),
    frozenset({"fixed32", "sfixed32"}),
    frozenset({"fixed64", "sfixed64"}),
    frozenset({"string", "bytes"}),
)

_NUMERIC_WIRE = (WIRE_VARINT, WIRE_I32, WIRE_I64)


@dataclass(frozen=True)
class FieldSpec:
    """One field. ``type`` is a key of ``SCALARS``, ``"message"`` or ``"enum"``."""

    name: str
    number: int
    type: str
    type_name: str = ""
    repeated: bool = False


@dataclass(frozen=True)
class MessageSpec:
    """One message. Reserved ranges are inclusive on both ends, as written in a .proto."""

    name: str
    fields: tuple[FieldSpec, ...] = ()
    reserved_ranges: tuple[tuple[int, int], ...] = ()
    reserved_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnumSpec:
    name: str
    values: tuple[tuple[str, int], ...]


def _wire_type_of(field: FieldSpec) -> int:
    if field.type in SCALARS:
        return SCALARS[field.type][1]
    return WIRE_VARINT if field.type == "enum" else WIRE_LEN


def build_classes(
    package: str,
    messages: tuple[MessageSpec, ...],
    *,
    enums: tuple[EnumSpec, ...] = (),
    syntax: str = "proto3",
) -> dict[str, MessageClass]:
    """Build message classes in a private pool.

    The upb pool does NOT check ``reserved`` ranges or names, nor the 19000-19999 band. The
    reserved checks live in the C++ ``DescriptorBuilder`` shared by ``protoc`` and the C++
    runtime (protobuf v36.2 src/google/protobuf/descriptor.cc, ``BuildMessage``); the band check
    is ``protoc``-only (src/google/protobuf/compiler/command_line_interface.cc).
    """
    fdp = descriptor_pb2.FileDescriptorProto(
        name=f"{package}.proto", package=package, syntax=syntax
    )
    for enum in enums:
        proto_enum = fdp.enum_type.add(name=enum.name)
        for value_name, number in enum.values:
            proto_enum.value.add(name=value_name, number=number)
    for spec in messages:
        proto_msg = fdp.message_type.add(name=spec.name)
        for fld in spec.fields:
            if fld.type in SCALARS:
                kind = SCALARS[fld.type][0]
            else:
                kind = FDP.TYPE_MESSAGE if fld.type == "message" else FDP.TYPE_ENUM
            proto_field = proto_msg.field.add(
                name=fld.name,
                number=fld.number,
                type=kind,
                label=FDP.LABEL_REPEATED if fld.repeated else FDP.LABEL_OPTIONAL,
            )
            if fld.type_name:
                proto_field.type_name = f".{package}.{fld.type_name}"
        for start, end in spec.reserved_ranges:
            proto_msg.reserved_range.add(start=start, end=end + 1)  # descriptor end is exclusive
        proto_msg.reserved_name.extend(spec.reserved_names)
    pool = descriptor_pool.DescriptorPool()
    pool.Add(fdp)
    return {
        spec.name: message_factory.GetMessageClass(
            pool.FindMessageTypeByName(f"{package}.{spec.name}")
        )
        for spec in messages
    }


# --------------------------------------------------------------------------------------
# Wire scanner
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WireRecord:
    """One top-level record: field number, wire type, and the full bytes including the tag."""

    number: int
    wire_type: int
    raw: bytes


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
    raise ValueError("varint longer than 10 bytes")


def _skip_value(data: bytes, pos: int, number: int, wire_type: int, depth: int) -> int:
    if wire_type == WIRE_VARINT:
        return _read_varint(data, pos)[1]
    if wire_type == WIRE_I64:
        end = pos + 8
    elif wire_type == WIRE_I32:
        end = pos + 4
    elif wire_type == WIRE_LEN:
        length, pos = _read_varint(data, pos)
        end = pos + length
    elif wire_type == WIRE_SGROUP:
        return _skip_group(data, pos, number, depth + 1)
    else:
        raise ValueError(f"unexpected wire type {wire_type}")
    if end > len(data):
        raise ValueError("record runs past the end of the buffer")
    return end


def _skip_group(data: bytes, pos: int, number: int, depth: int) -> int:
    if depth > MAX_GROUP_DEPTH:
        raise ValueError("group nesting too deep")
    while True:
        tag, pos = _read_varint(data, pos)
        inner_number, wire_type = tag >> 3, tag & 7
        if wire_type == WIRE_EGROUP:
            if inner_number != number:
                raise ValueError("mismatched end-group tag")
            return pos
        if inner_number == 0:
            raise ValueError("field number 0 is illegal")
        pos = _skip_value(data, pos, inner_number, wire_type, depth)


def scan_wire(data: bytes) -> list[WireRecord]:
    """Split a serialized message into top-level records. Raises ``ValueError`` if malformed."""
    records: list[WireRecord] = []
    pos = 0
    while pos < len(data):
        start = pos
        tag, pos = _read_varint(data, pos)
        number, wire_type = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("field number 0 is illegal")
        if wire_type == WIRE_EGROUP:
            raise ValueError("unexpected end-group tag")
        pos = _skip_value(data, pos, number, wire_type, 0)
        records.append(WireRecord(number, wire_type, data[start:pos]))
    return records


def unknown_records(cls: MessageClass, data: bytes) -> list[WireRecord]:
    """Records a parser built from ``cls`` cannot interpret and so keeps as unknown fields.

    A record is unknown when its number is not declared, or when its wire type cannot carry the
    declared type.
    A packed-capable repeated numeric field also accepts ``LEN``.
    Closed-enum values outside the enum are NOT detected here; they need the enum range.
    """
    allowed: dict[int, set[int]] = {}
    for fld in cls.DESCRIPTOR.fields:
        wire = _DESCRIPTOR_WIRE[fld.type]
        allowed[fld.number] = {wire}
        if fld.is_repeated and wire in _NUMERIC_WIRE:
            allowed[fld.number].add(WIRE_LEN)
    return [r for r in scan_wire(data) if r.wire_type not in allowed.get(r.number, set())]


_DESCRIPTOR_WIRE: dict[int, int] = {kind: wire for kind, wire in SCALARS.values()}
_DESCRIPTOR_WIRE[FDP.TYPE_ENUM] = WIRE_VARINT
_DESCRIPTOR_WIRE[FDP.TYPE_MESSAGE] = WIRE_LEN
_DESCRIPTOR_WIRE[FDP.TYPE_GROUP] = WIRE_SGROUP


# --------------------------------------------------------------------------------------
# Compile-time reserved checks (what protoc does and the runtime pool does not)
# --------------------------------------------------------------------------------------


def check_reserved(spec: MessageSpec) -> list[str]:
    """Return protoc-style errors for number, name, and range violations in one message."""
    errors: list[str] = []
    ranges = spec.reserved_ranges
    for i, (start1, end1) in enumerate(ranges):
        for start2, end2 in ranges[i + 1 :]:
            if end1 >= start2 and end2 >= start1:
                errors.append(
                    f"Reserved range {start2} to {end2} overlaps with "
                    f"already-defined range {start1} to {end1}."
                )
    seen_names: set[str] = set()
    for name in spec.reserved_names:
        if name in seen_names:
            errors.append(f'Field name "{name}" is reserved multiple times.')
        seen_names.add(name)
    used: dict[int, str] = {}
    for fld in spec.fields:
        if fld.number <= 0:
            errors.append("Field numbers must be positive integers.")
        elif fld.number > MAX_FIELD_NUMBER:
            errors.append(f"Field numbers cannot be greater than {MAX_FIELD_NUMBER}.")
        elif FIRST_IMPLEMENTATION_NUMBER <= fld.number <= LAST_IMPLEMENTATION_NUMBER:
            errors.append(
                f"Field numbers {FIRST_IMPLEMENTATION_NUMBER} through "
                f"{LAST_IMPLEMENTATION_NUMBER} are reserved for the protocol buffer "
                "library implementation."
            )
        if fld.number in used:
            errors.append(
                f'Field number {fld.number} has already been used in "{spec.name}" '
                f'by field "{used[fld.number]}".'
            )
        used[fld.number] = fld.name
        if any(start <= fld.number <= end for start, end in ranges):
            errors.append(f'Field "{fld.name}" uses reserved number {fld.number}.')
        if fld.name in seen_names:
            errors.append(f'Field name "{fld.name}" is reserved.')
    return errors


# --------------------------------------------------------------------------------------
# Type-change classification and schema diff
# --------------------------------------------------------------------------------------


class Compat(Enum):
    IDENTICAL = "identical"
    COMPATIBLE = "compatible"  # same value parses; may be lossy for out-of-range values
    WIRE_MISMATCH = "wire-mismatch"  # old bytes become an unknown field in the new schema
    REINTERPRETED = "reinterpreted"  # same wire type, different meaning: silent corruption


def classify_type_change(old: str, new: str) -> Compat:
    """Predict what a parser built for ``new`` does with bytes written for ``old``."""
    if old == new:
        return Compat.IDENTICAL
    if any(old in group and new in group for group in COMPATIBILITY_GROUPS):
        return Compat.COMPATIBLE
    if SCALARS[old][1] != SCALARS[new][1]:
        return Compat.WIRE_MISMATCH
    return Compat.REINTERPRETED


_PROBES: dict[str, Any] = {name: 7 for name in SCALAR_TYPES}
_PROBES.update({"bool": True, "float": 1.5, "double": 1.5, "string": "hi", "bytes": b"hi"})
_DEFAULTS: dict[str, Any] = {name: 0 for name in SCALAR_TYPES}
_DEFAULTS.update({"bool": False, "float": 0.0, "double": 0.0, "string": "", "bytes": b""})


@cache
def _single_field_class(type_name: str) -> MessageClass:
    spec = MessageSpec("M", (FieldSpec("v", 1, type_name),))
    return build_classes(f"probe_{type_name}", (spec,))["M"]


def _convert(value: Any, new: str) -> Any:
    if new == "bool":
        return bool(value)
    if new == "string" and isinstance(value, bytes):
        return value.decode()
    if new == "bytes" and isinstance(value, str):
        return value.encode()
    return value


def observe_type_change(old: str, new: str) -> Compat:
    """Measure the outcome by encoding a probe under ``old`` and parsing it under ``new``."""
    raw = _single_field_class(old)(v=_PROBES[old]).SerializeToString()
    parsed = _single_field_class(new).FromString(raw)
    if old == new:
        return Compat.IDENTICAL
    if parsed.v == _convert(_PROBES[old], new):
        return Compat.COMPATIBLE
    if parsed.v == _DEFAULTS[new] and parsed.SerializeToString() == raw:
        return Compat.WIRE_MISMATCH
    return Compat.REINTERPRETED


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str  # "error" or "warning"
    message: str


def _in_ranges(number: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= number <= end for start, end in ranges)


def diff_schema(old: MessageSpec, new: MessageSpec) -> list[Finding]:
    """Wire-level findings for ``old`` -> ``new``. A rename cannot be told from a reuse."""
    findings: list[Finding] = []
    old_by_number = {f.number: f for f in old.fields}
    new_by_number = {f.number: f for f in new.fields}
    for number, old_field in old_by_number.items():
        new_field = new_by_number.get(number)
        if new_field is None:
            if not _in_ranges(number, new.reserved_ranges):
                findings.append(
                    Finding(
                        "REMOVED_NUMBER_NOT_RESERVED",
                        "error",
                        f"field {number} ({old_field.name}) was removed but not reserved",
                    )
                )
            if old_field.name not in new.reserved_names and all(
                f.name != old_field.name for f in new.fields
            ):
                findings.append(
                    Finding(
                        "REMOVED_NAME_NOT_RESERVED",
                        "warning",
                        f"name {old_field.name} was removed but not reserved (JSON, text)",
                    )
                )
            continue
        findings.extend(_diff_field(old_field, new_field))
    for start, end in old.reserved_ranges:
        for number in range(start, min(end, start + 4096) + 1):
            if not _in_ranges(number, new.reserved_ranges):
                findings.append(
                    Finding(
                        "RESERVED_NUMBER_RELEASED",
                        "error",
                        f"number {number} is no longer reserved",
                    )
                )
                break
    for name in old.reserved_names:
        if name not in new.reserved_names:
            findings.append(
                Finding("RESERVED_NAME_RELEASED", "warning", f"name {name} is no longer reserved")
            )
    for number, new_field in new_by_number.items():
        if _in_ranges(number, old.reserved_ranges):
            findings.append(
                Finding(
                    "RESERVED_NUMBER_REUSED",
                    "error",
                    f"field {number} ({new_field.name}) uses a number reserved in the old schema",
                )
            )
    return findings


def _diff_field(old: FieldSpec, new: FieldSpec) -> list[Finding]:
    findings: list[Finding] = []
    number = old.number
    if old.type in SCALARS and new.type in SCALARS and old.type != new.type:
        compat = classify_type_change(old.type, new.type)
        if compat is Compat.COMPATIBLE:
            findings.append(
                Finding(
                    "TYPE_CHANGE_COMPATIBLE",
                    "warning",
                    f"field {number}: {old.type} -> {new.type} parses but may truncate",
                )
            )
        else:
            findings.append(
                Finding(
                    "NUMBER_REUSED_INCOMPATIBLE_TYPE",
                    "error",
                    f"field {number}: {old.type} -> {new.type} is {compat.value}",
                )
            )
    elif (old.type, old.type_name) != (new.type, new.type_name):
        findings.append(
            Finding(
                "NUMBER_REUSED_INCOMPATIBLE_TYPE",
                "error",
                f"field {number}: {old.type} {old.type_name} -> {new.type} {new.type_name}",
            )
        )
    if old.repeated != new.repeated:
        numeric = _wire_type_of(old) in _NUMERIC_WIRE
        findings.append(
            Finding(
                "CARDINALITY_CHANGE",
                "error" if numeric else "warning",
                f"field {number}: repeated {old.repeated} -> {new.repeated}",
            )
        )
    if old.name != new.name and not findings:
        findings.append(
            Finding(
                "RENAMED_OR_REUSED",
                "warning",
                f"field {number}: {old.name} -> {new.name} (rename or reuse; wire cannot tell)",
            )
        )
    return findings
