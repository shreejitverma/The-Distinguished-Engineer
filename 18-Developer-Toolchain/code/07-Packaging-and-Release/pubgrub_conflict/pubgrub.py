"""A small, faithful PubGrub: unit propagation, CDCL conflict resolution, and pub's error writer.

It follows dart-lang/pub doc/solver.md and lib/src/solver/{version_solver,partial_solution,failure}.dart:

- A term is a set of outcomes for one package: the versions it may take plus NONE
  ("not selected"). A positive term excludes NONE; `not foo S` is the complement of `foo S`.
  The registry is finite, so terms are exact finite sets and every set operation is exact.
- Decision levels follow pub's PartialSolution: the root decision is level 1, so backjumping
  to level 1 (the default previousSatisfierLevel) keeps the root and its derivations.
- Package choice follows pub: fewest matching versions first, then the latest version.
- Dependencies of adjacent versions with the same constraint collapse into one incompatibility.

Simplifications, stated so nobody mistakes this for pub: one source per package, no SDK
constraints, no lockfile preferences, and _and_text() does not reproduce pub's
"depends on both" and "which depends on" phrasing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

NONE = "<none>"
ROOT_VERSION = "1.0.0"


def vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def matches(version: str, spec: str) -> bool:
    """Evaluate a comma-joined list of >=, <, <=, >, == clauses (a subset of PEP 440)."""
    if spec in ("", "*"):
        return True
    v = vkey(version)
    for clause in spec.split(","):
        clause = clause.strip()
        for op in (">=", "<=", "==", ">", "<"):
            if clause.startswith(op):
                bound = vkey(clause[len(op):])
                ok = {
                    ">=": v >= bound, "<=": v <= bound, "==": v == bound,
                    ">": v > bound, "<": v < bound,
                }[op]
                if not ok:
                    return False
                break
        else:
            raise ValueError(f"unsupported clause {clause!r}")
    return True


class Universe:
    """Known versions per package, plus the root, for one target platform."""

    def __init__(self, root: str, root_deps, registry, platform: str | None):
        self.root = root
        self.platform = platform
        self.versions: dict[str, list[str]] = {root: [ROOT_VERSION]}
        self.deps: dict[tuple[str, str], list[tuple[str, frozenset, str | None]]] = {}
        for name, vers in registry.items():
            self.versions[name] = sorted(vers, key=vkey)
        self.deps[(root, ROOT_VERSION)] = self._resolve_deps(root_deps)
        for name, vers in registry.items():
            for v, deps in vers.items():
                self.deps[(name, v)] = self._resolve_deps(deps)

    def _resolve_deps(self, deps):
        out = []
        for name, spec, plat in deps:
            if plat is not None and self.platform is not None and plat != self.platform:
                continue  # the PEP 508 marker evaluates to False on this platform
            if isinstance(spec, frozenset):  # an explicit version set, used by the relaxation search
                allowed = spec & frozenset(self.versions[name])
            else:
                allowed = frozenset(v for v in self.versions[name] if matches(v, spec))
            out.append((name, allowed, plat))
        return out

    def full(self, pkg: str) -> frozenset:
        return frozenset(self.versions[pkg]) | {NONE}

    # -- rendering -------------------------------------------------------------
    def ranges(self, pkg: str, allowed: frozenset) -> str:
        """Render a set of versions as the PEP 440 ranges a human would write."""
        vs = self.versions[pkg]
        idx = [i for i, v in enumerate(vs) if v in allowed]
        if len(idx) == len(vs):
            return "*"
        runs, start = [], None
        for k, i in enumerate(idx):
            if start is None:
                start = i
            if k + 1 == len(idx) or idx[k + 1] != i + 1:
                runs.append((start, i))
                start = None
        parts = []
        for lo, hi in runs:
            if lo == hi and 0 < lo and hi < len(vs) - 1:
                parts.append(f"=={vs[lo]}")
            elif lo == 0 and hi == len(vs) - 1:
                parts.append("*")
            elif lo == 0:
                parts.append(f"<{vs[hi + 1]}")
            elif hi == len(vs) - 1:
                parts.append(f">={vs[lo]}")
            elif lo == hi:
                parts.append(f"=={vs[lo]}")
            else:
                parts.append(f">={vs[lo]},<{vs[hi + 1]}")
        return " || ".join(parts)

    def term_text(self, pkg: str, s: frozenset) -> str:
        if pkg == self.root and NONE not in s:
            return pkg
        if NONE in s:
            return "not " + self.term_text(pkg, self.full(pkg) - s)
        r = self.ranges(pkg, s)
        if r == "*":
            return f"every version of {pkg}"
        return f"{pkg}{r}" if r[0] in "<>=" else f"{pkg} {r}"


@dataclass(eq=False)
class Incompat:
    terms: dict[str, frozenset]
    kind: str  # root | dependency | no-versions | derived
    causes: tuple["Incompat", "Incompat"] | None = None
    marker: str | None = None
    ident: int = 0


@dataclass
class Assignment:
    pkg: str
    s: frozenset
    decision: bool
    level: int
    cause: Incompat | None
    index: int


@dataclass
class Solver:
    u: Universe
    trace: list[str] = field(default_factory=list)
    incompats: dict[str, list[Incompat]] = field(default_factory=dict)
    all_incompats: list[Incompat] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)
    decisions: dict[str, str] = field(default_factory=dict)
    added_deps: set = field(default_factory=set)
    stats: dict[str, int] = field(default_factory=lambda: {"decisions": 0, "conflicts": 0, "backjumps": 0, "learned": 0})

    # -- incompatibilities -----------------------------------------------------
    def make(self, terms: list[tuple[str, frozenset]], kind, causes=None, marker=None) -> Incompat:
        merged: dict[str, frozenset] = {}
        for pkg, s in terms:
            merged[pkg] = merged[pkg] & s if pkg in merged else s
        if kind == "derived" and len(merged) > 1:
            root_s = merged.get(self.u.root)
            if root_s is not None and NONE not in root_s:
                del merged[self.u.root]  # pub drops always-true positive root terms
        return Incompat(merged, kind, causes, marker)

    def add(self, inc: Incompat) -> None:
        inc.ident = len(self.all_incompats) + 1
        self.all_incompats.append(inc)
        for pkg in inc.terms:
            self.incompats.setdefault(pkg, []).append(inc)

    def text(self, inc: Incompat) -> str:
        u = self.u
        t = inc.terms
        if inc.kind == "root":
            return f"{u.root} is the project being resolved"
        if inc.kind == "no-versions":
            (pkg, s), = t.items()
            return f"no versions of {pkg} match {u.ranges(pkg, s) if s else 'the empty set'}"
        if inc.kind == "dependency":
            (a, sa), (b, sb) = t.items()
            if NONE in sa:
                (a, sa), (b, sb) = (b, sb), (a, sa)
            allowed = u.full(b) - sb
            dep = b if allowed == frozenset(u.versions[b]) else u.term_text(b, allowed)
            when = f" (when sys_platform == '{inc.marker}')" if inc.marker else ""
            return f"{u.term_text(a, sa)} depends on {dep}{when}"
        # derived
        if not t:
            return "version solving failed"
        if len(t) == 1:
            (pkg, s), = t.items()
            if pkg == u.root and NONE not in s:
                return "version solving failed"
            if NONE in s:
                return f"{u.term_text(pkg, u.full(pkg) - s)} is required"
            return f"{u.term_text(pkg, s)} is forbidden"
        pos = [(p, s) for p, s in t.items() if NONE not in s]
        neg = [(p, s) for p, s in t.items() if NONE in s]
        if len(pos) == 1 and len(neg) == 1:
            (p, s), (q, r) = pos[0], neg[0]
            return f"{u.term_text(p, s)} requires {u.term_text(q, u.full(q) - r)}"
        if len(pos) == 2 and not neg:
            (p, s), (q, r) = pos
            return f"{u.term_text(p, s)} is incompatible with {u.term_text(q, r)}"
        return "one of " + ", ".join(u.term_text(p, s) for p, s in t.items()) + " must be false"

    def short(self, inc: Incompat) -> str:
        body = ", ".join(self.u.term_text(p, s) for p, s in inc.terms.items())
        return f"#{inc.ident} {{{body}}}"

    # -- partial solution ------------------------------------------------------
    def current(self, pkg: str, upto: int | None = None) -> frozenset:
        s = self.u.full(pkg)
        for a in self.assignments[: None if upto is None else upto + 1]:
            if a.pkg == pkg:
                s = s & a.s
        return s

    def level(self) -> int:
        return len(self.decisions)

    def assign(self, pkg, s, decision, cause) -> None:
        if decision:
            self.decisions[pkg] = next(iter(s - {NONE}))
        self.assignments.append(Assignment(pkg, s, decision, self.level(), cause, len(self.assignments)))

    def relation(self, inc: Incompat):
        """Return ("satisfied", None), ("almost", pkg), ("contradicted", None) or ("inconclusive", None)."""
        unsatisfied = None
        for pkg, t in inc.terms.items():
            a = self.current(pkg)
            if a <= t:
                continue
            if not (a & t):
                return "contradicted", None
            if unsatisfied is not None:
                return "inconclusive", None
            unsatisfied = pkg
        return ("satisfied", None) if unsatisfied is None else ("almost", unsatisfied)

    def satisfier(self, pkg: str, t: frozenset) -> Assignment:
        s = self.u.full(pkg)
        for a in self.assignments:
            if a.pkg == pkg:
                s = s & a.s
                if s <= t:
                    return a
        raise AssertionError(f"no satisfier for {pkg}")

    def backtrack(self, level: int) -> None:
        self.assignments = [a for a in self.assignments if a.level <= level]
        self.decisions = {a.pkg: next(iter(a.s - {NONE})) for a in self.assignments if a.decision}
        self.stats["backjumps"] += 1
        self.trace.append(f"  backjump to decision level {level}")

    # -- the algorithm ---------------------------------------------------------
    def propagate(self, pkg: str) -> None:
        changed = [pkg]
        while changed:
            p = changed.pop()
            for inc in reversed(list(self.incompats.get(p, []))):
                rel, unsat = self.relation(inc)
                if rel == "satisfied":
                    self.stats["conflicts"] += 1
                    self.trace.append(f"conflict: {self.short(inc)}")
                    root_cause = self.resolve_conflict(inc)
                    rel2, unsat2 = self.relation(root_cause)
                    assert rel2 == "almost", rel2
                    self.derive(unsat2, root_cause)
                    changed = [unsat2]
                    break
                if rel == "almost":
                    self.derive(unsat, inc)
                    if unsat not in changed:
                        changed.append(unsat)

    def derive(self, pkg: str, inc: Incompat) -> None:
        s = self.u.full(pkg) - inc.terms[pkg]
        self.assign(pkg, s, False, inc)
        self.trace.append(f"  derive {self.u.term_text(pkg, s)}  (from #{inc.ident}, level {self.level()})")

    def resolve_conflict(self, inc: Incompat) -> Incompat:
        new = False
        while True:
            t = inc.terms
            if not t or (len(t) == 1 and self.u.root in t and NONE not in t[self.u.root]):
                raise SolveFailure(inc)
            most_recent = most_term = difference = None
            prev_level = 1
            for pkg, term in t.items():
                sat = self.satisfier(pkg, term)
                if most_recent is None:
                    most_recent, most_term = sat, pkg
                elif most_recent.index < sat.index:
                    prev_level = max(prev_level, most_recent.level)
                    most_recent, most_term, difference = sat, pkg, None
                else:
                    prev_level = max(prev_level, sat.level)
                if most_term == pkg:
                    diff = most_recent.s - term
                    difference = diff or None
                    if difference is not None:
                        inv = self.u.full(pkg) - difference
                        prev_level = max(prev_level, self.satisfier(pkg, inv).level)
            kind = "decision" if most_recent.decision else "derivation"
            if prev_level < most_recent.level or most_recent.decision:
                self.trace.append(
                    f"  satisfier {self.u.term_text(most_recent.pkg, most_recent.s)} is a {kind} at level"
                    f" {most_recent.level}; previous satisfier level {prev_level}"
                )
                self.backtrack(prev_level)
                if new:
                    self.add(inc)
                    self.stats["learned"] += 1
                    self.trace.append(f"  learn {self.short(inc)}: {self.text(inc)}")
                return inc
            cause = most_recent.cause
            terms = [(p, s) for p, s in t.items() if p != most_term]
            terms += [(p, s) for p, s in cause.terms.items() if p != most_recent.pkg]
            if difference is not None:
                terms.append((most_recent.pkg, self.u.full(most_recent.pkg) - difference))
            prior = self.make(terms, "derived", (inc, cause))
            self.trace.append(
                f"  resolve with #{cause.ident} on {most_recent.pkg}"
                f" -> {{{', '.join(self.u.term_text(p, s) for p, s in prior.terms.items())}}}"
            )
            inc, new = prior, True

    def dependency_incompats(self, pkg: str, version: str) -> list[Incompat]:
        """Collapse each dependency over the contiguous run of versions that declare it identically."""
        out = []
        vs = self.u.versions[pkg]
        i = vs.index(version)
        for dep, allowed, marker in self.u.deps[(pkg, version)]:
            def same(v):
                return (dep, allowed, marker) in self.u.deps[(pkg, v)]
            lo = i
            while lo > 0 and same(vs[lo - 1]):
                lo -= 1
            hi = i
            while hi < len(vs) - 1 and same(vs[hi + 1]):
                hi += 1
            depender = frozenset(vs[lo : hi + 1])
            out.append(self.make([(pkg, depender), (dep, self.u.full(dep) - allowed)], "dependency", marker=marker))
        return out

    def choose(self) -> str | None:
        seen, pending = set(), []
        for a in self.assignments:
            if a.pkg in seen or a.pkg in self.decisions:
                continue
            if NONE not in self.current(a.pkg):
                seen.add(a.pkg)
                pending.append(a.pkg)
        if not pending:
            return None

        def count(p):
            return sum(1 for v in self.u.versions[p] if v in self.current(p))

        pkg = min(pending, key=count)  # pub: fewest matching versions; ties keep first-seen order
        term = self.current(pkg)
        candidates = [v for v in self.u.versions[pkg] if v in term]
        if not candidates:
            inc = self.make([(pkg, term)], "no-versions")
            self.add(inc)
            self.trace.append(f"no versions: {self.short(inc)}")
            return pkg
        version = candidates[-1]
        conflict = False
        for inc in self.dependency_incompats(pkg, version):
            key = (frozenset(inc.terms.items()), inc.marker)
            if key not in self.added_deps:
                self.added_deps.add(key)
                self.add(inc)
                self.trace.append(f"  add {self.short(inc)}: {self.text(inc)}")
            if all(p == pkg or self.current(p) <= s for p, s in inc.terms.items()):
                conflict = True
        if not conflict:
            self.assign(pkg, frozenset([version]), True, None)
            self.stats["decisions"] += 1
            label = pkg if pkg != self.u.root else f"{pkg} (root)"
            self.trace.append(f"decide {label} {version}  (level {self.level()})")
        return pkg

    def solve(self) -> dict[str, str]:
        root = self.u.root
        self.add(self.make([(root, self.u.full(root) - {ROOT_VERSION})], "root"))
        nxt: str | None = root
        while nxt is not None:
            self.propagate(nxt)
            nxt = self.choose()
        return {p: v for p, v in self.decisions.items() if p != root}


class SolveFailure(Exception):
    def __init__(self, inc: Incompat):
        super().__init__("version solving failed")
        self.incompat = inc


# -- pub's error writer (failure.dart _Writer), ported -------------------------
class Writer:
    def __init__(self, solver: Solver, root: Incompat):
        self.s = solver
        self.root = root
        self.derivations: dict[int, int] = {}
        self.lines: list[tuple[str, int | None]] = []
        self.numbers: dict[int, int] = {}
        self._count(root)

    def _count(self, inc: Incompat) -> None:
        if id(inc) in self.derivations:
            self.derivations[id(inc)] += 1
            return
        self.derivations[id(inc)] = 1
        if inc.causes:
            self._count(inc.causes[0])
            self._count(inc.causes[1])

    def write(self) -> str:
        if self.root.causes:
            self._visit(self.root)
        else:
            self._write(self.root, f"Because {self.s.text(self.root)}, version solving failed.")
        pad = len(f"({max(self.numbers.values())}) ") if self.numbers else 0
        out, last_empty = [], False
        for msg, num in self.lines:
            if not msg:
                if not last_empty:
                    out.append("")
                last_empty = True
                continue
            last_empty = False
            prefix = f"({num})".ljust(pad) if num is not None else " " * pad
            out.append(prefix + msg)
        return "\n".join(out)

    def _write(self, inc: Incompat, msg: str, numbered: bool = False) -> None:
        if numbered:
            n = len(self.numbers) + 1
            self.numbers[id(inc)] = n
            self.lines.append((msg, n))
        else:
            self.lines.append((msg, None))

    def _and_text(self, a: Incompat, b: Incompat, la: int | None = None, lb: int | None = None) -> str:
        ta = self.s.text(a) + (f" ({la})" if la else "")
        tb = self.s.text(b) + (f" ({lb})" if lb else "")
        return f"{ta} and {tb}"

    @staticmethod
    def _single_line(inc: Incompat) -> bool:
        return inc.causes[0].causes is None and inc.causes[1].causes is None

    def _collapsible(self, inc: Incompat) -> bool:
        if self.derivations[id(inc)] > 1:
            return False
        c1, c2 = inc.causes
        if c1.causes and c2.causes:
            return False
        if not c1.causes and not c2.causes:
            return False
        complex_ = c1 if c1.causes else c2
        return id(complex_) not in self.numbers

    def _visit(self, inc: Incompat, conclusion: bool = False) -> None:
        numbered = conclusion or self.derivations[id(inc)] > 1
        conj = "So," if conclusion or inc is self.root else "And"
        text = self.s.text(inc)
        c1, c2 = inc.causes
        if c1.causes and c2.causes:
            l1, l2 = self.numbers.get(id(c1)), self.numbers.get(id(c2))
            if l1 and l2:
                self._write(inc, f"Because {self._and_text(c1, c2, l1, l2)}, {text}.", numbered)
            elif l1 or l2:
                with_line, without = (c1, c2) if l1 else (c2, c1)
                self._visit(without)
                line = l1 or l2
                self._write(inc, f"{conj} because {self.s.text(with_line)} ({line}), {text}.", numbered)
            else:
                single1, single2 = self._single_line(c1), self._single_line(c2)
                if single1 or single2:
                    first, second = (c1, c2) if single2 else (c2, c1)
                    self._visit(first)
                    self._visit(second)
                    self._write(inc, f"Thus, {text}.", numbered)
                else:
                    self._visit(c1, conclusion=True)
                    self.lines.append(("", None))
                    self._visit(c2)
                    self._write(
                        inc, f"{conj} because {self.s.text(c1)} ({self.numbers[id(c1)]}), {text}.", numbered
                    )
        elif c1.causes or c2.causes:
            derived, ext = (c1, c2) if c1.causes else (c2, c1)
            dl = self.numbers.get(id(derived))
            if dl:
                self._write(inc, f"Because {self._and_text(ext, derived, None, dl)}, {text}.", numbered)
            elif self._collapsible(derived):
                d1, d2 = derived.causes
                collapsed_derived, collapsed_ext = (d1, d2) if d1.causes else (d2, d1)
                self._visit(collapsed_derived)
                self._write(inc, f"{conj} because {self._and_text(collapsed_ext, ext)}, {text}.", numbered)
            else:
                self._visit(derived)
                self._write(inc, f"{conj} because {self.s.text(ext)}, {text}.", numbered)
        else:
            self._write(inc, f"Because {self._and_text(c1, c2)}, {text}.", numbered)


def external_leaves(inc: Incompat) -> list[Incompat]:
    """The external incompatibilities at the leaves of a derivation graph, in first-seen order."""
    out, seen, stack = [], set(), [inc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if cur.causes:
            stack.extend(reversed(cur.causes))
        else:
            out.append(cur)
    return out


def solve(root, root_deps, registry, platform):
    """Return (solver, solution or None, failure incompatibility or None)."""
    s = Solver(Universe(root, root_deps, registry, platform))
    try:
        return s, s.solve(), None
    except SolveFailure as f:
        return s, None, f.incompat
