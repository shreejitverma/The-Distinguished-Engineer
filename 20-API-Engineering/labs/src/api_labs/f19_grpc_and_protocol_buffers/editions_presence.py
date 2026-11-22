"""Protobuf Editions feature resolution and presence semantics lab.

Three mechanisms, all executed against the installed protobuf 7.36.2 runtime (upb backend):

1. Feature resolution: the algorithm from "Implementing Editions Support" on protobuf.dev
   (binary-search the compiled ``FeatureSetDefaults``, merge overridable into fixed features,
   merge each lexical level's explicit ``FeatureSet``), run over the defaults table that the
   wheel ships in ``google/protobuf/internal/python_edition_defaults.py``.
2. A probe schema compiled four ways (proto2, proto3, edition 2023, edition 2024) from
   ``FileDescriptorProto`` objects, so explicit versus implicit presence, open versus closed
   enums, and packed versus expanded encoding can be observed on real messages and wire bytes.
3. A Prototiller-style migration of a proto2 or proto3 ``FileDescriptorProto`` to an edition,
   whose wire behavior can be compared with the original.

protobuf 7.36.2 ships neither type stubs nor ``py.typed``, so the runtime is loaded once through
``importlib`` and treated as ``Any`` at this single boundary.
"""

from __future__ import annotations

import bisect
import functools
import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

_pb: Any = importlib.import_module("google.protobuf.descriptor_pb2")
_pool_module: Any = importlib.import_module("google.protobuf.descriptor_pool")
_factory: Any = importlib.import_module("google.protobuf.message_factory")
_json_format: Any = importlib.import_module("google.protobuf.json_format")
_message_module: Any = importlib.import_module("google.protobuf.message")
_unknown: Any = importlib.import_module("google.protobuf.unknown_fields")
_defaults_module: Any = importlib.import_module("google.protobuf.internal.python_edition_defaults")

EDITION_PROTO2: Final[int] = _pb.EDITION_PROTO2
EDITION_PROTO3: Final[int] = _pb.EDITION_PROTO3
EDITION_2023: Final[int] = _pb.EDITION_2023
EDITION_2024: Final[int] = _pb.EDITION_2024
EDITION_2026: Final[int] = _pb.EDITION_2026

EncodeError: Final[type[Exception]] = _message_module.EncodeError

Dialect = Literal["proto2", "proto3", "edition2023", "edition2024"]

DIALECT_EDITION: Final[dict[str, int]] = {
    "proto2": EDITION_PROTO2,
    "proto3": EDITION_PROTO3,
    "edition2023": EDITION_2023,
    "edition2024": EDITION_2024,
}


class FeatureResolutionError(ValueError):
    """Raised when a feature set cannot be resolved for an edition."""


# ---------------------------------------------------------------------------
# 1. Feature resolution
# ---------------------------------------------------------------------------


def make_features(**values: str) -> Any:
    """Build a ``FeatureSet`` from enum value names, e.g. ``field_presence="IMPLICIT"``."""
    features = _pb.FeatureSet()
    fields = features.DESCRIPTOR.fields_by_name
    for name, value in values.items():
        if name not in fields:
            raise FeatureResolutionError(f"unknown feature {name!r}")
        enum_values = fields[name].enum_type.values_by_name
        if value not in enum_values:
            raise FeatureResolutionError(f"feature {name!r} has no value {value!r}")
        setattr(features, name, enum_values[value].number)
    return features


def feature_names(features: Any) -> dict[str, str]:
    """Return every feature of a ``FeatureSet`` as ``{feature: VALUE_NAME}``."""
    return {
        field.name: field.enum_type.values_by_number[getattr(features, field.name)].name
        for field in features.DESCRIPTOR.fields
    }


@functools.cache
def edition_defaults() -> Any:
    """Parse the ``FeatureSetDefaults`` compiled into the protobuf Python wheel."""
    defaults = _pb.FeatureSetDefaults()
    defaults.ParseFromString(_defaults_module._PROTOBUF_INTERNAL_PYTHON_EDITION_DEFAULTS)
    return defaults


def resolve_features(
    edition: int,
    *levels: Any | None,
    defaults: Any | None = None,
) -> Any:
    """Resolve the feature set at the innermost of ``levels`` for a file at ``edition``.

    ``levels`` run from the file options down to the descriptor being resolved (message, then
    field, for example); ``None`` means that level sets nothing. Steps follow the
    "Feature Resolution" section of "Implementing Editions Support" on protobuf.dev.
    """
    table = edition_defaults() if defaults is None else defaults
    if not table.minimum_edition <= edition <= table.maximum_edition:
        raise FeatureResolutionError(
            f"edition {edition} is outside [{table.minimum_edition}, {table.maximum_edition}]"
        )
    editions = [entry.edition for entry in table.defaults]
    index = bisect.bisect_right(editions, edition) - 1
    if index < 0:
        raise FeatureResolutionError(f"no defaults at or before edition {edition}")
    chosen = table.defaults[index]
    resolved = _pb.FeatureSet()
    resolved.CopyFrom(chosen.fixed_features)
    resolved.MergeFrom(chosen.overridable_features)
    for level in levels:
        if level is None:
            continue
        for field, _ in level.ListFields():
            if chosen.fixed_features.HasField(field.name):
                raise FeatureResolutionError(
                    f"feature {field.name!r} is fixed in edition {edition} and cannot be set"
                )
        resolved.MergeFrom(level)
    return resolved


def has_presence(features: Any, *, repeated: bool = False, message_or_oneof: bool = False) -> bool:
    """``FieldDescriptor::has_presence`` expressed over resolved features."""
    if repeated:
        return False
    if message_or_oneof:
        return True
    return bool(features.field_presence != _pb.FeatureSet.IMPLICIT)


def is_closed_enum(features: Any) -> bool:
    """``EnumDescriptor::is_closed`` expressed over resolved features."""
    return bool(features.enum_type == _pb.FeatureSet.CLOSED)


def is_packed(features: Any) -> bool:
    """``FieldDescriptor::is_packed`` for a repeated scalar numeric field."""
    return bool(features.repeated_field_encoding == _pb.FeatureSet.PACKED)


def is_required(features: Any) -> bool:
    """``FieldDescriptor::is_required`` expressed over resolved features."""
    return bool(features.field_presence == _pb.FeatureSet.LEGACY_REQUIRED)


# ---------------------------------------------------------------------------
# 2. The probe schema in four dialects
# ---------------------------------------------------------------------------

# name, number, type, repeated
_PROBE_FIELDS: Final[tuple[tuple[str, int, str, bool], ...]] = (
    ("count", 1, "int32", False),
    ("label", 2, "string", False),
    ("color", 3, "enum", False),
    ("colors", 4, "enum", True),
    ("scores", 5, "int32", True),
    ("other_count", 6, "int32", False),
)

_TYPE_CODES: Final[dict[str, int]] = {
    "int32": _pb.FieldDescriptorProto.TYPE_INT32,
    "string": _pb.FieldDescriptorProto.TYPE_STRING,
    "enum": _pb.FieldDescriptorProto.TYPE_ENUM,
}


@dataclass(frozen=True, slots=True)
class Probe:
    """A compiled probe message and the descriptor it was built from."""

    dialect: Dialect
    file_proto: Any
    message_class: Any
    pool: Any


def probe_file_proto(
    dialect: Dialect,
    *,
    file_features: Any | None = None,
    field_features: Mapping[str, Any] | None = None,
    enum_features: Any | None = None,
) -> Any:
    """Build the probe ``FileDescriptorProto``: ``Color`` enum plus ``Probe`` message.

    ``other_count`` is a ``proto3`` ``optional`` field (explicit presence) in ``proto3`` and an
    ordinary field everywhere else, so a caller can override its features independently of
    ``count``.
    """
    editioned = dialect in ("edition2023", "edition2024")
    if not editioned and (file_features or field_features or enum_features):
        raise ValueError(f"{dialect} files cannot carry features")
    overrides = dict(field_features or {})
    unknown = set(overrides) - {name for name, *_ in _PROBE_FIELDS}
    if unknown:
        raise ValueError(f"unknown probe fields: {sorted(unknown)}")

    package = f"probe.{dialect}"
    file_proto = _pb.FileDescriptorProto(name=f"probe_{dialect}.proto", package=package)
    if editioned:
        file_proto.syntax = "editions"
        file_proto.edition = DIALECT_EDITION[dialect]
        if file_features is not None:
            file_proto.options.features.CopyFrom(file_features)
    else:
        file_proto.syntax = dialect

    color = file_proto.enum_type.add(name="Color")
    color.value.add(name="COLOR_UNSPECIFIED", number=0)
    color.value.add(name="COLOR_RED", number=1)
    if enum_features is not None:
        color.options.features.CopyFrom(enum_features)

    message = file_proto.message_type.add(name="Probe")
    for name, number, kind, repeated in _PROBE_FIELDS:
        field = message.field.add(name=name, number=number, type=_TYPE_CODES[kind])
        field.label = (
            _pb.FieldDescriptorProto.LABEL_REPEATED
            if repeated
            else _pb.FieldDescriptorProto.LABEL_OPTIONAL
        )
        if kind == "enum":
            field.type_name = f".{package}.Color"
        if name in overrides:
            field.options.features.CopyFrom(overrides[name])
        if dialect == "proto3" and name == "other_count":
            field.proto3_optional = True
            field.oneof_index = 0
            message.oneof_decl.add(name="_other_count")
    return file_proto


def compile_probe(file_proto: Any, dialect: Dialect) -> Probe:
    """Add ``file_proto`` to a fresh descriptor pool and return the ``Probe`` message class."""
    pool = _pool_module.DescriptorPool()
    pool.AddSerializedFile(file_proto.SerializeToString())
    descriptor = pool.FindMessageTypeByName(f"{file_proto.package}.Probe")
    return Probe(dialect, file_proto, _factory.GetMessageClass(descriptor), pool)


def build_probe(
    dialect: Dialect,
    *,
    file_features: Any | None = None,
    field_features: Mapping[str, Any] | None = None,
    enum_features: Any | None = None,
) -> Probe:
    """Build and compile the probe message in ``dialect``."""
    file_proto = probe_file_proto(
        dialect,
        file_features=file_features,
        field_features=field_features,
        enum_features=enum_features,
    )
    return compile_probe(file_proto, dialect)


@dataclass(frozen=True, slots=True)
class FieldBehavior:
    """Runtime answers for one field, from the descriptor helpers the docs list."""

    name: str
    has_presence: bool
    is_packed: bool
    is_required: bool


def describe_fields(message_class: Any) -> dict[str, FieldBehavior]:
    """Report ``has_presence``, ``is_packed`` and ``is_required`` for every field."""
    return {
        field.name: FieldBehavior(
            field.name, bool(field.has_presence), bool(field.is_packed), bool(field.is_required)
        )
        for field in message_class.DESCRIPTOR.fields
    }


def enum_is_closed(message_class: Any, field_name: str) -> bool:
    """Return the runtime's ``EnumDescriptor.is_closed`` for an enum-typed field."""
    field = message_class.DESCRIPTOR.fields_by_name[field_name]
    return bool(field.enum_type.is_closed)


def message_to_json(message: Any, *, always_print_fields_with_no_presence: bool = False) -> str:
    """Render ProtoJSON on one line; implicit-presence zeros appear only when asked for."""
    return str(
        _json_format.MessageToJson(
            message,
            indent=None,
            always_print_fields_with_no_presence=always_print_fields_with_no_presence,
        )
    )


def unknown_field_count(message: Any) -> int:
    """Number of unknown-field records the message is holding."""
    return len(_unknown.UnknownFieldSet(message))


# ---------------------------------------------------------------------------
# 3. Prototiller-style migration of a FileDescriptorProto
# ---------------------------------------------------------------------------


def migrate_to_edition(file_proto: Any, edition: int = EDITION_2023) -> Any:
    """Rewrite a proto2 or proto3 ``FileDescriptorProto`` as an edition file.

    Mirrors the manual steps in "Preserving proto2 or proto3 Behavior in Edition 2023" and
    "Caveats and Exceptions" on protobuf.dev, for top-level messages only: file-level features
    reproduce the dialect defaults, ``optional`` becomes ``field_presence = EXPLICIT``,
    ``required`` becomes ``LEGACY_REQUIRED``, and ``packed`` becomes
    ``repeated_field_encoding``.
    """
    syntax = file_proto.syntax or "proto2"
    if syntax not in ("proto2", "proto3"):
        raise ValueError(f"cannot migrate a {syntax!r} file")
    migrated = _pb.FileDescriptorProto()
    migrated.CopyFrom(file_proto)
    migrated.syntax = "editions"
    migrated.edition = edition

    features = migrated.options.features
    feature_set = _pb.FeatureSet
    if syntax == "proto3":
        features.field_presence = feature_set.IMPLICIT
    else:
        features.enum_type = feature_set.CLOSED
        features.repeated_field_encoding = feature_set.EXPANDED
        features.utf8_validation = feature_set.NONE
        features.json_format = feature_set.LEGACY_BEST_EFFORT

    for message in migrated.message_type:
        synthetic: list[int] = []
        for field in message.field:
            if field.proto3_optional:
                synthetic.append(field.oneof_index)
                field.ClearField("proto3_optional")
                field.ClearField("oneof_index")
                field.options.features.field_presence = feature_set.EXPLICIT
            if field.label == _pb.FieldDescriptorProto.LABEL_REQUIRED:
                field.label = _pb.FieldDescriptorProto.LABEL_OPTIONAL
                field.options.features.field_presence = feature_set.LEGACY_REQUIRED
            if field.options.HasField("packed"):
                packed = field.options.packed
                field.options.ClearField("packed")
                if syntax == "proto2" and packed:
                    field.options.features.repeated_field_encoding = feature_set.PACKED
                elif syntax == "proto3" and not packed:
                    field.options.features.repeated_field_encoding = feature_set.EXPANDED
        for index in sorted(synthetic, reverse=True):
            del message.oneof_decl[index]
    return migrated
