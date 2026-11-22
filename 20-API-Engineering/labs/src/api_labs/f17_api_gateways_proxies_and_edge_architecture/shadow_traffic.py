"""Request mirroring (shadow traffic) as pure ASGI middleware with a mutation guard and a differ.

The middleware answers every client from the primary app and, for the requests its policy
admits, replays a copy to a candidate app in a background task. The candidate's response is
captured, never sent to the client, and compared with the primary's captured response after
both finish. Everything is in-process and deterministic: no sockets, no wall-clock sleeps.

The admission order mirrors what a gateway does: mutation guard (method, path, opt-out header),
then the sampling decision, then body-size and overload limits. A request that fails any check
reaches the primary untouched and is only counted.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import secrets
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class Skip(StrEnum):
    """Why a request was not mirrored."""

    METHOD = "method"
    PATH = "path"
    OPT_OUT = "opt_out"
    SAMPLE = "sample"
    BODY_TOO_LARGE = "body_too_large"
    OVERLOAD = "overload"


class Outcome(StrEnum):
    """How a started shadow request ended."""

    MATCH = "match"
    MISMATCH = "mismatch"
    NOT_COMPARABLE = "not_comparable"
    CANDIDATE_ERROR = "candidate_error"
    CANDIDATE_TIMEOUT = "candidate_timeout"
    CANCELLED = "cancelled"
    DIFF_ERROR = "diff_error"


class DiffKind(StrEnum):
    """Kinds of difference the comparison reports."""

    CHANGED = "changed"
    TYPE = "type"
    MISSING = "missing_in_candidate"
    EXTRA = "extra_in_candidate"
    LENGTH = "length"
    STATUS = "status"
    HEADER = "header"
    BYTES = "bytes"
    INVALID_JSON = "invalid_json"


@dataclass(frozen=True, slots=True)
class MirrorPolicy:
    """Admission and comparison settings; defaults are the conservative ones."""

    numerator: int = 100
    denominator: int = 100
    methods: frozenset[str] = SAFE_METHODS
    deny_path_prefixes: tuple[str, ...] = ()
    opt_out_header: str = "x-no-shadow"
    marker_header: str = "x-shadow-request"
    drop_headers: frozenset[str] = frozenset()
    host_suffix: str = "-shadow"
    max_body_bytes: int = 64 * 1024
    max_capture_bytes: int = 1024 * 1024
    timeout_s: float = 2.0
    max_in_flight: int = 8
    ignore_paths: frozenset[str] = frozenset()
    float_tolerance: float = 0.0
    compare_headers: tuple[str, ...] = ("content-type",)

    def __post_init__(self) -> None:
        if self.denominator < 1 or not 0 <= self.numerator <= self.denominator:
            raise ValueError("need 0 <= numerator <= denominator and denominator >= 1")
        if self.max_in_flight < 1 or self.timeout_s <= 0:
            raise ValueError("max_in_flight must be >= 1 and timeout_s must be > 0")


# ---------------------------------------------------------------------------
# Sampling and the shadow Host rewrite
# ---------------------------------------------------------------------------


def sample_bucket(request_id: str, denominator: int) -> int:
    """Map a request id to a stable bucket in ``[0, denominator)``."""
    digest = hashlib.sha256(request_id.encode()).digest()
    return int.from_bytes(digest[:8], "big") % denominator


def is_sampled(request_id: str, numerator: int, denominator: int) -> bool:
    """Admit ids whose bucket is below the numerator, so raising it only adds ids."""
    return sample_bucket(request_id, denominator) < numerator


def shadow_host(host: str, suffix: str = "-shadow") -> str:
    """Append ``suffix`` to the host name, before any ``:port``; IPv6 literals are left alone."""
    if host.startswith("["):
        return host
    name, sep, port = host.rpartition(":")
    if sep and port.isdigit():
        return f"{name}{suffix}:{port}"
    return f"{host}{suffix}"


# ---------------------------------------------------------------------------
# JSON differ
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Diff:
    """One difference; ``path`` is an RFC 6901 style pointer, or a label like ``status``."""

    path: str
    kind: DiffKind
    primary: Any = None
    candidate: Any = None


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _pointer(segments: tuple[str, ...]) -> str:
    return "".join("/" + _escape(s) for s in segments)


def _parse_pattern(pattern: str) -> tuple[str, ...]:
    if not pattern:
        return ()
    return tuple(s.replace("~1", "/").replace("~0", "~") for s in pattern.split("/")[1:])


def _ignored(segments: tuple[str, ...], patterns: Iterable[tuple[str, ...]]) -> bool:
    """A ``*`` pattern segment matches exactly one path segment."""
    for pattern in patterns:
        if len(pattern) == len(segments) and all(
            p == "*" or p == s for p, s in zip(pattern, segments, strict=True)
        ):
            return True
    return False


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _as_dict(value: Any) -> dict[str, Any]:
    """Re-type a value already known to be a JSON object (parsed JSON keys are ``str``)."""
    return cast("dict[str, Any]", value)


def _as_list(value: Any) -> list[Any]:
    """Re-type a value already known to be a JSON array."""
    return cast("list[Any]", value)


def _walk(
    primary: Any,
    candidate: Any,
    segments: tuple[str, ...],
    patterns: tuple[tuple[str, ...], ...],
    tolerance: float,
    out: list[Diff],
) -> None:
    if _ignored(segments, patterns):
        return
    here = _pointer(segments)
    if _json_type(primary) != _json_type(candidate):
        out.append(Diff(here, DiffKind.TYPE, primary, candidate))
    elif isinstance(primary, dict) and isinstance(candidate, dict):
        left = _as_dict(primary)
        right = _as_dict(candidate)
        for key in sorted(left.keys() | right.keys()):
            child = (*segments, key)
            if key not in right:
                if not _ignored(child, patterns):
                    out.append(Diff(_pointer(child), DiffKind.MISSING, left[key], None))
            elif key not in left:
                if not _ignored(child, patterns):
                    out.append(Diff(_pointer(child), DiffKind.EXTRA, None, right[key]))
            else:
                _walk(left[key], right[key], child, patterns, tolerance, out)
    elif isinstance(primary, list) and isinstance(candidate, list):
        left_items = _as_list(primary)
        right_items = _as_list(candidate)
        if len(left_items) != len(right_items):
            out.append(Diff(here, DiffKind.LENGTH, len(left_items), len(right_items)))
        for index, (a, b) in enumerate(zip(left_items, right_items, strict=False)):
            _walk(a, b, (*segments, str(index)), patterns, tolerance, out)
    elif _json_type(primary) == "number":
        if abs(primary - candidate) > tolerance:
            out.append(Diff(here, DiffKind.CHANGED, primary, candidate))
    elif primary != candidate:
        out.append(Diff(here, DiffKind.CHANGED, primary, candidate))


def diff_json(
    primary: Any,
    candidate: Any,
    *,
    ignore: Iterable[str] = (),
    float_tolerance: float = 0.0,
) -> list[Diff]:
    """Structural diff of two parsed JSON values, skipping ``ignore`` pointer patterns."""
    patterns = tuple(_parse_pattern(p) for p in ignore)
    out: list[Diff] = []
    _walk(primary, candidate, (), patterns, float_tolerance, out)
    return out


def noise_paths(first_run: Any, second_run: Any) -> frozenset[str]:
    """Paths that already differ between two runs of the same known-good version (Diffy idea)."""
    return frozenset(d.path for d in diff_json(first_run, second_run))


# ---------------------------------------------------------------------------
# Capture and comparison
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Captured:
    """A response as observed on the send channel, bounded by a byte cap."""

    cap: int
    status: int | None = None
    headers: list[tuple[bytes, bytes]] = field(default_factory=list[tuple[bytes, bytes]])
    body: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    complete: bool = False

    def header(self, name: str) -> str | None:
        wanted = name.lower().encode("latin-1")
        for key, value in self.headers:
            if key.lower() == wanted:
                return value.decode("latin-1")
        return None

    def observe(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = int(message["status"])
            self.headers = [(bytes(k), bytes(v)) for k, v in message.get("headers", [])]
        elif message["type"] == "http.response.body":
            chunk = bytes(message.get("body", b""))
            room = self.cap - len(self.body)
            if len(chunk) > room:
                self.truncated = True
                chunk = chunk[: max(room, 0)]
            self.body.extend(chunk)
            if not message.get("more_body", False):
                self.complete = True


async def capture_app(app: ASGIApp, scope: Scope, messages: list[Message], cap: int) -> Captured:
    """Run ``app`` against recorded request messages and capture its response."""
    captured = Captured(cap=cap)
    pending = deque(dict(m) for m in messages)
    finished = asyncio.Event()

    async def receive() -> Message:
        if pending:
            return pending.popleft()
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        captured.observe(message)
        if captured.complete:
            finished.set()

    await app(scope, receive, send)
    return captured


def _is_json(captured: Captured) -> bool:
    return "json" in (captured.header("content-type") or "").lower()


def compare_responses(
    primary: Captured, candidate: Captured, policy: MirrorPolicy
) -> tuple[Outcome, tuple[Diff, ...]]:
    """Compare status, selected headers, and the body (structurally when it is JSON)."""
    if not (primary.complete and candidate.complete) or primary.truncated or candidate.truncated:
        return Outcome.NOT_COMPARABLE, ()
    diffs: list[Diff] = []
    if primary.status != candidate.status:
        diffs.append(Diff("status", DiffKind.STATUS, primary.status, candidate.status))
    for name in policy.compare_headers:
        a, b = primary.header(name), candidate.header(name)
        if a != b:
            diffs.append(Diff(f"header:{name}", DiffKind.HEADER, a, b))
    if _is_json(primary):
        try:
            parsed_primary = json.loads(primary.body)
            parsed_candidate = json.loads(candidate.body)
        except ValueError:
            diffs.append(
                Diff("body", DiffKind.INVALID_JSON, bytes(primary.body), bytes(candidate.body))
            )
        else:
            diffs.extend(
                diff_json(
                    parsed_primary,
                    parsed_candidate,
                    ignore=policy.ignore_paths,
                    float_tolerance=policy.float_tolerance,
                )
            )
    elif primary.body != candidate.body:
        diffs.append(Diff("body", DiffKind.BYTES, bytes(primary.body), bytes(candidate.body)))
    return (Outcome.MISMATCH if diffs else Outcome.MATCH), tuple(diffs)


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiffEntry:
    """A difference with values replaced by keyed digests, safe to ship to a log pipeline."""

    path: str
    kind: DiffKind
    primary_digest: str
    candidate_digest: str


@dataclass(frozen=True, slots=True)
class ShadowRecord:
    request_id: str
    method: str
    path: str
    outcome: Outcome
    diffs: tuple[DiffEntry, ...] = ()


@dataclass(slots=True)
class ShadowTelemetry:
    """Counters for every request, plus a bounded buffer of non-matching records."""

    redaction_key: bytes = field(default_factory=lambda: secrets.token_bytes(16))
    max_records: int = 1000
    started: int = 0
    skipped: Counter[Skip] = field(default_factory=Counter[Skip])
    outcomes: Counter[Outcome] = field(default_factory=Counter[Outcome])
    records: deque[ShadowRecord] = field(init=False)

    def __post_init__(self) -> None:
        self.records = deque(maxlen=self.max_records)

    def digest(self, value: Any) -> str:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=repr)
        return hmac.new(self.redaction_key, canonical.encode(), hashlib.sha256).hexdigest()[:12]

    def record(
        self, request_id: str, method: str, path: str, outcome: Outcome, diffs: Iterable[Diff] = ()
    ) -> None:
        self.outcomes[outcome] += 1
        if outcome is Outcome.MATCH:
            return
        entries = tuple(
            DiffEntry(d.path, d.kind, self.digest(d.primary), self.digest(d.candidate))
            for d in diffs
        )
        self.records.append(ShadowRecord(request_id, method, path, outcome, entries))


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


def build_shadow_scope(scope: Scope, policy: MirrorPolicy) -> dict[str, Any]:
    """Copy ``scope`` for the candidate: shadow Host, marker header, dropped headers removed."""
    drop = {n.lower().encode("latin-1") for n in policy.drop_headers}
    marker = policy.marker_header.lower().encode("latin-1")
    headers: list[tuple[bytes, bytes]] = []
    for name, value in scope["headers"]:
        lowered = bytes(name).lower()
        if lowered in drop or lowered == marker:
            continue
        if lowered == b"host":
            host = shadow_host(bytes(value).decode("latin-1"), policy.host_suffix)
            value = host.encode("latin-1")
        headers.append((bytes(name), bytes(value)))
    headers.append((marker, b"1"))
    shadow = dict(scope)
    shadow["headers"] = headers
    for key in ("state", "extensions"):
        if isinstance(shadow.get(key), dict):
            shadow[key] = dict(shadow[key])
    return shadow


async def _buffer_body(receive: Receive, cap: int) -> tuple[list[Message], bool]:
    """Read request messages until the body ends; stop early at ``cap`` bytes or a disconnect."""
    messages: list[Message] = []
    total = 0
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            return messages, False
        total += len(message.get("body", b""))
        if total > cap:
            return messages, False
        if not message.get("more_body", False):
            return messages, True


def _replay(messages: list[Message], receive: Receive) -> Receive:
    pending = deque(messages)

    async def replay() -> Message:
        if pending:
            return pending.popleft()
        return await receive()

    return replay


class ShadowTraffic:
    """Pure ASGI middleware: answer from ``app``, mirror admitted requests to ``candidate``."""

    def __init__(
        self,
        app: ASGIApp,
        candidate: ASGIApp,
        policy: MirrorPolicy | None = None,
        telemetry: ShadowTelemetry | None = None,
    ) -> None:
        self.app = app
        self.candidate = candidate
        self.policy = policy or MirrorPolicy()
        self.telemetry = telemetry or ShadowTelemetry()
        self._tasks: set[asyncio.Task[None]] = set()
        self._in_flight = 0
        self._ids = itertools.count(1)

    @property
    def pending(self) -> int:
        """Shadow tasks that have not finished (running, or waiting for the primary)."""
        return len(self._tasks)

    def _headers(self, scope: Scope) -> dict[str, str]:
        return {
            bytes(k).decode("latin-1").lower(): bytes(v).decode("latin-1")
            for k, v in scope["headers"]
        }

    def _skip_reason(self, scope: Scope, headers: dict[str, str], request_id: str) -> Skip | None:
        policy = self.policy
        if scope["method"] not in policy.methods:
            return Skip.METHOD
        if scope["path"].startswith(policy.deny_path_prefixes):
            return Skip.PATH
        if policy.opt_out_header in headers:
            return Skip.OPT_OUT
        if not is_sampled(request_id, policy.numerator, policy.denominator):
            return Skip.SAMPLE
        try:
            declared = int(headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > policy.max_body_bytes:
            return Skip.BODY_TOO_LARGE
        if self._in_flight >= policy.max_in_flight:
            return Skip.OVERLOAD
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = self._headers(scope)
        marker = self.policy.marker_header.lower()
        if marker in headers:  # a client must not be able to pose as shadow traffic
            kept = [(k, v) for k, v in scope["headers"] if bytes(k).lower() != marker.encode()]
            scope = {**scope, "headers": kept}
        request_id = headers.get("x-request-id") or f"shadow-{next(self._ids)}"
        reason = self._skip_reason(scope, headers, request_id)
        messages: list[Message] = []
        if reason is None:
            messages, complete = await _buffer_body(receive, self.policy.max_body_bytes)
            receive = _replay(messages, receive)
            if not complete:
                reason = Skip.BODY_TOO_LARGE
        if reason is not None:
            self.telemetry.skipped[reason] += 1
            await self.app(scope, receive, send)
            return

        primary = Captured(cap=self.policy.max_capture_bytes)
        primary_done: asyncio.Future[Captured] = asyncio.get_running_loop().create_future()

        async def capturing_send(message: Message) -> None:
            await send(message)
            primary.observe(message)

        self._in_flight += 1
        self.telemetry.started += 1
        task = asyncio.create_task(
            self._shadow(request_id, scope, messages, primary_done), name=f"shadow-{request_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            await self.app(scope, receive, capturing_send)
        finally:
            if not primary_done.done():  # a cancelled shadow task cancels the future it awaits
                primary_done.set_result(primary)

    async def _shadow(
        self,
        request_id: str,
        scope: Scope,
        messages: list[Message],
        primary_done: asyncio.Future[Captured],
    ) -> None:
        method, path = str(scope["method"]), str(scope["path"])
        shadow_scope = build_shadow_scope(scope, self.policy)
        primary: Captured | None = None
        candidate: Captured | None = None
        outcome: Outcome | None = None
        try:
            try:
                async with asyncio.timeout(self.policy.timeout_s):
                    candidate = await capture_app(
                        self.candidate, shadow_scope, messages, self.policy.max_capture_bytes
                    )
            finally:
                self._in_flight -= 1
            primary = await primary_done
        except TimeoutError:
            outcome = Outcome.CANDIDATE_TIMEOUT
        except asyncio.CancelledError:
            self.telemetry.outcomes[Outcome.CANCELLED] += 1
            raise
        except Exception:
            outcome = Outcome.CANDIDATE_ERROR
        if outcome is not None or candidate is None or primary is None:
            self.telemetry.record(request_id, method, path, outcome or Outcome.CANDIDATE_ERROR)
            return
        try:
            outcome, diffs = compare_responses(primary, candidate, self.policy)
        except Exception:
            self.telemetry.record(request_id, method, path, Outcome.DIFF_ERROR)
            return
        self.telemetry.record(request_id, method, path, outcome, diffs)

    async def drain(self, grace_s: float = 5.0) -> None:
        """Wait up to ``grace_s`` for shadow tasks, then cancel and await the rest."""
        pending = set(self._tasks)
        if not pending:
            return
        _, still_running = await asyncio.wait(pending, timeout=grace_s)
        for task in still_running:
            task.cancel()
        if still_running:
            await asyncio.gather(*still_running, return_exceptions=True)


class RefuseShadowWrites:
    """Candidate-side guard: answer ``403`` to any marked request whose method is not safe."""

    def __init__(self, app: ASGIApp, marker_header: str = "x-shadow-request") -> None:
        self.app = app
        self.marker = marker_header.lower().encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        marked = scope["type"] == "http" and any(
            bytes(k).lower() == self.marker for k, _ in scope["headers"]
        )
        if marked and scope["method"] not in SAFE_METHODS:
            body = b'{"error":"shadow writes refused"}'
            headers = [(b"content-type", b"application/json"), (b"content-length", b"33")]
            await send({"type": "http.response.start", "status": 403, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
