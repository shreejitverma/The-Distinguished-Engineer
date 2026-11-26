"""CloudEvents 1.0.2 envelope: context attributes, JSON event format, HTTP content modes.

Implements, from the v1.0.2 specification files (spec.md, bindings/http-protocol-binding.md,
formats/json-format.md):

* the required, optional, and extension context attributes with their type rules;
* HTTP binary mode (`ce-` headers plus raw `data` as the body);
* HTTP structured mode (`application/cloudevents+json`) and batched mode;
* the percent-encoding rules for header values (binding section 3.1.3.2);
* the `data` versus `data_base64` rules of the JSON format (format section 3.1).

Lab policies that go beyond the specification text are marked "lab policy" in comments.
"""

import base64
import binascii
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None
type Data = Json | bytes
type Extension = str | int | bool

SPECVERSION: Final = "1.0"
STRUCTURED_MEDIA_TYPE: Final = "application/cloudevents+json"
BATCH_MEDIA_TYPE: Final = "application/cloudevents-batch+json"
REQUIRED_ATTRIBUTES: Final = ("id", "source", "specversion", "type")
OPTIONAL_ATTRIBUTES: Final = ("datacontenttype", "dataschema", "subject", "time")
INT_MIN: Final = -(2**31)
INT_MAX: Final = 2**31 - 1

_JSON_MEMBERS: Final = frozenset(
    {*REQUIRED_ATTRIBUTES, *OPTIONAL_ATTRIBUTES, "data", "data_base64"}
)
_ATTRIBUTE_NAME: Final = re.compile(r"[a-z0-9]+")
_URI_REFERENCE: Final = re.compile(r"(?:[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=]|%[0-9A-Fa-f]{2})+")
_ABSOLUTE_URI_START: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")
_RFC3339: Final = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})"
)
_PERCENT_TRIPLET: Final = re.compile(r"%([0-9A-Fa-f]{2})")


class CloudEventError(ValueError):
    """The value is not a valid CloudEvent (a spec MUST was violated)."""


class NotACloudEventError(CloudEventError):
    """An HTTP binary-mode message lacks the mandatory `ce-` headers."""


class UnsupportedEventFormatError(CloudEventError):
    """The CloudEvents media type names an event format this implementation cannot decode."""


class _Unset(Enum):
    UNSET = "unset"


UNSET: Final = _Unset.UNSET


class Mode(Enum):
    BINARY = "binary"
    STRUCTURED = "structured"
    BATCHED = "batched"


def _is_noncharacter(cp: int) -> bool:
    return 0xFDD0 <= cp <= 0xFDEF or (cp & 0xFFFE) == 0xFFFE


def check_string(label: str, value: str) -> None:
    """Enforce the spec `String` type: no controls, noncharacters, or unpaired surrogates."""
    for ch in value:
        cp = ord(ch)
        if cp <= 0x1F or 0x7F <= cp <= 0x9F:
            raise CloudEventError(f"{label}: control character U+{cp:04X}")
        if 0xD800 <= cp <= 0xDFFF:
            # A Python str stores a valid surrogate pair as one code point, so any surrogate
            # code point left here is an unpaired one.
            raise CloudEventError(f"{label}: unpaired surrogate U+{cp:04X}")
        if _is_noncharacter(cp):
            raise CloudEventError(f"{label}: noncharacter U+{cp:04X}")


def _check_rfc3339(value: str) -> None:
    match = _RFC3339.fullmatch(value)
    if match is None:
        raise CloudEventError(f"time: not RFC 3339: {value!r}")
    year, month, day, hour, minute, second = (int(g) for g in match.groups())
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    if not (1 <= month <= 12 and 1 <= day <= days[month - 1]):
        raise CloudEventError(f"time: impossible date: {value!r}")
    if hour > 23 or minute > 59 or second > 60:  # 60 is the RFC 3339 leap second
        raise CloudEventError(f"time: impossible clock time: {value!r}")


def _canonical(value: Extension) -> str:
    if isinstance(value, bool):  # bool before int: bool is an int subclass
        return "true" if value else "false"
    return str(value)


@dataclass(frozen=True, slots=True)
class CloudEvent:
    """One event. `time` is kept as its RFC 3339 canonical string."""

    id: str
    source: str
    type: str
    specversion: str = SPECVERSION
    datacontenttype: str | None = None
    dataschema: str | None = None
    subject: str | None = None
    time: str | None = None
    extensions: dict[str, Extension] = field(default_factory=dict[str, Extension])
    data: Data | _Unset = UNSET

    def __post_init__(self) -> None:
        for name in REQUIRED_ATTRIBUTES:
            value = getattr(self, name)
            if not isinstance(value, str) or value == "":
                raise CloudEventError(f"{name}: REQUIRED and MUST be a non-empty string")
            check_string(name, value)
        if _URI_REFERENCE.fullmatch(self.source) is None:
            raise CloudEventError(f"source: not a URI-reference: {self.source!r}")
        for name in ("datacontenttype", "subject"):
            value = getattr(self, name)
            if value is not None:
                if value == "":
                    raise CloudEventError(f"{name}: if present MUST be non-empty")
                check_string(name, value)
        if self.dataschema is not None and (
            _ABSOLUTE_URI_START.match(self.dataschema) is None
            or _URI_REFERENCE.fullmatch(self.dataschema) is None
            or "#" in self.dataschema  # RFC 3986 Section 4.3 absolute-URI has no fragment
        ):
            raise CloudEventError(f"dataschema: not an absolute URI: {self.dataschema!r}")
        if self.time is not None:
            _check_rfc3339(self.time)
        for name, value in self.extensions.items():
            if _ATTRIBUTE_NAME.fullmatch(name) is None:
                raise CloudEventError(f"extension {name!r}: names are [a-z0-9]+")
            if name in _JSON_MEMBERS:
                raise CloudEventError(f"extension {name!r}: collides with a defined member")
            if isinstance(value, str):
                check_string(f"extension {name}", value)
            elif not isinstance(value, bool) and not INT_MIN <= value <= INT_MAX:
                raise CloudEventError(f"extension {name!r}: Integer is a signed 32-bit value")


def dedup_key(event: CloudEvent) -> tuple[str, str]:
    """Consumers MAY treat identical `source` + `id` as duplicates (spec.md, id)."""
    return (event.source, event.id)


def media_type(value: str) -> str:
    """Lower-cased `type/subtype` with parameters stripped (RFC 9110 Section 8.3.1)."""
    return value.split(";", 1)[0].strip().lower()


def is_json_media_type(content_type: str | None) -> bool:
    """`*/json` or `*/*+json`; an absent `datacontenttype` implies `application/json`."""
    if content_type is None:
        return True
    _, slash, subtype = media_type(content_type).partition("/")
    return slash == "/" and (subtype == "json" or subtype.endswith("+json"))


def encode_header_value(value: str) -> str:
    """Percent-encode per binding 3.1.3.2: space, `"`, `%`, and anything outside U+0021-U+007E."""
    out: list[str] = []
    for ch in value:
        if 0x21 <= ord(ch) <= 0x7E and ch not in '"%':
            out.append(ch)
        else:
            out.extend(f"%{byte:02X}" for byte in ch.encode("utf-8"))
    return "".join(out)


def _unquote(value: str) -> str:
    """Undo an RFC 9110 Section 5.6.4 quoted-string, which older producers still send."""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        out: list[str] = []
        chars = iter(value[1:-1])
        for ch in chars:
            out.append(next(chars, "") if ch == "\\" else ch)
        return "".join(out)
    return value


def decode_header_value(value: str) -> str:
    """Unquote, then percent-decode exactly once; invalid UTF-8 (e.g. `%C0%A0`) is rejected."""
    text = _unquote(value)
    if re.search(r"%(?![0-9A-Fa-f]{2})", text):
        raise CloudEventError(f"malformed percent-escape in header value {value!r}")
    raw = bytearray()
    pos = 0
    for match in _PERCENT_TRIPLET.finditer(text):
        raw += text[pos : match.start()].encode("utf-8")
        raw.append(int(match.group(1), 16))
        pos = match.end()
    raw += text[pos:].encode("utf-8")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CloudEventError(f"invalid UTF-8 after percent-decoding {value!r}") from exc
    check_string("header value", decoded)
    return decoded


@dataclass(frozen=True, slots=True)
class HttpMessage:
    """Headers and body of one HTTP request or response, independent of the start line."""

    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next((v for n, v in self.headers if n.lower() == wanted), None)

    def wire_size(self) -> int:
        """Header lines (`name: value CRLF`) + blank line + body; start line excluded."""
        lines = sum(len(f"{n}: {v}\r\n".encode()) for n, v in self.headers)
        return lines + 2 + len(self.body)


def _attribute_items(event: CloudEvent, *, skip_content_type: bool) -> list[tuple[str, str]]:
    items = [(n, getattr(event, n)) for n in (*REQUIRED_ATTRIBUTES, *OPTIONAL_ATTRIBUTES)]
    pairs = [
        (n, v)
        for n, v in items
        if v is not None and not (skip_content_type and n == "datacontenttype")
    ]
    pairs += [(n, _canonical(v)) for n, v in event.extensions.items()]
    return pairs


def _dump_json(value: Json) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
    except (TypeError, ValueError) as exc:
        raise CloudEventError(f"data is not representable as JSON: {exc}") from exc


def _data_bytes(event: CloudEvent) -> bytes | None:
    data = event.data
    if isinstance(data, _Unset):
        return None
    if isinstance(data, bytes):
        return data
    if is_json_media_type(event.datacontenttype):
        return _dump_json(data)
    if isinstance(data, str):
        return data.encode("utf-8")  # lab policy: only UTF-8 text is produced
    raise CloudEventError("a non-JSON datacontenttype needs str or bytes data")


def to_binary(event: CloudEvent) -> HttpMessage:
    """Binary content mode (binding section 3.1)."""
    headers = [
        (f"ce-{name}", encode_header_value(value))
        for name, value in _attribute_items(event, skip_content_type=True)
    ]
    body = _data_bytes(event)
    content_type = event.datacontenttype
    if content_type is None and body is not None:
        content_type = "application/json"  # spec.md: set the implied type explicitly
    if content_type is not None:
        if not all(0x20 <= ord(c) <= 0x7E for c in content_type):
            raise CloudEventError("datacontenttype must be printable ASCII to be a header")
        headers.append(("content-type", content_type))
    return HttpMessage(tuple(headers), body or b"")


def event_to_json_object(event: CloudEvent) -> dict[str, Json]:
    """JSON format section 3: attributes as members, `data` or `data_base64` for the payload."""
    obj: dict[str, Json] = dict(_attribute_items(event, skip_content_type=False))
    obj.update(event.extensions)
    data = event.data
    if isinstance(data, bytes):
        obj["data_base64"] = base64.b64encode(data).decode("ascii")
    elif not isinstance(data, _Unset):
        if not is_json_media_type(event.datacontenttype) and not isinstance(data, str):
            raise CloudEventError("a non-JSON datacontenttype needs str or bytes data")
        obj["data"] = data
    return obj


def to_structured(event: CloudEvent, *, mirror_headers: bool = False) -> HttpMessage:
    """Structured content mode (binding section 3.2); mirroring `ce-` headers is optional."""
    headers: list[tuple[str, str]] = [("content-type", f"{STRUCTURED_MEDIA_TYPE}; charset=UTF-8")]
    if mirror_headers:
        headers += [h for h in to_binary(event).headers if h[0] != "content-type"]
    return HttpMessage(tuple(headers), _dump_json(event_to_json_object(event)))


def to_batch(events: Sequence[CloudEvent]) -> HttpMessage:
    """Batched content mode (binding section 3.3); an empty batch is valid."""
    if len({e.specversion for e in events}) > 1:
        raise CloudEventError("all batched events MUST share one specversion")
    body = _dump_json([event_to_json_object(e) for e in events])
    return HttpMessage((("content-type", f"{BATCH_MEDIA_TYPE}; charset=UTF-8"),), body)


def detect_mode(content_type: str | None) -> Mode:
    """Binding section 3: batch prefix is tested before the structured prefix.

    `application/cloudevents-batch+json` also starts with `application/cloudevents`, so the
    order of the two prefix tests is load-bearing.
    """
    if content_type is None:
        return Mode.BINARY
    mt = media_type(content_type)
    for prefix, mode, expected in (
        ("application/cloudevents-batch", Mode.BATCHED, "+json"),
        ("application/cloudevents", Mode.STRUCTURED, "+json"),
    ):
        if mt.startswith(prefix):
            if mt[len(prefix) :] != expected:
                raise UnsupportedEventFormatError(f"no decoder for event format {mt!r}")
            return mode
    return Mode.BINARY


def _reject_duplicates(pairs: list[tuple[str, Json]]) -> dict[str, Json]:
    obj: dict[str, Json] = {}
    for key, value in pairs:
        if key in obj:
            raise CloudEventError(f"duplicate JSON member {key!r}")
        obj[key] = value
    return obj


def _reject_constant(name: str) -> Json:
    raise CloudEventError(f"{name} is not valid JSON")


def load_json(body: bytes) -> Json:
    """Strict JSON: UTF-8 without BOM, no NaN, no duplicate members, bounded nesting."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CloudEventError("body is not UTF-8") from exc
    try:
        loaded: Json = json.loads(
            text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant
        )
        return loaded
    except json.JSONDecodeError as exc:
        raise CloudEventError(f"body is not JSON: {exc}") from exc
    except RecursionError as exc:
        raise CloudEventError("JSON nesting too deep") from exc


def _string_member(obj: Mapping[str, Json], name: str, *, required: bool) -> str | None:
    value = obj.get(name)
    if value is None:
        if required:
            raise CloudEventError(f"{name}: REQUIRED")
        return None  # a JSON null means unset (json-format.md section 2.2)
    if not isinstance(value, str):
        raise CloudEventError(f"{name}: must be a JSON string")
    return value


def event_from_json_object(obj: Mapping[str, Json]) -> CloudEvent:
    extensions: dict[str, Extension] = {}
    for name, value in obj.items():
        if name in _JSON_MEMBERS or value is None:
            continue
        if isinstance(value, bool | str | int):
            extensions[name] = value
        else:
            raise CloudEventError(f"extension {name!r}: only Boolean, Integer, String here")
    if "data" in obj and "data_base64" in obj:
        raise CloudEventError("data and data_base64 are mutually exclusive")
    content_type = _string_member(obj, "datacontenttype", required=False)
    data: Data | _Unset = UNSET
    if "data_base64" in obj:
        encoded = obj["data_base64"]
        if encoded is not None:
            if not isinstance(encoded, str):
                raise CloudEventError("data_base64: must be a JSON string")
            try:
                data = base64.b64decode(encoded, validate=True)
            except binascii.Error as exc:
                raise CloudEventError("data_base64: invalid Base64") from exc
    elif "data" in obj:
        data = obj["data"]
        if not is_json_media_type(content_type) and not isinstance(data, str):
            raise CloudEventError("data must be a string when datacontenttype is not JSON")
    return CloudEvent(
        id=_string_member(obj, "id", required=True) or "",
        source=_string_member(obj, "source", required=True) or "",
        type=_string_member(obj, "type", required=True) or "",
        specversion=_string_member(obj, "specversion", required=True) or "",
        datacontenttype=content_type,
        dataschema=_string_member(obj, "dataschema", required=False),
        subject=_string_member(obj, "subject", required=False),
        time=_string_member(obj, "time", required=False),
        extensions=extensions,
        data=data,
    )


def _from_binary(message: HttpMessage) -> CloudEvent:
    seen: dict[str, str] = {}
    for name, value in message.headers:
        lowered = name.lower()
        if lowered.startswith("ce-"):
            if lowered in seen:
                # RFC 9110 Section 5.3 lets a recipient fold repeats into "a, b": ambiguous.
                raise CloudEventError(f"duplicate header {lowered}")
            seen[lowered] = value
    if "ce-datacontenttype" in seen:
        raise CloudEventError("ce-datacontenttype MUST NOT be present (binding 3.1.1)")
    attrs = {k[3:]: decode_header_value(v) for k, v in seen.items()}
    missing = [n for n in REQUIRED_ATTRIBUTES if n not in attrs]
    if missing:
        raise NotACloudEventError(f"missing mandatory headers: {missing}")
    content_type = message.header("content-type")
    data: Data | _Unset = UNSET
    if message.body:
        data = (
            load_json(message.body)
            if content_type and is_json_media_type(content_type)
            else message.body
        )
    known = {*REQUIRED_ATTRIBUTES, *OPTIONAL_ATTRIBUTES}
    return CloudEvent(
        id=attrs["id"],
        source=attrs["source"],
        type=attrs["type"],
        specversion=attrs["specversion"],
        datacontenttype=content_type,
        dataschema=attrs.get("dataschema"),
        subject=attrs.get("subject"),
        time=attrs.get("time"),
        extensions={k: v for k, v in attrs.items() if k not in known},
        data=data,
    )


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    mode: Mode
    events: tuple[CloudEvent, ...]


def parse_message(headers: Sequence[tuple[str, str]], body: bytes) -> ParsedMessage:
    """Receive side: pick the mode from `Content-Type`, then decode accordingly."""
    message = HttpMessage(tuple(headers), body)
    content_types = [v for n, v in message.headers if n.lower() == "content-type"]
    if len(content_types) > 1:
        raise CloudEventError("multiple Content-Type headers")
    mode = detect_mode(content_types[0] if content_types else None)
    if mode is Mode.BINARY:
        events: tuple[CloudEvent, ...] = (_from_binary(message),)
    elif mode is Mode.STRUCTURED:
        parsed = load_json(body)
        if not isinstance(parsed, dict):
            raise CloudEventError("structured body must be a JSON object")
        events = (event_from_json_object(parsed),)
    else:
        parsed = load_json(body)
        if not isinstance(parsed, list) or not all(isinstance(i, dict) for i in parsed):
            raise CloudEventError("batched body must be a JSON array of objects")
        events = tuple(event_from_json_object(i) for i in parsed if isinstance(i, dict))
    for event in events:
        if event.specversion != SPECVERSION:
            raise CloudEventError(f"unsupported specversion {event.specversion!r}")
    return ParsedMessage(mode, events)
