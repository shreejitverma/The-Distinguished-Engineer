"""Service mesh architecture: policy semantics at each enforcement point, rotation, and HBONE."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from h2.exceptions import TooManyStreamsError

from api_labs.f17_api_gateways_proxies_and_edge_architecture.service_mesh import (
    LATENCY_BAND_MS,
    RETRY_MAX_S,
    Action,
    Attach,
    CaOutage,
    DataPlane,
    EnvoyEnforcer,
    Fleet,
    HbonePool,
    Identity,
    ImpersonationDenied,
    MtlsMode,
    NodeAuthorizer,
    Operation,
    PathNormalization,
    PeerRejected,
    Pod,
    Policy,
    Request,
    Rule,
    Source,
    Workload,
    ZtMatch,
    deliver,
    effective_mtls_mode,
    evaluate_ztunnel,
    footprint,
    h2_pair,
    issue_svid,
    lower_peer_authentication,
    lower_policy,
    make_authority,
    max_waypoints_before_ambient_costs_more,
    parse_spiffe_id,
    principal_pattern_to_regex,
    proxy_hops,
    refresh_deadline,
    rotate_in,
    serve_hbone,
    simulate_rotation,
    string_match,
    svid_identity,
    tunnel_bytes,
    verify_peer,
    zt_matches,
)

pytestmark = pytest.mark.unit

TD = "cluster.local"
FRONT = Identity(TD, "shop", "frontend")
REPORTS = Identity(TD, "shop", "reports")
ORDERS_ID = Identity(TD, "shop", "orders")
WAYPOINT = Identity(TD, "shop", "waypoint")
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
DAY = 86400.0


def req(
    src: Identity | None = FRONT,
    method: str | None = "GET",
    path: str | None = "/orders/1",
    port: int = 8080,
    **kw: object,
) -> Request:
    return Request(src, "10.0.0.9", port, method, path, **kw)  # type: ignore[arg-type]


def only(*, principals: tuple[str, ...] = (), **op: object) -> Rule:
    return Rule(
        from_=(Source(principals=principals),) if principals else (),
        to=(Operation(**op),) if op else (),  # type: ignore[arg-type]
    )


ORDERS = Workload("orders", ORDERS_ID, "node-a", "10.0.1.5", waypoint=WAYPOINT)
GET_ONLY = Policy(
    "get-only",
    Action.ALLOW,
    (only(principals=(FRONT.principal,), methods=("GET",)),),
    Attach.TARGET_REF,
)
ONLY_WAYPOINT = Policy("only-waypoint", Action.ALLOW, (only(principals=(WAYPOINT.principal,)),))


# ---- identity ------------------------------------------------------------------------------


def test_parse_spiffe_id_enforces_layout_and_limits() -> None:
    assert parse_spiffe_id("spiffe://cluster.local/ns/shop/sa/frontend") == FRONT
    for bad in (
        "spiffe://Cluster.Local/ns/shop/sa/frontend",
        "spiffe://cluster.local/ns/shop/sa/frontend?x=1",
        "https://cluster.local/ns/shop/sa/frontend",
        "spiffe://cluster.local/",
        "spiffe://cluster.local/ns/shop/sa/" + "a" * 2048,
    ):
        with pytest.raises(ValueError, match=r"SPIFFE|spiffe"):
            parse_spiffe_id(bad)


# ---- Envoy enforcement point ---------------------------------------------------------------


def test_deny_is_evaluated_before_allow_and_no_allow_policy_means_allow() -> None:
    deny = Policy("no-delete", Action.DENY, (only(methods=("DELETE",)),))
    allow_all = Policy("allow-all", Action.ALLOW, (Rule(),))
    envoy = EnvoyEnforcer()
    assert envoy.evaluate([], req()).allowed
    assert envoy.evaluate([deny], req(method="GET")).allowed
    denied = envoy.evaluate([deny, allow_all], req(method="DELETE"))
    assert not denied.allowed
    assert denied.policy == "no-delete"


def test_allow_policy_without_rules_denies_everything_and_empty_rule_allows_everything() -> None:
    envoy = EnvoyEnforcer()
    nothing = Policy("allow-nothing", Action.ALLOW, ())
    everything = Policy("allow-all", Action.ALLOW, (Rule(),))
    assert not envoy.evaluate([nothing], req()).allowed
    assert envoy.evaluate([everything], req()).allowed


@pytest.mark.parametrize(
    ("pattern", "value", "expected"),
    [
        ("abc", "abc", True),
        ("abc", "abcd", False),
        ("abc*", "abc", True),
        ("abc*", "abcd", True),
        ("abc*", "xabc", False),
        ("*abc", "xabc", True),
        ("*abc", "abcx", False),
        ("*", "x", True),
        ("*", "", False),
    ],
)
def test_string_match_exact_prefix_suffix_and_presence(
    pattern: str, value: str, expected: bool
) -> None:
    assert string_match(pattern, value) is expected


def test_method_and_path_authorization_between_workload_identities() -> None:
    policy = Policy(
        "frontend-reads-orders",
        Action.ALLOW,
        (only(principals=(FRONT.principal,), methods=("GET",), paths=("/orders/*",)),),
    )
    envoy = EnvoyEnforcer()
    assert envoy.evaluate([policy], req()).allowed
    assert not envoy.evaluate([policy], req(method="POST")).allowed
    assert not envoy.evaluate([policy], req(path="/admin/users")).allowed
    assert not envoy.evaluate([policy], req(src=REPORTS)).allowed
    assert not envoy.evaluate([policy], req(src=None)).allowed


def test_deny_treats_missing_http_attributes_as_a_match_but_allow_does_not() -> None:
    tcp = req(method=None, path=None, port=8080)
    deny = Policy("deny-post", Action.DENY, (only(methods=("POST",)),))
    scoped = Policy("deny-post-8080", Action.DENY, (only(methods=("POST",), ports=(9090,)),))
    allow = Policy("allow-get", Action.ALLOW, (only(methods=("GET",)),))
    envoy = EnvoyEnforcer()
    assert not envoy.evaluate([deny], tcp).allowed  # unscoped DENY swallows all TCP
    assert envoy.evaluate([scoped], tcp).allowed  # a port scope contains the blast radius
    assert not envoy.evaluate([allow], tcp).allowed


@pytest.mark.parametrize(
    ("path", "blocked_by"),
    [
        ("/data/secret", set(PathNormalization)),
        (
            "/data//secret",
            {PathNormalization.MERGE_SLASHES, PathNormalization.DECODE_AND_MERGE_SLASHES},
        ),
        ("/data/x/../secret", set(PathNormalization) - {PathNormalization.NONE}),
        ("/data%2Fsecret", {PathNormalization.DECODE_AND_MERGE_SLASHES}),
        ("\\data\\secret", set(PathNormalization) - {PathNormalization.NONE}),
    ],
)
def test_deny_on_a_path_is_only_as_strong_as_the_normalization(
    path: str, blocked_by: set[PathNormalization]
) -> None:
    deny = Policy("deny-secret", Action.DENY, (only(paths=("/data/secret",)),))
    for mode in PathNormalization:
        decision = EnvoyEnforcer(path_normalization=mode).evaluate([deny], req(path=path))
        assert (not decision.allowed) is (mode in blocked_by), (path, mode)


def test_allow_with_positive_paths_fails_closed_under_every_normalization() -> None:
    allow = Policy("allow-public", Action.ALLOW, (only(paths=("/public",)),))
    for mode in PathNormalization:
        envoy = EnvoyEnforcer(path_normalization=mode)
        assert envoy.evaluate([allow], req(path="/public")).allowed
        for odd in ("/public/x", "/Public", "/public%2F", "/other"):
            assert not envoy.evaluate([allow], req(path=odd)).allowed, (mode, odd)


def test_http2_method_case_bypass_and_its_fix() -> None:
    deny = Policy("deny-post", Action.DENY, (only(methods=("POST",)),))
    allow_get = Policy("allow-get", Action.ALLOW, (only(methods=("GET",)),))
    lower_h2 = req(method="post", http_version="2")
    lower_h1 = req(method="post", http_version="1.1")
    prefix = EnvoyEnforcer(case_sensitive_methods=True)
    fixed = EnvoyEnforcer()
    assert prefix.evaluate([deny], lower_h2).allowed  # the bypass
    assert not fixed.evaluate([deny], lower_h2).allowed
    assert not prefix.evaluate([deny], lower_h1).allowed  # the HTTP/1.1 codec rejects it first
    assert "codec" in prefix.evaluate([deny], lower_h1).reason
    assert not prefix.evaluate([allow_get], lower_h2).allowed  # explicit ALLOW lists fail closed


def test_unescaped_dot_in_a_service_account_pattern_widens_the_match() -> None:
    pattern = "cluster.local/ns/shop/sa/billing.api"
    exact = "cluster.local/ns/shop/sa/billing.api"
    lookalike = "cluster.local/ns/shop/sa/billing-api"
    loose = principal_pattern_to_regex(pattern, escape=False)
    strict = principal_pattern_to_regex(pattern, escape=True)
    assert loose.fullmatch(exact)
    assert loose.fullmatch(lookalike)
    assert strict.fullmatch(exact)
    assert strict.fullmatch(lookalike) is None


# ---- ztunnel: lowering and L4 evaluation ---------------------------------------------------


def test_ztunnel_enforces_principal_and_port() -> None:
    policy = Policy(
        "frontend-8080",
        Action.ALLOW,
        (
            Rule(
                from_=(Source(principals=(FRONT.principal,)),),
                to=(Operation(ports=(8080,)),),
            ),
        ),
    )
    assert evaluate_ztunnel([policy], req()).allowed
    assert not evaluate_ztunnel([policy], req(src=REPORTS)).allowed
    assert not evaluate_ztunnel([policy], req(port=9090)).allowed


def test_allow_with_an_l7_attribute_is_dropped_so_ztunnel_denies_what_envoy_allows() -> None:
    policy = Policy(
        "get-from-frontend", Action.ALLOW, (only(principals=(FRONT.principal,), methods=("GET",)),)
    )
    lowered = lower_policy(policy)
    assert lowered is not None
    assert lowered.groups == ()
    assert lowered.unsupported == ("methods",)
    assert EnvoyEnforcer().evaluate([policy], req()).allowed
    assert not evaluate_ztunnel([policy], req()).allowed


def test_deny_with_only_l7_attributes_is_a_no_op_at_ztunnel() -> None:
    require_jwt = Policy(
        "require-jwt", Action.DENY, (Rule(from_=(Source(not_request_principals=("*",)),)),)
    )
    anonymous = req()
    assert not EnvoyEnforcer().evaluate([require_jwt], anonymous).allowed
    assert evaluate_ztunnel([require_jwt], anonymous).allowed
    with_jwt = req(request_principal="https://idp.example/alice")
    assert EnvoyEnforcer().evaluate([require_jwt], with_jwt).allowed


def test_deny_mixing_l4_and_l7_in_one_entry_enforces_only_the_l4_half() -> None:
    deny = Policy("deny-post-8080", Action.DENY, (only(ports=(8080,), methods=("POST",)),))
    assert EnvoyEnforcer().evaluate([deny], req(method="GET")).allowed
    assert not EnvoyEnforcer().evaluate([deny], req(method="POST")).allowed
    assert not evaluate_ztunnel([deny], req(method="GET")).allowed  # broader than intended
    assert evaluate_ztunnel([deny], req(method="GET", port=9090)).allowed


def test_deny_splitting_l4_and_l7_across_entries_never_matches_at_ztunnel() -> None:
    deny = Policy(
        "deny-reports-post",
        Action.DENY,
        (
            Rule(
                from_=(Source(principals=(REPORTS.principal,)),),
                to=(Operation(methods=("POST",)),),
            ),
        ),
    )
    post = req(src=REPORTS, method="POST")
    assert not EnvoyEnforcer().evaluate([deny], post).allowed
    assert evaluate_ztunnel([deny], post).allowed


def test_empty_rule_matches_everything_at_ztunnel_and_no_rules_matches_nothing() -> None:
    assert evaluate_ztunnel([Policy("all", Action.ALLOW, (Rule(),))], req(src=None)).allowed
    assert not evaluate_ztunnel([Policy("none", Action.ALLOW, ())], req()).allowed


def test_plaintext_has_no_identity_and_a_not_principals_deny_rejects_it() -> None:
    deny = Policy("require-mtls", Action.DENY, (Rule(from_=(Source(not_principals=("*",)),)),))
    assert not evaluate_ztunnel([deny], req(src=None)).allowed
    assert evaluate_ztunnel([deny], req(src=FRONT)).allowed


# ---- traffic paths ------------------------------------------------------------------------


def test_sidecar_evaluates_l7_policy_with_the_original_identity() -> None:
    policy = Policy("get-only", GET_ONLY.action, GET_ONLY.rules)
    ok = deliver(DataPlane.SIDECAR, req(), ORDERS, [policy])
    assert ok.allowed
    assert [h.proxy for h in ok.hops] == ["dest-sidecar"]
    assert not deliver(DataPlane.SIDECAR, req(method="POST"), ORDERS, [policy]).allowed
    assert not deliver(DataPlane.SIDECAR, req(src=REPORTS), ORDERS, [policy]).allowed


def test_waypoint_enforces_l7_and_ztunnel_then_sees_the_waypoint_identity() -> None:
    policies = [GET_ONLY, ONLY_WAYPOINT]
    ok = deliver(DataPlane.AMBIENT, req(), ORDERS, policies)
    assert ok.allowed
    assert ok.via_waypoint
    assert [h.proxy for h in ok.hops] == ["waypoint", "dest-ztunnel"]
    post = deliver(DataPlane.AMBIENT, req(method="POST"), ORDERS, policies)
    assert not post.allowed
    assert [h.proxy for h in post.hops] == ["waypoint"]
    by_client_identity = Policy(
        "only-frontend", Action.ALLOW, (only(principals=(FRONT.principal,)),)
    )
    blocked = deliver(DataPlane.AMBIENT, req(), ORDERS, [GET_ONLY, by_client_identity])
    assert not blocked.allowed
    assert blocked.hops[-1].proxy == "dest-ztunnel"


def test_dialing_the_pod_ip_bypasses_a_service_waypoint_until_ztunnel_requires_it() -> None:
    post = req(method="POST")
    bypass = deliver(DataPlane.AMBIENT, post, ORDERS, [GET_ONLY], dial="workload")
    assert bypass.allowed
    assert not bypass.via_waypoint
    via_service = deliver(DataPlane.AMBIENT, post, ORDERS, [GET_ONLY], dial="service")
    assert not via_service.allowed
    closed = deliver(DataPlane.AMBIENT, post, ORDERS, [GET_ONLY, ONLY_WAYPOINT], dial="workload")
    assert not closed.allowed
    assert closed.hops[-1].proxy == "dest-ztunnel"


def test_waypoint_for_selects_which_dials_traverse_the_waypoint() -> None:
    post = req(method="POST")
    cases: list[
        tuple[Literal["service", "workload", "all", "none"], Literal["service", "workload"], bool]
    ] = [
        ("all", "workload", True),
        ("workload", "workload", True),
        ("workload", "service", False),
        ("none", "service", False),
        ("service", "service", True),
    ]
    for waypoint_for, dial, traverses in cases:
        result = deliver(
            DataPlane.AMBIENT,
            post,
            ORDERS,
            [GET_ONLY],
            dial=dial,
            waypoint_for=waypoint_for,
        )
        assert result.via_waypoint is traverses, (waypoint_for, dial)
        assert result.allowed is (not traverses)


def test_a_waypoint_policy_does_nothing_when_the_destination_has_no_waypoint() -> None:
    bare = Workload("orders", ORDERS_ID, "node-a", "10.0.1.5")
    result = deliver(DataPlane.AMBIENT, req(method="POST"), bare, [GET_ONLY])
    assert result.allowed
    assert [h.proxy for h in result.hops] == ["dest-ztunnel"]


# ---- PeerAuthentication in ambient --------------------------------------------------------


def test_strict_lowers_to_a_deny_on_a_missing_principal() -> None:
    auth = lower_peer_authentication(MtlsMode.STRICT, {})
    assert auth is not None
    assert zt_matches(auth, req(src=None))
    assert not zt_matches(auth, req(src=FRONT))
    assert lower_peer_authentication(MtlsMode.PERMISSIVE, {}) is None


def test_port_level_strict_holds_under_a_permissive_workload_in_a_strict_mesh() -> None:
    ports = {8443: MtlsMode.STRICT}
    workload = effective_mtls_mode(
        MtlsMode.STRICT, MtlsMode.UNSET, MtlsMode.PERMISSIVE, ports, 9999
    )
    assert workload is MtlsMode.PERMISSIVE
    assert (
        effective_mtls_mode(MtlsMode.STRICT, MtlsMode.UNSET, MtlsMode.PERMISSIVE, ports, 8443)
        is MtlsMode.STRICT
    )
    auth = lower_peer_authentication(workload, ports)
    assert auth is not None
    assert zt_matches(auth, req(src=None, port=8443))
    assert not zt_matches(auth, req(src=None, port=8080))
    assert not zt_matches(auth, req(src=FRONT, port=8443))


def test_strict_workload_exempts_only_its_permissive_port() -> None:
    auth = lower_peer_authentication(MtlsMode.STRICT, {8080: MtlsMode.PERMISSIVE})
    assert auth is not None
    assert not zt_matches(auth, req(src=None, port=8080))
    assert zt_matches(auth, req(src=None, port=9090))


def test_mtls_mode_inheritance_defaults_to_permissive() -> None:
    unset = MtlsMode.UNSET
    assert effective_mtls_mode(unset, unset, unset, {}, 80) is MtlsMode.PERMISSIVE
    assert effective_mtls_mode(MtlsMode.STRICT, unset, unset, {}, 80) is MtlsMode.STRICT
    assert (
        effective_mtls_mode(MtlsMode.STRICT, MtlsMode.PERMISSIVE, unset, {}, 80)
        is MtlsMode.PERMISSIVE
    )
    assert (
        effective_mtls_mode(MtlsMode.STRICT, MtlsMode.PERMISSIVE, MtlsMode.STRICT, {}, 80)
        is MtlsMode.STRICT
    )
    assert (
        effective_mtls_mode(MtlsMode.STRICT, unset, MtlsMode.STRICT, {80: MtlsMode.DISABLE}, 80)
        is MtlsMode.DISABLE
    )


# ---- X.509 SVIDs ---------------------------------------------------------------------------


def test_refresh_deadline_is_the_half_life_of_a_real_certificate() -> None:
    authority = make_authority("root", now=NOW)
    cert = issue_svid(authority, [FRONT.spiffe_id], now=NOW, ttl=timedelta(hours=24))
    assert refresh_deadline(cert) == NOW + timedelta(hours=12)


def test_an_svid_must_carry_exactly_one_spiffe_uri_san() -> None:
    authority = make_authority("root", now=NOW)
    ttl = timedelta(hours=24)
    assert svid_identity(issue_svid(authority, [FRONT.spiffe_id], now=NOW, ttl=ttl)) == FRONT
    two = issue_svid(authority, [FRONT.spiffe_id, REPORTS.spiffe_id], now=NOW, ttl=ttl)
    with pytest.raises(PeerRejected, match="found 2"):
        svid_identity(two)
    none = issue_svid(authority, [], now=NOW, ttl=ttl, dns_names=["orders.shop.svc"])
    with pytest.raises(PeerRejected, match="found 0"):
        svid_identity(none)
    web = issue_svid(authority, ["https://cluster.local/ns/shop/sa/frontend"], now=NOW, ttl=ttl)
    with pytest.raises(PeerRejected, match="spiffe"):
        svid_identity(web)
    with pytest.raises(PeerRejected, match="CA certificate"):
        svid_identity(authority.certificate)


def test_peer_verification_matches_the_uri_san_and_never_the_dns_name() -> None:
    authority = make_authority("root", now=NOW)
    impostor = issue_svid(
        authority,
        [REPORTS.spiffe_id],
        now=NOW,
        ttl=timedelta(hours=24),
        dns_names=["orders.shop.svc.cluster.local"],
    )
    with pytest.raises(PeerRejected, match="not an expected peer"):
        verify_peer(impostor, authority.certificate, [ORDERS_ID], now=NOW)
    assert verify_peer(impostor, authority.certificate, [REPORTS], now=NOW) == REPORTS


def test_peer_verification_rejects_expired_future_and_foreign_certificates() -> None:
    authority = make_authority("root", now=NOW - timedelta(days=1))
    cert = issue_svid(authority, [FRONT.spiffe_id], now=NOW, ttl=timedelta(hours=24))
    assert verify_peer(cert, authority.certificate, [FRONT], now=NOW + timedelta(hours=23))
    with pytest.raises(PeerRejected, match="validity"):
        verify_peer(cert, authority.certificate, [FRONT], now=NOW + timedelta(hours=25))
    with pytest.raises(PeerRejected, match="validity"):
        verify_peer(cert, authority.certificate, [FRONT], now=NOW - timedelta(hours=1))
    other = make_authority("other", now=NOW - timedelta(days=1))
    with pytest.raises(PeerRejected, match="trust anchor"):
        verify_peer(cert, other.certificate, [FRONT], now=NOW)


# ---- rotation ------------------------------------------------------------------------------


def test_rotation_survives_a_ca_outage_that_ends_before_expiry() -> None:
    result = simulate_rotation(
        ttl_s=DAY,
        refresh_fraction=0.5,
        outages=[CaOutage(43_000, 86_000)],
        horizon_s=2 * DAY,
    )
    assert result.gaps == ()
    assert 86_000 <= result.issued_at[1] <= 86_150
    assert 285 <= result.failed_attempts <= 300


def test_rotation_leaves_a_gap_when_the_outage_outlasts_the_certificate() -> None:
    result = simulate_rotation(
        ttl_s=DAY,
        refresh_fraction=0.5,
        outages=[CaOutage(43_000, 90_000)],
        horizon_s=2 * DAY,
    )
    assert len(result.gaps) == 1
    start, end = result.gaps[0]
    assert start == DAY
    assert 90_000 <= end <= 90_150


def test_retries_back_off_one_two_four_eight_seconds_then_cap_at_150() -> None:
    short = simulate_rotation(
        ttl_s=DAY,
        refresh_fraction=0.5,
        outages=[CaOutage(43_200, 43_210)],
        horizon_s=DAY,
    )
    assert short.failed_attempts == 4
    assert short.issued_at[1] == 43_215
    assert RETRY_MAX_S == 150.0


def test_jitter_staggers_a_fleet_and_zero_jitter_renews_in_lockstep() -> None:
    rng = random.Random(7)  # noqa: S311 - deterministic test fixture
    spread = [
        rotate_in(lifetime_s=DAY, age_s=0.0, grace_ratio=0.5, jitter=0.01, rng=rng)
        for _ in range(5000)
    ]
    assert all(DAY / 2 - 864.1 <= d <= DAY / 2 + 864.1 for d in spread)
    assert max(spread) - min(spread) > 1500
    herd = {
        rotate_in(lifetime_s=DAY, age_s=0.0, grace_ratio=0.5, jitter=0.0, rng=rng)
        for _ in range(100)
    }
    assert herd == {DAY / 2}


def test_rotation_delay_clamps_the_ratio_and_never_goes_negative() -> None:
    rng = random.Random(1)  # noqa: S311 - deterministic test fixture
    assert rotate_in(lifetime_s=DAY, age_s=0, grace_ratio=1.2, jitter=0, rng=rng) == 0
    assert rotate_in(lifetime_s=DAY, age_s=0, grace_ratio=-0.5, jitter=0, rng=rng) == DAY
    assert rotate_in(lifetime_s=DAY, age_s=DAY, grace_ratio=0.5, jitter=0, rng=rng) == 0


# ---- node-scoped issuance --------------------------------------------------------------------


def test_the_ca_only_issues_identities_that_run_on_the_callers_node() -> None:
    ztunnel_a = Pod("ztunnel-a", "istio-system", "u1", "ztunnel", "node-a")
    ztunnel_b = Pod("ztunnel-b", "istio-system", "u2", "ztunnel", "node-b")
    orders = Pod("orders-1", "shop", "u3", "orders", "node-a")
    billing = Pod("billing-1", "shop", "u4", "billing", "node-b")
    authz = NodeAuthorizer(
        frozenset({("istio-system", "ztunnel")}), (ztunnel_a, ztunnel_b, orders, billing)
    )
    authz.authorize(ztunnel_a, "u1", ORDERS_ID)
    authz.authorize(ztunnel_b, "u2", Identity(TD, "shop", "billing"))
    with pytest.raises(ImpersonationDenied, match="caller's node"):
        authz.authorize(ztunnel_a, "u1", Identity(TD, "shop", "billing"))
    with pytest.raises(ImpersonationDenied, match="stale"):
        authz.authorize(ztunnel_a, "old", ORDERS_ID)
    with pytest.raises(ImpersonationDenied, match="trusted node account"):
        authz.authorize(orders, "u3", ORDERS_ID)
    ghost = Pod("ghost", "istio-system", "u9", "ztunnel", "node-a")
    with pytest.raises(ImpersonationDenied, match="not found"):
        authz.authorize(ghost, "u9", ORDERS_ID)


# ---- HBONE -----------------------------------------------------------------------------------


def test_hbone_connect_carries_only_method_and_authority_and_passes_bytes_through() -> None:
    pool = HbonePool()
    conn, stream_id, seen = pool.open_tunnel(FRONT, ORDERS_ID, "10.0.1.5:8080")
    assert seen == [(":method", "CONNECT"), (":authority", "10.0.1.5:8080")]
    assert not {":scheme", ":path"} & {name for name, _ in seen}
    payload = bytes(range(256))
    assert tunnel_bytes(conn, stream_id, payload) == payload


def test_hbone_pool_uses_one_connection_per_identity_pair_up_to_the_stream_cap() -> None:
    pool = HbonePool()
    for _ in range(101):
        pool.open_tunnel(FRONT, ORDERS_ID, "10.0.1.5:8080")
    pool.open_tunnel(REPORTS, ORDERS_ID, "10.0.1.5:8080")
    assert [c.streams for c in pool.connections] == [100, 1, 1]
    assert [c.key for c in pool.connections] == [
        (FRONT, ORDERS_ID),
        (FRONT, ORDERS_ID),
        (REPORTS, ORDERS_ID),
    ]


def test_http2_itself_refuses_a_stream_beyond_the_peers_concurrency_limit() -> None:
    client, _server = h2_pair(100)
    for stream_id in range(1, 200, 2):
        client.send_headers(stream_id, [(":method", "CONNECT"), (":authority", "10.0.1.5:8080")])
    with pytest.raises(TooManyStreamsError):
        client.send_headers(201, [(":method", "CONNECT"), (":authority", "10.0.1.5:8080")])


def test_hbone_inbound_status_follows_method_then_policy() -> None:
    connect = [(":method", "CONNECT"), (":authority", "10.0.1.5:8080")]
    get = [(":method", "GET"), (":path", "/")]
    allowed = evaluate_ztunnel([ONLY_WAYPOINT], req(src=WAYPOINT))
    denied = evaluate_ztunnel([ONLY_WAYPOINT], req(src=FRONT))
    assert serve_hbone(get, allowed) == 400
    assert serve_hbone(connect, denied) == 401
    assert serve_hbone(connect, allowed) == 200


# ---- footprint -------------------------------------------------------------------------------


def test_ambient_footprint_and_the_waypoint_count_that_erases_it() -> None:
    fleet = Fleet(nodes=10, pods_per_node=30, waypoints=3)
    sidecar = footprint(DataPlane.SIDECAR, fleet)
    ambient = footprint(DataPlane.AMBIENT, fleet)
    assert (sidecar.proxies, sidecar.memory_mb) == (300, 18_000.0)
    assert sidecar.vcpu == pytest.approx(60.0)
    assert (ambient.proxies, ambient.memory_mb) == (13, 300.0)
    assert ambient.vcpu == pytest.approx(1.35)
    limit = max_waypoints_before_ambient_costs_more(fleet)
    assert limit == 298
    at_limit = footprint(DataPlane.AMBIENT, Fleet(10, 30, limit)).memory_mb
    over_limit = footprint(DataPlane.AMBIENT, Fleet(10, 30, limit + 1)).memory_mb
    assert at_limit <= sidecar.memory_mb < over_limit


def test_hop_counts_and_documented_latency_bands_are_ordered() -> None:
    assert proxy_hops(DataPlane.SIDECAR, waypoint=False) == 2
    assert proxy_hops(DataPlane.AMBIENT, waypoint=False) == 2
    assert proxy_hops(DataPlane.AMBIENT, waypoint=True) == 3
    ambient = LATENCY_BAND_MS["ambient"]
    waypoint = LATENCY_BAND_MS["ambient+waypoint"]
    sidecar = LATENCY_BAND_MS["sidecar"]
    assert ambient[1] < waypoint[0]
    assert waypoint[1] < sidecar[0]


def test_zt_match_emptiness_is_what_makes_an_l7_only_clause_unsatisfiable() -> None:
    assert ZtMatch().is_empty()
    assert not ZtMatch(ports=(80,)).is_empty()
