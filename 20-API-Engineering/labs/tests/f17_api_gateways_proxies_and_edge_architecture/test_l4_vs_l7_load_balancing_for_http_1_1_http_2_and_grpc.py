"""Unit tests for L4 vs L7 load balancing under multiplexed HTTP/2 and gRPC."""

from __future__ import annotations

import h2.exceptions
import pytest
from h2.events import ConnectionTerminated, ResponseReceived
from h2.settings import SettingCodes

from api_labs.f17_api_gateways_proxies_and_edge_architecture.l4_vs_l7_load_balancing import (
    CLIENT_PREFACE,
    BackendPod,
    ClientSideLoadBalancer,
    L4LoadBalancer,
    L4WireFlow,
    L7LoadBalancer,
    L7WireProxy,
    PickFirstClient,
    Protocol,
    WireBackend,
    calculate_load_skew,
    new_h2,
    send_requests,
    summarize_distribution,
    unprocessed_streams,
)


@pytest.mark.unit
def test_l4_http2_multiplexing_pins_traffic_to_initial_backends() -> None:
    """Under L4 load balancing, multiplexed HTTP/2 connections pin all streams to initial pods.

    When autoscaling adds 8 new pods (from 2 to 10), existing long-lived connections
    continue sending 100% of their requests to the original 2 pods, leaving the new 8 pods
    at 0 requests (extreme skew).
    """
    initial_pods = [
        BackendPod(pod_id="pod-0", ip="10.244.0.1"),
        BackendPod(pod_id="pod-1", ip="10.244.0.2"),
    ]
    l4_lb = L4LoadBalancer(backends=initial_pods)

    # Two long-lived HTTP/2 client connections established before autoscaling
    conn_a = l4_lb.connect(client_id="client-A", protocol=Protocol.HTTP_2)
    conn_b = l4_lb.connect(client_id="client-B", protocol=Protocol.HTTP_2)

    assert conn_a.backend is not None
    assert conn_b.backend is not None
    assert {conn_a.backend.pod_id, conn_b.backend.pod_id} == {"pod-0", "pod-1"}

    # Autoscaler triggers: 8 new backend pods added to cluster (total 10 pods)
    for i in range(2, 10):
        new_pod = BackendPod(pod_id=f"pod-{i}", ip=f"10.244.0.{i + 1}")
        l4_lb.add_backend(new_pod)

    assert len(l4_lb.backends) == 10

    # Clients send 100 multiplexed HTTP/2 streams each over their existing TCP connection
    for stream_id in range(1, 201, 2):  # odd stream IDs per RFC 9113
        _pod_a, _ = l4_lb.send_request(conn_a, stream_id=stream_id)
        _pod_b, _ = l4_lb.send_request(conn_b, stream_id=stream_id)

    distribution = summarize_distribution(l4_lb.backends)

    # Verification: pod-0 and pod-1 handled 100 requests each; pods 2..9 handled exactly 0
    assert distribution["pod-0"] == 100
    assert distribution["pod-1"] == 100
    for i in range(2, 10):
        assert distribution[f"pod-{i}"] == 0

    # 80% of backend fleet received 0 requests despite cluster scaling
    idle_pods = [p for p in l4_lb.backends if p.total_requests == 0]
    assert len(idle_pods) == 8

    # Load skew (coefficient of variation) is 2.0 (high imbalance)
    skew = calculate_load_skew(l4_lb.backends)
    assert pytest.approx(skew, rel=1e-2) == 2.0


@pytest.mark.unit
def test_l7_stream_multiplexing_distributes_requests_across_all_pods() -> None:
    """An L7 load balancer parses HTTP/2 streams and distributes every request across all pods.

    Even though only 2 downstream client connections exist, when 8 new pods scale up,
    subsequent streams are routed across all 10 pods via per-request round-robin.
    """
    initial_pods = [
        BackendPod(pod_id="pod-0", ip="10.244.0.1"),
        BackendPod(pod_id="pod-1", ip="10.244.0.2"),
    ]
    l7_lb = L7LoadBalancer(backends=initial_pods)

    conn_a = l7_lb.connect(client_id="client-A", protocol=Protocol.HTTP_2)
    conn_b = l7_lb.connect(client_id="client-B", protocol=Protocol.HTTP_2)

    # Autoscaling: add 8 new pods before request stream arrives
    for i in range(2, 10):
        new_pod = BackendPod(pod_id=f"pod-{i}", ip=f"10.244.0.{i + 1}")
        l7_lb.add_backend(new_pod)

    assert len(l7_lb.backends) == 10

    # 2 clients send 100 requests each (200 requests total)
    for stream_id in range(1, 201, 2):
        l7_lb.send_request(conn_a, stream_id=stream_id)
        l7_lb.send_request(conn_b, stream_id=stream_id)

    distribution = summarize_distribution(l7_lb.backends)

    # Every pod received exactly 20 requests (perfect balance)
    for pod_id, count in distribution.items():
        assert count == 20, f"{pod_id} received {count} requests instead of 20"

    # Perfect balance: skew is 0.0
    skew = calculate_load_skew(l7_lb.backends)
    assert skew == 0.0


@pytest.mark.unit
def test_http11_connection_churn_partially_masks_l4_pinning() -> None:
    """HTTP/1.1 short-lived connections mask L4 pinning by creating new TCP handshakes.

    When clients open a new TCP connection per batch or request (connection churn),
    each connection triggers the L4 balancer to pick a new backend, spreading load.
    This explains why legacy HTTP/1.1 systems did not exhibit the extreme skew seen in HTTP/2.
    """
    backends = [BackendPod(pod_id=f"pod-{i}", ip=f"10.244.0.{i + 1}") for i in range(5)]
    l4_lb = L4LoadBalancer(backends=backends)

    # In HTTP/1.1 without long-lived keep-alive, client opens a new connection per request
    for req_idx in range(50):
        conn = l4_lb.connect(client_id=f"client-{req_idx}", protocol=Protocol.HTTP_1_1)
        _pod_id, _ = l4_lb.send_request(conn, stream_id=1)
        l4_lb.close_connection(conn)

    distribution = summarize_distribution(l4_lb.backends)

    # All 5 pods received exactly 10 requests because 50 distinct connections were balanced
    for i in range(5):
        assert distribution[f"pod-{i}"] == 10

    assert calculate_load_skew(l4_lb.backends) == 0.0


@pytest.mark.unit
def test_l4_mitigation_max_connection_age_goaway_triggers_rebalancing() -> None:
    """Server-side max connection age emits GOAWAY, forcing client reconnection and L4 rebalance.

    Simulates Envoy's max_requests_per_connection or gRPC's MAX_CONNECTION_AGE_MS.
    Once a connection hits the threshold, GOAWAY marks it draining.
    The client closes it and reconnects, spreading subsequent load to newly scaled pods.
    """
    initial_pods = [
        BackendPod(pod_id="pod-0", ip="10.244.0.1", max_requests_per_connection=25),
        BackendPod(pod_id="pod-1", ip="10.244.0.2", max_requests_per_connection=25),
    ]
    l4_lb = L4LoadBalancer(backends=initial_pods)

    conn = l4_lb.connect(client_id="client-A", protocol=Protocol.HTTP_2)
    assert conn.backend is not None
    assert conn.backend.pod_id == "pod-0"

    # Scale cluster from 2 to 4 pods
    for i in range(2, 4):
        l4_lb.add_backend(
            BackendPod(pod_id=f"pod-{i}", ip=f"10.244.0.{i + 1}", max_requests_per_connection=25)
        )

    # Send 25 requests on initial connection
    goaway_seen = False
    for stream_id in range(1, 51, 2):
        _, goaway = l4_lb.send_request(conn, stream_id=stream_id)
        if goaway:
            goaway_seen = True

    assert goaway_seen is True
    assert conn.is_draining is True

    # Attempting to send on draining connection raises RuntimeError
    with pytest.raises(RuntimeError, match="draining"):
        l4_lb.send_request(conn, stream_id=53)

    # Client closes draining connection and establishes a fresh connection via L4 LB
    l4_lb.close_connection(conn)
    new_conn = l4_lb.connect(client_id="client-A", protocol=Protocol.HTTP_2)

    # New connection balances to another pod in the expanded pool
    assert new_conn.backend is not None
    assert new_conn.backend.pod_id != "pod-0"

    # Send 25 more requests on new connection
    for stream_id in range(1, 51, 2):
        _pod_id, _ = l4_lb.send_request(new_conn, stream_id=stream_id)

    distribution = summarize_distribution(l4_lb.backends)
    assert distribution["pod-0"] == 25
    assert distribution[new_conn.backend.pod_id] == 25


@pytest.mark.unit
def test_client_side_grpc_round_robin_avoids_l4_pinning_without_l7_proxy() -> None:
    """Client-side gRPC load balancing connects to all backend pods directly.

    Using Kubernetes Headless Service DNS resolution, the client opens subchannels
    to each backend pod and performs request-level round-robin without an intermediary proxy.
    """
    backends = [BackendPod(pod_id=f"pod-{i}", ip=f"10.244.0.{i + 1}") for i in range(10)]
    client_lb = ClientSideLoadBalancer(client_id="grpc-service-client", backend_endpoints=backends)

    # Client established a subchannel to all 10 pods
    assert len(client_lb.subchannels) == 10
    for pod in backends:
        assert pod.active_connections == 1

    # Client sends 100 RPCs
    for rpc_id in range(100):
        client_lb.send_rpc(rpc_id=rpc_id)

    distribution = summarize_distribution(backends)

    # Each of the 10 pods received exactly 10 RPCs
    for i in range(10):
        assert distribution[f"pod-{i}"] == 10

    assert calculate_load_skew(backends) == 0.0


@pytest.mark.unit
def test_l4_least_conn_balances_connection_counts_but_not_request_volume() -> None:
    """L4 cannot detect application request bursts across existing connections.

    Two clients each hold 1 connection to 2 pods.
    Client A sends 1,000 requests; Client B is idle (0 requests).
    L4 sees equal connection counts (1:1), but Pod 0 is saturated while Pod 1 sits idle.
    """
    pod_0 = BackendPod(pod_id="pod-0", ip="10.244.0.1")
    pod_1 = BackendPod(pod_id="pod-1", ip="10.244.0.2")
    l4_lb = L4LoadBalancer(backends=[pod_0, pod_1], policy="least_conn")

    conn_active = l4_lb.connect(client_id="client-active", protocol=Protocol.HTTP_2)
    conn_idle = l4_lb.connect(client_id="client-idle", protocol=Protocol.HTTP_2)

    # L4 sees equal connection load
    assert pod_0.active_connections == 1
    assert pod_1.active_connections == 1

    # Client Active sends 1000 requests over its single connection
    for stream_id in range(1, 2001, 2):
        _pod_id, _ = l4_lb.send_request(conn_active, stream_id=stream_id)

    # Client Idle sends 0 requests
    distribution = summarize_distribution(l4_lb.backends)

    # Saturated pod-0 vs completely idle pod-1 despite identical L4 connection metric
    assert distribution[conn_active.backend.pod_id] == 1000  # type: ignore[union-attr]
    assert distribution[conn_idle.backend.pod_id] == 0  # type: ignore[union-attr]
    assert calculate_load_skew(l4_lb.backends) == 1.0


@pytest.mark.unit
def test_grpc_pick_first_default_pins_every_rpc_despite_many_resolved_addresses() -> None:
    """``pick_first`` keeps one subchannel, so a 10-address headless DNS answer changes nothing."""
    pods = [BackendPod(pod_id=f"pod-{i}", ip=f"10.244.0.{i + 1}") for i in range(10)]
    client = PickFirstClient(client_id="svc-client", resolved=pods)

    for rpc_id in range(100):
        client.send_rpc(rpc_id)

    distribution = summarize_distribution(pods)
    assert distribution["pod-0"] == 100
    assert sum(distribution.values()) == 100
    assert sum(p.active_connections for p in pods) == 1
    assert calculate_load_skew(pods) == pytest.approx(3.0)

    # The first address is skipped only when it is unreachable.
    pods[0].is_healthy = False
    fallback = PickFirstClient(client_id="svc-client-2", resolved=pods)
    assert fallback.send_rpc(0) == "pod-1"


@pytest.mark.unit
def test_pick_first_with_per_client_address_order_balances_connections_not_requests() -> None:
    """Rotating address order spreads connections, yet one hot client stays pinned."""
    pods = [BackendPod(pod_id=f"pod-{i}", ip=f"10.244.0.{i + 1}") for i in range(10)]
    clients = [
        PickFirstClient(
            client_id=f"client-{i}",
            resolved=pods,
            order=[(i + k) % 10 for k in range(10)],
        )
        for i in range(10)
    ]

    assert [p.active_connections for p in pods] == [1] * 10

    for rpc_id in range(100):
        clients[0].send_rpc(rpc_id)

    distribution = summarize_distribution(pods)
    assert distribution["pod-0"] == 100
    assert all(distribution[f"pod-{i}"] == 0 for i in range(1, 10))


@pytest.mark.unit
def test_l4_wire_relay_forwards_client_bytes_and_pins_all_streams_to_one_backend() -> None:
    """Real HTTP/2 frames: six streams on one TCP connection all reach the same origin."""
    pods = [WireBackend("pod-0"), WireBackend("pod-1")]
    flow = L4WireFlow(pods[0])  # the L4 decision was made here, at connect time
    client = new_h2(client_side=True)

    stream_ids = send_requests(client, 6)
    events = flow.pump(client)

    assert stream_ids == [1, 3, 5, 7, 9, 11]
    assert pods[0].seen == [(0, sid) for sid in stream_ids]
    assert pods[1].seen == []
    assert pods[0].connections_accepted == 1
    assert pods[1].connections_accepted == 0
    # The relay carried the RFC 9113 Section 3.4 connection preface untouched.
    assert b"".join(flow.bytes_up).startswith(CLIENT_PREFACE)
    answered = [e.stream_id for e in events if isinstance(e, ResponseReceived)]
    assert answered == stream_ids


@pytest.mark.unit
def test_l7_wire_proxy_splits_one_client_connection_across_backends_and_renumbers_streams() -> None:
    """Real HTTP/2 frames: one client connection becomes one upstream connection per backend."""
    pods = [WireBackend(f"pod-{i}") for i in range(3)]
    proxy = L7WireProxy(pods)
    client = new_h2(client_side=True)

    stream_ids = send_requests(client, 6)
    events = proxy.pump(client)

    assert stream_ids == [1, 3, 5, 7, 9, 11]
    # Client streams 3 and 9 went to pod-1, which numbered them 1 and 3 on its own connection.
    assert pods[1].seen == [(0, 1), (0, 3)]
    assert proxy.stream_map == [
        ("pod-0", 1, 1),
        ("pod-1", 3, 1),
        ("pod-2", 5, 1),
        ("pod-0", 7, 3),
        ("pod-1", 9, 3),
        ("pod-2", 11, 3),
    ]
    assert [p.connections_accepted for p in pods] == [1, 1, 1]
    assert [len(p.seen) for p in pods] == [2, 2, 2]
    answered = sorted(e.stream_id for e in events if isinstance(e, ResponseReceived))
    assert answered == stream_ids


@pytest.mark.unit
def test_goaway_last_stream_id_marks_later_streams_retryable_and_blocks_new_ones() -> None:
    """A GOAWAY with Last-Stream-ID 5 proves streams 7 and 9 were never processed."""
    pods = [WireBackend("pod-0"), WireBackend("pod-1")]
    flow = L4WireFlow(pods[0])
    client = new_h2(client_side=True)

    processed = send_requests(client, 3)
    flow.pump(client)
    in_flight = send_requests(client, 2)  # queued on the client, not yet delivered
    goaway = pods[0].goaway(flow.conn_no, last_stream_id=5)
    events = client.receive_data(goaway)

    terminated = [e for e in events if isinstance(e, ConnectionTerminated)]
    assert len(terminated) == 1
    assert terminated[0].last_stream_id == 5
    assert terminated[0].error_code == 0
    assert terminated[0].additional_data == b"max_age"
    assert unprocessed_streams(processed + in_flight, 5) == in_flight
    assert [sid for _, sid in pods[0].seen] == processed

    # RFC 9113 Section 6.8: no further streams on this connection.
    with pytest.raises(h2.exceptions.ProtocolError):
        send_requests(client, 1)

    # The retry needs a new connection, and only that connection can be balanced elsewhere.
    retry_flow = L4WireFlow(pods[1])
    retry_client = new_h2(client_side=True)
    send_requests(retry_client, len(in_flight))
    retry_flow.pump(retry_client)
    assert len(pods[1].seen) == 2


@pytest.mark.unit
def test_max_concurrent_streams_bounds_parallelism_per_connection() -> None:
    """The peer's SETTINGS_MAX_CONCURRENT_STREAMS caps open streams on one connection."""
    client = new_h2(client_side=True)
    server = new_h2(client_side=False)

    def exchange() -> None:
        for _ in range(3):
            server.receive_data(client.data_to_send())
            client.receive_data(server.data_to_send())

    exchange()
    assert client.remote_settings.max_concurrent_streams == 100  # h2 4.4.1 server default

    server.update_settings({SettingCodes.MAX_CONCURRENT_STREAMS: 2})
    exchange()
    assert client.remote_settings.max_concurrent_streams == 2

    for _ in range(2):
        client.send_headers(
            client.get_next_available_stream_id(),
            [(":method", "POST"), (":scheme", "http"), (":authority", "a"), (":path", "/p")],
        )
    assert client.open_outbound_streams == 2
    with pytest.raises(h2.exceptions.TooManyStreamsError):
        client.send_headers(
            client.get_next_available_stream_id(),
            [(":method", "POST"), (":scheme", "http"), (":authority", "a"), (":path", "/p")],
        )
