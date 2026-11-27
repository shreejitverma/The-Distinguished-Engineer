"""Lab: gRPC status codes and google.rpc.Status rich error payloads over real grpc.aio.

Unit tests prove the tables and the grpc-status-details-bin contract against the protobuf
runtime and hand-derived bytes.
Socket tests prove them against a real grpcio 1.85.1 server and client on 127.0.0.1, and
against a raw h2 peer that sends responses a gRPC server never would.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import socket
from typing import TYPE_CHECKING, Any, cast

import grpc  # type: ignore[import-untyped]
import grpc.aio  # type: ignore[import-untyped]
import h2.config
import h2.connection
import h2.events
import h2.exceptions
import pytest
import pytest_asyncio

from api_labs.f19_grpc_and_protocol_buffers.rich_status import (
    APPLICATION_ONLY,
    CANONICAL,
    DETAILS_KEY,
    LIBRARY_GENERATED,
    RST_STREAM_TO_GRPC,
    MalformedDetailsError,
    Retryability,
    StatusCode,
    StatusMismatchError,
    UnknownDetail,
    code_for_http_status,
    code_from_number,
    code_number,
    describe,
    details_trailer_cost,
    find_detail,
    from_trailing_metadata,
    make_status,
    message_class,
    retryability,
    to_grpc_status,
    unpack_details,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

# google/rpc/code.proto at googleapis master (fetched 2026-11-21), name and number in file order.
CODE_PROTO = [
    ("OK", 0),
    ("CANCELLED", 1),
    ("UNKNOWN", 2),
    ("INVALID_ARGUMENT", 3),
    ("DEADLINE_EXCEEDED", 4),
    ("NOT_FOUND", 5),
    ("ALREADY_EXISTS", 6),
    ("PERMISSION_DENIED", 7),
    ("UNAUTHENTICATED", 16),
    ("RESOURCE_EXHAUSTED", 8),
    ("FAILED_PRECONDITION", 9),
    ("ABORTED", 10),
    ("OUT_OF_RANGE", 11),
    ("UNIMPLEMENTED", 12),
    ("INTERNAL", 13),
    ("UNAVAILABLE", 14),
    ("DATA_LOSS", 15),
]


def _retry_info(seconds: int, nanos: int = 0) -> Any:
    return message_class("google.rpc.RetryInfo")(retry_delay={"seconds": seconds, "nanos": nanos})


def _error_info(reason: str, domain: str, **metadata: str) -> Any:
    return message_class("google.rpc.ErrorInfo")(reason=reason, domain=domain, metadata=metadata)


def _bad_request(*violations: tuple[str, str]) -> Any:
    fv = message_class("google.rpc.BadRequest.FieldViolation")
    return message_class("google.rpc.BadRequest")(
        field_violations=[fv(field=f, description=d) for f, d in violations]
    )


def _rich_status() -> Any:
    return make_status(
        StatusCode.INVALID_ARGUMENT,
        "2 invalid fields",
        _error_info("INVALID_ORDER", "orders.example.com", orderId="o-42"),
        _bad_request(("items[0].quantity", "must be >= 1"), ("shipTo.postalCode", "required")),
        _retry_info(2, 500_000_000),
    )


# --- unit tier: tables ---


@pytest.mark.unit
def test_status_code_numbers_match_code_proto() -> None:
    assert [(code.name, code_number(code)) for code in CANONICAL] == sorted(
        CODE_PROTO, key=lambda pair: pair[1]
    )
    assert len(CANONICAL) == 17  # OK plus the 16 error codes


@pytest.mark.unit
@pytest.mark.parametrize("number", [17, 99, -1, 2**31 - 1])
def test_numbers_outside_the_canonical_range_become_unknown(number: int) -> None:
    assert code_from_number(number) is StatusCode.UNKNOWN
    assert code_from_number(16) is StatusCode.UNAUTHENTICATED


@pytest.mark.unit
def test_library_and_application_code_sets_are_disjoint_and_exclude_ok() -> None:
    assert not LIBRARY_GENERATED & APPLICATION_ONLY
    assert StatusCode.OK not in LIBRARY_GENERATED | APPLICATION_ONLY
    assert len(APPLICATION_ONLY) == 7
    # PERMISSION_DENIED is in neither list of doc/statuscodes.md; it arises only via HTTP 403.
    unlisted = set(CANONICAL) - LIBRARY_GENERATED - APPLICATION_ONLY - {StatusCode.OK}
    assert unlisted == {StatusCode.PERMISSION_DENIED}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("http_status", "expected"),
    [
        (400, "INTERNAL"),
        (401, "UNAUTHENTICATED"),
        (403, "PERMISSION_DENIED"),
        (404, "UNIMPLEMENTED"),
        (429, "UNAVAILABLE"),
        (502, "UNAVAILABLE"),
        (503, "UNAVAILABLE"),
        (504, "UNAVAILABLE"),
        (500, "UNKNOWN"),
        (200, "UNKNOWN"),
        (418, "UNKNOWN"),
    ],
)
def test_http_status_table_matches_the_grpc_doc(http_status: int, expected: str) -> None:
    assert code_for_http_status(http_status).name == expected


@pytest.mark.unit
def test_retryability_follows_aip_194() -> None:
    by_class: dict[Retryability, set[str]] = {}
    for code in CANONICAL:
        by_class.setdefault(retryability(code), set()).add(code.name)
    assert by_class[Retryability.AUTOMATIC] == {"UNAVAILABLE"}
    assert by_class[Retryability.TRANSACTION] == {"ABORTED"}
    assert by_class[Retryability.GENERALLY_NOT] == {"RESOURCE_EXHAUSTED", "INTERNAL", "UNKNOWN"}
    assert {"DEADLINE_EXCEEDED", "CANCELLED", "INVALID_ARGUMENT", "DATA_LOSS", "OK"} <= (
        by_class[Retryability.NEVER]
    )


# --- unit tier: the google.rpc.Status bytes and the details contract ---


@pytest.mark.unit
def test_status_serializes_to_hand_derived_wire_bytes() -> None:
    status = make_status(StatusCode.INVALID_ARGUMENT, "bad", _retry_info(1))
    expected = (
        b"\x08\x03"  # field 1 (code) varint 3
        b"\x12\x03bad"  # field 2 (message) len 3
        b"\x1a\x30"  # field 3 (details[0]) len 48
        b"\x0a\x28type.googleapis.com/google.rpc.RetryInfo"  # Any.type_url, len 40
        b"\x12\x04\x0a\x02\x08\x01"  # Any.value = RetryInfo{retry_delay{seconds=1}}
    )
    assert status.SerializeToString(deterministic=True) == expected


@pytest.mark.unit
def test_every_standard_detail_type_resolves_in_the_pool() -> None:
    from api_labs.f19_grpc_and_protocol_buffers.rich_status import STANDARD_DETAIL_TYPES

    assert len(STANDARD_DETAIL_TYPES) == 10
    for name in STANDARD_DETAIL_TYPES:
        assert message_class(name).DESCRIPTOR.full_name == name


@pytest.mark.unit
def test_to_grpc_status_carries_code_message_and_the_details_trailer() -> None:
    status = _rich_status()
    out = to_grpc_status(status)
    assert out.code is StatusCode.INVALID_ARGUMENT
    assert out.details == "2 invalid fields"
    assert out.trailing_metadata == ((DETAILS_KEY, status.SerializeToString(deterministic=True)),)


@pytest.mark.unit
def test_to_grpc_status_rejects_ok_and_out_of_range_codes() -> None:
    with pytest.raises(ValueError, match="OK"):
        to_grpc_status(message_class("google.rpc.Status")(code=0, message="fine"))
    with pytest.raises(ValueError, match="Invalid status code 99"):
        to_grpc_status(message_class("google.rpc.Status")(code=99, message="?"))


@pytest.mark.unit
def test_round_trip_through_trailing_metadata_preserves_the_status() -> None:
    status = _rich_status()
    out = to_grpc_status(status)
    back = from_trailing_metadata(out.code, out.details, out.trailing_metadata)
    assert back == status


@pytest.mark.unit
def test_no_details_trailer_means_no_rich_status() -> None:
    assert from_trailing_metadata(StatusCode.NOT_FOUND, "x", [("x-trace-bin", b"\x00")]) is None
    assert from_trailing_metadata(StatusCode.NOT_FOUND, "x", None) is None


@pytest.mark.unit
def test_details_whose_code_contradicts_grpc_status_are_rejected() -> None:
    trailer = ((DETAILS_KEY, _rich_status().SerializeToString()),)
    with pytest.raises(StatusMismatchError, match="Code in Status proto"):
        from_trailing_metadata(StatusCode.NOT_FOUND, "2 invalid fields", trailer)


@pytest.mark.unit
def test_details_whose_message_contradicts_grpc_message_are_rejected() -> None:
    trailer = ((DETAILS_KEY, _rich_status().SerializeToString()),)
    with pytest.raises(StatusMismatchError, match="Message in Status proto"):
        from_trailing_metadata(StatusCode.INVALID_ARGUMENT, "something else", trailer)


@pytest.mark.unit
def test_garbage_details_bytes_raise_a_typed_error() -> None:
    with pytest.raises(MalformedDetailsError):
        from_trailing_metadata(StatusCode.INTERNAL, "", ((DETAILS_KEY, b"\xff\xff\xff"),))
    with pytest.raises(MalformedDetailsError, match="text"):
        from_trailing_metadata(StatusCode.INTERNAL, "", ((DETAILS_KEY, "text"),))


@pytest.mark.unit
def test_first_details_entry_wins_when_the_trailer_repeats() -> None:
    first = make_status(StatusCode.NOT_FOUND, "a")
    second = make_status(StatusCode.NOT_FOUND, "b")
    trailer = (
        (DETAILS_KEY, first.SerializeToString()),
        (DETAILS_KEY, second.SerializeToString()),
    )
    got = from_trailing_metadata(StatusCode.NOT_FOUND, "a", trailer)
    assert got is not None
    assert got.message == "a"


@pytest.mark.unit
def test_unpack_resolves_every_known_type_in_order() -> None:
    kinds = [d.DESCRIPTOR.full_name for d in unpack_details(_rich_status())]
    assert kinds == ["google.rpc.ErrorInfo", "google.rpc.BadRequest", "google.rpc.RetryInfo"]


@pytest.mark.unit
def test_type_url_prefix_does_not_select_the_type() -> None:
    status = message_class("google.rpc.Status")(code=3, message="m")
    packed = status.details.add()
    packed.Pack(_retry_info(7))
    packed.type_url = "example.org/some/path/google.rpc.RetryInfo"
    (detail,) = unpack_details(status)
    assert detail.retry_delay.seconds == 7


@pytest.mark.unit
def test_type_url_without_a_slash_is_unresolvable() -> None:
    status = message_class("google.rpc.Status")(code=3, message="m")
    packed = status.details.add()
    packed.Pack(_retry_info(7))
    packed.type_url = "google.rpc.RetryInfo"
    (detail,) = unpack_details(status)
    assert isinstance(detail, UnknownDetail)
    assert detail.type_url == "google.rpc.RetryInfo"


@pytest.mark.unit
def test_unknown_detail_types_survive_a_parse_and_reserialize_cycle() -> None:
    status = message_class("google.rpc.Status")(code=3, message="m")
    status.details.add(type_url="type.googleapis.com/acme.v1.Surprise", value=b"\x08\x2a")
    wire = status.SerializeToString(deterministic=True)

    reparsed = message_class("google.rpc.Status")()
    reparsed.ParseFromString(wire)
    (detail,) = unpack_details(reparsed)
    assert detail == UnknownDetail("type.googleapis.com/acme.v1.Surprise", b"\x08\x2a")
    assert reparsed.SerializeToString(deterministic=True) == wire


@pytest.mark.unit
def test_unresolvable_detail_is_listed_in_the_report_not_raised() -> None:
    status = make_status(StatusCode.UNAVAILABLE, "down", _error_info("SHARD_DOWN", "x.example"))
    status.details.add(type_url="t.example/acme.Other", value=b"")
    out = to_grpc_status(status)
    report = describe(out.code, out.details, out.trailing_metadata)
    assert report.reason == "SHARD_DOWN"
    assert report.unknown_types == ("t.example/acme.Other",)


@pytest.mark.unit
def test_describe_extracts_what_a_client_branches_on() -> None:
    out = to_grpc_status(_rich_status())
    report = describe(out.code, out.details, out.trailing_metadata)
    assert report.rich
    assert (report.reason, report.domain) == ("INVALID_ORDER", "orders.example.com")
    assert report.retry_after_seconds == 2.5
    assert report.field_violations == (
        ("items[0].quantity", "must be >= 1"),
        ("shipTo.postalCode", "required"),
    )
    assert report.retryability is Retryability.NEVER


@pytest.mark.unit
def test_describe_without_details_still_classifies_the_code() -> None:
    report = describe(StatusCode.UNAVAILABLE, "connection reset", None)
    assert not report.rich
    assert report.message == "connection reset"
    assert report.retryability is Retryability.AUTOMATIC
    assert find_detail((), "google.rpc.ErrorInfo") is None


@pytest.mark.unit
def test_details_trailer_cost_is_name_plus_raw_bytes_plus_32() -> None:
    status = _rich_status()
    raw = len(status.SerializeToString(deterministic=True))
    assert details_trailer_cost(status) == len(DETAILS_KEY) + raw + 32
    assert details_trailer_cost(status) == 23 + raw + 32


@pytest.mark.unit
def test_rst_stream_table_lists_every_code_except_stream_closed() -> None:
    assert set(RST_STREAM_TO_GRPC) == set(range(0x0, 0xD)) - {0x5}
    assert RST_STREAM_TO_GRPC[0x7] is StatusCode.UNAVAILABLE


# --- socket tier: a real grpc.aio server ---

_PERCENT_MESSAGE = "caf\u00e9 100% done\nnext"


async def _rich_fail(request: bytes, context: Any) -> bytes:
    await context.abort_with_status(to_grpc_status(_rich_status()))
    raise AssertionError("abort_with_status returns only by raising")


async def _plain_fail(request: bytes, context: Any) -> bytes:
    await context.abort(StatusCode.NOT_FOUND, "no such order")
    raise AssertionError("abort returns only by raising")


async def _raises(request: bytes, context: Any) -> bytes:
    raise ValueError("boom")


async def _percent(request: bytes, context: Any) -> bytes:
    await context.abort(StatusCode.FAILED_PRECONDITION, _PERCENT_MESSAGE)
    raise AssertionError("abort returns only by raising")


async def _abort_ok(request: bytes, context: Any) -> bytes:
    await context.abort(StatusCode.OK, "this is not an error")
    return b"unreachable"


async def _slow(request: bytes, context: Any) -> bytes:
    await asyncio.sleep(30)
    return b"late"


async def _contradict(request: bytes, context: Any) -> bytes:
    lie = make_status(StatusCode.INVALID_ARGUMENT, "details say invalid argument")
    context.set_trailing_metadata(((DETAILS_KEY, lie.SerializeToString()),))
    await context.abort(StatusCode.NOT_FOUND, "outer says not found")
    raise AssertionError("abort returns only by raising")


async def _big(request: bytes, context: Any) -> bytes:
    context.set_trailing_metadata(((DETAILS_KEY, b"a" * int(request)),))
    await context.abort(StatusCode.FAILED_PRECONDITION, "m")
    raise AssertionError("abort returns only by raising")


def _handlers() -> dict[str, Any]:
    funcs: dict[str, Callable[[bytes, Any], Any]] = {
        "RichFail": _rich_fail,
        "PlainFail": _plain_fail,
        "Raises": _raises,
        "Percent": _percent,
        "AbortOk": _abort_ok,
        "Slow": _slow,
        "Contradict": _contradict,
        "Big": _big,
    }
    return {name: grpc.unary_unary_rpc_method_handler(fn) for name, fn in funcs.items()}


@pytest_asyncio.fixture
async def port() -> AsyncIterator[int]:
    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler("lab.Errors", _handlers()),)
    )
    bound: int = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield bound
    finally:
        await server.stop(None)


async def _call(port: int, method: str, request: bytes = b"", deadline_s: float = 5) -> Any:
    """Invoke a lab method and return the AioRpcError it raises."""
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        stub = cast("Any", channel.unary_unary(f"/lab.Errors/{method}"))
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await stub(request, timeout=deadline_s)
    return info.value


@pytest.mark.socket
@pytest.mark.asyncio
async def test_rich_status_round_trips_over_a_real_grpc_aio_call(port: int) -> None:
    err = await _call(port, "RichFail")
    assert err.code() is StatusCode.INVALID_ARGUMENT
    assert err.details() == "2 invalid fields"
    trailers = dict(err.trailing_metadata())
    assert trailers[DETAILS_KEY] == _rich_status().SerializeToString(deterministic=True)
    report = describe(err.code(), err.details(), err.trailing_metadata())
    assert report.rich
    assert report.reason == "INVALID_ORDER"
    assert report.retry_after_seconds == 2.5
    assert [f for f, _ in report.field_violations] == ["items[0].quantity", "shipTo.postalCode"]


@pytest.mark.socket
@pytest.mark.asyncio
async def test_plain_abort_sends_no_details_trailer(port: int) -> None:
    err = await _call(port, "PlainFail")
    assert err.code() is StatusCode.NOT_FOUND
    assert DETAILS_KEY not in dict(err.trailing_metadata())
    report = describe(err.code(), err.details(), err.trailing_metadata())
    assert not report.rich
    assert report.message == "no such order"


@pytest.mark.socket
@pytest.mark.asyncio
async def test_uncaught_exception_becomes_unknown_with_the_exception_type(port: int) -> None:
    err = await _call(port, "Raises")
    assert err.code() is StatusCode.UNKNOWN
    assert err.details() == "Unexpected <class 'ValueError'>: boom"


@pytest.mark.socket
@pytest.mark.asyncio
async def test_grpc_message_percent_encoding_is_transparent(port: int) -> None:
    err = await _call(port, "Percent")
    assert err.code() is StatusCode.FAILED_PRECONDITION
    assert err.details() == _PERCENT_MESSAGE


@pytest.mark.socket
@pytest.mark.asyncio
async def test_grpc_does_not_enforce_the_details_consistency_rule_the_consumer_must(
    port: int,
) -> None:
    err = await _call(port, "Contradict")
    assert err.code() is StatusCode.NOT_FOUND  # grpcio delivered the lie untouched
    assert DETAILS_KEY in dict(err.trailing_metadata())
    with pytest.raises(StatusMismatchError):
        describe(err.code(), err.details(), err.trailing_metadata())


@pytest.mark.socket
@pytest.mark.asyncio
async def test_unimplemented_is_generated_by_the_library_for_a_missing_method(port: int) -> None:
    err = await _call(port, "NoSuchMethod")
    assert err.code() is StatusCode.UNIMPLEMENTED
    assert err.details() == "Method not found!"
    assert StatusCode.UNIMPLEMENTED in LIBRARY_GENERATED


@pytest.mark.socket
@pytest.mark.asyncio
async def test_deadline_exceeded_is_generated_when_the_server_is_too_slow(port: int) -> None:
    err = await _call(port, "Slow", deadline_s=0.2)
    assert err.code() is StatusCode.DEADLINE_EXCEEDED
    assert retryability(err.code()) is Retryability.NEVER


@pytest.mark.socket
@pytest.mark.asyncio
async def test_unavailable_is_generated_when_nothing_listens() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = int(probe.getsockname()[1])
    err = await _call(dead_port, "RichFail")
    assert err.code() is StatusCode.UNAVAILABLE
    assert retryability(err.code()) is Retryability.AUTOMATIC


@pytest.mark.socket
@pytest.mark.asyncio
async def test_aio_abort_with_ok_hangs_the_call_until_the_deadline(
    port: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)
    started = asyncio.get_running_loop().time()
    err = await _call(port, "AbortOk", deadline_s=0.5)
    elapsed = asyncio.get_running_loop().time() - started
    assert err.code() is StatusCode.DEADLINE_EXCEEDED  # the server never sent a status
    assert elapsed >= 0.45
    assert any(
        "Expecting non-ok status code" in rec.getMessage()
        or "Expecting non-ok status code" in (rec.exc_text or "")
        for rec in caplog.records
    )


@pytest.mark.socket
@pytest.mark.asyncio
async def test_a_proper_abort_returns_immediately_unlike_abort_with_ok(port: int) -> None:
    started = asyncio.get_running_loop().time()
    err = await _call(port, "PlainFail")
    assert err.code() is StatusCode.NOT_FOUND
    assert asyncio.get_running_loop().time() - started < 0.4


@pytest.mark.socket
@pytest.mark.asyncio
async def test_small_details_pass_and_details_over_the_hard_limit_are_replaced(port: int) -> None:
    ok = await _call(port, "Big", b"1000")
    assert ok.code() is StatusCode.FAILED_PRECONDITION
    assert len(dict(ok.trailing_metadata())[DETAILS_KEY]) == 1000

    # Hard limit is 16384 (grpc.absolute_max_metadata_size default); between 8192 and 16384
    # grpc core rejects a random sample, so only the two ends are deterministic.
    huge = await _call(port, "Big", b"20000")
    assert huge.code() is StatusCode.RESOURCE_EXHAUSTED  # the server's code was lost
    assert "exceeds hard limit" in huge.details()
    assert DETAILS_KEY not in dict(huge.trailing_metadata() or ())


@pytest.mark.socket
@pytest.mark.asyncio
async def test_soft_limit_message_counts_the_details_trailer_by_raw_length(port: int) -> None:
    """At 16000 raw bytes the soft-limit rejection is near certain (98% of the sampled range)."""
    outcomes: list[str] = []
    for _ in range(12):
        err = await _call(port, "Big", b"16000")
        if err.code() is StatusCode.RESOURCE_EXHAUSTED:
            outcomes.append(err.details())
    assert outcomes, "16000 bytes passed 12 times: soft-limit sampling is not in effect"
    assert any(f"{DETAILS_KEY}:{16000 + 23 + 32}B" in text for text in outcomes)


# --- socket tier: a raw h2 peer that sends what a gRPC server would not ---


class _RawResponse:
    def __init__(
        self,
        status: int = 200,
        headers: tuple[tuple[str, str], ...] = (),
        rst_code: int | None = None,
    ) -> None:
        self.status = status
        self.headers = headers
        self.rst_code = rst_code


async def _serve_raw(
    responses: dict[str, _RawResponse],
) -> tuple[asyncio.Server, int, list[asyncio.StreamWriter]]:
    writers: list[asyncio.StreamWriter] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.append(writer)
        conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        try:
            while data := await reader.read(65535):
                for event in conn.receive_data(data):
                    if isinstance(event, h2.events.RequestReceived):
                        path = dict(event.headers)[b":path"].decode()
                        response = responses[path]
                        if response.rst_code is not None:
                            conn.reset_stream(event.stream_id, error_code=response.rst_code)
                        else:
                            conn.send_headers(
                                event.stream_id,
                                [
                                    (":status", str(response.status)),
                                    ("content-type", "text/plain"),
                                    *response.headers,
                                ],
                                end_stream=True,
                            )
                writer.write(conn.data_to_send())
                await writer.drain()
        except (ConnectionError, h2.exceptions.ProtocolError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1]), writers


async def _raw_error(responses: dict[str, _RawResponse], path: str) -> Any:
    server, bound, writers = await _serve_raw(responses)
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{bound}") as channel:
            stub = cast("Any", channel.unary_unary(path))
            with pytest.raises(grpc.aio.AioRpcError) as info:
                await stub(b"", timeout=5)
        return info.value
    finally:
        # Python 3.12+ Server.wait_closed() waits for open connections, so close them first.
        server.close()
        for writer in writers:
            writer.close()
        await server.wait_closed()


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("http_status", [400, 401, 403, 404, 429, 500, 502, 503, 504, 200])
async def test_real_client_applies_the_http_to_grpc_table_when_grpc_status_is_missing(
    http_status: int,
) -> None:
    err = await _raw_error({"/raw/m": _RawResponse(http_status)}, "/raw/m")
    assert err.code() is code_for_http_status(http_status)


@pytest.mark.socket
@pytest.mark.asyncio
async def test_grpc_status_wins_over_the_http_status() -> None:
    raw = {"/raw/m": _RawResponse(503, (("grpc-status", "5"), ("grpc-message", "gone")))}
    err = await _raw_error(raw, "/raw/m")
    assert err.code() is StatusCode.NOT_FOUND
    assert err.details() == "gone"


@pytest.mark.socket
@pytest.mark.asyncio
async def test_out_of_range_grpc_status_number_becomes_unknown() -> None:
    err = await _raw_error({"/raw/m": _RawResponse(200, (("grpc-status", "99"),))}, "/raw/m")
    assert err.code() is StatusCode.UNKNOWN
    assert code_from_number(99) is StatusCode.UNKNOWN


@pytest.mark.socket
@pytest.mark.asyncio
async def test_non_numeric_grpc_status_becomes_internal_not_unknown() -> None:
    """The doc says parsing a bad status is UNKNOWN; grpc core 1.85.1 returns INTERNAL."""
    err = await _raw_error({"/raw/m": _RawResponse(200, (("grpc-status", "abc"),))}, "/raw/m")
    assert err.code() is StatusCode.INTERNAL
    assert "Error parsing 'grpc-status'" in err.details()


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("padded", [True, False])
async def test_client_accepts_padded_and_unpadded_base64_details(padded: bool) -> None:
    status = make_status(StatusCode.ABORTED, "retry txn", _error_info("TXN_CONFLICT", "db.example"))
    raw = status.SerializeToString(deterministic=True)
    encoded = base64.b64encode(raw).decode("ascii")
    assert encoded.endswith("="), "fixture must need base64 padding"
    value = encoded if padded else encoded.rstrip("=")
    assert value.endswith("=") is padded
    responses = {
        "/raw/m": _RawResponse(
            200,
            (("grpc-status", "10"), ("grpc-message", "retry txn"), (DETAILS_KEY, value)),
        )
    }
    err = await _raw_error(responses, "/raw/m")
    assert err.code() is StatusCode.ABORTED
    assert dict(err.trailing_metadata())[DETAILS_KEY] == raw
    report = describe(err.code(), err.details(), err.trailing_metadata())
    assert report.reason == "TXN_CONFLICT"
    assert report.retryability is Retryability.TRANSACTION


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("rst_code", sorted(set(RST_STREAM_TO_GRPC) - {0x0}))
async def test_real_client_maps_rst_stream_error_codes(rst_code: int) -> None:
    err = await _raw_error({"/raw/m": _RawResponse(rst_code=rst_code)}, "/raw/m")
    assert err.code() is RST_STREAM_TO_GRPC[rst_code], err.details()
