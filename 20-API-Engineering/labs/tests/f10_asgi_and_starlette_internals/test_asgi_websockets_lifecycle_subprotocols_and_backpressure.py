"""WebSockets in ASGI and Starlette: handshake, subprotocols, RFC 6455 close codes, backpressure."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import anyio
import httpx
import pytest
import wsproto
import wsproto.events
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketDisconnected
from websockets.client import ClientProtocol
from websockets.exceptions import NegotiationError, ProtocolError
from websockets.frames import Close
from websockets.protocol import Protocol, Side, State
from websockets.typing import Subprotocol
from websockets.uri import parse_uri
from websockets.utils import accept_key as websockets_accept_key
from wsproto.frame_protocol import FrameProtocol
from wsproto.utilities import LocalProtocolError, generate_accept_token

from api_labs.f10_asgi_and_starlette_internals.websocket_hub import (
    Hub,
    Policy,
    accept_key,
    build_app,
    connect,
    is_sendable_close_code,
    select_subprotocol,
)

if TYPE_CHECKING:
    from starlette.types import Message

RFC_KEY = "dGhlIHNhbXBsZSBub25jZQ=="  # RFC 6455 Section 4.2.2 example
V1, V2 = "chat.v1.example.com", "chat.v2.example.com"
POLICY = Policy(
    allowed_origins=frozenset({"https://chat.example.com"}),
    subprotocols=(V2, V1),
    max_message_chars=64,
    close_timeout=0.05,
)


def _fresh(outbox_size: int = 8) -> tuple[Hub, Starlette]:
    hub = Hub(outbox_size=outbox_size)
    return hub, build_app(hub, POLICY)


def _json(message: Message) -> Any:
    assert message["type"] == "websocket.send"
    return json.loads(message["text"])


# --- RFC 6455 wire rules, checked against the two sans-io stacks Uvicorn can use -------------


@pytest.mark.unit
def test_accept_key_matches_rfc_6455_example_and_both_libraries() -> None:
    assert accept_key(RFC_KEY) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    assert websockets_accept_key(RFC_KEY) == accept_key(RFC_KEY)
    assert generate_accept_token(RFC_KEY.encode()).decode() == accept_key(RFC_KEY)


@pytest.mark.unit
def test_sendable_close_codes_match_websockets_frame_validation() -> None:
    def websockets_allows(code: int) -> bool:
        try:
            Close(code, "").check()
        except ProtocolError:
            return False
        return True

    mismatches = [c for c in range(5100) if is_sendable_close_code(c) != websockets_allows(c)]
    assert mismatches == []
    assert [is_sendable_close_code(c) for c in (1004, 1005, 1006, 1015)] == [False] * 4
    assert [is_sendable_close_code(c) for c in (1012, 1013, 1014, 2999, 3000, 4999)] == [
        True,
        True,
        True,
        False,
        True,
        True,
    ]


@pytest.mark.unit
def test_close_frame_bytes_server_unmasked_client_masked() -> None:
    server = Protocol(Side.SERVER, state=State.OPEN)
    server.send_close(1000, "")
    assert server.data_to_send() == [b"\x88\x02\x03\xe8"]  # FIN+opcode 8, len 2, code 1000

    client = Protocol(Side.CLIENT, state=State.OPEN)
    client.send_close(1000, "")
    (frame,) = client.data_to_send()
    assert frame[0] == 0x88
    assert frame[1] == 0x80 | 2  # MASK bit set, payload length 2
    mask, masked = frame[2:6], frame[6:]
    assert bytes(b ^ mask[i % 4] for i, b in enumerate(masked)) == b"\x03\xe8"


@pytest.mark.unit
def test_reserved_close_codes_raise_in_websockets_but_are_rewritten_by_wsproto() -> None:
    server = Protocol(Side.SERVER, state=State.OPEN)
    with pytest.raises(ProtocolError, match="invalid status code"):
        server.send_close(1006, "")
    frames = FrameProtocol(client=False, extensions=[])
    assert bytes(frames.close(code=1006)) == b"\x88\x02\x03\xe8"  # silently becomes 1000
    assert bytes(frames.close(code=2000)) == b"\x88\x02\x07\xd0"  # unregistered, still sent
    assert not is_sendable_close_code(2000)


@pytest.mark.unit
def test_close_reason_is_capped_at_123_bytes() -> None:
    ok = Protocol(Side.SERVER, state=State.OPEN)
    ok.send_close(1000, "x" * 123)
    assert len(ok.data_to_send()[0]) == 2 + 125
    too_long = Protocol(Side.SERVER, state=State.OPEN)
    with pytest.raises(ProtocolError, match="control frame too long"):
        too_long.send_close(1000, "x" * 124)
    truncated = bytes(FrameProtocol(client=False, extensions=[]).close(code=1000, reason="x" * 200))
    assert truncated[1] == 125
    assert truncated[4:] == b"x" * 123


# --- Opening handshake through Starlette 1.10.0 ---------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("offered", "expected"),
    [
        ([V1, V2], V2),  # server preference wins over client order
        ([V1], V1),
        (["mqtt"], None),
        ([], None),
    ],
)
def test_select_subprotocol_uses_server_preference_among_offers(
    offered: list[str], expected: str | None
) -> None:
    assert select_subprotocol(offered, POLICY.subprotocols) == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_accept_carries_the_negotiated_subprotocol() -> None:
    hub, app = _fresh()
    async with connect(app, subprotocols=[V1, V2]) as ws:
        accept = await ws.next_message()
        assert accept == {"type": "websocket.accept", "subprotocol": V2, "headers": []}
        assert list(hub.subscribers) == [1]
        await ws.disconnect()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["https://evil.example", None])
async def test_disallowed_origin_is_closed_before_accept(origin: str | None) -> None:
    hub, app = _fresh()
    async with connect(app, origin=origin, subprotocols=[V1]) as ws:
        first = await ws.next_message()
        assert first["type"] == "websocket.close"  # pre-accept close -> server sends HTTP 403
        await ws.app_done.wait()
        assert ws.pending() == []
    assert hub.subscribers == {}
    assert hub.evictions == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_common_subprotocol_uses_the_denial_response_extension() -> None:
    _, app = _fresh()
    async with connect(app, subprotocols=["mqtt"]) as ws:
        start = await ws.next_message()
        body = await ws.next_message()
    assert start["type"] == "websocket.http.response.start"
    assert start["status"] == 400
    assert body["type"] == "websocket.http.response.body"
    assert body["body"] == b"no common subprotocol"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_common_subprotocol_without_the_extension_falls_back_to_close() -> None:
    _, app = _fresh()
    async with connect(app, subprotocols=["mqtt"], denial_extension=False) as ws:
        first = await ws.next_message()
    assert first["type"] == "websocket.close"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_passes_an_unoffered_subprotocol_through_unchecked() -> None:
    async def careless(websocket: WebSocket) -> None:
        await websocket.accept(subprotocol="mqtt")
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/ws", careless)])
    async with connect(app, subprotocols=[V1]) as ws:
        accept = await ws.next_message()
    assert accept["subprotocol"] == "mqtt"


@pytest.mark.unit
def test_protocol_layer_rejects_an_unoffered_subprotocol() -> None:
    server = wsproto.WSConnection(wsproto.ConnectionType.SERVER)
    server.receive_data(
        b"GET /ws HTTP/1.1\r\nHost: chat.example.com\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\nSec-WebSocket-Key: " + RFC_KEY.encode() + b"\r\n"
        b"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: " + V1.encode() + b"\r\n\r\n"
    )
    (request,) = list(server.events())
    assert isinstance(request, wsproto.events.Request)
    assert request.subprotocols == [V1]
    with pytest.raises(LocalProtocolError, match="unexpected subprotocol mqtt"):
        server.send(wsproto.events.AcceptConnection(subprotocol="mqtt"))

    client = ClientProtocol(parse_uri("ws://chat.example.com/ws"), subprotocols=[Subprotocol(V1)])
    handshake = client.connect()
    client.send_request(handshake)
    key = handshake.headers["Sec-WebSocket-Key"]
    client.receive_data(
        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + accept_key(key).encode() + b"\r\n"
        b"Sec-WebSocket-Protocol: mqtt\r\n\r\n"
    )
    assert isinstance(client.handshake_exc, NegotiationError)
    assert client.state is State.CONNECTING  # never reaches OPEN


# --- Concurrent send and receive loops ------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_full_duplex_pushes_broadcasts_without_client_traffic() -> None:
    _, app = _fresh()
    async with connect(app, subprotocols=[V1]) as listener, connect(app, subprotocols=[V1]) as pub:
        await listener.next_message()
        await pub.next_message()
        await pub.send_text(json.dumps({"op": "publish", "text": "hi"}))
        pushed = _json(await listener.next_message())
        assert pushed == {"op": "message", "from": 2, "text": "hi"}
        await pub.disconnect()
        await listener.disconnect()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naive_single_loop_withholds_broadcasts_until_the_client_speaks() -> None:
    _, app = _fresh()
    async with connect(app, "/naive") as naive, connect(app, subprotocols=[V1]) as pub:
        await naive.next_message()
        await pub.next_message()
        await pub.send_text(json.dumps({"op": "publish", "text": "hi"}))
        assert _json(await pub.next_message())["op"] == "message"
        await anyio.wait_all_tasks_blocked()
        assert naive.pending() == []  # the broadcast sits in the outbox
        await naive.send_text(json.dumps({"op": "ping"}))
        late = [_json(await naive.next_message()) for _ in range(2)]
        assert late == [{"op": "message", "from": 2, "text": "hi"}, {"op": "pong"}]
        await pub.disconnect()
        await naive.disconnect()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_slow_consumer_is_evicted_with_1013_and_never_blocks_the_publisher() -> None:
    hub, app = _fresh(outbox_size=2)
    async with (
        connect(app, subprotocols=[V1], client_buffer=0) as slow,
        connect(app, subprotocols=[V1]) as pub,
    ):
        await slow.next_message()  # accept; afterwards the slow client never reads again
        await pub.next_message()
        acks: list[int] = []
        for n in range(4):
            await pub.send_text(json.dumps({"op": "publish", "text": str(n)}))
            assert _json(await pub.next_message())["op"] == "message"
            acks.append(_json(await pub.next_message())["delivered"])
            await anyio.wait_all_tasks_blocked()
        # 1 message blocked in the writer's send + 2 buffered; the 4th overflows the outbox.
        assert acks == [2, 2, 2, 1]
        assert hub.evictions == [(1, 1013)]
        with anyio.fail_after(1):
            await slow.app_done.wait()  # close() gave up after close_timeout; TCP would drop
        assert slow.pending() == []
        assert list(hub.subscribers) == [2]
        await pub.disconnect()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "code", "reason"),
    [
        (b"\x00\x01", 1003, "text frames only"),
        ("not json", 1008, "malformed JSON"),
        ('{"op": "nope"}', 1008, "unknown op"),
        ("x" * 65, 1009, "limit 64 chars"),
    ],
    ids=["binary", "malformed", "unknown-op", "too-big"],
)
async def test_bad_input_maps_to_its_rfc_6455_close_code(
    payload: str | bytes, code: int, reason: str
) -> None:
    hub, app = _fresh()
    async with connect(app, subprotocols=[V1]) as ws:
        await ws.next_message()
        if isinstance(payload, bytes):
            await ws.send_bytes(payload)
        else:
            await ws.send_text(payload)
        close = await ws.next_message()
    assert close == {"type": "websocket.close", "code": code, "reason": reason}
    assert hub.subscribers == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_disconnect_cancels_the_writer_and_unregisters() -> None:
    hub, app = _fresh()
    async with connect(app, subprotocols=[V1]) as ws:
        await ws.next_message()
        await ws.disconnect(1001)
        with anyio.fail_after(1):
            await ws.app_done.wait()
        assert ws.pending() == []  # no Close is sent back after websocket.disconnect
    assert hub.subscribers == {}
    assert hub.broadcast("late") == 0


# --- Starlette's WebSocket state machine ----------------------------------------------------


def _bare_scope() -> dict[str, Any]:
    return {"type": "websocket", "path": "/ws", "headers": [], "query_string": b""}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_refuses_data_before_accept() -> None:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    websocket = WebSocket(_bare_scope(), receive, send)
    with pytest.raises(RuntimeError, match=r'Expected ASGI message "websocket\.accept"'):
        await websocket.send_text("too early")
    assert sent == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_maps_a_server_oserror_on_send_to_1006() -> None:
    events: list[Message] = [{"type": "websocket.connect"}]

    async def receive() -> Message:
        return events.pop(0)

    async def send(message: Message) -> None:
        if message["type"] == "websocket.send":
            raise OSError("peer gone")  # Uvicorn raises ClientDisconnected(OSError)

    websocket = WebSocket(_bare_scope(), receive, send)
    await websocket.accept()
    with pytest.raises(WebSocketDisconnect) as excinfo:
        await websocket.send_text("hello")
    assert excinfo.value.code == 1006
    with pytest.raises(WebSocketDisconnected, match='Cannot call "send"'):
        await websocket.send_text("again")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_refuses_receive_after_disconnect() -> None:
    events: list[Message] = [
        {"type": "websocket.connect"},
        {"type": "websocket.disconnect", "code": 1001, "reason": ""},
    ]

    async def receive() -> Message:
        return events.pop(0)

    async def send(message: Message) -> None:
        return None

    websocket = WebSocket(_bare_scope(), receive, send)
    await websocket.accept()
    with pytest.raises(WebSocketDisconnect) as excinfo:
        await websocket.receive_text()
    assert excinfo.value.code == 1001
    with pytest.raises(WebSocketDisconnected, match='Cannot call "receive"'):
        await websocket.receive()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpx_asgi_transport_reaches_a_websocket_route_only_as_http_404() -> None:
    _, app = _fresh()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/ws", headers={"upgrade": "websocket"})
    assert response.status_code == 404
