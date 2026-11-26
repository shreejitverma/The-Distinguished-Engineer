"""Lab tests: grpc.health.v1.Health, server reflection, and graceful stop on grpc.aio 1.85.1."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUnknownArgumentType=false, reportMissingTypeArgument=false, reportAttributeAccessIssue=false

from __future__ import annotations

import asyncio
import importlib
import time
from typing import Any

import grpc  # type: ignore[import-untyped]
import pytest

from api_labs.f19_grpc_and_protocol_buffers.health_reflection_shutdown import (
    ADMIN_SERVICE,
    HEALTH_SERVICE,
    MAX_LIST_SERVICES,
    NOT_SERVING,
    REFLECTION_SERVICE,
    SERVICE_UNKNOWN,
    SERVING,
    WORK_SERVICE,
    Client,
    HealthServicer,
    LabServer,
    ReflectionServicer,
    build_schema,
    drain_and_stop,
    next_or_none,
)

_pb: Any = importlib.import_module("google.protobuf.descriptor_pb2")

WAIT = 5.0  # seconds; only reached when a test is about to fail


class Aborted(Exception):
    """Stands in for `grpc.aio.AbortError` so handlers can be called without a socket."""

    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        super().__init__(code, details)
        self.code = code
        self.details = details


class FakeContext:
    async def abort(self, code: grpc.StatusCode, details: str = "") -> None:
        raise Aborted(code, details)


def health_request(service: str = "") -> Any:
    return build_schema().message("grpc.health.v1.HealthCheckRequest")(service=service)


def reflection_request(**kwargs: str) -> Any:
    return build_schema().message("grpc.reflection.v1.ServerReflectionRequest")(**kwargs)


async def request_stream(*requests: Any) -> Any:
    for request in requests:
        yield request


async def wait_for_unavailable(client: Client) -> list[grpc.StatusCode]:
    """Poll with fresh RPCs until one gets UNAVAILABLE: shutdown has begun and the port is closed.

    The first refusal can be CANCELLED instead (measured: a call that races the GOAWAY);
    every refusal is returned so the test can say what it saw.
    """
    refusals: list[grpc.StatusCode] = []
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        try:
            await client.do("probe", deadline=1.0)
        except grpc.aio.AioRpcError as error:
            refusals.append(error.code())
            if refusals[-1] == grpc.StatusCode.UNAVAILABLE:
                return refusals
        await asyncio.sleep(0.02)
    pytest.fail(f"server never answered UNAVAILABLE; refusals so far: {refusals}")


# --- wire contract (pure) -------------------------------------------------------------------


@pytest.mark.unit
def test_health_messages_use_the_field_numbers_of_health_proto() -> None:
    schema = build_schema()
    response_type = schema.message("grpc.health.v1.HealthCheckResponse")
    assert health_request("x").SerializeToString() == b"\n\x01x"
    assert response_type(status=SERVING).SerializeToString() == b"\x08\x01"
    assert response_type(status=SERVICE_UNKNOWN).SerializeToString() == b"\x08\x03"
    assert response_type().SerializeToString() == b""  # UNKNOWN = 0 is the proto3 default


@pytest.mark.unit
def test_enum_numbers_and_method_paths_match_the_spec() -> None:
    schema = build_schema()
    enum = schema.pool.FindEnumTypeByName("grpc.health.v1.HealthCheckResponse.ServingStatus")
    assert {value.name: value.number for value in enum.values} == {
        "UNKNOWN": 0,
        "SERVING": 1,
        "NOT_SERVING": 2,
        "SERVICE_UNKNOWN": 3,
    }
    service = schema.pool.FindServiceByName(HEALTH_SERVICE)
    assert [(m.name, m.server_streaming) for m in service.methods] == [
        ("Check", False),
        ("List", False),
        ("Watch", True),
    ]
    handler = HealthServicer(schema).handler()
    details = grpc.HandlerCallDetails
    for method in ("Check", "List", "Watch"):
        call = type("Details", (details,), {"method": f"/{HEALTH_SERVICE}/{method}"})()
        assert handler.service(call) is not None
    wrong = type("Details", (details,), {"method": f"/{HEALTH_SERVICE}/Nope"})()
    assert handler.service(wrong) is None


# --- Health semantics (in-process) ----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_unknown_service_aborts_not_found_and_overall_is_empty_string() -> None:
    servicer = HealthServicer(build_schema())
    assert (await servicer.check(health_request(""), FakeContext())).status == SERVING
    with pytest.raises(Aborted) as info:
        await servicer.check(health_request("pkg.Unregistered"), FakeContext())
    assert info.value.code == grpc.StatusCode.NOT_FOUND
    servicer.set("pkg.Registered", NOT_SERVING)
    assert (await servicer.check(health_request("pkg.Registered"), FakeContext())).status == (
        NOT_SERVING
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enter_graceful_shutdown_flips_every_service_and_freezes_the_registry() -> None:
    servicer = HealthServicer(build_schema())
    servicer.set("a.A", SERVING)
    servicer.set("b.B", SERVING)
    servicer.enter_graceful_shutdown()
    for name in ("", "a.A", "b.B"):
        assert (await servicer.check(health_request(name), FakeContext())).status == NOT_SERVING
    servicer.set("a.A", SERVING)  # a late "I am ready again" from a racing task
    servicer.set("c.C", SERVING)  # and a service registered after the flip
    assert (await servicer.check(health_request("a.A"), FakeContext())).status == NOT_SERVING
    with pytest.raises(Aborted):
        await servicer.check(health_request("c.C"), FakeContext())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_unfreezes_and_restores_serving() -> None:
    servicer = HealthServicer(build_schema())
    servicer.set("a.A", SERVING)
    servicer.enter_graceful_shutdown()
    servicer.resume()
    assert (await servicer.check(health_request("a.A"), FakeContext())).status == SERVING
    servicer.set("a.A", NOT_SERVING)
    assert (await servicer.check(health_request("a.A"), FakeContext())).status == NOT_SERVING


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_is_capped_at_100_services_like_grpc_go() -> None:
    servicer = HealthServicer(build_schema())
    list_request = build_schema().message("grpc.health.v1.HealthListRequest")()
    for index in range(MAX_LIST_SERVICES - 1):  # plus the "" entry makes exactly 100
        servicer.set(f"svc.S{index}", SERVING)
    reply = await servicer.list_statuses(list_request, FakeContext())
    assert len(reply.statuses) == MAX_LIST_SERVICES
    assert reply.statuses[""].status == SERVING
    servicer.set("svc.OneTooMany", SERVING)
    with pytest.raises(Aborted) as info:
        await servicer.list_statuses(list_request, FakeContext())
    assert info.value.code == grpc.StatusCode.RESOURCE_EXHAUSTED


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watch_sends_current_status_first_then_only_changes() -> None:
    servicer = HealthServicer(build_schema())
    stream = servicer.watch(health_request(""), FakeContext())
    assert (await anext(stream)).status == SERVING
    servicer.set("", NOT_SERVING)
    assert (await asyncio.wait_for(anext(stream), WAIT)).status == NOT_SERVING
    servicer.set("", NOT_SERVING)  # same status again: nothing to say
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(stream), 0.05)
    await stream.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watch_coalesces_a_burst_to_the_latest_status() -> None:
    servicer = HealthServicer(build_schema())
    stream = servicer.watch(health_request(""), FakeContext())
    assert (await anext(stream)).status == SERVING
    for status in (NOT_SERVING, SERVING, NOT_SERVING):  # three flips while the reader is busy
        servicer.set("", status)
    assert (await asyncio.wait_for(anext(stream), WAIT)).status == NOT_SERVING
    with pytest.raises(TimeoutError):  # no queued SERVING/NOT_SERVING backlog
        await asyncio.wait_for(anext(stream), 0.05)
    await stream.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watch_of_unknown_service_reports_service_unknown_then_the_real_status() -> None:
    servicer = HealthServicer(build_schema())
    stream = servicer.watch(health_request("late.Service"), FakeContext())
    assert (await anext(stream)).status == SERVICE_UNKNOWN
    servicer.set("late.Service", SERVING)
    assert (await asyncio.wait_for(anext(stream), WAIT)).status == SERVING
    await stream.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watch_deregisters_when_the_stream_closes() -> None:
    servicer = HealthServicer(build_schema())
    stream = servicer.watch(health_request(""), FakeContext())
    await anext(stream)
    assert servicer.watcher_count("") == 1
    await stream.aclose()
    assert servicer.watcher_count("") == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_watch_streams_delivers_the_pending_status_before_ending() -> None:
    servicer = HealthServicer(build_schema())
    stream = servicer.watch(health_request(""), FakeContext())
    assert (await anext(stream)).status == SERVING
    servicer.enter_graceful_shutdown()
    servicer.close_watch_streams()
    assert (await asyncio.wait_for(anext(stream), WAIT)).status == NOT_SERVING
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), WAIT)
    assert servicer.watcher_count("") == 0


# --- Reflection semantics (in-process) ------------------------------------------------------


def make_reflection(*, allowlist_only: bool = False) -> ReflectionServicer:
    listed = (WORK_SERVICE, HEALTH_SERVICE, REFLECTION_SERVICE)
    return ReflectionServicer(build_schema(), listed, allowlist_only=allowlist_only)


@pytest.mark.unit
def test_list_services_returns_only_the_names_it_was_given_sorted() -> None:
    reply = make_reflection().answer(reflection_request(list_services="*"))
    names = [item.name for item in reply.list_services_response.service]
    assert names == sorted([WORK_SERVICE, HEALTH_SERVICE, REFLECTION_SERVICE])
    assert ADMIN_SERVICE not in names


@pytest.mark.unit
def test_file_containing_symbol_returns_the_file_then_its_dependencies() -> None:
    reply = make_reflection().answer(reflection_request(file_containing_symbol=WORK_SERVICE))
    blobs = reply.file_descriptor_response.file_descriptor_proto
    files = [_pb.FileDescriptorProto.FromString(blob) for blob in blobs]
    assert [f.name for f in files] == ["lab/work/v1/work.proto", "lab/work/v1/types.proto"]
    assert files[0].service[0].method[0].name == "Do"
    assert reply.original_request.file_containing_symbol == WORK_SERVICE


@pytest.mark.unit
def test_file_by_filename_and_method_symbol_resolve_to_the_same_file() -> None:
    by_name = make_reflection().answer(
        reflection_request(file_by_filename="grpc/health/v1/health.proto")
    )
    by_symbol = make_reflection().answer(
        reflection_request(file_containing_symbol=f"{HEALTH_SERVICE}.Watch")
    )
    assert by_name.file_descriptor_response == by_symbol.file_descriptor_response


@pytest.mark.unit
def test_the_protobuf_pool_alone_does_not_resolve_method_symbols() -> None:
    pool = build_schema().pool
    assert pool.FindFileContainingSymbol(HEALTH_SERVICE).name == "grpc/health/v1/health.proto"
    with pytest.raises(KeyError):
        pool.FindFileContainingSymbol(f"{HEALTH_SERVICE}.Watch")  # reflection.proto allows it
    unknown_method = make_reflection().answer(
        reflection_request(file_containing_symbol=f"{HEALTH_SERVICE}.Nope")
    )
    assert unknown_method.error_response.error_code == 5


@pytest.mark.unit
def test_errors_carry_grpc_status_numbers_inside_a_normal_response() -> None:
    reflection = make_reflection()
    missing = reflection.answer(reflection_request(file_containing_symbol="no.Such"))
    assert missing.error_response.error_code == grpc.StatusCode.NOT_FOUND.value[0] == 5
    empty = reflection.answer(reflection_request(host="only-a-host"))
    assert empty.error_response.error_code == grpc.StatusCode.INVALID_ARGUMENT.value[0] == 3
    unsupported = reflection.answer(reflection_request(all_extension_numbers_of_type=WORK_SERVICE))
    assert unsupported.error_response.error_code == grpc.StatusCode.UNIMPLEMENTED.value[0] == 12


@pytest.mark.unit
def test_reference_style_reflection_describes_a_service_it_does_not_list() -> None:
    reply = make_reflection().answer(reflection_request(file_containing_symbol=ADMIN_SERVICE))
    blobs = reply.file_descriptor_response.file_descriptor_proto
    assert _pb.FileDescriptorProto.FromString(blobs[0]).service[0].method[0].name == "Purge"


@pytest.mark.unit
def test_allowlist_mode_hides_unlisted_files_but_serves_listed_closures() -> None:
    reflection = make_reflection(allowlist_only=True)
    hidden = reflection.answer(reflection_request(file_containing_symbol=ADMIN_SERVICE))
    assert hidden.error_response.error_code == 5
    hidden_by_file = reflection.answer(
        reflection_request(file_by_filename="lab/internal/v1/admin.proto")
    )
    assert hidden_by_file.error_response.error_code == 5
    shown = reflection.answer(reflection_request(file_containing_symbol=WORK_SERVICE))
    assert len(shown.file_descriptor_response.file_descriptor_proto) == 2


# --- over a real loopback socket ------------------------------------------------------------


@pytest.mark.socket
@pytest.mark.asyncio
async def test_check_over_the_wire_overall_named_and_unknown() -> None:
    async with LabServer() as lab, lab.channel() as channel:
        client = Client(channel, lab.schema)
        assert await client.check("") == SERVING
        lab.health.set(WORK_SERVICE, NOT_SERVING)
        assert await client.check(WORK_SERVICE) == NOT_SERVING
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await client.check("pkg.Unregistered")
        assert info.value.code() == grpc.StatusCode.NOT_FOUND


@pytest.mark.socket
@pytest.mark.asyncio
async def test_server_without_the_health_service_answers_unimplemented() -> None:
    async with LabServer(with_health=False) as lab, lab.channel() as channel:
        client = Client(channel, lab.schema)
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await client.check("")
        assert info.value.code() == grpc.StatusCode.UNIMPLEMENTED


@pytest.mark.socket
@pytest.mark.asyncio
async def test_watch_over_the_wire_streams_changes_and_survives_an_unknown_service() -> None:
    async with LabServer() as lab, lab.channel() as channel:
        client = Client(channel, lab.schema)
        overall = client.watch("")
        first = await asyncio.wait_for(overall.read(), WAIT)
        assert first.status == SERVING
        lab.health.set("", NOT_SERVING)
        assert (await asyncio.wait_for(overall.read(), WAIT)).status == NOT_SERVING
        overall.cancel()

        unknown = client.watch("not.Registered")
        assert (await asyncio.wait_for(unknown.read(), WAIT)).status == SERVICE_UNKNOWN
        assert not unknown.done()  # unlike Check, an unknown name does not end the call
        lab.health.set("not.Registered", SERVING)
        assert (await asyncio.wait_for(unknown.read(), WAIT)).status == SERVING
        unknown.cancel()


@pytest.mark.socket
@pytest.mark.asyncio
async def test_not_serving_is_advisory_and_does_not_reject_rpcs() -> None:
    async with LabServer() as lab, lab.channel() as channel:
        client = Client(channel, lab.schema)
        lab.health.enter_graceful_shutdown()
        assert await client.check("") == NOT_SERVING
        assert await client.do("still-served") == "still-served"


@pytest.mark.socket
@pytest.mark.asyncio
async def test_reflection_over_the_wire_lists_services_and_describes_a_symbol() -> None:
    async with LabServer() as lab, lab.channel() as channel:
        client = Client(channel, lab.schema)
        call = client.reflection_rpc()
        await call.write(reflection_request(list_services="*"))
        listing = await asyncio.wait_for(call.read(), WAIT)
        names = [item.name for item in listing.list_services_response.service]
        assert names == sorted([WORK_SERVICE, HEALTH_SERVICE, REFLECTION_SERVICE])
        await call.write(reflection_request(file_containing_symbol=HEALTH_SERVICE))
        described = await asyncio.wait_for(call.read(), WAIT)
        proto = _pb.FileDescriptorProto.FromString(
            described.file_descriptor_response.file_descriptor_proto[0]
        )
        assert [m.name for m in proto.service[0].method] == ["Check", "List", "Watch"]
        await call.done_writing()
        assert await asyncio.wait_for(call.code(), WAIT) == grpc.StatusCode.OK


@pytest.mark.socket
@pytest.mark.asyncio
async def test_stop_with_grace_finishes_inflight_rpcs_and_refuses_new_ones() -> None:
    async with LabServer() as lab, lab.channel() as channel:
        client = Client(channel, lab.schema)
        inflight = asyncio.ensure_future(client.do("block", deadline=20))
        await asyncio.wait_for(lab.work.entered.wait(), WAIT)
        stopping = asyncio.ensure_future(lab.server.stop(WAIT))
        refusals = await wait_for_unavailable(client)
        assert set(refusals) <= {grpc.StatusCode.CANCELLED, grpc.StatusCode.UNAVAILABLE}
        assert "probe" not in lab.work.seen  # refused before the handler ever ran
        assert not stopping.done()  # stop() is still waiting for the blocked handler
        lab.work.gate.set()
        assert await asyncio.wait_for(inflight, WAIT) == "block"
        await asyncio.wait_for(stopping, WAIT)
        assert lab.work.outcomes == ["completed"]


@pytest.mark.socket
@pytest.mark.asyncio
async def test_grace_expiry_cancels_inflight_rpcs_and_the_client_sees_unavailable() -> None:
    async with LabServer() as lab, lab.channel() as channel:
        client = Client(channel, lab.schema)
        inflight = asyncio.ensure_future(client.do("block", deadline=20))
        await asyncio.wait_for(lab.work.entered.wait(), WAIT)
        started = time.monotonic()
        await asyncio.wait_for(lab.server.stop(0.3), WAIT)
        elapsed = time.monotonic() - started
        assert 0.25 <= elapsed < 3.0
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await asyncio.wait_for(inflight, WAIT)
        assert info.value.code() == grpc.StatusCode.UNAVAILABLE
        assert lab.work.outcomes == ["cancelled"]


@pytest.mark.socket
@pytest.mark.asyncio
async def test_stop_with_none_aborts_inflight_rpcs_immediately() -> None:
    async with LabServer() as lab, lab.channel() as channel:
        client = Client(channel, lab.schema)
        inflight = asyncio.ensure_future(client.do("block", deadline=20))
        await asyncio.wait_for(lab.work.entered.wait(), WAIT)
        started = time.monotonic()
        await asyncio.wait_for(lab.server.stop(None), WAIT)
        assert time.monotonic() - started < 2.0
        with pytest.raises(grpc.aio.AioRpcError):
            await asyncio.wait_for(inflight, WAIT)
        assert lab.work.outcomes == ["cancelled"]


@pytest.mark.socket
@pytest.mark.asyncio
async def test_a_second_stop_with_a_smaller_grace_shortens_the_first() -> None:
    async with LabServer() as lab, lab.channel() as channel:
        client = Client(channel, lab.schema)
        inflight = asyncio.ensure_future(client.do("block", deadline=60))
        await asyncio.wait_for(lab.work.entered.wait(), WAIT)
        first = asyncio.ensure_future(lab.server.stop(30.0))
        await wait_for_unavailable(client)
        await asyncio.wait_for(lab.server.stop(0.1), WAIT)  # would take 30 s if it did not win
        await asyncio.wait_for(first, WAIT)
        with pytest.raises(grpc.aio.AioRpcError):
            await asyncio.wait_for(inflight, WAIT)
        assert lab.work.outcomes == ["cancelled"]


@pytest.mark.socket
@pytest.mark.asyncio
async def test_an_open_watch_stream_pins_stop_until_the_grace_expires() -> None:
    async with LabServer() as lab, lab.channel() as channel:
        client = Client(channel, lab.schema)
        watch = client.watch("")
        assert (await asyncio.wait_for(watch.read(), WAIT)).status == SERVING
        report = await asyncio.wait_for(
            drain_and_stop(lab, drain_delay=0.0, grace=0.6, end_watches=False), WAIT
        )
        assert report.stop_seconds >= 0.5  # nothing else was running: the Watch held it open
        assert (await asyncio.wait_for(watch.read(), WAIT)).status == NOT_SERVING
        assert await asyncio.wait_for(watch.code(), WAIT) == grpc.StatusCode.UNAVAILABLE


@pytest.mark.socket
@pytest.mark.asyncio
async def test_ending_watch_streams_lets_stop_return_well_inside_the_grace() -> None:
    async with LabServer() as lab, lab.channel() as channel:
        client = Client(channel, lab.schema)
        watch = client.watch("")
        assert (await asyncio.wait_for(watch.read(), WAIT)).status == SERVING
        report = await asyncio.wait_for(
            drain_and_stop(lab, drain_delay=0.0, grace=4.0, end_watches=True), WAIT
        )
        assert report.stop_seconds < 2.0
        assert (await asyncio.wait_for(watch.read(), WAIT)).status == NOT_SERVING
        assert await next_or_none(watch, WAIT) is None
        assert await asyncio.wait_for(watch.code(), WAIT) == grpc.StatusCode.OK


@pytest.mark.socket
@pytest.mark.asyncio
async def test_drain_delay_window_still_serves_rpcs_and_events_are_ordered() -> None:
    async with LabServer() as lab, lab.channel() as channel:
        client = Client(channel, lab.schema)
        watch = client.watch("")
        assert (await asyncio.wait_for(watch.read(), WAIT)).status == SERVING
        draining = asyncio.ensure_future(
            drain_and_stop(lab, drain_delay=0.4, grace=WAIT, end_watches=True)
        )
        assert (await asyncio.wait_for(watch.read(), WAIT)).status == NOT_SERVING
        assert await client.check("") == NOT_SERVING
        assert await client.do("during-drain-delay") == "during-drain-delay"  # not stopped yet
        report = await asyncio.wait_for(draining, WAIT)
        assert report.events == ["not_serving", "watches_closed", "drain_delay_elapsed", "stopped"]
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await client.do("after-stop", deadline=1.0)
        assert info.value.code() == grpc.StatusCode.UNAVAILABLE
