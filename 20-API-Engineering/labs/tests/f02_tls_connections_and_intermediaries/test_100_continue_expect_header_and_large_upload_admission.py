"""Lab: Expect: 100-continue admission rejects bad uploads before the body is sent."""

from __future__ import annotations

import http
from typing import TYPE_CHECKING, Annotated, Literal

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions
import h11
import httpx
import pytest
from fastapi import Body, Depends, FastAPI, Header, HTTPException
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from api_labs.f02_tls_connections_and_intermediaries.expect_continue import (
    BodyTooLargeError,
    ClientMode,
    ExpectAdmissionMiddleware,
    UploadPolicy,
    UploadServer,
    Verdict,
    admit,
    answer_head,
    drive_hypercorn,
    drive_uvicorn,
    make_upload_app,
    simulate_upload,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.requests import Request
    from starlette.types import Message, Receive, Scope, Send

MIB = 1024 * 1024
FIFTY_MIB = 50 * MIB
TOKEN = b"s3cret"
AUTH = (b"authorization", b"Bearer " + TOKEN)
POLICY = UploadPolicy(max_content_length=10 * MIB, bearer_tokens=frozenset({TOKEN}))
SMALL = UploadPolicy(max_content_length=MIB, bearer_tokens=frozenset({TOKEN}))
CONTINUE_WIRE = b"HTTP/1.1 100 Continue\r\n\r\n"


def upload_head(*extra: bytes, length: int = FIFTY_MIB, version: bytes = b"1.1") -> bytes:
    lines = [b"PUT /uploads HTTP/" + version, b"Host: uploads.example"]
    lines += [b"Content-Length: " + str(length).encode(), *extra]
    return b"\r\n".join(lines) + b"\r\n\r\n"


def server_request(head: bytes) -> tuple[h11.Connection, h11.Request]:
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(head)
    event = conn.next_event()
    assert isinstance(event, h11.Request)
    return conn, event


@pytest.mark.unit
@pytest.mark.parametrize(
    ("expect_line", "version", "waiting"),
    [
        (b"Expect: 100-continue", b"1.1", True),
        (b"Expect: 100-Continue", b"1.1", True),
        (b"Expect: foo, 100-continue", b"1.1", True),
        (b"Expect: y 100-continue", b"1.1", False),
        (b"Expect: 100-continue", b"1.0", False),
    ],
    ids=["exact", "case-insensitive", "list-member", "obfuscated", "http-1.0-ignored"],
)
def test_h11_flags_a_waiting_client_only_for_an_http11_100_continue_member(
    expect_line: bytes, version: bytes, waiting: bool
) -> None:
    conn, _ = server_request(upload_head(expect_line, version=version))
    assert conn.they_are_waiting_for_100_continue is waiting


@pytest.mark.unit
@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ([(b"expect", b"100-continue, x-fast-path")], 417),
        ([(b"content-length", b"1")], 401),
        ([(b"authorization", b"Bearer wrong"), (b"content-length", b"1")], 401),
        ([(b"authorization", b"Basic " + TOKEN), (b"content-length", b"1")], 401),
        ([(b"content-length", str(FIFTY_MIB).encode())], 401),
        ([AUTH, (b"content-length", str(10 * MIB + 1).encode())], 413),
        ([AUTH, (b"content-length", str(10 * MIB).encode())], 100),
        ([AUTH, (b"transfer-encoding", b"chunked")], 100),
        ([AUTH, (b"Expect", b"100-Continue"), (b"content-length", b"1")], 100),
    ],
    ids=[
        "unknown-expectation",
        "no-credentials",
        "wrong-token",
        "wrong-scheme",
        "anonymous-oversize-says-401-not-413",
        "one-byte-over",
        "exactly-at-limit",
        "unknown-length",
        "expectation-is-case-insensitive",
    ],
)
def test_admit_checks_expectation_then_identity_then_declared_size(
    headers: list[tuple[bytes, bytes]], status: int
) -> None:
    verdict = admit(headers, POLICY)
    assert verdict.status == status
    if status == 401:
        assert verdict.headers == ((b"www-authenticate", b'Bearer realm="uploads"'),)


@pytest.mark.unit
def test_waiting_client_sends_zero_body_bytes_of_an_unauthenticated_50_mib_upload() -> None:
    trace = simulate_upload(
        UploadServer(POLICY), body_size=FIFTY_MIB, headers=[], mode=ClientMode.EXPECT_WAIT
    )
    assert trace.interim_statuses == ()
    assert trace.final_status == 401
    assert trace.body_bytes_sent == 0


@pytest.mark.unit
def test_waiting_client_sends_zero_body_bytes_of_an_oversized_authenticated_upload() -> None:
    trace = simulate_upload(
        UploadServer(POLICY), body_size=FIFTY_MIB, headers=[AUTH], mode=ClientMode.EXPECT_WAIT
    )
    assert (trace.final_status, trace.body_bytes_sent) == (413, 0)


@pytest.mark.unit
def test_admitted_upload_gets_100_then_201_and_the_connection_is_reusable() -> None:
    server = UploadServer(POLICY)
    trace = simulate_upload(server, body_size=MIB, headers=[AUTH], mode=ClientMode.EXPECT_WAIT)
    assert trace.interim_statuses == (100,)
    assert (trace.final_status, trace.body_bytes_sent) == (201, MIB)
    assert server.body_bytes_received == MIB
    assert trace.server_reusable


@pytest.mark.unit
def test_admitted_upload_without_expect_gets_no_interim_response() -> None:
    trace = simulate_upload(
        UploadServer(POLICY), body_size=MIB, headers=[AUTH], mode=ClientMode.NO_EXPECT
    )
    assert trace.interim_statuses == ()
    assert trace.final_status == 201


@pytest.mark.unit
def test_client_without_expect_sends_the_whole_50_mib_before_reading_the_401() -> None:
    trace = simulate_upload(
        UploadServer(POLICY), body_size=FIFTY_MIB, headers=[], mode=ClientMode.NO_EXPECT
    )
    assert (trace.final_status, trace.body_bytes_sent) == (401, FIFTY_MIB)


@pytest.mark.unit
def test_client_that_stops_waiting_wastes_only_what_is_in_flight() -> None:
    trace = simulate_upload(
        UploadServer(POLICY),
        body_size=FIFTY_MIB,
        headers=[],
        mode=ClientMode.EXPECT_NO_WAIT,
        chunk_size=64 * 1024,
    )
    assert (trace.final_status, trace.body_bytes_sent) == (401, 64 * 1024)


@pytest.mark.unit
def test_early_401_without_connection_close_makes_a_go_style_client_send_everything() -> None:
    server = UploadServer(POLICY, close_on_reject=False)
    trace = simulate_upload(server, body_size=FIFTY_MIB, headers=[], mode=ClientMode.EXPECT_WAIT)
    assert (trace.final_status, trace.body_bytes_sent) == (401, FIFTY_MIB)
    assert server.body_bytes_received == FIFTY_MIB  # the server drained it to stay in sync
    assert trace.server_reusable


@pytest.mark.unit
def test_rejecting_with_close_leaves_h11_unable_to_reuse_the_connection() -> None:
    conn, request = server_request(upload_head(b"Expect: 100-continue"))
    verdict, wire = answer_head(conn, request, POLICY, close_on_reject=True)
    assert verdict == Verdict(401, ((b"www-authenticate", b'Bearer realm="uploads"'),))
    assert wire.startswith(b"HTTP/1.1 401 \r\n")
    assert b"connection: close\r\n" in wire
    assert (conn.our_state, conn.their_state) == (h11.MUST_CLOSE, h11.SEND_BODY)
    with pytest.raises(h11.LocalProtocolError, match="not in a reusable state"):
        conn.start_next_cycle()


@pytest.mark.unit
def test_rejecting_without_close_owes_the_peer_a_drain_before_reuse() -> None:
    conn, request = server_request(upload_head(b"Expect: 100-continue", length=1000))
    _, wire = answer_head(conn, request, POLICY, close_on_reject=False)
    assert b"connection: close" not in wire
    assert (conn.our_state, conn.their_state) == (h11.DONE, h11.SEND_BODY)
    with pytest.raises(h11.LocalProtocolError):
        conn.start_next_cycle()
    conn.receive_data(b"x" * 1000)
    assert isinstance(conn.next_event(), h11.Data)
    assert isinstance(conn.next_event(), h11.EndOfMessage)
    conn.start_next_cycle()
    assert conn.states == {h11.CLIENT: h11.IDLE, h11.SERVER: h11.IDLE}


@pytest.mark.unit
def test_server_reads_nothing_after_it_has_decided_to_close() -> None:
    server = UploadServer(POLICY)
    answer = server.feed(upload_head(b"Expect: 100-continue"))
    assert answer.startswith(b"HTTP/1.1 401 \r\n")
    assert server.conn.our_state is h11.MUST_CLOSE
    assert server.feed(b"x" * 4096) == b""
    assert server.body_bytes_received == 0
    assert server.conn.trailing_data == (b"", False)  # the late bytes were never buffered


@pytest.mark.unit
def test_chunked_upload_with_no_declared_length_is_capped_while_streaming() -> None:
    server = UploadServer(SMALL)
    trace = simulate_upload(
        server,
        body_size=4 * MIB,
        headers=[AUTH],
        mode=ClientMode.EXPECT_WAIT,
        chunked=True,
        chunk_size=64 * 1024,
    )
    assert trace.interim_statuses == (100,)
    assert trace.final_status == 413
    assert trace.body_bytes_sent == 17 * 64 * 1024  # the first chunk past 1 MiB trips the cap
    assert server.body_bytes_received == 17 * 64 * 1024
    assert not trace.server_reusable


@pytest.mark.unit
def test_chunked_upload_under_the_cap_completes_with_the_terminating_chunk() -> None:
    server = UploadServer(SMALL)
    trace = simulate_upload(
        server, body_size=MIB, headers=[AUTH], mode=ClientMode.EXPECT_WAIT, chunked=True
    )
    assert (trace.interim_statuses, trace.final_status) == ((100,), 201)
    assert server.body_bytes_received == MIB
    assert trace.server_reusable


@pytest.mark.unit
def test_h11_rejects_conflicting_content_length_before_any_admission_logic_runs() -> None:
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(b"PUT /u HTTP/1.1\r\nHost: x\r\nContent-Length: 1, 2\r\n\r\n")
    with pytest.raises(h11.RemoteProtocolError) as excinfo:
        conn.next_event()
    assert excinfo.value.error_status_hint == 400


async def reject_without_reading(scope: Scope, receive: Receive, send: Send) -> None:
    headers = [(b"content-length", b"0"), (b"connection", b"close")]
    await send({"type": "http.response.start", "status": 401, "headers": headers})
    await send({"type": "http.response.body", "body": b""})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uvicorn_never_writes_100_when_the_app_answers_before_receive() -> None:
    app = ExpectAdmissionMiddleware(reject_without_reading, POLICY)
    wire = await drive_uvicorn(app, upload_head(b"Expect: 100-continue"))
    assert wire.after_head.startswith(b"HTTP/1.1 401 Unauthorized\r\n")
    assert b"100 Continue" not in wire.total
    assert b"www-authenticate: Bearer" in wire.after_head
    assert wire.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uvicorn_writes_100_continue_on_the_apps_first_receive() -> None:
    app = make_upload_app(POLICY)
    head = upload_head(b"Authorization: Bearer s3cret", b"Expect: 100-continue", length=4)
    wire = await drive_uvicorn(app, head, body=b"data")
    assert wire.after_head == CONTINUE_WIRE
    assert wire.total.startswith(CONTINUE_WIRE + b"HTTP/1.1 201 Created\r\n")
    assert wire.total.endswith(b'{"received":4}')


async def read_then_accept(scope: Scope, receive: Receive, send: Send) -> None:
    while (await receive()).get("more_body", False):
        pass
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http", "expect_value", "after_head"),
    [
        ("h11", b"100-continue", CONTINUE_WIRE),
        ("httptools", b"100-continue", CONTINUE_WIRE),
        ("h11", b"100-continue, x-trace", CONTINUE_WIRE),
        ("httptools", b"100-continue, x-trace", b""),
    ],
    ids=["h11-exact", "httptools-exact", "h11-list", "httptools-list-not-recognized"],
)
async def test_uvicorn_parsers_disagree_on_an_expect_list(
    http: Literal["h11", "httptools"], expect_value: bytes, after_head: bytes
) -> None:
    head = upload_head(b"Expect: " + expect_value, length=4)
    wire = await drive_uvicorn(read_then_accept, head, body=b"data", http=http)
    assert wire.after_head == after_head
    assert wire.total.endswith(b"HTTP/1.1 204 No Content\r\n\r\n")


def require_bearer(authorization: Annotated[str | None, Header()] = None) -> None:
    if authorization != "Bearer s3cret":
        raise HTTPException(401, headers={"WWW-Authenticate": 'Bearer realm="uploads"'})


def make_fastapi_upload_app() -> FastAPI:
    api = FastAPI()

    @api.put("/uploads", status_code=201, dependencies=[Depends(require_bearer)])
    async def upload(
        payload: Annotated[bytes, Body(media_type="application/octet-stream")],
    ) -> dict[str, int]:
        return {"received": len(payload)}

    return api


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_body_parameter_invites_the_body_before_the_auth_dependency_runs() -> None:
    head = upload_head(b"Content-Type: application/octet-stream", b"Expect: 100-continue", length=4)
    wire = await drive_uvicorn(make_fastapi_upload_app(), head, body=b"data")
    assert wire.after_head == CONTINUE_WIRE  # body invited although credentials are missing
    assert wire.total.startswith(CONTINUE_WIRE + b"HTTP/1.1 401 Unauthorized\r\n")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_admission_middleware_in_front_of_fastapi_rejects_before_the_invitation() -> None:
    app = ExpectAdmissionMiddleware(make_fastapi_upload_app(), POLICY)
    head = upload_head(b"Content-Type: application/octet-stream", b"Expect: 100-continue")
    wire = await drive_uvicorn(app, head)
    assert wire.after_head.startswith(b"HTTP/1.1 401 Unauthorized\r\n")
    assert b"100 Continue" not in wire.total


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hypercorn_sends_100_before_the_app_can_reject() -> None:
    app = ExpectAdmissionMiddleware(reject_without_reading, POLICY)
    wire = await drive_hypercorn(app, upload_head(b"Expect: 100-continue"))
    assert wire.after_head == b"HTTP/1.1 100 \r\n\r\n"
    assert wire.total.startswith(wire.after_head + b"HTTP/1.1 401 \r\n")
    assert wire.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hypercorn_closes_after_an_early_response_even_without_connection_close() -> None:
    async def reject_keep_alive(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    wire = await drive_hypercorn(reject_keep_alive, upload_head())
    assert wire.total.startswith(b"HTTP/1.1 401 \r\n")
    assert b"connection: close" not in wire.total
    assert wire.closed  # body unread, so _maybe_recycle closes instead of draining
    complete = await drive_hypercorn(reject_keep_alive, upload_head(length=0))
    assert complete.total.startswith(b"HTTP/1.1 401 \r\n")
    assert not complete.closed  # nothing owed, so the same exchange recycles the connection


async def chunks(count: int, size: int, pulled: list[int]) -> AsyncIterator[bytes]:
    for _ in range(count):
        pulled.append(size)
        yield b"\0" * size


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_caps_a_streamed_body_and_stops_pulling_from_the_client() -> None:
    pulled: list[int] = []
    transport = httpx.ASGITransport(app=make_upload_app(SMALL))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/uploads",
            content=chunks(64, 64 * 1024, pulled),
            headers={"Authorization": "Bearer s3cret"},
        )
    assert response.status_code == 413
    assert response.headers["connection"] == "close"
    assert sum(pulled) == 17 * 64 * 1024


async def echo_length(request: Request) -> JSONResponse:
    return JSONResponse({"received": len(await request.body())}, status_code=201)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_wrapped_outside_starlette_loses_the_413_to_a_500() -> None:
    inner = Starlette(routes=[Route("/uploads", echo_length, methods=["PUT"])])
    app = ExpectAdmissionMiddleware(inner, SMALL)  # outside ServerErrorMiddleware
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/uploads",
            content=chunks(64, 64 * 1024, []),
            headers={"Authorization": "Bearer s3cret"},
        )
    assert response.status_code == 500


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_max_body_size_answers_413_before_100_but_keeps_the_connection() -> None:
    inner = Starlette(routes=[Route("/uploads", echo_length, methods=["PUT"])], max_body_size=MIB)
    wire = await drive_uvicorn(inner, upload_head(b"Expect: 100-continue"))
    phrase = http.HTTPStatus(413).phrase.encode()  # "Request Entity Too Large" before 3.13
    assert wire.after_head.startswith(b"HTTP/1.1 413 " + phrase + b"\r\n")
    assert b"100 Continue" not in wire.total
    assert b"connection: close" not in wire.total
    assert not wire.closed  # a Go client would now send all 50 MiB for Uvicorn to drain


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_passes_an_admitted_upload_through() -> None:
    transport = httpx.ASGITransport(app=make_upload_app(SMALL))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/uploads", content=b"x" * 1000, headers={"Authorization": "Bearer s3cret"}
        )
    assert (response.status_code, response.json()) == (201, {"received": 1000})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_never_replaces_a_response_that_already_started() -> None:
    async def stream_then_read(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while (await receive()).get("more_body", False):
            pass

    app = ExpectAdmissionMiddleware(stream_then_read, SMALL)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(BodyTooLargeError):
            await client.put(
                "/", content=chunks(32, 64 * 1024, []), headers={"Authorization": "Bearer s3cret"}
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_passes_non_http_scopes_untouched() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        raise AssertionError(message)

    await ExpectAdmissionMiddleware(inner, SMALL)({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]


@pytest.mark.unit
def test_h2_server_ends_the_upload_early_and_keeps_the_connection() -> None:
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    request = [(":method", "PUT"), (":path", "/uploads"), (":scheme", "https")]
    request += [(":authority", "uploads.example"), ("content-length", str(FIFTY_MIB))]
    client.send_headers(1, request)
    events = server.receive_data(client.data_to_send())
    assert any(isinstance(e, h2.events.RequestReceived) for e in events)
    response = [(":status", "401"), ("www-authenticate", 'Bearer realm="uploads"')]
    server.send_headers(1, response, end_stream=True)
    server.reset_stream(1, error_code=h2.errors.ErrorCodes.NO_ERROR)
    events = client.receive_data(server.data_to_send())
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["ResponseReceived", "StreamEnded", "StreamReset"]
    reset = events[-1]
    assert isinstance(reset, h2.events.StreamReset)
    assert reset.error_code == h2.errors.ErrorCodes.NO_ERROR
    with pytest.raises(h2.exceptions.StreamClosedError):
        client.send_data(1, b"\0" * 1024)
    client.send_headers(3, [*request[:4]], end_stream=True)
    events = server.receive_data(client.data_to_send())
    assert [e.stream_id for e in events if isinstance(e, h2.events.RequestReceived)] == [3]
