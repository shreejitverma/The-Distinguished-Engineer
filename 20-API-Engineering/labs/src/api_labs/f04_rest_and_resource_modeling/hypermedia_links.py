"""RFC 8288 Web Linking, a Level 3 order state machine, and a link-following client.

The server advertises every legal state transition of an order as a typed link, in three
serialisations at once: the RFC 8288 `Link` header field, HAL `_links`, and JSON:API `links`.
The client starts from one entry URI and never builds a URL; it selects links by relation type.
"""

from __future__ import annotations

import enum
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import unquote_to_bytes, urljoin, urlsplit

from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    import httpx
    from starlette.requests import Request

# Extension relation types (RFC 8288 Section 2.1.2): absolute, all-lowercase URIs whose
# definitions fix the method a client uses on the target (RFC 8288 Section 2.1.1).
REL_BASE: Final = "https://api.example.com/rels/"
REL_ORDERS: Final = REL_BASE + "orders"  # POST {"sku", "quantity"} -> 201 Created
REL_PAY: Final = REL_BASE + "pay"  # POST, empty body -> 200 or 409
REL_CANCEL: Final = REL_BASE + "cancel"
REL_SHIP: Final = REL_BASE + "ship"
REL_DELIVER: Final = REL_BASE + "deliver"

HAL_JSON: Final = "application/hal+json"
JSON_API: Final = "application/vnd.api+json"
PROBLEM_JSON: Final = "application/problem+json"

_OWS: Final = " \t"
_RWS_RE: Final = re.compile(
    r"[ \t]+"
)  # RFC 9110 Section 5.6.3; str.split() would also split on NBSP
_TOKEN_RE: Final = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # RFC 9110 Section 5.6.2
_REG_REL_RE: Final = re.compile(r"[a-z][a-z0-9.\-]*")  # RFC 8288 Section 3.3 reg-rel-type
_SINGLETON_ATTRS: Final = frozenset({"media", "title", "title*", "type"})


class LinkSyntaxError(ValueError):
    """A link cannot be serialised without breaking the Link field grammar."""


class HypermediaError(RuntimeError):
    """The client cannot make progress using only server-provided links."""


@dataclass(frozen=True, slots=True)
class Link:
    """One link: context has a `rel` resource at `target` (RFC 8288 Section 2)."""

    target: str
    rel: str
    context: str | None
    attributes: tuple[tuple[str, str], ...] = ()

    def attr(self, name: str) -> str | None:
        return next((value for key, value in self.attributes if key == name), None)


# ---------------------------------------------------------------------------------------------
# Parsing: RFC 8288 Appendix B, with verified erratum 5878 applied to steps 15 and 16.
# ---------------------------------------------------------------------------------------------


def _skip_ows(s: str, i: int) -> int:
    while i < len(s) and s[i] in _OWS:
        i += 1
    return i


def _parse_quoted_string(s: str, i: int) -> tuple[str, int]:
    """RFC 8288 Appendix B.4; `s[i]` is the opening DQUOTE."""
    out: list[str] = []
    i += 1
    while i < len(s):
        ch = s[i]
        if ch == "\\":
            if i + 1 >= len(s):
                return "".join(out), i + 1
            out.append(s[i + 1])
            i += 2
        elif ch == '"':
            return "".join(out), i + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out), i


def decode_ext_value(value: str) -> str | None:
    """RFC 8187 Section 3.2.1 ext-value; only UTF-8 is defined, so anything else is dropped."""
    parts = value.split("'", 2)
    if len(parts) != 3 or parts[0].lower() != "utf-8":
        return None
    try:
        return unquote_to_bytes(parts[2]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def _parse_parameters(s: str, i: int) -> tuple[list[tuple[str, str]], int]:
    """RFC 8288 Appendix B.3; returns the parameters and the index after the link-value."""
    params: list[tuple[str, str]] = []
    while i < len(s):
        i = _skip_ows(s, i)
        if i >= len(s) or s[i] != ";":
            break
        i = _skip_ows(s, i + 1)
        start = i
        while i < len(s) and s[i] not in " \t=;,":
            i += 1
        name = s[start:i].lower()
        i = _skip_ows(s, i)
        value = ""
        if i < len(s) and s[i] == "=":
            i = _skip_ows(s, i + 1)
            if i < len(s) and s[i] == '"':
                value, i = _parse_quoted_string(s, i)
            else:
                start = i
                while i < len(s) and s[i] not in ";,":
                    i += 1
                value = s[start:i].rstrip(_OWS)
            if name.endswith("*"):
                decoded = decode_ext_value(value)
                if decoded is None:
                    continue  # B.3 step 7.5: keep processing after an unrecoverable decode error
                value = decoded
        params.append((name, value))
        i = _skip_ows(s, i)
        if i >= len(s) or s[i] == ",":
            break
    # B.3 step 12 stops *at* the comma; consume it so B.2 step 2 sees the next "<".
    if i < len(s) and s[i] == ",":
        i += 1
    return params, i


def _first(params: Sequence[tuple[str, str]], name: str) -> str | None:
    return next((value for key, value in params if key == name), None)


def parse_link_value(field_value: str, *, base: str, context: str | None) -> list[Link]:
    """RFC 8288 Appendix B.2: parse one Link field value into links.

    `base` resolves relative references (RFC 8288 Section 3.1); `context` is the URL the
    representation identifies (RFC 9110 Section 6.4.2), or None when it is anonymous.
    """
    links: list[Link] = []
    s, i = field_value, 0
    while i < len(s):
        i = _skip_ows(s, i)
        if i >= len(s) or s[i] != "<":
            return links
        end = s.find(">", i + 1)
        if end == -1:
            return links
        target = urljoin(base, s[i + 1 : end])
        params, i = _parse_parameters(s, end + 1)
        anchor = _first(params, "anchor")
        link_context = urljoin(base, anchor) if anchor is not None else context
        attrs: list[tuple[str, str]] = []
        for name, value in params:
            if name in ("rel", "anchor"):
                continue
            if name in _SINGLETON_ATTRS and any(key == name for key, _ in attrs):
                continue
            attrs.append((name, value))
        for star in sorted({key for key, _ in attrs if key.endswith("*")}):
            plain = star[:-1]
            attrs = [(plain if k == star else k, v) for k, v in attrs if k != plain]
        for rel in _RWS_RE.split((_first(params, "rel") or "").strip(_OWS)):
            if not rel:
                continue
            links.append(Link(target, rel.lower(), link_context, tuple(attrs)))
    return links


def parse_link_header(field_values: Iterable[str], *, base: str, context: str | None) -> list[Link]:
    """RFC 8288 Appendix B.1: every Link field line, in order."""
    return [
        link
        for value in field_values
        for link in parse_link_value(value, base=base, context=context)
    ]


def representation_url(
    method: str, status: int, target_uri: str, content_location: str | None
) -> str | None:
    """Which resource the response content represents (RFC 9110 Section 6.4.2 rules 2-7).

    HEAD is treated like GET because its header fields describe the GET representation
    (RFC 9110 Section 9.3.2). None means the link context is anonymous (RFC 8288 Section 3.2).
    """
    if method in ("GET", "HEAD") and status in (200, 203, 206):
        return target_uri
    if content_location is not None:
        return urljoin(target_uri, content_location)
    return None


def links_from_response(response: httpx.Response) -> list[Link]:
    request = response.request
    target = str(request.url)
    context = representation_url(
        request.method, response.status_code, target, response.headers.get("content-location")
    )
    return parse_link_header(response.headers.get_list("link"), base=target, context=context)


# ---------------------------------------------------------------------------------------------
# Serialisation: RFC 8288 Section 3 with RFC 9110 Section 5.6.2 tokens and 5.6.4 quoting.
# ---------------------------------------------------------------------------------------------


def _check_header_safe(text: str, what: str) -> None:
    """Reject CR, LF, and other controls (header injection) and non-ASCII (IRIs must be
    converted to URIs for the Link field, RFC 8288 Section 6; titles use title*)."""
    if not text.isascii() or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise LinkSyntaxError(f"control or non-ASCII character in {what}: {text!r}")


def _quote(value: str) -> str:
    _check_header_safe(value, "parameter value")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def format_link(
    target: str,
    rels: Sequence[str],
    params: Sequence[tuple[str, str]] = (),
    *,
    anchor: str | None = None,
) -> str:
    """Serialise one link-value; rel is always quoted because URIs contain ':' and '/'.

    `anchor` is a keyword of its own so that a generic parameter list can never smuggle one in.
    """
    _check_header_safe(target, "target")
    if not rels or any(ch in target for ch in "<> "):
        raise LinkSyntaxError(f"cannot serialise target {target!r} with rels {rels!r}")
    for rel in rels:
        absolute = urlsplit(rel).scheme != ""
        if not (_REG_REL_RE.fullmatch(rel) or (absolute and rel == rel.lower() and " " not in rel)):
            raise LinkSyntaxError(f"not a reg-rel-type or lowercase absolute URI: {rel!r}")
    parts = [f"<{target}>", f"rel={_quote(' '.join(rels))}"]
    if anchor is not None:
        parts.append(f"anchor={_quote(anchor)}")
    for name, value in params:
        if not _TOKEN_RE.fullmatch(name) or name.lower() in ("rel", "anchor"):
            raise LinkSyntaxError(f"bad link-param name {name!r}")
        parts.append(f"{name}={_quote(value)}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------------------------
# The Level 3 server: an order state machine whose transitions are links.
# ---------------------------------------------------------------------------------------------


class OrderState(enum.StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


TRANSITIONS: Final[Mapping[OrderState, Mapping[str, OrderState]]] = {
    OrderState.PENDING_PAYMENT: {REL_PAY: OrderState.PAID, REL_CANCEL: OrderState.CANCELLED},
    OrderState.PAID: {REL_SHIP: OrderState.SHIPPED, REL_CANCEL: OrderState.CANCELLED},
    OrderState.SHIPPED: {REL_DELIVER: OrderState.DELIVERED},
    OrderState.DELIVERED: {},
    OrderState.CANCELLED: {},
}
ACTION_NAMES: Final[Mapping[str, str]] = {
    rel: rel.removeprefix(REL_BASE) for rel in (REL_PAY, REL_CANCEL, REL_SHIP, REL_DELIVER)
}


@dataclass(frozen=True, slots=True)
class UrlLayout:
    """The server's private namespace; clients must never depend on it (Fielding 2008)."""

    root: str
    collection: str
    item: str  # Starlette path template with {order_id}
    action: str  # Starlette path template with {order_id} and {action}
    rpc: str  # the Level 0 tunnel endpoint

    def order_url(self, order_id: str) -> str:
        return self.item.format(order_id=order_id)

    def action_url(self, order_id: str, rel: str) -> str:
        return self.action.format(order_id=order_id, action=ACTION_NAMES[rel])


LAYOUT_V1: Final = UrlLayout(
    "/", "/orders", "/orders/{order_id}", "/orders/{order_id}/{action}", "/rpc"
)
LAYOUT_V2: Final = UrlLayout(
    "/",
    "/api/v2/po",
    "/api/v2/po/{order_id}",
    "/api/v2/po/{order_id}/transitions/{action}",
    "/api/v2/rpc",
)


@dataclass
class Order:
    order_id: str
    sku: str
    quantity: int
    state: OrderState = OrderState.PENDING_PAYMENT


@dataclass
class OrderStore:
    orders: dict[str, Order] = field(default_factory=dict[str, Order])

    def create(self, sku: str, quantity: int) -> Order:
        order = Order(f"o{len(self.orders) + 1}", sku, quantity)
        self.orders[order.order_id] = order
        return order

    def apply(self, order: Order, rel: str) -> bool:
        """Apply the transition named by `rel` if the current state allows it."""
        next_state = TRANSITIONS[order.state].get(rel)
        if next_state is None:
            return False
        order.state = next_state
        return True


def order_links(layout: UrlLayout, order: Order) -> list[tuple[str, str]]:
    """(rel, target) pairs for an order: exactly the transitions legal in its current state."""
    url = layout.order_url(order.order_id)
    pairs = [("self", url), ("collection", layout.collection)]
    pairs += [(rel, layout.action_url(order.order_id, rel)) for rel in TRANSITIONS[order.state]]
    return pairs


def hal_document(order: Order, pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """HAL resource object (draft-kelly-json-hal-11 Sections 4 and 5)."""
    return {
        "_links": {rel: {"href": target} for rel, target in pairs},
        "id": order.order_id,
        "sku": order.sku,
        "quantity": order.quantity,
        "state": order.state.value,
    }


def jsonapi_document(order: Order, pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """JSON:API 1.1 resource links; URI rels need a link object because ':' and '/' are
    reserved in member names, so the member name is a short alias and `rel` carries the URI."""
    links: dict[str, Any] = {}
    for rel, target in pairs:
        if rel.startswith(REL_BASE):
            links[rel.removeprefix(REL_BASE)] = {"href": target, "rel": rel}
        else:
            links[rel] = target
    attributes = {"sku": order.sku, "quantity": order.quantity, "state": order.state.value}
    return {
        "data": {"type": "orders", "id": order.order_id, "attributes": attributes, "links": links}
    }


def links_from_hal(doc: Mapping[str, Any], *, base: str) -> set[tuple[str, str]]:
    raw: dict[str, Any] = doc["_links"]
    return {(rel, urljoin(base, obj["href"])) for rel, obj in raw.items()}


def links_from_jsonapi(doc: Mapping[str, Any], *, base: str) -> set[tuple[str, str]]:
    raw: dict[str, Any] = doc["data"]["links"]
    out: set[tuple[str, str]] = set()
    for name, value in raw.items():
        if isinstance(value, str):
            out.add((name, urljoin(base, value)))
        else:
            out.add((str(value.get("rel", name)), urljoin(base, str(value["href"]))))
    return out


def _problem(status: int, title: str, detail: str, self_url: str | None = None) -> Response:
    """RFC 9457 problem details plus a recovery link to the resource.

    A 4xx response does not represent the target resource, so its link context is anonymous
    (RFC 9110 Section 6.4.2 rule 7, RFC 8288 Section 3.2); `anchor` names the context explicitly.
    """
    body = json.dumps({"title": title, "status": status, "detail": detail})
    response = Response(body, status_code=status, media_type=PROBLEM_JSON)
    if self_url is not None:
        response.headers.append("link", format_link(self_url, ["self"], anchor=self_url))
    return response


def build_order_app(layout: UrlLayout = LAYOUT_V1, store: OrderStore | None = None) -> Starlette:
    """A Level 3 order API whose URL layout can change without breaking link-driven clients."""
    orders = store if store is not None else OrderStore()

    def represent(request: Request, order: Order, status: int) -> Response:
        pairs = order_links(layout, order)
        if JSON_API in request.headers.get("accept", ""):
            body, media_type = jsonapi_document(order, pairs), JSON_API
        else:
            body, media_type = hal_document(order, pairs), HAL_JSON
        response = Response(json.dumps(body), status_code=status, media_type=media_type)
        for rel, target in pairs:
            response.headers.append("link", format_link(target, [rel]))
        url = layout.order_url(order.order_id)
        if request.method != "GET":
            # Without Content-Location the content of a POST response is unidentified and the
            # Link context is anonymous (RFC 9110 Section 6.4.2 rule 7, RFC 8288 Section 3.2).
            response.headers["content-location"] = url
        if status == 201:
            response.headers["location"] = url
        response.headers["vary"] = "Accept"
        return response

    async def root(request: Request) -> Response:
        body = {"_links": {"self": {"href": layout.root}, REL_ORDERS: {"href": layout.collection}}}
        response = Response(json.dumps(body), media_type=HAL_JSON)
        response.headers.append("link", format_link(layout.root, ["self"]))
        response.headers.append("link", format_link(layout.collection, [REL_ORDERS]))
        return response

    async def create_order(request: Request) -> Response:
        payload: Any = await request.json()
        sku, quantity = payload.get("sku"), payload.get("quantity")
        if not isinstance(sku, str) or not isinstance(quantity, int) or quantity < 1:
            return _problem(422, "Unprocessable Content", "sku must be a string, quantity >= 1")
        return represent(request, orders.create(sku, quantity), 201)

    async def get_order(request: Request) -> Response:
        order = orders.orders.get(request.path_params["order_id"])
        if order is None:
            return _problem(404, "Not Found", "no such order")
        return represent(request, order, 200)

    async def transition(request: Request) -> Response:
        order = orders.orders.get(request.path_params["order_id"])
        rel = REL_BASE + request.path_params["action"]
        if order is None or rel not in ACTION_NAMES:
            return _problem(404, "Not Found", "no such order or transition")
        if not orders.apply(order, rel):
            detail = f"{ACTION_NAMES[rel]} is not available in state {order.state.value}"
            return _problem(409, "Conflict", detail, layout.order_url(order.order_id))
        return represent(request, order, 200)

    async def rpc(request: Request) -> Response:
        """Level 0: one URI, one method, success or failure only inside the body."""
        payload: Any = await request.json()
        order = orders.orders.get(str(payload.get("order_id")))
        rel = REL_BASE + str(payload.get("method"))
        if order is None or rel not in ACTION_NAMES or not orders.apply(order, rel):
            return Response(
                json.dumps({"error": "operation failed"}), media_type="application/json"
            )
        return Response(json.dumps({"result": order.state.value}), media_type="application/json")

    return Starlette(
        routes=[
            Route(layout.root, root, methods=["GET"]),
            Route(layout.collection, create_order, methods=["POST"]),
            Route(layout.item, get_order, methods=["GET"]),
            Route(layout.action, transition, methods=["POST"]),
            Route(layout.rpc, rpc, methods=["POST"]),
        ]
    )


# ---------------------------------------------------------------------------------------------
# The client: knows one entry URI and the relation vocabulary, never the URL layout.
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Representation:
    url: str | None
    status: int
    links: tuple[Link, ...]
    body: dict[str, Any]

    def rels(self) -> set[str]:
        if self.url is None:
            return set()
        return {link.rel for link in self.links if link.context == self.url}


def _origin(url: str) -> tuple[str, str | None, int | None]:
    """(scheme, host, port) with the default port filled in, so `:443` equals no port."""
    parts = urlsplit(url)
    port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    return parts.scheme, parts.hostname, port


class HypermediaClient:
    """Drives application state only through links found in received representations."""

    def __init__(self, http: httpx.AsyncClient, entry_point: str) -> None:
        self._http = http
        self._entry = str(http.base_url.join(entry_point))

    async def _send(
        self, method: str, url: str, body: dict[str, Any] | None = None
    ) -> Representation:
        response = await self._http.request(method, url, json=body)
        links = links_from_response(response)
        context = representation_url(
            method,
            response.status_code,
            str(response.request.url),
            response.headers.get("content-location"),
        )
        if response.is_success and context is None:
            raise HypermediaError(f"{method} {url}: anonymous link context, links are unusable")
        trusted = tuple(
            link
            for link in links
            if _origin(link.target) == _origin(self._entry)
            and (link.context is None or _origin(link.context) == _origin(self._entry))
        )
        return Representation(context, response.status_code, trusted, response.json())

    def follow(self, rep: Representation, rel: str) -> str:
        for link in rep.links:
            if rep.url is not None and link.rel == rel.lower() and link.context == rep.url:
                return link.target
        raise HypermediaError(f"no {rel!r} link from {rep.url}; available: {sorted(rep.rels())}")

    async def start(self) -> Representation:
        return await self._send("GET", self._entry)

    async def place_order(self, sku: str, quantity: int) -> Representation:
        home = await self.start()
        return await self._send(
            "POST", self.follow(home, REL_ORDERS), {"sku": sku, "quantity": quantity}
        )

    async def transition(self, rep: Representation, rel: str) -> Representation:
        return await self._send("POST", self.follow(rep, rel))

    async def refresh(self, rep: Representation) -> Representation:
        return await self._send("GET", self.follow(rep, "self"))


class HardcodedClient:
    """The Level 2 habit: URL templates copied from documentation into client code."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def place_and_pay(self, sku: str, quantity: int) -> int:
        created = await self._http.post("/orders", json={"sku": sku, "quantity": quantity})
        if created.status_code != 201:
            return created.status_code
        order_id = created.json()["id"]
        paid = await self._http.post(f"/orders/{order_id}/pay")
        return paid.status_code
