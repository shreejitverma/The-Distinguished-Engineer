"""Compare OpenAPI 3.0 Schema Objects with the OpenAPI 3.1 JSON Schema 2020-12 dialect."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import FastAPI
from jsonschema_path import SchemaPath
from openapi_schema_validator import OAS30Validator, OAS31Validator
from openapi_spec_validator import OpenAPIV30SpecValidator, OpenAPIV31SpecValidator
from openapi_spec_validator.schemas import get_validator_backend
from openapi_spec_validator.validation.keywords import OpenAPIV31SchemaValidator
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

type JsonObject = dict[str, Any]
type Tokens = tuple[str | int, ...]

OAS31_DIALECT = "https://spec.openapis.org/oas/3.1/dialect/base"
DRAFT4_DIALECT = "http://json-schema.org/draft-04/schema#"
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

_PATH_SEPARATOR = "\x00"
_SUBSCHEMA_MAPS = ("properties",)
_SUBSCHEMA_LISTS = ("allOf", "anyOf", "oneOf")
_SUBSCHEMA_SINGLE = ("items", "additionalProperties", "not")
_EXCLUSIVE_BOUNDS = (("minimum", "exclusiveMinimum"), ("maximum", "exclusiveMaximum"))
_SPEC_VALIDATORS = {"3.0": OpenAPIV30SpecValidator, "3.1": OpenAPIV31SpecValidator}


def oas30_document() -> JsonObject:
    """Return an OpenAPI 3.0.4 description that uses the 3.0-only Schema Object keywords."""
    subscription: JsonObject = {
        "type": "object",
        "required": ["id", "plan", "coupon", "seats"],
        "properties": {
            "id": {"type": "string", "example": "sub_1"},
            "plan": {"type": "string", "enum": ["pro"]},
            "coupon": {"type": "string", "nullable": True, "maxLength": 8},
            "seats": {"type": "integer", "minimum": 0, "exclusiveMinimum": True},
            "window": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 2,
            },
        },
    }
    return {
        "openapi": "3.0.4",
        "info": {"title": "Subscriptions", "version": "1.0.0"},
        "paths": {
            "/subscriptions/{subscription_id}": {
                "get": {
                    "operationId": "getSubscription",
                    "parameters": [
                        {
                            "name": "subscription_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "The subscription.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Subscription"}
                                }
                            },
                        }
                    },
                }
            }
        },
        "components": {"schemas": {"Subscription": subscription}},
    }


def oas31_document() -> JsonObject:
    """Return the same API as OpenAPI 3.1.2, using the constructs 3.0 cannot express."""
    subscription: JsonObject = {
        "type": "object",
        "required": ["id", "plan", "coupon", "seats"],
        "properties": {
            "id": {"type": "string", "examples": ["sub_1"]},
            "plan": {"const": "pro"},
            "coupon": {"type": ["string", "null"], "maxLength": 8},
            "seats": {"type": "integer", "exclusiveMinimum": 0},
            "window": {
                "type": "array",
                "prefixItems": [{"type": "string"}, {"type": "integer"}],
                "items": False,
            },
        },
    }
    event_body: JsonObject = {
        "required": True,
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Subscription"}}},
    }
    return {
        "openapi": "3.1.2",
        "info": {
            "title": "Subscriptions",
            "summary": "Subscription lifecycle API and its outbound events.",
            "version": "1.0.0",
            "license": {"name": "Apache 2.0", "identifier": "Apache-2.0"},
        },
        "jsonSchemaDialect": OAS31_DIALECT,
        "webhooks": {"subscription.renewed": {"$ref": "#/components/pathItems/SubscriptionEvent"}},
        "components": {
            "schemas": {"Subscription": subscription},
            "pathItems": {
                "SubscriptionEvent": {
                    "post": {
                        "operationId": "onSubscriptionRenewed",
                        "requestBody": event_body,
                        "responses": {"204": {"description": "Event accepted."}},
                    }
                }
            },
        },
    }


def migrate_schema_30_to_31(schema: JsonObject) -> JsonObject:
    """Rewrite one OpenAPI 3.0 Schema Object into the OpenAPI 3.1 dialect."""
    if "$ref" in schema:
        # A 3.0 Reference Object ignores its siblings; in 3.1 they would become live keywords.
        return {"$ref": schema["$ref"]}
    out: JsonObject = {}
    for key, value in schema.items():
        if key in ("nullable", "example"):
            continue
        if key in _SUBSCHEMA_MAPS:
            out[key] = {name: migrate_schema_30_to_31(sub) for name, sub in value.items()}
        elif key in _SUBSCHEMA_LISTS:
            out[key] = [migrate_schema_30_to_31(sub) for sub in value]
        elif key in _SUBSCHEMA_SINGLE and isinstance(value, dict):
            out[key] = migrate_schema_30_to_31(cast("JsonObject", value))
        else:
            out[key] = copy.deepcopy(value)
    for bound, exclusive in _EXCLUSIVE_BOUNDS:
        flag = out.get(exclusive)
        if isinstance(flag, bool):  # a number is already the 3.1 form and is left alone
            del out[exclusive]
            if flag and bound in out:
                out[exclusive] = out.pop(bound)
    if "example" in schema:
        out["examples"] = [copy.deepcopy(schema["example"])]
    if schema.get("type") == "string" and schema.get("format") == "byte":
        del out["format"]
        out["contentEncoding"] = "base64"
    elif schema.get("type") == "string" and schema.get("format") == "binary":
        # Raw binary is not a JSON instance: OAS 3.1.2 Section 4.4.2.1 drops `type` as well.
        del out["type"], out["format"]
        out["contentMediaType"] = "application/octet-stream"
    if schema.get("nullable") is True and "type" in out:
        out["type"] = [out["type"], "null"]
    return out


@dataclass(frozen=True, slots=True)
class SpecAudit:
    """Outcome of validating one OpenAPI document with openapi-spec-validator."""

    validator: str
    backend: str
    errors: tuple[str, ...]

    @property
    def valid(self) -> bool:
        """Report whether the validator yielded no error."""
        return not self.errors


def feature_set(document: JsonObject) -> str:
    """Return the `major.minor` part of the `openapi` field, which selects the feature set."""
    return ".".join(str(document.get("openapi", "")).split(".")[:2])


def audit(document: JsonObject) -> SpecAudit:
    """Validate a document with the validator class its `openapi` field selects."""
    validator_cls = _SPEC_VALIDATORS.get(feature_set(document))
    if validator_cls is None:
        raise ValueError(f"unsupported openapi version: {document.get('openapi')!r}")
    errors = tuple(
        str(error.message).splitlines()[0] for error in validator_cls(document).iter_errors()
    )
    return SpecAudit(
        validator=validator_cls.__name__, backend=get_validator_backend(), errors=errors
    )


def verdicts(
    family: Literal["3.0", "3.1"], schema: JsonObject | bool, instances: Sequence[object]
) -> tuple[bool, ...]:
    """Validate each instance against one Schema Object under the 3.0 or 3.1 keyword set."""
    validator = (OAS30Validator if family == "3.0" else OAS31Validator)(schema)
    return tuple(bool(validator.is_valid(instance)) for instance in instances)


def json_pointer(tokens: Tokens) -> str:
    """Encode reference tokens as a JSON Pointer."""
    return "".join("/" + str(token).replace("~", "~0").replace("/", "~1") for token in tokens)


def _is_reference(value: Any) -> bool:
    return not isinstance(value, dict) or "$ref" in value


def _content_schemas(tokens: Tokens, holder: JsonObject) -> Iterator[Tokens]:
    for media_type, media in holder.get("content", {}).items():
        base = (*tokens, "content", media_type)
        if "schema" in media:
            yield (*base, "schema")
        for name, encoding in media.get("encoding", {}).items():
            for header_name, header in encoding.get("headers", {}).items():
                header_tokens = (*base, "encoding", name, "headers", header_name)
                yield from _parameter_schemas(header_tokens, header)


def _parameter_schemas(tokens: Tokens, parameter: Any) -> Iterator[Tokens]:
    if _is_reference(parameter):
        return
    if "schema" in parameter:
        yield (*tokens, "schema")
    yield from _content_schemas(tokens, parameter)


def _response_schemas(tokens: Tokens, response: Any) -> Iterator[Tokens]:
    if _is_reference(response):
        return
    yield from _content_schemas(tokens, response)
    for name, header in response.get("headers", {}).items():
        yield from _parameter_schemas((*tokens, "headers", name), header)


def _callback_schemas(tokens: Tokens, callback: Any) -> Iterator[Tokens]:
    if _is_reference(callback):
        return
    for expression, item in callback.items():
        if not expression.startswith("x-"):
            yield from _path_item_schemas((*tokens, expression), item)


def _path_item_schemas(tokens: Tokens, path_item: Any) -> Iterator[Tokens]:
    if _is_reference(path_item):
        return
    for index, parameter in enumerate(path_item.get("parameters", [])):
        yield from _parameter_schemas((*tokens, "parameters", index), parameter)
    for method in HTTP_METHODS:
        operation = path_item.get(method)
        if operation is None:
            continue
        base = (*tokens, method)
        for index, parameter in enumerate(operation.get("parameters", [])):
            yield from _parameter_schemas((*base, "parameters", index), parameter)
        body = operation.get("requestBody")
        if not _is_reference(body):
            yield from _content_schemas((*base, "requestBody"), body)
        for status, response in operation.get("responses", {}).items():
            if not status.startswith("x-"):
                yield from _response_schemas((*base, "responses", status), response)
        for name, callback in operation.get("callbacks", {}).items():
            yield from _callback_schemas((*base, "callbacks", name), callback)


def schema_object_locations(document: JsonObject) -> tuple[Tokens, ...]:
    """List every location in an OpenAPI 3.1 document that holds a Schema Object."""
    found: list[Tokens] = []
    components: JsonObject = document.get("components", {})
    found.extend(("components", "schemas", name) for name in components.get("schemas", {}))
    for section in ("parameters", "headers"):
        for name, parameter in components.get(section, {}).items():
            found.extend(_parameter_schemas(("components", section, name), parameter))
    for name, body in components.get("requestBodies", {}).items():
        if not _is_reference(body):
            found.extend(_content_schemas(("components", "requestBodies", name), body))
    for name, response in components.get("responses", {}).items():
        found.extend(_response_schemas(("components", "responses", name), response))
    for name, callback in components.get("callbacks", {}).items():
        found.extend(_callback_schemas(("components", "callbacks", name), callback))
    for name, item in components.get("pathItems", {}).items():
        found.extend(_path_item_schemas(("components", "pathItems", name), item))
    for name, item in document.get("paths", {}).items():
        if not name.startswith("x-"):  # the Paths Object admits extensions, `webhooks` does not
            found.extend(_path_item_schemas(("paths", name), item))
    for name, item in document.get("webhooks", {}).items():
        found.extend(_path_item_schemas(("webhooks", name), item))
    return tuple(found)


def check_every_schema_object(document: JsonObject) -> tuple[str, ...]:
    """Run the validator's per-schema check on every Schema Object, not only the walked ones."""
    if feature_set(document) != "3.1":
        raise ValueError("the exhaustive walk implements the OpenAPI 3.1 object model only")
    validator = OpenAPIV31SpecValidator(document)
    problems: list[str] = []
    for tokens in schema_object_locations(document):
        # The `/` operator splits keys on "#", which callback expressions contain.
        path = SchemaPath(validator.schema_path.accessor, *tokens, separator=_PATH_SEPARATOR)
        # A fresh checker per location: the library dedupes schemas by `id()` per instance.
        check = OpenAPIV31SchemaValidator(validator.keyword_validators_registry)
        problems.extend(
            f"{json_pointer(tokens)}: {str(error.message).splitlines()[0]}" for error in check(path)
        )
    return tuple(problems)


def external_references(node: object) -> tuple[str, ...]:
    """Collect every `$ref` string that leaves the current document."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in cast("dict[str, object]", node).items():
            if key == "$ref" and isinstance(value, str):
                if not value.startswith("#"):
                    found.append(value)
            else:
                found.extend(external_references(value))
    elif isinstance(node, list):
        for value in cast("list[object]", node):
            found.extend(external_references(value))
    return tuple(found)


class Subscription(BaseModel):
    """Pydantic model whose generated JSON Schema needs the OpenAPI 3.1 dialect."""

    id: str
    plan: Literal["pro"]
    coupon: str | None
    seats: int = Field(gt=0)
    window: tuple[str, int]


def build_subscription_app() -> FastAPI:
    """Create an app with one path operation and one documented webhook."""
    app = FastAPI(title="Subscriptions", version="1.0.0")

    @app.get("/subscriptions/{subscription_id}")
    async def get_subscription(subscription_id: str) -> Subscription:
        return Subscription(
            id=subscription_id, plan="pro", coupon=None, seats=3, window=("2026-11", 30)
        )

    @app.webhooks.post("subscription.renewed", status_code=204)
    async def subscription_renewed(body: Subscription) -> None:
        """Document the request this API sends to the consumer."""

    del get_subscription, subscription_renewed
    return app
