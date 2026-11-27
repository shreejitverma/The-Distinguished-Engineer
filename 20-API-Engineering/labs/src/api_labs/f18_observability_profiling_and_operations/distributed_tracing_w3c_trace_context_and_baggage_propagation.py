"""W3C Trace Context and Baggage: strict wire codecs and a FastAPI/SQLAlchemy/httpx harness.

Two halves:

* Pure codecs written from the W3C texts (Trace Context Level 1 and 2, Baggage):
  `parse_traceparent`, `format_traceparent`, `parse_tracestate`, `set_tracestate_member`,
  `truncate_tracestate`, `parse_baggage`, `encode_baggage`.
  They are the executable reference the tests compare the OpenTelemetry Python propagators against.
* A hermetic service, `traced_service`, that receives a request through FastAPI, runs an async
  SQLAlchemy query, and makes an outbound httpx call, with every span in an `InMemorySpanExporter`.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, Callable, Generator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote

import httpx
from fastapi import FastAPI
from opentelemetry import baggage, context, propagate, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import (  # pyright: ignore[reportMissingTypeStubs]
    SQLAlchemyInstrumentor,
)
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, Sampler
from opentelemetry.trace import format_span_id, format_trace_id
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# --------------------------------------------------------------------------------------
# traceparent (W3C Trace Context Level 1 Section 3.2, Level 2 Section 3.2)
# --------------------------------------------------------------------------------------

FLAG_SAMPLED = 0x01
FLAG_RANDOM_TRACE_ID = 0x02
KNOWN_FLAGS = FLAG_SAMPLED | FLAG_RANDOM_TRACE_ID
TRACEPARENT_V00_LENGTH = 55

_LC_HEX = frozenset("0123456789abcdef")


def _is_lc_hex(value: str) -> bool:
    return bool(value) and all(ch in _LC_HEX for ch in value)


@dataclass(frozen=True, slots=True)
class Traceparent:
    """The four `traceparent` fields as parsed from the wire."""

    version: int
    trace_id: str
    parent_id: str
    flags: int

    @property
    def sampled(self) -> bool:
        return bool(self.flags & FLAG_SAMPLED)

    @property
    def random_trace_id(self) -> bool:
        return bool(self.flags & FLAG_RANDOM_TRACE_ID)


def parse_traceparent(value: str) -> Traceparent | None:
    """Parse a `traceparent` value; return `None` when the trace must be restarted.

    Version `00` must be exactly 55 characters.
    A higher version is parsed with the version-00 layout and may carry extra `-` suffixed fields
    (Level 1 Section 3.2.4).
    Version `ff`, uppercase hex, and all-zero ids are invalid.
    """
    if len(value) < TRACEPARENT_V00_LENGTH:
        return None
    version_s, trace_id, parent_id, flags_s = value[0:2], value[3:35], value[36:52], value[53:55]
    if (value[2], value[35], value[52]) != ("-", "-", "-"):
        return None
    if not all(_is_lc_hex(part) for part in (version_s, trace_id, parent_id, flags_s)):
        return None
    if version_s == "ff" or trace_id == "0" * 32 or parent_id == "0" * 16:
        return None
    if version_s == "00":
        if len(value) != TRACEPARENT_V00_LENGTH:
            return None
    elif len(value) > TRACEPARENT_V00_LENGTH and value[55] != "-":
        return None
    return Traceparent(int(version_s, 16), trace_id, parent_id, int(flags_s, 16))


def format_traceparent(trace_id: str, parent_id: str, flags: int) -> str:
    """Serialize version 00; unknown flag bits are cleared (Level 1 Section 3.2.2.5.2)."""
    return f"00-{trace_id}-{parent_id}-{flags & KNOWN_FLAGS:02x}"


# --------------------------------------------------------------------------------------
# tracestate (W3C Trace Context Level 1 Section 3.3)
# --------------------------------------------------------------------------------------

TRACESTATE_MAX_MEMBERS = 32
TRACESTATE_PROPAGATE_MIN = 512
TRACESTATE_BIG_ENTRY = 128

_TS_TAIL = r"[a-z0-9_\-*/]"
_TS_KEY = re.compile(
    rf"(?:[a-z]{_TS_TAIL}{{0,255}}|[a-z0-9]{_TS_TAIL}{{0,240}}@[a-z]{_TS_TAIL}{{0,13}})"
)
_TS_VALUE = re.compile(r"[\x20-\x2b\x2d-\x3c\x3e-\x7e]{0,255}[\x21-\x2b\x2d-\x3c\x3e-\x7e]")


def is_valid_tracestate_member(key: str, value: str) -> bool:
    return _TS_KEY.fullmatch(key) is not None and _TS_VALUE.fullmatch(value) is not None


def parse_tracestate(headers: Sequence[str]) -> list[tuple[str, str]]:
    """Combine field lines with `,` and parse; any invalid member discards the whole list.

    Returns `[]` for an empty header, for more than 32 members, for a duplicate key, and for any
    member that fails the key or value grammar (Level 1 Sections 3.3.1.1 to 3.3.1.4, 4.3).
    """
    members: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in headers:
        for raw in line.split(","):
            member = raw.strip(" \t")
            if not member:
                continue
            key, sep, value = member.partition("=")
            if not sep or not is_valid_tracestate_member(key, value) or key in seen:
                return []
            seen.add(key)
            members.append((key, value))
    if len(members) > TRACESTATE_MAX_MEMBERS:
        return []
    return members


def format_tracestate(members: Sequence[tuple[str, str]]) -> str:
    return ",".join(f"{key}={value}" for key, value in members)


def set_tracestate_member(
    members: Sequence[tuple[str, str]], key: str, value: str
) -> list[tuple[str, str]]:
    """Add or update `key`, moving it to the left; unrelated members keep their order.

    At 32 members a new key evicts the right-most member (the spec is silent here; this lab
    chooses eviction over refusing the write, which would lose the newest position).
    """
    if not is_valid_tracestate_member(key, value):
        raise ValueError(f"invalid tracestate member: {key!r}={value!r}")
    rest = [(k, v) for k, v in members if k != key]
    return [(key, value), *rest[: TRACESTATE_MAX_MEMBERS - 1]]


def truncate_tracestate(
    members: Sequence[tuple[str, str]], limit: int = TRACESTATE_PROPAGATE_MIN
) -> list[tuple[str, str]]:
    """Shrink to `limit` characters by whole entries (Level 1 Section 3.3.1.5).

    Entries longer than 128 characters go first, then entries from the right end.
    """
    if len(format_tracestate(members)) <= limit:
        return list(members)
    kept = [(k, v) for k, v in members if len(f"{k}={v}") <= TRACESTATE_BIG_ENTRY]
    while kept and len(format_tracestate(kept)) > limit:
        kept.pop()
    return kept


# --------------------------------------------------------------------------------------
# baggage (W3C Baggage Section 3.3)
# --------------------------------------------------------------------------------------

BAGGAGE_MAX_MEMBERS = 64
BAGGAGE_MAX_BYTES = 8192

_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_BAGGAGE_OCTET = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*")
# Every baggage-octet except "%", which must itself be percent-encoded.
_BAGGAGE_SAFE = "".join(chr(c) for c in range(0x21, 0x7F) if chr(c) not in '"%,;\\')


@dataclass(frozen=True, slots=True)
class BaggageMember:
    key: str
    value: str
    properties: tuple[str, ...] = ()


def parse_baggage(headers: Sequence[str]) -> list[BaggageMember]:
    """Parse every `baggage` field line as one combined list; drop malformed members.

    Order and duplicate keys are preserved (Section 3.3.1.1).
    Values are percent-decoded; invalid UTF-8 becomes U+FFFD (Section 3.3.1.3).
    Members beyond 64 and bytes beyond 8192 are dropped whole (Section 3.3.2).
    """
    out: list[BaggageMember] = []
    size = 0
    for line in headers:
        for raw in line.split(","):
            if len(out) >= BAGGAGE_MAX_MEMBERS:
                return out
            head, *props = raw.split(";")
            key, sep, value = head.partition("=")
            key, value = key.strip(" \t"), value.strip(" \t")
            if not sep or _TOKEN.fullmatch(key) is None or _BAGGAGE_OCTET.fullmatch(value) is None:
                continue
            size += len(raw.encode()) + (1 if out else 0)
            if size > BAGGAGE_MAX_BYTES:
                return out
            out.append(
                BaggageMember(
                    key, unquote(value, errors="replace"), tuple(p.strip(" \t") for p in props)
                )
            )
    return out


def encode_baggage(members: Sequence[BaggageMember]) -> str:
    """Serialize, percent-encoding values; drop trailing whole members that break a limit."""
    parts: list[str] = []
    size = 0
    for member in members:
        if _TOKEN.fullmatch(member.key) is None:
            raise ValueError(f"baggage key is not an RFC 7230 token: {member.key!r}")
        encoded = f"{member.key}={quote(member.value, safe=_BAGGAGE_SAFE)}"
        if member.properties:
            encoded += ";" + ";".join(member.properties)
        size += len(encoded.encode()) + (1 if parts else 0)
        if len(parts) >= BAGGAGE_MAX_MEMBERS or size > BAGGAGE_MAX_BYTES:
            break
        parts.append(encoded)
    return ",".join(parts)


# --------------------------------------------------------------------------------------
# Trust boundary and baggage-to-attribute processor
# --------------------------------------------------------------------------------------

_Scope = dict[str, Any]
_Receive = Callable[[], Any]
_Send = Callable[[dict[str, Any]], Any]
PROPAGATION_HEADERS = frozenset({b"traceparent", b"tracestate", b"baggage"})


class TrustBoundary:
    """Pure ASGI wrapper that strips inbound trace context from untrusted callers.

    Placed outside the instrumented app so the OpenTelemetry middleware sees a request with no
    `traceparent`, which restarts the trace (Level 1 Section 3.4, Restart trace).
    """

    def __init__(self, app: Any, *, trusted: Callable[[_Scope], bool]) -> None:
        self.app = app
        self.trusted = trusted

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        if scope["type"] == "http" and not self.trusted(scope):
            headers = [(k, v) for k, v in scope["headers"] if k.lower() not in PROPAGATION_HEADERS]
            scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


class BaggageAllowlistProcessor(SpanProcessor):
    """Copy only allowlisted baggage keys onto each span as `baggage.<key>` attributes."""

    def __init__(self, allowed: Sequence[str]) -> None:
        self.allowed = tuple(allowed)

    def on_start(self, span: SdkSpan, parent_context: context.Context | None = None) -> None:
        for key in self.allowed:
            value = baggage.get_baggage(key, parent_context)
            if value is not None:
                span.set_attribute(f"baggage.{key}", str(value))

    def on_end(self, span: ReadableSpan) -> None:
        return None


# --------------------------------------------------------------------------------------
# Hermetic traced service
# --------------------------------------------------------------------------------------


@dataclass
class TracedService:
    """Handles to a running in-process service and everything it recorded."""

    app: Any
    client: httpx.AsyncClient
    exporter: InMemorySpanExporter
    downstream_headers: list[httpx.Headers] = field(default_factory=list[httpx.Headers])

    def spans_by_name(self) -> dict[str, ReadableSpan]:
        return {span.name: span for span in self.exporter.get_finished_spans()}


@contextmanager
def global_propagators(*propagators: Any) -> Generator[None]:
    """Install a composite global propagator and restore the previous one on exit."""
    previous = propagate.get_global_textmap()
    propagate.set_global_textmap(CompositePropagator(list(propagators)))
    try:
        yield
    finally:
        propagate.set_global_textmap(previous)


@asynccontextmanager
async def traced_service(
    *,
    sampler: Sampler | None = None,
    baggage_allowlist: Sequence[str] = (),
    trusted: Callable[[_Scope], bool] | None = None,
    propagators: Sequence[Any] | None = None,
) -> AsyncGenerator[TracedService]:
    """Run `GET /orders/{id}`: FastAPI -> async SQLAlchemy `SELECT` -> outbound httpx call."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=sampler or ParentBased(ALWAYS_ON))
    provider.add_span_processor(BaggageAllowlistProcessor(baggage_allowlist))
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, sku TEXT NOT NULL)"))
        await conn.execute(text("INSERT INTO orders (id, sku) VALUES (1, 'sku-42')"))

    downstream: list[httpx.Headers] = []

    def inventory(request: httpx.Request) -> httpx.Response:
        downstream.append(request.headers)
        return httpx.Response(200, json={"stock": 7})

    outbound = httpx.AsyncClient(
        transport=httpx.MockTransport(inventory), base_url="http://inventory.internal"
    )
    app = FastAPI()

    @app.get("/orders/{order_id}")
    async def get_order(order_id: int) -> dict[str, Any]:
        async with engine.connect() as conn:
            row = (
                await conn.execute(text("SELECT sku FROM orders WHERE id = :id"), {"id": order_id})
            ).first()
        sku = row[0] if row else "unknown"
        stock = (await outbound.get(f"/stock/{sku}")).json()["stock"]
        ctx = trace.get_current_span().get_span_context()
        return {
            "trace_id": format_trace_id(ctx.trace_id),
            "span_id": format_span_id(ctx.span_id),
            "stock": stock,
            "baggage": {k: str(v) for k, v in baggage.get_all().items()},
        }

    default = [TraceContextTextMapPropagator(), W3CBaggagePropagator()]
    with global_propagators(*(propagators if propagators is not None else default)):
        HTTPXClientInstrumentor.instrument_client(outbound, tracer_provider=provider)
        sql = SQLAlchemyInstrumentor()
        # skip_dep_check: opentelemetry-instrumentation-sqlalchemy 0.66b2 declares
        # "sqlalchemy >= 1.0.0, < 2.1.0"; without it instrument() logs and returns None.
        sql.instrument(engine=engine.sync_engine, tracer_provider=provider, skip_dep_check=True)
        FastAPIInstrumentor.instrument_app(
            app, tracer_provider=provider, exclude_spans=["send", "receive"]
        )
        entry: Any = app if trusted is None else TrustBoundary(app, trusted=trusted)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=entry), base_url="http://test")
        try:
            yield TracedService(entry, client, exporter, downstream)
        finally:
            await client.aclose()
            await outbound.aclose()
            FastAPIInstrumentor.uninstrument_app(app)
            sql.uninstrument()
            await engine.dispose()
            provider.shutdown()


def header_map(headers: Mapping[str, str]) -> dict[str, str]:
    """Lower-case a mapping's keys for stable assertions."""
    return {k.lower(): v for k, v in headers.items()}
