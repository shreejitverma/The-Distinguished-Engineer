"""AIP-151 long-running operations lab: 202 start, Operation polling, cancel, and failures."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic import ValidationError

from api_labs.f06_bulk_long_running_and_error_contracts.long_running_operations import (
    ALREADY_EXISTS,
    CANCELLED,
    LroService,
    Operation,
    OperationTimeoutError,
    PollPolicy,
    PollResult,
    build_app,
    operation_url,
    parse_retry_after,
    wait_for_operation,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
IMPORT = "/v1/publishers/acme/books:import"


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def new_service(retry_after: int = 1) -> LroService:
    return LroService(retry_after_seconds=retry_after, clock=lambda: NOW)


@dataclass
class FakeTime:
    """Virtual clock whose sleep advances time and runs one worker step per call."""

    service: LroService
    op_id: str
    advance_worker: bool = True
    t: float = 0.0
    slept: list[float] = field(default_factory=list[float])

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds
        if self.advance_worker:
            self.service.step(self.op_id)

    async def poll(
        self, client: httpx.AsyncClient, start: httpx.Response, policy: PollPolicy | None = None
    ) -> PollResult:
        return await wait_for_operation(
            client,
            operation_url(start),
            policy=policy,
            sleep=self.sleep,
            monotonic=self.monotonic,
            now=lambda: NOW,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_returns_202_with_location_retry_after_and_running_operation() -> None:
    service = new_service()
    async with client_for(build_app(service)) as client:
        start = await client.post(IMPORT, json={"titles": ["Emma", "Dune"]})

    assert start.status_code == 202
    assert start.headers["Location"] == "/v1/operations/op-000001"
    assert start.headers["Retry-After"] == "1"
    assert start.headers["Cache-Control"] == "no-store"
    assert operation_url(start) == "http://test/v1/operations/op-000001"
    assert start.json() == {
        "name": "operations/op-000001",
        "metadata": {
            "@type": "type.googleapis.com/lab.library.v1.ImportBooksMetadata",
            "createTime": "2026-11-11T12:00:00Z",
            "processedCount": 0,
            "totalCount": 2,
            "progressPercent": 0,
            "cancelRequested": False,
        },
    }
    assert service.operations["op-000001"].processed == 0


@pytest.mark.unit
def test_protojson_omits_false_done_and_the_lab_server_matches() -> None:
    descriptor_pb2: Any = importlib.import_module("google.protobuf.descriptor_pb2")
    descriptor_pool: Any = importlib.import_module("google.protobuf.descriptor_pool")
    json_format: Any = importlib.import_module("google.protobuf.json_format")
    message_factory: Any = importlib.import_module("google.protobuf.message_factory")

    field_type: Any = descriptor_pb2.FieldDescriptorProto
    file_proto: Any = descriptor_pb2.FileDescriptorProto(
        name="lab/operation.proto", package="lab", syntax="proto3"
    )
    message: Any = file_proto.message_type.add(name="Operation")
    message.field.add(name="name", number=1, type=field_type.TYPE_STRING, label=1)
    message.field.add(name="done", number=3, type=field_type.TYPE_BOOL, label=1)
    pool: Any = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    operation_class: Any = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("lab.Operation")
    )

    running = json_format.MessageToDict(operation_class(name="operations/a", done=False))
    finished = json_format.MessageToDict(operation_class(name="operations/a", done=True))
    assert running == {"name": "operations/a"}
    assert finished == {"name": "operations/a", "done": True}

    lab_running = Operation(name="operations/a").to_proto_json()
    assert "done" not in lab_running
    assert Operation.model_validate(lab_running).done is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_poller_sleeps_for_retry_after_until_done() -> None:
    service = new_service(retry_after=2)
    progress: list[int] = []

    async def record_progress(response: httpx.Response) -> None:
        if response.request.method == "GET":
            await response.aread()
            progress.append(response.json()["metadata"]["progressPercent"])

    app = build_app(service)
    transport = httpx.ASGITransport(app=app)
    hooks = {"response": [record_progress]}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", event_hooks=hooks
    ) as client:
        start = await client.post(IMPORT, json={"titles": ["Emma", "Dune", "Ulysses"]})
        fake = FakeTime(service, "op-000001")
        result = await fake.poll(client, start)

    assert result.delays == [2.0, 2.0, 2.0]
    assert progress == [0, 33, 66, 100]
    assert result.operation.done is True
    assert result.operation.error is None
    assert result.operation.response == {
        "@type": "type.googleapis.com/lab.library.v1.ImportBooksResponse",
        "importedCount": 3,
    }


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server_hint", "expected_delay"),
    [(86400, 30.0), (0, 0.5)],
    ids=["one-day-hint-capped", "zero-hint-floored"],
)
async def test_poller_clamps_retry_after(server_hint: int, expected_delay: float) -> None:
    service = new_service(retry_after=server_hint)
    async with client_for(build_app(service)) as client:
        start = await client.post(IMPORT, json={"titles": ["Emma"]})
        fake = FakeTime(service, "op-000001")
        result = await fake.poll(client, start)

    assert result.delays == [expected_delay]
    assert result.operation.done is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("120", 120.0),
        (" 7 ", 7.0),
        (format_datetime(NOW + timedelta(seconds=90), usegmt=True), 90.0),
        (format_datetime(NOW - timedelta(seconds=90), usegmt=True), 0.0),
        (format_datetime(NOW.replace(tzinfo=None) + timedelta(seconds=90)), 90.0),
        ("soon", None),
        ("-5", None),
        ("1.5", None),
        ("\u00b2", None),
        (None, None),
    ],
    ids=[
        "delay-seconds",
        "whitespace",
        "http-date-future",
        "http-date-past",
        "http-date-minus-zero-is-utc",
        "garbage",
        "negative",
        "fraction",
        "non-ascii-digit",
        "absent",
    ],
)
def test_parse_retry_after(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header, NOW) == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_timeout_does_not_cancel_the_server_operation() -> None:
    service = new_service()
    async with client_for(build_app(service)) as client:
        start = await client.post(IMPORT, json={"titles": ["Emma"]})
        fake = FakeTime(service, "op-000001", advance_worker=False)
        with pytest.raises(OperationTimeoutError) as caught:
            await fake.poll(client, start, PollPolicy(timeout=2.5))

    assert fake.slept == [1.0, 1.0, 0.5]
    assert caught.value.last.done is False
    record = service.operations["op-000001"]
    assert (record.done, record.cancel_requested) == (False, False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execution_failure_is_a_200_get_carrying_operation_error() -> None:
    service = new_service()
    service.library["publishers/acme"] = ["Dune"]
    async with client_for(build_app(service)) as client:
        start = await client.post(IMPORT, json={"titles": ["Emma", "Dune"]})
        service.run_to_completion("op-000001")
        polled = await client.get(operation_url(start))

    assert start.status_code == 202
    assert polled.status_code == 200
    assert polled.raise_for_status() is polled
    body = polled.json()
    assert body["done"] is True
    assert body["error"] == {"code": ALREADY_EXISTS, "message": "book 'Dune' already exists"}
    assert "response" not in body
    assert "Retry-After" not in polled.headers
    assert polled.headers["Cache-Control"] == "no-store"
    assert service.library["publishers/acme"] == ["Dune", "Emma"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_errors_are_synchronous_and_parallel_work_is_aborted() -> None:
    service = new_service()
    async with client_for(build_app(service)) as client:
        empty = await client.post(IMPORT, json={"titles": []})
        oversized = await client.post(IMPORT, json={"titles": [f"t{i}" for i in range(101)]})
        first = await client.post(IMPORT, json={"titles": ["Emma"]})
        parallel = await client.post(IMPORT, json={"titles": ["Dune"]})
        other_parent = await client.post(
            "/v1/publishers/globex/books:import", json={"titles": ["Dune"]}
        )
        service.run_to_completion("op-000001")
        after_done = await client.post(IMPORT, json={"titles": ["Dune"]})

    assert empty.status_code == 422
    assert oversized.status_code == 422
    assert first.status_code == 202
    assert parallel.status_code == 409
    assert parallel.json()["error"]["status"] == "ABORTED"
    assert "Location" not in parallel.headers
    assert other_parent.status_code == 202
    assert after_done.status_code == 202
    assert sorted(service.operations) == ["op-000001", "op-000002", "op-000003"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_running_operation_ends_cancelled_without_rollback() -> None:
    service = new_service()
    async with client_for(build_app(service)) as client:
        await client.post(IMPORT, json={"titles": ["Emma", "Dune", "Ulysses"]})
        service.step("op-000001")
        cancel = await client.post("/v1/operations/op-000001:cancel")
        pending = await client.get("/v1/operations/op-000001")
        service.step("op-000001")
        final = await client.get("/v1/operations/op-000001")

    assert (cancel.status_code, cancel.json()) == (200, {})
    assert "done" not in pending.json()
    assert pending.json()["metadata"]["cancelRequested"] is True
    body = final.json()
    assert body["done"] is True
    assert body["error"]["code"] == CANCELLED
    assert "response" not in body
    assert body["metadata"]["processedCount"] == 1
    assert service.library["publishers/acme"] == ["Emma"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_after_done_keeps_the_response() -> None:
    service = new_service()
    async with client_for(build_app(service)) as client:
        await client.post(IMPORT, json={"titles": ["Emma"]})
        service.run_to_completion("op-000001")
        cancel = await client.post("/v1/operations/op-000001:cancel")
        final = await client.get("/v1/operations/op-000001")

    assert cancel.status_code == 200
    body = final.json()
    assert "error" not in body
    assert body["response"]["importedCount"] == 1
    assert body["metadata"]["cancelRequested"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_colon_custom_method_routing_and_unknown_operations() -> None:
    service = new_service()
    async with client_for(build_app(service)) as client:
        await client.post(IMPORT, json={"titles": ["Emma"]})
        get_on_cancel_url = await client.get("/v1/operations/op-000001:cancel")
        cancel_unknown = await client.post("/v1/operations/op-999999:cancel")
        get_unknown = await client.get("/v1/operations/op-999999")

    assert get_on_cancel_url.status_code == 404
    assert get_on_cancel_url.json() == {"detail": "operations/op-000001:cancel not found"}
    assert service.operations["op-000001"].cancel_requested is False
    assert cancel_unknown.status_code == 404
    assert get_unknown.status_code == 404


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {"name": "operations/x", "response": {}},
        {"name": "operations/x", "done": True, "response": {}, "error": {"code": 2, "message": ""}},
        {"name": "operations/x", "done": True, "result": {}},
    ],
    ids=["result-before-done", "both-oneof-members", "unknown-field"],
)
def test_operation_model_rejects_impossible_states(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Operation.model_validate(payload)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_background_tasks_finish_before_asgi_transport_returns() -> None:
    durable = new_service()
    inline = new_service()
    async with client_for(build_app(durable)) as client:
        await client.post(IMPORT, json={"titles": ["Emma", "Dune"]})
    async with client_for(build_app(inline, run_in_background_tasks=True)) as client:
        start = await client.post(IMPORT, json={"titles": ["Emma", "Dune"]})
        first_poll = await client.get(operation_url(start))

    assert durable.operations["op-000001"].processed == 0
    assert "done" not in start.json()
    assert inline.operations["op-000001"].processed == 2
    assert first_poll.json()["done"] is True


@pytest.mark.unit
def test_operation_url_requires_202_with_location() -> None:
    request = httpx.Request("POST", "http://test/v1/publishers/acme/books:import")
    ok_without_location = httpx.Response(200, request=request)
    accepted = httpx.Response(202, headers={"Location": "/v1/operations/op-7"}, request=request)

    with pytest.raises(ValueError, match="expected 202 with Location, got 200"):
        operation_url(ok_without_location)
    assert operation_url(accepted) == "http://test/v1/operations/op-7"
