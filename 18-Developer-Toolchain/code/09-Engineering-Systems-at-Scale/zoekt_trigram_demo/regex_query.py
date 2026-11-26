"""Turn a small regex subset into a boolean query over literal substrings.

Supported: literal characters, backslash escapes, '.*' and '.' as gaps, and groups of literal
alternatives such as (flush|drain). Anything else raises Unsupported, because a real
extractor must either handle an operator or give up and fall back to ANY.

The shape follows Cox's article ("exact" sets crossed through concatenation, ORs for
alternation) and Zoekt's example in doc/design.md, where
(Path|PathFragment).*=.*/usr/local becomes AND(OR(Path, PathFragment), /usr/local).
Literals shorter than three characters carry no trigram and are dropped.
"""

import re

from trigram_index import NGRAM

MAX_EXACT = 16


class Unsupported(Exception):
    pass


def parse_regex(pattern):
    """Return a query tree: ('lit', s) | ('or', [...]) | ('and', [...]) | ('any',)."""
    atoms = []
    exact = [""]  # the exact strings the current literal run can be
    i = 0

    def flush():
        nonlocal exact
        usable = [s for s in exact if len(s) >= NGRAM]
        if len(usable) == len(exact):
            atoms.append(("or", [("lit", s) for s in exact]) if len(exact) > 1 else ("lit", exact[0]))
        # A run containing a short alternative cannot be indexed: it contributes ANY, i.e. nothing.
        exact = [""]

    while i < len(pattern):
        ch = pattern[i]
        if ch == ".":
            flush()
            i += 2 if pattern[i : i + 2] == ".*" else 1
        elif ch == "\\":
            if i + 1 >= len(pattern):
                raise Unsupported("dangling backslash")
            exact = [s + pattern[i + 1] for s in exact]
            i += 2
        elif ch == "(":
            j = pattern.find(")", i)
            if j == -1:
                raise Unsupported("unclosed group")
            alts = pattern[i + 1 : j].split("|")
            if any(re.search(r"[\\.*+?()\[\]{}^$|]", a) for a in alts):
                raise Unsupported("group with operators")
            if len(exact) * len(alts) > MAX_EXACT:
                raise Unsupported("exact set too large")
            exact = [s + a for s in exact for a in alts]
            i = j + 1
        elif ch in "*+?[]{}^$|)":
            raise Unsupported(f"operator {ch!r}")
        else:
            exact = [s + ch for s in exact]
            i += 1
    flush()
    if not atoms:
        return ("any",)
    return atoms[0] if len(atoms) == 1 else ("and", atoms)


def show(q):
    kind = q[0]
    if kind == "any":
        return "ANY"
    if kind == "lit":
        return '"' + q[1] + '"'
    return kind.upper() + "(" + ", ".join(show(c) for c in q[1]) + ")"


def eval_query(q, all_docs, lit_docs):
    """lit_docs(s) returns the set of doc ids where substring s occurs."""
    kind = q[0]
    if kind == "any":
        return set(all_docs)
    if kind == "lit":
        return lit_docs(q[1])
    parts = [eval_query(c, all_docs, lit_docs) for c in q[1]]
    out = parts[0]
    for p in parts[1:]:
        out = (out & p) if kind == "and" else (out | p)
    return out
