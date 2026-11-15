"""SIGTERM drain in a live Uvicorn 0.54.0 worker, and the Kubernetes termination budget."""

from __future__ import annotations

import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import IO, TYPE_CHECKING

import httpx
import pytest

from api_labs.f12_asgi_servers_and_async_runtime.graceful_shutdown_drain import (
    Outcome,
    PodShutdown,
    Request,
    budget_violations,
    create_app,
    kubelet_signal_times,
    simulate_termination,
)

if TYPE_CHECKING:
    from collections.abc import Generator

APP = "api_labs.f12_asgi_servers_and_async_runtime.graceful_shutdown_drain:create_app"


@dataclass
class LiveServer:
    proc: subprocess.Popen[str]
    port: int = 0
    stdout: queue.Queue[str] = field(default_factory=queue.Queue[str])
    stderr: list[str] = field(default_factory=list[str])
    events: list[str] = field(default_factory=list[str])

    def wait_stderr(self, needle: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(needle in line for line in list(self.stderr)):
                return
            time.sleep(0.01)
        raise AssertionError(f"{needle!r} not logged; stderr={self.stderr}")

    def wait_event(self, name: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while name not in self.events:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"event {name!r} missing; events={self.events}")
            try:
                self.events.append(self.stdout.get(timeout=remaining).removeprefix("EVENT "))
            except queue.Empty:
                continue

    def drain_events(self) -> list[str]:
        while not self.stdout.empty():
            self.events.append(self.stdout.get_nowait().removeprefix("EVENT "))
        return self.events


def _pump(stream: IO[str], sink: queue.Queue[str] | list[str]) -> None:
    for line in stream:
        if isinstance(sink, list):
            sink.append(line.rstrip("\n"))
        else:
            sink.put(line.rstrip("\n"))


@contextmanager
def live_uvicorn(http: str, *extra: str) -> Generator[LiveServer]:
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        APP,
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--http",
        http,
        "--lifespan",
        "on",
        "--no-access-log",
        *extra,
    ]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
    )
    server = LiveServer(proc)
    assert proc.stdout is not None
    assert proc.stderr is not None
    threading.Thread(target=_pump, args=(proc.stdout, server.stdout), daemon=True).start()
    threading.Thread(target=_pump, args=(proc.stderr, server.stderr), daemon=True).start()
    try:
        server.wait_stderr("Uvicorn running on")
        line = next(x for x in server.stderr if "Uvicorn running on" in x)
        server.port = int(line.split("http://127.0.0.1:")[1].split()[0])
        yield server
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def _send_get(port: int, target: str) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(f"GET {target} HTTP/1.1\r\nHost: lab\r\n\r\n".encode())
    return sock


def _read_until_eof(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while chunk := sock.recv(65536):
        chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.socket
@pytest.mark.parametrize("http", ["h11", "httptools"])
def test_sigterm_finishes_inflight_refuses_new_connections_then_runs_lifespan_shutdown(
    http: str,
) -> None:
    with live_uvicorn(http) as server:
        inflight = _send_get(server.port, "/slow?ms=800")
        server.wait_event("request-start")
        server.proc.send_signal(signal.SIGTERM)
        # Logged after server.close() and the 0.1 s sleep: the listener is gone.
        server.wait_stderr("Waiting for connections to close")
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", server.port), timeout=2).close()
        raw = _read_until_eof(inflight)  # EOF proves keep_alive was switched off
        inflight.close()
        returncode = server.proc.wait(timeout=10)
        events = server.drain_events()

    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 OK")
    assert body == b"done"
    assert (b"connection: close" in head.lower()) is (http == "httptools")
    assert events == ["startup", "request-start", "request-end", "shutdown"]
    assert any("Application shutdown complete." in line for line in server.stderr)
    # Server.capture_signals re-raises SIGTERM with the default handler restored.
    assert returncode == -signal.SIGTERM
    print(f"\n[{http}] exit={returncode} events={events}")
    print("\n".join(f"[{http}] {line}" for line in server.stderr))


@pytest.mark.socket
def test_sigterm_closes_idle_keepalive_connection_immediately() -> None:
    with live_uvicorn("h11") as server:
        idle = _send_get(server.port, "/fast")
        first = b""
        while not first.endswith(b"\r\n\r\nok"):  # head and body may arrive separately
            first += idle.recv(65536)
        assert first.startswith(b"HTTP/1.1 200 OK")
        started = time.monotonic()
        server.proc.send_signal(signal.SIGTERM)
        assert idle.recv(65536) == b""  # FIN: a pooled client reusing this socket fails
        closed_after = time.monotonic() - started
        idle.close()
        server.proc.wait(timeout=10)
    assert closed_after < 1.0
    print(f"\nidle keep-alive closed {closed_after * 1000:.0f} ms after SIGTERM")


@pytest.mark.socket
def test_drain_timeout_cancels_slow_request_with_500_and_still_runs_lifespan_shutdown() -> None:
    with live_uvicorn("h11", "--timeout-graceful-shutdown", "1") as server:
        slow = _send_get(server.port, "/slow?ms=5000")
        server.wait_event("request-start")
        started = time.monotonic()
        server.proc.send_signal(signal.SIGTERM)
        raw = _read_until_eof(slow)
        slow.close()
        server.proc.wait(timeout=10)
        elapsed = time.monotonic() - started
        events = server.drain_events()
    assert raw.startswith(b"HTTP/1.1 500 Internal Server Error")
    cancel_log = "Cancel 1 running task(s), timeout graceful shutdown exceeded"
    assert any(cancel_log in line for line in server.stderr)
    assert "request-end" not in events
    assert events[-1] == "shutdown"
    assert 1.0 <= elapsed < 3.0
    print(f"\ncancelled after {elapsed:.2f} s; status line {raw.split(b'\r\n')[0]!r}")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_answers_in_process_without_a_server() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        slow = await client.get("/slow", params={"ms": "10"})
        fast = await client.get("/fast")
    assert (slow.status_code, slow.text) == (200, "done")
    assert (fast.status_code, fast.text) == (200, "ok")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("grace_s", "prestop_ms", "expected"),
    [
        (30, 0, (0, 30_000)),
        (30, 5_900, (5_900, 30_900)),  # elapsed truncates to 5 s, so 25 s remain
        (30, 29_500, (29_500, 31_500)),  # 1 s remains, floored to 2 s
        (30, 60_000, (30_000, 32_000)),  # hook times out at grace, then the 2 s floor
    ],
)
def test_kubelet_signal_times_follow_truncation_and_two_second_floor(
    grace_s: int, prestop_ms: int, expected: tuple[int, int]
) -> None:
    assert kubelet_signal_times(grace_s, prestop_ms) == expected


ARRIVALS = [Request(arrival_ms=t, duration_ms=50) for t in range(0, 3_000, 250)]


@pytest.mark.unit
def test_no_prestop_refuses_requests_until_endpoints_converge() -> None:
    pod = PodShutdown(prestop_ms=0, propagation_ms=2_000, drain_timeout_ms=10_000)
    timeline = simulate_termination(pod, ARRIVALS)
    assert timeline.listener_closed_ms == 100
    refused = [
        r.arrival_ms
        for r, o in zip(ARRIVALS, timeline.outcomes, strict=True)
        if o is Outcome.REFUSED
    ]
    assert refused == [250, 500, 750, 1_000, 1_250, 1_500, 1_750]
    assert timeline.outcomes.count(Outcome.SERVED) == 1


@pytest.mark.unit
def test_prestop_covering_propagation_drops_nothing() -> None:
    pod = PodShutdown(prestop_ms=5_000, propagation_ms=2_000, drain_timeout_ms=10_000)
    timeline = simulate_termination(pod, ARRIVALS)
    assert Outcome.REFUSED not in timeline.outcomes
    assert set(timeline.outcomes) == {Outcome.SERVED, Outcome.ROUTED_ELSEWHERE}
    assert budget_violations(pod, max_request_ms=1_000) == []


@pytest.mark.unit
def test_drain_timeout_shorter_than_request_cancels_it_but_lifespan_completes() -> None:
    pod = PodShutdown(prestop_ms=5_000, propagation_ms=2_000, drain_timeout_ms=3_000)
    timeline = simulate_termination(pod, [Request(arrival_ms=1_000, duration_ms=60_000)])
    assert timeline.outcomes == (Outcome.CANCELLED_500,)
    assert timeline.drain_deadline_ms == 8_200
    assert timeline.lifespan_done_ms == 8_200
    assert "drain timeout below the slowest request: those requests get 500" in budget_violations(
        pod, max_request_ms=60_000
    )


@pytest.mark.unit
def test_unbounded_drain_with_long_request_is_sigkilled_and_skips_lifespan_shutdown() -> None:
    pod = PodShutdown(prestop_ms=5_000, propagation_ms=2_000, drain_timeout_ms=None)
    timeline = simulate_termination(pod, [Request(arrival_ms=1_000, duration_ms=60_000)])
    assert timeline.outcomes == (Outcome.KILLED,)
    assert timeline.sigkill_ms == 30_000
    assert timeline.lifespan_done_ms is None
    assert any(p.startswith("unbounded drain") for p in budget_violations(pod, 60_000))


@pytest.mark.unit
def test_budget_flags_grace_period_too_small_for_prestop_plus_drain_plus_lifespan() -> None:
    pod = PodShutdown(
        termination_grace_s=30,
        prestop_ms=15_000,
        propagation_ms=10_000,
        drain_timeout_ms=15_000,
        lifespan_shutdown_ms=2_000,
    )
    assert budget_violations(pod, max_request_ms=10_000) == [
        "needs 32200 ms but terminationGracePeriodSeconds gives 30000"
    ]
    timeline = simulate_termination(pod, [Request(arrival_ms=9_000, duration_ms=20_000)])
    # SIGTERM at 15 s, SIGKILL at 30 s, drain deadline 30.2 s: the request ends at 29 s,
    # but the 2 s lifespan shutdown would end at 31 s, so SIGKILL cuts it off.
    assert (timeline.sigkill_ms, timeline.drain_deadline_ms) == (30_000, 30_200)
    assert timeline.outcomes == (Outcome.SERVED,)
    assert timeline.lifespan_done_ms is None
