"""Tests for the Protobuf Editions presence, enum, and feature-resolution lab."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from api_labs.f19_grpc_and_protocol_buffers.editions_presence import (
    DIALECT_EDITION,
    EDITION_2023,
    EDITION_2024,
    EDITION_2026,
    EDITION_PROTO2,
    EDITION_PROTO3,
    Dialect,
    EncodeError,
    FeatureResolutionError,
    build_probe,
    compile_probe,
    describe_fields,
    edition_defaults,
    enum_is_closed,
    feature_names,
    has_presence,
    is_closed_enum,
    is_packed,
    is_required,
    make_features,
    message_to_json,
    migrate_to_edition,
    probe_file_proto,
    resolve_features,
    unknown_field_count,
)

pytestmark = pytest.mark.unit

IMPLICIT = make_features(field_presence="IMPLICIT")
EXPLICIT = make_features(field_presence="EXPLICIT")
LEGACY_REQUIRED = make_features(field_presence="LEGACY_REQUIRED")
CLOSED = make_features(enum_type="CLOSED")
OPEN = make_features(enum_type="OPEN")
EXPANDED = make_features(repeated_field_encoding="EXPANDED")
PACKED = make_features(repeated_field_encoding="PACKED")

# Wire tags for the probe message: (field_number << 3) | wire_type.
COUNT_ZERO = b"\x08\x00"
OTHER_COUNT_ZERO = b"\x30\x00"
COLOR_UNKNOWN_5 = b"\x18\x05"
COLORS_0_5_1_5 = b"\x20\x00\x20\x05\x20\x01\x20\x05"


def populate(message: Any, values: dict[str, Any]) -> Any:
    """Set scalar fields and extend repeated fields from a plain dict."""
    for name, value in values.items():
        if isinstance(value, list):
            getattr(message, name).extend(value)
        else:
            setattr(message, name, value)
    return message


# ---------------------------------------------------------------------------
# Feature resolution
# ---------------------------------------------------------------------------


def test_compiled_defaults_cover_legacy_proto3_and_editions_2023_to_2026() -> None:
    table = edition_defaults()
    assert [entry.edition for entry in table.defaults] == [900, 999, 1000, 1001, 1002]
    assert (table.minimum_edition, table.maximum_edition) == (EDITION_PROTO2, EDITION_2026)


def test_edition_2023_defaults_match_the_documented_table() -> None:
    assert feature_names(resolve_features(EDITION_2023)) == {
        "field_presence": "EXPLICIT",
        "enum_type": "OPEN",
        "repeated_field_encoding": "PACKED",
        "utf8_validation": "VERIFY",
        "message_encoding": "LENGTH_PREFIXED",
        "json_format": "ALLOW",
        "enforce_naming_style": "STYLE_LEGACY",
        "default_symbol_visibility": "EXPORT_ALL",
        "enforce_proto_limits": "LEGACY_NO_EXPLICIT_LIMITS",
    }


def test_edition_2024_changes_only_naming_style_and_symbol_visibility() -> None:
    old = feature_names(resolve_features(EDITION_2023))
    new = feature_names(resolve_features(EDITION_2024))
    changed = {name: (old[name], new[name]) for name in old if old[name] != new[name]}
    assert changed == {
        "enforce_naming_style": ("STYLE_LEGACY", "STYLE2024"),
        "default_symbol_visibility": ("EXPORT_ALL", "EXPORT_TOP_LEVEL"),
    }


def test_edition_2026_changes_naming_visibility_and_limits_again() -> None:
    old = feature_names(resolve_features(EDITION_2024))
    new = feature_names(resolve_features(EDITION_2026))
    changed = {name: (old[name], new[name]) for name in old if old[name] != new[name]}
    assert changed == {
        "enforce_naming_style": ("STYLE2024", "STYLE2026"),
        "default_symbol_visibility": ("EXPORT_TOP_LEVEL", "STRICT"),
        "enforce_proto_limits": ("LEGACY_NO_EXPLICIT_LIMITS", "PROTO_LIMITS2026"),
    }


def test_proto2_and_proto3_resolve_as_legacy_pseudo_editions() -> None:
    proto2 = feature_names(resolve_features(EDITION_PROTO2))
    proto3 = feature_names(resolve_features(EDITION_PROTO3))
    assert {k: proto2[k] for k in list(proto2)[:6]} == {
        "field_presence": "EXPLICIT",
        "enum_type": "CLOSED",
        "repeated_field_encoding": "EXPANDED",
        "utf8_validation": "NONE",
        "message_encoding": "LENGTH_PREFIXED",
        "json_format": "LEGACY_BEST_EFFORT",
    }
    assert {k: proto3[k] for k in list(proto3)[:6]} == {
        "field_presence": "IMPLICIT",
        "enum_type": "OPEN",
        "repeated_field_encoding": "PACKED",
        "utf8_validation": "VERIFY",
        "message_encoding": "LENGTH_PREFIXED",
        "json_format": "ALLOW",
    }


def test_lexical_scoping_innermost_explicit_setting_wins() -> None:
    file_level = make_features(field_presence="IMPLICIT", enum_type="CLOSED")
    field_in_message = resolve_features(EDITION_2023, file_level, None, EXPLICIT)
    sibling_field = resolve_features(EDITION_2023, file_level, None)
    assert feature_names(field_in_message)["field_presence"] == "EXPLICIT"
    assert feature_names(sibling_field)["field_presence"] == "IMPLICIT"
    # Features nobody set keep the edition default; file-level settings flow down.
    assert feature_names(field_in_message)["repeated_field_encoding"] == "PACKED"
    assert feature_names(field_in_message)["enum_type"] == "CLOSED"


def test_enum_level_override_beats_file_level_but_siblings_inherit() -> None:
    file_level = CLOSED
    pay_type = resolve_features(EDITION_2023, file_level)
    employment = resolve_features(EDITION_2023, file_level, OPEN)
    assert is_closed_enum(pay_type)
    assert not is_closed_enum(employment)


def test_feature_not_yet_introduced_is_fixed_and_cannot_be_overridden() -> None:
    style = make_features(enforce_naming_style="STYLE2024")
    with pytest.raises(FeatureResolutionError, match="fixed in edition"):
        resolve_features(EDITION_2023, style)
    assert feature_names(resolve_features(EDITION_2024, style))["enforce_naming_style"] == (
        "STYLE2024"
    )
    legacy = make_features(enforce_naming_style="STYLE_LEGACY")
    assert feature_names(resolve_features(EDITION_2024, legacy))["enforce_naming_style"] == (
        "STYLE_LEGACY"
    )


@pytest.mark.parametrize("edition", [EDITION_PROTO2 - 1, EDITION_2026 + 1])
def test_edition_outside_the_compiled_range_is_rejected(edition: int) -> None:
    with pytest.raises(FeatureResolutionError, match="outside"):
        resolve_features(edition)


def test_resolution_uses_closest_entry_at_or_before_the_edition() -> None:
    table = copy.deepcopy(edition_defaults())
    index_2024 = next(i for i, e in enumerate(table.defaults) if e.edition == EDITION_2024)
    del table.defaults[index_2024]
    # With no entry for 2024, 2024 must fall back to the 2023 entry, not fail or pick 2026.
    assert resolve_features(EDITION_2024, defaults=table) == resolve_features(EDITION_2023)
    assert resolve_features(EDITION_2024, defaults=table) != resolve_features(EDITION_2024)


def test_make_features_rejects_unknown_names_and_values() -> None:
    with pytest.raises(FeatureResolutionError, match="unknown feature"):
        make_features(field_presense="IMPLICIT")
    with pytest.raises(FeatureResolutionError, match="no value"):
        make_features(field_presence="OPTIONAL")


# ---------------------------------------------------------------------------
# Presence on real messages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "count_tracked", "other_count_tracked"),
    [
        ("proto2", True, True),
        ("proto3", False, True),
        ("edition2023", True, True),
        ("edition2024", True, True),
    ],
)
def test_runtime_presence_by_dialect(
    dialect: Dialect, count_tracked: bool, other_count_tracked: bool
) -> None:
    fields = describe_fields(build_probe(dialect).message_class)
    assert fields["count"].has_presence is count_tracked
    assert fields["other_count"].has_presence is other_count_tracked
    # Repeated fields never track presence in any dialect.
    assert not fields["scores"].has_presence
    assert not fields["colors"].has_presence


def test_hasfield_on_an_implicit_field_raises_and_on_an_explicit_field_answers() -> None:
    probe = build_probe("edition2023", field_features={"count": IMPLICIT})
    message = probe.message_class()
    with pytest.raises(ValueError, match="does not have presence"):
        message.HasField("count")
    assert message.HasField("other_count") is False
    message.other_count = 0
    assert message.HasField("other_count") is True


@pytest.mark.parametrize(
    ("dialect", "file_features", "expected"),
    [
        ("proto2", None, COUNT_ZERO + OTHER_COUNT_ZERO),
        ("proto3", None, OTHER_COUNT_ZERO),
        ("edition2023", None, COUNT_ZERO + OTHER_COUNT_ZERO),
        ("edition2024", None, COUNT_ZERO + OTHER_COUNT_ZERO),
        ("edition2023", IMPLICIT, b""),
    ],
    ids=["proto2", "proto3", "ed2023", "ed2024", "ed2023-file-implicit"],
)
def test_explicit_zero_is_serialized_and_implicit_zero_vanishes(
    dialect: Dialect, file_features: Any, expected: bytes
) -> None:
    message = build_probe(dialect, file_features=file_features).message_class()
    message.count = 0
    message.other_count = 0
    assert message.SerializeToString() == expected


def test_explicit_empty_string_is_serialized_and_implicit_empty_string_is_not() -> None:
    explicit = build_probe("edition2023").message_class()
    implicit = build_probe("edition2023", file_features=IMPLICIT).message_class()
    explicit.label = ""
    implicit.label = ""
    assert explicit.SerializeToString() == b"\x12\x00"
    assert implicit.SerializeToString() == b""


def test_implicit_peer_round_trip_erases_presence_of_an_explicit_zero() -> None:
    explicit = build_probe("edition2023").message_class
    implicit = build_probe("edition2023", file_features=IMPLICIT).message_class
    sender = explicit()
    sender.count = 0
    wire = sender.SerializeToString()
    assert wire == COUNT_ZERO
    # Control: the explicit receiver sees the zero as present.
    assert explicit.FromString(wire).HasField("count")
    # The implicit peer reads 0 and re-emits nothing; the original sender loses presence.
    peer = implicit.FromString(wire)
    assert peer.count == 0
    echoed = peer.SerializeToString()
    assert echoed == b""
    assert not explicit.FromString(echoed).HasField("count")


def test_merge_from_skips_implicit_zero_but_applies_explicit_zero() -> None:
    probe = build_probe(
        "edition2023",
        field_features={"count": IMPLICIT, "other_count": EXPLICIT},
    )
    destination = populate(probe.message_class(), {"count": 7, "other_count": 7})
    source = populate(probe.message_class(), {"count": 0, "other_count": 0})
    destination.MergeFrom(source)
    assert destination.count == 7
    assert destination.other_count == 0


def test_protojson_omits_implicit_zero_unless_asked_and_keeps_explicit_zero() -> None:
    probe = build_probe(
        "edition2023",
        field_features={"count": IMPLICIT, "other_count": EXPLICIT},
    )
    message = populate(probe.message_class(), {"count": 0, "other_count": 0})
    assert json.loads(message_to_json(message)) == {"otherCount": 0}
    everything = message_to_json(message, always_print_fields_with_no_presence=True)
    # The option also prints empty repeated fields, but never unset explicit-presence fields.
    assert json.loads(everything) == {"count": 0, "otherCount": 0, "colors": [], "scores": []}


def test_proto3_optional_is_a_synthetic_oneof_that_editions_do_not_have() -> None:
    proto3 = build_probe("proto3")
    field = proto3.file_proto.message_type[0].field[5]
    assert field.name == "other_count"
    assert field.proto3_optional is True
    assert [oneof.name for oneof in proto3.file_proto.message_type[0].oneof_decl] == [
        "_other_count"
    ]
    assert proto3.message_class.DESCRIPTOR.fields_by_name["other_count"].containing_oneof.name == (
        "_other_count"
    )
    edition = build_probe("edition2023")
    assert list(edition.file_proto.message_type[0].oneof_decl) == []
    assert edition.message_class.DESCRIPTOR.fields_by_name["other_count"].containing_oneof is None


def test_legacy_required_blocks_serialization_until_the_field_is_set() -> None:
    probe = build_probe("edition2023", field_features={"count": LEGACY_REQUIRED})
    message = probe.message_class()
    assert describe_fields(probe.message_class)["count"].is_required
    assert message.IsInitialized() is False
    with pytest.raises(EncodeError, match="missing required fields: count"):
        message.SerializeToString()
    message.count = 0
    assert message.SerializeToString() == COUNT_ZERO


def test_implicit_presence_field_cannot_declare_an_explicit_default() -> None:
    file_proto = probe_file_proto("edition2023", field_features={"count": IMPLICIT})
    file_proto.message_type[0].field[0].default_value = "5"
    with pytest.raises(TypeError, match="implicit presence cannot have explicit defaults"):
        compile_probe(file_proto, "edition2023")
    # Control: with explicit presence the same default is legal and is not "present".
    control = probe_file_proto("edition2023")
    control.message_type[0].field[0].default_value = "5"
    message = compile_probe(control, "edition2023").message_class()
    assert message.count == 5
    assert message.HasField("count") is False


# ---------------------------------------------------------------------------
# Open versus closed enums
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "file_features", "enum_features", "closed"),
    [
        ("proto2", None, None, True),
        ("proto3", None, None, False),
        ("edition2023", None, None, False),
        ("edition2024", None, None, False),
        ("edition2023", None, CLOSED, True),
        ("edition2023", CLOSED, None, True),
        ("edition2023", CLOSED, OPEN, False),
    ],
    ids=["proto2", "proto3", "ed2023", "ed2024", "enum-closed", "file-closed", "enum-beats-file"],
)
def test_unknown_enum_value_is_stored_when_open_and_parked_as_unknown_when_closed(
    dialect: Dialect, file_features: Any, enum_features: Any, closed: bool
) -> None:
    probe = build_probe(dialect, file_features=file_features, enum_features=enum_features)
    message = probe.message_class.FromString(COLOR_UNKNOWN_5)
    assert enum_is_closed(probe.message_class, "color") is closed
    assert message.color == (0 if closed else 5)
    assert unknown_field_count(message) == (1 if closed else 0)
    if dialect != "proto3":
        assert message.HasField("color") is (not closed)
    # Both flavors re-emit the original bytes: closed via unknown fields, open via the field.
    assert message.SerializeToString() == COLOR_UNKNOWN_5


@pytest.mark.parametrize(
    ("dialect", "enum_features", "values", "wire"),
    [
        ("proto3", None, [0, 5, 1, 5], b"\x22\x04\x00\x05\x01\x05"),
        ("proto2", None, [0, 1], b"\x20\x00\x20\x01\x20\x05\x20\x05"),
        ("edition2023", CLOSED, [0, 1], b"\x22\x02\x00\x01\x20\x05\x20\x05"),
    ],
    ids=["open-proto3", "closed-proto2-expanded", "closed-edition-packed"],
)
def test_closed_repeated_enum_moves_unknown_values_to_the_end(
    dialect: Dialect, enum_features: Any, values: list[int], wire: bytes
) -> None:
    probe = build_probe(dialect, enum_features=enum_features)
    message = probe.message_class.FromString(COLORS_0_5_1_5)
    assert list(message.colors) == values
    assert message.SerializeToString() == wire


# ---------------------------------------------------------------------------
# Repeated field encoding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "file_features", "field_features", "wire"),
    [
        ("proto2", None, None, b"\x28\x01\x28\x02"),
        ("proto3", None, None, b"\x2a\x02\x01\x02"),
        ("edition2023", None, None, b"\x2a\x02\x01\x02"),
        ("edition2023", EXPANDED, None, b"\x28\x01\x28\x02"),
        ("edition2023", EXPANDED, {"scores": PACKED}, b"\x2a\x02\x01\x02"),
        ("edition2024", None, {"scores": EXPANDED}, b"\x28\x01\x28\x02"),
    ],
    ids=["proto2", "proto3", "ed2023", "file-expanded", "field-packed", "ed2024-field-expanded"],
)
def test_repeated_scalar_encoding_follows_the_resolved_feature(
    dialect: Dialect, file_features: Any, field_features: Any, wire: bytes
) -> None:
    probe = build_probe(dialect, file_features=file_features, field_features=field_features)
    message = populate(probe.message_class(), {"scores": [1, 2]})
    assert message.SerializeToString() == wire
    assert describe_fields(probe.message_class)["scores"].is_packed is (wire[0] == 0x2A)
    # Parsers accept either form whatever the schema says.
    other = b"\x28\x01\x28\x02" if wire[0] == 0x2A else b"\x2a\x02\x01\x02"
    assert list(probe.message_class.FromString(other).scores) == [1, 2]


# ---------------------------------------------------------------------------
# Resolver versus runtime
# ---------------------------------------------------------------------------

CASES: list[tuple[Dialect, Any, dict[str, Any], Any]] = [
    ("proto2", None, {}, None),
    ("proto3", None, {}, None),
    ("edition2023", None, {}, None),
    ("edition2024", None, {}, None),
    ("edition2023", IMPLICIT, {}, None),
    ("edition2023", IMPLICIT, {"count": EXPLICIT, "scores": EXPANDED}, CLOSED),
    (
        "edition2024",
        make_features(repeated_field_encoding="EXPANDED"),
        {"count": LEGACY_REQUIRED},
        None,
    ),
    ("edition2023", CLOSED, {"count": IMPLICIT}, OPEN),
]


@pytest.mark.parametrize(("dialect", "file_features", "fields", "enum_features"), CASES)
def test_python_resolver_agrees_with_the_runtime_descriptor(
    dialect: Dialect, file_features: Any, fields: dict[str, Any], enum_features: Any
) -> None:
    probe = build_probe(
        dialect,
        file_features=file_features,
        field_features=fields or None,
        enum_features=enum_features,
    )
    edition = DIALECT_EDITION[dialect]
    count = resolve_features(edition, file_features, fields.get("count"))
    scores = resolve_features(edition, file_features, fields.get("scores"))
    color = resolve_features(edition, file_features, enum_features)

    runtime = describe_fields(probe.message_class)
    assert runtime["count"].has_presence == has_presence(count)
    assert runtime["count"].is_required == is_required(count)
    assert runtime["scores"].is_packed == is_packed(scores)
    assert enum_is_closed(probe.message_class, "color") == is_closed_enum(color)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

SAMPLES: list[dict[str, Any]] = [
    {"count": 0},
    {"count": 3, "other_count": 0, "label": ""},
    {"count": 0, "other_count": 4, "label": "x", "color": 1, "colors": [0, 1], "scores": [1, 2]},
    {"count": 9, "scores": [300, 5], "colors": [1, 1]},
]


def test_migrating_proto3_to_edition_2023_preserves_presence_and_wire_bytes() -> None:
    original = build_probe("proto3")
    migrated_file = migrate_to_edition(original.file_proto)
    migrated = compile_probe(migrated_file, "edition2023")

    assert migrated_file.edition == EDITION_2023
    assert migrated_file.syntax == "editions"
    assert (
        migrated_file.options.features.field_presence
        == make_features(field_presence="IMPLICIT").field_presence
    )
    assert list(migrated_file.message_type[0].oneof_decl) == []
    other = migrated_file.message_type[0].field[5]
    assert other.proto3_optional is False
    assert other.options.features == EXPLICIT

    assert describe_fields(migrated.message_class) == describe_fields(original.message_class)
    for values in SAMPLES:
        old = populate(original.message_class(), values).SerializeToString()
        new = populate(migrated.message_class(), values).SerializeToString()
        assert new == old
        assert original.message_class.FromString(new).SerializeToString() == old


def test_migrating_proto2_to_edition_2023_preserves_required_packed_and_closed_enums() -> None:
    file_proto = probe_file_proto("proto2")
    message = file_proto.message_type[0]
    message.field[0].label = message.field[0].LABEL_REQUIRED
    message.field[4].options.packed = True
    original = compile_probe(file_proto, "proto2")

    migrated_file = migrate_to_edition(file_proto)
    migrated = compile_probe(migrated_file, "edition2023")
    assert migrated_file.message_type[0].field[0].options.features == LEGACY_REQUIRED
    assert not migrated_file.message_type[0].field[4].options.HasField("packed")
    assert migrated_file.message_type[0].field[4].options.features == PACKED

    assert describe_fields(migrated.message_class) == describe_fields(original.message_class)
    assert enum_is_closed(migrated.message_class, "color")
    for values in SAMPLES:
        old = populate(original.message_class(), values).SerializeToString()
        new = populate(migrated.message_class(), values).SerializeToString()
        assert new == old
    raw = b"\x08\x01" + COLOR_UNKNOWN_5 + COLORS_0_5_1_5
    old_parsed = original.message_class.FromString(raw)
    new_parsed = migrated.message_class.FromString(raw)
    assert unknown_field_count(new_parsed) == unknown_field_count(old_parsed) == 3
    assert new_parsed.SerializeToString() == old_parsed.SerializeToString()
    with pytest.raises(EncodeError, match="missing required fields: count"):
        migrated.message_class().SerializeToString()


def test_migration_refuses_files_that_are_already_editions() -> None:
    with pytest.raises(ValueError, match="cannot migrate"):
        migrate_to_edition(probe_file_proto("edition2023"))


def test_probe_builder_rejects_features_on_legacy_syntax_files() -> None:
    with pytest.raises(ValueError, match="cannot carry features"):
        build_probe("proto3", file_features=IMPLICIT)
    with pytest.raises(ValueError, match="unknown probe fields"):
        build_probe("edition2023", field_features={"nope": IMPLICIT})
