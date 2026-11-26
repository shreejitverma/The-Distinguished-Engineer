"""Schema evolution for CloudEvents `data`: a minimal registry, compatibility modes, wire framing.

This is a deliberately small model of the Avro-style reader/writer rules that Confluent Schema
Registry documents (backward, forward, full, and their transitive variants).
It is not a re-implementation of any registry product; it exists to make the rules executable.
"""

import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Final

from api_labs.f20_graphql_json_rpc_and_event_apis.cloudevents_envelope import (
    UNSET,
    CloudEvent,
    Json,
)

MAGIC_BYTE: Final = 0
FRAME_HEADER_SIZE: Final = 5


class Compatibility(Enum):
    NONE = "NONE"
    BACKWARD = "BACKWARD"
    BACKWARD_TRANSITIVE = "BACKWARD_TRANSITIVE"
    FORWARD = "FORWARD"
    FULL = "FULL"


@dataclass(frozen=True, slots=True)
class Field:
    """`kind` is one of string, int, number, bool; `has_default` makes a field safe to add."""

    kind: str
    has_default: bool = False


type Schema = Mapping[str, Field]

_PROMOTIONS: Final = frozenset({("int", "number")})


class IncompatibleSchemaError(ValueError):
    def __init__(self, subject: str, violations: list[str]) -> None:
        super().__init__(f"{subject}: {'; '.join(violations)}")
        self.violations = violations


class SchemaNotFoundError(KeyError):
    """The schema id or URI is not registered."""


def reader_violations(writer: Schema, reader: Schema) -> list[str]:
    """Why a consumer holding `reader` cannot decode data written with `writer`."""
    problems: list[str] = []
    for name, spec in reader.items():
        written = writer.get(name)
        if written is None:
            if not spec.has_default:
                problems.append(
                    f"reader field {name!r} is absent from the writer and has no default"
                )
        elif written.kind != spec.kind and (written.kind, spec.kind) not in _PROMOTIONS:
            problems.append(f"field {name!r}: writer {written.kind} cannot be read as {spec.kind}")
    return problems


@dataclass(frozen=True, slots=True)
class Registered:
    subject: str
    version: int
    schema_id: int
    uri: str


class SchemaRegistry:
    """Global ids, per-subject versions, and a compatibility gate on `register`."""

    def __init__(
        self,
        base_uri: str = "https://schemas.example.com",
        compatibility: Compatibility = Compatibility.BACKWARD,
    ) -> None:
        self._base_uri = base_uri
        self._compatibility = compatibility
        self._by_subject: dict[str, list[Registered]] = {}
        self._schemas: dict[int, Schema] = {}
        self._by_uri: dict[str, int] = {}

    def _violations(self, subject: str, candidate: Schema) -> list[str]:
        history = [self._schemas[r.schema_id] for r in self._by_subject.get(subject, [])]
        if not history or self._compatibility is Compatibility.NONE:
            return []
        mode = self._compatibility
        previous = history if mode is Compatibility.BACKWARD_TRANSITIVE else history[-1:]
        problems: list[str] = []
        for old in previous:
            if mode is not Compatibility.FORWARD:  # BACKWARD, BACKWARD_TRANSITIVE, FULL
                problems += reader_violations(writer=old, reader=candidate)
            if mode in (Compatibility.FORWARD, Compatibility.FULL):
                problems += reader_violations(writer=candidate, reader=old)
        return problems

    def register(self, subject: str, schema: Schema) -> Registered:
        problems = self._violations(subject, schema)
        if problems:
            raise IncompatibleSchemaError(subject, problems)
        versions = self._by_subject.setdefault(subject, [])
        schema_id = len(self._schemas) + 1
        version = len(versions) + 1
        uri = f"{self._base_uri}/subjects/{subject}/versions/{version}"
        entry = Registered(subject, version, schema_id, uri)
        versions.append(entry)
        self._schemas[schema_id] = dict(schema)
        self._by_uri[uri] = schema_id
        return entry

    def schema(self, schema_id: int) -> Schema:
        try:
            return self._schemas[schema_id]
        except KeyError:
            raise SchemaNotFoundError(schema_id) from None

    def schema_for_uri(self, dataschema: str) -> Schema:
        try:
            return self._schemas[self._by_uri[dataschema]]
        except KeyError:
            raise SchemaNotFoundError(dataschema) from None


def frame(schema_id: int, payload: bytes) -> bytes:
    """Confluent payload-prefix wire format: magic byte 0, 4-byte big-endian id, then data."""
    return bytes([MAGIC_BYTE]) + struct.pack(">I", schema_id) + payload


def unframe(blob: bytes) -> tuple[int, bytes]:
    if len(blob) < FRAME_HEADER_SIZE:
        raise ValueError("frame shorter than the 5-byte header")
    if blob[0] != MAGIC_BYTE:
        raise ValueError(f"unknown magic byte {blob[0]}")
    (schema_id,) = struct.unpack(">I", blob[1:FRAME_HEADER_SIZE])
    return schema_id, blob[FRAME_HEADER_SIZE:]


_KIND_CHECKS: Final[dict[str, Callable[[object], bool]]] = {
    "string": lambda v: isinstance(v, str),
    "bool": lambda v: isinstance(v, bool),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
}


def data_problems(schema: Schema, data: Json) -> list[str]:
    """Check a JSON `data` object against a schema: required fields and field kinds."""
    if not isinstance(data, dict):
        return ["data is not a JSON object"]
    problems: list[str] = []
    for name, spec in schema.items():
        if name not in data:
            if not spec.has_default:
                problems.append(f"missing required field {name!r}")
        elif not _KIND_CHECKS[spec.kind](data[name]):
            problems.append(f"field {name!r} is not a {spec.kind}")
    return problems


def validate_event(registry: SchemaRegistry, event: CloudEvent) -> list[str]:
    """Resolve `dataschema` in the registry and check `data` against it."""
    if event.dataschema is None:
        return ["event has no dataschema"]
    try:
        schema = registry.schema_for_uri(event.dataschema)
    except SchemaNotFoundError:
        return [f"unknown dataschema {event.dataschema}"]
    data = event.data
    if data is UNSET or isinstance(data, bytes):
        return ["data is not a JSON object"]
    return data_problems(schema, data)
