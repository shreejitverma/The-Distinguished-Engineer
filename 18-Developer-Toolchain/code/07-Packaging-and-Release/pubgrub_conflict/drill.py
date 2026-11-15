"""Stages 2-5 of the drill: solve the toy registry with pubgrub.py and compute minimal relaxations.

Usage: python3 drill.py FIXES_JSON

Writes every single-edge relaxation that makes the Linux resolution solvable to FIXES_JSON,
so uv_stage.py can replay each one through the real uv resolver.
"""

from __future__ import annotations

import copy
import json
import sys

import pubgrub as pg
from registry import PLATFORMS, REGISTRY, ROOT, ROOT_DEPS


def banner(text: str) -> None:
    print(f"=== {text} ===")


def spec_for(versions: list[str], allowed: set[str]) -> str:
    """Tightest PEP 440 range covering a contiguous run of known versions."""
    idx = sorted(versions.index(v) for v in allowed)
    if idx != list(range(idx[0], idx[-1] + 1)):
        raise ValueError("widened set is not contiguous")
    spec = f">={versions[idx[0]]}"
    if idx[-1] + 1 < len(versions):
        spec += f",<{versions[idx[-1] + 1]}"
    return spec


def apply_edge(depender: str, versions: list[str], dep: str, new_spec):
    """Return (root_deps, registry) with one dependency edge replaced."""
    root_deps = copy.deepcopy(ROOT_DEPS)
    registry = copy.deepcopy(REGISTRY)
    if depender == ROOT:
        root_deps = [(n, new_spec if n == dep else s, p) for n, s, p in root_deps]
    else:
        for v in versions:
            registry[depender][v] = [(n, new_spec if n == dep else s, p) for n, s, p in registry[depender][v]]
    return root_deps, registry


def edge_of(solver: pg.Solver, inc: pg.Incompat):
    (a, sa), (b, sb) = inc.terms.items()
    if pg.NONE in sa:
        (a, sa), (b, sb) = (b, sb), (a, sa)
    depender_versions = sorted(sa, key=pg.vkey) if a != ROOT else [pg.ROOT_VERSION]
    allowed = solver.u.full(b) - sb - {pg.NONE}
    return a, depender_versions, b, allowed


def solvable(root_deps, registry, platform) -> dict | None:
    _, sol, _ = pg.solve(ROOT, root_deps, registry, platform)
    return sol


def fmt_solution(sol: dict[str, str]) -> str:
    return " ".join(f"{k}=={v}" for k, v in sorted(sol.items()))


def main() -> int:
    fixes_path = sys.argv[1]

    banner("Stage 2: the toy PubGrub reproduces the platform split")
    results = {}
    for plat in PLATFORMS:
        solver, sol, fail = pg.solve(ROOT, ROOT_DEPS, REGISTRY, plat)
        results[plat] = (solver, sol, fail)
        st = solver.stats
        verdict = fmt_solution(sol) if sol else "NO SOLUTION"
        print(f"{plat:7} {verdict}")
        print(
            f"        decisions={st['decisions']} conflicts={st['conflicts']}"
            f" backjumps={st['backjumps']} incompatibilities={len(solver.all_incompats)}"
        )
    solver, _, fail = results["linux"]
    assert fail is not None, "the Linux resolution must fail, or the drill has no symptom"
    assert results["darwin"][1] is not None, "the macOS resolution must succeed"

    banner("Stage 3: the solver trace on linux (decisions, propagation, conflicts, backjumps)")
    for line in solver.trace:
        print(line)

    banner("Stage 4: the derivation graph and pub's error writer")
    def walk(inc, depth, seen):
        tag = "derived " if inc.causes else "external"
        ref = f"#{inc.ident}" if inc.ident else "-"  # the failing chain is never stored, only returned
        terms = ", ".join(solver.u.term_text(p, s) for p, s in inc.terms.items()) or "(empty)"
        print(f"{'  ' * depth}{tag} {ref:3} {{{terms}}}")
        if inc.causes and id(inc) not in seen:
            seen.add(id(inc))
            for c in inc.causes:
                walk(c, depth + 1, seen)
    walk(fail, 0, set())
    print("--- report ---")
    print(pg.Writer(solver, fail).write())

    leaves = [x for x in pg.external_leaves(fail) if x.kind == "dependency"]
    in_proof = {id(x) for x in leaves}
    unused = [x for x in solver.all_incompats if x.kind == "dependency" and id(x) not in in_proof]
    print(f"--- external facts: {len(leaves)} in the proof, {len(unused)} loaded but irrelevant ---")
    for x in unused:
        print(f"irrelevant: {solver.text(x)}")

    banner("Stage 5: every fact in the proof is necessary, and the cheapest widening of each")
    fixes = []
    for inc in leaves:
        depender, dvers, dep, allowed = edge_of(solver, inc)
        all_dep = solver.u.versions[dep]
        # Necessity: drop the constraint entirely; if Linux is still unsolvable, the fact was not needed.
        rd, rg = apply_edge(depender, dvers, dep, frozenset(all_dep))
        necessary = solvable(rd, rg, "linux") is not None
        # Cheapest widening: admit the excluded versions nearest the allowed run, one at a time.
        inside = [all_dep.index(v) for v in allowed]
        outside = sorted(
            (v for v in all_dep if v not in allowed),
            key=lambda v: (min(abs(all_dep.index(v) - i) for i in inside), all_dep.index(v)),
        )
        widened, sol = set(allowed), None
        for v in outside:
            widened.add(v)
            rd, rg = apply_edge(depender, dvers, dep, frozenset(widened))
            sol = solvable(rd, rg, "linux")
            if sol is not None:
                break
        owner = "project" if depender == ROOT else "third-party"
        edge = solver.text(inc)
        print(f"fact: {edge}")
        print(f"  necessary for the conflict: {'yes' if necessary else 'no'}")
        if sol is None:
            print("  no widening of this edge alone makes linux solvable")
            continue
        new_spec = spec_for(all_dep, widened)
        admitted = sorted(widened - allowed, key=pg.vkey)
        darwin = solvable(*apply_edge(depender, dvers, dep, frozenset(widened)), "darwin")
        print(f"  owner: {owner}; widen {dep} to {new_spec} (admits {', '.join(admitted)})")
        print(f"  linux:  {fmt_solution(sol)}")
        print(f"  darwin: {fmt_solution(darwin) if darwin else 'NO SOLUTION'}")
        declared = ROOT_DEPS if depender == ROOT else REGISTRY[depender][dvers[0]]
        marker = next(p for n, _, p in declared if n == dep)
        fixes.append({
            "owner": owner, "depender": depender, "versions": dvers, "dep": dep,
            "spec": new_spec, "marker": marker, "linux": sol, "admitted": admitted,
        })
    with open(fixes_path, "w", encoding="ascii") as fh:
        json.dump(fixes, fh, indent=1, sort_keys=True)
    print(f"--- {len(fixes)} single-edge relaxations, each admitting "
          f"{min(len(f['admitted']) for f in fixes)} version(s) ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
