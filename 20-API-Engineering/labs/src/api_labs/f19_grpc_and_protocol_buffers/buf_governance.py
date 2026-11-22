"""A small executable model of `buf lint` and `buf breaking` as shipped in buf 1.75.0.

Buf does not diff `.proto` text.
It compiles both versions to descriptors, pairs elements by identity (message by full name,
field by number, enum value by number, RPC by name), and runs one function per rule over the
pairs; a rule belongs to the categories FILE, PACKAGE, WIRE_JSON, and WIRE.
This module reproduces that structure over plain dataclasses so the semantics can be tested
without the `buf` binary.

Fidelity is checked in the test module against output captured from `buf` 1.75.0 on the same
schemas; where the model simplifies, the docstring of the rule says so.
Known simplifications: no nested messages or enums, no extensions, no editions features, no
map-entry synthesis, reserved ranges are expanded to number sets, and only the rules listed in
`BREAKING_RULES` and `LINT_RULES` exist.
"""

from __future__ import annotations

import enum
import importlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence


class Kind(enum.Enum):
    """Protobuf field kinds, named as in `FieldDescriptorProto.Type` without the TYPE_ prefix."""

    INT32 = "int32"
    INT64 = "int64"
    UINT32 = "uint32"
    UINT64 = "uint64"
    SINT32 = "sint32"
    SINT64 = "sint64"
    BOOL = "bool"
    FIXED32 = "fixed32"
    SFIXED32 = "sfixed32"
    FIXED64 = "fixed64"
    SFIXED64 = "sfixed64"
    FLOAT = "float"
    DOUBLE = "double"
    STRING = "string"
    BYTES = "bytes"
    ENUM = "enum"
    MESSAGE = "message"


# Wire compatibility groups, copied in structure from buf 1.75.0
# private/bufpkg/bufcheck/bufcheckserver/internal/bufcheckserverhandle/breaking_util.go,
# fieldKindToWireCompatibilityGroup.
# STRING and BYTES are deliberately in different groups so that string -> bytes can be allowed
# and bytes -> string rejected by an explicit special case.
WIRE_GROUP: Final[Mapping[Kind, int]] = {
    Kind.INT32: 1,
    Kind.INT64: 1,
    Kind.UINT32: 1,
    Kind.UINT64: 1,
    Kind.BOOL: 1,
    Kind.SINT32: 2,
    Kind.SINT64: 2,
    Kind.STRING: 3,
    Kind.BYTES: 4,
    Kind.FIXED32: 5,
    Kind.SFIXED32: 5,
    Kind.FIXED64: 6,
    Kind.SFIXED64: 6,
    Kind.DOUBLE: 7,
    Kind.FLOAT: 8,
    Kind.MESSAGE: 10,
    Kind.ENUM: 11,
}

# Same file, fieldKindToWireJSONCompatibilityGroup: JSON renders 64-bit integers as strings and
# 32-bit integers as numbers, so only same-width integer pairs interchange.
WIRE_JSON_GROUP: Final[Mapping[Kind, int]] = {
    Kind.INT32: 1,
    Kind.UINT32: 1,
    Kind.INT64: 2,
    Kind.UINT64: 2,
    Kind.FIXED32: 3,
    Kind.SFIXED32: 3,
    Kind.FIXED64: 4,
    Kind.SFIXED64: 4,
    Kind.BOOL: 5,
    Kind.SINT32: 6,
    Kind.SINT64: 7,
    Kind.STRING: 8,
    Kind.BYTES: 9,
    Kind.DOUBLE: 10,
    Kind.FLOAT: 11,
    Kind.MESSAGE: 13,
    Kind.ENUM: 14,
}


class Cardinality(enum.Enum):
    """Field cardinality as buf's getCardinality in cardinality.go computes it."""

    OPTIONAL_EXPLICIT = "optional with explicit presence"
    OPTIONAL_IMPLICIT = "optional with implicit presence"
    REPEATED = "repeated"
    MAP = "map"


# cardinality.go: cardinalityToWireCompatibilityGroup and cardinalityToWireJSONCompatibilityGroup.
# `required` is omitted because the model is proto3 only.
WIRE_CARDINALITY_GROUP: Final[Mapping[Cardinality, int]] = {
    Cardinality.OPTIONAL_EXPLICIT: 1,
    Cardinality.OPTIONAL_IMPLICIT: 1,
    Cardinality.REPEATED: 3,
    Cardinality.MAP: 3,
}
WIRE_JSON_CARDINALITY_GROUP: Final[Mapping[Cardinality, int]] = {
    Cardinality.OPTIONAL_EXPLICIT: 1,
    Cardinality.OPTIONAL_IMPLICIT: 1,
    Cardinality.REPEATED: 3,
    Cardinality.MAP: 4,
}


@dataclass(frozen=True, slots=True)
class FieldDef:
    """One message field; `type_name` is the fully qualified name for ENUM and MESSAGE kinds."""

    name: str
    number: int
    kind: Kind
    type_name: str = ""
    repeated: bool = False
    is_map: bool = False
    optional: bool = False
    oneof: str | None = None
    json_name: str | None = None

    @property
    def effective_json_name(self) -> str:
        """The `json_name` protoc would populate: lowerCamelCase unless set explicitly."""
        if self.json_name is not None:
            return self.json_name
        head, *tail = self.name.split("_")
        return head + "".join(part[:1].upper() + part[1:] for part in tail)

    @property
    def cardinality(self) -> Cardinality:
        """Map, then repeated, then explicit presence (optional, in a oneof, or a message)."""
        if self.is_map:
            return Cardinality.MAP
        if self.repeated:
            return Cardinality.REPEATED
        if self.optional or self.oneof is not None or self.kind is Kind.MESSAGE:
            return Cardinality.OPTIONAL_EXPLICIT
        return Cardinality.OPTIONAL_IMPLICIT


@dataclass(frozen=True, slots=True)
class EnumValueDef:
    """One enum value."""

    name: str
    number: int


@dataclass(frozen=True, slots=True)
class EnumDef:
    """A top-level enum with its reserved numbers and names."""

    name: str
    values: tuple[EnumValueDef, ...]
    reserved_numbers: frozenset[int] = frozenset()
    reserved_names: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class MessageDef:
    """A top-level message with its reserved numbers and names."""

    name: str
    fields: tuple[FieldDef, ...] = ()
    reserved_numbers: frozenset[int] = frozenset()
    reserved_names: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RpcDef:
    """One RPC; `request` and `response` are fully qualified message names."""

    name: str
    request: str
    response: str
    client_streaming: bool = False
    server_streaming: bool = False


@dataclass(frozen=True, slots=True)
class ServiceDef:
    """A service and its RPCs."""

    name: str
    rpcs: tuple[RpcDef, ...] = ()


@dataclass(frozen=True, slots=True)
class FileDef:
    """One `.proto` file; `path` is relative to the module root, as in buf."""

    path: str
    package: str
    messages: tuple[MessageDef, ...] = ()
    enums: tuple[EnumDef, ...] = ()
    services: tuple[ServiceDef, ...] = ()


type Module = tuple[FileDef, ...]


class Violation(NamedTuple):
    """One finding, in the shape of a buf annotation: rule id, file path, message."""

    rule: str
    path: str
    message: str


class Category(enum.Enum):
    """Breaking-change categories, strictest first (docs: breaking/rules, Categories)."""

    FILE = "FILE"
    PACKAGE = "PACKAGE"
    WIRE_JSON = "WIRE_JSON"
    WIRE = "WIRE"


def _full(package: str, name: str) -> str:
    return f"{package}.{name}" if package else name


@dataclass(frozen=True, slots=True)
class _View:
    """Indexes over one module version, the analogue of buf's bufprotosource maps."""

    files: Mapping[str, FileDef]
    messages: Mapping[str, tuple[str, MessageDef]]
    enums: Mapping[str, tuple[str, EnumDef]]
    services: Mapping[str, tuple[str, ServiceDef]]
    packages: frozenset[str]
    package_messages: Mapping[str, Mapping[str, str]]
    package_enums: Mapping[str, Mapping[str, str]]

    @staticmethod
    def of(module: Module) -> _View:
        messages: dict[str, tuple[str, MessageDef]] = {}
        enums: dict[str, tuple[str, EnumDef]] = {}
        services: dict[str, tuple[str, ServiceDef]] = {}
        package_messages: dict[str, dict[str, str]] = {}
        package_enums: dict[str, dict[str, str]] = {}
        for file in module:
            for message in file.messages:
                messages[_full(file.package, message.name)] = (file.path, message)
                package_messages.setdefault(file.package, {})[message.name] = file.path
            for enum_def in file.enums:
                enums[_full(file.package, enum_def.name)] = (file.path, enum_def)
                package_enums.setdefault(file.package, {})[enum_def.name] = file.path
            for service in file.services:
                services[_full(file.package, service.name)] = (file.path, service)
        return _View(
            files={file.path: file for file in module},
            messages=messages,
            enums=enums,
            services=services,
            packages=frozenset(file.package for file in module),
            package_messages=package_messages,
            package_enums=package_enums,
        )


type _Check = Callable[[_View, _View], Iterable[Violation]]


class FieldPair(NamedTuple):
    """A field present in both versions, with the message it lives in and its file."""

    path: str
    message: str
    previous: FieldDef
    current: FieldDef
    previous_message: MessageDef
    current_message: MessageDef


def _message_pairs(
    previous: _View, current: _View
) -> Iterable[tuple[str, str, MessageDef, MessageDef]]:
    for full_name, (_, previous_message) in sorted(previous.messages.items()):
        if full_name in current.messages:
            path, current_message = current.messages[full_name]
            yield path, full_name, previous_message, current_message


def _field_pairs(previous: _View, current: _View) -> Iterable[FieldPair]:
    """Fields paired by number inside messages paired by full name, as buf pairs them."""
    for path, full_name, previous_message, current_message in _message_pairs(previous, current):
        by_number = {f.number: f for f in current_message.fields}
        for previous_field in sorted(previous_message.fields, key=lambda f: f.number):
            current_field = by_number.get(previous_field.number)
            if current_field is not None:
                yield FieldPair(
                    path,
                    full_name,
                    previous_field,
                    current_field,
                    previous_message,
                    current_message,
                )


def _field_label(pair: FieldPair) -> str:
    return f'Field "{pair.previous.number}" on message "{pair.message}"'


def _no_delete_message(previous: _View, current: _View, kind: str) -> Iterable[Violation]:
    """Shared body of MESSAGE_NO_DELETE and ENUM_NO_DELETE: compared per file path."""
    for path, previous_file in sorted(previous.files.items()):
        current_file = current.files.get(path)
        if current_file is None:
            continue
        previous_names = (
            [m.name for m in previous_file.messages]
            if kind == "message"
            else [e.name for e in previous_file.enums]
        )
        current_names = (
            {m.name for m in current_file.messages}
            if kind == "message"
            else {e.name for e in current_file.enums}
        )
        for name in previous_names:
            if name not in current_names:
                yield Violation(
                    f"{kind.upper()}_NO_DELETE",
                    path,
                    f'Previously present {kind} "{name}" was deleted from file.',
                )


def _message_no_delete(previous: _View, current: _View) -> Iterable[Violation]:
    return _no_delete_message(previous, current, "message")


def _enum_no_delete(previous: _View, current: _View) -> Iterable[Violation]:
    return _no_delete_message(previous, current, "enum")


def _package_type_no_delete(
    kind: str,
    previous_types: Mapping[str, Mapping[str, str]],
    current_types: Mapping[str, Mapping[str, str]],
) -> Iterable[Violation]:
    """Types may move between files of a package, but the package must still hold one of them.

    `current_types` only has a key for a package with at least one type of this kind, and the
    loop skips packages without a key, exactly as `handleBreakingPackageMessageNoDelete` and
    `handleBreakingPackageEnumNoDelete` do (buf 1.75.0), so removing the last message or the last
    enum of a package is not reported by these rules.
    """
    for package, names in sorted(previous_types.items()):
        remaining = current_types.get(package)
        if remaining is None:
            continue
        for name, path in sorted(names.items()):
            if name not in remaining:
                yield Violation(
                    f"PACKAGE_{kind.upper()}_NO_DELETE",
                    path,
                    f'Previously present {kind} "{name}" was deleted from package "{package}".',
                )


def _package_message_no_delete(previous: _View, current: _View) -> Iterable[Violation]:
    return _package_type_no_delete("message", previous.package_messages, current.package_messages)


def _package_enum_no_delete(previous: _View, current: _View) -> Iterable[Violation]:
    return _package_type_no_delete("enum", previous.package_enums, current.package_enums)


def _package_no_delete(previous: _View, current: _View) -> Iterable[Violation]:
    for package in sorted(previous.packages - current.packages):
        path = min(f.path for f in previous.files.values() if f.package == package)
        yield Violation(
            "PACKAGE_NO_DELETE", path, f'Previously present package "{package}" was deleted.'
        )


def _file_same_package(previous: _View, current: _View) -> Iterable[Violation]:
    for path, previous_file in sorted(previous.files.items()):
        current_file = current.files.get(path)
        if current_file is not None and current_file.package != previous_file.package:
            yield Violation(
                "FILE_SAME_PACKAGE",
                path,
                f'File option "package" changed from "{previous_file.package}"'
                f' to "{current_file.package}".',
            )


def _reserved_message_no_delete(previous: _View, current: _View) -> Iterable[Violation]:
    for path, full_name, previous_message, current_message in _message_pairs(previous, current):
        lost_numbers = previous_message.reserved_numbers - current_message.reserved_numbers
        lost_names = previous_message.reserved_names - current_message.reserved_names
        for number in sorted(lost_numbers):
            yield Violation(
                "RESERVED_MESSAGE_NO_DELETE",
                path,
                f'Previously present reserved number {number} on message "{full_name}"'
                f" was removed.",
            )
        for name in sorted(lost_names):
            yield Violation(
                "RESERVED_MESSAGE_NO_DELETE",
                path,
                f'Previously present reserved name "{name}" on message "{full_name}" was deleted.',
            )


def _reserved_enum_no_delete(previous: _View, current: _View) -> Iterable[Violation]:
    for full_name, (_, previous_enum) in sorted(previous.enums.items()):
        if full_name not in current.enums:
            continue
        path, current_enum = current.enums[full_name]
        for number in sorted(previous_enum.reserved_numbers - current_enum.reserved_numbers):
            yield Violation(
                "RESERVED_ENUM_NO_DELETE",
                path,
                f'Previously present reserved number {number} on enum "{full_name}" was removed.',
            )
        for name in sorted(previous_enum.reserved_names - current_enum.reserved_names):
            yield Violation(
                "RESERVED_ENUM_NO_DELETE",
                path,
                f'Previously present reserved name "{name}" on enum "{full_name}" was deleted.',
            )


def _field_deleted(
    previous: _View,
    current: _View,
    rule: str,
    *,
    number_reserved_ok: bool,
    name_reserved_ok: bool,
) -> Iterable[Violation]:
    """FIELD_NO_DELETE and its two UNLESS_*_RESERVED variants (checkFieldNoDeleteWithRules)."""
    for path, full_name, previous_message, current_message in _message_pairs(previous, current):
        current_numbers = {f.number for f in current_message.fields}
        for field in sorted(previous_message.fields, key=lambda f: f.number):
            if field.number in current_numbers:
                continue
            allowed = (number_reserved_ok and field.number in current_message.reserved_numbers) or (
                name_reserved_ok and field.name in current_message.reserved_names
            )
            if allowed:
                continue
            suffix = ""
            if number_reserved_ok:
                suffix = f' without reserving the number "{field.number}"'
            elif name_reserved_ok:
                suffix = f' without reserving the name "{field.name}"'
            yield Violation(
                rule,
                path,
                f'Previously present field "{field.number}" with name "{field.name}"'
                f' on message "{full_name}" was deleted{suffix}.',
            )


def _field_no_delete(previous: _View, current: _View) -> Iterable[Violation]:
    return _field_deleted(
        previous, current, "FIELD_NO_DELETE", number_reserved_ok=False, name_reserved_ok=False
    )


def _field_no_delete_name(previous: _View, current: _View) -> Iterable[Violation]:
    return _field_deleted(
        previous,
        current,
        "FIELD_NO_DELETE_UNLESS_NAME_RESERVED",
        number_reserved_ok=False,
        name_reserved_ok=True,
    )


def _field_no_delete_number(previous: _View, current: _View) -> Iterable[Violation]:
    return _field_deleted(
        previous,
        current,
        "FIELD_NO_DELETE_UNLESS_NUMBER_RESERVED",
        number_reserved_ok=True,
        name_reserved_ok=False,
    )


def _enum_value_deleted(
    previous: _View,
    current: _View,
    rule: str,
    *,
    number_reserved_ok: bool,
    name_reserved_ok: bool,
) -> Iterable[Violation]:
    for full_name, (_, previous_enum) in sorted(previous.enums.items()):
        if full_name not in current.enums:
            continue
        path, current_enum = current.enums[full_name]
        current_numbers = {v.number for v in current_enum.values}
        for value in previous_enum.values:
            if value.number in current_numbers:
                continue
            allowed = (number_reserved_ok and value.number in current_enum.reserved_numbers) or (
                name_reserved_ok and value.name in current_enum.reserved_names
            )
            if allowed:
                continue
            suffix = ""
            if number_reserved_ok:
                suffix = f' without reserving the number "{value.number}"'
            elif name_reserved_ok:
                suffix = f' without reserving the name "{value.name}"'
            yield Violation(
                rule,
                path,
                f'Previously present enum value "{value.number}" on enum "{full_name}"'
                f" was deleted{suffix}.",
            )


def _enum_value_no_delete(previous: _View, current: _View) -> Iterable[Violation]:
    return _enum_value_deleted(
        previous, current, "ENUM_VALUE_NO_DELETE", number_reserved_ok=False, name_reserved_ok=False
    )


def _enum_value_no_delete_name(previous: _View, current: _View) -> Iterable[Violation]:
    return _enum_value_deleted(
        previous,
        current,
        "ENUM_VALUE_NO_DELETE_UNLESS_NAME_RESERVED",
        number_reserved_ok=False,
        name_reserved_ok=True,
    )


def _enum_value_no_delete_number(previous: _View, current: _View) -> Iterable[Violation]:
    return _enum_value_deleted(
        previous,
        current,
        "ENUM_VALUE_NO_DELETE_UNLESS_NUMBER_RESERVED",
        number_reserved_ok=True,
        name_reserved_ok=False,
    )


def _enum_value_same_name(previous: _View, current: _View) -> Iterable[Violation]:
    """Per number, the previous names must be a subset of the current names (allow_alias)."""
    for full_name, (_, previous_enum) in sorted(previous.enums.items()):
        if full_name not in current.enums:
            continue
        path, current_enum = current.enums[full_name]
        for value in previous_enum.values:
            current_names = {v.name for v in current_enum.values if v.number == value.number}
            if current_names and value.name not in current_names:
                yield Violation(
                    "ENUM_VALUE_SAME_NAME",
                    path,
                    f'Enum value "{value.number}" on enum "{full_name}" changed name'
                    f' from "{value.name}" to "{sorted(current_names)[0]}".',
                )


def _field_same_name(previous: _View, current: _View) -> Iterable[Violation]:
    for pair in _field_pairs(previous, current):
        if pair.previous.name != pair.current.name:
            yield Violation(
                "FIELD_SAME_NAME",
                pair.path,
                f"{_field_label(pair)} changed name from"
                f' "{pair.previous.name}" to "{pair.current.name}".',
            )


def _field_same_json_name(previous: _View, current: _View) -> Iterable[Violation]:
    for pair in _field_pairs(previous, current):
        before, after = pair.previous.effective_json_name, pair.current.effective_json_name
        if before != after:
            yield Violation(
                "FIELD_SAME_JSON_NAME",
                pair.path,
                f'{_field_label(pair)} changed option "json_name" from "{before}" to "{after}".',
            )


def _type_label(field: FieldDef) -> str:
    return field.type_name if field.kind in {Kind.ENUM, Kind.MESSAGE} else field.kind.value


def _changed_type(pair: FieldPair) -> Violation:
    return Violation(
        "",
        pair.path,
        f'{_field_label(pair)} changed type from "{_type_label(pair.previous)}"'
        f' to "{_type_label(pair.current)}".',
    )


def _enum_values(view: _View, full_name: str) -> frozenset[tuple[str, int]]:
    entry = view.enums.get(full_name)
    return frozenset() if entry is None else frozenset((v.name, v.number) for v in entry[1].values)


def _type_name_change_ok(previous: _View, current: _View, pair: FieldPair) -> bool:
    """Same kind, different type name: only an enum with the same short name and a superset.

    Mirrors checkEnumWireCompatibleForField: the previous enum's (name, number) pairs must be a
    subset of the current enum's, so a moved enum may gain values in the same change.
    """
    before, after = pair.previous, pair.current
    if before.type_name == after.type_name:
        return True
    if before.kind is not Kind.ENUM:
        return False
    if before.type_name.rsplit(".", 1)[-1] != after.type_name.rsplit(".", 1)[-1]:
        return False
    return _enum_values(previous, before.type_name) <= _enum_values(current, after.type_name)


def _field_same_type(previous: _View, current: _View) -> Iterable[Violation]:
    for pair in _field_pairs(previous, current):
        before, after = pair.previous, pair.current
        changed = before.kind is not after.kind or (
            before.kind in {Kind.ENUM, Kind.MESSAGE} and before.type_name != after.type_name
        )
        if changed:
            yield _changed_type(pair)._replace(rule="FIELD_SAME_TYPE")


def _group_type_check(
    previous: _View, current: _View, rule: str, groups: Mapping[Kind, int], *, wire: bool
) -> Iterable[Violation]:
    for pair in _field_pairs(previous, current):
        before, after = pair.previous, pair.current
        if groups[before.kind] != groups[after.kind]:
            if wire and before.kind is Kind.STRING and after.kind is Kind.BYTES:
                continue
            yield _changed_type(pair)._replace(rule=rule)
        elif not _type_name_change_ok(previous, current, pair):
            yield _changed_type(pair)._replace(rule=rule)


def _field_wire_compatible_type(previous: _View, current: _View) -> Iterable[Violation]:
    return _group_type_check(previous, current, "FIELD_WIRE_COMPATIBLE_TYPE", WIRE_GROUP, wire=True)


def _field_wire_json_compatible_type(previous: _View, current: _View) -> Iterable[Violation]:
    return _group_type_check(
        previous, current, "FIELD_WIRE_JSON_COMPATIBLE_TYPE", WIRE_JSON_GROUP, wire=False
    )


def _cardinality_check(
    previous: _View, current: _View, rule: str, groups: Mapping[Cardinality, int] | None
) -> Iterable[Violation]:
    for pair in _field_pairs(previous, current):
        before, after = pair.previous.cardinality, pair.current.cardinality
        changed = before is not after if groups is None else groups[before] != groups[after]
        if changed:
            yield Violation(
                rule,
                pair.path,
                f'{_field_label(pair)} changed cardinality from "{before.value}"'
                f' to "{after.value}".',
            )


def _field_same_cardinality(previous: _View, current: _View) -> Iterable[Violation]:
    return _cardinality_check(previous, current, "FIELD_SAME_CARDINALITY", None)


def _field_wire_compatible_cardinality(previous: _View, current: _View) -> Iterable[Violation]:
    return _cardinality_check(
        previous, current, "FIELD_WIRE_COMPATIBLE_CARDINALITY", WIRE_CARDINALITY_GROUP
    )


def _field_wire_json_compatible_cardinality(previous: _View, current: _View) -> Iterable[Violation]:
    return _cardinality_check(
        previous, current, "FIELD_WIRE_JSON_COMPATIBLE_CARDINALITY", WIRE_JSON_CARDINALITY_GROUP
    )


def _field_same_oneof(previous: _View, current: _View) -> Iterable[Violation]:
    """Moving into, out of, or between real oneofs; the synthetic oneof of `optional` is ignored.

    The model stores `optional` and `oneof` separately, so a proto3 `optional` field has
    `oneof is None`, which matches buf skipping synthetic oneofs.
    """
    for pair in _field_pairs(previous, current):
        before, after = pair.previous.oneof, pair.current.oneof
        if before == after:
            continue
        if before is not None and after is not None:
            detail = f'moved from oneof "{before}" to oneof "{after}"'
        else:
            where = "outside to inside" if after is not None else "inside to outside"
            detail = f"moved from {where} a oneof"
        yield Violation("FIELD_SAME_ONEOF", pair.path, f"{_field_label(pair)} {detail}.")


def _service_pairs(
    previous: _View, current: _View
) -> Iterable[tuple[str, str, ServiceDef, ServiceDef]]:
    for full_name, (_, previous_service) in sorted(previous.services.items()):
        if full_name in current.services:
            path, current_service = current.services[full_name]
            yield path, full_name, previous_service, current_service


def _rpc_no_delete(previous: _View, current: _View) -> Iterable[Violation]:
    for path, full_name, previous_service, current_service in _service_pairs(previous, current):
        current_names = {rpc.name for rpc in current_service.rpcs}
        for rpc in previous_service.rpcs:
            if rpc.name not in current_names:
                yield Violation(
                    "RPC_NO_DELETE",
                    path,
                    f'Previously present RPC "{rpc.name}" on service "{full_name}" was deleted.',
                )


def _rpc_same(previous: _View, current: _View, rule: str, attribute: str) -> Iterable[Violation]:
    for path, full_name, previous_service, current_service in _service_pairs(previous, current):
        current_rpcs = {rpc.name: rpc for rpc in current_service.rpcs}
        for old in previous_service.rpcs:
            new = current_rpcs.get(old.name)
            if new is None:
                continue
            before, after = getattr(old, attribute), getattr(new, attribute)
            if before != after:
                yield Violation(
                    rule,
                    path,
                    f'RPC "{old.name}" on service "{full_name}" changed {attribute}'
                    f" from {before!r} to {after!r}.",
                )


def _rpc_same_client_streaming(previous: _View, current: _View) -> Iterable[Violation]:
    return _rpc_same(previous, current, "RPC_SAME_CLIENT_STREAMING", "client_streaming")


def _rpc_same_server_streaming(previous: _View, current: _View) -> Iterable[Violation]:
    return _rpc_same(previous, current, "RPC_SAME_SERVER_STREAMING", "server_streaming")


def _rpc_same_request_type(previous: _View, current: _View) -> Iterable[Violation]:
    return _rpc_same(previous, current, "RPC_SAME_REQUEST_TYPE", "request")


def _rpc_same_response_type(previous: _View, current: _View) -> Iterable[Violation]:
    return _rpc_same(previous, current, "RPC_SAME_RESPONSE_TYPE", "response")


class BreakingRule(NamedTuple):
    """A rule id, the categories that include it, and its check function."""

    rule: str
    categories: frozenset[Category]
    check: _Check


_F, _P, _J, _W = Category.FILE, Category.PACKAGE, Category.WIRE_JSON, Category.WIRE
_ALL = (_F, _P, _J, _W)

# Category membership copied from `buf config ls-breaking-rules --format json` (buf 1.75.0).
BREAKING_RULES: Final[tuple[BreakingRule, ...]] = tuple(
    BreakingRule(rule, frozenset(categories), check)
    for rule, categories, check in (
        ("ENUM_NO_DELETE", (_F,), _enum_no_delete),
        ("MESSAGE_NO_DELETE", (_F,), _message_no_delete),
        ("PACKAGE_MESSAGE_NO_DELETE", (_P,), _package_message_no_delete),
        ("PACKAGE_ENUM_NO_DELETE", (_P,), _package_enum_no_delete),
        ("PACKAGE_NO_DELETE", (_P,), _package_no_delete),
        ("FILE_SAME_PACKAGE", _ALL, _file_same_package),
        ("RESERVED_MESSAGE_NO_DELETE", _ALL, _reserved_message_no_delete),
        ("RESERVED_ENUM_NO_DELETE", _ALL, _reserved_enum_no_delete),
        ("FIELD_NO_DELETE", (_F, _P), _field_no_delete),
        ("FIELD_NO_DELETE_UNLESS_NAME_RESERVED", (_J,), _field_no_delete_name),
        ("FIELD_NO_DELETE_UNLESS_NUMBER_RESERVED", (_J, _W), _field_no_delete_number),
        ("ENUM_VALUE_NO_DELETE", (_F, _P), _enum_value_no_delete),
        ("ENUM_VALUE_NO_DELETE_UNLESS_NAME_RESERVED", (_J,), _enum_value_no_delete_name),
        ("ENUM_VALUE_NO_DELETE_UNLESS_NUMBER_RESERVED", (_J, _W), _enum_value_no_delete_number),
        ("ENUM_VALUE_SAME_NAME", (_F, _P, _J), _enum_value_same_name),
        ("FIELD_SAME_NAME", (_F, _P, _J), _field_same_name),
        ("FIELD_SAME_JSON_NAME", (_F, _P, _J), _field_same_json_name),
        ("FIELD_SAME_TYPE", (_F, _P), _field_same_type),
        ("FIELD_WIRE_JSON_COMPATIBLE_TYPE", (_J,), _field_wire_json_compatible_type),
        ("FIELD_WIRE_COMPATIBLE_TYPE", (_W,), _field_wire_compatible_type),
        ("FIELD_SAME_CARDINALITY", (_F, _P), _field_same_cardinality),
        ("FIELD_WIRE_JSON_COMPATIBLE_CARDINALITY", (_J,), _field_wire_json_compatible_cardinality),
        ("FIELD_WIRE_COMPATIBLE_CARDINALITY", (_W,), _field_wire_compatible_cardinality),
        ("FIELD_SAME_ONEOF", _ALL, _field_same_oneof),
        ("RPC_NO_DELETE", (_F, _P), _rpc_no_delete),
        ("RPC_SAME_CLIENT_STREAMING", _ALL, _rpc_same_client_streaming),
        ("RPC_SAME_SERVER_STREAMING", _ALL, _rpc_same_server_streaming),
        ("RPC_SAME_REQUEST_TYPE", _ALL, _rpc_same_request_type),
        ("RPC_SAME_RESPONSE_TYPE", _ALL, _rpc_same_response_type),
    )
)


def check_breaking(
    previous: Module,
    current: Module,
    category: Category,
    *,
    ignore_unstable_packages: bool = False,
) -> list[Violation]:
    """Run every modelled rule of `category` and return violations sorted by path and rule.

    `ignore_unstable_packages` drops findings in files whose previous or current package ends in
    an unstable version component, the documented behaviour of the buf.yaml key of that name.
    """
    previous_view, current_view = _View.of(previous), _View.of(current)
    found: list[Violation] = []
    for spec in BREAKING_RULES:
        if category in spec.categories:
            found.extend(spec.check(previous_view, current_view))
    if ignore_unstable_packages:
        unstable_paths = {
            f.path
            for f in (*previous, *current)
            if package_stability(f.package) in {"alpha", "beta", "test"}
        }
        found = [v for v in found if v.path not in unstable_paths]
    return sorted(found)


def breaking_rule_ids(violations: Iterable[Violation]) -> frozenset[str]:
    """The set of rule ids in `violations`, the unit of comparison against `buf` output."""
    return frozenset(v.rule for v in violations)


# --- package versions and lint -------------------------------------------------------------


def _number(text: str, minimum: int) -> int | None:
    """An ASCII decimal integer >= minimum, as buf's getNumber accepts (leading zeros allowed)."""
    if not text or not text.isascii() or not text.isdigit():
        return None
    value = int(text)
    return value if value >= minimum else None


def package_stability(package: str) -> str | None:
    """Return stable, alpha, beta, test, or None when the package has no version component.

    Follows protoversion.newPackageVersionForPackage in buf 1.75.0: at least two dot-separated
    components, the last one `v<major>` with major >= 1, optionally `p<patch>` before `alpha` or
    `beta`, an optional minor >= 1 after it, or `test` followed by any suffix.
    """
    parts = package.split(".")
    if len(parts) < 2:
        return None
    component = parts[-1]
    if len(component) < 2 or component[0] != "v":
        return None
    version = component[1:]
    if "test" in version:
        major, _, _ = version.partition("test")
        return "test" if _number(major, 1) is not None else None
    has_alpha, has_beta = "alpha" in version, "beta" in version
    if has_alpha and has_beta:
        return None
    if not has_alpha and not has_beta:
        return "stable" if _number(version, 1) is not None else None
    level = "alpha" if has_alpha else "beta"
    left, _, right = version.partition(level)
    if right and _number(right, 1) is None:
        return None
    major_text, has_patch, patch_text = left.partition("p")
    if _number(major_text, 1) is None:
        return None
    if has_patch and _number(patch_text, 1) is None:
        return None
    return level


_LOWER_SNAKE: Final = re.compile(r"[a-z][a-z0-9]*(_[a-z0-9]+)*")
_UPPER_SNAKE: Final = re.compile(r"[A-Z][A-Z0-9]*(_[A-Z0-9]+)*")


def is_lower_snake_case(name: str) -> bool:
    """`user_id`, `field1`, `user_2_id` pass; `Name`, `userID`, `a__b`, `_x`, `x_` fail."""
    return _LOWER_SNAKE.fullmatch(name) is not None


def is_upper_snake_case(name: str) -> bool:
    """`FOO_BAR`, `FOO_1`, `A` pass; `Foo1`, `FOO__BAR` fail."""
    return _UPPER_SNAKE.fullmatch(name) is not None


def is_pascal_case(name: str) -> bool:
    """First character A-Z and no underscore: `HTTPServer`, `URL`, `Foo1` pass; `FOO_BAR` fails."""
    return bool(name) and "A" <= name[0] <= "Z" and "_" not in name


def to_pascal_case(name: str) -> str:
    """`get_pet` -> `GetPet`; already-PascalCase names are unchanged."""
    return "".join(part[:1].upper() + part[1:] for part in name.split("_") if part)


def to_upper_snake_case(name: str) -> str:
    """`PetStatus` -> `PET_STATUS`, `HTTPMethod` -> `HTTP_METHOD`."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", spaced)
    return spaced.upper()


class LintCategory(enum.IntEnum):
    """The strictness ladder: anything passing STANDARD also passes BASIC and MINIMAL."""

    MINIMAL = 1
    BASIC = 2
    STANDARD = 3


@dataclass(frozen=True, slots=True)
class LintConfig:
    """The buf.yaml `lint` keys the model honours; defaults match buf's documented defaults."""

    use: LintCategory = LintCategory.STANDARD
    enum_zero_value_suffix: str = "_UNSPECIFIED"
    service_suffix: str = "Service"
    rpc_allow_google_protobuf_empty_requests: bool = False
    rpc_allow_google_protobuf_empty_responses: bool = False
    rpc_allow_same_request_response: bool = False


type _LintCheck = Callable[[Module, LintConfig], Iterable[Violation]]


def _directory(path: str) -> str:
    return path.rpartition("/")[0]


def _package_defined(module: Module, _: LintConfig) -> Iterable[Violation]:
    for file in module:
        if not file.package:
            yield Violation("PACKAGE_DEFINED", file.path, "Files must have a package defined.")


def _package_directory_match(module: Module, _: LintConfig) -> Iterable[Violation]:
    for file in module:
        if file.package and _directory(file.path) != file.package.replace(".", "/"):
            expected = file.package.replace(".", "/")
            yield Violation(
                "PACKAGE_DIRECTORY_MATCH",
                file.path,
                f'Files with package "{file.package}" must be within a directory "{expected}"'
                f' relative to root but were in directory "{_directory(file.path)}".',
            )


def _directory_same_package(module: Module, _: LintConfig) -> Iterable[Violation]:
    by_directory: dict[str, set[str]] = {}
    for file in module:
        by_directory.setdefault(_directory(file.path), set()).add(file.package)
    for file in module:
        packages = by_directory[_directory(file.path)]
        if len(packages) > 1:
            yield Violation(
                "DIRECTORY_SAME_PACKAGE",
                file.path,
                f'Multiple packages "{",".join(sorted(packages))}" detected within directory'
                f' "{_directory(file.path)}".',
            )


def _package_same_directory(module: Module, _: LintConfig) -> Iterable[Violation]:
    by_package: dict[str, set[str]] = {}
    for file in module:
        by_package.setdefault(file.package, set()).add(_directory(file.path))
    for file in module:
        directories = by_package[file.package]
        if file.package and len(directories) > 1:
            yield Violation(
                "PACKAGE_SAME_DIRECTORY",
                file.path,
                f'Multiple directories "{",".join(sorted(directories))}" contain files with'
                f' package "{file.package}".',
            )


def _case_rule(
    rule: str,
    label: str,
    predicate: Callable[[str], bool],
    names: Callable[[FileDef], Iterable[str]],
) -> _LintCheck:
    def check(module: Module, _: LintConfig) -> Iterable[Violation]:
        for file in module:
            for name in names(file):
                if not predicate(name):
                    yield Violation(rule, file.path, f'{label} name "{name}" has the wrong case.')

    return check


def _field_names(file: FileDef) -> Iterable[str]:
    return (f.name for m in file.messages for f in m.fields)


def _enum_value_names(file: FileDef) -> Iterable[str]:
    return (v.name for e in file.enums for v in e.values)


def _rpc_names(file: FileDef) -> Iterable[str]:
    return (r.name for s in file.services for r in s.rpcs)


def _package_names(file: FileDef) -> Iterable[str]:
    return (p for p in file.package.split(".") if p)


def _enum_first_value_zero(module: Module, _: LintConfig) -> Iterable[Violation]:
    for file in module:
        for enum_def in file.enums:
            if enum_def.values and enum_def.values[0].number != 0:
                yield Violation(
                    "ENUM_FIRST_VALUE_ZERO",
                    file.path,
                    f'First enum value "{enum_def.values[0].name}" should have a numeric'
                    " value of 0.",
                )


def _enum_value_prefix(module: Module, _: LintConfig) -> Iterable[Violation]:
    for file in module:
        for enum_def in file.enums:
            prefix = to_upper_snake_case(enum_def.name) + "_"
            for value in enum_def.values:
                if not value.name.startswith(prefix):
                    yield Violation(
                        "ENUM_VALUE_PREFIX",
                        file.path,
                        f'Enum value name "{value.name}" should be prefixed with "{prefix}".',
                    )


def _enum_zero_value_suffix(module: Module, config: LintConfig) -> Iterable[Violation]:
    for file in module:
        for enum_def in file.enums:
            for value in enum_def.values:
                if value.number == 0 and not value.name.endswith(config.enum_zero_value_suffix):
                    yield Violation(
                        "ENUM_ZERO_VALUE_SUFFIX",
                        file.path,
                        f'Enum zero value name "{value.name}" should be suffixed with'
                        f' "{config.enum_zero_value_suffix}".',
                    )


def _file_lower_snake_case(module: Module, _: LintConfig) -> Iterable[Violation]:
    for file in module:
        stem = file.path.rpartition("/")[2].removesuffix(".proto")
        if not is_lower_snake_case(stem):
            yield Violation(
                "FILE_LOWER_SNAKE_CASE", file.path, "Filename should be lower_snake_case.proto."
            )


def _package_version_suffix(module: Module, _: LintConfig) -> Iterable[Violation]:
    for file in module:
        if file.package and package_stability(file.package) is None:
            yield Violation(
                "PACKAGE_VERSION_SUFFIX",
                file.path,
                f'Package name "{file.package}" should be suffixed with a correctly formed'
                f' version, such as "{file.package}.v1".',
            )


def _service_suffix(module: Module, config: LintConfig) -> Iterable[Violation]:
    for file in module:
        for service in file.services:
            if not service.name.endswith(config.service_suffix):
                yield Violation(
                    "SERVICE_SUFFIX",
                    file.path,
                    f'Service name "{service.name}" should be suffixed with'
                    f' "{config.service_suffix}".',
                )


_EMPTY: Final = "google.protobuf.Empty"


def _standard_name_rule(rule: str, *, request: bool) -> _LintCheck:
    word = "request" if request else "response"

    def check(module: Module, config: LintConfig) -> Iterable[Violation]:
        allow_empty = (
            config.rpc_allow_google_protobuf_empty_requests
            if request
            else config.rpc_allow_google_protobuf_empty_responses
        )
        for file in module:
            for service in file.services:
                for rpc in service.rpcs:
                    type_name = rpc.request if request else rpc.response
                    if allow_empty and type_name == _EMPTY:
                        continue
                    short = type_name.rsplit(".", 1)[-1]
                    first = to_pascal_case(rpc.name) + word.capitalize()
                    second = to_pascal_case(service.name) + first
                    if short not in {first, second}:
                        yield Violation(
                            rule,
                            file.path,
                            f'RPC {word} type "{short}" should be named "{first}" or "{second}".',
                        )

    return check


def _rpc_request_response_unique(module: Module, config: LintConfig) -> Iterable[Violation]:
    """One finding per RPC whose request equals its response, plus one per shared message type."""
    users: dict[str, set[str]] = {}
    for file in module:
        for service in file.services:
            for rpc in service.rpcs:
                for type_name in {rpc.request, rpc.response}:
                    users.setdefault(type_name, set()).add(f"{service.name}.{rpc.name}")
    for file in module:
        for service in file.services:
            for rpc in service.rpcs:
                if rpc.request == rpc.response and not config.rpc_allow_same_request_response:
                    yield Violation(
                        "RPC_REQUEST_RESPONSE_UNIQUE",
                        file.path,
                        f'RPC "{rpc.name}" has the same type "{rpc.request}" for the request'
                        " and response.",
                    )
                for type_name in sorted({rpc.request, rpc.response}):
                    if len(users[type_name]) > 1:
                        yield Violation(
                            "RPC_REQUEST_RESPONSE_UNIQUE",
                            file.path,
                            f'"{type_name}" is used as the request or response type for'
                            " multiple RPCs.",
                        )


class LintRule(NamedTuple):
    """A lint rule, the lowest category that includes it, and its check function."""

    rule: str
    level: LintCategory
    check: _LintCheck


_MIN, _BAS, _STD = LintCategory.MINIMAL, LintCategory.BASIC, LintCategory.STANDARD

# Category membership copied from `buf config ls-lint-rules --format json` (buf 1.75.0).
LINT_RULES: Final[tuple[LintRule, ...]] = (
    LintRule("PACKAGE_DEFINED", _MIN, _package_defined),
    LintRule("PACKAGE_DIRECTORY_MATCH", _MIN, _package_directory_match),
    LintRule("DIRECTORY_SAME_PACKAGE", _MIN, _directory_same_package),
    LintRule("PACKAGE_SAME_DIRECTORY", _MIN, _package_same_directory),
    LintRule("ENUM_FIRST_VALUE_ZERO", _BAS, _enum_first_value_zero),
    LintRule(
        "ENUM_VALUE_UPPER_SNAKE_CASE",
        _BAS,
        _case_rule(
            "ENUM_VALUE_UPPER_SNAKE_CASE", "Enum value", is_upper_snake_case, _enum_value_names
        ),
    ),
    LintRule(
        "FIELD_LOWER_SNAKE_CASE",
        _BAS,
        _case_rule("FIELD_LOWER_SNAKE_CASE", "Field", is_lower_snake_case, _field_names),
    ),
    LintRule(
        "MESSAGE_PASCAL_CASE",
        _BAS,
        _case_rule(
            "MESSAGE_PASCAL_CASE", "Message", is_pascal_case, lambda f: (m.name for m in f.messages)
        ),
    ),
    LintRule(
        "PACKAGE_LOWER_SNAKE_CASE",
        _BAS,
        _case_rule("PACKAGE_LOWER_SNAKE_CASE", "Package", is_lower_snake_case, _package_names),
    ),
    LintRule(
        "RPC_PASCAL_CASE", _BAS, _case_rule("RPC_PASCAL_CASE", "RPC", is_pascal_case, _rpc_names)
    ),
    LintRule(
        "SERVICE_PASCAL_CASE",
        _BAS,
        _case_rule(
            "SERVICE_PASCAL_CASE", "Service", is_pascal_case, lambda f: (s.name for s in f.services)
        ),
    ),
    LintRule("ENUM_VALUE_PREFIX", _STD, _enum_value_prefix),
    LintRule("ENUM_ZERO_VALUE_SUFFIX", _STD, _enum_zero_value_suffix),
    LintRule("FILE_LOWER_SNAKE_CASE", _STD, _file_lower_snake_case),
    LintRule("PACKAGE_VERSION_SUFFIX", _STD, _package_version_suffix),
    LintRule("RPC_REQUEST_RESPONSE_UNIQUE", _STD, _rpc_request_response_unique),
    LintRule(
        "RPC_REQUEST_STANDARD_NAME",
        _STD,
        _standard_name_rule("RPC_REQUEST_STANDARD_NAME", request=True),
    ),
    LintRule(
        "RPC_RESPONSE_STANDARD_NAME",
        _STD,
        _standard_name_rule("RPC_RESPONSE_STANDARD_NAME", request=False),
    ),
    LintRule("SERVICE_SUFFIX", _STD, _service_suffix),
)


def lint(
    module: Module, config: LintConfig | None = None, *, ignore_rules: Sequence[str] = ()
) -> list[Violation]:
    """Run every modelled lint rule at or below `config.use` and return sorted violations."""
    config = config or LintConfig()
    found: list[Violation] = []
    for spec in LINT_RULES:
        if spec.level <= config.use and spec.rule not in ignore_rules:
            found.extend(spec.check(module, config))
    return sorted(found)


# --- runtime evidence: what a rename or retype does to real encoded data ----------------------

_descriptor_pb2: Any = importlib.import_module("google.protobuf.descriptor_pb2")
_descriptor_pool: Any = importlib.import_module("google.protobuf.descriptor_pool")
_json_format: Any = importlib.import_module("google.protobuf.json_format")
_message_factory: Any = importlib.import_module("google.protobuf.message_factory")


def _dynamic_message_class(package: str, field_name: str, type_code: int) -> Any:
    """A one-field proto3 message `<package>.Pet` built from a FileDescriptorProto."""
    proto = _descriptor_pb2.FileDescriptorProto()
    proto.name = f"{package}.proto"
    proto.package = package
    proto.syntax = "proto3"
    message = proto.message_type.add()
    message.name = "Pet"
    field = message.field.add()
    field.name = field_name
    field.number = 1
    field.type = type_code
    field.label = _descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    pool = _descriptor_pool.DescriptorPool()
    pool.Add(proto)
    return _message_factory.GetMessageClass(pool.FindMessageTypeByName(f"{package}.Pet"))


@dataclass(frozen=True, slots=True)
class RuntimeEffects:
    """Measured consequences of changes that WIRE accepts but WIRE_JSON or FILE reject."""

    renamed_field_read_from_old_bytes: str
    renamed_field_json_parse_error: bool
    int32_json: str
    int64_json: str
    int64_to_int32_truncated: int
    sint32_read_as_int32: int


def measure_runtime_effects() -> RuntimeEffects:
    """Encode with the old schema, decode with the new one, using the real protobuf runtime."""
    kinds: Any = _descriptor_pb2.FieldDescriptorProto
    old_name = _dynamic_message_class("old_name", "name", kinds.TYPE_STRING)
    new_name = _dynamic_message_class("new_name", "full_name", kinds.TYPE_STRING)
    old_bytes: bytes = old_name(name="rex").SerializeToString()
    renamed: str = new_name.FromString(old_bytes).full_name
    old_json: str = _json_format.MessageToJson(old_name(name="rex"), indent=None)
    try:
        _json_format.Parse(old_json, new_name())
        json_error = False
    except _json_format.ParseError:
        json_error = True

    int32 = _dynamic_message_class("age32", "age", kinds.TYPE_INT32)
    int64 = _dynamic_message_class("age64", "age", kinds.TYPE_INT64)
    wide: bytes = int64(age=2**33 + 5).SerializeToString()
    truncated: int = int32.FromString(wide).age

    sint32 = _dynamic_message_class("age_sint", "age", kinds.TYPE_SINT32)
    zigzag: bytes = sint32(age=-1).SerializeToString()
    misread: int = int32.FromString(zigzag).age
    return RuntimeEffects(
        renamed_field_read_from_old_bytes=renamed,
        renamed_field_json_parse_error=json_error,
        int32_json=_json_format.MessageToJson(int32(age=5), indent=None),
        int64_json=_json_format.MessageToJson(int64(age=5), indent=None),
        int64_to_int32_truncated=truncated,
        sint32_read_as_int32=misread,
    )
