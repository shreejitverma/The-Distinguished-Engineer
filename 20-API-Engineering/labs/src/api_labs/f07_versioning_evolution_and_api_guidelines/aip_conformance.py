"""Google AIP conformance and gRPC-HTTP transcoding lab.

Three mechanisms that make one protobuf contract serve both gRPC and HTTP/JSON:

1. ``google.api.http`` path templates (grammar from googleapis ``google/api/http.proto``),
   matched against a URL and bound to request fields, including the single-segment versus
   multi-segment percent-decoding rule.
2. Transcoding a REST request (path, query string, JSON body) into a real protobuf message
   with the installed protobuf runtime, and rendering the response with ProtoJSON.
3. A small AIP linter over method, message, resource, and service specs, using the api-linter
   v2.4.1 rule names for AIP-121, 123, 127, 131-136, 140, 142, 148, and 159.

protobuf 7.36.2 ships neither type stubs nor ``py.typed``, so the runtime is loaded once
through ``importlib`` and treated as ``Any`` at this single boundary.
"""

from __future__ import annotations

import importlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from urllib.parse import unquote

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_descriptor_pb2: Any = importlib.import_module("google.protobuf.descriptor_pb2")
_descriptor_pool: Any = importlib.import_module("google.protobuf.descriptor_pool")
_json_format: Any = importlib.import_module("google.protobuf.json_format")
_message_factory: Any = importlib.import_module("google.protobuf.message_factory")
_timestamp_pb2: Any = importlib.import_module("google.protobuf.timestamp_pb2")
_field_mask_pb2: Any = importlib.import_module("google.protobuf.field_mask_pb2")

HttpVerb = Literal["GET", "POST", "PATCH", "DELETE", "PUT"]

# ---------------------------------------------------------------------------
# 1. Path templates: Template = "/" Segments [ Verb ] (google/api/http.proto)
# ---------------------------------------------------------------------------

_IDENT: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LITERAL: Final = re.compile(r"[A-Za-z0-9._~-]+")


class TemplateSyntaxError(ValueError):
    """The path template does not follow the http.proto grammar."""


@dataclass(frozen=True, slots=True)
class Variable:
    """``{field.path=seg/seg}``; ``segments`` holds ``*``, ``**``, or literals."""

    field_path: str
    segments: tuple[str, ...]

    @property
    def multi_segment(self) -> bool:
        return self.segments != ("*",)


type Segment = str | Variable


@dataclass(frozen=True, slots=True)
class PathTemplate:
    segments: tuple[Segment, ...]
    verb: str | None

    def variables(self) -> tuple[Variable, ...]:
        return tuple(s for s in self.segments if isinstance(s, Variable))


def _split_top_level(text: str, sep: str) -> list[str]:
    parts: list[str] = []
    depth, start = 0, 0
    for i, ch in enumerate(text):
        if ch == "{":
            depth += 1
            if depth > 1:
                raise TemplateSyntaxError("a variable template must not contain other variables")
        elif ch == "}":
            depth -= 1
            if depth < 0:
                raise TemplateSyntaxError("unbalanced '}'")
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    if depth != 0:
        raise TemplateSyntaxError("unbalanced '{'")
    parts.append(text[start:])
    return parts


def _plain_segment(text: str) -> str:
    if text in ("*", "**") or _LITERAL.fullmatch(text):
        return text
    raise TemplateSyntaxError(f"invalid segment {text!r}")


def parse_template(text: str) -> PathTemplate:
    """Parse a ``google.api.http`` path template, rejecting what the grammar forbids."""
    if not text.startswith("/"):
        raise TemplateSyntaxError("template must start with '/'")
    body, verb = text[1:], None
    last = _split_top_level(body, "/")[-1]
    colon_parts = _split_top_level(last, ":")
    if len(colon_parts) > 2:
        raise TemplateSyntaxError("at most one ':verb' suffix")
    if len(colon_parts) == 2:
        verb = colon_parts[1]
        if not _LITERAL.fullmatch(verb):
            raise TemplateSyntaxError(f"invalid verb {verb!r}")
        body = body[: -(len(verb) + 1)]
    segments: list[Segment] = []
    for raw in _split_top_level(body, "/"):
        if raw.startswith("{") and raw.endswith("}"):
            field_path, _, sub = raw[1:-1].partition("=")
            if not all(_IDENT.fullmatch(p) for p in field_path.split(".")):
                raise TemplateSyntaxError(f"invalid field path {field_path!r}")
            subs = tuple(_plain_segment(s) for s in sub.split("/")) if sub else ("*",)
            segments.append(Variable(field_path, subs))
        else:
            segments.append(_plain_segment(raw))
    flat = [p for s in segments for p in (s.segments if isinstance(s, Variable) else (s,))]
    if "**" in flat[:-1]:
        raise TemplateSyntaxError("'**' must be the last segment (before the verb)")
    return PathTemplate(tuple(segments), verb)


def _decode(raw: str, *, keep_slash: bool) -> str:
    """http.proto: multi-segment variables decode everything except ``%2F`` / ``%2f``."""
    if not keep_slash:
        return unquote(raw)
    return "".join(
        piece if piece.lower() == "%2f" else unquote(piece) for piece in re.split(r"(%2[fF])", raw)
    )


def match_path(template: PathTemplate, path: str) -> dict[str, str] | None:
    """Return field-path bindings when ``path`` matches, else ``None``."""
    if not path.startswith("/"):
        return None
    raw = path[1:]
    if template.verb is not None:
        suffix = ":" + template.verb
        if not raw.endswith(suffix):
            return None
        raw = raw[: -len(suffix)]
    parts = raw.split("/")
    captured: dict[str, list[str]] = {}
    i = 0
    for seg in template.segments:
        patterns = seg.segments if isinstance(seg, Variable) else (seg,)
        taken: list[str] = []
        for pattern in patterns:
            if pattern == "**":
                taken.extend(parts[i:])
                i = len(parts)
                continue
            if i >= len(parts) or not parts[i] or (pattern != "*" and parts[i] != pattern):
                return None
            taken.append(parts[i])
            i += 1
        if isinstance(seg, Variable):
            captured[seg.field_path] = taken
    if i != len(parts):
        return None
    by_name = {v.field_path: v for v in template.variables()}
    return {
        name: _decode("/".join(segs), keep_slash=by_name[name].multi_segment)
        for name, segs in captured.items()
    }


# ---------------------------------------------------------------------------
# 2. Transcoding: path + query + body -> protobuf request; response -> ProtoJSON
# ---------------------------------------------------------------------------


class TranscodeError(ValueError):
    """The HTTP request cannot be mapped onto the RPC request message."""


@dataclass(frozen=True, slots=True)
class HttpRule:
    """One ``google.api.http`` binding; ``body`` is ``None``, ``"*"``, or a top-level field."""

    method: HttpVerb
    path: str
    body: str | None = None


def _field(descriptor: Any, name: str) -> Any:
    """Look a field up by proto name or json_name, as ProtoJSON parsers do."""
    by_name: Mapping[str, Any] = descriptor.fields_by_name
    if name in by_name:
        return by_name[name]
    for candidate in descriptor.fields:
        if candidate.json_name == name:
            return candidate
    raise TranscodeError(f"{descriptor.full_name} has no field {name!r}")


def _canonical_key(node: dict[str, Any], fd: Any, dotted: str) -> str:
    """Move a ``json_name`` key to the proto name so both spellings meet one conflict check."""
    name: str = fd.name
    alias: str = fd.json_name
    if alias != name and alias in node:
        if name in node:
            raise TranscodeError(f"{dotted!r}: body carries both {name!r} and {alias!r}")
        node[name] = node.pop(alias)
    return name


def _set_path(doc: dict[str, Any], descriptor: Any, dotted: str, value: object) -> None:
    """Set ``dotted`` (proto or JSON names) inside ``doc``; refuse conflicting values."""
    *parents, leaf = dotted.split(".")
    node, desc = doc, descriptor
    for part in parents:
        fd = _field(desc, part)
        if fd.message_type is None or fd.is_repeated:
            raise TranscodeError(f"{dotted!r}: {part!r} is not a singular message field")
        child = node.setdefault(_canonical_key(node, fd, dotted), {})
        if not isinstance(child, dict):
            raise TranscodeError(f"{dotted!r}: {part!r} is not an object")
        node, desc = cast("dict[str, Any]", child), fd.message_type
    key = _canonical_key(node, _field(desc, leaf), dotted)
    existing = node.get(key)
    if existing is not None and existing != value:
        raise TranscodeError(f"{dotted!r}: URL path says {value!r}, body says {existing!r}")
    node[key] = value


def _query_value(descriptor: Any, dotted: str, values: list[str]) -> object:
    desc: Any = descriptor
    *parents, leaf = dotted.split(".")
    for part in parents:
        desc = _field(desc, part).message_type
        if desc is None:
            raise TranscodeError(f"query parameter {dotted!r} walks through a scalar")
    fd = _field(desc, leaf)
    if fd.message_type is not None and fd.message_type.full_name not in _WKT_STRINGS:
        raise TranscodeError(f"query parameter {dotted!r} targets a message field")
    coerced = [_coerce_text(fd, v, dotted) for v in values]
    if fd.is_repeated:
        return coerced
    if len(coerced) != 1:
        raise TranscodeError(f"query parameter {dotted!r} repeated for a singular field")
    return coerced[0]


_WKT_STRINGS: Final = frozenset({"google.protobuf.Timestamp", "google.protobuf.FieldMask"})


def _coerce_text(fd: Any, text: str, dotted: str) -> object:
    """URL text -> ProtoJSON value; ProtoJSON rejects ``"true"`` for bool, so coerce it here."""
    if fd.type == _descriptor_pb2.FieldDescriptorProto.TYPE_BOOL:
        if text not in ("true", "false"):
            raise TranscodeError(f"query parameter {dotted!r}: expected true or false")
        return text == "true"
    return text


def transcode(
    rule: HttpRule,
    request_class: Any,
    method: str,
    path: str,
    query: Sequence[tuple[str, str]] = (),
    body: bytes = b"",
) -> Any:
    """Map one HTTP request onto ``request_class`` following the http.proto mapping rules."""
    template = parse_template(rule.path)
    bindings = match_path(template, path) if method == rule.method else None
    if bindings is None:
        raise TranscodeError(f"{method} {path} does not match {rule.method} {rule.path}")
    descriptor: Any = request_class.DESCRIPTOR
    doc: dict[str, Any] = {}
    if rule.body is None:
        if body:
            raise TranscodeError(f"{rule.method} {rule.path} defines no HTTP body")
    else:
        parsed: object = json.loads(body) if body else {}
        if rule.body == "*":
            if query:
                raise TranscodeError("body '*' leaves no fields for URL query parameters")
            if not isinstance(parsed, dict):
                raise TranscodeError("body '*' must be a JSON object")
            doc = cast("dict[str, Any]", parsed)
        else:
            doc[_field(descriptor, rule.body).name] = parsed
    grouped: dict[str, list[str]] = {}
    for key, value in query:
        grouped.setdefault(key, []).append(value)
    for key, values in grouped.items():
        _set_path(doc, descriptor, key, _query_value(descriptor, key, values))
    for field_path, value in bindings.items():
        _set_path(doc, descriptor, field_path, value)
    try:
        return _json_format.ParseDict(doc, request_class())
    except _json_format.ParseError as exc:
        raise TranscodeError(str(exc)) from exc


def to_json(message: Any) -> dict[str, object]:
    """Render a message with ProtoJSON (lowerCamelCase keys, int64 as strings)."""
    rendered: dict[str, object] = _json_format.MessageToDict(message)
    return rendered


# google/rpc/code.proto "HTTP Mapping" comments.
HTTP_STATUS_FOR_CODE: Final[Mapping[str, int]] = {
    "OK": 200,
    "CANCELLED": 499,
    "UNKNOWN": 500,
    "INVALID_ARGUMENT": 400,
    "DEADLINE_EXCEEDED": 504,
    "NOT_FOUND": 404,
    "ALREADY_EXISTS": 409,
    "PERMISSION_DENIED": 403,
    "UNAUTHENTICATED": 401,
    "RESOURCE_EXHAUSTED": 429,
    "FAILED_PRECONDITION": 400,
    "ABORTED": 409,
    "OUT_OF_RANGE": 400,
    "UNIMPLEMENTED": 501,
    "INTERNAL": 500,
    "UNAVAILABLE": 503,
    "DATA_LOSS": 500,
}


def error_envelope(code: str, message: str) -> tuple[int, dict[str, object]]:
    """AIP-193 HTTP/1.1+JSON error: ``code`` is the HTTP status, ``status`` the enum name."""
    status = HTTP_STATUS_FOR_CODE[code]
    return status, {"error": {"code": status, "message": message, "status": code}}


# ---------------------------------------------------------------------------
# 3. Contract model, real descriptors, and the AIP linter
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    type: str  # "string", "int64", "bool", a WKT full name, or a sibling message name
    repeated: bool = False
    optional: bool = False  # proto3 ``optional``: explicit presence (AIP-149)


@dataclass(frozen=True, slots=True)
class MessageSpec:
    name: str
    fields: tuple[FieldSpec, ...]
    resource: bool = False


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """A ``google.api.resource`` annotation: the global type and its name patterns (AIP-123)."""

    type: str  # "{Service Name}/{Type}", for example "library.googleapis.com/Book"
    patterns: tuple[str, ...]

    @property
    def has_parent(self) -> bool:
        """api-linter ``utils.HasParent``: some pattern has more than one ID variable."""
        return any(pattern.count("{") > 1 for pattern in self.patterns)

    @property
    def singleton(self) -> bool:
        """AIP-156: the name ends in a fixed segment, so there is no ID to list by."""
        return bool(self.patterns) and not self.patterns[0].endswith("}")


@dataclass(frozen=True, slots=True)
class MethodSpec:
    rpc: str
    rule: HttpRule
    resource_field: str | None = None  # request field holding the resource (Create/Update)
    resource: ResourceSpec | None = None  # the resource the method operates on


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str  # api-linter v2.4.1 rule name
    target: str
    message: str


_SCALARS: Final[Mapping[str, str]] = {
    "string": "TYPE_STRING",
    "int64": "TYPE_INT64",
    "int32": "TYPE_INT32",
    "bool": "TYPE_BOOL",
}
_WKT_FILES: Final[Mapping[str, Any]] = {
    "google.protobuf.Timestamp": _timestamp_pb2,
    "google.protobuf.FieldMask": _field_mask_pb2,
}


def build_messages(package: str, specs: Sequence[MessageSpec]) -> dict[str, Any]:
    """Compile specs into real protobuf message classes in a private descriptor pool."""
    proto = _descriptor_pb2.FieldDescriptorProto
    pool: Any = _descriptor_pool.DescriptorPool()
    file = _descriptor_pb2.FileDescriptorProto(
        name=f"{package.replace('.', '/')}/lab.proto", package=package, syntax="proto3"
    )
    for module in _WKT_FILES.values():
        pool.AddSerializedFile(module.DESCRIPTOR.serialized_pb)
        file.dependency.append(module.DESCRIPTOR.name)
    for spec in specs:
        message = file.message_type.add(name=spec.name)
        for number, fs in enumerate(spec.fields, start=1):
            label = proto.LABEL_REPEATED if fs.repeated else proto.LABEL_OPTIONAL
            if fs.type in _SCALARS:
                field = message.field.add(
                    name=fs.name, number=number, label=label, type=getattr(proto, _SCALARS[fs.type])
                )
            else:
                full = fs.type if fs.type in _WKT_FILES else f"{package}.{fs.type}"
                field = message.field.add(
                    name=fs.name,
                    number=number,
                    label=label,
                    type=proto.TYPE_MESSAGE,
                    type_name=f".{full}",
                )
            if fs.optional:  # proto3 optional is a one-member synthetic oneof
                field.proto3_optional = True
                field.oneof_index = len(message.oneof_decl)
                message.oneof_decl.add(name=f"_{fs.name}")
    pool.Add(file)
    return {
        spec.name: _message_factory.GetMessageClass(
            pool.FindMessageTypeByName(f"{package}.{spec.name}")
        )
        for spec in specs
    }


_METHOD_KIND: Final = (
    ("Get", re.compile(r"^Get(?:[A-Z]|$)")),
    ("List", re.compile(r"^List(?:[A-Z]|$)")),
    ("Create", re.compile(r"^Create(?:[A-Z]|$)")),
    ("Update", re.compile(r"^Update(?:[A-Z]|$)")),
    ("Delete", re.compile(r"^Delete(?:[A-Z]|$)")),
)
_STANDARD: Final[Mapping[str, tuple[int, HttpVerb, bool]]] = {
    # kind -> (AIP number, required HTTP verb, has a body)
    "Get": (131, "GET", False),
    "List": (132, "GET", False),
    "Create": (133, "POST", True),
    "Update": (134, "PATCH", True),
    "Delete": (135, "DELETE", False),
}


_PATTERN_VARIABLE: Final = re.compile(r"\{([^}=]+)}")
_TEMPLATE_WILDCARDS: Final[Mapping[str, str]] = {"**": r"(([^/]+/)*[^/]+)?", "*": r"[^/]+"}


def _words(identifier: str) -> list[str]:
    return re.findall(r"[A-Z][a-z0-9]*", identifier)


def _method_kind(rpc: str) -> str:
    return next((kind for kind, rx in _METHOD_KIND if rx.match(rpc)), "Custom")


def pattern_wildcards(pattern: str) -> str:
    """``publishers/{publisher}/books/{book}`` -> ``publishers/*/books/*``."""
    return _PATTERN_VARIABLE.sub("*", pattern)


def template_matches_resource(variable: Variable, resource: ResourceSpec) -> bool:
    """Whether a URI variable's template matches one of the resource's name patterns."""
    regex = "".join(
        _TEMPLATE_WILDCARDS.get(piece, re.escape(piece))
        for piece in re.split(r"(\*\*|\*)", "/".join(variable.segments))
    )
    return any(re.fullmatch(regex, pattern) for pattern in resource.patterns)


def lint_method(method: MethodSpec) -> list[Finding]:
    """AIP-127, AIP-131..136, and AIP-159 HTTP-binding checks for one RPC."""
    rpc, rule = method.rpc, method.rule
    try:
        template = parse_template(rule.path)
    except TemplateSyntaxError as exc:
        return [Finding("core::0127::http-template-syntax", rpc, str(exc))]
    kind = _method_kind(rpc)
    variables = {v.field_path: v for v in template.variables()}
    findings: list[Finding] = []
    plain = [
        p for s in template.segments for p in (s.segments if isinstance(s, Variable) else (s,))
    ]
    if "-" in plain[:-1]:
        findings.append(
            Finding("core::0159::hardcoded-hyphen", rpc, "URI hard-codes a '-' segment")
        )
    if method.resource is not None:
        findings.extend(
            Finding("core::0127::http-template-pattern", rpc, f"{{{path}}} matches no pattern")
            for path, variable in variables.items()
            if path.split(".")[-1] == "name"
            and not template_matches_resource(variable, method.resource)
        )
    if kind in _STANDARD:
        aip, verb, has_body = _STANDARD[kind]
        if rule.method != verb:
            findings.append(
                Finding(f"core::{aip:04d}::http-method", rpc, f"should use HTTP {verb}")
            )
        want_body = method.resource_field if has_body else None
        if rule.body != want_body:
            findings.append(
                Finding(f"core::{aip:04d}::http-body", rpc, f"body should be {want_body!r}")
            )
        if kind in ("List", "Create"):
            parented = method.resource is None or method.resource.has_parent
            if parented and (
                "parent" not in variables or (kind == "Create" and len(variables) != 1)
            ):
                findings.append(
                    Finding(
                        f"core::{aip:04d}::http-uri-parent", rpc, "URI should bind only {parent}"
                    )
                )
        else:
            want_var = f"{method.resource_field}.name" if kind == "Update" else "name"
            if want_var not in variables:
                findings.append(
                    Finding(
                        f"core::{aip:04d}::http-uri-name", rpc, f"URI should bind {{{want_var}}}"
                    )
                )
        return findings
    if rule.method not in ("GET", "POST"):
        findings.append(Finding("core::0136::http-method", rpc, "custom methods use GET or POST"))
    allowed_bodies = {None} if rule.method == "GET" else {"*", method.resource_field}
    if rule.body not in allowed_bodies:
        findings.append(Finding("core::0136::http-body", rpc, f"body {rule.body!r} not allowed"))
    words = _words(rpc)
    keys = set(variables)
    on_resource = bool(keys & {"name", "parent"}) or any(k.endswith(".name") for k in keys)
    last = template.segments[-1] if template.segments else ""
    on_collection = isinstance(last, str) and rpc.lower().endswith(last.lower())
    verb_noun = words[0].lower() + "".join(words[1:])
    want = words[0].lower() if on_resource or on_collection else verb_noun
    if template.verb != want:
        findings.append(Finding("core::0136::http-uri-suffix", rpc, f"URI should end :{want}"))
    return findings


_TIME_MISTAKES: Final = (
    ("created", "create_time"),
    ("creation", "create_time"),
    ("expired", "expire_time"),
    ("modified", "update_time"),
    ("updated", "update_time"),
    ("purged", "purge_time"),
)
_TIME_SUFFIXES: Final = frozenset(
    {
        "date", "datetime", "ms", "msec", "msecs", "millis", "nanos", "ns", "nsec", "nsecs",
        "sec", "secs", "seconds", "time", "timestamp", "us", "usec", "usecs",
    }
)  # fmt: skip
_TIME_TYPES: Final = frozenset(
    {
        "google.protobuf.Timestamp",
        "google.type.Date",
        "google.type.DateTime",
        "google.type.TimeOfDay",
    }
)
_HUMAN_NAMES: Final[Mapping[str, str]] = {"first_name": "given_name", "last_name": "family_name"}


def _to_lower_snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def lint_message(spec: MessageSpec) -> list[Finding]:
    """AIP-123, 140, 142, and 148 field checks for one message."""
    findings: list[Finding] = []
    for fs in spec.fields:
        target = f"{spec.name}.{fs.name}"
        if fs.name != _to_lower_snake(fs.name):
            findings.append(Finding("core::0140::lower-snake", target, "use lower_snake_case"))
        if any(word[:1].isdigit() for word in fs.name.split("_")):
            findings.append(Finding("core::0140::numbers", target, "word starts with a number"))
        if fs.name.startswith("_") or fs.name.endswith("_") or "__" in fs.name:
            findings.append(Finding("core::0140::underscores", target, "bad underscores"))
        if fs.type == "google.protobuf.Timestamp":
            mistake = next((want for got, want in _TIME_MISTAKES if got in fs.name), None)
            if mistake is not None:
                findings.append(Finding("core::0142::time-field-names", target, f"use {mistake}"))
            elif not fs.name.endswith("_time") and not (fs.repeated and fs.name.endswith("_times")):
                findings.append(Finding("core::0142::time-field-names", target, "end in _time"))
        if fs.name.split("_")[-1] in _TIME_SUFFIXES and fs.type not in _TIME_TYPES:
            findings.append(
                Finding("core::0142::time-field-type", target, "use google.protobuf.Timestamp")
            )
        if fs.name in _HUMAN_NAMES:
            findings.append(
                Finding("core::0148::human-names", target, f"use {_HUMAN_NAMES[fs.name]}")
            )
    if spec.resource:
        name = next((f for f in spec.fields if f.name == "name"), None)
        if name is None or name.type != "string" or name.repeated:
            findings.append(
                Finding("core::0123::resource-name-field", spec.name, "needs singular string name")
            )
    return findings


def lint_resource(resource: ResourceSpec) -> list[Finding]:
    """AIP-123 checks on the name patterns of one resource annotation."""
    if not resource.patterns:
        return [Finding("core::0123::resource-pattern", resource.type, "declare a name pattern")]
    findings: list[Finding] = []
    for pattern in resource.patterns:
        plain = pattern_wildcards(pattern)
        if "_" in plain or " " in plain:
            findings.append(
                Finding("core::0123::resource-pattern", resource.type, f"{pattern}: use camelCase")
            )
        findings.extend(
            Finding(
                "core::0123::resource-variables", resource.type, f"{{{variable}}}: snake, no _id"
            )
            for variable in _PATTERN_VARIABLE.findall(pattern)
            if variable != variable.lower() or variable.endswith("_id")
        )
    return findings


def lint_service(methods: Sequence[MethodSpec]) -> list[Finding]:
    """AIP-121: a resource with standard methods needs Get, and List unless it is a singleton."""
    kinds: dict[str, set[str]] = {}
    resources: dict[str, ResourceSpec] = {}
    for method in methods:
        if method.resource is not None:
            resources[method.resource.type] = method.resource
            kinds.setdefault(method.resource.type, set()).add(_method_kind(method.rpc))
    findings: list[Finding] = []
    for name, seen in kinds.items():
        if seen & {"Create", "Update", "List"} and "Get" not in seen:
            findings.append(
                Finding("core::0121::resource-must-support-get", name, "missing standard Get")
            )
        needs_list = seen & {"Create", "Update", "Get"} and not resources[name].singleton
        if needs_list and "List" not in seen:
            findings.append(
                Finding("core::0121::resource-must-support-list", name, "missing standard List")
            )
    return findings
