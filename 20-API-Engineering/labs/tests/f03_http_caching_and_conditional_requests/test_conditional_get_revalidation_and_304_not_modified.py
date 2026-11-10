"""Conditional GET precedence (RFC 9110 Section 13.2.2) and 304 merging (RFC 9111 Section 4.3.4)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import h2.config
import h2.connection
import h2.events
import h2.exceptions
import h11
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from api_labs.f03_http_caching_and_conditional_requests.conditional_get import (
    MAX_LIST_ELEMENTS,
    AnyRepresentation,
    EntityTag,
    OriginResource,
    Outcome,
    Representation,
    RevalidatingCache,
    StoredResponse,
    build_origin_app,
    build_validation_headers,
    evaluate_cache_validators,
    format_http_date,
    freshen,
    merge_header_fields,
    parse_http_date,
    parse_if_none_match,
)

if TYPE_CHECKING:
    from pathlib import Path

    from aioquic.h3.connection import H3Connection
    from aioquic.h3.events import H3Event
    from aioquic.quic.connection import QuicConnection
    from aioquic.quic.events import ConnectionTerminated
    from starlette.requests import Request

NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
LAST_MODIFIED = datetime(2026, 11, 1, 8, 30, 0, tzinfo=UTC)
CURRENT = Representation(etag=EntityTag('"v2"'), last_modified=LAST_MODIFIED)
NOT_MODIFIED, PERFORM = Outcome.NOT_MODIFIED, Outcome.PERFORM


def client_for(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
def test_if_none_match_mismatch_overrides_a_matching_if_modified_since() -> None:
    # The resource changed in a way the date cannot see: same second, new entity tag.
    headers = [
        ("If-None-Match", '"v1"'),
        ("If-Modified-Since", format_http_date(LAST_MODIFIED)),
    ]
    evaluation = evaluate_cache_validators("GET", headers, CURRENT, now=NOW)
    assert evaluation.outcome is Outcome.PERFORM
    assert evaluation.decided_by == "if-none-match"
    # Alone, the same If-Modified-Since would have produced a stale 304.
    alone = evaluate_cache_validators("GET", headers[1:], CURRENT, now=NOW)
    assert alone.outcome is Outcome.NOT_MODIFIED


@pytest.mark.unit
def test_if_none_match_uses_weak_comparison() -> None:
    for sent in ('W/"v2"', '"v2"'):
        headers = [("if-none-match", sent)]
        evaluation = evaluate_cache_validators("GET", headers, CURRENT, now=NOW)
        assert evaluation.outcome is Outcome.NOT_MODIFIED, sent
    weak_current = Representation(etag=EntityTag('"v2"', weak=True), last_modified=None)
    evaluation = evaluate_cache_validators(
        "GET", [("if-none-match", '"v2"')], weak_current, now=NOW
    )
    assert evaluation.outcome is Outcome.NOT_MODIFIED


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("*", AnyRepresentation.STAR),
        ('"a", W/"b"', [EntityTag('"a"'), EntityTag('"b"', weak=True)]),
        ('"a,b"', [EntityTag('"a,b"')]),
        (' , "a" ,, "b", ', [EntityTag('"a"'), EntityTag('"b"')]),
        ('""', [EntityTag('""')]),
        ('*, "a"', None),
        ('"a" "b"', None),
        ("a", None),
        ('w/"a"', None),
        ("," * (MAX_LIST_ELEMENTS + 1), None),
    ],
    ids=[
        "star",
        "list",
        "comma-inside-opaque-tag",
        "empty-elements",
        "empty-tag",
        "star-in-list-invalid",
        "missing-comma",
        "unquoted",
        "lowercase-weak-prefix",
        "too-many-elements",
    ],
)
def test_parse_if_none_match_follows_the_entity_tag_grammar(
    value: str, expected: AnyRepresentation | list[EntityTag] | None
) -> None:
    assert parse_if_none_match(value) == expected


@pytest.mark.unit
def test_if_none_match_list_star_and_multiple_field_lines() -> None:
    listed = [("if-none-match", '"v0", "v1"'), ("if-none-match", '"v2"')]
    assert evaluate_cache_validators("GET", listed, CURRENT, now=NOW).outcome is (
        Outcome.NOT_MODIFIED
    )
    star = [("if-none-match", "*")]
    assert evaluate_cache_validators("GET", star, CURRENT, now=NOW).outcome is (
        Outcome.NOT_MODIFIED
    )
    assert evaluate_cache_validators("GET", star, None, now=NOW).outcome is Outcome.PERFORM


@pytest.mark.unit
def test_malformed_if_none_match_still_disables_if_modified_since() -> None:
    headers = [
        ("if-none-match", '*, "v2"'),
        ("if-modified-since", format_http_date(LAST_MODIFIED)),
    ]
    evaluation = evaluate_cache_validators("GET", headers, CURRENT, now=NOW)
    assert evaluation == evaluate_cache_validators("GET", headers[:1], CURRENT, now=NOW)
    assert evaluation.outcome is Outcome.PERFORM
    assert evaluation.decided_by == "if-none-match"


@pytest.mark.unit
def test_if_none_match_failure_on_unsafe_method_is_412() -> None:
    headers = [("if-none-match", '"v2"')]
    for method in ("PUT", "DELETE", "POST"):
        evaluation = evaluate_cache_validators(method, headers, CURRENT, now=NOW)
        assert evaluation.outcome is Outcome.PRECONDITION_FAILED, method
    assert evaluate_cache_validators("HEAD", headers, CURRENT, now=NOW).outcome is (
        Outcome.NOT_MODIFIED
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "lines", "current", "expected"),
    [
        ("GET", [format_http_date(LAST_MODIFIED)], CURRENT, Outcome.NOT_MODIFIED),
        ("GET", [format_http_date(LAST_MODIFIED + timedelta(days=1))], CURRENT, NOT_MODIFIED),
        ("GET", [format_http_date(LAST_MODIFIED - timedelta(seconds=1))], CURRENT, PERFORM),
        ("GET", ["yesterday"], CURRENT, Outcome.PERFORM),
        ("GET", [format_http_date(LAST_MODIFIED)] * 2, CURRENT, Outcome.PERFORM),
        ("POST", [format_http_date(LAST_MODIFIED)], CURRENT, Outcome.PERFORM),
        (
            "GET",
            [format_http_date(LAST_MODIFIED)],
            Representation(etag=EntityTag('"v2"'), last_modified=None),
            Outcome.PERFORM,
        ),
    ],
    ids=[
        "equal-is-not-modified",
        "later-is-not-modified",
        "one-second-older-is-modified",
        "invalid-date-ignored",
        "two-members-ignored",
        "non-get-ignored",
        "no-last-modified-ignored",
    ],
)
def test_if_modified_since_boundaries_and_ignore_rules(
    method: str, lines: list[str], current: Representation, expected: Outcome
) -> None:
    headers = [("if-modified-since", line) for line in lines]
    assert evaluate_cache_validators(method, headers, current, now=NOW).outcome is expected


@pytest.mark.unit
def test_http_date_accepts_three_formats_and_rejects_lenient_variants() -> None:
    instant = datetime(1994, 11, 6, 8, 49, 37, tzinfo=UTC)
    for text in (
        "Sun, 06 Nov 1994 08:49:37 GMT",
        "Sunday, 06-Nov-94 08:49:37 GMT",
        "Sun Nov  6 08:49:37 1994",
    ):
        assert parse_http_date(text, now=NOW) == instant, text
    for text in (
        "Sun, 06 Nov 1994 08:49:37 PST",
        "Sun, 06 Nov 1994 08:49:37 GMT, Mon, 07 Nov 1994 08:49:37 GMT",
        "sun, 06 nov 1994 08:49:37 gmt",
        "Sun, 31 Feb 1994 08:49:37 GMT",
    ):
        assert parse_http_date(text, now=NOW) is None, text
    # A two-digit year more than 50 years ahead belongs to the previous century.
    assert parse_http_date("Thursday, 01-Jan-80 00:00:00 GMT", now=NOW) == datetime(
        1980, 1, 1, tzinfo=UTC
    )
    assert parse_http_date("Monday, 01-Jan-46 00:00:00 GMT", now=NOW) == datetime(
        2046, 1, 1, tzinfo=UTC
    )
    assert format_http_date(instant) == "Sun, 06 Nov 1994 08:49:37 GMT"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "now", "expected_year"),
    [
        ("Monday, 09-Nov-76 12:00:00 GMT", NOW, 2076),  # exactly 50 years ahead: not "more than"
        ("Monday, 09-Nov-76 12:00:01 GMT", NOW, 1976),  # one second past the limit
        ("Tuesday, 01-Jan-76 00:00:00 GMT", NOW, 2076),  # earlier in the year than now: 49.x years
        ("Wednesday, 01-Dec-76 00:00:00 GMT", NOW, 1976),  # later in the year than now: 50.x years
        ("Friday, 01-Jan-00 00:00:00 GMT", datetime(2099, 6, 1, tzinfo=UTC), 2100),
        ("Wednesday, 01-Jan-25 00:00:00 GMT", NOW, 2025),
    ],
    ids=[
        "exactly-50-years",
        "one-second-over",
        "same-year-earlier-month",
        "same-year-later-month",
        "century-rollover",
        "recent-past",
    ],
)
def test_rfc850_two_digit_year_uses_the_full_timestamp_for_the_fifty_year_rule(
    text: str, now: datetime, expected_year: int
) -> None:
    parsed = parse_http_date(text, now=now)
    assert parsed is not None
    assert parsed.year == expected_year


@pytest.mark.unit
@pytest.mark.asyncio
async def test_origin_304_carries_only_cache_update_metadata() -> None:
    resource = OriginResource(body=b'{"id": 1}', version=2, last_modified=LAST_MODIFIED)
    async with client_for(build_origin_app(resource)) as client:
        response = await client.get("/resource", headers={"if-none-match": 'W/"v2"'})

    assert response.status_code == 304
    assert response.content == b""
    assert sorted(response.headers.keys()) == ["cache-control", "date", "etag", "vary"]
    assert response.headers["etag"] == '"v2"'
    assert (resource.full_responses, resource.not_modified_responses) == (0, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_revalidation_round_trip_merges_304_headers_into_stored_response() -> None:
    resource = OriginResource(body=b'{"id": 1}', version=1, last_modified=LAST_MODIFIED)
    cache = RevalidatingCache()
    async with client_for(build_origin_app(resource)) as client:
        first = await cache.get(client, "/resource", now=0.0)
        resource.cache_control = "max-age=300"
        resource.clock = NOW + timedelta(minutes=5)
        second = await cache.get(client, "/resource", now=300.0)

    assert first.source == "network"
    assert second.source == "revalidated"
    assert second.sent_validators == [
        ("if-none-match", '"v1"'),
        ("if-modified-since", format_http_date(LAST_MODIFIED)),
    ]
    assert second.status == 200
    assert second.body == b'{"id": 1}'
    merged = dict(second.headers)
    assert merged["cache-control"] == "max-age=300"
    assert merged["date"] == format_http_date(NOW + timedelta(minutes=5))
    # Fields absent from the 304 survive from the stored 200.
    assert merged["content-type"] == "application/json"
    assert merged["content-length"] == "9"
    assert merged["x-request-cost"] == "expensive"
    assert (resource.full_responses, resource.not_modified_responses) == (1, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_changed_representation_replaces_stored_response() -> None:
    resource = OriginResource(body=b'{"id": 1}', version=1, last_modified=LAST_MODIFIED)
    cache = RevalidatingCache()
    async with client_for(build_origin_app(resource)) as client:
        await cache.get(client, "/resource", now=0.0)
        resource.body, resource.version = b'{"id": 1, "v": 2}', 2
        result = await cache.get(client, "/resource", now=10.0)

    assert result.source == "replaced"
    assert result.body == b'{"id": 1, "v": 2}'
    assert dict(result.headers)["etag"] == '"v2"'
    assert cache.entries["/resource"].etag() == EntityTag('"v2"')


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_refetches_when_a_304_identifies_no_stored_response() -> None:
    # A misbehaving origin answers every conditional GET with a 304 for a tag nobody stored.
    async def endpoint(request: Request) -> Response:
        if "if-none-match" in request.headers:
            return Response(status_code=304, headers={"etag": '"other"'})
        return Response(content=b"fresh", headers={"etag": '"v1"', "cache-control": "no-cache"})

    app = Starlette(routes=[Route("/r", endpoint)])
    cache = RevalidatingCache()
    async with client_for(app) as client:
        await cache.get(client, "/r", now=0.0)
        result = await cache.get(client, "/r", now=1.0)

    assert result.source == "replaced"
    assert result.status == 200
    assert result.body == b"fresh"
    assert cache.entries["/r"].update_count == 0


def stored(
    etag: str | None, *, response_time: float, last_modified: str | None = None
) -> StoredResponse:
    headers = [("content-type", "application/json"), ("cache-control", "max-age=10")]
    if etag is not None:
        headers.append(("etag", etag))
    if last_modified is not None:
        headers.append(("last-modified", last_modified))
    return StoredResponse(200, headers, b"{}", response_time)


@pytest.mark.unit
def test_validation_request_lists_every_stored_tag_and_dates_only_a_single_response() -> None:
    lm = format_http_date(LAST_MODIFIED)
    variants = [
        stored('"a"', response_time=1.0, last_modified=lm),
        stored('W/"b"', response_time=2.0, last_modified=lm),
        stored('"a"', response_time=3.0),
        stored("unquoted-is-not-a-tag", response_time=4.0),
    ]
    assert build_validation_headers(variants) == [("if-none-match", '"a", W/"b"')]
    assert build_validation_headers(variants[:1]) == [
        ("if-none-match", '"a"'),
        ("if-modified-since", lm),
    ]
    assert build_validation_headers(variants[3:]) == []


@pytest.mark.unit
def test_strong_validator_freshens_only_matching_variants() -> None:
    gzip_variant = stored('"a-gzip"', response_time=1.0)
    identity_variant = stored('"a"', response_time=2.0)
    updated = freshen(
        [gzip_variant, identity_variant],
        [("etag", '"a-gzip"'), ("cache-control", "max-age=99")],
        response_time=50.0,
    )
    assert updated == [gzip_variant]
    assert dict(gzip_variant.headers)["cache-control"] == "max-age=99"
    assert dict(identity_variant.headers)["cache-control"] == "max-age=10"
    assert (gzip_variant.update_count, identity_variant.update_count) == (1, 0)
    # Age restarts at the 304 (RFC 9111 Section 4.2.3 resident_time); the other variant keeps its.
    assert (gzip_variant.response_time, identity_variant.response_time) == (50.0, 2.0)


@pytest.mark.unit
def test_strong_304_validator_does_not_match_a_stored_weak_tag_with_the_same_opaque_value() -> None:
    # RFC 9111 Section 4.3.4 rule 1 needs "the same strong validators"; W/"a" is not one.
    weak_variant = stored('W/"a"', response_time=1.0)
    strong_variant = stored('"a"', response_time=2.0)
    updated = freshen(
        [weak_variant, strong_variant],
        [("etag", '"a"'), ("cache-control", "max-age=99")],
        response_time=50.0,
    )
    assert updated == [strong_variant]
    assert dict(weak_variant.headers)["cache-control"] == "max-age=10"
    assert weak_variant.update_count == 0
    # A weak tag in the 304 falls to rule 2, which compares weakly and so reaches the strong copy.
    entry = stored('"a"', response_time=1.0)
    assert freshen([entry], [("etag", 'W/"a"')], response_time=9.0) == [entry]


@pytest.mark.unit
def test_strong_validator_matching_nothing_updates_nothing() -> None:
    entry = stored('"a"', response_time=1.0)
    not_modified = [("etag", '"b"'), ("cache-control", "max-age=99")]
    updated = freshen([entry], not_modified, response_time=9.0)
    assert updated == []
    assert dict(entry.headers)["cache-control"] == "max-age=10"
    assert entry.response_time == 1.0


@pytest.mark.unit
def test_weak_validator_freshens_only_the_most_recent_match() -> None:
    older = stored('W/"x"', response_time=1.0)
    newer = stored('W/"x"', response_time=5.0)
    other = stored('W/"y"', response_time=9.0)
    updated = freshen(
        [older, newer, other],
        [("etag", 'W/"x"'), ("cache-control", "max-age=99")],
        response_time=60.0,
    )
    assert updated == [newer]
    assert [entry.update_count for entry in (older, newer, other)] == [0, 1, 0]


@pytest.mark.unit
def test_validatorless_304_updates_a_single_validatorless_entry_only() -> None:
    lone = stored(None, response_time=1.0)
    assert freshen([lone], [("cache-control", "max-age=99")], response_time=2.0) == [lone]
    pair = [stored(None, response_time=1.0), stored(None, response_time=2.0)]
    assert freshen(pair, [("cache-control", "max-age=99")], response_time=3.0) == []


@pytest.mark.unit
def test_merge_skips_content_length_hop_by_hop_and_proxy_fields() -> None:
    stored_headers = [
        ("Content-Type", "application/json"),
        ("Content-Length", "1234"),
        ("Cache-Control", "max-age=10"),
        ("Link", "</a>; rel=preload"),
        ("Link", "</b>; rel=preload"),
    ]
    not_modified = [
        ("cache-control", "max-age=600"),
        ("content-length", "0"),
        ("connection", "x-hop-trace"),
        ("x-hop-trace", "abc"),
        ("keep-alive", "timeout=5"),
        ("proxy-authenticate", 'Basic realm="edge"'),
        ("link", "</c>; rel=preload"),
        ("x-new", "1"),
    ]
    assert merge_header_fields(stored_headers, not_modified) == [
        ("Content-Type", "application/json"),
        ("Content-Length", "1234"),
        ("cache-control", "max-age=600"),
        ("link", "</c>; rel=preload"),
        ("x-new", "1"),
    ]
    protected = merge_header_fields(
        [("content-encoding", "gzip")],
        [("content-encoding", "br")],
        protected=frozenset({"content-encoding"}),
    )
    assert protected == [("content-encoding", "gzip")]


@pytest.mark.unit
def test_h11_frames_304_as_bodyless_despite_content_length() -> None:
    client = h11.Connection(our_role=h11.CLIENT)
    client.send(h11.Request(method="GET", target="/r", headers=[("Host", "t")]))
    client.send(h11.EndOfMessage())
    wire = (
        b'HTTP/1.1 304 Not Modified\r\netag: "v1"\r\ncontent-length: 5000\r\n\r\n'
        b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok"
    )
    client.receive_data(wire)
    first = client.next_event()
    assert isinstance(first, h11.Response)
    assert first.status_code == 304
    assert isinstance(client.next_event(), h11.EndOfMessage)
    # The connection is reusable: the next response's bytes were not eaten as a 304 body.
    client.start_next_cycle()
    client.send(h11.Request(method="GET", target="/r", headers=[("Host", "t")]))
    client.send(h11.EndOfMessage())
    second = client.next_event()
    assert isinstance(second, h11.Response)
    assert second.status_code == 200
    body = client.next_event()
    assert isinstance(body, h11.Data)
    assert body.data == b"ok"


def h2_pair() -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    server.receive_data(client.data_to_send())
    client.send_headers(
        1,
        [
            (":method", "GET"),
            (":path", "/r"),
            (":scheme", "https"),
            (":authority", "t"),
            ("if-none-match", '"v1"'),
        ],
        end_stream=True,
    )
    server.receive_data(client.data_to_send())
    return client, server


NOT_MODIFIED_H2 = [(":status", "304"), ("etag", '"v1"'), ("content-length", "5000")]


@pytest.mark.unit
def test_h2_accepts_304_content_length_when_headers_end_the_stream() -> None:
    client, server = h2_pair()
    server.send_headers(1, NOT_MODIFIED_H2, end_stream=True)
    events = client.receive_data(server.data_to_send())
    assert [type(event) for event in events] == [
        h2.events.ResponseReceived,
        h2.events.StreamEnded,
    ]


@pytest.mark.unit
def test_h2_rejects_304_content_length_when_an_empty_data_frame_ends_the_stream() -> None:
    client, server = h2_pair()
    server.send_headers(1, NOT_MODIFIED_H2)
    server.send_data(1, b"", end_stream=True)
    with pytest.raises(h2.exceptions.InvalidBodyLengthError, match="Expected 5000 bytes"):
        client.receive_data(server.data_to_send())


CLIENT_ADDRESS = ("127.0.0.1", 5000)
SERVER_ADDRESS = ("127.0.0.1", 4433)
NOT_MODIFIED_H3 = [(b":status", b"304"), (b"etag", b'"v1"'), (b"content-length", b"5000")]


@dataclass(slots=True)
class H3Pair:
    """An aioquic client and server joined by in-memory datagrams: no sockets, no clock."""

    client: QuicConnection
    server: QuicConnection
    h3_client: H3Connection
    h3_server: H3Connection
    now: float = 0.0

    def pump(self) -> None:
        for _ in range(300):
            for source, target, address in (
                (self.client, self.server, CLIENT_ADDRESS),
                (self.server, self.client, SERVER_ADDRESS),
            ):
                for datagram, _ in source.datagrams_to_send(now=self.now):
                    target.receive_datagram(datagram, address, now=self.now)
                timer = source.get_timer()
                if timer is not None and timer <= self.now:
                    source.handle_timer(now=self.now)
            self.now += 0.01

    @staticmethod
    def drain(
        quic: QuicConnection, h3: H3Connection
    ) -> tuple[list[H3Event], list[ConnectionTerminated]]:
        from aioquic.quic.events import ConnectionTerminated

        events: list[H3Event] = []
        terminated: list[ConnectionTerminated] = []
        while (event := quic.next_event()) is not None:
            if isinstance(event, ConnectionTerminated):
                terminated.append(event)
            events.extend(h3.handle_event(event))
        return events, terminated


def h3_pair() -> H3Pair:
    # aioquic needs pylsqpack, which has no free-threaded 3.15.0b3 wheel and fails to build there.
    pytest.importorskip("aioquic")
    from aioquic.h3.connection import H3Connection
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.connection import QuicConnection

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
    client_config = QuicConfiguration(is_client=True, alpn_protocols=["h3"], verify_mode=0)
    client = QuicConnection(configuration=client_config)
    client.connect(SERVER_ADDRESS, now=0.0)
    server = QuicConnection(
        configuration=server_config,
        original_destination_connection_id=client.original_destination_connection_id,
    )
    pair = H3Pair(client, server, H3Connection(client), H3Connection(server))
    pair.pump()
    return pair


def h3_exchange(response_headers: list[tuple[bytes, bytes]], *, end_on_headers: bool) -> H3Pair:
    """Send one conditional GET and the given response; leave the response unread at the client."""
    pair = h3_pair()
    from aioquic.h3.events import HeadersReceived

    stream_id = pair.client.get_next_available_stream_id()
    pair.h3_client.send_headers(
        stream_id,
        [
            (b":method", b"GET"),
            (b":scheme", b"https"),
            (b":authority", b"t"),
            (b":path", b"/r"),
            (b"if-none-match", b'"v1"'),
        ],
        end_stream=True,
    )
    pair.pump()
    received, _ = pair.drain(pair.server, pair.h3_server)
    assert [type(event) for event in received] == [HeadersReceived]
    pair.h3_server.send_headers(stream_id, response_headers, end_stream=end_on_headers)
    if not end_on_headers:
        pair.h3_server.send_data(stream_id, b"", end_stream=True)
    pair.pump()
    return pair


@pytest.mark.unit
@pytest.mark.parametrize(
    "end_on_headers", [True, False], ids=["end-stream-on-headers", "empty-data"]
)
def test_aioquic_h3_rejects_304_content_length_in_both_framings(end_on_headers: bool) -> None:
    pair = h3_exchange(NOT_MODIFIED_H3, end_on_headers=end_on_headers)
    from aioquic.h3.connection import ErrorCode

    events, _ = pair.drain(pair.client, pair.h3_client)
    # Unlike h2, aioquic checks the declared length even when HEADERS carries the end of the stream.
    # The client never surfaces the response and closes the connection; the server sees that close.
    assert events == []
    pair.pump()
    _, terminated = pair.drain(pair.server, pair.h3_server)
    assert [(item.error_code, item.reason_phrase) for item in terminated] == [
        (ErrorCode.H3_MESSAGE_ERROR, "content-length does not match data size")
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "end_on_headers", [True, False], ids=["end-stream-on-headers", "empty-data"]
)
def test_aioquic_h3_accepts_304_without_content_length(end_on_headers: bool) -> None:
    # Control for the test above: the pair is healthy and only `content-length` differs.
    pair = h3_exchange(NOT_MODIFIED_H3[:2], end_on_headers=end_on_headers)
    from aioquic.h3.events import HeadersReceived

    events, _ = pair.drain(pair.client, pair.h3_client)
    pair.pump()
    _, terminated = pair.drain(pair.server, pair.h3_server)
    assert terminated == []
    assert [type(event) for event in events][:1] == [HeadersReceived]
    headers = events[0]
    assert isinstance(headers, HeadersReceived)
    assert (b":status", b"304") in headers.headers
    assert (b"content-length", b"5000") not in headers.headers


@pytest.fixture
def static_app(tmp_path: Path) -> Starlette:
    target = tmp_path / "doc.json"
    target.write_bytes(b'{"id": 1}')
    epoch = LAST_MODIFIED.timestamp()
    os.utime(target, (epoch, epoch))
    return Starlette(routes=[Mount("/static", app=StaticFiles(directory=tmp_path))])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_staticfiles_prefers_if_none_match_and_trims_304_headers(
    static_app: Starlette,
) -> None:
    async with client_for(static_app) as client:
        full = await client.get("/static/doc.json")
        etag = full.headers["etag"]
        stale_tag = await client.get(
            "/static/doc.json",
            headers={
                "if-none-match": '"not-the-tag"',
                "if-modified-since": full.headers["last-modified"],
            },
        )
        weak = await client.get("/static/doc.json", headers={"if-none-match": f"W/{etag}"})

    assert full.headers["last-modified"] == format_http_date(LAST_MODIFIED)
    assert stale_tag.status_code == 200
    assert weak.status_code == 304
    assert weak.content == b""
    assert sorted(weak.headers.keys()) == ["etag"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        format_http_date(LAST_MODIFIED) + ", Mon, 01 Jan 1990 00:00:00 GMT",
        format_http_date(LAST_MODIFIED).replace("GMT", "PST"),
    ],
    ids=["two-members", "non-gmt-zone"],
)
async def test_starlette_staticfiles_honors_if_modified_since_the_rfc_says_to_ignore(
    static_app: Starlette, value: str
) -> None:
    async with client_for(static_app) as client:
        response = await client.get("/static/doc.json", headers={"if-modified-since": value})
    # starlette 1.10.0 uses email.utils.parsedate, which takes the first date and drops the zone.
    assert response.status_code == 304
    # This lab's evaluator ignores the field, as RFC 9110 Section 13.1.3 requires.
    headers = [("if-modified-since", value)]
    assert evaluate_cache_validators("GET", headers, CURRENT, now=NOW).outcome is Outcome.PERFORM
