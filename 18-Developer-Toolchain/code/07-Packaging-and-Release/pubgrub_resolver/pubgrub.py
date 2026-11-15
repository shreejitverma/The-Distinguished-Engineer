"""A from-scratch PubGrub version solver, ported from dart-lang/pub.

The algorithm follows pub's doc/solver.md and lib/src/solver/*.dart:
terms over version ranges, incompatibilities, a partial solution of
decisions and derivations, unit propagation, conflict resolution by
generalized resolution, lazy dependency loading with adjacent-version
collapsing, and the derivation-graph error reporter.

Standard library only; runs on Python 3.9+.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

Version = Tuple[int, int, int]
Bound = Optional[Version]  # None is -inf as a lower bound, +inf as an upper bound


def parse_version(text: str) -> Version:
    parts = [int(p) for p in text.split(".")]
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def fmt_version(v: Version) -> str:
    return "%d.%d.%d" % v


def _next_patch(v: Version) -> Version:
    # With three integer components nothing sorts between v and v + 0.0.1,
    # so [v, v + 0.0.1) contains exactly v.
    return (v[0], v[1], v[2] + 1)


def _caret_upper(v: Version) -> Version:
    if v[0] > 0:
        return (v[0] + 1, 0, 0)
    if v[1] > 0:
        return (0, v[1] + 1, 0)
    return (0, 0, v[2] + 1)


# ---------------------------------------------------------------------------
# Version ranges: a normalized union of disjoint half-open intervals [lo, hi).
# ---------------------------------------------------------------------------


def _lo_lt(a: Bound, b: Bound) -> bool:  # a < b, both lower bounds
    if a is None:
        return b is not None
    return b is not None and a < b


def _hi_le(a: Bound, b: Bound) -> bool:  # a <= b, both upper bounds
    if b is None:
        return True
    return a is not None and a <= b


def _nonempty(lo: Bound, hi: Bound) -> bool:
    return lo is None or hi is None or lo < hi


class Range:
    __slots__ = ("iv",)

    def __init__(self, intervals: Sequence[Tuple[Bound, Bound]] = ()) -> None:
        items = [iv for iv in intervals if _nonempty(iv[0], iv[1])]
        items.sort(key=lambda iv: (iv[0] is not None, iv[0] or (0, 0, 0)))
        merged: List[Tuple[Bound, Bound]] = []
        for lo, hi in items:
            if merged:
                plo, phi = merged[-1]
                # Overlapping or adjacent: [plo, phi) and [lo, hi) with lo <= phi.
                if phi is None or lo is None or lo <= phi:
                    if _hi_le(phi, hi):
                        merged[-1] = (plo, hi)
                    continue
            merged.append((lo, hi))
        self.iv = tuple(merged)

    @staticmethod
    def full() -> "Range":
        return Range([(None, None)])

    @staticmethod
    def exact(v: Version) -> "Range":
        return Range([(v, _next_patch(v))])

    @staticmethod
    def caret(v: Version) -> "Range":
        return Range([(v, _caret_upper(v))])

    @staticmethod
    def between(lo: Bound, hi: Bound) -> "Range":
        return Range([(lo, hi)])

    def is_empty(self) -> bool:
        return not self.iv

    def is_full(self) -> bool:
        return self.iv == ((None, None),)

    def contains(self, v: Version) -> bool:
        for lo, hi in self.iv:
            if (lo is None or lo <= v) and (hi is None or v < hi):
                return True
        return False

    def intersect(self, other: "Range") -> "Range":
        out = []
        a, b = self.iv, other.iv
        i = j = 0
        while i < len(a) and j < len(b):
            lo = b[j][0] if _lo_lt(a[i][0], b[j][0]) else a[i][0]
            hi = a[i][1] if _hi_le(a[i][1], b[j][1]) else b[j][1]
            if _nonempty(lo, hi):
                out.append((lo, hi))
            if _hi_le(a[i][1], b[j][1]):
                i += 1
            else:
                j += 1
        return Range(out)

    def complement(self) -> "Range":
        out = []
        cur: Bound = None
        first = True
        for lo, hi in self.iv:
            if lo is not None:
                if first:
                    out.append((None, lo))
                else:
                    out.append((cur, lo))
            first = False
            cur = hi
            if hi is None:
                return Range(out)
        if first:
            return Range.full()
        out.append((cur, None))
        return Range(out)

    def union(self, other: "Range") -> "Range":
        return self.complement().intersect(other.complement()).complement()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Range) and self.iv == other.iv

    def __hash__(self) -> int:
        return hash(self.iv)

    def __str__(self) -> str:
        if not self.iv:
            return "<empty>"
        parts = []
        for lo, hi in self.iv:
            if lo is None and hi is None:
                parts.append("any")
            elif lo is not None and hi == _next_patch(lo):
                parts.append(fmt_version(lo))
            elif lo is not None and hi is not None and hi == _caret_upper(lo):
                parts.append("^" + fmt_version(lo))
            elif lo is None:
                parts.append("<" + fmt_version(hi))  # type: ignore[arg-type]
            elif hi is None:
                parts.append(">=" + fmt_version(lo))
            else:
                parts.append(">=%s <%s" % (fmt_version(lo), fmt_version(hi)))
        return " or ".join(parts)


# ---------------------------------------------------------------------------
# Terms: "foo R" (positive) or "not foo R" (negative).
# A negative term is also true when no version of the package is selected.
# ---------------------------------------------------------------------------


class Term:
    __slots__ = ("pkg", "positive", "rng")

    def __init__(self, pkg: str, positive: bool, rng: Range) -> None:
        self.pkg = pkg
        self.positive = positive
        self.rng = rng

    def negate(self) -> "Term":
        return Term(self.pkg, not self.positive, self.rng)

    def intersect(self, other: "Term") -> "Term":
        a, b = self.rng, other.rng
        if self.positive and other.positive:
            return Term(self.pkg, True, a.intersect(b))
        if self.positive:
            return Term(self.pkg, True, a.intersect(b.complement()))
        if other.positive:
            return Term(self.pkg, True, b.intersect(a.complement()))
        return Term(self.pkg, False, a.union(b))

    def is_empty(self) -> bool:
        return self.positive and self.rng.is_empty()

    def satisfies(self, other: "Term") -> bool:
        """self is a subset of other: other is true whenever self is."""
        return self.intersect(other.negate()).is_empty()

    def relation(self, other: "Term") -> str:
        if self.satisfies(other):
            return "subset"
        if self.intersect(other).is_empty():
            return "disjoint"
        return "overlapping"

    def key(self) -> Tuple[str, bool, tuple]:
        return (self.pkg, self.positive, self.rng.iv)

    def __str__(self) -> str:
        return ("" if self.positive else "not ") + "%s %s" % (self.pkg, self.rng)


# ---------------------------------------------------------------------------
# Incompatibilities: a set of terms that must not all be true.
# ---------------------------------------------------------------------------


class Incompatibility:
    __slots__ = ("terms", "kind", "causes", "root")

    def __init__(self, terms: List[Term], kind: str, root: str, causes: Tuple = ()) -> None:
        # kind: "root", "dependency", "no_versions", or "conflict" (derived).
        # pub removes positive root terms from derived incompatibilities with
        # more than one term, because they are always satisfied.
        if kind == "conflict" and len(terms) != 1 and any(t.positive and t.pkg == root for t in terms):
            terms = [t for t in terms if not (t.positive and t.pkg == root)]
        by_pkg: Dict[str, List[Term]] = {}
        for t in terms:
            by_pkg.setdefault(t.pkg, []).append(t)
        merged = []
        for pkg, ts in by_pkg.items():
            acc = ts[0]
            for t in ts[1:]:
                acc = acc.intersect(t)
            merged.append(acc)
        self.terms = merged
        self.kind = kind
        self.causes = causes
        self.root = root

    def is_failure(self) -> bool:
        return not self.terms or (len(self.terms) == 1 and self.terms[0].pkg == self.root)

    def key(self) -> tuple:
        return (self.kind, tuple(t.key() for t in self.terms))

    # -- pub's human-readable phrasing (lib/src/solver/incompatibility.dart) --

    def _terse(self, t: Optional[Term], allow_every: bool = False) -> str:
        assert t is not None
        if t.pkg == self.root:
            return t.pkg
        if allow_every and t.rng.is_full():
            return "every version of " + t.pkg
        return "%s %s" % (t.pkg, t.rng)

    def _single(self, positive: bool) -> Optional[Term]:
        found = None
        for t in self.terms:
            if t.positive != positive:
                continue
            if found is not None:
                return None
            found = t
        return found

    def __str__(self) -> str:
        if self.kind == "dependency":
            return "%s depends on %s" % (self._terse(self.terms[0], True), self._terse(self.terms[1]))
        if self.kind == "no_versions":
            return "no versions of %s match %s" % (self.terms[0].pkg, self.terms[0].rng)
        if self.is_failure():
            return "version solving failed"
        if len(self.terms) == 1:
            t = self.terms[0]
            word = "forbidden" if t.positive else "required"
            if t.rng.is_full():
                return "%s is %s" % (t.pkg, word)
            return "%s is %s" % (self._terse(t), word)
        if len(self.terms) == 2 and self.terms[0].positive == self.terms[1].positive:
            t1, t2 = self.terms
            if t1.positive:
                return "%s is incompatible with %s" % (self._terse(t1), self._terse(t2))
            return "either %s or %s" % (self._terse(t1), self._terse(t2))
        pos = [t for t in self.terms if t.positive]
        neg = [t for t in self.terms if not t.positive]
        if pos and neg:
            if len(pos) == 1:
                return "%s requires %s" % (self._terse(pos[0], True), " or ".join(self._terse(t) for t in neg))
            return "if %s then %s" % (
                " and ".join(self._terse(t) for t in pos),
                " or ".join(self._terse(t) for t in neg),
            )
        if pos:
            return "one of %s must be false" % " or ".join(self._terse(t) for t in pos)
        return "one of %s must be true" % " or ".join(self._terse(t) for t in neg)

    def and_str(self, other: "Incompatibility", this_line: Optional[int] = None, other_line: Optional[int] = None) -> str:
        for attempt in (self._requires_both, self._requires_through, self._requires_forbidden):
            s = attempt(other, this_line, other_line)
            if s is not None:
                return s
        out = str(self)
        if this_line is not None:
            out += " (%d)" % this_line
        out += " and " + str(other)
        if other_line is not None:
            out += " (%d)" % other_line
        return out

    def _requires_both(self, other: "Incompatibility", this_line, other_line) -> Optional[str]:
        if len(self.terms) == 1 or len(other.terms) == 1:
            return None
        tp = self._single(True)
        op = other._single(True)
        # pub compares PackageRange values, which include the constraint, not just the name.
        if tp is None or op is None or tp.pkg != op.pkg or tp.rng != op.rng:
            return None
        tn = " or ".join(self._terse(t) for t in self.terms if not t.positive)
        on = " or ".join(other._terse(t) for t in other.terms if not t.positive)
        verb = "depends on" if self.kind == "dependency" and other.kind == "dependency" else "requires"
        out = "%s %s both %s" % (self._terse(tp, True), verb, tn)
        if this_line is not None:
            out += " (%d)" % this_line
        out += " and " + on
        if other_line is not None:
            out += " (%d)" % other_line
        return out

    def _requires_through(self, other: "Incompatibility", this_line, other_line) -> Optional[str]:
        if len(self.terms) == 1 or len(other.terms) == 1:
            return None
        tn, on = self._single(False), other._single(False)
        if tn is None and on is None:
            return None
        tp, op = self._single(True), other._single(True)
        if tn is not None and op is not None and tn.pkg == op.pkg and tn.negate().satisfies(op):
            prior, prior_neg, prior_line, latter, latter_line = self, tn, this_line, other, other_line
        elif on is not None and tp is not None and on.pkg == tp.pkg and on.negate().satisfies(tp):
            prior, prior_neg, prior_line, latter, latter_line = other, on, other_line, self, this_line
        else:
            return None
        prior_pos = [t for t in prior.terms if t.positive]
        if len(prior_pos) > 1:
            out = "if %s then " % " or ".join(prior._terse(t) for t in prior_pos)
        else:
            verb = "depends on" if prior.kind == "dependency" else "requires"
            out = "%s %s " % (prior._terse(prior_pos[0], True), verb)
        out += prior._terse(prior_neg)
        if prior_line is not None:
            out += " (%d)" % prior_line
        out += " which " + ("depends on " if latter.kind == "dependency" else "requires ")
        out += " or ".join(latter._terse(t) for t in latter.terms if not t.positive)
        if latter_line is not None:
            out += " (%d)" % latter_line
        return out

    def _requires_forbidden(self, other: "Incompatibility", this_line, other_line) -> Optional[str]:
        if len(self.terms) != 1 and len(other.terms) != 1:
            return None
        if len(self.terms) == 1:
            prior, latter, prior_line, latter_line = other, self, other_line, this_line
        else:
            prior, latter, prior_line, latter_line = self, other, this_line, other_line
        neg = prior._single(False)
        if neg is None or not latter.terms[0].positive or not neg.negate().satisfies(latter.terms[0]):
            return None
        positives = [t for t in prior.terms if t.positive]
        if not positives:
            return None
        if len(positives) > 1:
            out = "if %s then " % " or ".join(prior._terse(t) for t in positives)
        else:
            out = prior._terse(positives[0], True)
            out += " depends on " if prior.kind == "dependency" else " requires "
        out += prior._terse(latter.terms[0]) + " "
        if prior_line is not None:
            out += "(%d) " % prior_line
        out += "which doesn't match any versions" if latter.kind == "no_versions" else "which is forbidden"
        if latter_line is not None:
            out += " (%d)" % latter_line
        return out


# ---------------------------------------------------------------------------
# Partial solution
# ---------------------------------------------------------------------------


class Assignment:
    __slots__ = ("term", "level", "cause", "index", "version")

    def __init__(self, term: Term, level: int, cause: Optional[Incompatibility], index: int, version: Optional[Version]) -> None:
        self.term = term
        self.level = level
        self.cause = cause  # None for a decision
        self.index = index
        self.version = version  # set for a decision

    def is_decision(self) -> bool:
        return self.cause is None


class PartialSolution:
    def __init__(self) -> None:
        self.assignments: List[Assignment] = []
        self.decisions: Dict[str, Version] = {}
        self._acc: Dict[str, Term] = {}

    @property
    def level(self) -> int:
        # pub: decisionLevel => _decisions.length, so the root decision is level 1.
        return len(self.decisions)

    def _register(self, a: Assignment) -> None:
        old = self._acc.get(a.term.pkg)
        self._acc[a.term.pkg] = a.term if old is None else old.intersect(a.term)

    def decide(self, pkg: str, v: Version) -> None:
        self.decisions[pkg] = v
        a = Assignment(Term(pkg, True, Range.exact(v)), self.level, None, len(self.assignments), v)
        self.assignments.append(a)
        self._register(a)

    def derive(self, term: Term, cause: Incompatibility) -> None:
        a = Assignment(term, self.level, cause, len(self.assignments), None)
        self.assignments.append(a)
        self._register(a)

    def backtrack(self, level: int) -> None:
        touched = set()
        while self.assignments and self.assignments[-1].level > level:
            a = self.assignments.pop()
            touched.add(a.term.pkg)
            if a.is_decision():
                del self.decisions[a.term.pkg]
        for pkg in touched:
            self._acc.pop(pkg, None)
        for a in self.assignments:
            if a.term.pkg in touched:
                self._register(a)

    def relation(self, term: Term) -> str:
        acc = self._acc.get(term.pkg)
        if acc is None:
            return "overlapping"
        return acc.relation(term)

    def satisfies(self, term: Term) -> bool:
        return self.relation(term) == "subset"

    def satisfier(self, term: Term) -> Assignment:
        acc: Optional[Term] = None
        for a in self.assignments:
            if a.term.pkg != term.pkg:
                continue
            acc = a.term if acc is None else acc.intersect(a.term)
            if acc.satisfies(term):
                return a
        raise AssertionError("BUG: %s is not satisfied" % term)

    def unsatisfied(self) -> List[str]:
        return [p for p, t in self._acc.items() if t.positive and p not in self.decisions]

    def term_for(self, pkg: str) -> Term:
        return self._acc[pkg]


# ---------------------------------------------------------------------------
# The solver
# ---------------------------------------------------------------------------

# Index: package -> {version string -> {dependency -> range}}
Index = Dict[str, Dict[str, Dict[str, Range]]]


class SolveFailure(Exception):
    def __init__(self, incompatibility: Incompatibility) -> None:
        super().__init__("version solving failed")
        self.incompatibility = incompatibility


class Solver:
    def __init__(self, index: Index, root: str) -> None:
        self.root = root
        self.versions: Dict[str, List[Version]] = {p: sorted(parse_version(v) for v in vs) for p, vs in index.items()}
        self.deps: Dict[Tuple[str, Version], Dict[str, Range]] = {
            (p, parse_version(v)): d for p, vs in index.items() for v, d in vs.items()
        }
        self.sol = PartialSolution()
        self.incompat: Dict[str, List[Incompatibility]] = {}
        self._seen: set = set()
        self.learned = 0
        self.learned_list: List[str] = []
        self.conflicts = 0
        self.decisions_made = 0
        self.dep_lookups = 0  # how many package versions had their dependencies loaded

    # -- incompatibility store --

    def _add(self, inc: Incompatibility) -> None:
        k = inc.key()
        if k in self._seen:
            return
        self._seen.add(k)
        for t in inc.terms:
            self.incompat.setdefault(t.pkg, []).append(inc)

    def _dependency_incompats(self, pkg: str, v: Version) -> List[Incompatibility]:
        """pub's PackageLister: collapse identical deps of adjacent versions."""
        self.dep_lookups += 1
        out = []
        vs = self.versions[pkg]
        k = vs.index(v)
        for dep, rng in self.deps[(pkg, v)].items():
            if pkg == self.root:
                depender = Range.exact(v)
            else:
                lo = k
                while lo > 0 and self.deps[(pkg, vs[lo - 1])].get(dep) == rng:
                    lo -= 1
                hi = k
                while hi + 1 < len(vs) and self.deps[(pkg, vs[hi + 1])].get(dep) == rng:
                    hi += 1
                lower = None if lo == 0 else vs[lo]
                upper = None if hi == len(vs) - 1 else vs[hi + 1]
                depender = Range.between(lower, upper)
            out.append(Incompatibility([Term(pkg, True, depender), Term(dep, False, rng)], "dependency", self.root))
        return out

    # -- main loop --

    def solve(self, root_version: str = "1.0.0") -> Dict[str, Version]:
        rv = parse_version(root_version)
        self._add(Incompatibility([Term(self.root, False, Range.exact(rv))], "root", self.root))
        nxt: Optional[str] = self.root
        while nxt is not None:
            self._propagate(nxt)
            nxt = self._choose()
        return dict(self.sol.decisions)

    def _propagate(self, pkg: str) -> None:
        changed = [pkg]
        while changed:
            p = changed.pop(0)
            for inc in reversed(list(self.incompat.get(p, []))):
                res = self._propagate_one(inc)
                if res == "conflict":
                    root_cause = self._resolve_conflict(inc)
                    nxt = self._propagate_one(root_cause)
                    assert isinstance(nxt, str) and nxt != "conflict"
                    changed = [nxt]
                    break
                if isinstance(res, str) and res not in changed:
                    changed.append(res)

    def _propagate_one(self, inc: Incompatibility):
        unsatisfied = None
        for t in inc.terms:
            rel = self.sol.relation(t)
            if rel == "disjoint":
                return None
            if rel == "overlapping":
                if unsatisfied is not None:
                    return None
                unsatisfied = t
        if unsatisfied is None:
            return "conflict"
        self.sol.derive(unsatisfied.negate(), inc)
        return unsatisfied.pkg

    def _resolve_conflict(self, inc: Incompatibility) -> Incompatibility:
        self.conflicts += 1
        new_inc = False
        while not inc.is_failure():
            recent_term: Optional[Term] = None
            recent: Optional[Assignment] = None
            difference: Optional[Term] = None
            prev_level = 1
            for t in inc.terms:
                s = self.sol.satisfier(t)
                if recent is None:
                    recent_term, recent = t, s
                elif recent.index < s.index:
                    prev_level = max(prev_level, recent.level)
                    recent_term, recent, difference = t, s, None
                else:
                    prev_level = max(prev_level, s.level)
                if recent_term is t:
                    d = recent.term.intersect(t.negate())
                    difference = None if d.is_empty() else d
                    if difference is not None:
                        prev_level = max(prev_level, self.sol.satisfier(difference.negate()).level)
            assert recent is not None and recent_term is not None
            if prev_level < recent.level or recent.cause is None:
                self.sol.backtrack(prev_level)
                if new_inc:
                    self._add(inc)
                    self.learned += 1
                    self.learned_list.append(str(inc))
                return inc
            cause = recent.cause
            terms = [t for t in inc.terms if t is not recent_term]
            terms += [t for t in cause.terms if t.pkg != recent.term.pkg]
            if difference is not None:
                terms.append(difference.negate())
            inc = Incompatibility(terms, "conflict", self.root, (inc, cause))
            new_inc = True
        raise SolveFailure(inc)

    def _choose(self) -> Optional[str]:
        unsat = self.sol.unsatisfied()
        if not unsat:
            return None

        def count(p: str) -> int:
            r = self.sol.term_for(p).rng
            return sum(1 for v in self.versions.get(p, []) if r.contains(v))

        # pub: the package with the fewest versions matching its constraint.
        pkg = min(unsat, key=count)
        term = self.sol.term_for(pkg)
        best = None
        for v in reversed(self.versions.get(pkg, [])):
            if term.rng.contains(v):
                best = v
                break
        if best is None:
            self._add(Incompatibility([Term(pkg, True, term.rng)], "no_versions", self.root))
            return pkg
        conflict = False
        for inc in self._dependency_incompats(pkg, best):
            self._add(inc)
            conflict = conflict or all(t.pkg == pkg or self.sol.satisfies(t) for t in inc.terms)
        if not conflict:
            self.sol.decide(pkg, best)
            self.decisions_made += 1
        return pkg


# ---------------------------------------------------------------------------
# Failure report (pub lib/src/solver/failure.dart, _Writer)
# ---------------------------------------------------------------------------


def explain(root_inc: Incompatibility) -> List[str]:
    derivations: Dict[int, int] = {}
    lines: List[Tuple[str, Optional[int]]] = []
    numbers: Dict[int, int] = {}

    def count(inc: Incompatibility) -> None:
        if id(inc) in derivations:
            derivations[id(inc)] += 1
            return
        derivations[id(inc)] = 1
        if inc.kind == "conflict":
            count(inc.causes[0])
            count(inc.causes[1])

    def write(inc: Incompatibility, msg: str, numbered: bool) -> None:
        if numbered:
            n = len(numbers) + 1
            numbers[id(inc)] = n
            lines.append((msg, n))
        else:
            lines.append((msg, None))

    def derived(inc: Incompatibility) -> bool:
        return inc.kind == "conflict"

    def single_line(inc: Incompatibility) -> bool:
        return not derived(inc.causes[0]) and not derived(inc.causes[1])

    def collapsible(inc: Incompatibility) -> bool:
        if derivations[id(inc)] > 1:
            return False
        c1, c2 = inc.causes
        if derived(c1) and derived(c2):
            return False
        if not derived(c1) and not derived(c2):
            return False
        complex_ = c1 if derived(c1) else c2
        return id(complex_) not in numbers

    def visit(inc: Incompatibility, conclusion: bool = False) -> None:
        numbered = conclusion or derivations[id(inc)] > 1
        conj = "So," if conclusion or inc is root_inc else "And"
        text = str(inc)
        conflict, other = inc.causes
        if derived(conflict) and derived(other):
            cl, ol = numbers.get(id(conflict)), numbers.get(id(other))
            if cl is not None and ol is not None:
                write(inc, "Because %s, %s." % (conflict.and_str(other, cl, ol), text), numbered)
            elif cl is not None or ol is not None:
                with_line, without = (conflict, other) if cl is not None else (other, conflict)
                visit(without)
                write(inc, "%s because %s (%d), %s." % (conj, with_line, numbers[id(with_line)], text), numbered)
            else:
                sl_conflict, sl_other = single_line(conflict), single_line(other)
                if sl_other or sl_conflict:
                    first, second = (conflict, other) if sl_other else (other, conflict)
                    visit(first)
                    visit(second)
                    write(inc, "Thus, %s." % text, numbered)
                else:
                    visit(conflict, conclusion=True)
                    lines.append(("", None))
                    visit(other)
                    write(inc, "%s because %s (%d), %s." % (conj, conflict, numbers[id(conflict)], text), numbered)
        elif derived(conflict) or derived(other):
            der, ext = (conflict, other) if derived(conflict) else (other, conflict)
            dl = numbers.get(id(der))
            if dl is not None:
                write(inc, "Because %s, %s." % (ext.and_str(der, None, dl), text), numbered)
            elif collapsible(der):
                dc1, dc2 = der.causes
                collapsed_der, collapsed_ext = (dc1, dc2) if derived(dc1) else (dc2, dc1)
                visit(collapsed_der)
                write(inc, "%s because %s, %s." % (conj, collapsed_ext.and_str(ext), text), numbered)
            else:
                visit(der)
                write(inc, "%s because %s, %s." % (conj, ext, text), numbered)
        else:
            write(inc, "Because %s, %s." % (conflict.and_str(other), text), numbered)

    count(root_inc)
    if root_inc.kind == "conflict":
        visit(root_inc)
    else:
        write(root_inc, "Because %s, version solving failed." % root_inc, False)
    pad = len("(%d) " % max(numbers.values())) if numbers else 0
    out: List[str] = []
    for msg, n in lines:
        if not msg:
            out.append("")
        elif n is not None:
            out.append(("(%d)" % n).ljust(pad) + msg)
        else:
            out.append(" " * pad + msg)
    return out


def parse_constraint(text: str) -> Range:
    """Parse "any", "1.2.3", "^1.2.3", ">=1.0.0", "<2.0.0", ">=1.0.0 <2.0.0"."""
    text = text.strip()
    if text == "any":
        return Range.full()
    if text.startswith("^"):
        return Range.caret(parse_version(text[1:]))
    rng = Range.full()
    for part in text.split():
        if part.startswith(">="):
            rng = rng.intersect(Range.between(parse_version(part[2:]), None))
        elif part.startswith("<"):
            rng = rng.intersect(Range.between(None, parse_version(part[1:])))
        else:
            rng = rng.intersect(Range.exact(parse_version(part)))
    return rng


def build_index(spec: Dict[str, Dict[str, Dict[str, str]]]) -> Index:
    return {p: {v: {d: parse_constraint(c) for d, c in deps.items()} for v, deps in vs.items()} for p, vs in spec.items()}
