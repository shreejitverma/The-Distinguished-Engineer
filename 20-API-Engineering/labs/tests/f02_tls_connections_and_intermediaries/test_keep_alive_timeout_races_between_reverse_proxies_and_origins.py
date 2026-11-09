"""Lab: keep-alive idle timeout races between a reverse proxy and an origin."""

from __future__ import annotations

import asyncio
import dataclasses
import math
import random
import select
import socket
import time
from typing import Any

import hypercorn.config
import pytest
import uvicorn
from granian.constants import Interfaces
from granian.http import HTTP1Settings
from granian.server.embed import Server as GranianServer

from api_labs.f02_tls_connections_and_intermediaries.keep_alive_race import (
    MILLISECOND_US,
    SECOND_US,
    Outcome,
    Timeouts,
    classify_reuse,
    is_race_free,
    poisson_race_probability,
    proxy_response,
    race_window,
    replay_lost_race,
    stale_connection_is_rejected,
)

# nginx upstream `keepalive_timeout` default (60s) in front of uvicorn's default (5s).
NGINX_IN_FRONT_OF_UVICORN = Timeouts(
    proxy_idle_us=60 * SECOND_US, origin_idle_us=5 * SECOND_US, one_way_delay_us=500
)


@pytest.mark.unit
def test_default_nginx_over_uvicorn_has_a_one_rtt_race_window() -> None:
    t = NGINX_IN_FRONT_OF_UVICORN
    assert race_window(t) == (5 * SECOND_US - 1_000, 5 * SECOND_US)
    assert not is_race_free(t)
    assert classify_reuse(t, 5 * SECOND_US - 1_001) is Outcome.REUSED
    assert classify_reuse(t, 5 * SECOND_US - 1_000) is Outcome.RACE_LOST
    assert classify_reuse(t, 5 * SECOND_US - 1) is Outcome.RACE_LOST
    assert classify_reuse(t, 5 * SECOND_US) is Outcome.STALE_DETECTED
    assert classify_reuse(t, 60 * SECOND_US) is Outcome.PROXY_EXPIRED


@pytest.mark.unit
def test_equal_timeouts_still_race() -> None:
    t = Timeouts(proxy_idle_us=5 * SECOND_US, origin_idle_us=5 * SECOND_US, one_way_delay_us=500)
    assert race_window(t) == (5 * SECOND_US - 1_000, 5 * SECOND_US)
    assert not is_race_free(t)


@pytest.mark.unit
def test_origin_longer_by_one_rtt_closes_the_window() -> None:
    t = Timeouts(
        proxy_idle_us=60 * SECOND_US, origin_idle_us=60 * SECOND_US + 1_000, one_way_delay_us=500
    )
    assert is_race_free(t)
    assert race_window(t) is None
    # One microsecond less margin reopens a one-microsecond window.
    tight = dataclasses.replace(t, origin_idle_us=t.origin_idle_us - 1)
    assert not is_race_free(tight)
    assert race_window(tight) == (60 * SECOND_US - 1, 60 * SECOND_US)


@pytest.mark.unit
def test_race_window_matches_exhaustive_sweep() -> None:
    t = Timeouts(proxy_idle_us=2_000, origin_idle_us=1_500, one_way_delay_us=100)
    window = race_window(t)
    assert window is not None
    assert window == (1_300, 1_500)
    lo, hi = window
    for gap in range(3_000):
        lost = classify_reuse(t, gap) is Outcome.RACE_LOST
        assert lost == (lo <= gap < hi), gap


@pytest.mark.unit
def test_rule_is_exactly_an_empty_window_for_positive_delay() -> None:
    for proxy_us in range(1, 40):
        for origin_us in range(1, 40):
            for delay_us in range(1, 8):
                t = Timeouts(
                    proxy_idle_us=proxy_us, origin_idle_us=origin_us, one_way_delay_us=delay_us
                )
                assert is_race_free(t) == (race_window(t) is None), t
    # With zero delay there is no window at all, yet the rule still asks for To >= Tp.
    zero = Timeouts(proxy_idle_us=60, origin_idle_us=5, one_way_delay_us=0)
    assert race_window(zero) is None
    assert not is_race_free(zero)


@pytest.mark.unit
def test_lost_race_is_502_for_post_and_a_retry_for_get() -> None:
    assert proxy_response("POST", Outcome.RACE_LOST).status == 502
    assert proxy_response("PATCH", Outcome.RACE_LOST).upstream_attempts == 1
    assert proxy_response("GET", Outcome.RACE_LOST) == proxy_response("PUT", Outcome.RACE_LOST)
    assert proxy_response("GET", Outcome.RACE_LOST).upstream_attempts == 2
    assert proxy_response("POST", Outcome.RACE_LOST, retry_non_idempotent=True).status == 200
    assert proxy_response("POST", Outcome.STALE_DETECTED).status == 200


@pytest.mark.unit
@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE", "get"])
def test_every_idempotent_method_is_retried_after_a_lost_race(method: str) -> None:
    result = proxy_response(method, Outcome.RACE_LOST)
    assert (result.status, result.upstream_attempts) == (200, 2)


@pytest.mark.unit
@pytest.mark.parametrize("method", ["POST", "PATCH", "LOCK", "post"])
def test_non_idempotent_methods_surface_502_after_a_lost_race(method: str) -> None:
    result = proxy_response(method, Outcome.RACE_LOST)
    assert (result.status, result.upstream_attempts) == (502, 1)


@pytest.mark.unit
def test_window_is_clamped_at_zero_when_rtt_exceeds_the_origin_timeout() -> None:
    t = Timeouts(proxy_idle_us=50, origin_idle_us=10, one_way_delay_us=20)
    assert race_window(t) == (0, 10)
    assert classify_reuse(t, 0) is Outcome.RACE_LOST
    assert classify_reuse(t, 10) is Outcome.STALE_DETECTED
    # Every gap in the window loses, so the probability is the whole mass up to hi.
    assert poisson_race_probability(t, 1e6) == pytest.approx(1 - math.exp(-10), rel=1e-9)


@pytest.mark.unit
def test_invalid_inputs_are_rejected() -> None:
    ok = Timeouts(proxy_idle_us=10, origin_idle_us=10, one_way_delay_us=1)
    with pytest.raises(ValueError, match="idle timeouts must be positive"):
        Timeouts(proxy_idle_us=0, origin_idle_us=10, one_way_delay_us=1)
    with pytest.raises(ValueError, match="idle timeouts must be positive"):
        Timeouts(proxy_idle_us=10, origin_idle_us=0, one_way_delay_us=1)
    with pytest.raises(ValueError, match="one-way delay must be non-negative"):
        Timeouts(proxy_idle_us=10, origin_idle_us=10, one_way_delay_us=-1)
    with pytest.raises(ValueError, match="idle gap must be non-negative"):
        classify_reuse(ok, -1)
    with pytest.raises(ValueError, match="rate must be positive"):
        poisson_race_probability(ok, 0.0)


@pytest.mark.unit
def test_poisson_probability_matches_formula_and_seeded_sampling() -> None:
    rate = 0.2  # requests per second on one pooled connection
    p = poisson_race_probability(NGINX_IN_FRONT_OF_UVICORN, rate)
    assert p == pytest.approx(7.3583e-05, rel=1e-3)
    by_rate = {
        r: poisson_race_probability(NGINX_IN_FRONT_OF_UVICORN, r) for r in (0.05, 0.1, 0.5, 1.0)
    }
    assert by_rate[0.05] == pytest.approx(3.894e-05, rel=1e-3)
    assert by_rate[1.0] == pytest.approx(6.741e-06, rel=1e-3)
    # A narrow window just below origin_idle peaks near r = 1 / origin_idle (0.2 req/s).
    assert all(p > other for other in by_rate.values())
    wide = Timeouts(
        proxy_idle_us=60 * SECOND_US,
        origin_idle_us=5 * SECOND_US,
        one_way_delay_us=250 * MILLISECOND_US,
    )
    expected = poisson_race_probability(wide, rate)
    rng = random.Random(9112)  # noqa: S311 - deterministic simulation, not crypto
    samples = 100_000
    lost = sum(
        classify_reuse(wide, int(rng.expovariate(rate) * SECOND_US)) is Outcome.RACE_LOST
        for _ in range(samples)
    )
    assert lost / samples == pytest.approx(expected, rel=0.05)
    safe = dataclasses.replace(wide, origin_idle_us=61 * SECOND_US)
    assert poisson_race_probability(safe, rate) == 0.0


@pytest.mark.unit
def test_h11_proxy_gets_eof_instead_of_a_response_when_request_crosses_fin() -> None:
    trace = replay_lost_race("POST", b'{"sku":"A1","qty":1}')
    # 54-byte head + 20-byte JSON body; every byte "succeeds" and is lost.
    assert trace.request_bytes_written == 74
    assert trace.origin_events_seen == 0
    assert "ConnectionClosed when role=SERVER and state=SEND_RESPONSE" in trace.proxy_error


@pytest.mark.unit
def test_h11_refuses_to_reuse_a_connection_once_the_fin_is_processed() -> None:
    assert stale_connection_is_rejected() == ("MUST_CLOSE", "CLOSED", True)


@pytest.mark.unit
def test_python_server_idle_defaults_are_below_common_proxy_defaults() -> None:
    assert uvicorn.Config(app=_counting_app).timeout_keep_alive == 5
    assert hypercorn.config.Config().keep_alive_timeout == 5
    # granian 2.8.7 has no HTTP/1 keep-alive timeout field; an idle connection is
    # closed by `header_read_timeout` (30 s), see the granian socket test below.
    assert "keep_alive_timeout" not in {f.name for f in dataclasses.fields(HTTP1Settings)}
    assert HTTP1Settings().header_read_timeout == 30_000
    for origin_s in (5, 60):
        t = Timeouts(
            proxy_idle_us=60 * SECOND_US, origin_idle_us=origin_s * SECOND_US, one_way_delay_us=500
        )
        assert not is_race_free(t)


@pytest.mark.socket
def test_loopback_write_after_peer_close_succeeds_and_reads_eof() -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        proxy = socket.create_connection(listener.getsockname())
        origin, _ = listener.accept()
        with proxy:
            origin.close()  # origin idle timer fires: a bare FIN, no HTTP signal
            poller = select.poll()
            poller.register(proxy, select.POLLIN)
            assert poller.poll(5_000), "FIN never arrived"
            # The pool's pre-dispatch check: readable + zero-byte peek means stale.
            assert proxy.recv(1, socket.MSG_PEEK) == b""
            # A pool that skips the check still gets a successful write ...
            request = b"POST /orders HTTP/1.1\r\nHost: o\r\nContent-Length: 2\r\n\r\n{}"
            assert proxy.send(request) == len(request)
            # ... and then EOF or RST instead of a status line.
            try:
                reply = proxy.recv(4096)
            except ConnectionResetError:
                reply = b""
            assert reply == b""


app_calls: list[str] = []


async def _counting_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    assert scope["type"] == "http"
    app_calls.append(scope["method"])
    await send(
        {"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]}
    )
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.socket
@pytest.mark.asyncio
async def test_uvicorn_closes_idle_connection_silently_and_drops_the_next_post() -> None:
    app_calls.clear()
    config = uvicorn.Config(
        app=_counting_app,
        host="127.0.0.1",
        port=0,
        timeout_keep_alive=1,
        lifespan="off",
        log_level="critical",
        http="h11",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    try:
        for _ in range(500):  # uvicorn exposes `started` as a flag, not an event
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started, "uvicorn did not start within 5 s"
        port = server.servers[0].sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /warm HTTP/1.1\r\nHost: o\r\n\r\n")
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200 OK\r\n")
        assert await reader.readexactly(2) == b"ok"
        # No advance warning: no `Connection: close`, no `Keep-Alive: timeout=`.
        assert b"\r\nconnection:" not in head.lower()
        assert b"\r\nkeep-alive:" not in head.lower()
        idle_started = time.monotonic()
        assert await asyncio.wait_for(reader.read(1), timeout=5) == b""  # FIN
        idle_s = time.monotonic() - idle_started
        assert 0.9 <= idle_s < 3.0
        writer.write(b"POST /orders HTTP/1.1\r\nHost: o\r\nContent-Length: 2\r\n\r\n{}")
        await writer.drain()
        try:
            reply = await asyncio.wait_for(reader.read(4096), timeout=5)
        except ConnectionResetError:
            reply = b""
        assert reply == b""
        assert app_calls == ["GET"]  # the POST never reached the application
        writer.close()
    finally:
        server.should_exit = True
        await serve_task


@pytest.mark.socket
@pytest.mark.asyncio
async def test_granian_closes_idle_http1_connection_at_header_read_timeout() -> None:
    app_calls.clear()
    with socket.socket() as probe:  # granian's embedded server needs a concrete port
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = GranianServer(
        _counting_app,
        address="127.0.0.1",
        port=port,
        interface=Interfaces.ASGINL,
        websockets=False,
        http1_settings=HTTP1Settings(header_read_timeout=1_000),
        log_enabled=False,
    )
    serve_task = asyncio.create_task(server.serve())
    try:
        for _ in range(500):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.01)
        else:
            pytest.fail("granian did not start within 5 s")
        writer.write(b"GET /warm HTTP/1.1\r\nHost: o\r\n\r\n")
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200 OK\r\n")
        assert await reader.readexactly(2) == b"ok"
        assert b"\r\nconnection:" not in head.lower()
        assert b"\r\nkeep-alive:" not in head.lower()
        idle_started = time.monotonic()
        assert await asyncio.wait_for(reader.read(1), timeout=5) == b""  # bare FIN
        assert 0.9 <= time.monotonic() - idle_started < 3.0
        assert app_calls == ["GET"]
        writer.close()
    finally:
        server.stop()  # type: ignore[no-untyped-call]  # unannotated in granian 2.8.7
        await asyncio.wait_for(serve_task, timeout=10)
