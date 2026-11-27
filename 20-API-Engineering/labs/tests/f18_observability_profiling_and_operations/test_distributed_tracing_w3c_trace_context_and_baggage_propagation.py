"""Lab for W3C Trace Context (traceparent, tracestate) and Baggage propagation.

Pure codec tests pin the W3C grammar; propagator tests pin where opentelemetry-api 1.45.2 deviates;
service tests drive FastAPI -> async SQLAlchemy -> httpx and assert one parent-child span tree.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
import pytest
from opentelemetry import baggage, context, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.instrumentation.sqlalchemy import (  # pyright: ignore[reportMissingTypeStubs]
    SQLAlchemyInstrumentor,
)
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
from opentelemetry.trace import format_span_id, format_trace_id
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.trace.span import TraceState
from sqlalchemy import create_engine, text

from api_labs.f18_observability_profiling_and_operations.distributed_tracing_w3c_trace_context_and_baggage_propagation import (  # noqa: E501
    BAGGAGE_MAX_BYTES,
    BAGGAGE_MAX_MEMBERS,
    BaggageMember,
    encode_baggage,
    format_traceparent,
    format_tracestate,
    parse_baggage,
    parse_traceparent,
    parse_tracestate,
    set_tracestate_member,
    traced_service,
    truncate_tracestate,
)

TID = "4bf92f3577b34da6a3ce929d0e0e4736"
PID = "00f067aa0ba902b7"
TP = f"00-{TID}-{PID}-01"
TRACEPARENT_RE = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")


def _ctx(span: ReadableSpan) -> trace.SpanContext:
    ctx = span.context
    assert isinstance(ctx, trace.SpanContext)
    return ctx


def _otel_extract(carrier: dict[str, str]) -> trace.SpanContext:
    ctx = TraceContextTextMapPropagator().extract(carrier)
    return trace.get_current_span(ctx).get_span_context()


# ---------------------------------------------------------------- traceparent codec


@pytest.mark.unit
def test_spec_examples_parse_and_roundtrip() -> None:
    sampled = parse_traceparent(f"00-{TID}-{PID}-01")
    unsampled = parse_traceparent(f"00-{TID}-{PID}-00")
    assert sampled is not None
    assert unsampled is not None
    assert (sampled.version, sampled.trace_id, sampled.parent_id) == (0, TID, PID)
    assert sampled.sampled
    assert not unsampled.sampled
    assert format_traceparent(TID, PID, 1) == f"00-{TID}-{PID}-01"
    assert len(format_traceparent(TID, PID, 1)) == 55


INVALID_TRACEPARENTS = [
    pytest.param(f"00-{TID.upper()}-{PID}-01", id="uppercase-trace-id"),
    pytest.param(f"00-{TID}-{PID.upper()}-01", id="uppercase-parent-id"),
    pytest.param(f"00-{'0' * 32}-{PID}-01", id="zero-trace-id"),
    pytest.param(f"00-{TID}-{'0' * 16}-01", id="zero-parent-id"),
    pytest.param(f"ff-{TID}-{PID}-01", id="version-ff"),
    pytest.param(f"00-{TID}-{PID}-01-extra", id="v00-with-suffix"),
    pytest.param(f"00-{TID}-{PID}-0", id="short-flags"),
    pytest.param(f"00_{TID}-{PID}-01", id="bad-delimiter"),
    pytest.param(f"00-{TID[:-1]}g-{PID}-01", id="non-hex-trace-id"),
    pytest.param(f"01-{TID}-{PID}-01extra", id="v01-suffix-without-dash"),
    pytest.param("", id="empty"),
]


@pytest.mark.unit
@pytest.mark.parametrize("value", INVALID_TRACEPARENTS)
def test_invalid_traceparent_is_rejected_by_reference_and_otel(value: str) -> None:
    assert parse_traceparent(value) is None
    assert not _otel_extract({"traceparent": value}).is_valid


@pytest.mark.unit
def test_higher_version_is_parsed_with_the_v00_layout() -> None:
    exact = parse_traceparent(f"01-{TID}-{PID}-01")
    extended = parse_traceparent(f"01-{TID}-{PID}-01-opaque-future-field")
    assert exact is not None
    assert extended is not None
    assert (extended.version, extended.trace_id, extended.parent_id) == (1, TID, PID)
    otel = _otel_extract({"traceparent": f"01-{TID}-{PID}-01-opaque-future-field"})
    assert format_trace_id(otel.trace_id) == TID


@pytest.mark.unit
def test_flags_are_a_bit_field_not_a_number() -> None:
    assert parse_traceparent(f"00-{TID}-{PID}-09") is not None
    for flags, sampled, random in [("09", True, False), ("08", False, False), ("03", True, True)]:
        parsed = parse_traceparent(f"00-{TID}-{PID}-{flags}")
        assert parsed is not None
        assert (parsed.sampled, parsed.random_trace_id) == (sampled, random)


@pytest.mark.unit
def test_format_clears_unknown_flag_bits() -> None:
    assert format_traceparent(TID, PID, 0xFF) == f"00-{TID}-{PID}-03"
    assert format_traceparent(TID, PID, 0x08) == f"00-{TID}-{PID}-00"


@pytest.mark.unit
def test_passthrough_propagator_forwards_unknown_flag_bits_unlike_the_spec() -> None:
    prop = TraceContextTextMapPropagator()
    ctx = prop.extract({"traceparent": f"00-{TID}-{PID}-ff"})
    out: dict[str, str] = {}
    prop.inject(out, ctx)
    assert out["traceparent"] == f"00-{TID}-{PID}-ff"
    assert format_traceparent(TID, PID, 0xFF) != out["traceparent"]


# ---------------------------------------------------------------- tracestate codec

SPEC_TS = "rojo=00f067aa0ba902b7,congo=t61rcWkgMzE"


@pytest.mark.unit
def test_tracestate_ows_empty_members_and_order() -> None:
    members = parse_tracestate([" rojo=00f067aa0ba902b7 ,, \t congo=t61rcWkgMzE ", ""])
    assert members == [("rojo", "00f067aa0ba902b7"), ("congo", "t61rcWkgMzE")]
    assert format_tracestate(members) == SPEC_TS


TRACESTATE_TABLE = [
    pytest.param("a=1,b=2", True, id="two"),
    pytest.param("a=1,a=2", False, id="duplicate-key"),
    pytest.param("A=1", False, id="uppercase-key"),
    pytest.param("a=b=c", False, id="equals-in-value"),
    pytest.param("a=1 ", True, id="trailing-space-is-ows"),
    pytest.param("a=", False, id="empty-value"),
    pytest.param("a", False, id="no-equals"),
    pytest.param("tenant@vendor=1", True, id="multi-tenant"),
    pytest.param("1tenant@vendor=1", True, id="tenant-may-start-with-digit"),
    pytest.param("1simple=1", False, id="simple-key-needs-letter"),
    pytest.param("t@1vendor=1", False, id="system-id-needs-letter"),
    pytest.param("a" * 256 + "=1", True, id="key-256"),
    pytest.param("a" * 257 + "=1", False, id="key-257"),
    pytest.param("a=" + "v" * 256, True, id="value-256"),
    pytest.param("a=" + "v" * 257, False, id="value-257"),
    pytest.param("t" * 241 + "@" + "s" * 14 + "=1", True, id="tenant-241-system-14"),
    pytest.param("t" * 242 + "@s=1", False, id="tenant-242"),
    pytest.param("t@" + "s" * 15 + "=1", False, id="system-15"),
    pytest.param(",".join(f"k{i}=v" for i in range(32)), True, id="32-members"),
    pytest.param(",".join(f"k{i}=v" for i in range(33)), False, id="33-members"),
]


@pytest.mark.unit
@pytest.mark.parametrize(("header", "valid"), TRACESTATE_TABLE)
def test_tracestate_grammar_agrees_with_otel(header: str, valid: bool) -> None:
    ours = parse_tracestate([header])
    theirs = TraceState.from_header([header])
    assert bool(ours) is valid
    assert bool(len(theirs)) is valid
    assert [(k, v) for k, v in theirs.items()] == ours


@pytest.mark.unit
def test_set_member_moves_key_left_and_keeps_the_rest_in_order() -> None:
    start = parse_tracestate(["congo=congosFirstPosition,rojo=rojosFirstPosition"])
    again = set_tracestate_member(start, "congo", "congosSecondPosition")
    assert format_tracestate(again) == "congo=congosSecondPosition,rojo=rojosFirstPosition"
    added = set_tracestate_member(again, "vendor", "x")
    assert [k for k, _ in added] == ["vendor", "congo", "rojo"]
    full = parse_tracestate([",".join(f"k{i}=v" for i in range(32))])
    grown = set_tracestate_member(full, "new", "v")
    assert len(grown) == 32
    assert grown[0] == ("new", "v")
    assert ("k31", "v") not in grown
    with pytest.raises(ValueError, match="invalid tracestate member"):
        set_tracestate_member(full, "Bad", "v")


@pytest.mark.unit
def test_truncate_drops_big_entries_first_then_from_the_right_by_whole_entries() -> None:
    big = ("big", "x" * 140)
    members = [("a", "1" * 100), big, ("b", "2" * 100), ("c", "3" * 100), ("d", "4" * 100)]
    assert len(format_tracestate(members)) > 512
    truncated = truncate_tracestate(members)
    assert big not in truncated
    assert (
        truncated
        == [("a", "1" * 100), ("b", "2" * 100), ("c", "3" * 100), ("d", "4" * 100)][
            : len(truncated)
        ]
    )
    assert [k for k, _ in truncated] == ["a", "b", "c", "d"][: len(truncated)]
    assert len(format_tracestate(truncated)) <= 512
    assert truncate_tracestate([("a", "1")]) == [("a", "1")]
    only_big = truncate_tracestate([("a", "1" * 600)])
    assert only_big == []


@pytest.mark.unit
def test_otel_tracestate_add_at_32_members_refuses_where_the_reference_evicts() -> None:
    state = TraceState([(f"k{i}", "v") for i in range(32)])
    assert state.add("new", "v") is state
    assert "new" not in state


# ---------------------------------------------------------------- baggage codec


@pytest.mark.unit
def test_baggage_spec_examples_decode_and_roundtrip() -> None:
    wire = "userId=Am%C3%A9lie,serverNode=DF%2028,isProduction=false"
    members = parse_baggage([wire])
    assert [(m.key, m.value) for m in members] == [
        ("userId", "Am\u00e9lie"),
        ("serverNode", "DF 28"),
        ("isProduction", "false"),
    ]
    assert encode_baggage(members) == wire


@pytest.mark.unit
def test_baggage_whitespace_properties_and_equals_in_value() -> None:
    wire = "key1=value1;property1;property2, key2 = value2, key3=value3; propertyKey=propertyValue"
    members = parse_baggage([wire])
    assert [m.key for m in members] == ["key1", "key2", "key3"]
    assert members[0].properties == ("property1", "property2")
    assert members[2].properties == ("propertyKey=propertyValue",)
    assert parse_baggage(["a=b=c"])[0].value == "b=c"
    assert encode_baggage([BaggageMember("a", "b=c")]) == "a=b=c"


@pytest.mark.unit
def test_baggage_plus_is_a_literal_and_percent_must_be_encoded() -> None:
    assert parse_baggage(["a=x+y%20z"])[0].value == "x+y z"
    assert encode_baggage([BaggageMember("a", "x+y z%")]) == "a=x+y%20z%25"
    assert parse_baggage(["a=%FF"])[0].value == "\ufffd"


@pytest.mark.unit
def test_baggage_malformed_members_are_dropped_and_duplicates_preserved() -> None:
    members = parse_baggage(['ok=1,no-equals,bad key=2,quote="x",dup=1,dup=2,semi=a;b'])
    assert [(m.key, m.value) for m in members] == [
        ("ok", "1"),
        ("dup", "1"),
        ("dup", "2"),
        ("semi", "a"),
    ]
    assert parse_baggage(["a=1", "b=2"]) == [BaggageMember("a", "1"), BaggageMember("b", "2")]
    with pytest.raises(ValueError, match="token"):
        encode_baggage([BaggageMember("bad key", "1")])


@pytest.mark.unit
def test_baggage_limits_drop_whole_members_only() -> None:
    many = [BaggageMember(f"k{i}", "v") for i in range(BAGGAGE_MAX_MEMBERS + 5)]
    assert len(parse_baggage([encode_baggage(many)])) == BAGGAGE_MAX_MEMBERS
    assert len(encode_baggage(many).split(",")) == BAGGAGE_MAX_MEMBERS
    wide = [BaggageMember(f"k{i}", "v" * 1000) for i in range(10)]
    encoded = encode_baggage(wide)
    assert len(encoded.encode()) <= BAGGAGE_MAX_BYTES
    assert all(part.endswith("v" * 1000) for part in encoded.split(","))
    assert len(encoded.split(",")) == 8


@pytest.mark.unit
def test_otel_baggage_decodes_plus_as_space_and_encodes_space_as_plus() -> None:
    prop = W3CBaggagePropagator()
    ctx = prop.extract({"baggage": "a=x+y,b=m%20n"})
    assert baggage.get_all(ctx) == {"a": "x y", "b": "m n"}
    assert parse_baggage(["a=x+y"])[0].value == "x+y"
    out: dict[str, str] = {}
    prop.inject(out, baggage.set_baggage("k", "a b"))
    assert out["baggage"] == "k=a+b"
    assert encode_baggage([BaggageMember("k", "a b")]) == "k=a%20b"


@pytest.mark.unit
def test_otel_baggage_reads_only_the_first_header_line_even_from_a_list_carrier() -> None:
    prop = W3CBaggagePropagator()
    ctx = prop.extract({"baggage": ["a=1", "b=2"]})
    assert baggage.get_all(ctx) == {"a": "1"}
    assert [m.key for m in parse_baggage(["a=1", "b=2"])] == ["a", "b"]


@pytest.mark.unit
def test_otel_baggage_limits_are_180_pairs_not_the_spec_minimum_of_64() -> None:
    prop = W3CBaggagePropagator()
    header = ",".join(f"k{i}=v" for i in range(200))
    ctx = prop.extract({"baggage": header})
    assert len(baggage.get_all(ctx)) == 180
    assert len(parse_baggage([header])) == BAGGAGE_MAX_MEMBERS
    assert not baggage.get_all(prop.extract({"baggage": "a=" + "v" * 8200}))


# ---------------------------------------------------------------- carriers and context


@pytest.mark.unit
def test_dict_carrier_lookup_is_case_sensitive_so_normalize_names_yourself() -> None:
    assert _otel_extract({"traceparent": TP}).is_valid
    assert not _otel_extract({"Traceparent": TP}).is_valid


@pytest.mark.unit
def test_empty_composite_propagator_extract_returns_none() -> None:
    assert CompositePropagator([]).extract({"traceparent": TP}) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_crosses_create_task_and_to_thread_but_not_run_in_executor() -> None:
    token = context.attach(baggage.set_baggage("tenant", "acme"))
    try:
        loop = asyncio.get_running_loop()
        via_task = await asyncio.create_task(asyncio.sleep(0, result=dict(baggage.get_all())))
        via_thread = await asyncio.to_thread(lambda: dict(baggage.get_all()))
        via_executor = await loop.run_in_executor(None, lambda: dict(baggage.get_all()))
    finally:
        context.detach(token)
    assert via_task == {"tenant": "acme"}
    assert via_thread == {"tenant": "acme"}
    assert via_executor == {}


# ---------------------------------------------------------------- the traced service


def _headers(**extra: str) -> dict[str, str]:
    return {
        "traceparent": TP,
        "tracestate": SPEC_TS,
        "baggage": "tenant=acme,secret=hunter2",
        **extra,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_span_tree_from_request_through_sql_and_http() -> None:
    async with traced_service() as svc:
        response = await svc.client.get("/orders/1", headers=_headers())
        spans = svc.exporter.get_finished_spans()
    assert response.status_code == 200
    server = next(s for s in spans if s.kind is trace.SpanKind.SERVER)
    children = [s for s in spans if s is not server]
    assert {s.name for s in children} == {"connect", "SELECT", "GET"}
    assert server.parent is not None
    assert server.parent.is_remote
    assert format_span_id(server.parent.span_id) == PID
    assert {format_trace_id(_ctx(s).trace_id) for s in spans} == {TID}
    for child in children:
        assert child.parent is not None
        assert child.parent.span_id == _ctx(server).span_id
        assert not child.parent.is_remote
    assert len({_ctx(s).span_id for s in spans}) == len(spans)
    assert response.json()["trace_id"] == TID


@pytest.mark.unit
@pytest.mark.asyncio
async def test_outbound_traceparent_names_the_client_span_as_parent_and_forwards_tracestate() -> (
    None
):
    async with traced_service() as svc:
        await svc.client.get("/orders/1", headers=_headers())
        client_span = svc.spans_by_name()["GET"]
        sent = svc.downstream_headers[0]
    assert TRACEPARENT_RE.fullmatch(sent["traceparent"])
    assert len(sent["traceparent"]) == 55
    assert sent["traceparent"] == f"00-{TID}-{format_span_id(_ctx(client_span).span_id)}-01"
    assert PID not in sent["traceparent"]
    assert sent["tracestate"] == SPEC_TS


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("incoming", "outgoing"), [("01", "01"), ("03", "03"), ("ff", "03")])
async def test_sampled_and_random_flags_are_recomputed_not_copied(
    incoming: str, outgoing: str
) -> None:
    async with traced_service() as svc:
        await svc.client.get(
            "/orders/1", headers=_headers(traceparent=f"00-{TID}-{PID}-{incoming}")
        )
        sent = svc.downstream_headers[0]["traceparent"]
    assert sent.endswith(f"-{outgoing}")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsampled_parent_records_nothing_but_still_propagates_with_a_fresh_parent_id() -> (
    None
):
    async with traced_service() as svc:
        response = await svc.client.get(
            "/orders/1", headers=_headers(traceparent=f"00-{TID}-{PID}-00")
        )
        sent = svc.downstream_headers[0]["traceparent"]
        spans = svc.exporter.get_finished_spans()
    assert response.status_code == 200
    assert spans == ()
    version, trace_id, parent_id, flags = sent.split("-")
    assert (version, trace_id, flags) == ("00", TID, "00")
    assert parent_id not in (PID, "0" * 16)
    # The outbound parent-id is the (non-recording) httpx client span, not the server span.
    assert parent_id != response.json()["span_id"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("value", [p.values[0] for p in INVALID_TRACEPARENTS if p.values[0]])
async def test_invalid_traceparent_restarts_the_trace_and_drops_tracestate(value: str) -> None:
    async with traced_service() as svc:
        await svc.client.get("/orders/1", headers=_headers(traceparent=str(value)))
        server = next(s for s in svc.exporter.get_finished_spans() if s.name.startswith("GET /"))
        sent = svc.downstream_headers[0]
    assert server.parent is None
    assert format_trace_id(_ctx(server).trace_id) != TID
    assert "tracestate" not in sent
    assert sent["traceparent"].split("-")[1] == format_trace_id(_ctx(server).trace_id)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_future_version_is_continued_and_downgraded_to_00() -> None:
    async with traced_service() as svc:
        await svc.client.get("/orders/1", headers=_headers(traceparent=f"01-{TID}-{PID}-01-future"))
        sent = svc.downstream_headers[0]["traceparent"]
    assert sent.startswith(f"00-{TID}-")
    assert len(sent) == 55


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multiple_tracestate_field_lines_are_combined_in_order() -> None:
    raw = httpx.Headers([("traceparent", TP), ("tracestate", "a=1"), ("tracestate", "b=2")])
    async with traced_service() as svc:
        await svc.client.get("/orders/1", headers=raw)
        sent = svc.downstream_headers[0]["tracestate"]
    assert sent == "a=1,b=2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_baggage_survives_a_traceparent_restart_because_it_is_a_separate_propagator() -> None:
    async with traced_service() as svc:
        response = await svc.client.get("/orders/1", headers=_headers(traceparent="garbage"))
        sent = svc.downstream_headers[0]
    assert response.json()["trace_id"] != TID
    assert sent["baggage"] == "tenant=acme,secret=hunter2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_baggage_field_lines_lose_the_second_in_the_server_context() -> None:
    raw = httpx.Headers([("traceparent", TP), ("baggage", "a=1"), ("baggage", "b=2")])
    async with traced_service() as svc:
        response = await svc.client.get("/orders/1", headers=raw)
        sent = svc.downstream_headers[0]["baggage"]
    assert response.json()["baggage"] == {"a": "1"}
    assert sent == "a=1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_only_allowlisted_baggage_keys_reach_span_attributes_but_all_reach_outbound() -> None:
    async with traced_service(baggage_allowlist=["tenant"]) as svc:
        await svc.client.get("/orders/1", headers=_headers())
        spans = svc.exporter.get_finished_spans()
        sent = svc.downstream_headers[0]["baggage"]
    assert sent == "tenant=acme,secret=hunter2"
    for span in spans:
        assert span.attributes is not None
        assert span.attributes["baggage.tenant"] == "acme"
        assert "baggage.secret" not in span.attributes
    sql = next(s for s in spans if s.name == "SELECT")
    assert sql.attributes is not None
    assert "hunter2" not in repr(dict(sql.attributes))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_response_does_not_echo_trace_context_headers() -> None:
    async with traced_service() as svc:
        response = await svc.client.get("/orders/1", headers=_headers())
    assert "traceparent" not in response.headers
    assert "tracestate" not in response.headers
    assert "baggage" not in response.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_untrusted_callers_are_restarted_and_lose_baggage_trusted_callers_continue() -> None:
    def trusted(scope: dict[str, object]) -> bool:
        headers = scope["headers"]
        assert isinstance(headers, list)
        return (b"x-internal", b"1") in headers

    async with traced_service(trusted=trusted) as svc:
        outside = await svc.client.get("/orders/1", headers=_headers())
        inside = await svc.client.get("/orders/1", headers=_headers(**{"x-internal": "1"}))
        sent_outside, sent_inside = svc.downstream_headers
    assert outside.json()["trace_id"] != TID
    assert "baggage" not in sent_outside
    assert "tracestate" not in sent_outside
    assert inside.json()["trace_id"] == TID
    assert sent_inside["baggage"] == "tenant=acme,secret=hunter2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_requests_do_not_share_trace_or_baggage_context() -> None:
    other_tid = "11111111111111111111111111111111"

    async def call(svc_client: httpx.AsyncClient, tid: str, tenant: str) -> dict[str, object]:
        headers = {"traceparent": f"00-{tid}-{PID}-01", "baggage": f"tenant={tenant}"}
        return dict((await svc_client.get("/orders/1", headers=headers)).json())

    async with traced_service() as svc:
        a, b = await asyncio.gather(
            call(svc.client, TID, "acme"), call(svc.client, other_tid, "zed")
        )
    assert (a["trace_id"], a["baggage"]) == (TID, {"tenant": "acme"})
    assert (b["trace_id"], b["baggage"]) == (other_tid, {"tenant": "zed"})


# ---------------------------------------------------------------- instrumentation gate


@pytest.mark.unit
def test_sqlalchemy_instrumentor_refuses_sqlalchemy_2_1_and_emits_no_spans(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ParentBased(ALWAYS_ON))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    engine = create_engine("sqlite://")
    with caplog.at_level(logging.ERROR):
        result = SQLAlchemyInstrumentor().instrument(engine=engine, tracer_provider=provider)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    engine.dispose()
    assert result is None
    assert "falls outside of that range" in caplog.text
    assert exporter.get_finished_spans() == ()
