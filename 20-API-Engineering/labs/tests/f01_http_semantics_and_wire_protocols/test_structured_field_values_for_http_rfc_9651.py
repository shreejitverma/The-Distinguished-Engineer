"""Unit tests for strict RFC 9651 Structured Field Values parsing and serialization."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import h11
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from api_labs.f01_http_semantics_and_wire_protocols.structured_fields import (
    BareItem,
    Date,
    DisplayString,
    FieldType,
    InnerList,
    Item,
    Priority,
    ServiceLimit,
    StructuredFieldError,
    Token,
    combine_field_lines,
    deprecation_from_field,
    parse,
    parse_dictionary,
    parse_item,
    parse_list,
    priority_from_field_lines,
    ratelimit_from_field_lines,
    serialize_dictionary,
    serialize_item,
    serialize_list,
)

if TYPE_CHECKING:
    from starlette.requests import Request


def _reserialize(value: bytes, field_type: FieldType) -> str | None:
    parsed = parse(value, field_type)
    if isinstance(parsed, Item):
        return serialize_item(parsed)
    if isinstance(parsed, list):
        return serialize_list(parsed)
    return serialize_dictionary(parsed)


@pytest.mark.unit
def test_rfc9421_sf_reserialization_collapses_optional_whitespace() -> None:
    # RFC 9421 Section 2.1.1 example: the "sf" flag re-serializes strictly before signing.
    raw = b"a=1,    b=2;x=1;y=2,   c=(a   b   c)"
    assert _reserialize(raw, "dictionary") == "a=1, b=2;x=1;y=2, c=(a b c)"
    assert _reserialize(raw + b", d", "dictionary") == "a=1, b=2;x=1;y=2, c=(a b c), d"


# Hand-written cases that follow RFC 9651 Section 4.2 step by step; 10 of them appear verbatim
# in httpwg/structured-field-tests at commit 10781e4. None means parsing must fail.
CONFORMANCE: list[tuple[FieldType, bytes, str | None]] = [
    ("list", b"a,\tb", "a, b"),
    ("list", b"1\t,\t42", "1, 42"),
    ("list", b"\ta, b", None),
    ("list", b"1, 2,", None),
    ("list", b"(a  b );x, ()", "(a b);x, ()"),
    ("list", b"(a,b)", None),
    # Section 4.2.1.2 steps 3.1 and 3.5: only SP separates Inner List items, never HTAB.
    ("list", b"(1\t 42)", None),
    ("list", b"(\ta)", None),
    ("list", b"(a \tb)", None),
    ("item", b"  42  ", "42"),
    ("item", b"0002", "2"),
    ("item", b"-0", "0"),
    ("item", b"999999999999999", "999999999999999"),
    ("item", b"1000000000000000", None),
    ("item", b"123456789012.123", "123456789012.123"),
    ("item", b"1234567890123.0", None),
    ("item", b"1.5000", None),
    ("item", b"1.", None),
    ("item", b'"a\\"b"', '"a\\"b"'),
    ("item", b'"a\\qb"', None),
    ("item", b"'foo'", None),
    ("item", b"?2", None),
    ("item", b"@1.5", None),
    ("item", b'%"f%C3%BC"', None),
    ("item", b":aGVsbG8:", ":aGVsbG8=:"),
    ("item", b":aGVs bG8=:", None),
    ("item", b"foo123/456:x", "foo123/456:x"),
    ("item", b"a b", None),
    ("item", b"1;a=?1;b=?0", "1;a;b=?0"),
    ("item", b"1; a=2", "1;a=2"),
    ("item", b"1;A=2", None),
    ("dictionary", b"a=1, b", "a=1, b"),
    ("dictionary", b"A=1", None),
    ("dictionary", b"a=(1 2);q, b=?1;x", "a=(1 2);q, b;x"),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field_type", "raw", "canonical"),
    CONFORMANCE,
    ids=[f"{t}:{r.decode()}" for t, r, _ in CONFORMANCE],
)
def test_conformance_vectors_parse_strictly_and_reserialize_canonically(
    field_type: FieldType, raw: bytes, canonical: str | None
) -> None:
    if canonical is None:
        with pytest.raises(StructuredFieldError):
            parse(raw, field_type)
    else:
        assert _reserialize(raw, field_type) == canonical


@pytest.mark.unit
def test_python_bool_serializes_as_boolean_not_integer() -> None:
    # bool subclasses int: dispatching on int first would emit "1" for True.
    assert serialize_item(Item(True)) == "?1"
    assert serialize_item(Item(False)) == "?0"
    assert serialize_item(Item(1)) == "1"
    assert serialize_item(Item(1, {"x": True, "y": False})) == "1;x;y=?0"
    assert serialize_dictionary({"a": Item(True), "b": Item(False)}) == "a, b=?0"


@pytest.mark.unit
def test_token_and_string_stay_distinct_types() -> None:
    assert parse_item(b"foo").value == Token("foo")
    assert parse_item(b'"foo"').value == "foo"
    assert parse_item(b"foo").value != parse_item(b'"foo"').value
    assert serialize_item(Item("foo")) == '"foo"'
    assert serialize_item(Item(Token("foo"))) == "foo"


@pytest.mark.unit
def test_duplicate_keys_last_value_wins_in_first_position() -> None:
    parsed = parse_dictionary(b"a=1, b=2, a=3")
    assert [(k, m.value) for k, m in parsed.items() if isinstance(m, Item)] == [("a", 3), ("b", 2)]
    assert serialize_dictionary(parsed) == "a=3, b=2"
    item = parse_item(b"1;a=1;b=2;a=3")
    assert list(item.params.items()) == [("a", 3), ("b", 2)]


@pytest.mark.unit
def test_field_lines_are_combined_with_comma_space_before_parsing() -> None:
    assert priority_from_field_lines([b"u=0", b"i"]) == Priority(urgency=0, incremental=True)
    assert parse_list(b", ".join([b"sugar, tea", b"rum"])) == [
        Item(Token("sugar")),
        Item(Token("tea")),
        Item(Token("rum")),
    ]
    # RFC 9651 Section 4.2: a String split across lines silently gains ", ".
    assert parse_item(combine_field_lines([b'"foo', b'bar"'])).value == "foo, bar"
    with pytest.raises(StructuredFieldError):
        parse_item(combine_field_lines([b"abc", b"def"]))


def _priority_probe_app() -> Starlette:
    async def probe(request: Request) -> JSONResponse:
        raw = [v for k, v in request.scope["headers"] if k == b"priority"]
        combined = priority_from_field_lines(raw)
        first_only = priority_from_field_lines([request.headers["priority"].encode("ascii")])
        return JSONResponse(
            {
                "starlette_getitem": request.headers["priority"],
                "starlette_getlist": request.headers.getlist("priority"),
                "first_line_urgency": first_only.urgency,
                "combined_urgency": combined.urgency,
            }
        )

    return Starlette(routes=[Route("/", probe)])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_getitem_sees_only_the_first_priority_field_line() -> None:
    transport = httpx.ASGITransport(app=_priority_probe_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/", headers=[("priority", "u=1"), ("priority", "u=6")])
    assert response.json() == {
        "starlette_getitem": "u=1",
        "starlette_getlist": ["u=1", "u=6"],
        "first_line_urgency": 1,
        "combined_urgency": 6,
    }
    assert httpx.Headers([("priority", "u=1"), ("priority", "u=6")])["priority"] == "u=1, u=6"


@pytest.mark.unit
def test_h11_delivers_separate_lines_and_obs_text_that_the_sf_parser_must_reject() -> None:
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(
        b"GET / HTTP/1.1\r\nHost: x\r\n"
        b"Priority: u=1\r\nPriority: \t i \r\n"
        b'Example: "caf\xe9"\r\n\r\n'
    )
    event = conn.next_event()
    assert isinstance(event, h11.Request)
    lines = [value for name, value in event.headers if name == b"priority"]
    # h11 0.16.0 keeps each field line and strips surrounding OWS (including HTAB).
    assert lines == [b"u=1", b"i"]
    assert priority_from_field_lines(lines) == Priority(urgency=1, incremental=True)
    example = next(value for name, value in event.headers if name == b"example")
    assert example == b'"caf\xe9"'
    with pytest.raises(StructuredFieldError, match="not ASCII"):
        parse_item(example)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        ([b"u=5, i"], Priority(5, True)),
        ([b"u=?1"], Priority(3, False)),
        ([b'u="0"'], Priority(3, False)),
        ([b"u=8"], Priority(3, False)),
        ([b"u=-1"], Priority(3, False)),
        ([b"u=1.0"], Priority(3, False)),
        ([b"i=1"], Priority(3, False)),
        ([b"u=0, x-vendor=abc;foo"], Priority(0, False)),
        ([b"u=0, U=1"], Priority(3, False)),
        ([b"u=0,"], Priority(3, False)),
        ([], Priority(3, False)),
    ],
    ids=[
        "valid",
        "bool-not-urgency",
        "string-not-urgency",
        "above-7",
        "negative",
        "decimal",
        "int-not-incremental",
        "unknown-ignored",
        "uppercase-key-voids-field",
        "trailing-comma-voids-field",
        "absent",
    ],
)
def test_priority_ignores_wrong_types_ranges_and_unparseable_fields(
    lines: list[bytes], expected: Priority
) -> None:
    assert priority_from_field_lines(lines) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0.0005"), "0.0"),
        (Decimal("0.0015"), "0.002"),
        (Decimal("1.0025"), "1.002"),
        (Decimal("2.50"), "2.5"),
        (Decimal("-0.0004"), "0.0"),
        (Decimal("-1.5"), "-1.5"),
        (Decimal("999999999999.9994"), "999999999999.999"),
        (Decimal("999999999999.9995"), None),
        (Decimal("1E+30"), None),
        (Decimal("NaN"), None),
    ],
    ids=str,
)
def test_decimal_serialization_rounds_half_even_then_checks_twelve_digits(
    value: Decimal, expected: str | None
) -> None:
    if expected is None:
        with pytest.raises(StructuredFieldError):
            serialize_item(Item(value))
    else:
        assert serialize_item(Item(value)) == expected


@pytest.mark.unit
def test_integer_and_date_range_is_fifteen_signed_digits() -> None:
    assert serialize_item(Item(999_999_999_999_999)) == "999999999999999"
    assert serialize_item(Item(-999_999_999_999_999)) == "-999999999999999"
    for bad in (10**15, -(10**15)):
        with pytest.raises(StructuredFieldError):
            serialize_item(Item(bad))
    with pytest.raises(StructuredFieldError):
        serialize_item(Item(Date(10**15)))
    # RFC 9651 Section 3.3.7: parsers must cover years 1 to 9999.
    assert parse_item(b"@-62135596800").value == Date(-62_135_596_800)
    assert parse_item(b"@253402214400").value == Date(253_402_214_400)


@pytest.mark.unit
def test_byte_sequence_tolerates_missing_padding_and_nonzero_pad_bits() -> None:
    assert parse_item(b":aGVsbG8:").value == b"hello"
    # draft-ietf-httpapi-ratelimit-headers-11 Section 3.2 example has non-zero pad bits.
    pk = parse_item(b'"peruser";q=100;w=60;pk=:cHsdsRa894==:').params["pk"]
    assert pk == bytes.fromhex("707b1db116bcf7")
    assert serialize_item(Item(pk)) == ":cHsdsRa89w==:"
    for bad in (b":aGVs\nbG8=:", b":aGVsbG8=", b":a-b_:"):
        with pytest.raises(StructuredFieldError):
            parse_item(bad)


@pytest.mark.unit
def test_display_string_percent_encodes_utf8_with_lowercase_hex() -> None:
    text = 'f\u00fc\u00fc 50% "off"'
    wire = serialize_item(Item(DisplayString(text)))
    assert wire == '%"f%c3%bc%c3%bc 50%25 %22off%22"'
    assert parse_item(wire.encode("ascii")).value == DisplayString(text)
    for bad in (b'%"%c3%28"', b'%"%c"', b'%"abc'):
        with pytest.raises(StructuredFieldError):
            parse_item(bad)
    with pytest.raises(StructuredFieldError):
        serialize_item(Item(DisplayString("\ud800")))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"@1688169599", datetime(2023, 6, 30, 23, 59, 59, tzinfo=UTC)),
        (b"@1688169599;x=1", datetime(2023, 6, 30, 23, 59, 59, tzinfo=UTC)),
        (b"true", None),
        (b"?1", None),
        (b"1688169599", None),
        (b"@1688169599.5", None),
        (b"Fri, 30 Jun 2023 23:59:59 GMT", None),
        (b"@-62135596800", datetime(1, 1, 1, tzinfo=UTC)),
        (b"@253402300799", datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)),
        (b"@-62135596801", None),
        (b"@253402300800", None),
        (b"@999999999999999", None),
        (b"@-999999999999999", None),
    ],
    ids=[
        "rfc9745-example",
        "unknown-param",
        "token",
        "boolean",
        "integer",
        "decimal",
        "imf",
        "year-1-start",
        "year-9999-end",
        "year-0",
        "year-10000",
        "max-15-digit",
        "min-15-digit",
    ],
)
def test_deprecation_accepts_only_an_sf_date(value: bytes, expected: datetime | None) -> None:
    # RFC 9651 Section 3.3.7 only requires years 1 to 9999; a Date outside the
    # datetime range is well-formed but unrepresentable, so it is ignored, not raised.
    assert deprecation_from_field(value) == expected


@pytest.mark.unit
def test_ratelimit_draft_examples_and_malformed_fields() -> None:
    assert ratelimit_from_field_lines([b'"default";r=50;t=30']) == [
        ServiceLimit("default", 50, 30, None)
    ]
    assert ratelimit_from_field_lines([b'"a";r=1', b'"b";r=2;pk=:QXBwLTk5OQ==:']) == [
        ServiceLimit("a", 1, None, None),
        ServiceLimit("b", 2, None, b"App-999"),
    ]
    malformed: list[bytes] = [
        b"default;r=50",
        b'"default";t=30',
        b'"default";r=-1',
        b'"default";r=?1',
        b'"default";r=5;t="30"',
        b'"default";r=5;pk="abc"',
        b'"default";r=5,',
    ]
    for value in malformed:
        assert ratelimit_from_field_lines([value]) is None, value


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (b"\xc3\xbc", "not ASCII"),
        ('"\u00fc"', "not ASCII"),
        (b'"caf\xe9"', "not ASCII"),
        (b'"a\tb"', "control character"),
    ],
    ids=["utf8-bytes", "non-ascii-str", "obs-text", "tab-in-string"],
)
def test_non_ascii_and_control_characters_fail_parsing(value: bytes | str, reason: str) -> None:
    # Section 4.2 step 1 rejects non-ASCII before any type-specific rule runs.
    with pytest.raises(StructuredFieldError, match=reason):
        parse_item(value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "item",
    [
        Item(1, {"A": 1}),
        Item(1, {"1a": 1}),
        Item(Token("1abc")),
        Item(Token("a b")),
        Item("line\nbreak"),
        Item("caf\u00e9"),
    ],
    ids=["uppercase-key", "digit-key", "digit-token", "space-token", "newline", "non-ascii"],
)
def test_serializer_refuses_values_the_parser_would_reject(item: Item) -> None:
    with pytest.raises(StructuredFieldError):
        serialize_item(item)


@pytest.mark.unit
def test_empty_list_and_dictionary_mean_omit_the_field() -> None:
    assert parse_list(b"") == []
    assert parse_dictionary(b"   ") == {}
    assert serialize_list([]) is None
    assert serialize_dictionary({}) is None
    with pytest.raises(StructuredFieldError):
        parse_item(b"")


@pytest.mark.unit
def test_parser_meets_rfc9651_minimum_supported_sizes() -> None:
    members = ", ".join(str(n) for n in range(1024))
    assert len(parse_list(members.encode())) == 1024
    inner = parse_list(("(" + " ".join(["a"] * 256) + ")").encode())[0]
    assert isinstance(inner, InnerList)
    assert len(inner.items) == 256
    params: dict[str, BareItem] = {f"k{n}": n for n in range(256)}
    params["k" * 64] = True
    wire = serialize_item(Item(1, params))
    assert parse_item(wire.encode()) == Item(1, params)
    for big in (Item("s" * 1024), Item(Token("t" * 512)), Item(bytes(range(256)) * 64)):
        assert parse_item(serialize_item(big).encode()) == big
