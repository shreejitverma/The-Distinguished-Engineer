"""Header normalization differences across CDN, gateway, and ASGI server tiers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import h2.exceptions
import h11
import httpx
import pytest
from gunicorn.http.errors import InvalidHeader, InvalidHeaderName
from starlette.datastructures import Headers

from api_labs.f17_api_gateways_proxies_and_edge_architecture.header_normalization import (
    RawHeaders,
    UnderscoreAction,
    UnderscoreRejected,
    UvicornParser,
    apply_underscore_policy,
    build_fastapi_app,
    gunicorn_environ,
    h2_deliver,
    lookup_combined,
    lookup_first,
    nginx_http_variable,
    overwrite_trusted,
    probe_through_uvicorn,
    render_request,
    serve_once,
    strip_connection_listed,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

PARSERS: list[UvicornParser] = ["h11", "httptools"]

# What an attacker sends when the trusted name is X-Tenant-Id: an underscore twin and a duplicate.
ATTACK: RawHeaders = [
    (b"X-Tenant-Id", b"gw"),
    (b"X_Tenant_Id", b"victim"),
    (b"X-Tenant-ID", b"gw2"),
]


async def asgi_get(app: FastAPI, path: str, headers: list[tuple[str, str]]) -> dict[str, Any]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=headers)
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("parser", PARSERS)
async def test_uvicorn_lowercases_names_keeps_underscore_and_every_duplicate(
    parser: UvicornParser,
) -> None:
    raw = b"GET / HTTP/1.1\r\nHost: x\r\nX-TENANT-Id: a\r\nx_tenant_id: b\r\nX-Tenant-ID: c\r\n"
    raw += b"Connection: close\r\n\r\n"

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    headers, _ = await serve_once(app, raw, parser)

    assert headers == [
        (b"host", b"x"),
        (b"x-tenant-id", b"a"),
        (b"x_tenant_id", b"b"),
        (b"x-tenant-id", b"c"),
        (b"connection", b"close"),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("parser", PARSERS)
async def test_starlette_first_wins_getlist_all_and_dict_forms_disagree(
    parser: UvicornParser,
) -> None:
    result = await probe_through_uvicorn(render_request(ATTACK), parser)

    assert result["first"] == "gw"
    assert result["all"] == ["gw", "gw2"]
    assert result["underscore"] == ["victim"]
    assert result["via_dict_of_mapping"] == "gw"
    assert result["via_dict_of_items"] == "gw2"
    assert result["len"] == 5


@pytest.mark.unit
def test_starlette_lookup_requires_lowercase_raw_names_unlike_constructor_input() -> None:
    from_raw = Headers(raw=[(b"X-Tenant-Id", b"a")])
    from_mapping = Headers(headers={"X-Tenant-Id": "a"})

    assert from_raw.get("x-tenant-id") is None
    assert from_raw.getlist("X-Tenant-Id") == []
    assert from_mapping.get("x-tenant-id") == "a"


@pytest.mark.unit
def test_gunicorn_header_map_drop_dangerous_and_refuse_on_the_same_bytes() -> None:
    raw = render_request(ATTACK)

    assert gunicorn_environ(raw, "drop")["HTTP_X_TENANT_ID"] == "gw,gw2"
    assert gunicorn_environ(raw, "dangerous")["HTTP_X_TENANT_ID"] == "gw,victim,gw2"
    with pytest.raises(InvalidHeaderName):
        gunicorn_environ(raw, "refuse")


@pytest.mark.unit
def test_gunicorn_rejects_a_repeated_singleton_field() -> None:
    raw = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Type: a/b\r\nContent-Type: c/d\r\n\r\n"

    with pytest.raises(InvalidHeader):
        gunicorn_environ(raw, "drop")


@pytest.mark.unit
def test_underscore_transfer_encoding_reaches_the_wsgi_environ_only_in_dangerous_mode() -> None:
    raw = b"GET / HTTP/1.1\r\nHost: x\r\nTransfer_Encoding: chunked\r\n\r\n"

    assert gunicorn_environ(raw, "dangerous")["HTTP_TRANSFER_ENCODING"] == "chunked"
    assert "HTTP_TRANSFER_ENCODING" not in gunicorn_environ(raw, "drop")


@pytest.mark.unit
def test_gateway_overwrite_does_not_stop_the_underscore_twin_reaching_a_cgi_mapper() -> None:
    gateway_out = overwrite_trusted([(b"X_Tenant_Id", b"victim")], "X-Tenant-Id", "acme")
    wire = render_request(gateway_out)

    assert gunicorn_environ(wire, "dangerous")["HTTP_X_TENANT_ID"] == "victim,acme"
    assert gunicorn_environ(wire, "drop")["HTTP_X_TENANT_ID"] == "acme"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("parser", PARSERS)
async def test_asgi_backend_keeps_the_trusted_and_underscore_headers_apart(
    parser: UvicornParser,
) -> None:
    gateway_out = overwrite_trusted([(b"X_Tenant_Id", b"victim")], "X-Tenant-Id", "acme")

    result = await probe_through_uvicorn(render_request(gateway_out), parser)

    assert result["first"] == "acme"
    assert result["underscore"] == ["victim"]


@pytest.mark.unit
def test_underscore_policy_allow_drop_and_reject() -> None:
    assert apply_underscore_policy(ATTACK, UnderscoreAction.ALLOW) == ATTACK
    assert apply_underscore_policy(ATTACK, UnderscoreAction.DROP) == [ATTACK[0], ATTACK[2]]
    with pytest.raises(UnderscoreRejected):
        apply_underscore_policy(ATTACK, UnderscoreAction.REJECT)


@pytest.mark.unit
def test_dropping_underscores_at_the_edge_closes_the_cgi_merge() -> None:
    edge_out = apply_underscore_policy(ATTACK, UnderscoreAction.DROP)
    gateway_out = overwrite_trusted(edge_out, "X-Tenant-Id", "acme")

    assert gunicorn_environ(render_request(gateway_out), "dangerous")["HTTP_X_TENANT_ID"] == "acme"


@pytest.mark.unit
def test_nginx_http_variable_conflates_dash_and_underscore_and_joins_with_comma_space() -> None:
    headers: RawHeaders = [(b"X-Tenant-Id", b"a"), (b"X_Tenant_Id", b"b"), (b"X-Tenant-Id2", b"z")]

    assert nginx_http_variable(headers, "http_x_tenant_id") == "a, b"
    kept = apply_underscore_policy(headers, UnderscoreAction.DROP)
    assert nginx_http_variable(kept, "http_x_tenant_id") == "a"


@pytest.mark.unit
def test_five_stacks_read_the_same_duplicate_pair_five_ways() -> None:
    pair: RawHeaders = [(b"X-Tenant-Id", b"a"), (b"X-Tenant-Id", b"c")]
    httpx_headers = httpx.Headers([("X-Tenant-Id", "a"), ("X-Tenant-Id", "c")])

    readings = {
        "starlette_get": Headers(raw=[(k.lower(), v) for k, v in pair]).get("x-tenant-id"),
        "httpx_getitem": httpx_headers["x-tenant-id"],
        "rfc9110_combined": lookup_combined(pair, "x-tenant-id"),
        "nginx_variable": nginx_http_variable(pair, "http_x_tenant_id"),
        "gunicorn_environ": gunicorn_environ(render_request(pair), "drop")["HTTP_X_TENANT_ID"],
    }

    assert lookup_first(pair, "x-tenant-id") == "a"
    assert readings == {
        "starlette_get": "a",
        "httpx_getitem": "a, c",
        "rfc9110_combined": "a, c",
        "nginx_variable": "a, c",
        "gunicorn_environ": "a,c",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_header_param_maps_underscore_parameter_to_hyphen_wire_name() -> None:
    app = build_fastapi_app()

    assert (await asgi_get(app, "/converted", [("X-Tenant-Id", "a")]))["value"] == "a"
    assert (await asgi_get(app, "/converted", [("X_Tenant_Id", "victim")]))["value"] is None
    assert (await asgi_get(app, "/literal", [("X_Tenant_Id", "victim")]))["value"] == "victim"
    assert (await asgi_get(app, "/literal", [("X-Tenant-Id", "a")]))["value"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_scalar_header_param_takes_the_first_duplicate() -> None:
    app = build_fastapi_app()

    result = await asgi_get(app, "/converted", [("X-Tenant-Id", "a"), ("X-Tenant-Id", "c")])

    assert result["value"] == "a"


@pytest.mark.unit
def test_h2_passes_underscores_and_order_through_and_rejects_uppercase_names() -> None:
    delivered = h2_deliver([(b"x_tenant_id", b"b"), (b"x-tenant-id", b"a"), (b"x-tenant-id", b"c")])
    assert delivered[-3:] == [
        (b"x_tenant_id", b"b"),
        (b"x-tenant-id", b"a"),
        (b"x-tenant-id", b"c"),
    ]

    with pytest.raises(h2.exceptions.ProtocolError, match="uppercase"):
        h2_deliver([(b"X-Tenant-Id", b"a")], client_validates=False)


@pytest.mark.unit
def test_h2_joins_cookie_crumbs_with_semicolon_space_and_moves_them_last() -> None:
    delivered = h2_deliver([(b"cookie", b"a=1"), (b"x-tenant-id", b"t"), (b"cookie", b"b=2")])

    assert delivered[-2:] == [(b"x-tenant-id", b"t"), (b"cookie", b"a=1; b=2")]
    assert sum(1 for name, _ in delivered if name == b"cookie") == 1


@pytest.mark.unit
def test_h11_treats_underscore_framing_names_as_plain_headers_but_real_ones_as_framing() -> None:
    fake = h11.Connection(h11.SERVER)
    fake.receive_data(b"POST / HTTP/1.1\r\nHost: x\r\nContent_Length: 5\r\n\r\n")
    assert isinstance(fake.next_event(), h11.Request)
    assert isinstance(fake.next_event(), h11.EndOfMessage)

    real = h11.Connection(h11.SERVER)
    real.receive_data(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\n")
    assert isinstance(real.next_event(), h11.Request)
    assert real.next_event() is h11.NEED_DATA


@pytest.mark.unit
def test_h11_rejects_space_before_colon_and_replaces_obs_fold_with_space() -> None:
    bad = h11.Connection(h11.SERVER)
    bad.receive_data(b"GET / HTTP/1.1\r\nHost: x\r\nX-A : b\r\n\r\n")
    with pytest.raises(h11.RemoteProtocolError):
        bad.next_event()

    folded = h11.Connection(h11.SERVER)
    folded.receive_data(b"GET / HTTP/1.1\r\nHost: x\r\nX-A: b\r\n c\r\n\r\n")
    event = folded.next_event()
    assert isinstance(event, h11.Request)
    assert (b"x-a", b"b c") in list(event.headers)


@pytest.mark.unit
def test_connection_listed_trusted_header_survives_only_if_the_gateway_strips_first() -> None:
    attacker: RawHeaders = [(b"Connection", b"close, X-Tenant-Id")]

    sloppy_gateway = overwrite_trusted(attacker, "X-Tenant-Id", "acme")
    sloppy_backend_view = strip_connection_listed(sloppy_gateway)
    correct_gateway = overwrite_trusted(strip_connection_listed(attacker), "X-Tenant-Id", "acme")
    correct_backend_view = strip_connection_listed(correct_gateway)

    assert lookup_first(sloppy_backend_view, "x-tenant-id") is None
    assert lookup_first(correct_backend_view, "x-tenant-id") == "acme"
