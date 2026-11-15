"""Drive the from-scratch resolvers and check every result.

Sections:
  1. The six worked examples from dart-lang/pub doc/solver.md.
  2. Russ Cox's 3-SAT -> version-selection reduction, both formulas from "Version SAT".
  3. Clause learning versus chronological backtracking on a thrashing family.
  4. Minimal version selection on the Go Modules Reference example.

Every check raises on a mismatch, so a clean run means all of them passed.
"""

from __future__ import annotations

import itertools
import json
import sys
from typing import Dict, List, Tuple

from mvs import build_list
from naive import NaiveSolver
from pubgrub import SolveFailure, Solver, build_index, explain, fmt_version


def check(cond: bool, what: str) -> None:
    if not cond:
        raise SystemExit("CHECK FAILED: " + what)


def fmt_solution(sol: Dict) -> str:
    return ", ".join("%s %s" % (p, fmt_version(v)) for p, v in sorted(sol.items()))


# ---------------------------------------------------------------------------
# 1. pub doc/solver.md examples
# ---------------------------------------------------------------------------

V1 = {"root": {"1.0.0": {}}}

EXAMPLES: List[Tuple[str, Dict, object]] = [
    (
        "No Conflicts",
        {
            "root": {"1.0.0": {"foo": "^1.0.0"}},
            "foo": {"1.0.0": {"bar": "^1.0.0"}},
            "bar": {"1.0.0": {}, "2.0.0": {}},
        },
        {"root": "1.0.0", "foo": "1.0.0", "bar": "1.0.0"},
    ),
    (
        "Avoiding Conflict During Decision Making",
        {
            "root": {"1.0.0": {"foo": "^1.0.0", "bar": "^1.0.0"}},
            "foo": {"1.0.0": {}, "1.1.0": {"bar": "^2.0.0"}},
            "bar": {"1.0.0": {}, "1.1.0": {}, "2.0.0": {}},
        },
        {"root": "1.0.0", "foo": "1.0.0", "bar": "1.1.0"},
    ),
    (
        "Performing Conflict Resolution",
        {
            "root": {"1.0.0": {"foo": ">=1.0.0"}},
            "foo": {"1.0.0": {}, "2.0.0": {"bar": "^1.0.0"}},
            "bar": {"1.0.0": {"foo": "^1.0.0"}},
        },
        {"root": "1.0.0", "foo": "1.0.0"},
    ),
    (
        "Conflict Resolution With a Partial Satisfier",
        {
            "root": {"1.0.0": {"foo": "^1.0.0", "target": "^2.0.0"}},
            "foo": {"1.0.0": {}, "1.1.0": {"left": "^1.0.0", "right": "^1.0.0"}},
            "left": {"1.0.0": {"shared": ">=1.0.0"}},
            "right": {"1.0.0": {"shared": "<2.0.0"}},
            "shared": {"1.0.0": {"target": "^1.0.0"}, "2.0.0": {}},
            "target": {"1.0.0": {}, "2.0.0": {}},
        },
        {"root": "1.0.0", "foo": "1.0.0", "target": "2.0.0"},
    ),
    (
        "Partial Satisfier, variant with shared 0.5.0 so shared 1.0.0 collapses to ^1.0.0",
        {
            "root": {"1.0.0": {"foo": "^1.0.0", "target": "^2.0.0"}},
            "foo": {"1.0.0": {}, "1.1.0": {"left": "^1.0.0", "right": "^1.0.0"}},
            "left": {"1.0.0": {"shared": ">=1.0.0"}},
            "right": {"1.0.0": {"shared": "<2.0.0"}},
            "shared": {"0.5.0": {}, "1.0.0": {"target": "^1.0.0"}, "2.0.0": {}},
            "target": {"1.0.0": {}, "2.0.0": {}},
        },
        {"root": "1.0.0", "foo": "1.0.0", "target": "2.0.0"},
    ),
    (
        "Linear Error Reporting",
        {
            "root": {"1.0.0": {"foo": "^1.0.0", "baz": "^1.0.0"}},
            "foo": {"1.0.0": {"bar": "^2.0.0"}},
            "bar": {"2.0.0": {"baz": "^3.0.0"}},
            "baz": {"1.0.0": {}, "3.0.0": {}},
        },
        [
            "Because every version of foo depends on bar ^2.0.0 which depends on baz ^3.0.0, every version of foo requires baz ^3.0.0.",
            "So, because root depends on both baz ^1.0.0 and foo ^1.0.0, version solving failed.",
        ],
    ),
    (
        "Branching Error Reporting",
        {
            "root": {"1.0.0": {"foo": "^1.0.0"}},
            "foo": {"1.0.0": {"a": "^1.0.0", "b": "^1.0.0"}, "1.1.0": {"x": "^1.0.0", "y": "^1.0.0"}},
            "a": {"1.0.0": {"b": "^2.0.0"}},
            "b": {"1.0.0": {}, "2.0.0": {}},
            "x": {"1.0.0": {"y": "^2.0.0"}},
            "y": {"1.0.0": {}, "2.0.0": {}},
        },
        [
            "Because foo <1.1.0 depends on a ^1.0.0 which depends on b ^2.0.0, foo <1.1.0 requires b ^2.0.0.",
            "(1) So, because foo <1.1.0 depends on b ^1.0.0, foo <1.1.0 is forbidden.",
            "",
            "Because foo >=1.1.0 depends on x ^1.0.0 which depends on y ^2.0.0, foo >=1.1.0 requires y ^2.0.0.",
            "And because foo >=1.1.0 depends on y ^1.0.0, foo >=1.1.0 is forbidden.",
            "And because foo <1.1.0 is forbidden (1), foo is forbidden.",
            "So, because root depends on foo ^1.0.0, version solving failed.",
        ],
    ),
]


def section_examples() -> None:
    print("== 1. pub doc/solver.md examples (from-scratch PubGrub) ==")
    for title, spec, expect in EXAMPLES:
        s = Solver(build_index(spec), "root")
        print()
        print("-- %s --" % title)
        try:
            sol = s.solve()
        except SolveFailure as e:
            report = explain(e.incompatibility)
            for line in report:
                print("  " + line if line else "")
            if isinstance(expect, list) and len(expect) > 2:
                check([l.strip() for l in report] == expect, "%s report differs from solver.md" % title)
            elif isinstance(expect, list):
                # solver.md's trace decides foo before baz; ours (and the order pub's code
                # iterates) decides baz first, which only swaps the two operands of "both".
                swapped = [expect[0], expect[1].replace("baz ^1.0.0 and foo ^1.0.0", "foo ^1.0.0 and baz ^1.0.0")]
                check([l.strip() for l in report] in (expect, swapped), "%s report differs from solver.md" % title)
            else:
                raise SystemExit("CHECK FAILED: %s has no expected report" % title)
            print("  stats: decisions=%d learned=%d conflicts=%d" % (s.decisions_made, s.learned, s.conflicts))
            continue
        check(isinstance(expect, dict), "%s solved but solver.md says it fails" % title)
        want = {p: v for p, v in expect.items()}  # type: ignore[union-attr]
        got = {p: fmt_version(v) for p, v in sol.items()}
        check(got == want, "%s: got %s, solver.md says %s" % (title, got, want))
        print("  solution: %s" % fmt_solution(sol))
        for learned in s.learned_list:
            print("  learned: %s" % learned)
        print("  stats: decisions=%d learned=%d conflicts=%d" % (s.decisions_made, s.learned, s.conflicts))
    print()


# ---------------------------------------------------------------------------
# 2. 3-SAT -> VERSION (Russ Cox, "Version SAT", 2016)
# ---------------------------------------------------------------------------

# Literal +j means x_j, -j means not x_j.
F1 = [(-1, -2, -3), (-2, -3, -4), (-2, -2, 3), (2, 2, 2)]
F2 = F1 + [(1, -2, 4)]


def reduce_3sat(clauses: List[Tuple[int, int, int]], nvars: int) -> Dict:
    # X_j:0 is version 1.0.0 and X_j:1 is version 2.0.0; C_i:k is version (k+1).0.0.
    spec: Dict = {"F": {"1.0.0": {"C%d" % (i + 1): "any" for i in range(len(clauses))}}}
    for j in range(1, nvars + 1):
        spec["X%d" % j] = {"1.0.0": {}, "2.0.0": {}}
    for i, clause in enumerate(clauses):
        spec["C%d" % (i + 1)] = {
            "%d.0.0" % (k + 1): {"X%d" % abs(lit): ("2.0.0" if lit > 0 else "1.0.0")} for k, lit in enumerate(clause)
        }
    return spec


def satisfies(clauses, assign: Dict[int, bool]) -> bool:
    return all(any(assign[abs(l)] == (l > 0) for l in c) for c in clauses)


def section_sat() -> None:
    print("== 2. 3-SAT reduced to version selection (Cox, Version SAT) ==")
    for name, clauses in (("F1 (4 clauses)", F1), ("F2 (F1 plus (x1 or not x2 or x4))", F2)):
        n = 4
        brute = [a for a in itertools.product([False, True], repeat=n) if satisfies(clauses, dict(enumerate(a, 1)))]
        spec = reduce_3sat(clauses, n)
        packages = len(spec)
        versions = sum(len(v) for v in spec.values())
        s = Solver(build_index(spec), "F")
        print()
        print("-- %s: %d packages, %d package versions --" % (name, packages, versions))
        print("  brute force over 2^%d assignments: %d satisfying" % (n, len(brute)))
        try:
            sol = s.solve()
        except SolveFailure as e:
            check(not brute, "%s: PubGrub says UNSAT but brute force found %d" % (name, len(brute)))
            report = explain(e.incompatibility)
            print("  PubGrub: UNSAT, derivation report has %d lines:" % len(report))
            for line in report:
                print("    " + line if line else "")
            print("  stats: learned=%d conflicts=%d" % (s.learned, s.conflicts))
            continue
        assign = {j: sol["X%d" % j] == (2, 0, 0) for j in range(1, n + 1) if "X%d" % j in sol}
        # A variable no clause forced is left uninstalled; either value then works.
        full = {j: assign.get(j, False) for j in range(1, n + 1)}
        check(satisfies(clauses, full), "%s: decoded assignment does not satisfy the formula" % name)
        check(tuple(full[j] for j in range(1, n + 1)) in brute, "%s: assignment not in brute-force set" % name)
        print("  PubGrub: SAT, x1..x4 = %s (read from X_j:1 installed)" %
              " ".join("1" if full[j] else "0" for j in range(1, n + 1)))
        print("  installed: %s" % fmt_solution(sol))
        print("  stats: learned=%d conflicts=%d" % (s.learned, s.conflicts))
    print()


# ---------------------------------------------------------------------------
# 3. Thrashing: learning versus chronological backtracking
# ---------------------------------------------------------------------------


def thrash_spec(n: int, sat: bool) -> Dict:
    # x 2.0.0 needs z ^2.0.0, which does not exist; a and b are irrelevant
    # packages with n versions each.  With sat=False, x 1.0.0 needs it too.
    vs = {"%d.0.0" % k: {} for k in range(1, n + 1)}
    return {
        "root": {"1.0.0": {"x": "any", "a": "any", "b": "any"}},
        "x": {"1.0.0": {} if sat else {"z": "^2.0.0"}, "2.0.0": {"z": "^2.0.0"}},
        "a": dict(vs),
        "b": dict(vs),
        "z": {"1.0.0": {}},
    }


def section_thrash() -> None:
    print("== 3. Clause learning versus chronological backtracking ==")
    print("  root -> x any, a any, b any; x 2.0.0 -> z ^2.0.0 (no such z); a and b have n versions each")
    print()
    print("  %-4s %-3s | %-21s | %s" % ("n", "sat", "naive: versions tried", "PubGrub: versions loaded / conflicts"))
    for sat in (True, False):
        for n in (5, 10, 20, 40):
            spec = thrash_spec(n, sat)
            naive = NaiveSolver(build_index(spec), "root")
            nres = naive.solve()
            pg = Solver(build_index(spec), "root")
            try:
                pres = pg.solve()
            except SolveFailure:
                pres = None
            check((nres is None) == (pres is None), "solvers disagree on satisfiability at n=%d" % n)
            if pres is not None:
                check({p: v for p, v in nres.items()} == pres, "solvers chose different solutions at n=%d" % n)
            if not sat:
                check(naive.tried >= n * n, "naive solver did not thrash at n=%d" % n)
            check(pg.dep_lookups <= 6, "PubGrub loaded too many versions at n=%d" % n)
            print("  %-4d %-3s | %-21d | %d / %d" % (n, "yes" if sat else "no", naive.tried, pg.dep_lookups, pg.conflicts))
    print()


# ---------------------------------------------------------------------------
# 4. Minimal version selection: the Go Modules Reference example
# ---------------------------------------------------------------------------

GO_GRAPH = {
    ("main", ""): [("A", "1.2"), ("B", "1.2")],
    ("A", "1.2"): [("C", "1.3")],
    ("B", "1.2"): [("C", "1.4")],
    ("C", "1.3"): [("D", "1.2")],
    ("C", "1.4"): [("D", "1.2")],
    # Published but never required: MVS must neither load nor select them.
    ("B", "1.3"): [("C", "1.4")],
    ("D", "1.3"): [],
    ("D", "1.2"): [],
}


def mvs_str(sel) -> str:
    return " ".join("%s %d.%d" % (m, v[0], v[1]) for m, v in sorted(sel.items()))


def section_mvs() -> Dict[str, str]:
    print("== 4. Minimal version selection (Go Modules Reference example) ==")
    sel, loads = build_list(GO_GRAPH, "main")
    check(mvs_str(sel) == "A 1.2 B 1.2 C 1.4 D 1.2", "MVS build list differs from go.dev/ref/mod")
    print("  build list: %s  (go.mod files loaded: %d of %d published)" % (mvs_str(sel), loads, len(GO_GRAPH) - 1))
    ex, _ = build_list(GO_GRAPH, "main", excluded={("C", "1.4")})
    check(mvs_str(ex) == "A 1.2 B 1.2 C 1.3 D 1.2", "exclude semantics changed")
    print("  exclude C 1.4 (Go 1.16+ rule: requirement ignored): %s" % mvs_str(ex))
    print()
    return {"plain": mvs_str(sel), "exclude": mvs_str(ex)}


def main() -> None:
    section_examples()
    section_sat()
    section_thrash()
    expected_go = section_mvs()
    with open(sys.argv[1], "w") as fh:
        json.dump(expected_go, fh)


if __name__ == "__main__":
    main()
