"""Tests for request mirroring: admission, isolation from the primary, diffing, and telemetry."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from api_labs.f17_api_gateways_proxies_and_edge_architecture.shadow_traffic import (
    SAFE_METHODS,
    ASGIApp,
    Captured,
    DiffKind,
    Message,
    MirrorPolicy,
    Outcome,
    Receive,
    RefuseShadowWrites,
    Scope,
    Send,
    ShadowTelemetry,
    ShadowTraffic,
    Skip,
    compare_responses,
    diff_json,
    is_sampled,
    noise_paths,
    sample_bucket,
    shadow_host,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

PRIMARY_JSON = {"id": 7, "name": "widget", "price": 10}


class RecordingApp:
    """An ASGI app that records what it saw and answers with a configurable response."""

    def __init__(
        self,
        payload: Any = None,
        *,
        status: int = 200,
        gate: asyncio.Event | None = None,
        fail: bool = False,
        make_response: Callable[[], Response] | None = None,
    ) -> None:
        self.payload = PRIMARY_JSON if payload is None else payload
        self.status = status
        self.gate = gate
        self.fail = fail
        self.make_response = make_response
        self.entered = asyncio.Event()
        self.calls = 0
        self.writes = 0
        self.side_effects = 0
        self.bodies: list[bytes] = []
        self.headers: list[list[tuple[str, str]]] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        self.entered.set()
        request = Request(scope, receive)
        self.headers.append([(k, v) for k, v in request.headers.items()])
        self.bodies.append(await request.body())
        if scope["method"] not in SAFE_METHODS:
            self.writes += 1
        if scope["path"].startswith("/admin"):
            self.side_effects += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("candidate exploded")
        if self.make_response is not None:
            await self.make_response()(scope, receive, send)
            return
        await JSONResponse(self.payload, status_code=self.status)(scope, receive, send)


def client_for(app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def call(
    mirror: ShadowTraffic, method: str = "GET", path: str = "/items", **kwargs: Any
) -> httpx.Response:
    async with asyncio.timeout(5), client_for(mirror) as client:
        return await client.request(method, path, **kwargs)


# --- isolation of the primary -------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_gets_primary_response_while_candidate_is_still_running() -> None:
    gate = asyncio.Event()
    candidate = RecordingApp(gate=gate)
    mirror = ShadowTraffic(RecordingApp(), candidate)

    response = await call(mirror)

    assert response.status_code == 200
    assert response.json() == PRIMARY_JSON
    await asyncio.wait_for(candidate.entered.wait(), 5)
    assert mirror.telemetry.started == 1
    assert not mirror.telemetry.outcomes  # the candidate has not finished, the client already has
    gate.set()
    await mirror.drain()
    assert mirror.telemetry.outcomes == {Outcome.MATCH: 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_candidate_exception_does_not_reach_the_client() -> None:
    mirror = ShadowTraffic(RecordingApp(), RecordingApp(fail=True))

    response = await call(mirror)
    await mirror.drain()

    assert response.status_code == 200
    assert response.json() == PRIMARY_JSON
    assert mirror.telemetry.outcomes == {Outcome.CANDIDATE_ERROR: 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_candidate_timeout_is_recorded_and_primary_is_unaffected() -> None:
    candidate = RecordingApp(gate=asyncio.Event())  # never released
    mirror = ShadowTraffic(RecordingApp(), candidate, MirrorPolicy(timeout_s=0.01))

    response = await call(mirror)
    await mirror.drain()

    assert response.status_code == 200
    assert mirror.telemetry.outcomes == {Outcome.CANDIDATE_TIMEOUT: 1}
    assert mirror.telemetry.records[0].outcome is Outcome.CANDIDATE_TIMEOUT


@pytest.mark.unit
@pytest.mark.asyncio
async def test_primary_exception_propagates_and_shadow_is_not_comparable() -> None:
    mirror = ShadowTraffic(RecordingApp(fail=True), RecordingApp())

    with pytest.raises(RuntimeError, match="candidate exploded"):
        await call(mirror)
    await mirror.drain()

    assert mirror.telemetry.outcomes == {Outcome.NOT_COMPARABLE: 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_drain_cancels_candidates_that_outlive_the_grace_period() -> None:
    candidate = RecordingApp(gate=asyncio.Event())
    mirror = ShadowTraffic(RecordingApp(), candidate, MirrorPolicy(timeout_s=30))

    await call(mirror)
    await asyncio.wait_for(candidate.entered.wait(), 5)
    await mirror.drain(grace_s=0.01)

    assert mirror.telemetry.outcomes == {Outcome.CANCELLED: 1}
    assert mirror.pending == 0  # the task set must not leak finished tasks


@pytest.mark.unit
@pytest.mark.asyncio
async def test_drain_during_a_slow_primary_does_not_break_the_primary_response() -> None:
    primary_gate = asyncio.Event()
    primary = RecordingApp(gate=primary_gate)
    mirror = ShadowTraffic(primary, RecordingApp())

    request = asyncio.create_task(call(mirror))
    await asyncio.wait_for(primary.entered.wait(), 5)
    await mirror.drain(grace_s=0.05)  # the candidate is done and waits on the primary: cancelled
    primary_gate.set()
    response = await request

    assert response.status_code == 200
    assert mirror.telemetry.outcomes == {Outcome.CANCELLED: 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_http_scopes_bypass_the_mirror() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    mirror = ShadowTraffic(app, RecordingApp())
    await mirror({"type": "lifespan"}, _never_receive, _discard_send)

    assert seen == ["lifespan"]
    assert mirror.telemetry.started == 0


async def _never_receive() -> dict[str, Any]:
    raise AssertionError("receive must not be called")


async def _discard_send(message: Message) -> None:
    return None


# --- the mutation guard --------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsafe_methods_are_not_mirrored_by_default() -> None:
    primary, candidate = RecordingApp(status=201), RecordingApp()
    mirror = ShadowTraffic(primary, candidate)

    response = await call(mirror, "POST", json={"n": 1})
    await mirror.drain()

    assert response.status_code == 201
    assert (primary.writes, candidate.calls, candidate.writes) == (1, 0, 0)
    assert mirror.telemetry.skipped == {Skip.METHOD: 1}
    assert mirror.telemetry.started == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_get_with_side_effects_is_mirrored_unless_the_path_is_denied() -> None:
    open_candidate = RecordingApp()
    open_mirror = ShadowTraffic(RecordingApp(), open_candidate)
    await call(open_mirror, "GET", "/admin/purge")
    await open_mirror.drain()

    guarded_candidate = RecordingApp()
    guarded = ShadowTraffic(
        RecordingApp(), guarded_candidate, MirrorPolicy(deny_path_prefixes=("/admin",))
    )
    await call(guarded, "GET", "/admin/purge")
    await guarded.drain()

    assert open_candidate.side_effects == 1  # the method guard cannot see a mutating GET
    assert guarded_candidate.side_effects == 0
    assert guarded.telemetry.skipped == {Skip.PATH: 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_opt_out_header_skips_mirroring() -> None:
    candidate = RecordingApp()
    mirror = ShadowTraffic(RecordingApp(), candidate)

    await call(mirror, headers={"x-no-shadow": "1"})
    await mirror.drain()

    assert candidate.calls == 0
    assert mirror.telemetry.skipped == {Skip.OPT_OUT: 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_opted_in_post_reaches_the_candidate_with_an_identical_body() -> None:
    primary, candidate = RecordingApp(status=201), RecordingApp(status=201)
    policy = MirrorPolicy(methods=SAFE_METHODS | {"POST"})
    mirror = ShadowTraffic(primary, candidate, policy)

    await call(mirror, "POST", content=b'{"n":1}')
    await mirror.drain()

    assert primary.bodies == [b'{"n":1}']
    assert candidate.bodies == [b'{"n":1}']
    assert candidate.writes == 1  # without a candidate-side guard the mirrored write happens


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refuse_shadow_writes_blocks_a_marked_post_at_the_candidate() -> None:
    primary, inner = RecordingApp(status=201), RecordingApp(status=201)
    policy = MirrorPolicy(methods=SAFE_METHODS | {"POST"})
    mirror = ShadowTraffic(primary, RefuseShadowWrites(inner), policy)

    await call(mirror, "POST", content=b"{}")
    await mirror.drain()

    assert (primary.writes, inner.calls, inner.writes) == (1, 0, 0)
    record = mirror.telemetry.records[0]
    assert record.outcome is Outcome.MISMATCH
    assert record.diffs[0].kind is DiffKind.STATUS


# --- admission limits ----------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_overload_sheds_instead_of_queueing_and_frees_the_slot_afterwards() -> None:
    gate = asyncio.Event()
    candidate = RecordingApp(gate=gate)
    mirror = ShadowTraffic(RecordingApp(), candidate, MirrorPolicy(max_in_flight=1))

    first = await call(mirror)
    second = await call(mirror)

    assert (first.status_code, second.status_code) == (200, 200)
    assert mirror.telemetry.started == 1
    assert mirror.telemetry.skipped == {Skip.OVERLOAD: 1}
    gate.set()
    await mirror.drain()
    await call(mirror)
    await mirror.drain()
    assert mirror.telemetry.started == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_declared_oversized_body_is_not_mirrored_but_reaches_the_primary() -> None:
    primary, candidate = RecordingApp(), RecordingApp()
    policy = MirrorPolicy(methods=SAFE_METHODS | {"PUT"}, max_body_bytes=8)
    mirror = ShadowTraffic(primary, candidate, policy)

    await call(mirror, "PUT", content=b"x" * 20)
    await mirror.drain()

    assert primary.bodies == [b"x" * 20]
    assert candidate.calls == 0
    assert mirror.telemetry.skipped == {Skip.BODY_TOO_LARGE: 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chunked_body_over_the_cap_reaches_the_primary_in_full() -> None:
    async def parts() -> AsyncIterator[bytes]:
        for chunk in (b"aaaaaa", b"bbbbbb", b"cccccc"):
            yield chunk

    primary, candidate = RecordingApp(), RecordingApp()
    policy = MirrorPolicy(methods=SAFE_METHODS | {"PUT"}, max_body_bytes=8)
    mirror = ShadowTraffic(primary, candidate, policy)

    await call(mirror, "PUT", content=parts())  # no content-length: the cap is hit mid-stream
    await mirror.drain()

    assert primary.bodies == [b"aaaaaabbbbbbcccccc"]
    assert candidate.calls == 0
    assert mirror.telemetry.skipped == {Skip.BODY_TOO_LARGE: 1}


# --- sampling ------------------------------------------------------------------------------


@pytest.mark.unit
def test_sampling_is_deterministic_and_raising_the_fraction_only_adds_ids() -> None:
    ids = [f"req-{i}" for i in range(2000)]
    ten = {i for i in ids if is_sampled(i, 10, 100)}
    fifty = {i for i in ids if is_sampled(i, 50, 100)}

    assert ten == {i for i in ids if is_sampled(i, 10, 100)}
    assert ten < fifty  # strict subset
    assert 100 <= len(ten) <= 300  # about 10 percent of 2000
    assert all(0 <= sample_bucket(i, 100) < 100 for i in ids)
    assert not any(is_sampled(i, 0, 100) for i in ids)
    assert all(is_sampled(i, 100, 100) for i in ids)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_mirrors_exactly_the_sampled_request_ids() -> None:
    ids = [f"req-{i}" for i in range(40)]
    candidate = RecordingApp()
    # No I/O yields between sequential calls, so candidate tasks queue up until drain():
    # raise the in-flight cap so only the sampling decision is under test.
    policy = MirrorPolicy(numerator=1, denominator=2, max_in_flight=100)
    mirror = ShadowTraffic(RecordingApp(), candidate, policy)

    for request_id in ids:
        await call(mirror, headers={"x-request-id": request_id})
    await mirror.drain()

    expected = {i for i in ids if is_sampled(i, 1, 2)}
    mirrored = {dict(h)["x-request-id"] for h in candidate.headers}
    assert mirrored == expected
    assert 0 < len(expected) < len(ids)
    assert mirror.telemetry.skipped[Skip.SAMPLE] == len(ids) - len(expected)


# --- what the candidate sees ---------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("api.example", "api.example-shadow"),
        ("api.example:8080", "api.example-shadow:8080"),
        ("localhost", "localhost-shadow"),
        ("[::1]:80", "[::1]:80"),
    ],
)
def test_shadow_host_appends_the_suffix_before_the_port(host: str, expected: str) -> None:
    assert shadow_host(host) == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_candidate_gets_shadow_host_marker_and_dropped_headers() -> None:
    primary, candidate = RecordingApp(), RecordingApp()
    mirror = ShadowTraffic(primary, candidate, MirrorPolicy(drop_headers=frozenset({"cookie"})))

    cookie = "sid=EXAMPLE-NOT-A-REAL-SESSION"
    await call(mirror, headers={"cookie": cookie, "x-shadow-request": "9"})
    await mirror.drain()

    primary_headers = dict(primary.headers[0])
    candidate_headers = candidate.headers[0]
    assert primary_headers["host"] == "test"
    assert primary_headers["cookie"] == cookie
    assert "x-shadow-request" not in primary_headers  # a client cannot pose as shadow traffic
    assert dict(candidate_headers)["host"] == "test-shadow"
    assert "cookie" not in dict(candidate_headers)
    assert [v for k, v in candidate_headers if k == "x-shadow-request"] == ["1"]


# --- the differ ----------------------------------------------------------------------------


@pytest.mark.unit
def test_diff_json_reports_each_kind_with_pointer_paths() -> None:
    primary = {"a/b": 1, "gone": 1, "n": None, "flag": True, "list": [1, 2], "ok": "same"}
    candidate = {"a/b": 2, "new": 1, "n": 0, "flag": 1, "list": [1], "ok": "same"}

    found = {(d.path, d.kind) for d in diff_json(primary, candidate)}

    assert found == {
        ("/a~1b", DiffKind.CHANGED),
        ("/gone", DiffKind.MISSING),
        ("/new", DiffKind.EXTRA),
        ("/n", DiffKind.TYPE),
        ("/flag", DiffKind.TYPE),
        ("/list", DiffKind.LENGTH),
    }


@pytest.mark.unit
def test_ignore_patterns_match_one_segment_and_tolerance_absorbs_float_noise() -> None:
    primary = {"items": [{"updated_at": 1, "meta": {"updated_at": 1}}], "ratio": 1.0}
    candidate = {"items": [{"updated_at": 2, "meta": {"updated_at": 2}}], "ratio": 1.004}

    assert len(diff_json(primary, candidate)) == 3
    ignored = diff_json(primary, candidate, ignore={"/items/*/updated_at"}, float_tolerance=0.01)
    assert [d.path for d in ignored] == ["/items/0/meta/updated_at"]  # '*' does not cross levels
    assert diff_json(primary, {**candidate, "ratio": 1.02}, ignore={"/items"}, float_tolerance=0.01)


@pytest.mark.unit
def test_noise_learned_from_two_primary_runs_is_excluded_from_the_candidate_diff() -> None:
    run_a = {"ts": 1, "v": 1}
    run_b = {"ts": 2, "v": 1}
    noise = noise_paths(run_a, run_b)
    candidate = {"ts": 3, "v": 2}

    assert noise == {"/ts"}
    assert [d.path for d in diff_json(run_a, candidate)] == ["/ts", "/v"]
    assert [d.path for d in diff_json(run_a, candidate, ignore=noise)] == ["/v"]


def _captured(status: int, body: bytes, content_type: str, cap: int = 1024) -> Captured:
    captured = Captured(cap=cap)
    captured.observe(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", content_type.encode())],
        }
    )
    captured.observe({"type": "http.response.body", "body": body})
    return captured


@pytest.mark.unit
def test_compare_responses_handles_non_json_invalid_json_and_truncation() -> None:
    policy = MirrorPolicy()
    text = _captured(200, b"alpha", "text/plain")

    outcome, diffs = compare_responses(text, _captured(200, b"beta", "text/plain"), policy)
    assert (outcome, [d.kind for d in diffs]) == (Outcome.MISMATCH, [DiffKind.BYTES])

    good = _captured(200, b'{"a":1}', "application/json")
    outcome, diffs = compare_responses(good, _captured(200, b"{oops", "application/json"), policy)
    assert (outcome, [d.kind for d in diffs]) == (Outcome.MISMATCH, [DiffKind.INVALID_JSON])

    outcome, diffs = compare_responses(good, _captured(200, b'{"a":1}', "text/html"), policy)
    assert [d.kind for d in diffs] == [DiffKind.HEADER]  # same body, different content-type

    big = _captured(200, b"x" * 20, "text/plain", cap=10)
    assert big.truncated
    assert compare_responses(text, big, policy) == (Outcome.NOT_COMPARABLE, ())


# --- telemetry ------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mismatch_records_paths_and_kinds_but_never_raw_values() -> None:
    primary = RecordingApp({"id": 1, "token": "primary-EXAMPLE-SECRET", "tags": ["a"]})
    candidate = RecordingApp({"id": 1, "token": "candidate-EXAMPLE-SECRET", "tags": ["a", "b"]})
    mirror = ShadowTraffic(primary, candidate)

    await call(mirror)
    await mirror.drain()

    (record,) = mirror.telemetry.records
    assert record.outcome is Outcome.MISMATCH
    assert [(d.path, d.kind) for d in record.diffs] == [
        ("/tags", DiffKind.LENGTH),
        ("/token", DiffKind.CHANGED),
    ]
    token = record.diffs[1]
    assert token.primary_digest != token.candidate_digest
    assert "EXAMPLE-SECRET" not in repr(record)


@pytest.mark.unit
def test_digests_are_keyed_so_equal_values_match_only_under_the_same_key() -> None:
    one = ShadowTelemetry(b"k" * 16)
    same = ShadowTelemetry(b"k" * 16)
    other = ShadowTelemetry(b"j" * 16)

    assert one.digest(200) == same.digest(200)
    assert one.digest(200) != other.digest(200)
    assert one.digest(200) != one.digest(500)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_buffer_is_bounded_but_counters_keep_counting() -> None:
    telemetry = ShadowTelemetry(max_records=2)
    mirror = ShadowTraffic(RecordingApp(), RecordingApp({"id": 2}), telemetry=telemetry)

    for _ in range(3):
        await call(mirror)
    await mirror.drain()

    assert len(telemetry.records) == 2
    assert telemetry.outcomes == {Outcome.MISMATCH: 3}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_difference_between_primary_and_candidate_is_reported() -> None:
    mirror = ShadowTraffic(RecordingApp(), RecordingApp(status=500))

    await call(mirror)
    await mirror.drain()

    (record,) = mirror.telemetry.records
    assert (record.diffs[0].path, record.diffs[0].kind) == ("status", DiffKind.STATUS)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ignore_policy_turns_a_noisy_field_into_a_match() -> None:
    primary = RecordingApp({"id": 1, "ts": 100})
    candidate = RecordingApp({"id": 1, "ts": 999})
    strict = ShadowTraffic(primary, candidate)
    skip_ts = replace(MirrorPolicy(), ignore_paths=frozenset({"/ts"}))
    tolerant = ShadowTraffic(primary, candidate, skip_ts)

    await call(strict)
    await call(tolerant)
    await strict.drain()
    await tolerant.drain()

    assert strict.telemetry.outcomes == {Outcome.MISMATCH: 1}
    assert tolerant.telemetry.outcomes == {Outcome.MATCH: 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_response_larger_than_the_capture_cap_is_not_comparable() -> None:
    payload = {"blob": "x" * 200}
    mirror = ShadowTraffic(
        RecordingApp(payload), RecordingApp(payload), MirrorPolicy(max_capture_bytes=50)
    )

    response = await call(mirror)
    await mirror.drain()

    assert json.loads(response.content) == payload  # the client still gets the whole body
    assert mirror.telemetry.outcomes == {Outcome.NOT_COMPARABLE: 1}


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [
        {"denominator": 0},
        {"numerator": 101},
        {"numerator": -1},
        {"max_in_flight": 0},
        {"timeout_s": 0},
    ],
)
def test_policy_rejects_nonsense_settings(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"need|must"):
        MirrorPolicy(**kwargs)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_plain_text_endpoints_are_compared_byte_for_byte() -> None:
    primary = RecordingApp(make_response=lambda: PlainTextResponse("v1"))
    candidate = RecordingApp(make_response=lambda: PlainTextResponse("v2"))
    mirror = ShadowTraffic(primary, candidate)

    await call(mirror)
    await mirror.drain()

    assert mirror.telemetry.records[0].diffs[0].kind is DiffKind.BYTES
