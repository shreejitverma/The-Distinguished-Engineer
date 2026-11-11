"""Unit tests for RFC 8288 Link parsing, a Level 3 order state machine, and a link-driven client."""

import re

import h11
import httpcore
import httpx
import pytest

from api_labs.f04_rest_and_resource_modeling.hypermedia_links import (
    HAL_JSON,
    JSON_API,
    LAYOUT_V1,
    LAYOUT_V2,
    REL_CANCEL,
    REL_DELIVER,
    REL_ORDERS,
    REL_PAY,
    REL_SHIP,
    HardcodedClient,
    HypermediaClient,
    HypermediaError,
    Link,
    LinkSyntaxError,
    OrderStore,
    Representation,
    UrlLayout,
    build_order_app,
    format_link,
    links_from_hal,
    links_from_jsonapi,
    links_from_response,
    order_links,
    parse_link_header,
    representation_url,
)

BASE = "http://test"
CTX = "https://example.org/TheBook/chapter3"
# JSON:API 1.1 member names: globally allowed characters at both ends, "-" and "_" inside.
JSONAPI_MEMBER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?")


def _client(layout: UrlLayout = LAYOUT_V1, store: OrderStore | None = None) -> httpx.AsyncClient:
    app = build_order_app(layout, store)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


def _httpx_links(*values: str) -> dict[str | None, dict[str, str]]:
    request = httpx.Request("GET", CTX)
    return httpx.Response(200, headers=[("link", v) for v in values], request=request).links


@pytest.mark.unit
def test_parser_matches_rfc_8288_section_3_5_examples() -> None:
    values = [
        "</TheBook/chapter2>; rel=\"previous\"; title*=UTF-8'de'letztes%20Kapitel,"
        " </TheBook/chapter4>; rel=\"next\"; title*=UTF-8'de'n%c3%a4chstes%20Kapitel",
        '<http://example.org/>; rel="start http://example.net/relation/other"',
        '</terms>; rel="copyright"; anchor="#foo"',
    ]
    links = parse_link_header(values, base=CTX, context=CTX)
    assert [(link.rel, link.target, link.attr("title")) for link in links[:2]] == [
        ("previous", "https://example.org/TheBook/chapter2", "letztes Kapitel"),
        ("next", "https://example.org/TheBook/chapter4", "n\u00e4chstes Kapitel"),
    ]
    assert [(link.rel, link.target) for link in links[2:4]] == [
        ("start", "http://example.org/"),
        ("http://example.net/relation/other", "http://example.org/"),
    ]
    assert links[4] == Link("https://example.org/terms", "copyright", CTX + "#foo")
    one_line = parse_link_header(
        ['<https://example.org/>; rel="start", <https://example.org/index>; rel="index"'],
        base=CTX,
        context=CTX,
    )
    two_lines = parse_link_header(
        ['<https://example.org/>; rel="start"', '<https://example.org/index>; rel="index"'],
        base=CTX,
        context=CTX,
    )
    assert one_line == two_lines
    assert [link.rel for link in one_line] == ["start", "index"]


@pytest.mark.unit
def test_parameter_rules_token_equals_quoted_first_singleton_wins_star_wins() -> None:
    (token,) = parse_link_header(["</a>; rel=NEXT"], base=CTX, context=CTX)
    (quoted,) = parse_link_header(['</a>; rel="next"'], base=CTX, context=CTX)
    assert token == quoted
    assert token.rel == "next"
    (dup,) = parse_link_header(
        ['</a>; rel=next; rel=prev; title="one"; title="two"'], base=CTX, context=CTX
    )
    assert (dup.rel, dup.attr("title")) == ("next", "one")
    (star,) = parse_link_header(
        ["</a>; rel=next; title=\"plain\"; title*=UTF-8''st%C3%A4r"], base=CTX, context=CTX
    )
    assert star.attributes == (("title", "st\u00e4r"),)
    (bad_charset,) = parse_link_header(
        ["</a>; rel=next; title=\"plain\"; title*=ISO-8859-1''x"], base=CTX, context=CTX
    )
    assert bad_charset.attributes == (("title", "plain"),)
    # The lab strips trailing OWS from token values (Appendix B.3 step 7.4 would keep it).
    (spaced,) = parse_link_header(
        ["</a>; rel=next ; title=plain \t; type=text/html"], base=CTX, context=CTX
    )
    assert spaced.attributes == (("title", "plain"), ("type", "text/html"))
    # rel MUST be present (RFC 8288 Section 3.3); without it there is no link at all.
    assert parse_link_header(['</a>; title="x"', '</b>; rel=""'], base=CTX, context=CTX) == []


@pytest.mark.unit
def test_quoted_comma_and_angle_bracket_cannot_inject_a_phantom_link() -> None:
    hostile = (
        '<https://example.org/a>; rel="item"; title="x, <https://evil.example/steal>; rel=next"'
    )
    links = parse_link_header([hostile], base=CTX, context=CTX)
    assert [(link.rel, link.target) for link in links] == [("item", "https://example.org/a")]
    # httpx 0.28.1 Response.links splits on ", <" before honouring quotes.
    assert _httpx_links(hostile)["next"]["url"] == "https://evil.example/steal"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("values", "httpx_keys", "rfc_pairs"),
    [
        pytest.param(
            ('<https://example.org/1>; rel="item"', '<https://example.org/2>; rel="item"'),
            ["item"],
            [("item", "https://example.org/1"), ("item", "https://example.org/2")],
            id="same-rel-collapses",
        ),
        pytest.param(
            ('<https://example.org/1>; rel="next last"',),
            ["next last"],
            [("next", "https://example.org/1"), ("last", "https://example.org/1")],
            id="multi-rel-unsplit",
        ),
        pytest.param(
            ('<https://example.org/1>; rel="NEXT"',),
            ["NEXT"],
            [("next", "https://example.org/1")],
            id="rel-not-case-folded",
        ),
        pytest.param(
            ('<https://example.org/1>; title="a=b"; rel="next"',),
            ["https://example.org/1"],
            [("next", "https://example.org/1")],
            id="equals-in-title-drops-rel",
        ),
    ],
)
def test_httpx_0_28_1_links_property_loses_information(
    values: tuple[str, ...], httpx_keys: list[str], rfc_pairs: list[tuple[str, str]]
) -> None:
    assert httpx.__version__ == "0.28.1"
    assert sorted(k or "" for k in _httpx_links(*values)) == sorted(httpx_keys)
    links = parse_link_header(values, base=CTX, context=CTX)
    assert [(link.rel, link.target) for link in links] == rfc_pairs


@pytest.mark.unit
def test_httpx_0_28_1_does_not_resolve_relative_targets() -> None:
    assert _httpx_links('</TheBook/chapter4>; rel="next"')["next"]["url"] == "/TheBook/chapter4"
    (link,) = parse_link_header(['</TheBook/chapter4>; rel="next"'], base=CTX, context=CTX)
    assert link.target == "https://example.org/TheBook/chapter4"


@pytest.mark.unit
def test_serializer_quotes_hostile_titles_so_they_round_trip() -> None:
    title = 'say "hi", \\ then; <go>'
    value = format_link("/orders/o1", ["self", REL_PAY], [("title", title)])
    assert value == (
        '</orders/o1>; rel="self https://api.example.com/rels/pay"; '
        'title="say \\"hi\\", \\\\ then; <go>"'
    )
    links = parse_link_header([value], base=BASE + "/x", context=None)
    assert [(link.rel, link.attr("title")) for link in links] == [("self", title), (REL_PAY, title)]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("target", "rels", "params"),
    [
        pytest.param("/a\r\nSet-Cookie: x=1", ["self"], [], id="crlf-in-target"),
        pytest.param("/a>; rel=evil", ["self"], [], id="angle-bracket-in-target"),
        pytest.param("/a b", ["self"], [], id="space-in-target"),
        pytest.param("/caf\u00e9", ["self"], [], id="iri-not-converted-to-uri"),
        pytest.param("/a", ["/linkrels/slot/book"], [], id="relative-extension-rel"),
        pytest.param("/a", ["https://api.example.com/Rels/Pay"], [], id="uppercase-extension-rel"),
        pytest.param(
            "/a", ["https://api.example.com/rels/a b"], [], id="space-inside-one-extension-rel"
        ),
        pytest.param("/a", [], [], id="no-rel"),
        pytest.param("/a", ["self"], [("ti;tle", "x")], id="param-name-not-a-token"),
        pytest.param("/a", ["self"], [("title", "line\nbreak")], id="lf-in-title"),
        pytest.param("/a", ["self"], [("anchor", "https://evil.example/")], id="anchor-param"),
        pytest.param(
            "/a", ["self"], [("Anchor", "https://evil.example/")], id="anchor-param-mixed-case"
        ),
        pytest.param("/a", ["self"], [("REL", "next")], id="rel-param-uppercase"),
    ],
)
def test_serializer_rejects_links_that_would_break_the_field_grammar(
    target: str, rels: list[str], params: list[tuple[str, str]]
) -> None:
    with pytest.raises(LinkSyntaxError):
        format_link(target, rels, params)


@pytest.mark.unit
def test_anchor_keyword_sets_the_link_context_explicitly() -> None:
    value = format_link("/orders/o1", ["self"], anchor="/orders/o1")
    assert value == '</orders/o1>; rel="self"; anchor="/orders/o1"'
    (link,) = parse_link_header([value], base=BASE + "/orders/o1/pay", context=None)
    assert link == Link(BASE + "/orders/o1", "self", BASE + "/orders/o1")


@pytest.mark.unit
def test_rel_splits_only_on_space_and_tab() -> None:
    tab = parse_link_header(['</a>; rel="next\tlast"'], base=CTX, context=CTX)
    assert [link.rel for link in tab] == ["next", "last"]
    nbsp = f'</a>; rel="https://api.example.com/rels/x\u00a0{REL_PAY}"'
    (link,) = parse_link_header([nbsp], base=CTX, context=CTX)
    assert link.rel == f"https://api.example.com/rels/x\u00a0{REL_PAY}"


@pytest.mark.unit
def test_representation_url_applies_rfc_9110_section_6_4_2() -> None:
    order = "http://test/orders/o1"
    assert representation_url("GET", 200, order, None) == order
    assert representation_url("GET", 200, order, "/elsewhere") == order
    assert representation_url("HEAD", 200, order, None) == order
    assert representation_url("GET", 203, order, None) == order
    assert representation_url("GET", 206, order, None) == order
    assert representation_url("GET", 204, order, None) is None
    assert representation_url("GET", 404, order, None) is None
    assert representation_url("POST", 200, order + "/pay", None) is None
    assert representation_url("POST", 200, order + "/pay", "/orders/o1") == order
    assert representation_url("POST", 201, "http://test/orders", "orders/o1") == order


@pytest.mark.unit
@pytest.mark.asyncio
async def test_links_advertise_exactly_the_legal_transitions_in_three_serialisations() -> None:
    expected = {
        "pending_payment": {REL_PAY, REL_CANCEL},
        "paid": {REL_SHIP, REL_CANCEL},
        "shipped": {REL_DELIVER},
        "delivered": set[str](),
    }
    async with _client() as http:
        created = await http.post("/orders", json={"sku": "kb-1", "quantity": 2})
        assert created.status_code == 201
        assert created.headers["location"] == created.headers["content-location"] == "/orders/o1"
        for state, rels, step in [
            ("pending_payment", expected["pending_payment"], "pay"),
            ("paid", expected["paid"], "ship"),
            ("shipped", expected["shipped"], "deliver"),
            ("delivered", expected["delivered"], None),
        ]:
            hal = await http.get("/orders/o1")
            api = await http.get("/orders/o1", headers={"accept": JSON_API})
            assert hal.headers["content-type"] == HAL_JSON
            assert api.headers["content-type"] == JSON_API
            assert hal.headers["vary"] == "Accept"
            header = {(link.rel, link.target) for link in links_from_response(hal)}
            assert header == links_from_hal(hal.json(), base=str(hal.url))
            assert header == links_from_jsonapi(api.json(), base=str(api.url))
            assert all(JSONAPI_MEMBER.fullmatch(name) for name in api.json()["data"]["links"])
            actions = {rel for rel, _ in header} - {"self", "collection"}
            assert (hal.json()["state"], actions) == (state, rels)
            if step is not None:
                assert (await http.post(f"/orders/o1/{step}")).status_code == 200
        wrong_method = await http.get("/orders/o1/pay")
        assert (wrong_method.status_code, wrong_method.headers["allow"]) == (405, "POST")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("layout", [LAYOUT_V1, LAYOUT_V2], ids=["v1", "v2"])
async def test_link_driven_client_survives_a_url_layout_change(layout: UrlLayout) -> None:
    async with _client(layout) as http:
        client = HypermediaClient(http, "/")
        rep = await client.place_order("kb-1", 1)
        assert rep.status == 201
        for rel in (REL_PAY, REL_SHIP, REL_DELIVER):
            rep = await client.transition(rep, rel)
            assert rep.status == 200
        assert rep.body["state"] == "delivered"
        assert rep.rels() == {"self", "collection"}
        assert rep.url == BASE + layout.order_url("o1")
        with pytest.raises(HypermediaError, match=re.escape(f"no {REL_CANCEL!r} link")):
            client.follow(rep, REL_CANCEL)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hardcoded_client_breaks_on_the_same_layout_change() -> None:
    async with _client(LAYOUT_V1) as http:
        assert await HardcodedClient(http).place_and_pay("kb-1", 1) == 200
    async with _client(LAYOUT_V2) as http:
        assert await HardcodedClient(http).place_and_pay("kb-1", 1) == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_link_gets_409_problem_with_self_link_and_refresh_drops_it() -> None:
    store = OrderStore()
    async with _client(LAYOUT_V1, store) as http:
        alice = HypermediaClient(http, "/")
        stale = await alice.place_order("kb-1", 1)
        assert REL_CANCEL in stale.rels()
        assert (await http.post("/orders/o1/pay")).status_code == 200
        assert (await http.post("/orders/o1/ship")).status_code == 200
        conflict = await http.post(alice.follow(stale, REL_CANCEL))
        assert conflict.status_code == 409
        assert conflict.headers["content-type"] == "application/problem+json"
        assert conflict.json()["detail"] == "cancel is not available in state shipped"
        assert links_from_response(conflict) == [
            Link(BASE + "/orders/o1", "self", BASE + "/orders/o1")
        ]
        fresh = await alice.refresh(stale)
        assert fresh.rels() == {"self", "collection", REL_DELIVER}
        assert store.orders["o1"].state == "shipped"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_level_0_tunnel_reports_failure_as_200_while_level_3_returns_409() -> None:
    async with _client() as http:
        await http.post("/orders", json={"sku": "kb-1", "quantity": 1})
        await http.post("/orders/o1/cancel")
        tunnel = await http.post("/rpc", json={"method": "pay", "order_id": "o1"})
        assert (tunnel.status_code, tunnel.json()) == (200, {"error": "operation failed"})
        assert "link" not in tunnel.headers
        level3 = await http.post("/orders/o1/pay")
        assert level3.status_code == 409
        assert (await http.get("/orders/o1")).json()["state"] == "cancelled"


def _mock_client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler, base_url="https://api.example.com")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_refuses_anonymous_context_and_cross_origin_links() -> None:
    def home(request: httpx.Request) -> httpx.Response:
        links = [
            '<https://evil.example/orders>; rel="https://api.example.com/rels/orders"',
            '</orders>; rel="https://api.example.com/rels/orders"; anchor="https://evil.example/"',
            '<https://api.example.com:443/v2/orders>; rel="https://api.example.com/rels/orders"',
        ]
        if request.method == "POST":
            return httpx.Response(200, json={}, headers=[("link", '</orders/o1>; rel="self"')])
        return httpx.Response(200, json={}, headers=[("link", v) for v in links])

    async with _mock_client(httpx.MockTransport(home)) as http:
        client = HypermediaClient(http, "/")
        rep = await client.start()
        assert [link.target for link in rep.links] == ["https://api.example.com:443/v2/orders"]
        assert client.follow(rep, REL_ORDERS) == "https://api.example.com:443/v2/orders"
        with pytest.raises(HypermediaError, match="anonymous link context"):
            await client.transition(
                Representation(rep.url, 200, (Link(rep.url or "", REL_PAY, rep.url),), {}),
                REL_PAY,
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_never_follows_links_from_an_anonymous_error_response() -> None:
    def conflict(request: httpx.Request) -> httpx.Response:
        link = '</orders/o1/pay>; rel="https://api.example.com/rels/pay"'
        return httpx.Response(409, json={"title": "Conflict"}, headers=[("link", link)])

    async with _mock_client(httpx.MockTransport(conflict)) as http:
        client = HypermediaClient(http, "/")
        rep = await client.start()
        assert (rep.status, rep.url, rep.rels()) == (409, None, set())
        assert [link.context for link in rep.links] == [None]
        with pytest.raises(HypermediaError, match=re.escape(f"no {REL_PAY!r} link")):
            client.follow(rep, REL_PAY)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_follows_only_links_whose_context_is_the_representation_it_holds() -> None:
    def home(request: httpx.Request) -> httpx.Response:
        links = [
            '</other/pay>; rel="https://api.example.com/rels/pay"; anchor="/other"',
            '</orders>; rel="https://api.example.com/rels/orders"',
        ]
        return httpx.Response(200, json={}, headers=[("link", v) for v in links])

    async with _mock_client(httpx.MockTransport(home)) as http:
        client = HypermediaClient(http, "/")
        rep = await client.start()
        assert {link.rel for link in rep.links} == {REL_PAY, REL_ORDERS}
        assert rep.rels() == {REL_ORDERS}
        assert client.follow(rep, REL_ORDERS) == "https://api.example.com/orders"
        with pytest.raises(HypermediaError, match=re.escape(f"no {REL_PAY!r} link")):
            client.follow(rep, REL_PAY)


def _feed_response_head(link_bytes: int) -> str:
    """Feed an unfinished response head (no blank line yet) to an h11 client connection."""
    conn = h11.Connection(
        h11.CLIENT,
        max_incomplete_event_size=httpcore.AsyncHTTP11Connection.MAX_INCOMPLETE_EVENT_SIZE,
    )
    conn.send(h11.Request(method="GET", target="/", headers=[("Host", "test")]))
    link = b'<https://api.example.com/orders/o1>; rel="item"'
    value = b", ".join([link] * (link_bytes // (len(link) + 2)))
    conn.receive_data(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nLink: " + value + b"\r\n")
    try:
        event = conn.next_event()
    except h11.RemoteProtocolError as exc:
        return str(exc)
    return "need-data" if event is h11.NEED_DATA else type(event).__name__


@pytest.mark.unit
def test_httpcore_h11_client_caps_an_unfinished_header_block_at_100_kib() -> None:
    assert httpcore.AsyncHTTP11Connection.MAX_INCOMPLETE_EVENT_SIZE == 100 * 1024
    assert _feed_response_head(99 * 1024) == "need-data"
    assert _feed_response_head(101 * 1024) == "Receive buffer too long"


@pytest.mark.unit
def test_link_header_bytes_per_state_track_the_advertised_transitions() -> None:
    store = OrderStore()
    order = store.create("kb-1", 1)
    sizes: dict[str, int] = {}
    for rel in (REL_PAY, REL_SHIP, REL_DELIVER, None):
        pairs = order_links(LAYOUT_V1, order)
        sizes[order.state.value] = len(", ".join(format_link(t, [r]) for r, t in pairs))
        if rel is not None:
            assert store.apply(order, rel)
    assert sizes == {"pending_payment": 175, "paid": 177, "shipped": 119, "delivered": 53}
