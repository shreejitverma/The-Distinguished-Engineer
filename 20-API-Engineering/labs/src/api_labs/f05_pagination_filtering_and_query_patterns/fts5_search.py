"""Search vs list endpoints over SQLite FTS5: BM25 ranking, facets, fuzzy terms, score cursors.

`GET /articles` is a list endpoint: exact filter, keyset order on the immutable primary key.
`GET /articles:search` is a search endpoint: FTS5 `MATCH`, `bm25()` order with an `id`
tie-breaker, disjunctive category facets, and optional Elasticsearch-style `AUTO` fuzziness.
A search cursor carries the exact score (`float.hex`), the last `id`, a query fingerprint, and
the corpus generation, because BM25 scores move whenever any row is written.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from starlette.requests import Request

# bm25() weights for the (title, body) columns; they must match RANKED_SQL below.
BM25_WEIGHTS = (10.0, 1.0)
# Constants hard-coded in SQLite ext/fts5/fts5_aux.c, fts5Bm25Function and fts5Bm25GetData.
K1 = 1.2
B = 0.75
IDF_FLOOR = 1e-6
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50
MAX_QUERY_CHARS = 256
MAX_TERMS = 8
MAX_EXPANSIONS = 50
# Letters and digits; an approximation of the unicode61 token classes (L*, N*, Co).
TOKEN_RE = re.compile(r"[^\W_]+")

SCHEMA = """
CREATE TABLE articles (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  category TEXT NOT NULL
);
CREATE VIRTUAL TABLE articles_fts USING fts5(
  title, body, content='articles', content_rowid='id'
);
CREATE VIRTUAL TABLE articles_vocab USING fts5vocab(articles_fts, 'row');
CREATE VIRTUAL TABLE articles_inst USING fts5vocab(articles_fts, 'instance');
CREATE TABLE index_meta (k TEXT PRIMARY KEY, generation INTEGER NOT NULL);
INSERT INTO index_meta VALUES ('corpus', 0);
CREATE TRIGGER articles_ai AFTER INSERT ON articles BEGIN
  INSERT INTO articles_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
  UPDATE index_meta SET generation = generation + 1 WHERE k = 'corpus';
END;
CREATE TRIGGER articles_ad AFTER DELETE ON articles BEGIN
  INSERT INTO articles_fts(articles_fts, rowid, title, body)
    VALUES ('delete', old.id, old.title, old.body);
  UPDATE index_meta SET generation = generation + 1 WHERE k = 'corpus';
END;
CREATE TRIGGER articles_au AFTER UPDATE ON articles BEGIN
  INSERT INTO articles_fts(articles_fts, rowid, title, body)
    VALUES ('delete', old.id, old.title, old.body);
  INSERT INTO articles_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
  UPDATE index_meta SET generation = generation + 1 WHERE k = 'corpus';
END;
"""

RANKED_SQL = """
WITH hits AS MATERIALIZED (
  SELECT a.id AS id, a.title AS title, a.category AS category,
         bm25(articles_fts, 10.0, 1.0) AS score
  FROM articles_fts JOIN articles AS a ON a.id = articles_fts.rowid
  WHERE articles_fts MATCH :match
)
SELECT id, title, category, score FROM hits
WHERE (:category IS NULL OR category = :category)
  AND (score, id) > (:after_score, :after_id)
ORDER BY score, id
LIMIT :limit
"""

FACET_SQL = """
SELECT a.category, count(*) AS n
FROM articles_fts JOIN articles AS a ON a.id = articles_fts.rowid
WHERE articles_fts MATCH :match
GROUP BY a.category
ORDER BY n DESC, a.category
"""

LIST_SQL = """
SELECT id, title, category FROM articles
WHERE (:category IS NULL OR category = :category) AND id > :after_id
ORDER BY id
LIMIT :limit
"""


class SearchError(ValueError):
    """A client error that the endpoints map to `400 Bad Request`."""

    code = "invalid_argument"


class PageTokenError(SearchError):
    code = "invalid_page_token"


class StalePageTokenError(SearchError):
    code = "stale_page_token"


@dataclass(frozen=True, slots=True)
class Article:
    id: int
    title: str
    body: str
    category: str


@dataclass(frozen=True, slots=True)
class Hit:
    id: int
    title: str
    category: str
    score: float


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    terms: tuple[str, ...]
    match: str


def open_index(articles: Iterable[Article] = ()) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    add_articles(conn, articles)
    return conn


def add_articles(conn: sqlite3.Connection, articles: Iterable[Article]) -> None:
    with conn:
        conn.executemany(
            "INSERT INTO articles (id, title, body, category) VALUES (?, ?, ?, ?)",
            [(a.id, a.title, a.body, a.category) for a in articles],
        )


def corpus_generation(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT generation FROM index_meta WHERE k = 'corpus'").fetchone()
    return int(row[0])


def quote_fts5(term: str) -> str:
    """FTS5 string literal: double quotes, embedded quotes doubled (fts5.html Section 3.1)."""
    return '"' + term.replace('"', '""') + '"'


def fuzziness_auto(term: str) -> int:
    """Elasticsearch `fuzziness: AUTO` (AUTO:3,6): 0..2 exact, 3..5 one edit, >5 two edits."""
    if len(term) <= 2:
        return 0
    if len(term) <= 5:
        return 1
    return 2


def osa_distance(a: str, b: str, limit: int) -> int:
    """Optimal string alignment distance (adjacent transpositions count as one edit).

    Returns `limit + 1` as soon as the distance is known to exceed `limit`.
    """
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return min(prev[len(b)], limit + 1)


def expand_term(
    conn: sqlite3.Connection, term: str, max_expansions: int = MAX_EXPANSIONS
) -> list[str]:
    """Indexed terms within `fuzziness_auto(term)` edits, closest and most frequent first."""
    k = fuzziness_auto(term)
    if k == 0:
        return [term]
    rows = conn.execute(
        "SELECT term, doc FROM articles_vocab WHERE length(term) BETWEEN ? AND ?",
        (len(term) - k, len(term) + k),
    ).fetchall()
    ranked: list[tuple[int, int, str]] = []
    for vocab_term, doc_count in rows:
        distance = osa_distance(term, str(vocab_term), k)
        if distance <= k:
            ranked.append((distance, -int(doc_count), str(vocab_term)))
    ranked.sort()
    return [t for _, _, t in ranked[:max_expansions]] or [term]


def parse_query(conn: sqlite3.Connection, q: str, *, fuzzy: bool) -> ParsedQuery:
    """Turn untrusted `q` into an FTS5 expression of quoted terms joined by `AND`."""
    if len(q) > MAX_QUERY_CHARS:
        raise SearchError(f"q is longer than {MAX_QUERY_CHARS} characters")
    terms = tuple(t.lower() for t in TOKEN_RE.findall(q))
    if not terms:
        raise SearchError("q has no searchable terms")
    if len(terms) > MAX_TERMS:
        raise SearchError(f"q has more than {MAX_TERMS} terms")
    groups: list[str] = []
    for term in terms:
        alternatives = expand_term(conn, term) if fuzzy else [term]
        quoted = [quote_fts5(t) for t in alternatives]
        groups.append(quoted[0] if len(quoted) == 1 else "(" + " OR ".join(quoted) + ")")
    return ParsedQuery(terms=terms, match=" AND ".join(groups))


def fetch_ranked(
    conn: sqlite3.Connection,
    match: str,
    *,
    category: str | None,
    after: tuple[float, int] | None,
    limit: int,
) -> list[Hit]:
    after_score, after_id = after if after is not None else (-math.inf, 0)
    rows = conn.execute(
        RANKED_SQL,
        {
            "match": match,
            "category": category,
            "after_score": after_score,
            "after_id": after_id,
            "limit": limit,
        },
    ).fetchall()
    return [Hit(int(r[0]), str(r[1]), str(r[2]), float(r[3])) for r in rows]


def category_facets(conn: sqlite3.Connection, match: str) -> list[tuple[str, int]]:
    """Counts over every match, ignoring the category filter (post-filter semantics)."""
    rows = conn.execute(FACET_SQL, {"match": match}).fetchall()
    return [(str(r[0]), int(r[1])) for r in rows]


def fingerprint(*parts: object) -> str:
    canonical = json.dumps(parts, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def encode_token(payload: Mapping[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_token(token: str, expected_fingerprint: str) -> dict[str, object]:
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        payload: object = json.loads(raw)
    except (binascii.Error, ValueError) as exc:
        raise PageTokenError("page_token is malformed") from exc
    if not isinstance(payload, dict):
        raise PageTokenError("page_token is malformed")
    fields = cast("dict[str, object]", payload)  # json object keys are always str
    if fields.get("v") != 1:
        raise PageTokenError("page_token is malformed")
    if fields.get("f") != expected_fingerprint:
        raise PageTokenError("page_token was issued for a different query")
    return fields


def _int_field(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise PageTokenError("page_token is malformed")
    return value


def _score_field(payload: dict[str, object]) -> float:
    value = payload.get("s")
    if not isinstance(value, str):
        raise PageTokenError("page_token is malformed")
    try:
        score = float.fromhex(value)
    except ValueError as exc:
        raise PageTokenError("page_token is malformed") from exc
    if not math.isfinite(score):
        raise PageTokenError("page_token is malformed")
    return score


def parse_page_size(raw: str | None) -> int:
    """AIP-158: absent or 0 -> default, above max -> coerced down, negative -> error."""
    if raw is None:
        return DEFAULT_PAGE_SIZE
    try:
        size = int(raw)
    except ValueError as exc:
        raise SearchError("page_size must be an integer") from exc
    if size < 0:
        raise SearchError("page_size must not be negative")
    return min(size or DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)


def parse_fuzzy(raw: str | None) -> bool:
    if raw is None or raw == "false":
        return False
    if raw == "true":
        return True
    raise SearchError("fuzzy must be true or false")


def search_page(
    conn: sqlite3.Connection,
    q: str,
    *,
    category: str | None = None,
    fuzzy: bool = False,
    page_size: int = DEFAULT_PAGE_SIZE,
    page_token: str | None = None,
) -> dict[str, object]:
    parsed = parse_query(conn, q, fuzzy=fuzzy)
    query_fp = fingerprint("search", parsed.terms, category, fuzzy)
    generation = corpus_generation(conn)
    after: tuple[float, int] | None = None
    if page_token is not None:
        payload = decode_token(page_token, query_fp)
        if _int_field(payload, "g") != generation:
            raise StalePageTokenError("corpus changed since page_token was issued; restart")
        after = (_score_field(payload), _int_field(payload, "i"))
    rows = fetch_ranked(conn, parsed.match, category=category, after=after, limit=page_size + 1)
    hits, more = rows[:page_size], len(rows) > page_size
    next_token = None
    if more:
        last = hits[-1]
        next_token = encode_token(
            {"v": 1, "f": query_fp, "g": generation, "s": last.score.hex(), "i": last.id}
        )
    return {
        "results": [
            {"id": h.id, "title": h.title, "category": h.category, "score": h.score} for h in hits
        ],
        "facets": {
            "category": [{"value": v, "count": n} for v, n in category_facets(conn, parsed.match)]
        },
        "next_page_token": next_token,
    }


def list_page(
    conn: sqlite3.Connection,
    *,
    category: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    page_token: str | None = None,
) -> dict[str, object]:
    list_fp = fingerprint("list", category)
    after_id = 0
    if page_token is not None:
        after_id = _int_field(decode_token(page_token, list_fp), "i")
    rows = conn.execute(
        LIST_SQL, {"category": category, "after_id": after_id, "limit": page_size + 1}
    ).fetchall()
    items = [{"id": int(r[0]), "title": str(r[1]), "category": str(r[2])} for r in rows]
    page, more = items[:page_size], len(items) > page_size
    next_token = encode_token({"v": 1, "f": list_fp, "i": page[-1]["id"]}) if more else None
    return {"results": page, "next_page_token": next_token}


def _error(exc: SearchError) -> JSONResponse:
    return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=400)


def create_app(conn: sqlite3.Connection) -> Starlette:
    async def list_articles(request: Request) -> JSONResponse:
        params = request.query_params
        try:
            body = list_page(
                conn,
                category=params.get("category"),
                page_size=parse_page_size(params.get("page_size")),
                page_token=params.get("page_token"),
            )
        except SearchError as exc:
            return _error(exc)
        return JSONResponse(body)

    async def search_articles(request: Request) -> JSONResponse:
        params = request.query_params
        try:
            body = search_page(
                conn,
                params.get("q", ""),
                category=params.get("category"),
                fuzzy=parse_fuzzy(params.get("fuzzy")),
                page_size=parse_page_size(params.get("page_size")),
                page_token=params.get("page_token"),
            )
        except SearchError as exc:
            return _error(exc)
        return JSONResponse(body)

    return Starlette(
        routes=[
            Route("/articles", list_articles, methods=["GET"]),
            Route("/articles:search", search_articles, methods=["GET"]),
        ]
    )
