"""Stripe-style date versioning: pinning, header overrides, transforms, and side-effect gates."""

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncGenerator, MutableMapping
from datetime import UTC, datetime, timedelta
from typing import Any

import h2.config
import h2.connection
import h2.errors
import h2.events
import httpx
import hypercorn.asyncio
import hypercorn.config
import pytest
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from api_labs.f07_versioning_evolution_and_api_guidelines.date_versioning import (
    COLLAPSE_EVENT_REQUEST,
    RENAME_CUSTOMER_ACCOUNT_BALANCE,
    Account,
    ApiVersion,
    Credential,
    LabState,
    RegistryError,
    UnknownVersionError,
    VersionChange,
    VersioningError,
    VersionRegistry,
    build_app,
    build_registry,
    pinned_accounts,
    render_event,
    seeded_state,
    select_version,
    synthetic_chain,
)

DIRECT = {"Authorization": "Bearer sk_acct_1"}
OAUTH_APP = {"Authorization": "Bearer sk_app_on_acct_1"}


def client_for(state: LabState) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(state)), base_url="http://test"
    )


def at(version: str, base: dict[str, str] = DIRECT) -> dict[str, str]:
    return {**base, "Stripe-Version": version}


@contextlib.asynccontextmanager
async def serving(app: ASGIApp, *, pass_raw_headers: bool = False) -> AsyncGenerator[int]:
    """Hypercorn on an ephemeral loopback port; yields the port and stops the server on exit."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()  # the kernel queues the connection until hypercorn accepts
    port: int = listener.getsockname()[1]
    config = hypercorn.config.Config()
    config.bind = [f"fd://{listener.detach()}"]  # hypercorn owns and closes the descriptor
    config.loglevel = "WARNING"
    config.h11_pass_raw_headers = pass_raw_headers
    stop = asyncio.Event()
    task = asyncio.create_task(
        hypercorn.asyncio.serve(app, config, shutdown_trigger=stop.wait, mode="asgi")  # type: ignore[arg-type]
    )
    try:
        yield port
    finally:
        stop.set()
        await task


async def http2_exchange(
    port: int, fields: list[tuple[bytes, bytes]], *, wire_case_as_given: bool = False
) -> list[h2.events.Event]:
    """One HTTP/2 prior-knowledge `GET` on stream 1; returns every event the client saw.

    `wire_case_as_given` turns off h2's outbound normalization and validation, so a
    mixed-case field name really reaches the wire.
    """
    connection = h2.connection.H2Connection(
        config=h2.config.H2Configuration(
            client_side=True,
            normalize_outbound_headers=not wire_case_as_given,
            validate_outbound_headers=not wire_case_as_given,
        )
    )
    pseudo = [
        (b":method", b"GET"),
        (b":path", b"/v1/events/evt_1"),
        (b":scheme", b"http"),
        (b":authority", b"test"),
    ]
    terminal = (h2.events.StreamEnded, h2.events.StreamReset, h2.events.ConnectionTerminated)
    events: list[h2.events.Event] = []
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        connection.initiate_connection()
        connection.send_headers(1, pseudo + fields, end_stream=True)
        writer.write(connection.data_to_send())
        await writer.drain()
        while not any(isinstance(event, terminal) for event in events):
            data = await reader.read(65535)
            if not data:
                break
            events.extend(connection.receive_data(data))
            writer.write(connection.data_to_send())  # SETTINGS ack and window updates
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()
    return events


@pytest.mark.unit
@pytest.mark.asyncio
async def test_first_request_pins_account_and_pin_survives_a_new_release() -> None:
    state = seeded_state()
    async with client_for(state) as client:
        first = await client.post("/v1/customers", json={"balance": 1}, headers=DIRECT)
        assert first.headers["Stripe-Version"] == "2022-11-15"
        assert pinned_accounts(state) == {"acct_1": "2022-11-15"}

        newer = VersionChange("rename_balance_to_cash", "test", "customer", lambda o: o)
        state.registry = VersionRegistry(
            [
                ("2017-02-14", []),
                ("2017-05-25", [COLLAPSE_EVENT_REQUEST]),
                ("2019-10-17", [RENAME_CUSTOMER_ACCOUNT_BALANCE]),
                ("2022-11-15", []),
                ("2024-09-30", [newer]),
            ]
        )
        second = await client.post("/v1/customers", json={"balance": 2}, headers=DIRECT)
    assert second.headers["Stripe-Version"] == "2022-11-15"
    assert pinned_accounts(state) == {"acct_1": "2022-11-15"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_header_overrides_one_request_without_repinning() -> None:
    state = seeded_state()
    async with client_for(state) as client:
        await client.post("/v1/customers", json={"balance": 5}, headers=DIRECT)
        old = await client.post(
            "/v1/customers", json={"account_balance": 5}, headers=at("2017-02-14")
        )
        pinned = await client.post("/v1/customers", json={"balance": 5}, headers=DIRECT)
    assert old.json() == {"id": "cus_2", "object": "customer", "account_balance": 5}
    assert old.headers["Stripe-Version"] == "2017-02-14"
    assert pinned.json() == {"id": "cus_3", "object": "customer", "balance": 5}
    assert pinned_accounts(state) == {"acct_1": "2022-11-15"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oauth_app_version_beats_pin_and_header_beats_app() -> None:
    state = seeded_state()
    async with client_for(state) as client:
        await client.get("/v1/events/evt_1", headers=DIRECT)
        app_default = await client.get("/v1/events/evt_1", headers=OAUTH_APP)
        app_header = await client.get("/v1/events/evt_1", headers=at("2022-11-15", OAUTH_APP))
    assert app_default.headers["Stripe-Version"] == "2017-05-25"
    assert app_header.headers["Stripe-Version"] == "2022-11-15"
    assert pinned_accounts(state) == {"acct_1": "2022-11-15"}


@pytest.mark.unit
def test_select_version_reports_its_source_and_strips_optional_whitespace() -> None:
    registry = build_registry()
    account = Account("acct_1")
    direct = Credential("acct_1")
    app = Credential("acct_1", oauth_app_version="2017-05-25")
    old = ApiVersion.parse("2017-02-14")
    assert select_version(registry, account, [" 2017-02-14\t"], app) == (old, "header")
    assert select_version(registry, account, [], app) == (
        ApiVersion.parse("2017-05-25"),
        "oauth_app",
    )
    assert account.pinned is None
    assert select_version(registry, account, [], direct) == (
        registry.latest,
        "account_first_request",
    )
    assert select_version(registry, account, [], direct) == (registry.latest, "account_pin")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_upgrade_means_head_handler_sees_only_head_fields() -> None:
    state = seeded_state()
    async with client_for(state) as client:
        old = await client.post(
            "/v1/customers", json={"account_balance": 700}, headers=at("2017-02-14")
        )
        head = await client.post("/v1/customers", json={"balance": 700}, headers=at("2022-11-15"))
        stale = await client.post(
            "/v1/customers", json={"account_balance": 700}, headers=at("2019-10-17")
        )
    assert state.handler_inputs == [{"balance": 700}, {"balance": 700}, {"account_balance": 700}]
    assert stale.status_code == 400
    assert stale.json() == {"error": {"message": "unknown parameter: account_balance"}}
    assert old.json()["account_balance"] == 700
    assert "balance" not in old.json()
    assert head.json()["balance"] == 700


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nested_event_payload_is_rendered_version_by_version() -> None:
    state = seeded_state()
    bodies: dict[str, Any] = {}
    async with client_for(state) as client:
        for version in ("2017-02-14", "2017-05-25", "2019-10-17"):
            response = await client.get("/v1/events/evt_1", headers=at(version))
            bodies[version] = response.json()
    assert bodies["2017-02-14"]["request"] == "req_1"
    assert bodies["2017-02-14"]["data"]["object"] == {
        "id": "cus_9",
        "object": "customer",
        "account_balance": -300,
    }
    assert bodies["2017-05-25"]["request"] == {"id": "req_1", "idempotency_key": "ik_1"}
    assert "account_balance" in bodies["2017-05-25"]["data"]["object"]
    assert bodies["2019-10-17"]["data"]["object"]["balance"] == -300
    assert state.events["evt_1"]["request"] == {"id": "req_1", "idempotency_key": "ik_1"}


@pytest.mark.unit
def test_collapse_event_request_passes_through_a_null_request() -> None:
    event = {"id": "evt_2", "object": "event", "request": None}
    assert build_registry().downgrade(event, ApiVersion.parse("2017-02-14")) == event


@pytest.mark.unit
def test_downgrade_walks_newest_to_oldest_and_stops_at_target() -> None:
    def rename(old: str, new: str) -> VersionChange:
        def downgrade(obj: dict[str, Any]) -> dict[str, Any]:
            return {(old if k == new else k): v for k, v in obj.items()}

        return VersionChange(f"{old}_to_{new}", "test", "widget", downgrade)

    registry = VersionRegistry(
        [("2020-01-01", []), ("2021-01-01", [rename("a", "b")]), ("2022-01-01", [rename("b", "c")])]
    )
    head = {"object": "widget", "c": 1}
    assert registry.downgrade(head, ApiVersion.parse("2020-01-01")) == {"object": "widget", "a": 1}
    assert registry.downgrade(head, ApiVersion.parse("2021-01-01")) == {"object": "widget", "b": 1}
    assert registry.downgrade(head, ApiVersion.parse("2022-01-01")) == head


@pytest.mark.unit
def test_transforms_touch_only_their_resource_and_reach_into_lists() -> None:
    registry = build_registry()
    base = ApiVersion.parse("2017-02-14")
    head = {
        "object": "list",
        "data": [
            {"id": "cus_1", "object": "customer", "balance": 1},
            {"id": "ba_1", "object": "bank_account", "balance": 2, "request": {"id": "req_9"}},
            [{"id": "cus_2", "object": "customer", "balance": 3}],
        ],
    }
    assert registry.downgrade(head, base) == {
        "object": "list",
        "data": [
            {"id": "cus_1", "object": "customer", "account_balance": 1},
            {"id": "ba_1", "object": "bank_account", "balance": 2, "request": {"id": "req_9"}},
            [{"id": "cus_2", "object": "customer", "account_balance": 3}],
        ],
    }
    assert head["data"][0] == {"id": "cus_1", "object": "customer", "balance": 1}
    old_params = {"account_balance": 4}
    assert registry.upgrade_request("customer", old_params, base) == {"balance": 4}
    assert registry.upgrade_request("charge", old_params, base) == {"account_balance": 4}
    assert old_params == {"account_balance": 4}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_side_effect_gate_expands_refunds_only_before_2022_11_15() -> None:
    state = seeded_state()
    async with client_for(state) as client:
        old = await client.get("/v1/charges/ch_1", headers=at("2019-10-17"))
        head = await client.get("/v1/charges/ch_1", headers=at("2022-11-15"))
    assert old.json()["refunds"] == {
        "object": "list",
        "data": [{"id": "re_1", "object": "refund", "amount": 500}],
    }
    assert head.json()["refunds"] == ["re_1"]
    registry = build_registry()
    assert not registry.is_applied("charge_refunds_not_expanded", ApiVersion.parse("2019-10-17"))
    assert registry.is_applied("charge_refunds_not_expanded", ApiVersion.parse("2022-11-15"))
    with pytest.raises(RegistryError, match="never registered"):
        registry.is_applied("no_such_change", registry.latest)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        at("2018-01-01"),
        at("2017-13-01"),
        at("v1"),
        at("2017-02-14, 2019-10-17"),
        [("Authorization", "Bearer sk_acct_1"), ("Stripe-Version", "2017-02-14")] * 2,
    ],
)
async def test_unknown_malformed_or_duplicate_versions_are_rejected(
    headers: dict[str, str] | list[tuple[str, str]],
) -> None:
    state = seeded_state()
    async with client_for(state) as client:
        response = await client.get("/v1/events/evt_1", headers=headers)
    assert response.status_code == 400
    assert "Stripe-Version" not in response.headers
    assert pinned_accounts(state) == {"acct_1": None}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mixed_case_header_name_in_raw_scope_still_overrides() -> None:
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/events/evt_1",
        "raw_path": b"/v1/events/evt_1",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"Authorization", b"Bearer sk_acct_1"), (b"Stripe-Version", b"2017-02-14")],
        "client": None,
        "server": None,
    }
    assert Headers(scope=dict(scope)).getlist("Stripe-Version") == []
    assert Headers(scope=dict(scope)).get("authorization") is None
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await build_app(seeded_state())(scope, receive, send)
    assert sent[0]["status"] == 200
    assert (b"stripe-version", b"2017-02-14") in sent[0]["headers"]
    assert json.loads(sent[1]["body"])["request"] == "req_1"


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("pass_raw_headers", [False, True])
async def test_hypercorn_header_case_reaches_the_scope_and_the_app_still_resolves(
    pass_raw_headers: bool,
) -> None:
    lab = build_app(seeded_state())
    seen: list[bytes] = []

    async def recording(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            seen.extend(name for name, _ in scope["headers"])
        await lab(scope, receive, send)

    async with serving(recording, pass_raw_headers=pass_raw_headers) as port, asyncio.timeout(10):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /v1/events/evt_1 HTTP/1.1\r\nHost: test\r\n"
            b"Authorization: Bearer sk_acct_1\r\nStripe-Version: 2017-02-14\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        raw = await reader.read()  # hypercorn closes after the response
        writer.close()
        await writer.wait_closed()

    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")
    assert b"\r\nstripe-version: 2017-02-14\r\n" in head.lower() + b"\r\n"
    assert json.loads(body)["request"] == "req_1"
    expected = (
        [b"Host", b"Authorization", b"Stripe-Version", b"Connection"]
        if pass_raw_headers
        else [b"host", b"authorization", b"stripe-version", b"connection"]
    )
    assert seen == expected


@pytest.mark.socket
@pytest.mark.asyncio
async def test_http2_delivers_lowercase_names_and_refuses_uppercase_ones_as_malformed() -> None:
    lab = build_app(seeded_state())
    seen: list[tuple[str, list[bytes]]] = []

    async def recording(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            seen.append((scope["http_version"], [name for name, _ in scope["headers"]]))
        await lab(scope, receive, send)

    mixed_case = [(b"Authorization", b"Bearer sk_acct_1"), (b"Stripe-Version", b"2017-02-14")]
    async with serving(recording) as port, asyncio.timeout(10):
        answered = await http2_exchange(port, mixed_case)
        refused = await http2_exchange(port, mixed_case, wire_case_as_given=True)

    # h2 lowercased the names before sending, as RFC 9113 Section 8.2 requires of a sender.
    assert seen == [("2", [b"host", b"authorization", b"stripe-version"])]
    responses = [event for event in answered if isinstance(event, h2.events.ResponseReceived)]
    assert len(responses) == 1
    fields = dict(responses[0].headers or [])
    assert (fields[b":status"], fields[b"stripe-version"]) == (b"200", b"2017-02-14")
    assert fields[b"vary"] == b"Stripe-Version"
    body = b"".join(
        event.data or b"" for event in answered if isinstance(event, h2.events.DataReceived)
    )
    assert json.loads(body)["request"] == "req_1"
    # Uppercase on the wire is malformed (RFC 9113 Section 8.2.1): no response, no app call.
    assert not any(isinstance(event, h2.events.ResponseReceived) for event in refused)
    goaway = [event for event in refused if isinstance(event, h2.events.ConnectionTerminated)]
    assert [event.error_code for event in goaway] == [h2.errors.ErrorCodes.PROTOCOL_ERROR]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_waterfall_policy_rounds_unreleased_dates_down() -> None:
    state = seeded_state(policy="waterfall")
    async with client_for(state) as client:
        late = await client.get("/v1/events/evt_1", headers=at("2019-12-31"))
        early = await client.get("/v1/events/evt_1", headers=at("2019-10-16"))
        too_old = await client.get("/v1/events/evt_1", headers=at("2016-01-01"))
    assert late.headers["Stripe-Version"] == "2019-10-17"
    assert late.json()["data"]["object"]["balance"] == -300
    assert early.headers["Stripe-Version"] == "2017-05-25"
    assert early.json()["data"]["object"]["account_balance"] == -300
    assert too_old.status_code == 400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_responses_echo_resolved_version_and_vary_on_it() -> None:
    async with client_for(seeded_state()) as client:
        response = await client.get("/v1/events/evt_1", headers=at("2017-05-25"))
        unauthenticated = await client.get("/v1/events/evt_1")
    assert response.headers["Stripe-Version"] == "2017-05-25"
    assert response.headers["Vary"] == "Stripe-Version"
    assert unauthenticated.status_code == 401


@pytest.mark.unit
@pytest.mark.asyncio
async def test_error_bodies_render_embedded_resources_and_records_never_mutate() -> None:
    state = seeded_state()
    async with client_for(state) as client:
        await client.post("/v1/customers", json={"id": "cus_a", "balance": 9}, headers=DIRECT)
        conflict = await client.post(
            "/v1/customers", json={"id": "cus_a"}, headers=at("2017-02-14")
        )
        bad_type = await client.post(
            "/v1/customers", json={"account_balance": "x"}, headers=at("2017-02-14")
        )
        bool_balance = await client.post("/v1/customers", json={"balance": True}, headers=DIRECT)
        bad_id = await client.post("/v1/customers", json={"id": 7}, headers=DIRECT)
        not_object = await client.post("/v1/customers", json=[1], headers=DIRECT)
        not_json = await client.post("/v1/customers", content=b"{", headers=DIRECT)
        missing = await client.get("/v1/charges/ch_404", headers=at("2017-02-14"))
        no_event = await client.get("/v1/events/evt_404", headers=DIRECT)
        await client.get("/v1/charges/ch_1", headers=at("2017-02-14"))
        head = await client.get("/v1/charges/ch_1", headers=at("2022-11-15"))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["customer"] == {
        "id": "cus_a",
        "object": "customer",
        "account_balance": 9,
    }
    assert state.customers["cus_a"] == {"id": "cus_a", "object": "customer", "balance": 9}
    assert bad_type.json() == {"error": {"message": "balance must be an integer"}}
    assert bool_balance.json() == {"error": {"message": "balance must be an integer"}}
    assert bad_id.json() == {"error": {"message": "id must be a string"}}
    assert not_object.json() == {"error": {"message": "request body must be a JSON object"}}
    assert not_json.json() == {"error": {"message": "request body must be JSON"}}
    rejected = (bad_type, bool_balance, bad_id, not_object, not_json)
    assert [r.status_code for r in rejected] == [400] * 5
    assert list(state.customers) == ["cus_a"]
    assert [missing.status_code, no_event.status_code] == [404, 404]
    assert missing.headers["Stripe-Version"] == "2017-02-14"
    assert head.json()["refunds"] == ["re_1"]
    assert state.charges["ch_1"]["refunds"] == ["re_1"]


@pytest.mark.unit
def test_webhook_renders_at_endpoint_version_else_account_pin() -> None:
    registry = build_registry()
    state = seeded_state()
    event = state.events["evt_1"]
    account = Account("acct_1", pinned=ApiVersion.parse("2017-05-25"))
    by_endpoint = render_event(registry, event, account, ApiVersion.parse("2017-02-14"))
    by_account = render_event(registry, event, account, None)
    unpinned = render_event(registry, event, Account("acct_2"), None)
    assert by_endpoint["request"] == "req_1"
    assert by_account["request"] == {"id": "req_1", "idempotency_key": "ik_1"}
    assert by_account["data"]["object"] == {
        "id": "cus_9",
        "object": "customer",
        "account_balance": -300,
    }
    assert unpinned == event


@pytest.mark.unit
def test_upgrade_rollback_window_is_72_hours() -> None:
    start = datetime(2026, 11, 11, tzinfo=UTC)
    account = Account("acct_1", pinned=ApiVersion.parse("2019-10-17"))
    with pytest.raises(VersioningError, match="newer version"):
        account.upgrade(ApiVersion.parse("2017-05-25"), start)
    with pytest.raises(VersioningError, match="newer version"):
        account.upgrade(ApiVersion.parse("2019-10-17"), start)
    assert (account.pinned, account.previous) == (ApiVersion.parse("2019-10-17"), None)
    account.upgrade(ApiVersion.parse("2022-11-15"), start)
    account.rollback(start + timedelta(hours=72))
    assert account.pinned == ApiVersion.parse("2019-10-17")
    with pytest.raises(VersioningError, match="no upgrade"):
        account.rollback(start)

    account.upgrade(ApiVersion.parse("2022-11-15"), start)
    with pytest.raises(VersioningError, match="window has closed"):
        account.rollback(start + timedelta(hours=72, seconds=1))
    assert account.pinned == ApiVersion.parse("2022-11-15")
    with pytest.raises(VersioningError, match="newer version"):
        Account("acct_new").upgrade(ApiVersion.parse("2022-11-15"), start)


@pytest.mark.unit
def test_registry_rejects_lists_that_break_engine_invariants() -> None:
    change = RENAME_CUSTOMER_ACCOUNT_BALANCE
    with pytest.raises(RegistryError, match="at least one"):
        VersionRegistry([])
    with pytest.raises(RegistryError, match="strictly increase"):
        VersionRegistry([("2020-01-01", []), ("2020-01-01", [change])])
    with pytest.raises(RegistryError, match="base"):
        VersionRegistry([("2020-01-01", [change])])
    with pytest.raises(RegistryError, match="twice"):
        VersionRegistry([("2020-01-01", []), ("2021-01-01", [change]), ("2022-01-01", [change])])
    with pytest.raises(RegistryError, match="must not define"):
        VersionChange("x", "x", "charge", lambda o: o, has_side_effects=True)
    with pytest.raises(RegistryError, match="needs a transform"):
        VersionChange("x", "x", "charge")
    with pytest.raises(UnknownVersionError, match="malformed"):
        ApiVersion.parse("2020-02-30")


@pytest.mark.unit
def test_named_release_lines_parse_and_order_by_date() -> None:
    acacia = ApiVersion.parse("2024-09-30.acacia")
    assert str(acacia) == "2024-09-30.acacia"
    assert acacia.line == "acacia"
    assert ApiVersion.parse("2024-10-28.acacia") > acacia > ApiVersion.parse("2024-06-20")
    registry = VersionRegistry(
        [("2024-06-20", []), ("2024-09-30.acacia", [COLLAPSE_EVENT_REQUEST])]
    )
    assert registry.resolve("2024-09-30.acacia") == acacia
    with pytest.raises(UnknownVersionError, match="unknown"):
        registry.resolve("2024-09-30.basil")
    # Waterfall compares dates only, so a wrong line on a released date still resolves.
    assert str(registry.resolve("2024-09-30.basil", "waterfall")) == "2024-09-30.acacia"
    assert str(registry.resolve("2024-10-15", "waterfall")) == "2024-09-30.acacia"
    # A bare date and a suffixed one on the same day compare equal: the line never orders.
    assert str(registry.resolve("2024-09-30", "waterfall")) == "2024-09-30.acacia"
    assert ApiVersion.parse("2024-09-30") == acacia
    assert not ApiVersion.parse("2024-09-30.basil") < acacia


@pytest.mark.unit
def test_changelog_is_generated_from_change_modules_newest_first() -> None:
    assert build_registry().changelog() == [
        "2022-11-15 charge_refunds_not_expanded: Charge objects no longer auto-expand `refunds`.",
        "2019-10-17 rename_customer_account_balance: Customer `account_balance` is renamed to "
        "`balance`.",
        "2017-05-25 collapse_event_request: Event `request` is an object with `id` and "
        "`idempotency_key`, not a string.",
    ]


@pytest.mark.unit
def test_synthetic_chain_of_100_changes_walks_every_step_back_to_base() -> None:
    registry, head = synthetic_chain(100)
    base = registry.versions[0]
    assert len(registry.versions) == 101
    old = registry.downgrade(head, base)
    assert old["f0"] == 1
    assert "f100" not in old
    assert len(old) == len(head) == 22
    assert registry.downgrade(head, registry.versions[50])["f50"] == 1
