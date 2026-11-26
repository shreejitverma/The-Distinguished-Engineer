"""Lab part 1: trigram search over a synthetic corpus, checked against brute force."""

import hashlib
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from corpus import make_corpus  # noqa: E402
from regex_query import Unsupported, eval_query, parse_regex, show  # noqa: E402
from trigram_index import (  # noqa: E402
    DocIndex,
    PositionalIndex,
    brute_substring,
    pack_ngram,
    substring_search,
)

NGRAM_BUCKET = 512  # Zoekt index/ngramindex.go: ngramBucketSize


def main():
    docs = make_corpus()
    pos = PositionalIndex(docs)
    doc = DocIndex(docs)
    n_docs = len(docs)

    print("== corpus")
    print(f"documents: {n_docs}")
    print(f"corpus bytes: {pos.corpus_bytes()}")
    print(f"distinct trigrams: {len(pos.post)}")
    print(f"packed ngram 'par' = {pack_ngram('par')} (112 << 42 | 97 << 21 | 114)")
    heads = max((len(pos.post) + NGRAM_BUCKET - 1) // NGRAM_BUCKET - 1, 0)
    print(f"ngram buckets of {NGRAM_BUCKET}: {heads} bucket heads would stay in memory")

    print()
    print("== substring queries, case sensitive")
    print(f"{'query':<24}{'hits':>6}{'docs':>6}{'cox':>6}{'cand':>6}  {'pair (dist)':<18}{'read':>6}")
    queries = ["zxqv_checkpoint_flush", "parse_header(", "for item in", "return", "ab"]
    for q in queries:
        hits, st = substring_search(pos, q)
        assert hits == brute_substring(pos, q), q
        match_docs = len({d for d, _ in hits})
        cox = doc.candidates(q)
        # cox is a superset of the docs that match, by construction.
        assert {d for d, _ in hits} <= set(cox), q
        if st.brute_fallback:
            pair = "scan (<3 chars)"
        else:
            pair = f"{st.pair[0]!r}+{st.pair[1]!r} ({st.dist})" if st.pair[0] != st.pair[1] else f"{st.pair[0]!r} (0)"
        cand = "-" if st.brute_fallback else str(st.candidates)
        print(f"{q:<24}{len(hits):>6}{match_docs:>6}{len(cox):>6}{cand:>6}  {pair:<18}{st.postings_read:>6}")
    print("hits = occurrences; docs = documents with a hit; cox = documents holding every trigram of the query")
    print("cand = pair-and-distance candidate occurrences before the final string compare; read = posting entries read")

    print()
    print("== case-insensitive")
    q = "ZXQV_CheckPoint_FLUSH"
    hits_cs, _ = substring_search(pos, q)
    hits_ci, st = substring_search(pos, q, ci=True)
    assert hits_ci == brute_substring(pos, q, ci=True)
    print(f"query {q!r}: case sensitive hits {len(hits_cs)}, case insensitive hits {len(hits_ci)}")
    print(f"trigram lookups for {len(q)} chars: {st.lookups} (case sensitive would be {len(q) - 2})")

    print()
    print("== regex to query")
    regexes = [
        r"(Path|PathFragment).*=.*/usr/local",
        r"zxqv_checkpoint_(flush|drain)",
        r"a.*b",
    ]
    all_ids = range(n_docs)
    cache = {}

    def lit_docs(s):
        if s not in cache:
            hits, _ = substring_search(pos, s)
            cache[s] = {d for d, _ in hits}
        return cache[s]

    for rx in regexes:
        q = parse_regex(rx)
        cand = eval_query(q, all_ids, lit_docs)
        compiled = re.compile(rx)
        matched = {d for d in cand if compiled.search(docs[d][1])}
        truth = {d for d in all_ids if compiled.search(docs[d][1])}
        assert matched == truth, rx
        print(f"{rx}")
        print(f"  query {show(q)}")
        print(f"  candidates {len(cand)} of {n_docs}, matching documents {len(matched)}, brute force agrees")
    for rx in [r"a+b", r"[Pp]ath"]:
        try:
            parse_regex(rx)
        except Unsupported as e:
            print(f"{rx}: unsupported ({e}), a real engine falls back to ANY and scans")

    print()
    print("== index size")
    cb = pos.corpus_bytes()
    print(f"positional: {pos.entries()} entries, {pos.index_bytes()} varint bytes, {pos.index_bytes() / cb:.2f}x corpus")
    print(f"document-level: {doc.entries()} entries, {doc.index_bytes()} varint bytes, {doc.index_bytes() / cb:.2f}x corpus")

    print()
    print("== differential check against brute force")
    rng = random.Random(7)
    n_cs = n_ci = 0
    for _ in range(200):
        _, content = docs[rng.randrange(n_docs)]
        length = rng.randint(3, 12)
        start = rng.randrange(max(len(content) - length, 1))
        needle = content[start : start + length]
        if "\n" in needle:
            needle = needle.split("\n")[0]
        if len(needle) < 3:
            continue
        for ci in (False, True):
            hits, _ = substring_search(pos, needle, ci=ci)
            assert hits == brute_substring(pos, needle, ci=ci), (needle, ci)
            if ci:
                n_ci += 1
            else:
                n_cs += 1
    print(f"{n_cs} case-sensitive and {n_ci} case-insensitive queries equal brute force")
    # A needle that never occurs must return nothing, whatever the postings say.
    hits, _ = substring_search(pos, "qqqq_not_in_corpus")
    assert hits == []
    print("absent needle returns no hits")

    print()
    print("== shard by blob hash")
    blobs = {}
    for path, content in docs:
        blobs.setdefault(hashlib.sha1(content.encode("ascii")).hexdigest(), []).append(path)
    shards = [0, 0, 0, 0]
    for h in blobs:
        shards[int(h[:8], 16) % 4] += 1
    print(f"files {n_docs}, unique blobs {len(blobs)}, duplicate files {n_docs - len(blobs)}")
    print(f"unique blobs per shard: {shards}")
    # Same content, same blob id, same shard: duplicates collapse without any coordination.
    assert sum(shards) == len(blobs)
    assert len(blobs) == n_docs - 30, "the corpus plants 30 byte-identical copies"


if __name__ == "__main__":
    main()
