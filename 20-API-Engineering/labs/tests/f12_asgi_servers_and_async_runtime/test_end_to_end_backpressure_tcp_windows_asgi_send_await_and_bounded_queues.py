"""Backpressure from a slow TCP reader through uvicorn 0.54.0 to an ASGI app's await send()."""

from __future__ import annotations

import asyncio
from itertools import pairwise
from typing import TYPE_CHECKING

import httpx
import pytest
import uvloop
from uvicorn.protocols.http.flow_control import FlowControl

from api_labs.f12_asgi_servers_and_async_runtime.backpressure import (
    ASYNCIO_DEFAULT_HIGH,
    ASYNCIO_DEFAULT_LOW,
    QueueRelay,
    SlowClientTransport,
    StreamProbe,
    awaiting_body,
    chunk_frame_size,
    counting_body,
    hang_up,
    loopback_write_buffer_limits,
    measure_loopback_stall,
    run_until_quiet,
    send_loop_app,
    simulate_slow_client,
    single_send_app,
    start_uvicorn,
    streaming_app,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from api_labs.f12_asgi_servers_and_async_runtime.backpressure import HttpImpl, UvicornHarness

CHUNK = 1024
FRAME = chunk_frame_size(CHUNK)  # b"400\r\n" + 1024 bytes + b"\r\n"
KERNEL = 8 * 1024
TOTAL = 10_000


async def stall(harness: UvicornHarness, probe: StreamProbe) -> None:
    await run_until_quiet(lambda: (probe.produced_chunks, harness.transport.total_written))


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("http", ["h11", "httptools"])
async def test_stalled_client_pauses_writing_and_suspends_send(http: HttpImpl) -> None:
    probe = StreamProbe(chunk_size=CHUNK)
    harness = start_uvicorn(
        streaming_app(lambda: counting_body(probe, TOTAL)), http=http, kernel_capacity=KERNEL
    )
    await stall(harness, probe)
    transport = harness.transport

    assert harness.write_paused
    assert transport.pauses == 1
    assert transport.in_kernel == KERNEL
    assert ASYNCIO_DEFAULT_HIGH < transport.get_write_buffer_size() <= ASYNCIO_DEFAULT_HIGH + FRAME
    # Every produced chunk but the last is on the wire; the last waits inside send().
    head = transport.total_written - (probe.produced_chunks - 1) * FRAME
    assert 0 < head < FRAME
    assert probe.produced_chunks < TOTAL // 100
    await hang_up(harness, probe)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_kib_per_tick_reader_throttles_the_producer_with_hysteresis() -> None:
    probe = StreamProbe(chunk_size=CHUNK)
    harness = start_uvicorn(
        streaming_app(lambda: counting_body(probe, TOTAL)), http="h11", kernel_capacity=KERNEL
    )
    await stall(harness, probe)
    samples = await simulate_slow_client(harness, probe, read_per_tick=CHUNK, ticks=200)

    assert samples[-1].delivered_bytes == 200 * CHUNK
    in_flight_cap = KERNEL + ASYNCIO_DEFAULT_HIGH + FRAME
    for sample in samples:
        assert sample.produced_bytes - sample.delivered_bytes <= in_flight_cap
    longest_idle = idle = 0
    for before, after in pairwise(samples):
        idle = idle + 1 if after.produced_bytes == before.produced_bytes else 0
        longest_idle = max(longest_idle, idle)
    # Paused above 64 KiB, resumed only at <= 16 KiB: about 48 ticks of silence per cycle.
    assert longest_idle >= (ASYNCIO_DEFAULT_HIGH - ASYNCIO_DEFAULT_LOW) // CHUNK - 1
    assert harness.transport.resumes == 4
    await hang_up(harness, probe)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_large_send_overshoots_the_high_watermark_by_the_whole_message() -> None:
    body = b"x" * (1 << 20)
    harness = start_uvicorn(single_send_app(body), http="h11", kernel_capacity=KERNEL)
    await run_until_quiet(lambda: harness.transport.total_written)

    assert harness.app_tasks == 0  # the app returned; its 1 MiB now lives in the transport
    assert harness.write_paused
    assert harness.transport.get_write_buffer_size() >= len(body) - KERNEL
    harness.transport.disconnect(ConnectionResetError("peer reset"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unbounded_queue_keeps_producing_while_send_is_suspended() -> None:
    probe = StreamProbe(chunk_size=CHUNK)
    relay = QueueRelay(probe=probe, total_chunks=2_000, maxsize=0)
    harness = start_uvicorn(relay.app, http="h11", kernel_capacity=KERNEL)
    await stall(harness, probe)

    assert harness.write_paused
    assert probe.produced_chunks == 2_000
    assert relay.queue.qsize() >= 2_000 - 100
    assert relay.peak_queued_bytes >= (2_000 - 100) * CHUNK
    await hang_up(harness, probe)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bounded_queue_holds_at_most_maxsize_chunks() -> None:
    probe = StreamProbe(chunk_size=CHUNK)
    relay = QueueRelay(probe=probe, total_chunks=5_000, maxsize=8)
    harness = start_uvicorn(relay.app, http="h11", kernel_capacity=KERNEL)
    await stall(harness, probe)

    assert harness.write_paused
    assert relay.queue.qsize() == 8
    assert relay.peak_queued_bytes == 8 * CHUNK
    on_wire_or_in_send = (KERNEL + ASYNCIO_DEFAULT_HIGH + FRAME) // FRAME + 2
    assert probe.produced_chunks <= on_wire_or_in_send + 8
    await hang_up(harness, probe)
    assert probe.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disconnect_while_paused_cancels_a_generator_that_awaits() -> None:
    probe = StreamProbe(chunk_size=CHUNK)
    harness = start_uvicorn(
        streaming_app(lambda: awaiting_body(probe, TOTAL)), http="h11", kernel_capacity=KERNEL
    )
    await stall(harness, probe)
    at_stall = probe.produced_chunks
    await hang_up(harness, probe)

    assert probe.closed
    assert harness.app_tasks == 0
    assert probe.produced_chunks == at_stall


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disconnect_while_paused_runs_a_non_yielding_generator_to_exhaustion() -> None:
    probe = StreamProbe(chunk_size=CHUNK)
    harness = start_uvicorn(
        streaming_app(lambda: counting_body(probe, TOTAL)), http="h11", kernel_capacity=KERNEL
    )
    await stall(harness, probe)
    written = harness.transport.total_written
    await hang_up(harness, probe)

    # send() returns at once after the disconnect, so Starlette's cancel never gets a turn.
    assert probe.produced_chunks == TOTAL
    assert harness.transport.total_written == written


@pytest.mark.unit
@pytest.mark.asyncio
async def test_raw_app_without_receive_gets_silent_successful_sends_after_disconnect() -> None:
    probe = StreamProbe(chunk_size=CHUNK)
    harness = start_uvicorn(send_loop_app(probe, TOTAL), http="h11", kernel_capacity=KERNEL)
    await stall(harness, probe)
    written = harness.transport.total_written
    await hang_up(harness, probe)

    assert probe.closed  # the app ran past its last send() with no exception
    assert probe.produced_chunks == TOTAL
    assert harness.transport.total_written == written


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_does_not_yield_while_the_transport_accepts_everything() -> None:
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    seen: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        for _ in range(1_000):
            seen.append(ticks)
            yield b"x" * CHUNK

    ticker_task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    harness = start_uvicorn(streaming_app(body), http="h11", kernel_capacity=10**9)
    await run_until_quiet(lambda: (len(seen), harness.transport.total_written))
    ticker_task.cancel()

    assert len(seen) == 1_000
    assert len(set(seen)) == 1  # no other task ran between the first and last chunk
    assert not harness.write_paused


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpx_asgitransport_buffers_the_whole_body_before_the_first_read() -> None:
    probe = StreamProbe(chunk_size=CHUNK)
    app = streaming_app(lambda: counting_body(probe, TOTAL))
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        client.stream("GET", "/stream") as response,
    ):
        first = await anext(response.aiter_raw())
        assert first
        assert probe.produced_chunks == TOTAL


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uvicorn_flow_control_drain_waits_for_resume_writing() -> None:
    transport = SlowClientTransport(asyncio.Protocol(), kernel_capacity=0)
    flow = FlowControl(transport)
    flow.pause_writing()
    flow.pause_writing()  # idempotent: one pause, one Event
    waiter = asyncio.create_task(flow.drain())
    for _ in range(10):
        await asyncio.sleep(0)
    assert not waiter.done()

    flow.resume_writing()
    await asyncio.wait_for(waiter, 1.0)
    assert not flow.write_paused


@pytest.mark.socket
@pytest.mark.parametrize(
    ("loop_factory", "expected"),
    [
        (asyncio.new_event_loop, (ASYNCIO_DEFAULT_LOW, ASYNCIO_DEFAULT_HIGH)),
        (uvloop.new_event_loop, (16, ASYNCIO_DEFAULT_HIGH)),
    ],
    ids=["asyncio", "uvloop"],
)
def test_default_tcp_write_buffer_limits(
    loop_factory: Callable[[], asyncio.AbstractEventLoop], expected: tuple[int, int]
) -> None:
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        assert runner.run(loopback_write_buffer_limits()) == expected


@pytest.mark.socket
@pytest.mark.parametrize(
    "loop_factory", [asyncio.new_event_loop, uvloop.new_event_loop], ids=["asyncio", "uvloop"]
)
def test_loopback_reader_that_stops_reading_stalls_uvicorn_then_drains(
    loop_factory: Callable[[], asyncio.AbstractEventLoop],
) -> None:
    chunk = 16_384
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        result = runner.run(measure_loopback_stall(chunk_size=chunk, total_chunks=4_096))

    assert result.write_paused_at_stall
    high = result.write_buffer_limits[1]
    assert high < result.user_buffer_at_stall <= high + chunk_frame_size(chunk)
    assert result.beyond_user_buffer_at_stall > 0  # the kernel absorbed bytes first
    assert result.produced_at_stall < result.total_bytes // 10
    assert result.saw_terminator
    assert result.generator_closed
    assert result.produced_at_end == result.total_bytes
    assert result.received_bytes > result.total_bytes
