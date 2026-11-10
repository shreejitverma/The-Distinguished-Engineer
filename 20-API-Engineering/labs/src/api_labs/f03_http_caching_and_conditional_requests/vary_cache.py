"""Vary-driven secondary cache keys (RFC 9111 Section 4.1), normalization, and hit-ratio collapse.

The lab models a shared cache for `GET` only, so the primary key is the target URI.
Each stored response carries its own parsed `Vary` and the normalized values of the fields it
nominated in the request that caused it to be stored.
"""

from __future__ import annotations

import random
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

type HeaderLines = Sequence[tuple[str, str]]
type VarySpec = tuple[str, ...] | Literal["*"]
type Nominated = tuple[tuple[str, str | None], ...]
type Normalizer = Callable[[str], str]
type Canonicalizer = Callable[[str | None], str]

WILDCARD: Final = "*"
_QVALUE: Final = re.compile(r"(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)")


def field_lines(headers: HeaderLines, name: str) -> list[str]:
    """Every field line value for `name`, in order, with surrounding OWS removed."""
    wanted = name.lower()
    return [value.strip(" \t") for key, value in headers if key.lower() == wanted]


def combined_field_value(headers: HeaderLines, name: str) -> str | None:
    """Combine repeated field lines (RFC 9110 Section 5.2); `None` means the field is absent.

    `Cookie` is the exception: HTTP/2 and HTTP/3 split it into crumbs that must be rejoined with
    `"; "` (RFC 9113 Section 8.2.3, RFC 9114 Section 4.2.1), never with a comma.
    """
    lines = field_lines(headers, name)
    if not lines:
        return None
    separator = "; " if name.lower() == "cookie" else ", "
    return separator.join(lines)


def parse_vary(lines: Sequence[str]) -> VarySpec:
    """Parse `Vary = #( "*" / field-name )` (RFC 9110 Section 12.5.5) across all field lines."""
    members: list[str] = []
    for line in lines:
        for raw in line.split(","):
            member = raw.strip(" \t").lower()  # field names are case-insensitive (Section 5.1)
            if not member:
                continue  # empty list elements are ignored (Section 5.6.1.2)
            if member == WILDCARD:
                return WILDCARD
            if member not in members:
                members.append(member)
    return tuple(members)


def list_members(value: str) -> list[str]:
    """Split a `#rule` list, dropping OWS and empty elements (RFC 9110 Section 5.6.1.2)."""
    return [member.strip(" \t") for member in value.split(",") if member.strip(" \t")]


def parse_weighted(member: str) -> tuple[str, int | None]:
    """Split `token [ weight ]` into a lowercased token and a weight in thousandths.

    The weight is `None` when the `q` parameter is not a valid qvalue (RFC 9110 Section 12.4.2).
    """
    token, _, params = member.partition(";")
    weight: int | None = 1000
    for param in params.split(";"):
        name, _, raw = param.strip(" \t").partition("=")
        if name.strip(" \t").lower() != "q":
            continue
        qvalue = raw.strip(" \t")
        weight = round(float(qvalue) * 1000) if _QVALUE.fullmatch(qvalue) else None
    return token.strip(" \t").lower(), weight


def normalize_weighted_list(value: str) -> str:
    """Semantics-preserving normalization for `Accept-Encoding` and `Accept-Language`.

    Removes OWS and empty elements, lowercases case-insensitive tokens and the `q` name, and
    spells weight 1 as the default (no `q`).
    Member order is kept, because RFC 9110 Section 12.5.4 warns that some recipients rank
    equal-weight members by order, so reordering is not known to be semantics-preserving.
    """
    out: list[str] = []
    for member in list_members(value):
        token, weight = parse_weighted(member)
        if weight is None:
            out.append(member.replace(" ", "").replace("\t", "").lower())
        elif weight == 1000:
            out.append(token)
        else:
            out.append(f"{token};q={weight / 1000:g}")
    return ", ".join(out)


def select_content_coding(value: str | None, supported: Sequence[str]) -> str:
    """The content coding an origin offering `supported` (in preference order) would send.

    Applies RFC 9110 Section 12.5.3: a qvalue of 0 excludes a coding, `*` covers codings not
    listed, and the highest non-zero qvalue wins, ties going to the earlier `supported` entry.
    `identity` is the fallback when no supported coding qualifies; `identity;q=0` and `*;q=0`,
    which also make `identity` unacceptable, are not modeled.
    An absent field allows any coding; this origin policy answers it with `identity`.
    """
    if value is None:
        return "identity"
    weights: dict[str, int] = {}
    star: int | None = None
    for member in list_members(value):
        token, weight = parse_weighted(member)
        if weight is None:
            continue
        if token == WILDCARD:
            star = weight
        else:
            weights[token] = weight
    best, best_weight = "identity", 0
    for coding in supported:
        weight = weights.get(coding, star if star is not None else 0)
        if weight > best_weight:
            best, best_weight = coding, weight
    return best


def lookup_fallbacks(language_range: str) -> list[str]:
    """The progressive truncation of RFC 4647 Section 3.4, most specific first.

    Single-letter or digit subtags are removed together with their closest trailing subtag.
    """
    subtags = language_range.split("-")
    fallbacks: list[str] = []
    while subtags:
        fallbacks.append("-".join(subtags))
        subtags.pop()
        if subtags and len(subtags[-1]) == 1:
            subtags.pop()
    return fallbacks


def lookup_language(value: str | None, supported: Sequence[str], default: str) -> str:
    """RFC 4647 Section 3.4 Lookup over an `Accept-Language` value; returns a supported tag.

    Ranges are tried in descending weight, ties in field order; weight 0 means "not acceptable"
    (RFC 9110 Section 12.4.2) and `*` is skipped.
    """
    if value is None:
        return default
    by_lower = {tag.lower(): tag for tag in supported}
    ranked = [
        (token, weight)
        for token, weight in map(parse_weighted, list_members(value))
        if weight is not None and weight > 0 and token != WILDCARD
    ]
    for language_range, _ in sorted(ranked, key=lambda item: -item[1]):
        for candidate in lookup_fallbacks(language_range):
            if candidate in by_lower:
                return by_lower[candidate]
    return default


@dataclass(frozen=True)
class StoredResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    vary: VarySpec
    has_vary: bool
    nominated: Nominated
    stored_at: int  # caller's clock (a request tick), standing in for the Date header field


@dataclass
class VaryCache:
    """Responses indexed by URI, then by Vary spec, then by normalized nominated values.

    `normalizers` apply to the cache key only, so they must be semantics-preserving
    (RFC 9111 Section 4.1, third bullet).
    Lookup cost is one dictionary probe per distinct Vary spec stored for the URI, not one
    comparison per stored variant.
    """

    normalizers: Mapping[str, Normalizer] = field(default_factory=dict[str, Normalizer])
    _store: dict[str, dict[VarySpec, dict[Nominated, StoredResponse]]] = field(
        default_factory=dict[str, dict[VarySpec, dict[Nominated, StoredResponse]]]
    )
    _clock: int = 0

    def nominated_values(self, request_headers: HeaderLines, vary: tuple[str, ...]) -> Nominated:
        values: list[tuple[str, str | None]] = []
        for name in vary:
            value = combined_field_value(request_headers, name)
            normalizer = self.normalizers.get(name)
            if value is not None and normalizer is not None:
                value = normalizer(value)
            values.append((name, value))  # None (absent) only ever equals None
        return tuple(values)

    def store(
        self,
        uri: str,
        request_headers: HeaderLines,
        status: int,
        response_headers: HeaderLines,
        body: bytes,
        *,
        now: int | None = None,
    ) -> StoredResponse:
        vary_lines = field_lines(response_headers, "vary")
        vary = parse_vary(vary_lines)
        nominated: Nominated = ()
        if vary != WILDCARD:
            nominated = self.nominated_values(request_headers, vary)
        self._clock = self._clock + 1 if now is None else now
        stored = StoredResponse(
            status=status,
            headers=tuple(response_headers),
            body=body,
            vary=vary,
            has_vary=bool(vary_lines),
            nominated=nominated,
            stored_at=self._clock,
        )
        # A newer response for the same secondary key replaces the older one.
        self._store.setdefault(uri, {}).setdefault(vary, {})[nominated] = stored
        return stored

    def lookup(self, uri: str, request_headers: HeaderLines) -> StoredResponse | None:
        matches: list[StoredResponse] = []
        for spec, variants in self._store.get(uri, {}).items():
            if spec == WILDCARD:
                continue  # "*" always fails to match (RFC 9111 Section 4.1)
            hit = variants.get(self.nominated_values(request_headers, spec))
            if hit is not None:
                matches.append(hit)
        if not matches:
            return None
        # Prefer the most recent match that carried a Vary field (RFC 9111 Section 4.1).
        with_vary = [stored for stored in matches if stored.has_vary]
        return max(with_vary or matches, key=lambda stored: stored.stored_at)

    def variant_count(self, uri: str) -> int:
        return sum(len(variants) for variants in self._store.get(uri, {}).values())


@dataclass(frozen=True)
class SimulationResult:
    requests: int
    hits: int
    variants: int

    @property
    def hit_ratio(self) -> float:
        return self.hits / self.requests


def simulate(
    workload: Sequence[HeaderLines],
    vary: str,
    *,
    lifetime: int | None = None,
    normalizers: Mapping[str, Normalizer] | None = None,
    canonicalizers: Mapping[str, Canonicalizer] | None = None,
    uri: str = "/catalog",
) -> SimulationResult:
    """Replay `workload` against one URI whose origin always answers with `Vary: <vary>`.

    Request `i` arrives at tick `i`; a stored response is fresh for `lifetime` ticks, so
    `lifetime` is the number of requests that arrive within one freshness lifetime.
    With `lifetime=None` nothing expires and every distinct secondary key misses exactly once:
    `hits == requests - variants`.
    """
    cache = VaryCache(normalizers=dict(normalizers or {}))
    hits = 0
    for tick, headers in enumerate(workload):
        request = rewrite_request(headers, canonicalizers or {})
        stored = cache.lookup(uri, request)
        if stored is not None and (lifetime is None or tick - stored.stored_at < lifetime):
            hits += 1
        else:
            cache.store(uri, request, 200, [("vary", vary)], b"{}", now=tick)
    return SimulationResult(len(workload), hits, cache.variant_count(uri))


def rewrite_request(
    headers: HeaderLines, canonicalizers: Mapping[str, Canonicalizer]
) -> HeaderLines:
    """Replace each canonicalized field with one line holding its canonical value."""
    if not canonicalizers:
        return headers
    kept = [(name, value) for name, value in headers if name.lower() not in canonicalizers]
    for name, canonicalize in canonicalizers.items():
        kept.append((name, canonicalize(combined_field_value(headers, name))))
    return kept


type Upstream = Callable[[str, HeaderLines], Awaitable[tuple[int, HeaderLines, bytes]]]


@dataclass
class EdgeCache:
    """A shared cache in front of an async upstream.

    `forward_canonical=True` sends the canonicalized request upstream, so the origin selects the
    variant the cache key describes; `False` keys on canonical values but forwards the original
    request, which is the bug this lab demonstrates.
    """

    upstream: Upstream
    canonicalizers: Mapping[str, Canonicalizer] = field(default_factory=dict[str, Canonicalizer])
    forward_canonical: bool = True
    cache: VaryCache = field(default_factory=VaryCache)
    hits: int = 0
    misses: int = 0

    async def get(self, uri: str, headers: HeaderLines) -> StoredResponse:
        keyed = rewrite_request(headers, self.canonicalizers)
        stored = self.cache.lookup(uri, keyed)
        if stored is not None:
            self.hits += 1
            return stored
        self.misses += 1
        forwarded = keyed if self.forward_canonical else headers
        status, response_headers, body = await self.upstream(uri, forwarded)
        return self.cache.store(uri, keyed, status, response_headers, body)


_BROWSERS: Final = (
    "Chrome/{v}.0.{b}.{p} Safari/537.36",
    "Firefox/{v}.0",
    "Version/{v}.{p} Safari/605.1.15",
    "Chrome/{v}.0.{b}.{p} Safari/537.36 Edg/{v}.0.{b}.{p}",
)
_PLATFORMS: Final = (
    ("Windows NT 10.0; Win64; x64", "?0"),
    ("Macintosh; Intel Mac OS X 10_15_7", "?0"),
    ("X11; Linux x86_64", "?0"),
    ("Linux; Android 14; Pixel 8", "?1"),
    ("Linux; Android 13; SM-S911B", "?1"),
    ("iPhone; CPU iPhone OS 18_1 like Mac OS X", "?1"),
    ("iPad; CPU OS 18_1 like Mac OS X", "?1"),
    ("Windows NT 10.0; WOW64", "?0"),
)


def user_agent_workload(distinct: int, requests: int, seed: int) -> list[HeaderLines]:
    """`requests` requests drawn Zipf-like (weight 1/rank) from `distinct` synthetic UA strings.

    Each request also carries the matching `Sec-CH-UA-Mobile` low-entropy client hint.
    """
    population: list[tuple[str, str]] = []
    index = 0
    while len(population) < distinct:
        platform, mobile = _PLATFORMS[index % len(_PLATFORMS)]
        browser = _BROWSERS[(index // len(_PLATFORMS)) % len(_BROWSERS)]
        step = index // (len(_PLATFORMS) * len(_BROWSERS))
        product = browser.format(v=100 + step % 40, b=6000 + step // 40, p=step % 7)
        population.append((f"Mozilla/5.0 ({platform}) {product}", mobile))
        index += 1
    rng = random.Random(seed)  # noqa: S311 - deterministic synthetic workload, not security
    weights = [1.0 / rank for rank in range(1, distinct + 1)]
    picks = rng.choices(population, weights=weights, k=requests)
    return [[("user-agent", ua), ("sec-ch-ua-mobile", mobile)] for ua, mobile in picks]


_PRIMARY_LANGUAGES: Final = ("en-US", "en-GB", "fr-FR", "de-DE", "es-ES", "fr-CA", "de-AT", "it-IT")
_SECONDARY_LANGUAGES: Final = ("en", "fr", "de", "es", "it", "nl", "pt", "ja")


def accept_language_workload(requests: int, seed: int) -> list[HeaderLines]:
    """`Accept-Language` values with browser-like shapes plus case, spacing, and `q` noise."""
    rng = random.Random(seed)  # noqa: S311 - deterministic synthetic workload, not security
    workload: list[HeaderLines] = []
    for _ in range(requests):
        primary = rng.choice(_PRIMARY_LANGUAGES)
        members = [primary, f"{primary.split('-')[0]};q=0.9"]
        for rank, extra in enumerate(rng.sample(_SECONDARY_LANGUAGES, rng.randrange(0, 3))):
            members.append(f"{extra};q=0.{8 - rank}")
        if rng.random() < 0.3:
            members[0] = members[0].lower()
        if rng.random() < 0.2:
            members[0] += ";q=1.0"
        separator = rng.choice((",", ", "))
        workload.append([("accept-language", separator.join(members))])
    return workload
