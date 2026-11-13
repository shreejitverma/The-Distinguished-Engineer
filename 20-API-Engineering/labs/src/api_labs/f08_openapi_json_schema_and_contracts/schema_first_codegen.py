"""Schema-first code generation: OpenAPI 3.1.2 -> Pydantic v2 (datamodel-code-generator 0.89.0).

The lab compiles one OpenAPI document under option profiles, imports the generated source
in-process, type-checks it with mypy --strict, and compares Pydantic verdicts with a JSON
Schema 2020-12 oracle. It is hermetic: remote `$ref` fetching is refused and only a temp
directory is written.
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from datamodel_code_generator import (
    Formatter,
    InputFileType,
    PythonVersion,
    ReadOnlyWriteOnlyModelType,
    TargetPydanticVersion,
    generate,
)
from datamodel_code_generator.enums import DataModelType, StrictTypes
from mypy import api as mypy_api
from pydantic import BaseModel, ValidationError

_MONEY: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["amount_minor", "currency"],
    "properties": {
        "amount_minor": {"type": "integer", "minimum": 0, "maximum": 9007199254740991},
        "currency": {"$ref": "#/components/schemas/Currency"},
    },
}
_LINE_ITEM: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sku", "quantity", "unit_price"],
    "properties": {
        "sku": {"type": "string", "pattern": "^[A-Z]{3}-[0-9]{4}$"},
        "quantity": {"type": "integer", "minimum": 1, "maximum": 1000},
        "unit_price": {"$ref": "#/components/schemas/Money"},
        "note": {"type": ["string", "null"], "maxLength": 280},
    },
}
_PAYMENT: Final[dict[str, Any]] = {
    "oneOf": [
        {"$ref": "#/components/schemas/CardPayment"},
        {"$ref": "#/components/schemas/WirePayment"},
    ],
    "discriminator": {
        "propertyName": "kind",
        "mapping": {
            "card": "#/components/schemas/CardPayment",
            "wire": "#/components/schemas/WirePayment",
        },
    },
}
_ORDER: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "created_at", "items", "payment"],
    "properties": {
        "id": {"type": "string", "format": "uuid", "readOnly": True},
        "created_at": {"type": "string", "format": "date-time", "readOnly": True},
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "items": {"$ref": "#/components/schemas/LineItem"},
        },
        "payment": {"$ref": "#/components/schemas/Payment"},
        "coupon": {"type": ["string", "null"], "minLength": 4, "maxLength": 16},
        "priority": {"type": "integer", "minimum": 0, "maximum": 9, "default": 5},
        "tags": {
            "type": "array",
            "uniqueItems": True,
            "maxItems": 8,
            "items": {"type": "string", "minLength": 1},
        },
    },
}
ORDERS_OPENAPI: Final[dict[str, Any]] = {
    "openapi": "3.1.2",
    "info": {"title": "Orders API", "version": "1.4.0"},
    "paths": {},
    "components": {
        "schemas": {
            "Currency": {"type": "string", "enum": ["USD", "EUR", "JPY"]},
            "Money": _MONEY,
            "LineItem": _LINE_ITEM,
            "CardPayment": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "last4"],
                "properties": {
                    "kind": {"const": "card"},
                    "last4": {"type": "string", "pattern": "^[0-9]{4}$"},
                },
            },
            "WirePayment": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "iban"],
                "properties": {
                    "kind": {"const": "wire"},
                    "iban": {"type": "string", "minLength": 15, "maxLength": 34},
                },
            },
            "Payment": _PAYMENT,
            "Order": _ORDER,
        }
    },
}

EDGE_OPENAPI: Final[dict[str, Any]] = {
    "openapi": "3.1.2",
    "info": {"title": "Edge cases", "version": "1.0.0"},
    "paths": {},
    "components": {
        "schemas": {
            "Account": {
                "type": "object",
                "required": ["user-id", "class", "code", "price"],
                "properties": {
                    "user-id": {"type": "string"},
                    "class": {"type": "string"},
                    "code": {"type": "string", "pattern": "[0-9]{4}"},
                    "password": {"type": "string", "pattern": "^(?=.*[A-Z]).{8,}$"},
                    "price": {"type": "number", "multipleOf": 0.01, "exclusiveMinimum": 0},
                    "ledger_id": {"type": "integer", "format": "int64"},
                },
            }
        }
    },
}


@dataclass(frozen=True, slots=True)
class Profile:
    """Generator switches; the defaults keep the generator's own type-shaping defaults."""

    strict: bool = False
    unique_items_as_set: bool = False
    collapse_root_models: bool = False
    schema_validators: bool = False
    request_response: bool = False
    descriptions: bool = False


LAX: Final = Profile()
STRICT: Final = Profile(strict=True)


def generate_models(spec: dict[str, Any] | str, profile: Profile) -> str:
    """Compile an OpenAPI document to Pydantic v2 source; every profile pins output stability."""
    source = spec if isinstance(spec, str) else json.dumps(spec)
    output = generate(
        source,
        input_file_type=InputFileType.OpenAPI,
        input_filename="orders.json",
        output_model_type=DataModelType.PydanticV2BaseModel,
        target_python_version=PythonVersion.PY_312,
        target_pydantic_version=TargetPydanticVersion.V2_14,
        formatters=[Formatter.BUILTIN],
        disable_timestamp=True,
        allow_remote_refs=False,
        use_annotated=profile.strict,
        strict_nullable=profile.strict,
        strict_types=list(StrictTypes) if profile.strict else None,
        use_unique_items_as_set=profile.unique_items_as_set,
        collapse_root_models=profile.collapse_root_models,
        generate_schema_validators=profile.schema_validators,
        read_only_write_only_model_type=(
            ReadOnlyWriteOnlyModelType.RequestResponse if profile.request_response else None
        ),
        use_schema_description=profile.descriptions,
        use_field_description=profile.descriptions,
    )
    if not isinstance(output, str):
        msg = f"expected one module of source, got {type(output).__name__}"
        raise TypeError(msg)
    return output


def load_models(source: str, module_name: str) -> ModuleType:
    """Execute generated source as a fresh module; Pydantic resolves annotations via sys.modules."""
    module = ModuleType(module_name)
    sys.modules[module_name] = module
    try:
        exec(compile(source, f"<{module_name}>", "exec"), module.__dict__)  # noqa: S102 - the lab's subject
    except BaseException:
        del sys.modules[module_name]
        raise
    return module


def model_class(module: ModuleType, name: str) -> type[BaseModel]:
    """Return a generated model class, failing loudly if the generator renamed or dropped it."""
    candidate: object = getattr(module, name)
    if not (isinstance(candidate, type) and issubclass(candidate, BaseModel)):
        msg = f"{name} is not a pydantic model in {module.__name__}"
        raise TypeError(msg)
    return candidate


def rejections(model: type[BaseModel], payload: object) -> list[tuple[str, str]]:
    """Validate JSON text as a server does; return (loc, error type) pairs, [] when accepted."""
    try:
        model.model_validate_json(json.dumps(payload))
    except ValidationError as exc:
        return [(".".join(str(part) for part in err["loc"]), err["type"]) for err in exc.errors()]
    return []


def json_schema_errors(
    spec: dict[str, Any], component: str, instance: object, *, check_formats: bool = True
) -> list[str]:
    """JSON Schema 2020-12 oracle (jsonschema 4.26.0); returns the failing keyword names, sorted."""
    # jsonschema 4.26.0 ships no py.typed marker, so it is bound as an explicit Any.
    jsonschema: Any = importlib.import_module("jsonschema")
    validator_type = jsonschema.Draft202012Validator
    schema = {"$ref": f"#/components/schemas/{component}", "components": spec["components"]}
    checker = validator_type.FORMAT_CHECKER if check_formats else None
    validator = validator_type(schema, format_checker=checker)
    return sorted(str(error.validator) for error in validator.iter_errors(instance))


def mypy_strict_errors(source: str) -> list[str]:
    """Run mypy --strict with the pydantic plugin on generated source; return its error codes."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        models = root / "models.py"
        models.write_text(source, encoding="utf-8")
        config = root / "mypy.ini"
        config.write_text("[mypy]\nstrict = True\nplugins = pydantic.mypy\n", encoding="utf-8")
        stdout, _stderr, _status = mypy_api.run(
            [
                "--config-file",
                str(config),
                "--cache-dir",
                str(root / ".mypy_cache"),
                "--python-version",
                "3.12",
                "--no-error-summary",
                "--show-error-codes",
                str(models),
            ]
        )
    errors = [line for line in stdout.splitlines() if ": error:" in line]
    return [line.rsplit("[", 1)[-1].rstrip("]") for line in errors]
