"""Lab: TLS 1.3 session tickets, 0-RTT replay, and 425 Too Early (RFC 9846, RFC 8470)."""

from __future__ import annotations

import contextlib
import datetime
import ssl
from typing import TYPE_CHECKING

import httpx
import pytest

# aioquic.buffer re-exports Buffer without __all__, so strict checkers want the defining module.
from aioquic._buffer import Buffer as AioquicBuffer
from aioquic.tls import NewSessionTicket as AioquicNewSessionTicket
from aioquic.tls import pull_new_session_ticket, push_new_session_ticket
from cryptography.hazmat.primitives import serialization
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from api_labs.f02_tls_connections_and_intermediaries.early_data import (
    QUIC_MAX_EARLY_DATA_SENTINEL,
    TOO_EARLY_BODY,
    Attempt,
    GatewayAction,
    NewSessionTicket,
    ServerIdentity,
    TicketFormatError,
    TicketStore,
    TooEarlyMiddleware,
    allowlist_policy,
    client_ticket_age,
    decode_new_session_ticket,
    encode_new_session_ticket,
    gateway_forward_headers,
    gateway_on_425,
    is_fresh_client_hello,
    make_identity,
    obfuscate_ticket_age,
    obtain_ticket,
    replay_flight,
    user_agent_request,
    zero_rtt_request,
)

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request

BALANCE = b"GET /balance HTTP/1.1\r\nhost: api.test\r\n\r\n"
TRANSFER = b"POST /transfer HTTP/1.1\r\nhost: api.test\r\ncontent-length: 3\r\n\r\n100"

# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_new_session_ticket_wire_layout_is_byte_exact() -> None:
    nst = NewSessionTicket(
        ticket_lifetime=7200,
        ticket_age_add=0x01020304,
        ticket_nonce=b"\x00",
        ticket=b"T" * 4,
        max_early_data_size=16384,
    )
    assert encode_new_session_ticket(nst).hex(" ") == (
        "04 00 00 1a"  # new_session_ticket(4), uint24 length 26
        " 00 00 1c 20"  # ticket_lifetime 7200 s
        " 01 02 03 04"  # ticket_age_add
        " 01 00"  # ticket_nonce<0..255>
        " 00 04 54 54 54 54"  # ticket<1..2^16-1>
        " 00 08 00 2a 00 04 00 00 40 00"  # extensions: early_data(42), max_early_data_size
    )


@pytest.mark.unit
@pytest.mark.parametrize("max_early", [None, 0, 16384, QUIC_MAX_EARLY_DATA_SENTINEL])
def test_codec_matches_aioquic_byte_for_byte(max_early: int | None) -> None:
    ours = NewSessionTicket(86400, 0xDEADBEEF, b"", b"\xab" * 64, max_early)
    theirs = AioquicNewSessionTicket(
        ticket_lifetime=86400,
        ticket_age_add=0xDEADBEEF,
        ticket_nonce=b"",
        ticket=b"\xab" * 64,
        max_early_data_size=max_early,
    )
    buf = AioquicBuffer(capacity=512)
    push_new_session_ticket(buf, theirs)
    assert encode_new_session_ticket(ours) == buf.data
    assert decode_new_session_ticket(buf.data) == ours
    parsed = pull_new_session_ticket(AioquicBuffer(data=encode_new_session_ticket(ours)))
    assert parsed.max_early_data_size == max_early


@pytest.mark.unit
def test_lifetime_above_seven_days_is_rejected_on_both_sides() -> None:
    week = NewSessionTicket(604_800, 0, b"", b"t")
    assert decode_new_session_ticket(encode_new_session_ticket(week)) == week
    with pytest.raises(TicketFormatError, match="604800"):
        encode_new_session_ticket(NewSessionTicket(604_801, 0, b"", b"t"))
    tampered = bytearray(encode_new_session_ticket(week))
    tampered[4:8] = (604_801).to_bytes(4, "big")
    with pytest.raises(TicketFormatError, match="604800"):
        decode_new_session_ticket(bytes(tampered))


@pytest.mark.unit
def test_decoder_skips_unknown_extensions_and_rejects_truncation() -> None:
    body = bytes.fromhex("00000e10 00000000 00 0001 74")
    unknown_then_early = bytes.fromhex("000e" + "fafa0002ffff" + "002a0004" + "00001000")

    def frame(message: bytes) -> bytes:
        return b"\x04" + len(message).to_bytes(3, "big") + message

    assert decode_new_session_ticket(frame(body + unknown_then_early)) == NewSessionTicket(
        3600, 0, b"", b"t", 4096
    )
    with pytest.raises(TicketFormatError, match="truncated"):
        decode_new_session_ticket(frame(body + unknown_then_early[:-1]))
    with pytest.raises(TicketFormatError, match="length does not match"):
        decode_new_session_ticket(frame(body + unknown_then_early)[:-1])


@pytest.mark.unit
def test_obfuscated_age_wraps_modulo_2_32_and_round_trips() -> None:
    age_add = 0xFFFF_FF00
    obfuscated = obfuscate_ticket_age(1000, age_add)
    assert obfuscated == 1000 - 0x100  # wrapped past 2^32
    assert client_ticket_age(obfuscated, age_add) == 1000
    # The largest legal age (7 days in ms) still fits in 32 bits.
    assert 604_800 * 1000 < 2**32


@pytest.mark.unit
@pytest.mark.parametrize(
    ("client_age_ms", "now_ms", "fresh"),
    [
        (60_000, 160_000, True),  # arrives exactly when expected
        (60_000, 169_999, True),  # 9.999 s late, inside a 10 s window
        (60_000, 170_001, False),  # replayed 10.001 s later
        (60_000, 140_000, False),  # client clock far ahead
    ],
    ids=["on-time", "edge-late", "stale-replay", "future"],
)
def test_freshness_check_bounds_the_replay_window(
    client_age_ms: int, now_ms: int, fresh: bool
) -> None:
    age_add = 0x9E3779B9
    assert (
        is_fresh_client_hello(
            adjusted_creation_ms=100_000,
            obfuscated_ticket_age=obfuscate_ticket_age(client_age_ms, age_add),
            ticket_age_add=age_add,
            now_ms=now_ms,
            window_ms=10_000,
        )
        is fresh
    )


# ---------------------------------------------------------------------------
# Real 0-RTT through aioquic, in memory
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def identity() -> ServerIdentity:
    return make_identity()


@pytest.mark.unit
def test_quic_ticket_offers_0rtt_with_the_sentinel_size(identity: ServerIdentity) -> None:
    store = TicketStore(single_use=False)
    ticket = obtain_ticket(identity, store)
    assert ticket.max_early_data_size == QUIC_MAX_EARLY_DATA_SENTINEL
    assert list(store.tickets) == [ticket.ticket]
    lifetime = ticket.not_valid_after - ticket.not_valid_before
    assert lifetime == datetime.timedelta(seconds=86400)  # aioquic 1.7.2 hard-codes one day


@pytest.mark.unit
def test_genuine_0rtt_request_reaches_the_app_before_the_handshake_completes(
    identity: ServerIdentity,
) -> None:
    store = TicketStore(single_use=True)
    ticket = obtain_ticket(identity, store)
    run = zero_rtt_request(identity, store, ticket, TRANSFER, after_handshake=BALANCE)
    assert run.server.early_stream_data == (TRANSFER,)
    assert run.server.one_rtt_stream_data == (BALANCE,)  # same connection, after Finished
    assert run.server.handshake is not None
    assert run.server.handshake.session_resumed is True
    assert run.server.handshake.early_data_accepted is True
    assert len(run.flight) == 1
    assert len(run.flight[0]) == 1200  # client Initial datagrams are padded (RFC 9000 14.1)


@pytest.mark.unit
def test_replayed_flight_is_processed_again_when_tickets_are_reusable(
    identity: ServerIdentity,
) -> None:
    store = TicketStore(single_use=False)
    ticket = obtain_ticket(identity, store)
    run = zero_rtt_request(identity, store, ticket, TRANSFER)
    replays = [replay_flight(identity, store, run.flight) for _ in range(3)]
    for replay in replays:
        assert replay.early_stream_data == (TRANSFER,)  # the POST runs again
        assert replay.handshake is None  # and the attacker never finished a handshake


@pytest.mark.unit
def test_single_use_tickets_stop_the_replay_at_the_tls_layer(identity: ServerIdentity) -> None:
    store = TicketStore(single_use=True)
    ticket = obtain_ticket(identity, store)
    run = zero_rtt_request(identity, store, ticket, TRANSFER)
    assert run.server.early_stream_data == (TRANSFER,)
    replay = replay_flight(identity, store, run.flight)
    assert replay.early_stream_data == ()
    assert replay.handshake is None


@pytest.mark.unit
def test_replay_beats_single_use_tickets_when_instances_do_not_share_the_store(
    identity: ServerIdentity,
) -> None:
    issuing = TicketStore(single_use=True)
    ticket = obtain_ticket(identity, issuing)
    replica = TicketStore(single_use=True, tickets=dict(issuing.tickets))  # copied, not shared
    run = zero_rtt_request(identity, issuing, ticket, TRANSFER)
    assert replay_flight(identity, issuing, run.flight).early_stream_data == ()
    assert replay_flight(identity, replica, run.flight).early_stream_data == (TRANSFER,)


# ---------------------------------------------------------------------------
# CPython ssl: resumption yes, early data no
# ---------------------------------------------------------------------------


def _ssl_contexts(identity: ServerIdentity, tmp_path: Path) -> tuple[ssl.SSLContext, ...]:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_bytes(identity.certificate.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(
        identity.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    server.minimum_version = ssl.TLSVersion.TLSv1_3
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.load_verify_locations(cert)
    return server, client


def _memory_handshake(
    server_ctx: ssl.SSLContext, client_ctx: ssl.SSLContext, session: ssl.SSLSession | None
) -> tuple[ssl.SSLObject, ssl.SSLObject]:
    c_in, c_out, s_in, s_out = (ssl.MemoryBIO() for _ in range(4))
    client = client_ctx.wrap_bio(c_in, c_out, server_hostname="api.test", session=session)
    server = server_ctx.wrap_bio(s_in, s_out, server_side=True)
    for _ in range(4):
        for side in (client, server):
            with contextlib.suppress(ssl.SSLWantReadError):
                side.do_handshake()
        s_in.write(c_out.read())
        c_in.write(s_out.read())
    server.write(b"ok")  # NewSessionTicket rides after the server's Finished
    c_in.write(s_out.read())
    assert client.read(2) == b"ok"
    return client, server


@pytest.mark.unit
def test_cpython_ssl_resumes_tls13_but_has_no_early_data_api(
    identity: ServerIdentity, tmp_path: Path
) -> None:
    server_ctx, client_ctx = _ssl_contexts(identity, tmp_path)
    assert server_ctx.num_tickets == 2
    first, _ = _memory_handshake(server_ctx, client_ctx, None)
    assert first.version() == "TLSv1.3"
    assert first.session is not None
    assert first.session.has_ticket
    second_client, second_server = _memory_handshake(server_ctx, client_ctx, first.session)
    assert second_client.session_reused
    assert second_server.session_reused
    names = {n.lower() for n in [*dir(ssl), *dir(ssl.SSLObject), *dir(ssl.SSLContext)]}
    assert not {n for n in names if "early" in n}


@pytest.mark.unit
def test_openssl_server_accepts_the_same_ticket_twice(
    identity: ServerIdentity, tmp_path: Path
) -> None:
    server_ctx, client_ctx = _ssl_contexts(identity, tmp_path)
    first, _ = _memory_handshake(server_ctx, client_ctx, None)
    session = first.session
    assert session is not None
    reused = [_memory_handshake(server_ctx, client_ctx, session)[1].session_reused for _ in "ab"]
    assert reused == [True, True]


@pytest.mark.unit
def test_num_tickets_zero_disables_resumption(identity: ServerIdentity, tmp_path: Path) -> None:
    server_ctx, client_ctx = _ssl_contexts(identity, tmp_path)
    server_ctx.num_tickets = 0
    first, _ = _memory_handshake(server_ctx, client_ctx, None)
    assert first.session is not None
    assert not first.session.has_ticket
    second, _ = _memory_handshake(server_ctx, client_ctx, first.session)
    assert not second.session_reused


# ---------------------------------------------------------------------------
# Origin: 425 Too Early
# ---------------------------------------------------------------------------


class Ledger:
    def __init__(self) -> None:
        self.transfers = 0
        self.reports = 0


def make_origin(ledger: Ledger) -> Starlette:
    async def balance(request: Request) -> PlainTextResponse:
        return PlainTextResponse("balance=0", headers={"early-data": "1"})

    async def transfer(request: Request) -> PlainTextResponse:
        ledger.transfers += 1
        return PlainTextResponse("transferred", status_code=201)

    async def report(request: Request) -> PlainTextResponse:
        ledger.reports += 1  # a GET with a side effect: it enqueues an expensive job
        return PlainTextResponse("report queued")

    return Starlette(
        routes=[
            Route("/balance", balance, methods=["GET"]),
            Route("/transfer", transfer, methods=["POST"]),
            Route("/report", report, methods=["GET"]),
        ]
    )


def client_for(app: TooEarlyMiddleware | Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_early_post_gets_425_and_has_no_side_effect() -> None:
    ledger = Ledger()
    app = TooEarlyMiddleware(make_origin(ledger))
    async with client_for(app) as client:
        response = await client.post("/transfer", content=b"100", headers={"early-data": "1"})
    assert response.status_code == 425
    assert int(httpx.codes.TOO_EARLY) == 425
    assert response.content == TOO_EARLY_BODY
    assert ledger.transfers == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_without_the_middleware_the_early_post_runs() -> None:
    ledger = Ledger()
    async with client_for(make_origin(ledger)) as client:
        response = await client.post("/transfer", content=b"100", headers={"early-data": "1"})
    assert response.status_code == 201
    assert ledger.transfers == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_early_get_is_served_and_early_data_never_leaks_into_responses() -> None:
    app = TooEarlyMiddleware(make_origin(Ledger()))
    async with client_for(app) as client:
        response = await client.get("/balance", headers={"early-data": "1"})
    assert response.status_code == 200
    assert response.text == "balance=0"
    assert "early-data" not in response.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_after_handshake_is_processed_not_425() -> None:
    ledger = Ledger()
    app = TooEarlyMiddleware(make_origin(ledger))
    async with client_for(app) as client:
        response = await client.post("/transfer", content=b"100")
    assert response.status_code == 201
    assert ledger.transfers == 1


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [[("early-data", "0")], [("early-data", "yes")], [("early-data", "1"), ("early-data", "1")]],
    ids=["zero", "invalid", "duplicated"],
)
async def test_any_early_data_instance_counts_as_early(headers: list[tuple[str, str]]) -> None:
    ledger = Ledger()
    app = TooEarlyMiddleware(make_origin(ledger))
    async with client_for(app) as client:
        response = await client.post("/transfer", content=b"100", headers=headers)
    assert response.status_code == 425
    assert ledger.transfers == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_allowlist_policy_rejects_a_side_effecting_get() -> None:
    ledger = Ledger()
    app = TooEarlyMiddleware(make_origin(ledger), allowlist_policy([("GET", "/balance")]))
    async with client_for(app) as client:
        report = await client.get("/report", headers={"early-data": "1"})
        balance = await client.get("/balance", headers={"early-data": "1"})
    assert (report.status_code, balance.status_code) == (425, 200)
    assert ledger.reports == 0


# ---------------------------------------------------------------------------
# Hops: gateway and user agent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_gateway_adds_early_data_before_handshake_and_never_removes_it() -> None:
    host = [(b"host", b"api.test")]
    assert gateway_forward_headers(host, handshake_complete=False) == [*host, (b"early-data", b"1")]
    assert gateway_forward_headers(host, handshake_complete=True) == host
    marked = [*host, (b"Early-Data", b"1")]
    assert gateway_forward_headers(marked, handshake_complete=True) == marked
    assert gateway_forward_headers(marked, handshake_complete=False) == marked


@pytest.mark.unit
@pytest.mark.parametrize(
    ("had_header", "received_early", "action"),
    [
        (True, True, GatewayAction.FORWARD_425),
        (True, False, GatewayAction.FORWARD_425),
        (False, True, GatewayAction.RETRY_AFTER_HANDSHAKE),
        (False, False, GatewayAction.FORWARD_425),
    ],
)
def test_gateway_425_decision(had_header: bool, received_early: bool, action: str) -> None:
    decision = gateway_on_425(
        request_had_early_data_header=had_header, received_in_early_data=received_early
    )
    assert decision == action


@pytest.mark.unit
@pytest.mark.asyncio
async def test_user_agent_retries_425_once_and_never_in_early_data() -> None:
    ledger = Ledger()
    app = TooEarlyMiddleware(make_origin(ledger), allowlist_policy([("GET", "/balance")]))
    async with client_for(app) as client:
        report = await user_agent_request(client, "GET", "/report")
        balance = await user_agent_request(client, "GET", "/balance")
        transfer = await user_agent_request(client, "POST", "/transfer", content=b"100")
    assert report == [Attempt(early=True, status=425), Attempt(early=False, status=200)]
    assert balance == [Attempt(early=True, status=200)]
    assert transfer == [Attempt(early=False, status=201)]  # unsafe: never sent early
    assert (ledger.reports, ledger.transfers) == (1, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replayed_0rtt_post_is_rejected_by_the_origin_with_425(
    identity: ServerIdentity,
) -> None:
    store = TicketStore(single_use=False)
    ticket = obtain_ticket(identity, store)
    run = zero_rtt_request(identity, store, ticket, TRANSFER)
    ledger = Ledger()
    app = TooEarlyMiddleware(make_origin(ledger))
    statuses: list[int] = []
    async with client_for(app) as client:
        for flight in (run.flight, run.flight):
            view = replay_flight(identity, store, flight)
            (raw,) = view.early_stream_data
            method, path, _ = raw.split(b" ", 2)
            headers = gateway_forward_headers([], handshake_complete=view.handshake is not None)
            response = await client.request(
                method.decode(),
                path.decode(),
                content=raw.split(b"\r\n\r\n", 1)[1],
                headers=[(k.decode(), v.decode()) for k, v in headers],
            )
            statuses.append(response.status_code)
    assert statuses == [425, 425]
    assert ledger.transfers == 0
