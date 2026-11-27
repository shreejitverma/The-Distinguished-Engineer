"""gRPC client-side load balancing, checked against grpcio 1.85.1 (grpc-core v1.85.1).

Two layers, both deterministic:

* Pure models ported from grpc-core 1.85.1: the `round_robin` state aggregation and picker
  (`round_robin.cc`), `pick_first` address interleaving and Happy Eyeballs delay
  (`pick_first.cc`), the connection backoff (`util/backoff.cc`), the DNS re-resolution
  cooldown (`resolver/polling_resolver.cc`), LB policy selection (`client_channel.cc`
  `ChooseLbPolicy`, `lb_policy_registry.cc`), and the weighted round robin weight formula
  (`weighted_round_robin.cc` `MaybeUpdateWeight`, `GetWeight`).
* A thin real `grpc.aio` harness on `127.0.0.1:0`: N backends that report their own name, an
  L4 (TCP connection level) proxy, and helpers that count which backend served each RPC.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections import Counter
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Sequence

# --- constants (grpc-core v1.85.1 unless noted) ------------------------------------------------
INITIAL_BACKOFF_S: Final = 1.0  # subchannel.cc GRPC_SUBCHANNEL_INITIAL_CONNECT_BACKOFF_SECONDS
BACKOFF_MULTIPLIER: Final = 1.6  # subchannel.cc GRPC_SUBCHANNEL_RECONNECT_BACKOFF_MULTIPLIER
MAX_BACKOFF_S: Final = 120.0  # subchannel.cc GRPC_SUBCHANNEL_RECONNECT_MAX_BACKOFF_SECONDS
BACKOFF_JITTER: Final = 0.2  # subchannel.cc GRPC_SUBCHANNEL_RECONNECT_JITTER
DNS_MIN_TIME_BETWEEN_RESOLUTIONS_S: Final = 30.0  # dns_resolver.cc CreateResolver default
HAPPY_EYEBALLS_DEFAULT_MS: Final = 250  # pick_first.cc constructor, value_or(250)
HAPPY_EYEBALLS_MIN_MS: Final = 100  # pick_first.cc Clamp(..., 100, 2000)
HAPPY_EYEBALLS_MAX_MS: Final = 2000
WRR_WEIGHT_EXPIRATION_S: Final = 180.0  # weighted_round_robin.h weight_expiration_period_
WRR_BLACKOUT_S: Final = 10.0  # weighted_round_robin.h blackout_period_
WRR_ERROR_UTILIZATION_PENALTY: Final = 1.0  # weighted_round_robin.h error_utilization_penalty_
REGISTERED_POLICIES: Final = frozenset({"pick_first", "round_robin", "weighted_round_robin"})
WHO_METHOD: Final = "/lab.Who/Who"


# --- connectivity state aggregation (round_robin.cc) -------------------------------------------
class State(StrEnum):
    """`grpc_connectivity_state` without SHUTDOWN (never reported to an LB policy)."""

    IDLE = "IDLE"
    CONNECTING = "CONNECTING"
    READY = "READY"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"


def aggregate_round_robin(states: Sequence[State]) -> State:
    """`MaybeUpdateRoundRobinConnectivityStateLocked`: first matching rule wins.

    IDLE children are counted as CONNECTING (`UpdateStateCountersLocked`), so the policy itself
    never reports IDLE, unlike the prose in doc/load-balancing.md. An empty list is the "empty
    address list" case, which reports TRANSIENT_FAILURE (`RoundRobin::UpdateLocked`).
    """
    if any(s is State.READY for s in states):
        return State.READY
    if any(s in (State.CONNECTING, State.IDLE) for s in states):
        return State.CONNECTING
    return State.TRANSIENT_FAILURE


@dataclass(slots=True)
class RoundRobinPicker:
    """`RoundRobin::Picker`: a shared counter modulo the READY children, random start index."""

    ready: tuple[str, ...]
    next_index: int

    def pick(self) -> str:
        index = self.next_index % len(self.ready)
        self.next_index += 1
        return self.ready[index]


def build_round_robin_picker(
    names: Sequence[str], states: Sequence[State], *, start: int
) -> RoundRobinPicker | None:
    """A picker over only the READY children; `None` when the policy is not READY."""
    ready = tuple(n for n, s in zip(names, states, strict=True) if s is State.READY)
    if not ready:
        return None
    return RoundRobinPicker(ready=ready, next_index=start)


# --- pick_first (pick_first.cc) ----------------------------------------------------------------
def address_family(address: str) -> str:
    """`ipv6` for a bracketed literal such as `[::1]:443`, else `ipv4`."""
    return "ipv6" if address.startswith("[") else "ipv4"


def interleave_by_family(addresses: Sequence[str]) -> list[str]:
    """RFC 8305 Section 4 interleaving as done in `PickFirst::UpdateLocked`.

    The first address family seen goes first; families then alternate, and a family that runs
    out is skipped. Order inside a family is preserved.
    """
    families: list[str] = []
    for address in addresses:
        family = address_family(address)
        if family not in families:
            families.append(family)
    queues = {f: [a for a in addresses if address_family(a) == f] for f in families}
    out: list[str] = []
    while len(out) < len(addresses):
        for family in families:
            if queues[family]:
                out.append(queues[family].pop(0))
    return out


def happy_eyeballs_delay_ms(configured: int | None) -> int:
    """`grpc.happy_eyeballs_connection_attempt_delay_ms`: default 250, clamped to 100..2000."""
    value = HAPPY_EYEBALLS_DEFAULT_MS if configured is None else configured
    return max(HAPPY_EYEBALLS_MIN_MS, min(HAPPY_EYEBALLS_MAX_MS, value))


def pick_first_target(addresses: Sequence[str], reachable: Iterable[str]) -> str | None:
    """The address `pick_first` settles on when attempts are sequential: first reachable."""
    up = set(reachable)
    return next((a for a in addresses if a in up), None)


# --- connection backoff (util/backoff.cc) ------------------------------------------------------
@dataclass(slots=True)
class Backoff:
    """`BackOff::NextAttemptDelay`: first delay is the initial value, then x1.6, capped, jittered.

    `unit_random` in `[0, 1)` replaces `absl::Uniform(1 - jitter, 1 + jitter)` so tests control it.
    """

    initial_s: float = INITIAL_BACKOFF_S
    multiplier: float = BACKOFF_MULTIPLIER
    max_s: float = MAX_BACKOFF_S
    jitter: float = BACKOFF_JITTER
    _current: float = field(init=False)
    _initial: bool = field(init=False, default=True)

    def __post_init__(self) -> None:
        self._current = self.initial_s

    def next_delay(self, unit_random: float) -> float:
        if self._initial:
            self._initial = False
        else:
            self._current *= self.multiplier
        self._current = min(self._current, self.max_s)
        factor = (1 - self.jitter) + unit_random * 2 * self.jitter
        return self._current * factor

    def reset(self) -> None:
        self._current = self.initial_s
        self._initial = True


# --- DNS re-resolution cooldown (polling_resolver.cc) ------------------------------------------
def earliest_resolution_s(
    last_resolution_s: float | None,
    now_s: float,
    min_between_s: float = DNS_MIN_TIME_BETWEEN_RESOLUTIONS_S,
) -> float:
    """`PollingResolver::MaybeStartResolvingLocked`: a request inside the cooldown is deferred."""
    if last_resolution_s is None:
        return now_s
    return max(now_s, last_resolution_s + min_between_s)


# --- LB policy selection (client_channel.cc ChooseLbPolicy) ------------------------------------
def pick_from_lb_config(
    lb_config: Sequence[dict[str, Any]], registry: frozenset[str] = REGISTERED_POLICIES
) -> str:
    """`ParseLoadBalancingConfigHelper`: the first entry the client knows, else an error."""
    tried: list[str] = []
    for entry in lb_config:
        if len(entry) != 1:
            raise ValueError("oneOf violation" if entry else "no policy found in child entry")
        ((name, config),) = entry.items()
        if not isinstance(config, dict):
            raise ValueError("child entry should be of type object")
        if name in registry:
            return name
        tried.append(name)
    raise ValueError("No known policies in list: " + " ".join(tried))


def choose_lb_policy(
    *,
    lb_config: Sequence[dict[str, Any]] | None = None,
    deprecated_policy: str | None = None,
    channel_arg: str | None = None,
    registry: frozenset[str] = REGISTERED_POLICIES,
) -> str:
    """Precedence: `loadBalancingConfig`, then `loadBalancingPolicy`, then `grpc.lb_policy_name`.

    A `grpc.lb_policy_name` that is not registered falls back to `pick_first` (with an error
    log in grpc-core); so does having nothing at all.
    """
    if lb_config:
        return pick_from_lb_config(lb_config, registry)
    if deprecated_policy:
        return deprecated_policy
    if channel_arg is not None:
        return channel_arg if channel_arg in registry else "pick_first"
    return "pick_first"


def lb_service_config(policy: str, config: dict[str, Any] | None = None) -> str:
    """JSON for the `grpc.service_config` channel argument selecting one LB policy."""
    return json.dumps({"loadBalancingConfig": [{policy: config or {}}]}, separators=(",", ":"))


# --- weighted round robin weights (weighted_round_robin.cc) ------------------------------------
def wrr_weight(
    qps: float,
    eps: float,
    utilization: float,
    error_penalty: float = WRR_ERROR_UTILIZATION_PENALTY,
) -> float:
    """`EndpointWeight::MaybeUpdateWeight`: `qps / (utilization + eps / qps * penalty)`.

    A weight of 0 means "no update": the previous weight stays in place.
    """
    if qps <= 0 or utilization <= 0:
        return 0.0
    penalty = eps / qps * error_penalty if eps > 0 and error_penalty > 0 else 0.0
    return qps / (utilization + penalty)


def wrr_usable_weight(
    *,
    now_s: float,
    last_update_s: float,
    non_empty_since_s: float,
    weight: float,
    expiration_s: float = WRR_WEIGHT_EXPIRATION_S,
    blackout_s: float = WRR_BLACKOUT_S,
) -> float:
    """`EndpointWeight::GetWeight`: stale or still-in-blackout weights count as 0 (use the mean)."""
    if now_s - last_update_s >= expiration_s:
        return 0.0
    if blackout_s > 0 and now_s - non_empty_since_s < blackout_s:
        return 0.0
    return weight


# --- real grpc.aio harness (loopback sockets) --------------------------------------------------
_grpc: Any = importlib.import_module("grpc")
_aio: Any = importlib.import_module("grpc.aio")


@dataclass(slots=True)
class Backend:
    """One `grpc.aio` server that answers `/lab.Who/Who` with its own name."""

    name: str
    server: Any
    port: int
    hits: int = 0
    stopped: bool = False

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if not self.stopped:
            self.stopped = True
            await self.server.stop(None)


def _who_handler(backend_box: list[Backend]) -> Any:
    async def who(_request: bytes, _context: Any) -> bytes:
        backend = backend_box[0]
        backend.hits += 1
        return backend.name.encode("ascii")

    return _grpc.method_handlers_generic_handler(
        "lab.Who", {"Who": _grpc.unary_unary_rpc_method_handler(who)}
    )


@asynccontextmanager
async def start_backends(
    count: int, options: Sequence[tuple[str, int]] = ()
) -> AsyncGenerator[list[Backend]]:
    """Start `count` servers named `b0`, `b1`, ... and stop them all on exit."""
    backends: list[Backend] = []
    try:
        for i in range(count):
            box: list[Backend] = []
            server = _aio.server(options=list(options))
            server.add_generic_rpc_handlers((_who_handler(box),))
            port = server.add_insecure_port("127.0.0.1:0")
            await server.start()
            backend = Backend(name=f"b{i}", server=server, port=port)
            box.append(backend)
            backends.append(backend)
        yield backends
    finally:
        for backend in backends:
            await backend.stop()


def ipv4_target(addresses: Sequence[str]) -> str:
    """The C-core `ipv4:` resolver scheme: a static, comma-separated address list."""
    return "ipv4:" + ",".join(addresses)


async def call_who(channel: Any, *, wait_for_ready: bool = False, timeout_s: float = 5.0) -> str:
    """One unary RPC; returns the serving backend's name or raises `grpc.aio.AioRpcError`."""
    method = channel.unary_unary(WHO_METHOD)
    reply: bytes = await method(b"", wait_for_ready=wait_for_ready, timeout=timeout_s)
    return reply.decode("ascii")


async def sample(channel: Any, count: int) -> Counter[str]:
    """`count` serial RPCs with `wait_for_ready`; the per-backend tally."""
    tally: Counter[str] = Counter()
    for _ in range(count):
        tally[await call_who(channel, wait_for_ready=True)] += 1
    return tally


async def eventually(
    probe: Callable[[], Awaitable[bool]], *, timeout_s: float = 10.0, step_s: float = 0.02
) -> None:
    """Poll an async predicate; raise `TimeoutError` if it never becomes true."""
    async with asyncio.timeout(timeout_s):
        while True:
            if await probe():
                return
            await asyncio.sleep(step_s)


async def warm_up(channel: Any, expected: set[str]) -> None:
    """Send RPCs until every name in `expected` has answered at least once.

    For `round_robin` this proves every subchannel was READY at the same time, so later picks
    cycle over all of them.
    """
    seen: set[str] = set()

    async def probe() -> bool:
        seen.add(await call_who(channel, wait_for_ready=True))
        return expected <= seen

    await eventually(probe)


async def survive_call(channel: Any) -> str | None:
    """One RPC; `None` if the channel answered `UNAVAILABLE` (a stale connection)."""
    try:
        return await call_who(channel, wait_for_ready=False, timeout_s=2.0)
    except _aio.AioRpcError as error:
        if error.code() == _grpc.StatusCode.UNAVAILABLE:
            return None
        raise


def make_channel(target: str, options: Sequence[tuple[str, Any]] = ()) -> Any:
    """A plaintext `grpc.aio` channel; the caller owns `close()`."""
    return _aio.insecure_channel(target, options=list(options))


def imbalance(tally: Counter[str], backends: Sequence[str]) -> float:
    """Busiest backend divided by the mean load; `1.0` is perfectly even."""
    total = sum(tally.values())
    if total == 0:
        return 0.0
    return max(tally[b] for b in backends) / (total / len(backends))


# --- L4 proxy: balances TCP connections, never RPCs --------------------------------------------
class L4Proxy:
    """A TCP proxy that sends each accepted connection to the next upstream in turn.

    This models a Kubernetes ClusterIP / kube-proxy or a cloud L4 load balancer: the unit of
    balancing is the connection. HTTP/2 multiplexes every RPC of a channel onto one connection,
    so the proxy never sees the RPCs.
    """

    def __init__(self, upstream_ports: Sequence[int]) -> None:
        self._ports = list(upstream_ports)
        self.assignments: list[int] = []
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def connections_accepted(self) -> int:
        return len(self.assignments)

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._on_connect, "127.0.0.1", 0)
        sockets = self._server.sockets
        return int(sockets[0].getsockname()[1])

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        index = len(self.assignments) % len(self._ports)
        self.assignments.append(index)
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self._ports[index])
        except OSError:
            writer.close()
            return
        try:
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        finally:
            up_writer.close()
            writer.close()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with suppress(asyncio.CancelledError, Exception):
                await task
        if self._server is not None:
            await self._server.wait_closed()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        return
    finally:
        with suppress(OSError, RuntimeError):
            writer.write_eof()
