"""Stripe-style date-based API versioning: pinning, header overrides, and version changes.

Handlers speak only the newest (head) schema.
Each dated version carries declarative version change modules that upgrade old request
parameters to head before the handler runs and downgrade head responses back to the
caller's version afterwards, walking the version list newest -> oldest.
"""

from __future__ import annotations

import copy
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from starlette.requests import Request

type JSONObject = dict[str, Any]
type Transform = Callable[[JSONObject], JSONObject]
type ResolutionPolicy = Literal["strict", "waterfall"]
type VersionSource = Literal["header", "oauth_app", "account_pin", "account_first_request"]

VERSION_HEADER: Final = "Stripe-Version"
ROLLBACK_WINDOW: Final = timedelta(hours=72)
_VERSION_RE: Final = re.compile(r"\A(\d{4}-\d{2}-\d{2})(?:\.([a-z]+))?\Z")


class VersioningError(ValueError):
    """Base class for every version resolution or registry error."""


class UnknownVersionError(VersioningError):
    """The requested version is malformed or is not a released version."""


class RegistryError(VersioningError):
    """The version list violates an invariant of the engine."""


@dataclass(frozen=True, order=True, slots=True)
class ApiVersion:
    """A dated version such as `2019-10-17`, optionally on a named line (`2024-09-30.acacia`)."""

    released: date
    line: str | None = field(default=None, compare=False)

    @classmethod
    def parse(cls, text: str) -> ApiVersion:
        match = _VERSION_RE.fullmatch(text)
        if match is None:
            raise UnknownVersionError(f"malformed API version {text!r}")
        try:
            released = date.fromisoformat(match.group(1))
        except ValueError as exc:
            raise UnknownVersionError(f"malformed API version {text!r}") from exc
        return cls(released, match.group(2))

    def __str__(self) -> str:
        base = self.released.isoformat()
        return base if self.line is None else f"{base}.{self.line}"


@dataclass(frozen=True, slots=True)
class VersionChange:
    """One backwards-incompatible change, scoped to one resource type (`object` value).

    `downgrade_response` turns the resource as it looks just after the change into the
    shape it had just before it; `upgrade_request` does the reverse for request params.
    A change with side effects carries no transform: handler code asks `is_applied`.
    """

    name: str
    description: str
    resource: str
    downgrade_response: Transform | None = None
    upgrade_request: Transform | None = None
    has_side_effects: bool = False

    def __post_init__(self) -> None:
        has_transform = self.downgrade_response is not None or self.upgrade_request is not None
        if self.has_side_effects and has_transform:
            raise RegistryError(f"{self.name}: a side-effect change must not define transforms")
        if not self.has_side_effects and not has_transform:
            raise RegistryError(f"{self.name}: a change needs a transform or has_side_effects")


@dataclass(frozen=True, slots=True)
class DatedVersion:
    version: ApiVersion
    changes: tuple[VersionChange, ...]


def _walk(node: Any, resource: str, transform: Transform) -> Any:
    """Apply `transform` to every JSON object whose `object` field equals `resource`."""
    if isinstance(node, list):
        items = cast("list[object]", node)
        return [_walk(item, resource, transform) for item in items]
    if isinstance(node, dict):
        source = cast("JSONObject", node)
        obj: JSONObject = {key: _walk(value, resource, transform) for key, value in source.items()}
        return transform(obj) if obj.get("object") == resource else obj
    return node


class VersionRegistry:
    """The master list of dated versions, oldest first, with the transform walk."""

    def __init__(self, versions: Sequence[tuple[str, Sequence[VersionChange]]]) -> None:
        dated = [DatedVersion(ApiVersion.parse(text), tuple(chs)) for text, chs in versions]
        if not dated:
            raise RegistryError("at least one version is required")
        for older, newer in pairwise(dated):
            if newer.version <= older.version:
                raise RegistryError(f"versions must strictly increase: {older.version}")
        if dated[0].changes:
            raise RegistryError("the oldest version is the base and cannot carry changes")
        self._changed_at: dict[str, ApiVersion] = {}
        for entry in dated:
            for change in entry.changes:
                if change.name in self._changed_at:
                    raise RegistryError(f"change {change.name} is registered twice")
                self._changed_at[change.name] = entry.version
        self._dated: tuple[DatedVersion, ...] = tuple(dated)
        self._by_text: dict[str, ApiVersion] = {str(d.version): d.version for d in dated}

    @property
    def latest(self) -> ApiVersion:
        return self._dated[-1].version

    @property
    def versions(self) -> tuple[ApiVersion, ...]:
        return tuple(d.version for d in self._dated)

    def resolve(self, text: str, policy: ResolutionPolicy = "strict") -> ApiVersion:
        """Map a client-supplied identifier to a released version.

        `strict` accepts only released identifiers; `waterfall` rounds an unreleased date
        down to the closest older release (Cadwyn's behavior for date versions).
        """
        exact = self._by_text.get(text)
        if exact is not None:
            return exact
        requested = ApiVersion.parse(text)
        if policy == "strict":
            raise UnknownVersionError(f"unknown API version {text!r}")
        index = bisect_right(self.versions, requested)
        if index == 0:
            raise UnknownVersionError(f"{text!r} predates the oldest supported version")
        return self._dated[index - 1].version

    def is_applied(self, change_name: str, version: ApiVersion) -> bool:
        """True when `version` already includes the change (the new behavior applies)."""
        try:
            return self._changed_at[change_name] <= version
        except KeyError:
            raise RegistryError(f"change {change_name} was never registered") from None

    def downgrade(self, body: JSONObject, target: ApiVersion) -> JSONObject:
        """Render a head-shaped body at `target`: newest version first, stop at target."""
        out: Any = body  # _walk rebuilds every dict and list, so `body` is never mutated
        for entry in reversed(self._dated):
            if entry.version <= target:
                break
            for change in entry.changes:
                if change.downgrade_response is not None:
                    out = _walk(out, change.resource, change.downgrade_response)
        result: JSONObject = out
        return result

    def upgrade_request(self, resource: str, params: JSONObject, source: ApiVersion) -> JSONObject:
        """Lift request params sent at `source` to head: oldest newer version first."""
        out = params  # transforms return new dicts (see _rename); none mutate in place
        for entry in self._dated:
            if entry.version <= source:
                continue
            for change in entry.changes:
                if change.resource == resource and change.upgrade_request is not None:
                    out = change.upgrade_request(out)
        return out

    def changelog(self) -> list[str]:
        """Generate the public changelog from the change modules, newest first."""
        return [
            f"{entry.version} {change.name}: {change.description}"
            for entry in reversed(self._dated)
            for change in entry.changes
        ]


@dataclass(slots=True)
class Account:
    """Per-account version state; `pinned` is set by the account's first request."""

    id: str
    pinned: ApiVersion | None = None
    previous: ApiVersion | None = None
    upgraded_at: datetime | None = None

    def upgrade(self, target: ApiVersion, now: datetime) -> None:
        if self.pinned is None or target <= self.pinned:
            raise VersioningError("an upgrade must move a pinned account to a newer version")
        self.previous, self.pinned, self.upgraded_at = self.pinned, target, now

    def rollback(self, now: datetime) -> None:
        if self.previous is None or self.upgraded_at is None:
            raise VersioningError("no upgrade to roll back")
        if now - self.upgraded_at > ROLLBACK_WINDOW:
            raise VersioningError("the rollback window has closed")
        self.pinned, self.previous, self.upgraded_at = self.previous, None, None


@dataclass(frozen=True, slots=True)
class Credential:
    """An API key; OAuth app keys carry the version the app integrated against."""

    account_id: str
    oauth_app_version: str | None = None


def header_values(raw: Sequence[tuple[bytes, bytes]], name: str) -> list[str]:
    """Case-insensitive lookup: ASGI says request header names *should* be lowercased."""
    key = name.lower().encode("latin-1")
    return [value.decode("latin-1") for item, value in raw if item.lower() == key]


def select_version(
    registry: VersionRegistry,
    account: Account,
    header_values: Sequence[str],
    credential: Credential,
    policy: ResolutionPolicy = "strict",
) -> tuple[ApiVersion, VersionSource]:
    """Header, then OAuth app version, then the account pin (set on first request)."""
    if len(header_values) > 1:
        raise UnknownVersionError(f"{VERSION_HEADER} must appear at most once")
    if header_values:
        return registry.resolve(header_values[0].strip(), policy), "header"
    if credential.oauth_app_version is not None:
        return registry.resolve(credential.oauth_app_version), "oauth_app"
    if account.pinned is None:
        account.pinned = registry.latest
        return account.pinned, "account_first_request"
    return account.pinned, "account_pin"


def render_event(
    registry: VersionRegistry,
    event: JSONObject,
    account: Account,
    endpoint_version: ApiVersion | None,
) -> JSONObject:
    """Render a webhook event at the endpoint's own version, else the account's pin."""
    target = endpoint_version or account.pinned or registry.latest
    return registry.downgrade(event, target)


def _collapse_event_request(event: JSONObject) -> JSONObject:
    request: Any = event.get("request")
    if isinstance(request, dict):
        nested = cast("JSONObject", request)
        return {**event, "request": nested.get("id")}
    return event


def _rename(obj: JSONObject, old: str, new: str) -> JSONObject:
    if old not in obj:
        return obj
    return {(new if key == old else key): value for key, value in obj.items()}


COLLAPSE_EVENT_REQUEST: Final = VersionChange(
    name="collapse_event_request",
    description="Event `request` is an object with `id` and `idempotency_key`, not a string.",
    resource="event",
    downgrade_response=_collapse_event_request,
)
RENAME_CUSTOMER_ACCOUNT_BALANCE: Final = VersionChange(
    name="rename_customer_account_balance",
    description="Customer `account_balance` is renamed to `balance`.",
    resource="customer",
    downgrade_response=lambda obj: _rename(obj, "balance", "account_balance"),
    upgrade_request=lambda params: _rename(params, "account_balance", "balance"),
)
CHARGE_REFUNDS_NOT_EXPANDED: Final = VersionChange(
    name="charge_refunds_not_expanded",
    description="Charge objects no longer auto-expand `refunds`.",
    resource="charge",
    has_side_effects=True,
)


def build_registry() -> VersionRegistry:
    """Three dated changes on top of a base version, modeled on documented Stripe changes."""
    return VersionRegistry(
        [
            ("2017-02-14", []),
            ("2017-05-25", [COLLAPSE_EVENT_REQUEST]),
            ("2019-10-17", [RENAME_CUSTOMER_ACCOUNT_BALANCE]),
            ("2022-11-15", [CHARGE_REFUNDS_NOT_EXPANDED]),
        ]
    )


def synthetic_chain(changes: int, width: int = 20) -> tuple[VersionRegistry, JSONObject]:
    """A registry of `changes` monthly renames `f{n}` -> ... -> `f0` and a head body."""

    def rename_step(index: int) -> VersionChange:
        return VersionChange(
            name=f"rename_f{index}_to_f{index + 1}",
            description="benchmark rename",
            resource="widget",
            downgrade_response=lambda obj: _rename(obj, f"f{index + 1}", f"f{index}"),
        )

    start = date(2011, 1, 1)
    versions: list[tuple[str, Sequence[VersionChange]]] = [(start.isoformat(), [])]
    versions += [
        ((start + timedelta(days=30 * (i + 1))).isoformat(), [rename_step(i)])
        for i in range(changes)
    ]
    body: JSONObject = {"object": "widget", f"f{changes}": 1}
    body.update({f"x{j}": j for j in range(width)})
    return VersionRegistry(versions), body


@dataclass(slots=True)
class LabState:
    """In-memory accounts, credentials, and head-shaped records for the ASGI app."""

    registry: VersionRegistry = field(default_factory=build_registry)
    accounts: dict[str, Account] = field(default_factory=dict[str, Account])
    credentials: dict[str, Credential] = field(default_factory=dict[str, Credential])
    customers: dict[str, JSONObject] = field(default_factory=dict[str, JSONObject])
    charges: dict[str, JSONObject] = field(default_factory=dict[str, JSONObject])
    refunds: dict[str, JSONObject] = field(default_factory=dict[str, JSONObject])
    events: dict[str, JSONObject] = field(default_factory=dict[str, JSONObject])
    policy: ResolutionPolicy = "strict"
    handler_inputs: list[JSONObject] = field(default_factory=list[JSONObject])


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message}}, status_code=status)


def build_app(state: LabState) -> Starlette:
    """A Starlette app whose handlers speak only the head schema."""

    def versioned(
        handler: Callable[[Request, ApiVersion, JSONObject], tuple[int, JSONObject]],
        resource: str | None = None,
    ) -> Callable[[Request], Any]:
        async def endpoint(request: Request) -> JSONResponse:
            # Both lookups fold case themselves: Starlette compares raw scope names as given.
            authorization = header_values(request.headers.raw, "Authorization")
            token = authorization[0].removeprefix("Bearer ") if authorization else ""
            credential = state.credentials.get(token)
            if credential is None:
                return _error(401, "invalid API key")
            account = state.accounts.setdefault(
                credential.account_id, Account(credential.account_id)
            )
            try:
                version, _source = select_version(
                    state.registry,
                    account,
                    header_values(request.headers.raw, VERSION_HEADER),
                    credential,
                    state.policy,
                )
            except VersioningError as exc:
                return _error(400, str(exc))
            params: JSONObject = {}
            if request.method == "POST":
                try:
                    raw: Any = await request.json()
                except ValueError:
                    return _error(400, "request body must be JSON")
                if not isinstance(raw, dict):
                    return _error(400, "request body must be a JSON object")
                params = cast("JSONObject", raw)
            if resource is not None:
                params = state.registry.upgrade_request(resource, params, version)
            status, body = handler(request, version, params)
            # Error bodies are downgraded too: an embedded resource must match the caller.
            response = JSONResponse(state.registry.downgrade(body, version), status_code=status)
            response.headers[VERSION_HEADER] = str(version)
            response.headers["Vary"] = VERSION_HEADER
            return response

        return endpoint

    def create_customer(
        request: Request, version: ApiVersion, params: JSONObject
    ) -> tuple[int, JSONObject]:
        state.handler_inputs.append(params)
        unknown = sorted(set(params) - {"id", "balance"})
        if unknown:
            return 400, {"error": {"message": f"unknown parameter: {unknown[0]}"}}
        balance: Any = params.get("balance", 0)
        if not isinstance(balance, int) or isinstance(balance, bool):
            return 400, {"error": {"message": "balance must be an integer"}}
        customer_id: Any = params.get("id", f"cus_{len(state.customers) + 1}")
        if not isinstance(customer_id, str):
            return 400, {"error": {"message": "id must be a string"}}
        existing = state.customers.get(customer_id)
        if existing is not None:
            return 409, {"error": {"message": "customer exists", "customer": existing}}
        customer: JSONObject = {"id": customer_id, "object": "customer", "balance": balance}
        state.customers[customer_id] = customer
        return 200, customer

    def retrieve_charge(
        request: Request, version: ApiVersion, params: JSONObject
    ) -> tuple[int, JSONObject]:
        charge = state.charges.get(request.path_params["charge_id"])
        if charge is None:
            return 404, {"error": {"message": "no such charge"}}
        body = copy.deepcopy(charge)
        if not state.registry.is_applied(CHARGE_REFUNDS_NOT_EXPANDED.name, version):
            refund_ids: list[str] = body["refunds"]
            body["refunds"] = {
                "object": "list",
                "data": [state.refunds[refund_id] for refund_id in refund_ids],
            }
        return 200, body

    def retrieve_event(
        request: Request, version: ApiVersion, params: JSONObject
    ) -> tuple[int, JSONObject]:
        event = state.events.get(request.path_params["event_id"])
        if event is None:
            return 404, {"error": {"message": "no such event"}}
        return 200, event

    return Starlette(
        routes=[
            Route("/v1/customers", versioned(create_customer, "customer"), methods=["POST"]),
            Route("/v1/charges/{charge_id}", versioned(retrieve_charge), methods=["GET"]),
            Route("/v1/events/{event_id}", versioned(retrieve_event), methods=["GET"]),
        ]
    )


def seeded_state(policy: ResolutionPolicy = "strict") -> LabState:
    """Deterministic fixtures: one direct key, one OAuth app key, one charge, one event."""
    state = LabState(policy=policy)
    state.credentials["sk_acct_1"] = Credential("acct_1")
    state.credentials["sk_app_on_acct_1"] = Credential("acct_1", oauth_app_version="2017-05-25")
    state.refunds["re_1"] = {"id": "re_1", "object": "refund", "amount": 500}
    state.charges["ch_1"] = {"id": "ch_1", "object": "charge", "amount": 2000, "refunds": ["re_1"]}
    state.events["evt_1"] = {
        "id": "evt_1",
        "object": "event",
        "type": "customer.created",
        "request": {"id": "req_1", "idempotency_key": "ik_1"},
        "data": {"object": {"id": "cus_9", "object": "customer", "balance": -300}},
    }
    return state


def pinned_accounts(state: LabState) -> Mapping[str, str | None]:
    """Snapshot of each account's pin, for assertions."""
    return {
        acct_id: None if acct.pinned is None else str(acct.pinned)
        for acct_id, acct in state.accounts.items()
    }
