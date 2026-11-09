"""Hop-by-hop stripping, the Connection header abuse, and how h11, h2, aioquic, werkzeug react."""

from __future__ import annotations

from functools import partial

import h2.config
import h2.connection
import h2.events
import h2.exceptions
import h11
import httpx
import pytest
from aioquic.h3.connection import MessageError, validate_request_headers
from hyperframe.frame import Frame, GoAwayFrame
from werkzeug.http import remove_hop_by_hop_headers

from api_labs.f02_tls_connections_and_intermediaries.hop_by_hop import (
    ConnectionOptionError,
    build_backend,
    build_edge_proxy,
    connection_options,
    hardened_forward,
    naive_forward,
    relay_connection_verbatim,
    strip_hop_by_hop,
)

ATTACK = {"Connection": "keep-alive, X-Internal-Auth"}


def _names(headers: list[tuple[str, str]]) -> list[str]:
    return [name.lower() for name, _ in headers]


@pytest.mark.unit
def test_connection_options_merges_lines_folds_case_and_trims_ows() -> None:
    headers = [("Connection", "Keep-Alive ,\tX-Internal-Auth"), ("connection", ", close,")]
    assert connection_options(headers) == {"keep-alive", "x-internal-auth", "close"}


@pytest.mark.unit
def test_connection_options_rejects_a_non_token_option() -> None:
    with pytest.raises(ConnectionOptionError, match="not a token"):
        connection_options([("Connection", "close, x-a b")])


@pytest.mark.unit
def test_connection_options_accepts_every_tchar_and_rejects_delimiters() -> None:
    tchars = "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for char in tchars:
        assert connection_options([("Connection", f"a{char}b")]) == {f"a{char}b".lower()}
    for char in '"():;<=>?@[\\]{}/':  # delimiters except ",", which separates list elements
        with pytest.raises(ConnectionOptionError, match="not a token"):
            connection_options([("Connection", f"a{char}b")])


@pytest.mark.unit
def test_strip_removes_known_and_nominated_fields_and_keeps_end_to_end_ones() -> None:
    headers = [
        ("Connection", "close, X-Trace-Hop"),
        ("Keep-Alive", "timeout=5"),
        ("Proxy-Connection", "keep-alive"),
        ("TE", "trailers"),
        ("Transfer-Encoding", "chunked"),
        ("Upgrade", "websocket"),
        ("X-Trace-Hop", "edge-1"),
        ("Accept", "application/json"),
        ("Cache-Control", "no-cache"),
    ]
    assert strip_hop_by_hop(headers) == [
        ("Accept", "application/json"),
        ("Cache-Control", "no-cache"),
    ]


@pytest.mark.unit
def test_naive_forward_lets_the_client_delete_the_edge_identity() -> None:
    forwarded = naive_forward([("Accept", "*/*"), *ATTACK.items()], "anonymous")
    assert _names(forwarded) == ["accept"]


@pytest.mark.unit
def test_hardened_forward_strips_first_drops_spoofs_and_stamps_last() -> None:
    attack = [*ATTACK.items(), ("X-Internal-Auth", "admin"), ("Accept", "*/*")]
    spoof = [("X-Internal-Auth", "admin"), ("Accept", "*/*")]
    expected = [("Accept", "*/*"), ("x-internal-auth", "anonymous")]
    assert hardened_forward(attack, "anonymous") == expected
    assert hardened_forward(spoof, "anonymous") == expected


@pytest.mark.unit
@pytest.mark.parametrize("field", ["Host", "Content-Length", "Authorization", "Cookie"])
def test_hardened_forward_refuses_end_to_end_connection_options(field: str) -> None:
    inbound = [("Connection", f"close, {field}"), (field, "value")]
    assert field.lower() not in _names(naive_forward(inbound, "anonymous"))
    with pytest.raises(ConnectionOptionError, match=field.lower()):
        hardened_forward(inbound, "anonymous")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_endpoints_do_not_strip_nominated_fields() -> None:
    backend = build_backend(fail_closed=False)
    transport = httpx.ASGITransport(app=backend)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin", headers={**ATTACK, "X-Internal-Auth": "anonymous"})
        visible = await client.get("/admin", headers={**ATTACK, "X-Internal-Auth": "billing"})
    assert response.status_code == 403
    assert visible.status_code == 200
    assert {"connection", "x-internal-auth"} <= set(visible.json()["seen"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naive_edge_and_fail_open_backend_escalate_to_service_role() -> None:
    proxy = build_edge_proxy(
        build_backend(fail_closed=False), partial(naive_forward, identity="anonymous")
    )
    transport = httpx.ASGITransport(app=proxy)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        baseline = await client.get("/admin")
        attacked = await client.get("/admin", headers=ATTACK)
    assert baseline.status_code == 403
    assert attacked.status_code == 200
    assert attacked.json()["identity"] == "mesh-service"
    assert "x-internal-auth" not in attacked.json()["seen"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hardened_edge_keeps_identity_and_rejects_end_to_end_options() -> None:
    proxy = build_edge_proxy(
        build_backend(fail_closed=False), partial(hardened_forward, identity="anonymous")
    )
    transport = httpx.ASGITransport(app=proxy)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        attacked = await client.get("/admin", headers=ATTACK)
        refused = await client.get(
            "/admin", headers={"Connection": "close, Authorization", "Authorization": "Bearer t"}
        )
    assert attacked.status_code == 403
    assert refused.status_code == 400
    assert refused.headers["connection"] == "close"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fail_closed_backend_turns_the_stripped_identity_into_401() -> None:
    proxy = build_edge_proxy(
        build_backend(fail_closed=True), partial(naive_forward, identity="anonymous")
    )
    transport = httpx.ASGITransport(app=proxy)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        attacked = await client.get("/admin", headers=ATTACK)
    assert attacked.status_code == 401


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_compliant_looking_hops_lose_the_stamp_when_the_front_relays_connection() -> None:
    backend = build_backend(fail_closed=False)
    inner = build_edge_proxy(backend, strip_hop_by_hop)
    leaky = build_edge_proxy(inner, partial(relay_connection_verbatim, identity="anonymous"))
    safe = build_edge_proxy(inner, partial(hardened_forward, identity="anonymous"))
    results: list[int] = []
    for front in (leaky, safe):
        transport = httpx.ASGITransport(app=front)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            results.append((await client.get("/admin", headers=ATTACK)).status_code)
    assert results == [200, 403]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("connection", "after_response"),
    [(b"keep-alive, X-Internal-Auth", h11.DONE), (b"close, X-Internal-Auth", h11.MUST_CLOSE)],
)
def test_h11_endpoint_keeps_nominated_fields_and_only_close_ends_reuse(
    connection: bytes, after_response: type[h11.DONE] | type[h11.MUST_CLOSE]
) -> None:
    server = h11.Connection(h11.SERVER)
    server.receive_data(
        b"GET /admin HTTP/1.1\r\nHost: api.test\r\nConnection: " + connection + b"\r\n"
        b"X-Internal-Auth: anonymous\r\n\r\n"
    )
    request = server.next_event()
    assert isinstance(request, h11.Request)
    assert (b"x-internal-auth", b"anonymous") in list(request.headers)
    assert isinstance(server.next_event(), h11.EndOfMessage)
    server.send(h11.Response(status_code=204, headers=[]))
    server.send(h11.EndOfMessage())
    assert server.our_state is after_response


def _h2_pair(
    *, normalize: bool = True, validate: bool = True
) -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    config = h2.config.H2Configuration(
        client_side=True, normalize_outbound_headers=normalize, validate_outbound_headers=validate
    )
    client = h2.connection.H2Connection(config)
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    client.data_to_send()
    server.data_to_send()
    return client, server


_H2_REQUEST = [
    (b":method", b"GET"),
    (b":scheme", b"https"),
    (b":authority", b"api.test"),
    (b":path", b"/admin"),
    (b"connection", b"keep-alive, x-internal-auth"),
    (b"x-internal-auth", b"anonymous"),
]


@pytest.mark.unit
def test_h2_normalization_drops_connection_but_forwards_the_nominated_field() -> None:
    client, server = _h2_pair()
    client.send_headers(1, _H2_REQUEST, end_stream=True)
    events = server.receive_data(client.data_to_send())
    received = next(e for e in events if isinstance(e, h2.events.RequestReceived))
    names = [name for name, _ in received.headers or []]
    assert b"connection" not in names
    assert b"x-internal-auth" in names


@pytest.mark.unit
def test_h2_rejects_connection_field_on_send_and_answers_receipt_with_goaway() -> None:
    strict_client, _ = _h2_pair(normalize=False)
    with pytest.raises(h2.exceptions.ProtocolError, match="Connection-specific"):
        strict_client.send_headers(1, _H2_REQUEST, end_stream=True)

    raw_client, server = _h2_pair(normalize=False, validate=False)
    raw_client.send_headers(1, _H2_REQUEST, end_stream=True)
    with pytest.raises(h2.exceptions.ProtocolError, match="Connection-specific"):
        server.receive_data(raw_client.data_to_send())
    wire = memoryview(server.data_to_send())
    frame, length = Frame.parse_frame_header(wire[:9])
    frame.parse_body(wire[9 : 9 + length])
    assert isinstance(frame, GoAwayFrame)
    assert frame.error_code == 1  # PROTOCOL_ERROR


@pytest.mark.unit
@pytest.mark.parametrize(("te", "accepted"), [(b"trailers", True), (b"gzip", False)])
def test_h2_allows_te_only_as_trailers(te: bytes, accepted: bool) -> None:
    client, _ = _h2_pair()
    headers = [*_H2_REQUEST[:4], (b"te", te)]
    if accepted:
        client.send_headers(1, headers, end_stream=True)
    else:
        with pytest.raises(h2.exceptions.ProtocolError, match="TE header"):
            client.send_headers(1, headers, end_stream=True)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "value"),
    [
        (b"connection", b"keep-alive"),
        (b"keep-alive", b"timeout=5"),
        (b"proxy-connection", b"keep-alive"),
        (b"transfer-encoding", b"chunked"),
        (b"upgrade", b"websocket"),
        (b"te", b"gzip"),
    ],
)
def test_aioquic_h3_treats_connection_specific_fields_as_malformed(
    name: bytes, value: bytes
) -> None:
    base = [
        (b":method", b"GET"),
        (b":scheme", b"https"),
        (b":authority", b"api.test"),
        (b":path", b"/"),
    ]
    validate_request_headers([*base, (b"te", b"trailers")])
    with pytest.raises(MessageError):
        validate_request_headers([*base, (name, value)])


@pytest.mark.unit
def test_werkzeug_static_list_ignores_connection_nominations() -> None:
    headers = [
        ("Connection", "close, X-Internal-Auth"),
        ("X-Internal-Auth", "edge"),
        ("Keep-Alive", "5"),
    ]
    remove_hop_by_hop_headers(headers)
    assert headers == [("X-Internal-Auth", "edge")]
