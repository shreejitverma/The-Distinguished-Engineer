"""Lab: AIP-193 google.rpc.Status rendered as proto bytes, HTTP/1.1+JSON, and gRPC trailers.

The oracle is the real protobuf runtime: google/rpc/status.proto and error_details.proto are
rebuilt as descriptors (same field numbers), so the hand-rolled encoder is checked byte for byte.
"""

from __future__ import annotations

import base64
import socket
from concurrent import futures
from typing import TYPE_CHECKING, Any, cast

import grpc  # type: ignore[import-untyped]
import h2.config
import h2.connection
import h2.events
import httpx
import pytest
from google.protobuf import (  # type: ignore[import-untyped]
    any_pb2,
    descriptor_pb2,
    descriptor_pool,
    duration_pb2,
    json_format,
    message_factory,
)
from starlette.applications import Starlette
from starlette.routing import Route

from api_labs.f06_bulk_long_running_and_error_contracts.google_rpc_status import (
    DETAILS_TRAILER,
    HTTP_STATUS,
    ApiError,
    BadRequest,
    Code,
    ErrorInfo,
    ErrorModelViolation,
    FieldViolation,
    LocalizedMessage,
    QuotaFailure,
    QuotaViolation,
    RetryInfo,
    Status,
    api_error_handler,
    decode_error_info,
    decode_retry_info,
    decode_status,
    duration_json,
    encode_status,
    parse_grpc_trailers,
    percent_decode,
    percent_encode,
    to_grpc_trailers,
    to_http_json,
    trailers_size,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.requests import Request
    from starlette.responses import Response

    from api_labs.f06_bulk_long_running_and_error_contracts.google_rpc_status import JsonValue

# --- the oracle: google.rpc descriptors built from the googleapis .proto field numbers ---

_F = descriptor_pb2.FieldDescriptorProto
_STR, _I32, _I64, _MSG = _F.TYPE_STRING, _F.TYPE_INT32, _F.TYPE_INT64, _F.TYPE_MESSAGE


def _add(msg: Any, name: str, number: int, ftype: int, type_name: str = "", **kw: Any) -> None:
    label = _F.LABEL_REPEATED if kw.pop("repeated", False) else _F.LABEL_OPTIONAL
    if type_name:
        kw["type_name"] = type_name
    msg.field.add(name=name, number=number, type=ftype, label=label, **kw)


def _string_map(msg: Any, name: str, number: int, entry: str, scope: str) -> None:
    nested = msg.nested_type.add(name=entry)
    nested.options.map_entry = True
    _add(nested, "key", 1, _STR)
    _add(nested, "value", 2, _STR)
    _add(msg, name, number, _MSG, f".{scope}.{entry}", repeated=True)


def _build_pool() -> Any:
    pool = descriptor_pool.DescriptorPool()
    for wkt in (any_pb2, duration_pb2):
        proto = descriptor_pb2.FileDescriptorProto()
        wkt.DESCRIPTOR.CopyToProto(proto)
        pool.Add(proto)
    status = descriptor_pb2.FileDescriptorProto(
        name="google/rpc/status.proto",
        package="google.rpc",
        syntax="proto3",
        dependency=["google/protobuf/any.proto"],
    )
    msg = status.message_type.add(name="Status")
    _add(msg, "code", 1, _I32)
    _add(msg, "message", 2, _STR)
    _add(msg, "details", 3, _MSG, ".google.protobuf.Any", repeated=True)
    pool.Add(status)

    errs = descriptor_pb2.FileDescriptorProto(
        name="google/rpc/error_details.proto",
        package="google.rpc",
        syntax="proto3",
        dependency=["google/protobuf/duration.proto"],
    )
    info = errs.message_type.add(name="ErrorInfo")
    _add(info, "reason", 1, _STR)
    _add(info, "domain", 2, _STR)
    _string_map(info, "metadata", 3, "MetadataEntry", "google.rpc.ErrorInfo")
    retry = errs.message_type.add(name="RetryInfo")
    _add(retry, "retry_delay", 1, _MSG, ".google.protobuf.Duration")
    quota = errs.message_type.add(name="QuotaFailure")
    violation = quota.nested_type.add(name="Violation")
    for number, name in enumerate(
        ("subject", "description", "api_service", "quota_metric", "quota_id"), start=1
    ):
        _add(violation, name, number, _STR)
    scope = "google.rpc.QuotaFailure.Violation"
    _string_map(violation, "quota_dimensions", 6, "QuotaDimensionsEntry", scope)
    _add(violation, "quota_value", 7, _I64)
    violation.oneof_decl.add(name="_future_quota_value")
    _add(violation, "future_quota_value", 8, _I64, oneof_index=0, proto3_optional=True)
    _add(quota, "violations", 1, _MSG, ".google.rpc.QuotaFailure.Violation", repeated=True)
    localized = errs.message_type.add(name="LocalizedMessage")
    _add(localized, "locale", 1, _STR)
    _add(localized, "message", 2, _STR)
    bad = errs.message_type.add(name="BadRequest")
    fv = bad.nested_type.add(name="FieldViolation")
    _add(fv, "field", 1, _STR)
    _add(fv, "description", 2, _STR)
    _add(fv, "reason", 3, _STR)
    _add(fv, "localized_message", 4, _MSG, ".google.rpc.LocalizedMessage")
    _add(bad, "field_violations", 1, _MSG, ".google.rpc.BadRequest.FieldViolation", repeated=True)
    pool.Add(errs)
    return pool


POOL = _build_pool()


def _message(full_name: str) -> Any:
    return message_factory.GetMessageClass(POOL.FindMessageTypeByName(full_name))()


def _oracle_bytes(status: Status) -> bytes:
    """Serialize the same Status with the protobuf runtime, starting from our ProtoJSON."""
    error = cast("dict[str, Any]", to_http_json(status)["error"])
    out = _message("google.rpc.Status")
    out.code = int(status.code)
    out.message = status.message
    for detail_json in error["details"]:
        body = dict(detail_json)
        type_url: str = body.pop("@type")
        inner = _message(type_url.removeprefix("type.googleapis.com/"))
        json_format.ParseDict(body, inner, descriptor_pool=POOL)
        out.details.add().Pack(inner, deterministic=True)
    return cast("bytes", out.SerializeToString(deterministic=True))


# --- fixtures: one Status that exercises every detail type and every encoding edge ---

QUOTA_STATUS = Status(
    code=Code.RESOURCE_EXHAUSTED,
    message=(
        "Quota 'writes per minute' exhausted (100% used) for project 'demo-42'; retry after 1.5s"
    ),
    details=(
        ErrorInfo(
            reason="RATE_LIMIT_EXCEEDED",
            domain="orders.example.com",
            metadata={"quotaLimit": "WritesPerMinute", "consumer": "projects/demo-42"},
        ),
        RetryInfo(seconds=1, nanos=500_000_000),
        QuotaFailure(
            violations=(
                QuotaViolation(
                    subject="project:demo-42",
                    description="Writes per minute exceeded",
                    api_service="orders.example.com",
                    quota_metric="orders.example.com/writes",
                    quota_id="WritesPerMinutePerProject",
                    quota_dimensions={"region": "us-east1", "tier": "free"},
                    quota_value=600,
                    future_quota_value=0,
                ),
            )
        ),
        LocalizedMessage(locale="fr-CH", message="Quota d'\u00e9critures \u00e9puis\u00e9 (100%)."),
    ),
)

BAD_REQUEST_STATUS = Status(
    code=Code.INVALID_ARGUMENT,
    message="Request has 2 invalid fields.",
    details=(
        ErrorInfo(reason="INVALID_ORDER", domain="orders.example.com"),
        BadRequest(
            field_violations=(
                FieldViolation(
                    field="lineItems[0].quantity",
                    description="must be >= 1",
                    reason="QUANTITY_TOO_SMALL",
                    localized_message=LocalizedMessage("en-US", "Quantity must be at least 1."),
                ),
                FieldViolation(field="shippingAddress.postalCode", description="required"),
            )
        ),
    ),
)

RETRY_NOW_STATUS = Status(
    code=Code.UNAVAILABLE,
    message="Shard is failing over.",
    details=(
        ErrorInfo(reason="SHARD_FAILOVER", domain="orders.example.com"),
        RetryInfo(seconds=0),  # a zero Duration is still a set message field: bytes 0a 00
    ),
)

STATUSES = [QUOTA_STATUS, BAD_REQUEST_STATUS, RETRY_NOW_STATUS]
STATUS_IDS = ["quota", "bad", "retry-now"]

# google/rpc/code.proto "HTTP Mapping" comments, copied literally.
CODE_PROTO_HTTP_MAPPING = [
    ("OK", 200),
    ("CANCELLED", 499),
    ("UNKNOWN", 500),
    ("INVALID_ARGUMENT", 400),
    ("DEADLINE_EXCEEDED", 504),
    ("NOT_FOUND", 404),
    ("ALREADY_EXISTS", 409),
    ("PERMISSION_DENIED", 403),
    ("UNAUTHENTICATED", 401),
    ("RESOURCE_EXHAUSTED", 429),
    ("FAILED_PRECONDITION", 400),
    ("ABORTED", 409),
    ("OUT_OF_RANGE", 400),
    ("UNIMPLEMENTED", 501),
    ("INTERNAL", 500),
    ("UNAVAILABLE", 503),
    ("DATA_LOSS", 500),
]


@pytest.mark.unit
@pytest.mark.parametrize(("name", "http_status"), CODE_PROTO_HTTP_MAPPING)
def test_http_status_follows_code_proto_mapping(name: str, http_status: int) -> None:
    assert HTTP_STATUS[Code[name]] == http_status
    assert len(HTTP_STATUS) == len(CODE_PROTO_HTTP_MAPPING) == 17


@pytest.mark.unit
@pytest.mark.parametrize("status", STATUSES, ids=STATUS_IDS)
def test_status_bytes_equal_protobuf_runtime_deterministic_bytes(status: Status) -> None:
    assert encode_status(status) == _oracle_bytes(status)


@pytest.mark.unit
@pytest.mark.parametrize("status", STATUSES, ids=STATUS_IDS)
def test_runtime_decodes_status_bytes_to_the_http_json_details(status: Status) -> None:
    parsed = _message("google.rpc.Status")
    parsed.ParseFromString(encode_status(status))
    as_json = json_format.MessageToDict(parsed, descriptor_pool=POOL)
    error = cast("dict[str, Any]", to_http_json(status)["error"])
    assert as_json["code"] == int(status.code)
    assert as_json["message"] == error["message"]
    assert as_json["details"] == error["details"]


@pytest.mark.unit
def test_http_json_reproduces_the_aip_193_published_example() -> None:
    # The published example also carries a google.rpc.Help detail; the lab has no Help type.
    message = (
        "An <e2-medium> VM instance with <local-ssd=3,nvidia-t4=2> is currently unavailable"
        " in the <us-east1-a> zone. Consider trying your request in the"
        " <us-central1-f,us-central1-c> zone(s), which currently has/have capacity to"
        " accommodate your request. Alternatively, you can try your request again with a"
        " different VM hardware configuration or at a later time. For more information, see"
        " the troubleshooting documentation."
    )
    status = Status(
        code=Code.RESOURCE_EXHAUSTED,
        message=(
            "The zone 'us-east1-a' does not have enough resources available to fulfill the"
            " request. Try a different zone, or try again later."
        ),
        details=(
            ErrorInfo(
                reason="RESOURCE_AVAILABILITY",
                domain="compute.googleapis.com",
                metadata={
                    "zone": "us-east1-a",
                    "vmType": "e2-medium",
                    "attachment": "local-ssd=3,nvidia-t4=2",
                    "zonesWithCapacity": "us-central1-f,us-central1-c",
                },
            ),
            LocalizedMessage(locale="en-US", message=message),
        ),
    )
    body = to_http_json(status)
    error = cast("dict[str, Any]", body["error"])
    assert list(error) == ["code", "message", "status", "details"]
    assert error["code"] == 429
    assert error["status"] == "RESOURCE_EXHAUSTED"
    assert error["details"] == [
        {
            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": "RESOURCE_AVAILABILITY",
            "domain": "compute.googleapis.com",
            "metadata": {
                "zone": "us-east1-a",
                "vmType": "e2-medium",
                "attachment": "local-ssd=3,nvidia-t4=2",
                "zonesWithCapacity": "us-central1-f,us-central1-c",
            },
        },
        {
            "@type": "type.googleapis.com/google.rpc.LocalizedMessage",
            "locale": "en-US",
            "message": message,
        },
    ]


@pytest.mark.unit
def test_quota_int64_fields_are_json_strings_and_optional_zero_is_present() -> None:
    error = cast("dict[str, Any]", to_http_json(QUOTA_STATUS)["error"])
    quota = error["details"][2]
    assert quota["violations"][0]["quotaValue"] == "600"
    assert quota["violations"][0]["futureQuotaValue"] == "0"
    no_rollout = QuotaViolation(subject="project:demo-42", description="d", quota_value=0)
    assert no_rollout.to_json() == {"subject": "project:demo-42", "description": "d"}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("seconds", "nanos", "expected"),
    [
        (1, 0, "1s"),
        (1, 500_000_000, "1.500s"),
        (2, 250_000, "2.000250s"),
        (0, 1_500, "0.000001500s"),
        (0, 1_000, "0.000001s"),
        (3, 120_000, "3.000120s"),
    ],
)
def test_retry_delay_duration_json_matches_runtime(seconds: int, nanos: int, expected: str) -> None:
    oracle = duration_pb2.Duration(seconds=seconds, nanos=nanos).ToJsonString()
    assert duration_json(seconds, nanos) == oracle == expected


@pytest.mark.unit
def test_ok_code_is_not_an_error_status() -> None:
    with pytest.raises(ErrorModelViolation, match=r"Code\.OK"):
        encode_status(Status(Code.OK, "fine", (ErrorInfo("FINE", "orders.example.com"),)))


@pytest.mark.unit
def test_error_info_is_mandatory() -> None:
    with pytest.raises(ErrorModelViolation, match="must include an ErrorInfo"):
        to_http_json(Status(Code.UNAVAILABLE, "try later", (RetryInfo(seconds=2),)))


@pytest.mark.unit
def test_each_detail_type_appears_at_most_once() -> None:
    twice = Status(
        Code.INVALID_ARGUMENT,
        "bad",
        (
            ErrorInfo("INVALID_ORDER", "orders.example.com"),
            BadRequest((FieldViolation("a", "x"),)),
            BadRequest((FieldViolation("b", "y"),)),
        ),
    )
    with pytest.raises(ErrorModelViolation, match=r"repeated: \['google.rpc.BadRequest'\]"):
        encode_status(twice)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("reason", "valid"),
    [
        ("CPU_AVAILABILITY", True),
        ("NO_STOCK", True),
        ("A" * 63, True),
        ("A" * 64, False),
        ("AB", False),
        ("_LEADING", False),
        ("TRAILING_", False),
        ("1DIGIT", False),
        ("lower_case", False),
    ],
)
def test_error_info_reason_must_be_upper_snake_case(reason: str, valid: bool) -> None:
    info = ErrorInfo(reason, "orders.example.com")
    if valid:
        info.validate()
    else:
        with pytest.raises(ErrorModelViolation, match="UPPER_SNAKE_CASE"):
            info.validate()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "valid"),
    [("zone", True), ("vmType", True), ("a" * 64, True), ("a" * 65, False), ("Zone", False)],
)
def test_error_info_metadata_key_shape(key: str, valid: bool) -> None:
    info = ErrorInfo("NO_STOCK", "orders.example.com", {key: "v"})
    if valid:
        info.validate()
    else:
        with pytest.raises(ErrorModelViolation, match="metadata key"):
            info.validate()


@pytest.mark.unit
def test_grpc_message_percent_encoding_round_trips() -> None:
    message = "Quota \u00e9puis\u00e9 (100%)\nretry"
    encoded = percent_encode(message)
    assert encoded == "Quota %C3%A9puis%C3%A9 (100%25)%0Aretry"
    assert percent_decode(encoded) == message
    assert percent_decode("50%ZZ off") == "50%ZZ off"  # a broken escape is kept, not an error
    assert percent_decode("50%Z") == "50%Z"  # a truncated escape is kept too
    assert (
        percent_decode("bad %FF byte") == "bad \ufffd byte"
    )  # invalid UTF-8 is replaced, not raised


@pytest.mark.unit
def test_trailers_round_trip_and_both_base64_paddings_decode() -> None:
    trailers = to_grpc_trailers(QUOTA_STATUS)
    assert trailers[0] == ("grpc-status", "8")
    details_value = trailers[2][1]
    assert not details_value.endswith("=")
    assert len(encode_status(QUOTA_STATUS)) % 3 != 0  # so padding would have been visible

    padded = base64.b64encode(encode_status(QUOTA_STATUS)).decode("ascii")
    for value in (details_value, padded):
        decoded = parse_grpc_trailers([*trailers[:2], (DETAILS_TRAILER, value)])
        assert decoded.code == Code.RESOURCE_EXHAUSTED
        assert decoded.message == QUOTA_STATUS.message
        info = decoded.detail(ErrorInfo.TYPE_NAME)
        retry = decoded.detail(RetryInfo.TYPE_NAME)
        assert info is not None
        assert retry is not None
        assert decode_error_info(info) == QUOTA_STATUS.error_info()
        assert decode_retry_info(retry) == RetryInfo(seconds=1, nanos=500_000_000)


@pytest.mark.unit
def test_details_that_contradict_grpc_status_are_rejected() -> None:
    trailers = to_grpc_trailers(QUOTA_STATUS)
    forged = [("grpc-status", str(int(Code.UNAVAILABLE))), *trailers[1:]]
    with pytest.raises(ErrorModelViolation, match="code 8 contradicts grpc-status 14"):
        parse_grpc_trailers(forged)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "match"),
    [
        (b"\x08", "truncated varint"),  # field 1 tag, value missing
        (b"\x08\x80", "truncated varint"),  # continuation bit on the last byte
        (b"\x08" + b"\xff" * 10 + b"\x01", "longer than 10 bytes"),
        (b"\x12\x05abc", "overruns the buffer"),  # message claims 5 bytes, 3 present
        (b"\x19\x00\x00", "overruns the buffer"),  # fixed64 field with 2 of 8 bytes
    ],
    ids=["no-value", "open-varint", "long-varint", "short-len", "short-fixed64"],
)
def test_malformed_details_bytes_are_rejected(raw: bytes, match: str) -> None:
    with pytest.raises(ErrorModelViolation, match=match):
        decode_status(raw)


@pytest.mark.unit
@pytest.mark.parametrize(
    "unknown",
    [b"\x7d" + b"\x00" * 4, b"\x79" + b"\x00" * 8],
    ids=["fixed32", "fixed64"],
)
def test_unknown_fixed_width_fields_are_skipped(unknown: bytes) -> None:
    known = decode_status(encode_status(QUOTA_STATUS))
    assert decode_status(encode_status(QUOTA_STATUS) + unknown) == known


OVERSIZE_STATUS = Status(
    code=Code.FAILED_PRECONDITION,
    message="Order is locked.",
    details=(ErrorInfo("ORDER_LOCKED", "orders.example.com", {"blob": "x" * 20_000}),),
)


@pytest.mark.unit
def test_trailers_over_the_8_kib_budget_are_refused_at_the_server() -> None:
    assert trailers_size(to_grpc_trailers(QUOTA_STATUS)) <= 8192
    with pytest.raises(ErrorModelViolation, match="over the 8192-byte budget"):
        to_grpc_trailers(OVERSIZE_STATUS)


@pytest.mark.unit
def test_trailers_size_counts_name_value_and_32_bytes_per_field() -> None:
    assert trailers_size([("a", "bc"), ("def", "")]) == (1 + 2 + 32) + (3 + 0 + 32)
    size = trailers_size(to_grpc_trailers(QUOTA_STATUS))
    assert to_grpc_trailers(QUOTA_STATUS, budget=size)  # exactly at the budget passes
    with pytest.raises(ErrorModelViolation, match=f"trailers are {size} bytes"):
        to_grpc_trailers(QUOTA_STATUS, budget=size - 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_handler_sends_http_status_and_aip_193_body() -> None:
    async def create_order(request: Request) -> Response:
        raise ApiError(QUOTA_STATUS)

    app = Starlette(
        routes=[Route("/v1/orders", create_order, methods=["POST"])],
        exception_handlers={ApiError: api_error_handler},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/orders", json={})
    assert response.status_code == 429
    assert response.headers["content-type"] == "application/json"
    expected: JsonValue = to_http_json(QUOTA_STATUS)
    assert response.json() == expected


# --- socket tier: a real grpcio server on 127.0.0.1:0 ---


def _aborter(status: Status) -> Any:
    def handler(request: bytes, context: Any) -> bytes:
        context.set_trailing_metadata(((DETAILS_TRAILER, encode_status(status)),))
        context.abort(grpc.StatusCode[status.code.name], status.message)
        raise AssertionError("abort() returns only by raising")

    method_handler: grpc.RpcMethodHandler[bytes, bytes] = grpc.unary_unary_rpc_method_handler(
        handler
    )
    return method_handler


def _rpc_error(port: int, method: str) -> Any:
    """Invoke a lab method and return the grpc.RpcError (also a grpc.Call) it raises."""
    with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
        call: grpc.UnaryUnaryMultiCallable[bytes, bytes] = channel.unary_unary(method)
        with pytest.raises(grpc.RpcError) as info:
            call(b"", timeout=5)
    return info.value


@pytest.fixture
def grpc_port() -> Iterator[int]:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    handlers = {"Fail": _aborter(QUOTA_STATUS), "FailHuge": _aborter(OVERSIZE_STATUS)}
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("lab.Errors", handlers),))
    port: int = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield port
    finally:
        server.stop(None).wait()


@pytest.mark.socket
def test_grpcio_delivers_status_details_bytes_unchanged(grpc_port: int) -> None:
    err = _rpc_error(grpc_port, "/lab.Errors/Fail")
    assert err.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    assert err.details() == QUOTA_STATUS.message
    metadata = dict(err.trailing_metadata())
    assert metadata[DETAILS_TRAILER] == encode_status(QUOTA_STATUS)


def _h2_text(value: str | bytes) -> str:
    return value.decode("ascii") if isinstance(value, bytes) else value


@pytest.mark.socket
def test_grpcio_wire_trailers_equal_the_lab_encoding(grpc_port: int) -> None:
    conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    conn.initiate_connection()
    stream = conn.get_next_available_stream_id()
    conn.send_headers(
        stream,
        [
            (":method", "POST"),
            (":scheme", "http"),
            (":path", "/lab.Errors/Fail"),
            (":authority", f"127.0.0.1:{grpc_port}"),
            ("content-type", "application/grpc"),
            ("te", "trailers"),
        ],
    )
    conn.send_data(stream, b"\x00\x00\x00\x00\x00", end_stream=True)  # empty, uncompressed
    received: list[tuple[str, str]] = []
    kinds: list[str] = []
    with socket.create_connection(("127.0.0.1", grpc_port), timeout=5) as sock:
        sock.sendall(conn.data_to_send())
        ended = False
        while not ended:
            data = sock.recv(65535)
            assert data, "server closed before END_STREAM"
            for event in conn.receive_data(data):
                kinds.append(type(event).__name__)
                if isinstance(event, h2.events.ResponseReceived | h2.events.TrailersReceived):
                    received += [(_h2_text(k), _h2_text(v)) for k, v in event.headers or []]
                ended = ended or isinstance(event, h2.events.StreamEnded)
            sock.sendall(conn.data_to_send())
    wire = dict(received)
    assert "TrailersReceived" not in kinds  # grpcio sent Trailers-Only: one HEADERS block
    assert wire[":status"] == "200"
    for name, value in to_grpc_trailers(QUOTA_STATUS):
        assert wire[name] == value, name


@pytest.mark.socket
def test_grpcio_replaces_the_code_when_trailers_pass_the_hard_limit(grpc_port: int) -> None:
    err = _rpc_error(grpc_port, "/lab.Errors/FailHuge")
    assert err.code() == grpc.StatusCode.RESOURCE_EXHAUSTED  # not the server's FAILED_PRECONDITION
    assert "exceeds hard limit" in err.details()
    assert DETAILS_TRAILER not in dict(err.trailing_metadata() or ())
