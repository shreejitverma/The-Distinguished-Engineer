"""Search vs list endpoints: FTS5 BM25 ranking, facets, fuzzy terms, and (score, id) cursors."""

from __future__ import annotations

import base64
import json
import math
import sqlite3
from collections import defaultdict
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from api_labs.f05_pagination_filtering_and_query_patterns.fts5_search import (
    IDF_FLOOR,
    K1,
    MAX_PAGE_SIZE,
    Article,
    B,
    add_articles,
    create_app,
    encode_token,
    expand_term,
    fetch_ranked,
    fingerprint,
    fuzziness_auto,
    open_index,
    osa_distance,
    parse_query,
    quote_fts5,
    search_page,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

CORPUS = (
    Article(1, "Keyset pagination", "seek over a composite index with a cursor", "pagination"),
    Article(2, "Offset pagination", "offset scans every skipped row", "pagination"),
    Article(3, "Cursor tokens", "opaque cursor tokens bind the query", "pagination"),
    Article(4, "Cursor tokens", "opaque cursor tokens bind the query", "pagination"),
    Article(5, "BM25 ranking", "rank documents by relevance", "search"),
    Article(6, "Facet counts", "count matches per category", "search"),
    Article(7, "Fuzzy matching", "edit distance tolerates a typo in a cursor query", "search"),
    Article(8, "HTTP caching", "validators and freshness", "http"),
    Article(9, "Rate limits", "token buckets", "http"),
    Article(10, "Idempotency keys", "retry safety", "http"),
)


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = open_index(CORPUS)
    yield conn
    conn.close()


def client_for(conn: sqlite3.Connection) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(conn)), base_url="http://test"
    )


def bm25_reference(conn: sqlite3.Connection, terms: tuple[str, ...]) -> dict[int, float]:
    """Independent BM25 from corpus statistics, following ext/fts5/fts5_aux.c."""
    weights = {"title": 10.0, "body": 1.0}
    n_rows = int(conn.execute("SELECT count(*) FROM articles").fetchone()[0])
    doc_len = {
        int(d): int(n)
        for d, n in conn.execute("SELECT doc, count(*) FROM articles_inst GROUP BY doc")
    }
    avgdl = sum(doc_len.values()) / n_rows
    per_term: list[tuple[float, dict[int, float]]] = []
    for term in terms:
        n_hit = int(
            conn.execute("SELECT doc FROM articles_vocab WHERE term = ?", (term,)).fetchone()[0]
        )
        idf = math.log((n_rows - n_hit + 0.5) / (n_hit + 0.5))
        if idf <= 0.0:
            idf = IDF_FLOOR
        freq: dict[int, float] = defaultdict(float)
        for doc, col in conn.execute("SELECT doc, col FROM articles_inst WHERE term = ?", (term,)):
            freq[int(doc)] += weights[str(col)]
        per_term.append((idf, freq))
    matching: set[int] = set(per_term[0][1]).intersection(*(f for _, f in per_term[1:]))
    return {
        d: -sum(
            idf * (f[d] * (K1 + 1.0)) / (f[d] + K1 * (1 - B + B * doc_len[d] / avgdl))
            for idf, f in per_term
        )
        for d in matching
    }


def ranked_ids(conn: sqlite3.Connection, q: str) -> list[int]:
    match = parse_query(conn, q, fuzzy=False).match
    return [h.id for h in fetch_ranked(conn, match, category=None, after=None, limit=1000)]


def walk_search(conn: sqlite3.Connection, q: str, page_size: int) -> list[int]:
    seen: list[int] = []
    token: str | None = None
    for _ in range(100):  # a cursor that never advances must fail, not hang
        page = search_page(conn, q, page_size=page_size, page_token=token)
        results: list[dict[str, Any]] = page["results"]  # type: ignore[assignment]
        seen.extend(int(r["id"]) for r in results)
        token = page["next_page_token"]  # type: ignore[assignment]
        if token is None:
            return seen
    raise AssertionError(f"pagination did not terminate: {seen[:10]}")


# --- Ranking mechanism -------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("q", ["cursor", "cursor query", "pagination", "opaque tokens"])
def test_sqlite_bm25_equals_reference_formula(db: sqlite3.Connection, q: str) -> None:
    parsed = parse_query(db, q, fuzzy=False)
    got = {
        h.id: h.score for h in fetch_ranked(db, parsed.match, category=None, after=None, limit=100)
    }
    want = bm25_reference(db, parsed.terms)
    assert got.keys() == want.keys()
    for doc_id, score in got.items():
        assert math.isclose(score, want[doc_id], rel_tol=1e-12), doc_id


@pytest.mark.unit
def test_better_match_is_numerically_lower_and_title_weight_wins(db: sqlite3.Connection) -> None:
    # Rows 3 and 4 have "cursor" in the title (weight 10); rows 1 and 7 only in the body.
    assert ranked_ids(db, "cursor") == [3, 4, 1, 7]
    scores = [h.score for h in fetch_ranked(db, '"cursor"', category=None, after=None, limit=10)]
    assert all(s < 0 for s in scores)
    assert scores == sorted(scores)


@pytest.mark.unit
def test_idf_is_floored_when_term_is_in_more_than_half_the_rows() -> None:
    docs = [Article(i, "common", "common word" if i < 4 else "rare", "x") for i in range(1, 6)]
    conn = open_index(docs)
    try:
        # n = 3 of N = 5: log((5 - 3 + 0.5) / (3 + 0.5)) < 0, so FTS5 uses 1e-6.
        scores = [
            h.score for h in fetch_ranked(conn, '"word"', category=None, after=None, limit=10)
        ]
        assert len(scores) == 3
        assert all(-10 * IDF_FLOOR < s < 0 for s in scores)
    finally:
        conn.close()


# --- Score cursor and tie-breaking ------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("page_size", [1, 2, 3, 4, 5])
def test_walking_pages_yields_full_ranking_exactly_once(
    db: sqlite3.Connection, page_size: int
) -> None:
    assert walk_search(db, "cursor", page_size) == ranked_ids(db, "cursor") == [3, 4, 1, 7]


@pytest.mark.unit
def test_equal_scores_split_across_a_page_boundary_by_id(db: sqlite3.Connection) -> None:
    first = search_page(db, "cursor", page_size=1)
    results: list[dict[str, Any]] = first["results"]  # type: ignore[assignment]
    assert [r["id"] for r in results] == [3]
    second = search_page(db, "cursor", page_size=1, page_token=str(first["next_page_token"]))
    results = second["results"]  # type: ignore[assignment]
    assert [r["id"] for r in results] == [4]
    assert results[0]["score"] == first["results"][0]["score"]  # type: ignore[index]


@pytest.mark.unit
def test_page_token_carries_the_score_bit_exact(db: sqlite3.Connection) -> None:
    page = search_page(db, "cursor", page_size=3)
    token = str(page["next_page_token"])
    payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    last = page["results"][-1]  # type: ignore[index]
    assert float.fromhex(payload["s"]) == last["score"]
    assert payload["i"] == last["id"] == 1


@pytest.mark.unit
def test_unrelated_insert_moves_scores_and_a_stale_score_cursor_skips_a_hit(
    db: sqlite3.Connection,
) -> None:
    match = '"cursor"'
    page1 = fetch_ranked(db, match, category=None, after=None, limit=3)
    assert [h.id for h in page1] == [3, 4, 1]
    before = {h.id: h.score for h in fetch_ranked(db, match, category=None, after=None, limit=10)}
    add_articles(db, [Article(11, "Unrelated", "nothing to see", "http")])
    after = {h.id: h.score for h in fetch_ranked(db, match, category=None, after=None, limit=10)}
    assert all(after[i] < before[i] for i in before)  # N grew, IDF grew, every score moved
    cursor = (page1[-1].score, page1[-1].id)
    page2 = fetch_ranked(db, match, category=None, after=cursor, limit=10)
    assert [h.id for h in page2] == []  # row 7 (-0.40 now < -0.31) is silently skipped


@pytest.mark.unit
def test_unrelated_delete_moves_scores_and_a_stale_score_cursor_duplicates_hits(
    db: sqlite3.Connection,
) -> None:
    match = '"cursor"'
    page1 = fetch_ranked(db, match, category=None, after=None, limit=2)
    assert [h.id for h in page1] == [3, 4]
    with db:
        db.execute("DELETE FROM articles WHERE id IN (8, 9, 10)")
    cursor = (page1[-1].score, page1[-1].id)
    page2 = fetch_ranked(db, match, category=None, after=cursor, limit=10)
    assert [h.id for h in page2] == [3, 4, 1, 7]  # rows 3 and 4 are served twice


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_endpoint_rejects_token_after_corpus_write(db: sqlite3.Connection) -> None:
    async with client_for(db) as client:
        first = await client.get("/articles:search", params={"q": "cursor", "page_size": 3})
        token = first.json()["next_page_token"]
        add_articles(db, [Article(11, "Unrelated", "nothing to see", "http")])
        resp = await client.get(
            "/articles:search", params={"q": "cursor", "page_size": 3, "page_token": token}
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "stale_page_token"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "write",
    [
        "DELETE FROM articles WHERE id = 10",
        "UPDATE articles SET body = 'retry safety again' WHERE id = 10",
    ],
    ids=["delete", "update"],
)
async def test_search_token_is_stale_after_an_unrelated_delete_or_update(
    db: sqlite3.Connection, write: str
) -> None:
    async with client_for(db) as client:
        first = await client.get("/articles:search", params={"q": "cursor", "page_size": 3})
        token = first.json()["next_page_token"]
        with db:
            db.execute(write)
        resp = await client.get(
            "/articles:search", params={"q": "cursor", "page_size": 3, "page_token": token}
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "stale_page_token"


@pytest.mark.unit
def test_update_trigger_reindexes_the_row(db: sqlite3.Connection) -> None:
    with db:
        db.execute("UPDATE articles SET body = 'edit distance tolerates typos' WHERE id = 7")
    assert ranked_ids(db, "cursor") == [3, 4, 1]  # the old body no longer matches
    assert ranked_ids(db, "typos") == [7]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_keyset_cursor_survives_an_unrelated_insert(db: sqlite3.Connection) -> None:
    async with client_for(db) as client:
        first = await client.get("/articles", params={"category": "pagination", "page_size": 2})
        add_articles(db, [Article(11, "Unrelated", "nothing to see", "http")])
        second = await client.get(
            "/articles",
            params={
                "category": "pagination",
                "page_size": 2,
                "page_token": first.json()["next_page_token"],
            },
        )
    assert second.status_code == 200
    ids = [r["id"] for r in first.json()["results"]] + [r["id"] for r in second.json()["results"]]
    assert ids == [1, 2, 3, 4]
    assert second.json()["next_page_token"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_page_token_is_bound_to_the_category_filter(db: sqlite3.Connection) -> None:
    async with client_for(db) as client:
        first = await client.get("/articles", params={"category": "pagination", "page_size": 2})
        token = first.json()["next_page_token"]
        same = await client.get(
            "/articles", params={"category": "pagination", "page_size": 2, "page_token": token}
        )
        other = await client.get(
            "/articles", params={"category": "http", "page_size": 2, "page_token": token}
        )
    assert same.status_code == 200
    assert other.status_code == 400
    assert other.json()["error"]["code"] == "invalid_page_token"


# --- Facets -------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_facets_ignore_the_category_filter_while_results_respect_it(
    db: sqlite3.Connection,
) -> None:
    async with client_for(db) as client:
        resp = await client.get("/articles:search", params={"q": "cursor", "category": "search"})
    body = resp.json()
    assert [r["id"] for r in body["results"]] == [7]
    assert body["facets"]["category"] == [
        {"value": "pagination", "count": 3},
        {"value": "search", "count": 1},
    ]


@pytest.mark.unit
def test_facets_count_every_match_not_just_the_page(db: sqlite3.Connection) -> None:
    page = search_page(db, "cursor", page_size=1)
    assert len(page["results"]) == 1  # type: ignore[arg-type]
    facets: dict[str, list[dict[str, Any]]] = page["facets"]  # type: ignore[assignment]
    assert sum(f["count"] for f in facets["category"]) == 4


@pytest.mark.unit
def test_facets_with_equal_counts_are_ordered_by_category_name() -> None:
    docs = [Article(i, "word", "x", c) for i, c in enumerate(["zeta", "alpha", "mid"], start=1)]
    conn = open_index(docs)
    try:
        facets: dict[str, list[dict[str, Any]]] = search_page(conn, "word")["facets"]  # type: ignore[assignment]
        assert facets["category"] == [
            {"value": "alpha", "count": 1},
            {"value": "mid", "count": 1},
            {"value": "zeta", "count": 1},
        ]
    finally:
        conn.close()


# --- Fuzzy matching -----------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("term", "edits"), [("ab", 0), ("abc", 1), ("abcde", 1), ("abcdef", 2), ("pagination", 2)]
)
def test_fuzziness_auto_follows_elasticsearch_auto_3_6(term: str, edits: int) -> None:
    assert fuzziness_auto(term) == edits


@pytest.mark.unit
@pytest.mark.parametrize(
    ("a", "b", "dist"),
    [
        ("cursor", "cursor", 0),
        ("cursor", "cusror", 1),
        ("cursor", "curser", 1),
        ("cursor", "cr", 3),
    ],
)
def test_osa_distance_counts_a_transposition_as_one_edit(a: str, b: str, dist: int) -> None:
    assert osa_distance(a, b, limit=2) == min(dist, 3)


@pytest.mark.unit
def test_fuzzy_finds_a_typo_that_exact_search_misses(db: sqlite3.Connection) -> None:
    assert ranked_ids(db, "cusror") == []
    assert parse_query(db, "cusror pagintion", fuzzy=True).match == '"cursor" AND "pagination"'
    fuzzy = parse_query(db, "cusror", fuzzy=True).match
    assert [h.id for h in fetch_ranked(db, fuzzy, category=None, after=None, limit=10)] == [
        3,
        4,
        1,
        7,
    ]


@pytest.mark.unit
def test_short_terms_are_never_expanded(db: sqlite3.Connection) -> None:
    assert expand_term(db, "ro") == ["ro"]


@pytest.mark.unit
def test_expansions_are_capped_and_ordered_by_distance_then_doc_frequency() -> None:
    docs = [Article(1, "card", "card", "x"), Article(2, "card cart", "care", "x")]
    docs += [Article(3, "cart", "cars", "x")]
    conn = open_index(docs)
    try:
        # "card": distance 0. "cart" (2 docs) before "care" and "cars" (1 doc each).
        assert expand_term(conn, "card") == ["card", "cart", "care", "cars"]
        assert expand_term(conn, "card", max_expansions=2) == ["card", "cart"]
    finally:
        conn.close()


# --- Untrusted query input ----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("raw", ['cursor"', "AND", "cursor OR", "NEAR(cursor", "*"])
def test_raw_user_input_is_not_a_valid_fts5_expression(db: sqlite3.Connection, raw: str) -> None:
    with pytest.raises(sqlite3.OperationalError):
        db.execute("SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?", (raw,)).fetchall()


@pytest.mark.unit
def test_quote_fts5_doubles_embedded_quotes(db: sqlite3.Connection) -> None:
    assert quote_fts5('say "hi"') == '"say ""hi"""'
    # The FTS5 string is `cursor"`; the tokenizer drops the quote, so it matches `cursor`.
    match = quote_fts5('cursor"')
    assert [h.id for h in fetch_ranked(db, match, category=None, after=None, limit=10)] == [
        3,
        4,
        1,
        7,
    ]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("q", "status", "ids"),
    [
        ('cursor"', 200, [3, 4, 1, 7]),
        ("cursor OR", 200, []),
        ("NEAR(cursor", 200, []),
        ("title:cursor", 200, []),
        ("AND", 200, [8]),  # the word "and", not the operator
        ("*", 400, None),
        ("", 400, None),
    ],
)
async def test_search_endpoint_quotes_terms_instead_of_failing(
    db: sqlite3.Connection, q: str, status: int, ids: list[int] | None
) -> None:
    async with client_for(db) as client:
        resp = await client.get("/articles:search", params={"q": q})
    assert resp.status_code == status
    if ids is not None:
        assert [r["id"] for r in resp.json()["results"]] == ids
    else:
        assert resp.json()["error"]["code"] == "invalid_argument"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("q", ["x" * 257, " ".join(["cursor"] * 9)])
async def test_query_size_limits_return_400(db: sqlite3.Connection, q: str) -> None:
    async with client_for(db) as client:
        resp = await client.get("/articles:search", params={"q": q})
    assert resp.status_code == 400


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "status", "count"),
    [(None, 200, 10), ("0", 200, 10), ("1000", 200, MAX_PAGE_SIZE), ("-1", 400, 0), ("x", 400, 0)],
)
async def test_page_size_follows_aip_158(raw: str | None, status: int, count: int) -> None:
    conn = open_index(Article(i, "cursor", "text", "x") for i in range(1, 61))
    try:
        params = {"q": "cursor"} | ({} if raw is None else {"page_size": raw})
        async with client_for(conn) as client:
            resp = await client.get("/articles:search", params=params)
    finally:
        conn.close()
    assert resp.status_code == status
    if status == 200:
        assert len(resp.json()["results"]) == count


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed", [{"q": "pagination"}, {"category": "search"}, {"fuzzy": "true"}]
)
async def test_page_token_is_bound_to_the_query(
    db: sqlite3.Connection, changed: dict[str, str]
) -> None:
    async with client_for(db) as client:
        first = await client.get("/articles:search", params={"q": "cursor", "page_size": 1})
        params = {"q": "cursor", "page_size": "1", "page_token": first.json()["next_page_token"]}
        resp = await client.get("/articles:search", params=params | changed)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_page_token"


CURSOR_FP = fingerprint("search", ("cursor",), None, False)
GOOD: dict[str, object] = {"v": 1, "f": CURSOR_FP, "g": len(CORPUS), "s": "-0x1.0p-1", "i": 1}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token",
    [
        "not base64!",
        encode_token(GOOD)[:4] + "...." + encode_token(GOOD)[4:],
        base64.urlsafe_b64encode(b"[1]").decode(),
        encode_token(GOOD | {"s": "inf"}),
        encode_token(GOOD | {"s": 1.5}),
        encode_token(GOOD | {"s": "not-hex"}),
        encode_token(GOOD | {"i": True}),
        encode_token(GOOD | {"v": 2}),
    ],
    ids=[
        "alphabet",
        "malleable",
        "not-object",
        "inf-score",
        "float-score",
        "hex-garbage",
        "bool-id",
        "version",
    ],
)
async def test_tampered_page_tokens_return_400(db: sqlite3.Connection, token: str) -> None:
    async with client_for(db) as client:
        good = await client.get(
            "/articles:search", params={"q": "cursor", "page_token": encode_token(GOOD)}
        )
        resp = await client.get("/articles:search", params={"q": "cursor", "page_token": token})
    assert good.status_code == 200  # the untampered control token is accepted
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_page_token"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_and_search_routes_are_distinct(db: sqlite3.Connection) -> None:
    async with client_for(db) as client:
        listed = await client.get("/articles", params={"page_size": 3})
        searched = await client.get("/articles:search", params={"q": "relevance"})
        wrong_method = await client.post("/articles:search", params={"q": "relevance"})
    assert [r["id"] for r in listed.json()["results"]] == [1, 2, 3]
    assert "score" not in listed.json()["results"][0]
    assert [r["id"] for r in searched.json()["results"]] == [5]
    assert wrong_method.status_code == 405


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bad_fuzzy_flag_and_bad_list_token_return_400(db: sqlite3.Connection) -> None:
    async with client_for(db) as client:
        fuzzy = await client.get("/articles:search", params={"q": "cursor", "fuzzy": "yes"})
        listed = await client.get("/articles", params={"page_token": encode_token(GOOD)})
    assert fuzzy.status_code == 400
    assert fuzzy.json()["error"]["code"] == "invalid_argument"
    assert listed.status_code == 400  # a search token is not valid for the list endpoint
    assert listed.json()["error"]["code"] == "invalid_page_token"
