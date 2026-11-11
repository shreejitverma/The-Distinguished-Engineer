"""Deprecation (RFC 9745) and Sunset (RFC 8594): codecs, FastAPI emission, client telemetry."""

from __future__ import annotations

import asyncio
import email.utils
import socket
import threading
import time
import warnings
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Literal
from zoneinfo import ZoneInfo

import h2.config
import h2.connection
import h2.events
import h11
import hpack
import httpx
import hypercorn.asyncio
import hypercorn.config
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from granian.constants import Interfaces
from granian.server.embed import Server as GranianServer

from api_labs.f07_versioning_evolution_and_api_guidelines.retirement_headers import (
    RETIREMENT_HEADERS,
    SCOPE_KEY,
    ApiRetirementWarning,
    FieldParseError,
    RetirementHeadersMiddleware,
    RetirementPolicy,
    RetirementRoute,
    RetirementTelemetry,
    SfDate,
    Token,
    deprecated,
    format_http_date,
    parse_deprecation,
    parse_http_date,
    parse_sf_item,
    serialize_sf_date,
    sf_date_to_datetime,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from starlette.types import Message, Receive, Scope, Send

NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
POLICY = RetirementPolicy(
    deprecated_at=datetime(2026, 11, 1, tzinfo=UTC),
    sunset_at=datetime(2027, 5, 1, tzinfo=UTC),
    deprecation_doc="https://developer.example.com/deprecation/orders-v1",
    sunset_doc="https://developer.example.com/sunset-policy",
)


# ---------------------------------------------------------------- codecs


@pytest.mark.unit
def test_sf_date_serializes_rfc_9745_example_and_enforces_integer_range() -> None:
    assert serialize_sf_date(1688169599) == "@1688169599"
    assert serialize_sf_date(-62135596800) == "@-62135596800"
    assert serialize_sf_date(999_999_999_999_999) == "@999999999999999"
    for bad in (1_000_000_000_000_000, -1_000_000_000_000_000, True):
        with pytest.raises(ValueError, match="out of the sf-integer range"):
            serialize_sf_date(bad)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field_value", "seconds"),
    [
        ("@1688169599", 1688169599),
        ("   @1688169599", 1688169599),
        ("@1688169599  ", 1688169599),
        ("@-62135596800", -62135596800),
        ("@1793491200;grease=?1", 1793491200),
        ("@999999999999999", 999_999_999_999_999),
    ],
)
def test_strict_parser_accepts_valid_sf_dates(field_value: str, seconds: int) -> None:
    assert parse_deprecation(field_value) == SfDate(seconds)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field_value", "reason"),
    [
        ("1688169599", "must be a Date, got int"),
        ("?1", "must be a Date, got bool"),
        ("true", "must be a Date, got Token"),
        ("@1688169599.5", "a Date must be an integer"),
        ("@", "expected a DIGIT"),
        ("@ 1688169599", "expected a DIGIT"),
        ("@1688169599, @1700000000", "trailing characters"),
        ("@1234567890123456", "integer longer than 15 digits"),
        ("Fri, 30 Jun 2023 23:59:59 GMT", "trailing characters"),
        ("@1688169599;Upper=1", "malformed parameter key"),
        ("@1688169599\t", "trailing characters"),
        ("@\u0661\u0662", "not ASCII"),
    ],
)
def test_strict_parser_rejects_whole_field(field_value: str, reason: str) -> None:
    with pytest.raises(FieldParseError, match=reason):
        parse_deprecation(field_value)


@pytest.mark.unit
def test_item_parameters_cover_every_bare_item_type() -> None:
    item = parse_sf_item(
        '@1;i=-7;d=1.250;s="a\\"b";t=foo/bar;b=:aGk=:;f=?0;dt=@2;ds=%"caf%c3%a9";x;x=9'
    )
    assert item.value == SfDate(1)
    assert item.params == {
        "i": -7,
        "d": Decimal("1.250"),
        "s": 'a"b',
        "t": Token("foo/bar"),
        "b": b"hi",
        "f": False,
        "dt": SfDate(2),
        "ds": "caf\u00e9",
        "x": 9,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field_value", "reason"),
    [
        ("1234567890123.5", "decimal integer part longer than 12 digits"),
        ("123456789012.1234", "decimal longer than 16 characters"),
        ("1.2345", "malformed decimal fraction"),
        ("1.", "malformed decimal fraction"),
        ("?2", "malformed boolean"),
        ("!x", "unrecognized item type"),
        ('"a\\x"', "bad escape in string"),
        ('"a\x01"', "control character in string"),
        ('"abc', "unterminated string"),
        (":aGk=", "unterminated byte sequence"),
        (":a*b:", "outside the base64 alphabet"),
        (":a:", "invalid base64"),
        ("%x", "malformed display string"),
        ('%"\x01"', "control character in display string"),
        ('%"%zz"', "bad percent-encoding"),
        ('%"%ff"', "not UTF-8"),
        ('%"abc', "unterminated display string"),
    ],
)
def test_bare_item_error_paths_fail_the_whole_field(field_value: str, reason: str) -> None:
    with pytest.raises(FieldParseError, match=reason):
        parse_sf_item(field_value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_passes_non_http_scopes_through_untouched() -> None:
    seen: list[str] = []
    inner_send: list[Send] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])
        inner_send.append(send)
        await send({"type": "lifespan.startup.complete"})

    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        sent.append(message)

    await RetirementHeadersMiddleware(inner)({"type": "lifespan", SCOPE_KEY: POLICY}, receive, send)
    assert seen == ["lifespan"]
    assert inner_send == [send]
    assert sent == [{"type": "lifespan.startup.complete"}]


async def raw_asgi_messages(scope: Scope) -> list[Message]:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        start = {"type": "http.response.start", "status": 200, "headers": [(b"link", b"<a>")]}
        await send(start)
        await send({"type": "http.response.body", "body": b"{}"})

    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request"}

    async def send(message: Message) -> None:
        sent.append(message)

    await RetirementHeadersMiddleware(inner)(scope, receive, send)
    return sent


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_appends_lowercase_byte_pairs_to_the_raw_asgi_header_list() -> None:
    body = {"type": "http.response.body", "body": b"{}"}
    tagged = await raw_asgi_messages({"type": "http", SCOPE_KEY: POLICY})
    assert tagged == [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"link", b"<a>"),
                (b"deprecation", b"@1793491200"),
                (b"sunset", b"Sat, 01 May 2027 00:00:00 GMT"),
                (b"link", POLICY.header_items()[2][1].encode()),
            ],
        },
        body,
    ]
    untagged = await raw_asgi_messages({"type": "http"})
    assert untagged == [
        {"type": "http.response.start", "status": 200, "headers": [(b"link", b"<a>")]},
        body,
    ]


@pytest.mark.unit
def test_sf_date_maps_to_datetime_portably_and_flags_unrepresentable_years() -> None:
    assert sf_date_to_datetime(SfDate(1688169599)) == datetime(2023, 6, 30, 23, 59, 59, tzinfo=UTC)
    assert sf_date_to_datetime(SfDate(-62135596800)) == datetime(1, 1, 1, tzinfo=UTC)
    assert sf_date_to_datetime(SfDate(253402214400)) == datetime(9999, 12, 31, tzinfo=UTC)
    with pytest.raises(FieldParseError, match=r"outside datetime's year 1\.\.9999"):
        sf_date_to_datetime(SfDate(999_999_999_999_999))


@pytest.mark.unit
@pytest.mark.parametrize(
    "field_value",
    ["Sun, 06 Nov 1994 08:49:37 GMT", "Sunday, 06-Nov-94 08:49:37 GMT", "Sun Nov  6 08:49:37 1994"],
)
def test_http_date_parser_accepts_all_three_rfc_9110_formats(field_value: str) -> None:
    assert parse_http_date(field_value, now=NOW) == datetime(1994, 11, 6, 8, 49, 37, tzinfo=UTC)


@pytest.mark.unit
def test_rfc850_two_digit_year_resolves_against_the_clock() -> None:
    assert parse_http_date("Monday, 01-Jan-76 00:00:00 GMT", now=NOW).year == 2076
    assert parse_http_date("Monday, 01-Jan-77 00:00:00 GMT", now=NOW).year == 1977


@pytest.mark.unit
def test_rfc850_fifty_year_window_compares_instants_not_calendar_years() -> None:
    exactly_fifty_years_ahead = parse_http_date("Wednesday, 11-Nov-76 12:00:00 GMT", now=NOW)
    assert exactly_fifty_years_ahead == datetime(2076, 11, 11, 12, tzinfo=UTC)
    one_second_more = "Thursday, 11-Nov-76 12:00:01 GMT"
    assert parse_http_date(one_second_more, now=NOW) == datetime(1976, 11, 11, 12, 0, 1, tzinfo=UTC)
    assert parse_http_date("Friday, 31-Dec-76 23:59:59 GMT", now=NOW).year == 1976
    kiritimati_clock = NOW.astimezone(timezone(timedelta(hours=14)))
    assert parse_http_date(one_second_more, now=kiritimati_clock).year == 1976


@pytest.mark.unit
def test_http_date_leap_second_rolls_into_next_minute() -> None:
    parsed = parse_http_date("Sat, 31 Dec 2016 23:59:60 GMT", now=NOW)
    assert parsed == datetime(2017, 1, 1, tzinfo=UTC)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field_value", "stdlib_result"),
    [
        ("Sun, 30 Jun 2024 23:59:59 UTC", datetime(2024, 6, 30, 23, 59, 59, tzinfo=UTC)),
        ("sun, 30 jun 2024 23:59:59 gmt", datetime(2024, 6, 30, 23, 59, 59, tzinfo=UTC)),
        (
            "Sun, 30 Jun 2024 23:59:59 +0200",
            datetime(2024, 6, 30, 23, 59, 59, tzinfo=timezone(timedelta(hours=2))),
        ),
        ("Sun, 30 Jun 2024 23:59:59", datetime(2024, 6, 30, 23, 59, 59)),
        (
            "Sat, 31 Dec 2018 23:59:59 GMT, Wed, 11 Nov 2026 11:11:11 GMT",
            datetime(2018, 12, 31, 23, 59, 59),
        ),
    ],
)
def test_strict_http_date_rejects_what_email_utils_silently_accepts(
    field_value: str, stdlib_result: datetime
) -> None:
    assert email.utils.parsedate_to_datetime(field_value) == stdlib_result
    with pytest.raises(FieldParseError, match="not an HTTP-date"):
        parse_http_date(field_value, now=NOW)


@pytest.mark.unit
def test_email_utils_naive_result_cannot_be_compared_with_an_aware_clock() -> None:
    combined = "Sat, 31 Dec 2018 23:59:59 GMT, Wed, 11 Nov 2026 11:11:11 GMT"
    parsed = email.utils.parsedate_to_datetime(combined)
    assert parsed.tzinfo is None
    with pytest.raises(TypeError, match="offset-naive and offset-aware"):
        _ = parsed < NOW
    asctime = "Sun Nov  6 08:49:37 1994"
    assert email.utils.parsedate_to_datetime(asctime).tzinfo is None
    assert parse_http_date(asctime, now=NOW).tzinfo is UTC


@pytest.mark.unit
def test_impossible_calendar_dates_are_rejected() -> None:
    with pytest.raises(FieldParseError, match="impossible calendar date"):
        parse_http_date("Sat, 30 Feb 2027 00:00:00 GMT", now=NOW)
    with pytest.raises(FieldParseError, match="time-of-day out of range"):
        parse_http_date("Sat, 01 May 2027 24:00:00 GMT", now=NOW)


@pytest.mark.unit
def test_imf_fixdate_formatting_accepts_any_zero_offset_tzinfo() -> None:
    assert format_http_date(datetime(2026, 11, 11, 11, 11, 11, tzinfo=UTC)) == (
        "Wed, 11 Nov 2026 11:11:11 GMT"
    )
    zoneinfo_utc = datetime(2027, 5, 1, tzinfo=ZoneInfo("UTC"))
    assert format_http_date(zoneinfo_utc) == "Sat, 01 May 2027 00:00:00 GMT"
    with pytest.raises(ValueError, match="usegmt option requires a UTC datetime"):
        email.utils.format_datetime(zoneinfo_utc, usegmt=True)
    for bad in (datetime(2027, 5, 1), datetime(2027, 5, 1, tzinfo=ZoneInfo("Europe/Berlin"))):
        with pytest.raises(ValueError, match=r"naive|must be UTC"):
            format_http_date(bad)


@pytest.mark.unit
def test_policy_enforces_rfc_9745_ordering_and_whole_seconds() -> None:
    with pytest.raises(ValueError, match="MUST NOT be earlier"):
        RetirementPolicy(
            deprecated_at=datetime(2027, 1, 1, tzinfo=UTC),
            sunset_at=datetime(2026, 12, 31, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="whole seconds"):
        RetirementPolicy(deprecated_at=datetime(2027, 1, 1, 0, 0, 0, 500_000, tzinfo=UTC))
    with pytest.raises(ValueError, match="unsafe link target"):
        RetirementPolicy(
            deprecated_at=datetime(2027, 1, 1, tzinfo=UTC),
            deprecation_doc='https://x.test/>; rel="successor-version"',
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "unsafe",
    ["<", ">", '"', " ", ",", ";", "\u00e9", "\x7f", "\r\n"],
    ids=["lt", "gt", "dquote", "space", "comma", "semicolon", "non-ascii", "del", "crlf"],
)
@pytest.mark.parametrize("field", ["deprecation_doc", "sunset_doc"])
def test_policy_rejects_every_unsafe_link_target_character(unsafe: str, field: str) -> None:
    base = RetirementPolicy(deprecated_at=datetime(2027, 1, 1, tzinfo=UTC))
    target = f"https://x.test/a{unsafe}b"
    deprecation_doc = target if field == "deprecation_doc" else None
    sunset_doc = target if field == "sunset_doc" else None
    with pytest.raises(ValueError, match="unsafe link target"):
        replace(base, deprecation_doc=deprecation_doc, sunset_doc=sunset_doc)
    safe = replace(base, sunset_doc="/a?b=c#d")
    assert safe.header_items()[1] == ("Link", '</a?b=c#d>; rel="sunset"; type="text/html"')


@pytest.mark.unit
def test_policy_emits_exact_header_values() -> None:
    assert POLICY.header_items() == [
        ("Deprecation", "@1793491200"),
        ("Sunset", "Sat, 01 May 2027 00:00:00 GMT"),
        (
            "Link",
            '<https://developer.example.com/deprecation/orders-v1>; rel="deprecation"; '
            'type="text/html", <https://developer.example.com/sunset-policy>; '
            'rel="sunset"; type="text/html"',
        ),
    ]


# ------------------------------------------------------------- server side


def build_app(*, cors: bool = False) -> FastAPI:
    app = FastAPI()
    v1 = APIRouter(prefix="/v1", route_class=RetirementRoute)
    v2 = APIRouter(prefix="/v2", route_class=RetirementRoute)

    @v1.get("/orders/{order_id}")
    @deprecated(POLICY)
    async def get_order_v1(order_id: int) -> dict[str, int | str]:
        if order_id == 404:
            raise HTTPException(status_code=404, detail="no such order")
        return {"id": order_id, "state": "open"}

    @v1.get("/orders")
    @deprecated(POLICY)
    async def list_orders_v1() -> JSONResponse:
        return JSONResponse([], headers={"Link": '</v1/orders?page=2>; rel="next"'})

    @v2.get("/orders/{order_id}")
    async def get_order_v2(order_id: int) -> dict[str, int | str]:
        return {"id": order_id, "state": "open"}

    app.include_router(v1)
    app.include_router(v2)
    app.add_middleware(RetirementHeadersMiddleware)
    if cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["https://dashboard.example.com"],
            expose_headers=RETIREMENT_HEADERS,
        )
    return app


def client_for(app: FastAPI, **kwargs: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        **kwargs,  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    )


def retirement_lines(response: httpx.Response) -> list[tuple[str, str]]:
    return [(k, v) for k, v in response.headers.multi_items() if k in ("deprecation", "sunset")]


EXPECTED_LINES = [("deprecation", "@1793491200"), ("sunset", "Sat, 01 May 2027 00:00:00 GMT")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_headers_on_success_error_validation_and_direct_response_paths() -> None:
    async with client_for(build_app()) as client:
        ok = await client.get("/v1/orders/7")
        not_found = await client.get("/v1/orders/404")
        invalid = await client.get("/v1/orders/not-an-int")
        listed = await client.get("/v1/orders")
    assert [ok.status_code, not_found.status_code, invalid.status_code] == [200, 404, 422]
    for response in (ok, not_found, invalid, listed):
        assert retirement_lines(response) == EXPECTED_LINES
    assert listed.headers.get_list("link") == [
        '</v1/orders?page=2>; rel="next"',
        POLICY.header_items()[2][1],
    ]
    assert set(listed.links) == {"next", "deprecation", "sunset"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deprecation_does_not_change_resource_behavior() -> None:
    async with client_for(build_app()) as client:
        old = await client.get("/v1/orders/7")
        new = await client.get("/v2/orders/7")
    assert old.content == new.content == b'{"id":7,"state":"open"}'
    assert retirement_lines(new) == []
    assert "link" not in new.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openapi_marks_only_decorated_operations_deprecated() -> None:
    async with client_for(build_app()) as client:
        paths = (await client.get("/openapi.json")).json()["paths"]
    assert paths["/v1/orders/{order_id}"]["get"]["deprecated"] is True
    assert paths["/v1/orders"]["get"]["deprecated"] is True
    assert "deprecated" not in paths["/v2/orders/{order_id}"]["get"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_deprecated_flag_alone_emits_no_header() -> None:
    app = FastAPI()

    @app.get("/legacy", deprecated=True)
    async def legacy() -> dict[str, bool]:
        return {"ok": True}

    async with client_for(app) as client:
        response = await client.get("/legacy")
        spec = (await client.get("/openapi.json")).json()
    assert spec["paths"]["/legacy"]["get"]["deprecated"] is True
    assert "deprecation" not in response.headers


def announce_naively(response: Response) -> None:
    for name, value in POLICY.header_items():
        response.headers[name] = value


Announced = Annotated[None, Depends(announce_naively)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naive_dependency_loses_headers_on_direct_response_and_errors() -> None:
    app = FastAPI()

    @app.get("/model")
    async def model(_: Announced) -> dict[str, int]:
        return {"id": 1}

    @app.get("/direct")
    async def direct(_: Announced) -> JSONResponse:
        return JSONResponse({"id": 1})

    @app.get("/error")
    async def error(_: Announced) -> dict[str, int]:
        raise HTTPException(status_code=404)

    async with client_for(app) as client:
        model_response = await client.get("/model")
        direct_response = await client.get("/direct")
        error_response = await client.get("/error")
    statuses = [r.status_code for r in (model_response, direct_response, error_response)]
    assert statuses == [200, 200, 404]
    assert retirement_lines(model_response) == EXPECTED_LINES
    assert retirement_lines(direct_response) == []
    assert retirement_lines(error_response) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cors_must_expose_retirement_headers_to_browser_code() -> None:
    origin = {"Origin": "https://dashboard.example.com"}
    async with client_for(build_app(cors=True)) as client:
        response = await client.get("/v1/orders/7", headers=origin)
    assert response.headers["access-control-expose-headers"] == "Deprecation, Sunset, Link"
    assert retirement_lines(response) == EXPECTED_LINES


# ------------------------------------------------------------------- wire


@pytest.mark.unit
def test_http_1_1_wire_bytes_from_h11() -> None:
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(b"GET /v1/orders/7 HTTP/1.1\r\nHost: api.example.test\r\n\r\n")
    assert isinstance(conn.next_event(), h11.Request)
    headers = [(name, value) for name, value in POLICY.header_items()[:2]]
    wire = conn.send(h11.Response(status_code=200, headers=[*headers, ("Content-Length", "0")]))
    assert wire == (
        b"HTTP/1.1 200 \r\n"
        b"Deprecation: @1793491200\r\n"
        b"Sunset: Sat, 01 May 2027 00:00:00 GMT\r\n"
        b"Content-Length: 0\r\n\r\n"
    )


@pytest.mark.unit
def test_http_2_lowercases_names_and_carries_values_verbatim() -> None:
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    request = [(":method", "GET"), (":path", "/v1/orders/7"), (":scheme", "https")]
    client.send_headers(1, [*request, (":authority", "api.example.test")], end_stream=True)
    server.receive_data(client.data_to_send())
    server.send_headers(1, [(":status", "200"), *POLICY.header_items()[:2]], end_stream=True)
    events = client.receive_data(server.data_to_send())
    received = next(e for e in events if isinstance(e, h2.events.ResponseReceived))
    assert received.headers is not None
    assert list(received.headers)[1:] == [
        (b"deprecation", b"@1793491200"),
        (b"sunset", b"Sat, 01 May 2027 00:00:00 GMT"),
    ]


@pytest.mark.unit
def test_measured_per_response_header_cost_http_1_1_vs_hpack() -> None:
    http11 = [len(f"{name}: {value}\r\n") for name, value in POLICY.header_items()]
    items = [(name.lower(), value) for name, value in POLICY.header_items()]
    per_field = [len(hpack.Encoder().encode([item])) for item in items]
    encoder = hpack.Encoder()
    first, repeat = len(encoder.encode(items)), len(encoder.encode(items))
    assert (http11, sum(http11)) == ([26, 39, 177], 242)
    assert (per_field, first, repeat) == ([20, 29, 132], 181, 3)


CLIENT_ADDRESS, SERVER_ADDRESS = ("127.0.0.1", 5000), ("127.0.0.1", 4433)


def http_3_response_as_seen_by_client(
    response_headers: list[tuple[bytes, bytes]],
) -> tuple[list[tuple[bytes, bytes]] | None, list[tuple[int, str]]]:
    """Send one response over aioquic HTTP/3 joined by in-memory datagrams: no sockets, no clock.

    Returns the header list the client surfaced (or None) and the connection errors the server saw.
    """
    # aioquic needs pylsqpack, which has no free-threaded wheel and does not build on 3.15.2t.
    pytest.importorskip("aioquic")
    from aioquic.h3.connection import H3Connection
    from aioquic.h3.events import H3Event, HeadersReceived
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.connection import QuicConnection
    from aioquic.quic.events import ConnectionTerminated

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    server_config = QuicConfiguration(is_client=False, alpn_protocols=["h3"])
    server_config.certificate, server_config.private_key = certificate, key
    client = QuicConnection(
        configuration=QuicConfiguration(is_client=True, alpn_protocols=["h3"], verify_mode=0)
    )
    client.connect(SERVER_ADDRESS, now=0.0)
    server = QuicConnection(
        configuration=server_config,
        original_destination_connection_id=client.original_destination_connection_id,
    )
    h3_client, h3_server = H3Connection(client), H3Connection(server)
    now = 0.0

    def pump() -> None:
        nonlocal now
        for _ in range(300):
            for source, target, address in (
                (client, server, CLIENT_ADDRESS),
                (server, client, SERVER_ADDRESS),
            ):
                for datagram, _ in source.datagrams_to_send(now=now):
                    target.receive_datagram(datagram, address, now=now)
                timer = source.get_timer()
                if timer is not None and timer <= now:
                    source.handle_timer(now=now)
            now += 0.01

    def drain(
        quic: QuicConnection, h3: H3Connection
    ) -> tuple[list[H3Event], list[tuple[int, str]]]:
        events: list[H3Event] = []
        terminated: list[tuple[int, str]] = []
        while (event := quic.next_event()) is not None:
            if isinstance(event, ConnectionTerminated):
                terminated.append((event.error_code, event.reason_phrase))
            events.extend(h3.handle_event(event))
        return events, terminated

    pump()
    stream_id = client.get_next_available_stream_id()
    request = [(b":method", b"GET"), (b":scheme", b"https"), (b":authority", b"api.example.test")]
    h3_client.send_headers(stream_id, [*request, (b":path", b"/v1/orders/7")], end_stream=True)
    pump()
    received, _ = drain(server, h3_server)
    assert [type(event) for event in received] == [HeadersReceived]
    h3_server.send_headers(stream_id, response_headers, end_stream=True)
    pump()
    surfaced, _ = drain(client, h3_client)
    pump()
    _, errors = drain(server, h3_server)
    responses = [event.headers for event in surfaced if isinstance(event, HeadersReceived)]
    return (responses[0] if responses else None), errors


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_3_delivers_the_lowercase_asgi_fields_and_rejects_capitalized_names() -> None:
    start = (await raw_asgi_messages({"type": "http", SCOPE_KEY: POLICY}))[0]
    lowercase: list[tuple[bytes, bytes]] = [(b":status", b"200"), *start["headers"]]
    assert http_3_response_as_seen_by_client(lowercase) == (lowercase, [])
    # aioquic 1.7.2 does not normalize outbound names the way h2 4.4.1 does, so a capitalized
    # name reaches the peer, which must treat the message as malformed (RFC 9114 Section 4.2).
    capitalized = [(name.encode(), value.encode()) for name, value in POLICY.header_items()]
    assert http_3_response_as_seen_by_client([(b":status", b"200"), *capitalized]) == (
        None,
        [(0x10E, "Header b'Deprecation' contains invalid characters")],
    )


# ------------------------------------------------------------ real servers

ServerKind = Literal["uvicorn-h11", "uvicorn-httptools", "hypercorn", "granian"]
POLICY_LINK = POLICY.header_items()[2][1].encode()
WIRE_DATES = [b"deprecation: @1793491200", b"sunset: Sat, 01 May 2027 00:00:00 GMT"]
NEXT_LINK = b'link: </v1/orders?page=2>; rel="next"'


@asynccontextmanager
async def live_server(kind: ServerKind) -> AsyncGenerator[int]:
    """Serve ``build_app()`` with a real ASGI server on an ephemeral loopback port; yield it."""
    app = build_app()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    stop: Callable[[], None]
    if kind == "hypercorn":
        config = hypercorn.config.Config()
        config.bind = [f"127.0.0.1:{port}"]
        config.loglevel = "WARNING"
        trigger = asyncio.Event()
        task = asyncio.create_task(
            hypercorn.asyncio.serve(app, config, shutdown_trigger=trigger.wait, mode="asgi")  # type: ignore[arg-type]
        )
        stop = trigger.set
    elif kind == "granian":
        granian = GranianServer(
            app, address="127.0.0.1", port=port, interface=Interfaces.ASGINL, log_enabled=False
        )
        task = asyncio.create_task(granian.serve())
        stop = granian.stop
    else:
        parser: Literal["h11", "httptools"] = "h11" if kind == "uvicorn-h11" else "httptools"
        server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=port, http=parser, lifespan="off", log_level="warning"
            )
        )
        task = asyncio.create_task(server.serve())

        def stop() -> None:
            server.should_exit = True

    try:
        for _ in range(500):
            assert not task.done(), f"{kind} exited before it accepted connections"
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(0.01)
                continue
            writer.close()
            await writer.wait_closed()
            break
        else:
            raise AssertionError(f"{kind} did not accept connections")
        yield port
    finally:
        stop()
        await asyncio.wait_for(task, timeout=10)


async def http_1_1_retirement_lines(port: int, path: str) -> tuple[bytes, list[bytes]]:
    """One HTTP/1.1 exchange on a raw socket: the status code and the raw retirement field lines."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
    raw = await asyncio.wait_for(reader.read(), timeout=10)
    writer.close()
    await writer.wait_closed()
    status_line, *lines = raw.split(b"\r\n\r\n")[0].split(b"\r\n")
    names = (b"deprecation:", b"sunset:", b"link:")
    return status_line.split(b" ")[1], [line for line in lines if line.lower().startswith(names)]


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["uvicorn-h11", "uvicorn-httptools", "hypercorn", "granian"])
async def test_real_servers_put_the_fields_on_the_http_1_1_wire_for_every_status(
    kind: ServerKind,
) -> None:
    async with live_server(kind) as port:
        ok = await http_1_1_retirement_lines(port, "/v1/orders/7")
        not_found = await http_1_1_retirement_lines(port, "/v1/orders/404")
        invalid = await http_1_1_retirement_lines(port, "/v1/orders/not-an-int")
        listed = await http_1_1_retirement_lines(port, "/v1/orders")
        replacement = await http_1_1_retirement_lines(port, "/v2/orders/7")
    policy_lines = [*WIRE_DATES, b"link: " + POLICY_LINK]
    assert ok == (b"200", policy_lines)
    assert not_found == (b"404", policy_lines)
    assert invalid == (b"422", policy_lines)
    assert replacement == (b"200", [])
    # Granian groups field lines that share a name; the others keep the ASGI list order.
    if kind == "granian":
        assert listed == (b"200", [NEXT_LINK, b"link: " + POLICY_LINK, *WIRE_DATES])
    else:
        assert listed == (b"200", [NEXT_LINK, *policy_lines])


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["hypercorn", "granian"])
async def test_real_servers_deliver_the_fields_over_cleartext_http_2(kind: ServerKind) -> None:
    conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    conn.initiate_connection()
    request = [(":method", "GET"), (":path", "/v1/orders/404"), (":scheme", "http")]
    conn.send_headers(1, [*request, (":authority", "127.0.0.1")], end_stream=True)
    response: h2.events.ResponseReceived | None = None
    async with live_server(kind) as port, asyncio.timeout(10):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(conn.data_to_send())
        while response is None:
            data = await reader.read(65535)
            assert data, f"{kind} closed the connection before responding"
            for event in conn.receive_data(data):
                if isinstance(event, h2.events.ResponseReceived):
                    response = event
            writer.write(conn.data_to_send())
        writer.close()
        await writer.wait_closed()
    assert response.headers is not None
    headers = list(response.headers)
    assert headers[0] == (b":status", b"404")
    assert [pair for pair in headers if pair[0] in (b"deprecation", b"sunset", b"link")] == [
        (b"deprecation", b"@1793491200"),
        (b"sunset", b"Sat, 01 May 2027 00:00:00 GMT"),
        (b"link", POLICY_LINK),
    ]


# ----------------------------------------------------------- client side


def telemetry_client(app: FastAPI, telemetry: RetirementTelemetry) -> httpx.AsyncClient:
    return client_for(app, event_hooks={"response": [telemetry.async_hook]})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_telemetry_warns_once_and_counts_every_call() -> None:
    telemetry = RetirementTelemetry(clock=lambda: NOW)
    async with telemetry_client(build_app(), telemetry) as client:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for _ in range(3):
                await client.get("/v1/orders/7")
            await client.get("/v2/orders/7")
    assert [(w.category, str(w.message)[:31]) for w in caught] == [
        (ApiRetirementWarning, "GET /v1/orders/7 is deprecated;")
    ]
    assert telemetry.calls == {("GET", "/v1/orders/7"): 3}
    (signal,) = telemetry.signals
    assert signal.state == "deprecated"
    assert signal.deprecated_at == datetime(2026, 11, 1, tzinfo=UTC)
    assert signal.sunset_at == datetime(2027, 5, 1, tzinfo=UTC)
    assert signal.docs == {
        "deprecation": "https://developer.example.com/deprecation/orders-v1",
        "sunset": "https://developer.example.com/sunset-policy",
    }
    assert signal.problems == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("clock", "state"),
    [
        (datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC), "announced"),
        (datetime(2026, 11, 1, tzinfo=UTC), "deprecated"),
        (datetime(2027, 5, 1, tzinfo=UTC), "sunset-passed"),
    ],
)
def test_client_state_follows_the_clock(clock: datetime, state: str) -> None:
    telemetry = RetirementTelemetry(clock=lambda: clock)
    response = httpx.Response(
        200,
        headers=POLICY.header_items()[:2],
        request=httpx.Request("GET", "https://api.example.test/v1/orders/7"),
    )
    with pytest.warns(ApiRetirementWarning):
        signal = telemetry.observe(response)
    assert signal is not None
    assert signal.state == state


@pytest.mark.unit
def test_client_warns_again_when_the_state_or_a_field_value_changes() -> None:
    clock = [datetime(2026, 10, 31, tzinfo=UTC)]
    telemetry = RetirementTelemetry(clock=lambda: clock[0])
    request = httpx.Request("GET", "https://api.example.test/v1/orders/7")
    moved_sunset = [("Deprecation", "@1793491200"), ("Sunset", "Mon, 01 Mar 2027 00:00:00 GMT")]

    def states(headers: list[tuple[str, str]]) -> list[str]:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            telemetry.observe(httpx.Response(200, headers=headers, request=request))
        return [str(w.message).split(";")[0] for w in caught]

    original = POLICY.header_items()[:2]
    assert states(original) == ["GET /v1/orders/7 is announced"]
    assert states(original) == []
    clock[0] = NOW
    assert states(original) == ["GET /v1/orders/7 is deprecated"]
    assert states(original) == []
    assert states(moved_sunset) == ["GET /v1/orders/7 is deprecated"]
    assert states(moved_sunset) == []
    assert [s.state for s in telemetry.signals] == ["announced", "deprecated", "deprecated"]
    assert telemetry.calls == {("GET", "/v1/orders/7"): 6}


def observe_raw(headers: list[tuple[str, str]]) -> tuple[str, tuple[str, ...]]:
    telemetry = RetirementTelemetry(clock=lambda: NOW)
    request = httpx.Request("GET", "https://api.example.test/v1/orders/7")
    with pytest.warns(ApiRetirementWarning):
        signal = telemetry.observe(httpx.Response(200, headers=headers, request=request))
    assert signal is not None
    return signal.state, signal.problems


@pytest.mark.unit
def test_client_ignores_invalid_fields_and_reports_why() -> None:
    state, problems = observe_raw([("Deprecation", "@1793491200"), ("Deprecation", "@1793491201")])
    assert state == "invalid"
    assert problems == ("Deprecation ignored: trailing characters: ', @1793491201'",)
    state, problems = observe_raw(
        [("Deprecation", "@1793491200"), ("Sunset", "Sun, 30 Jun 2024 23:59:59 UTC")]
    )
    assert state == "deprecated"
    assert problems == ("Sunset ignored: not an HTTP-date: 'Sun, 30 Jun 2024 23:59:59 UTC'",)


@pytest.mark.unit
def test_client_flags_sunset_before_deprecation_and_treats_past_sunset_as_now() -> None:
    state, problems = observe_raw(
        [("Deprecation", "@1793491200"), ("Sunset", "Sat, 31 Dec 2016 23:59:59 GMT")]
    )
    assert state == "sunset-passed"
    assert problems == ("Sunset earlier than Deprecation (RFC 9745 Section 4)",)


@pytest.mark.unit
def test_client_reports_a_lone_sunset_as_scheduled_and_invalid_only_when_nothing_parses() -> None:
    # RFC 8594 defines Sunset on its own; a resource can be retiring without being deprecated.
    assert observe_raw([("Sunset", "Sat, 01 May 2027 00:00:00 GMT")]) == ("sunset-scheduled", ())
    assert observe_raw([("Sunset", "Wed, 11 Nov 2026 12:00:00 GMT")]) == ("sunset-passed", ())
    assert observe_raw([("Deprecation", "true"), ("Sunset", "Sat, 01 May 2027 00:00:00 GMT")]) == (
        "sunset-scheduled",
        ("Deprecation ignored: Deprecation must be a Date, got Token",),
    )
    assert observe_raw([("Sunset", "Sat, 01 May 2027 00:00:00 UTC")]) == (
        "invalid",
        ("Sunset ignored: not an HTTP-date: 'Sat, 01 May 2027 00:00:00 UTC'",),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telemetry_drops_the_query_string_and_unrelated_link_relations() -> None:
    telemetry = RetirementTelemetry(clock=lambda: NOW)
    async with telemetry_client(build_app(), telemetry) as client:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            listed = await client.get("/v1/orders?access_token=s3cret")
    assert set(listed.links) == {"next", "deprecation", "sunset"}
    assert telemetry.calls == {("GET", "/v1/orders"): 1}
    (signal,) = telemetry.signals
    assert (signal.method, signal.path) == ("GET", "/v1/orders")
    assert set(signal.docs) == {"deprecation", "sunset"}
    (warning,) = caught
    assert str(warning.message).startswith("GET /v1/orders is deprecated;")
    assert "s3cret" not in str(warning.message)


@pytest.mark.unit
def test_retirement_warning_survives_the_filter_that_hides_deprecation_warnings() -> None:
    assert issubclass(ApiRetirementWarning, FutureWarning)
    assert not issubclass(ApiRetirementWarning, DeprecationWarning)
    telemetry = RetirementTelemetry(clock=lambda: NOW)
    request = httpx.Request("GET", "https://api.example.test/v1/orders/7")
    response = httpx.Response(200, headers=POLICY.header_items()[:2], request=request)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warnings.simplefilter("ignore", DeprecationWarning)  # CPython's default outside __main__
        warnings.warn("hidden from end users", DeprecationWarning, stacklevel=1)
        telemetry.observe(response)
    assert [w.category for w in caught] == [ApiRetirementWarning]


class RendezvousSeen(set[tuple[str | None, ...]]):
    """A set that holds each membership answer until a second thread has one too.

    The pause sits between the check and the record, which is where a missing lock loses.
    """

    def __init__(self) -> None:
        super().__init__()
        self.barrier = threading.Barrier(2)

    def __contains__(self, item: object) -> bool:
        answer = super().__contains__(item)
        with suppress(threading.BrokenBarrierError):
            self.barrier.wait(timeout=0.25)
        return answer


class RendezvousTelemetry(RetirementTelemetry):
    def __init__(self, clock: Callable[[], datetime]) -> None:
        super().__init__(clock)
        self._seen = RendezvousSeen()


@pytest.mark.unit
def test_telemetry_lock_makes_check_then_record_atomic_across_threads() -> None:
    # Without the lock both threads hold a "not seen" answer when they meet and both record the
    # signal; with it the second thread is still waiting for the lock when the rendezvous times out.
    telemetry = RendezvousTelemetry(clock=lambda: NOW)
    request = httpx.Request("GET", "https://api.example.test/v1/orders/7")

    def call() -> None:
        telemetry.observe(httpx.Response(200, headers=POLICY.header_items()[:2], request=request))

    threads = [threading.Thread(target=call) for _ in range(2)]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert telemetry.calls == {("GET", "/v1/orders/7"): 2}
    assert [signal.state for signal in telemetry.signals] == ["deprecated"]
    assert [w.category for w in caught] == [ApiRetirementWarning]


@pytest.mark.unit
def test_cve_2025_25290_link_payload_is_parsed_without_quadratic_backtracking() -> None:
    # GHSA-rmvr-2pp2-xj38: @octokit/request matched /<([^>]+)>; rel="deprecation"/ on `link`.
    hostile = "<" * 100_000 + ">"
    telemetry = RetirementTelemetry(clock=lambda: NOW)
    request = httpx.Request("GET", "https://api.example.test/v1/orders/7")
    headers = [("Deprecation", "@1793491200"), ("Link", hostile)]
    started = time.process_time()
    with pytest.warns(ApiRetirementWarning):
        signal = telemetry.observe(httpx.Response(200, headers=headers, request=request))
    cpu_seconds = time.process_time() - started
    assert signal is not None
    assert (signal.state, signal.docs, signal.problems) == ("deprecated", {}, ())
    # Measured under 0.002 s of CPU; the vulnerable pattern needs 0.9 s for 16,000 characters and
    # quadruples with every doubling, so a 2 s bound separates the two by orders of magnitude.
    assert cpu_seconds < 2.0


@pytest.mark.unit
def test_httpx_split_commas_breaks_an_http_date_in_two() -> None:
    headers = httpx.Headers([("Sunset", "Sat, 01 May 2027 00:00:00 GMT")])
    assert headers.get_list("sunset", split_commas=True) == ["Sat", "01 May 2027 00:00:00 GMT"]
    assert headers.get_list("sunset") == ["Sat, 01 May 2027 00:00:00 GMT"]


@pytest.mark.unit
def test_multi_rel_link_is_split_into_both_relations() -> None:
    telemetry = RetirementTelemetry(clock=lambda: NOW)
    response = httpx.Response(
        200,
        headers=[
            ("Deprecation", "@1793491200"),
            ("Link", '<https://developer.example.com/retire>; rel="deprecation sunset"'),
        ],
        request=httpx.Request("GET", "https://api.example.test/v1/orders/7"),
    )
    assert set(response.links) == {"deprecation sunset"}
    with pytest.warns(ApiRetirementWarning):
        signal = telemetry.observe(response)
    assert signal is not None
    assert signal.docs == {
        "deprecation": "https://developer.example.com/retire",
        "sunset": "https://developer.example.com/retire",
    }
