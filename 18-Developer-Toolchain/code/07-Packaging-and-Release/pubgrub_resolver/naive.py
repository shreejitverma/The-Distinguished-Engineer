"""Baseline: chronological backtracking with no learning.

Requirements are processed first-in first-out in the order they are
discovered; each package takes its newest version that satisfies every
requirement collected so far.  On a dead end the solver undoes the most
recent choice and tries that package's next older version.  Nothing is
remembered across branches, so the same conflict is rediscovered under
every combination of earlier, unrelated choices.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from pubgrub import Index, Range, Version, parse_version


class NaiveSolver:
    def __init__(self, index: Index, root: str) -> None:
        self.root = root
        self.versions = {p: sorted((parse_version(v) for v in vs), reverse=True) for p, vs in index.items()}
        self.deps = {(p, parse_version(v)): d for p, vs in index.items() for v, d in vs.items()}
        self.tried = 0  # candidate versions examined

    def solve(self, root_version: str = "1.0.0") -> Optional[Dict[str, Version]]:
        rv = parse_version(root_version)
        queue: List[Tuple[str, Range]] = list(self.deps[(self.root, rv)].items())
        return self._search({self.root: rv}, queue)

    def _search(self, chosen: Dict[str, Version], queue: List[Tuple[str, Range]]) -> Optional[Dict[str, Version]]:
        # Drop requirements already satisfied by an earlier choice; fail on a violated one.
        while queue:
            pkg, rng = queue[0]
            if pkg not in chosen:
                break
            if not rng.contains(chosen[pkg]):
                return None
            queue = queue[1:]
        if not queue:
            return dict(chosen)
        pkg = queue[0][0]
        constraint = Range.full()
        for p, r in queue:
            if p == pkg:
                constraint = constraint.intersect(r)
        for v in self.versions.get(pkg, []):
            if not constraint.contains(v):
                continue
            self.tried += 1
            chosen[pkg] = v
            found = self._search(chosen, queue[1:] + list(self.deps[(pkg, v)].items()))
            if found is not None:
                return found
            del chosen[pkg]
        return None
