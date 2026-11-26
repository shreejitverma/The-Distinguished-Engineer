"""JSON-RPC 2.0: the specification's own examples, then every edge the dispatcher must hold."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from api_labs.f20_graphql_json_rpc_and_event_apis.json_rpc_dispatcher import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    RESERVED_MIN,
    SERVER_ERROR_MAX,
    SERVER_ERROR_MIN,
    STANDARD_MESSAGES,
    BatchRejectedError,
    Dispatcher,
    Limits,
    RpcError,
    build_app,
    correlate,
)

NOTIFICATIONS: list[tuple[str, Any]] = []


def make_dispatcher(limits: Limits | None = None) -> Dispatcher:
    """The methods the specification's Section 7 examples call, plus a notification recorder."""
    d = Dispatcher(limits)

    def subtract(minuend: float, subtrahend: float) -> float:
        return minuend - subtrahend

    def total(*numbers: float) -> float:
        return sum(numbers)

    def record(name: str) -> Any:
        def handler(*args: Any) -> None:
            NOTIFICATIONS.append((name, args))

        return handler

    def get_data() -> list[Any]:
        return ["hello", 5]

    d.register("subtract", subtract)
    d.register("sum", total)
    d.register("get_data", get_data)
    for name in ("update", "notify_hello", "notify_sum"):
        d.register(name, record(name))
    return d


async def call(d: Dispatcher, payload: Any) -> Any:
    """Send a Python value as JSON; return the decoded reply, or None when nothing is sent."""
    reply = await d.handle(json.dumps(payload))
    return None if reply is None else json.loads(reply)


def err(code: int, request_id: Any = None) -> dict[str, Any]:
    error = {"code": code, "message": STANDARD_MESSAGES[code]}
    return {"jsonrpc": "2.0", "error": error, "id": request_id}


def ok(result: Any, request_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "result": result, "id": request_id}


def rpc(method: str, params: Any = None, **ident: Any) -> dict[str, Any]:
    request: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        request["params"] = params
    return request | ident


SPEC_BATCH = [
    rpc("sum", [1, 2, 4], id="1"),
    rpc("notify_hello", [7]),
    rpc("subtract", [42, 23], id="2"),
    {"foo": "boo"},
    rpc("foo.get", {"name": "myself"}, id="5"),
    rpc("get_data", id="9"),
]

SPEC_EXAMPLES: list[tuple[str, Any, Any]] = [
    ("positional", rpc("subtract", [42, 23], id=1), ok(19, 1)),
    ("positional-swapped", rpc("subtract", [23, 42], id=2), ok(-19, 2)),
    ("named", rpc("subtract", {"subtrahend": 23, "minuend": 42}, id=3), ok(19, 3)),
    ("named-reordered", rpc("subtract", {"minuend": 42, "subtrahend": 23}, id=4), ok(19, 4)),
    ("notification", rpc("update", [1, 2, 3, 4, 5]), None),
    ("notification-unknown-method", rpc("foobar"), None),
    ("method-not-found", rpc("foobar", id="1"), err(METHOD_NOT_FOUND, "1")),
    ("invalid-request", {"jsonrpc": "2.0", "method": 1, "params": "bar"}, err(INVALID_REQUEST)),
    ("empty-batch", [], err(INVALID_REQUEST)),
    ("batch-of-one-invalid", [1], [err(INVALID_REQUEST)]),
    ("batch-of-three-invalid", [1, 2, 3], [err(INVALID_REQUEST)] * 3),
    (
        "mixed-batch",
        SPEC_BATCH,
        [
            ok(7, "1"),
            ok(19, "2"),
            err(INVALID_REQUEST),
            err(METHOD_NOT_FOUND, "5"),
            ok(["hello", 5], "9"),
        ],
    ),
    ("all-notifications", [rpc("notify_sum", [1, 2, 4]), rpc("notify_hello", [7])], None),
]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "request_obj", "expected"), SPEC_EXAMPLES)
async def test_specification_section_7_examples(name: str, request_obj: Any, expected: Any) -> None:
    assert await call(make_dispatcher(), request_obj) == expected, name


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_json_examples_return_one_parse_error_with_null_id() -> None:
    d = make_dispatcher()
    single = '{"jsonrpc": "2.0", "method": "foobar, "params": "bar", "baz]'
    batch = '[{"jsonrpc": "2.0", "method": "sum", "params": [1,2,4], "id": "1"}, {"jsonrpc"'
    for text in (single, batch):
        reply = await d.handle(text)
        assert reply is not None
        assert json.loads(reply) == err(PARSE_ERROR)


@pytest.mark.unit
def test_standard_error_table_matches_section_5_1() -> None:
    assert STANDARD_MESSAGES == {
        -32700: "Parse error",
        -32600: "Invalid Request",
        -32601: "Method not found",
        -32602: "Invalid params",
        -32603: "Internal error",
    }
    assert all(RESERVED_MIN <= code <= SERVER_ERROR_MAX for code in STANDARD_MESSAGES)
    assert (SERVER_ERROR_MIN, SERVER_ERROR_MAX) == (-32099, -32000)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_success_carries_result_even_when_it_is_null_and_never_error() -> None:
    d = Dispatcher()
    d.register("nothing", lambda: None)
    reply = await call(d, rpc("nothing", id=1))
    assert reply == {"jsonrpc": "2.0", "result": None, "id": 1}
    assert "error" not in reply


@pytest.mark.unit
@pytest.mark.asyncio
async def test_error_response_never_carries_result() -> None:
    reply = await call(make_dispatcher(), rpc("nope", id=1))
    assert set(reply) == {"jsonrpc", "error", "id"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notification_runs_the_handler_but_sends_no_reply() -> None:
    NOTIFICATIONS.clear()
    assert await call(make_dispatcher(), rpc("update", [1, 2])) is None
    assert NOTIFICATIONS == [("update", (1, 2))]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failing_notification_stays_silent_for_every_error_kind() -> None:
    d = make_dispatcher()

    def boom() -> None:
        raise RuntimeError("secret")

    d.register("boom", boom)
    assert await call(d, rpc("boom")) is None
    assert await call(d, rpc("subtract", [1])) is None
    assert await call(d, rpc("missing")) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notification_shaped_garbage_still_gets_invalid_request() -> None:
    assert await call(make_dispatcher(), rpc("update", params=5)) == err(INVALID_REQUEST)
    assert await call(make_dispatcher(), {"jsonrpc": "2.0", "params": []}) == err(INVALID_REQUEST)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_null_id_is_a_request_that_is_answered_not_a_notification() -> None:
    reply = await call(make_dispatcher(), rpc("sum", [1, 2], id=None))
    assert reply == ok(3, None)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", [True, False, {"a": 1}, [1]])
async def test_id_must_be_string_number_or_null_and_booleans_are_not_numbers(bad_id: Any) -> None:
    assert await call(make_dispatcher(), rpc("sum", [1], id=bad_id)) == err(INVALID_REQUEST)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fractional_and_string_ids_are_echoed_unchanged() -> None:
    d = make_dispatcher()
    assert await call(d, rpc("sum", [1], id=1.5)) == ok(1, 1.5)
    assert await call(d, rpc("sum", [1], id="abc-7")) == ok(1, "abc-7")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_request_echoes_a_well_typed_id_and_nulls_an_ill_typed_one() -> None:
    d = make_dispatcher()
    old = {"jsonrpc": "1.0", "method": "sum", "id": 7}
    assert await call(d, old) == err(INVALID_REQUEST, 7)
    assert await call(d, old | {"id": [7]}) == err(INVALID_REQUEST)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_obj",
    [
        {"method": "sum", "id": 1},
        {"jsonrpc": 2.0, "method": "sum", "id": 1},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "method": None, "id": 1},
        {"jsonrpc": "2.0", "method": "sum", "params": None, "id": 1},
        {"jsonrpc": "2.0", "method": "sum", "params": "x", "id": 1},
    ],
)
async def test_envelope_violations_are_invalid_request(request_obj: Any) -> None:
    reply = await call(make_dispatcher(), request_obj)
    assert reply["error"]["code"] == INVALID_REQUEST


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_members_are_ignored() -> None:
    assert await call(make_dispatcher(), rpc("sum", [1], id=1, trace="x")) == ok(1, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_params_by_position_and_by_name_and_omitted() -> None:
    d = make_dispatcher()
    assert (await call(d, rpc("get_data", id=1)))["result"] == ["hello", 5]
    assert (await call(d, rpc("get_data", [], id=1)))["result"] == ["hello", 5]
    assert (await call(d, rpc("get_data", {}, id=1)))["result"] == ["hello", 5]
    assert (await call(d, rpc("subtract", [5, 3], id=1)))["result"] == 2
    assert (await call(d, rpc("subtract", {"minuend": 5, "subtrahend": 3}, id=1)))["result"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [[1], [1, 2, 3], {"minuend": 1}, {"minuend": 1, "subtrahend": 2, "extra": 3}, {"Minuend": 1}],
)
async def test_param_shape_mismatch_is_invalid_params(params: Any) -> None:
    reply = await call(make_dispatcher(), rpc("subtract", params, id=1))
    assert reply == err(INVALID_PARAMS, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_type_error_raised_inside_a_handler_is_internal_error_not_invalid_params() -> None:
    d = Dispatcher()

    def concat(value: Any) -> Any:
        return value + "x"

    d.register("concat", concat)
    assert await call(d, rpc("concat", [1], id=1)) == err(INTERNAL_ERROR, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_internal_error_hides_the_exception_text() -> None:
    d = Dispatcher()

    def leak() -> None:
        raise RuntimeError("password=hunter2")

    d.register("leak", leak)
    reply = await d.handle(json.dumps(rpc("leak", id=1)))
    assert reply is not None
    assert "hunter2" not in reply
    assert json.loads(reply) == err(INTERNAL_ERROR, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_application_errors_keep_code_message_and_data() -> None:
    d = Dispatcher()

    def deny() -> None:
        raise RpcError(-32001, "Quota exceeded", {"retry_after": 30})

    def bare() -> None:
        raise RpcError(1001, "Custom")

    def null_data() -> None:
        raise RpcError(1002, "Null data", None)

    for name, fn in (("deny", deny), ("bare", bare), ("null_data", null_data)):
        d.register(name, fn)
    assert (await call(d, rpc("deny", id=1)))["error"] == {
        "code": -32001,
        "message": "Quota exceeded",
        "data": {"retry_after": 30},
    }
    assert (await call(d, rpc("bare", id=1)))["error"] == {"code": 1001, "message": "Custom"}
    assert (await call(d, rpc("null_data", id=1)))["error"]["data"] is None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("code", [-32100, -32500, -32768, -32604])
async def test_handlers_cannot_mint_reserved_codes(code: int) -> None:
    d = Dispatcher()

    def forge() -> None:
        raise RpcError(code, "forged")

    d.register("forge", forge)
    reply = await call(d, rpc("forge", id=1))
    assert reply == err(INTERNAL_ERROR, 1)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("code", [-32000, -32099, -32602, -32769, -32800, -31999, 0, 7])
async def test_handlers_may_use_server_range_standard_and_free_codes(code: int) -> None:
    d = Dispatcher()

    def raise_it() -> None:
        raise RpcError(code, "mine")

    d.register("raise_it", raise_it)
    assert (await call(d, rpc("raise_it", id=1)))["error"]["code"] == code


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unserializable_data_in_an_error_degrades_to_internal_error() -> None:
    d = Dispatcher()

    def bad() -> None:
        raise RpcError(5, "x", {1, 2})

    d.register("bad", bad)
    assert await call(d, rpc("bad", id=1)) == err(INTERNAL_ERROR, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rpc_prefix_is_reserved_for_extensions() -> None:
    d = Dispatcher()
    with pytest.raises(ValueError, match="reserved"):
        d.register("rpc.discover", lambda: None)
    d.register("rpcx", lambda: 1)
    assert (await call(d, rpc("rpcx", id=1)))["result"] == 1
    assert await call(d, rpc("rpc.discover", id=2)) == err(METHOD_NOT_FOUND, 2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_and_async_handlers_both_work() -> None:
    d = Dispatcher()

    async def slow(x: int) -> int:
        await asyncio.sleep(0)
        return x * 2

    d.register("slow", slow)

    def fast(x: int) -> int:
        return x + 1

    d.register("fast", fast)
    assert (await call(d, rpc("slow", [4], id=1)))["result"] == 8
    assert (await call(d, rpc("fast", [4], id=1)))["result"] == 5


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"   ",
        b'{"jsonrpc":"2.0","method":"sum","id":1} trailing',
        b"\xff\xfe{",
        b'\xef\xbb\xbf{"jsonrpc":"2.0","method":"sum","params":[1],"id":1}',
        '{"jsonrpc":"2.0","method":"sum","params":[1],"id":1}'.encode("utf-16"),
        b'{"jsonrpc":"2.0","method":"sum","params":[NaN],"id":1}',
        b'{"jsonrpc":"2.0","method":"sum","params":[Infinity],"id":1}',
        b'{"jsonrpc":"2.0","method":"sum","params":[1e400],"id":1}',
    ],
    ids=[
        "empty",
        "blank",
        "trailing-garbage",
        "invalid-utf8",
        "utf8-bom",
        "utf16",
        "nan",
        "infinity",
        "overflowing-float",
    ],
)
async def test_undecodable_or_non_json_input_is_parse_error(raw: bytes) -> None:
    reply = await Dispatcher().handle(raw)
    assert reply is not None
    assert json.loads(reply) == err(PARSE_ERROR)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b"[" * 100_000,
        b"[" * 100_000 + b"]" * 100_000,
        b'{"a":' * 100_000,
    ],
    ids=["unclosed-deep-nesting", "balanced-deep-nesting", "deep-objects"],
)
async def test_nesting_beyond_max_depth_is_refused_without_calling_the_parser(raw: bytes) -> None:
    reply = await Dispatcher().handle(raw)
    assert reply is not None
    assert json.loads(reply)["error"] == {
        "code": INVALID_REQUEST,
        "message": "Invalid Request",
        "data": {"max_depth": 64},
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_depth_is_inclusive_and_ignores_brackets_inside_strings() -> None:
    d = make_dispatcher(Limits(max_depth=3))
    # depth 3: batch, request, params array -> allowed
    assert await call(d, [rpc("sum", [1, 2], id=1)]) == [ok(3, 1)]
    # depth 4: one more nested array in params -> refused
    refused = await call(d, [rpc("sum", [[1]], id=1)])
    assert refused["error"]["data"] == {"max_depth": 3}
    # brackets and escaped quotes inside a string literal do not count
    tricky_id = '[[[[" [[[['
    assert await call(d, [rpc("sum", [1], id=tricky_id)]) == [ok(1, tricky_id)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unterminated_string_scan_is_linear_not_quadratic() -> None:
    reply = await Dispatcher(Limits(max_request_bytes=2_000_000)).handle(b'"' + b'\\"' * 400_000)
    assert reply is not None
    assert json.loads(reply) == err(PARSE_ERROR)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_members_are_rejected_by_default_and_last_wins_when_allowed() -> None:
    raw = '{"jsonrpc":"2.0","method":"sum","params":[1],"id":1,"id":2}'
    strict = make_dispatcher()
    reply = await strict.handle(raw)
    assert reply is not None
    assert json.loads(reply) == err(INVALID_REQUEST)
    lax = make_dispatcher(Limits(reject_duplicate_members=False))
    reply = await lax.handle(raw)
    assert reply is not None
    assert json.loads(reply) == ok(1, 2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oversized_message_is_rejected_before_parsing() -> None:
    d = make_dispatcher(Limits(max_request_bytes=64))
    big = b'{"jsonrpc":"2.0","method":"sum","params":[' + b"1," * 64 + b'1],"id":1}'
    reply = await d.handle(big)
    assert reply is not None
    assert json.loads(reply)["error"] == {
        "code": INVALID_REQUEST,
        "message": "Invalid Request",
        "data": {"max_request_bytes": 64},
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_size_limit_is_inclusive() -> None:
    d = make_dispatcher(Limits(max_batch_size=3))
    three = [rpc("sum", [i], id=i) for i in range(3)]
    assert len(await call(d, three)) == 3
    refused = await call(d, [*three, rpc("sum", [9], id=9)])
    assert refused["error"]["code"] == INVALID_REQUEST
    assert refused["error"]["data"] == {"max_batch_size": 3}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nested_batch_element_is_invalid_request_not_recursion() -> None:
    reply = await call(make_dispatcher(), [[rpc("sum", [1], id=1)], rpc("sum", [2], id=2)])
    assert reply == [err(INVALID_REQUEST), ok(2, 2)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_failing_item_does_not_poison_the_batch() -> None:
    d = make_dispatcher()

    def boom() -> None:
        raise RuntimeError("x")

    d.register("boom", boom)
    batch = [rpc("boom", id=1), rpc("sum", [1, 2], id=2), rpc("subtract", [1], id=3)]
    assert await call(d, batch) == [err(INTERNAL_ERROR, 1), ok(3, 2), err(INVALID_PARAMS, 3)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unserializable_and_non_finite_results_fail_only_their_own_item() -> None:
    d = make_dispatcher()
    d.register("a_set", lambda: {1, 2})
    d.register("not_a_number", lambda: float("nan"))
    d.register("cycle", lambda: _cycle())
    names = ["a_set", "not_a_number", "cycle"]
    batch = [rpc(name, id=i) for i, name in enumerate(names, 1)] + [rpc("sum", [1], id=4)]
    assert await call(d, batch) == [
        err(INTERNAL_ERROR, 1),
        err(INTERNAL_ERROR, 2),
        err(INTERNAL_ERROR, 3),
        ok(1, 4),
    ]


def _cycle() -> list[Any]:
    loop: list[Any] = []
    loop.append(loop)
    return loop


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notifications_in_a_batch_run_but_leave_no_array_slot() -> None:
    NOTIFICATIONS.clear()
    batch = [rpc("notify_hello", [1]), rpc("sum", [2], id="a"), rpc("notify_hello", [3])]
    assert await call(make_dispatcher(), batch) == [ok(2, "a")]
    assert NOTIFICATIONS == [("notify_hello", (1,)), ("notify_hello", (3,))]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_notification_batch_sends_nothing_rather_than_an_empty_array() -> None:
    d = make_dispatcher()
    assert await d.handle(json.dumps([rpc("update", [1]), rpc("update", [2])])) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_ids_in_a_batch_are_each_answered() -> None:
    reply = await call(make_dispatcher(), [rpc("sum", [1], id=1), rpc("sum", [2], id=1)])
    assert reply == [ok(1, 1), ok(2, 1)]


def rendezvous_dispatcher(limits: Limits) -> Dispatcher:
    """Two methods that can only both finish if they run at the same time."""
    d = Dispatcher(limits)
    a_ready, b_ready = asyncio.Event(), asyncio.Event()

    async def a() -> str:
        a_ready.set()
        await b_ready.wait()
        return "a"

    async def b() -> str:
        b_ready.set()
        await a_ready.wait()
        return "b"

    d.register("a", a)
    d.register("b", b)
    return d


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_items_run_concurrently() -> None:
    d = rendezvous_dispatcher(Limits(max_concurrency=2))
    reply = await asyncio.wait_for(call(d, [rpc("a", id=1), rpc("b", id=2)]), timeout=5)
    assert reply == [ok("a", 1), ok("b", 2)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrency_limit_of_one_serializes_and_so_the_rendezvous_deadlocks() -> None:
    d = rendezvous_dispatcher(Limits(max_concurrency=1))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(call(d, [rpc("a", id=1), rpc("b", id=2)]), timeout=0.2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replies_follow_request_order_even_when_completion_order_differs() -> None:
    d = Dispatcher()
    finished: list[str] = []

    async def delayed(name: str, delay: float) -> str:
        await asyncio.sleep(delay)
        finished.append(name)
        return name

    d.register("delayed", delayed)
    batch = [rpc("delayed", ["slow", 0.05], id=1), rpc("delayed", ["fast", 0], id=2)]
    assert await call(d, batch) == [ok("slow", 1), ok("fast", 2)]
    assert finished == ["fast", "slow"]


@pytest.mark.unit
def test_correlate_joins_by_id_in_any_order_and_reports_gaps() -> None:
    reply = json.dumps([ok("c", 3), err(INVALID_REQUEST), ok("a", 1)])
    result = correlate([1, 2, 3], reply)
    assert result.by_id == {1: ok("a", 1), 3: ok("c", 3)}
    assert result.missing == (2,)
    assert result.unmatched == (err(INVALID_REQUEST),)


@pytest.mark.unit
def test_correlate_raises_when_the_whole_batch_was_rejected_and_handles_silence() -> None:
    with pytest.raises(BatchRejectedError) as info:
        correlate([1], json.dumps(err(INVALID_REQUEST)))
    assert info.value.response == err(INVALID_REQUEST)
    assert correlate([1, 2], None).missing == (1, 2)


async def post(
    app_dispatcher: Dispatcher, content: bytes, content_type: str = "application/json"
) -> httpx.Response:
    transport = httpx.ASGITransport(app=build_app(app_dispatcher))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/rpc", content=content, headers={"Content-Type": content_type})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_call_is_200_json_and_notification_is_204_with_no_body() -> None:
    d = make_dispatcher()
    reply = await post(d, json.dumps(rpc("sum", [1, 2], id=1)).encode())
    assert (reply.status_code, reply.headers["content-type"]) == (200, "application/json")
    assert reply.json() == ok(3, 1)
    silent = await post(d, json.dumps(rpc("update", [1])).encode())
    assert (silent.status_code, silent.content) == (204, b"")
    all_silent = await post(d, json.dumps([rpc("update", [1])]).encode())
    assert (all_silent.status_code, all_silent.content) == (204, b"")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_status_does_not_carry_json_rpc_errors() -> None:
    d = make_dispatcher()
    for body in (b"{bad", json.dumps(rpc("nope", id=1)).encode(), b"[]"):
        reply = await post(d, body)
        assert reply.status_code == 200
        assert "error" in reply.json()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_charset_parameter_is_accepted_and_other_media_types_are_415() -> None:
    d = make_dispatcher()
    body = json.dumps(rpc("sum", [1], id=1)).encode()
    assert (await post(d, body, "application/json; charset=utf-8")).status_code == 200
    assert (await post(d, body, "text/plain")).status_code == 415


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_oversize_body_is_413_and_get_is_405_with_allow() -> None:
    d = make_dispatcher(Limits(max_request_bytes=32))
    assert (await post(d, b" " * 33)).status_code == 413
    transport = httpx.ASGITransport(app=build_app(d))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        reply = await client.get("/rpc")
    assert (reply.status_code, reply.headers["allow"]) == (405, "POST")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_batch_returns_an_array() -> None:
    reply = await post(make_dispatcher(), json.dumps(SPEC_BATCH).encode())
    assert reply.status_code == 200
    assert [r["id"] for r in reply.json()] == ["1", "2", None, "5", "9"]
