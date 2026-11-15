"""Minimal version selection (Russ Cox, research.swtch.com/vgo-mvs; go.dev/ref/mod).

A requirement names a minimum version only.  The build list is the
maximum required version of every module reachable from the main module,
found by one graph traversal that visits each module version at most once.
"""

from __future__ import annotations

from typing import Dict, List, Set, Tuple

from pubgrub import Version, parse_version

Graph = Dict[Tuple[str, str], List[Tuple[str, str]]]  # (module, version) -> requirements


def build_list(graph: Graph, main: str, excluded: Set[Tuple[str, str]] = frozenset()) -> Tuple[Dict[str, Version], int]:
    """Return (selected versions, number of go.mod files loaded).

    Exclusions follow Go 1.16+ semantics from the `exclude directive`
    section of the Go Modules Reference: a requirement on an excluded
    version is ignored, not redirected.
    """
    selected: Dict[str, Version] = {}
    seen: Set[Tuple[str, str]] = set()
    stack = [(main, "")]
    loads = 0
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        loads += 1
        for mod, ver in graph.get(node, []):
            if (mod, ver) in excluded:
                continue
            v = parse_version(ver)
            if mod not in selected or selected[mod] < v:
                selected[mod] = v
            stack.append((mod, ver))
    return selected, loads - 1  # the main module's own go.mod is not a dependency load
