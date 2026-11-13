"""YAML parser hazards for OpenAPI documents: YAML 1.1 vs 1.2, alias expansion, key coercion.

PyYAML resolves plain scalars with the YAML 1.1 type repository and ruamel.yaml defaults to
YAML 1.2, so the same OpenAPI text loads as two different documents.  `load_openapi_yaml` is
the gate: it reads PyYAML's event stream (no composer, no constructor, no recursion) and
accepts only the JSON-compatible subset that both versions read identically (RFC 9512
Section 3.4).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, cast

import ruamel.yaml
import yaml as _pyyaml  # type: ignore[import-untyped]
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from starlette.requests import Request

# Neither library is fully annotated, so both are used through explicit Any handles.
yaml: Any = _pyyaml
_ruamel: Any = ruamel.yaml

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type YamlMediaType = Literal["registered", "suffix", "deprecated-alias"]

_TAG_PREFIX: Final = "tag:yaml.org,2002:"
_DEPRECATED_ALIASES: Final = frozenset({"application/x-yaml", "text/yaml", "text/x-yaml"})
_LITERAL_KEYWORDS: Final = ("const", "default", "example")
_LITERAL_LIST_KEYWORDS: Final = ("enum", "examples")

# YAML 1.2.2 Section 10.3.2 (core schema), first match wins.
_CORE_NULL: Final = re.compile(r"null|Null|NULL|~|")
_CORE_BOOL: Final = re.compile(r"true|True|TRUE|false|False|FALSE")
_CORE_INT: Final = re.compile(r"[-+]?[0-9]+")
_CORE_OCT: Final = re.compile(r"0o[0-7]+")
_CORE_HEX: Final = re.compile(r"0x[0-9a-fA-F]+")
_CORE_FLOAT: Final = re.compile(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?")
_CORE_INF: Final = re.compile(r"[-+]?(\.inf|\.Inf|\.INF)")
_CORE_NAN: Final = re.compile(r"\.nan|\.NaN|\.NAN")


class YamlHazardError(ValueError):
    """A YAML construct that is unsafe, not JSON-compatible, or YAML-version dependent."""

    def __init__(self, code: str, pointer: str, detail: str) -> None:
        super().__init__(f"{code} at {pointer!r}: {detail}")
        self.code = code
        self.pointer = pointer
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Limits:
    """Resource bounds enforced before and during loading."""

    max_bytes: int = 1_048_576
    max_depth: int = 64
    max_nodes: int = 100_000


@dataclass(frozen=True, slots=True)
class Divergence:
    """One place where the YAML 1.1 and YAML 1.2 loads of the same text differ."""

    kind: Literal["key", "value"]
    pointer: str
    yaml_1_1: str
    yaml_1_2: str


@dataclass(frozen=True, slots=True)
class ExpansionReport:
    """Size of a document as the graph a loader builds and as the tree a serializer walks."""

    stream_bytes: int
    graph_nodes: int
    tree_nodes: int | None
    cyclic: bool


@dataclass(slots=True)
class _Frame:
    container: dict[str, JsonValue] | list[JsonValue]
    pointer: str
    anchor: str | None
    nodes_before: int
    key: str | None = None
    height: int = 1


def load_yaml_1_1(text: str) -> object:
    """Load with PyYAML's safe loader, which resolves plain scalars by YAML 1.1 rules."""
    return yaml.safe_load(text)


def load_yaml_1_2(text: str) -> object:
    """Load with ruamel.yaml's pure-Python safe loader, which defaults to YAML 1.2."""
    return _ruamel.YAML(typ="safe", pure=True).load(text)


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _mapping(value: object) -> dict[object, object] | None:
    return cast("dict[object, object]", value) if isinstance(value, dict) else None


def _sequence(value: object) -> list[object] | None:
    return cast("list[object]", value) if isinstance(value, list) else None


def resolution_divergences(text: str) -> tuple[Divergence, ...]:
    """Return every key and value that the YAML 1.1 and YAML 1.2 loaders read differently."""
    found: list[Divergence] = []
    stack: list[tuple[str, object, object]] = [("", load_yaml_1_1(text), load_yaml_1_2(text))]
    while stack:
        pointer, old, new = stack.pop()
        old_map, new_map = _mapping(old), _mapping(new)
        old_seq, new_seq = _sequence(old), _sequence(new)
        if old_map is not None and new_map is not None and len(old_map) == len(new_map):
            for (old_key, old_value), (new_key, new_value) in zip(
                old_map.items(), new_map.items(), strict=True
            ):
                if repr(old_key) != repr(new_key):
                    found.append(Divergence("key", pointer, repr(old_key), repr(new_key)))
                stack.append((f"{pointer}/{_escape(str(new_key))}", old_value, new_value))
        elif old_seq is not None and new_seq is not None and len(old_seq) == len(new_seq):
            for index, (old_item, new_item) in enumerate(zip(old_seq, new_seq, strict=True)):
                stack.append((f"{pointer}/{index}", old_item, new_item))
        elif repr(old) != repr(new):
            found.append(Divergence("value", pointer, repr(old), repr(new)))
    return tuple(sorted(found, key=lambda item: (item.pointer, item.kind)))


def resolve_core_schema(text: str) -> bool | int | float | str | None:
    """Resolve one plain scalar by the YAML 1.2.2 core schema (Section 10.3.2)."""
    if _CORE_NULL.fullmatch(text):
        return None
    if _CORE_BOOL.fullmatch(text):
        return text.lower() == "true"
    if _CORE_INT.fullmatch(text):
        return int(text)
    if _CORE_OCT.fullmatch(text):
        return int(text[2:], 8)
    if _CORE_HEX.fullmatch(text):
        return int(text[2:], 16)
    if _CORE_FLOAT.fullmatch(text):
        return float(text)
    if _CORE_INF.fullmatch(text):
        return float("-inf") if text.startswith("-") else float("inf")
    if _CORE_NAN.fullmatch(text):
        return float("nan")
    return text


def resolve_yaml_1_1(text: str) -> object:
    """Resolve one plain scalar exactly as PyYAML does: its resolver table, then its constructor."""
    tag = f"{_TAG_PREFIX}str"
    table = yaml.SafeLoader.yaml_implicit_resolvers
    for candidate, pattern in table.get(text[:1], []) + table.get(None, []):
        if pattern.match(text):
            tag = candidate
            break
    return yaml.constructor.SafeConstructor().construct_object(yaml.ScalarNode(tag, text))


def measure_expansion(text: str) -> ExpansionReport:
    """Count the nodes of the composed graph and of the tree an alias-blind walker would visit.

    The graph comes from PyYAML's recursive composer, so input nested past the interpreter's
    recursion limit raises RecursionError here; `load_openapi_yaml` has no such limit.
    """
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if root is None:
        return ExpansionReport(len(text.encode("utf-8")), 0, 0, cyclic=False)
    sizes: dict[int, int] = {}
    open_nodes: set[int] = set()
    cyclic = False
    stack: list[tuple[Any, bool]] = [(root, False)]
    while stack:
        node, children_done = stack.pop()
        ident = id(node)
        children: list[Any] = []
        if isinstance(node, yaml.SequenceNode):
            children = list(node.value)
        elif isinstance(node, yaml.MappingNode):
            children = [child for pair in node.value for child in pair]
        if children_done:
            open_nodes.discard(ident)
            sizes[ident] = 1 + sum(sizes.get(id(child), 0) for child in children)
        elif ident in open_nodes:
            cyclic = True
        elif ident not in sizes:
            open_nodes.add(ident)
            stack.append((node, True))
            stack.extend((child, False) for child in children)
    return ExpansionReport(
        stream_bytes=len(text.encode("utf-8")),
        graph_nodes=len(sizes),
        tree_nodes=None if cyclic else sizes[id(root)],
        cyclic=cyclic,
    )


def _yaml_1_1_repr(text: str) -> str:
    try:
        return repr(resolve_yaml_1_1(text))
    except yaml.YAMLError:
        return "not constructible"


def _resolve_scalar(event: Any, pointer: str) -> JsonValue:
    """Turn one scalar event into a JSON value, or raise for anything outside the subset."""
    text: str = event.value
    tag: str | None = event.tag
    if tag == f"{_TAG_PREFIX}str" or (tag is None and event.style is not None):
        return text
    try:
        value = resolve_core_schema(text)
        current, legacy = repr(value), _yaml_1_1_repr(text)
    except ValueError as error:
        raise YamlHazardError("unloadable-number", pointer, str(error)) from error
    resolved = "null" if value is None else type(value).__name__
    if tag is not None and tag != f"{_TAG_PREFIX}{resolved}":
        raise YamlHazardError("tag", pointer, f"{text!r} tagged {tag!r} is outside the JSON schema")
    if legacy != current:
        raise YamlHazardError(
            "version-dependent-scalar",
            pointer,
            f"plain scalar {text!r} is {legacy} in YAML 1.1 and {current} in YAML 1.2",
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise YamlHazardError("non-json-float", pointer, f"{text!r} has no JSON representation")
    return value


def load_openapi_yaml(text: str, limits: Limits | None = None) -> JsonValue:
    """Load one YAML document as JSON-compatible data, rejecting every hazard with a pointer."""
    bounds = limits if limits is not None else Limits()
    if len(text.encode("utf-8", "surrogatepass")) > bounds.max_bytes:
        raise YamlHazardError("too-large", "", f"stream exceeds {bounds.max_bytes} bytes")
    frames: list[_Frame] = []
    anchors: dict[str, tuple[JsonValue, int, int]] = {}
    open_anchors: set[str] = set()
    documents = 0
    nodes = 0
    result: JsonValue = None

    def here() -> str:
        if not frames:
            return ""
        frame = frames[-1]
        if isinstance(frame.container, list):
            return f"{frame.pointer}/{len(frame.container)}"
        return frame.pointer if frame.key is None else f"{frame.pointer}/{_escape(frame.key)}"

    def at_key() -> bool:
        return bool(frames) and isinstance(frames[-1].container, dict) and frames[-1].key is None

    def bind(anchor: str | None, value: JsonValue, size: int, height: int) -> None:
        if anchor is not None:
            open_anchors.discard(anchor)
            anchors[anchor] = (value, size, height)

    def count(added: int) -> None:
        nonlocal nodes
        nodes += added
        if nodes > bounds.max_nodes:
            raise YamlHazardError(
                "node-budget", here(), f"document expands past {bounds.max_nodes} nodes"
            )

    def place(value: JsonValue, height: int) -> None:
        nonlocal result
        if not frames:
            result = value
            return
        frame = frames[-1]
        frame.height = max(frame.height, height + 1)
        if isinstance(frame.container, list):
            frame.container.append(value)
        elif frame.key is not None:
            frame.container[frame.key] = value
            frame.key = None
        elif not isinstance(value, str):
            raise YamlHazardError(
                "non-string-key", frame.pointer, f"mapping key {value!r} is not a string"
            )
        elif value in frame.container:
            raise YamlHazardError("duplicate-key", frame.pointer, f"key {value!r} appears twice")
        else:
            frame.key = value

    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise YamlHazardError("multi-document", "", "stream holds a second document")
                if event.version not in (None, (1, 2)):
                    raise YamlHazardError(
                        "yaml-version", "", f"%YAML directive {event.version} is not 1.2"
                    )
            elif isinstance(event, yaml.AliasEvent):
                name: str = event.anchor
                if name in open_anchors:
                    raise YamlHazardError("cyclic-alias", here(), f"alias *{name} is its own child")
                if name not in anchors:
                    raise YamlHazardError("undefined-alias", here(), f"alias *{name} has no anchor")
                target, size, height = anchors[name]
                if len(frames) + height > bounds.max_depth:
                    raise YamlHazardError(
                        "too-deep", here(), f"alias *{name} nests past {bounds.max_depth} levels"
                    )
                count(size)
                place(target, height)
            elif isinstance(event, yaml.NodeEvent) and (
                event.anchor in anchors or event.anchor in open_anchors
            ):
                raise YamlHazardError(
                    "duplicate-anchor", here(), f"anchor &{event.anchor} is defined twice"
                )
            elif isinstance(event, yaml.ScalarEvent):
                if at_key() and event.style is None and event.tag is None and event.value == "<<":
                    raise YamlHazardError(
                        "merge-key",
                        here(),
                        "'<<' merges in YAML 1.1 and is a plain key in YAML 1.2",
                    )
                count(1)
                value = _resolve_scalar(event, here())
                bind(event.anchor, value, 1, 0)
                place(value, 0)
            elif isinstance(event, yaml.CollectionStartEvent):
                pointer = here()
                tag: str | None = event.tag
                kind = "seq" if isinstance(event, yaml.SequenceStartEvent) else "map"
                if tag is not None and tag != f"{_TAG_PREFIX}{kind}":
                    raise YamlHazardError("tag", pointer, f"tag {tag!r} is outside the JSON schema")
                if len(frames) >= bounds.max_depth:
                    raise YamlHazardError(
                        "too-deep", pointer, f"nesting exceeds {bounds.max_depth} levels"
                    )
                if event.anchor is not None:
                    open_anchors.add(event.anchor)
                container: dict[str, JsonValue] | list[JsonValue] = [] if kind == "seq" else {}
                frames.append(_Frame(container, pointer, event.anchor, nodes))
                count(1)
            elif isinstance(event, yaml.CollectionEndEvent):
                frame = frames.pop()
                bind(frame.anchor, frame.container, nodes - frame.nodes_before, frame.height)
                place(frame.container, frame.height)
    except yaml.YAMLError as error:
        raise YamlHazardError("syntax", here(), str(error).splitlines()[0]) from error
    if documents == 0:
        raise YamlHazardError("empty", "", "stream holds no document")
    return result


def find_mistyped_string_literals(document: object) -> tuple[str, ...]:
    """Return pointers to literals (enum, const, default, example, examples) under a string type.

    A schema counts as a string schema when `type` is `string` or a list of `string` and `null`.
    """
    found: list[str] = []
    stack: list[tuple[str, object]] = [("", document)]
    while stack:
        pointer, node = stack.pop()
        stack.extend(
            (f"{pointer}/{index}", item) for index, item in enumerate(_sequence(node) or [])
        )
        schema = _mapping(node)
        if schema is None:
            continue
        stack.extend((f"{pointer}/{_escape(str(key))}", value) for key, value in schema.items())
        declared = schema.get("type")
        names = _sequence(declared) or [declared]
        if "string" not in names or any(name not in ("string", "null") for name in names):
            continue
        allowed = (str, type(None)) if "null" in names else (str,)
        literals = [
            (f"{keyword}/{index}", item)
            for keyword in _LITERAL_LIST_KEYWORDS
            for index, item in enumerate(_sequence(schema.get(keyword)) or [])
        ]
        literals += [(name, schema[name]) for name in _LITERAL_KEYWORDS if name in schema]
        found.extend(
            f"{pointer}/{name}" for name, value in literals if not isinstance(value, allowed)
        )
    return tuple(sorted(found))


def classify_yaml_media_type(content_type: str) -> YamlMediaType | None:
    """Classify a Content-Type value per RFC 9512 Section 2; parameters are ignored."""
    essence = content_type.split(";", 1)[0].strip().lower()
    if essence == "application/yaml":
        return "registered"
    if essence in _DEPRECATED_ALIASES:
        return "deprecated-alias"
    type_name, _, subtype = essence.partition("/")
    if type_name and "/" not in subtype and subtype.endswith("+yaml") and subtype != "+yaml":
        return "suffix"
    return None


def _problem(status: int, title: str, **extra: str) -> JSONResponse:
    body = {"type": "about:blank", "title": title, "status": status, **extra}
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def create_app(limits: Limits | None = None) -> Starlette:
    """Build an ASGI app whose POST /openapi/normalize turns hazard-free YAML into JSON."""
    bounds = limits if limits is not None else Limits()

    async def normalize(request: Request) -> JSONResponse:
        media_type = classify_yaml_media_type(request.headers.get("content-type", ""))
        if media_type is None:
            return _problem(415, "Unsupported Media Type")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > bounds.max_bytes:
                return _problem(413, "Content Too Large")
        try:
            document = load_openapi_yaml(body.decode("utf-8"), bounds)
        except UnicodeDecodeError:
            return _problem(400, "Bad Request", detail="body is not UTF-8")
        except YamlHazardError as error:
            malformed = error.code == "syntax"
            return _problem(
                400 if malformed else 422,
                "Bad Request" if malformed else "Unprocessable Content",
                code=error.code,
                pointer=error.pointer,
                detail=error.detail,
            )
        return JSONResponse({"mediaType": media_type, "document": document})

    return Starlette(routes=[Route("/openapi/normalize", normalize, methods=["POST"])])
