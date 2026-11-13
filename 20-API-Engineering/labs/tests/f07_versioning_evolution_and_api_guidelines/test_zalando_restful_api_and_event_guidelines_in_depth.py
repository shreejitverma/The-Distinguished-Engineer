"""Unit tests for the Zalando guideline rules on JSON names, Money, Problem JSON, and events."""

import itertools
import json
import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http import HTTPStatus

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel
from starlette.types import Message, Receive, Scope, Send

from api_labs.f07_versioning_evolution_and_api_guidelines.zalando_guidelines import (
    FLOW_ID_MAX_LENGTH,
    MAX_BODY_BYTES,
    FlowIdMiddleware,
    Json,
    Money,
    OrderReplica,
    Problem,
    create_app,
    data_change_event,
    dump_json,
    load_i_json,
    non_snake_case_properties,
    normalize_flow_id,
    read_problem,
    utc_timestamp,
)

PRECISE_MONEY = '{"amount": 1024.42250000000000000001, "currency": "EUR"}'
BERLIN_WINTER = timezone(timedelta(hours=1))
FIXED_NOW = datetime(2026, 11, 11, 10, 30, tzinfo=BERLIN_WINTER)


class Harness:
    """A sales order app with a counting Flow-ID minter and an in-memory event sink."""

    def __init__(
        self, *, broker_error: Exception | None = None, max_body_size: int | None = None
    ) -> None:
        self.minted = 0
        self.events: list[dict[str, Json]] = []
        self.broker_error = broker_error
        self.app = create_app(
            mint_flow_id=self.mint,
            now=lambda: FIXED_NOW,
            publish=self.publish,
            max_body_size=max_body_size,
        )

    def mint(self) -> str:
        self.minted += 1
        return f"minted-{self.minted}"

    def publish(self, event: dict[str, Json]) -> None:
        if self.broker_error is not None:
            raise self.broker_error
        self.events.append(event)

    def client(self, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=raise_app_exceptions)
        return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.unit
def test_property_names_must_fully_match_the_rule_118_snake_case_pattern() -> None:
    document: Json = {
        "customer_number": "c-1",
        "billingAddress": {"zip": "10115", "countryCode": "DE"},
        "items": [{"sku": "s-1"}, {"SKU": "s-2"}],
        "_links": {},
        "9lives": True,
    }

    assert non_snake_case_properties(document) == [
        "/billingAddress",
        "/billingAddress/countryCode",
        "/items/1/SKU",
        "/9lives",
    ]
    # The guideline regex with re.match accepts a trailing newline; fullmatch does not.
    assert re.match(r"^[a-z_][a-z_0-9]*$", "customer_number\n") is not None
    assert non_snake_case_properties({"customer_number\n": 1}) == ["/customer_number\n"]
    # RFC 6901 escaping keeps a bad name apart from a valid nested member with the same path.
    assert non_snake_case_properties({"a/b": 1, "a": {"b": 1}, "x~y": 2}) == ["/a~1b", "/x~0y"]


@pytest.mark.unit
def test_money_amount_survives_as_an_exact_json_number_where_binary64_rounds_it() -> None:
    money = Money.from_json(load_i_json(PRECISE_MONEY))

    assert money == Money(Decimal("1024.42250000000000000001"), "EUR")
    assert json.loads(PRECISE_MONEY)["amount"] == 1024.4225
    assert dump_json(money.to_json()) == '{"amount":1024.42250000000000000001,"currency":"EUR"}'
    assert dump_json({"amount": Decimal("1E+3")}) == '{"amount":1000}'


@pytest.mark.unit
@pytest.mark.parametrize("amount", ["42.20", "42.2", "42.0", "42", "0.23", "1024.4225"])
def test_money_accepts_the_number_spellings_listed_in_rule_173(amount: str) -> None:
    money = Money.from_json(load_i_json(f'{{"amount": {amount}, "currency": "EUR"}}'))

    assert money.amount == Decimal(amount)
    assert Money(Decimal("42.20"), "EUR") == Money(Decimal("42.2"), "EUR")
    # The bound is inclusive: 64 printed digits pass on either side of the decimal point.
    assert Money(Decimal("0." + "1" * 64), "EUR").amount.as_tuple().exponent == -64
    assert format(Money(Decimal("1E+63"), "EUR").amount, "f") == "1" + "0" * 63
    with pytest.raises(ValueError, match="at most 64 digits"):
        Money(Decimal("1E+64"), "EUR")
    with pytest.raises(ValueError, match="amount must be finite"):
        Money(Decimal("Infinity"), "EUR")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('{"amount": "19.99", "currency": "EUR"}', "not a string or boolean"),
        ('{"amount": true, "currency": "EUR"}', "not a string or boolean"),
        ('{"amount": 19.99, "currency": "EUR", "discounted_amount": 9.99}', "exactly the members"),
        ('{"amount": 19.99}', "exactly the members"),
        ('{"amount": 19.99, "currency": "eur"}', "ISO 4217"),
        ('{"amount": 19.99, "currency": "EURO"}', "ISO 4217"),
        ('{"amount": 19.99, "currency": "EUR\\n"}', "ISO 4217"),
        ('{"amount": 19.99, "currency": 978}', "JSON string"),
        ('{"amount": NaN, "currency": "EUR"}', "not a JSON number"),
        ('{"amount": 1E+999999999, "currency": "EUR"}', "at most 64 digits"),
        ('{"amount": 1E+9999999999999999999999, "currency": "EUR"}', "exponent is out of range"),
        ('{"amount": 1E-1500000000000000000, "currency": "EUR"}', "exponent is out of range"),
        (f'{{"amount": 0.{"1" * 65}, "currency": "EUR"}}', "at most 64 digits"),
        ('{"amount": 1, "amount": 2, "currency": "EUR"}', "duplicate member name"),
    ],
    ids=[
        "string-amount",
        "boolean-amount",
        "inherited-member",
        "missing-currency",
        "lower-case-currency",
        "four-letter-currency",
        "trailing-newline-currency",
        "numeric-currency",
        "nan-amount",
        "gigabyte-exponent",
        "unrepresentable-exponent",
        "exponent-libmpdec-accepts",
        "sixty-five-digit-fraction",
        "duplicate-amount",
    ],
)
def test_money_rejects_documents_outside_the_closed_money_object(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Money.from_json(load_i_json(text))


@pytest.mark.unit
def test_duplicate_member_names_fail_instead_of_last_one_wins() -> None:
    text = '{"currency": "EUR", "currency": "USD"}'

    assert json.loads(text) == {"currency": "USD"}
    with pytest.raises(ValueError, match=r"RFC 7493 Section 2\.3"):
        load_i_json(text)
    with pytest.raises(ValueError, match="RFC 8259 Section 6"):
        dump_json({"amount": Decimal("Infinity")})


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        '"\\ud800"',
        '{"\\udfff": 1}',
        '[["\\ufdd0"]]',
        '{"a": {"b": "\\ufdef"}}',
        '{"a": "\\ufffe"}',
        '{"a": "\\ud83f\\udfff"}',
        '{"a": "\\udbff\\udffe"}',
        '"' + chr(0xFFFF) + '"',
    ],
    ids=[
        "lone-high-surrogate",
        "lone-low-surrogate-in-a-name",
        "noncharacter-in-a-nested-array",
        "last-noncharacter-of-the-block",
        "bmp-plane-end",
        "plane-1-end-as-escaped-pair",
        "plane-16-end-as-escaped-pair",
        "unescaped-noncharacter",
    ],
)
def test_i_json_strings_reject_surrogates_and_noncharacters_that_plain_json_accepts(
    text: str,
) -> None:
    json.loads(text)

    with pytest.raises(ValueError, match=r"RFC 7493 Section 2\.1"):
        load_i_json(text)
    # A well-formed escaped pair is one code point, and the neighbours of each range are legal.
    assert load_i_json('"\\ud83d\\ude00"') == chr(0x1F600)
    neighbours = "\\ud7ff\\ue000\\ufdcf\\ufdf0\\ufffd\\ud83f\\udffd"
    assert load_i_json(f'{{"a": ["{neighbours}"]}}') == {
        "a": ["".join(map(chr, (0xD7FF, 0xE000, 0xFDCF, 0xFDF0, 0xFFFD, 0x1FFFD)))]
    }


@pytest.mark.unit
def test_hostile_nesting_is_a_value_error_for_the_parser_and_harmless_for_the_walker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Build dependent: some CPython builds raise RecursionError here, others JSONDecodeError.
    with pytest.raises(ValueError, match=r"nested too deeply|Expecting value"):
        load_i_json("[" * 100_000)

    def overflow(*_args: object, **_kwargs: object) -> object:
        raise RecursionError

    with monkeypatch.context() as patched:
        patched.setattr(json, "loads", overflow)
        with pytest.raises(ValueError, match="nested too deeply"):
            load_i_json("[]")

    deep: Json = {"badName": 1}
    for _ in range(5_000):
        deep = {"ok": [deep]}

    assert non_snake_case_properties(deep) == ["/ok/0" * 5_000 + "/badName"]
    for body in (b"[" * 100_000, b'{"status": 1E+9999999999999999999999}'):
        assert read_problem(500, "application/problem+json", body) == Problem(
            500, "Internal Server Error"
        )


@pytest.mark.unit
def test_pydantic_default_decimal_field_rounds_json_numbers_and_emits_a_string() -> None:
    class NaiveMoney(BaseModel):
        amount: Decimal
        currency: str

    naive = NaiveMoney.model_validate_json(PRECISE_MONEY)

    assert naive.amount == Decimal("1024.4225")
    assert naive.model_dump_json() == '{"amount":"1024.4225","currency":"EUR"}'
    assert NaiveMoney.model_json_schema(mode="serialization")["properties"]["amount"] == {
        "title": "Amount",
        "type": "string",
    }
    # The same model is exact when pydantic never sees JSON text: parse and serialize outside it.
    exact = NaiveMoney.model_validate(load_i_json(PRECISE_MONEY))
    assert exact.amount == Decimal("1024.42250000000000000001")
    assert dump_json(exact.model_dump()) == '{"amount":1024.42250000000000000001,"currency":"EUR"}'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_default_is_422_application_json_where_rule_150_wants_a_400() -> None:
    class SalesOrder(BaseModel):
        grand_total: int

    app = FastAPI()

    @app.put("/sales-orders/{order_id}")
    async def put_sales_order(order_id: str, order: SalesOrder) -> SalesOrder:
        return order

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        invalid = await client.put(
            "/sales-orders/o-1", json={"grandTotal": 1}, headers={"X-Flow-ID": "flow-422"}
        )
        missing = await client.get("/nowhere")

    assert invalid.status_code == 422
    assert invalid.headers["content-type"] == "application/json"
    assert "x-flow-id" not in invalid.headers
    assert [(error["type"], error["loc"]) for error in invalid.json()["detail"]] == [
        ("missing", ["body", "grand_total"])
    ]
    assert missing.status_code == 404
    assert missing.headers["content-type"] == "application/json"
    assert missing.json() == {"detail": "Not Found"}


@pytest.mark.unit
def test_rule_233_character_set_is_not_a_valid_regex_until_the_dash_is_escaped() -> None:
    with pytest.raises(re.error, match="bad character range _-="):
        re.compile("[a-zA-Z0-9/+_-=]")

    def unused_mint() -> str:
        raise AssertionError("a valid Flow-ID must not be replaced")

    for flow_id in ("GKY7oDhpSiKY_gAAAABZ_A", "105a76d8-db49-4144-ace7-e683e8f4ba46", "a/b+c=="):
        assert normalize_flow_id(flow_id, unused_mint) == flow_id


@pytest.mark.unit
def test_flow_id_is_stripped_of_forbidden_characters_capped_and_minted_when_empty() -> None:
    minted = itertools.count(1)

    def mint() -> str:
        return f"minted-{next(minted)}"

    assert normalize_flow_id("abc\r\nX-Admin: 1", mint) == "abcX-Admin1"
    assert normalize_flow_id("f" * 200, mint) == "f" * FLOW_ID_MAX_LENGTH
    # Filter first, then cap: stripped characters must not eat into the 128 allowed ones.
    assert normalize_flow_id("\r\n" + "f" * 200, mint) == "f" * FLOW_ID_MAX_LENGTH
    assert normalize_flow_id(None, mint) == "minted-1"
    assert normalize_flow_id(" \r\n\t", mint) == "minted-2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flow_id_middleware_hands_non_http_scopes_through_untouched() -> None:
    seen: list[Scope] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    def unused_mint() -> str:
        raise AssertionError("a lifespan scope has no headers and needs no Flow-ID")

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        raise AssertionError(f"unexpected message {message}")

    await FlowIdMiddleware(inner, unused_mint)({"type": "lifespan"}, receive, send)

    assert seen == [{"type": "lifespan"}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_put_sales_order_echoes_flow_id_and_stamps_it_on_the_data_change_event() -> None:
    harness = Harness()

    async with harness.client() as client:
        created = await client.put(
            "/sales-orders/o-1",
            content='{"grand_total": {"amount": 19.990, "currency": "EUR"}}',
            headers={"X-Flow-ID": "GKY7oDhpSiKY_gAAAABZ_A"},
        )
        updated = await client.put(
            "/sales-orders/o-1",
            content='{"grand_total": {"amount": 21, "currency": "EUR"}}',
            headers={"X-Flow-ID": "second-flow"},
        )

    assert created.status_code == 201
    assert created.headers["content-type"] == "application/json"
    assert created.headers.get_list("x-flow-id") == ["GKY7oDhpSiKY_gAAAABZ_A"]
    expected_body = '{"grand_total":{"amount":19.990,"currency":"EUR"},"id":"o-1","version":1}'
    assert created.text == expected_body
    assert updated.status_code == 200
    assert harness.minted == 0
    assert harness.events == [
        {
            "metadata": {
                "eid": "b9507f3f-0835-5cd4-aaaa-ebeaa15a4259",
                "event_type": "sales-order.order-changed",
                "occurred_at": "2026-11-11T09:30:00.000Z",
                "flow_id": "GKY7oDhpSiKY_gAAAABZ_A",
            },
            "data_type": "sales_order.order",
            "data_op": "C",
            "data": {
                "id": "o-1",
                "grand_total": {"amount": Decimal("19.990"), "currency": "EUR"},
                "version": 1,
            },
        },
        {
            "metadata": {
                "eid": "5d82e14a-15a7-5642-a39b-c66b941884e6",
                "event_type": "sales-order.order-changed",
                "occurred_at": "2026-11-11T09:30:00.000Z",
                "flow_id": "second-flow",
            },
            "data_type": "sales_order.order",
            "data_op": "U",
            "data": {
                "id": "o-1",
                "grand_total": {"amount": Decimal(21), "currency": "EUR"},
                "version": 2,
            },
        },
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_flow_id_is_minted_once_and_shared_by_response_and_event() -> None:
    harness = Harness()

    async with harness.client() as client:
        response = await client.put(
            "/sales-orders/o-7",
            content='{"grand_total": {"amount": 5, "currency": "CHF"}}',
        )

    assert response.status_code == 201
    assert harness.minted == 1
    assert response.headers.get_list("x-flow-id") == ["minted-1"]
    metadata = harness.events[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["flow_id"] == "minted-1"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "detail"),
    [
        (
            '{"grandTotal": {"amount": 19.99, "currency": "EUR"}}',
            "property names must be snake_case: /grandTotal",
        ),
        (
            '{"grand_total": {"amount": "19.99", "currency": "EUR"}}',
            "amount must be a JSON number, not a string or boolean",
        ),
        (
            '{"grand_total": {"amount": 1, "currency": "EUR"}, "note": "x"}',
            "expected exactly one member: grand_total",
        ),
        (
            '{"grand_total": {"amount": 1E+9999999999999999999999, "currency": "EUR"}}',
            "number exponent is out of range",
        ),
        (
            '{"aA": 1, "bB": 2, "cC": 3, "dD": 4, "eE": 5, "fF": 6, "gG": 7}',
            "property names must be snake_case: /aA, /bB, /cC, /dD, /eE and 2 more",
        ),
        (
            b"\xff",
            "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
        ),
        (
            b'\xef\xbb\xbf{"grand_total": {"amount": 1, "currency": "EUR"}}',
            "Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1 (char 0)",
        ),
        (
            '{"grand_total": {"amount": 1, "currency": "\\ud800"}}',
            "surrogate or noncharacter in a string (RFC 7493 Section 2.1)",
        ),
    ],
    ids=[
        "camel-case",
        "string-amount",
        "unknown-member",
        "huge-exponent",
        "capped-name-list",
        "invalid-utf-8",
        "byte-order-mark",
        "lone-surrogate",
    ],
)
async def test_invalid_request_returns_problem_json_with_relative_type_and_no_event(
    content: str | bytes, detail: str
) -> None:
    harness = Harness()

    async with harness.client() as client:
        response = await client.put(
            "/sales-orders/o-1", content=content, headers={"X-Flow-ID": "flow-400"}
        )

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers.get_list("x-flow-id") == ["flow-400"]
    assert response.json() == {
        "type": "/problems/invalid-sales-order",
        "title": "Invalid sales order",
        "status": 400,
        "detail": detail,
    }
    assert harness.events == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unhandled_error_and_router_errors_become_problem_json_without_internals() -> None:
    harness = Harness(broker_error=RuntimeError("broker auth failed: password=hunter2"))

    async with harness.client(raise_app_exceptions=False) as client:
        failed = await client.put(
            "/sales-orders/o-1",
            content='{"grand_total": {"amount": 1, "currency": "EUR"}}',
            headers={"X-Flow-ID": "flow-500"},
        )
        missing = await client.get("/nowhere", headers={"X-Flow-ID": "flow-404"})
        wrong_method = await client.delete("/sales-orders/o-1")

    assert failed.status_code == 500
    assert failed.headers["content-type"] == "application/problem+json"
    assert failed.headers.get_list("x-flow-id") == ["flow-500"]
    assert failed.json() == {"type": "about:blank", "title": "Internal Server Error", "status": 500}
    assert "hunter2" not in failed.text
    assert "Traceback" not in failed.text
    assert missing.status_code == 404
    assert missing.headers["content-type"] == "application/problem+json"
    assert missing.json() == {"type": "about:blank", "title": "Not Found", "status": 404}
    assert wrong_method.status_code == 405
    assert wrong_method.headers["allow"] == "PUT"
    assert wrong_method.json()["title"] == "Method Not Allowed"

    # The failed publish must not consume a version: the retry is still the creation.
    harness.broker_error = None
    async with harness.client() as client:
        retried = await client.put(
            "/sales-orders/o-1",
            content='{"grand_total": {"amount": 1, "currency": "EUR"}}',
        )
    assert retried.status_code == 201
    assert harness.events[0]["data_op"] == "C"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_body_is_capped_at_the_documented_size_and_deep_nesting_is_a_400() -> None:
    harness = Harness()
    valid = '{"grand_total": {"amount": 1, "currency": "EUR"}}'

    async with harness.client() as client:
        at_limit = await client.put(
            "/sales-orders/o-1", content=valid + " " * (MAX_BODY_BYTES - len(valid))
        )
        over_limit = await client.put(
            "/sales-orders/o-2",
            content=valid + " " * (MAX_BODY_BYTES - len(valid) + 1),
            headers={"X-Flow-ID": "flow-413"},
        )
        nested = await client.put("/sales-orders/o-3", content="[" * MAX_BODY_BYTES)

    assert at_limit.status_code == 201
    assert over_limit.status_code == 413
    assert over_limit.headers["content-type"] == "application/problem+json"
    assert over_limit.headers.get_list("x-flow-id") == ["flow-413"]
    assert over_limit.json() == {"type": "about:blank", "title": "Content Too Large", "status": 413}
    assert nested.status_code == 400
    assert nested.json()["type"] == "/problems/invalid-sales-order"
    assert len(harness.events) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_max_body_size_is_plain_text_once_content_length_is_declared() -> None:
    harness = Harness(max_body_size=64)
    valid = '{"grand_total": {"amount": 1, "currency": "EUR"}}'

    async def two_chunks() -> AsyncIterator[bytes]:
        yield b" " * 40
        yield b" " * 40

    async with harness.client() as client:
        declared = await client.put(
            "/sales-orders/o-1", content=b" " * 65, headers={"X-Flow-ID": "flow-declared"}
        )
        streamed = await client.put(
            "/sales-orders/o-1", content=two_chunks(), headers={"X-Flow-ID": "flow-streamed"}
        )
        within = await client.put("/sales-orders/o-1", content=valid)

    # Declared length: the limit middleware sits outside the app and replaces its response.
    assert declared.request.headers["content-length"] == "65"
    assert declared.status_code == 413
    assert declared.headers["content-type"] == "text/plain; charset=utf-8"
    assert declared.text == "Content Too Large"
    assert "x-flow-id" not in declared.headers
    # No declared length: the limit surfaces as an HTTPException inside the handler's read.
    assert "content-length" not in streamed.request.headers
    assert streamed.status_code == 413
    assert streamed.headers["content-type"] == "application/problem+json"
    assert streamed.headers.get_list("x-flow-id") == ["flow-streamed"]
    assert streamed.json() == {
        "type": "about:blank",
        "title": HTTPStatus(413).phrase,
        "status": 413,
    }
    assert within.status_code == 201
    assert len(harness.events) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repeating_a_put_is_idempotent_and_publishes_no_second_event() -> None:
    harness = Harness()

    async def put(amount: str, currency: str = "EUR") -> httpx.Response:
        content = f'{{"grand_total": {{"amount": {amount}, "currency": "{currency}"}}}}'
        async with harness.client() as client:
            return await client.put("/sales-orders/o-1", content=content)

    created = await put("20.00")
    replayed = await put("20.00")
    respelled = await put("20.0")
    changed = await put("20.01")
    other_currency = await put("20.01", "CHF")

    assert [response.status_code for response in (created, replayed, respelled)] == [201, 200, 200]
    stored = '{"grand_total":{"amount":20.00,"currency":"EUR"},"id":"o-1","version":1}'
    assert created.text == replayed.text == respelled.text == stored
    assert changed.status_code == 200
    assert (
        changed.text == '{"grand_total":{"amount":20.01,"currency":"EUR"},"id":"o-1","version":2}'
    )
    assert other_currency.json()["version"] == 3
    assert [event["data_op"] for event in harness.events] == ["C", "U", "U"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resource_identifier_must_be_url_friendly_before_it_reaches_an_event() -> None:
    harness = Harness()
    valid = '{"grand_total": {"amount": 1, "currency": "EUR"}}'

    async with harness.client() as client:
        compound = await client.put("/sales-orders/de:1681e6b88ec1", content=valid)
        spaced = await client.put("/sales-orders/o%201", content=valid)
        newline = await client.put("/sales-orders/o-1%0A", content=valid)

    assert compound.status_code == 201
    assert compound.json()["id"] == "de:1681e6b88ec1"
    for rejected in (spaced, newline):
        assert rejected.status_code == 400
        assert rejected.json() == {
            "type": "/problems/invalid-sales-order",
            "title": "Invalid sales order",
            "status": 400,
            "detail": "resource identifier must match [a-zA-Z0-9:._-]+ (rule 228)",
        }
    assert len(harness.events) == 1


@pytest.mark.unit
def test_read_problem_ignores_mistyped_members_and_survives_non_problem_bodies() -> None:
    mistyped = b'{"type": 7, "title": ["x"], "status": "200", "detail": "d", "balance": 30}'
    relative = b'{"type": "/problems/out-of-stock", "title": "Out of stock", "status": 409}'

    assert read_problem(409, "application/problem+json", mistyped) == Problem(
        status=409, title="Conflict", type="about:blank", detail="d"
    )
    assert read_problem(409, "Application/Problem+JSON; charset=utf-8", relative) == Problem(
        status=409, title="Out of stock", type="/problems/out-of-stock"
    )
    # An intermediary rewrote the status line: the status line wins over the member.
    assert read_problem(502, "application/problem+json", relative).status == 502
    assert read_problem(502, "text/html", b"<html>Bad Gateway</html>") == Problem(
        502, "Bad Gateway"
    )
    assert read_problem(503, None, b"") == Problem(503, "Service Unavailable")
    assert read_problem(500, "application/problem+json", b"[1, 2") == Problem(
        500, "Internal Server Error"
    )
    not_a_problem = b'{"type": "/problems/teapot", "title": "Teapot"}'
    assert read_problem(599, "application/json", not_a_problem) == Problem(599, "Unknown Status")
    # Valid JSON that is not an object is not a problem document either.
    assert read_problem(500, "application/problem+json", b'["type", "title"]') == Problem(
        500, "Internal Server Error"
    )
    # instance survives the round trip, and an absent detail is not serialized as null.
    located = Problem(409, "Out of stock", "/problems/out-of-stock", instance="/sales-orders/o-9")
    wire = dump_json(located.to_json())
    assert wire == (
        '{"instance":"/sales-orders/o-9","status":409,'
        '"title":"Out of stock","type":"/problems/out-of-stock"}'
    )
    assert read_problem(409, "application/problem+json", wire.encode()) == located


@pytest.mark.unit
def test_publish_retry_reuses_eid_and_consumer_drops_duplicates_and_stale_versions() -> None:
    def order_changed(
        version: int,
        occurred_at: datetime,
        amount: str,
        event_type: str = "sales-order.order-changed",
    ) -> dict[str, Json]:
        return data_change_event(
            event_type=event_type,
            data_type="sales_order.order",
            data_op="U",
            data={
                "id": "o-1",
                "grand_total": Money(Decimal(amount), "EUR").to_json(),
                "version": version,
            },
            occurred_at=occurred_at,
            flow_id="flow-1",
        )

    first_attempt = order_changed(2, FIXED_NOW, "20.00")
    retry = order_changed(2, FIXED_NOW + timedelta(seconds=30), "20.00")
    newer = order_changed(3, FIXED_NOW, "25.00")
    same_data_new_version = order_changed(4, FIXED_NOW, "25.00")
    respelled = order_changed(2, FIXED_NOW, "20.0")

    first_metadata, retry_metadata = first_attempt["metadata"], retry["metadata"]
    assert isinstance(first_metadata, dict)
    assert isinstance(retry_metadata, dict)
    assert first_metadata["eid"] == retry_metadata["eid"]
    assert first_metadata["occurred_at"] != retry_metadata["occurred_at"]
    # The eid hashes serialized bytes: an equal amount spelled differently is a different name.
    assert respelled["data"] == first_attempt["data"]
    assert respelled["metadata"] != first_attempt["metadata"]
    eids = {
        dump_json(event["metadata"]).split('"eid":"')[1][:36]
        for event in (first_attempt, newer, same_data_new_version)
    }
    assert len(eids) == 3

    replica = OrderReplica()
    assert replica.apply(newer) == "applied"
    assert replica.apply(first_attempt) == "stale"
    assert replica.apply(retry) == "duplicate"
    assert replica.apply(newer) == "duplicate"
    assert replica.apply(same_data_new_version) == "applied"
    # An equal version under a new eid is not newer, so it must not overwrite the replica.
    assert replica.apply(order_changed(4, FIXED_NOW, "26.00")) == "stale"
    assert replica.versions == {"o-1": 4}
    assert replica.orders["o-1"]["grand_total"] == {"amount": Decimal("25.00"), "currency": "EUR"}
    with pytest.raises(ValueError, match="not a data change event"):
        replica.apply({"metadata": {"eid": "x"}})
    # Mistyped members are refused before any state changes, so the eid is not burned.
    with pytest.raises(ValueError, match="event lacks"):
        replica.apply({"metadata": {"eid": "x"}, "data": {"id": "o-1", "version": "5"}})
    assert "x" not in replica.seen_eids
    assert replica.versions == {"o-1": 4}
    # A naive datetime would be read as local time and shift occurred_at silently.
    with pytest.raises(ValueError, match="aware datetime"):
        utc_timestamp(FIXED_NOW.replace(tzinfo=None))
    # Rule 213: the registered name, optionally with a major version, and nothing after it.
    versioned = order_changed(2, FIXED_NOW, "20.00", "transactions-order.order-cancelled.v2")
    assert versioned["metadata"] != first_attempt["metadata"]
    for bad_name in ("OrderChanged", "order-changed", "sales-order.order-changed\n"):
        with pytest.raises(ValueError, match="rule 213"):
            order_changed(2, FIXED_NOW, "20.00", bad_name)
