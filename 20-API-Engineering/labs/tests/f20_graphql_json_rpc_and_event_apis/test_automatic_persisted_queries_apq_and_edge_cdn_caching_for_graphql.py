"""Lab tests for APQ, trusted documents, and shared-cache behavior in front of GraphQL."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from graphql import parse, print_ast

from api_labs.f20_graphql_json_rpc_and_event_apis.automatic_persisted_queries_apq_and_edge_cdn_caching_for_graphql import (  # noqa: E501
    CODE_NOT_ALLOWED,
    CODE_NOT_FOUND,
    UNCACHED_PQ_ERROR,
    ApqClient,
    ApqRegistry,
    ApqServer,
    CacheHint,
    EdgeCache,
    HttpRequest,
    HttpResponse,
    ManualClock,
    PersistedQueryMode,
    build_app,
    build_manifest,
    sha256_hex,
)

PRODUCT = '{ product(id: "1") { name price } }'
PRODUCT_REVIEWS = '{ product(id: "1") { name reviews(limit: 2) { body } } }'
CLOCK = "{ clock }"
VIEWER = "{ viewer { name email } }"
TYPENAME = "{__typename}"
VARIABLES_QUERY = (
    "query P($id: ID!, $limit: Int) { product(id: $id) { reviews(limit: $limit) { body } } }"
)
MUTATION = 'mutation { setPrice(id: "1", price: 12.5) { id price } }'
DOCUMENTED_TYPENAME_HASH = "ecf4edb46db40b5132295c0291d62fb65d6759a9eedfa4d5d612dd5ec54a6b38"


def ext_json(digest: str) -> str:
    return json.dumps(
        {"persistedQuery": {"version": 1, "sha256Hash": digest}}, separators=(",", ":")
    )


def get_hash_only(query: str, **headers: str) -> HttpRequest:
    target = "/graphql?extensions=" + quote(ext_json(sha256_hex(query)), safe="")
    return HttpRequest("GET", target, dict(headers))


def get_full(query: str) -> HttpRequest:
    target = (
        "/graphql?query="
        + quote(query, safe="")
        + "&extensions="
        + quote(ext_json(sha256_hex(query)), safe="")
    )
    return HttpRequest("GET", target)


def post(payload: dict[str, Any], **headers: str) -> HttpRequest:
    all_headers = {"content-type": "application/json", **headers}
    return HttpRequest("POST", "/graphql", all_headers, json.dumps(payload).encode())


def post_full(query: str) -> HttpRequest:
    return post({"query": query, "extensions": json.loads(ext_json(sha256_hex(query)))})


def error_code(response: HttpResponse) -> str:
    return str(response.json()["errors"][0]["extensions"]["code"])


def long_query(aliases: int) -> str:
    fields = " ".join(f'a{i}: product(id: "1") {{ name }}' for i in range(aliases))
    return "{ " + fields + " }"


# -- protocol handshake ------------------------------------------------------------------------


@pytest.mark.unit
def test_hash_of_typename_query_matches_the_documented_value() -> None:
    assert sha256_hex(TYPENAME) == DOCUMENTED_TYPENAME_HASH


@pytest.mark.unit
def test_hash_only_request_on_an_empty_registry_is_persisted_query_not_found() -> None:
    server = ApqServer()
    response = server.handle(get_hash_only(PRODUCT))
    assert response.status == 200
    assert response.json()["errors"][0]["message"] == "PersistedQueryNotFound"
    assert error_code(response) == CODE_NOT_FOUND
    assert response.headers["cache-control"] == UNCACHED_PQ_ERROR
    assert server.stats.executed == 0


@pytest.mark.unit
def test_registration_then_hash_only_request_executes_the_stored_text() -> None:
    server = ApqServer()
    registered = server.handle(get_full(PRODUCT))
    assert registered.json()["data"]["product"] == {"name": "product-1", "price": 10.0}
    hit = server.handle(get_hash_only(PRODUCT))
    assert hit.json() == registered.json()
    assert (server.stats.registered, server.stats.hits, server.stats.executed) == (1, 1, 2)


@pytest.mark.unit
def test_a_mismatched_hash_is_rejected_and_nothing_is_registered() -> None:
    server = ApqServer()
    forged = post({"query": CLOCK, "extensions": json.loads(ext_json(sha256_hex(PRODUCT)))})
    response = server.handle(forged)
    assert response.status == 400
    assert response.json()["errors"][0]["message"] == "provided sha does not match query"
    assert len(server.registry) == 0
    assert error_code(server.handle(get_hash_only(PRODUCT))) == CODE_NOT_FOUND


@pytest.mark.unit
def test_unsupported_version_is_a_400() -> None:
    payload = {"extensions": {"persistedQuery": {"version": 2, "sha256Hash": "0" * 64}}}
    response = ApqServer().handle(post(payload))
    assert response.status == 400
    assert response.json()["errors"][0]["message"] == "Unsupported persisted query version"


@pytest.mark.unit
def test_a_query_that_fails_parse_or_validation_is_never_registered() -> None:
    server = ApqServer()
    assert server.handle(get_full("{ product(id: ")).status == 400
    assert server.handle(get_full("{ nonexistentField }")).status == 422
    assert len(server.registry) == 0
    assert server.stats.registered == 0


@pytest.mark.unit
def test_mutation_over_get_is_405_with_allow_post_and_is_not_registered() -> None:
    server = ApqServer()
    response = server.handle(get_full(MUTATION))
    assert response.status == 405
    assert response.headers["allow"] == "POST"
    assert len(server.registry) == 0
    assert server.stats.executed == 0


@pytest.mark.unit
def test_mutation_over_post_executes_and_is_no_store() -> None:
    server = ApqServer()
    response = server.handle(post({"query": MUTATION}))
    assert response.status == 200
    assert response.json()["data"]["setPrice"]["price"] == 12.5
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.unit
def test_disabled_apq_answers_persisted_query_not_supported() -> None:
    server = ApqServer(mode=PersistedQueryMode.DISABLED)
    response = server.handle(get_hash_only(PRODUCT))
    assert response.status == 200
    assert error_code(response) == "PERSISTED_QUERY_NOT_SUPPORTED"
    assert response.headers["cache-control"] == UNCACHED_PQ_ERROR


@pytest.mark.unit
def test_get_with_a_non_json_content_type_is_415() -> None:
    request = HttpRequest("GET", "/graphql?query=%7B__typename%7D", {"content-type": "text/plain"})
    assert ApqServer().handle(request).status == 415


@pytest.mark.unit
def test_a_repeated_query_parameter_is_a_400() -> None:
    target = "/graphql?query=%7B__typename%7D&query=%7B__typename%7D"
    assert ApqServer().handle(HttpRequest("GET", target)).status == 400


@pytest.mark.unit
def test_an_empty_post_without_query_or_extension_is_a_400() -> None:
    assert ApqServer().handle(post({})).status == 400


# -- Cache-Control derived from the schema -----------------------------------------------------


@pytest.mark.unit
def test_cache_control_is_the_minimum_max_age_over_the_selected_fields() -> None:
    server = ApqServer()
    assert server.handle(get_full(PRODUCT)).headers["cache-control"] == "max-age=60, public"
    assert server.handle(get_full(PRODUCT_REVIEWS)).headers["cache-control"] == "max-age=30, public"


@pytest.mark.unit
def test_an_unhinted_root_field_makes_the_whole_response_no_store() -> None:
    server = ApqServer()
    mixed = '{ product(id: "1") { name } clock }'
    assert server.handle(get_full(mixed)).headers["cache-control"] == "no-store"
    assert server.handle(get_full(CLOCK)).headers["cache-control"] == "no-store"


@pytest.mark.unit
def test_a_private_field_makes_the_response_private() -> None:
    server = ApqServer()
    alice = {"authorization": "Bearer alice"}
    server.handle(HttpRequest("GET", get_full(VIEWER).target, alice))
    cached = server.handle(HttpRequest("GET", get_hash_only(VIEWER).target, alice))
    assert cached.headers["cache-control"] == "max-age=60, private"
    assert cached.json()["data"]["viewer"]["name"] == "alice"


@pytest.mark.unit
def test_a_request_error_is_no_store_and_422() -> None:
    server = ApqServer()
    response = server.handle(
        post({"query": "query P($id: ID!) { product(id: $id) { name } }"}),
    )
    assert response.status == 422
    assert response.headers["cache-control"] == "no-store"


# -- the shared cache --------------------------------------------------------------------------


@pytest.mark.unit
def test_an_edge_cache_never_stores_post_so_identical_posts_reach_the_origin() -> None:
    clock = ManualClock()
    server = ApqServer()
    edge = EdgeCache(server.handle, clock=clock)
    for _ in range(3):
        assert edge(post_full(PRODUCT)).status == 200
    assert server.stats.executed == 3
    assert (edge.stats.bypasses, edge.stats.stores, edge.stats.hits) == (3, 0, 0)


@pytest.mark.unit
def test_hash_only_get_is_served_from_the_edge_after_one_origin_execution() -> None:
    clock = ManualClock()
    server = ApqServer()
    edge = EdgeCache(server.handle, clock=clock)
    client = ApqClient(edge)
    for _ in range(5):
        assert client.execute(PRODUCT).response.json()["data"]["product"]["name"] == "product-1"
    assert server.stats.executed == 2
    assert server.stats.not_found == 1
    assert (edge.stats.hits, edge.stats.misses, edge.stats.bypasses, edge.stats.stores) == (
        3,
        2,
        1,
        1,
    )


@pytest.mark.unit
def test_edge_entry_expires_after_max_age_and_the_origin_is_asked_again() -> None:
    clock = ManualClock()
    server = ApqServer()
    edge = EdgeCache(server.handle, clock=clock)
    client = ApqClient(edge)
    client.execute(PRODUCT)
    client.execute(PRODUCT)
    executed = server.stats.executed
    clock.advance(59)
    hit = client.execute(PRODUCT).response
    assert hit.headers["x-lab-edge"] == "HIT"
    assert hit.headers["age"] == "59"
    clock.advance(1)
    assert client.execute(PRODUCT).response.headers["x-lab-edge"] == "MISS"
    assert server.stats.executed == executed + 1


@pytest.mark.unit
def test_not_found_is_not_stored_even_by_an_edge_that_caches_by_status_alone() -> None:
    clock = ManualClock()
    server = ApqServer()
    edge = EdgeCache(server.handle, clock=clock, heuristic_ttl=7200.0)
    first = edge(get_hash_only(PRODUCT))
    assert error_code(first) == CODE_NOT_FOUND
    assert edge(post_full(PRODUCT)).status == 200
    second = edge(get_hash_only(PRODUCT))
    assert second.json()["data"]["product"]["name"] == "product-1"
    assert edge.stats.stores == 1


@pytest.mark.unit
def test_a_heuristic_edge_does_store_a_200_without_cache_control() -> None:
    clock = ManualClock()
    origin_calls: list[str] = []

    def origin(request: HttpRequest) -> HttpResponse:
        origin_calls.append(request.target)
        return HttpResponse(200, {}, b"{}")

    edge = EdgeCache(origin, clock=clock, heuristic_ttl=7200.0)
    edge(HttpRequest("GET", "/x"))
    edge(HttpRequest("GET", "/x"))
    assert origin_calls == ["/x"]


@pytest.mark.unit
def test_private_scope_is_never_stored_by_the_shared_cache() -> None:
    clock = ManualClock()
    server = ApqServer()
    edge = EdgeCache(server.handle, clock=clock)
    client = ApqClient(edge, headers={"authorization": "Bearer alice"})
    for _ in range(3):
        result = client.execute(VIEWER)
        assert result.response.json()["data"]["viewer"]["name"] == "alice"
    assert edge.stats.stores == 0
    assert server.stats.executed == 3


def _viewer_via_edge(edge: EdgeCache, user: str) -> str:
    client = ApqClient(edge, headers={"authorization": f"Bearer {user}"})
    return str(client.execute(VIEWER).response.json()["data"]["viewer"]["name"])


@pytest.mark.unit
def test_a_wrongly_public_hint_leaks_one_users_data_to_another_through_a_url_keyed_edge() -> None:
    leaky = {"Query.viewer": CacheHint(max_age=60, private=False)}
    clock = ManualClock()
    server = ApqServer(hints=leaky)
    edge = EdgeCache(server.handle, clock=clock)
    assert _viewer_via_edge(edge, "alice") == "alice"
    assert _viewer_via_edge(edge, "alice") == "alice"
    assert _viewer_via_edge(edge, "bob") == "alice"


@pytest.mark.unit
def test_vary_authorization_stops_the_leak_even_with_the_wrong_hint() -> None:
    leaky = {"Query.viewer": CacheHint(max_age=60, private=False)}
    clock = ManualClock()
    server = ApqServer(hints=leaky, vary=("authorization",))
    edge = EdgeCache(server.handle, clock=clock)
    assert _viewer_via_edge(edge, "alice") == "alice"
    assert _viewer_via_edge(edge, "alice") == "alice"
    assert _viewer_via_edge(edge, "bob") == "bob"


@pytest.mark.unit
@pytest.mark.parametrize(("cache_control", "stored"), [("max-age=60", 0), ("s-maxage=60", 1)])
def test_authorized_responses_need_an_explicit_shared_cache_directive(
    cache_control: str, stored: int
) -> None:
    def origin(_request: HttpRequest) -> HttpResponse:
        return HttpResponse(200, {"cache-control": cache_control}, b"{}")

    edge = EdgeCache(origin, clock=ManualClock())
    edge(HttpRequest("GET", "/x", {"authorization": "Bearer alice"}))
    assert edge.stats.stores == stored


@pytest.mark.unit
def test_an_overlong_get_url_is_refused_by_the_edge_but_the_hash_url_is_tiny() -> None:
    query = long_query(400)
    clock = ManualClock()
    server = ApqServer()
    edge = EdgeCache(server.handle, clock=clock, max_url_bytes=8192)
    plain = edge(HttpRequest("GET", "/graphql?query=" + quote(query, safe="")))
    assert plain.status == 414
    assert server.stats.requests == 0
    client = ApqClient(edge)
    result = client.execute(query)
    assert result.response.status == 200
    assert len(result.response.json()["data"]) == 400
    assert result.requests[0].wire_bytes < 300
    assert edge.stats.too_long == 1


@pytest.mark.unit
def test_measured_request_sizes_for_a_large_query() -> None:
    query = long_query(400)
    server = ApqServer()
    client = ApqClient(server.handle)
    cold = client.execute(query)
    warm = client.execute(query)
    full_post = ApqClient(server.handle, use_get_for_hashed_queries=False).execute(query)
    print(
        f"MEASURED query_chars={len(query)} cold_requests={len(cold.requests)} "
        f"cold_wire_bytes={cold.wire_bytes} warm_wire_bytes={warm.wire_bytes} "
        f"hash_get_target_bytes={warm.requests[0].wire_bytes}"
    )
    assert len(cold.requests) == 2
    assert len(warm.requests) == 1
    assert warm.wire_bytes < 300 < len(query)
    assert cold.wire_bytes > len(query)
    assert full_post.response.status == 200


# -- QUERY method ------------------------------------------------------------------------------


def _query_request(query: str) -> HttpRequest:
    headers = {"content-type": "application/json"}
    return HttpRequest("QUERY", "/graphql", headers, json.dumps({"query": query}).encode())


@pytest.mark.unit
def test_query_method_is_cacheable_keyed_by_body_while_post_is_not() -> None:
    clock = ManualClock()
    server = ApqServer(enable_query_method=True)
    edge = EdgeCache(
        server.handle, clock=clock, cacheable_methods=frozenset({"GET", "HEAD", "QUERY"})
    )
    edge(_query_request(PRODUCT))
    edge(_query_request(PRODUCT))
    assert server.stats.executed == 1
    edge(_query_request(PRODUCT_REVIEWS))
    assert server.stats.executed == 2
    edge(post({"query": PRODUCT}))
    edge(post({"query": PRODUCT}))
    assert server.stats.executed == 4


@pytest.mark.unit
def test_a_mutation_over_query_is_refused_because_query_is_safe() -> None:
    server = ApqServer(enable_query_method=True)
    response = server.handle(_query_request(MUTATION))
    assert response.status == 405
    assert response.headers["allow"] == "POST"


@pytest.mark.unit
def test_query_method_is_405_unless_enabled() -> None:
    assert ApqServer().handle(_query_request(PRODUCT)).status == 405


# -- client behavior ---------------------------------------------------------------------------


@pytest.mark.unit
def test_client_retries_exactly_once_and_never_loops() -> None:
    sent: list[HttpRequest] = []

    def always_not_found(request: HttpRequest) -> HttpResponse:
        sent.append(request)
        payload = {
            "errors": [
                {"message": "PersistedQueryNotFound", "extensions": {"code": CODE_NOT_FOUND}}
            ]
        }
        return HttpResponse(200, {}, json.dumps(payload).encode())

    result = ApqClient(always_not_found).execute(PRODUCT)
    assert [r.method for r in sent] == ["GET", "POST"]
    assert error_code(result.response) == CODE_NOT_FOUND


@pytest.mark.unit
def test_not_supported_turns_apq_off_for_the_rest_of_the_session() -> None:
    server = ApqServer(mode=PersistedQueryMode.DISABLED)
    client = ApqClient(server.handle)
    first = client.execute(PRODUCT)
    assert first.response.status == 200
    assert len(first.requests) == 2
    assert client.apq_enabled is False
    second = client.execute(PRODUCT)
    assert len(second.requests) == 1
    assert "extensions" not in json.loads(second.requests[0].body)


@pytest.mark.unit
def test_client_never_sends_a_mutation_over_get() -> None:
    server = ApqServer()
    client = ApqClient(server.handle)
    result = client.execute(MUTATION)
    assert [r.method for r in result.requests] == ["POST", "POST"]
    assert result.response.json()["data"]["setPrice"]["price"] == 12.5


@pytest.mark.unit
def test_document_normalization_makes_equivalent_texts_share_one_registration() -> None:
    compact, spaced = "{__typename}", "{ __typename }"
    assert sha256_hex(compact) != sha256_hex(spaced)
    normalized = ApqServer()
    client = ApqClient(normalized.handle, normalize_document=True)
    assert len(client.execute(compact).requests) == 2
    assert len(client.execute(spaced).requests) == 1
    raw = ApqServer()
    raw_client = ApqClient(raw.handle, normalize_document=False)
    assert len(raw_client.execute(compact).requests) == 2
    assert len(raw_client.execute(spaced).requests) == 2


def _prime(server: ApqServer, query: str) -> None:
    text = print_ast(parse(query))
    assert server.registry.put(sha256_hex(text), text)


@pytest.mark.unit
@pytest.mark.parametrize(("canonical", "origin_misses"), [(False, 2), (True, 1)])
def test_variable_key_order_fragments_the_edge_cache_unless_urls_are_canonical(
    canonical: bool, origin_misses: int
) -> None:
    server = ApqServer()
    _prime(server, VARIABLES_QUERY)
    edge = EdgeCache(server.handle, clock=ManualClock())
    client = ApqClient(edge, canonical_urls=canonical)
    client.execute(VARIABLES_QUERY, {"id": "1", "limit": 2})
    client.execute(VARIABLES_QUERY, {"limit": 2, "id": "1"})
    assert edge.stats.misses == origin_misses
    assert server.stats.executed == origin_misses


# -- registry as an attack surface and an operations concern -----------------------------------


@pytest.mark.unit
def test_a_flood_of_unique_queries_evicts_a_hot_entry_and_costs_one_extra_round_trip() -> None:
    registry = ApqRegistry(max_entries=4)
    server = ApqServer(registry=registry)
    client = ApqClient(server.handle)
    assert len(client.execute(PRODUCT).requests) == 2
    assert len(client.execute(PRODUCT).requests) == 1
    for i in range(4):
        assert server.handle(post_full(long_query(i + 1))).status == 200
    assert registry.evictions >= 1
    assert error_code(server.handle(get_hash_only(PRODUCT))) == CODE_NOT_FOUND
    assert len(client.execute(PRODUCT).requests) == 2


@pytest.mark.unit
def test_an_oversized_query_executes_but_is_not_registered() -> None:
    registry = ApqRegistry(max_query_bytes=100)
    server = ApqServer(registry=registry)
    query = long_query(50)
    assert server.handle(post_full(query)).status == 200
    assert (server.stats.registered, server.stats.registration_rejected) == (0, 1)
    assert error_code(server.handle(get_hash_only(query))) == CODE_NOT_FOUND


@pytest.mark.unit
def test_registry_ttl_expiry_is_healed_by_the_client_retry() -> None:
    clock = ManualClock()
    server = ApqServer(registry=ApqRegistry(ttl_seconds=60.0, clock=clock))
    client = ApqClient(server.handle)
    assert len(client.execute(PRODUCT).requests) == 2
    clock.advance(30)
    assert len(client.execute(PRODUCT).requests) == 1
    clock.advance(31)
    assert len(client.execute(PRODUCT).requests) == 2


@pytest.mark.unit
@pytest.mark.parametrize(
    ("shared", "second_instance_answer"), [(False, CODE_NOT_FOUND), (True, "")]
)
def test_per_process_registries_make_every_instance_miss_once(
    shared: bool, second_instance_answer: str
) -> None:
    registry = ApqRegistry()
    a = ApqServer(registry=registry)
    b = ApqServer(registry=registry if shared else ApqRegistry())
    assert a.handle(post_full(PRODUCT)).status == 200
    response = b.handle(get_hash_only(PRODUCT))
    if shared:
        assert response.json()["data"]["product"]["name"] == "product-1"
    else:
        assert error_code(response) == second_instance_answer


# -- trusted documents -------------------------------------------------------------------------


def _allowlist_server(*, require_id: bool = False) -> ApqServer:
    manifest = build_manifest([PRODUCT, CLOCK])
    return ApqServer(mode=PersistedQueryMode.ALLOWLIST, manifest=manifest, require_id=require_id)


@pytest.mark.unit
def test_manifest_ids_are_the_sha256_of_the_normalized_text() -> None:
    manifest = build_manifest([PRODUCT])
    text = print_ast(parse(PRODUCT))
    assert manifest == {sha256_hex(text): text}


@pytest.mark.unit
def test_allowlist_rejects_text_outside_the_manifest_and_registers_nothing() -> None:
    server = _allowlist_server()
    plain = server.handle(post({"query": VIEWER}))
    assert plain.status == 403
    assert error_code(plain) == CODE_NOT_ALLOWED
    assert server.handle(post_full(VIEWER)).status == 403
    assert len(server.registry) == 0
    assert server.stats.executed == 0


@pytest.mark.unit
def test_allowlist_serves_a_manifest_id_through_the_edge_with_no_registration_traffic() -> None:
    server = _allowlist_server()
    edge = EdgeCache(server.handle, clock=ManualClock())
    client = ApqClient(edge)
    for _ in range(4):
        result = client.execute(PRODUCT)
        assert result.response.json()["data"]["product"]["name"] == "product-1"
        assert len(result.requests) == 1
    assert (server.stats.executed, server.stats.not_found, len(server.registry)) == (1, 0, 0)
    assert edge.stats.hits == 3


@pytest.mark.unit
def test_allowlist_answers_403_for_an_unknown_id_and_the_client_does_not_retry() -> None:
    server = _allowlist_server()
    result = ApqClient(server.handle).execute(VIEWER)
    assert result.response.status == 403
    assert error_code(result.response) == CODE_NOT_ALLOWED
    assert len(result.requests) == 1
    assert server.stats.not_found == 0


@pytest.mark.unit
def test_require_id_rejects_operation_text_even_when_the_text_is_in_the_manifest() -> None:
    server = _allowlist_server(require_id=True)
    text = print_ast(parse(PRODUCT))
    assert server.handle(post({"query": text})).status == 403
    assert server.handle(post_full(text)).status == 403
    assert server.handle(get_hash_only(text)).status == 200


@pytest.mark.unit
def test_a_flood_of_unknown_ids_grows_no_state_and_executes_nothing() -> None:
    server = _allowlist_server()
    size = len(server.manifest)
    for i in range(1000):
        target = "/graphql?extensions=" + quote(ext_json(sha256_hex(str(i))), safe="")
        assert server.handle(HttpRequest("GET", target)).status == 403
    assert (server.stats.forbidden, server.stats.executed) == (1000, 0)
    assert (len(server.registry), len(server.manifest)) == (0, size)


# -- ASGI binding ------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_curl_handshake_works_through_real_asgi_with_real_headers() -> None:
    server = ApqServer()
    transport = httpx.ASGITransport(app=build_app(server))
    ext = ext_json(sha256_hex(PRODUCT))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        miss = await client.get("/graphql", params={"extensions": ext})
        assert error_code(HttpResponse(miss.status_code, {}, miss.content)) == CODE_NOT_FOUND
        assert miss.headers["cache-control"] == UNCACHED_PQ_ERROR
        stored = await client.get("/graphql", params={"query": PRODUCT, "extensions": ext})
        assert stored.status_code == 200
        assert stored.headers["cache-control"] == "max-age=60, public"
        hit = await client.get("/graphql", params={"extensions": ext})
        assert hit.json() == stored.json()
        refused = await client.get("/graphql", params={"query": MUTATION})
        assert refused.status_code == 405
        assert refused.headers["allow"] == "POST"
    assert (server.stats.hits, server.stats.registered) == (1, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_asgi_binding_returns_what_the_core_handler_returns() -> None:
    reference = ApqServer()
    bound = ApqServer()
    requests = [get_hash_only(PRODUCT), get_full(PRODUCT), get_hash_only(PRODUCT), post_full(CLOCK)]
    transport = httpx.ASGITransport(app=build_app(bound))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for request in requests:
            expected = reference.handle(request)
            if request.method == "GET":
                actual = await client.get(request.target)
            else:
                actual = await client.post(
                    request.target, content=request.body, headers=dict(request.headers)
                )
            assert actual.status_code == expected.status
            assert actual.content == expected.body
            assert actual.headers["cache-control"] == expected.headers["cache-control"]
