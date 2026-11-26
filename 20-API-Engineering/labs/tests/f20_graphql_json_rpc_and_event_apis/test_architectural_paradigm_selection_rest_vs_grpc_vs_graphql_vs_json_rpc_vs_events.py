"""Lab for the paradigm decision matrix: wire bytes, schema evolution, round trips, chooser."""

import asyncio
import dataclasses
import json
from collections.abc import Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI
from google.protobuf import json_format  # type: ignore[import-untyped]

from api_labs.f20_graphql_json_rpc_and_event_apis.architectural_paradigm_selection_rest_vs_grpc_vs_graphql_vs_json_rpc_vs_events import (  # noqa: E501
    DEFAULT_GRPC_MAX_RECEIVE_BYTES,
    FULL_ORDER_QUERY,
    GRPC_WEB_TRAILER_FLAG,
    LINE_FIELDS,
    ORDER_FIELDS,
    ORDER_SCHEMA,
    RULES,
    SAMPLE_ORDER,
    Boundary,
    Change,
    FieldSpec,
    Outcome,
    Paradigm,
    Requirements,
    Rule,
    ScreenView,
    Topology,
    Transport,
    benchmark_roundtrips,
    build_screen_app,
    compile_schema,
    critical_path_ms,
    encode_protobuf,
    encode_rest,
    evolution_matrix,
    exceeds_default_receive_limit,
    execute_graphql,
    grpc_frame,
    grpc_web_trailer_frame,
    is_json_interoperable_int,
    load_screen_graphql,
    load_screen_json_rpc,
    load_screen_rest,
    load_screen_rest_expand,
    measure_wire,
    parse_frames,
    parse_like_javascript,
    proto_decode,
    proto_encode,
    protobuf_backend,
    rank,
    time_per_call_ns,
    tracked_client,
    writer_fields,
)


def varint(value: int) -> bytes:
    """Independent oracle: base-128 varint, least significant group first (protobuf.dev)."""
    out = bytearray()
    while True:
        group = value & 0x7F
        value >>= 7
        if value:
            out.append(group | 0x80)
        else:
            out.append(group)
            return bytes(out)


def tag(number: int, wire_type: int) -> bytes:
    return varint((number << 3) | wire_type)


def len_delimited(number: int, payload: bytes) -> bytes:
    return tag(number, 2) + varint(len(payload)) + payload


def hand_encoded_order() -> bytes:
    out = tag(1, 0) + varint(SAMPLE_ORDER.id) + tag(2, 0) + varint(SAMPLE_ORDER.customer_id)
    out += tag(3, 0) + varint(2) + tag(4, 0) + varint(SAMPLE_ORDER.created_at)
    out += len_delimited(5, b"USD")
    for line in SAMPLE_ORDER.lines:
        body = len_delimited(1, line.sku.encode("ascii"))
        body += tag(2, 0) + varint(line.quantity) + tag(3, 0) + varint(line.unit_price_cents)
        out += len_delimited(6, body)
    return out


# ---------------------------------------------------------------- protobuf wire bytes


@pytest.mark.unit
def test_protobuf_bytes_equal_an_independent_hand_encoding() -> None:
    assert encode_protobuf(SAMPLE_ORDER) == hand_encoded_order()
    assert len(encode_protobuf(SAMPLE_ORDER)) == 71


@pytest.mark.unit
def test_varint_150_is_the_documented_9601() -> None:
    schema = compile_schema("lab.probe150", {"M": (FieldSpec("a", 1, "int32"),)})
    assert proto_encode(schema, "M", {"a": 150}) == bytes.fromhex("089601")


@pytest.mark.unit
def test_field_numbers_above_15_cost_a_second_tag_byte() -> None:
    schema = compile_schema(
        "lab.tagsize", {"M": (FieldSpec("low", 15, "int32"), FieldSpec("high", 16, "int32"))}
    )
    assert len(proto_encode(schema, "M", {"low": 1})) == 2
    assert len(proto_encode(schema, "M", {"high": 1})) == 3


@pytest.mark.unit
def test_proto3_default_values_are_not_serialized() -> None:
    schema = compile_schema("lab.defaults", {"M": (FieldSpec("a", 1, "int32"),)})
    assert proto_encode(schema, "M", {"a": 0}) == b""


@pytest.mark.unit
def test_protobuf_backend_is_a_known_runtime() -> None:
    assert protobuf_backend() in {"upb", "cpp", "python"}


# ---------------------------------------------------------------- wire size comparison


@pytest.mark.unit
def test_rest_body_is_the_golden_literal() -> None:
    expected = (
        b'{"id":100234,"customerId":7731,"status":"CONFIRMED","createdAt":1767225600,'
        b'"currency":"USD","lines":[{"sku":"SKU-1001","quantity":2,"unitPriceCents":1999},'
        b'{"sku":"SKU-2002","quantity":1,"unitPriceCents":14950},'
        b'{"sku":"SKU-3003","quantity":5,"unitPriceCents":325}]}'
    )
    assert encode_rest(SAMPLE_ORDER) == expected
    assert len(expected) == 264


@pytest.mark.unit
def test_response_size_ordering_for_the_same_order() -> None:
    sizes = measure_wire(SAMPLE_ORDER)
    assert sizes["grpc"].response_bytes == 5 + 71
    assert (
        sizes["grpc"].response_bytes
        < sizes["rest"].response_bytes
        < sizes["json_rpc"].response_bytes
        < sizes["event"].response_bytes
    )
    assert sizes["graphql_full"].response_bytes > sizes["rest"].response_bytes
    assert sizes["json_rpc"].response_bytes - sizes["rest"].response_bytes == len(
        b'{"jsonrpc":"2.0","result":,"id":1}'
    )


@pytest.mark.unit
def test_graphql_selection_beats_binary_encoding_by_sending_fewer_fields() -> None:
    sizes = measure_wire(SAMPLE_ORDER)
    assert sizes["graphql_slim"].response_bytes < sizes["graphql_full"].response_bytes
    assert sizes["graphql_slim"].response_bytes < sizes["grpc"].response_bytes
    assert sizes["graphql_full"].response_bytes > sizes["grpc"].response_bytes


@pytest.mark.unit
def test_graphql_pays_for_the_query_text_in_the_request_body() -> None:
    sizes = measure_wire(SAMPLE_ORDER)
    assert sizes["rest"].request_bytes == 0
    assert sizes["graphql_full"].request_bytes > sizes["json_rpc"].request_bytes
    assert sizes["json_rpc"].request_bytes > sizes["grpc"].request_bytes
    assert sizes["graphql_full"].request_bytes - sizes["graphql_slim"].request_bytes == len(
        FULL_ORDER_QUERY
    ) - len("query($id: ID!){order(id:$id){id status}}")


@pytest.mark.unit
def test_gzip_cannot_help_a_message_this_small() -> None:
    sizes = measure_wire(SAMPLE_ORDER)
    assert sizes["grpc"].gzip_response_bytes > sizes["grpc"].response_bytes - 5
    assert sizes["rest"].gzip_response_bytes < sizes["rest"].response_bytes


# ---------------------------------------------------------------- JSON number limits


@pytest.mark.unit
def test_int_above_2_pow_53_is_corrupted_by_binary64_json_parsers() -> None:
    big = dataclasses.replace(SAMPLE_ORDER, id=2**53 + 1)
    parsed = parse_like_javascript(encode_rest(big))
    assert parsed["id"] != big.id
    assert int(parsed["id"]) == 2**53


@pytest.mark.unit
def test_protobuf_and_graphql_id_keep_int64_exact() -> None:
    big = dataclasses.replace(SAMPLE_ORDER, id=2**53 + 1)
    assert proto_decode(ORDER_SCHEMA, "Order", encode_protobuf(big))["id"] == 2**53 + 1
    payload = json.loads(execute_graphql(big, FULL_ORDER_QUERY))
    assert payload["data"]["order"]["id"] == str(2**53 + 1)


@pytest.mark.unit
def test_interoperable_integer_boundaries() -> None:
    assert is_json_interoperable_int(2**53 - 1)
    assert is_json_interoperable_int(-(2**53) + 1)
    assert not is_json_interoperable_int(2**53)
    assert not is_json_interoperable_int(-(2**53))


@pytest.mark.unit
def test_graphql_int_is_32_bit() -> None:
    past_2038 = dataclasses.replace(SAMPLE_ORDER, created_at=2**31)
    body = json.loads(execute_graphql(past_2038, FULL_ORDER_QUERY))
    assert body["data"] == {"order": None}
    assert "32-bit" in body["errors"][0]["message"]
    just_fits = dataclasses.replace(SAMPLE_ORDER, created_at=2**31 - 1)
    assert "errors" not in json.loads(execute_graphql(just_fits, FULL_ORDER_QUERY))


# ---------------------------------------------------------------- gRPC and gRPC-Web framing


@pytest.mark.unit
def test_grpc_frame_layout_is_flag_then_big_endian_length() -> None:
    frame = grpc_frame(b"x" * 258)
    assert frame[0] == 0
    assert frame[1:5] == b"\x00\x00\x01\x02"
    assert len(frame) == 5 + 258
    assert grpc_frame(b"", compressed=True) == b"\x01\x00\x00\x00\x00"


@pytest.mark.unit
def test_grpc_web_trailers_ride_in_the_body_with_the_msb_set() -> None:
    body = grpc_frame(encode_protobuf(SAMPLE_ORDER)) + grpc_web_trailer_frame(0)
    (data_flag, data), (trailer_flag, trailer) = parse_frames(body)
    assert data_flag == 0
    assert data == encode_protobuf(SAMPLE_ORDER)
    assert trailer_flag == GRPC_WEB_TRAILER_FLAG == 0b10000000
    assert trailer == b"grpc-status: 0\r\n"


@pytest.mark.unit
def test_grpc_web_trailer_carries_status_message_lines() -> None:
    frame = grpc_web_trailer_frame(5, "order not found")
    assert parse_frames(frame) == [(0x80, b"grpc-status: 5\r\ngrpc-message: order not found\r\n")]


@pytest.mark.unit
def test_default_receive_limit_is_4_mib_and_exclusive() -> None:
    assert DEFAULT_GRPC_MAX_RECEIVE_BYTES == 4_194_304
    assert not exceeds_default_receive_limit(4_194_304)
    assert exceeds_default_receive_limit(4_194_305)


# ---------------------------------------------------------------- schema evolution matrix


EXPECTED_MATRIX: dict[tuple[Paradigm, Change], Outcome] = {
    (Paradigm.REST, Change.ADD_FIELD): Outcome.COMPATIBLE,
    (Paradigm.REST, Change.REMOVE_FIELD): Outcome.HARD_ERROR,
    (Paradigm.REST, Change.RENAME_FIELD): Outcome.HARD_ERROR,
    (Paradigm.REST, Change.CHANGE_TYPE): Outcome.HARD_ERROR,
    (Paradigm.JSON_RPC, Change.ADD_FIELD): Outcome.COMPATIBLE,
    (Paradigm.JSON_RPC, Change.REMOVE_FIELD): Outcome.HARD_ERROR,
    (Paradigm.JSON_RPC, Change.RENAME_FIELD): Outcome.HARD_ERROR,
    (Paradigm.JSON_RPC, Change.CHANGE_TYPE): Outcome.HARD_ERROR,
    (Paradigm.EVENTS, Change.ADD_FIELD): Outcome.COMPATIBLE,
    (Paradigm.EVENTS, Change.REMOVE_FIELD): Outcome.HARD_ERROR,
    (Paradigm.EVENTS, Change.RENAME_FIELD): Outcome.HARD_ERROR,
    (Paradigm.EVENTS, Change.CHANGE_TYPE): Outcome.HARD_ERROR,
    (Paradigm.GRPC, Change.ADD_FIELD): Outcome.COMPATIBLE,
    (Paradigm.GRPC, Change.REMOVE_FIELD): Outcome.SILENT_WRONG,
    (Paradigm.GRPC, Change.RENAME_FIELD): Outcome.COMPATIBLE,
    (Paradigm.GRPC, Change.CHANGE_TYPE): Outcome.SILENT_WRONG,
    (Paradigm.GRAPHQL, Change.ADD_FIELD): Outcome.COMPATIBLE,
    (Paradigm.GRAPHQL, Change.REMOVE_FIELD): Outcome.HARD_ERROR,
    (Paradigm.GRAPHQL, Change.RENAME_FIELD): Outcome.HARD_ERROR,
    (Paradigm.GRAPHQL, Change.CHANGE_TYPE): Outcome.SILENT_WRONG,
}


@pytest.mark.unit
def test_evolution_matrix_matches_the_executed_experiments() -> None:
    assert evolution_matrix() == EXPECTED_MATRIX


@pytest.mark.unit
def test_old_protobuf_reader_preserves_the_unknown_field_on_reserialize() -> None:
    writer = compile_schema("lab.unknown", writer_fields(Change.ADD_FIELD))
    data = {"id": 1, "fulfillment_note": "gift wrap"}
    payload = proto_encode(writer, "Order", data)
    old = ORDER_SCHEMA.classes["Order"].FromString(payload)
    assert len_delimited(7, b"gift wrap") in old.SerializeToString()
    assert proto_decode(writer, "Order", old.SerializeToString())["fulfillment_note"] == "gift wrap"


@pytest.mark.unit
def test_reusing_a_removed_field_number_silently_changes_meaning() -> None:
    reused = compile_schema(
        "lab.reuse",
        {
            "Order": (
                *(spec for spec in ORDER_FIELDS if spec.name != "status"),
                FieldSpec("priority", 3, "int32"),
            ),
            "OrderLine": LINE_FIELDS,
        },
    )
    payload = proto_encode(reused, "Order", {"id": 1, "priority": 9})
    assert proto_decode(ORDER_SCHEMA, "Order", payload)["status"] == 9


@pytest.mark.unit
def test_renamed_protobuf_field_keeps_the_wire_but_breaks_protojson() -> None:
    writer = compile_schema("lab.rename", writer_fields(Change.RENAME_FIELD))
    payload = proto_encode(writer, "Order", {"id": 1, "state": 2})
    assert proto_decode(ORDER_SCHEMA, "Order", payload)["status"] == 2
    as_json = json_format.MessageToJson(writer.classes["Order"].FromString(payload))
    with pytest.raises(json_format.ParseError, match="state"):
        json_format.Parse(as_json, ORDER_SCHEMA.classes["Order"]())


@pytest.mark.unit
def test_wire_type_mismatch_becomes_an_unknown_field_without_an_error() -> None:
    writer = compile_schema("lab.typechange", writer_fields(Change.CHANGE_TYPE))
    payload = proto_encode(writer, "Order", {"id": 1, "created_at": "2026-01-01T00:00:00Z"})
    assert proto_decode(ORDER_SCHEMA, "Order", payload)["created_at"] == 0
    old = ORDER_SCHEMA.classes["Order"].FromString(payload)
    assert len_delimited(4, b"2026-01-01T00:00:00Z") in old.SerializeToString()


# ---------------------------------------------------------------- one screen, four ways


EXPECTED_VIEW = ScreenView(
    order_id=100_234,
    status="CONFIRMED",
    customer_name="Ada Lovelace",
    customer_tier="GOLD",
    lines=(
        ("SKU-1001", 2, "Brass Gear"),
        ("SKU-2002", 1, "Steel Spring"),
        ("SKU-3003", 5, "Rivet"),
    ),
)
type Loader = Callable[[httpx.AsyncClient, int], Awaitable[ScreenView]]
LOADERS: dict[str, Loader] = {
    "rest": load_screen_rest,
    "rest_expand": load_screen_rest_expand,
    "json_rpc": load_screen_json_rpc,
    "graphql": load_screen_graphql,
}


async def run_loader(name: str, app: FastAPI) -> tuple[ScreenView, int, int, int]:
    client, tracker = tracked_client(app)
    async with client:
        view = await LOADERS[name](client, SAMPLE_ORDER.id)
    return view, tracker.requests, tracker.max_depth, tracker.response_bytes


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "requests", "depth"),
    [("rest", 5, 2), ("rest_expand", 1, 1), ("json_rpc", 2, 2), ("graphql", 1, 1)],
)
async def test_round_trips_depend_on_what_the_interface_can_express(
    name: str, requests: int, depth: int
) -> None:
    view, made, max_depth, _ = await run_loader(name, build_screen_app())
    assert view == EXPECTED_VIEW
    assert (made, max_depth) == (requests, depth)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_graphql_moves_fewest_bytes_for_the_same_screen() -> None:
    app = build_screen_app()
    sizes = {name: (await run_loader(name, app))[3] for name in LOADERS}
    assert sizes["graphql"] < min(v for k, v in sizes.items() if k != "graphql")
    assert sizes["json_rpc"] > sizes["rest"] > sizes["graphql"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tracker_separates_parallel_siblings_from_dependent_chains() -> None:
    client, tracker = tracked_client(build_screen_app())
    async with client:
        await asyncio.gather(*(client.get("/products/SKU-1001") for _ in range(5)))
        assert (tracker.requests, tracker.max_depth) == (5, 1)
        for _ in range(3):
            await client.get("/products/SKU-1001")
    assert (tracker.requests, tracker.max_depth) == (8, 4)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_json_rpc_batch_has_no_way_to_reference_an_earlier_result() -> None:
    client, _ = tracked_client(build_screen_app())
    async with client:
        batch = [
            {"jsonrpc": "2.0", "method": "order.get", "params": {"id": 1}, "id": 1},
            {
                "jsonrpc": "2.0",
                "method": "customer.get",
                "params": {"id": "$1.customerId"},
                "id": 2,
            },
        ]
        replies = (await client.post("/rpc", json=batch)).json()
    by_id = {r["id"]: r for r in replies}
    assert "result" in by_id[1]
    assert "result" not in by_id[2]
    assert by_id[2]["error"]["code"] == -32603


@pytest.mark.unit
@pytest.mark.asyncio
async def test_json_rpc_unknown_method_and_notification_follow_the_spec() -> None:
    client, _ = tracked_client(build_screen_app())
    async with client:
        missing = await client.post(
            "/rpc", json={"jsonrpc": "2.0", "method": "order.fetch", "id": 7}
        )
        batch = await client.post(
            "/rpc",
            json=[
                {"jsonrpc": "2.0", "method": "order.get", "params": {}, "id": 1},
                {"jsonrpc": "2.0", "method": "order.get", "params": {}},
            ],
        )
    assert missing.json()["error"]["code"] == -32601
    assert missing.status_code == 200
    assert [r["id"] for r in batch.json()] == [1]


@pytest.mark.unit
def test_critical_path_is_depth_times_per_hop_cost() -> None:
    assert critical_path_ms(2, rtt_ms=80.0, server_ms=5.0) == 170.0
    assert critical_path_ms(1, rtt_ms=80.0, server_ms=5.0) == 85.0
    assert critical_path_ms(0, rtt_ms=80.0, server_ms=5.0) == 0.0


# ---------------------------------------------------------------- measurement plumbing


@pytest.mark.unit
def test_time_per_call_divides_elapsed_by_iterations_and_runs_every_call() -> None:
    ticks = iter([1_000, 1_000 + 5_000])
    calls: list[int] = []
    result = time_per_call_ns(lambda: calls.append(1), 10, clock=lambda: next(ticks))
    assert len(calls) == 10
    assert result == 500.0


@pytest.mark.unit
def test_benchmark_reports_every_codec_path() -> None:
    result = benchmark_roundtrips(SAMPLE_ORDER, iterations=3)
    assert set(result) == {
        "rest_json",
        "graphql_full",
        "protobuf",
        "rest_json_decode_only",
        "graphql_decode_only",
        "protobuf_decode_only",
    }
    assert all(value > 0 for value in result.values())


# ---------------------------------------------------------------- the decision engine


PUBLIC_PAYMENTS = Requirements(
    Boundary.PUBLIC_THIRD_PARTY, Topology.SINGLE_TEAM, cdn_cacheable_reads=True,
    needs_synchronous_answer=True,
)  # fmt: skip
INTERNAL_MESH = Requirements(
    Boundary.INTERNAL_SERVICE, Topology.MANY_SERVICE_TEAMS, polyglot_services=True,
    serial_hops=4, needs_synchronous_answer=True,
)  # fmt: skip
MOBILE_AND_WEB = Requirements(
    Boundary.FIRST_PARTY_CLIENT, Topology.FRONTEND_BACKEND_SPLIT, browser_direct=True,
    client_shaped_reads=True, needs_synchronous_answer=True,
)  # fmt: skip
DOMAIN_EVENTS = Requirements(
    Boundary.INTERNAL_SERVICE, Topology.MANY_SERVICE_TEAMS, decoupled_in_time=True
)
EDITOR_PROTOCOL = Requirements(
    Boundary.FIRST_PARTY_CLIENT, Topology.SINGLE_TEAM, transport=Transport.STDIO_OR_WEBSOCKET,
    needs_synchronous_answer=True,
)  # fmt: skip
CATALOG = [
    PUBLIC_PAYMENTS,
    INTERNAL_MESH,
    MOBILE_AND_WEB,
    DOMAIN_EVENTS,
    EDITOR_PROTOCOL,
    dataclasses.replace(MOBILE_AND_WEB, can_run_translation_proxy=False),
    dataclasses.replace(INTERNAL_MESH, bidirectional_streaming=True),
    dataclasses.replace(DOMAIN_EVENTS, needs_synchronous_answer=True),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("requirements", "winner"),
    [
        (PUBLIC_PAYMENTS, Paradigm.REST),
        (INTERNAL_MESH, Paradigm.GRPC),
        (MOBILE_AND_WEB, Paradigm.GRAPHQL),
        (DOMAIN_EVENTS, Paradigm.EVENTS),
        (EDITOR_PROTOCOL, Paradigm.JSON_RPC),
    ],
)
def test_canonical_scenarios_pick_their_paradigm(
    requirements: Requirements, winner: Paradigm
) -> None:
    assert rank(requirements)[0].paradigm is winner


@pytest.mark.unit
def test_exact_scores_for_the_public_payments_scenario() -> None:
    scores = {r.paradigm: r.score for r in rank(PUBLIC_PAYMENTS)}
    assert scores == {
        Paradigm.REST: 1 + 3 + 3,
        Paradigm.GRPC: -2 - 3,
        Paradigm.GRAPHQL: -1,
        Paradigm.JSON_RPC: -1 - 2,
        Paradigm.EVENTS: 0,
    }


@pytest.mark.unit
def test_non_http_transport_vetoes_rest_and_grpc() -> None:
    ranked = rank(EDITOR_PROTOCOL)
    vetoed = {r.paradigm for r in ranked if r.vetoed}
    assert vetoed == {Paradigm.REST, Paradigm.GRPC, Paradigm.EVENTS}
    assert [r.paradigm for r in ranked[:2]] == [Paradigm.JSON_RPC, Paradigm.GRAPHQL]
    without = [rule for rule in RULES if rule.name != "non_http_transport"]
    reranked = rank(EDITOR_PROTOCOL, without)
    assert {r.paradigm for r in reranked if r.vetoed} == {Paradigm.EVENTS}
    assert reranked[0].paradigm is Paradigm.REST


@pytest.mark.unit
def test_synchronous_answer_vetoes_events_even_when_decoupling_scores_it_first() -> None:
    wants_both = dataclasses.replace(DOMAIN_EVENTS, needs_synchronous_answer=True)
    ranked = rank(wants_both)
    assert ranked[-1].paradigm is Paradigm.EVENTS
    assert ranked[-1].vetoed
    assert ranked[-1].score == 5 + 1


@pytest.mark.unit
def test_browser_without_a_proxy_vetoes_grpc_but_with_one_only_penalizes_it() -> None:
    no_proxy = dataclasses.replace(MOBILE_AND_WEB, can_run_translation_proxy=False)
    assert next(r for r in rank(no_proxy) if r.paradigm is Paradigm.GRPC).vetoed
    with_proxy = next(r for r in rank(MOBILE_AND_WEB) if r.paradigm is Paradigm.GRPC)
    assert not with_proxy.vetoed
    assert with_proxy.score == -2


@pytest.mark.unit
def test_a_veto_outranks_any_score_and_ties_break_by_declared_order() -> None:
    boost = Rule("boost", lambda r: True, {Paradigm.REST: 100}, frozenset({Paradigm.REST}), "x")
    quiet = Requirements(Boundary.INTERNAL_SERVICE, Topology.SINGLE_TEAM)
    boosted = rank(quiet, (*RULES, boost))
    assert boosted[-1].paradigm is Paradigm.REST
    assert boosted[-1].score == 101
    order = [r.paradigm for r in rank(quiet, ())]
    assert order == [Paradigm.REST, Paradigm.GRPC, Paradigm.GRAPHQL, Paradigm.JSON_RPC,
                     Paradigm.EVENTS]  # fmt: skip


@pytest.mark.unit
def test_reasons_name_the_rules_that_fired() -> None:
    top = rank(MOBILE_AND_WEB)[0]
    assert "client_shaped_reads: +3" in top.reasons
    assert "frontend_backend_split: +2" in top.reasons


@pytest.mark.unit
def test_every_rule_changes_the_outcome_of_some_catalog_scenario() -> None:
    def snapshot(req: Requirements, rules: tuple[Rule, ...]) -> list[tuple[str, int, bool]]:
        return [(r.paradigm.value, r.score, r.vetoed) for r in rank(req, rules)]

    for rule in RULES:
        without = tuple(other for other in RULES if other is not rule)
        assert any(snapshot(req, RULES) != snapshot(req, without) for req in CATALOG), rule.name
    assert len({rule.name for rule in RULES}) == len(RULES)
