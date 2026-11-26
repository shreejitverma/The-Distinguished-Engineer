"""Two trigram indexes over one corpus, and the search algorithms on top of them.

PositionalIndex follows Zoekt's design (doc/design.md): every trigram maps to the sorted
offsets where it occurs, offsets live in one concatenated content space, and a substring
query reads only two posting lists, the two rarest trigrams of the query, and checks that
they sit the right distance apart.

DocIndex follows Russ Cox's article: every trigram maps to the sorted ids of the documents
that contain it, and a query ANDs the lists of all its trigrams.

Both stay ASCII-only on purpose; Zoekt indexes runes and keeps a rune-to-byte table, which
this lab does not need.
"""

import bisect
import itertools

NGRAM = 3
INF = 1 << 60


def varint_len(n):
    """Bytes a LEB128 unsigned varint needs for n."""
    size = 1
    while n >= 0x80:
        n >>= 7
        size += 1
    return size


def delta_varint_bytes(sorted_ints):
    prev, total = 0, 0
    for x in sorted_ints:
        total += varint_len(x - prev)
        prev = x
    return total


def pack_ngram(s):
    """Zoekt's runesToNGram: 21 bits per rune in a uint64 (index/bits.go)."""
    return (ord(s[0]) << 42) | (ord(s[1]) << 21) | ord(s[2])


def case_variants(gram):
    options = [sorted({c.lower(), c.upper()}) for c in gram]
    return ["".join(p) for p in itertools.product(*options)]


class PositionalIndex:
    def __init__(self, docs):
        self.docs = docs
        self.starts, self.ends = [], []
        parts, base = [], 0
        for _, content in docs:
            self.starts.append(base)
            parts.append(content)
            base += len(content)
            self.ends.append(base)
        self.blob = "".join(parts)
        self.post = {}
        for d, (_, content) in enumerate(docs):
            b = self.starts[d]
            for i in range(len(content) - NGRAM + 1):
                self.post.setdefault(content[i : i + NGRAM], []).append(b + i)

    def doc_of(self, offset):
        return bisect.bisect_right(self.starts, offset) - 1

    def positions(self, gram, ci):
        if not ci:
            return self.post.get(gram, [])
        merged = []
        for v in case_variants(gram.lower()):
            merged.extend(self.post.get(v, []))
        return sorted(merged)

    def freq(self, gram, ci):
        if not ci:
            return len(self.post.get(gram, []))
        return sum(len(self.post.get(v, [])) for v in case_variants(gram.lower()))

    def index_bytes(self):
        return sum(delta_varint_bytes(p) for p in self.post.values())

    def entries(self):
        return sum(len(p) for p in self.post.values())

    def corpus_bytes(self):
        return len(self.blob)


class DocIndex:
    def __init__(self, docs):
        self.n_docs = len(docs)
        self.post = {}
        for d, (_, content) in enumerate(docs):
            for g in {content[i : i + NGRAM] for i in range(len(content) - NGRAM + 1)}:
                self.post.setdefault(g, []).append(d)

    def index_bytes(self):
        return sum(delta_varint_bytes(p) for p in self.post.values())

    def entries(self):
        return sum(len(p) for p in self.post.values())

    def candidates(self, needle):
        """Docs that hold every trigram of needle, in any order, anywhere."""
        if len(needle) < NGRAM:
            return list(range(self.n_docs))
        lists = [self.post.get(needle[i : i + NGRAM], []) for i in range(len(needle) - NGRAM + 1)]
        result = set(lists[0])
        for lst in lists[1:]:
            result &= set(lst)
        return sorted(result)


def pick_pair(needle, freq):
    """Port of Zoekt findSelectiveNgrams (index/indexdata.go).

    Take the two lowest-frequency trigrams, ties resolved in sorted-trigram order, put the
    earlier one first, and if they overlap, shift them to the nearest non-overlapping pair.
    Returns ((gram, index), (gram, index)); equal when the needle is a single trigram.
    """
    offs = sorted((needle[i : i + NGRAM], i) for i in range(len(needle) - NGRAM + 1))
    index_map = [0] * len(offs)
    for pos, (_, i) in enumerate(offs):
        index_map[i] = pos
    freqs = [freq(g) for g, _ in offs]
    idx0 = idx1 = 0
    min0 = min1 = INF
    for i, x in enumerate(freqs):
        if x <= min0:
            idx0, idx1 = i, idx0
            min0, min1 = x, min0
        elif x <= min1:
            idx1 = i
            min1 = x
    first, last = offs[idx0], offs[idx1]
    if first[1] > last[1]:
        first, last = last, first
    if last[1] - first[1] < NGRAM:
        new_first = max(last[1] - NGRAM, 0)
        if new_first != first[1]:
            first = offs[index_map[new_first]]
        new_last = min(first[1] + NGRAM, len(offs) - 1)
        if new_last != last[1]:
            last = offs[index_map[new_last]]
    return first, last


class SearchStats:
    def __init__(self):
        self.pair = None
        self.dist = 0
        self.candidates = 0
        self.postings_read = 0
        self.lookups = 0
        self.brute_fallback = False


def brute_substring(idx, needle, ci=False):
    """Reference answer: every (doc, start) where needle occurs, overlaps included."""
    hits = []
    for d, (_, content) in enumerate(idx.docs):
        hay = content.lower() if ci else content
        pat = needle.lower() if ci else needle
        start = hay.find(pat)
        while start != -1:
            hits.append((d, start))
            start = hay.find(pat, start + 1)
    return hits


def substring_search(idx, needle, ci=False):
    """Zoekt-style search. Returns (sorted (doc, start-in-doc) hits, SearchStats)."""
    st = SearchStats()
    if len(needle) < NGRAM:
        # Zoekt sends patterns under three runes to a regexp match tree, which scans documents.
        st.brute_fallback = True
        st.candidates = len(idx.docs)
        return brute_substring(idx, needle, ci), st

    key = needle.lower() if ci else needle
    first, last = pick_pair(key, lambda g: idx.freq(g, ci))
    st.pair = (first[0], last[0])
    st.dist = last[1] - first[1]
    # Zoekt looks up every trigram of the pattern once to estimate frequencies (index/indexdata.go,
    # iterateNgrams), and once per case variant when the search is case-insensitive.
    for i in range(len(key) - NGRAM + 1):
        st.lookups += len(case_variants(key[i : i + NGRAM])) if ci else 1
    pf = idx.positions(first[0], ci)
    st.postings_read = len(pf)
    if last == first:
        starts = [p - first[1] for p in pf]
    else:
        pl = idx.positions(last[0], ci)
        st.postings_read += len(pl)
        lastset = set(pl)
        starts = [
            p - first[1]
            for p in pf
            if p + st.dist in lastset and idx.doc_of(p) == idx.doc_of(p + st.dist)
        ]
    st.candidates = len(starts)

    hits = []
    n = len(key)
    for s in starts:
        if s < 0:
            continue
        d = idx.doc_of(s)
        if s + n > idx.ends[d]:
            continue
        seg = idx.blob[s : s + n]
        if (seg.lower() if ci else seg) == key:
            hits.append((d, s - idx.starts[d]))
    return sorted(hits), st
