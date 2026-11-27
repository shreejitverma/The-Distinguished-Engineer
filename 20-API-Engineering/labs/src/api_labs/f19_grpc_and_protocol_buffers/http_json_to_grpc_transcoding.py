"""google.api.http transcoding: map an HTTP/JSON request onto a Protobuf request message.

A small, deterministic model of what Envoy's gRPC-JSON transcoder and the
grpc-httpjson-transcoding library do for one `google.api.HttpRule`:
parse the path template, match the request, extract path and query bindings,
weave them into the JSON body, and parse the result with the proto3 JSON mapping.
The response side maps gRPC status codes to HTTP statuses and renders `google.rpc.Status`.

The `google.api.http` option itself needs `googleapis-common-protos`, which this workspace
does not pin, so `HttpRule` is a plain dataclass with the same fields as `http.proto`.
Message types are built at runtime from `FileDescriptorProto` values, so no protoc is needed.
The protobuf runtime ships no type stubs, so it is imported through `importlib` and typed as `Any`.
"""

from __future__ import annotations

import base64
import enum
import importlib
import json
import re
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, NoReturn, cast

from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from starlette.requests import Request

_descriptor_pb2: Any = importlib.import_module("google.protobuf.descriptor_pb2")
_descriptor_pool: Any = importlib.import_module("google.protobuf.descriptor_pool")
_message_factory: Any = importlib.import_module("google.protobuf.message_factory")
_field_mask_pb2: Any = importlib.import_module("google.protobuf.field_mask_pb2")
_any_pb2: Any = importlib.import_module("google.protobuf.any_pb2")
_json_format: Any = importlib.import_module("google.protobuf.json_format")
_descriptor: Any = importlib.import_module("google.protobuf.descriptor")

type Message = Any
type Descriptor = Any
type FieldDescriptor = Any

TYPE_URL_PREFIX: Final = "type.googleapis.com/"
LIBRARY_PACKAGE: Final = "library.v1"
LIBRARY_SERVICE: Final = "library.v1.Library"
_STRING_WELL_KNOWN_TYPES: Final = frozenset(
    {"google.protobuf.FieldMask", "google.protobuf.Timestamp", "google.protobuf.Duration"}
)
_INT_RE: Final = re.compile(r"-?[0-9]+")


class GrpcCode(enum.IntEnum):
    """google.rpc.Code, the canonical error space (google/rpc/code.proto)."""

    OK = 0
    CANCELLED = 1
    UNKNOWN = 2
    INVALID_ARGUMENT = 3
    DEADLINE_EXCEEDED = 4
    NOT_FOUND = 5
    ALREADY_EXISTS = 6
    PERMISSION_DENIED = 7
    RESOURCE_EXHAUSTED = 8
    FAILED_PRECONDITION = 9
    ABORTED = 10
    OUT_OF_RANGE = 11
    UNIMPLEMENTED = 12
    INTERNAL = 13
    UNAVAILABLE = 14
    DATA_LOSS = 15
    UNAUTHENTICATED = 16


# The "HTTP Mapping" comment on each value of google.rpc.Code in google/rpc/code.proto.
HTTP_STATUS_BY_CODE: Final[dict[GrpcCode, int]] = {
    GrpcCode.OK: 200,
    GrpcCode.CANCELLED: 499,
    GrpcCode.UNKNOWN: 500,
    GrpcCode.INVALID_ARGUMENT: 400,
    GrpcCode.DEADLINE_EXCEEDED: 504,
    GrpcCode.NOT_FOUND: 404,
    GrpcCode.ALREADY_EXISTS: 409,
    GrpcCode.PERMISSION_DENIED: 403,
    GrpcCode.RESOURCE_EXHAUSTED: 429,
    GrpcCode.FAILED_PRECONDITION: 400,
    GrpcCode.ABORTED: 409,
    GrpcCode.OUT_OF_RANGE: 400,
    GrpcCode.UNIMPLEMENTED: 501,
    GrpcCode.INTERNAL: 500,
    GrpcCode.UNAVAILABLE: 503,
    GrpcCode.DATA_LOSS: 500,
    GrpcCode.UNAUTHENTICATED: 401,
}

# doc/http-grpc-status-mapping.md: only for clients that got no grpc-status; not the inverse.
GRPC_CODE_BY_HTTP_STATUS: Final[dict[int, GrpcCode]] = {
    400: GrpcCode.INTERNAL,
    401: GrpcCode.UNAUTHENTICATED,
    403: GrpcCode.PERMISSION_DENIED,
    404: GrpcCode.UNIMPLEMENTED,
    429: GrpcCode.UNAVAILABLE,
    502: GrpcCode.UNAVAILABLE,
    503: GrpcCode.UNAVAILABLE,
    504: GrpcCode.UNAVAILABLE,
}


def http_status_for(code: int) -> int:
    """HTTP status for a gRPC status code; a code outside 0..16 maps to 500."""
    try:
        return HTTP_STATUS_BY_CODE[GrpcCode(code)]
    except ValueError:
        return 500


def grpc_code_for_http(status: int) -> GrpcCode:
    """The client-side table for a response without `grpc-status`; every other status is UNKNOWN."""
    return GRPC_CODE_BY_HTTP_STATUS.get(status, GrpcCode.UNKNOWN)


class TranscodingError(Exception):
    """A request that cannot be transcoded; `code` is the gRPC status it maps to."""

    def __init__(self, code: GrpcCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def http_status(self) -> int:
        return http_status_for(self.code)


class TemplateError(ValueError):
    """A path template or HttpRule that violates the http.proto grammar or the AIP rules."""


def _invalid(message: str) -> NoReturn:
    raise TranscodingError(GrpcCode.INVALID_ARGUMENT, message)


# ---------------------------------------------------------------------------
# Percent decoding (grpc-httpjson-transcoding src/include/grpc_transcoding/percent_encoding.h)
# ---------------------------------------------------------------------------


class UnescapeSpec(enum.Enum):
    """Envoy `GrpcJsonTranscoder.UrlUnescapeSpec`: how multi-segment variables are decoded."""

    ALL_CHARACTERS_EXCEPT_RESERVED = 0
    ALL_CHARACTERS_EXCEPT_SLASH = 1
    ALL_CHARACTERS = 2


_RESERVED: Final = frozenset(b"!#$&'()*+,/:;=?@[]")  # RFC 6570 Section 1.5 reserved set
_HEX_DIGITS: Final = frozenset(b"0123456789abcdefABCDEF")
_SLASH: Final = 0x2F


def url_unescape(
    part: str,
    spec: UnescapeSpec = UnescapeSpec.ALL_CHARACTERS,
    *,
    plus_as_space: bool = False,
) -> str:
    """Decode `%XX` escapes, keeping the escapes that `spec` says must stay encoded."""
    src = part.encode("utf-8")
    out = bytearray()
    i = 0
    while i < len(src):
        byte = src[i]
        if plus_as_space and byte == 0x2B:
            out.append(0x20)
            i += 1
            continue
        if (
            byte == 0x25
            and i + 2 < len(src)
            and src[i + 1] in _HEX_DIGITS
            and src[i + 2] in _HEX_DIGITS
        ):
            decoded = int(src[i + 1 : i + 3], 16)
            keep = (
                spec is UnescapeSpec.ALL_CHARACTERS_EXCEPT_RESERVED and decoded in _RESERVED
            ) or (spec is UnescapeSpec.ALL_CHARACTERS_EXCEPT_SLASH and decoded == _SLASH)
            if not keep:
                out.append(decoded)
                i += 3
                continue
        out.append(byte)
        i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError as exc:
        _invalid(f"percent-decoded value is not valid UTF-8: {exc.reason}")


# ---------------------------------------------------------------------------
# Path templates (google/api/http.proto "Path template syntax")
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Variable:
    """A `{field.path=segments}` variable: a half-open range of template segments."""

    field_path: tuple[str, ...]
    start: int
    end: int
    has_double_star: bool


@dataclass(frozen=True, slots=True)
class PathTemplate:
    """A parsed template. `{var}` has already been rewritten to the segment `*`."""

    text: str
    segments: tuple[str, ...]
    verb: str
    variables: tuple[Variable, ...]


class _TemplateParser:
    """Recursive descent over `Template = "/" Segments [ Verb ]`, after http_template.cc."""

    def __init__(self, text: str, *, literals_after_double_star: bool) -> None:
        self.text = text
        self.pos = 0
        self.segments: list[str] = []
        self.variables: list[Variable] = []
        self.verb = ""
        self.in_variable = False
        self.literals_after_double_star = literals_after_double_star

    def fail(self, why: str) -> NoReturn:
        raise TemplateError(f"{self.text!r}: {why} at offset {self.pos}")

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def parse(self) -> PathTemplate:
        if self.peek() != "/":
            self.fail("template must start with '/'")
        self.pos += 1
        self.parse_segments()
        if self.peek() == ":":
            self.pos += 1
            self.verb = self.parse_literal()
        if self.pos != len(self.text):
            self.fail("unexpected input")
        self.validate_double_star()
        return PathTemplate(self.text, tuple(self.segments), self.verb, tuple(self.variables))

    def parse_segments(self) -> None:
        self.parse_segment()
        while self.peek() == "/":
            self.pos += 1
            self.parse_segment()

    def parse_segment(self) -> None:
        char = self.peek()
        if char == "*":
            self.pos += 1
            if self.peek() == "*":
                self.pos += 1
                self.segments.append("**")
            else:
                self.segments.append("*")
        elif char == "{":
            self.parse_variable()
        else:
            self.segments.append(self.parse_literal())

    def parse_variable(self) -> None:
        if self.in_variable:
            self.fail("a variable must not contain another variable")
        self.pos += 1
        self.in_variable = True
        start = len(self.segments)
        field_path = self.parse_field_path()
        if self.peek() == "=":
            self.pos += 1
            self.parse_segments()
        else:
            self.segments.append("*")
        if self.peek() != "}":
            self.fail("expected '}'")
        self.pos += 1
        self.in_variable = False
        end = len(self.segments)
        self.variables.append(Variable(field_path, start, end, "**" in self.segments[start:end]))

    def parse_field_path(self) -> tuple[str, ...]:
        names = [self.parse_identifier()]
        while self.peek() == ".":
            self.pos += 1
            names.append(self.parse_identifier())
        return tuple(names)

    def parse_identifier(self) -> str:
        begin = self.pos
        while self.peek() not in {"", ".", "}", "="}:
            self.pos += 1
        if self.pos == begin:
            self.fail("empty field name")
        return self.text[begin : self.pos]

    def parse_literal(self) -> str:
        begin = self.pos
        while self.peek() not in {"", "/", ":", "}"}:
            if self.peek() in {"*", "{"}:
                self.fail("a literal must not contain '*' or '{'")
            self.pos += 1
        if self.pos == begin:
            self.fail("empty literal")
        return self.text[begin : self.pos]

    def validate_double_star(self) -> None:
        if "**" not in self.segments:
            return
        index = self.segments.index("**")
        tail = self.segments[index + 1 :]
        if tail and not self.literals_after_double_star:
            self.fail("'**' must be the last segment before the verb")
        if any(segment in {"*", "**"} for segment in tail):
            self.fail("only literal segments may follow '**'")


def parse_template(text: str, *, literals_after_double_star: bool = False) -> PathTemplate:
    """Parse a path template.

    The http.proto grammar requires `**` to be last; grpc-httpjson-transcoding also accepts
    literal segments after it (`ValidateParts`), which `literals_after_double_star` enables.
    """
    if text == "/":
        return PathTemplate(text, (), "", ())
    return _TemplateParser(text, literals_after_double_star=literals_after_double_star).parse()


def split_request_path(
    path: str, custom_verbs: frozenset[str], *, match_unregistered_custom_verb: bool = False
) -> tuple[list[str], str]:
    """Split a raw request path into segments and a custom verb (`ExtractRequestParts`).

    The verb is only taken from the text after the last `:` when that `:` follows the last `/`
    and the verb is registered, so `/v1/books/a:b` stays one segment by default.
    Only trailing empty segments are dropped; an empty segment in the middle is kept.
    """
    path = path.split("?", 1)[0]
    verb = ""
    colon = path.rfind(":")
    if colon != -1 and colon > path.rfind("/"):
        candidate = path[colon + 1 :]
        if match_unregistered_custom_verb or candidate in custom_verbs:
            verb = candidate
            path = path[:colon]
    parts = path[1:].split("/") if path else []
    while parts and parts[-1] == "":
        parts.pop()
    return parts, verb


def match_segments(segments: Sequence[str], parts: Sequence[str]) -> list[tuple[int, int]] | None:
    """Match template segments against request parts; return the part range of each segment.

    `*` matches exactly one part (even an empty one, as in `PathMatcherNode::LookupPath`);
    `**` matches zero or more parts and absorbs everything between the head and the literal tail.
    """
    if "**" in segments:
        star = segments.index("**")
        head, tail = segments[:star], segments[star + 1 :]
        if len(parts) < len(head) + len(tail):
            return None
        pieces = [*head, "**", *tail]
        spans = [(i, i + 1) for i in range(len(head))]
        spans.append((len(head), len(parts) - len(tail)))
        spans.extend(
            (len(parts) - len(tail) + i, len(parts) - len(tail) + i + 1) for i in range(len(tail))
        )
        parts_for = [*parts[: len(head)], "", *parts[len(parts) - len(tail) :]]
    else:
        if len(parts) != len(segments):
            return None
        pieces = list(segments)
        spans = [(i, i + 1) for i in range(len(segments))]
        parts_for = list(parts)
    for piece, part in zip(pieces, parts_for, strict=True):
        if piece not in {"*", "**"} and piece != part:
            return None
    return spans


def _rank(segments: Sequence[str]) -> tuple[int, ...]:
    """Literal < `*` < `**`: the order in which `PathMatcherNode::LookupPath` tries children."""
    return tuple(2 if s == "**" else 1 if s == "*" else 0 for s in segments)


# ---------------------------------------------------------------------------
# HttpRule and the service model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HttpRule:
    """The fields of `google.api.HttpRule` that transcoding reads."""

    http_method: str
    template: str
    body: str = ""
    response_body: str = ""
    additional_bindings: tuple[HttpRule, ...] = ()


@dataclass(frozen=True, slots=True)
class RpcMethod:
    service: str
    name: str
    request_type: str
    response_type: str
    rule: HttpRule | None
    client_streaming: bool = False
    server_streaming: bool = False

    @property
    def grpc_path(self) -> str:
        return f"/{self.service}/{self.name}"


@dataclass(frozen=True, slots=True)
class Binding:
    """One value extracted from the URL: a dotted field path and its decoded text."""

    field_path: tuple[str, ...]
    value: str
    source: Literal["path", "query"]


@dataclass(frozen=True, slots=True)
class TranscoderOptions:
    """The subset of `GrpcJsonTranscoder` options this lab models."""

    url_unescape_spec: UnescapeSpec = UnescapeSpec.ALL_CHARACTERS_EXCEPT_RESERVED
    query_param_unescape_plus: bool = False
    match_unregistered_custom_verb: bool = False
    ignore_unknown_query_parameters: bool = False
    reject_binding_body_field_collisions: bool = False


@dataclass(frozen=True, slots=True)
class TranscodedRequest:
    method: RpcMethod
    rule: HttpRule
    message: Message
    bindings: tuple[Binding, ...]

    @property
    def wire(self) -> bytes:
        return bytes(self.message.SerializeToString(deterministic=True))

    @property
    def frame(self) -> bytes:
        return frame_grpc_message(self.wire)


def frame_grpc_message(payload: bytes) -> bytes:
    """The gRPC length-prefixed message: compressed flag 0, then a big-endian u32 length."""
    return b"\x00" + struct.pack(">I", len(payload)) + payload


# ---------------------------------------------------------------------------
# Descriptor helpers
# ---------------------------------------------------------------------------


def find_field(descriptor: Descriptor, name: str) -> FieldDescriptor | None:
    """Find a field by proto name or JSON (lowerCamelCase) name; the JSON spec accepts both."""
    found = descriptor.fields_by_name.get(name)
    if found is not None:
        return found
    for candidate in descriptor.fields:
        if candidate.json_name == name:
            return candidate
    return None


def resolve_field_path(descriptor: Descriptor, names: Sequence[str]) -> list[FieldDescriptor]:
    """Resolve `a.b.c` to field descriptors; every non-leaf must be a singular message."""
    resolved: list[FieldDescriptor] = []
    current = descriptor
    for index, name in enumerate(names):
        found = find_field(current, name)
        if found is None:
            _invalid(f'Could not find field "{name}" in the type "{current.full_name}".')
        resolved.append(found)
        if index < len(names) - 1:
            if found.cpp_type != _descriptor.FieldDescriptor.CPPTYPE_MESSAGE or found.is_repeated:
                _invalid(
                    f'Encountered a non-leaf field "{found.name}" that is not a singular message.'
                )
            current = found.message_type
    return resolved


def _leaf_json_value(leaf: FieldDescriptor, text: str, field_path: Sequence[str]) -> Any:
    """Turn the URL text for a leaf field into the JSON value the proto3 JSON parser expects."""
    cpp = _descriptor.FieldDescriptor
    dotted = ".".join(field_path)
    if leaf.cpp_type in {
        cpp.CPPTYPE_INT32,
        cpp.CPPTYPE_INT64,
        cpp.CPPTYPE_UINT32,
        cpp.CPPTYPE_UINT64,
    }:
        if not _INT_RE.fullmatch(text):
            _invalid(f"Failed to convert binding value {dotted}:{text} to an integer")
        return int(text)
    if leaf.cpp_type in {cpp.CPPTYPE_DOUBLE, cpp.CPPTYPE_FLOAT}:
        if text in {"NaN", "Infinity", "-Infinity"}:
            return text
        try:
            return float(text)
        except ValueError:
            _invalid(f"Failed to convert binding value {dotted}:{text} to a number")
    if leaf.cpp_type == cpp.CPPTYPE_BOOL:
        if text not in {"true", "false"}:
            _invalid(f"Failed to convert binding value {dotted}:{text} to a bool")
        return text == "true"
    if leaf.cpp_type == cpp.CPPTYPE_ENUM:
        if _INT_RE.fullmatch(text):
            return int(text)
        if text not in leaf.enum_type.values_by_name:
            _invalid(
                f"Failed to convert binding value {dotted}:{text} to enum {leaf.enum_type.name}"
            )
        return text
    if leaf.cpp_type == cpp.CPPTYPE_MESSAGE:
        if leaf.message_type.full_name not in _STRING_WELL_KNOWN_TYPES:
            _invalid(f'Field "{dotted}" is a message; bind one of its leaf fields instead.')
        return text
    return text


def _same_value(leaf: FieldDescriptor, existing: Any, new: Any) -> bool:
    cpp = _descriptor.FieldDescriptor
    try:
        if leaf.cpp_type in {
            cpp.CPPTYPE_INT32,
            cpp.CPPTYPE_INT64,
            cpp.CPPTYPE_UINT32,
            cpp.CPPTYPE_UINT64,
        }:
            return int(existing) == int(new)
        if leaf.cpp_type in {cpp.CPPTYPE_DOUBLE, cpp.CPPTYPE_FLOAT}:
            return float(existing) == float(new)
    except (TypeError, ValueError):
        return False
    return bool(existing == new)


def _json_key(obj: dict[str, Any], leaf: FieldDescriptor) -> str | None:
    for name in (leaf.json_name, leaf.name):
        key = str(name)
        if key in obj:
            return key
    return None


def weave_bindings(
    request: Descriptor,
    root: dict[str, Any],
    bindings: Iterable[Binding],
    *,
    reject_collisions: bool = False,
    ignore_unknown: bool = False,
    protected: set[tuple[int, str]] | None = None,
) -> None:
    """Merge URL bindings into the JSON object that came from the body (`RequestWeaver`).

    A scalar that the body already carries keeps the body value, as in Envoy by default;
    `reject_collisions` turns a differing value into INVALID_ARGUMENT.
    A repeated field gets the URL value appended after the body values.
    A scalar set by an earlier URL binding is overwritten by a later one (lab choice).
    """
    body_keys: set[tuple[int, str]] = protected if protected is not None else set()
    for binding in bindings:
        try:
            path = resolve_field_path(request, binding.field_path)
        except TranscodingError:
            if ignore_unknown:
                continue
            raise
        node = root
        for step in path[:-1]:
            key = _json_key(node, step) or step.json_name
            child: Any = node.setdefault(key, {})
            if not isinstance(child, dict):
                _invalid(
                    f'"{key}" in the body must be an object to bind {".".join(binding.field_path)}'
                )
            node = cast("dict[str, Any]", child)
        leaf = path[-1]
        value = _leaf_json_value(leaf, binding.value, binding.field_path)
        key = _json_key(node, leaf)
        if leaf.is_repeated:
            if key is None:
                node[leaf.json_name] = [value]
            elif isinstance(node[key], list):
                node[key].append(value)
            else:
                _invalid(f'"{key}" in the body must be an array')
        elif key is None:
            node[leaf.json_name] = value
        elif (id(node), key) in body_keys:
            if reject_collisions and not _same_value(leaf, node[key], value):
                _invalid(
                    f"The binding value {binding.value} of the field {leaf.name} "
                    f"is conflicting with the value {node[key]} in the body."
                )
        else:
            node[key] = value


def _mark_body_keys(node: Any, marked: set[tuple[int, str]]) -> None:
    """Record every (object, key) pair the body supplied, so bindings cannot overwrite it."""
    if isinstance(node, dict):
        fields = cast("dict[str, Any]", node)
        for key, value in fields.items():
            marked.add((id(fields), key))
            _mark_body_keys(value, marked)
    elif isinstance(node, list):
        items = cast("list[Any]", node)  # type: ignore[redundant-cast]  # pyright infers list[Unknown]
        for item in items:
            _mark_body_keys(item, marked)


# ---------------------------------------------------------------------------
# The transcoder
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Route:
    method: RpcMethod
    rule: HttpRule
    template: PathTemplate


type _RouteKey = tuple[str, tuple[str, ...]]


class Transcoder:
    """Register `RpcMethod` values, then turn HTTP requests into Protobuf request messages."""

    def __init__(self, pool: Any, options: TranscoderOptions | None = None) -> None:
        self.pool = pool
        self.options = options or TranscoderOptions()
        self._routes: dict[_RouteKey, _Route] = {}
        self._ambiguous: set[_RouteKey] = set()
        self._verbs: set[str] = set()
        self._classes: dict[str, Any] = {}

    def register(self, method: RpcMethod) -> None:
        if method.rule is None:
            return
        for rule in (method.rule, *method.rule.additional_bindings):
            if rule is not method.rule and rule.additional_bindings:
                raise TemplateError("additional_bindings must not nest")
            self._register_rule(method, rule)

    def _register_rule(self, method: RpcMethod, rule: HttpRule) -> None:
        template = parse_template(rule.template)
        request = self.pool.FindMessageTypeByName(method.request_type)
        for variable in template.variables:
            try:
                leaf = resolve_field_path(request, variable.field_path)[-1]
            except TranscodingError as exc:
                raise TemplateError(f"{rule.template}: {exc.message}") from exc
            if leaf.is_repeated or leaf.cpp_type == _descriptor.FieldDescriptor.CPPTYPE_MESSAGE:
                raise TemplateError(f"{rule.template}: path variable must be a singular scalar")
        if rule.body not in {"", "*"} and find_field(request, rule.body) is None:
            raise TemplateError(
                f"{rule.template}: body field {rule.body!r} is not in {method.request_type}"
            )
        key = (rule.http_method.upper() + template.verb, template.segments)
        if key in self._routes:
            self._ambiguous.add(key)
        self._routes[key] = _Route(method, rule, template)
        if template.verb:
            self._verbs.add(template.verb)
        if method.request_type not in self._classes:
            self._classes[method.request_type] = _message_factory.GetMessageClass(request)

    def lookup(self, http_method: str, path: str) -> tuple[_Route, list[str]] | None:
        """Find the route for a method and raw path; None if nothing matches or it is ambiguous."""
        parts, verb = split_request_path(
            path,
            frozenset(self._verbs),
            match_unregistered_custom_verb=self.options.match_unregistered_custom_verb,
        )
        best: tuple[tuple[int, ...], tuple[str, tuple[str, ...]], list[tuple[int, int]]] | None = (
            None
        )
        for key, route in self._routes.items():
            if key[0] != http_method.upper() + verb:
                continue
            spans = match_segments(route.template.segments, parts)
            if spans is None:
                continue
            candidate = (_rank(route.template.segments), key, spans)
            if best is None or candidate[0] < best[0]:
                best = candidate
        if best is None or best[1] in self._ambiguous:
            return None
        route = self._routes[best[1]]
        values = [
            "/".join(
                url_unescape(
                    part,
                    self.options.url_unescape_spec
                    if (variable.has_double_star or variable.end - variable.start > 1)
                    else UnescapeSpec.ALL_CHARACTERS,
                )
                for part in parts[best[2][variable.start][0] : best[2][variable.end - 1][1]]
            )
            for variable in route.template.variables
        ]
        return route, values

    def parse_query(self, query: str) -> list[Binding]:
        """`name=value` pairs; a pair without `=` or with an empty name is skipped silently."""
        bindings: list[Binding] = []
        for pair in query.split("&"):
            name, equals, value = pair.partition("=")
            if not equals or not name:
                continue
            bindings.append(
                Binding(
                    tuple(name.split(".")),
                    url_unescape(value, plus_as_space=self.options.query_param_unescape_plus),
                    "query",
                )
            )
        return bindings

    def transcode(self, http_method: str, target: str, body: bytes = b"") -> TranscodedRequest:
        """Transcode one HTTP request; raise `TranscodingError` with a gRPC code on failure."""
        path, _, query = target.partition("?")
        found = self.lookup(http_method, path)
        if found is None:
            raise TranscodingError(GrpcCode.NOT_FOUND, f"Could not resolve {path} to a method.")
        route, values = found
        rule = route.rule
        request = self.pool.FindMessageTypeByName(route.method.request_type)

        root: dict[str, Any] = {}
        if body.strip():
            if not rule.body:
                _invalid(f"{rule.http_method} {rule.template} takes no request body")
            try:
                parsed = json.loads(body)
            except ValueError as exc:
                _invalid(f"request body is not valid JSON: {exc}")
            if not isinstance(parsed, dict):
                _invalid("request body must be a JSON object")
            fields = cast("dict[str, Any]", parsed)
            root = fields if rule.body == "*" else {rule.body: fields}
        protected: set[tuple[int, str]] = set()
        _mark_body_keys(root, protected)

        bindings = [
            Binding(variable.field_path, value, "path")
            for variable, value in zip(route.template.variables, values, strict=True)
        ]
        bindings.extend(self.parse_query(query))
        weave_bindings(
            request,
            root,
            bindings,
            reject_collisions=self.options.reject_binding_body_field_collisions,
            ignore_unknown=self.options.ignore_unknown_query_parameters,
            protected=protected,
        )
        message = self._classes[route.method.request_type]()
        try:
            _json_format.ParseDict(root, message, ignore_unknown_fields=True)
        except (_json_format.ParseError, ValueError) as exc:
            _invalid(str(exc))
        return TranscodedRequest(route.method, rule, message, tuple(bindings))


# ---------------------------------------------------------------------------
# AIP-127 rule linting
# ---------------------------------------------------------------------------


def lint_rule(rule: HttpRule, request: Descriptor, *, bidi_streaming: bool = False) -> list[str]:
    """Report violations of the AIP-127 "must" and "should not" rules for one annotation."""
    problems: list[str] = []
    if bidi_streaming:
        return ["bidirectional streaming RPCs should not include a google.api.http annotation"]
    for index, candidate in enumerate((rule, *rule.additional_bindings)):
        where = "annotation" if index == 0 else f"additional_binding {index}"
        if index and candidate.additional_bindings:
            problems.append(f"{where}: additional bindings must not nest")
        if candidate.http_method.upper() in {"PUT", "*"}:
            problems.append(f"{where}: should not use {candidate.http_method.lower()}")
        if candidate.body and candidate.http_method.upper() in {"GET", "DELETE"}:
            problems.append(f"{where}: {candidate.http_method} must not define a body")
        if candidate.body != rule.body:
            problems.append(f"{where}: body must be identical to the top-level annotation")
        if "." in candidate.body:
            problems.append(f"{where}: body must not be a nested field")
        elif candidate.body not in {"", "*"}:
            body_field = find_field(request, candidate.body)
            if body_field is None:
                problems.append(f"{where}: body field {candidate.body!r} is not in the request")
            elif body_field.is_repeated:
                problems.append(f"{where}: body must not be a repeated field")
            for variable in parse_template(candidate.template).variables:
                if variable.field_path == (candidate.body,):
                    problems.append(f"{where}: body must not be the same as a URI parameter")
    return problems


# ---------------------------------------------------------------------------
# Response side: google.rpc.Status and response_body
# ---------------------------------------------------------------------------


def convert_grpc_status(
    pool: Any,
    grpc_status: int,
    grpc_message: str | None = None,
    details_bin: str | None = None,
    *,
    has_body: bool = False,
) -> tuple[int, str] | None:
    """Envoy `convert_grpc_status`: build the JSON error body from gRPC trailers.

    Returns None when there is nothing to convert (a body was already sent, or the status is
    OK or outside 0..16).
    Prefers `grpc-status-details-bin` (standard base64, padding optional), else builds a
    `google.rpc.Status` from `grpc-status` and the percent-decoded `grpc-message`.
    """
    if has_body or grpc_status == GrpcCode.OK or grpc_status not in {c.value for c in GrpcCode}:
        return None
    status_class = _message_factory.GetMessageClass(pool.FindMessageTypeByName("google.rpc.Status"))
    status = status_class()
    if details_bin:
        status.ParseFromString(base64.b64decode(details_bin + "=" * (-len(details_bin) % 4)))
    else:
        status.code = grpc_status
        if grpc_message:
            status.message = url_unescape(grpc_message)
    body = _json_format.MessageToDict(status, descriptor_pool=pool)
    return http_status_for(grpc_status), json.dumps(body, separators=(",", ":"))


def render_response(rule: HttpRule, message: Message) -> str:
    """Render a gRPC response as compact JSON; `response_body` selects one field as in http.proto.

    Envoy v1.40.2 refuses any `response_body` that is not a `google.api.HttpBody` field.
    """
    data = _json_format.MessageToDict(message)
    if rule.response_body:
        selected = find_field(message.DESCRIPTOR, rule.response_body)
        if selected is None:
            raise TemplateError(f"response_body {rule.response_body!r} is not in the response")
        fallback: Any = [] if selected.is_repeated else {}
        return json.dumps(data.get(selected.json_name, fallback), separators=(",", ":"))
    return json.dumps(data, separators=(",", ":"))


# ---------------------------------------------------------------------------
# AIP-134 update semantics (service layer; transcoding only fills the request)
# ---------------------------------------------------------------------------


def apply_update(stored: Message, request: Message) -> Message:
    """Apply an `UpdateBookRequest` to a stored `Book` as AIP-134 and field_mask.proto describe.

    Omitted mask: update every populated field except `name`.
    Present but empty mask: no-op.
    `*`: full replacement, handled here because the runtime does not recognize it.
    Other paths: `FieldMask.MergeMessage`, replacing repeated fields instead of appending.
    """
    result = type(stored)()
    result.CopyFrom(stored)
    if not request.HasField("update_mask"):
        mask = _field_mask_pb2.FieldMask(
            paths=[f.name for f, _ in request.book.ListFields() if f.name != "name"]
        )
    else:
        mask = request.update_mask
    if not mask.paths:
        return result
    if list(mask.paths) == ["*"]:
        result.CopyFrom(request.book)
        result.name = stored.name
        return result
    if not mask.IsValidForDescriptor(stored.DESCRIPTOR):
        _invalid(f"update_mask has paths that are not fields of Book: {list(mask.paths)}")
    mask.MergeMessage(
        request.book, result, replace_message_field=False, replace_repeated_field=True
    )
    return result


# ---------------------------------------------------------------------------
# The sample library.v1 contract, built without protoc
# ---------------------------------------------------------------------------


def _add_field(
    message: Any,
    name: str,
    number: int,
    kind: int,
    *,
    type_name: str = "",
    repeated: bool = False,
) -> None:
    proto = message.field.add(name=name, number=number, type=kind)
    proto.label = (
        _descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED
        if repeated
        else _descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    )
    if type_name:
        proto.type_name = type_name


def build_pool() -> Any:
    """A private descriptor pool holding library.v1, google.rpc.Status and RequestInfo."""
    fdp = _descriptor_pb2.FieldDescriptorProto
    pool = _descriptor_pool.DescriptorPool()
    for module in (_any_pb2, _field_mask_pb2):
        pool.AddSerializedFile(module.DESCRIPTOR.serialized_pb)

    rpc = _descriptor_pb2.FileDescriptorProto(
        name="google/rpc/status_lab.proto",
        package="google.rpc",
        syntax="proto3",
        dependency=["google/protobuf/any.proto"],
    )
    status = rpc.message_type.add(name="Status")
    _add_field(status, "code", 1, fdp.TYPE_INT32)
    _add_field(status, "message", 2, fdp.TYPE_STRING)
    _add_field(
        status, "details", 3, fdp.TYPE_MESSAGE, type_name=".google.protobuf.Any", repeated=True
    )
    request_info = rpc.message_type.add(name="RequestInfo")
    _add_field(request_info, "request_id", 1, fdp.TYPE_STRING)
    _add_field(request_info, "serving_data", 2, fdp.TYPE_STRING)
    pool.Add(rpc)

    lib = _descriptor_pb2.FileDescriptorProto(
        name="library/v1/library_lab.proto",
        package=LIBRARY_PACKAGE,
        syntax="proto3",
        dependency=["google/protobuf/field_mask.proto"],
    )
    genre = lib.enum_type.add(name="Genre")
    for number, value in enumerate(("GENRE_UNSPECIFIED", "FICTION", "SCIENCE")):
        genre.value.add(name=value, number=number)
    book = lib.message_type.add(name="Book")
    _add_field(book, "name", 1, fdp.TYPE_STRING)
    _add_field(book, "title", 2, fdp.TYPE_STRING)
    _add_field(book, "author", 3, fdp.TYPE_STRING)
    _add_field(book, "page_count", 4, fdp.TYPE_INT64)
    _add_field(book, "tags", 5, fdp.TYPE_STRING, repeated=True)
    _add_field(book, "genre", 6, fdp.TYPE_ENUM, type_name=".library.v1.Genre")
    get_req = lib.message_type.add(name="GetBookRequest")
    _add_field(get_req, "name", 1, fdp.TYPE_STRING)
    list_req = lib.message_type.add(name="ListBooksRequest")
    _add_field(list_req, "parent", 1, fdp.TYPE_STRING)
    _add_field(list_req, "page_size", 2, fdp.TYPE_INT32)
    _add_field(list_req, "page_token", 3, fdp.TYPE_STRING)
    _add_field(list_req, "filter_tags", 4, fdp.TYPE_STRING, repeated=True)
    _add_field(list_req, "filter_book", 5, fdp.TYPE_MESSAGE, type_name=".library.v1.Book")
    list_resp = lib.message_type.add(name="ListBooksResponse")
    _add_field(list_resp, "books", 1, fdp.TYPE_MESSAGE, type_name=".library.v1.Book", repeated=True)
    _add_field(list_resp, "next_page_token", 2, fdp.TYPE_STRING)
    create_req = lib.message_type.add(name="CreateBookRequest")
    _add_field(create_req, "parent", 1, fdp.TYPE_STRING)
    _add_field(create_req, "book", 2, fdp.TYPE_MESSAGE, type_name=".library.v1.Book")
    _add_field(create_req, "book_id", 3, fdp.TYPE_STRING)
    update_req = lib.message_type.add(name="UpdateBookRequest")
    _add_field(update_req, "book", 1, fdp.TYPE_MESSAGE, type_name=".library.v1.Book")
    _add_field(
        update_req, "update_mask", 2, fdp.TYPE_MESSAGE, type_name=".google.protobuf.FieldMask"
    )
    _add_field(update_req, "allow_missing", 3, fdp.TYPE_BOOL)
    archive_req = lib.message_type.add(name="ArchiveBookRequest")
    _add_field(archive_req, "name", 1, fdp.TYPE_STRING)
    _add_field(archive_req, "reason", 2, fdp.TYPE_STRING)
    delete_req = lib.message_type.add(name="DeleteBookRequest")
    _add_field(delete_req, "name", 1, fdp.TYPE_STRING)
    pool.Add(lib)
    return pool


def new_message(pool: Any, full_name: str) -> Message:
    """An empty message of a type in `pool`, such as `library.v1.Book`."""
    return _message_factory.GetMessageClass(pool.FindMessageTypeByName(full_name))()


def _method(name: str, request: str, response: str, rule: HttpRule) -> RpcMethod:
    return RpcMethod(
        LIBRARY_SERVICE, name, f"{LIBRARY_PACKAGE}.{request}", f"{LIBRARY_PACKAGE}.{response}", rule
    )


LIBRARY_METHODS: Final[tuple[RpcMethod, ...]] = (
    _method("GetBook", "GetBookRequest", "Book", HttpRule("GET", "/v1/{name=books/*}")),
    _method(
        "ListBooks",
        "ListBooksRequest",
        "ListBooksResponse",
        HttpRule("GET", "/v1/books", response_body="books"),
    ),
    _method(
        "CreateBook",
        "CreateBookRequest",
        "Book",
        HttpRule(
            "POST",
            "/v1/books",
            body="book",
            additional_bindings=(HttpRule("POST", "/v1/{parent=publishers/*}/books", body="book"),),
        ),
    ),
    _method(
        "UpdateBook",
        "UpdateBookRequest",
        "Book",
        HttpRule("PATCH", "/v1/{book.name=books/*}", body="book"),
    ),
    _method(
        "ArchiveBook",
        "ArchiveBookRequest",
        "Book",
        HttpRule("POST", "/v1/{name=books/*}:archive", body="*"),
    ),
    _method("DeleteBook", "DeleteBookRequest", "Book", HttpRule("DELETE", "/v1/{name=books/*}")),
)


def build_library_transcoder(options: TranscoderOptions | None = None) -> Transcoder:
    """A `Transcoder` for `LIBRARY_METHODS` over a fresh descriptor pool."""
    transcoder = Transcoder(build_pool(), options or TranscoderOptions())
    for method in LIBRARY_METHODS:
        transcoder.register(method)
    return transcoder


# ---------------------------------------------------------------------------
# An in-process gateway: the Envoy filter's job, with Python handlers as the "upstream"
# ---------------------------------------------------------------------------

type Handler = Callable[[Message], Message]
_ALL_METHODS: Final = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def build_gateway_app(transcoder: Transcoder, handlers: Mapping[str, Handler]) -> Starlette:
    """An ASGI app that transcodes each request, calls the handler for the matched RPC, and
    renders the response or the error as JSON with the HTTP status of the gRPC code.

    A handler signals a gRPC error by raising `TranscodingError`.
    """

    async def endpoint(request: Request) -> Response:
        target = request.scope["raw_path"].decode("ascii")
        query = request.scope["query_string"].decode("ascii")
        if query:
            target = f"{target}?{query}"
        try:
            result = transcoder.transcode(request.method, target, await request.body())
            handler = handlers.get(result.method.name)
            if handler is None:
                raise TranscodingError(
                    GrpcCode.UNIMPLEMENTED, f"{result.method.name} has no handler"
                )
            reply = handler(result.message)
        except TranscodingError as exc:
            payload = json.dumps({"code": int(exc.code), "message": exc.message})
            return Response(payload, exc.http_status, media_type="application/json")
        return Response(render_response(result.rule, reply), 200, media_type="application/json")

    return Starlette(routes=[Route("/{path:path}", endpoint, methods=_ALL_METHODS)])
