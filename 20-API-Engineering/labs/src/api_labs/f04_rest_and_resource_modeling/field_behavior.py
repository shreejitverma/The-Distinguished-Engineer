"""Enforce Google AIP-203 field behaviors on a JSON resource API.

AIP-203 says the `google.api.field_behavior` vocabulary is descriptive only and
adds no validation, so every guarantee below is server code. One registry of
per-field behaviors drives four places that otherwise drift apart:

- create: drop OUTPUT_ONLY and IDENTIFIER values, then require REQUIRED fields
  to be truthy;
- update: ignore OUTPUT_ONLY paths, accept an IMMUTABLE or IDENTIFIER value
  only when it equals the stored value, and reject a falsy REQUIRED value;
- output: never emit INPUT_ONLY fields;
- OpenAPI: OUTPUT_ONLY -> `readOnly: true`, INPUT_ONLY -> `writeOnly: true`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from pydantic import GetJsonSchemaHandler
    from pydantic.json_schema import JsonSchemaValue
    from pydantic_core import CoreSchema

ERROR_DOMAIN = "disks.example.com"


class FieldBehavior(StrEnum):
    """The subset of `google.api.FieldBehavior` values this lab enforces."""

    OPTIONAL = "OPTIONAL"
    REQUIRED = "REQUIRED"
    OUTPUT_ONLY = "OUTPUT_ONLY"
    INPUT_ONLY = "INPUT_ONLY"
    IMMUTABLE = "IMMUTABLE"
    IDENTIFIER = "IDENTIFIER"


B = FieldBehavior


@dataclass(frozen=True, slots=True)
class Behaviors:
    """`Annotated` metadata carrying the AIP-203 behaviors of one field."""

    values: frozenset[FieldBehavior]

    def __get_pydantic_json_schema__(
        self, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        schema = handler(core_schema)
        if B.OUTPUT_ONLY in self.values:
            schema["readOnly"] = True
        if B.INPUT_ONLY in self.values:
            schema["writeOnly"] = True
        return schema


def behaviors(*values: FieldBehavior) -> Behaviors:
    return Behaviors(frozenset(values))


class FieldBehaviorSchemaError(Exception):
    """A model's behavior annotations break an AIP-203 rule."""


class InvalidArgument(Exception):
    """A request violates a field behavior; maps to `400` / `INVALID_ARGUMENT`."""

    def __init__(self, field: str, reason: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.reason = reason
        self.message = message


class Disk(BaseModel):
    """One model for requests and responses, as AIP-133 and AIP-134 use one resource message.

    Every field has a default so that parsing never decides "required": Pydantic's
    notion (key present) differs from AIP-203's (truthy value stored), and an
    update may omit a REQUIRED field that is not in the mask.
    """

    model_config = ConfigDict(extra="ignore")

    name: Annotated[str, behaviors(B.IDENTIFIER)] = ""
    display_name: Annotated[str, behaviors(B.REQUIRED)] = ""
    disk_type: Annotated[str, behaviors(B.REQUIRED, B.IMMUTABLE)] = ""
    size_gb: Annotated[int, Field(ge=0), behaviors(B.OPTIONAL)] = 0
    ttl_seconds: Annotated[int | None, Field(ge=1), behaviors(B.INPUT_ONLY, B.OPTIONAL)] = None
    uid: Annotated[str, behaviors(B.OUTPUT_ONLY)] = ""
    state: Annotated[str, behaviors(B.OUTPUT_ONLY)] = ""
    create_time: Annotated[datetime | None, behaviors(B.OUTPUT_ONLY)] = None
    update_time: Annotated[datetime | None, behaviors(B.OUTPUT_ONLY)] = None
    expire_time: Annotated[datetime | None, behaviors(B.OUTPUT_ONLY)] = None


def behavior_table(model: type[BaseModel]) -> dict[str, frozenset[FieldBehavior]]:
    """Read and check every field's behaviors; raise on an AIP-203 violation."""
    table: dict[str, frozenset[FieldBehavior]] = {}
    for name, info in model.model_fields.items():
        found = [m.values for m in info.metadata if isinstance(m, Behaviors)]
        if not found:
            raise FieldBehaviorSchemaError(f"{name}: no field_behavior annotation")
        values = frozenset[FieldBehavior]().union(*found)
        if not values & {B.REQUIRED, B.OPTIONAL, B.OUTPUT_ONLY, B.IDENTIFIER}:
            raise FieldBehaviorSchemaError(f"{name}: needs REQUIRED, OPTIONAL, or OUTPUT_ONLY")
        if B.OUTPUT_ONLY in values and values & {B.REQUIRED, B.INPUT_ONLY}:
            raise FieldBehaviorSchemaError(f"{name}: OUTPUT_ONLY contradicts {sorted(values)}")
        if B.IDENTIFIER in values and name != "name":
            raise FieldBehaviorSchemaError(f"{name}: IDENTIFIER belongs only on `name`")
        table[name] = values
    return table


DISK_BEHAVIORS = behavior_table(Disk)


def is_truthy(value: object) -> bool:
    """AIP-203 "truthy": not 0, 0.0, empty string or bytes, false, empty container, or None."""
    return bool(value)


@dataclass(frozen=True, slots=True)
class Plan:
    """The field values a request may write, and the sent fields it ignored."""

    changes: dict[str, Any]
    ignored: tuple[str, ...]


def plan_create(body: Disk, table: Mapping[str, frozenset[FieldBehavior]]) -> Plan:
    sent = body.model_fields_set
    ignored = tuple(
        sorted(f for f in sent if table[f] & {B.OUTPUT_ONLY, B.IDENTIFIER}),
    )
    changes = {f: getattr(body, f) for f in sorted(sent) if f not in ignored}
    for field, values in table.items():
        if B.REQUIRED in values and not is_truthy(changes.get(field)):
            raise InvalidArgument(field, "FIELD_REQUIRED", f"`{field}` is required on create")
    return Plan(changes, ignored)


def plan_update(
    stored: Disk,
    body: Disk,
    update_mask: Sequence[str] | None,
    table: Mapping[str, frozenset[FieldBehavior]],
) -> Plan:
    """Apply AIP-203 per masked path; no mask means "the fields the client sent"."""
    paths = sorted(body.model_fields_set) if update_mask is None else list(update_mask)
    changes: dict[str, Any] = {}
    ignored: list[str] = []
    for path in paths:
        if path not in table:
            raise InvalidArgument(path, "UNKNOWN_FIELD", f"`{path}` is not a field of Disk")
        values = table[path]
        if B.OUTPUT_ONLY in values:
            ignored.append(path)
            continue
        new = getattr(body, path)
        if values & {B.IMMUTABLE, B.IDENTIFIER}:
            if new == getattr(stored, path):
                ignored.append(path)
                continue
            raise InvalidArgument(path, "FIELD_IMMUTABLE", f"`{path}` cannot change after create")
        if B.REQUIRED in values and not is_truthy(new):
            raise InvalidArgument(path, "FIELD_REQUIRED", f"`{path}` cannot be cleared")
        changes[path] = new
    return Plan(changes, tuple(ignored))


def render(resource: Disk, table: Mapping[str, frozenset[FieldBehavior]]) -> dict[str, Any]:
    """Serialize for output; INPUT_ONLY fields never leave the server."""
    hidden = {f for f, values in table.items() if B.INPUT_ONLY in values}
    return resource.model_dump(mode="json", exclude=hidden)


def sequential_uuid4() -> Callable[[], str]:
    """Deterministic version-4-shaped UUIDs for tests (AIP-148 requires UUID4 for `uid`)."""
    counter = 0

    def next_uid() -> str:
        nonlocal counter
        counter += 1
        return str(uuid.UUID(int=counter, version=4))

    return next_uid


class DiskStore:
    """In-memory store that applies the behavior plans; no I/O, injected clock and ids."""

    def __init__(self, clock: Callable[[], datetime], new_uid: Callable[[], str]) -> None:
        self._clock = clock
        self._new_uid = new_uid
        self._disks: dict[str, Disk] = {}
        self.table = DISK_BEHAVIORS

    def create(self, disk_id: str, body: Disk) -> Disk:
        plan = plan_create(body, self.table)
        now = self._clock()
        ttl = plan.changes.pop("ttl_seconds", None)
        disk = Disk.model_validate(
            {
                **plan.changes,
                "name": f"disks/{disk_id}",
                "uid": self._new_uid(),
                "state": "READY",
                "create_time": now,
                "update_time": now,
                "expire_time": None if ttl is None else now + timedelta(seconds=ttl),
            }
        )
        self._disks[disk_id] = disk
        return disk

    def get(self, disk_id: str) -> Disk:
        return self._disks[disk_id]

    def update(self, disk_id: str, body: Disk, update_mask: Sequence[str] | None) -> Disk:
        stored = self._disks[disk_id]
        plan = plan_update(stored, body, update_mask, self.table)
        if not plan.changes:
            return stored
        now = self._clock()
        changes = dict(plan.changes)
        if "ttl_seconds" in changes:
            ttl = changes.pop("ttl_seconds")
            changes["expire_time"] = None if ttl is None else now + timedelta(seconds=ttl)
        updated = Disk.model_validate({**stored.model_dump(), **changes, "update_time": now})
        self._disks[disk_id] = updated
        return updated


def error_body(exc: InvalidArgument) -> dict[str, Any]:
    """AIP-193 HTTP/1.1+JSON error: HTTP code, google.rpc.Code name, ErrorInfo detail."""
    return {
        "error": {
            "code": 400,
            "message": exc.message,
            "status": "INVALID_ARGUMENT",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": exc.reason,
                    "domain": ERROR_DOMAIN,
                    "metadata": {"field": exc.field},
                }
            ],
        }
    }


def create_app(store: DiskStore) -> FastAPI:
    app = FastAPI()

    async def invalid_argument(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, InvalidArgument):  # registered for this type only
            raise exc
        return JSONResponse(error_body(exc), status_code=400)

    app.add_exception_handler(InvalidArgument, invalid_argument)

    @app.post("/v1/disks")
    async def create_disk(disk_id: str, body: Disk) -> dict[str, Any]:
        return render(store.create(disk_id, body), store.table)

    @app.get("/v1/disks/{disk_id}")
    async def get_disk(disk_id: str) -> dict[str, Any]:
        return render(store.get(disk_id), store.table)

    @app.patch("/v1/disks/{disk_id}")
    async def update_disk(
        disk_id: str, body: Disk, update_mask: str | None = None
    ) -> dict[str, Any]:
        mask = None if update_mask is None else [p for p in update_mask.split(",") if p]
        return render(store.update(disk_id, body, mask), store.table)

    return app


def fixed_clock(start: datetime, step: timedelta) -> Callable[[], datetime]:
    """A clock that advances by `step` on every read; deterministic timestamps."""
    current = start - step

    def now() -> datetime:
        nonlocal current
        current += step
        return current

    return now


EPOCH = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)
