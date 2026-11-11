"""A declarative-friendly resource (AIP-128) and the declarative client that drives it.

The resource is a cluster: `display_name` and `annotations` apply at once, while `node_count` is
moved one node per reconciler tick, so `GET` shows the current state with `reconciling: true`
until it matches the intended state.
The service implements the AIP-134 `allow_missing` upsert, the AIP-135 `allow_missing` delete,
the AIP-154 `etag` check, the AIP-148 `uid`, and the AIP-163 `validate_only` flag.
The client plans like a configuration-as-code tool: read, wait while reconciling, diff, then
send one masked upsert carrying the `etag` it read.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx
    from starlette.requests import Request

type JSONObject = dict[str, object]

SPEC_FIELDS: Final = ("annotations", "display_name", "node_count")
OUTPUT_ONLY_FIELDS: Final = frozenset(
    {"name", "uid", "etag", "reconciling", "create_time", "update_time"}
)
MAX_DISPLAY_NAME_CHARS: Final = 63  # AIP-148 display_name
ANNOTATIONS_LIMIT_BYTES: Final = 256 * (1 << 10)  # Kubernetes TotalAnnotationSizeLimitB
MAX_NODES: Final = 100  # lab design choice
CLUSTER_ID: Final = re.compile(r"[a-z][a-z0-9-]{2,61}[a-z0-9]")  # AIP-133 example: 4-63 chars
ANNOTATION_KEY: Final = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+/[A-Za-z0-9._-]{1,63}")
EPOCH: Final = datetime(2026, 11, 10, tzinfo=UTC)


class Code(StrEnum):
    """The google.rpc.Code names this lab uses."""

    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    ABORTED = "ABORTED"


HTTP_STATUS: Final[dict[Code, int]] = {  # google/rpc/code.proto "HTTP Mapping" lines
    Code.INVALID_ARGUMENT: 400,
    Code.UNAUTHENTICATED: 401,
    Code.PERMISSION_DENIED: 403,
    Code.NOT_FOUND: 404,
    Code.ALREADY_EXISTS: 409,
    Code.ABORTED: 409,
}


class ApiError(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def body(self) -> JSONObject:
        """The AIP-193 HTTP/1.1+JSON error shape."""
        error = {"code": HTTP_STATUS[self.code], "message": self.message, "status": self.code}
        return {"error": error}


@dataclass(frozen=True, slots=True)
class Spec:
    """The user-settable, intended state."""

    display_name: str
    node_count: int
    annotations: dict[str, str]


@dataclass(slots=True)
class Record:
    """What the service stores: the intended spec plus the observed node count."""

    cluster_id: str
    uid: str
    desired: Spec
    current_nodes: int
    create_time: int
    update_time: int


def _timestamp(tick: int) -> str:
    return (EPOCH + timedelta(seconds=tick)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _display_name(value: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_DISPLAY_NAME_CHARS:
        raise ApiError(Code.INVALID_ARGUMENT, "display_name: string of <= 63 characters")
    return value


def _node_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_NODES:
        raise ApiError(Code.INVALID_ARGUMENT, f"node_count: integer in 1..{MAX_NODES}")
    return value


def _annotations(value: object) -> dict[str, str]:
    """AIP-148: Kubernetes limits and dot-namespaced keys, so tools do not trample each other."""
    if not isinstance(value, dict):
        raise ApiError(Code.INVALID_ARGUMENT, "annotations: object of strings")
    annotations: dict[str, str] = {}
    size = 0
    for key, item in value.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(key, str) or not isinstance(item, str):
            raise ApiError(Code.INVALID_ARGUMENT, "annotations: object of strings")
        if ANNOTATION_KEY.fullmatch(key) is None:
            raise ApiError(Code.INVALID_ARGUMENT, f"annotations: key {key!r} needs a dns/ prefix")
        size += len(key.encode()) + len(item.encode())
        annotations[key] = item
    if size > ANNOTATIONS_LIMIT_BYTES:
        raise ApiError(Code.INVALID_ARGUMENT, f"annotations: {size} bytes > 262144")
    return annotations


def _spec_from(fields: JSONObject) -> Spec:
    """Build a full spec; a missing `node_count` is a missing required field."""
    if "node_count" not in fields:
        raise ApiError(Code.INVALID_ARGUMENT, "node_count is required")
    return Spec(
        _display_name(fields.get("display_name", "")),
        _node_count(fields["node_count"]),
        _annotations(fields.get("annotations", {})),
    )


def _input_fields(body: JSONObject) -> JSONObject:
    """Drop output-only fields without error (AIP-203); reject unknown ones."""
    fields: JSONObject = {}
    for key, value in body.items():
        if key in OUTPUT_ONLY_FIELDS:
            continue
        if key not in SPEC_FIELDS:
            raise ApiError(Code.INVALID_ARGUMENT, f"unknown field {key!r}")
        fields[key] = value
    return fields


def _is_populated(value: object) -> bool:
    return value not in ("", 0, None) and value != {}


@dataclass
class ClusterService:
    """In-memory, strongly consistent store plus a one-node-per-tick reconciler."""

    tokens: dict[str, frozenset[str]]
    seed: int = 0
    upsert_requires_create: bool = False  # AIP-134 needs only update permission
    records: dict[str, Record] = field(default_factory=dict[str, Record])
    clock: int = 0
    commits: int = 0
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        # Seeded so the lab is deterministic; a real service draws uid bits from a CSPRNG.
        self._rng = random.Random(self.seed)  # noqa: S311

    def _new_uid(self) -> str:
        # RFC 9562 Section 5.4: 128 random bits, then the version and variant bits overwritten.
        return str(uuid.UUID(int=self._rng.getrandbits(128), version=4))

    def _authorize(self, token: str | None, permission: str) -> None:
        if token is None or token not in self.tokens:
            raise ApiError(Code.UNAUTHENTICATED, "missing or unknown credentials")
        if permission not in self.tokens[token]:
            raise ApiError(Code.PERMISSION_DENIED, f"caller lacks {permission}")

    def represent(self, record: Record, *, with_identity: bool = True) -> JSONObject:
        """The current state, never the intended one, for `node_count` (AIP-128)."""
        rep: JSONObject = {
            "name": f"clusters/{record.cluster_id}",
            "uid": record.uid,
            "display_name": record.desired.display_name,
            "node_count": record.current_nodes,
            "annotations": dict(record.desired.annotations),
            "reconciling": record.current_nodes != record.desired.node_count,
            "create_time": _timestamp(record.create_time),
            "update_time": _timestamp(record.update_time),
        }
        canonical = json.dumps(rep, sort_keys=True, separators=(",", ":")).encode()
        rep["etag"] = '"' + hashlib.sha256(canonical).hexdigest()[:16] + '"'
        if not with_identity:  # AIP-163: omit generated values on a validate_only create
            del rep["uid"], rep["etag"]
        return rep

    def _check_etag(self, record: Record, etag: object) -> None:
        if etag in (None, ""):
            return  # AIP-154: an absent etag should be permitted
        if etag != self.represent(record)["etag"]:
            raise ApiError(Code.ABORTED, "etag does not match the current resource")

    def tick(self) -> None:
        """Advance the clock; the reconciler reads the latest intent (level-based)."""
        self.clock += 1
        for record in self.records.values():
            gap = record.desired.node_count - record.current_nodes
            if gap:
                record.current_nodes += 1 if gap > 0 else -1

    def lose_nodes(self, cluster_id: str, nodes: int) -> None:
        """System action: hardware loss drops the observed count below the intent."""
        record = self.records[cluster_id]
        record.current_nodes = max(0, record.current_nodes - nodes)

    def get(self, token: str | None, cluster_id: str) -> JSONObject:
        self._authorize(token, "clusters.get")
        record = self.records.get(cluster_id)
        if record is None:
            raise ApiError(Code.NOT_FOUND, f"clusters/{cluster_id} not found")
        return self.represent(record)

    def create(
        self, token: str | None, cluster_id: str, body: JSONObject, *, validate_only: bool
    ) -> JSONObject:
        self._authorize(token, "clusters.create")
        if CLUSTER_ID.fullmatch(cluster_id) is None:
            raise ApiError(Code.INVALID_ARGUMENT, f"cluster_id must match {CLUSTER_ID.pattern}")
        if cluster_id in self.records:
            raise ApiError(Code.ALREADY_EXISTS, f"clusters/{cluster_id} already exists")
        return self._insert(cluster_id, _spec_from(_input_fields(body)), validate_only)

    def _insert(self, cluster_id: str, spec: Spec, validate_only: bool) -> JSONObject:
        record = Record(cluster_id, "", spec, 0, self.clock, self.clock)
        if validate_only:
            return self.represent(record, with_identity=False)
        record.uid = self._new_uid()
        self.records[cluster_id] = record
        self.commits += 1
        return self.represent(record)

    def update(
        self,
        token: str | None,
        cluster_id: str,
        body: JSONObject,
        *,
        update_mask: list[str] | None,
        allow_missing: bool,
        validate_only: bool,
    ) -> JSONObject:
        # Permission before existence, so a 404 never leaks to an unauthorized caller.
        self._authorize(token, "clusters.update")
        fields = _input_fields(body)
        record = self.records.get(cluster_id)
        if record is None:
            if not allow_missing:
                raise ApiError(Code.NOT_FOUND, f"clusters/{cluster_id} not found")
            if self.upsert_requires_create:
                self._authorize(token, "clusters.create")
            if body.get("etag") not in (None, ""):
                # Lab design choice: an etag names a version that no longer exists.
                raise ApiError(Code.ABORTED, "etag given but the resource does not exist")
            if CLUSTER_ID.fullmatch(cluster_id) is None:
                raise ApiError(Code.INVALID_ARGUMENT, f"cluster_id must match {CLUSTER_ID.pattern}")
            # AIP-134: on create every field applies, whatever the update_mask says.
            return self._insert(cluster_id, _spec_from(fields), validate_only)
        self._check_etag(record, body.get("etag"))
        if update_mask is None:  # AIP-134: implied mask of the populated fields
            paths = [key for key, value in fields.items() if _is_populated(value)]
        elif update_mask == ["*"]:
            paths = list(SPEC_FIELDS)
        else:
            paths = [path for path in update_mask if path not in OUTPUT_ONLY_FIELDS]
        merged: JSONObject = {
            "display_name": record.desired.display_name,
            "node_count": record.desired.node_count,
            "annotations": record.desired.annotations,
        }
        for path in paths:
            if path not in SPEC_FIELDS:
                raise ApiError(Code.INVALID_ARGUMENT, f"unknown update_mask path {path!r}")
            if path in fields:
                merged[path] = fields[path]
            else:
                merged.pop(path)  # a masked but absent field is cleared
        spec = _spec_from(merged)
        if spec == record.desired:
            return self.represent(record)  # AIP-134: all fields match, returned unchanged
        candidate = replace(record, desired=spec, update_time=self.clock)
        if validate_only:
            return self.represent(candidate)
        self.records[cluster_id] = candidate
        self.commits += 1
        return self.represent(candidate)

    def delete(
        self,
        token: str | None,
        cluster_id: str,
        *,
        etag: str | None,
        allow_missing: bool,
        validate_only: bool,
    ) -> JSONObject:
        self._authorize(token, "clusters.delete")
        record = self.records.get(cluster_id)
        if record is None:
            if allow_missing:
                return {}  # AIP-135: a no-op, and the etag is ignored
            raise ApiError(Code.NOT_FOUND, f"clusters/{cluster_id} not found")
        self._check_etag(record, etag)
        if not validate_only:
            del self.records[cluster_id]
            self.commits += 1
        return {}


def _param(request: Request, name: str) -> str | None:
    """One value or none: Starlette's `QueryParams.get` silently keeps the last duplicate."""
    values = request.query_params.getlist(name)
    if len(values) > 1:
        raise ApiError(Code.INVALID_ARGUMENT, f"{name} given {len(values)} times")
    return values[0] if values else None


def _flag(request: Request, name: str) -> bool:
    raw = _param(request, name)
    if raw is None:
        return False
    if raw not in ("true", "false"):
        raise ApiError(Code.INVALID_ARGUMENT, f"{name} must be true or false")
    return raw == "true"


def _token(request: Request) -> str | None:
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    return credentials if scheme.lower() == "bearer" and credentials else None


async def _body(request: Request) -> JSONObject:
    try:
        parsed: object = await request.json()
    except ValueError as exc:
        raise ApiError(Code.INVALID_ARGUMENT, "body is not JSON") from exc
    if not isinstance(parsed, dict):
        raise ApiError(Code.INVALID_ARGUMENT, "body must be a JSON object")
    items: dict[object, object] = parsed  # pyright: ignore[reportUnknownVariableType]
    return {str(key): value for key, value in items.items()}


def build_app(service: ClusterService) -> Starlette:
    """Map the standard methods onto HTTP as AIP-131, AIP-133, AIP-134, and AIP-135 do."""

    async def dispatch(request: Request) -> JSONObject:
        token = _token(request)
        validate_only = _flag(request, "validate_only")
        if request.method == "POST":
            cluster_id = _param(request, "cluster_id") or ""
            body = await _body(request)
            return service.create(token, cluster_id, body, validate_only=validate_only)
        cluster_id = str(request.path_params["cluster"])
        if request.method == "GET":
            return service.get(token, cluster_id)
        allow_missing = _flag(request, "allow_missing")
        if request.method == "DELETE":
            etag = _param(request, "etag")
            return service.delete(
                token,
                cluster_id,
                etag=etag,
                allow_missing=allow_missing,
                validate_only=validate_only,
            )
        raw_mask = _param(request, "update_mask")
        mask = None if raw_mask is None else [path for path in raw_mask.split(",") if path]
        body = await _body(request)
        return service.update(
            token,
            cluster_id,
            body,
            update_mask=mask,
            allow_missing=allow_missing,
            validate_only=validate_only,
        )

    async def endpoint(request: Request) -> JSONResponse:
        try:
            result = await dispatch(request)
        except ApiError as error:
            return JSONResponse(error.body(), status_code=HTTP_STATUS[error.code])
        etag = result.get("etag")
        return JSONResponse(result, headers={"etag": etag} if isinstance(etag, str) else None)

    routes = [
        Route("/v1/clusters", endpoint, methods=["POST"]),
        Route("/v1/clusters/{cluster}", endpoint, methods=["GET", "PATCH", "DELETE"]),
    ]
    return Starlette(routes=routes)


class Action(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    WAIT = "WAIT"
    NOOP = "NOOP"


@dataclass(frozen=True, slots=True)
class Plan:
    action: Action
    update_mask: tuple[str, ...] = ()
    etag: str | None = None


class ReplacedOutOfBandError(Exception):
    """The name now refers to a different resource than the one this client created."""


def error_status(response: httpx.Response) -> str | None:
    """The google.rpc.Code name; ABORTED and ALREADY_EXISTS are both HTTP 409."""
    payload: object = response.json()
    if not isinstance(payload, dict):
        return None
    envelope: dict[str, object] = payload  # pyright: ignore[reportUnknownVariableType]
    error = envelope.get("error")
    if not isinstance(error, dict):
        return None
    fields: dict[str, object] = error  # pyright: ignore[reportUnknownVariableType]
    status = fields.get("status")
    return status if isinstance(status, str) else None


@dataclass
class DeclarativeClient:
    """A configuration-as-code applier: plan from a read, then one masked upsert."""

    http: httpx.AsyncClient
    token: str
    known_uids: dict[str, str] = field(default_factory=dict[str, str])  # the tool's state file
    write_requests: int = 0

    @property
    def _auth(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}"}

    async def plan(self, cluster_id: str, desired: JSONObject) -> Plan:
        response = await self.http.get(f"/v1/clusters/{cluster_id}", headers=self._auth)
        if response.status_code == 404:
            return Plan(Action.CREATE)
        response.raise_for_status()
        current: JSONObject = response.json()
        uid = str(current["uid"])
        known = self.known_uids.setdefault(cluster_id, uid)
        if known != uid:
            raise ReplacedOutOfBandError(f"clusters/{cluster_id}: uid {known} became {uid}")
        if current["reconciling"] is True:
            return Plan(Action.WAIT)  # GET shows the current state, so a diff now is noise
        mask = tuple(sorted(key for key, value in desired.items() if current.get(key) != value))
        if not mask:
            return Plan(Action.NOOP)
        return Plan(Action.UPDATE, mask, str(current["etag"]))

    async def apply(self, cluster_id: str, desired: JSONObject, plan: Plan) -> httpx.Response:
        body = dict(desired) if plan.etag is None else {**desired, "etag": plan.etag}
        params = {"allow_missing": "true", "update_mask": ",".join(plan.update_mask)}
        self.write_requests += 1
        response = await self.http.patch(
            f"/v1/clusters/{cluster_id}", params=params, json=body, headers=self._auth
        )
        if response.status_code == 200:
            self.known_uids[cluster_id] = str(response.json()["uid"])
        return response

    async def converge(
        self, cluster_id: str, desired: JSONObject, tick: Callable[[], None], max_rounds: int = 20
    ) -> list[Action]:
        """Plan and apply until a plan is a no-op; re-read after ABORTED (code.proto)."""
        actions: list[Action] = []
        for _ in range(max_rounds):
            plan = await self.plan(cluster_id, desired)
            actions.append(plan.action)
            if plan.action is Action.NOOP:
                return actions
            if plan.action is Action.WAIT:
                tick()
                continue
            response = await self.apply(cluster_id, desired, plan)
            if response.status_code == 409 and error_status(response) == Code.ABORTED:
                continue
            response.raise_for_status()
        raise RuntimeError(f"no convergence in {max_rounds} rounds: {actions}")

    async def destroy(self, cluster_id: str) -> httpx.Response:
        """Delete idempotently: `allow_missing` turns a repeated delete into a no-op."""
        self.write_requests += 1
        response = await self.http.delete(
            f"/v1/clusters/{cluster_id}", params={"allow_missing": "true"}, headers=self._auth
        )
        if response.status_code == 200:
            self.known_uids.pop(cluster_id, None)
        return response
