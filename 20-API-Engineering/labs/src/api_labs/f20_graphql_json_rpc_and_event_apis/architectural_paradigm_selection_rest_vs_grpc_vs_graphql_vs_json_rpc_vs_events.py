"""One Order payload on five wires, a measured schema-evolution matrix, and a rule-table chooser.

The lab answers four questions with executable code instead of opinion:

* ``measure_wire`` encodes the same ``Order`` as a REST resource, a JSON-RPC response, a GraphQL
  response (executed by Strawberry), a framed gRPC message (real protobuf, no ``protoc``), and a
  CloudEvents structured event, and counts body bytes.
* ``evolution_outcome`` applies four schema changes to a writer and runs an old reader against it,
  so "how much does each paradigm hurt when the schema moves" is a table of real parse results.
* ``build_screen_app`` serves one screen's data through REST, REST with ``expand``, JSON-RPC and
  GraphQL, and ``Tracker`` counts requests and sequential round-trip depth for each loader.
* ``rank`` scores the five paradigms against ``Requirements`` with an explicit, mutable rule table.
"""

import asyncio
import gzip
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

import graphql
import httpx
import strawberry
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from google.protobuf import (  # type: ignore[import-untyped]
    descriptor_pb2,
    descriptor_pool,
    message_factory,
)
from google.protobuf.internal import api_implementation  # type: ignore[import-untyped]
from google.protobuf.message import Message  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, ValidationError
from strawberry.fastapi import GraphQLRouter
from strawberry.http import process_result
from strawberry.types import Info

# --------------------------------------------------------------------------------------------
# The shared domain payload
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderLine:
    sku: str
    quantity: int
    unit_price_cents: int


@dataclass(frozen=True, slots=True)
class Order:
    id: int
    customer_id: int
    status: str
    created_at: int
    currency: str
    lines: tuple[OrderLine, ...]


SAMPLE_ORDER = Order(
    id=100_234,
    customer_id=7_731,
    status="CONFIRMED",
    created_at=1_767_225_600,
    currency="USD",
    lines=(
        OrderLine("SKU-1001", 2, 1_999),
        OrderLine("SKU-2002", 1, 14_950),
        OrderLine("SKU-3003", 5, 325),
    ),
)

STATUS_NUMBERS: Mapping[str, int] = {
    "UNSPECIFIED": 0,
    "PENDING": 1,
    "CONFIRMED": 2,
    "SHIPPED": 3,
    "CANCELLED": 4,
}


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def order_dict(order: Order, *, proto: bool = False) -> dict[str, Any]:
    """Logical snake_case view of an order; ``proto`` swaps the status name for its enum number."""
    return {
        "id": order.id,
        "customer_id": order.customer_id,
        "status": STATUS_NUMBERS[order.status] if proto else order.status,
        "created_at": order.created_at,
        "currency": order.currency,
        "lines": [
            {"sku": ln.sku, "quantity": ln.quantity, "unit_price_cents": ln.unit_price_cents}
            for ln in order.lines
        ],
    }


def camelize(value: Any) -> Any:
    """Recursively rewrite dict keys to lowerCamelCase, the JSON and GraphQL spelling."""
    if isinstance(value, dict):
        return {_camel(str(k)): camelize(v) for k, v in cast("dict[object, Any]", value).items()}
    if isinstance(value, list):
        return [camelize(v) for v in cast("list[object]", value)]
    return value


def dump_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("ascii")


# --------------------------------------------------------------------------------------------
# REST, JSON-RPC and event encodings (JSON on the wire)
# --------------------------------------------------------------------------------------------


def encode_rest(order: Order) -> bytes:
    """Body of ``GET /orders/{id}``: the resource representation, nothing else."""
    return dump_json(camelize(order_dict(order)))


def encode_json_rpc_request(method: str, params: object, request_id: int) -> bytes:
    return dump_json({"jsonrpc": "2.0", "method": method, "params": params, "id": request_id})


def encode_json_rpc_response(order: Order, request_id: int) -> bytes:
    return dump_json({"jsonrpc": "2.0", "result": camelize(order_dict(order)), "id": request_id})


def encode_cloudevent(order: Order) -> bytes:
    """CloudEvents 1.0.2 structured-mode JSON: four required attributes plus `data`."""
    return dump_json(
        {
            "specversion": "1.0",
            "id": "evt-0001",
            "source": "/orders-service",
            "type": "com.example.order.confirmed.v1",
            "datacontenttype": "application/json",
            "time": "2026-01-01T00:00:00Z",
            "data": camelize(order_dict(order)),
        }
    )


def parse_like_javascript(body: bytes) -> Any:
    """Parse JSON the way a binary64-only consumer does: every integer becomes a float."""
    return json.loads(body, parse_int=float)


JSON_SAFE_INTEGER = 2**53 - 1


def is_json_interoperable_int(value: int) -> bool:
    """RFC 8259 Section 6: integers in [-(2**53)+1, (2**53)-1] are exact for binary64 parsers."""
    return -JSON_SAFE_INTEGER <= value <= JSON_SAFE_INTEGER


# --------------------------------------------------------------------------------------------
# Protobuf without protoc: descriptors built in code
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    number: int
    kind: str
    repeated: bool = False


_SCALARS = {
    "int64": descriptor_pb2.FieldDescriptorProto.TYPE_INT64,
    "int32": descriptor_pb2.FieldDescriptorProto.TYPE_INT32,
    "string": descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
}

LINE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("sku", 1, "string"),
    FieldSpec("quantity", 2, "int32"),
    FieldSpec("unit_price_cents", 3, "int64"),
)
ORDER_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("id", 1, "int64"),
    FieldSpec("customer_id", 2, "int64"),
    FieldSpec("status", 3, "int32"),
    FieldSpec("created_at", 4, "int64"),
    FieldSpec("currency", 5, "string"),
    FieldSpec("lines", 6, "OrderLine", repeated=True),
)


@dataclass(frozen=True, slots=True)
class ProtoSchema:
    """Compiled message classes plus the field tables they were built from."""

    messages: Mapping[str, tuple[FieldSpec, ...]]
    classes: Mapping[str, type[Message]]


def compile_schema(package: str, messages: Mapping[str, Sequence[FieldSpec]]) -> ProtoSchema:
    """Build a proto3 file in memory, in its own pool, and return the generated classes."""
    file_proto = descriptor_pb2.FileDescriptorProto(
        name=f"{package}.proto", package=package, syntax="proto3"
    )
    for message_name, specs in messages.items():
        message = file_proto.message_type.add(name=message_name)
        for spec in specs:
            entry = message.field.add(name=spec.name, number=spec.number)
            entry.label = (
                descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED
                if spec.repeated
                else descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
            )
            if spec.kind in _SCALARS:
                entry.type = _SCALARS[spec.kind]
            else:
                entry.type = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
                entry.type_name = f".{package}.{spec.kind}"
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    classes = {
        name: message_factory.GetMessageClass(pool.FindMessageTypeByName(f"{package}.{name}"))
        for name in messages
    }
    return ProtoSchema({k: tuple(v) for k, v in messages.items()}, classes)


def proto_encode(schema: ProtoSchema, root: str, data: Mapping[str, Any]) -> bytes:
    return bytes(_fill(schema, root, data).SerializeToString())


def _fill(schema: ProtoSchema, type_name: str, data: Mapping[str, Any]) -> Message:
    message = schema.classes[type_name]()
    for spec in schema.messages[type_name]:
        if spec.name not in data:
            continue
        value = data[spec.name]
        if spec.kind not in _SCALARS:
            children = value if spec.repeated else [value]
            for child in children:
                target = getattr(message, spec.name)
                built = _fill(schema, spec.kind, child)
                if spec.repeated:
                    target.add().CopyFrom(built)
                else:
                    target.CopyFrom(built)
        else:
            setattr(message, spec.name, value)
    return message


def proto_decode(schema: ProtoSchema, root: str, payload: bytes) -> dict[str, Any]:
    """Parse ``payload`` and read every declared field, so absent fields show as proto3 defaults."""
    return _read(schema, root, schema.classes[root].FromString(payload))


def _read(schema: ProtoSchema, type_name: str, message: Message) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for spec in schema.messages[type_name]:
        value = getattr(message, spec.name)
        if spec.kind in _SCALARS:
            result[spec.name] = value
        elif spec.repeated:
            result[spec.name] = [_read(schema, spec.kind, item) for item in value]
        else:
            result[spec.name] = _read(schema, spec.kind, value)
    return result


ORDER_SCHEMA = compile_schema(
    "lab.order.v1",
    {
        "Order": ORDER_FIELDS,
        "OrderLine": LINE_FIELDS,
        "GetOrderRequest": (FieldSpec("id", 1, "int64"),),
    },
)


def encode_protobuf(order: Order) -> bytes:
    return proto_encode(ORDER_SCHEMA, "Order", order_dict(order, proto=True))


def protobuf_backend() -> str:
    """Which protobuf runtime is active: ``upb`` (C), ``cpp``, or ``python`` (pure Python)."""
    return str(api_implementation.Type())


# --------------------------------------------------------------------------------------------
# gRPC and gRPC-Web framing (PROTOCOL-HTTP2.md and PROTOCOL-WEB.md)
# --------------------------------------------------------------------------------------------

DEFAULT_GRPC_MAX_RECEIVE_BYTES = 4 * 1024 * 1024
GRPC_WEB_TRAILER_FLAG = 0x80


def grpc_frame(message: bytes, *, compressed: bool = False) -> bytes:
    """Length-Prefixed-Message: 1 flag byte, 4 byte big-endian length, then the message."""
    return bytes([1 if compressed else 0]) + len(message).to_bytes(4, "big") + message


def grpc_web_trailer_frame(status: int, message: str = "") -> bytes:
    """gRPC-Web carries trailers in the body: flag byte with the MSB set, then header lines."""
    block = f"grpc-status: {status}\r\n"
    if message:
        block += f"grpc-message: {message}\r\n"
    payload = block.encode("ascii")
    return bytes([GRPC_WEB_TRAILER_FLAG]) + len(payload).to_bytes(4, "big") + payload


def parse_frames(body: bytes) -> list[tuple[int, bytes]]:
    """Split a gRPC or gRPC-Web body into ``(flag, payload)`` pairs."""
    frames: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(body):
        flag = body[offset]
        length = int.from_bytes(body[offset + 1 : offset + 5], "big")
        frames.append((flag, body[offset + 5 : offset + 5 + length]))
        offset += 5 + length
    return frames


def exceeds_default_receive_limit(message_bytes: int) -> bool:
    return message_bytes > DEFAULT_GRPC_MAX_RECEIVE_BYTES


# --------------------------------------------------------------------------------------------
# GraphQL (real Strawberry execution) and the wire-size report
# --------------------------------------------------------------------------------------------


@strawberry.type
class LineType:
    sku: str
    quantity: int
    unit_price_cents: int


@strawberry.type
class OrderType:
    id: strawberry.ID
    customer_id: strawberry.ID
    status: str
    created_at: int
    currency: str
    lines: list[LineType]


@strawberry.type
class OrderQuery:
    @strawberry.field
    def order(self, info: Info, id: strawberry.ID) -> OrderType | None:
        found: Order | None = info.context["orders"].get(int(id))
        if found is None:
            return None
        return OrderType(
            id=strawberry.ID(str(found.id)),
            customer_id=strawberry.ID(str(found.customer_id)),
            status=found.status,
            created_at=found.created_at,
            currency=found.currency,
            lines=[
                LineType(sku=ln.sku, quantity=ln.quantity, unit_price_cents=ln.unit_price_cents)
                for ln in found.lines
            ],
        )


ORDER_GRAPHQL_SCHEMA = strawberry.Schema(query=OrderQuery)
FULL_ORDER_QUERY = (
    "query($id: ID!){order(id:$id){id customerId status createdAt currency"
    " lines{sku quantity unitPriceCents}}}"
)
SLIM_ORDER_QUERY = "query($id: ID!){order(id:$id){id status}}"


def graphql_request(query: str, order_id: int) -> bytes:
    return dump_json({"query": query, "variables": {"id": str(order_id)}})


def execute_graphql(order: Order, query: str) -> bytes:
    """Run ``query`` against the Strawberry schema and serialize like the HTTP view does."""
    result = ORDER_GRAPHQL_SCHEMA.execute_sync(
        query, variable_values={"id": str(order.id)}, context_value={"orders": {order.id: order}}
    )
    return dump_json(process_result(result))


@dataclass(frozen=True, slots=True)
class WireSize:
    request_bytes: int
    response_bytes: int
    gzip_response_bytes: int


def _gzip_len(data: bytes) -> int:
    return len(gzip.compress(data, compresslevel=6, mtime=0))


def measure_wire(order: Order) -> dict[str, WireSize]:
    """Body bytes only: HTTP headers, TLS records and HTTP/2 framing are excluded on purpose."""
    rpc_request = encode_json_rpc_request("order.get", {"id": order.id}, 1)
    bodies: dict[str, tuple[bytes, bytes]] = {
        "rest": (b"", encode_rest(order)),
        "json_rpc": (rpc_request, encode_json_rpc_response(order, 1)),
        "graphql_full": (
            graphql_request(FULL_ORDER_QUERY, order.id),
            execute_graphql(order, FULL_ORDER_QUERY),
        ),
        "graphql_slim": (
            graphql_request(SLIM_ORDER_QUERY, order.id),
            execute_graphql(order, SLIM_ORDER_QUERY),
        ),
        "grpc": (
            grpc_frame(proto_encode(ORDER_SCHEMA, "GetOrderRequest", {"id": order.id})),
            grpc_frame(encode_protobuf(order)),
        ),
        "event": (b"", encode_cloudevent(order)),
    }
    return {
        name: WireSize(len(request), len(response), _gzip_len(response))
        for name, (request, response) in bodies.items()
    }


def time_per_call_ns(
    fn: Callable[[], object], iterations: int, clock: Callable[[], int] = time.perf_counter_ns
) -> float:
    start = clock()
    for _ in range(iterations):
        fn()
    return (clock() - start) / iterations


def benchmark_roundtrips(order: Order, iterations: int) -> dict[str, float]:
    """Nanoseconds per call to build and re-read one response; GraphQL includes execution."""
    rest = encode_rest(order)
    graphql_response = execute_graphql(order, FULL_ORDER_QUERY)
    proto = encode_protobuf(order)
    runners: dict[str, Callable[[], object]] = {
        "rest_json": lambda: json.loads(encode_rest(order)),
        "graphql_full": lambda: json.loads(execute_graphql(order, FULL_ORDER_QUERY)),
        "protobuf": lambda: proto_decode(ORDER_SCHEMA, "Order", encode_protobuf(order)),
        "rest_json_decode_only": lambda: json.loads(rest),
        "graphql_decode_only": lambda: json.loads(graphql_response),
        "protobuf_decode_only": lambda: proto_decode(ORDER_SCHEMA, "Order", proto),
    }
    return {name: time_per_call_ns(fn, iterations) for name, fn in runners.items()}


def critical_path_ms(depth: int, rtt_ms: float, server_ms: float) -> float:
    """Latency of ``depth`` strictly sequential round trips; parallel siblings add nothing."""
    return depth * (rtt_ms + server_ms)


# --------------------------------------------------------------------------------------------
# Schema evolution: apply a change on the writer, read with the old (v1) reader
# --------------------------------------------------------------------------------------------


class Paradigm(StrEnum):
    REST = "rest"
    GRPC = "grpc"
    GRAPHQL = "graphql"
    JSON_RPC = "json_rpc"
    EVENTS = "events"


class Change(StrEnum):
    ADD_FIELD = "add_field"
    REMOVE_FIELD = "remove_field"
    RENAME_FIELD = "rename_field"
    CHANGE_TYPE = "change_type"


class Outcome(StrEnum):
    COMPATIBLE = "compatible"
    SILENT_WRONG = "silent_wrong"
    HARD_ERROR = "hard_error"


def _apply_change(data: dict[str, Any], change: Change) -> dict[str, Any]:
    changed = dict(data)
    match change:
        case Change.ADD_FIELD:
            changed["fulfillment_note"] = "gift wrap"
        case Change.REMOVE_FIELD:
            del changed["status"]
        case Change.RENAME_FIELD:
            changed["state"] = changed.pop("status")
        case Change.CHANGE_TYPE:
            changed["created_at"] = "2026-01-01T00:00:00Z"
    return changed


class _LineV1(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    sku: str
    quantity: int
    unitPriceCents: int


class OrderJsonV1(BaseModel):
    """Strict, tolerant-of-extras v1 reader for the JSON paradigms."""

    model_config = ConfigDict(strict=True, extra="ignore")
    id: int
    customerId: int
    status: str
    createdAt: int
    currency: str
    lines: list[_LineV1]


def _json_outcome(paradigm: Paradigm, change: Change) -> Outcome:
    document = camelize(_apply_change(order_dict(SAMPLE_ORDER), change))
    if paradigm is Paradigm.JSON_RPC:
        body = dump_json({"jsonrpc": "2.0", "result": document, "id": 1})
        payload = json.loads(body)["result"]
    elif paradigm is Paradigm.EVENTS:
        body = dump_json({"specversion": "1.0", "id": "e", "source": "/s", "type": "t",
                          "data": document})  # fmt: skip
        payload = json.loads(body)["data"]
    else:
        payload = json.loads(dump_json(document))
    try:
        parsed = OrderJsonV1.model_validate(payload)
    except ValidationError:
        return Outcome.HARD_ERROR
    expected = OrderJsonV1.model_validate(camelize(order_dict(SAMPLE_ORDER)))
    return Outcome.COMPATIBLE if parsed == expected else Outcome.SILENT_WRONG


def writer_fields(change: Change) -> dict[str, tuple[FieldSpec, ...]]:
    """The writer's field tables after ``change``; the reader always keeps ``ORDER_FIELDS``."""
    order_fields = list(ORDER_FIELDS)
    by_name = {spec.name: index for index, spec in enumerate(order_fields)}
    match change:
        case Change.ADD_FIELD:
            order_fields.append(FieldSpec("fulfillment_note", 7, "string"))
        case Change.REMOVE_FIELD:
            del order_fields[by_name["status"]]
        case Change.RENAME_FIELD:
            order_fields[by_name["status"]] = FieldSpec("state", 3, "int32")
        case Change.CHANGE_TYPE:
            order_fields[by_name["created_at"]] = FieldSpec("created_at", 4, "string")
    return {"Order": tuple(order_fields), "OrderLine": LINE_FIELDS}


def _proto_outcome(change: Change) -> Outcome:
    writer = compile_schema(f"lab.writer.{change.value}", writer_fields(change))
    data = _apply_change(order_dict(SAMPLE_ORDER, proto=True), change)
    payload = proto_encode(writer, "Order", data)
    try:
        read_back = proto_decode(ORDER_SCHEMA, "Order", payload)
    except Exception:
        return Outcome.HARD_ERROR
    expected = order_dict(SAMPLE_ORDER, proto=True)
    return Outcome.COMPATIBLE if read_back == expected else Outcome.SILENT_WRONG


_GRAPHQL_V1_SDL = """
type Line { sku: String! quantity: Int! unitPriceCents: Int! }
type Order { id: ID! customerId: ID! status: String! createdAt: Int! currency: String!
  lines: [Line!]! }
type Query { order: Order }
"""
_GRAPHQL_CLIENT_QUERY = (
    "{order{id customerId status createdAt currency lines{sku quantity unitPriceCents}}}"
)


def _graphql_sdl(change: Change) -> str:
    match change:
        case Change.ADD_FIELD:
            return _GRAPHQL_V1_SDL.replace(
                "status: String!", "status: String! fulfillmentNote: String"
            )
        case Change.REMOVE_FIELD:
            return _GRAPHQL_V1_SDL.replace(" status: String!", "")
        case Change.RENAME_FIELD:
            return _GRAPHQL_V1_SDL.replace("status: String!", "state: String!")
        case Change.CHANGE_TYPE:
            return _GRAPHQL_V1_SDL.replace("createdAt: Int!", "createdAt: String!")


def _graphql_run(sdl: str, root: dict[str, Any]) -> graphql.ExecutionResult:
    schema = graphql.build_schema(sdl)
    return graphql.graphql_sync(  # pyright: ignore[reportUnknownMemberType]
        schema, _GRAPHQL_CLIENT_QUERY, root_value=root
    )


def _graphql_outcome(change: Change) -> Outcome:
    base = camelize({"order": order_dict(SAMPLE_ORDER)})
    expected = _graphql_run(_GRAPHQL_V1_SDL, base).data
    root = camelize({"order": _apply_change(order_dict(SAMPLE_ORDER), change)})
    if change is Change.CHANGE_TYPE:
        root["order"]["createdAt"] = "2026-01-01T00:00:00Z"
    result = _graphql_run(_graphql_sdl(change), root)
    if result.errors:
        return Outcome.HARD_ERROR
    return Outcome.COMPATIBLE if result.data == expected else Outcome.SILENT_WRONG


def evolution_outcome(paradigm: Paradigm, change: Change) -> Outcome:
    """Run the writer-newer, reader-older experiment for one cell of the matrix."""
    if paradigm is Paradigm.GRPC:
        return _proto_outcome(change)
    if paradigm is Paradigm.GRAPHQL:
        return _graphql_outcome(change)
    return _json_outcome(paradigm, change)


def evolution_matrix() -> dict[tuple[Paradigm, Change], Outcome]:
    return {(p, c): evolution_outcome(p, c) for p in Paradigm for c in Change}


# --------------------------------------------------------------------------------------------
# One screen, four ways: requests, sequential depth, and bytes
# --------------------------------------------------------------------------------------------

CUSTOMERS: dict[int, dict[str, Any]] = {
    7_731: {
        "id": 7_731,
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "tier": "GOLD",
        "createdAt": 1_600_000_000,
        "address": {"line1": "12 Analytical Way", "city": "London", "country": "GB"},
    }
}
PRODUCTS: dict[str, dict[str, Any]] = {
    sku: {
        "sku": sku,
        "title": title,
        "description": "A long marketing description the order screen never shows. " * 4,
        "weightGrams": weight,
        "categoryId": 40 + index,
    }
    for index, (sku, title, weight) in enumerate(
        [
            ("SKU-1001", "Brass Gear", 250),
            ("SKU-2002", "Steel Spring", 80),
            ("SKU-3003", "Rivet", 3),
        ]
    )
}


@dataclass(frozen=True, slots=True)
class ScreenView:
    """What the order screen renders; every loader must produce exactly this."""

    order_id: int
    status: str
    customer_name: str
    customer_tier: str
    lines: tuple[tuple[str, int, str], ...]


@strawberry.type
class ProductNode:
    title: str


@strawberry.type
class CustomerNode:
    name: str
    tier: str


@strawberry.type
class LineNode:
    sku: str
    quantity: int

    @strawberry.field
    async def product(self) -> ProductNode:
        await asyncio.sleep(0)
        return ProductNode(title=PRODUCTS[self.sku]["title"])


@strawberry.type
class ScreenOrder:
    id: strawberry.ID
    status: str
    customer_id: strawberry.ID
    lines: list[LineNode]

    @strawberry.field
    async def customer(self) -> CustomerNode:
        await asyncio.sleep(0)
        row = CUSTOMERS[int(self.customer_id)]
        return CustomerNode(name=row["name"], tier=row["tier"])


@strawberry.type
class ScreenQuery:
    @strawberry.field
    async def order(self, id: strawberry.ID) -> ScreenOrder:
        await asyncio.sleep(0)
        found = SAMPLE_ORDER if int(id) == SAMPLE_ORDER.id else None
        if found is None:
            raise LookupError(id)
        return ScreenOrder(
            id=strawberry.ID(str(found.id)),
            status=found.status,
            customer_id=strawberry.ID(str(found.customer_id)),
            lines=[LineNode(sku=ln.sku, quantity=ln.quantity) for ln in found.lines],
        )


SCREEN_QUERY = (
    "query($id: ID!){order(id:$id){id status customer{name tier}"
    " lines{sku quantity product{title}}}}"
)
type RpcHandler = Callable[[dict[str, Any]], Any]
_RPC_METHODS: dict[str, RpcHandler] = {
    "order.get": lambda p: camelize(order_dict(SAMPLE_ORDER)),
    "customer.get": lambda p: CUSTOMERS[p["id"]],
    "product.get": lambda p: PRODUCTS[p["sku"]],
}


async def _rpc_dispatch(raw: object) -> dict[str, Any]:
    await asyncio.sleep(0)
    item = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    request_id = item.get("id")
    if item.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid Request"},
                "id": None}  # fmt: skip
    method = item.get("method")
    handler = _RPC_METHODS.get(method) if isinstance(method, str) else None
    if handler is None:
        return {"jsonrpc": "2.0", "error": {"code": -32601, "message": "Method not found"},
                "id": request_id}  # fmt: skip
    try:
        result = handler(item.get("params") or {})
    except Exception:
        return {"jsonrpc": "2.0", "error": {"code": -32603, "message": "Internal error"},
                "id": request_id}  # fmt: skip
    return {"jsonrpc": "2.0", "result": result, "id": request_id}


def build_screen_app() -> FastAPI:
    app = FastAPI()

    @app.get("/orders/{order_id}")
    async def get_order(order_id: int, expand: str = "") -> JSONResponse:
        await asyncio.sleep(0)
        body = camelize(order_dict(SAMPLE_ORDER))
        if "customer" in expand:
            body["customer"] = CUSTOMERS[SAMPLE_ORDER.customer_id]
        if "lines.product" in expand:
            for line in body["lines"]:
                line["product"] = PRODUCTS[line["sku"]]
        return JSONResponse(body)

    @app.get("/customers/{customer_id}")
    async def get_customer(customer_id: int) -> JSONResponse:
        await asyncio.sleep(0)
        return JSONResponse(CUSTOMERS[customer_id])

    @app.get("/products/{sku}")
    async def get_product(sku: str) -> JSONResponse:
        await asyncio.sleep(0)
        return JSONResponse(PRODUCTS[sku])

    @app.post("/rpc")
    async def rpc(request: Request) -> Response:
        payload: object = json.loads(await request.body())
        if isinstance(payload, list):
            items = cast("list[object]", payload)
            replies = await asyncio.gather(*(_rpc_dispatch(item) for item in items))
            return JSONResponse([r for r in replies if r.get("id") is not None or "error" in r])
        return JSONResponse(await _rpc_dispatch(payload))

    app.include_router(GraphQLRouter(strawberry.Schema(query=ScreenQuery)), prefix="/graphql")
    return app


@dataclass(slots=True)
class Tracker:
    """Counts requests and the longest chain of requests that had to wait for an earlier reply."""

    requests: int = 0
    response_bytes: int = 0
    max_depth: int = 0
    _completed_depth: int = 0
    _depth_of: dict[int, int] = field(default_factory=lambda: {})

    async def on_request(self, request: httpx.Request) -> None:
        self.requests += 1
        self._depth_of[id(request)] = self._completed_depth + 1

    async def on_response(self, response: httpx.Response) -> None:
        await response.aread()
        depth = self._depth_of[id(response.request)]
        self.response_bytes += len(response.content)
        self._completed_depth = max(self._completed_depth, depth)
        self.max_depth = max(self.max_depth, depth)


def tracked_client(app: FastAPI) -> tuple[httpx.AsyncClient, Tracker]:
    tracker = Tracker()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        event_hooks={"request": [tracker.on_request], "response": [tracker.on_response]},
    )
    return client, tracker


async def load_screen_rest(client: httpx.AsyncClient, order_id: int) -> ScreenView:
    order = (await client.get(f"/orders/{order_id}")).json()
    customer_reply, *product_replies = await asyncio.gather(
        client.get(f"/customers/{order['customerId']}"),
        *(client.get(f"/products/{line['sku']}") for line in order["lines"]),
    )
    customer = customer_reply.json()
    titles = {r.json()["sku"]: r.json()["title"] for r in product_replies}
    return ScreenView(
        order["id"], order["status"], customer["name"], customer["tier"],
        tuple((ln["sku"], ln["quantity"], titles[ln["sku"]]) for ln in order["lines"]),
    )  # fmt: skip


async def load_screen_rest_expand(client: httpx.AsyncClient, order_id: int) -> ScreenView:
    order = (await client.get(f"/orders/{order_id}?expand=customer,lines.product")).json()
    return ScreenView(
        order["id"], order["status"], order["customer"]["name"], order["customer"]["tier"],
        tuple((ln["sku"], ln["quantity"], ln["product"]["title"]) for ln in order["lines"]),
    )  # fmt: skip


async def load_screen_json_rpc(client: httpx.AsyncClient, order_id: int) -> ScreenView:
    first = (
        await client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "method": "order.get", "params": {"id": order_id}, "id": 1},
        )
    ).json()
    order = first["result"]
    batch = [{"jsonrpc": "2.0", "method": "customer.get",
              "params": {"id": order["customerId"]}, "id": "c"}]  # fmt: skip
    batch += [{"jsonrpc": "2.0", "method": "product.get", "params": {"sku": ln["sku"]},
               "id": ln["sku"]} for ln in order["lines"]]  # fmt: skip
    replies = {r["id"]: r["result"] for r in (await client.post("/rpc", json=batch)).json()}
    return ScreenView(
        order["id"], order["status"], replies["c"]["name"], replies["c"]["tier"],
        tuple((ln["sku"], ln["quantity"], replies[ln["sku"]]["title"]) for ln in order["lines"]),
    )  # fmt: skip


async def load_screen_graphql(client: httpx.AsyncClient, order_id: int) -> ScreenView:
    reply = await client.post(
        "/graphql", json={"query": SCREEN_QUERY, "variables": {"id": str(order_id)}}
    )
    order = reply.json()["data"]["order"]
    return ScreenView(
        int(order["id"]), order["status"], order["customer"]["name"], order["customer"]["tier"],
        tuple((ln["sku"], ln["quantity"], ln["product"]["title"]) for ln in order["lines"]),
    )  # fmt: skip


# --------------------------------------------------------------------------------------------
# The decision engine: explicit rules, explicit vetoes, explicit reasons
# --------------------------------------------------------------------------------------------


class Boundary(StrEnum):
    PUBLIC_THIRD_PARTY = "public_third_party"
    FIRST_PARTY_CLIENT = "first_party_client"
    INTERNAL_SERVICE = "internal_service"


class Topology(StrEnum):
    SINGLE_TEAM = "single_team"
    FRONTEND_BACKEND_SPLIT = "frontend_backend_split"
    MANY_SERVICE_TEAMS = "many_service_teams"


class Transport(StrEnum):
    HTTP = "http"
    STDIO_OR_WEBSOCKET = "stdio_or_websocket"


@dataclass(frozen=True, slots=True)
class Requirements:
    boundary: Boundary
    topology: Topology
    transport: Transport = Transport.HTTP
    browser_direct: bool = False
    can_run_translation_proxy: bool = True
    cdn_cacheable_reads: bool = False
    client_shaped_reads: bool = False
    bidirectional_streaming: bool = False
    decoupled_in_time: bool = False
    needs_synchronous_answer: bool = False
    polyglot_services: bool = False
    serial_hops: int = 1


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    applies: Callable[[Requirements], bool]
    effects: Mapping[Paradigm, int]
    vetoes: frozenset[Paradigm]
    reason: str


P = Paradigm
RULES: tuple[Rule, ...] = (
    Rule(
        "default_rest",
        lambda r: True,
        {P.REST: 1},
        frozenset(),
        "uniform interface and the widest tooling make REST the default",
    ),
    Rule(
        "non_http_transport",
        lambda r: r.transport is Transport.STDIO_OR_WEBSOCKET,
        {P.JSON_RPC: 5, P.GRAPHQL: -3},
        frozenset({P.REST, P.GRPC}),
        "REST needs HTTP semantics and gRPC needs HTTP/2; JSON-RPC is transport agnostic",
    ),
    Rule(
        "public_third_party",
        lambda r: r.boundary is Boundary.PUBLIC_THIRD_PARTY,
        {P.REST: 3, P.GRPC: -2, P.JSON_RPC: -1},
        frozenset(),
        "strangers need a self-describing, cacheable, toolchain-free interface",
    ),
    Rule(
        "browser_without_proxy",
        lambda r: r.browser_direct and not r.can_run_translation_proxy,
        {},
        frozenset({P.GRPC}),
        "browsers cannot speak native gRPC framing and trailers",
    ),
    Rule(
        "browser_with_proxy",
        lambda r: r.browser_direct and r.can_run_translation_proxy,
        {P.GRPC: -2},
        frozenset(),
        "gRPC-Web or Connect adds a translation layer to operate",
    ),
    Rule(
        "cdn_cacheable_reads",
        lambda r: r.cdn_cacheable_reads,
        {P.REST: 3, P.GRAPHQL: -1, P.JSON_RPC: -2, P.GRPC: -3},
        frozenset(),
        "GET with validators is what shared caches key on",
    ),
    Rule(
        "client_shaped_reads",
        lambda r: r.client_shaped_reads,
        {P.GRAPHQL: 3, P.REST: -1},
        frozenset(),
        "many screens over a deep graph want field selection and one round trip",
    ),
    Rule(
        "frontend_backend_split",
        lambda r: r.topology is Topology.FRONTEND_BACKEND_SPLIT,
        {P.GRAPHQL: 2},
        frozenset(),
        "frontend squads ship without waiting for a new backend endpoint",
    ),
    Rule(
        "bidirectional_streaming",
        lambda r: r.bidirectional_streaming,
        {P.GRPC: 3, P.EVENTS: 1, P.REST: -2, P.JSON_RPC: -1, P.GRAPHQL: -1},
        frozenset(),
        "gRPC has first-class streaming; others bolt it on",
    ),
    Rule(
        "polyglot_internal",
        lambda r: r.boundary is Boundary.INTERNAL_SERVICE and r.polyglot_services,
        {P.GRPC: 3},
        frozenset(),
        "one .proto generates typed clients for every language",
    ),
    Rule(
        "deep_serial_chain",
        lambda r: r.boundary is Boundary.INTERNAL_SERVICE and r.serial_hops >= 3,
        {P.GRPC: 2},
        frozenset(),
        "deadline propagation and multiplexing pay off on deep chains",
    ),
    Rule(
        "many_service_teams",
        lambda r: r.topology is Topology.MANY_SERVICE_TEAMS,
        {P.GRPC: 1, P.EVENTS: 1},
        frozenset(),
        "independent teams need a governed contract and temporal decoupling",
    ),
    Rule(
        "decoupled_in_time",
        lambda r: r.decoupled_in_time,
        {P.EVENTS: 5, P.REST: -1, P.GRPC: -1, P.GRAPHQL: -1, P.JSON_RPC: -1},
        frozenset(),
        "request-response needs the callee up now; a log does not",
    ),
    Rule(
        "synchronous_answer",
        lambda r: r.needs_synchronous_answer,
        {},
        frozenset({P.EVENTS}),
        "the caller must block on the result; a log cannot answer",
    ),
)


@dataclass(frozen=True, slots=True)
class Ranked:
    paradigm: Paradigm
    score: int
    vetoed: bool
    reasons: tuple[str, ...]


def rank(requirements: Requirements, rules: Sequence[Rule] = RULES) -> list[Ranked]:
    """Score every paradigm; vetoed paradigms sort last whatever their score."""
    order = list(Paradigm)
    scores = dict.fromkeys(order, 0)
    vetoed: set[Paradigm] = set()
    reasons: dict[Paradigm, list[str]] = {p: [] for p in order}
    for rule in rules:
        if not rule.applies(requirements):
            continue
        for paradigm, delta in rule.effects.items():
            scores[paradigm] += delta
            reasons[paradigm].append(f"{rule.name}: {delta:+d}")
        for paradigm in rule.vetoes:
            vetoed.add(paradigm)
            reasons[paradigm].append(f"{rule.name}: VETO")
    ranked = [Ranked(p, scores[p], p in vetoed, tuple(reasons[p])) for p in order]
    return sorted(ranked, key=lambda r: (r.vetoed, -r.score, order.index(r.paradigm)))
