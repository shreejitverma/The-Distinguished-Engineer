"""Schema governance for Protocol Buffers with Buf: model fidelity against buf 1.75.0 output.

The ORACLE tables below were captured by running `buf breaking` and `buf lint` 1.75.0 on the
same schemas on macOS arm64 (the note lists the commands); the tests assert that the
in-process model in `buf_governance.py` reports the same rule ids, so they fail if a rule, a
category membership, or a pairing mechanism of the model is broken.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final

import pytest

from api_labs.f19_grpc_and_protocol_buffers.buf_governance import (
    BREAKING_RULES,
    LINT_RULES,
    Category,
    EnumDef,
    EnumValueDef,
    FieldDef,
    FileDef,
    Kind,
    LintCategory,
    LintConfig,
    MessageDef,
    Module,
    RpcDef,
    ServiceDef,
    breaking_rule_ids,
    check_breaking,
    is_lower_snake_case,
    is_pascal_case,
    is_upper_snake_case,
    lint,
    measure_runtime_effects,
    package_stability,
)

if TYPE_CHECKING:
    from collections.abc import Callable

PKG: Final = "acme.pet.v1"
CATEGORIES: Final = (Category.FILE, Category.PACKAGE, Category.WIRE_JSON, Category.WIRE)

# --- fixtures: the schema the captured buf runs used -------------------------------------------

STATUS = EnumDef(
    "Status",
    (
        EnumValueDef("STATUS_UNSPECIFIED", 0),
        EnumValueDef("STATUS_ACTIVE", 1),
        EnumValueDef("STATUS_RETIRED", 2),
    ),
)
PET = MessageDef(
    "Pet",
    (
        FieldDef("name", 1, Kind.STRING),
        FieldDef("age", 2, Kind.INT32),
        FieldDef("status", 3, Kind.ENUM, f"{PKG}.Status"),
        FieldDef("tags", 4, Kind.STRING, repeated=True),
        FieldDef("note", 5, Kind.STRING),
        FieldDef("blob", 6, Kind.BYTES),
        FieldDef("owner_id", 7, Kind.STRING),
    ),
    reserved_numbers=frozenset({9}),
    reserved_names=frozenset({"legacy"}),
)
GET_REQ = MessageDef("GetPetRequest", (FieldDef("name", 1, Kind.STRING),))
GET_RES = MessageDef("GetPetResponse", (FieldDef("pet", 1, Kind.MESSAGE, f"{PKG}.Pet"),))
LIST_REQ = MessageDef("ListPetsRequest", (FieldDef("page_size", 1, Kind.INT32),))
LIST_RES = MessageDef("ListPetsResponse", (FieldDef("pet", 1, Kind.MESSAGE, f"{PKG}.Pet"),))
GET_RPC = RpcDef("GetPet", f"{PKG}.GetPetRequest", f"{PKG}.GetPetResponse")
LIST_RPC = RpcDef("ListPets", f"{PKG}.ListPetsRequest", f"{PKG}.ListPetsResponse")
BASE: Module = (
    FileDef(
        "acme/pet/v1/pet.proto",
        PKG,
        messages=(PET, GET_REQ, GET_RES, LIST_REQ, LIST_RES),
        enums=(STATUS,),
        services=(ServiceDef("PetService", (GET_RPC, LIST_RPC)),),
    ),
)

type Edit = Callable[[MessageDef], MessageDef]


def edit_pet(base: Module, edit: Edit) -> Module:
    """Apply `edit` to the `Pet` message of the first file."""
    file = base[0]
    messages = tuple(edit(m) if m.name == "Pet" else m for m in file.messages)
    return (replace(file, messages=messages), *base[1:])


def change_field(number: int, **changes: Any) -> Edit:
    """Replace attributes of the field with `number`."""

    def edit(pet: MessageDef) -> MessageDef:
        fields = tuple(replace(f, **changes) if f.number == number else f for f in pet.fields)
        return replace(pet, fields=fields)

    return edit


def drop_field(
    number: int, *, reserve_number: bool = False, reserve_name: str | None = None
) -> Edit:
    """Delete a field, optionally reserving its number and/or its name."""

    def edit(pet: MessageDef) -> MessageDef:
        numbers = pet.reserved_numbers | ({number} if reserve_number else frozenset())
        names = pet.reserved_names | ({reserve_name} if reserve_name else frozenset())
        return replace(
            pet,
            fields=tuple(f for f in pet.fields if f.number != number),
            reserved_numbers=frozenset(numbers),
            reserved_names=frozenset(names),
        )

    return edit


def with_enum(base: Module, enum_def: EnumDef) -> Module:
    return (replace(base[0], enums=(enum_def,)), *base[1:])


def with_rpcs(base: Module, *rpcs: RpcDef) -> Module:
    return (replace(base[0], services=(ServiceDef("PetService", rpcs),)), *base[1:])


def split_status(base: Module) -> Module:
    """Move `Status` into its own file of the same package."""
    return (replace(base[0], enums=()), FileDef("acme/pet/v1/status.proto", PKG, enums=(STATUS,)))


def without_list_request(base: Module) -> Module:
    file = base[0]
    messages = tuple(m for m in file.messages if m.name != "ListPetsRequest")
    return (replace(file, messages=messages, services=(ServiceDef("PetService", (GET_RPC,)),)),)


def without_status_enum(base: Module) -> Module:
    pet_module = edit_pet(base, drop_field(3, reserve_number=True, reserve_name="status"))
    return (replace(pet_module[0], enums=()),)


STATUS_WITHOUT_RETIRED = replace(STATUS, values=STATUS.values[:2])
CASES: Final[dict[str, Module]] = {
    "rename_field": edit_pet(BASE, change_field(1, name="full_name")),
    "int32_to_int64": edit_pet(BASE, change_field(2, kind=Kind.INT64)),
    "int32_to_string": edit_pet(BASE, change_field(2, kind=Kind.STRING)),
    "delete_field_reserve_number": edit_pet(BASE, drop_field(4, reserve_number=True)),
    "delete_field_reserve_both": edit_pet(
        BASE, drop_field(4, reserve_number=True, reserve_name="tags")
    ),
    "delete_field_no_reserve": edit_pet(BASE, drop_field(4)),
    "delete_enum_value_reserve_number": with_enum(
        BASE, replace(STATUS_WITHOUT_RETIRED, reserved_numbers=frozenset({2}))
    ),
    "delete_enum_value_reserve_name": with_enum(
        BASE, replace(STATUS_WITHOUT_RETIRED, reserved_names=frozenset({"STATUS_RETIRED"}))
    ),
    "string_to_bytes": edit_pet(BASE, change_field(5, kind=Kind.BYTES)),
    "bytes_to_string": edit_pet(BASE, change_field(6, kind=Kind.STRING)),
    "implicit_to_optional": edit_pet(BASE, change_field(7, optional=True)),
    "repeated_to_scalar": edit_pet(BASE, change_field(4, repeated=False)),
    "field_into_oneof": edit_pet(BASE, change_field(5, oneof="extra")),
    "add_field": edit_pet(
        BASE, lambda pet: replace(pet, fields=(*pet.fields, FieldDef("color", 8, Kind.STRING)))
    ),
    "add_rpc": with_rpcs(
        BASE,
        RpcDef("DeletePet", f"{PKG}.GetPetRequest", f"{PKG}.GetPetResponse"),
        GET_RPC,
        LIST_RPC,
    ),
    "rpc_server_streaming": with_rpcs(BASE, GET_RPC, replace(LIST_RPC, server_streaming=True)),
    "rpc_new_request_type": with_rpcs(
        BASE, replace(GET_RPC, request=f"{PKG}.ListPetsRequest"), LIST_RPC
    ),
    "delete_rpc": with_rpcs(BASE, GET_RPC),
    "change_package": (replace(BASE[0], package="acme.pet.v2"),),
    "move_enum_to_new_file": split_status(BASE),
    "drop_reserved": edit_pet(
        BASE, lambda pet: replace(pet, reserved_numbers=frozenset(), reserved_names=frozenset())
    ),
    "delete_message": without_list_request(BASE),
    "enum_value_rename": with_enum(
        BASE, replace(STATUS, values=(*STATUS.values[:2], EnumValueDef("STATUS_ARCHIVED", 2)))
    ),
    "reuse_reserved_number": edit_pet(
        BASE,
        lambda pet: replace(
            pet,
            fields=(*pet.fields, FieldDef("color", 9, Kind.STRING)),
            reserved_numbers=frozenset(),
        ),
    ),
    "delete_enum": without_status_enum(BASE),
    "enum_value_add": with_enum(
        BASE, replace(STATUS, values=(*STATUS.values, EnumValueDef("STATUS_NEW", 3)))
    ),
}

# Captured: `buf breaking . --against ../old --error-format json` once per category, with
# `breaking: {use: [<CATEGORY>]}` in the new directory's buf.yaml.  Order: FILE, PACKAGE,
# WIRE_JSON, WIRE.  Each entry is the sorted set of rule ids buf reported.
ORACLE: Final[dict[str, tuple[tuple[str, ...], ...]]] = {
    "rename_field": (
        (
            "FIELD_SAME_JSON_NAME",
            "FIELD_SAME_NAME",
        ),
        (
            "FIELD_SAME_JSON_NAME",
            "FIELD_SAME_NAME",
        ),
        (
            "FIELD_SAME_JSON_NAME",
            "FIELD_SAME_NAME",
        ),
        (),
    ),
    "int32_to_int64": (
        ("FIELD_SAME_TYPE",),
        ("FIELD_SAME_TYPE",),
        ("FIELD_WIRE_JSON_COMPATIBLE_TYPE",),
        (),
    ),
    "int32_to_string": (
        ("FIELD_SAME_TYPE",),
        ("FIELD_SAME_TYPE",),
        ("FIELD_WIRE_JSON_COMPATIBLE_TYPE",),
        ("FIELD_WIRE_COMPATIBLE_TYPE",),
    ),
    "delete_field_reserve_number": (
        ("FIELD_NO_DELETE",),
        ("FIELD_NO_DELETE",),
        ("FIELD_NO_DELETE_UNLESS_NAME_RESERVED",),
        (),
    ),
    "delete_field_reserve_both": (("FIELD_NO_DELETE",), ("FIELD_NO_DELETE",), (), ()),
    "delete_field_no_reserve": (
        ("FIELD_NO_DELETE",),
        ("FIELD_NO_DELETE",),
        (
            "FIELD_NO_DELETE_UNLESS_NAME_RESERVED",
            "FIELD_NO_DELETE_UNLESS_NUMBER_RESERVED",
        ),
        ("FIELD_NO_DELETE_UNLESS_NUMBER_RESERVED",),
    ),
    "delete_enum_value_reserve_number": (
        ("ENUM_VALUE_NO_DELETE",),
        ("ENUM_VALUE_NO_DELETE",),
        ("ENUM_VALUE_NO_DELETE_UNLESS_NAME_RESERVED",),
        (),
    ),
    "delete_enum_value_reserve_name": (
        ("ENUM_VALUE_NO_DELETE",),
        ("ENUM_VALUE_NO_DELETE",),
        ("ENUM_VALUE_NO_DELETE_UNLESS_NUMBER_RESERVED",),
        ("ENUM_VALUE_NO_DELETE_UNLESS_NUMBER_RESERVED",),
    ),
    "string_to_bytes": (
        ("FIELD_SAME_TYPE",),
        ("FIELD_SAME_TYPE",),
        ("FIELD_WIRE_JSON_COMPATIBLE_TYPE",),
        (),
    ),
    "bytes_to_string": (
        ("FIELD_SAME_TYPE",),
        ("FIELD_SAME_TYPE",),
        ("FIELD_WIRE_JSON_COMPATIBLE_TYPE",),
        ("FIELD_WIRE_COMPATIBLE_TYPE",),
    ),
    "implicit_to_optional": (("FIELD_SAME_CARDINALITY",), ("FIELD_SAME_CARDINALITY",), (), ()),
    "repeated_to_scalar": (
        ("FIELD_SAME_CARDINALITY",),
        ("FIELD_SAME_CARDINALITY",),
        ("FIELD_WIRE_JSON_COMPATIBLE_CARDINALITY",),
        ("FIELD_WIRE_COMPATIBLE_CARDINALITY",),
    ),
    "field_into_oneof": (
        (
            "FIELD_SAME_CARDINALITY",
            "FIELD_SAME_ONEOF",
        ),
        (
            "FIELD_SAME_CARDINALITY",
            "FIELD_SAME_ONEOF",
        ),
        ("FIELD_SAME_ONEOF",),
        ("FIELD_SAME_ONEOF",),
    ),
    "add_field": ((), (), (), ()),
    "add_rpc": ((), (), (), ()),
    "rpc_server_streaming": (
        ("RPC_SAME_SERVER_STREAMING",),
        ("RPC_SAME_SERVER_STREAMING",),
        ("RPC_SAME_SERVER_STREAMING",),
        ("RPC_SAME_SERVER_STREAMING",),
    ),
    "rpc_new_request_type": (
        ("RPC_SAME_REQUEST_TYPE",),
        ("RPC_SAME_REQUEST_TYPE",),
        ("RPC_SAME_REQUEST_TYPE",),
        ("RPC_SAME_REQUEST_TYPE",),
    ),
    "delete_rpc": (("RPC_NO_DELETE",), ("RPC_NO_DELETE",), (), ()),
    "change_package": (
        ("FILE_SAME_PACKAGE",),
        (
            "FILE_SAME_PACKAGE",
            "PACKAGE_NO_DELETE",
        ),
        ("FILE_SAME_PACKAGE",),
        ("FILE_SAME_PACKAGE",),
    ),
    "move_enum_to_new_file": (("ENUM_NO_DELETE",), (), (), ()),
    "drop_reserved": (
        ("RESERVED_MESSAGE_NO_DELETE",),
        ("RESERVED_MESSAGE_NO_DELETE",),
        ("RESERVED_MESSAGE_NO_DELETE",),
        ("RESERVED_MESSAGE_NO_DELETE",),
    ),
    "delete_message": (
        (
            "MESSAGE_NO_DELETE",
            "RPC_NO_DELETE",
        ),
        (
            "PACKAGE_MESSAGE_NO_DELETE",
            "RPC_NO_DELETE",
        ),
        (),
        (),
    ),
    "enum_value_rename": (
        ("ENUM_VALUE_SAME_NAME",),
        ("ENUM_VALUE_SAME_NAME",),
        ("ENUM_VALUE_SAME_NAME",),
        (),
    ),
    "reuse_reserved_number": (
        ("RESERVED_MESSAGE_NO_DELETE",),
        ("RESERVED_MESSAGE_NO_DELETE",),
        ("RESERVED_MESSAGE_NO_DELETE",),
        ("RESERVED_MESSAGE_NO_DELETE",),
    ),
    "delete_enum": (
        (
            "ENUM_NO_DELETE",
            "FIELD_NO_DELETE",
        ),
        ("FIELD_NO_DELETE",),
        (),
        (),
    ),
    "enum_value_add": ((), (), (), ()),
}

# Captured: `buf config ls-breaking-rules --format json` (the CSR category is not modelled).
BREAKING_CATEGORIES: Final[dict[str, frozenset[str]]] = {
    "ENUM_NO_DELETE": frozenset({"FILE"}),
    "MESSAGE_NO_DELETE": frozenset({"FILE"}),
    "PACKAGE_MESSAGE_NO_DELETE": frozenset({"PACKAGE"}),
    "PACKAGE_ENUM_NO_DELETE": frozenset({"PACKAGE"}),
    "PACKAGE_NO_DELETE": frozenset({"PACKAGE"}),
    "FILE_SAME_PACKAGE": frozenset({"FILE", "PACKAGE", "WIRE_JSON", "WIRE"}),
    "RESERVED_MESSAGE_NO_DELETE": frozenset({"FILE", "PACKAGE", "WIRE_JSON", "WIRE"}),
    "RESERVED_ENUM_NO_DELETE": frozenset({"FILE", "PACKAGE", "WIRE_JSON", "WIRE"}),
    "FIELD_NO_DELETE": frozenset({"FILE", "PACKAGE"}),
    "FIELD_NO_DELETE_UNLESS_NAME_RESERVED": frozenset({"WIRE_JSON"}),
    "FIELD_NO_DELETE_UNLESS_NUMBER_RESERVED": frozenset({"WIRE_JSON", "WIRE"}),
    "ENUM_VALUE_NO_DELETE": frozenset({"FILE", "PACKAGE"}),
    "ENUM_VALUE_NO_DELETE_UNLESS_NAME_RESERVED": frozenset({"WIRE_JSON"}),
    "ENUM_VALUE_NO_DELETE_UNLESS_NUMBER_RESERVED": frozenset({"WIRE_JSON", "WIRE"}),
    "ENUM_VALUE_SAME_NAME": frozenset({"FILE", "PACKAGE", "WIRE_JSON"}),
    "FIELD_SAME_NAME": frozenset({"FILE", "PACKAGE", "WIRE_JSON"}),
    "FIELD_SAME_JSON_NAME": frozenset({"FILE", "PACKAGE", "WIRE_JSON"}),
    "FIELD_SAME_TYPE": frozenset({"FILE", "PACKAGE"}),
    "FIELD_WIRE_JSON_COMPATIBLE_TYPE": frozenset({"WIRE_JSON"}),
    "FIELD_WIRE_COMPATIBLE_TYPE": frozenset({"WIRE"}),
    "FIELD_SAME_CARDINALITY": frozenset({"FILE", "PACKAGE"}),
    "FIELD_WIRE_JSON_COMPATIBLE_CARDINALITY": frozenset({"WIRE_JSON"}),
    "FIELD_WIRE_COMPATIBLE_CARDINALITY": frozenset({"WIRE"}),
    "FIELD_SAME_ONEOF": frozenset({"FILE", "PACKAGE", "WIRE_JSON", "WIRE"}),
    "RPC_NO_DELETE": frozenset({"FILE", "PACKAGE"}),
    "RPC_SAME_CLIENT_STREAMING": frozenset({"FILE", "PACKAGE", "WIRE_JSON", "WIRE"}),
    "RPC_SAME_SERVER_STREAMING": frozenset({"FILE", "PACKAGE", "WIRE_JSON", "WIRE"}),
    "RPC_SAME_REQUEST_TYPE": frozenset({"FILE", "PACKAGE", "WIRE_JSON", "WIRE"}),
    "RPC_SAME_RESPONSE_TYPE": frozenset({"FILE", "PACKAGE", "WIRE_JSON", "WIRE"}),
}

# Captured: `buf config ls-lint-rules --format json`, lowest of MINIMAL, BASIC, STANDARD.
LINT_LEVELS: Final[dict[str, str]] = {
    "PACKAGE_DEFINED": "MINIMAL",
    "PACKAGE_DIRECTORY_MATCH": "MINIMAL",
    "DIRECTORY_SAME_PACKAGE": "MINIMAL",
    "PACKAGE_SAME_DIRECTORY": "MINIMAL",
    "ENUM_FIRST_VALUE_ZERO": "BASIC",
    "ENUM_VALUE_UPPER_SNAKE_CASE": "BASIC",
    "FIELD_LOWER_SNAKE_CASE": "BASIC",
    "MESSAGE_PASCAL_CASE": "BASIC",
    "PACKAGE_LOWER_SNAKE_CASE": "BASIC",
    "RPC_PASCAL_CASE": "BASIC",
    "SERVICE_PASCAL_CASE": "BASIC",
    "ENUM_VALUE_PREFIX": "STANDARD",
    "ENUM_ZERO_VALUE_SUFFIX": "STANDARD",
    "FILE_LOWER_SNAKE_CASE": "STANDARD",
    "PACKAGE_VERSION_SUFFIX": "STANDARD",
    "RPC_REQUEST_RESPONSE_UNIQUE": "STANDARD",
    "RPC_REQUEST_STANDARD_NAME": "STANDARD",
    "RPC_RESPONSE_STANDARD_NAME": "STANDARD",
    "SERVICE_SUFFIX": "STANDARD",
}


def model(module_name: str, category: Category) -> tuple[str, ...]:
    violations = check_breaking(BASE, CASES[module_name], category)
    return tuple(sorted(breaking_rule_ids(violations)))


@pytest.mark.unit
@pytest.mark.parametrize("case", sorted(CASES))
def test_breaking_rule_ids_match_buf_1_75_0_for_every_category(case: str) -> None:
    for index, category in enumerate(CATEGORIES):
        assert model(case, category) == ORACLE[case][index], (case, category.value)


@pytest.mark.unit
def test_every_case_has_an_oracle_row_and_the_oracle_has_no_orphans() -> None:
    assert set(CASES) == set(ORACLE)
    assert len(CASES) == 26


@pytest.mark.unit
def test_modelled_rule_categories_equal_buf_config_listing() -> None:
    modelled = {spec.rule: frozenset(c.value for c in spec.categories) for spec in BREAKING_RULES}
    assert modelled == BREAKING_CATEGORIES


@pytest.mark.unit
def test_wire_accepts_a_rename_that_wire_json_and_file_reject() -> None:
    case = CASES["rename_field"]
    assert check_breaking(BASE, case, Category.WIRE) == []
    assert breaking_rule_ids(check_breaking(BASE, case, Category.WIRE_JSON)) == {
        "FIELD_SAME_NAME",
        "FIELD_SAME_JSON_NAME",
    }
    assert breaking_rule_ids(check_breaking(BASE, case, Category.FILE)) >= {"FIELD_SAME_NAME"}


@pytest.mark.unit
def test_file_rejects_deleting_a_field_even_when_number_and_name_are_reserved() -> None:
    case = CASES["delete_field_reserve_both"]
    assert breaking_rule_ids(check_breaking(BASE, case, Category.FILE)) == {"FIELD_NO_DELETE"}
    assert check_breaking(BASE, case, Category.WIRE_JSON) == []
    assert check_breaking(BASE, case, Category.WIRE) == []


@pytest.mark.unit
def test_int32_to_int64_is_wire_compatible_but_not_json_compatible() -> None:
    case = CASES["int32_to_int64"]
    assert check_breaking(BASE, case, Category.WIRE) == []
    assert breaking_rule_ids(check_breaking(BASE, case, Category.WIRE_JSON)) == {
        "FIELD_WIRE_JSON_COMPATIBLE_TYPE"
    }


@pytest.mark.unit
def test_string_to_bytes_passes_wire_but_bytes_to_string_fails_it() -> None:
    assert check_breaking(BASE, CASES["string_to_bytes"], Category.WIRE) == []
    assert breaking_rule_ids(check_breaking(BASE, CASES["bytes_to_string"], Category.WIRE)) == {
        "FIELD_WIRE_COMPATIBLE_TYPE"
    }


@pytest.mark.unit
def test_package_change_hides_every_full_name_paired_rule_behind_file_same_package() -> None:
    renamed = replace(BASE[0], package="acme.pet.v2")
    retyped = edit_pet((renamed,), change_field(2, kind=Kind.STRING))
    found = check_breaking(BASE, retyped, Category.FILE)
    assert breaking_rule_ids(found) == {"FILE_SAME_PACKAGE"}


def _package_module(
    enums: tuple[str, ...] = (), messages: tuple[str, ...] = (), package: str = "a.v1"
) -> Module:
    return (
        FileDef(
            "a/v1/a.proto",
            package,
            messages=tuple(MessageDef(n) for n in messages),
            enums=tuple(EnumDef(n, (EnumValueDef(f"{n.upper()}_UNSPECIFIED", 0),)) for n in enums),
        ),
    )


@pytest.mark.unit
def test_package_category_skips_a_package_that_loses_its_last_enum() -> None:
    old = _package_module(enums=("Color", "Shape"), messages=("M",))
    one_left = _package_module(enums=("Shape",), messages=("M",))
    none_left = _package_module(messages=("M",))
    assert breaking_rule_ids(check_breaking(old, one_left, Category.PACKAGE)) == {
        "PACKAGE_ENUM_NO_DELETE"
    }
    assert check_breaking(old, none_left, Category.PACKAGE) == []
    assert breaking_rule_ids(check_breaking(old, none_left, Category.FILE)) == {"ENUM_NO_DELETE"}


@pytest.mark.unit
def test_package_category_skips_a_package_that_loses_its_last_message() -> None:
    old = _package_module(enums=("Shape",), messages=("MReq", "MRes"))
    one_left = _package_module(enums=("Shape",), messages=("MReq",))
    none_left = _package_module(enums=("Shape",))
    assert breaking_rule_ids(check_breaking(old, one_left, Category.PACKAGE)) == {
        "PACKAGE_MESSAGE_NO_DELETE"
    }
    assert check_breaking(old, none_left, Category.PACKAGE) == []
    assert breaking_rule_ids(check_breaking(old, none_left, Category.FILE)) == {"MESSAGE_NO_DELETE"}


@pytest.mark.unit
def test_moving_a_type_between_files_of_a_package_passes_package_but_not_file() -> None:
    moved = CASES["move_enum_to_new_file"]
    assert check_breaking(BASE, moved, Category.PACKAGE) == []
    assert breaking_rule_ids(check_breaking(BASE, moved, Category.FILE)) == {"ENUM_NO_DELETE"}


@pytest.mark.unit
def test_ignore_unstable_packages_drops_findings_in_beta_but_not_in_v1() -> None:
    def pet(package: str, kind: Kind) -> Module:
        message = MessageDef("Pet", (FieldDef("name", 1, kind),))
        return (FileDef("p.proto", package, messages=(message,)),)

    beta_old, beta_new = pet("acme.pet.v1beta1", Kind.STRING), pet("acme.pet.v1beta1", Kind.INT32)
    assert breaking_rule_ids(check_breaking(beta_old, beta_new, Category.FILE)) == {
        "FIELD_SAME_TYPE"
    }
    assert check_breaking(beta_old, beta_new, Category.FILE, ignore_unstable_packages=True) == []
    stable_old, stable_new = pet("acme.pet.v1", Kind.STRING), pet("acme.pet.v1", Kind.INT32)
    kept = check_breaking(stable_old, stable_new, Category.FILE, ignore_unstable_packages=True)
    assert breaking_rule_ids(kept) == {"FIELD_SAME_TYPE"}


@pytest.mark.unit
def test_enum_retype_is_wire_compatible_only_for_same_short_name_and_superset_values() -> None:
    """Derived from checkEnumWireCompatibleForField in the buf source, not from a captured run."""

    def enum_field(type_name: str) -> Module:
        message = MessageDef("Pet", (FieldDef("status", 1, Kind.ENUM, type_name),))
        old = EnumDef("Status", (EnumValueDef("S_UNSPECIFIED", 0), EnumValueDef("S_ON", 1)))
        grown = EnumDef(
            "Status",
            (EnumValueDef("S_UNSPECIFIED", 0), EnumValueDef("S_ON", 1), EnumValueDef("S_OFF", 2)),
        )
        other = EnumDef("Other", (EnumValueDef("O_UNSPECIFIED", 0),))
        return (
            FileDef("a/v1/a.proto", "a.v1", messages=(message,), enums=(old,)),
            FileDef("a/v2/a.proto", "a.v2", enums=(grown, other)),
        )

    before = enum_field("a.v1.Status")
    assert check_breaking(before, enum_field("a.v2.Status"), Category.WIRE) == []
    assert breaking_rule_ids(check_breaking(before, enum_field("a.v2.Other"), Category.WIRE)) == {
        "FIELD_WIRE_COMPATIBLE_TYPE"
    }


# --- lint -------------------------------------------------------------------------------------

LINT_MODULE: Final[Module] = (
    FileDef(
        "acme/pet/v1/pet.proto",
        PKG,
        messages=(
            MessageDef(
                "Pet",
                (FieldDef("Name", 1, Kind.STRING), FieldDef("color", 2, Kind.ENUM, f"{PKG}.Color")),
            ),
        ),
        enums=(EnumDef("Color", (EnumValueDef("RED", 0), EnumValueDef("GREEN", 1))),),
        services=(
            ServiceDef(
                "Petstore",
                (
                    RpcDef("get_pet", f"{PKG}.Pet", f"{PKG}.Pet"),
                    RpcDef("Ping", "google.protobuf.Empty", "google.protobuf.Empty"),
                ),
            ),
        ),
    ),
    FileDef("wrong/dir.proto", "acme.other", messages=(MessageDef("Thing"),)),
)

# Captured: `buf lint --error-format json` with `lint: {use: [<CATEGORY>]}` and module path
# `proto`, as sorted (path, rule, count) tuples with the `proto/` prefix removed.
LINT_ORACLE: Final[dict[str, tuple[tuple[str, str, int], ...]]] = {
    "MINIMAL": (("wrong/dir.proto", "PACKAGE_DIRECTORY_MATCH", 1),),
    "BASIC": (
        ("acme/pet/v1/pet.proto", "FIELD_LOWER_SNAKE_CASE", 1),
        ("acme/pet/v1/pet.proto", "RPC_PASCAL_CASE", 1),
        ("wrong/dir.proto", "PACKAGE_DIRECTORY_MATCH", 1),
    ),
    "STANDARD": (
        ("acme/pet/v1/pet.proto", "ENUM_VALUE_PREFIX", 2),
        ("acme/pet/v1/pet.proto", "ENUM_ZERO_VALUE_SUFFIX", 1),
        ("acme/pet/v1/pet.proto", "FIELD_LOWER_SNAKE_CASE", 1),
        ("acme/pet/v1/pet.proto", "RPC_PASCAL_CASE", 1),
        ("acme/pet/v1/pet.proto", "RPC_REQUEST_RESPONSE_UNIQUE", 2),
        ("acme/pet/v1/pet.proto", "RPC_REQUEST_STANDARD_NAME", 2),
        ("acme/pet/v1/pet.proto", "RPC_RESPONSE_STANDARD_NAME", 2),
        ("acme/pet/v1/pet.proto", "SERVICE_SUFFIX", 1),
        ("wrong/dir.proto", "PACKAGE_DIRECTORY_MATCH", 1),
        ("wrong/dir.proto", "PACKAGE_VERSION_SUFFIX", 1),
    ),
}


@pytest.mark.unit
@pytest.mark.parametrize("category", list(LintCategory), ids=lambda c: c.name)
def test_lint_findings_match_buf_1_75_0_per_category(category: LintCategory) -> None:
    found = lint(LINT_MODULE, LintConfig(use=category))
    counts = Counter((v.path, v.rule) for v in found)
    assert tuple(sorted((p, r, n) for (p, r), n in counts.items())) == LINT_ORACLE[category.name]


@pytest.mark.unit
def test_lint_rule_levels_equal_buf_config_listing() -> None:
    assert {spec.rule: spec.level.name for spec in LINT_RULES} == LINT_LEVELS


@pytest.mark.unit
def test_lint_ladder_is_monotone_so_standard_findings_contain_basic_and_minimal() -> None:
    ids = {c: {v.rule for v in lint(LINT_MODULE, LintConfig(use=c))} for c in LintCategory}
    assert ids[LintCategory.MINIMAL] < ids[LintCategory.BASIC] < ids[LintCategory.STANDARD]


@pytest.mark.unit
def test_lint_service_suffix_and_empty_allowances_follow_config() -> None:
    config = LintConfig(
        service_suffix="store",
        rpc_allow_google_protobuf_empty_requests=True,
        rpc_allow_google_protobuf_empty_responses=True,
    )
    rules = Counter(v.rule for v in lint(LINT_MODULE, config))
    assert "SERVICE_SUFFIX" not in rules
    assert rules["RPC_REQUEST_STANDARD_NAME"] == 1  # only get_pet(Pet) remains
    assert rules["RPC_RESPONSE_STANDARD_NAME"] == 1


@pytest.mark.unit
def test_rpc_request_response_unique_reports_every_shared_message_type() -> None:
    pkg = "acme.x.v1"
    service = ServiceDef(
        "XService",
        (
            RpcDef("A", f"{pkg}.AReq", f"{pkg}.ARes"),
            RpcDef("B", f"{pkg}.AReq", f"{pkg}.BRes"),
            RpcDef("C", f"{pkg}.ARes", f"{pkg}.BRes"),
        ),
    )
    module: Module = (FileDef("acme/x/v1/x.proto", pkg, services=(service,)),)
    found = [v for v in lint(module) if v.rule == "RPC_REQUEST_RESPONSE_UNIQUE"]
    assert Counter(v.message for v in found) == {
        '"acme.x.v1.AReq" is used as the request or response type for multiple RPCs.': 2,
        '"acme.x.v1.ARes" is used as the request or response type for multiple RPCs.': 2,
        '"acme.x.v1.BRes" is used as the request or response type for multiple RPCs.': 2,
    }


# Captured: `buf lint --path <dir> --error-format json` with only PACKAGE_VERSION_SUFFIX enabled,
# one file per package; True means buf accepted the package.
PACKAGE_VERSION_ORACLE: Final[dict[str, bool]] = {
    "foo.v1": True,
    "foo.v2": True,
    "foo.v10": True,
    "v1": False,
    "foo.v0": False,
    "foo.v01": True,
    "foo.v1alpha": True,
    "foo.v1alpha0": False,
    "foo.v1alpha1": True,
    "foo.v1alpha02": True,
    "foo.v1beta2": True,
    "foo.v1p1alpha": True,
    "foo.v1p1beta2": True,
    "foo.v1p0beta": False,
    "foo.v1p01beta": True,
    "foo.v1test": True,
    "foo.v1testfoo": True,
    "foo.v1gamma": False,
    "foo.v1alphabeta": False,
    "foo.V1": False,
    "foo.v1.bar": False,
    "foo.v": False,
    "foo.v1x": False,
    "foo.v0alpha1": False,
    "foo.v2test1": True,
}


@pytest.mark.unit
@pytest.mark.parametrize(("package", "accepted"), sorted(PACKAGE_VERSION_ORACLE.items()))
def test_package_version_acceptance_matches_buf(package: str, accepted: bool) -> None:
    assert (package_stability(package) is not None) is accepted


@pytest.mark.unit
def test_package_stability_levels() -> None:
    assert package_stability("foo.v1") == "stable"
    assert package_stability("foo.v1beta2") == "beta"
    assert package_stability("foo.v1p1alpha1") == "alpha"
    assert package_stability("foo.v1testfoo") == "test"
    assert package_stability("v1") is None


# Captured: one file per name under `buf lint` with FIELD_LOWER_SNAKE_CASE,
# MESSAGE_PASCAL_CASE, and ENUM_VALUE_UPPER_SNAKE_CASE enabled; these are the names buf rejected.
NAMES: Final = [
    "name",
    "Name",
    "user_id",
    "userID",
    "user_id2",
    "field1",
    "field_1",
    "_x",
    "x_",
    "a__b",
    "URL",
    "url",
    "fooBar",
    "foo_Bar",
    "a",
    "A",
    "x1y",
    "user_2_id",
    "HTTPServer",
    "HttpServer",
    "Http_Server",
    "FOO_BAR",
    "FOO__BAR",
    "FOO_1",
    "Foo1",
    "foo1Bar",
]
FIELD_REJECTED: Final = frozenset(
    [
        "Name",
        "userID",
        "_x",
        "x_",
        "a__b",
        "URL",
        "fooBar",
        "foo_Bar",
        "A",
        "HTTPServer",
        "HttpServer",
        "Http_Server",
        "FOO_BAR",
        "FOO__BAR",
        "FOO_1",
        "Foo1",
        "foo1Bar",
    ]
)
MESSAGE_REJECTED: Final = frozenset(
    [
        "name",
        "user_id",
        "userID",
        "user_id2",
        "field1",
        "field_1",
        "_x",
        "x_",
        "a__b",
        "url",
        "fooBar",
        "foo_Bar",
        "a",
        "x1y",
        "user_2_id",
        "Http_Server",
        "FOO_BAR",
        "FOO__BAR",
        "FOO_1",
        "foo1Bar",
    ]
)
ENUM_VALUE_REJECTED: Final = frozenset(
    [
        "name",
        "Name",
        "user_id",
        "userID",
        "user_id2",
        "field1",
        "field_1",
        "_x",
        "x_",
        "a__b",
        "url",
        "fooBar",
        "foo_Bar",
        "a",
        "x1y",
        "user_2_id",
        "HTTPServer",
        "HttpServer",
        "Http_Server",
        "FOO__BAR",
        "Foo1",
        "foo1Bar",
    ]
)


@pytest.mark.unit
@pytest.mark.parametrize("name", NAMES)
def test_case_predicates_match_buf_on_every_probe_name(name: str) -> None:
    assert is_lower_snake_case(name) is (name not in FIELD_REJECTED)
    assert is_pascal_case(name) is (name not in MESSAGE_REJECTED)
    assert is_upper_snake_case(name) is (name not in ENUM_VALUE_REJECTED)


# --- runtime evidence ---------------------------------------------------------------------------


@pytest.mark.unit
def test_runtime_effects_show_why_wire_json_and_wire_differ() -> None:
    effects = measure_runtime_effects()
    assert effects.renamed_field_read_from_old_bytes == "rex"
    assert effects.renamed_field_json_parse_error is True
    assert effects.int32_json == '{"age": 5}'
    assert effects.int64_json == '{"age": "5"}'
    assert effects.int64_to_int32_truncated == 5
    assert effects.sint32_read_as_int32 == 1
