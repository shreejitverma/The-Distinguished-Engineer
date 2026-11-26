"""AsyncAPI 3.1.0 document semantics: references, traits, structure, expressions, payloads.

The checks follow the normative text of the 3.1.0 specification (spec/asyncapi.md at tag v3.1.0).
The Schema Object is a superset of JSON Schema Draft 07, not of 2020-12, so payloads are
validated with Draft7Validator and every `$ref` is rewritten to point into the whole document.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import unquote

from jsonschema import (  # type: ignore[import-untyped]
    Draft7Validator,
    Draft202012Validator,
)
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

from api_labs.f20_graphql_json_rpc_and_event_apis.asyncapi_bindings import (
    BINDING_PROTOCOLS,
    Finding,
    validate_binding,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

type JsonObject = dict[str, Any]

SPEC_VERSION: Final = "3.1.0"
DEFAULT_SCHEMA_FORMAT: Final = f"application/vnd.aai.asyncapi+json;version={SPEC_VERSION}"
AAI_SCHEMA_FORMATS: Final = frozenset(
    {
        f"application/vnd.aai.asyncapi;version={SPEC_VERSION}",
        f"application/vnd.aai.asyncapi+json;version={SPEC_VERSION}",
        f"application/vnd.aai.asyncapi+yaml;version={SPEC_VERSION}",
    }
)
DRAFT7_SCHEMA_FORMATS: Final = frozenset(
    {"application/schema+json;version=draft-07", "application/schema+yaml;version=draft-07"}
)
OPERATION_TRAIT_FORBIDDEN: Final = frozenset({"action", "channel", "messages", "traits"})
MESSAGE_TRAIT_FORBIDDEN: Final = frozenset({"payload", "traits"})
DOCUMENT_URI: Final = "urn:asyncapi:document"

_VERSION = re.compile(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?")
_EXPRESSION = re.compile(r"\$message\.(?P<source>header|payload)(?:#(?P<fragment>.*))?", re.DOTALL)
_ADDRESS_PARAMETER = re.compile(r"\{([^{}]*)\}")
_BAD_TILDE = re.compile(r"~(?![01])")
_MAX_REF_HOPS: Final = 32


class AsyncApiError(Exception):
    """Base class for every error the lab raises on purpose."""


class RefError(AsyncApiError):
    """A JSON Pointer or `$ref` that cannot be resolved."""


class TraitError(AsyncApiError):
    """A trait that carries a field the specification forbids in traits."""


class ExpressionError(AsyncApiError):
    """A string that is not a valid runtime expression."""


class AddressError(AsyncApiError):
    """A channel address that cannot be rendered."""


class UnsupportedSchemaFormat(AsyncApiError):
    """A `schemaFormat` this validator does not implement (support is OPTIONAL beyond Draft 07)."""


class UnsupportedContentType(AsyncApiError):
    """A message `contentType` this lab cannot decode."""


class MessageMatchError(AsyncApiError):
    """Zero or several messages match, violating 'valid against one, and only one'."""


# --- RFC 6901 JSON Pointer and `$ref` resolution -------------------------------------------


def parse_pointer(pointer: str) -> list[str]:
    """Split an RFC 6901 pointer into unescaped tokens ("~1" is decoded before "~0")."""
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise RefError(f"a JSON Pointer is empty or starts with '/': {pointer!r}")
    tokens: list[str] = []
    for raw in pointer[1:].split("/"):
        if _BAD_TILDE.search(raw):
            raise RefError(f"'~' must be followed by 0 or 1 in {raw!r}")
        tokens.append(raw.replace("~1", "/").replace("~0", "~"))
    return tokens


def escape_token(token: str) -> str:
    """Escape one token: "~" first, then "/" (RFC 6901 Section 3)."""
    return token.replace("~", "~0").replace("/", "~1")


def resolve_pointer(root: Any, pointer: str) -> Any:
    """Walk `pointer` through nested dicts and lists."""
    node: Any = root
    for part in parse_pointer(pointer):
        if isinstance(node, dict) and part in node:
            node = cast("JsonObject", node)[part]
        elif isinstance(node, list) and part.isdecimal() and (part == "0" or part[0] != "0"):
            items: list[Any] = node  # pyright: ignore[reportUnknownVariableType]
            if int(part) >= len(items):
                raise RefError(f"index {part} is out of range at {pointer!r}")
            node = items[int(part)]
        else:
            raise RefError(f"{pointer!r} does not resolve: no {part!r}")
    return node


def ref_pointer(ref: str) -> str:
    """Return the pointer of an internal reference; external references are not supported."""
    if not ref.startswith("#"):
        raise RefError(f"only internal references are supported, got {ref!r}")
    return unquote(ref[1:])


def is_reference(node: object) -> bool:
    """A Reference Object is any object whose `$ref` is a string."""
    return isinstance(node, dict) and isinstance(node.get("$ref"), str)  # pyright: ignore[reportUnknownMemberType]


def canonical_pointer(document: JsonObject, pointer: str) -> str:
    """Follow `$ref` hops from `pointer` until the node is not a Reference Object."""
    for _ in range(_MAX_REF_HOPS):
        node = resolve_pointer(document, pointer)
        if not is_reference(node):
            return pointer
        pointer = ref_pointer(node["$ref"])
    raise RefError(f"reference cycle or chain longer than {_MAX_REF_HOPS} at {pointer!r}")


def deref(document: JsonObject, node: Any) -> Any:
    """Resolve a Reference Object to its target; any other node is returned unchanged."""
    if not is_reference(node):
        return node
    return resolve_pointer(document, canonical_pointer(document, ref_pointer(node["$ref"])))


# --- RFC 7386 JSON Merge Patch and the traits merge mechanism --------------------------------


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 Section 2 MergePatch: objects merge, `null` deletes, anything else replaces."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result: JsonObject = (
        copy.deepcopy(cast("JsonObject", target)) if isinstance(target, dict) else {}
    )
    for name, value in cast("JsonObject", patch).items():
        if value is None:
            result.pop(name, None)
        else:
            result[name] = merge_patch(result.get(name), value)
    return result


def apply_traits(document: JsonObject, node: JsonObject, forbidden: frozenset[str]) -> JsonObject:
    """Merge `node["traits"]` in order, then the node itself last so its own fields win.

    This is the order the reference parser uses (asyncapi/parser-js apply-traits.ts,
    applyTraitsToObjectV3).
    The result drops the `traits` key, so applying it twice changes nothing.
    """
    merged: JsonObject = {}
    for raw in node.get("traits", []):
        trait = deref(document, raw)
        bad = sorted(forbidden & trait.keys())
        if bad:
            raise TraitError(f"a trait MUST NOT contain {bad}")
        merged = merge_patch(merged, trait)
    own = {key: value for key, value in node.items() if key != "traits"}
    result: JsonObject = merge_patch(merged, own)
    return result


# --- Runtime expressions --------------------------------------------------------------------


def parse_runtime_expression(expression: str) -> tuple[str, str | None]:
    """Parse `$message.(header|payload)["#" fragment]`; the fragment must be a JSON Pointer."""
    match = _EXPRESSION.fullmatch(expression)
    if match is None:
        raise ExpressionError(f"not a runtime expression: {expression!r}")
    fragment = match.group("fragment")
    if fragment is not None:
        try:
            parse_pointer(fragment)
        except RefError as error:
            raise ExpressionError(str(error)) from error
    return match.group("source"), fragment


def evaluate_runtime_expression(expression: str, *, headers: Any, payload: Any) -> Any:
    """Evaluate against a message; the value keeps its JSON type (an int stays an int)."""
    source, fragment = parse_runtime_expression(expression)
    root = headers if source == "header" else payload
    return root if fragment is None else resolve_pointer(root, fragment)


# --- Structure validation -------------------------------------------------------------------


def address_parameters(address: str | None) -> set[str]:
    """Names of the `{expression}` parameters in a channel address."""
    return set(_ADDRESS_PARAMETER.findall(address or ""))


def _ref_tokens(node: Any) -> list[str] | None:
    if not is_reference(node):
        return None
    try:
        return parse_pointer(ref_pointer(node["$ref"]))
    except RefError:
        return None


def _check_expression(findings: list[Finding], expression: object, path: str) -> None:
    try:
        parse_runtime_expression(str(expression))
    except ExpressionError as error:
        findings.append(Finding("expression.invalid", path, str(error)))


def _check_bindings(
    findings: list[Finding], owner: Mapping[str, Any], level: Any, path: str, registry: bool
) -> None:
    raw: Any = owner.get("bindings")
    if not isinstance(raw, dict):
        return
    bindings = cast("JsonObject", raw)
    if is_reference(bindings):
        return
    for protocol, binding in bindings.items():
        findings.extend(
            validate_binding(
                protocol, level, binding, f"{path}/bindings/{protocol}", registry=registry
            )
        )


def _check_channel(
    findings: list[Finding], document: JsonObject, channel_id: str, *, registry: bool
) -> None:
    path = f"/channels/{escape_token(channel_id)}"
    channel = deref(document, document["channels"][channel_id])
    declared = set(channel.get("parameters", {}))
    used = address_parameters(channel.get("address"))
    for name in sorted(used - declared):
        findings.append(Finding("channel.parameter.missing", path, f"{{{name}}} has no parameter"))
    for name in sorted(declared - used):
        findings.append(
            Finding("channel.parameter.unused", path, f"parameter {name!r} is not in the address")
        )
    for index, server in enumerate(channel.get("servers", [])):
        tokens = _ref_tokens(server)
        if tokens is None or len(tokens) != 2 or tokens[0] != "servers":
            findings.append(
                Finding(
                    "channel.servers.not-root",
                    f"{path}/servers/{index}",
                    "MUST point into #/servers",
                )
            )
        elif tokens[1] not in document.get("servers", {}):
            findings.append(
                Finding("channel.servers.dangling", f"{path}/servers/{index}", tokens[1])
            )
    _check_bindings(findings, channel, "channel", path, registry)
    for message_id, raw in channel.get("messages", {}).items():
        message = deref(document, raw)
        message_path = f"{path}/messages/{escape_token(message_id)}"
        if "correlationId" in message:
            correlation = deref(document, message["correlationId"])
            _check_expression(
                findings, correlation.get("location"), f"{message_path}/correlationId"
            )
        for index, trait in enumerate(message.get("traits", [])):
            bad = sorted(MESSAGE_TRAIT_FORBIDDEN & deref(document, trait).keys())
            if bad:
                findings.append(
                    Finding("message.trait.forbidden", f"{message_path}/traits/{index}", str(bad))
                )
        _check_bindings(findings, message, "message", message_path, registry)


def _check_operation(findings: list[Finding], document: JsonObject, operation_id: str) -> None:
    path = f"/operations/{escape_token(operation_id)}"
    operation = deref(document, document["operations"][operation_id])
    if operation.get("action") not in {"send", "receive"}:
        findings.append(
            Finding("operation.action", f"{path}/action", "action MUST be send or receive")
        )
    tokens = _ref_tokens(operation.get("channel"))
    channel_id: str | None = None
    if tokens is None or len(tokens) != 2 or tokens[0] != "channels":
        findings.append(
            Finding(
                "operation.channel.not-root", f"{path}/channel", "MUST be a $ref to #/channels/<id>"
            )
        )
    elif tokens[1] not in document.get("channels", {}):
        findings.append(Finding("operation.channel.dangling", f"{path}/channel", tokens[1]))
    else:
        channel_id = tokens[1]
    if channel_id is not None and "messages" in operation:
        allowed = set(deref(document, document["channels"][channel_id]).get("messages", {}))
        for index, ref in enumerate(operation["messages"]):
            parts = _ref_tokens(ref)
            ok = parts is not None and len(parts) == 4 and parts[:2] == ["channels", channel_id]
            if not ok or parts is None or parts[2] != "messages" or parts[3] not in allowed:
                findings.append(
                    Finding(
                        "operation.messages.not-channel-subset",
                        f"{path}/messages/{index}",
                        f"MUST point into #/channels/{channel_id}/messages",
                    )
                )
    for index, trait in enumerate(operation.get("traits", [])):
        bad = sorted(OPERATION_TRAIT_FORBIDDEN & deref(document, trait).keys())
        if bad:
            findings.append(
                Finding("operation.trait.forbidden", f"{path}/traits/{index}", str(bad))
            )
    _check_bindings(findings, operation, "operation", path, registry=False)
    if "reply" in operation:
        _check_reply(findings, document, deref(document, operation["reply"]), f"{path}/reply")


def _check_reply(
    findings: list[Finding], document: JsonObject, reply: JsonObject, path: str
) -> None:
    address = deref(document, reply["address"]) if "address" in reply else None
    if address is not None:
        _check_expression(findings, address.get("location"), f"{path}/address/location")
    tokens = _ref_tokens(reply.get("channel"))
    if tokens is None or len(tokens) != 2 or tokens[0] != "channels":
        findings.append(
            Finding(
                "reply.channel.not-root", f"{path}/channel", "MUST be a $ref to #/channels/<id>"
            )
        )
        return
    channel = deref(document, document.get("channels", {}).get(tokens[1], {}))
    if address is not None and channel.get("address") is not None:
        findings.append(
            Finding(
                "reply.address.channel-address-set",
                f"{path}/channel",
                "when reply.address is set, the reply channel address MUST be null or absent",
            )
        )


def validate_document(document: JsonObject) -> tuple[Finding, ...]:
    """Check the structural rules of the 3.1.0 text that a JSON Schema alone cannot express."""
    findings: list[Finding] = []
    version = document.get("asyncapi")
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        findings.append(Finding("asyncapi.version", "/asyncapi", "MUST be major.minor.patch"))
    elif not version.startswith("3.1."):
        findings.append(Finding("asyncapi.version.minor", "/asyncapi", f"{version} is not 3.1.x"))
    info = document.get("info", {})
    findings.extend(
        Finding("info.required", f"/info/{name}", f"info.{name} is REQUIRED")
        for name in ("title", "version")
        if name not in info
    )
    registry = any(
        "schemaRegistryUrl" in deref(document, server).get("bindings", {}).get("kafka", {})
        for server in document.get("servers", {}).values()
    )
    for server_id, server in document.get("servers", {}).items():
        _check_bindings(
            findings, deref(document, server), "server", f"/servers/{server_id}", registry
        )
    for channel_id in document.get("channels", {}):
        _check_channel(findings, document, channel_id, registry=registry)
    for operation_id in document.get("operations", {}):
        _check_operation(findings, document, operation_id)
    return tuple(findings)


# --- Views, addresses, and replies ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OperationView:
    """What one operation means for the application that implements the document."""

    operation_id: str
    action: str
    channel_id: str
    address: str | None
    message_ids: tuple[str, ...]


def operation_views(document: JsonObject) -> tuple[OperationView, ...]:
    """One view per root operation; an omitted `messages` list means every channel message."""
    views: list[OperationView] = []
    for operation_id, raw in document.get("operations", {}).items():
        operation = deref(document, raw)
        channel_id = parse_pointer(ref_pointer(operation["channel"]["$ref"]))[1]
        channel = deref(document, document["channels"][channel_id])
        if "messages" in operation:
            message_ids = tuple(
                parse_pointer(ref_pointer(ref["$ref"]))[3] for ref in operation["messages"]
            )
        else:
            message_ids = tuple(channel.get("messages", {}))
        views.append(
            OperationView(
                operation_id, operation["action"], channel_id, channel.get("address"), message_ids
            )
        )
    return tuple(views)


def render_address(document: JsonObject, channel_id: str, values: Mapping[str, str]) -> str:
    """Substitute channel address expressions, enforcing each parameter's `enum` and `default`."""
    channel = deref(document, document["channels"][channel_id])
    address = channel.get("address")
    if address is None:
        raise AddressError(f"channel {channel_id!r} has no address: it is unknown until runtime")
    parameters = channel.get("parameters", {})

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        parameter = deref(document, parameters.get(name, {}))
        value: Any = values.get(name, parameter.get("default"))
        if value is None:
            raise AddressError(f"no value or default for {{{name}}}")
        if "enum" in parameter and value not in parameter["enum"]:
            raise AddressError(f"{value!r} is not in the enum of {{{name}}}")
        return str(value)

    return _ADDRESS_PARAMETER.sub(substitute, address)


def reply_address(document: JsonObject, operation_id: str, *, headers: Any, payload: Any) -> str:
    """Where the reply goes: `reply.address.location` evaluated on the request, else the channel."""
    operation = deref(document, document["operations"][operation_id])
    reply = deref(document, operation["reply"])
    if "address" in reply:
        location = deref(document, reply["address"])["location"]
        return str(evaluate_runtime_expression(location, headers=headers, payload=payload))
    return render_address(document, parse_pointer(ref_pointer(reply["channel"]["$ref"]))[1], {})


# --- Payload conformance --------------------------------------------------------------------


def _absolutize(schema: Any) -> Any:
    """Point every in-document `$ref` at the registered document so any schema can be compiled."""
    if isinstance(schema, list):
        return [_absolutize(item) for item in schema]  # pyright: ignore[reportUnknownVariableType]
    if not isinstance(schema, dict):
        return schema
    out: JsonObject = {}
    for key, value in schema.items():  # pyright: ignore[reportUnknownVariableType]
        if key == "$ref" and isinstance(value, str) and value.startswith("#"):
            out[key] = DOCUMENT_URI + value
        else:
            out[key] = _absolutize(value)
    return out


def compile_schema(document: JsonObject, schema: Any, *, schema_format: str | None = None) -> Any:
    """Build the validator for a schema according to its `schemaFormat`.

    The default format is `application/vnd.aai.asyncapi+json;version=3.1.0`,
    which is a JSON Schema Draft 07 superset.
    Any other format raises, because support beyond Draft 07 is OPTIONAL.
    """
    chosen = schema_format or DEFAULT_SCHEMA_FORMAT
    if chosen not in AAI_SCHEMA_FORMATS | DRAFT7_SCHEMA_FORMATS:
        raise UnsupportedSchemaFormat(chosen)
    resource: Any = Resource.from_contents(document, default_specification=DRAFT7)
    empty: Any = Registry()  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    registry: Any = empty.with_resource(DOCUMENT_URI, resource)
    validator: Any = Draft7Validator(_absolutize(schema), registry=registry)
    return validator


def schema_errors(document: JsonObject, payload_definition: Any, instance: Any) -> list[str]:
    """Validate `instance` against a Schema Object or a Multi Format Schema Object."""
    definition: Any = deref(document, payload_definition)
    schema_format: str | None = None
    if isinstance(definition, dict) and "schemaFormat" in definition and "schema" in definition:
        wrapper = cast("JsonObject", definition)
        schema_format = wrapper["schemaFormat"]
        definition = wrapper["schema"]
    validator = compile_schema(document, definition, schema_format=schema_format)
    return sorted(error.message for error in validator.iter_errors(instance))


def effective_message(document: JsonObject, channel_id: str, message_id: str) -> JsonObject:
    """The channel message with its traits merged in."""
    channel = deref(document, document["channels"][channel_id])
    message = deref(document, channel["messages"][message_id])
    return apply_traits(document, message, MESSAGE_TRAIT_FORBIDDEN)


def message_errors(
    document: JsonObject, channel_id: str, message_id: str, *, headers: Any, payload: Any
) -> list[str]:
    """Errors of `headers` and `payload` against one message definition."""
    message = effective_message(document, channel_id, message_id)
    errors = [
        f"payload: {e}" for e in schema_errors(document, message.get("payload", True), payload)
    ]
    if "headers" in message:
        errors += [f"headers: {e}" for e in schema_errors(document, message["headers"], headers)]
    return errors


def match_message(document: JsonObject, operation_id: str, *, headers: Any, payload: Any) -> str:
    """Return the one message id an incoming message conforms to, else raise.

    Zero matches and several matches both violate "valid against one, and only one".
    """
    view = next(v for v in operation_views(document) if v.operation_id == operation_id)
    matches = [
        message_id
        for message_id in view.message_ids
        if not message_errors(
            document, view.channel_id, message_id, headers=headers, payload=payload
        )
    ]
    if len(matches) != 1:
        raise MessageMatchError(f"{len(matches)} messages match: {matches}")
    return matches[0]


def content_type(document: JsonObject, message: JsonObject) -> str:
    """`contentType`, else the document `defaultContentType`, else the parser default."""
    return str(
        message.get("contentType") or document.get("defaultContentType") or "application/json"
    )


def decode_payload(document: JsonObject, message: JsonObject, raw: bytes) -> Any:
    """Decode bytes with the message content type; only JSON is implemented."""
    media = content_type(document, message)
    if media.split(";")[0].strip() != "application/json":
        raise UnsupportedContentType(media)
    return json.loads(raw)


def draft7_accepts_but_2020_12_rejects(schema: Any, instance: Any) -> bool:
    """True when the dialect gap hides an error: Draft 07 passes what 2020-12 would fail."""
    draft7: Any = Draft7Validator(schema)
    draft2020: Any = Draft202012Validator(schema)
    return bool(draft7.is_valid(instance)) and not draft2020.is_valid(instance)


def known_binding_protocols() -> frozenset[str]:
    """The 20 protocol keys of the 3.1.0 Bindings Objects."""
    return BINDING_PROTOCOLS
