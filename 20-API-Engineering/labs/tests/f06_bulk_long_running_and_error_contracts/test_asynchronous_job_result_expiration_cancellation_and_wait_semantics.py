"""Unit tests for LRO :wait long-polling, completion-relative TTL, cancellation, and replay."""

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from api_labs.f06_bulk_long_running_and_error_contracts.lro_waiter import (
    CODE_CANCELLED,
    CODE_INTERNAL,
    ExportRequest,
    JobResult,
    ManualClock,
    OperationStore,
    create_app,
)

TTL = 60.0
GUARD = 2.0  # seconds; a test that needs longer than this has hung


@dataclass
class GatedJob:
    """Export job that blocks until the test opens its gate."""

    gate: asyncio.Event = field(default_factory=asyncio.Event)
    runs: list[str] = field(default_factory=list[str])

    async def __call__(self, request: ExportRequest) -> JobResult:
        self.runs.append(request.table)
        await self.gate.wait()
        return {"table": request.table, "rows": 3}


def make(max_wait: float = 30.0) -> tuple[GatedJob, ManualClock, OperationStore, httpx.AsyncClient]:
    job, clock = GatedJob(), ManualClock()
    store = OperationStore(job, clock, ttl=TTL, max_wait=max_wait)
    transport = httpx.ASGITransport(app=create_app(store))
    return job, clock, store, httpx.AsyncClient(transport=transport, base_url="http://test")


async def until(predicate: Callable[[], bool], max_turns: int = 10_000) -> None:
    """Yield to the event loop until predicate holds; counted turns keep it deterministic."""
    for _ in range(max_turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"condition not reached in {max_turns} event loop turns")


async def start(client: httpx.AsyncClient, request_id: str, table: str = "orders") -> str:
    response = await client.post("/v1/exports", json={"request_id": request_id, "table": table})
    assert response.status_code == 202
    name: str = response.json()["name"]
    return name


def new_id() -> str:
    return str(uuid.uuid4())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_on_finished_operation_returns_at_once_because_done_is_level_triggered() -> None:
    job, _, store, client = make()
    async with client:
        name = await start(client, new_id())
        job.gate.set()
        await until(lambda: (op := store.get(name)) is not None and op.done)
        async with asyncio.timeout(GUARD):
            response = await client.post(f"/v1/{name}:wait", json={"timeout": "30s"})
    assert response.status_code == 200
    assert response.json()["done"] is True
    assert response.json()["response"] == {"table": "orders", "rows": 3}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_completion_wakes_every_waiter_before_its_timeout() -> None:
    job, _, store, client = make()
    async with client:
        name = await start(client, new_id())
        waits = [
            asyncio.create_task(client.post(f"/v1/{name}:wait", json={"timeout": "30s"}))
            for _ in range(3)
        ]
        await until(lambda: store.active_waiters == 3)
        job.gate.set()
        async with asyncio.timeout(GUARD):
            responses = await asyncio.gather(*waits)
    assert [r.json()["done"] for r in responses] == [True, True, True]
    assert store.active_waiters == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_timeout_returns_latest_state_as_200_not_an_error() -> None:
    _, _, store, client = make()
    async with client:
        name = await start(client, new_id())
        async with asyncio.timeout(GUARD):
            response = await client.post(f"/v1/{name}:wait", json={"timeout": "0.05s"})
    assert response.status_code == 200
    body = response.json()
    assert body["done"] is False
    assert "response" not in body
    assert "error" not in body
    assert store.active_waiters == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_clamps_client_timeout_to_server_maximum() -> None:
    _, _, store, client = make(max_wait=0.05)
    async with client:
        name = await start(client, new_id())
        begin = time.perf_counter()
        async with asyncio.timeout(GUARD):
            response = await client.post(f"/v1/{name}:wait", json={"timeout": "3600s"})
        elapsed = time.perf_counter() - begin
    assert response.status_code == 200
    assert response.json()["done"] is False
    assert 0.05 <= elapsed < GUARD
    assert store.active_waiters == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_releases_its_waiter_when_the_client_disconnects() -> None:
    job, _, store, client = make(max_wait=30.0)
    async with client:
        name = await start(client, new_id())
    body = json.dumps({"timeout": "30s"}).encode()
    client_gone = asyncio.Event()
    delivered = False
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await client_gone.wait()  # what Uvicorn does: block until connection_lost
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        sent.append(dict(message))

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/v1/{name}:wait",
        "raw_path": f"/v1/{name}:wait".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    task = asyncio.create_task(create_app(store)(scope, receive, send))
    await until(lambda: store.active_waiters == 1)
    client_gone.set()
    async with asyncio.timeout(GUARD):  # without the disconnect watcher this waits 30 s
        await task
    assert store.active_waiters == 0
    assert job.runs == ["orders"]
    op = store.get(name)
    assert op is not None
    assert op.done is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_finishes_operation_with_code_1_and_wakes_the_waiter() -> None:
    job, _, store, client = make()
    async with client:
        name = await start(client, new_id())
        waiter = asyncio.create_task(client.post(f"/v1/{name}:wait", json={"timeout": "30s"}))
        await until(lambda: store.active_waiters == 1)
        cancel = await client.post(f"/v1/{name}:cancel", json={})
        async with asyncio.timeout(GUARD):
            waited = await waiter
        polled = await client.get(f"/v1/{name}")
    assert cancel.status_code == 200
    assert cancel.json() == {}
    assert waited.json()["done"] is True
    assert waited.json()["error"]["code"] == CODE_CANCELLED
    assert "response" not in waited.json()
    assert polled.json() == waited.json()
    assert job.gate.is_set() is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_after_completion_keeps_the_response() -> None:
    job, _, store, client = make()
    async with client:
        name = await start(client, new_id())
        job.gate.set()
        await until(lambda: (op := store.get(name)) is not None and op.done)
        cancel = await client.post(f"/v1/{name}:cancel", json={})
        polled = await client.get(f"/v1/{name}")
    assert cancel.status_code == 200
    assert polled.json()["response"] == {"table": "orders", "rows": 3}
    assert "error" not in polled.json()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_job_finishes_with_code_13_and_hides_the_exception() -> None:
    async def failing(request: ExportRequest) -> JobResult:
        raise RuntimeError(f"disk full while writing {request.table}")

    store = OperationStore(failing, ManualClock(), ttl=TTL, max_wait=30.0)
    transport = httpx.ASGITransport(app=create_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        name = await start(client, new_id())
        async with asyncio.timeout(GUARD):
            waited = await client.post(f"/v1/{name}:wait", json={"timeout": "30s"})
    assert waited.json()["done"] is True
    assert waited.json()["error"] == {"code": CODE_INTERNAL, "message": "Export failed."}
    assert "response" not in waited.json()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ttl_counts_from_completion_not_from_creation() -> None:
    job, clock, store, client = make()
    async with client:
        name = await start(client, new_id())
        clock.advance(10 * TTL)  # a slow job outlives the TTL while it is still running
        running = await client.get(f"/v1/{name}")
        job.gate.set()
        await until(lambda: (op := store.get(name)) is not None and op.done)
        clock.advance(TTL - 0.001)
        last_moment = await client.get(f"/v1/{name}")
        clock.advance(0.001)
        expired = await client.get(f"/v1/{name}")
    assert running.status_code == 200
    assert running.json()["done"] is False
    assert last_moment.status_code == 200
    assert last_moment.json()["metadata"] == {
        "create_time": "2026-01-01T00:00:00Z",
        "end_time": "2026-01-01T00:10:00Z",
        "expire_time": "2026-01-01T00:11:00Z",
    }
    assert expired.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_answers_404_when_the_record_expires_while_the_waiter_is_parked() -> None:
    clock = ManualClock()
    gate = asyncio.Event()

    async def expiring_job(request: ExportRequest) -> JobResult:
        await gate.wait()
        # Runs before the waiter resumes: the record finishes, then its whole TTL elapses.
        asyncio.get_running_loop().call_soon(clock.advance, TTL)
        return {"table": request.table}

    store = OperationStore(expiring_job, clock, ttl=TTL, max_wait=30.0)
    transport = httpx.ASGITransport(app=create_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        name = await start(client, new_id())
        waiting = asyncio.create_task(client.post(f"/v1/{name}:wait", json={"timeout": "30s"}))
        await until(lambda: store.active_waiters == 1)
        gate.set()
        async with asyncio.timeout(GUARD):
            response = await waiting
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert store.active_waiters == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expired_operation_is_invisible_before_the_sweep_and_the_sweep_frees_both_maps() -> (
    None
):
    job, clock, store, client = make()
    async with client:
        name = await start(client, new_id())
        job.gate.set()
        await until(lambda: (op := store.get(name)) is not None and op.done)
        clock.advance(TTL)
        statuses = [
            (await client.get(f"/v1/{name}")).status_code,
            (await client.post(f"/v1/{name}:wait", json={"timeout": "1s"})).status_code,
            (await client.post(f"/v1/{name}:cancel", json={})).status_code,
        ]
    assert statuses == [404, 404, 404]
    assert (store.operation_count, store.request_id_count) == (1, 1)  # not swept yet
    assert store.sweep() == 1
    assert (store.operation_count, store.request_id_count) == (0, 0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replay_with_same_request_id_returns_same_operation_and_runs_job_once() -> None:
    job, _, store, client = make()
    request_id = new_id()
    async with client:
        first = await start(client, request_id)
        job.gate.set()
        await until(lambda: (op := store.get(first)) is not None and op.done)
        replay = await client.post(
            "/v1/exports", json={"request_id": request_id, "table": "orders"}
        )
    assert replay.status_code == 202
    assert replay.json()["name"] == first
    assert replay.json()["done"] is True
    assert replay.json()["response"] == {"table": "orders", "rows": 3}
    assert job.runs == ["orders"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_replays_start_exactly_one_job() -> None:
    job, _, store, client = make()
    request_id = new_id()
    async with client:
        names = await asyncio.gather(*(start(client, request_id) for _ in range(5)))
        await until(lambda: len(job.runs) >= 1)
        await asyncio.sleep(0)
    assert len(set(names)) == 1
    assert store.job_starts == 1
    assert job.runs == ["orders"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_id_reused_with_different_payload_is_422() -> None:
    _, _, store, client = make()
    request_id = new_id()
    async with client:
        await start(client, request_id, table="orders")
        reused = await client.post(
            "/v1/exports", json={"request_id": request_id, "table": "invoices"}
        )
    assert reused.status_code == 422
    assert reused.headers["content-type"] == "application/problem+json"
    assert store.job_starts == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replay_after_retention_window_runs_the_job_again() -> None:
    job, clock, store, client = make()
    request_id = new_id()
    async with client:
        first = await start(client, request_id)
        job.gate.set()
        await until(lambda: (op := store.get(first)) is not None and op.done)
        clock.advance(TTL)
        assert store.sweep() == 1
        second = await start(client, request_id)
        await until(lambda: len(job.runs) == 2)
    assert second != first
    assert job.runs == ["orders", "orders"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("duration", ["5", "-1s", "1h", "1.0000000001s"])
async def test_wait_rejects_malformed_duration(duration: str) -> None:
    _, _, store, client = make()
    async with client:
        name = await start(client, new_id())
        response = await client.post(f"/v1/{name}:wait", json={"timeout": duration})
    assert response.status_code == 422
    assert store.active_waiters == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_with_custom_method_suffix_is_rejected_not_looked_up() -> None:
    _, _, _, client = make()
    async with client:
        name = await start(client, new_id())
        response = await client.get(f"/v1/{name}:wait")
    assert response.status_code == 422
