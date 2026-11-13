"""Executable subset of the Zalando RESTful API and Event Guidelines.

Rules 118 (snake_case), 167 (I-JSON), 173 (Money), 176 and 177 (Problem JSON),
233 (X-Flow-ID), 149 and 228 (idempotent PUT, URL-friendly identifiers), and
211, 213, 214, 242, 247 (event names, metadata, eid, ordering).
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from typing import TYPE_CHECKING, Final, Literal

from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.responses import Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

type Json = dict[str, Json] | list[Json] | str | int | Decimal | bool | None

SNAKE_CASE: Final = re.compile(r"[a-z_][a-z_0-9]*")
PROBLEM_MEDIA_TYPE: Final = "application/problem+json"
FLOW_ID_HEADER: Final = "X-Flow-ID"
FLOW_ID_MAX_LENGTH: Final = 128
MONEY_MAX_DIGITS: Final = 64
NUMBER_MAX_EXPONENT: Final = 999_999_999_999_999_999
MAX_BODY_BYTES: Final = 4096
MAX_REPORTED_NAMES: Final = 5
EID_NAMESPACE: Final = uuid.uuid5(uuid.NAMESPACE_DNS, "sales-order.example")
EVENT_TYPE_NAME: Final = re.compile(r"[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*(\.[Vv][0-9.]+)?")
RESOURCE_ID: Final = re.compile(r"[a-zA-Z0-9:._\-]+")
_FLOW_ID_FORBIDDEN: Final = re.compile(r"[^a-zA-Z0-9/+_\-=]")
_CURRENCY: Final = re.compile(r"[A-Z]{3}")
_PLANE_END_NONCHARACTERS: Final = "".join(
    chr(plane << 16 | last) for plane in range(17) for last in (0xFFFE, 0xFFFF)
)
_NOT_I_JSON: Final = re.compile(f"[\ud800-\udfff\ufdd0-\ufdef{_PLANE_END_NONCHARACTERS}]")


def _unique_members(pairs: list[tuple[str, Json]]) -> dict[str, Json]:
    members = dict(pairs)
    if len(members) != len(pairs):
        raise ValueError("duplicate member name (RFC 7493 Section 2.3)")
    return members


def _reject_constant(token: str) -> Json:
    raise ValueError(f"{token} is not a JSON number (RFC 8259 Section 6)")


def _exact_decimal(token: str) -> Decimal:
    """Exact parse with one exponent bound, whichever decimal implementation the build has."""
    try:
        value = Decimal(token)
    except InvalidOperation:
        value = None
    if value is None or abs(value.adjusted()) > NUMBER_MAX_EXPONENT:
        raise ValueError("number exponent is out of range")
    return value


def _require_i_json_strings(document: Json) -> None:
    stack: list[Json] = [document]
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            if _NOT_I_JSON.search(value) is not None:
                raise ValueError("surrogate or noncharacter in a string (RFC 7493 Section 2.1)")
        elif isinstance(value, dict):
            stack.extend(value)
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)


def load_i_json(text: str) -> Json:
    """Parse I-JSON keeping fractional numbers exact; every rejection is a ValueError."""
    try:
        document: Json = json.loads(
            text,
            parse_float=_exact_decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_members,
        )
    except RecursionError:
        raise ValueError("JSON document is nested too deeply") from None
    _require_i_json_strings(document)
    return document


def dump_json(value: Json) -> str:
    """Serialize with sorted names and every Decimal as an exact JSON number."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("NaN and Infinity are not JSON numbers (RFC 8259 Section 6)")
        return format(value, "f")
    if isinstance(value, dict):
        members = (f"{json.dumps(name)}:{dump_json(item)}" for name, item in sorted(value.items()))
        return "{" + ",".join(members) + "}"
    if isinstance(value, list):
        return "[" + ",".join(dump_json(item) for item in value) + "]"
    return json.dumps(value)


def non_snake_case_properties(document: Json) -> list[str]:
    """Return an RFC 6901 JSON Pointer, in document order, for every name violating rule 118."""
    found: list[str] = []
    stack: list[tuple[str, str | None, Json]] = [("", None, document)]
    while stack:
        pointer, name, value = stack.pop()
        if name is not None and SNAKE_CASE.fullmatch(name) is None:
            found.append(pointer)
        if isinstance(value, dict):
            for key, item in reversed(value.items()):
                token = key.replace("~", "~0").replace("/", "~1")
                stack.append((f"{pointer}/{token}", key, item))
        elif isinstance(value, list):
            for index in range(len(value) - 1, -1, -1):
                stack.append((f"{pointer}/{index}", None, value[index]))
    return found


@dataclass(frozen=True, slots=True)
class Money:
    """Rule 173: a closed object of an exact, size-bounded amount and an ISO 4217 code."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        digits, exponent = self.amount.as_tuple()[1:]
        if not isinstance(exponent, int):
            raise ValueError("amount must be finite")
        if max(len(digits), len(digits) + exponent, -exponent) > MONEY_MAX_DIGITS:
            raise ValueError(f"amount must have at most {MONEY_MAX_DIGITS} digits")
        if _CURRENCY.fullmatch(self.currency) is None:
            raise ValueError("currency must be a three letter upper case ISO 4217 code")

    @classmethod
    def from_json(cls, value: Json) -> Money:
        if not isinstance(value, dict) or value.keys() != {"amount", "currency"}:
            raise ValueError("Money must have exactly the members amount and currency")
        amount, currency = value["amount"], value["currency"]
        if isinstance(amount, bool) or not isinstance(amount, int | Decimal):
            raise ValueError("amount must be a JSON number, not a string or boolean")
        if not isinstance(currency, str):
            raise ValueError("currency must be a JSON string")
        return cls(Decimal(amount), currency)

    def to_json(self) -> dict[str, Json]:
        return {"amount": self.amount, "currency": self.currency}


def normalize_flow_id(received: str | None, mint: Callable[[], str]) -> str:
    """Rule 233: keep the sanitized caller Flow-ID, or mint one when nothing survives."""
    cleaned = _FLOW_ID_FORBIDDEN.sub("", received or "")[:FLOW_ID_MAX_LENGTH]
    return cleaned or mint()


def _phrase(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Unknown Status"


def _text(document: dict[str, Json], name: str) -> str | None:
    value = document.get(name)
    return value if isinstance(value, str) else None


@dataclass(frozen=True, slots=True)
class Problem:
    """RFC 9457 problem details as profiled by rule 176 (relative type identifiers)."""

    status: int
    title: str
    type: str = "about:blank"
    detail: str | None = None
    instance: str | None = None

    def to_json(self) -> dict[str, Json]:
        members: dict[str, Json] = {"type": self.type, "title": self.title, "status": self.status}
        if self.detail is not None:
            members["detail"] = self.detail
        if self.instance is not None:
            members["instance"] = self.instance
        return members


def read_problem(status_code: int, content_type: str | None, body: bytes) -> Problem:
    """Client side: trust the status line, ignore mistyped members, survive non-problem bodies."""
    fallback = Problem(status_code, _phrase(status_code))
    if (content_type or "").split(";")[0].strip().lower() != PROBLEM_MEDIA_TYPE:
        return fallback
    try:
        document = load_i_json(body.decode("utf-8"))
    except ValueError:
        return fallback
    if not isinstance(document, dict):
        return fallback
    return Problem(
        status=status_code,
        title=_text(document, "title") or fallback.title,
        type=_text(document, "type") or "about:blank",
        detail=_text(document, "detail"),
        instance=_text(document, "instance"),
    )


def problem_response(
    request: Request, problem: Problem, headers: Mapping[str, str] | None = None
) -> Response:
    return Response(
        dump_json(problem.to_json()),
        status_code=problem.status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers={**(headers or {}), FLOW_ID_HEADER: request.state.flow_id},
    )


def utc_timestamp(moment: datetime) -> str:
    """Rule 169: RFC 3339 date-time in UTC with upper case T and Z."""
    if moment.tzinfo is None:
        raise ValueError("occurred_at needs an aware datetime")
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def data_change_event(
    *,
    event_type: str,
    data_type: str,
    data_op: Literal["C", "U", "D", "S"],
    data: dict[str, Json],
    occurred_at: datetime,
    flow_id: str,
) -> dict[str, Json]:
    """Rules 198, 211, 213, 247: the eid hashes the serialized change, so a retry reuses it."""
    if EVENT_TYPE_NAME.fullmatch(event_type) is None:
        raise ValueError("event type name must be <functional-name>.<event-name> (rule 213)")
    body: dict[str, Json] = {"data_type": data_type, "data_op": data_op, "data": data}
    eid = uuid.uuid5(EID_NAMESPACE, f"{event_type}\n{dump_json(body)}")
    metadata: dict[str, Json] = {
        "eid": str(eid),
        "event_type": event_type,
        "occurred_at": utc_timestamp(occurred_at),
        "flow_id": flow_id,
    }
    return {"metadata": metadata, **body}


@dataclass(slots=True)
class OrderReplica:
    """Rules 212, 214, 242: deduplicate on eid and never let an older version win."""

    orders: dict[str, dict[str, Json]] = field(default_factory=dict[str, dict[str, Json]])
    versions: dict[str, int] = field(default_factory=dict[str, int])
    seen_eids: set[str] = field(default_factory=set[str])

    def apply(self, event: Mapping[str, Json]) -> Literal["applied", "duplicate", "stale"]:
        metadata, data = event.get("metadata"), event.get("data")
        if not isinstance(metadata, dict) or not isinstance(data, dict):
            raise ValueError("not a data change event")
        eid, order_id, version = metadata.get("eid"), data.get("id"), data.get("version")
        if not (isinstance(eid, str) and isinstance(order_id, str) and isinstance(version, int)):
            raise ValueError("event lacks metadata.eid, data.id, or data.version")
        if eid in self.seen_eids:
            return "duplicate"
        self.seen_eids.add(eid)
        if version <= self.versions.get(order_id, 0):
            return "stale"
        self.versions[order_id] = version
        self.orders[order_id] = data
        return "applied"


class FlowIdMiddleware:
    """Rule 233: accept or mint the Flow-ID once and echo it on every response."""

    def __init__(self, app: ASGIApp, mint: Callable[[], str]) -> None:
        self.app = app
        self.mint = mint

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        flow_id = normalize_flow_id(Headers(scope=scope).get(FLOW_ID_HEADER), self.mint)
        scope.setdefault("state", {})["flow_id"] = flow_id

        async def send_with_flow_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[FLOW_ID_HEADER] = flow_id
            await send(message)

        await self.app(scope, receive, send_with_flow_id)


def create_app(
    *,
    mint_flow_id: Callable[[], str],
    now: Callable[[], datetime],
    publish: Callable[[dict[str, Json]], None],
    max_body_size: int | None = None,
) -> Starlette:
    """Sales order service: Money in, Money out, one data change event per state change.

    max_body_size is handed to Starlette unchanged, to show how its limit meets the handlers.
    """
    stored: dict[str, tuple[int, Money]] = {}

    async def put_sales_order(request: Request) -> Response:
        order_id: str = request.path_params["order_id"]
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                return problem_response(request, Problem(413, "Content Too Large"))
        try:
            if RESOURCE_ID.fullmatch(order_id) is None:
                raise ValueError("resource identifier must match [a-zA-Z0-9:._-]+ (rule 228)")
            document = load_i_json(body.decode("utf-8"))
            if violations := non_snake_case_properties(document):
                listed = ", ".join(violations[:MAX_REPORTED_NAMES])
                more = len(violations) - MAX_REPORTED_NAMES
                suffix = f" and {more} more" if more > 0 else ""
                raise ValueError(f"property names must be snake_case: {listed}{suffix}")
            if not isinstance(document, dict) or document.keys() != {"grand_total"}:
                raise ValueError("expected exactly one member: grand_total")
            grand_total = Money.from_json(document["grand_total"])
        except ValueError as exc:
            problem = Problem(400, "Invalid sales order", "/problems/invalid-sales-order", str(exc))
            return problem_response(request, problem)
        version, current = stored.get(order_id, (0, None))
        if current is not None and grand_total == current:
            # Rule 149: repeating a PUT changes nothing, so it publishes nothing.
            data: dict[str, Json] = {
                "id": order_id,
                "grand_total": current.to_json(),
                "version": version,
            }
            return Response(dump_json(data), media_type="application/json")
        version += 1
        data = {"id": order_id, "grand_total": grand_total.to_json(), "version": version}
        publish(
            data_change_event(
                event_type="sales-order.order-changed",
                data_type="sales_order.order",
                data_op="C" if version == 1 else "U",
                data=data,
                occurred_at=now(),
                flow_id=request.state.flow_id,
            )
        )
        stored[order_id] = (version, grand_total)
        status_code = 201 if version == 1 else 200
        return Response(dump_json(data), status_code=status_code, media_type="application/json")

    async def any_error(request: Request, exc: Exception) -> Response:
        if isinstance(exc, HTTPException):
            problem = Problem(exc.status_code, _phrase(exc.status_code))
            return problem_response(request, problem, exc.headers)
        return problem_response(request, Problem(500, _phrase(500)))

    return Starlette(
        routes=[Route("/sales-orders/{order_id}", put_sales_order, methods=["PUT"])],
        middleware=[Middleware(FlowIdMiddleware, mint=mint_flow_id)],
        exception_handlers={HTTPException: any_error, Exception: any_error},
        max_body_size=max_body_size,
    )
