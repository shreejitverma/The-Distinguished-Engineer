"""Zero-dropped-request deploys: a Kubernetes termination timeline model and a drain-aware app.

Two pieces, both deterministic:

* ``simulate_termination`` replays one Pod's termination in integer milliseconds.
  It combines the kubelet's grace arithmetic (preStop first, remaining whole seconds
  passed to ``StopContainer``, a 2 s floor) with Uvicorn 0.54.0 ``Server.shutdown``
  (listener closed on the first 0.1 s tick after ``SIGTERM``, a 0.1 s sleep, then a
  drain bounded by ``timeout_graceful_shutdown``, then lifespan shutdown).
* ``create_app`` is a raw ASGI 3 app for the live ``SIGTERM`` lab: ``/slow`` sleeps,
  ``/fast`` answers at once, and lifespan events are printed to stdout so a test can
  check their order against the request.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qs

# kubelet: pkg/kubelet/kuberuntime/kuberuntime_manager.go, minimumGracePeriodInSeconds.
KUBELET_MIN_GRACE_S = 2
# uvicorn 0.54.0 uvicorn/server.py: main_loop sleeps 0.1 s per tick; shutdown sleeps 0.1 s
# after calling connection.shutdown() and before the bounded drain starts.
UVICORN_TICK_MS = 100
UVICORN_POST_CLOSE_SLEEP_MS = 100


class Outcome(StrEnum):
    SERVED = "served"  # accepted before the listener closed and finished before any deadline
    REFUSED = "refused"  # routed here after the listener closed: TCP RST, ECONNREFUSED
    ROUTED_ELSEWHERE = "routed_elsewhere"  # every dataplane had dropped this endpoint
    CANCELLED_500 = "cancelled_500"  # still running when timeout_graceful_shutdown expired
    KILLED = "killed"  # still running at SIGKILL: connection reset mid-response


@dataclass(frozen=True, slots=True)
class PodShutdown:
    """Knobs that decide whether a rollout drops requests. Durations in milliseconds."""

    termination_grace_s: int = 30  # Pod spec terminationGracePeriodSeconds (default 30)
    prestop_ms: int = 0  # lifecycle.preStop sleep; 0 means no hook
    propagation_ms: int = 0  # deletion -> last dataplane stops routing to this Pod IP
    drain_timeout_ms: int | None = None  # uvicorn --timeout-graceful-shutdown (None = forever)
    lifespan_shutdown_ms: int = 0  # time the app's lifespan shutdown handler needs


@dataclass(frozen=True, slots=True)
class Request:
    arrival_ms: int  # relative to the Pod deletion timestamp (t = 0)
    duration_ms: int


@dataclass(frozen=True, slots=True)
class Timeline:
    sigterm_ms: int
    sigkill_ms: int
    listener_closed_ms: int
    drain_deadline_ms: int | None
    lifespan_done_ms: int | None  # None when SIGKILL lands first
    outcomes: tuple[Outcome, ...]


def kubelet_signal_times(termination_grace_s: int, prestop_ms: int) -> tuple[int, int]:
    """Return (SIGTERM, SIGKILL) times in ms after deletion, mirroring kubelet killContainer.

    The preStop hook runs with the full grace period as its timeout; the kubelet then
    subtracts the hook's elapsed time truncated to whole seconds and passes the rest,
    floored at 2 s, to the runtime's StopContainer, which sends SIGTERM and later SIGKILL.
    """
    grace_ms = termination_grace_s * 1000
    sigterm_ms = min(prestop_ms, grace_ms)
    elapsed_s = sigterm_ms // 1000  # Go int64(duration.Seconds()) truncates
    remaining_s = max(termination_grace_s - elapsed_s, KUBELET_MIN_GRACE_S)
    return sigterm_ms, sigterm_ms + remaining_s * 1000


def simulate_termination(pod: PodShutdown, requests: list[Request]) -> Timeline:
    """Replay one Pod's termination and classify every request routed during it."""
    sigterm_ms, sigkill_ms = kubelet_signal_times(pod.termination_grace_s, pod.prestop_ms)
    # Worst case: the signal handler fires just after a tick, so should_exit is seen 0.1 s later.
    listener_closed_ms = sigterm_ms + UVICORN_TICK_MS
    drain_start_ms = listener_closed_ms + UVICORN_POST_CLOSE_SLEEP_MS
    drain_deadline_ms = (
        None if pod.drain_timeout_ms is None else drain_start_ms + pod.drain_timeout_ms
    )
    hard_stop_ms = sigkill_ms if drain_deadline_ms is None else min(drain_deadline_ms, sigkill_ms)

    outcomes: list[Outcome] = []
    last_finish_ms = drain_start_ms
    for req in requests:
        if req.arrival_ms >= pod.propagation_ms:
            outcomes.append(Outcome.ROUTED_ELSEWHERE)
            continue
        if req.arrival_ms >= listener_closed_ms:
            outcomes.append(Outcome.REFUSED)
            continue
        finish_ms = req.arrival_ms + req.duration_ms
        if finish_ms <= hard_stop_ms:
            outcomes.append(Outcome.SERVED)
            last_finish_ms = max(last_finish_ms, finish_ms)
        elif drain_deadline_ms is not None and drain_deadline_ms < sigkill_ms:
            outcomes.append(Outcome.CANCELLED_500)
            last_finish_ms = max(last_finish_ms, drain_deadline_ms)
        else:
            outcomes.append(Outcome.KILLED)
            last_finish_ms = max(last_finish_ms, sigkill_ms)

    # Lifespan shutdown starts once the drain ends and must finish before SIGKILL.
    lifespan_end_ms = last_finish_ms + pod.lifespan_shutdown_ms
    lifespan_done_ms = lifespan_end_ms if lifespan_end_ms < sigkill_ms else None
    return Timeline(
        sigterm_ms=sigterm_ms,
        sigkill_ms=sigkill_ms,
        listener_closed_ms=listener_closed_ms,
        drain_deadline_ms=drain_deadline_ms,
        lifespan_done_ms=lifespan_done_ms,
        outcomes=tuple(outcomes),
    )


def budget_violations(pod: PodShutdown, max_request_ms: int) -> list[str]:
    """Static checks a platform team can run in CI against a Deployment's numbers."""
    problems: list[str] = []
    grace_ms = pod.termination_grace_s * 1000
    if pod.prestop_ms < pod.propagation_ms:
        problems.append("preStop shorter than endpoint propagation: new connections refused")
    if pod.prestop_ms >= grace_ms:
        problems.append("preStop consumes the grace period: SIGKILL 2 s after SIGTERM")
    if pod.drain_timeout_ms is None:
        problems.append("unbounded drain: SIGKILL, not Uvicorn, decides when draining ends")
    elif pod.drain_timeout_ms < max_request_ms:
        problems.append("drain timeout below the slowest request: those requests get 500")
    drain_ms = max_request_ms if pod.drain_timeout_ms is None else pod.drain_timeout_ms
    overhead_ms = UVICORN_TICK_MS + UVICORN_POST_CLOSE_SLEEP_MS
    needed_ms = pod.prestop_ms + overhead_ms + drain_ms + pod.lifespan_shutdown_ms
    if needed_ms > grace_ms:
        problems.append(f"needs {needed_ms} ms but terminationGracePeriodSeconds gives {grace_ms}")
    return problems


Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


def _event(name: str) -> None:
    sys.stdout.write(f"EVENT {name}\n")
    sys.stdout.flush()


async def _lifespan(receive: Receive, send: Send) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            _event("startup")
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            _event("shutdown")
            await send({"type": "lifespan.shutdown.complete"})
            return


async def _respond(send: Send, status: int, body: bytes) -> None:
    headers = [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] == "lifespan":
        await _lifespan(receive, send)
        return
    if scope["type"] != "http":
        return
    path: str = scope["path"]
    if path == "/slow":
        query = parse_qs(scope["query_string"].decode())
        delay_ms = int(query.get("ms", ["500"])[0])
        _event("request-start")
        await asyncio.sleep(delay_ms / 1000)
        _event("request-end")
        await _respond(send, 200, b"done")
    elif path == "/fast":
        await _respond(send, 200, b"ok")
    else:
        await _respond(send, 404, b"not found")


def create_app() -> Callable[[Scope, Receive, Send], Awaitable[None]]:
    """Factory for ``uvicorn --factory``."""
    return _app
